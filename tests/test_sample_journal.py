from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import multiprocessing

import pytest

from shared.review.sample_journal import (
    JOURNAL_SCHEMA_VERSION,
    JournalConflictError,
    JournalSafetyError,
    SampleJournal,
)


UTC = timezone.utc
AUTHORITY = {
    "capital_authority_id": "ashare-capital-v1",
    "authority_generation": 1,
    "execution_lineage_id": "ashare-sim-fresh-20260712-v1",
}


def _candidate(*, style: str = "trend_breakout", **overrides):
    candidate = {
        "market": "ashare",
        "symbol": "600000.SH",
        "style": style,
        "strategy_version": "%s-v1" % style,
        "prediction_at": "2026-07-13T01:30:00+00:00",
        "reference_price": 10.0,
        "direction": "long",
        "raw_style_score": 0.42,
        "score_semantics": "uncalibrated_heuristic",
        "calibrated_probability": None,
        "probability_model_state": "not_calibrated",
        "mature_threshold_passed": False,
        "execution_gate_passed": False,
        "execution_reject_reason": "edge_below_exploitation_threshold",
        "costs": {
            "round_trip_fee_bps": 105.0,
            "round_trip_slippage_bps": 10.0,
            "cost_model_version": "ashare-execution-reality-20260706-v1",
            "cost_basis_notional_cny": 1000.0,
        },
        "data_quality": {
            "reliable": True,
            "source": "sharedsignals.5min",
            "price_timestamp": "2026-07-13T01:30:00+00:00",
        },
        "real_trading_enabled": False,
        "live_execution_enabled": False,
        **AUTHORITY,
    }
    candidate.update(overrides)
    return candidate


def _targets(start: datetime) -> dict[str, datetime]:
    return {
        "m30": start + timedelta(minutes=30),
        "m60": start + timedelta(minutes=60),
        "close": start + timedelta(hours=5, minutes=30),
        "1d": start + timedelta(days=1),
        "3d": start + timedelta(days=3),
        "5d": start + timedelta(days=5),
    }


def _append_worker(path: str, index: int) -> None:
    SampleJournal(path).append_prediction(
        _candidate(
            symbol="%06d.SH" % (600000 + index),
            prediction_at="2026-07-13T01:%02d:00+00:00" % index,
        )
    )


# -- v2 journal schema tests ---------------------------------------------------


def test_journal_schema_is_v2():
    assert JOURNAL_SCHEMA_VERSION == 2


def test_prediction_events_have_v2_cost_evidence():
    candidate = _candidate()
    event = SampleJournal._prediction_event(candidate)
    assert event["journal_schema_version"] == 2
    assert "costs" in event
    assert (
        event["costs"]["cost_model_version"] == "ashare-execution-reality-20260706-v1"
    )
    assert event["primary_label_horizon"] == "1d"
    assert event["sample_science_contract_version"] == "ashare-sample-science-v1"
    assert event["decision_cluster_id"].startswith("decision-cluster:")


def test_label_update_includes_cost_model_version(tmp_path):
    journal = SampleJournal(tmp_path / "samples.jsonl")
    prediction = journal.append_prediction(_candidate())["record"]
    start = datetime(2026, 7, 13, 1, 30, tzinfo=UTC)

    update = journal.materialize_labels(
        prediction["snapshot_id"],
        [
            {
                "timestamp": start + timedelta(minutes=30),
                "price": 10.2,
                "reliable": True,
                "source": "sharedsignals.5min",
            }
        ],
        as_of=start + timedelta(minutes=30),
        horizon_targets=_targets(start),
    )["record"]

    assert update["cost_model_version"] == "ashare-execution-reality-20260706-v1"
    assert (
        update["labels"]["m30"]["cost_model_version"]
        == "ashare-execution-reality-20260706-v1"
    )


def test_cost_versioned_idempotency_prevent_old_zero_cost_collision(tmp_path):
    """Ensure labels with different cost versions get different journal_event_ids."""
    journal = SampleJournal(tmp_path / "samples.jsonl")
    prediction = journal.append_prediction(_candidate())["record"]
    start = datetime(2026, 7, 13, 1, 30, tzinfo=UTC)
    points = [
        {
            "timestamp": start + timedelta(minutes=30),
            "price": 10.2,
            "reliable": True,
            "source": "sharedsignals.5min",
        }
    ]

    # Label with conservative costs
    r1 = journal.materialize_labels(
        prediction["snapshot_id"],
        points,
        as_of=start + timedelta(minutes=30),
        horizon_targets=_targets(start),
    )
    assert r1["status"] == "appended"
    eid1 = r1["record"]["journal_event_id"]

    # Should be idempotent with same costs
    r2 = journal.materialize_labels(
        prediction["snapshot_id"],
        points,
        as_of=start + timedelta(minutes=30),
        horizon_targets=_targets(start),
    )
    assert r2["status"] == "idempotent"
    assert r2["record"]["journal_event_id"] == eid1

    # Different cost model version should NOT collide
    r3 = journal.materialize_labels(
        prediction["snapshot_id"],
        points,
        as_of=start + timedelta(minutes=30),
        horizon_targets=_targets(start),
        costs={
            "round_trip_fee_bps": 20.0,
            "round_trip_slippage_bps": 15.0,
            "cost_model_version": "actual_execution_costs_v1",
        },
    )
    assert r3["status"] == "appended"
    assert r3["record"]["journal_event_id"] != eid1


# -- existing tests ------------------------------------------------------------


def test_prediction_journal_records_each_style_despite_strategy_and_execution_rejection(
    tmp_path,
):
    journal = SampleJournal(tmp_path / "samples.jsonl")
    low_score = _candidate()
    defensive = _candidate(style="defensive_low_volatility", direction="abstain")
    before = (deepcopy(low_score), deepcopy(defensive))

    first = journal.append_prediction(low_score)
    second = journal.append_prediction(defensive)

    assert (low_score, defensive) == before
    assert first["status"] == second["status"] == "appended"
    events = journal.read_events()
    assert len(events) == 2
    assert {event["style"] for event in events} == {
        "trend_breakout",
        "defensive_low_volatility",
    }
    assert all(event["record_type"] == "prediction" for event in events)
    assert all(
        event["sample_layer"] == "observation_counterfactual" for event in events
    )
    assert all(event["mature_threshold_passed"] is False for event in events)
    assert all(event["execution_gate_passed"] is False for event in events)
    assert all(event["real_trading_enabled"] is False for event in events)


def test_unreliable_prediction_is_kept_and_due_labels_are_data_quality_rejected(
    tmp_path,
):
    journal = SampleJournal(tmp_path / "samples.jsonl")
    candidate = _candidate(
        data_quality={"reliable": False, "source": "sharedsignals.5min"}
    )
    prediction = journal.append_prediction(candidate)["record"]
    assert prediction["forward_label_eligibility"] == "rejected_data_quality"


def test_snapshot_id_is_idempotent_and_conflicting_content_fails_closed(tmp_path):
    journal = SampleJournal(tmp_path / "samples.jsonl")
    candidate = _candidate(snapshot_id="fixed-snapshot")

    assert journal.append_prediction(candidate)["status"] == "appended"
    assert journal.append_prediction(candidate)["status"] == "idempotent"
    assert len(journal.read_events()) == 1

    with pytest.raises(JournalConflictError, match="snapshot_id"):
        journal.append_prediction(
            _candidate(snapshot_id="fixed-snapshot", raw_style_score=0.91)
        )
    assert len(journal.read_events()) == 1


def test_prediction_batch_is_single_projection_with_atomic_conflict_handling(tmp_path):
    journal = SampleJournal(tmp_path / "samples.jsonl")
    rows = [
        _candidate(snapshot_id="batch-1", symbol="600001.SH"),
        _candidate(snapshot_id="batch-2", symbol="600002.SH"),
        _candidate(snapshot_id="batch-3", symbol="600003.SH"),
    ]

    first = journal.append_predictions(rows)
    second = journal.append_predictions(rows)

    assert [result["status"] for result in first] == ["appended"] * 3
    assert [result["status"] for result in second] == ["idempotent"] * 3
    assert len(journal.read_events()) == 3

    with pytest.raises(JournalConflictError, match="snapshot_id"):
        journal.append_predictions(
            [
                _candidate(snapshot_id="batch-new", symbol="600004.SH"),
                _candidate(
                    snapshot_id="batch-2", symbol="600002.SH", raw_style_score=0.99
                ),
            ]
        )
    assert {row["snapshot_id"] for row in journal.read_events()} == {
        "batch-1",
        "batch-2",
        "batch-3",
    }


def test_sample_layers_are_persisted_and_counted_without_collapsing(tmp_path):
    journal = SampleJournal(tmp_path / "samples.jsonl")
    journal.append_sample(
        {
            "event_id": "explore-1",
            "record_type": "fill",
            "primary_style": "trend_breakout",
            "sample_intent": "exploration",
            **AUTHORITY,
        }
    )
    journal.append_sample(
        {
            "event_id": "exploit-1",
            "record_type": "fill",
            "primary_style": "trend_breakout",
            "sample_intent": "exploitation",
            **AUTHORITY,
        }
    )
    journal.append_sample(
        {
            "event_id": "reject-1",
            "record_type": "risk_reject",
            "style": "trend_breakout",
            "reject_reason": "single_name_exposure",
            **AUTHORITY,
        }
    )

    kpi = journal.build_kpi()

    assert kpi["sample_layer_totals"]["exploration_fill"] == 1
    assert kpi["sample_layer_totals"]["exploitation_fill"] == 1
    assert kpi["sample_layer_totals"]["risk_reject"] == 1
    with pytest.raises(JournalSafetyError, match="mutually exclusive"):
        journal.append_sample(
            {
                "event_id": "mixed-1",
                "record_type": "fill",
                "sample_intent": "exploration",
                "sample_layers": ["exploration_fill", "risk_reject"],
            }
        )


def test_generic_sample_event_id_is_idempotent_and_conflict_is_rejected(tmp_path):
    journal = SampleJournal(tmp_path / "samples.jsonl")
    row = {
        "event_id": "risk-1",
        "record_type": "risk_reject",
        "style": "event_catalyst",
        "reject_reason": "stale_data",
    }

    assert journal.append_sample(row)["status"] == "appended"
    assert journal.append_sample(row)["status"] == "idempotent"
    with pytest.raises(JournalConflictError, match="journal_event_id"):
        journal.append_sample({**row, "reject_reason": "insufficient_cash"})


def test_sample_batch_is_atomic_idempotent_and_can_guard_pairing_snapshot(tmp_path):
    journal = SampleJournal(tmp_path / "samples.jsonl")
    rows = [
        {
            "event_id": "exit-1",
            "record_type": "exit",
            "sample_intent": "exploration",
            "entry_fill_identity": "entry-1",
        },
        {
            "event_id": "round-1",
            "record_type": "completed_round_trip",
            "sample_intent": "exploration",
            "round_trip_complete": True,
            "gross_pnl_cny": 1.0,
            "net_pnl_cny": 1.0,
        },
    ]

    first = journal.append_samples(rows, expected_event_count=0)
    second = journal.append_samples(rows, expected_event_count=2)

    assert [result["status"] for result in first] == ["appended", "appended"]
    assert [result["status"] for result in second] == ["idempotent", "idempotent"]
    assert len(journal.read_events()) == 2

    with pytest.raises(
        JournalConflictError, match="journal changed during outcome pairing"
    ):
        journal.append_samples(
            [{"event_id": "exit-2", "record_type": "exit"}],
            expected_event_count=0,
        )
    assert len(journal.read_events()) == 2

    with pytest.raises(JournalConflictError, match="journal_event_id"):
        journal.append_samples(
            [rows[0], {**rows[0], "entry_fill_identity": "different"}],
            expected_event_count=2,
        )
    assert len(journal.read_events()) == 2


def test_label_materialization_is_append_idempotent_and_latest_projection_feeds_kpi(
    tmp_path,
):
    journal = SampleJournal(tmp_path / "samples.jsonl")
    prediction = journal.append_prediction(_candidate())["record"]
    start = datetime(2026, 7, 13, 1, 30, tzinfo=UTC)
    kwargs = {
        "as_of": start + timedelta(minutes=30),
        "horizon_targets": _targets(start),
    }
    points = [
        {
            "timestamp": start + timedelta(minutes=30),
            "price": 10.2,
            "reliable": True,
            "source": "sharedsignals.5min",
        }
    ]

    assert (
        journal.materialize_labels(prediction["snapshot_id"], points, **kwargs)[
            "status"
        ]
        == "appended"
    )
    assert (
        journal.materialize_labels(prediction["snapshot_id"], points, **kwargs)[
            "status"
        ]
        == "idempotent"
    )

    assert len(journal.read_events()) == 2
    projected = journal.latest_sample_records()
    assert len(projected) == 1
    assert projected[0]["labels"]["m30"]["status"] == "ready"
    # net = 0.02 - 115/10000 = 0.02 - 0.0115 = 0.0085
    assert projected[0]["labels"]["m30"]["net_return_after_costs"] == pytest.approx(
        0.0085
    )
    assert journal.build_kpi()["styles"]["trend_breakout"]["forward_label_counts"][
        "m30"
    ] == {"ready": 1}


def test_latest_label_projection_compares_timezone_offsets_chronologically(tmp_path):
    journal = SampleJournal(tmp_path / "samples.jsonl")
    prediction = journal.append_prediction(_candidate())["record"]
    start = datetime(2026, 7, 13, 1, 30, tzinfo=UTC)
    targets = _targets(start)
    journal.materialize_labels(
        prediction["snapshot_id"],
        [
            {
                "timestamp": start + timedelta(minutes=30),
                "price": 10.1,
                "reliable": True,
                "source": "sharedsignals.5min",
            }
        ],
        as_of="2026-07-13T10:00:00+08:00",
        horizon_targets=targets,
    )
    journal.materialize_labels(
        prediction["snapshot_id"],
        [
            {
                "timestamp": start + timedelta(minutes=30),
                "price": 10.1,
                "reliable": True,
                "source": "sharedsignals.5min",
            },
            {
                "timestamp": start + timedelta(minutes=60),
                "price": 10.3,
                "reliable": True,
                "source": "sharedsignals.5min",
            },
        ],
        as_of="2026-07-13T02:30:00+00:00",
        horizon_targets=targets,
    )

    latest = journal.latest_sample_records()[0]

    assert latest["labels_as_of"] == "2026-07-13T02:30:00+00:00"
    assert latest["labels"]["m60"]["status"] == "ready"


def test_kpi_uses_only_read_only_authoritative_portfolio_snapshot(tmp_path):
    journal = SampleJournal(tmp_path / "samples.jsonl")
    journal.append_prediction(_candidate(shadow_capital_cny=50_000))
    journal.append_prediction(
        _candidate(
            style="event_catalyst",
            prediction_at="2026-07-13T01:31:00+00:00",
            shadow_capital_cny=50_000,
        )
    )
    portfolio = {
        "source": "ashare_market_capital_ledger",
        "account_equity_cny": 50_000,
        "total_risk_cny": 1_250,
        "gross_exposure_cny": 12_000,
        "as_of": "2026-07-13T07:00:00+00:00",
        "real_trading_enabled": False,
        **AUTHORITY,
    }
    before = deepcopy(portfolio)

    kpi = journal.build_kpi(portfolio_snapshot=portfolio)

    assert portfolio == before
    assert kpi["portfolio"]["account_equity_cny"] == 50_000.0
    assert kpi["portfolio"]["total_risk_cny"] == 1_250.0
    assert kpi["shadow_capital_aggregated"] is False
    assert kpi["real_trading_enabled"] is False


@pytest.mark.parametrize(
    "payload",
    [
        {"real_trading_enabled": True},
        {"live_execution_enabled": True},
        {"account": {"live_broker_enabled": True}},
        {"capital_layer": "real"},
        {"account_type": "live"},
    ],
)
def test_any_live_marker_is_rejected_instead_of_silently_downgraded(tmp_path, payload):
    journal = SampleJournal(tmp_path / "samples.jsonl")

    with pytest.raises(JournalSafetyError, match="live trading marker"):
        journal.append_prediction(_candidate(**payload))


def test_live_portfolio_snapshot_is_rejected(tmp_path):
    journal = SampleJournal(tmp_path / "samples.jsonl")

    with pytest.raises(JournalSafetyError, match="live trading marker"):
        journal.build_kpi(
            portfolio_snapshot={
                "source": "master_capital_ledger",
                "account_equity_cny": 50_000,
                "total_risk_cny": 0,
                "gross_exposure_cny": 0,
                "live_execution_enabled": True,
            }
        )


def test_legacy_or_cross_market_portfolio_snapshot_cannot_enter_current_kpi(tmp_path):
    journal = SampleJournal(tmp_path / "samples.jsonl")
    journal.append_prediction(_candidate())

    with pytest.raises(JournalSafetyError, match="portfolio authority"):
        journal.build_kpi(
            portfolio_snapshot={
                "source": "retired_shared_master",
                "account_equity_cny": 50_000,
                "total_risk_cny": 0,
                "gross_exposure_cny": 0,
                "capital_authority_id": "retired-shared-master",
                "authority_generation": 2,
                "execution_lineage_id": "retired-epoch-2",
                "real_trading_enabled": False,
            }
        )


def test_journal_file_symlink_fails_closed(tmp_path):
    target = tmp_path / "real.jsonl"
    target.write_text("", encoding="utf-8")
    link = tmp_path / "samples.jsonl"
    link.symlink_to(target)

    with pytest.raises(JournalSafetyError, match="symlink"):
        SampleJournal(link).append_prediction(_candidate())
    assert target.read_text(encoding="utf-8") == ""


def test_journal_parent_symlink_fails_closed(tmp_path):
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(JournalSafetyError, match="symlink"):
        SampleJournal(linked_parent / "samples.jsonl").append_prediction(_candidate())
    assert list(real_parent.iterdir()) == []


def test_actual_cost_evidence_id_fingerprint_is_respected(tmp_path):
    """Labels with different execution evidence ids must not silently collide."""
    journal = SampleJournal(tmp_path / "samples.jsonl")
    prediction = journal.append_prediction(_candidate())["record"]
    start = datetime(2026, 7, 13, 1, 30, tzinfo=UTC)
    points = [
        {
            "timestamp": start + timedelta(minutes=30),
            "price": 10.2,
            "reliable": True,
            "source": "sharedsignals.5min",
        }
    ]
    base_kwargs = {
        "as_of": start + timedelta(minutes=30),
        "horizon_targets": _targets(start),
        "costs": {
            "round_trip_fee_bps": 50.0,
            "round_trip_slippage_bps": 15.0,
            "cost_model_version": "actual_execution_costs_v1",
            "cost_evidence_event_id": "fill-event-A",
        },
    }

    r1 = journal.materialize_labels(prediction["snapshot_id"], points, **base_kwargs)
    assert r1["status"] == "appended"

    # Same evidence → idempotent
    r2 = journal.materialize_labels(prediction["snapshot_id"], points, **base_kwargs)
    assert r2["status"] == "idempotent"

    # Different evidence → new append (different fingerprint)
    kwargs_b = deepcopy(base_kwargs)
    kwargs_b["costs"] = {
        **base_kwargs["costs"],
        "cost_evidence_event_id": "fill-event-B",
    }
    r3 = journal.materialize_labels(prediction["snapshot_id"], points, **kwargs_b)
    assert r3["status"] == "appended"
    assert r3["record"]["journal_event_id"] != r1["record"]["journal_event_id"]


def test_process_lock_keeps_concurrent_appends_as_valid_complete_json_lines(tmp_path):
    path = tmp_path / "samples.jsonl"
    context = multiprocessing.get_context("fork")
    processes = [
        context.Process(target=_append_worker, args=(str(path), index))
        for index in range(8)
    ]

    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0

    events = SampleJournal(path).read_events()
    assert len(events) == 8
    assert len({event["snapshot_id"] for event in events}) == 8
