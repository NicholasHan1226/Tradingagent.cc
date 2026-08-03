"""Injected-client, receipt-bound fut_weekly_monthly current snapshot reader."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
import re
from types import MappingProxyType
from typing import Any, Mapping

from shared.data.sharedsignals_v1 import QueryRequest, SharedSignalsV1Client, SharedSignalsV1Error
from shared.data.tradingdatas_pagination import PaginationContractError, collect_query_pages


DATASET_ID = "cn.dataset.fut_weekly_monthly"
SCHEMA_MAJOR = 1
IDENTITY_FIELDS = ("trade_date", "freq", "ts_code")
QUERY_ORDER = ("trade_date:asc", "freq:asc", "ts_code:asc")
QUERY_FIELDS = (
    "ts_code",
    "trade_date",
    "end_date",
    "freq",
    "open",
    "high",
    "low",
    "close",
    "pre_close",
    "settle",
    "pre_settle",
    "vol",
    "amount",
    "oi",
    "oi_chg",
    "exchange",
    "change1",
    "change2",
)
RAW_FIELDS = tuple(field for field in QUERY_FIELDS if field not in IDENTITY_FIELDS)
PAGE_LIMIT = 500
EXACT_PAGE_COUNT = 5
EXACT_ROW_COUNT = 2231
EXACT_WEEK_ROW_COUNT = 1081
EXACT_MONTH_ROW_COUNT = 1150

_DAY = re.compile(r"^[0-9]{8}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FREQUENCIES = frozenset({"week", "month"})


class FutWeeklyMonthlyCurrentSnapshotConsumerError(ValueError):
    """A fail-closed violation of the frozen current-snapshot contract."""


@dataclass(frozen=True)
class FutWeeklyMonthlyRawFact:
    """One receipt-bound weekly or monthly raw provider row."""

    trade_date: str
    freq: str
    ts_code: str
    receipt_id: str
    lineage_sha256: str
    raw_values: Mapping[str, Any]
    stable: bool = False
    pit_authority: bool = False
    session_authority: bool = False
    rollover_authority: bool = False
    simulation_ready: bool = False
    runtime_eligible: bool = False
    execution_eligible: bool = False
    trading_eligible: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "raw_values", MappingProxyType(dict(self.raw_values)))
        if any(
            (
                self.stable,
                self.pit_authority,
                self.session_authority,
                self.rollover_authority,
                self.simulation_ready,
                self.runtime_eligible,
                self.execution_eligible,
                self.trading_eligible,
            )
        ):
            raise FutWeeklyMonthlyCurrentSnapshotConsumerError("raw_fact_authority_invalid")


@dataclass(frozen=True)
class FutWeeklyMonthlyCurrentSnapshot:
    """A bounded, raw-only current partition; never a PIT or trading authority."""

    dataset_id: str
    schema_major: int
    catalog_version: str
    trade_date: str
    receipt_id: str
    lineage_sha256: str
    data_through: datetime
    observed_at: datetime
    decision_time: datetime
    page_count: int
    row_count: int
    week_row_count: int
    month_row_count: int
    terminal_pagination: bool
    replay_verified: bool
    semantic_sha256: str
    pagination_trace_sha256: str
    as_of: None = None
    stable: bool = False
    pit_authority: bool = False
    session_authority: bool = False
    rollover_authority: bool = False
    simulation_ready: bool = False
    runtime_eligible: bool = False
    execution_eligible: bool = False
    trading_eligible: bool = False
    facts: tuple[FutWeeklyMonthlyRawFact, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not (
            _time(self.data_through, "snapshot_data_through")
            <= _time(self.observed_at, "snapshot_observed_at")
            <= _decision_time(self.decision_time)
        ):
            raise FutWeeklyMonthlyCurrentSnapshotConsumerError("snapshot_time_order_invalid")
        if any(
            (
                self.dataset_id != DATASET_ID,
                self.schema_major != SCHEMA_MAJOR,
                _day_text(self.trade_date) != self.trade_date,
                self.page_count != EXACT_PAGE_COUNT,
                self.row_count != EXACT_ROW_COUNT,
                self.week_row_count != EXACT_WEEK_ROW_COUNT,
                self.month_row_count != EXACT_MONTH_ROW_COUNT,
                not self.terminal_pagination,
                not self.replay_verified,
                self.as_of is not None,
                len(self.facts) != EXACT_ROW_COUNT,
                any(
                    (
                        self.stable,
                        self.pit_authority,
                        self.session_authority,
                        self.rollover_authority,
                        self.simulation_ready,
                        self.runtime_eligible,
                        self.execution_eligible,
                        self.trading_eligible,
                    )
                ),
            )
        ):
            raise FutWeeklyMonthlyCurrentSnapshotConsumerError("snapshot_authority_invalid")

        identities = {(fact.trade_date, fact.freq, fact.ts_code) for fact in self.facts}
        if len(identities) != EXACT_ROW_COUNT:
            raise FutWeeklyMonthlyCurrentSnapshotConsumerError("snapshot_fact_binding_invalid")
        week_count = 0
        month_count = 0
        for fact in self.facts:
            if (
                fact.trade_date != self.trade_date
                or fact.receipt_id != self.receipt_id
                or fact.lineage_sha256 != self.lineage_sha256
                or fact.freq not in _FREQUENCIES
                or tuple(fact.raw_values) != RAW_FIELDS
            ):
                raise FutWeeklyMonthlyCurrentSnapshotConsumerError("snapshot_fact_binding_invalid")
            if fact.freq == "week":
                week_count += 1
            else:
                month_count += 1
        if week_count != self.week_row_count or month_count != self.month_row_count:
            raise FutWeeklyMonthlyCurrentSnapshotConsumerError("snapshot_fact_binding_invalid")


def load_fut_weekly_monthly_current_snapshot(
    *,
    client: SharedSignalsV1Client,
    trade_date: str,
    expected_catalog_version: str,
    expected_receipt_id: str,
    expected_lineage_sha256: str,
    decision_time: datetime,
) -> FutWeeklyMonthlyCurrentSnapshot:
    """Read and replay exactly one governed weekly/monthly current partition."""

    if not isinstance(client, SharedSignalsV1Client):
        raise TypeError("client must be SharedSignalsV1Client")
    day = _day_text(trade_date)
    decision = _decision_time(decision_time)
    receipt = _text(expected_receipt_id, "expected_receipt_id")
    lineage_sha256 = _sha256_text(expected_lineage_sha256, "expected_lineage_sha256")

    try:
        catalog = client.get_catalog()
        if catalog.catalog_version != _text(expected_catalog_version, "expected_catalog_version"):
            raise FutWeeklyMonthlyCurrentSnapshotConsumerError("catalog_version_mismatch")
        _validate_catalog([row for row in catalog.data if row.get("dataset_id") == DATASET_ID])
        request = QueryRequest(
            dataset_id=DATASET_ID,
            schema_major=SCHEMA_MAJOR,
            fields=QUERY_FIELDS,
            filters={"trade_date": {"eq": day}},
            as_of=None,
            order=QUERY_ORDER,
            limit=PAGE_LIMIT,
        )
        first = collect_query_pages(
            client=client,
            request=request,
            identity_fields=IDENTITY_FIELDS,
            max_pages=EXACT_PAGE_COUNT,
            max_rows=EXACT_ROW_COUNT,
        )
        replay = collect_query_pages(
            client=client,
            request=request,
            identity_fields=IDENTITY_FIELDS,
            max_pages=EXACT_PAGE_COUNT,
            max_rows=EXACT_ROW_COUNT,
        )
    except FutWeeklyMonthlyCurrentSnapshotConsumerError:
        raise
    except PaginationContractError as exc:
        raise FutWeeklyMonthlyCurrentSnapshotConsumerError(str(exc)) from exc
    except SharedSignalsV1Error as exc:
        raise FutWeeklyMonthlyCurrentSnapshotConsumerError("tradingdatas_read_failed") from exc

    if (
        first.semantic_sha256 != replay.semantic_sha256
        or first.semantic_trace_sha256 != replay.semantic_trace_sha256
    ):
        raise FutWeeklyMonthlyCurrentSnapshotConsumerError("replay_drift")
    if first.page_count != EXACT_PAGE_COUNT or first.row_count != EXACT_ROW_COUNT:
        raise FutWeeklyMonthlyCurrentSnapshotConsumerError("row_count_invalid")

    data_through, observed_at = _validate_metadata(
        first.envelope.metadata,
        receipt=receipt,
        expected_lineage_sha256=lineage_sha256,
        decision_time=decision,
    )
    facts = _map_facts(
        first.envelope.data,
        trade_date=day,
        receipt=receipt,
        lineage_sha256=lineage_sha256,
    )
    week_row_count = sum(fact.freq == "week" for fact in facts)
    month_row_count = sum(fact.freq == "month" for fact in facts)
    if week_row_count != EXACT_WEEK_ROW_COUNT or month_row_count != EXACT_MONTH_ROW_COUNT:
        raise FutWeeklyMonthlyCurrentSnapshotConsumerError("frequency_row_count_invalid")

    return FutWeeklyMonthlyCurrentSnapshot(
        dataset_id=first.envelope.dataset_id,
        schema_major=SCHEMA_MAJOR,
        catalog_version=first.envelope.catalog_version,
        trade_date=day,
        receipt_id=receipt,
        lineage_sha256=lineage_sha256,
        data_through=data_through,
        observed_at=observed_at,
        decision_time=decision,
        page_count=first.page_count,
        row_count=first.row_count,
        week_row_count=week_row_count,
        month_row_count=month_row_count,
        terminal_pagination=first.envelope.next_cursor is None,
        replay_verified=True,
        semantic_sha256=first.semantic_sha256,
        pagination_trace_sha256=first.pagination_trace_sha256,
        facts=tuple(facts),
    )


def _validate_catalog(rows: list[Mapping[str, Any]]) -> None:
    if len(rows) != 1:
        raise FutWeeklyMonthlyCurrentSnapshotConsumerError("catalog_dataset_missing_or_duplicate")
    row = rows[0]
    if row.get("schema_major") != SCHEMA_MAJOR:
        raise FutWeeklyMonthlyCurrentSnapshotConsumerError("catalog_schema_invalid")
    if tuple(row.get("identity_fields", ())) != IDENTITY_FIELDS:
        raise FutWeeklyMonthlyCurrentSnapshotConsumerError("catalog_identity_invalid")
    if tuple(row.get("default_order", ())) != QUERY_ORDER:
        raise FutWeeklyMonthlyCurrentSnapshotConsumerError("catalog_order_invalid")
    if not set(QUERY_FIELDS).issubset(row.get("default_fields", ())):
        raise FutWeeklyMonthlyCurrentSnapshotConsumerError("catalog_raw_fields_missing")
    if "eq" not in row.get("filter_operators", {}).get("trade_date", ()):
        raise FutWeeklyMonthlyCurrentSnapshotConsumerError("catalog_trade_date_filter_invalid")


def _validate_metadata(
    metadata: Any,
    *,
    receipt: str,
    expected_lineage_sha256: str,
    decision_time: datetime,
) -> tuple[datetime, datetime]:
    if (
        metadata.state.strip().lower() != "ready"
        or metadata.degraded is not False
        or metadata.freshness.get("state") != "fresh"
        or metadata.freshness.get("stale") is not False
        or metadata.quality.get("state") != "valid"
        or metadata.quality.get("valid") is not True
        or tuple(metadata.reasons) != ()
    ):
        raise FutWeeklyMonthlyCurrentSnapshotConsumerError("metadata_contract_invalid")
    if metadata.receipt_id != receipt:
        raise FutWeeklyMonthlyCurrentSnapshotConsumerError("receipt_mismatch")
    if (
        not isinstance(metadata.lineage, Mapping)
        or metadata.lineage.get("complete") is not True
        or metadata.lineage.get("provider_neutral") is not True
    ):
        raise FutWeeklyMonthlyCurrentSnapshotConsumerError("lineage_incomplete")
    if _hash(metadata.lineage) != expected_lineage_sha256:
        raise FutWeeklyMonthlyCurrentSnapshotConsumerError("lineage_mismatch")
    data_through = _time(metadata.data_through, "metadata_data_through")
    observed_at = _time(metadata.observed_at, "metadata_observed_at")
    if not data_through <= observed_at <= decision_time:
        raise FutWeeklyMonthlyCurrentSnapshotConsumerError("metadata_time_order_invalid")
    return data_through, observed_at


def _map_facts(
    rows: tuple[Mapping[str, Any], ...],
    *,
    trade_date: str,
    receipt: str,
    lineage_sha256: str,
) -> list[FutWeeklyMonthlyRawFact]:
    facts: list[FutWeeklyMonthlyRawFact] = []
    for row in rows:
        if _day_text(row.get("trade_date")) != trade_date:
            raise FutWeeklyMonthlyCurrentSnapshotConsumerError("trade_date_partition_drift")
        freq = _text(row.get("freq"), "row.freq")
        if freq not in _FREQUENCIES:
            raise FutWeeklyMonthlyCurrentSnapshotConsumerError("frequency_partition_invalid")
        missing = [field for field in RAW_FIELDS if field not in row]
        if missing:
            raise FutWeeklyMonthlyCurrentSnapshotConsumerError("raw_field_missing")
        facts.append(
            FutWeeklyMonthlyRawFact(
                trade_date=trade_date,
                freq=freq,
                ts_code=_text(row.get("ts_code"), "row.ts_code"),
                receipt_id=receipt,
                lineage_sha256=lineage_sha256,
                raw_values={field: row[field] for field in RAW_FIELDS},
            )
        )
    return facts


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise FutWeeklyMonthlyCurrentSnapshotConsumerError(f"{name}_invalid")
    return value


def _day_text(value: Any) -> str:
    text = _text(value, "trade_date")
    if not _DAY.fullmatch(text):
        raise FutWeeklyMonthlyCurrentSnapshotConsumerError("trade_date_invalid")
    return text


def _sha256_text(value: Any, name: str) -> str:
    text = _text(value, name)
    if not _SHA256.fullmatch(text):
        raise FutWeeklyMonthlyCurrentSnapshotConsumerError(f"{name}_invalid")
    return text


def _time(value: Any, name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        if not isinstance(value, str) or not value.strip():
            raise FutWeeklyMonthlyCurrentSnapshotConsumerError(f"{name}_missing")
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise FutWeeklyMonthlyCurrentSnapshotConsumerError(f"{name}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise FutWeeklyMonthlyCurrentSnapshotConsumerError(f"{name}_timezone_invalid")
    return parsed


def _decision_time(value: Any) -> datetime:
    if not isinstance(value, datetime):
        raise FutWeeklyMonthlyCurrentSnapshotConsumerError("decision_time_invalid")
    if value.tzinfo is None or value.utcoffset() is None:
        raise FutWeeklyMonthlyCurrentSnapshotConsumerError("decision_time_timezone_invalid")
    return value


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()
