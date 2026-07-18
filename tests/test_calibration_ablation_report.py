from __future__ import annotations

from copy import deepcopy

import pytest

from shared.review.calibration_ablation import (
    build_calibration_ablation_report,
    verify_calibration_ablation_report,
)
from shared.review.outcome_evaluation import canonical_sha256

from tests.test_outcome_evaluation import (
    AUTHORITY,
    MARKET_TRUTH_VERIFIER,
    PLAN_PROVENANCE_VERIFIER,
    TRUSTED_PLAN_PROVENANCE,
    VALIDATION_PLAN,
    build_outcome_evaluation,
    forward_label_update,
    prediction,
)


def _pair(arm: str) -> dict[str, object]:
    row = prediction(
        snapshot_id=f"prediction:{arm}",
        net_return=None,
        arm=arm,
    )
    row["pair_id"] = "pair:one"
    row["decision_cluster_id"] = "decision:one"
    return row


def _pair_events(arm: str, net_return: float) -> list[dict[str, object]]:
    base = _pair(arm)
    return [base, forward_label_update(base, net_return=net_return)]


def _build_report(events, outcomes):
    return build_calibration_ablation_report(
        events=events,
        outcome_report=outcomes,
        expected_as_of=outcomes["as_of"],
        expected_authority_scope=AUTHORITY,
        validation_plan=VALIDATION_PLAN,
        validation_plan_provenance=TRUSTED_PLAN_PROVENANCE,
        validation_plan_provenance_verifier=PLAN_PROVENANCE_VERIFIER,
        market_truth_verifier=MARKET_TRUTH_VERIFIER,
    )


def _verify_report(report, events, outcomes):
    return verify_calibration_ablation_report(
        report,
        events=events,
        outcome_report=outcomes,
        expected_as_of=outcomes["as_of"],
        expected_authority_scope=AUTHORITY,
        validation_plan=VALIDATION_PLAN,
        validation_plan_provenance=TRUSTED_PLAN_PROVENANCE,
        validation_plan_provenance_verifier=PLAN_PROVENANCE_VERIFIER,
        market_truth_verifier=MARKET_TRUTH_VERIFIER,
    )


def test_paired_ablation_requires_shared_invariants_and_keeps_calibration_honest() -> (
    None
):
    events = [*_pair_events("mg_off", 0.01), *_pair_events("mg_on", 0.01)]
    outcomes = build_outcome_evaluation(
        events,
        as_of="2026-07-18T00:00:00+08:00",
        authority_scope=AUTHORITY,
    )
    report = _build_report(events, outcomes)

    assert _verify_report(report, events, outcomes) is True
    assert report["ablation"]["eligible_pair_count"] == 1
    pair = report["ablation"]["pairs"][0]
    assert pair["shared_actual_net_return_after_costs"] == 0.01
    assert pair["causal_increment_estimate_available"] is False
    assert "net_return_delta_mg_on_minus_off" not in pair
    assert "mean_net_return_delta_mg_on_minus_off" not in report["ablation"]
    assert report["ablation"]["incremental_effect_status"] == (
        "unavailable_shared_realized_outcome_is_not_a_causal_counterfactual"
    )
    assert report["calibration"]["status"] == "unavailable_no_calibrated_predictions"
    assert report["authority"]["ranking_effect"] == "none"


def test_unpaired_or_cost_mismatched_rows_are_excluded_not_selected() -> None:
    off = _pair_events("mg_off", 0.01)
    on = _pair_events("mg_on", 0.03)
    on[1]["labels"]["1d"]["cost_model_version"] = "different-cost-model"
    events = [*off, *on]
    outcomes = build_outcome_evaluation(
        events,
        as_of="2026-07-18T00:00:00+08:00",
        authority_scope=AUTHORITY,
    )
    report = _build_report(events, outcomes)
    assert report["ablation"]["eligible_pair_count"] == 0
    assert report["ablation"]["exclusion_reason_counts"] == {
        "paired_actual_outcome_mismatch": 1
    }


def test_different_realized_labels_cannot_be_subtracted_as_marketgraph_increment() -> (
    None
):
    events = [*_pair_events("mg_off", 0.01), *_pair_events("mg_on", 0.99)]
    outcomes = build_outcome_evaluation(
        events,
        as_of="2026-07-18T00:00:00+08:00",
        authority_scope=AUTHORITY,
    )
    report = _build_report(events, outcomes)

    assert report["ablation"]["eligible_pair_count"] == 0
    assert report["ablation"]["exclusion_reason_counts"] == {
        "paired_actual_outcome_mismatch": 1
    }


def test_foreign_update_cannot_change_calibration_cohort() -> None:
    base = prediction(net_return=None)
    base["calibration_role"] = "primary"
    base["calibrated_probability"] = 0.7
    base["probability_model_state"] = "frozen_out_of_sample_calibrated"
    valid = forward_label_update(base, net_return=0.02)
    baseline_events = [base, valid]
    baseline_outcomes = build_outcome_evaluation(
        baseline_events,
        as_of="2026-07-18T00:00:00+08:00",
        authority_scope=AUTHORITY,
    )
    baseline = _build_report(baseline_events, baseline_outcomes)

    foreign = forward_label_update(
        base,
        net_return=-0.99,
        authority={
            "capital_authority_id": "foreign",
            "authority_generation": 999,
            "execution_lineage_id": "foreign",
        },
    )
    attacked_events = [base, valid, foreign]
    attacked_outcomes = build_outcome_evaluation(
        attacked_events,
        as_of="2026-07-18T00:00:00+08:00",
        authority_scope=AUTHORITY,
    )
    attacked = _build_report(attacked_events, attacked_outcomes)

    assert attacked["calibration"] == baseline["calibration"]
    assert attacked["calibration_cohort"] == baseline["calibration_cohort"]


def test_rehashed_calibration_tamper_is_rejected_against_exact_sources() -> None:
    events = [*_pair_events("mg_off", 0.01), *_pair_events("mg_on", 0.01)]
    outcomes = build_outcome_evaluation(
        events,
        as_of="2026-07-18T00:00:00+08:00",
        authority_scope=AUTHORITY,
    )
    report = _build_report(events, outcomes)
    tampered = deepcopy(report)
    tampered["calibration"]["status"] = "forged"
    unsigned = deepcopy(tampered)
    unsigned.pop("report_sha256")
    tampered["report_sha256"] = canonical_sha256(unsigned)

    with pytest.raises(
        ValueError,
        match="calibration_ablation_does_not_match_exact_sources",
    ):
        _verify_report(tampered, events, outcomes)
