"""Caller-invoked receipt-bound ``fut_settle`` raw market-rule mapping.

This module has no endpoint configuration, persistence, scheduler, runtime, or
execution path.  A caller injects the existing TradingDatas V1 client together
with the receipt and lineage it intends to consume.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
from types import MappingProxyType
from typing import Any, Mapping

from shared.data.sharedsignals_v1 import (
    SharedSignalsV1Client,
    SharedSignalsV1Error,
    QueryRequest,
)
from shared.data.tradingdatas_pagination import (
    PaginationContractError,
    collect_query_pages,
)


DATASET_ID = "cn.dataset.fut_settle"
SCHEMA_MAJOR = 2
IDENTITY_FIELDS = ("trade_date", "ts_code")
QUERY_FIELDS = (
    "trade_date",
    "ts_code",
    "settle",
    "trading_fee_rate",
    "trading_fee",
    "delivery_fee",
    "b_hedging_margin_rate",
    "s_hedging_margin_rate",
    "long_margin_rate",
    "short_margin_rate",
)
RAW_MARKET_RULE_FIELDS = QUERY_FIELDS[2:]
QUERY_ORDER = ("trade_date:asc", "ts_code:asc")
PAGE_LIMIT = 500
MAX_PAGES = 3
MAX_ROWS = 1_000
_TRADE_DATE = re.compile(r"^[0-9]{8}$")
_M_DCE_SYMBOL = re.compile(r"^M[0-9]{3,4}\.DCE$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class FutSettleMarketRuleConsumerError(ValueError):
    """Raised when a raw fut_settle readback is not safe to consume."""


@dataclass(frozen=True)
class FutSettleRawMarketRuleFact:
    """One DCE M receipt-bound raw market-rule row, without inferred semantics."""

    trade_date: str
    ts_code: str
    receipt_id: str
    lineage_sha256: str
    raw_values: Mapping[str, Any]
    pit_authority: bool = False
    execution_eligible: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "raw_values", MappingProxyType(dict(self.raw_values)))
        if self.pit_authority or self.execution_eligible:
            raise FutSettleMarketRuleConsumerError("raw_fact_authority_invalid")


@dataclass(frozen=True)
class FutSettleRawMarketRuleSnapshot:
    """Bounded, replay-checked mapping from one receipt-backed query cohort."""

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
    pit_authority: bool = False
    execution_eligible: bool = False
    facts: tuple[FutSettleRawMarketRuleFact, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if (
            self.dataset_id != DATASET_ID
            or self.schema_major != SCHEMA_MAJOR
            or self.as_of is not None
            or self.pit_authority
            or self.execution_eligible
            or not self.terminal_pagination
            or not self.replay_verified
        ):
            raise FutSettleMarketRuleConsumerError("raw_snapshot_authority_invalid")


def load_fut_settle_raw_market_rules(
    *,
    client: SharedSignalsV1Client,
    trade_date: str,
    expected_catalog_version: str,
    expected_receipt_id: str,
    expected_lineage_sha256: str,
) -> FutSettleRawMarketRuleSnapshot:
    """Map one complete DCE/M rule cohort through fixed TradingDatas V1 routes.

    The caller owns transport construction and the expected receipt/lineage.
    ``as_of`` is deliberately omitted because this contract has no PIT authority.
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
            raise FutSettleMarketRuleConsumerError("catalog_version_mismatch")
        catalog_row = _single_catalog_row(catalog.data)
        _validate_catalog_row(catalog_row)
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
    except FutSettleMarketRuleConsumerError:
        raise
    except PaginationContractError as exc:
        raise FutSettleMarketRuleConsumerError(str(exc)) from exc
    except SharedSignalsV1Error as exc:
        raise FutSettleMarketRuleConsumerError("tradingdatas_read_failed") from exc

    if (
        first.semantic_sha256 != replay.semantic_sha256
        or first.semantic_trace_sha256 != replay.semantic_trace_sha256
    ):
        raise FutSettleMarketRuleConsumerError("replay_drift")

    metadata = first.envelope.metadata
    _validate_metadata(
        metadata=metadata,
        expected_receipt_id=expected_receipt,
        expected_lineage_sha256=expected_lineage,
    )
    facts = _map_dce_m_rows(
        rows=first.envelope.data,
        trade_date=normalized_trade_date,
        receipt_id=expected_receipt,
        lineage_sha256=expected_lineage,
    )
    return FutSettleRawMarketRuleSnapshot(
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
        facts=facts,
    )


def _single_catalog_row(rows: tuple[dict[str, Any], ...]) -> Mapping[str, Any]:
    matches = [row for row in rows if row.get("dataset_id") == DATASET_ID]
    if len(matches) != 1:
        raise FutSettleMarketRuleConsumerError("catalog_dataset_missing_or_duplicate")
    return matches[0]


def _validate_catalog_row(row: Mapping[str, Any]) -> None:
    if row.get("schema_major") != SCHEMA_MAJOR:
        raise FutSettleMarketRuleConsumerError("catalog_schema_invalid")
    if tuple(_text_list(row.get("identity_fields"), "catalog.identity_fields")) != IDENTITY_FIELDS:
        raise FutSettleMarketRuleConsumerError("catalog_identity_invalid")
    fields = _text_list(row.get("default_fields"), "catalog.default_fields")
    if not set(QUERY_FIELDS).issubset(fields):
        raise FutSettleMarketRuleConsumerError("catalog_raw_fields_missing")
    if tuple(_text_list(row.get("default_order"), "catalog.default_order")) != QUERY_ORDER:
        raise FutSettleMarketRuleConsumerError("catalog_order_invalid")
    operators = row.get("filter_operators")
    if not isinstance(operators, Mapping):
        raise FutSettleMarketRuleConsumerError("catalog_filter_contract_missing")
    trade_date_operators = operators.get("trade_date")
    if "eq" not in _text_list(trade_date_operators, "catalog.trade_date_operators"):
        raise FutSettleMarketRuleConsumerError("catalog_trade_date_filter_invalid")


def _validate_metadata(
    *,
    metadata: Any,
    expected_receipt_id: str,
    expected_lineage_sha256: str,
) -> None:
    if metadata.state.strip().lower() != "ready":
        raise FutSettleMarketRuleConsumerError("metadata_not_ready")
    if metadata.degraded:
        raise FutSettleMarketRuleConsumerError("metadata_degraded")
    freshness = metadata.freshness
    if freshness.get("state") != "fresh" or freshness.get("stale") is not False:
        raise FutSettleMarketRuleConsumerError("metadata_not_fresh")
    quality = metadata.quality
    if quality.get("state") != "valid" or quality.get("valid") is not True:
        raise FutSettleMarketRuleConsumerError("metadata_invalid")
    if metadata.receipt_id != expected_receipt_id:
        raise FutSettleMarketRuleConsumerError("receipt_mismatch")
    if not isinstance(metadata.lineage, Mapping):
        raise FutSettleMarketRuleConsumerError("lineage_missing")
    if (
        metadata.lineage.get("complete") is not True
        or metadata.lineage.get("provider_neutral") is not True
    ):
        raise FutSettleMarketRuleConsumerError("lineage_incomplete")
    if _sha256(metadata.lineage) != expected_lineage_sha256:
        raise FutSettleMarketRuleConsumerError("lineage_mismatch")


def _map_dce_m_rows(
    *,
    rows: tuple[dict[str, Any], ...],
    trade_date: str,
    receipt_id: str,
    lineage_sha256: str,
) -> tuple[FutSettleRawMarketRuleFact, ...]:
    facts: list[FutSettleRawMarketRuleFact] = []
    for row in rows:
        if _trade_date(row.get("trade_date")) != trade_date:
            raise FutSettleMarketRuleConsumerError("trade_date_partition_drift")
        ts_code = _text(row.get("ts_code"), "row.ts_code")
        if not _M_DCE_SYMBOL.fullmatch(ts_code):
            continue
        missing = [field for field in RAW_MARKET_RULE_FIELDS if field not in row]
        if missing:
            raise FutSettleMarketRuleConsumerError("raw_field_missing")
        facts.append(
            FutSettleRawMarketRuleFact(
                trade_date=trade_date,
                ts_code=ts_code,
                receipt_id=receipt_id,
                lineage_sha256=lineage_sha256,
                raw_values={field: row[field] for field in RAW_MARKET_RULE_FIELDS},
            )
        )
    if not facts:
        raise FutSettleMarketRuleConsumerError("dce_m_rows_missing")
    return tuple(facts)


def _trade_date(value: Any) -> str:
    text = _text(value, "trade_date")
    if not _TRADE_DATE.fullmatch(text):
        raise FutSettleMarketRuleConsumerError("trade_date_invalid")
    return text


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise FutSettleMarketRuleConsumerError(f"{name}_invalid")
    return value


def _text_list(value: Any, name: str) -> list[str]:
    if not isinstance(value, list):
        raise FutSettleMarketRuleConsumerError(f"{name}_invalid")
    return [_text(item, name) for item in value]


def _sha256_text(value: Any, name: str) -> str:
    text = _text(value, name)
    if not _SHA256.fullmatch(text):
        raise FutSettleMarketRuleConsumerError(f"{name}_invalid")
    return text


def _sha256(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "DATASET_ID",
    "FutSettleMarketRuleConsumerError",
    "FutSettleRawMarketRuleFact",
    "FutSettleRawMarketRuleSnapshot",
    "IDENTITY_FIELDS",
    "RAW_MARKET_RULE_FIELDS",
    "SCHEMA_MAJOR",
    "load_fut_settle_raw_market_rules",
]
