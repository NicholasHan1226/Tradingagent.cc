from __future__ import annotations

from datetime import datetime, timezone

import pytest

from Ashare.rt_min_daily_pit import (
    RT_MIN_DATASET_ID,
    RT_MIN_EXACT_SLOT_FREQUENCIES,
    RT_MIN_DAILY_DATASET_ID,
    RtMinDailyPITContractError,
    build_rt_min_exact_slot_proof_envelope,
    build_rt_min_daily_pit_feature_contract,
    load_rt_min_exact_slot_proof_envelope,
)
from shared.data.sharedsignals_v1 import QueryRequest, parse_query_envelope
from shared.data.sharedsignals_v1 import HTTPResponse, SharedSignalsV1Client, SharedSignalsV1Config
from shared.data.tradingdatas_pagination import bind_complete_page


def _exact_rows_and_proofs() -> tuple[list[dict], list[dict], tuple[str, ...]]:
    symbols = tuple(
        [f"{index:06d}.SZ" for index in range(1, 16)]
        + [f"{index:06d}.SH" for index in range(600000, 600015)]
    )
    rows = [
        {
            "ts_code": symbol,
            "freq": "5MIN",
            "time": "2026-08-13 09:40:00",
            "open": 10.0,
            "close": 10.1,
            "high": 10.2,
            "low": 9.9,
            "vol": 1000.0,
            "amount": 10100.0,
        }
        for symbol in symbols
    ]
    import hashlib
    import json

    def digest(value: object) -> str:
        return hashlib.sha256(
            json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest()

    proofs = [
        {
            "page_index": index,
            "row_identity_sha256": digest({"ts_code": row["ts_code"], "freq": row["freq"], "time": row["time"]}),
            "provider": "tushare",
            "source": "tushare",
            "receipt_id": f"receipt-09:40-{index % 5}",
            "dataset_id": RT_MIN_DATASET_ID,
            "status": "success",
            "execution_id": "exec-09:40",
            "config_hash": "a" * 64,
            "request_window": {},
            "data_through": "2026-08-13 09:40:00",
            "finished_at": "2026-08-13T01:41:00+00:00",
            "receipt_proof_sha256": digest(["receipt-09:40", index % 5]),
        }
        for index, row in enumerate(rows)
    ]
    return rows, proofs, symbols


def test_rt_min_exact_slot_adapter_accepts_ordered_30_row_five_receipt_cohort() -> None:
    rows, proofs, symbols = _exact_rows_and_proofs()
    envelope = parse_query_envelope(
        {
            "api_version": "v1",
            "catalog_version": "catalog-20260813",
            "request_id": "query-09:40",
            "dataset_id": RT_MIN_DATASET_ID,
            "data": rows,
            "next_cursor": None,
            "metadata": {
                "state": "ready",
                "degraded": False,
                "freshness": {"state": "fresh"},
                "quality": {"state": "valid"},
                "lineage": {"complete": True, "authority": "td-receipt"},
                "receipt_id": "latest-failed-must-not-be-used",
                "data_through": "2026-08-13T01:40:00+00:00",
                "observed_at": "2026-08-13T05:00:00+00:00",
                "reasons": [],
                "row_receipt_proofs": proofs,
            },
        }
    )
    result = build_rt_min_exact_slot_proof_envelope(
        envelope=envelope,
        requested_symbols=symbols,
        requested_slot="2026-08-13 09:40:00",
        decision_as_of=datetime(2026, 8, 13, 2, 0, tzinfo=timezone.utc),
    )
    assert result.quality_status == "usable"
    assert len(result.rows) == len(result.row_receipt_proofs) == 30
    assert [proof["page_index"] for proof in result.row_receipt_proofs] == list(range(30))
    assert [row["ts_code"] for row in result.rows] == list(result.accepted_symbols)
    assert len(set(result.receipt_ids)) == 5
    assert result.receipt_lineage is True
    assert result.learning_eligible is False
    assert result.promotion_eligible is False
    assert result.execution_authority is False
    assert result.real_trading_enabled is False


@pytest.mark.parametrize("frequency", ["1MIN", "5min"])
def test_rt_min_exact_slot_accepts_canonical_frequency_equivalent_spellings(
    frequency: str,
) -> None:
    rows, proofs, symbols = _exact_rows_and_proofs()
    for row in rows:
        row["freq"] = frequency
    envelope = parse_query_envelope(
        {
            "api_version": "v1", "catalog_version": "catalog-20260813",
            "request_id": "query-frequency-equivalent", "dataset_id": RT_MIN_DATASET_ID,
            "data": rows, "next_cursor": None,
            "metadata": {
                "state": "failed", "degraded": True,
                "freshness": {"state": "failed"}, "quality": {"state": "degraded"},
                "lineage": {"complete": True}, "receipt_id": "latest-failed",
                "data_through": "2026-08-13T01:40:00+00:00",
                "observed_at": "2026-08-13T05:00:00+00:00", "reasons": [],
                "row_receipt_proofs": proofs,
            },
        }
    )
    result = build_rt_min_exact_slot_proof_envelope(
        envelope=envelope,
        requested_symbols=symbols,
        requested_slot="2026-08-13 09:40:00",
        decision_as_of=datetime(2026, 8, 13, 2, 0, tzinfo=timezone.utc),
    )
    assert result.accepted_symbols == symbols
    assert result.rows[0]["freq"] == frequency


def test_rt_min_exact_slot_rejects_frequency_not_in_production_contract() -> None:
    rows, proofs, symbols = _exact_rows_and_proofs()
    rows[0]["freq"] = "15MIN"
    envelope = parse_query_envelope(
        {
            "api_version": "v1", "catalog_version": "catalog-20260813",
            "request_id": "query-frequency-invalid", "dataset_id": RT_MIN_DATASET_ID,
            "data": rows, "next_cursor": None,
            "metadata": {
                "state": "failed", "degraded": True,
                "freshness": {"state": "failed"}, "quality": {"state": "degraded"},
                "lineage": {"complete": True}, "receipt_id": "latest-failed",
                "data_through": "2026-08-13T01:40:00+00:00",
                "observed_at": "2026-08-13T05:00:00+00:00", "reasons": [],
                "row_receipt_proofs": proofs,
            },
        }
    )
    with pytest.raises(RtMinDailyPITContractError, match="row_freq_invalid"):
        build_rt_min_exact_slot_proof_envelope(
            envelope=envelope,
            requested_symbols=symbols,
            requested_slot="2026-08-13 09:40:00",
            decision_as_of=datetime(2026, 8, 13, 2, 0, tzinfo=timezone.utc),
        )


def test_rt_min_exact_slot_adapter_uses_existing_client_query_opt_in() -> None:
    rows, proofs, symbols = _exact_rows_and_proofs()
    calls: list[str] = []

    def transport(*, method, url, headers, json_body, timeout_seconds):
        calls.append(url)
        if url.endswith("/v1/catalog"):
            return HTTPResponse(
                200,
                {
                    "api_version": "v1",
                    "catalog_version": "catalog-20260813",
                    "request_id": "catalog-1",
                    "data": [{"dataset_id": RT_MIN_DATASET_ID}],
                },
            )
        assert json_body["include_receipt_proofs"] is True
        return HTTPResponse(
            200,
            {
                "api_version": "v1",
                "catalog_version": "catalog-20260813",
                "request_id": "query-09:40",
                "dataset_id": RT_MIN_DATASET_ID,
                "data": rows,
                "next_cursor": None,
                "metadata": {
                    "state": "ready", "degraded": False,
                    "freshness": {"state": "fresh"}, "quality": {"state": "valid"},
                    "lineage": {"complete": True}, "receipt_id": "latest-failed",
                    "data_through": "2026-08-13T01:40:00+00:00",
                    "observed_at": "2026-08-13T05:00:00+00:00", "reasons": [],
                    "row_receipt_proofs": proofs,
                },
            },
        )

    client = SharedSignalsV1Client(
        SharedSignalsV1Config(
            base_url="https://td.test",
            expected_catalog_version="catalog-20260813",
            dataset_ids=frozenset({RT_MIN_DATASET_ID}),
            access_policy_id="fixture",
            catalog_version_policy="evidence_only",
        ),
        transport=transport,
    )
    result = load_rt_min_exact_slot_proof_envelope(
        client=client,
        request=QueryRequest(
            dataset_id=RT_MIN_DATASET_ID,
            schema_major=1,
            fields=("ts_code", "freq", "time"),
            filters={"time": {"eq": "2026-08-13 09:40:00"}},
            order=("time", "ts_code"),
            limit=30,
            include_receipt_proofs=True,
        ),
        requested_symbols=symbols,
        requested_slot="2026-08-13 09:40:00",
        decision_as_of=datetime(2026, 8, 13, 2, 0, tzinfo=timezone.utc),
    )
    assert len(calls) == 2
    assert len(result.rows) == len(result.row_receipt_proofs) == 30


def test_rt_min_exact_slot_ignores_latest_failed_runtime_metadata() -> None:
    rows, proofs, symbols = _exact_rows_and_proofs()
    envelope = parse_query_envelope(
        {
            "api_version": "v1",
            "catalog_version": "catalog-20260813",
            "request_id": "query-latest-failed",
            "dataset_id": RT_MIN_DATASET_ID,
            "data": rows,
            "next_cursor": None,
            "metadata": {
                "state": "failed",
                "degraded": True,
                "freshness": {"state": "failed"},
                "quality": {"state": "degraded"},
                "lineage": {"complete": True},
                "receipt_id": "later-failed-receipt",
                "data_through": "2026-08-13T01:40:00+00:00",
                "observed_at": "2026-08-13T05:00:00+00:00",
                "reasons": ["latest_runtime_failed"],
                "row_receipt_proofs": proofs,
            },
        }
    )
    result = build_rt_min_exact_slot_proof_envelope(
        envelope=envelope,
        requested_symbols=symbols,
        requested_slot="2026-08-13 09:40:00",
        decision_as_of=datetime(2026, 8, 13, 2, 0, tzinfo=timezone.utc),
    )
    assert result.accepted_symbols == symbols
    assert result.receipt_lineage is True
    assert result.execution_authority is False


@pytest.mark.parametrize(
    "mutation,reason",
    [
        (lambda rows, proofs: proofs.pop(), "row_receipt_proof_alignment_invalid"),
        (lambda rows, proofs: proofs[1].__setitem__("page_index", 0), "row_receipt_proof_page_index_mismatch"),
        (lambda rows, proofs: proofs[2].__setitem__("execution_id", "other"), "row_receipt_proof_cohort_mismatch"),
        (lambda rows, proofs: proofs[3].__setitem__("data_through", "2026-08-13 09:45:00"), "row_receipt_proof_data_through_mismatch"),
        (lambda rows, proofs: rows[4].__setitem__("time", "2026-08-13 09:45:00"), "row_time_slot_mismatch"),
        (lambda rows, proofs: proofs[5].__setitem__("provider", "other"), "row_receipt_proof_provider_mismatch"),
        (lambda rows, proofs: proofs[6].__setitem__("status", "failed"), "row_receipt_proof_not_success"),
        (lambda rows, proofs: proofs[7].__setitem__("finished_at", "2026-08-13T02:01:00+00:00"), "row_receipt_proof_after_decision_as_of"),
        (lambda rows, proofs: proofs[8].__setitem__("dataset_id", "cn.dataset.other"), "row_receipt_proof_dataset_mismatch"),
        (lambda rows, proofs: proofs[9].pop("receipt_id"), "row_receipt_proof_receipt_id_invalid"),
    ],
)
def test_rt_min_exact_slot_adapter_fails_closed_on_proof_gaps(
    mutation, reason: str
) -> None:
    rows, proofs, symbols = _exact_rows_and_proofs()
    mutation(rows, proofs)
    envelope = parse_query_envelope(
        {
            "api_version": "v1",
            "catalog_version": "catalog-20260813",
            "request_id": "query-bad",
            "dataset_id": RT_MIN_DATASET_ID,
            "data": rows,
            "next_cursor": None,
            "metadata": {
                "state": "ready", "degraded": False,
                "freshness": {"state": "fresh"}, "quality": {"state": "valid"},
                "lineage": {"complete": True}, "receipt_id": "r",
                "data_through": "2026-08-13T01:40:00+00:00",
                "observed_at": "2026-08-13T05:00:00+00:00", "reasons": [],
                "row_receipt_proofs": proofs,
            },
        }
    )
    with pytest.raises(RtMinDailyPITContractError, match=reason):
        build_rt_min_exact_slot_proof_envelope(
            envelope=envelope,
            requested_symbols=symbols,
            requested_slot="2026-08-13 09:40:00",
            decision_as_of=datetime(2026, 8, 13, 2, 0, tzinfo=timezone.utc),
        )


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
