"""Caller-invoked receipt-bound ``fut_daily`` current-partition mapping.

The caller injects the existing TradingDatas V1 client and an already validated
``fut_mapping`` snapshot. This module configures no transport, runtime,
persistence, timer, execution, or trading path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
from types import MappingProxyType
from typing import Any, Mapping

from CNFutures.fut_mapping_current_snapshot import (
    DATASET_ID as FUT_MAPPING_DATASET_ID,
    M_DCE_TS_CODE,
    SCHEMA_MAJOR as FUT_MAPPING_SCHEMA_MAJOR,
    FutMappingCurrentSnapshot,
)
from shared.data.sharedsignals_v1 import (
    QueryRequest,
    SharedSignalsV1Client,
    SharedSignalsV1Error,
)
from shared.data.tradingdatas_pagination import (
    PaginationContractError,
    collect_query_pages,
)


DATASET_ID = "cn.dataset.fut_daily"
SCHEMA_MAJOR = 1
IDENTITY_FIELDS = ("trade_date", "ts_code")
QUERY_FIELDS = (
    "trade_date",
    "ts_code",
    "open",
    "high",
    "low",
    "close",
    "settle",
    "vol",
    "oi",
)
RAW_DAILY_FIELDS = QUERY_FIELDS[2:]
QUERY_ORDER = ("trade_date:asc", "ts_code:asc")
PAGE_LIMIT = 500
MAX_PAGES = 3
MAX_ROWS = 1_000
_TRADE_DATE = re.compile(r"^[0-9]{8}$")
_M_DCE_SYMBOL = re.compile(r"^M[0-9]{3,4}\.DCE$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class FutDailyCurrentSnapshotConsumerError(ValueError):
    """Raised when a receipt-bound daily current snapshot is not consumable."""


@dataclass(frozen=True)
class FutDailyRawCurrentSnapshotFact:
    """One mapping-selected daily row, without authority beyond raw facts."""

    trade_date: str
    ts_code: str
    receipt_id: str
    lineage_sha256: str
    raw_values: Mapping[str, Any]
    stable: bool = False
    pit_authority: bool = False
    session_authority: bool = False
    simulation_ready: bool = False
    runtime_eligible: bool = False
    execution_eligible: bool = False
    trading_eligible: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "raw_values", MappingProxyType(dict(self.raw_values)))
        if (
            self.stable
            or self.pit_authority
            or self.session_authority
            or self.simulation_ready
            or self.runtime_eligible
            or self.execution_eligible
            or self.trading_eligible
        ):
            raise FutDailyCurrentSnapshotConsumerError("raw_fact_authority_invalid")


@dataclass(frozen=True)
class FutDailyCurrentSnapshot:
    """One bounded daily partition selected by a current mapping raw fact."""

    dataset_id: str
    schema_major: int
    catalog_version: str
    trade_date: str
    receipt_id: str
    lineage_sha256: str
    mapping_ts_code: str
    mapping_receipt_id: str
    mapping_lineage_sha256: str
    page_count: int
    row_count: int
    terminal_pagination: bool
    replay_verified: bool
    semantic_sha256: str
    pagination_trace_sha256: str
    as_of: None = None
    stable: bool = False
    pit_authority: bool = False
    session_authority: bool = False
    simulation_ready: bool = False
    runtime_eligible: bool = False
    execution_eligible: bool = False
    trading_eligible: bool = False
    facts: tuple[FutDailyRawCurrentSnapshotFact, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if (
            self.dataset_id != DATASET_ID
            or self.schema_major != SCHEMA_MAJOR
            or self.page_count < 1
            or self.page_count > MAX_PAGES
            or self.row_count < 1
            or self.row_count > MAX_ROWS
            or not self.terminal_pagination
            or not self.replay_verified
            or self.as_of is not None
            or self.stable
            or self.pit_authority
            or self.session_authority
            or self.simulation_ready
            or self.runtime_eligible
            or self.execution_eligible
            or self.trading_eligible
            or len(self.facts) != 1
            or self.facts[0].ts_code != self.mapping_ts_code
        ):
            raise FutDailyCurrentSnapshotConsumerError("snapshot_authority_invalid")


def load_fut_daily_current_snapshot(
    *,
    client: SharedSignalsV1Client,
    trade_date: str,
    expected_catalog_version: str,
    expected_receipt_id: str,
    expected_lineage_sha256: str,
    mapping_snapshot: FutMappingCurrentSnapshot,
) -> FutDailyCurrentSnapshot:
    """Map one current-day daily fact selected by a receipt-bound M mapping.

    This reads only the exact-day partition. The mapping selection is local, so
    the TradingDatas query never uses a compound filter. Neither snapshot
    supplies PIT, session, simulation, execution, or trading authority.
    """

    if not isinstance(client, SharedSignalsV1Client):
        raise TypeError("client must be SharedSignalsV1Client")
    normalized_trade_date = _trade_date(trade_date)
    expected_catalog = _text(expected_catalog_version, "expected_catalog_version")
    expected_receipt = _text(expected_receipt_id, "expected_receipt_id")
    expected_lineage = _sha256_text(expected_lineage_sha256, "expected_lineage_sha256")
    mapping_ts_code, mapping_receipt, mapping_lineage = _validate_mapping_snapshot(
        mapping_snapshot=mapping_snapshot,
        trade_date=normalized_trade_date,
    )

    try:
        catalog = client.get_catalog()
        if catalog.catalog_version != expected_catalog:
            raise FutDailyCurrentSnapshotConsumerError("catalog_version_mismatch")
        _validate_catalog_row(_single_catalog_row(catalog.data))
        request = QueryRequest(
            dataset_id=DATASET_ID,
            schema_major=SCHEMA_MAJOR,
            fields=QUERY_FIELDS,
            filters={"trade_date": {"eq": normalized_trade_date}},
            as_of=None,
            order=QUERY_ORDER,
            limit=PAGE_LIMIT,
        )
        first = collect_query_pages(
            client=client,
            request=request,
            identity_fields=IDENTITY_FIELDS,
            max_pages=MAX_PAGES,
            max_rows=MAX_ROWS,
        )
        replay = collect_query_pages(
            client=client,
            request=request,
            identity_fields=IDENTITY_FIELDS,
            max_pages=MAX_PAGES,
            max_rows=MAX_ROWS,
        )
    except FutDailyCurrentSnapshotConsumerError:
        raise
    except PaginationContractError as exc:
        raise FutDailyCurrentSnapshotConsumerError(str(exc)) from exc
    except SharedSignalsV1Error as exc:
        raise FutDailyCurrentSnapshotConsumerError("tradingdatas_read_failed") from exc

    if (
        first.semantic_sha256 != replay.semantic_sha256
        or first.semantic_trace_sha256 != replay.semantic_trace_sha256
    ):
        raise FutDailyCurrentSnapshotConsumerError("replay_drift")

    _validate_metadata(
        metadata=first.envelope.metadata,
        expected_receipt_id=expected_receipt,
        expected_lineage_sha256=expected_lineage,
    )
    fact = _map_mapping_selected_row(
        rows=first.envelope.data,
        trade_date=normalized_trade_date,
        mapping_ts_code=mapping_ts_code,
        receipt_id=expected_receipt,
        lineage_sha256=expected_lineage,
    )
    return FutDailyCurrentSnapshot(
        dataset_id=first.envelope.dataset_id,
        schema_major=SCHEMA_MAJOR,
        catalog_version=first.envelope.catalog_version,
        trade_date=normalized_trade_date,
        receipt_id=expected_receipt,
        lineage_sha256=expected_lineage,
        mapping_ts_code=mapping_ts_code,
        mapping_receipt_id=mapping_receipt,
        mapping_lineage_sha256=mapping_lineage,
        page_count=first.page_count,
        row_count=first.row_count,
        terminal_pagination=first.envelope.next_cursor is None,
        replay_verified=True,
        semantic_sha256=first.semantic_sha256,
        pagination_trace_sha256=first.pagination_trace_sha256,
        facts=(fact,),
    )


def _validate_mapping_snapshot(
    *, mapping_snapshot: FutMappingCurrentSnapshot, trade_date: str
) -> tuple[str, str, str]:
    if not isinstance(mapping_snapshot, FutMappingCurrentSnapshot):
        raise TypeError("mapping_snapshot must be FutMappingCurrentSnapshot")
    if mapping_snapshot.trade_date != trade_date:
        raise FutDailyCurrentSnapshotConsumerError("mapping_trade_date_mismatch")
    if (
        mapping_snapshot.dataset_id != FUT_MAPPING_DATASET_ID
        or mapping_snapshot.schema_major != FUT_MAPPING_SCHEMA_MAJOR
        or mapping_snapshot.as_of is not None
        or not mapping_snapshot.terminal_pagination
        or not mapping_snapshot.replay_verified
        or mapping_snapshot.stable
        or mapping_snapshot.pit_rollover_authority
        or mapping_snapshot.simulation_ready
        or mapping_snapshot.runtime_eligible
        or mapping_snapshot.execution_eligible
        or mapping_snapshot.trading_eligible
        or len(mapping_snapshot.facts) != 1
    ):
        raise FutDailyCurrentSnapshotConsumerError("mapping_snapshot_authority_invalid")
    mapping_fact = mapping_snapshot.facts[0]
    if (
        mapping_fact.trade_date != mapping_snapshot.trade_date
        or mapping_fact.ts_code != M_DCE_TS_CODE
    ):
        raise FutDailyCurrentSnapshotConsumerError("mapping_fact_trade_date_mismatch")
    if mapping_fact.receipt_id != mapping_snapshot.receipt_id:
        raise FutDailyCurrentSnapshotConsumerError("mapping_fact_receipt_mismatch")
    if mapping_fact.lineage_sha256 != mapping_snapshot.lineage_sha256:
        raise FutDailyCurrentSnapshotConsumerError("mapping_fact_lineage_mismatch")
    raw_values = mapping_fact.raw_values
    mapping_ts_code = _text(raw_values.get("mapping_ts_code"), "mapping_ts_code")
    if not _M_DCE_SYMBOL.fullmatch(mapping_ts_code):
        raise FutDailyCurrentSnapshotConsumerError("mapping_ts_code_invalid")
    return (
        mapping_ts_code,
        _text(mapping_snapshot.receipt_id, "mapping_receipt_id"),
        _sha256_text(mapping_snapshot.lineage_sha256, "mapping_lineage_sha256"),
    )


def _single_catalog_row(rows: tuple[dict[str, Any], ...]) -> Mapping[str, Any]:
    matches = [row for row in rows if row.get("dataset_id") == DATASET_ID]
    if len(matches) != 1:
        raise FutDailyCurrentSnapshotConsumerError("catalog_dataset_missing_or_duplicate")
    return matches[0]


def _validate_catalog_row(row: Mapping[str, Any]) -> None:
    if row.get("schema_major") != SCHEMA_MAJOR:
        raise FutDailyCurrentSnapshotConsumerError("catalog_schema_invalid")
    if tuple(_text_list(row.get("identity_fields"), "catalog.identity_fields")) != IDENTITY_FIELDS:
        raise FutDailyCurrentSnapshotConsumerError("catalog_identity_invalid")
    if tuple(_text_list(row.get("default_order"), "catalog.default_order")) != QUERY_ORDER:
        raise FutDailyCurrentSnapshotConsumerError("catalog_order_invalid")
    fields = _text_list(row.get("default_fields"), "catalog.default_fields")
    if not set(QUERY_FIELDS).issubset(fields):
        raise FutDailyCurrentSnapshotConsumerError("catalog_raw_fields_missing")
    operators = row.get("filter_operators")
    if not isinstance(operators, Mapping):
        raise FutDailyCurrentSnapshotConsumerError("catalog_filter_contract_missing")
    if "eq" not in _text_list(operators.get("trade_date"), "catalog.trade_date_operators"):
        raise FutDailyCurrentSnapshotConsumerError("catalog_trade_date_filter_invalid")


def _validate_metadata(
    *,
    metadata: Any,
    expected_receipt_id: str,
    expected_lineage_sha256: str,
) -> None:
    if metadata.state.strip().lower() != "ready":
        raise FutDailyCurrentSnapshotConsumerError("metadata_not_ready")
    if metadata.degraded is not False:
        raise FutDailyCurrentSnapshotConsumerError("metadata_degraded")
    freshness = metadata.freshness
    if freshness.get("state") != "fresh" or freshness.get("stale") is not False:
        raise FutDailyCurrentSnapshotConsumerError("metadata_not_fresh")
    quality = metadata.quality
    if quality.get("state") != "valid" or quality.get("valid") is not True:
        raise FutDailyCurrentSnapshotConsumerError("metadata_invalid")
    if metadata.receipt_id != expected_receipt_id:
        raise FutDailyCurrentSnapshotConsumerError("receipt_mismatch")
    if not isinstance(metadata.lineage, Mapping):
        raise FutDailyCurrentSnapshotConsumerError("lineage_missing")
    if (
        metadata.lineage.get("complete") is not True
        or metadata.lineage.get("provider_neutral") is not True
    ):
        raise FutDailyCurrentSnapshotConsumerError("lineage_incomplete")
    if _sha256(metadata.lineage) != expected_lineage_sha256:
        raise FutDailyCurrentSnapshotConsumerError("lineage_mismatch")


def _map_mapping_selected_row(
    *,
    rows: tuple[dict[str, Any], ...],
    trade_date: str,
    mapping_ts_code: str,
    receipt_id: str,
    lineage_sha256: str,
) -> FutDailyRawCurrentSnapshotFact:
    matches: list[FutDailyRawCurrentSnapshotFact] = []
    for row in rows:
        if _trade_date(row.get("trade_date")) != trade_date:
            raise FutDailyCurrentSnapshotConsumerError("trade_date_partition_drift")
        ts_code = _text(row.get("ts_code"), "row.ts_code")
        if ts_code != mapping_ts_code:
            continue
        missing = [field for field in RAW_DAILY_FIELDS if field not in row]
        if missing:
            raise FutDailyCurrentSnapshotConsumerError("raw_field_missing")
        matches.append(
            FutDailyRawCurrentSnapshotFact(
                trade_date=trade_date,
                ts_code=ts_code,
                receipt_id=receipt_id,
                lineage_sha256=lineage_sha256,
                raw_values={field: row[field] for field in RAW_DAILY_FIELDS},
            )
        )
    if len(matches) != 1:
        raise FutDailyCurrentSnapshotConsumerError(
            "mapping_selected_daily_row_missing_or_nonunique"
        )
    return matches[0]


def _trade_date(value: Any) -> str:
    text = _text(value, "trade_date")
    if not _TRADE_DATE.fullmatch(text):
        raise FutDailyCurrentSnapshotConsumerError("trade_date_invalid")
    return text


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise FutDailyCurrentSnapshotConsumerError(f"{name}_invalid")
    return value


def _text_list(value: Any, name: str) -> list[str]:
    if not isinstance(value, list):
        raise FutDailyCurrentSnapshotConsumerError(f"{name}_invalid")
    return [_text(item, name) for item in value]


def _sha256_text(value: Any, name: str) -> str:
    text = _text(value, name)
    if not _SHA256.fullmatch(text):
        raise FutDailyCurrentSnapshotConsumerError(f"{name}_invalid")
    return text


def _sha256(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "DATASET_ID",
    "FutDailyCurrentSnapshot",
    "FutDailyCurrentSnapshotConsumerError",
    "FutDailyRawCurrentSnapshotFact",
    "IDENTITY_FIELDS",
    "RAW_DAILY_FIELDS",
    "SCHEMA_MAJOR",
    "load_fut_daily_current_snapshot",
]
