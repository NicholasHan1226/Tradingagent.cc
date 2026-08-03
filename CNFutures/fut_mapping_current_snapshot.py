"""Caller-invoked receipt-bound ``fut_mapping`` current-snapshot mapping.

The caller injects the existing TradingDatas V1 client and expected readback
evidence.  This module configures no transport, runtime, persistence, timer,
execution, or trading path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
from types import MappingProxyType
from typing import Any, Mapping

from shared.data.sharedsignals_v1 import (
    QueryRequest,
    SharedSignalsV1Client,
    SharedSignalsV1Error,
)
from shared.data.tradingdatas_pagination import (
    PaginationContractError,
    collect_query_pages,
)


DATASET_ID = "cn.dataset.fut_mapping"
SCHEMA_MAJOR = 1
IDENTITY_FIELDS = ("trade_date", "ts_code")
QUERY_FIELDS = ("trade_date", "ts_code", "mapping_ts_code")
RAW_MAPPING_FIELDS = ("mapping_ts_code",)
QUERY_ORDER = ("trade_date:asc", "ts_code:asc")
PAGE_LIMIT = 500
MAX_PAGES = 1
EXACT_ROW_COUNT = 202
M_DCE_TS_CODE = "M.DCE"
_TRADE_DATE = re.compile(r"^[0-9]{8}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class FutMappingCurrentSnapshotConsumerError(ValueError):
    """Raised when a current exact-day mapping snapshot cannot be consumed."""


@dataclass(frozen=True)
class FutMappingRawCurrentSnapshotFact:
    """The one receipt-bound ``M.DCE`` mapping row, without rollover semantics."""

    trade_date: str
    ts_code: str
    receipt_id: str
    lineage_sha256: str
    raw_values: Mapping[str, Any]
    stable: bool = False
    pit_rollover_authority: bool = False
    simulation_ready: bool = False
    runtime_eligible: bool = False
    execution_eligible: bool = False
    trading_eligible: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "raw_values", MappingProxyType(dict(self.raw_values)))
        if (
            self.stable
            or self.pit_rollover_authority
            or self.simulation_ready
            or self.runtime_eligible
            or self.execution_eligible
            or self.trading_eligible
        ):
            raise FutMappingCurrentSnapshotConsumerError("raw_fact_authority_invalid")


@dataclass(frozen=True)
class FutMappingCurrentSnapshot:
    """One complete current-day receipt snapshot, explicitly not PIT authority."""

    dataset_id: str
    schema_major: int
    catalog_version: str
    trade_date: str
    receipt_id: str
    lineage_sha256: str
    page_count: int
    row_count: int
    terminal_pagination: bool
    replay_verified: bool
    semantic_sha256: str
    pagination_trace_sha256: str
    as_of: None = None
    stable: bool = False
    pit_rollover_authority: bool = False
    simulation_ready: bool = False
    runtime_eligible: bool = False
    execution_eligible: bool = False
    trading_eligible: bool = False
    facts: tuple[FutMappingRawCurrentSnapshotFact, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if (
            self.dataset_id != DATASET_ID
            or self.schema_major != SCHEMA_MAJOR
            or self.page_count != MAX_PAGES
            or self.row_count != EXACT_ROW_COUNT
            or not self.terminal_pagination
            or not self.replay_verified
            or self.as_of is not None
            or self.stable
            or self.pit_rollover_authority
            or self.simulation_ready
            or self.runtime_eligible
            or self.execution_eligible
            or self.trading_eligible
            or len(self.facts) != 1
            or self.facts[0].ts_code != M_DCE_TS_CODE
        ):
            raise FutMappingCurrentSnapshotConsumerError("current_snapshot_authority_invalid")


def load_fut_mapping_current_snapshot(
    *,
    client: SharedSignalsV1Client,
    trade_date: str,
    expected_catalog_version: str,
    expected_receipt_id: str,
    expected_lineage_sha256: str,
) -> FutMappingCurrentSnapshot:
    """Read one complete current ``trade_date`` cohort and return only ``M.DCE``.

    The exact-day receipt completeness validates identity and partition integrity;
    it does not make mapping effective intervals, rollover history, or PIT known.
    """

    if not isinstance(client, SharedSignalsV1Client):
        raise TypeError("client must be SharedSignalsV1Client")
    normalized_trade_date = _trade_date(trade_date)
    expected_catalog = _text(expected_catalog_version, "expected_catalog_version")
    expected_receipt = _text(expected_receipt_id, "expected_receipt_id")
    expected_lineage = _sha256_text(expected_lineage_sha256, "expected_lineage_sha256")

    try:
        catalog = client.get_catalog()
        if catalog.catalog_version != expected_catalog:
            raise FutMappingCurrentSnapshotConsumerError("catalog_version_mismatch")
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
            max_rows=EXACT_ROW_COUNT,
        )
        replay = collect_query_pages(
            client=client,
            request=request,
            identity_fields=IDENTITY_FIELDS,
            max_pages=MAX_PAGES,
            max_rows=EXACT_ROW_COUNT,
        )
    except FutMappingCurrentSnapshotConsumerError:
        raise
    except PaginationContractError as exc:
        raise FutMappingCurrentSnapshotConsumerError(str(exc)) from exc
    except SharedSignalsV1Error as exc:
        raise FutMappingCurrentSnapshotConsumerError("tradingdatas_read_failed") from exc

    if (
        first.semantic_sha256 != replay.semantic_sha256
        or first.semantic_trace_sha256 != replay.semantic_trace_sha256
    ):
        raise FutMappingCurrentSnapshotConsumerError("replay_drift")
    if first.row_count != EXACT_ROW_COUNT:
        raise FutMappingCurrentSnapshotConsumerError("row_count_invalid")

    _validate_metadata(
        metadata=first.envelope.metadata,
        expected_receipt_id=expected_receipt,
        expected_lineage_sha256=expected_lineage,
    )
    fact = _map_m_dce_row(
        rows=first.envelope.data,
        trade_date=normalized_trade_date,
        receipt_id=expected_receipt,
        lineage_sha256=expected_lineage,
    )
    return FutMappingCurrentSnapshot(
        dataset_id=first.envelope.dataset_id,
        schema_major=SCHEMA_MAJOR,
        catalog_version=first.envelope.catalog_version,
        trade_date=normalized_trade_date,
        receipt_id=expected_receipt,
        lineage_sha256=expected_lineage,
        page_count=first.page_count,
        row_count=first.row_count,
        terminal_pagination=first.envelope.next_cursor is None,
        replay_verified=True,
        semantic_sha256=first.semantic_sha256,
        pagination_trace_sha256=first.pagination_trace_sha256,
        facts=(fact,),
    )


def _single_catalog_row(rows: tuple[dict[str, Any], ...]) -> Mapping[str, Any]:
    matches = [row for row in rows if row.get("dataset_id") == DATASET_ID]
    if len(matches) != 1:
        raise FutMappingCurrentSnapshotConsumerError("catalog_dataset_missing_or_duplicate")
    return matches[0]


def _validate_catalog_row(row: Mapping[str, Any]) -> None:
    if row.get("schema_major") != SCHEMA_MAJOR:
        raise FutMappingCurrentSnapshotConsumerError("catalog_schema_invalid")
    if tuple(_text_list(row.get("identity_fields"), "catalog.identity_fields")) != IDENTITY_FIELDS:
        raise FutMappingCurrentSnapshotConsumerError("catalog_identity_invalid")
    if tuple(_text_list(row.get("default_order"), "catalog.default_order")) != QUERY_ORDER:
        raise FutMappingCurrentSnapshotConsumerError("catalog_order_invalid")
    fields = _text_list(row.get("default_fields"), "catalog.default_fields")
    if not set(QUERY_FIELDS).issubset(fields):
        raise FutMappingCurrentSnapshotConsumerError("catalog_raw_fields_missing")
    operators = row.get("filter_operators")
    if not isinstance(operators, Mapping):
        raise FutMappingCurrentSnapshotConsumerError("catalog_filter_contract_missing")
    if "eq" not in _text_list(operators.get("trade_date"), "catalog.trade_date_operators"):
        raise FutMappingCurrentSnapshotConsumerError("catalog_trade_date_filter_invalid")


def _validate_metadata(
    *,
    metadata: Any,
    expected_receipt_id: str,
    expected_lineage_sha256: str,
) -> None:
    if metadata.state.strip().lower() != "ready":
        raise FutMappingCurrentSnapshotConsumerError("metadata_not_ready")
    if metadata.degraded is not False:
        raise FutMappingCurrentSnapshotConsumerError("metadata_degraded")
    freshness = metadata.freshness
    if freshness.get("state") != "fresh" or freshness.get("stale") is not False:
        raise FutMappingCurrentSnapshotConsumerError("metadata_not_fresh")
    quality = metadata.quality
    if quality.get("state") != "valid" or quality.get("valid") is not True:
        raise FutMappingCurrentSnapshotConsumerError("metadata_invalid")
    if metadata.receipt_id != expected_receipt_id:
        raise FutMappingCurrentSnapshotConsumerError("receipt_mismatch")
    if not isinstance(metadata.lineage, Mapping):
        raise FutMappingCurrentSnapshotConsumerError("lineage_missing")
    if (
        metadata.lineage.get("complete") is not True
        or metadata.lineage.get("provider_neutral") is not True
    ):
        raise FutMappingCurrentSnapshotConsumerError("lineage_incomplete")
    if _sha256(metadata.lineage) != expected_lineage_sha256:
        raise FutMappingCurrentSnapshotConsumerError("lineage_mismatch")


def _map_m_dce_row(
    *,
    rows: tuple[dict[str, Any], ...],
    trade_date: str,
    receipt_id: str,
    lineage_sha256: str,
) -> FutMappingRawCurrentSnapshotFact:
    matches: list[FutMappingRawCurrentSnapshotFact] = []
    for row in rows:
        if _trade_date(row.get("trade_date")) != trade_date:
            raise FutMappingCurrentSnapshotConsumerError("trade_date_partition_drift")
        ts_code = _text(row.get("ts_code"), "row.ts_code")
        if ts_code != M_DCE_TS_CODE:
            continue
        missing = [field for field in RAW_MAPPING_FIELDS if field not in row]
        if missing:
            raise FutMappingCurrentSnapshotConsumerError("raw_field_missing")
        matches.append(
            FutMappingRawCurrentSnapshotFact(
                trade_date=trade_date,
                ts_code=ts_code,
                receipt_id=receipt_id,
                lineage_sha256=lineage_sha256,
                raw_values={field: row[field] for field in RAW_MAPPING_FIELDS},
            )
        )
    if len(matches) != 1:
        raise FutMappingCurrentSnapshotConsumerError("m_dce_row_missing_or_nonunique")
    return matches[0]


def _trade_date(value: Any) -> str:
    text = _text(value, "trade_date")
    if not _TRADE_DATE.fullmatch(text):
        raise FutMappingCurrentSnapshotConsumerError("trade_date_invalid")
    return text


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise FutMappingCurrentSnapshotConsumerError(f"{name}_invalid")
    return value


def _text_list(value: Any, name: str) -> list[str]:
    if not isinstance(value, list):
        raise FutMappingCurrentSnapshotConsumerError(f"{name}_invalid")
    return [_text(item, name) for item in value]


def _sha256_text(value: Any, name: str) -> str:
    text = _text(value, name)
    if not _SHA256.fullmatch(text):
        raise FutMappingCurrentSnapshotConsumerError(f"{name}_invalid")
    return text


def _sha256(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "DATASET_ID",
    "EXACT_ROW_COUNT",
    "FutMappingCurrentSnapshot",
    "FutMappingCurrentSnapshotConsumerError",
    "FutMappingRawCurrentSnapshotFact",
    "IDENTITY_FIELDS",
    "M_DCE_TS_CODE",
    "RAW_MAPPING_FIELDS",
    "SCHEMA_MAJOR",
    "load_fut_mapping_current_snapshot",
]
