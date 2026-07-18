from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from shared.review.outcome_evaluation import (
    OutcomeEvaluationError,
    build_outcome_evaluation as _build_outcome_evaluation,
    canonical_sha256,
    verify_outcome_evaluation,
)
from shared.review.forward_labels import validate_evidence_envelope

from tests._ashare_validation_plan_fixture import (
    build_non_production_ashare_validation_plan,
)


AUTHORITY = {
    "capital_authority_id": "ashare-capital-v1",
    "authority_generation": 2,
    "execution_lineage_id": "ashare-lineage-2",
}
VALIDATION_PLAN = build_non_production_ashare_validation_plan()
VALIDATION_PLAN_BINDING = {
    "validation_plan_sha256": VALIDATION_PLAN.sha256(),
    "trading_session_calendar_sha256": (
        VALIDATION_PLAN.trading_session_calendar.calendar_sha256
    ),
    "trading_session_calendar_verification_proof_sha256": (
        VALIDATION_PLAN.trading_session_calendar_verification.proof_sha256
    ),
}
TRUSTED_PLAN_PROVENANCE = {
    "validation_plan_sha256": VALIDATION_PLAN.sha256(),
    "artifact_sha256": "c" * 64,
    "authority_tier": "externally_verified_research",
    "production_eligible": True,
    "verification_receipt_sha256": "9" * 64,
}


class TrustedTestPlanProvenanceVerifier:
    verifier_id = "trusted-test-plan-provenance-verifier"
    verifier_version = "1.0.0"

    def verify(self, *, validation_plan, provenance):
        return {
            "accepted": True,
            "production_eligible": True,
            "verifier_id": self.verifier_id,
            "verifier_version": self.verifier_version,
            "proof_sha256": canonical_sha256(
                {
                    "plan": validation_plan.sha256(),
                    "provenance": provenance,
                }
            ),
            "validation_plan_sha256": provenance["validation_plan_sha256"],
            "artifact_sha256": provenance["artifact_sha256"],
            "verification_receipt_sha256": provenance["verification_receipt_sha256"],
        }


class TrustedTestMarketTruthVerifier:
    verifier_id = "trusted-test-market-truth-verifier"
    verifier_version = "1.0.0"

    def verify(
        self,
        *,
        snapshot_id,
        horizon,
        reference_evidence,
        exit_evidence,
        target_at,
        as_of,
    ):
        reference_sha = canonical_sha256(reference_evidence)
        exit_sha = canonical_sha256(exit_evidence)
        return {
            "accepted": True,
            "production_eligible": True,
            "verifier_id": self.verifier_id,
            "verifier_version": self.verifier_version,
            "proof_sha256": canonical_sha256(
                {
                    "snapshot_id": snapshot_id,
                    "horizon": horizon,
                    "reference": reference_sha,
                    "exit": exit_sha,
                    "target_at": target_at.isoformat(),
                    "as_of": as_of.isoformat(),
                }
            ),
            "reference_evidence_sha256": reference_sha,
            "exit_evidence_sha256": exit_sha,
        }


PLAN_PROVENANCE_VERIFIER = TrustedTestPlanProvenanceVerifier()
MARKET_TRUTH_VERIFIER = TrustedTestMarketTruthVerifier()


def build_outcome_evaluation(*args: object, **kwargs: object) -> dict[str, object]:
    kwargs.setdefault("validation_plan", VALIDATION_PLAN)
    kwargs.setdefault("validation_plan_provenance", TRUSTED_PLAN_PROVENANCE)
    kwargs.setdefault("validation_plan_provenance_verifier", PLAN_PROVENANCE_VERIFIER)
    kwargs.setdefault("market_truth_verifier", MARKET_TRUTH_VERIFIER)
    return _build_outcome_evaluation(*args, **kwargs)


def _label_point_in_time_lineage() -> dict[str, object]:
    envelope = {
        "event_time_fields": {
            "exit.event_time": "2026-07-17T15:00:00+08:00",
        },
        "availability_time_fields": {
            "exit.available_at": "2026-07-17T15:00:01+08:00",
        },
        "ingestion_time_fields": {
            "exit.ingested_at": "2026-07-17T15:00:02+08:00",
        },
        "retrieval_time_fields": {
            "exit.retrieved_as_of": "2026-07-17T15:00:03+08:00",
        },
        "structure_errors": [],
    }
    validation = validate_evidence_envelope(
        envelope,
        require_receipts=True,
    )
    return {
        "status": "valid",
        "complete": True,
        "timestamps": validation["canonical_timestamps"],
        "evidence_envelope_validation": validation,
    }


def prediction(
    *,
    snapshot_id: str = "prediction:one",
    symbol: str = "600000.SH",
    net_return: float | None = 0.02,
    source_class: str = "captured",
    arm: str = "mg_off",
) -> dict[str, object]:
    reference_payload = {
        "price": 10.0,
        "source": "sharedsignals.v1",
        "reliable": True,
        "event_time": "2026-07-16T09:59:00+08:00",
        "available_at": "2026-07-16T09:59:10+08:00",
        "ingested_at": "2026-07-16T09:59:20+08:00",
        "retrieved_as_of": "2026-07-16T09:59:30+08:00",
    }
    gross_return = net_return + 0.003 if net_return is not None else None
    exit_price = 10.0 * (1.0 + gross_return) if gross_return is not None else None
    exit_payload = (
        {
            "price": exit_price,
            "source": "sharedsignals.v1",
            "reliable": True,
            "event_time": "2026-07-17T15:00:00+08:00",
            "available_at": "2026-07-17T15:00:01+08:00",
            "ingested_at": "2026-07-17T15:00:02+08:00",
            "retrieved_as_of": "2026-07-17T15:00:03+08:00",
        }
        if net_return is not None
        else None
    )
    label = {
        "horizon": "1d",
        "status": "ready" if net_return is not None else "missing_exit_evidence",
        "reason": (
            "verified_exit_evidence"
            if net_return is not None
            else "no_exit_evidence_as_of"
        ),
        "target_at": "2026-07-17T15:00:00+08:00",
        "evidence_at": (
            "2026-07-17T15:00:00+08:00" if net_return is not None else None
        ),
        "evidence_source": "sharedsignals.v1" if net_return is not None else None,
        "exit_price": exit_price,
        "exit_evidence_payload": exit_payload,
        "exit_evidence_sha256": (
            canonical_sha256(exit_payload) if exit_payload is not None else None
        ),
        "market_return": gross_return,
        "gross_return_after_direction": gross_return,
        "fee_bps": 10.0 if net_return is not None else None,
        "slippage_bps": 20.0 if net_return is not None else None,
        "total_cost_bps": 30.0 if net_return is not None else None,
        "net_return_after_costs": net_return,
        "cost_model_version": "ashare-research-cost-v1",
        "cost_evidence_event_id": (
            "cost-evidence:ashare-research-v1" if net_return is not None else None
        ),
        "outcome": (
            "win"
            if net_return is not None and net_return > 0
            else "loss"
            if net_return is not None and net_return < 0
            else "flat"
            if net_return is not None
            else None
        ),
        "point_in_time_lineage": (
            _label_point_in_time_lineage() if net_return is not None else None
        ),
        **VALIDATION_PLAN_BINDING,
    }
    return {
        "journal_event_type": "prediction_snapshot",
        "record_type": "prediction",
        "snapshot_id": snapshot_id,
        "decision_cluster_id": "decision:one",
        "symbol": symbol,
        "market": "ashare",
        "style": "champion",
        "model_id": "frozen-champion",
        "model_version": "1",
        "prediction_at": "2026-07-16T10:00:00+08:00",
        "reference_price": 10.0,
        "reference_evidence_payload": reference_payload,
        "reference_evidence_sha256": canonical_sha256(reference_payload),
        "event_time": "2026-07-16T09:59:00+08:00",
        "available_at": "2026-07-16T09:59:10+08:00",
        "ingested_at": "2026-07-16T09:59:20+08:00",
        "retrieved_as_of": "2026-07-16T09:59:30+08:00",
        "trade_date": "20260716",
        "primary_label_horizon": "1d",
        "maturity_weight": 1.0,
        "sample_intent": "observation",
        "source_class": source_class,
        "pair_id": "pair:one",
        "base_snapshot_sha256": "a" * 64,
        "source_snapshot_sha256": "b" * 64,
        "point_in_time_as_of": "2026-07-16T09:59:30+08:00",
        "data_quality": {"reliable": True, "source": "sharedsignals.v1"},
        "costs": {
            "round_trip_fee_bps": 10.0,
            "round_trip_slippage_bps": 20.0,
            "cost_model_version": "ashare-research-cost-v1",
        },
        "marketgraph": {"ablation_group": arm},
        "labels_as_of": "2026-07-17T15:00:03+08:00",
        "labels": {"1d": label},
        "forward_label_authority_binding": VALIDATION_PLAN_BINDING,
        **AUTHORITY,
        "capital_layer": "simulated",
        "account_type": "simulated",
        "real_trading_enabled": False,
    }


def forward_label_update(
    prediction_event: dict[str, object],
    *,
    net_return: float = 0.02,
    authority: dict[str, object] | None = None,
) -> dict[str, object]:
    label = prediction(
        snapshot_id=str(prediction_event["snapshot_id"]),
        symbol=str(prediction_event["symbol"]),
        net_return=net_return,
        arm=str(
            prediction_event.get("marketgraph", {}).get("ablation_group", "mg_off")
        ),
    )["labels"]["1d"]
    return {
        "journal_event_type": "forward_label_update",
        "snapshot_id": prediction_event["snapshot_id"],
        "market": prediction_event["market"],
        "symbol": prediction_event["symbol"],
        "decision_cluster_id": prediction_event["decision_cluster_id"],
        "primary_label_horizon": prediction_event["primary_label_horizon"],
        "source_snapshot_sha256": prediction_event["source_snapshot_sha256"],
        "base_snapshot_sha256": prediction_event["base_snapshot_sha256"],
        "pair_id": prediction_event["pair_id"],
        "labels_as_of": "2026-07-17T15:00:03+08:00",
        "labels": {"1d": label},
        "forward_label_authority_binding": VALIDATION_PLAN_BINDING,
        **(authority if authority is not None else AUTHORITY),
    }


def ready_events(
    *,
    prediction_event: dict[str, object] | None = None,
    net_return: float = 0.02,
) -> list[dict[str, object]]:
    base = prediction_event or prediction(net_return=None)
    return [base, forward_label_update(base, net_return=net_return)]


def decision(*, disposition: str = "paper_filled") -> dict[str, object]:
    return {
        "record_type": "chain_validation",
        "audit_event_type": "decision_exposure_disposition",
        **AUTHORITY,
        "decision_exposure": {
            "decision_id": "decision-id-1",
            "decision_cluster_id": "decision:one",
            "decision_time": "2026-07-16T10:00:00+08:00",
            "symbol": "600000.SH",
            "model_id": "frozen-champion",
            "model_version": "1",
            "action": "buy",
            "disposition": disposition,
            "rejection_reason": None,
            "nonfill_reason": None,
        },
    }


def test_builds_deterministic_research_only_outcome_and_joins_decision() -> None:
    events = [*ready_events(), decision()]
    first = build_outcome_evaluation(
        events,
        as_of="2026-07-18T00:00:00+08:00",
        authority_scope=AUTHORITY,
    )
    second = build_outcome_evaluation(
        deepcopy(events),
        as_of="2026-07-18T00:00:00+08:00",
        authority_scope=AUTHORITY,
    )

    assert first == second
    assert (
        verify_outcome_evaluation(
            first,
            events=events,
            expected_as_of="2026-07-18T00:00:00+08:00",
            expected_authority_scope=AUTHORITY,
            validation_plan=VALIDATION_PLAN,
            validation_plan_provenance=TRUSTED_PLAN_PROVENANCE,
            validation_plan_provenance_verifier=PLAN_PROVENANCE_VERIFIER,
            market_truth_verifier=MARKET_TRUTH_VERIFIER,
        )
        is True
    )
    row = first["outcomes"][0]
    assert row["decision_id"] == "decision-id-1"
    assert row["disposition"] == "paper_filled"
    assert row["label"]["net_return_after_costs"] == pytest.approx(0.02)
    assert row["path_outcome"] == {
        "status": "unavailable_without_verified_path",
        "mae_return": None,
        "mfe_return": None,
    }
    assert row["eligible_for_promotion"] is False
    assert first["authority"] == {
        "research_only": True,
        "capital_authority": False,
        "position_authority": False,
        "order_authority": False,
        "automatic_promotion_enabled": False,
        "automatic_risk_expansion_enabled": False,
        "live_transition_authorized": False,
        "real_trading_enabled": False,
    }


def test_fixture_missing_label_duplicate_and_wrong_authority_fail_closed() -> None:
    fixture = build_outcome_evaluation(
        [prediction(net_return=None, source_class="fixture")],
        as_of="2026-07-18T00:00:00+08:00",
        authority_scope=AUTHORITY,
    )["outcomes"][0]
    assert fixture["eligible_for_statistical_learning"] is False
    assert {
        "fixture_source_excluded",
        "primary_label_not_ready",
        "forward_label_update_required",
        "market_truth_authority_not_verified",
    }.issubset(set(fixture["exclusion_reasons"]))

    wrong = prediction()
    wrong["authority_generation"] = 3
    report = build_outcome_evaluation(
        [wrong],
        as_of="2026-07-18T00:00:00+08:00",
        authority_scope=AUTHORITY,
    )
    assert report["outcomes"] == []
    assert report["excluded_authority_event_count"] == 1

    with pytest.raises(OutcomeEvaluationError, match="duplicate_prediction_identity"):
        build_outcome_evaluation(
            [prediction(), prediction()],
            as_of="2026-07-18T00:00:00+08:00",
            authority_scope=AUTHORITY,
        )


def test_ambiguous_decision_evidence_is_not_selected_post_hoc() -> None:
    events = [*ready_events(), decision(), decision(disposition="rejected")]
    report = build_outcome_evaluation(
        events,
        as_of="2026-07-18T00:00:00+08:00",
        authority_scope=AUTHORITY,
    )
    row = report["outcomes"][0]
    assert row["decision_id"] is None
    assert row["disposition"] == "observation_only"
    assert "ambiguous_decision_evidence" in row["exclusion_reasons"]
    assert row["eligible_for_statistical_learning"] is False


def test_forward_label_update_is_projected_and_future_evidence_is_excluded() -> None:
    base = prediction(net_return=None)
    update = forward_label_update(base)
    projected = build_outcome_evaluation(
        [base, update],
        as_of="2026-07-18T00:00:00+08:00",
        authority_scope=AUTHORITY,
    )["outcomes"][0]
    assert projected["label"]["status"] == "ready"
    assert projected["eligible_for_statistical_learning"] is True

    future = prediction(net_return=None)
    future_update = forward_label_update(future)
    future_update["labels"]["1d"]["evidence_at"] = "2026-07-19T15:00:01+08:00"
    excluded = build_outcome_evaluation(
        [future, future_update],
        as_of="2026-07-18T00:00:00+08:00",
        authority_scope=AUTHORITY,
    )["outcomes"][0]
    assert "label_evidence_after_as_of" in excluded["exclusion_reasons"]
    assert excluded["eligible_for_statistical_learning"] is False

    late_update = deepcopy(update)
    late_update["labels_as_of"] = "2026-07-19T15:00:01+08:00"
    late_projection = build_outcome_evaluation(
        [base, late_update],
        as_of="2026-07-18T00:00:00+08:00",
        authority_scope=AUTHORITY,
    )["outcomes"][0]
    assert "label_projection_after_as_of" in late_projection["exclusion_reasons"]
    assert late_projection["eligible_for_statistical_learning"] is False


@pytest.mark.parametrize(
    "authority_override",
    [
        {},
        {
            "capital_authority_id": "ashare-capital-v1",
            "authority_generation": 999,
            "execution_lineage_id": "ashare-lineage-wrong",
        },
    ],
)
def test_forward_label_update_requires_exact_prediction_authority_binding(
    authority_override: dict[str, object],
) -> None:
    base = prediction(net_return=None)
    update = {
        "journal_event_type": "forward_label_update",
        "snapshot_id": "prediction:one",
        "labels_as_of": "2026-07-17T15:00:01+08:00",
        "labels": {"1d": prediction(net_return=0.02)["labels"]["1d"]},
        **authority_override,
    }

    row = build_outcome_evaluation(
        [base, update],
        as_of="2026-07-18T00:00:00+08:00",
        authority_scope=AUTHORITY,
    )["outcomes"][0]

    assert (
        "forward_label_update_authority_or_identity_invalid" in row["exclusion_reasons"]
    )
    assert row["eligible_for_statistical_learning"] is False


def test_ready_label_without_canonical_exit_and_pit_evidence_is_observation_only() -> (
    None
):
    stripped = prediction(net_return=None)
    update = forward_label_update(stripped)
    update["labels"]["1d"].pop("evidence_source")
    update["labels"]["1d"].pop("exit_price")
    update["labels"]["1d"].pop("point_in_time_lineage")

    row = build_outcome_evaluation(
        [stripped, update],
        as_of="2026-07-18T00:00:00+08:00",
        authority_scope=AUTHORITY,
    )["outcomes"][0]

    assert "label_exit_evidence_incomplete" in row["exclusion_reasons"]
    assert "label_point_in_time_lineage_not_verified" in row["exclusion_reasons"]
    assert row["eligible_for_statistical_learning"] is False


def test_label_target_must_match_frozen_trading_session_calendar() -> None:
    mismatched = prediction(net_return=None)
    update = forward_label_update(mismatched)
    update["labels"]["1d"]["target_at"] = "2026-07-18T15:00:00+08:00"

    row = build_outcome_evaluation(
        [mismatched, update],
        as_of="2026-07-19T00:00:00+08:00",
        authority_scope=AUTHORITY,
    )["outcomes"][0]

    assert "label_target_calendar_authority_mismatch" in row["exclusion_reasons"]
    assert row["eligible_for_statistical_learning"] is False


def test_verifier_rejects_rehashed_authority_escalation() -> None:
    events = ready_events()
    report = build_outcome_evaluation(
        events,
        as_of="2026-07-18T00:00:00+08:00",
        authority_scope=AUTHORITY,
    )
    report["authority"]["automatic_promotion_enabled"] = True
    with pytest.raises(OutcomeEvaluationError, match="authority_invalid"):
        verify_outcome_evaluation(
            report,
            events=events,
            expected_as_of="2026-07-18T00:00:00+08:00",
            expected_authority_scope=AUTHORITY,
            validation_plan=VALIDATION_PLAN,
            validation_plan_provenance=TRUSTED_PLAN_PROVENANCE,
            validation_plan_provenance_verifier=PLAN_PROVENANCE_VERIFIER,
            market_truth_verifier=MARKET_TRUTH_VERIFIER,
        )


def test_future_or_incomplete_point_in_time_evidence_is_observation_only() -> None:
    future = prediction()
    future["point_in_time_as_of"] = "2026-07-16T10:00:01+08:00"
    future_row = build_outcome_evaluation(
        [future],
        as_of="2026-07-18T00:00:00+08:00",
        authority_scope=AUTHORITY,
    )["outcomes"][0]
    assert "point_in_time_as_of_after_prediction" in future_row["exclusion_reasons"]
    assert future_row["eligible_for_statistical_learning"] is False

    incomplete = prediction()
    incomplete.pop("available_at")
    incomplete_row = build_outcome_evaluation(
        [incomplete],
        as_of="2026-07-18T00:00:00+08:00",
        authority_scope=AUTHORITY,
    )["outcomes"][0]
    assert "point_in_time_lineage_not_verified" in incomplete_row["exclusion_reasons"]
    assert incomplete_row["eligible_for_statistical_learning"] is False


def test_fixture_source_and_unverified_label_cost_math_cannot_enter_learning() -> None:
    fixture = prediction()
    fixture["data_quality"]["source"] = "sharedsignals.mock-fixture"
    fixture_row = build_outcome_evaluation(
        [fixture],
        as_of="2026-07-18T00:00:00+08:00",
        authority_scope=AUTHORITY,
    )["outcomes"][0]
    assert "fixture_source_excluded" in fixture_row["exclusion_reasons"]
    assert fixture_row["eligible_for_statistical_learning"] is False

    mismatched = prediction(net_return=None)
    mismatched_update = forward_label_update(mismatched)
    mismatched_update["labels"]["1d"]["net_return_after_costs"] = 0.50
    mismatch_row = build_outcome_evaluation(
        [mismatched, mismatched_update],
        as_of="2026-07-18T00:00:00+08:00",
        authority_scope=AUTHORITY,
    )["outcomes"][0]
    assert "label_cost_arithmetic_mismatch" in mismatch_row["exclusion_reasons"]
    assert mismatch_row["eligible_for_statistical_learning"] is False


def test_decision_must_match_prediction_identity_and_valid_time_window() -> None:
    mismatched_decision = decision()
    mismatched_decision["decision_exposure"]["model_version"] = "2"
    row = build_outcome_evaluation(
        [prediction(), mismatched_decision],
        as_of="2026-07-18T00:00:00+08:00",
        authority_scope=AUTHORITY,
    )["outcomes"][0]
    assert "decision_prediction_identity_mismatch" in row["exclusion_reasons"]

    early_decision = decision()
    early_decision["decision_exposure"]["decision_time"] = "2026-07-16T09:59:59+08:00"
    early_row = build_outcome_evaluation(
        [prediction(), early_decision],
        as_of="2026-07-18T00:00:00+08:00",
        authority_scope=AUTHORITY,
    )["outcomes"][0]
    assert "decision_time_outside_valid_window" in early_row["exclusion_reasons"]


def test_inline_ready_label_never_replaces_authority_bound_update() -> None:
    row = build_outcome_evaluation(
        [prediction()],
        as_of="2026-07-18T00:00:00+08:00",
        authority_scope=AUTHORITY,
    )["outcomes"][0]

    assert row["label"]["status"] == "missing"
    assert "forward_label_update_required" in row["exclusion_reasons"]
    assert row["eligible_for_statistical_learning"] is False


def test_reference_and_exit_payloads_are_rehashed_and_prices_recomputed() -> None:
    base = prediction(net_return=None)
    update = forward_label_update(base)
    update["labels"]["1d"]["exit_evidence_payload"]["price"] = 999.0
    update["labels"]["1d"]["exit_evidence_sha256"] = canonical_sha256(
        update["labels"]["1d"]["exit_evidence_payload"]
    )
    exit_row = build_outcome_evaluation(
        [base, update],
        as_of="2026-07-18T00:00:00+08:00",
        authority_scope=AUTHORITY,
    )["outcomes"][0]
    assert "exit_evidence_payload_not_verified" in exit_row["exclusion_reasons"]

    base = prediction(net_return=None)
    update = forward_label_update(base)
    update["labels"]["1d"]["market_return"] = 0.99
    update["labels"]["1d"]["gross_return_after_direction"] = 0.99
    update["labels"]["1d"]["net_return_after_costs"] = 0.987
    update["labels"]["1d"]["outcome"] = "win"
    return_row = build_outcome_evaluation(
        [base, update],
        as_of="2026-07-18T00:00:00+08:00",
        authority_scope=AUTHORITY,
    )["outcomes"][0]
    assert "label_market_return_price_mismatch" in return_row["exclusion_reasons"]


def test_session_exit_evidence_must_be_exactly_at_prespecified_target() -> None:
    base = prediction(net_return=None)
    update = forward_label_update(base)
    update["labels"]["1d"]["evidence_at"] = "2026-07-17T15:00:01+08:00"
    row = build_outcome_evaluation(
        [base, update],
        as_of="2026-07-18T00:00:00+08:00",
        authority_scope=AUTHORITY,
    )["outcomes"][0]
    assert "label_session_evidence_time_mismatch" in row["exclusion_reasons"]


def test_plan_must_be_frozen_before_prediction_and_provenance_cannot_self_assert() -> (
    None
):
    late_frozen_at = datetime(2026, 7, 17, tzinfo=timezone.utc)
    late_proof = replace(
        VALIDATION_PLAN.trading_session_calendar_verification,
        verified_at=late_frozen_at - timedelta(minutes=1),
        frozen_at=late_frozen_at,
    )
    late_plan = replace(
        VALIDATION_PLAN,
        frozen_at=late_frozen_at,
        trading_session_calendar_verification=late_proof,
    )
    late_row = _build_outcome_evaluation(
        ready_events(),
        as_of="2026-07-18T00:00:00+08:00",
        authority_scope=AUTHORITY,
        validation_plan=late_plan,
        validation_plan_provenance=None,
        market_truth_verifier=MARKET_TRUTH_VERIFIER,
    )["outcomes"][0]
    assert "validation_plan_frozen_after_prediction" in late_row["exclusion_reasons"]

    self_asserted = _build_outcome_evaluation(
        ready_events(),
        as_of="2026-07-18T00:00:00+08:00",
        authority_scope=AUTHORITY,
        validation_plan=VALIDATION_PLAN,
        validation_plan_provenance=TRUSTED_PLAN_PROVENANCE,
        market_truth_verifier=MARKET_TRUTH_VERIFIER,
    )["outcomes"][0]
    assert (
        "validation_plan_provenance_not_verified" in self_asserted["exclusion_reasons"]
    )


def test_trade_date_is_derived_from_shanghai_time_and_frozen_calendar() -> None:
    wrong = prediction(net_return=None)
    wrong["trade_date"] = "20260717"
    wrong_row = build_outcome_evaluation(
        ready_events(prediction_event=wrong),
        as_of="2026-07-18T00:00:00+08:00",
        authority_scope=AUTHORITY,
    )["outcomes"][0]
    assert "trade_date_prediction_time_mismatch" in wrong_row["exclusion_reasons"]
    assert wrong_row["trade_date"] is None

    weekend = prediction(net_return=None)
    weekend["prediction_at"] = "2026-07-18T10:00:00+08:00"
    weekend["trade_date"] = "20260718"
    weekend_row = build_outcome_evaluation(
        ready_events(prediction_event=weekend),
        as_of="2026-07-20T00:00:00+08:00",
        authority_scope=AUTHORITY,
    )["outcomes"][0]
    assert "trade_date_calendar_authority_mismatch" in weekend_row["exclusion_reasons"]


def test_market_truth_has_no_default_authority() -> None:
    row = _build_outcome_evaluation(
        ready_events(),
        as_of="2026-07-18T00:00:00+08:00",
        authority_scope=AUTHORITY,
        validation_plan=VALIDATION_PLAN,
        validation_plan_provenance=TRUSTED_PLAN_PROVENANCE,
        validation_plan_provenance_verifier=PLAN_PROVENANCE_VERIFIER,
        market_truth_verifier=None,
    )["outcomes"][0]
    assert "market_truth_authority_not_verified" in row["exclusion_reasons"]
    assert row["eligible_for_statistical_learning"] is False


def test_exact_verifier_requires_independent_as_of_and_authority_scope() -> None:
    events = ready_events()
    report = build_outcome_evaluation(
        events,
        as_of="2026-07-18T00:00:00+08:00",
        authority_scope=AUTHORITY,
    )

    with pytest.raises(
        OutcomeEvaluationError,
        match="outcome_expected_as_of_mismatch",
    ):
        verify_outcome_evaluation(
            report,
            events=events,
            expected_as_of="2026-07-17T23:59:59+08:00",
            expected_authority_scope=AUTHORITY,
            validation_plan=VALIDATION_PLAN,
            validation_plan_provenance=TRUSTED_PLAN_PROVENANCE,
            validation_plan_provenance_verifier=PLAN_PROVENANCE_VERIFIER,
            market_truth_verifier=MARKET_TRUTH_VERIFIER,
        )

    wrong_scope = {**AUTHORITY, "authority_generation": 3}
    with pytest.raises(
        OutcomeEvaluationError,
        match="outcome_expected_authority_scope_mismatch",
    ):
        verify_outcome_evaluation(
            report,
            events=events,
            expected_as_of="2026-07-18T00:00:00+08:00",
            expected_authority_scope=wrong_scope,
            validation_plan=VALIDATION_PLAN,
            validation_plan_provenance=TRUSTED_PLAN_PROVENANCE,
            validation_plan_provenance_verifier=PLAN_PROVENANCE_VERIFIER,
            market_truth_verifier=MARKET_TRUTH_VERIFIER,
        )
