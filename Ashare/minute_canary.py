"""Read-only TradingDatas five-minute canary for the A-share lane.

The command consumes a secret-free, catalog-bound profile plus a separate
TA-owned reference-fact file.  It never calls a provider directly and cannot
create orders, mutate capital, or enable a scheduler.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import hashlib
import json
import math
import os
from pathlib import Path
import sys
from typing import Any
from zoneinfo import ZoneInfo

from Ashare.minute_data import (
    MinuteDataContractError,
    MinuteBarEvidence,
    MinuteBarSnapshot,
    MinuteDatasetProfile,
    MinuteEvidenceAuditLedger,
    MinuteEvidenceUse,
    MinuteReferenceFact,
    MinuteTimestampSemantics,
    TradingDatasMinuteMarketDataPort,
)
from shared.data.sharedsignals_v1 import (
    CatalogContractError,
    ContractViolation,
    HTTPTransport,
    HTTPStatusError,
    SharedSignalsV1Error,
    SharedSignalsV1Client,
    SharedSignalsV1Config,
    TransportNotConfigured,
)
from shared.data.tradingdatas_pagination import PaginationContractError
from shared.data.tradingdatas_transport import (
    RuntimeGateConfigurationError,
    TradingDatasAuthenticationError,
    build_runtime_transport,
)


class MinuteCanaryConfigurationError(ValueError):
    """Fail-closed canary configuration failure."""


TransportFactory = Callable[..., HTTPTransport]
SHANGHAI = ZoneInfo("Asia/Shanghai")
FIVE_MINUTES = timedelta(minutes=5)
SNAPSHOT_ROWS_CONTRACT = "tradingagent.ashare.minute_canary_snapshot_rows.v1"
_SNAPSHOT_ROW_NUMERIC_FIELDS = (
    "open_cny",
    "high_cny",
    "low_cny",
    "close_cny",
    "volume_shares",
    "amount_cny",
    "previous_close_cny",
)
_SNAPSHOT_ROW_FIELDS = frozenset(
    {
        "symbol",
        "bar_start",
        "bar_end",
        *_SNAPSHOT_ROW_NUMERIC_FIELDS,
        "suspended",
        "market_session",
        "dataset_id",
        "catalog_version",
        "receipt_id",
        "data_through",
        "observed_at",
        "available_at",
        "decision_time",
        "source_lineage_sha256",
        "envelope_proof_sha256",
        "source_row_sha256",
        "reference_evidence_sha256",
        "evidence_use",
    }
)


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise MinuteCanaryConfigurationError(f"{field_name}_invalid")
    return value


def _positive_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise MinuteCanaryConfigurationError(f"{field_name}_invalid")
    return value


def _mapping(value: object, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise MinuteCanaryConfigurationError(f"{field_name}_invalid")
    return value


def _optional_text(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _text(value, field_name)


_FAILURE_STAGES = frozenset(
    {
        "catalog_request",
        "catalog_contract",
        "query_request",
        "query_contract",
        "pagination",
        "auth",
        "transport",
        "configuration",
        "unknown",
    }
)
_FAILURE_CLASSES = frozenset(
    {
        "CatalogContractError",
        "ContractViolation",
        "HTTPStatusError",
        "PaginationContractError",
        "RuntimeGateConfigurationError",
        "SharedSignalsV1Error",
        "TradingDatasAuthenticationError",
        "TransportNotConfigured",
        "unknown",
    }
)


def _failure_stage(error: BaseException) -> str:
    """Return the explicit bounded phase marker, never exception text."""

    marked = getattr(error, "failure_stage", None)
    if isinstance(marked, str) and marked in _FAILURE_STAGES:
        return marked
    if isinstance(error, MinuteDataContractError):
        return "unknown"

    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, TradingDatasAuthenticationError):
            return "auth"
        if isinstance(current, HTTPStatusError):
            return "unknown"
        if isinstance(current, PaginationContractError):
            return "pagination"
        if isinstance(current, CatalogContractError):
            return "unknown"
        if isinstance(current, RuntimeGateConfigurationError):
            return "configuration"
        if isinstance(current, TransportNotConfigured):
            return "transport"
        if isinstance(current, OSError):
            return "transport"
        if isinstance(current, ContractViolation):
            return "unknown"
        current = current.__cause__ or current.__context__
    if isinstance(error, SharedSignalsV1Error):
        return "unknown"
    return "unknown"


def _failure_class(error: BaseException) -> str:
    """Return only an allow-listed source class, never arbitrary type names."""

    marked = getattr(error, "failure_class", None)
    if isinstance(marked, str) and marked in _FAILURE_CLASSES:
        return marked

    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(
            current,
            (
                TradingDatasAuthenticationError,
                HTTPStatusError,
                PaginationContractError,
                CatalogContractError,
                RuntimeGateConfigurationError,
                TransportNotConfigured,
                ContractViolation,
                SharedSignalsV1Error,
            ),
        ):
            name = type(current).__name__
            return name if name in _FAILURE_CLASSES else "unknown"
        current = current.__cause__ or current.__context__
    return "unknown"


def _failure_receipt(
    *,
    error: BaseException,
    dataset_id: str | None,
    requested: int,
    slot: str | None,
) -> dict[str, object]:
    reason = (
        error.reason_code
        if isinstance(error, MinuteDataContractError)
        else "minute_tradingdatas_request_failed"
    )
    return {
        "status": "failed_closed",
        "dataset_id": dataset_id,
        "reason_code": reason,
        "failure_stage": _failure_stage(error),
        "failure_class": _failure_class(error),
        "failure_count": 1,
        "requested": requested,
        "accepted": 0,
        "slot": slot,
        "receipt_lineage": False,
        "execution_authority": False,
        "execution_eligible": False,
        "learning_eligible": False,
        "promotion_authorized": False,
        "real_trading_enabled": False,
    }


def _canonical_sha256(value: object, field_name: str) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise MinuteCanaryConfigurationError(f"{field_name}_not_canonical") from exc
    return hashlib.sha256(encoded).hexdigest()


def _aware_iso(value: object, field_name: str) -> datetime:
    raw = _text(value, field_name)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MinuteCanaryConfigurationError(f"{field_name}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MinuteCanaryConfigurationError(f"{field_name}_timezone_required")
    return parsed


def _canonical_snapshot_row(bar: MinuteBarEvidence) -> dict[str, Any]:
    payload = bar.canonical_payload()
    if frozenset(payload) != _SNAPSHOT_ROW_FIELDS:
        raise MinuteCanaryConfigurationError("minute_snapshot_row_fields_invalid")
    for field_name in _SNAPSHOT_ROW_NUMERIC_FIELDS:
        value = payload[field_name]
        if (
            type(value) is not float
            or not math.isfinite(value)
        ):
            raise MinuteCanaryConfigurationError(
                "minute_snapshot_row_numeric_not_canonical"
            )
    _canonical_sha256(payload, "minute_snapshot_row")
    return payload


def _snapshot_rows_payload(
    snapshot: MinuteBarSnapshot,
    *,
    reference_facts: Mapping[str, MinuteReferenceFact],
    selected_bar_end: datetime | None,
) -> dict[str, Any]:
    if selected_bar_end is None:
        raise MinuteCanaryConfigurationError("minute_snapshot_exact_slot_required")
    bars = snapshot.bars
    symbols = [bar.symbol for bar in bars]
    if len(symbols) != len(set(symbols)):
        raise MinuteCanaryConfigurationError("minute_snapshot_duplicate_symbol")
    reference_symbols = set(reference_facts)
    if not set(symbols) <= reference_symbols:
        raise MinuteCanaryConfigurationError("minute_snapshot_universe_mismatch")
    bar_ends = {bar.bar_end for bar in bars}
    if len(bar_ends) != 1:
        raise MinuteCanaryConfigurationError("minute_snapshot_bar_end_mismatch")
    snapshot_bar_end = next(iter(bar_ends))
    if selected_bar_end is not None and snapshot_bar_end != selected_bar_end:
        raise MinuteCanaryConfigurationError("minute_snapshot_bar_end_mismatch")
    for field_name, values in (
        ("receipt", {bar.receipt_id for bar in bars}),
        ("lineage", {bar.source_lineage_sha256 for bar in bars}),
        ("envelope", {bar.envelope_proof_sha256 for bar in bars}),
        ("data_through", {bar.data_through for bar in bars}),
        ("catalog", {bar.catalog_version for bar in bars}),
    ):
        if len(values) != 1:
            raise MinuteCanaryConfigurationError(
                f"minute_snapshot_{field_name}_binding_mismatch"
            )
    rows = [_canonical_snapshot_row(bar) for bar in bars]
    if len(rows) != snapshot.row_count:
        raise MinuteCanaryConfigurationError("minute_snapshot_row_count_mismatch")
    return {
        "contractId": SNAPSHOT_ROWS_CONTRACT,
        "count": len(rows),
        "sha256": _canonical_sha256(rows, "minute_snapshot_rows"),
        "items": rows,
    }


@dataclass(frozen=True)
class MinuteCanaryConfig:
    """External, secret-free runtime inputs for one bounded canary."""

    base_url: str
    expected_catalog_version: str
    dataset_id: str
    access_policy_id: str
    transport_id: str
    timeout_seconds: float
    filters: Mapping[str, Any]
    profile: Mapping[str, Any]

    def __post_init__(self) -> None:
        for field_name in (
            "base_url",
            "expected_catalog_version",
            "dataset_id",
            "access_policy_id",
            "transport_id",
        ):
            _text(getattr(self, field_name), field_name)
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or self.timeout_seconds <= 0
        ):
            raise MinuteCanaryConfigurationError("timeout_seconds_invalid")
        _mapping(self.filters, "filters")
        _mapping(self.profile, "profile")

    @property
    def page_limit(self) -> int:
        return _positive_int(self.profile.get("page_limit"), "profile_page_limit")

    def client_config(self) -> SharedSignalsV1Config:
        """Return the query client after catalog-bound profile validation.

        ``evidence_only`` does not make a global catalog version authoritative:
        the target row is independently fingerprinted before any query and each
        query envelope remains bound to the catalog this client observed.
        """

        return SharedSignalsV1Config(
            base_url=self.base_url,
            expected_catalog_version=self.expected_catalog_version,
            dataset_ids=frozenset({self.dataset_id}),
            access_policy_id=self.access_policy_id,
            catalog_version_policy="evidence_only",
            timeout_seconds=float(self.timeout_seconds),
            max_limit=self.page_limit,
            cache_ttl_seconds=0,
        )

    def build_profile(
        self,
        client: SharedSignalsV1Client,
        *,
        require_declared_bindings: bool = True,
    ) -> MinuteDatasetProfile:
        values = self.profile
        try:
            timestamp_semantics = MinuteTimestampSemantics(
                _text(
                    values.get("timestamp_semantics"),
                    "profile_timestamp_semantics",
                )
            )
        except ValueError as exc:
            raise MinuteCanaryConfigurationError(
                "profile_timestamp_semantics_invalid"
            ) from exc
        identity_fields = values.get("identity_fields")
        if not isinstance(identity_fields, list) or not identity_fields:
            raise MinuteCanaryConfigurationError("profile_identity_fields_invalid")
        expected_fingerprint = values.get("dataset_contract_fingerprint")
        expected_consumer_sha = values.get("consumer_profile_sha256")
        if require_declared_bindings:
            expected_fingerprint = _text(
                expected_fingerprint,
                "profile_dataset_contract_fingerprint",
            )
            expected_consumer_sha = _text(
                expected_consumer_sha,
                "profile_consumer_profile_sha256",
            )
        elif expected_fingerprint is not None:
            expected_fingerprint = _text(
                expected_fingerprint,
                "profile_dataset_contract_fingerprint",
            )
        profile = MinuteDatasetProfile.from_catalog(
            client.get_catalog(),
            expected_catalog_version=self.expected_catalog_version,
            expected_dataset_contract_fingerprint=expected_fingerprint,
            dataset_id=self.dataset_id,
            identity_fields=tuple(
                _text(value, "profile_identity_field") for value in identity_fields
            ),
            symbol_field=_text(values.get("symbol_field"), "profile_symbol_field"),
            timestamp_field=_text(
                values.get("timestamp_field"), "profile_timestamp_field"
            ),
            open_field=_text(values.get("open_field"), "profile_open_field"),
            high_field=_text(values.get("high_field"), "profile_high_field"),
            low_field=_text(values.get("low_field"), "profile_low_field"),
            close_field=_text(values.get("close_field"), "profile_close_field"),
            volume_field=_text(values.get("volume_field"), "profile_volume_field"),
            amount_field=_text(values.get("amount_field"), "profile_amount_field"),
            previous_close_field=_optional_text(
                values.get("previous_close_field"),
                "profile_previous_close_field",
            ),
            suspension_field=_optional_text(
                values.get("suspension_field"), "profile_suspension_field"
            ),
            frequency_field=_optional_text(
                values.get("frequency_field"), "profile_frequency_field"
            ),
            frequency_value=_optional_text(
                values.get("frequency_value"), "profile_frequency_value"
            ),
            timestamp_format=_text(
                values.get("timestamp_format"), "profile_timestamp_format"
            ),
            timestamp_semantics=timestamp_semantics,
            volume_multiplier_to_shares=values.get("volume_multiplier_to_shares"),
            amount_multiplier_to_cny=values.get("amount_multiplier_to_cny"),
            price_adjustment=_text(
                values.get("price_adjustment"), "profile_price_adjustment"
            ),
            max_pages=_positive_int(values.get("max_pages"), "profile_max_pages"),
            max_rows=_positive_int(values.get("max_rows"), "profile_max_rows"),
            page_limit=self.page_limit,
        )
        if (
            expected_consumer_sha is not None
            and profile.consumer_profile_sha256 != expected_consumer_sha
        ):
            raise MinuteCanaryConfigurationError("profile_consumer_profile_drift")
        return profile


def load_minute_canary_config(path: Path | str) -> MinuteCanaryConfig:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MinuteCanaryConfigurationError("minute_canary_manifest_invalid") from exc
    value = _mapping(raw, "minute_canary_manifest")
    if "expected_catalog_version" in value:
        expected_catalog_version = value.get("expected_catalog_version")
        if "catalog_version" in value and (
            _text(expected_catalog_version, "expected_catalog_version")
            != _text(value.get("catalog_version"), "catalog_version")
        ):
            raise MinuteCanaryConfigurationError(
                "catalog_version_compatibility_mismatch"
            )
    else:
        expected_catalog_version = value.get("catalog_version")
    return MinuteCanaryConfig(
        base_url=value.get("base_url"),
        expected_catalog_version=expected_catalog_version,
        dataset_id=value.get("dataset_id"),
        access_policy_id=value.get("access_policy_id"),
        transport_id=value.get("transport_id"),
        timeout_seconds=value.get("timeout_seconds"),
        filters=_mapping(value.get("filters"), "filters"),
        profile=_mapping(value.get("profile"), "profile"),
    )


def load_reference_facts(path: Path | str) -> dict[str, MinuteReferenceFact]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MinuteCanaryConfigurationError(
            "minute_reference_manifest_invalid"
        ) from exc
    if not isinstance(raw, list) or not raw:
        raise MinuteCanaryConfigurationError("minute_reference_manifest_invalid")
    result: dict[str, MinuteReferenceFact] = {}
    for item in raw:
        row = _mapping(item, "minute_reference_row")
        symbol = _text(row.get("symbol"), "minute_reference_symbol").upper()
        try:
            trade_date = date.fromisoformat(
                _text(row.get("trade_date"), "minute_reference_trade_date")
            )
            fact = MinuteReferenceFact(
                symbol=symbol,
                trade_date=trade_date,
                previous_close_cny=row.get("previous_close_cny"),
                suspended=row.get("suspended"),
                evidence_sha256=row.get("evidence_sha256"),
            )
        except (ValueError, MinuteDataContractError) as exc:
            raise MinuteCanaryConfigurationError(
                "minute_reference_row_invalid"
            ) from exc
        if symbol in result:
            raise MinuteCanaryConfigurationError("minute_reference_duplicate_symbol")
        result[symbol] = fact
    return result


def _normalize_bar_end(
    value: str | datetime,
    *,
    timestamp_format: str,
) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        raw = value.strip()
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = datetime.strptime(raw, timestamp_format)
            except ValueError as exc:
                raise MinuteCanaryConfigurationError("bar_end_invalid") from exc
    else:
        raise MinuteCanaryConfigurationError("bar_end_invalid")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=SHANGHAI)
    return parsed.astimezone(SHANGHAI)


def _exact_slot_filters(
    config: MinuteCanaryConfig,
    profile: MinuteDatasetProfile,
    reference_facts: Mapping[str, MinuteReferenceFact],
    bar_end: datetime | None,
) -> Mapping[str, Any]:
    filters = dict(config.filters)
    filter_contract = dict(profile.filter_operators)
    symbols = tuple(sorted(reference_facts))
    if bar_end is not None and symbols:
        if "in" not in filter_contract.get(profile.symbol_field, ()):
            raise MinuteDataContractError("minute_symbol_filter_not_catalog_authorized")
        filters[profile.symbol_field] = {"in": list(symbols)}
    if bar_end is not None:
        if "eq" not in filter_contract.get(profile.timestamp_field, ()):
            raise MinuteDataContractError("minute_bar_end_filter_not_catalog_authorized")
        query_time = (
            bar_end
            if profile.timestamp_semantics is MinuteTimestampSemantics.BAR_END
            else bar_end - FIVE_MINUTES
        )
        filters[profile.timestamp_field] = {
            "eq": query_time.strftime(profile.timestamp_format)
        }
    return filters


def _validate_exact_selection(
    snapshot: MinuteBarSnapshot,
    *,
    reference_facts: Mapping[str, MinuteReferenceFact],
    bar_end: datetime | None,
) -> None:
    observed_symbols = {bar.symbol for bar in snapshot.bars}
    if not observed_symbols <= set(reference_facts):
        raise MinuteDataContractError("minute_reference_universe_mismatch")
    if bar_end is not None and any(bar.bar_end != bar_end for bar in snapshot.bars):
        raise MinuteDataContractError("minute_bar_end_mismatch")


def run_minute_canary(
    config: MinuteCanaryConfig,
    *,
    token_file: Path | str,
    decision_time: datetime,
    trading_date: date,
    reference_facts: Mapping[str, MinuteReferenceFact],
    bar_end: str | datetime | None = None,
    evidence_use: MinuteEvidenceUse = MinuteEvidenceUse.LOW_LATENCY_EXECUTION,
    transport_factory: TransportFactory = build_runtime_transport,
) -> dict[str, Any]:
    profile, snapshot, audit = load_minute_snapshot(
        config,
        token_file=token_file,
        decision_time=decision_time,
        trading_date=trading_date,
        reference_facts=reference_facts,
        bar_end=bar_end,
        evidence_use=evidence_use,
        transport_factory=transport_factory,
    )
    selected_bar_end = (
        _normalize_bar_end(bar_end, timestamp_format=profile.timestamp_format)
        if bar_end is not None
        else None
    )
    if selected_bar_end is not None:
        _validate_exact_selection(
            snapshot,
            reference_facts=reference_facts,
            bar_end=selected_bar_end,
        )
    snapshot_rows = None
    if selected_bar_end is not None:
        snapshot_rows = _snapshot_rows_payload(
            snapshot,
            reference_facts=reference_facts,
            selected_bar_end=selected_bar_end,
        )
    requested_symbols = set(reference_facts)
    accepted_symbols = {bar.symbol for bar in snapshot.bars}
    missing_symbols = sorted(requested_symbols - accepted_symbols)
    receipt_ids = sorted({bar.receipt_id for bar in snapshot.bars})
    data_through = sorted(
        {
            bar.data_through.astimezone(SHANGHAI).isoformat()
            for bar in snapshot.bars
        }
    )
    source_lineage_sha256 = sorted(
        {bar.source_lineage_sha256 for bar in snapshot.bars}
    )
    receipt_id = receipt_ids[0] if len(receipt_ids) == 1 else None
    data_through_value = data_through[0] if len(data_through) == 1 else None
    source_lineage_value = (
        source_lineage_sha256[0] if len(source_lineage_sha256) == 1 else None
    )
    receipt = {
        "status": "pass",
        "authority_tier": "observation_only",
        "evidence_use": evidence_use.value,
        "execution_latency_eligible": all(
            bar.execution_latency_eligible for bar in snapshot.bars
        ),
        "real_trading_enabled": False,
        "trading_date": trading_date.isoformat(),
        "decision_time": decision_time.isoformat(),
        "bar_end": (
            selected_bar_end.isoformat() if selected_bar_end is not None else None
        ),
        "reference_symbols": sorted(accepted_symbols),
        "accepted_symbols": sorted(accepted_symbols),
        "requested_symbols": sorted(requested_symbols),
        "dataset_id": profile.dataset_id,
        "expected_catalog_version": profile.expected_catalog_version,
        "observed_catalog_version": snapshot.observed_catalog_version,
        "catalog_version_drift": snapshot.catalog_version_drift,
        "dataset_contract_fingerprint": profile.dataset_contract_fingerprint,
        "consumer_profile_sha256": profile.consumer_profile_sha256,
        "row_count": snapshot.row_count,
        "quality_status": (
            "usable" if not missing_symbols else "usable_degraded"
        ),
        "requested_count": len(requested_symbols),
        "accepted_count": len(accepted_symbols),
        "missing_count": len(missing_symbols),
        "missing_symbols": missing_symbols,
        "page_count": snapshot.page_count,
        "same_observation": snapshot.same_observation,
        "lineage_complete": True,
        "snapshot_sha256": snapshot.sha256,
        "receipt_id": receipt_id,
        "data_through": data_through_value,
        "source_lineage_sha256": source_lineage_value,
        "receipt_ids": receipt_ids,
        "data_through_values": data_through,
        "source_lineage_sha256s": source_lineage_sha256,
        "replay": {
            "same_observation": snapshot.same_observation,
            "pagination_trace_sha256": snapshot.pagination_trace_sha256,
            "first_semantic_sha256": snapshot.first_semantic_sha256,
            "replay_semantic_sha256": snapshot.replay_semantic_sha256,
        },
        "bars": [
            {
                "symbol": bar.symbol,
                "bar_end": bar.bar_end.isoformat(),
                "receipt_id": bar.receipt_id,
                "data_through": bar.data_through.astimezone(SHANGHAI).isoformat(),
                "observed_at": bar.observed_at.isoformat(),
                "source_lineage_sha256": bar.source_lineage_sha256,
                "envelope_proof_sha256": bar.envelope_proof_sha256,
                "sha256": bar.sha256,
            }
            for bar in snapshot.bars
        ],
        "audit_rejections": len(audit.records()),
    }
    if snapshot_rows is not None:
        receipt["snapshot_rows"] = snapshot_rows
    return receipt


def snapshot_from_canary_receipt(
    receipt: Mapping[str, Any],
    *,
    profile: MinuteDatasetProfile,
) -> MinuteBarSnapshot:
    """Rebuild one accepted snapshot from a v1 canary artifact without I/O.

    The caller must provide the already-bound profile.  No catalog or provider
    access is performed here; every profile, row, receipt and replay binding
    is checked against the immutable artifact before a snapshot is returned.
    """

    value = _mapping(receipt, "minute_canary_receipt")
    if value.get("status") != "pass":
        raise MinuteCanaryConfigurationError("minute_canary_receipt_not_accepted")
    if value.get("real_trading_enabled") is not False:
        raise MinuteCanaryConfigurationError("real_trading_must_remain_disabled")
    rows_value = _mapping(value.get("snapshot_rows"), "minute_snapshot_rows")
    if rows_value.get("contractId") != SNAPSHOT_ROWS_CONTRACT:
        raise MinuteCanaryConfigurationError("minute_snapshot_rows_contract_invalid")
    items = rows_value.get("items")
    if not isinstance(items, list) or not items:
        raise MinuteCanaryConfigurationError("minute_snapshot_rows_items_invalid")
    count = _positive_int(rows_value.get("count"), "minute_snapshot_rows_count")
    if count != len(items):
        raise MinuteCanaryConfigurationError("minute_snapshot_rows_count_mismatch")
    rows_sha = _text(rows_value.get("sha256"), "minute_snapshot_rows_sha256")
    if len(rows_sha) != 64 or any(c not in "0123456789abcdef" for c in rows_sha):
        raise MinuteCanaryConfigurationError("minute_snapshot_rows_sha256_invalid")
    if _canonical_sha256(items, "minute_snapshot_rows") != rows_sha:
        raise MinuteCanaryConfigurationError("minute_snapshot_rows_sha256_mismatch")

    try:
        dataset_id = _text(value.get("dataset_id"), "minute_dataset_id")
        if dataset_id != profile.dataset_id:
            raise MinuteCanaryConfigurationError("minute_snapshot_profile_mismatch")
        if (
            _text(value.get("expected_catalog_version"), "minute_expected_catalog")
            != profile.expected_catalog_version
        ):
            raise MinuteCanaryConfigurationError("minute_snapshot_profile_mismatch")
        if (
            _text(value.get("observed_catalog_version"), "minute_observed_catalog")
            != profile.observed_catalog_version
        ):
            raise MinuteCanaryConfigurationError("minute_snapshot_profile_mismatch")
        if (
            _text(value.get("dataset_contract_fingerprint"), "minute_dataset_fingerprint")
            != profile.dataset_contract_fingerprint
        ):
            raise MinuteCanaryConfigurationError("minute_snapshot_profile_mismatch")
        if (
            _text(value.get("consumer_profile_sha256"), "minute_consumer_profile")
            != profile.consumer_profile_sha256
        ):
            raise MinuteCanaryConfigurationError("minute_snapshot_profile_mismatch")
        row_count = _positive_int(value.get("row_count"), "minute_snapshot_row_count")
        page_count = _positive_int(value.get("page_count"), "minute_snapshot_page_count")
        if row_count != count:
            raise MinuteCanaryConfigurationError("minute_snapshot_row_count_mismatch")
        reference_symbols = value.get("reference_symbols")
        if not isinstance(reference_symbols, list) or len(reference_symbols) != len(
            set(reference_symbols)
        ):
            raise MinuteCanaryConfigurationError("minute_snapshot_reference_symbols_invalid")
        reference_symbols = [
            _text(symbol, "minute_snapshot_reference_symbol")
            for symbol in reference_symbols
        ]
        if len(reference_symbols) != count:
            raise MinuteCanaryConfigurationError("minute_snapshot_reference_symbols_invalid")
        expected_bar_end = _aware_iso(value.get("bar_end"), "minute_snapshot_bar_end")
        receipt_id = _text(value.get("receipt_id"), "minute_snapshot_receipt")
        data_through = _aware_iso(value.get("data_through"), "minute_snapshot_data_through")
        lineage_sha = _text(value.get("source_lineage_sha256"), "minute_snapshot_lineage")
        if len(lineage_sha) != 64 or any(c not in "0123456789abcdef" for c in lineage_sha):
            raise MinuteCanaryConfigurationError("minute_snapshot_lineage_invalid")
        replay = _mapping(value.get("replay"), "minute_snapshot_replay")
        if replay.get("same_observation") is not True:
            raise MinuteCanaryConfigurationError("minute_same_observation_mismatch")
        pagination_trace = _text(replay.get("pagination_trace_sha256"), "minute_pagination_trace")
        first_semantic = _text(replay.get("first_semantic_sha256"), "minute_first_semantic")
        replay_semantic = _text(replay.get("replay_semantic_sha256"), "minute_replay_semantic")
        for field_name, digest in (
            ("minute_pagination_trace", pagination_trace),
            ("minute_first_semantic", first_semantic),
            ("minute_replay_semantic", replay_semantic),
        ):
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise MinuteCanaryConfigurationError(f"{field_name}_invalid")
        if first_semantic != replay_semantic:
            raise MinuteCanaryConfigurationError("minute_same_observation_mismatch")
    except TypeError as exc:
        raise MinuteCanaryConfigurationError("minute_snapshot_receipt_invalid") from exc

    bars: list[MinuteBarEvidence] = []
    for item in items:
        row = _mapping(item, "minute_snapshot_row")
        if frozenset(row) != _SNAPSHOT_ROW_FIELDS:
            raise MinuteCanaryConfigurationError("minute_snapshot_row_fields_invalid")
        for field_name in _SNAPSHOT_ROW_NUMERIC_FIELDS:
            numeric = row.get(field_name)
            if type(numeric) is not float or not math.isfinite(numeric):
                raise MinuteCanaryConfigurationError(
                    "minute_snapshot_row_numeric_not_canonical"
                )
        try:
            evidence = MinuteBarEvidence(
                symbol=_text(row.get("symbol"), "minute_snapshot_symbol").upper(),
                bar_start=_aware_iso(row.get("bar_start"), "minute_snapshot_bar_start"),
                bar_end=_aware_iso(row.get("bar_end"), "minute_snapshot_bar_end"),
                open_cny=row.get("open_cny"),
                high_cny=row.get("high_cny"),
                low_cny=row.get("low_cny"),
                close_cny=row.get("close_cny"),
                volume_shares=row.get("volume_shares"),
                amount_cny=row.get("amount_cny"),
                previous_close_cny=row.get("previous_close_cny"),
                suspended=row.get("suspended"),
                market_session=_text(row.get("market_session"), "minute_snapshot_session"),
                dataset_id=_text(row.get("dataset_id"), "minute_snapshot_dataset"),
                catalog_version=_text(row.get("catalog_version"), "minute_snapshot_catalog"),
                receipt_id=_text(row.get("receipt_id"), "minute_snapshot_receipt"),
                data_through=_aware_iso(row.get("data_through"), "minute_snapshot_data_through"),
                observed_at=_aware_iso(row.get("observed_at"), "minute_snapshot_observed_at"),
                available_at=_aware_iso(row.get("available_at"), "minute_snapshot_available_at"),
                decision_time=_aware_iso(row.get("decision_time"), "minute_snapshot_decision_time"),
                source_lineage_sha256=_text(
                    row.get("source_lineage_sha256"), "minute_snapshot_lineage"
                ),
                envelope_proof_sha256=_text(
                    row.get("envelope_proof_sha256"), "minute_snapshot_envelope"
                ),
                source_row_sha256=_text(
                    row.get("source_row_sha256"), "minute_snapshot_source_row"
                ),
                reference_evidence_sha256=_text(
                    row.get("reference_evidence_sha256"),
                    "minute_snapshot_reference_evidence",
                ),
                evidence_use=MinuteEvidenceUse(
                    _text(row.get("evidence_use"), "minute_snapshot_evidence_use")
                ),
            )
        except (MinuteDataContractError, ValueError, TypeError) as exc:
            raise MinuteCanaryConfigurationError("minute_snapshot_row_invalid") from exc
        if evidence.bar_end != expected_bar_end:
            raise MinuteCanaryConfigurationError("minute_snapshot_bar_end_mismatch")
        if (
            evidence.dataset_id != dataset_id
            or evidence.catalog_version != profile.observed_catalog_version
        ):
            raise MinuteCanaryConfigurationError("minute_snapshot_binding_mismatch")
        if evidence.receipt_id != receipt_id or evidence.data_through != data_through:
            raise MinuteCanaryConfigurationError("minute_snapshot_receipt_binding_mismatch")
        if evidence.source_lineage_sha256 != lineage_sha:
            raise MinuteCanaryConfigurationError("minute_snapshot_lineage_binding_mismatch")
        bars.append(evidence)

    if set(bar.symbol for bar in bars) != set(reference_symbols) or len(bars) != len(
        reference_symbols
    ):
        raise MinuteCanaryConfigurationError("minute_snapshot_reference_symbols_mismatch")
    if value.get("receipt_ids") != [receipt_id]:
        raise MinuteCanaryConfigurationError("minute_snapshot_receipt_binding_mismatch")
    data_through_values = value.get("data_through_values")
    if not isinstance(data_through_values, list) or len(data_through_values) != 1:
        raise MinuteCanaryConfigurationError("minute_snapshot_data_through_mismatch")
    try:
        if _aware_iso(data_through_values[0], "minute_snapshot_data_through") != data_through:
            raise MinuteCanaryConfigurationError("minute_snapshot_data_through_mismatch")
    except MinuteCanaryConfigurationError as exc:
        if exc.args and exc.args[0] == "minute_snapshot_data_through_mismatch":
            raise
        raise MinuteCanaryConfigurationError(
            "minute_snapshot_data_through_mismatch"
        ) from exc
    if value.get("source_lineage_sha256s") != [lineage_sha]:
        raise MinuteCanaryConfigurationError("minute_snapshot_lineage_binding_mismatch")
    legacy_rows = value.get("bars")
    if not isinstance(legacy_rows, list) or len(legacy_rows) != len(bars):
        raise MinuteCanaryConfigurationError("minute_snapshot_legacy_rows_mismatch")
    for legacy, bar in zip(legacy_rows, bars):
        legacy_row = _mapping(legacy, "minute_snapshot_legacy_row")
        for field_name, expected in (
            ("symbol", bar.symbol),
            ("receipt_id", bar.receipt_id),
            ("source_lineage_sha256", bar.source_lineage_sha256),
            ("envelope_proof_sha256", bar.envelope_proof_sha256),
            ("sha256", bar.sha256),
        ):
            if legacy_row.get(field_name) != expected:
                raise MinuteCanaryConfigurationError(
                    "minute_snapshot_legacy_rows_mismatch"
                )
        for field_name, expected in (
            ("bar_end", bar.bar_end),
            ("data_through", bar.data_through),
            ("observed_at", bar.observed_at),
        ):
            try:
                actual = _aware_iso(
                    legacy_row.get(field_name),
                    f"minute_snapshot_legacy_{field_name}",
                )
            except MinuteCanaryConfigurationError as exc:
                raise MinuteCanaryConfigurationError(
                    "minute_snapshot_legacy_rows_mismatch"
                ) from exc
            if actual != expected:
                raise MinuteCanaryConfigurationError(
                    "minute_snapshot_legacy_rows_mismatch"
                )

    try:
        snapshot = MinuteBarSnapshot(
            profile=profile,
            bars=tuple(bars),
            page_count=page_count,
            row_count=row_count,
            pagination_trace_sha256=pagination_trace,
            first_semantic_sha256=first_semantic,
            replay_semantic_sha256=replay_semantic,
            same_observation=True,
        )
    except MinuteDataContractError as exc:
        raise MinuteCanaryConfigurationError("minute_snapshot_invalid") from exc
    snapshot_sha = _text(value.get("snapshot_sha256"), "minute_snapshot_sha256")
    if len(snapshot_sha) != 64 or snapshot.sha256 != snapshot_sha:
        raise MinuteCanaryConfigurationError("minute_snapshot_sha256_mismatch")
    return snapshot


def load_minute_snapshot(
    config: MinuteCanaryConfig,
    *,
    token_file: Path | str,
    decision_time: datetime,
    trading_date: date,
    reference_facts: Mapping[str, MinuteReferenceFact],
    bar_end: str | datetime | None = None,
    evidence_use: MinuteEvidenceUse = MinuteEvidenceUse.LOW_LATENCY_EXECUTION,
    transport_factory: TransportFactory = build_runtime_transport,
) -> tuple[MinuteDatasetProfile, MinuteBarSnapshot, MinuteEvidenceAuditLedger]:
    """Load one exact-bar snapshot for observation or explicit delayed paper."""

    if os.environ.get("REAL_TRADING_ENABLED", "false").strip().lower() != "false":
        raise MinuteCanaryConfigurationError("real_trading_must_remain_disabled")
    transport = transport_factory(
        config.transport_id,
        token_file=token_file,
        base_url=config.base_url,
    )
    client = SharedSignalsV1Client(config.client_config(), transport=transport)
    profile = config.build_profile(client)
    audit = MinuteEvidenceAuditLedger()
    selected_bar_end = (
        _normalize_bar_end(bar_end, timestamp_format=profile.timestamp_format)
        if bar_end is not None
        else None
    )
    if selected_bar_end is not None and selected_bar_end.date() != trading_date:
        raise MinuteCanaryConfigurationError("bar_end_trade_date_mismatch")
    snapshot = TradingDatasMinuteMarketDataPort(client).load_snapshot(
        profile=profile,
        filters=_exact_slot_filters(
            config,
            profile,
            reference_facts,
            selected_bar_end,
        ),
        decision_time=decision_time,
        trading_dates=frozenset({trading_date}),
        audit_ledger=audit,
        reference_facts=reference_facts,
        evidence_use=evidence_use,
    )
    if selected_bar_end is not None:
        _validate_exact_selection(
            snapshot,
            reference_facts=reference_facts,
            bar_end=selected_bar_end,
        )
    return profile, snapshot, audit


def _write_receipt(path: Path, receipt: Mapping[str, Any]) -> None:
    encoded = (
        json.dumps(
            dict(receipt),
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except OSError as exc:
        raise MinuteCanaryConfigurationError(
            "minute_canary_receipt_persist_failed"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only A-share five-minute TradingDatas canary"
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--reference-facts", type=Path, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--decision-time", required=True)
    parser.add_argument("--trading-date", required=True)
    parser.add_argument(
        "--bar-end",
        help="optional exact completed bar_end to replay after later bars exist",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--evidence-use",
        choices=tuple(value.value for value in MinuteEvidenceUse),
        default=MinuteEvidenceUse.LOW_LATENCY_EXECUTION.value,
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    requested = 0
    dataset_id: str | None = None
    try:
        if not args.token_file.is_absolute():
            raise MinuteCanaryConfigurationError("token_file_must_be_absolute")
        manifest = load_minute_canary_config(args.manifest)
        dataset_id = manifest.dataset_id
        reference_facts = load_reference_facts(args.reference_facts)
        requested = len(reference_facts)
        receipt = run_minute_canary(
            manifest,
            token_file=args.token_file,
            decision_time=datetime.fromisoformat(args.decision_time),
            trading_date=date.fromisoformat(args.trading_date),
            reference_facts=reference_facts,
            bar_end=args.bar_end,
            evidence_use=MinuteEvidenceUse(args.evidence_use),
        )
        _write_receipt(args.output, receipt)
    except (
        MinuteCanaryConfigurationError,
        MinuteDataContractError,
        SharedSignalsV1Error,
        RuntimeGateConfigurationError,
        OSError,
        ValueError,
    ) as exc:
        failure = _failure_receipt(
            error=exc,
            dataset_id=dataset_id,
            requested=requested,
            slot=args.bar_end,
        )
        try:
            _write_receipt(args.output, failure)
        except (OSError, MinuteCanaryConfigurationError):
            pass
        print("minute canary failed closed", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    else:
        print(
            f"PASS dataset={receipt['dataset_id']} rows={receipt['row_count']} "
            f"pages={receipt['page_count']} replay={receipt['same_observation']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "MinuteCanaryConfig",
    "MinuteCanaryConfigurationError",
    "SNAPSHOT_ROWS_CONTRACT",
    "load_minute_canary_config",
    "load_minute_snapshot",
    "load_reference_facts",
    "main",
    "run_minute_canary",
    "snapshot_from_canary_receipt",
]
