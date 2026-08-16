"""Contract tests for the automatic event-catalyst promotion gate."""

from __future__ import annotations

from datetime import datetime

import pytest

from Ashare.event_catalyst_promotion import (
    EVENT_CATALYST_PROMOTION_CONTRACT,
    EventCatalystPromotionError,
    LabeledObservation,
    PromotionDecision,
    PromotionPolicy,
    evaluate_promotion,
)


AS_OF = datetime.fromisoformat("2026-08-15T18:00:00+08:00")


def _policy(**overrides) -> PromotionPolicy:
    payload = {
        "policy_id": "event-catalyst-promotion-v1",
        "min_labeled_observations": 8,
        "min_distinct_event_clusters": 3,
        "min_time_windows": 2,
        "min_window_hit_rate": 0.6,
        "cost_per_round_trip": 0.002,
        "demote_recent_labels": 4,
        "demote_max_hit_rate": 0.25,
    }
    payload.update(overrides)
    return PromotionPolicy(**payload)


def _label(hypothesis, cluster, window, signed):
    return LabeledObservation(
        hypothesis=hypothesis,
        event_cluster=cluster,
        window_id=window,
        signed_post_return=signed,
    )


def _winning_labels(hypothesis, *, n=8, sign=1.0):
    """n labels across 4 clusters and 2 windows, all hypothesis-correct."""
    labels = []
    for index in range(n):
        cluster = f"evt-{index % 4}"
        window = "w1" if index % 2 == 0 else "w2"
        labels.append(_label(hypothesis, cluster, window, 0.03 * sign))
    return labels


class TestPolicyContract:
    def test_policy_hash_stable(self):
        assert _policy().policy_sha256 == _policy().policy_sha256

    def test_policy_hash_changes_with_threshold(self):
        assert (
            _policy().policy_sha256
            != _policy(min_window_hit_rate=0.7).policy_sha256
        )

    def test_single_window_policy_rejected(self):
        with pytest.raises(EventCatalystPromotionError) as excinfo:
            _policy(min_time_windows=1)
        assert (
            excinfo.value.reason_code
            == "event_catalyst_policy_windows_invalid"
        )


class TestGraduation:
    def test_winning_hypothesis_graduates(self):
        decision = evaluate_promotion(
            _winning_labels("realize_on_event", sign=-1.0),
            policy=_policy(),
            as_of=AS_OF,
        )
        assert isinstance(decision, PromotionDecision)
        assert decision.contract == EVENT_CATALYST_PROMOTION_CONTRACT
        verdict = next(
            v for v in decision.verdicts if v.hypothesis == "realize_on_event"
        )
        assert verdict.decision == "graduate_to_validated_factor"
        assert verdict.reason_code == "policy_gates_all_passed"
        assert verdict.expectancy_after_cost == pytest.approx(0.028)

    def test_empty_history_keeps_shadow(self):
        decision = evaluate_promotion([], policy=_policy(), as_of=AS_OF)
        for verdict in decision.verdicts:
            assert verdict.decision == "keep_shadow"
            assert verdict.reason_code == "no_labeled_observations"

    def test_insufficient_clusters_keep_shadow(self):
        labels = _winning_labels("hold_through_event")
        labels = [
            LabeledObservation(
                hypothesis=label.hypothesis,
                event_cluster="single-cluster",
                window_id=label.window_id,
                signed_post_return=label.signed_post_return,
            )
            for label in labels
        ]
        decision = evaluate_promotion(labels, policy=_policy(), as_of=AS_OF)
        verdict = next(
            v for v in decision.verdicts if v.hypothesis == "hold_through_event"
        )
        assert verdict.decision == "keep_shadow"
        assert verdict.reason_code == "insufficient_event_clusters"

    def test_single_window_keeps_shadow(self):
        labels = [
            LabeledObservation(
                hypothesis=label.hypothesis,
                event_cluster=label.event_cluster,
                window_id="only-window",
                signed_post_return=label.signed_post_return,
            )
            for label in _winning_labels("hold_through_event")
        ]
        decision = evaluate_promotion(labels, policy=_policy(), as_of=AS_OF)
        verdict = next(
            v for v in decision.verdicts if v.hypothesis == "hold_through_event"
        )
        assert verdict.reason_code == "insufficient_time_windows"

    def test_cost_kills_marginal_expectancy(self):
        labels = _winning_labels("hold_through_event")
        labels = [
            LabeledObservation(
                hypothesis=label.hypothesis,
                event_cluster=label.event_cluster,
                window_id=label.window_id,
                signed_post_return=0.001,
            )
            for label in labels
        ]
        decision = evaluate_promotion(labels, policy=_policy(), as_of=AS_OF)
        verdict = next(
            v for v in decision.verdicts if v.hypothesis == "hold_through_event"
        )
        assert verdict.decision == "keep_shadow"
        assert verdict.reason_code == "expectancy_not_positive_after_cost"

    def test_inconsistent_window_keeps_shadow(self):
        labels = _winning_labels("hold_through_event")
        # Flip all window-2 labels against the hypothesis.
        labels = [
            LabeledObservation(
                hypothesis=label.hypothesis,
                event_cluster=label.event_cluster,
                window_id=label.window_id,
                signed_post_return=(
                    -0.005 if label.window_id == "w2" else 0.04
                ),
            )
            for label in labels
        ]
        decision = evaluate_promotion(labels, policy=_policy(), as_of=AS_OF)
        verdict = next(
            v for v in decision.verdicts if v.hypothesis == "hold_through_event"
        )
        assert verdict.decision == "keep_shadow"
        assert verdict.reason_code == "window_consistency_failed"


class TestAutoDemotion:
    def test_recent_contradiction_demotes_despite_history(self):
        labels = _winning_labels("realize_on_event", sign=-1.0)
        # Append a recent streak that contradicts the hypothesis.
        labels = labels + [
            _label("realize_on_event", f"recent-{i}", "w2", 0.05)
            for i in range(4)
        ]
        decision = evaluate_promotion(labels, policy=_policy(), as_of=AS_OF)
        verdict = next(
            v for v in decision.verdicts if v.hypothesis == "realize_on_event"
        )
        assert verdict.decision == "auto_demote"
        assert verdict.reason_code == "recent_labels_contradict_hypothesis"
        assert verdict.recent_hit_rate == 0.0

    def test_short_recent_streak_does_not_demote(self):
        labels = _winning_labels("realize_on_event", sign=-1.0)
        labels = labels + [
            _label("realize_on_event", f"recent-x-{i}", "w2", 0.05)
            for i in range(2)
        ]
        decision = evaluate_promotion(labels, policy=_policy(), as_of=AS_OF)
        verdict = next(
            v for v in decision.verdicts if v.hypothesis == "realize_on_event"
        )
        assert verdict.decision != "auto_demote"


class TestGateDiscipline:
    def test_deterministic_receipt(self):
        labels = _winning_labels("hold_through_event")
        first = evaluate_promotion(labels, policy=_policy(), as_of=AS_OF)
        second = evaluate_promotion(labels, policy=_policy(), as_of=AS_OF)
        assert first.decision_receipt_sha256 == second.decision_receipt_sha256

    def test_receipt_changes_with_labels(self):
        first = evaluate_promotion(
            _winning_labels("hold_through_event"), policy=_policy(), as_of=AS_OF
        )
        second = evaluate_promotion(
            _winning_labels("hold_through_event", n=9),
            policy=_policy(),
            as_of=AS_OF,
        )
        assert first.decision_receipt_sha256 != second.decision_receipt_sha256

    def test_authority_locks(self):
        decision = evaluate_promotion([], policy=_policy(), as_of=AS_OF)
        assert decision.execution_eligible is False
        assert decision.real_trading_enabled is False
        with pytest.raises(EventCatalystPromotionError) as excinfo:
            PromotionDecision(
                contract=decision.contract,
                as_of=decision.as_of,
                policy_sha256=decision.policy_sha256,
                verdicts=decision.verdicts,
                decision_receipt_sha256=decision.decision_receipt_sha256,
                execution_eligible=True,
            )
        assert (
            excinfo.value.reason_code
            == "event_catalyst_decision_authority_invalid"
        )

    def test_unscored_hypothesis_label_rejected(self):
        with pytest.raises(EventCatalystPromotionError) as excinfo:
            _label("no_signal", "evt-1", "w1", 0.01)
        assert (
            excinfo.value.reason_code
            == "event_catalyst_label_hypothesis_invalid"
        )

    def test_naive_as_of_rejected(self):
        with pytest.raises(EventCatalystPromotionError):
            evaluate_promotion(
                [], policy=_policy(), as_of=datetime(2026, 8, 15, 18, 0, 0)
            )
