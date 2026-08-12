from __future__ import annotations

from datetime import datetime, timezone

import pytest

from Ashare.rt_min_daily_pit import (
    RT_MIN_DAILY_DATASET_ID,
    RtMinDailyPITContractError,
    build_rt_min_daily_pit_feature_contract,
)
from shared.data.sharedsignals_v1 import QueryRequest, parse_query_envelope
from shared.data.tradingdatas_pagination import bind_complete_page


def _run(*, rows: list[dict], observed_at: str = "2026-08-12T06:00:00+00:00"):
    envelope = parse_query_envelope(
        {
            "api_version": "v1",
            "catalog_version": "catalog-20260812",
            "request_id": "request-rt-min-daily",
            "dataset_id": RT_MIN_DAILY_DATASET_ID,
            "data": rows,
            "next_cursor": None,
            "metadata": {
                "state": "ready",
                "degraded": False,
                "freshness": {"state": "fresh", "fresh": True, "stale": False},
                "quality": {"state": "valid", "valid": True},
                "lineage": {
                    "complete": True,
                    "provider_neutral": True,
                    "authority": "sqlite_ingest_receipts",
                },
                "receipt_id": "receipt:rt-min-daily-test",
                "data_through": "2026-08-12T05:59:00+00:00",
                "observed_at": observed_at,
                "reasons": [],
            },
        }
    )
    return bind_complete_page(
        request=QueryRequest(
            dataset_id=RT_MIN_DAILY_DATASET_ID,
            schema_major=1,
            fields=("ts_code", "freq", "time", "open", "close", "high", "low", "vol", "amount"),
            limit=100,
        ),
        envelope=envelope,
        identity_fields=("ts_code", "freq", "time"),
    )


def _row(symbol: str) -> dict:
    return {
        "ts_code": symbol,
        "freq": "1MIN",
        "time": "2026-08-12 13:59:00",
        "open": 10.0,
        "close": 10.1,
        "high": 10.2,
        "low": 9.9,
        "vol": 1000.0,
        "amount": 10100.0,
    }


def test_rt_min_daily_binds_receipt_and_retains_partial_coverage() -> None:
    contract = build_rt_min_daily_pit_feature_contract(
        page_run=_run(rows=[_row("000001.SZ")]),
        requested_symbols=("000001.SZ", "600000.SH"),
        decision_as_of=datetime(2026, 8, 12, 7, 0, tzinfo=timezone.utc),
    )

    assert contract.quality_status == "usable_degraded"
    assert contract.requested_count == 2
    assert contract.accepted_count == 1
    assert contract.missing_symbols == ("600000.SH",)
    assert contract.receipt_id == "receipt:rt-min-daily-test"
    assert contract.lineage_complete is True
    assert contract.historical_pit_eligible is False
    assert contract.learning_eligible is False
    assert contract.promotion_eligible is False
    assert contract.execution_authority is False
    assert contract.features[0]["calibrated_probability"] is None


def test_rt_min_daily_rejects_future_observation_and_out_of_scope_rows() -> None:
    with pytest.raises(RtMinDailyPITContractError, match="observed_at_after_decision_as_of"):
        build_rt_min_daily_pit_feature_contract(
            page_run=_run(rows=[_row("000001.SZ")], observed_at="2026-08-12T08:00:00+00:00"),
            requested_symbols=("000001.SZ",),
            decision_as_of=datetime(2026, 8, 12, 7, 0, tzinfo=timezone.utc),
        )

    with pytest.raises(RtMinDailyPITContractError, match="row_symbol_out_of_requested_scope"):
        build_rt_min_daily_pit_feature_contract(
            page_run=_run(rows=[_row("000001.SZ")]),
            requested_symbols=("600000.SH",),
            decision_as_of=datetime(2026, 8, 12, 7, 0, tzinfo=timezone.utc),
        )

    future = _row("000001.SZ") | {"time": "2026-08-12 14:01:00"}
    with pytest.raises(RtMinDailyPITContractError, match="row_0_time_after_observed_at"):
        build_rt_min_daily_pit_feature_contract(
            page_run=_run(rows=[future]),
            requested_symbols=("000001.SZ",),
            decision_as_of=datetime(2026, 8, 12, 7, 0, tzinfo=timezone.utc),
        )


def test_rt_min_daily_rejects_incomplete_lineage() -> None:
    page_run = _run(rows=[_row("000001.SZ")])
    metadata = page_run.envelope.metadata
    envelope = parse_query_envelope(
        {
            "api_version": "v1",
            "catalog_version": page_run.envelope.catalog_version,
            "request_id": "request-bad-lineage",
            "dataset_id": RT_MIN_DAILY_DATASET_ID,
            "data": [_row("000001.SZ")],
            "next_cursor": None,
            "metadata": {
                "state": "ready",
                "degraded": False,
                "freshness": {"state": "fresh", "fresh": True, "stale": False},
                "quality": {"state": "valid", "valid": True},
                "lineage": {"complete": False, "provider_neutral": True},
                "receipt_id": metadata.receipt_id,
                "data_through": metadata.data_through,
                "observed_at": metadata.observed_at,
                "reasons": [],
            },
        }
    )
    broken = bind_complete_page(
        request=QueryRequest(dataset_id=RT_MIN_DAILY_DATASET_ID, schema_major=1, limit=100),
        envelope=envelope,
        identity_fields=("ts_code", "freq", "time"),
    )
    with pytest.raises(RtMinDailyPITContractError, match="lineage_incomplete"):
        build_rt_min_daily_pit_feature_contract(
            page_run=broken,
            requested_symbols=("000001.SZ",),
            decision_as_of=datetime(2026, 8, 12, 7, 0, tzinfo=timezone.utc),
        )
