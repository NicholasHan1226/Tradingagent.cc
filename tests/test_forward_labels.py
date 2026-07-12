from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from shared.review.forward_labels import (
    CANONICAL_HORIZONS,
    build_prediction_snapshot,
    canonical_horizon,
    materialize_forward_labels,
    validate_point_in_time_lineage,
    _stable_label_update_id,
)


UTC = timezone.utc


def test_point_in_time_lineage_requires_all_four_ordered_timestamps():
    valid = validate_point_in_time_lineage(
        {
            "event_time": "2026-07-13T09:30:00+08:00",
            "available_at": "2026-07-13T09:30:01+08:00",
            "ingested_at": "2026-07-13T09:30:02+08:00",
            "retrieved_as_of": "2026-07-13T09:30:03+08:00",
            "prediction_at": "2026-07-13T09:30:04+08:00",
        }
    )
    missing = validate_point_in_time_lineage(
        {
            "event_time": "2026-07-13T09:30:00+08:00",
            "retrieved_as_of": "2026-07-13T09:30:03+08:00",
            "prediction_at": "2026-07-13T09:30:04+08:00",
        }
    )
    out_of_order = validate_point_in_time_lineage(
        {
            "event_time": "2026-07-13T09:30:02+08:00",
            "available_at": "2026-07-13T09:30:01+08:00",
            "ingested_at": "2026-07-13T09:30:03+08:00",
            "retrieved_as_of": "2026-07-13T09:30:04+08:00",
            "prediction_at": "2026-07-13T09:30:05+08:00",
        }
    )

    assert valid["status"] == "valid"
    assert valid["complete"] is True
    assert missing["status"] == "missing_timestamps"
    assert missing["missing_fields"] == ["available_at", "ingested_at"]
    assert out_of_order["status"] == "invalid_timestamp_order"
    assert out_of_order["complete"] is False


def _candidate(**overrides):
    row = {
        "market": "ashare",
        "symbol": "600000.SH",
        "style": "trend_breakout",
        "strategy_version": "trend-v1",
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
        "real_trading_enabled": True,
        "live_execution_enabled": True,
    }
    row.update(overrides)
    return row


def _targets(start: datetime) -> dict[str, datetime]:
    return {
        "m30": start + timedelta(minutes=30),
        "m60": start + timedelta(minutes=60),
        "close": start + timedelta(hours=5, minutes=30),
        "1d": start + timedelta(days=1),
        "3d": start + timedelta(days=3),
        "5d": start + timedelta(days=5),
    }


# -- v2 schema tests -----------------------------------------------------------


def test_snapshot_has_v2_fields_not_legacy_probability():
    candidate = _candidate()
    snapshot = build_prediction_snapshot(candidate)
    assert "raw_style_score" in snapshot
    assert "score_semantics" in snapshot
    assert snapshot["calibrated_probability"] is None
    assert snapshot["probability_model_state"] == "not_calibrated"
    # No legacy probability in v2 snapshots
    assert "probability" not in snapshot


# -- cost evidence tests -------------------------------------------------------


def test_labels_include_cost_model_version():
    start = datetime(2026, 7, 13, 1, 30, tzinfo=UTC)
    snapshot = build_prediction_snapshot(_candidate())
    points = [
        {
            "timestamp": start + timedelta(minutes=30),
            "price": 10.2,
            "reliable": True,
            "source": "sharedsignals.5min",
        },
    ]

    result = materialize_forward_labels(
        snapshot,
        points,
        as_of=start + timedelta(minutes=30),
        horizon_targets=_targets(start),
    )

    label = result["labels"]["m30"]
    assert label["status"] == "ready"
    assert label["cost_model_version"] == "ashare-execution-reality-20260706-v1"
    assert label["fee_bps"] == 105.0
    assert label["slippage_bps"] == 10.0
    assert label["total_cost_bps"] == 115.0


def test_labels_rejected_when_no_cost_evidence():
    start = datetime(2026, 7, 13, 1, 30, tzinfo=UTC)
    candidate = _candidate()
    candidate.pop("costs", None)
    snapshot = build_prediction_snapshot(candidate)
    points = [
        {
            "timestamp": start + timedelta(minutes=30),
            "price": 10.2,
            "reliable": True,
            "source": "sharedsignals.5min",
        },
    ]

    result = materialize_forward_labels(
        snapshot,
        points,
        as_of=start + timedelta(minutes=30),
        horizon_targets=_targets(start),
    )

    label = result["labels"]["m30"]
    assert label["status"] == "rejected_missing_cost_evidence"
    assert label["reason"] == "no_versioned_cost_evidence"
    assert label["cost_model_version"] is None


def test_costs_with_no_model_version_also_rejected():
    start = datetime(2026, 7, 13, 1, 30, tzinfo=UTC)
    candidate = _candidate()
    # Remove cost_model_version from embedded costs
    candidate["costs"] = {"round_trip_fee_bps": 10.0, "round_trip_slippage_bps": 5.0}
    snapshot = build_prediction_snapshot(candidate)
    points = [
        {
            "timestamp": start + timedelta(minutes=30),
            "price": 10.2,
            "reliable": True,
            "source": "sharedsignals.5min",
        },
    ]

    result = materialize_forward_labels(
        snapshot,
        points,
        as_of=start + timedelta(minutes=30),
        horizon_targets=_targets(start),
    )

    assert result["labels"]["m30"]["status"] == "rejected_missing_cost_evidence"


def test_label_idempotency_fingerprint_includes_cost_version():
    id1 = _stable_label_update_id(
        "snap-1", "2026-07-13T02:00:00+00:00", "ashare-execution-reality-20260706-v1"
    )
    id2 = _stable_label_update_id(
        "snap-1", "2026-07-13T02:00:00+00:00", "ashare-execution-reality-20260706-v1"
    )
    id_no_cost = _stable_label_update_id("snap-1", "2026-07-13T02:00:00+00:00", None)
    id_diff_version = _stable_label_update_id(
        "snap-1", "2026-07-13T02:00:00+00:00", "different-model"
    )

    # Same inputs produce same fingerprint
    assert id1 == id2
    # Different cost versions produce different fingerprints
    assert id1 != id_no_cost
    assert id1 != id_diff_version


def test_label_idempotency_fingerprint_includes_evidence_id():
    """Different evidence ids produce different fingerprints even with same cost model."""
    base_args = ("snap-1", "2026-07-13T02:00:00+00:00", "actual_execution_costs_v1")

    id_no_evidence = _stable_label_update_id(*base_args)
    id_with_evidence_1 = _stable_label_update_id(
        *base_args, cost_evidence_id="event-fill-1"
    )
    id_with_evidence_2 = _stable_label_update_id(
        *base_args, cost_evidence_id="event-fill-2"
    )
    id_same_evidence = _stable_label_update_id(
        *base_args, cost_evidence_id="event-fill-1"
    )

    assert id_no_evidence != id_with_evidence_1
    assert id_with_evidence_1 != id_with_evidence_2
    assert id_with_evidence_1 == id_same_evidence


def test_net_return_calculation_at_10_yuan_100_shares():
    """10元/100股: 买入1000元, 涨到10.2卖出1020, 费105bps+滑10bps=115bps"""
    start = datetime(2026, 7, 13, 1, 30, tzinfo=UTC)
    snapshot = build_prediction_snapshot(_candidate(reference_price=10.0))
    points = [
        {
            "timestamp": start + timedelta(minutes=30),
            "price": 10.2,
            "reliable": True,
            "source": "sharedsignals.5min",
        },
    ]

    result = materialize_forward_labels(
        snapshot,
        points,
        as_of=start + timedelta(minutes=30),
        horizon_targets=_targets(start),
    )

    label = result["labels"]["m30"]
    # gross return = (10.2 - 10.0) / 10.0 = 0.02
    assert label["market_return"] == pytest.approx(0.02)
    assert label["gross_return_after_direction"] == pytest.approx(0.02)
    # total cost = 115 bps = 0.0115
    assert label["total_cost_bps"] == pytest.approx(115.0)
    # net = 0.02 - 0.0115 = 0.0085
    assert label["net_return_after_costs"] == pytest.approx(0.0085)


# -- existing tests preserved with v2 compatibility ----------------------------


def test_snapshot_records_observation_even_when_strategy_and_execution_gates_fail():
    candidate = _candidate()
    original = deepcopy(candidate)

    snapshot = build_prediction_snapshot(candidate)

    assert candidate == original
    assert snapshot["snapshot_status"] == "recorded"
    assert snapshot["sample_layer"] == "observation_counterfactual"
    assert snapshot["forward_label_eligibility"] == "eligible"
    assert snapshot["mature_threshold_passed"] is False
    assert snapshot["execution_gate_passed"] is False
    assert snapshot["real_trading_enabled"] is False
    assert snapshot["live_execution_enabled"] is False


@pytest.mark.parametrize(
    ("quality", "reason"),
    [
        (
            {"reliable": False, "source": "sharedsignals.5min"},
            "unreliable_reference_data",
        ),
        (
            {"reliable": True, "price_timestamp": "2026-07-13T01:30:00+00:00"},
            "missing_reference_source",
        ),
        (
            {"reliable": True, "source": "sharedsignals.5min"},
            "missing_reference_timestamp",
        ),
    ],
)
def test_snapshot_is_still_recorded_but_label_ineligible_when_data_evidence_is_bad(
    quality, reason
):
    candidate = _candidate(data_quality=quality)
    snapshot = build_prediction_snapshot(candidate)

    assert snapshot["snapshot_status"] == "recorded"
    assert snapshot["forward_label_eligibility"] == "rejected_data_quality"
    assert snapshot["forward_label_rejection_reason"] == reason


def test_snapshot_rejects_future_reference_price_without_dropping_prediction():
    candidate = _candidate(
        data_quality={
            "reliable": True,
            "source": "sharedsignals.5min",
            "price_timestamp": "2026-07-13T01:31:00+00:00",
        }
    )

    snapshot = build_prediction_snapshot(candidate)

    assert snapshot["snapshot_status"] == "recorded"
    assert snapshot["forward_label_eligibility"] == "rejected_data_quality"
    assert (
        snapshot["forward_label_rejection_reason"] == "reference_price_after_prediction"
    )


def test_horizon_alias_is_canonical_without_adding_a_second_label_bucket():
    assert CANONICAL_HORIZONS == ("m30", "m60", "close", "1d", "3d", "5d")
    assert canonical_horizon("next-day") == "1d"
    assert canonical_horizon("next_day") == "1d"
    assert canonical_horizon("1d") == "1d"


def test_as_of_prevents_lookahead_and_only_materializes_due_horizons():
    start = datetime(2026, 7, 13, 1, 30, tzinfo=UTC)
    snapshot = build_prediction_snapshot(_candidate())
    points = [
        {
            "timestamp": start + timedelta(minutes=30),
            "price": 10.2,
            "reliable": True,
            "source": "sharedsignals.5min",
        },
        {
            "timestamp": start + timedelta(minutes=60),
            "price": 10.4,
            "reliable": True,
            "source": "sharedsignals.5min",
        },
    ]

    result = materialize_forward_labels(
        snapshot,
        points,
        as_of=start + timedelta(minutes=40),
        horizon_targets=_targets(start),
    )

    assert result["labels"]["m30"]["status"] == "ready"
    assert result["labels"]["m30"]["exit_price"] == 10.2
    assert result["labels"]["m60"]["status"] == "pending_not_due"
    assert result["labels"]["m60"]["exit_price"] is None
    assert result["label_aliases"] == {"next-day": "1d", "next_day": "1d"}
    assert tuple(result["labels"]) == CANONICAL_HORIZONS


def test_net_return_deducts_round_trip_fees_and_slippage_for_long_and_short():
    start = datetime(2026, 7, 13, 1, 30, tzinfo=UTC)
    point = {
        "timestamp": start + timedelta(minutes=30),
        "price": 10.2,
        "reliable": True,
        "source": "sharedsignals.5min",
    }
    kwargs = {
        "as_of": start + timedelta(minutes=30),
        "horizon_targets": _targets(start),
        "costs": {
            "round_trip_fee_bps": 8,
            "round_trip_slippage_bps": 12,
            "cost_model_version": "test-model-v1",
        },
    }

    long_label = materialize_forward_labels(
        build_prediction_snapshot(_candidate(direction="long")), [point], **kwargs
    )["labels"]["m30"]
    short_label = materialize_forward_labels(
        build_prediction_snapshot(_candidate(direction="short")), [point], **kwargs
    )["labels"]["m30"]

    assert long_label["market_return"] == pytest.approx(0.02)
    assert long_label["gross_return_after_direction"] == pytest.approx(0.02)
    assert long_label["total_cost_bps"] == pytest.approx(20)
    assert long_label["net_return_after_costs"] == pytest.approx(0.018)
    assert long_label["cost_model_version"] == "test-model-v1"
    assert short_label["gross_return_after_direction"] == pytest.approx(-0.02)
    assert short_label["net_return_after_costs"] == pytest.approx(-0.022)


def test_unreliable_or_missing_due_exit_evidence_has_explicit_non_ready_status():
    start = datetime(2026, 7, 13, 1, 30, tzinfo=UTC)
    snapshot = build_prediction_snapshot(_candidate())
    result = materialize_forward_labels(
        snapshot,
        [
            {
                "timestamp": start + timedelta(minutes=30),
                "price": 10.2,
                "reliable": False,
                "source": "sharedsignals.5min",
            }
        ],
        as_of=start + timedelta(minutes=60),
        horizon_targets=_targets(start),
    )

    assert result["labels"]["m30"]["status"] == "rejected_data_quality"
    assert result["labels"]["m30"]["reason"] == "unreliable_exit_evidence"
    assert result["labels"]["m60"]["status"] == "missing_exit_evidence"
    assert result["labels"]["m60"]["reason"] == "no_exit_evidence_as_of"


def test_intraday_horizon_never_uses_daily_close_as_a_fake_m30_price():
    start = datetime(2026, 7, 13, 1, 30, tzinfo=UTC)
    snapshot = build_prediction_snapshot(_candidate())
    daily_close = {
        "timestamp": start + timedelta(hours=5, minutes=30),
        "price": 10.8,
        "reliable": True,
        "source": "sharedsignals.daily",
        "eligible_horizons": ["close", "1d", "3d", "5d"],
    }

    result = materialize_forward_labels(
        snapshot,
        [daily_close],
        as_of=start + timedelta(hours=5, minutes=30),
        horizon_targets=_targets(start),
    )

    assert result["labels"]["m30"]["status"] == "missing_exit_evidence"
    assert result["labels"]["m60"]["status"] == "missing_exit_evidence"
    assert result["labels"]["close"]["status"] == "ready"
    assert result["labels"]["close"]["exit_price"] == 10.8


def test_late_price_outside_intraday_evidence_window_is_not_backfilled_as_m30():
    start = datetime(2026, 7, 13, 1, 30, tzinfo=UTC)
    snapshot = build_prediction_snapshot(_candidate())
    late = {
        "timestamp": start + timedelta(minutes=75),
        "price": 10.5,
        "reliable": True,
        "source": "sharedsignals.5min",
    }

    result = materialize_forward_labels(
        snapshot,
        [late],
        as_of=start + timedelta(minutes=75),
        horizon_targets=_targets(start),
    )

    assert result["labels"]["m30"]["status"] == "missing_exit_evidence"
    assert result["labels"]["m60"]["status"] == "ready"


def test_materialization_does_not_mutate_snapshot_or_price_points():
    start = datetime(2026, 7, 13, 1, 30, tzinfo=UTC)
    snapshot = build_prediction_snapshot(_candidate())
    points = [
        {
            "timestamp": start + timedelta(minutes=30),
            "price": 10.1,
            "reliable": True,
            "source": "sharedsignals.5min",
        }
    ]
    before_snapshot = deepcopy(snapshot)
    before_points = deepcopy(points)

    materialize_forward_labels(
        snapshot,
        points,
        as_of=start + timedelta(minutes=30),
        horizon_targets=_targets(start),
    )

    assert snapshot == before_snapshot
    assert points == before_points
