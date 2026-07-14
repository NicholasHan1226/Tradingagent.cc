from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from shared.review.forward_labels import (
    CANONICAL_HORIZONS,
    PIT_TIMESTAMP_FIELDS,
    build_prediction_snapshot,
    canonicalize_evidence_record,
    canonical_horizon,
    evidence_envelope_from_record,
    materialize_forward_labels,
    validate_evidence_envelope,
    validate_point_in_time_lineage,
    _stable_label_update_id,
)


UTC = timezone.utc


def test_embedded_structure_errors_are_irreversible_across_canonicalization():
    raw = {
        "event_time": "2026-07-13T02:00:00+00:00",
        "available_at": "2026-07-13T02:00:01+00:00",
        "ingested_at": "2026-07-13T02:00:02+00:00",
        "retrieved_as_of": "2026-07-13T02:00:03+00:00",
        "price": 99.0,
        "source": "sharedsignals.5min",
        "reliable": True,
        "evidence_envelope": {
            "retrieval_time_fields": "not-a-mapping",
            "structure_errors": ["provider.invalid", "provider.invalid"],
        },
    }

    states = []
    current = raw
    for _ in range(4):
        current = canonicalize_evidence_record(current)
        states.append(current)

    for state in states:
        validation = state["evidence_envelope_validation"]
        assert validation["status"] == "invalid_envelope_structure"
        assert validation["complete"] is False
        errors = state["evidence_envelope"]["structure_errors"]
        assert len(errors) == len(set(errors))
        assert "provider.invalid" in errors
        assert any("retrieval_time_fields" in error for error in errors)

    start = datetime(2026, 7, 13, 1, 30, tzinfo=UTC)
    label = materialize_forward_labels(
        build_prediction_snapshot(_candidate()),
        [states[-1]],
        as_of=start + timedelta(hours=1),
        horizon_targets=_targets(start),
    )["labels"]["m30"]
    assert label["status"] == "missing_exit_evidence"
    assert label["exit_price"] is None
    assert label["point_in_time_lineage"]["complete"] is False


def test_every_present_receipt_alias_obeys_cross_stage_order():
    raw = {
        "bar_time": "2026-07-13T10:00:00+08:00",
        "available_at": "2026-07-13T10:20:00+08:00",
        "published_at": "2026-07-13T10:10:00+08:00",
        "received_at": "2026-07-13T10:15:00+08:00",
        "ingested_at": "2026-07-13T10:25:00+08:00",
        "retrieved_at": "2026-07-13T10:30:00+08:00",
    }
    validation = validate_evidence_envelope(
        evidence_envelope_from_record(raw),
        boundary=datetime(2026, 7, 13, 4, 0, tzinfo=UTC),
    )
    assert validation["status"] == "invalid_receipt_order"
    assert validation["complete"] is False


def test_multiple_receipt_aliases_are_valid_when_every_stage_is_ordered():
    raw = {
        "bar_time": "2026-07-13T10:00:00+08:00",
        "event_time": "2026-07-13T02:00:00+00:00",
        "published_at": "2026-07-13T10:10:00+08:00",
        "available_at": "2026-07-13T02:20:00+00:00",
        "received_at": "2026-07-13T10:20:00+08:00",
        "ingested_at": "2026-07-13T02:25:00+00:00",
        "retrieved_as_of": "2026-07-13T10:25:00+08:00",
        "retrieved_at": "2026-07-13T02:30:00+00:00",
    }
    validation = validate_evidence_envelope(
        evidence_envelope_from_record(raw),
        boundary=datetime(2026, 7, 13, 4, 0, tzinfo=UTC),
    )
    assert validation["status"] == "valid"
    assert validation["complete"] is True


@pytest.mark.parametrize(
    ("receipts", "expected"),
    [
        (
            {
                "received_at": "2026-07-13T10:10:00+08:00",
                "retrieved_at": "2026-07-13T10:20:00+08:00",
            },
            ("02:10:00+00:00", "02:10:00+00:00", "02:20:00+00:00"),
        ),
        (
            {
                "available_at": "2026-07-13T10:10:00+08:00",
                "retrieved_at": "2026-07-13T10:20:00+08:00",
            },
            ("02:10:00+00:00", "02:10:00+00:00", "02:20:00+00:00"),
        ),
        (
            {
                "available_at": "2026-07-13T10:10:00+08:00",
                "ingested_at": "2026-07-13T10:20:00+08:00",
            },
            ("02:10:00+00:00", "02:20:00+00:00", "02:20:00+00:00"),
        ),
    ],
)
def test_missing_receipt_stage_uses_existing_conservative_derivation(
    receipts, expected
):
    validation = validate_evidence_envelope(
        evidence_envelope_from_record(
            {"bar_time": "2026-07-13T10:00:00+08:00", **receipts}
        ),
        boundary=datetime(2026, 7, 13, 4, 0, tzinfo=UTC),
    )
    assert validation["status"] == "valid"
    canonical = validation["canonical_timestamps"]
    assert canonical["available_at"].endswith(expected[0])
    assert canonical["ingested_at"].endswith(expected[1])
    assert canonical["retrieved_as_of"].endswith(expected[2])


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
        "event_time": "2026-07-13T01:30:00+00:00",
        "available_at": "2026-07-13T01:30:00+00:00",
        "ingested_at": "2026-07-13T01:30:00+00:00",
        "retrieved_as_of": "2026-07-13T01:30:00+00:00",
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
    prediction_at = str(row.get("prediction_at") or "")
    data_as_of = str(row.get("data_as_of") or prediction_at)
    if "decision_timestamp_lineage" not in overrides:
        row["decision_timestamp_lineage"] = {
            field: {
                "source_field": field,
                "raw_value": value,
                "normalized_value": value,
                "timezone_semantics": "ashare_decision_time",
                "normalization_rule": "convert_aware_instant_to_asia_shanghai",
                "valid": True,
            }
            for field, value in (
                ("prediction_at", prediction_at),
                ("data_as_of", data_as_of),
            )
        }
    quality = dict(row.get("data_quality") or {})
    if "reference_timestamp_lineage" not in quality:
        price_timestamp = quality.get("price_timestamp")
        quality["reference_timestamp_lineage"] = {
            "source_field": "bar_time",
            "raw_value": price_timestamp,
            "normalized_value": price_timestamp,
            "timezone_semantics": "ashare_exchange_event_time",
            "normalization_rule": "convert_aware_instant_to_asia_shanghai",
            "valid": price_timestamp not in (None, ""),
        }
    row["data_quality"] = quality
    if "point_in_time_lineage" not in overrides:
        row["point_in_time_lineage"] = {
            "timestamps": {field: row.get(field) for field in PIT_TIMESTAMP_FIELDS}
        }
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


def _point(
    timestamp: datetime,
    price: float,
    *,
    reliable: bool = True,
    source: str = "sharedsignals.5min",
    **overrides,
) -> dict:
    timestamp_iso = timestamp.isoformat()
    row = {
        "timestamp": timestamp,
        "event_time": timestamp_iso,
        "available_at": timestamp_iso,
        "ingested_at": timestamp_iso,
        "retrieved_as_of": timestamp_iso,
        "price": price,
        "reliable": reliable,
        "source": source,
    }
    row.update(overrides)
    if "point_in_time_lineage" not in overrides:
        row["point_in_time_lineage"] = {
            "timestamps": {field: row.get(field) for field in PIT_TIMESTAMP_FIELDS}
        }
    return row


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
    points = [_point(start + timedelta(minutes=30), 10.2)]

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
    points = [_point(start + timedelta(minutes=30), 10.2)]

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
    points = [_point(start + timedelta(minutes=30), 10.2)]

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
    points = [_point(start + timedelta(minutes=30), 10.2)]

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


def test_snapshot_compares_aware_reference_and_decision_as_the_same_instant():
    candidate = _candidate(
        prediction_at="2026-07-13T09:30:00+08:00",
        data_as_of="2026-07-13T09:30:00+08:00",
        data_quality={
            "reliable": True,
            "source": "sharedsignals.5min",
            "price_timestamp": "2026-07-13T01:30:00+00:00",
            "reference_timestamp_lineage": {
                "source_field": "bar_time",
                "raw_value": "2026-07-13T09:30:00+08:00",
                "normalized_value": "2026-07-13T09:30:00+08:00",
                "timezone_semantics": "ashare_exchange_event_time",
                "normalization_rule": "convert_aware_instant_to_asia_shanghai",
                "valid": True,
            },
        },
    )

    snapshot = build_prediction_snapshot(candidate)

    assert snapshot["forward_label_eligibility"] == "eligible"
    assert snapshot["reference_evidence_status"] == "verified_reference_data"


def test_snapshot_accepts_only_contract_annotated_naive_ashare_bar_time():
    candidate = _candidate(
        prediction_at="2026-07-13T09:31:00+08:00",
        data_as_of="2026-07-13T09:31:00+08:00",
        data_quality={
            "reliable": True,
            "source": "tushare_rt_min",
            "price_timestamp": "2026-07-13T09:30:00+08:00",
            "reference_timestamp_lineage": {
                "source_field": "bar_time",
                "raw_value": "2026-07-13 09:30:00",
                "normalized_value": "2026-07-13T09:30:00+08:00",
                "timezone_semantics": "ashare_exchange_event_time",
                "normalization_rule": "ashare_exchange_local_attach_asia_shanghai",
                "valid": True,
            },
        },
    )

    snapshot = build_prediction_snapshot(candidate)

    assert snapshot["forward_label_eligibility"] == "eligible"


@pytest.mark.parametrize(
    ("candidate", "reason"),
    [
        (
            _candidate(
                data_quality={
                    "reliable": True,
                    "source": "sharedsignals.5min",
                    "price_timestamp": "2026-07-13T01:30:00",
                }
            ),
            "reference_timestamp_timezone_mismatch",
        ),
        (
            _candidate(
                data_quality={
                    "reliable": True,
                    "source": "sharedsignals.5min",
                    "price_timestamp": "2026-07-13T01:30:00+00:00",
                    "reference_timestamp_lineage": {
                        "source_field": "bar_time",
                        "raw_value": "2026-07-13 09:31:00",
                        "normalized_value": "2026-07-13T09:30:00+08:00",
                        "timezone_semantics": "ashare_exchange_event_time",
                        "normalization_rule": (
                            "ashare_exchange_local_attach_asia_shanghai"
                        ),
                        "valid": True,
                    },
                }
            ),
            "reference_timestamp_lineage_conflict",
        ),
        (
            _candidate(data_as_of="2026-07-13T01:29:59+00:00"),
            "reference_price_after_data_as_of",
        ),
        (
            _candidate(data_as_of=""),
            "data_as_of_timestamp_timezone_mismatch",
        ),
        (
            _candidate(
                prediction_at="2026-07-13T09:30:00+08:00",
                data_as_of="2026-07-13T09:30:00+08:00",
                decision_timestamp_lineage={
                    "prediction_at": {
                        "source_field": "prediction_at",
                        "raw_value": "2026-07-13T09:31:00+08:00",
                        "normalized_value": "2026-07-13T09:31:00+08:00",
                        "timezone_semantics": "ashare_decision_time",
                        "normalization_rule": "convert_aware_instant_to_asia_shanghai",
                        "valid": True,
                    },
                    "data_as_of": {
                        "source_field": "data_as_of",
                        "raw_value": "2026-07-13T09:30:00+08:00",
                        "normalized_value": "2026-07-13T09:30:00+08:00",
                        "timezone_semantics": "ashare_decision_time",
                        "normalization_rule": "convert_aware_instant_to_asia_shanghai",
                        "valid": True,
                    },
                },
            ),
            "decision_timestamp_lineage_conflict",
        ),
    ],
)
def test_snapshot_fails_closed_for_naive_conflicting_or_post_cutoff_reference(
    candidate, reason
):
    snapshot = build_prediction_snapshot(candidate)

    assert snapshot["forward_label_eligibility"] == "rejected_data_quality"
    assert snapshot["forward_label_rejection_reason"] == reason


def test_missing_reference_price_remains_retryable_and_nonterminal():
    candidate = _candidate(reference_price=None)
    candidate["data_quality"] = {
        "reliable": False,
        "source": None,
        "price_timestamp": None,
    }
    snapshot = build_prediction_snapshot(candidate)

    assert snapshot["forward_label_eligibility"] == "pending_reference_evidence"
    assert snapshot["forward_label_pending_reason"] == "missing_reference_price"
    assert snapshot["forward_label_rejection_reason"] is None

    start = datetime(2026, 7, 13, 1, 30, tzinfo=UTC)
    result = materialize_forward_labels(
        snapshot,
        [],
        as_of=start + timedelta(minutes=30),
        horizon_targets=_targets(start),
    )
    assert result["labels"]["m30"]["status"] == "missing_exit_evidence"
    assert result["labels"]["m30"]["reason"] == "missing_reference_price"
    assert result["labels"]["m30"]["exit_price"] is None


@pytest.mark.parametrize(
    ("candidate", "eligibility", "reason"),
    [
        (
            _candidate(
                data_quality={
                    "reliable": True,
                    "source": "sharedsignals.5min",
                    "price_timestamp": "2026-07-13T01:30:00+00:00",
                    "reference_timestamp_lineage": None,
                }
            ),
            "pending_reference_evidence",
            "missing_reference_timestamp_lineage",
        ),
        (
            _candidate(decision_timestamp_lineage=None),
            "pending_reference_evidence",
            "missing_decision_timestamp_lineage",
        ),
        (
            _candidate(
                data_quality={
                    "reliable": True,
                    "source": "sharedsignals.5min",
                    "price_timestamp": "2026-07-13T01:30:00+00:00",
                    "reference_timestamp_lineage": {
                        "source_field": "bar_time",
                        "raw_value": "2026-07-13T09:30:00+08:00",
                        "normalized_value": "2026-07-13T09:30:00+08:00",
                        "timezone_semantics": "ashare_exchange_event_time",
                        "valid": True,
                    },
                }
            ),
            "pending_reference_evidence",
            "incomplete_reference_timestamp_lineage",
        ),
        (
            _candidate(
                data_quality={
                    "reliable": True,
                    "source": "sharedsignals.5min",
                    "price_timestamp": "2026-07-13T01:30:00+00:00",
                    "reference_timestamp_lineage": {
                        "source_field": "bar_time",
                        "raw_value": "2026-07-13T09:30:00+08:00",
                        "normalized_value": "2026-07-13T09:31:00+08:00",
                        "timezone_semantics": "ashare_exchange_event_time",
                        "normalization_rule": "convert_aware_instant_to_asia_shanghai",
                        "valid": True,
                    },
                }
            ),
            "rejected_data_quality",
            "reference_timestamp_lineage_conflict",
        ),
    ],
)
def test_strict_timestamp_lineage_missing_incomplete_or_conflicting_fails_closed(
    candidate, eligibility, reason
):
    snapshot = build_prediction_snapshot(candidate)

    assert snapshot["forward_label_eligibility"] == eligibility
    reason_field = (
        "forward_label_pending_reason"
        if eligibility == "pending_reference_evidence"
        else "forward_label_rejection_reason"
    )
    assert snapshot[reason_field] == reason
    assert snapshot["reference_evidence_status"] == reason


def test_horizon_alias_is_canonical_without_adding_a_second_label_bucket():
    assert CANONICAL_HORIZONS == ("m30", "m60", "close", "1d", "3d", "5d")
    assert canonical_horizon("next-day") == "1d"
    assert canonical_horizon("next_day") == "1d"
    assert canonical_horizon("1d") == "1d"


def test_as_of_prevents_lookahead_and_only_materializes_due_horizons():
    start = datetime(2026, 7, 13, 1, 30, tzinfo=UTC)
    snapshot = build_prediction_snapshot(_candidate())
    points = [
        _point(start + timedelta(minutes=30), 10.2),
        _point(start + timedelta(minutes=60), 10.4),
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


def test_exit_point_available_after_as_of_cannot_be_ready():
    snapshot = build_prediction_snapshot(_candidate())
    point = _point(
        datetime(2026, 7, 13, 10, 0, tzinfo=timezone(timedelta(hours=8))),
        12.0,
        available_at="2026-07-13T16:30:00+08:00",
        ingested_at="2026-07-13T16:31:00+08:00",
        retrieved_as_of="2026-07-13T16:00:00+08:00",
    )

    result = materialize_forward_labels(
        snapshot,
        [point],
        as_of="2026-07-13T16:00:00+08:00",
        horizon_targets=_targets(datetime(2026, 7, 13, 1, 30, tzinfo=UTC)),
    )

    label = result["labels"]["m30"]
    assert label["status"] == "missing_exit_evidence"
    assert label["reason"] == "point_in_time_lineage_invalid_timestamp_order"
    assert label["exit_price"] is None
    assert label["point_in_time_lineage"]["complete"] is False


def test_exit_point_ingestion_and_retrieval_order_conflict_stays_pending():
    start = datetime(2026, 7, 13, 1, 30, tzinfo=UTC)
    point = _point(
        start + timedelta(minutes=30),
        10.2,
        available_at="2026-07-13T02:00:01+00:00",
        ingested_at="2026-07-13T02:00:03+00:00",
        retrieved_as_of="2026-07-13T02:00:02+00:00",
    )

    label = materialize_forward_labels(
        build_prediction_snapshot(_candidate()),
        [point],
        as_of=start + timedelta(minutes=30),
        horizon_targets=_targets(start),
    )["labels"]["m30"]

    assert label["status"] == "missing_exit_evidence"
    assert label["reason"] == "point_in_time_lineage_invalid_timestamp_order"
    assert label["point_in_time_lineage"]["complete"] is False


@pytest.mark.parametrize(
    "nested_timestamps",
    [
        {
            "event_time": "2026-07-13T02:00:00+00:00",
            "available_at": "2026-07-13T02:00:01",
            "ingested_at": "2026-07-13T02:00:02+00:00",
            "retrieved_as_of": "2026-07-13T02:00:03+00:00",
        },
        {
            "event_time": "2026-07-13T02:00:00+00:00",
            "available_at": "2026-07-13T02:00:03+00:00",
            "ingested_at": "2026-07-13T02:00:02+00:00",
            "retrieved_as_of": "2026-07-13T02:00:04+00:00",
        },
    ],
)
def test_nested_invalid_or_naive_exit_lineage_overrides_valid_top_level(
    nested_timestamps,
):
    start = datetime(2026, 7, 13, 1, 30, tzinfo=UTC)
    point = _point(start + timedelta(minutes=30), 10.2)
    point["point_in_time_lineage"] = {"timestamps": nested_timestamps}

    label = materialize_forward_labels(
        build_prediction_snapshot(_candidate()),
        [point],
        as_of=start + timedelta(minutes=40),
        horizon_targets=_targets(start),
    )["labels"]["m30"]

    assert label["status"] == "missing_exit_evidence"
    assert label["reason"].startswith("point_in_time_lineage_")
    assert label["point_in_time_lineage"]["complete"] is False


def test_invalid_high_price_is_skipped_before_valid_low_price_selection():
    start = datetime(2026, 7, 13, 1, 30, tzinfo=UTC)
    invalid_high = _point(
        start + timedelta(minutes=30),
        99.0,
        available_at="2026-07-13T16:30:00+08:00",
        ingested_at="2026-07-13T16:31:00+08:00",
        retrieved_as_of="2026-07-13T16:00:00+08:00",
    )
    valid_low = _point(start + timedelta(minutes=31), 10.1)

    label = materialize_forward_labels(
        build_prediction_snapshot(_candidate()),
        [invalid_high, valid_low],
        as_of="2026-07-13T16:00:00+08:00",
        horizon_targets=_targets(start),
    )["labels"]["m30"]

    assert label["status"] == "ready"
    assert label["exit_price"] == 10.1
    lineage = label["point_in_time_lineage"]
    assert lineage["status"] == "valid"
    assert lineage["complete"] is True
    assert lineage["timestamps"] == {
        "event_time": valid_low["event_time"],
        "available_at": valid_low["available_at"],
        "ingested_at": valid_low["ingested_at"],
        "retrieved_as_of": valid_low["retrieved_as_of"],
    }
    assert lineage["canonical_event_time"] == valid_low["event_time"]
    assert lineage["evidence_envelope_validation"]["status"] == "valid"


def test_conflicting_nested_event_time_high_price_cannot_beat_valid_low_price():
    start = datetime(2026, 7, 13, 1, 30, tzinfo=UTC)
    invalid_high = _point(start + timedelta(minutes=30), 99.0)
    invalid_high["point_in_time_lineage"] = {
        "timestamps": {
            "event_time": "2026-07-13T02:30:00+00:00",
            "available_at": "2026-07-13T02:31:00+00:00",
            "ingested_at": "2026-07-13T02:32:00+00:00",
            "retrieved_as_of": "2026-07-13T02:33:00+00:00",
        }
    }
    valid_low = _point(start + timedelta(minutes=31), 10.1)

    label = materialize_forward_labels(
        build_prediction_snapshot(_candidate()),
        [invalid_high, valid_low],
        as_of="2026-07-13T03:00:00+00:00",
        horizon_targets=_targets(start),
    )["labels"]["m30"]

    assert label["status"] == "ready"
    assert label["exit_price"] == 10.1
    assert label["evidence_at"] == valid_low["event_time"]


def test_only_conflicting_point_stays_pending_without_fake_exit():
    start = datetime(2026, 7, 13, 1, 30, tzinfo=UTC)
    point = _point(start + timedelta(minutes=30), 99.0)
    point["point_in_time_lineage"] = {
        "timestamps": {
            "event_time": "2026-07-13T02:30:00+00:00",
            "available_at": "2026-07-13T02:31:00+00:00",
            "ingested_at": "2026-07-13T02:32:00+00:00",
            "retrieved_as_of": "2026-07-13T02:33:00+00:00",
        }
    }

    label = materialize_forward_labels(
        build_prediction_snapshot(_candidate()),
        [point],
        as_of="2026-07-13T03:00:00+00:00",
        horizon_targets=_targets(start),
    )["labels"]["m30"]

    assert label["status"] == "missing_exit_evidence"
    assert label["reason"] == "point_in_time_lineage_event_time_conflict"
    assert label["exit_price"] is None
    assert label["evidence_at"] is None
    assert label["point_in_time_lineage"]["complete"] is False


def test_equivalent_shanghai_and_utc_event_instants_are_accepted():
    start = datetime(2026, 7, 13, 1, 30, tzinfo=UTC)
    point = _point(
        datetime(2026, 7, 13, 10, 0, tzinfo=timezone(timedelta(hours=8))),
        10.2,
    )
    point.update(
        {
            "available_at": "2026-07-13T10:01:00+08:00",
            "ingested_at": "2026-07-13T10:02:00+08:00",
            "retrieved_as_of": "2026-07-13T10:03:00+08:00",
        }
    )
    point["point_in_time_lineage"] = {
        "timestamps": {
            "event_time": "2026-07-13T02:00:00+00:00",
            "available_at": "2026-07-13T02:01:00+00:00",
            "ingested_at": "2026-07-13T02:02:00+00:00",
            "retrieved_as_of": "2026-07-13T02:03:00+00:00",
        }
    }

    label = materialize_forward_labels(
        build_prediction_snapshot(_candidate()),
        [point],
        as_of="2026-07-13T03:00:00+00:00",
        horizon_targets=_targets(start),
    )["labels"]["m30"]

    assert label["status"] == "ready"
    assert label["evidence_at"] == "2026-07-13T02:00:00+00:00"
    assert label["point_in_time_lineage"]["canonical_event_time"] == (
        "2026-07-13T02:00:00+00:00"
    )


@pytest.mark.parametrize("missing", ["top", "nested"])
def test_missing_top_or_nested_event_clock_stays_pending(missing):
    start = datetime(2026, 7, 13, 1, 30, tzinfo=UTC)
    point = _point(start + timedelta(minutes=30), 10.2)
    if missing == "top":
        point.pop("timestamp")
        point.pop("event_time")
    else:
        point.pop("point_in_time_lineage")

    label = materialize_forward_labels(
        build_prediction_snapshot(_candidate()),
        [point],
        as_of="2026-07-13T03:00:00+00:00",
        horizon_targets=_targets(start),
    )["labels"]["m30"]

    assert label["status"] == "missing_exit_evidence"
    assert label["reason"].startswith("point_in_time_lineage_missing_")
    assert label["exit_price"] is None


def test_canonical_event_outside_horizon_window_stays_pending():
    start = datetime(2026, 7, 13, 1, 30, tzinfo=UTC)
    point = _point(start + timedelta(hours=1), 10.2)

    label = materialize_forward_labels(
        build_prediction_snapshot(_candidate()),
        [point],
        as_of="2026-07-13T03:00:00+00:00",
        horizon_targets=_targets(start),
    )["labels"]["m30"]

    assert label["status"] == "missing_exit_evidence"
    assert label["reason"] == "canonical_event_outside_horizon_window"
    assert label["exit_price"] is None


def test_reference_top_and_nested_event_time_conflict_rejects_ready_label():
    start = datetime(2026, 7, 13, 1, 30, tzinfo=UTC)
    candidate = _candidate()
    candidate["point_in_time_lineage"] = {
        "timestamps": {
            "event_time": "2026-07-13T01:29:00+00:00",
            "available_at": "2026-07-13T01:29:00+00:00",
            "ingested_at": "2026-07-13T01:29:00+00:00",
            "retrieved_as_of": "2026-07-13T01:29:00+00:00",
        }
    }

    label = materialize_forward_labels(
        build_prediction_snapshot(candidate),
        [_point(start + timedelta(minutes=30), 10.2)],
        as_of="2026-07-13T03:00:00+00:00",
        horizon_targets=_targets(start),
    )["labels"]["m30"]

    assert label["status"] == "rejected_data_quality"
    assert label["reason"] == "reference_point_in_time_lineage_event_time_conflict"
    assert label["exit_price"] is None


def test_reference_equivalent_shanghai_and_utc_event_instants_are_accepted():
    start = datetime(2026, 7, 13, 1, 30, tzinfo=UTC)
    candidate = _candidate(
        event_time="2026-07-13T09:30:00+08:00",
        available_at="2026-07-13T09:30:00+08:00",
        ingested_at="2026-07-13T09:30:00+08:00",
        retrieved_as_of="2026-07-13T09:30:00+08:00",
    )
    candidate["point_in_time_lineage"] = {
        "timestamps": {
            "event_time": "2026-07-13T01:30:00+00:00",
            "available_at": "2026-07-13T01:30:00+00:00",
            "ingested_at": "2026-07-13T01:30:00+00:00",
            "retrieved_as_of": "2026-07-13T01:30:00+00:00",
        }
    }

    label = materialize_forward_labels(
        build_prediction_snapshot(candidate),
        [_point(start + timedelta(minutes=30), 10.2)],
        as_of="2026-07-13T03:00:00+00:00",
        horizon_targets=_targets(start),
    )["labels"]["m30"]

    assert label["status"] == "ready"
    assert label["exit_price"] == 10.2


def test_materializer_recomputes_reference_pit_instead_of_trusting_cached_result():
    start = datetime(2026, 7, 13, 1, 30, tzinfo=UTC)
    snapshot = build_prediction_snapshot(_candidate())
    assert snapshot["point_in_time_lineage_validation"]["complete"] is True
    snapshot["point_in_time_lineage"]["timestamps"]["available_at"] = (
        "2026-07-13T02:30:00+00:00"
    )
    snapshot["point_in_time_lineage"]["timestamps"]["ingested_at"] = (
        "2026-07-13T02:31:00+00:00"
    )

    label = materialize_forward_labels(
        snapshot,
        [_point(start + timedelta(minutes=30), 10.2)],
        as_of=start + timedelta(minutes=30),
        horizon_targets=_targets(start),
    )["labels"]["m30"]

    assert label["status"] == "rejected_data_quality"
    assert label["reason"] == "reference_point_in_time_lineage_invalid_timestamp_order"
    assert label["point_in_time_lineage"]["complete"] is False


def test_net_return_deducts_round_trip_fees_and_slippage_for_long_and_short():
    start = datetime(2026, 7, 13, 1, 30, tzinfo=UTC)
    point = _point(start + timedelta(minutes=30), 10.2)
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
        [_point(start + timedelta(minutes=30), 10.2, reliable=False)],
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
    daily_close = _point(
        start + timedelta(hours=5, minutes=30),
        10.8,
        source="sharedsignals.daily",
        eligible_horizons=["close", "1d", "3d", "5d"],
    )

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
    late = _point(start + timedelta(minutes=75), 10.5)

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
    points = [_point(start + timedelta(minutes=30), 10.1)]
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
