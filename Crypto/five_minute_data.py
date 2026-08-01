"""Provider-neutral, mock-ready Crypto closed-five-minute data boundary.

The module owns no transport configuration.  A caller must inject the existing
TradingDatas V1 typed client and a profile frozen from one catalog response.
Only ``GET /v1/catalog`` and ``POST /v1/query`` are reachable through that
client.  The checked-in Crypto configuration deliberately contains no endpoint,
credential, catalog version, or dataset identifier.

The first candidate profile is intentionally shaped around four independently
receipted datasets: BTC/ETH bars and BTC/ETH instrument rules.  It remains a
fixture/candidate contract until the same bytes exist on TradingDatas main and
the formal internal endpoint is independently read back.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Protocol

from Crypto.fixture_sim.contracts import (
    ALLOWED_SYMBOLS,
    CryptoEvidenceError,
    SpotInstrumentRules,
)
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
from shared.governance.evidence_readiness import dataset_contract_fingerprint


FIVE_MINUTES = timedelta(minutes=5)
BINANCE_INCLUSIVE_CLOSE_OFFSET = timedelta(milliseconds=1)
REQUIRED_WINDOW_BARS = 13
PROFILE_MODES = frozenset({"fixture_mock", "tradingdatas_handoff"})
_SHA256_HEX = frozenset("0123456789abcdef")
_BAR_FILTER_ROLES = frozenset({"symbol", "open_time_window"})
_RULE_FILTER_ROLES = frozenset({"symbol", "active_status"})


class CryptoFiveMinuteDataError(ValueError):
    """Fail-closed data boundary error carrying a stable reason code."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def _canonical_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _canonical_value(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (tuple, list)):
        return [_canonical_value(item) for item in value]
    return value


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            _canonical_value(value),
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise CryptoFiveMinuteDataError("crypto_5m_payload_not_canonical") from exc


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _is_sha256(value: Any) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) == 64
        and all(character in _SHA256_HEX for character in value)
    )


def _text(value: Any, reason: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise CryptoFiveMinuteDataError(reason)
    return value


def _strings(
    value: Any,
    reason: str,
    *,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, (tuple, list)):
        raise CryptoFiveMinuteDataError(reason)
    result: list[str] = []
    for item in value:
        normalized = _text(item, reason)
        if normalized in result:
            raise CryptoFiveMinuteDataError(reason)
        result.append(normalized)
    if not result and not allow_empty:
        raise CryptoFiveMinuteDataError(reason)
    return tuple(result)


def _utc_datetime(
    value: Any,
    reason: str,
    *,
    five_minute_aligned: bool = False,
) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = _text(value, reason)
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise CryptoFiveMinuteDataError(reason) from exc
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() is None
        or parsed.utcoffset() != timedelta(0)
    ):
        raise CryptoFiveMinuteDataError("crypto_5m_timestamp_must_be_utc")
    parsed = parsed.astimezone(timezone.utc)
    if five_minute_aligned and (
        parsed.minute % 5 != 0 or parsed.second != 0 or parsed.microsecond != 0
    ):
        raise CryptoFiveMinuteDataError("crypto_5m_timestamp_alignment_invalid")
    return parsed


def _decimal_text(
    value: Any,
    reason: str,
    *,
    positive: bool = False,
    nonnegative: bool = False,
) -> Decimal:
    if not isinstance(value, str) or not value or value != value.strip():
        raise CryptoFiveMinuteDataError(reason)
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise CryptoFiveMinuteDataError(reason) from exc
    if not result.is_finite():
        raise CryptoFiveMinuteDataError(reason)
    if positive and result <= 0:
        raise CryptoFiveMinuteDataError(reason)
    if nonnegative and result < 0:
        raise CryptoFiveMinuteDataError(reason)
    return result


def _step_aligned(value: Decimal, step: Decimal) -> bool:
    try:
        return value % step == 0
    except InvalidOperation:
        return False


def _active_catalog_row(row: Mapping[str, Any]) -> bool:
    availability = row.get("availability")
    queryability = row.get("queryability")
    return bool(
        isinstance(availability, Mapping)
        and availability.get("activation_states") == ["active"]
        and isinstance(queryability, Mapping)
        and queryability.get("queryable") is True
    )


def _catalog_fields(row: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    raw = row.get("fields")
    if not isinstance(raw, list) or not raw:
        raise CryptoFiveMinuteDataError("crypto_5m_catalog_fields_invalid")
    result: dict[str, Mapping[str, Any]] = {}
    for item in raw:
        if not isinstance(item, Mapping):
            raise CryptoFiveMinuteDataError("crypto_5m_catalog_fields_invalid")
        name = _text(item.get("name"), "crypto_5m_catalog_fields_invalid")
        if name in result:
            raise CryptoFiveMinuteDataError("crypto_5m_catalog_fields_invalid")
        result[name] = item
    return result


@dataclass(frozen=True)
class CryptoQueryFilterBinding:
    role: str
    field: str
    operator: str

    def __post_init__(self) -> None:
        _text(self.role, "crypto_5m_filter_role_invalid")
        _text(self.field, "crypto_5m_filter_field_invalid")
        _text(self.operator, "crypto_5m_filter_operator_invalid")


@dataclass(frozen=True)
class CryptoDatasetQueryProfile:
    """One catalog row plus an explicit bounded consumer query."""

    catalog_version: str
    dataset_id: str
    schema_major: int
    selected_fields: tuple[str, ...]
    query_order: tuple[str, ...]
    identity_fields: tuple[str, ...]
    filter_bindings: tuple[CryptoQueryFilterBinding, ...]
    catalog_contract_sha256: str
    page_limit: int
    max_pages: int
    max_rows: int

    def __post_init__(self) -> None:
        _text(self.catalog_version, "crypto_5m_catalog_version_invalid")
        _text(self.dataset_id, "crypto_5m_dataset_id_invalid")
        if (
            isinstance(self.schema_major, bool)
            or not isinstance(self.schema_major, int)
            or self.schema_major <= 0
        ):
            raise CryptoFiveMinuteDataError("crypto_5m_schema_major_invalid")
        selected = _strings(self.selected_fields, "crypto_5m_selected_fields_invalid")
        _strings(self.query_order, "crypto_5m_query_order_invalid")
        identity = _strings(self.identity_fields, "crypto_5m_identity_fields_invalid")
        if not set(identity).issubset(selected):
            raise CryptoFiveMinuteDataError("crypto_5m_identity_fields_missing")
        if (
            not isinstance(self.filter_bindings, tuple)
            or not self.filter_bindings
            or any(
                not isinstance(item, CryptoQueryFilterBinding)
                for item in self.filter_bindings
            )
        ):
            raise CryptoFiveMinuteDataError("crypto_5m_filter_bindings_invalid")
        roles = [binding.role for binding in self.filter_bindings]
        if len(roles) != len(set(roles)):
            raise CryptoFiveMinuteDataError("crypto_5m_filter_role_duplicated")
        for name in ("page_limit", "max_pages", "max_rows"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise CryptoFiveMinuteDataError(f"crypto_5m_{name}_invalid")
        if self.page_limit > self.max_rows:
            raise CryptoFiveMinuteDataError("crypto_5m_page_limit_exceeds_row_budget")
        if not _is_sha256(self.catalog_contract_sha256):
            raise CryptoFiveMinuteDataError("crypto_5m_catalog_contract_sha256_invalid")

    @property
    def consumer_profile_sha256(self) -> str:
        """Bind Crypto's bounded query choices separately from TD's contract."""

        return _sha256(
            {
                "dataset_id": self.dataset_id,
                "selected_fields": list(self.selected_fields),
                "query_order": list(self.query_order),
                "identity_fields": list(self.identity_fields),
                "filter_bindings": [
                    _canonical_value(binding) for binding in self.filter_bindings
                ],
                "page_limit": self.page_limit,
                "max_pages": self.max_pages,
                "max_rows": self.max_rows,
            }
        )

    @classmethod
    def from_catalog(
        cls,
        catalog: CatalogEnvelope,
        *,
        expected_catalog_version: str,
        dataset_id: str,
        expected_schema_major: int,
        selected_fields: tuple[str, ...],
        query_order: tuple[str, ...],
        identity_fields: tuple[str, ...],
        filter_bindings: tuple[CryptoQueryFilterBinding, ...],
        page_limit: int,
        max_pages: int,
        max_rows: int,
    ) -> "CryptoDatasetQueryProfile":
        if not isinstance(catalog, CatalogEnvelope):
            raise CryptoFiveMinuteDataError("crypto_5m_catalog_envelope_required")
        matches = [row for row in catalog.data if row.get("dataset_id") == dataset_id]
        if len(matches) != 1:
            raise CryptoFiveMinuteDataError("crypto_5m_dataset_catalog_row_missing")
        row = matches[0]
        if not _active_catalog_row(row):
            raise CryptoFiveMinuteDataError("crypto_5m_dataset_not_active")
        schema_major = row.get("schema_major")
        if (
            isinstance(schema_major, bool)
            or not isinstance(schema_major, int)
            or schema_major != expected_schema_major
        ):
            raise CryptoFiveMinuteDataError("crypto_5m_schema_major_drift")
        selected = _strings(selected_fields, "crypto_5m_selected_fields_invalid")
        default_fields = _strings(
            row.get("default_fields"),
            "crypto_5m_catalog_default_fields_invalid",
        )
        if selected != default_fields:
            raise CryptoFiveMinuteDataError("crypto_5m_selected_fields_drift")
        field_rows = _catalog_fields(row)
        if any(
            field not in field_rows or field_rows[field].get("selectable") is not True
            for field in selected
        ):
            raise CryptoFiveMinuteDataError("crypto_5m_selected_fields_not_selectable")
        order = _strings(query_order, "crypto_5m_query_order_invalid")
        order_fields: list[str] = []
        for term in order:
            parts = term.rsplit(":", 1)
            if (
                len(parts) != 2
                or parts[1] not in {"asc", "desc"}
                or parts[0] not in field_rows
                or field_rows[parts[0]].get("sortable") is not True
                or parts[0] in order_fields
            ):
                raise CryptoFiveMinuteDataError(
                    "crypto_5m_query_order_not_catalog_authorized"
                )
            order_fields.append(parts[0])
        identity = _strings(identity_fields, "crypto_5m_identity_fields_invalid")
        if not set(identity).issubset(selected):
            raise CryptoFiveMinuteDataError("crypto_5m_identity_fields_missing")
        catalog_identity = _strings(
            row.get("identity_fields"),
            "crypto_5m_catalog_identity_fields_invalid",
            allow_empty=True,
        )
        if identity != catalog_identity:
            raise CryptoFiveMinuteDataError("crypto_5m_identity_fields_drift")
        raw_operators = row.get("filter_operators")
        if not isinstance(raw_operators, Mapping):
            raise CryptoFiveMinuteDataError(
                "crypto_5m_catalog_filter_operators_invalid"
            )
        for binding in filter_bindings:
            advertised = raw_operators.get(binding.field)
            if (
                not isinstance(binding, CryptoQueryFilterBinding)
                or not isinstance(advertised, list)
                or binding.operator not in advertised
            ):
                raise CryptoFiveMinuteDataError(
                    "crypto_5m_filter_binding_not_catalog_authorized"
                )
        limits = row.get("limits")
        max_page_size = (
            limits.get("max_page_size") if isinstance(limits, Mapping) else None
        )
        if (
            isinstance(max_page_size, bool)
            or not isinstance(max_page_size, int)
            or max_page_size <= 0
            or page_limit > max_page_size
        ):
            raise CryptoFiveMinuteDataError("crypto_5m_page_limit_exceeds_catalog")
        try:
            canonical_contract_sha256 = dataset_contract_fingerprint(row)
        except ValueError as exc:
            raise CryptoFiveMinuteDataError(
                "crypto_5m_catalog_contract_invalid"
            ) from exc
        return cls(
            catalog_version=expected_catalog_version,
            dataset_id=dataset_id,
            schema_major=expected_schema_major,
            selected_fields=selected,
            query_order=order,
            identity_fields=identity,
            filter_bindings=tuple(filter_bindings),
            catalog_contract_sha256=canonical_contract_sha256,
            page_limit=page_limit,
            max_pages=max_pages,
            max_rows=max_rows,
        )

    def verify_catalog(self, catalog: CatalogEnvelope) -> dict[str, Any]:
        rebuilt = type(self).from_catalog(
            catalog,
            expected_catalog_version=self.catalog_version,
            dataset_id=self.dataset_id,
            expected_schema_major=self.schema_major,
            selected_fields=self.selected_fields,
            query_order=self.query_order,
            identity_fields=self.identity_fields,
            filter_bindings=self.filter_bindings,
            page_limit=self.page_limit,
            max_pages=self.max_pages,
            max_rows=self.max_rows,
        )
        if rebuilt != self:
            raise CryptoFiveMinuteDataError("crypto_5m_catalog_contract_drift")
        return {
            "expected_catalog_version": self.catalog_version,
            "observed_catalog_version": catalog.catalog_version,
            "catalog_version_drift": catalog.catalog_version != self.catalog_version,
        }

    @property
    def filter_roles(self) -> frozenset[str]:
        return frozenset(binding.role for binding in self.filter_bindings)

    def to_payload(self) -> dict[str, Any]:
        return _canonical_value(self)


@dataclass(frozen=True)
class CryptoSymbolDatasetBinding:
    symbol: str
    bars: CryptoDatasetQueryProfile
    instrument_rules: CryptoDatasetQueryProfile

    def __post_init__(self) -> None:
        if self.symbol not in ALLOWED_SYMBOLS:
            raise CryptoFiveMinuteDataError("crypto_5m_binding_symbol_invalid")
        if (
            not isinstance(self.bars, CryptoDatasetQueryProfile)
            or not isinstance(self.instrument_rules, CryptoDatasetQueryProfile)
            or self.bars.dataset_id == self.instrument_rules.dataset_id
            or self.bars.catalog_version != self.instrument_rules.catalog_version
        ):
            raise CryptoFiveMinuteDataError("crypto_5m_symbol_dataset_binding_invalid")
        if self.bars.filter_roles != _BAR_FILTER_ROLES:
            raise CryptoFiveMinuteDataError("crypto_5m_bar_filter_roles_invalid")
        if self.instrument_rules.filter_roles != _RULE_FILTER_ROLES:
            raise CryptoFiveMinuteDataError("crypto_5m_rule_filter_roles_invalid")


@dataclass(frozen=True)
class CryptoBarFieldMap:
    symbol: str
    open_time: str
    close_time: str
    open: str
    high: str
    low: str
    close: str
    volume: str
    quote_volume: str
    trade_count: str

    def __post_init__(self) -> None:
        _strings(
            tuple(getattr(self, field.name) for field in fields(self)),
            "crypto_5m_bar_field_map_invalid",
        )


@dataclass(frozen=True)
class CryptoInstrumentRuleFieldMap:
    symbol: str
    status: str
    base_asset: str
    quote_asset: str
    price_tick: str
    quantity_step: str
    min_quantity: str
    min_notional: str

    def __post_init__(self) -> None:
        _strings(
            tuple(getattr(self, field.name) for field in fields(self)),
            "crypto_5m_rule_field_map_invalid",
        )


@dataclass(frozen=True)
class CryptoFiveMinuteDataProfile:
    """Four candidate dataset bindings plus Crypto-owned interpretation."""

    mode: str
    catalog_version: str
    symbols: tuple[CryptoSymbolDatasetBinding, ...]
    bar_fields: CryptoBarFieldMap
    rule_fields: CryptoInstrumentRuleFieldMap
    bar_close_time_semantics: str
    bar_closed_semantics: str
    active_rule_status: str
    max_bar_observation_lag_seconds: int
    max_rule_observation_lag_seconds: int

    def __post_init__(self) -> None:
        if self.mode not in PROFILE_MODES:
            raise CryptoFiveMinuteDataError("crypto_5m_profile_mode_invalid")
        _text(self.catalog_version, "crypto_5m_catalog_version_invalid")
        if not isinstance(self.symbols, tuple) or tuple(
            binding.symbol for binding in self.symbols
        ) != tuple(sorted(ALLOWED_SYMBOLS)):
            raise CryptoFiveMinuteDataError("crypto_5m_profile_symbols_invalid")
        dataset_ids: list[str] = []
        for binding in self.symbols:
            if (
                not isinstance(binding, CryptoSymbolDatasetBinding)
                or binding.bars.catalog_version != self.catalog_version
                or binding.instrument_rules.catalog_version != self.catalog_version
            ):
                raise CryptoFiveMinuteDataError(
                    "crypto_5m_profile_dataset_binding_invalid"
                )
            dataset_ids.extend(
                (
                    binding.bars.dataset_id,
                    binding.instrument_rules.dataset_id,
                )
            )
        if len(dataset_ids) != 4 or len(set(dataset_ids)) != 4:
            raise CryptoFiveMinuteDataError("crypto_5m_profile_dataset_binding_invalid")
        if not isinstance(self.bar_fields, CryptoBarFieldMap) or not isinstance(
            self.rule_fields, CryptoInstrumentRuleFieldMap
        ):
            raise CryptoFiveMinuteDataError("crypto_5m_profile_field_map_invalid")
        bar_fields = set(_canonical_value(self.bar_fields).values())
        rule_fields = set(_canonical_value(self.rule_fields).values())
        for binding in self.symbols:
            if not bar_fields.issubset(binding.bars.selected_fields):
                raise CryptoFiveMinuteDataError("crypto_5m_bar_fields_missing")
            if not rule_fields.issubset(binding.instrument_rules.selected_fields):
                raise CryptoFiveMinuteDataError("crypto_5m_rule_fields_missing")
        if self.bar_close_time_semantics != "inclusive_last_millisecond":
            raise CryptoFiveMinuteDataError("crypto_5m_close_time_semantics_invalid")
        if self.bar_closed_semantics != "dataset_contract_discards_open_bars":
            raise CryptoFiveMinuteDataError("crypto_5m_closed_semantics_invalid")
        if self.active_rule_status != "TRADING":
            raise CryptoFiveMinuteDataError("crypto_5m_active_rule_status_invalid")
        for name, upper in (
            ("max_bar_observation_lag_seconds", 600),
            ("max_rule_observation_lag_seconds", 86400),
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value <= 0
                or value > upper
            ):
                raise CryptoFiveMinuteDataError(f"crypto_5m_{name}_invalid")

    @property
    def sha256(self) -> str:
        return _sha256(self)

    def binding_for(self, symbol: str) -> CryptoSymbolDatasetBinding:
        matches = [binding for binding in self.symbols if binding.symbol == symbol]
        if len(matches) != 1:
            raise CryptoFiveMinuteDataError("crypto_5m_symbol_dataset_binding_missing")
        return matches[0]

    def verify_catalog(self, catalog: CatalogEnvelope) -> dict[str, Any]:
        dataset_evidence: list[dict[str, Any]] = []
        for binding in self.symbols:
            dataset_evidence.extend(
                (
                    binding.bars.verify_catalog(catalog),
                    binding.instrument_rules.verify_catalog(catalog),
                )
            )
        return {
            "expected_catalog_version": self.catalog_version,
            "observed_catalog_version": catalog.catalog_version,
            "catalog_version_drift": catalog.catalog_version != self.catalog_version,
            "dataset_contracts": dataset_evidence,
        }

    def to_payload(self) -> dict[str, Any]:
        return _canonical_value(self)


@dataclass(frozen=True)
class CryptoFiveMinuteWindowRequest:
    """The latest 13 closed bars available by one UTC observation cutoff."""

    window_end: datetime
    observation_cutoff: datetime
    symbols: tuple[str, ...] = ("BTCUSDT", "ETHUSDT")
    bar_count: int = REQUIRED_WINDOW_BARS

    def __post_init__(self) -> None:
        end = _utc_datetime(
            self.window_end,
            "crypto_5m_window_end_invalid",
            five_minute_aligned=True,
        )
        cutoff = _utc_datetime(
            self.observation_cutoff,
            "crypto_5m_observation_cutoff_invalid",
        )
        if cutoff < end:
            raise CryptoFiveMinuteDataError(
                "crypto_5m_observation_cutoff_precedes_window"
            )
        if self.symbols != tuple(sorted(ALLOWED_SYMBOLS)):
            raise CryptoFiveMinuteDataError("crypto_5m_symbols_must_be_frozen")
        if isinstance(self.bar_count, bool) or self.bar_count != REQUIRED_WINDOW_BARS:
            raise CryptoFiveMinuteDataError("crypto_5m_window_bar_count_invalid")
        object.__setattr__(self, "window_end", end)
        object.__setattr__(self, "observation_cutoff", cutoff)

    @property
    def window_start(self) -> datetime:
        return self.window_end - FIVE_MINUTES * self.bar_count

    @property
    def latest_open_time(self) -> datetime:
        return self.window_end - FIVE_MINUTES

    def to_payload(self) -> dict[str, Any]:
        return _canonical_value(self)


@dataclass(frozen=True)
class CryptoSourceProof:
    symbol: str
    dataset_kind: str
    dataset_id: str
    catalog_version: str
    receipt_id: str
    data_through: datetime
    observed_at: datetime
    lineage_sha256: str
    envelope_sha256: str
    page_count: int
    row_count: int
    pagination_trace_sha256: str
    semantic_sha256: str
    same_observation: bool = True

    def __post_init__(self) -> None:
        if self.symbol not in ALLOWED_SYMBOLS:
            raise CryptoFiveMinuteDataError("crypto_5m_source_proof_symbol_invalid")
        if self.dataset_kind not in {"closed_5m_bars", "instrument_rules"}:
            raise CryptoFiveMinuteDataError("crypto_5m_source_proof_kind_invalid")
        for name in ("dataset_id", "catalog_version", "receipt_id"):
            _text(getattr(self, name), f"crypto_5m_{name}_invalid")
        for name in (
            "lineage_sha256",
            "envelope_sha256",
            "pagination_trace_sha256",
            "semantic_sha256",
        ):
            if not _is_sha256(getattr(self, name)):
                raise CryptoFiveMinuteDataError(f"crypto_5m_{name}_invalid")
        data_through = _utc_datetime(
            self.data_through, "crypto_5m_data_through_invalid"
        )
        observed_at = _utc_datetime(self.observed_at, "crypto_5m_observed_at_invalid")
        if data_through > observed_at:
            raise CryptoFiveMinuteDataError("crypto_5m_data_through_after_observed_at")
        if (
            isinstance(self.page_count, bool)
            or not isinstance(self.page_count, int)
            or self.page_count <= 0
            or isinstance(self.row_count, bool)
            or not isinstance(self.row_count, int)
            or self.row_count < 0
            or self.same_observation is not True
        ):
            raise CryptoFiveMinuteDataError("crypto_5m_source_proof_invalid")

    def to_payload(self) -> dict[str, Any]:
        return _canonical_value(self)


@dataclass(frozen=True)
class CryptoFiveMinuteBarEvidence:
    symbol: str
    open_time: datetime
    close_time: datetime
    source_close_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    quote_volume: Decimal
    trade_count: int
    closed: bool
    source_row_sha256: str
    source_receipt_id: str
    source_lineage_sha256: str
    data_through: datetime
    observed_at: datetime

    def __post_init__(self) -> None:
        if self.symbol not in ALLOWED_SYMBOLS:
            raise CryptoFiveMinuteDataError("crypto_5m_symbol_invalid")
        open_time = _utc_datetime(
            self.open_time,
            "crypto_5m_open_time_invalid",
            five_minute_aligned=True,
        )
        close_time = _utc_datetime(
            self.close_time,
            "crypto_5m_close_time_invalid",
            five_minute_aligned=True,
        )
        source_close = _utc_datetime(
            self.source_close_time,
            "crypto_5m_source_close_time_invalid",
        )
        if (
            close_time != open_time + FIVE_MINUTES
            or source_close != close_time - BINANCE_INCLUSIVE_CLOSE_OFFSET
        ):
            raise CryptoFiveMinuteDataError("crypto_5m_close_time_semantics_invalid")
        if self.closed is not True:
            raise CryptoFiveMinuteDataError("crypto_5m_bar_not_closed")
        for name in ("open", "high", "low", "close"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise CryptoFiveMinuteDataError(f"crypto_5m_{name}_invalid")
        for name in ("volume", "quote_volume"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
                raise CryptoFiveMinuteDataError(f"crypto_5m_{name}_invalid")
        if (
            self.high < max(self.open, self.close)
            or self.low > min(self.open, self.close)
            or self.low > self.high
        ):
            raise CryptoFiveMinuteDataError("crypto_5m_ohlc_invalid")
        if (
            isinstance(self.trade_count, bool)
            or not isinstance(self.trade_count, int)
            or self.trade_count < 0
        ):
            raise CryptoFiveMinuteDataError("crypto_5m_trade_count_invalid")
        if not _is_sha256(self.source_row_sha256) or not _is_sha256(
            self.source_lineage_sha256
        ):
            raise CryptoFiveMinuteDataError("crypto_5m_source_digest_invalid")
        _text(
            self.source_receipt_id,
            "crypto_5m_source_receipt_id_invalid",
        )
        data_through = _utc_datetime(
            self.data_through,
            "crypto_5m_data_through_invalid",
        )
        observed_at = _utc_datetime(
            self.observed_at,
            "crypto_5m_observed_at_invalid",
        )
        if self.source_close_time > data_through or data_through > observed_at:
            raise CryptoFiveMinuteDataError("crypto_5m_bar_source_watermark_invalid")

    def to_payload(self) -> dict[str, Any]:
        return _canonical_value(self)


@dataclass(frozen=True)
class CryptoFiveMinuteSnapshot:
    profile_sha256: str
    request: CryptoFiveMinuteWindowRequest
    bars: tuple[CryptoFiveMinuteBarEvidence, ...]
    instrument_rules: tuple[SpotInstrumentRules, ...]
    source_proofs: tuple[CryptoSourceProof, ...]
    market_content_sha256: str
    observation_sha256: str
    same_observation: bool = True

    def __post_init__(self) -> None:
        if (
            not isinstance(self.request, CryptoFiveMinuteWindowRequest)
            or not isinstance(self.bars, tuple)
            or not all(
                isinstance(bar, CryptoFiveMinuteBarEvidence) for bar in self.bars
            )
            or not isinstance(self.instrument_rules, tuple)
            or not all(
                isinstance(rule, SpotInstrumentRules) for rule in self.instrument_rules
            )
            or not isinstance(self.source_proofs, tuple)
            or not all(
                isinstance(proof, CryptoSourceProof) for proof in self.source_proofs
            )
            or len(self.bars) != len(ALLOWED_SYMBOLS) * REQUIRED_WINDOW_BARS
            or len(self.instrument_rules) != len(ALLOWED_SYMBOLS)
            or len(self.source_proofs) != len(ALLOWED_SYMBOLS) * 2
            or self.same_observation is not True
        ):
            raise CryptoFiveMinuteDataError("crypto_5m_snapshot_incomplete")
        if any(
            not _is_sha256(value)
            for value in (
                self.profile_sha256,
                self.market_content_sha256,
                self.observation_sha256,
            )
        ):
            raise CryptoFiveMinuteDataError("crypto_5m_snapshot_digest_invalid")
        expected_symbols = tuple(sorted(ALLOWED_SYMBOLS))
        if tuple(rule.symbol for rule in self.instrument_rules) != expected_symbols:
            raise CryptoFiveMinuteDataError("crypto_5m_instrument_rules_incomplete")
        if tuple(bar.symbol for bar in self.bars) != tuple(
            symbol for symbol in expected_symbols for _ in range(REQUIRED_WINDOW_BARS)
        ):
            raise CryptoFiveMinuteDataError("crypto_5m_bar_order_or_gap_invalid")
        expected_proof_bindings = tuple(
            (symbol, dataset_kind)
            for symbol in expected_symbols
            for dataset_kind in ("closed_5m_bars", "instrument_rules")
        )
        actual_proof_bindings = tuple(
            (proof.symbol, proof.dataset_kind) for proof in self.source_proofs
        )
        if (
            actual_proof_bindings != expected_proof_bindings
            or len({proof.dataset_id for proof in self.source_proofs}) != 4
            or len({proof.catalog_version for proof in self.source_proofs}) != 1
        ):
            raise CryptoFiveMinuteDataError("crypto_5m_source_proof_binding_invalid")
        bar_proofs = {
            proof.symbol: proof
            for proof in self.source_proofs
            if proof.dataset_kind == "closed_5m_bars"
        }
        for bar in self.bars:
            proof = bar_proofs.get(bar.symbol)
            if (
                proof is None
                or bar.source_receipt_id != proof.receipt_id
                or bar.source_lineage_sha256 != proof.lineage_sha256
                or bar.data_through != proof.data_through
                or bar.observed_at != proof.observed_at
            ):
                raise CryptoFiveMinuteDataError("crypto_5m_bar_source_proof_mismatch")

        market_payload = {
            "bars": [_canonical_value(bar) for bar in self.bars],
            "instrument_rules": [
                _canonical_value(rules) for rules in self.instrument_rules
            ],
        }
        if self.market_content_sha256 != _sha256(market_payload):
            raise CryptoFiveMinuteDataError("crypto_5m_market_content_digest_mismatch")
        observation_payload = {
            "profile_sha256": self.profile_sha256,
            "request": self.request.to_payload(),
            "market_content_sha256": self.market_content_sha256,
            "source_proofs": [proof.to_payload() for proof in self.source_proofs],
            "same_observation": True,
            "execution_eligible": False,
            "execution_authority": False,
            "production_eligible": False,
        }
        if self.observation_sha256 != _sha256(observation_payload):
            raise CryptoFiveMinuteDataError("crypto_5m_observation_digest_mismatch")

    def bars_for(self, symbol: str) -> tuple[CryptoFiveMinuteBarEvidence, ...]:
        rows = tuple(bar for bar in self.bars if bar.symbol == symbol)
        if len(rows) != REQUIRED_WINDOW_BARS:
            raise CryptoFiveMinuteDataError("crypto_5m_window_incomplete")
        return rows

    def rules_for(self, symbol: str) -> SpotInstrumentRules:
        matches = [rules for rules in self.instrument_rules if rules.symbol == symbol]
        if len(matches) != 1:
            raise CryptoFiveMinuteDataError("crypto_5m_instrument_rules_incomplete")
        return matches[0]

    def proof_for(self, dataset_id: str) -> CryptoSourceProof:
        matches = [
            proof for proof in self.source_proofs if proof.dataset_id == dataset_id
        ]
        if len(matches) != 1:
            raise CryptoFiveMinuteDataError("crypto_5m_source_proof_missing")
        return matches[0]

    def verify_profile(self, profile: CryptoFiveMinuteDataProfile) -> None:
        if (
            not isinstance(profile, CryptoFiveMinuteDataProfile)
            or self.profile_sha256 != profile.sha256
        ):
            raise CryptoFiveMinuteDataError("crypto_5m_snapshot_profile_mismatch")
        expected_bindings = tuple(
            (
                binding.symbol,
                dataset_kind,
                dataset_profile.dataset_id,
            )
            for binding in profile.symbols
            for dataset_kind, dataset_profile in (
                ("closed_5m_bars", binding.bars),
                ("instrument_rules", binding.instrument_rules),
            )
        )
        actual_bindings = tuple(
            (
                proof.symbol,
                proof.dataset_kind,
                proof.dataset_id,
            )
            for proof in self.source_proofs
        )
        if actual_bindings != expected_bindings:
            raise CryptoFiveMinuteDataError(
                "crypto_5m_snapshot_profile_binding_mismatch"
            )

    def verify_against(
        self,
        *,
        profile: CryptoFiveMinuteDataProfile,
        request: CryptoFiveMinuteWindowRequest,
    ) -> None:
        self.__post_init__()
        self.verify_profile(profile)
        if (
            not isinstance(request, CryptoFiveMinuteWindowRequest)
            or self.request != request
        ):
            raise CryptoFiveMinuteDataError(
                "crypto_5m_snapshot_request_or_profile_mismatch"
            )
        expected_open_times = tuple(
            request.window_start + FIVE_MINUTES * index
            for index in range(REQUIRED_WINDOW_BARS)
        )
        proofs = {
            (proof.symbol, proof.dataset_kind): proof for proof in self.source_proofs
        }
        for binding in profile.symbols:
            bars = self.bars_for(binding.symbol)
            rules = self.rules_for(binding.symbol)
            bar_proof = proofs[(binding.symbol, "closed_5m_bars")]
            rule_proof = proofs[(binding.symbol, "instrument_rules")]
            if (
                tuple(bar.open_time for bar in bars) != expected_open_times
                or bars[0].open_time != request.window_start
                or bars[-1].close_time != request.window_end
                or bars[-1].source_close_time
                != request.window_end - BINANCE_INCLUSIVE_CLOSE_OFFSET
            ):
                raise CryptoFiveMinuteDataError(
                    "crypto_5m_snapshot_window_binding_mismatch"
                )
            if any(
                not _step_aligned(price, rules.price_tick)
                for bar in bars
                for price in (bar.open, bar.high, bar.low, bar.close)
            ):
                raise CryptoFiveMinuteDataError("crypto_5m_price_off_tick")
            if (
                bar_proof.row_count != REQUIRED_WINDOW_BARS
                or rule_proof.row_count != 1
                or bar_proof.data_through
                != request.window_end - BINANCE_INCLUSIVE_CLOSE_OFFSET
            ):
                raise CryptoFiveMinuteDataError(
                    "crypto_5m_snapshot_source_window_mismatch"
                )
            for proof, dataset_profile, max_lag_seconds in (
                (
                    bar_proof,
                    binding.bars,
                    profile.max_bar_observation_lag_seconds,
                ),
                (
                    rule_proof,
                    binding.instrument_rules,
                    profile.max_rule_observation_lag_seconds,
                ),
            ):
                if (
                    proof.page_count > dataset_profile.max_pages
                    or proof.row_count > dataset_profile.max_rows
                    or proof.observed_at > request.observation_cutoff
                ):
                    raise CryptoFiveMinuteDataError(
                        "crypto_5m_snapshot_source_budget_or_cutoff_invalid"
                    )
                maximum_lag = timedelta(seconds=max_lag_seconds)
                if (
                    proof.observed_at - proof.data_through > maximum_lag
                    or request.observation_cutoff - proof.data_through > maximum_lag
                ):
                    raise CryptoFiveMinuteDataError(
                        "crypto_5m_snapshot_source_freshness_invalid"
                    )

    def source_bindings(self) -> dict[str, Any]:
        return {proof.dataset_id: proof.to_payload() for proof in self.source_proofs}

    def to_payload(self) -> dict[str, Any]:
        return _canonical_value(self)


class CryptoFiveMinuteMarketDataPort(Protocol):
    def load_snapshot(
        self,
        *,
        profile: CryptoFiveMinuteDataProfile,
        request: CryptoFiveMinuteWindowRequest,
    ) -> CryptoFiveMinuteSnapshot: ...


def _query_twice(
    client: SharedSignalsV1Client,
    *,
    profile: CryptoDatasetQueryProfile,
    filters: Mapping[str, Any],
    as_of: datetime | None,
) -> PagedQueryRun:
    request = QueryRequest(
        dataset_id=profile.dataset_id,
        schema_major=profile.schema_major,
        fields=profile.selected_fields,
        filters=filters,
        as_of=(None if as_of is None else as_of.astimezone(timezone.utc).isoformat()),
        order=profile.query_order,
        limit=profile.page_limit,
    )
    first = collect_query_pages(
        client=client,
        request=request,
        identity_fields=profile.identity_fields,
        max_pages=profile.max_pages,
        max_rows=profile.max_rows,
    )
    replay = collect_query_pages(
        client=client,
        request=request,
        identity_fields=profile.identity_fields,
        max_pages=profile.max_pages,
        max_rows=profile.max_rows,
    )
    first.verify_integrity(identity_fields=profile.identity_fields)
    replay.verify_integrity(identity_fields=profile.identity_fields)
    if (
        first.semantic_sha256 != replay.semantic_sha256
        or first.semantic_trace_sha256 != replay.semantic_trace_sha256
        or first.ordered_rows_sha256 != replay.ordered_rows_sha256
    ):
        raise CryptoFiveMinuteDataError("crypto_5m_same_observation_mismatch")
    return first


def _complete_lineage(value: Any) -> bool:
    providers = value.get("providers") if isinstance(value, Mapping) else None
    return bool(
        isinstance(value, Mapping)
        and value.get("complete") is True
        and value.get("provider_neutral") is True
        and isinstance(providers, list)
        and providers
        and all(
            isinstance(provider, str) and provider and provider == provider.strip()
            for provider in providers
        )
        and len(providers) == len(set(providers))
        and isinstance(value.get("transport_service"), str)
        and bool(value.get("transport_service"))
    )


def _source_proof(
    run: PagedQueryRun,
    *,
    profile: CryptoDatasetQueryProfile,
    symbol: str,
    dataset_kind: str,
    cutoff: datetime,
    max_lag_seconds: int,
) -> CryptoSourceProof:
    envelope = run.envelope
    metadata = envelope.metadata
    if envelope.dataset_id != profile.dataset_id:
        raise CryptoFiveMinuteDataError("crypto_5m_query_binding_mismatch")
    if metadata.state.strip().lower() != "ready" or metadata.degraded is not False:
        raise CryptoFiveMinuteDataError("crypto_5m_metadata_not_ready")
    if (
        str(metadata.freshness.get("state") or "").strip().lower() != "fresh"
        or metadata.freshness.get("stale") is not False
    ):
        raise CryptoFiveMinuteDataError("crypto_5m_metadata_not_fresh")
    if str(metadata.quality.get("state") or "").strip().lower() != "valid":
        raise CryptoFiveMinuteDataError("crypto_5m_metadata_quality_invalid")
    if not _complete_lineage(metadata.lineage):
        raise CryptoFiveMinuteDataError("crypto_5m_metadata_lineage_incomplete")
    receipt_id = _text(
        metadata.receipt_id,
        "crypto_5m_metadata_receipt_missing",
    )
    data_through = _utc_datetime(
        metadata.data_through, "crypto_5m_data_through_invalid"
    )
    observed_at = _utc_datetime(metadata.observed_at, "crypto_5m_observed_at_invalid")
    if data_through > observed_at:
        raise CryptoFiveMinuteDataError("crypto_5m_data_through_after_observed_at")
    if observed_at > cutoff:
        raise CryptoFiveMinuteDataError("crypto_5m_observation_after_cutoff")
    if observed_at - data_through > timedelta(seconds=max_lag_seconds):
        raise CryptoFiveMinuteDataError("crypto_5m_observation_stale")
    if cutoff - data_through > timedelta(seconds=max_lag_seconds):
        raise CryptoFiveMinuteDataError("crypto_5m_observation_stale_by_cutoff")
    assert metadata.lineage is not None
    return CryptoSourceProof(
        symbol=symbol,
        dataset_kind=dataset_kind,
        dataset_id=profile.dataset_id,
        catalog_version=envelope.catalog_version,
        receipt_id=receipt_id,
        data_through=data_through,
        observed_at=observed_at,
        lineage_sha256=_sha256(metadata.lineage),
        envelope_sha256=_sha256(
            {
                "dataset_id": envelope.dataset_id,
                "catalog_version": envelope.catalog_version,
                "receipt_id": receipt_id,
                "data_through": metadata.data_through,
                "observed_at": metadata.observed_at,
                "freshness": metadata.freshness,
                "quality": metadata.quality,
                "lineage": metadata.lineage,
            }
        ),
        page_count=run.page_count,
        row_count=run.row_count,
        pagination_trace_sha256=run.pagination_trace_sha256,
        semantic_sha256=run.semantic_sha256,
    )


def _exact_row_fields(
    row: Mapping[str, Any],
    expected: tuple[str, ...],
    reason: str,
) -> None:
    if set(row) != set(expected):
        raise CryptoFiveMinuteDataError(reason)


def _map_rules(
    run: PagedQueryRun,
    *,
    binding: CryptoSymbolDatasetBinding,
    profile: CryptoFiveMinuteDataProfile,
    cutoff: datetime,
) -> tuple[SpotInstrumentRules, CryptoSourceProof]:
    proof = _source_proof(
        run,
        profile=binding.instrument_rules,
        symbol=binding.symbol,
        dataset_kind="instrument_rules",
        cutoff=cutoff,
        max_lag_seconds=profile.max_rule_observation_lag_seconds,
    )
    if len(run.envelope.data) != 1:
        raise CryptoFiveMinuteDataError("crypto_5m_instrument_rules_incomplete")
    row = run.envelope.data[0]
    _exact_row_fields(
        row,
        binding.instrument_rules.selected_fields,
        "crypto_5m_instrument_rule_fields_invalid",
    )
    fields_map = profile.rule_fields
    if row.get(fields_map.symbol) != binding.symbol:
        raise CryptoFiveMinuteDataError("crypto_5m_instrument_symbol_invalid")
    if row.get(fields_map.status) != profile.active_rule_status:
        raise CryptoFiveMinuteDataError("crypto_5m_instrument_not_trading")
    try:
        rules = SpotInstrumentRules(
            symbol=binding.symbol,
            base_asset=_text(
                row.get(fields_map.base_asset),
                "crypto_5m_base_asset_invalid",
            ),
            quote_asset=_text(
                row.get(fields_map.quote_asset),
                "crypto_5m_quote_asset_invalid",
            ),
            price_tick=_decimal_text(
                row.get(fields_map.price_tick),
                "crypto_5m_price_tick_invalid",
                positive=True,
            ),
            quantity_step=_decimal_text(
                row.get(fields_map.quantity_step),
                "crypto_5m_quantity_step_invalid",
                positive=True,
            ),
            min_quantity=_decimal_text(
                row.get(fields_map.min_quantity),
                "crypto_5m_min_quantity_invalid",
                positive=True,
            ),
            min_notional=_decimal_text(
                row.get(fields_map.min_notional),
                "crypto_5m_min_notional_invalid",
                positive=True,
            ),
        )
    except CryptoEvidenceError as exc:
        raise CryptoFiveMinuteDataError(str(exc)) from exc
    return rules, proof


def _map_bars(
    run: PagedQueryRun,
    *,
    binding: CryptoSymbolDatasetBinding,
    profile: CryptoFiveMinuteDataProfile,
    request: CryptoFiveMinuteWindowRequest,
    rules: SpotInstrumentRules,
) -> tuple[tuple[CryptoFiveMinuteBarEvidence, ...], CryptoSourceProof]:
    proof = _source_proof(
        run,
        profile=binding.bars,
        symbol=binding.symbol,
        dataset_kind="closed_5m_bars",
        cutoff=request.observation_cutoff,
        max_lag_seconds=profile.max_bar_observation_lag_seconds,
    )
    raw_rows = tuple(run.envelope.data)
    if len(raw_rows) != REQUIRED_WINDOW_BARS:
        raise CryptoFiveMinuteDataError("crypto_5m_window_incomplete")
    if proof.data_through != request.window_end - BINANCE_INCLUSIVE_CLOSE_OFFSET:
        raise CryptoFiveMinuteDataError("crypto_5m_data_through_mismatch")
    field_map = profile.bar_fields
    descending: list[CryptoFiveMinuteBarEvidence] = []
    for row in raw_rows:
        _exact_row_fields(
            row,
            binding.bars.selected_fields,
            "crypto_5m_bar_fields_invalid",
        )
        if row.get(field_map.symbol) != binding.symbol:
            raise CryptoFiveMinuteDataError("crypto_5m_symbol_invalid")
        open_time = _utc_datetime(
            row.get(field_map.open_time),
            "crypto_5m_open_time_invalid",
            five_minute_aligned=True,
        )
        source_close_time = _utc_datetime(
            row.get(field_map.close_time),
            "crypto_5m_source_close_time_invalid",
        )
        logical_close_time = open_time + FIVE_MINUTES
        if (
            source_close_time != logical_close_time - BINANCE_INCLUSIVE_CLOSE_OFFSET
            or source_close_time > request.observation_cutoff
        ):
            raise CryptoFiveMinuteDataError("crypto_5m_close_time_semantics_invalid")
        open_price = _decimal_text(
            row.get(field_map.open),
            "crypto_5m_open_invalid",
            positive=True,
        )
        high = _decimal_text(
            row.get(field_map.high),
            "crypto_5m_high_invalid",
            positive=True,
        )
        low = _decimal_text(
            row.get(field_map.low),
            "crypto_5m_low_invalid",
            positive=True,
        )
        close = _decimal_text(
            row.get(field_map.close),
            "crypto_5m_close_invalid",
            positive=True,
        )
        for price in (open_price, high, low, close):
            if not _step_aligned(price, rules.price_tick):
                raise CryptoFiveMinuteDataError("crypto_5m_price_off_tick")
        volume = _decimal_text(
            row.get(field_map.volume),
            "crypto_5m_volume_invalid",
            nonnegative=True,
        )
        quote_volume = _decimal_text(
            row.get(field_map.quote_volume),
            "crypto_5m_quote_volume_invalid",
            nonnegative=True,
        )
        trade_count = row.get(field_map.trade_count)
        if (
            isinstance(trade_count, bool)
            or not isinstance(trade_count, int)
            or trade_count < 0
        ):
            raise CryptoFiveMinuteDataError("crypto_5m_trade_count_invalid")
        if high < max(open_price, close) or low > min(open_price, close) or low > high:
            raise CryptoFiveMinuteDataError("crypto_5m_ohlc_invalid")
        descending.append(
            CryptoFiveMinuteBarEvidence(
                symbol=binding.symbol,
                open_time=open_time,
                close_time=logical_close_time,
                source_close_time=source_close_time,
                open=open_price,
                high=high,
                low=low,
                close=close,
                volume=volume,
                quote_volume=quote_volume,
                trade_count=trade_count,
                closed=True,
                source_row_sha256=_sha256(row),
                source_receipt_id=proof.receipt_id,
                source_lineage_sha256=proof.lineage_sha256,
                data_through=proof.data_through,
                observed_at=proof.observed_at,
            )
        )
    expected_descending = tuple(
        request.latest_open_time - FIVE_MINUTES * index
        for index in range(REQUIRED_WINDOW_BARS)
    )
    if tuple(bar.open_time for bar in descending) != expected_descending:
        raise CryptoFiveMinuteDataError("crypto_5m_bar_order_or_gap_invalid")
    ascending = tuple(reversed(descending))
    if (
        ascending[0].open_time != request.window_start
        or ascending[-1].close_time != request.window_end
        or proof.data_through != ascending[-1].source_close_time
    ):
        reason = (
            "crypto_5m_data_through_mismatch"
            if proof.data_through != ascending[-1].source_close_time
            else "crypto_5m_window_incomplete"
        )
        raise CryptoFiveMinuteDataError(reason)
    return ascending, proof


class TradingDatasCryptoFiveMinuteDataPort:
    """Read four explicitly profiled datasets through the shared V1 client."""

    def __init__(self, client: SharedSignalsV1Client) -> None:
        if not isinstance(client, SharedSignalsV1Client):
            raise TypeError("client must be SharedSignalsV1Client")
        self._client = client

    def load_snapshot(
        self,
        *,
        profile: CryptoFiveMinuteDataProfile,
        request: CryptoFiveMinuteWindowRequest,
    ) -> CryptoFiveMinuteSnapshot:
        if not isinstance(profile, CryptoFiveMinuteDataProfile):
            raise TypeError("profile must be CryptoFiveMinuteDataProfile")
        if not isinstance(request, CryptoFiveMinuteWindowRequest):
            raise TypeError("request must be CryptoFiveMinuteWindowRequest")
        try:
            catalog = self._client.get_catalog()
            profile.verify_catalog(catalog)
            all_bars: list[CryptoFiveMinuteBarEvidence] = []
            all_rules: list[SpotInstrumentRules] = []
            all_proofs: list[CryptoSourceProof] = []
            for symbol in request.symbols:
                binding = profile.binding_for(symbol)
                rules_run = _query_twice(
                    self._client,
                    profile=binding.instrument_rules,
                    filters={
                        profile.rule_fields.symbol: {"eq": symbol},
                        profile.rule_fields.status: {"eq": profile.active_rule_status},
                    },
                    as_of=None,
                )
                rules, rule_proof = _map_rules(
                    rules_run,
                    binding=binding,
                    profile=profile,
                    cutoff=request.observation_cutoff,
                )
                bars_run = _query_twice(
                    self._client,
                    profile=binding.bars,
                    filters={
                        profile.bar_fields.symbol: {"eq": symbol},
                        profile.bar_fields.open_time: {
                            "between": [
                                request.window_start.astimezone(
                                    timezone.utc
                                ).isoformat(),
                                request.latest_open_time.astimezone(
                                    timezone.utc
                                ).isoformat(),
                            ]
                        },
                    },
                    as_of=request.observation_cutoff,
                )
                bars, bar_proof = _map_bars(
                    bars_run,
                    binding=binding,
                    profile=profile,
                    request=request,
                    rules=rules,
                )
                all_bars.extend(bars)
                all_rules.append(rules)
                all_proofs.extend((bar_proof, rule_proof))
        except CryptoFiveMinuteDataError:
            raise
        except (PaginationContractError, SharedSignalsV1Error) as exc:
            raise CryptoFiveMinuteDataError(str(exc)) from exc
        except (TypeError, ValueError) as exc:
            raise CryptoFiveMinuteDataError("crypto_5m_contract_failure") from exc

        market_payload = {
            "bars": [_canonical_value(bar) for bar in all_bars],
            "instrument_rules": [_canonical_value(rules) for rules in all_rules],
        }
        market_content_sha256 = _sha256(market_payload)
        observation_payload = {
            "profile_sha256": profile.sha256,
            "request": request.to_payload(),
            "market_content_sha256": market_content_sha256,
            "source_proofs": [proof.to_payload() for proof in all_proofs],
            "same_observation": True,
            "execution_eligible": False,
            "execution_authority": False,
            "production_eligible": False,
        }
        return CryptoFiveMinuteSnapshot(
            profile_sha256=profile.sha256,
            request=request,
            bars=tuple(all_bars),
            instrument_rules=tuple(all_rules),
            source_proofs=tuple(all_proofs),
            market_content_sha256=market_content_sha256,
            observation_sha256=_sha256(observation_payload),
            same_observation=True,
        )


__all__ = [
    "BINANCE_INCLUSIVE_CLOSE_OFFSET",
    "CryptoBarFieldMap",
    "CryptoDatasetQueryProfile",
    "CryptoFiveMinuteBarEvidence",
    "CryptoFiveMinuteDataError",
    "CryptoFiveMinuteDataProfile",
    "CryptoFiveMinuteMarketDataPort",
    "CryptoFiveMinuteSnapshot",
    "CryptoFiveMinuteWindowRequest",
    "CryptoInstrumentRuleFieldMap",
    "CryptoQueryFilterBinding",
    "CryptoSourceProof",
    "CryptoSymbolDatasetBinding",
    "FIVE_MINUTES",
    "REQUIRED_WINDOW_BARS",
    "TradingDatasCryptoFiveMinuteDataPort",
]
