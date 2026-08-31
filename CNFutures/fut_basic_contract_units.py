"""Caller-invoked receipt-bound ``fut_basic`` raw contract-unit mapping.

The module is intentionally a consumer-only boundary: a caller injects the
existing TradingDatas V1 client and its expected catalog, receipt, and lineage.
It configures no transport, persistence, scheduler, runtime, or execution path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
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


DATASET_ID = "cn.dataset.fut_basic"
SCHEMA_MAJOR = 1  # Legacy public symbol; new reads bind the catalog major.
SUPPORTED_SCHEMA_MAJORS = frozenset({1, 2})
MAJOR2_MAX_ROWS = 500
MAJOR2_MAX_PAGES = 5
IDENTITY_FIELDS = ("ts_code",)
PRODUCT_CODE = "M"
EXCHANGE = "DCE"
QUERY_FIELDS = (
    "ts_code",
    "exchange",
    "fut_code",
    "multiplier",
    "trade_unit",
    "per_unit",
    "quote_unit",
    "quote_unit_desc",
)
RAW_CONTRACT_UNIT_FIELDS = QUERY_FIELDS[3:]
QUERY_ORDER = ("ts_code:asc",)
PAGE_LIMIT = 100
MAX_PAGES = 3
EXACT_ROW_COUNT = 207
_SHA256_LENGTH = 64
_ALLOWED_DEGRADED_REASON = "response_completeness_unverified"
_M_DCE_TS_CODE = re.compile(r"^M[0-9]{3,4}\.DCE$")
_MAJOR2_NATIVE_REFERENCE_IDS = frozenset({"M.DCE", "ML.DCE"})


class FutBasicContractUnitConsumerError(ValueError):
    """Raised when a partial ``fut_basic`` cohort cannot be safely consumed."""


@dataclass(frozen=True)
class FutBasicRawContractUnitFact:
    """One receipt-bound DCE/M row without derived market-rule semantics."""

    ts_code: str
    receipt_id: str
    lineage_sha256: str
    raw_values: Mapping[str, Any]
    pit_authority: bool = False
    execution_eligible: bool = False
    trading_eligible: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "raw_values", MappingProxyType(dict(self.raw_values)))
        if self.pit_authority or self.execution_eligible or self.trading_eligible:
            raise FutBasicContractUnitConsumerError("raw_fact_authority_invalid")


@dataclass(frozen=True)
class FutBasicRawContractUnitSnapshot:
    """Exact partial cohort and its explicit, non-runtime coverage debt."""

    dataset_id: str
    schema_major: int
    catalog_version: str
    receipt_id: str
    lineage_sha256: str
    page_count: int
    row_count: int
    terminal_pagination: bool
    replay_verified: bool
    semantic_sha256: str
    pagination_trace_sha256: str
    state: str
    degraded: bool
    coverage_complete: bool
    coverage_reason: str
    as_of: None = None
    pit_authority: bool = False
    runtime_eligible: bool = False
    execution_eligible: bool = False
    trading_eligible: bool = False
    facts: tuple[FutBasicRawContractUnitFact, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if (
            self.dataset_id != DATASET_ID
            or type(self.schema_major) is not int
            or self.schema_major not in SUPPORTED_SCHEMA_MAJORS
            or type(self.row_count) is not int
            or type(self.page_count) is not int
            or (self.schema_major == 1 and (self.row_count != EXACT_ROW_COUNT or self.page_count != MAX_PAGES))
            or (self.schema_major == 2 and not (1 <= self.row_count <= MAJOR2_MAX_ROWS and 1 <= self.page_count <= MAJOR2_MAX_PAGES))
            or self.as_of is not None
            or self.pit_authority
            or self.runtime_eligible
            or self.execution_eligible
            or self.trading_eligible
            or not self.terminal_pagination
            or not self.replay_verified
            or (self.schema_major == 1 and (self.state != "partial" or self.degraded is not True))
            or (self.schema_major == 2 and (self.state not in {"ready", "success"} or self.degraded is not False))
            or self.coverage_complete
            or self.coverage_reason != _ALLOWED_DEGRADED_REASON
            or len(self.facts) != self.row_count
        ):
            raise FutBasicContractUnitConsumerError("raw_snapshot_authority_invalid")


def load_fut_basic_raw_contract_units(
    *,
    client: SharedSignalsV1Client,
    expected_catalog_version: str,
    expected_receipt_id: str,
    expected_lineage_sha256: str,
) -> FutBasicRawContractUnitSnapshot:
    """Map the fixed ``fut_code=M`` cohort as partial raw contract facts.

    ``as_of`` is deliberately omitted.  The only accepted degraded response is
    the current explicit completeness debt, so it cannot be mistaken for a
    complete, runtime-eligible, PIT, or trading authority.
    """

    if not isinstance(client, SharedSignalsV1Client):
        raise TypeError("client must be SharedSignalsV1Client")
    expected_catalog = _text(expected_catalog_version, "expected_catalog_version")
    expected_receipt = _text(expected_receipt_id, "expected_receipt_id")
    expected_lineage = _sha256_text(expected_lineage_sha256, "expected_lineage_sha256")

    try:
        catalog = client.get_catalog()
        if catalog.catalog_version != expected_catalog:
            raise FutBasicContractUnitConsumerError("catalog_version_mismatch")
        schema_major = _validate_catalog_row(_single_catalog_row(catalog.data))
        max_pages = MAX_PAGES if schema_major == 1 else MAJOR2_MAX_PAGES
        max_rows = EXACT_ROW_COUNT if schema_major == 1 else MAJOR2_MAX_ROWS
        request = QueryRequest(
            dataset_id=DATASET_ID,
            schema_major=schema_major,
            fields=QUERY_FIELDS,
            filters={"fut_code": {"eq": PRODUCT_CODE}},
            as_of=None,
            order=QUERY_ORDER,
            limit=PAGE_LIMIT,
        )
        first = collect_query_pages(
            client=client,
            request=request,
            identity_fields=IDENTITY_FIELDS,
            max_pages=max_pages,
            max_rows=max_rows,
        )
        replay = collect_query_pages(
            client=client,
            request=request,
            identity_fields=IDENTITY_FIELDS,
            max_pages=max_pages,
            max_rows=max_rows,
        )
    except FutBasicContractUnitConsumerError:
        raise
    except PaginationContractError as exc:
        raise FutBasicContractUnitConsumerError(str(exc)) from exc
    except SharedSignalsV1Error as exc:
        raise FutBasicContractUnitConsumerError("tradingdatas_read_failed") from exc

    if (
        first.semantic_sha256 != replay.semantic_sha256
        or first.semantic_trace_sha256 != replay.semantic_trace_sha256
    ):
        raise FutBasicContractUnitConsumerError("replay_drift")

    _validate_metadata(
        metadata=first.envelope.metadata,
        expected_receipt_id=expected_receipt,
        expected_lineage_sha256=expected_lineage,
        schema_major=schema_major,
    )
    facts = _map_rows(
        rows=first.envelope.data,
        receipt_id=expected_receipt,
        lineage_sha256=expected_lineage,
        schema_major=schema_major,
    )
    return FutBasicRawContractUnitSnapshot(
        dataset_id=first.envelope.dataset_id,
        schema_major=schema_major,
        catalog_version=first.envelope.catalog_version,
        receipt_id=expected_receipt,
        lineage_sha256=expected_lineage,
        page_count=first.page_count,
        row_count=first.row_count,
        terminal_pagination=first.envelope.next_cursor is None,
        replay_verified=True,
        semantic_sha256=first.semantic_sha256,
        pagination_trace_sha256=first.pagination_trace_sha256,
        state=first.envelope.metadata.state.strip().lower(),
        degraded=first.envelope.metadata.degraded,
        coverage_complete=False,
        coverage_reason=_ALLOWED_DEGRADED_REASON,
        facts=facts,
    )


def _single_catalog_row(rows: tuple[dict[str, Any], ...]) -> Mapping[str, Any]:
    matches = [row for row in rows if row.get("dataset_id") == DATASET_ID]
    if len(matches) != 1:
        raise FutBasicContractUnitConsumerError("catalog_dataset_missing_or_duplicate")
    return matches[0]


def _validate_catalog_row(row: Mapping[str, Any]) -> int:
    major = row.get("schema_major")
    if type(major) is not int or major not in SUPPORTED_SCHEMA_MAJORS:
        raise FutBasicContractUnitConsumerError("catalog_schema_invalid")
    identity_fields = _text_list(row.get("identity_fields"), "catalog.identity_fields")
    if tuple(identity_fields) != IDENTITY_FIELDS:
        raise FutBasicContractUnitConsumerError("catalog_identity_invalid")
    default_fields = _text_list(row.get("default_fields"), "catalog.default_fields")
    if not set(QUERY_FIELDS).issubset(default_fields):
        raise FutBasicContractUnitConsumerError("catalog_raw_fields_missing")
    default_order = _text_list(row.get("default_order"), "catalog.default_order")
    if tuple(default_order) != QUERY_ORDER:
        raise FutBasicContractUnitConsumerError("catalog_order_invalid")
    operators = row.get("filter_operators")
    if not isinstance(operators, Mapping):
        raise FutBasicContractUnitConsumerError("catalog_filter_contract_missing")
    if "eq" not in _text_list(operators.get("fut_code"), "catalog.fut_code_operators"):
        raise FutBasicContractUnitConsumerError("catalog_fut_code_filter_invalid")
    return major


def _validate_metadata(
    *,
    metadata: Any,
    expected_receipt_id: str,
    expected_lineage_sha256: str,
    schema_major: int = 1,
) -> None:
    if schema_major == 1 and metadata.state.strip().lower() != "partial":
        raise FutBasicContractUnitConsumerError("metadata_state_invalid")
    if schema_major == 1 and metadata.degraded is not True:
        raise FutBasicContractUnitConsumerError("metadata_degraded_invalid")
    if schema_major == 1 and tuple(metadata.reasons) != (_ALLOWED_DEGRADED_REASON,):
        raise FutBasicContractUnitConsumerError("metadata_degraded_reason_invalid")
    if schema_major == 2:
        freshness, quality = metadata.freshness, metadata.quality
        if (metadata.state.strip().lower() not in {"ready", "success"}
                or metadata.degraded is not False or metadata.reasons
                or not isinstance(freshness, Mapping) or freshness.get("state") != "fresh"
                or freshness.get("stale") is not False or freshness.get("fresh") is False
                or not isinstance(quality, Mapping) or quality.get("state") != "valid"
                or quality.get("valid") is not True):
            raise FutBasicContractUnitConsumerError("metadata_major2_not_ready")
        try:
            through = datetime.fromisoformat(metadata.data_through.replace("Z", "+00:00"))
            observed = datetime.fromisoformat(metadata.observed_at.replace("Z", "+00:00"))
            if through.utcoffset() is None or observed.utcoffset() is None or through > observed:
                raise ValueError("invalid source time order")
        except (AttributeError, TypeError, ValueError) as exc:
            raise FutBasicContractUnitConsumerError("metadata_time_invalid") from exc
    if metadata.receipt_id != expected_receipt_id:
        raise FutBasicContractUnitConsumerError("receipt_mismatch")
    if not isinstance(metadata.lineage, Mapping):
        raise FutBasicContractUnitConsumerError("lineage_missing")
    if (
        metadata.lineage.get("complete") is not True
        or metadata.lineage.get("provider_neutral") is not True
    ):
        raise FutBasicContractUnitConsumerError("lineage_incomplete")
    if _sha256(metadata.lineage) != expected_lineage_sha256:
        raise FutBasicContractUnitConsumerError("lineage_mismatch")


def _map_rows(
    *,
    rows: tuple[dict[str, Any], ...],
    receipt_id: str,
    lineage_sha256: str,
    schema_major: int = 1,
) -> tuple[FutBasicRawContractUnitFact, ...]:
    if (schema_major == 1 and len(rows) != EXACT_ROW_COUNT) or (schema_major == 2 and not 1 <= len(rows) <= MAJOR2_MAX_ROWS):
        raise FutBasicContractUnitConsumerError("row_count_invalid")
    facts: list[FutBasicRawContractUnitFact] = []
    for row in rows:
        ts_code = _text(row.get("ts_code"), "row.ts_code")
        if row.get("exchange") != EXCHANGE:
            raise FutBasicContractUnitConsumerError("row_exchange_invalid")
        if row.get("fut_code") != PRODUCT_CODE:
            raise FutBasicContractUnitConsumerError("row_fut_code_invalid")
        if not _M_DCE_TS_CODE.fullmatch(ts_code) and not (
            schema_major == 2 and ts_code in _MAJOR2_NATIVE_REFERENCE_IDS
        ):
            raise FutBasicContractUnitConsumerError("row_ts_code_invalid")
        missing = [field for field in RAW_CONTRACT_UNIT_FIELDS if field not in row]
        if missing:
            raise FutBasicContractUnitConsumerError("raw_field_missing")
        facts.append(
            FutBasicRawContractUnitFact(
                ts_code=ts_code,
                receipt_id=receipt_id,
                lineage_sha256=lineage_sha256,
                raw_values={field: row[field] for field in RAW_CONTRACT_UNIT_FIELDS},
            )
        )
    return tuple(facts)


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise FutBasicContractUnitConsumerError(f"{name}_invalid")
    return value


def _text_list(value: Any, name: str) -> list[str]:
    if not isinstance(value, list):
        raise FutBasicContractUnitConsumerError(f"{name}_invalid")
    return [_text(item, name) for item in value]


def _sha256_text(value: Any, name: str) -> str:
    text = _text(value, name)
    if len(text) != _SHA256_LENGTH or any(character not in "0123456789abcdef" for character in text):
        raise FutBasicContractUnitConsumerError(f"{name}_invalid")
    return text


def _sha256(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "DATASET_ID",
    "EXACT_ROW_COUNT",
    "FutBasicContractUnitConsumerError",
    "FutBasicRawContractUnitFact",
    "FutBasicRawContractUnitSnapshot",
    "IDENTITY_FIELDS",
    "RAW_CONTRACT_UNIT_FIELDS",
    "SCHEMA_MAJOR",
    "load_fut_basic_raw_contract_units",
]
