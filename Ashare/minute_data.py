"""Strict five-minute TradingDatas evidence contract for the A-share lane.

The module is deliberately provider-neutral and mock-first.  A dataset profile
must be derived from one frozen ``GET /v1/catalog`` response.  Rows are then
read only through ``POST /v1/query`` and are bound to the response-envelope
receipt, lineage, freshness and observation timestamps.

No production dataset ID, transport, SQLite path, legacy route, fallback, or
trading authority exists here.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from typing import Any, Mapping, Protocol
from zoneinfo import ZoneInfo

from shared.data.sharedsignals_v1 import (
    CatalogEnvelope,
    QueryRequest,
    SharedSignalsV1Client,
    SharedSignalsV1Error,
)
from shared.data.tradingdatas_pagination import (
    PagedQueryRun,
    PaginationContractError,
    collect_query_pages,
)
from shared.universe.policy import is_mainboard_tradable


SHANGHAI = ZoneInfo("Asia/Shanghai")
FIVE_MINUTES = timedelta(minutes=5)
MAX_MINUTE_DATA_LATENCY = timedelta(seconds=30)
FIXED_CATALOG_ROUTE = "GET /v1/catalog"
FIXED_QUERY_ROUTE = "POST /v1/query"

_SHA256_HEX = frozenset("0123456789abcdef")


class MinuteDataContractError(ValueError):
    """Fail-closed minute-data contract failure with a stable reason code."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise MinuteDataContractError("minute_payload_not_canonical") from exc


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _text(value: object, reason: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise MinuteDataContractError(reason)
    return value


def _aware(value: object, reason: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise MinuteDataContractError(reason)
    return value


def _parse_aware_iso(value: object, reason: str) -> datetime:
    raw = _text(value, reason)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MinuteDataContractError(reason) from exc
    return _aware(parsed, reason)


def _finite(value: object, reason: str, *, positive: bool = False) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or (float(value) <= 0 if positive else float(value) < 0)
    ):
        raise MinuteDataContractError(reason)
    return float(value)


def _strings(value: object, reason: str, *, nonempty: bool = True) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise MinuteDataContractError(reason)
    result: list[str] = []
    for item in value:
        normalized = _text(item, reason)
        if normalized in result:
            raise MinuteDataContractError(reason)
        result.append(normalized)
    if nonempty and not result:
        raise MinuteDataContractError(reason)
    return tuple(result)


def _active_catalog_row(row: Mapping[str, Any]) -> bool:
    availability = row.get("availability")
    return bool(
        isinstance(availability, Mapping)
        and availability.get("activation_states") == ["active"]
    )


def _fresh(metadata_freshness: Mapping[str, Any]) -> bool:
    state = metadata_freshness.get("state")
    return (
        isinstance(state, str)
        and state.strip().lower() == "fresh"
        and metadata_freshness.get("stale") is False
    )


def _valid_quality(metadata_quality: Mapping[str, Any]) -> bool:
    state = metadata_quality.get("state")
    return isinstance(state, str) and state.strip().lower() == "valid"


def _complete_lineage(lineage: Mapping[str, Any] | None) -> bool:
    return bool(
        isinstance(lineage, Mapping)
        and lineage.get("complete") is True
        and lineage.get("provider_neutral") is True
        and isinstance(lineage.get("provider"), str)
        and bool(lineage.get("provider"))
        and isinstance(lineage.get("transport_service"), str)
        and bool(lineage.get("transport_service"))
    )


def _session_for_bar(bar_start: datetime, bar_end: datetime) -> str:
    local_start = bar_start.astimezone(SHANGHAI)
    local_end = bar_end.astimezone(SHANGHAI)
    if local_start.date() != local_end.date():
        raise MinuteDataContractError("minute_bar_crosses_trade_date")
    start_value = local_start.time()
    end_value = local_end.time()
    if time(9, 30) <= start_value and end_value <= time(11, 30):
        return "continuous_auction_am"
    if time(13, 0) <= start_value and end_value <= time(15, 0):
        return "continuous_auction_pm"
    raise MinuteDataContractError("minute_bar_outside_trading_session")


class MinuteTimestampSemantics(str, Enum):
    BAR_END = "bar_end"
    BAR_START = "bar_start"


@dataclass(frozen=True)
class MinuteDatasetProfile:
    """TA-owned interpretation of one exact active catalog contract."""

    catalog_version: str
    dataset_id: str
    schema_major: int
    default_fields: tuple[str, ...]
    default_order: tuple[str, ...]
    filter_operators: tuple[tuple[str, tuple[str, ...]], ...]
    catalog_contract_sha256: str
    identity_fields: tuple[str, ...]
    symbol_field: str
    timestamp_field: str
    open_field: str
    high_field: str
    low_field: str
    close_field: str
    volume_field: str
    amount_field: str
    previous_close_field: str | None
    suspension_field: str | None
    frequency_field: str | None
    frequency_value: str | None
    timestamp_format: str
    timestamp_semantics: MinuteTimestampSemantics
    volume_multiplier_to_shares: float
    amount_multiplier_to_cny: float
    price_adjustment: str
    max_pages: int
    max_rows: int
    page_limit: int
    catalog_route: str = FIXED_CATALOG_ROUTE
    query_route: str = FIXED_QUERY_ROUTE

    def __post_init__(self) -> None:
        for field_name in (
            "catalog_version",
            "dataset_id",
            "symbol_field",
            "timestamp_field",
            "open_field",
            "high_field",
            "low_field",
            "close_field",
            "volume_field",
            "amount_field",
            "timestamp_format",
        ):
            _text(getattr(self, field_name), f"minute_profile_{field_name}_invalid")
        if (self.previous_close_field is None) != (self.suspension_field is None):
            raise MinuteDataContractError("minute_reference_field_contract_incomplete")
        for field_name in ("previous_close_field", "suspension_field"):
            value = getattr(self, field_name)
            if value is not None:
                _text(value, f"minute_profile_{field_name}_invalid")
        if self.catalog_route != FIXED_CATALOG_ROUTE:
            raise MinuteDataContractError("minute_catalog_route_invalid")
        if self.query_route != FIXED_QUERY_ROUTE:
            raise MinuteDataContractError("minute_query_route_invalid")
        if (
            isinstance(self.schema_major, bool)
            or not isinstance(self.schema_major, int)
            or self.schema_major <= 0
        ):
            raise MinuteDataContractError("minute_schema_major_invalid")
        if self.timestamp_semantics not in MinuteTimestampSemantics:
            raise MinuteDataContractError("minute_timestamp_semantics_invalid")
        for field_name in (
            "volume_multiplier_to_shares",
            "amount_multiplier_to_cny",
        ):
            value = getattr(self, field_name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or value <= 0
            ):
                raise MinuteDataContractError(f"minute_{field_name}_invalid")
        if self.price_adjustment != "raw_unadjusted":
            raise MinuteDataContractError(
                "minute_execution_prices_must_be_raw_unadjusted"
            )
        if (
            not isinstance(self.catalog_contract_sha256, str)
            or len(self.catalog_contract_sha256) != 64
            or any(
                character not in _SHA256_HEX
                for character in self.catalog_contract_sha256
            )
        ):
            raise MinuteDataContractError("minute_catalog_contract_sha256_invalid")
        if (self.frequency_field is None) != (self.frequency_value is None):
            raise MinuteDataContractError("minute_frequency_contract_incomplete")
        if self.frequency_field is not None:
            _text(self.frequency_field, "minute_frequency_field_invalid")
            if str(self.frequency_value).strip().lower() not in {
                "5min",
                "5m",
                "5",
            }:
                raise MinuteDataContractError("minute_frequency_must_be_five_minutes")
        for name, value in (
            ("max_pages", self.max_pages),
            ("max_rows", self.max_rows),
            ("page_limit", self.page_limit),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise MinuteDataContractError(f"minute_{name}_invalid")
        required = {
            self.symbol_field,
            self.timestamp_field,
            self.open_field,
            self.high_field,
            self.low_field,
            self.close_field,
            self.volume_field,
            self.amount_field,
        }
        if self.previous_close_field is not None:
            required.add(self.previous_close_field)
        if self.suspension_field is not None:
            required.add(self.suspension_field)
        if self.frequency_field is not None:
            required.add(self.frequency_field)
        if not required.issubset(set(self.default_fields)):
            raise MinuteDataContractError("minute_profile_required_fields_missing")
        if not {self.symbol_field, self.timestamp_field}.issubset(
            set(self.identity_fields)
        ):
            raise MinuteDataContractError("minute_profile_identity_incomplete")
        if not set(self.identity_fields).issubset(set(self.default_fields)):
            raise MinuteDataContractError("minute_profile_identity_field_missing")
        filter_fields: set[str] = set()
        for field_name, operators in self.filter_operators:
            if field_name in filter_fields or field_name not in self.default_fields:
                raise MinuteDataContractError("minute_filter_operators_invalid")
            filter_fields.add(field_name)
            _strings(operators, "minute_filter_operators_invalid")

    @classmethod
    def from_catalog(
        cls,
        catalog: CatalogEnvelope,
        *,
        expected_catalog_version: str,
        dataset_id: str,
        identity_fields: tuple[str, ...],
        symbol_field: str,
        timestamp_field: str,
        open_field: str,
        high_field: str,
        low_field: str,
        close_field: str,
        volume_field: str,
        amount_field: str,
        previous_close_field: str | None,
        suspension_field: str | None,
        timestamp_format: str,
        timestamp_semantics: MinuteTimestampSemantics,
        volume_multiplier_to_shares: float,
        amount_multiplier_to_cny: float,
        price_adjustment: str,
        max_pages: int,
        max_rows: int,
        page_limit: int,
        frequency_field: str | None = None,
        frequency_value: str | None = None,
    ) -> "MinuteDatasetProfile":
        """Freeze a profile from one exact formal catalog row.

        The caller supplies only TA domain interpretation.  Dataset ID,
        schema, selectable fields, default order and page-size authority come
        from the returned catalog and are never inferred from a provider name.
        """

        if not isinstance(catalog, CatalogEnvelope):
            raise MinuteDataContractError("minute_catalog_envelope_required")
        if catalog.catalog_version != expected_catalog_version:
            raise MinuteDataContractError("minute_catalog_version_drift")
        matches = [row for row in catalog.data if row.get("dataset_id") == dataset_id]
        if len(matches) != 1:
            raise MinuteDataContractError("minute_dataset_catalog_row_missing")
        row = matches[0]
        if not _active_catalog_row(row):
            raise MinuteDataContractError("minute_dataset_not_active")
        schema_major = row.get("schema_major")
        if (
            isinstance(schema_major, bool)
            or not isinstance(schema_major, int)
            or schema_major <= 0
        ):
            raise MinuteDataContractError("minute_catalog_schema_major_invalid")
        default_fields = _strings(
            row.get("default_fields"),
            "minute_catalog_default_fields_invalid",
        )
        default_order = _strings(
            row.get("default_order", []),
            "minute_catalog_default_order_invalid",
            nonempty=False,
        )
        limits = row.get("limits")
        if not isinstance(limits, Mapping):
            raise MinuteDataContractError("minute_catalog_limits_invalid")
        server_page_size = limits.get("max_page_size")
        if (
            isinstance(server_page_size, bool)
            or not isinstance(server_page_size, int)
            or server_page_size <= 0
            or page_limit > server_page_size
        ):
            raise MinuteDataContractError("minute_page_limit_exceeds_catalog")
        raw_filter_operators = row.get("filter_operators")
        if not isinstance(raw_filter_operators, Mapping):
            raise MinuteDataContractError("minute_catalog_filter_operators_invalid")
        filter_operators: list[tuple[str, tuple[str, ...]]] = []
        for raw_field_name in sorted(raw_filter_operators):
            field_name = _text(
                raw_field_name, "minute_catalog_filter_operators_invalid"
            )
            filter_operators.append(
                (
                    field_name,
                    _strings(
                        raw_filter_operators[raw_field_name],
                        "minute_catalog_filter_operators_invalid",
                    ),
                )
            )
        catalog_contract = {
            "dataset_id": dataset_id,
            "schema_major": schema_major,
            "default_fields": list(default_fields),
            "default_order": list(default_order),
            "filter_operators": {
                field_name: list(operators)
                for field_name, operators in filter_operators
            },
            "limits": dict(limits),
            "availability": row.get("availability"),
        }
        return cls(
            catalog_version=catalog.catalog_version,
            dataset_id=dataset_id,
            schema_major=schema_major,
            default_fields=default_fields,
            default_order=default_order,
            filter_operators=tuple(filter_operators),
            catalog_contract_sha256=_sha256(catalog_contract),
            identity_fields=_strings(
                identity_fields,
                "minute_profile_identity_fields_invalid",
            ),
            symbol_field=symbol_field,
            timestamp_field=timestamp_field,
            open_field=open_field,
            high_field=high_field,
            low_field=low_field,
            close_field=close_field,
            volume_field=volume_field,
            amount_field=amount_field,
            previous_close_field=previous_close_field,
            suspension_field=suspension_field,
            frequency_field=frequency_field,
            frequency_value=frequency_value,
            timestamp_format=timestamp_format,
            timestamp_semantics=timestamp_semantics,
            volume_multiplier_to_shares=volume_multiplier_to_shares,
            amount_multiplier_to_cny=amount_multiplier_to_cny,
            price_adjustment=price_adjustment,
            max_pages=max_pages,
            max_rows=max_rows,
            page_limit=page_limit,
        )


@dataclass(frozen=True)
class MinuteBarEvidence:
    """One accepted, completed five-minute bar and its envelope proof."""

    symbol: str
    bar_start: datetime
    bar_end: datetime
    open_cny: float
    high_cny: float
    low_cny: float
    close_cny: float
    volume_shares: float
    amount_cny: float
    previous_close_cny: float
    suspended: bool
    market_session: str
    dataset_id: str
    catalog_version: str
    receipt_id: str
    data_through: datetime
    observed_at: datetime
    available_at: datetime
    decision_time: datetime
    source_lineage_sha256: str
    envelope_proof_sha256: str
    source_row_sha256: str
    reference_evidence_sha256: str

    def __post_init__(self) -> None:
        if not is_mainboard_tradable(self.symbol):
            raise MinuteDataContractError("minute_symbol_not_mainboard_tradable")
        for field_name in (
            "dataset_id",
            "catalog_version",
            "receipt_id",
            "market_session",
        ):
            _text(getattr(self, field_name), f"minute_{field_name}_invalid")
        for field_name in (
            "source_lineage_sha256",
            "envelope_proof_sha256",
            "source_row_sha256",
            "reference_evidence_sha256",
        ):
            value = getattr(self, field_name)
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in _SHA256_HEX for character in value)
            ):
                raise MinuteDataContractError(f"minute_{field_name}_invalid")
        bar_start = _aware(self.bar_start, "minute_bar_start_timezone_required")
        bar_end = _aware(self.bar_end, "minute_bar_end_timezone_required")
        data_through = _aware(
            self.data_through, "minute_data_through_timezone_required"
        )
        observed = _aware(self.observed_at, "minute_observed_at_timezone_required")
        available = _aware(self.available_at, "minute_available_at_timezone_required")
        decision = _aware(self.decision_time, "minute_decision_time_timezone_required")
        if bar_end - bar_start != FIVE_MINUTES:
            raise MinuteDataContractError("minute_bar_duration_invalid")
        expected_session = _session_for_bar(bar_start, bar_end)
        if self.market_session != expected_session:
            raise MinuteDataContractError("minute_market_session_mismatch")
        if bar_end.astimezone(SHANGHAI).weekday() >= 5:
            raise MinuteDataContractError("minute_weekend_bar_forbidden")
        if not (bar_end <= data_through <= observed <= available <= decision):
            raise MinuteDataContractError("minute_evidence_time_order_invalid")
        if available - bar_end > MAX_MINUTE_DATA_LATENCY:
            raise MinuteDataContractError("minute_evidence_latency_exceeded")
        if self.suspended is not False:
            raise MinuteDataContractError("minute_suspended_instrument")
        opening = _finite(self.open_cny, "minute_open_invalid", positive=True)
        high = _finite(self.high_cny, "minute_high_invalid", positive=True)
        low = _finite(self.low_cny, "minute_low_invalid", positive=True)
        close = _finite(self.close_cny, "minute_close_invalid", positive=True)
        _finite(
            self.previous_close_cny,
            "minute_previous_close_invalid",
            positive=True,
        )
        volume = _finite(self.volume_shares, "minute_volume_invalid")
        _finite(self.amount_cny, "minute_amount_invalid")
        if high < max(opening, close, low) or low > min(opening, close, high):
            raise MinuteDataContractError("minute_ohlc_relationship_invalid")
        if volume <= 0:
            raise MinuteDataContractError("minute_zero_volume_not_tradable")

    @property
    def identity(self) -> tuple[str, datetime]:
        return self.symbol, self.bar_end

    def canonical_payload(self) -> dict[str, Any]:
        def stamp(value: datetime) -> str:
            return value.astimezone(timezone.utc).isoformat()

        return {
            "symbol": self.symbol,
            "bar_start": stamp(self.bar_start),
            "bar_end": stamp(self.bar_end),
            "open_cny": self.open_cny,
            "high_cny": self.high_cny,
            "low_cny": self.low_cny,
            "close_cny": self.close_cny,
            "volume_shares": self.volume_shares,
            "amount_cny": self.amount_cny,
            "previous_close_cny": self.previous_close_cny,
            "suspended": self.suspended,
            "market_session": self.market_session,
            "dataset_id": self.dataset_id,
            "catalog_version": self.catalog_version,
            "receipt_id": self.receipt_id,
            "data_through": stamp(self.data_through),
            "observed_at": stamp(self.observed_at),
            "available_at": stamp(self.available_at),
            "decision_time": stamp(self.decision_time),
            "source_lineage_sha256": self.source_lineage_sha256,
            "envelope_proof_sha256": self.envelope_proof_sha256,
            "source_row_sha256": self.source_row_sha256,
            "reference_evidence_sha256": self.reference_evidence_sha256,
        }

    @property
    def sha256(self) -> str:
        return _sha256(self.canonical_payload())


@dataclass(frozen=True)
class MinuteReferenceFact:
    """TA-owned daily/reference evidence required by provider-native minute rows."""

    symbol: str
    trade_date: date
    previous_close_cny: float
    suspended: bool
    evidence_sha256: str

    def __post_init__(self) -> None:
        if not is_mainboard_tradable(self.symbol):
            raise MinuteDataContractError(
                "minute_reference_symbol_not_mainboard_tradable"
            )
        if not isinstance(self.trade_date, date) or isinstance(
            self.trade_date, datetime
        ):
            raise MinuteDataContractError("minute_reference_trade_date_invalid")
        _finite(
            self.previous_close_cny,
            "minute_reference_previous_close_invalid",
            positive=True,
        )
        if type(self.suspended) is not bool:
            raise MinuteDataContractError("minute_reference_suspension_invalid")
        if (
            not isinstance(self.evidence_sha256, str)
            or len(self.evidence_sha256) != 64
            or any(character not in _SHA256_HEX for character in self.evidence_sha256)
        ):
            raise MinuteDataContractError("minute_reference_evidence_sha256_invalid")


@dataclass(frozen=True)
class MinuteBarSnapshot:
    """One replay-proven, bounded set of accepted minute bars."""

    profile: MinuteDatasetProfile
    bars: tuple[MinuteBarEvidence, ...]
    page_count: int
    row_count: int
    pagination_trace_sha256: str
    first_semantic_sha256: str
    replay_semantic_sha256: str
    same_observation: bool

    def __post_init__(self) -> None:
        if not isinstance(self.profile, MinuteDatasetProfile):
            raise MinuteDataContractError("minute_snapshot_profile_invalid")
        if not self.bars:
            raise MinuteDataContractError("minute_snapshot_empty")
        if self.row_count != len(self.bars):
            raise MinuteDataContractError("minute_snapshot_row_count_mismatch")
        if not (1 <= self.page_count <= self.profile.max_pages):
            raise MinuteDataContractError("minute_snapshot_page_count_invalid")
        identities: dict[tuple[str, datetime], str] = {}
        for bar in self.bars:
            if not isinstance(bar, MinuteBarEvidence):
                raise MinuteDataContractError("minute_snapshot_bar_invalid")
            if (
                bar.dataset_id != self.profile.dataset_id
                or bar.catalog_version != self.profile.catalog_version
            ):
                raise MinuteDataContractError("minute_snapshot_binding_mismatch")
            previous = identities.get(bar.identity)
            if previous is not None:
                reason = (
                    "minute_duplicate_bar"
                    if previous == bar.sha256
                    else "minute_conflicting_bar"
                )
                raise MinuteDataContractError(reason)
            identities[bar.identity] = bar.sha256
        for field_name in (
            "pagination_trace_sha256",
            "first_semantic_sha256",
            "replay_semantic_sha256",
        ):
            value = getattr(self, field_name)
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in _SHA256_HEX for character in value)
            ):
                raise MinuteDataContractError(f"minute_{field_name}_invalid")
        if self.same_observation is not True:
            raise MinuteDataContractError("minute_same_observation_mismatch")
        if self.first_semantic_sha256 != self.replay_semantic_sha256:
            raise MinuteDataContractError("minute_same_observation_mismatch")

    @property
    def sha256(self) -> str:
        return _sha256(
            {
                "profile": {
                    "catalog_version": self.profile.catalog_version,
                    "dataset_id": self.profile.dataset_id,
                    "schema_major": self.profile.schema_major,
                },
                "bars": [bar.sha256 for bar in self.bars],
                "page_count": self.page_count,
                "row_count": self.row_count,
                "pagination_trace_sha256": self.pagination_trace_sha256,
                "semantic_sha256": self.first_semantic_sha256,
                "same_observation": True,
            }
        )


@dataclass(frozen=True)
class MinuteEvidenceAuditRecord:
    """Audit-only failure record; never eligible for features or execution."""

    reason_code: str
    dataset_id: str
    catalog_version: str
    decision_time: datetime
    rejected_payload_sha256: str
    feature_eligible: bool = False
    candidate_eligible: bool = False
    execution_eligible: bool = False

    def __post_init__(self) -> None:
        _text(self.reason_code, "minute_audit_reason_invalid")
        _text(self.dataset_id, "minute_audit_dataset_invalid")
        _text(self.catalog_version, "minute_audit_catalog_invalid")
        _aware(self.decision_time, "minute_audit_decision_time_invalid")
        if len(self.rejected_payload_sha256) != 64 or any(
            c not in _SHA256_HEX for c in self.rejected_payload_sha256
        ):
            raise MinuteDataContractError("minute_audit_payload_hash_invalid")
        if any(
            (
                self.feature_eligible,
                self.candidate_eligible,
                self.execution_eligible,
            )
        ):
            raise MinuteDataContractError("minute_rejected_evidence_must_be_audit_only")


class MinuteEvidenceAuditLedger:
    """Idempotent process-local collector for rejected minute evidence."""

    def __init__(self) -> None:
        self._records: dict[str, MinuteEvidenceAuditRecord] = {}

    def append(self, record: MinuteEvidenceAuditRecord) -> bool:
        if not isinstance(record, MinuteEvidenceAuditRecord):
            raise MinuteDataContractError("minute_audit_record_invalid")
        identity = _sha256(
            {
                "reason": record.reason_code,
                "dataset_id": record.dataset_id,
                "catalog_version": record.catalog_version,
                "decision_time": record.decision_time.astimezone(
                    timezone.utc
                ).isoformat(),
                "payload": record.rejected_payload_sha256,
            }
        )
        previous = self._records.get(identity)
        if previous is None:
            self._records[identity] = record
            return True
        if previous == record:
            return False
        raise MinuteDataContractError("minute_audit_identity_conflict")

    def records(self) -> tuple[MinuteEvidenceAuditRecord, ...]:
        return tuple(self._records.values())


class MinuteMarketDataPort(Protocol):
    """Internal TA role port; no transport or provider is implied."""

    def load_snapshot(
        self,
        *,
        profile: MinuteDatasetProfile,
        filters: Mapping[str, Any],
        decision_time: datetime,
        trading_dates: frozenset[date],
        audit_ledger: MinuteEvidenceAuditLedger,
        reference_facts: Mapping[str, MinuteReferenceFact] | None = None,
    ) -> MinuteBarSnapshot: ...


def _provider_timestamp(
    raw: object,
    *,
    profile: MinuteDatasetProfile,
) -> tuple[datetime, datetime]:
    value = _text(raw, "minute_row_timestamp_invalid")
    try:
        parsed = datetime.strptime(value, profile.timestamp_format)
    except ValueError as exc:
        raise MinuteDataContractError("minute_row_timestamp_invalid") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI)
    else:
        parsed = parsed.astimezone(SHANGHAI)
    if profile.timestamp_semantics is MinuteTimestampSemantics.BAR_END:
        return parsed - FIVE_MINUTES, parsed
    return parsed, parsed + FIVE_MINUTES


def _map_run(
    *,
    profile: MinuteDatasetProfile,
    run: PagedQueryRun,
    decision_time: datetime,
    trading_dates: frozenset[date],
    reference_facts: Mapping[str, MinuteReferenceFact] | None,
) -> tuple[MinuteBarEvidence, ...]:
    run.verify_integrity(identity_fields=profile.identity_fields)
    envelope = run.envelope
    metadata = envelope.metadata
    if (
        envelope.dataset_id != profile.dataset_id
        or envelope.catalog_version != profile.catalog_version
    ):
        raise MinuteDataContractError("minute_query_binding_mismatch")
    if metadata.state.strip().lower() != "ready" or metadata.degraded is not False:
        raise MinuteDataContractError("minute_metadata_not_ready")
    if not _fresh(metadata.freshness):
        raise MinuteDataContractError("minute_metadata_not_fresh")
    if not _valid_quality(metadata.quality):
        raise MinuteDataContractError("minute_metadata_quality_invalid")
    if not _complete_lineage(metadata.lineage):
        raise MinuteDataContractError("minute_metadata_lineage_incomplete")
    if not all(
        isinstance(value, str) and bool(value)
        for value in (
            metadata.receipt_id,
            metadata.data_through,
            metadata.observed_at,
        )
    ):
        raise MinuteDataContractError("minute_metadata_proof_incomplete")
    decision = _aware(decision_time, "minute_decision_time_timezone_required")
    data_through = _parse_aware_iso(
        metadata.data_through, "minute_data_through_invalid"
    )
    observed = _parse_aware_iso(metadata.observed_at, "minute_observed_at_invalid")
    assert metadata.lineage is not None
    lineage_sha = _sha256(metadata.lineage)
    envelope_proof_sha = _sha256(
        {
            "dataset_id": envelope.dataset_id,
            "catalog_version": envelope.catalog_version,
            "receipt_id": metadata.receipt_id,
            "data_through": metadata.data_through,
            "observed_at": metadata.observed_at,
            "freshness": metadata.freshness,
            "quality": metadata.quality,
            "lineage": metadata.lineage,
        }
    )
    bars: list[MinuteBarEvidence] = []
    seen: dict[tuple[str, datetime], str] = {}
    for row in envelope.data:
        symbol = _text(
            row.get(profile.symbol_field), "minute_row_symbol_missing"
        ).upper()
        bar_start, bar_end = _provider_timestamp(
            row.get(profile.timestamp_field),
            profile=profile,
        )
        if bar_end.astimezone(SHANGHAI).date() not in trading_dates:
            raise MinuteDataContractError("minute_trade_date_not_calendar_eligible")
        if profile.frequency_field is not None:
            actual_frequency = str(row.get(profile.frequency_field) or "").lower()
            if actual_frequency != str(profile.frequency_value).lower():
                raise MinuteDataContractError("minute_row_frequency_mismatch")
        source_row_sha = _sha256(row)
        if profile.previous_close_field is None:
            reference = (
                reference_facts.get(symbol)
                if isinstance(reference_facts, Mapping)
                else None
            )
            if not isinstance(reference, MinuteReferenceFact):
                raise MinuteDataContractError("minute_reference_fact_missing")
            if reference.symbol != symbol:
                raise MinuteDataContractError("minute_reference_symbol_mismatch")
            if reference.trade_date != bar_end.astimezone(SHANGHAI).date():
                raise MinuteDataContractError("minute_reference_trade_date_mismatch")
            previous_close = reference.previous_close_cny
            suspended = reference.suspended
            reference_evidence_sha = reference.evidence_sha256
        else:
            assert profile.suspension_field is not None
            previous_close = row.get(profile.previous_close_field)
            suspended = row.get(profile.suspension_field)
            if type(suspended) is not bool:
                raise MinuteDataContractError("minute_row_suspension_invalid")
            reference_evidence_sha = source_row_sha
        evidence = MinuteBarEvidence(
            symbol=symbol,
            bar_start=bar_start,
            bar_end=bar_end,
            open_cny=_finite(
                row.get(profile.open_field), "minute_open_invalid", positive=True
            ),
            high_cny=_finite(
                row.get(profile.high_field), "minute_high_invalid", positive=True
            ),
            low_cny=_finite(
                row.get(profile.low_field), "minute_low_invalid", positive=True
            ),
            close_cny=_finite(
                row.get(profile.close_field), "minute_close_invalid", positive=True
            ),
            volume_shares=(
                _finite(row.get(profile.volume_field), "minute_volume_invalid")
                * profile.volume_multiplier_to_shares
            ),
            amount_cny=(
                _finite(row.get(profile.amount_field), "minute_amount_invalid")
                * profile.amount_multiplier_to_cny
            ),
            previous_close_cny=_finite(
                previous_close,
                "minute_previous_close_invalid",
                positive=True,
            ),
            suspended=suspended,
            market_session=_session_for_bar(bar_start, bar_end),
            dataset_id=envelope.dataset_id,
            catalog_version=envelope.catalog_version,
            receipt_id=str(metadata.receipt_id),
            data_through=data_through,
            observed_at=observed,
            available_at=observed,
            decision_time=decision,
            source_lineage_sha256=lineage_sha,
            envelope_proof_sha256=envelope_proof_sha,
            source_row_sha256=source_row_sha,
            reference_evidence_sha256=reference_evidence_sha,
        )
        previous = seen.get(evidence.identity)
        if previous is not None:
            reason = (
                "minute_duplicate_bar"
                if previous == evidence.sha256
                else "minute_conflicting_bar"
            )
            raise MinuteDataContractError(reason)
        seen[evidence.identity] = evidence.sha256
        bars.append(evidence)
    if not bars:
        raise MinuteDataContractError("minute_query_returned_no_bars")
    return tuple(bars)


def snapshot_from_runs(
    *,
    profile: MinuteDatasetProfile,
    first: PagedQueryRun,
    replay: PagedQueryRun,
    decision_time: datetime,
    trading_dates: frozenset[date],
    audit_ledger: MinuteEvidenceAuditLedger,
    reference_facts: Mapping[str, MinuteReferenceFact] | None = None,
) -> MinuteBarSnapshot:
    """Map two bounded reads and require identical same-observation semantics."""

    rejected_payload = {
        "dataset_id": profile.dataset_id,
        "catalog_version": profile.catalog_version,
        "first_semantic_sha256": getattr(first, "semantic_sha256", None),
        "replay_semantic_sha256": getattr(replay, "semantic_sha256", None),
    }
    try:
        if (
            first.semantic_sha256 != replay.semantic_sha256
            or first.semantic_trace_sha256 != replay.semantic_trace_sha256
        ):
            raise MinuteDataContractError("minute_same_observation_mismatch")
        bars = _map_run(
            profile=profile,
            run=first,
            decision_time=decision_time,
            trading_dates=trading_dates,
            reference_facts=reference_facts,
        )
        replay_bars = _map_run(
            profile=profile,
            run=replay,
            decision_time=decision_time,
            trading_dates=trading_dates,
            reference_facts=reference_facts,
        )
        if [bar.sha256 for bar in bars] != [bar.sha256 for bar in replay_bars]:
            raise MinuteDataContractError("minute_same_observation_mismatch")
        return MinuteBarSnapshot(
            profile=profile,
            bars=bars,
            page_count=first.page_count,
            row_count=first.row_count,
            pagination_trace_sha256=first.pagination_trace_sha256,
            first_semantic_sha256=first.semantic_sha256,
            replay_semantic_sha256=replay.semantic_sha256,
            same_observation=True,
        )
    except MinuteDataContractError as exc:
        audit_ledger.append(
            MinuteEvidenceAuditRecord(
                reason_code=exc.reason_code,
                dataset_id=profile.dataset_id,
                catalog_version=profile.catalog_version,
                decision_time=_aware(
                    decision_time, "minute_decision_time_timezone_required"
                ),
                rejected_payload_sha256=_sha256(rejected_payload),
            )
        )
        raise


class TradingDatasMinuteMarketDataPort:
    """Injected-client adapter for the fixed TradingDatas V1 data plane."""

    def __init__(self, client: SharedSignalsV1Client) -> None:
        if not isinstance(client, SharedSignalsV1Client):
            raise TypeError("client must be SharedSignalsV1Client")
        self._client = client

    def load_snapshot(
        self,
        *,
        profile: MinuteDatasetProfile,
        filters: Mapping[str, Any],
        decision_time: datetime,
        trading_dates: frozenset[date],
        audit_ledger: MinuteEvidenceAuditLedger,
        reference_facts: Mapping[str, MinuteReferenceFact] | None = None,
    ) -> MinuteBarSnapshot:
        audit_count_before = len(audit_ledger.records())
        try:
            catalog = self._client.get_catalog()
            if catalog.catalog_version != profile.catalog_version:
                raise MinuteDataContractError("minute_catalog_version_drift")
            row = next(
                (
                    item
                    for item in catalog.data
                    if item.get("dataset_id") == profile.dataset_id
                ),
                None,
            )
            if row is None or not _active_catalog_row(row):
                raise MinuteDataContractError("minute_dataset_not_active")
            current_catalog_contract = {
                "dataset_id": row.get("dataset_id"),
                "schema_major": row.get("schema_major"),
                "default_fields": row.get("default_fields"),
                "default_order": row.get("default_order"),
                "filter_operators": row.get("filter_operators"),
                "limits": row.get("limits"),
                "availability": row.get("availability"),
            }
            if _sha256(current_catalog_contract) != profile.catalog_contract_sha256:
                raise MinuteDataContractError("minute_catalog_contract_drift")
            filter_contract = dict(profile.filter_operators)
            for field_name, condition in filters.items():
                if (
                    field_name not in filter_contract
                    or not isinstance(condition, Mapping)
                    or not condition
                    or any(
                        operator not in filter_contract[field_name]
                        for operator in condition
                    )
                ):
                    raise MinuteDataContractError(
                        "minute_query_filter_not_catalog_authorized"
                    )
            request = QueryRequest(
                dataset_id=profile.dataset_id,
                schema_major=profile.schema_major,
                fields=profile.default_fields,
                filters=filters,
                order=profile.default_order or None,
                limit=profile.page_limit,
            )
            first = collect_query_pages(
                client=self._client,
                request=request,
                identity_fields=profile.identity_fields,
                max_pages=profile.max_pages,
                max_rows=profile.max_rows,
            )
            replay = collect_query_pages(
                client=self._client,
                request=request,
                identity_fields=profile.identity_fields,
                max_pages=profile.max_pages,
                max_rows=profile.max_rows,
            )
            return snapshot_from_runs(
                profile=profile,
                first=first,
                replay=replay,
                decision_time=decision_time,
                trading_dates=trading_dates,
                audit_ledger=audit_ledger,
                reference_facts=reference_facts,
            )
        except MinuteDataContractError as exc:
            if len(audit_ledger.records()) == audit_count_before:
                audit_ledger.append(
                    MinuteEvidenceAuditRecord(
                        reason_code=exc.reason_code,
                        dataset_id=profile.dataset_id,
                        catalog_version=profile.catalog_version,
                        decision_time=_aware(
                            decision_time,
                            "minute_decision_time_timezone_required",
                        ),
                        rejected_payload_sha256=_sha256(
                            {
                                "failure_class": "minute_contract",
                                "reason_code": exc.reason_code,
                                "dataset_id": profile.dataset_id,
                            }
                        ),
                    )
                )
            raise
        except PaginationContractError as exc:
            reason = str(exc) or "minute_pagination_contract_failed"
            audit_ledger.append(
                MinuteEvidenceAuditRecord(
                    reason_code=reason,
                    dataset_id=profile.dataset_id,
                    catalog_version=profile.catalog_version,
                    decision_time=_aware(
                        decision_time, "minute_decision_time_timezone_required"
                    ),
                    rejected_payload_sha256=_sha256(
                        {
                            "failure_class": "pagination",
                            "reason_code": reason,
                            "dataset_id": profile.dataset_id,
                        }
                    ),
                )
            )
            raise MinuteDataContractError(reason) from exc
        except SharedSignalsV1Error as exc:
            reason = "minute_tradingdatas_request_failed"
            audit_ledger.append(
                MinuteEvidenceAuditRecord(
                    reason_code=reason,
                    dataset_id=profile.dataset_id,
                    catalog_version=profile.catalog_version,
                    decision_time=_aware(
                        decision_time, "minute_decision_time_timezone_required"
                    ),
                    rejected_payload_sha256=_sha256(
                        {
                            "failure_class": type(exc).__name__,
                            "dataset_id": profile.dataset_id,
                        }
                    ),
                )
            )
            raise MinuteDataContractError(reason) from exc


__all__ = [
    "FIXED_CATALOG_ROUTE",
    "FIXED_QUERY_ROUTE",
    "FIVE_MINUTES",
    "MAX_MINUTE_DATA_LATENCY",
    "MinuteBarEvidence",
    "MinuteBarSnapshot",
    "MinuteDataContractError",
    "MinuteDatasetProfile",
    "MinuteEvidenceAuditLedger",
    "MinuteEvidenceAuditRecord",
    "MinuteMarketDataPort",
    "MinuteReferenceFact",
    "MinuteTimestampSemantics",
    "TradingDatasMinuteMarketDataPort",
    "snapshot_from_runs",
]
