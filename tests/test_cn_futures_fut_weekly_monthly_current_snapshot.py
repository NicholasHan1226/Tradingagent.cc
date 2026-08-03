"""Failure-first contract tests for receipt-bound weekly/monthly raw facts."""

from __future__ import annotations

import copy
from dataclasses import replace
from datetime import datetime
import hashlib
import json
from typing import Callable

import pytest

from CNFutures.fut_weekly_monthly_current_snapshot import (
    FutWeeklyMonthlyCurrentSnapshotConsumerError,
    load_fut_weekly_monthly_current_snapshot,
)
from shared.data.sharedsignals_v1 import (
    HTTPResponse,
    SharedSignalsV1Client,
    SharedSignalsV1Config,
)


TRADE_DATE = "20260731"
CATALOG_VERSION = "v1-a97bfae120a50aa3"
RECEIPT_ID = "receipt:ab67d136e8c05d4c5f3d5d505395799695b6caf1834288377bb82b2a0365d2e6"
LINEAGE = {"complete": True, "provider_neutral": True, "provider": "fixture"}
LINEAGE_SHA256 = "e31df4a83968ee7e9c8d1e852232df037a72d36842d278d633b52faed2ac7740"
DECISION_TIME = datetime.fromisoformat("2026-08-03T23:20:00+00:00")
QUERY_FIELDS = (
    "ts_code", "trade_date", "end_date", "freq", "open", "high", "low", "close",
    "pre_close", "settle", "pre_settle", "vol", "amount", "oi", "oi_chg",
    "exchange", "change1", "change2",
)


def _sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _row(index: int, *, freq: str) -> dict[str, object]:
    return {
        "ts_code": f"{freq[:1].upper()}{index:04d}.FIX",
        "trade_date": TRADE_DATE,
        "end_date": TRADE_DATE,
        "freq": freq,
        "open": 100.0 + index,
        "high": 101.0 + index,
        "low": 99.0 + index,
        "close": 100.5 + index,
        "pre_close": 100.0 + index,
        "settle": 100.25 + index,
        "pre_settle": 99.75 + index,
        "vol": index + 1,
        "amount": 1000.0 + index,
        "oi": index + 2,
        "oi_chg": index - 1,
        "exchange": "FIX",
        "change1": 0.25,
        "change2": 0.5,
    }


def _rows() -> list[dict[str, object]]:
    return [
        *[_row(index, freq="week") for index in range(1081)],
        *[_row(index, freq="month") for index in range(1150)],
    ]


def _catalog_row() -> dict[str, object]:
    return {
        "dataset_id": "cn.dataset.fut_weekly_monthly",
        "schema_major": 1,
        "default_fields": list(QUERY_FIELDS),
        "identity_fields": ["trade_date", "freq", "ts_code"],
        "default_order": ["trade_date:asc", "freq:asc", "ts_code:asc"],
        "filter_operators": {"trade_date": ["eq"]},
    }


def _metadata(**overrides: object) -> dict[str, object]:
    metadata: dict[str, object] = {
        "state": "ready",
        "degraded": False,
        "reasons": [],
        "freshness": {"state": "fresh", "stale": False},
        "quality": {"state": "valid", "valid": True},
        "receipt_id": RECEIPT_ID,
        "lineage": copy.deepcopy(LINEAGE),
        "data_through": "2026-08-03T23:00:00+00:00",
        "observed_at": "2026-08-03T23:01:00+00:00",
    }
    metadata.update(overrides)
    return metadata


class FixtureTransport:
    def __init__(
        self,
        *,
        rows: list[dict[str, object]] | None = None,
        catalog_row: dict[str, object] | None = None,
        metadata: dict[str, object] | None = None,
        terminal: bool = True,
        replay_mutator: Callable[[list[dict[str, object]]], None] | None = None,
    ) -> None:
        self.rows = copy.deepcopy(rows if rows is not None else _rows())
        self.catalog_row = copy.deepcopy(catalog_row or _catalog_row())
        self.metadata = copy.deepcopy(metadata or _metadata())
        self.terminal = terminal
        self.replay_mutator = replay_mutator
        self.run_index = -1

    def __call__(self, **kwargs: object) -> HTTPResponse:
        if kwargs["method"] == "GET":
            return HTTPResponse(
                200,
                {
                    "api_version": "v1",
                    "catalog_version": CATALOG_VERSION,
                    "request_id": "catalog",
                    "data": [self.catalog_row],
                },
            )
        body = kwargs["json_body"]
        assert isinstance(body, dict)
        assert body["filters"] == {"trade_date": {"eq": TRADE_DATE}}
        assert body["fields"] == list(QUERY_FIELDS)
        assert body["order"] == ["trade_date:asc", "freq:asc", "ts_code:asc"]
        assert body["limit"] == 500
        assert "as_of" not in body
        cursor = body.get("cursor")
        if cursor is None:
            self.run_index += 1
        values = copy.deepcopy(self.rows)
        if self.run_index == 1 and self.replay_mutator is not None:
            self.replay_mutator(values)
        offset = 0 if cursor is None else int(str(cursor).rsplit(":", 1)[1])
        page = values[offset : offset + 500]
        next_cursor = (
            f"cursor:{offset + 500}"
            if offset + 500 < len(values)
            else (None if self.terminal else "cursor:terminal")
        )
        return HTTPResponse(
            200,
            {
                "api_version": "v1",
                "catalog_version": CATALOG_VERSION,
                "request_id": f"query-{self.run_index}-{offset}",
                "dataset_id": "cn.dataset.fut_weekly_monthly",
                "data": page,
                "next_cursor": next_cursor,
                "metadata": self.metadata,
            },
        )


def _load(transport: FixtureTransport):
    client = SharedSignalsV1Client(
        SharedSignalsV1Config(
            base_url="https://fixture.cnfutures.invalid",
            expected_catalog_version=CATALOG_VERSION,
            dataset_ids=frozenset({"cn.dataset.fut_weekly_monthly"}),
            access_policy_id="fixture-weekly-monthly",
            max_limit=500,
            cache_ttl_seconds=0,
        ),
        transport=transport,
    )
    return load_fut_weekly_monthly_current_snapshot(
        client=client,
        trade_date=TRADE_DATE,
        expected_catalog_version=CATALOG_VERSION,
        expected_receipt_id=RECEIPT_ID,
        expected_lineage_sha256=_sha256(LINEAGE),
        decision_time=DECISION_TIME,
    )


def test_maps_exact_terminal_week_and_month_raw_facts() -> None:
    snapshot = _load(FixtureTransport())

    assert snapshot.page_count == 5
    assert snapshot.row_count == 2231
    assert snapshot.week_row_count == 1081
    assert snapshot.month_row_count == 1150
    assert len(snapshot.facts) == 2231
    assert snapshot.facts[0].raw_values["end_date"] == TRADE_DATE
    assert all(fact.receipt_id == RECEIPT_ID for fact in snapshot.facts)
    assert all(fact.lineage_sha256 == _sha256(LINEAGE) for fact in snapshot.facts)
    assert snapshot.data_through <= snapshot.observed_at <= snapshot.decision_time
    assert not any(
        (
            snapshot.stable,
            snapshot.pit_authority,
            snapshot.session_authority,
            snapshot.rollover_authority,
            snapshot.simulation_ready,
            snapshot.runtime_eligible,
            snapshot.execution_eligible,
            snapshot.trading_eligible,
        )
    )


@pytest.mark.parametrize(
    ("mutator", "reason"),
    (
        (lambda row: row.update(schema_major=2), "catalog_schema_invalid"),
        (lambda row: row.update(identity_fields=["trade_date", "ts_code"]), "catalog_identity_invalid"),
        (lambda row: row.update(default_order=["trade_date:asc", "ts_code:asc"]), "catalog_order_invalid"),
        (lambda row: row.update(default_fields=["trade_date", "freq", "ts_code"]), "catalog_raw_fields_missing"),
    ),
)
def test_rejects_catalog_drift(mutator: Callable[[dict[str, object]], None], reason: str) -> None:
    catalog = _catalog_row()
    mutator(catalog)
    with pytest.raises(FutWeeklyMonthlyCurrentSnapshotConsumerError, match=reason):
        _load(FixtureTransport(catalog_row=catalog))


@pytest.mark.parametrize(
    ("metadata", "reason"),
    (
        (_metadata(state="partial"), "metadata_contract_invalid"),
        (_metadata(receipt_id="receipt:other"), "receipt_mismatch"),
        (_metadata(lineage={"complete": True, "provider_neutral": True, "revision": "other"}), "lineage_mismatch"),
        (_metadata(data_through="2026-08-03T23:02:00+00:00", observed_at="2026-08-03T23:01:00+00:00"), "tradingdatas_read_failed"),
    ),
)
def test_rejects_metadata_drift(metadata: dict[str, object], reason: str) -> None:
    with pytest.raises(FutWeeklyMonthlyCurrentSnapshotConsumerError, match=reason):
        _load(FixtureTransport(metadata=metadata))


def test_rejects_pagination_replay_identity_and_raw_field_drift() -> None:
    with pytest.raises(FutWeeklyMonthlyCurrentSnapshotConsumerError, match="replay_drift"):
        _load(FixtureTransport(replay_mutator=lambda values: values[0].update(close=0.0)))
    with pytest.raises(FutWeeklyMonthlyCurrentSnapshotConsumerError):
        _load(FixtureTransport(rows=_rows() * 2))
    invalid_count = _rows()[:-1]
    with pytest.raises(FutWeeklyMonthlyCurrentSnapshotConsumerError, match="row_count_invalid"):
        _load(FixtureTransport(rows=invalid_count))
    invalid_freq = _rows()
    invalid_freq[0]["freq"] = "quarter"
    with pytest.raises(FutWeeklyMonthlyCurrentSnapshotConsumerError, match="frequency_partition_invalid"):
        _load(FixtureTransport(rows=invalid_freq))
    missing = _rows()
    missing[0].pop("end_date")
    with pytest.raises(FutWeeklyMonthlyCurrentSnapshotConsumerError, match="raw_field_missing"):
        _load(FixtureTransport(rows=missing))
    with pytest.raises(FutWeeklyMonthlyCurrentSnapshotConsumerError):
        _load(FixtureTransport(terminal=False))


def test_rejects_fact_binding_and_authority_lift() -> None:
    snapshot = _load(FixtureTransport())
    with pytest.raises(FutWeeklyMonthlyCurrentSnapshotConsumerError, match="snapshot_fact_binding_invalid"):
        replace(snapshot, facts=(replace(snapshot.facts[0], receipt_id="receipt:drift"), *snapshot.facts[1:]))
    with pytest.raises(FutWeeklyMonthlyCurrentSnapshotConsumerError, match="snapshot_authority_invalid"):
        replace(snapshot, stable=True)
