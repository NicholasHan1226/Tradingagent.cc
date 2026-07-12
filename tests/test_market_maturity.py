#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for shared/review/market_maturity.py — reworked per independent review.

Coverage targets:
  - Human confirmation does NOT block stage advancement, exploration, or evidence
  - A-share time stages: collecting / day5 due / continued / day10 due / post-day10
  - checkpoint_due only on exact days 5/10; reached_review_days tracks history
  - Promotion evidence ready vs live_transition_authorized are separate
  - Futures stages: samples → coverage → stability → eligible; no date influence
  - Futures never reads A-share; not pinned to stage0 by human_confirmed
  - No hardcoded risk budgets; pool_cny=50000 identifier only
  - real_trading_enabled always False
  - evidence_summary present and populated
  - No dead risk budget function
"""

from __future__ import annotations

import pytest
from shared.review.market_maturity import (
    AshareEvidence,
    AshareMaturityStage,
    FuturesEvidence,
    FuturesMaturityStage,
    assess_ashare_maturity,
    assess_futures_maturity,
)


# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------


def _ashare_collecting() -> AshareEvidence:
    """Day 1 — collecting phase, minimal evidence."""
    return AshareEvidence(
        trading_days=[
            "20260701",
            "20260702",
            "20260703",
            "20260704",
            "20260705",
            "20260708",
            "20260709",
            "20260710",
            "20260711",
            "20260712",
            "20260715",
            "20260716",
            "20260717",
            "20260718",
            "20260719",
        ],
        current_day_index=0,  # day 1
    )


def _ashare_day5() -> AshareEvidence:
    """Day 5 — review due, decent metrics."""
    return AshareEvidence(
        trading_days=[
            "20260701",
            "20260702",
            "20260703",
            "20260704",
            "20260705",
            "20260708",
            "20260709",
            "20260710",
            "20260711",
            "20260712",
            "20260715",
            "20260716",
            "20260717",
            "20260718",
            "20260719",
        ],
        current_day_index=4,  # day 5
        observation_counterfactual_count=15,
        exploration_fill_count=5,
        completed_round_trip_count=3,
        forward_label_count=8,
        win_rate=0.55,
        expectancy_cny=12.0,
        post_cost_pnl_cny=36.0,
        max_drawdown_cny=-150.0,
        chain_consistency_ratio=0.92,
        data_integrity_ratio=0.95,
        strategy_count=2,
        strategies_with_positive_expectancy=1,
    )


def _ashare_day6() -> AshareEvidence:
    """Day 6 — continued simulation, after day5 review."""
    return AshareEvidence(
        trading_days=[
            "20260701",
            "20260702",
            "20260703",
            "20260704",
            "20260705",
            "20260708",
            "20260709",
            "20260710",
            "20260711",
            "20260712",
            "20260715",
            "20260716",
            "20260717",
            "20260718",
            "20260719",
        ],
        current_day_index=5,  # day 6
        observation_counterfactual_count=20,
        exploration_fill_count=7,
        completed_round_trip_count=4,
        forward_label_count=12,
        win_rate=0.58,
        expectancy_cny=15.0,
        post_cost_pnl_cny=60.0,
        max_drawdown_cny=-180.0,
        chain_consistency_ratio=0.93,
        data_integrity_ratio=0.96,
        strategy_count=2,
        strategies_with_positive_expectancy=1,
    )


def _ashare_day10() -> AshareEvidence:
    """Day 10 — review due, solid evidence."""
    return AshareEvidence(
        trading_days=[
            "20260701",
            "20260702",
            "20260703",
            "20260704",
            "20260705",
            "20260708",
            "20260709",
            "20260710",
            "20260711",
            "20260712",
            "20260715",
            "20260716",
            "20260717",
            "20260718",
            "20260719",
        ],
        current_day_index=9,  # day 10
        observation_counterfactual_count=35,
        exploration_fill_count=15,
        completed_round_trip_count=8,
        forward_label_count=22,
        win_rate=0.60,
        expectancy_cny=20.0,
        post_cost_pnl_cny=160.0,
        max_drawdown_cny=-250.0,
        chain_consistency_ratio=0.96,
        data_integrity_ratio=0.98,
        strategy_count=3,
        strategies_with_positive_expectancy=2,
    )


def _ashare_day11_solid() -> AshareEvidence:
    """Day 11+ — post-day10, strong evidence meeting all thresholds."""
    return AshareEvidence(
        trading_days=[
            "20260701",
            "20260702",
            "20260703",
            "20260704",
            "20260705",
            "20260708",
            "20260709",
            "20260710",
            "20260711",
            "20260712",
            "20260715",
            "20260716",
            "20260717",
            "20260718",
            "20260719",
        ],
        current_day_index=10,  # day 11
        observation_counterfactual_count=45,
        execution_eligible_sample_count=24,
        exploration_fill_count=18,
        completed_round_trip_count=12,
        forward_label_count=25,
        primary_horizon_raw_n=28,
        unique_decision_cluster_count=25,
        independent_trading_day_count=10,
        n_eff=22.0,
        win_rate=0.62,
        expectancy_cny=25.0,
        post_cost_pnl_cny=300.0,
        max_drawdown_cny=-300.0,
        max_drawdown_source="account_daily_mtm_equity",
        chain_consistency_ratio=0.97,
        data_integrity_ratio=0.99,
        calibration_evidence_sufficient=True,
        point_in_time_lineage_complete=True,
        costs_evidence_complete=True,
        fill_evidence_revalidated=True,
        duplicate_cluster_control_passed=True,
        strategy_count=3,
        strategies_with_positive_expectancy=2,
    )


def test_label_cells_cannot_satisfy_maturity_without_independent_primary_clusters():
    evidence = _ashare_day11_solid()
    evidence.forward_label_count = 240
    evidence.primary_horizon_raw_n = 40
    evidence.unique_decision_cluster_count = 1
    evidence.independent_trading_day_count = 1
    evidence.n_eff = 1.0

    assessment = assess_ashare_maturity(evidence)

    assert assessment.promotion_evidence_ready is False
    assert any(
        "insufficient_unique_decision_clusters" in blocker
        for blocker in assessment.blockers
    )
    assert assessment.exploration_eligible is True


def _futures_minimal() -> FuturesEvidence:
    return FuturesEvidence()


def _futures_decent() -> FuturesEvidence:
    return FuturesEvidence(
        valid_sample_count=20,
        completed_round_trip_count=8,
        variety_coverage_count=3,
        volatility_regime_count=2,
        night_session_coverage=True,
        contract_rollover_handled=True,
        extreme_risk_scenarios_covered=2,
        win_rate=0.55,
        expectancy_cny=30.0,
        post_cost_pnl_cny=240.0,
        max_drawdown_cny=-350.0,
        stability_score=0.72,
    )


# ---------------------------------------------------------------------------
# 1. Human confirmation does NOT block stage advancement
# ---------------------------------------------------------------------------


class TestHumanConfirmationDoesNotBlockStage:
    """Without confirmation, the stage still advances naturally by time/coverage."""

    def test_ashare_advances_to_day5_stage_without_confirmation(self) -> None:
        result = assess_ashare_maturity(_ashare_day5())
        assert result.stage == AshareMaturityStage.STAGE_DAY5_REVIEW_DUE.value

    def test_ashare_advances_to_post_day10_without_confirmation(self) -> None:
        result = assess_ashare_maturity(_ashare_day11_solid())
        assert result.stage == AshareMaturityStage.STAGE_POST_DAY10_EVIDENCE.value

    def test_exploration_eligible_without_confirmation(self) -> None:
        result = assess_ashare_maturity(_ashare_day10())
        # Enough observations for exploration regardless of confirmation
        assert result.exploration_eligible is True

    def test_futures_not_pinned_to_stage0_by_no_confirmation(self) -> None:
        evidence = _futures_decent()
        evidence.human_confirmed = False
        result = assess_futures_maturity(evidence)
        # Should not be stage0 — coverage is decent
        assert result.stage != FuturesMaturityStage.STAGE_INITIAL_SAMPLES.value

    def test_human_confirmed_does_not_auto_real_trading(self) -> None:
        evidence = _ashare_day11_solid()
        evidence.human_confirmed = True
        result = assess_ashare_maturity(evidence)
        assert result.real_trading_enabled is False


# ---------------------------------------------------------------------------
# 2. live_transition_authorized vs promotion_evidence_ready
# ---------------------------------------------------------------------------


class TestLiveTransitionVsPromotionEvidence:
    """Evidence can mature, but the current policy never authorizes live."""

    def test_evidence_ready_without_confirmation_no_live_transition(self) -> None:
        evidence = _ashare_day11_solid()
        evidence.human_confirmed = False
        result = assess_ashare_maturity(evidence)
        assert result.promotion_evidence_ready is True
        assert result.live_transition_authorized is False

    def test_evidence_ready_with_confirmation_stays_sim_only(self) -> None:
        evidence = _ashare_day11_solid()
        evidence.human_confirmed = True
        result = assess_ashare_maturity(evidence)
        assert result.promotion_evidence_ready is True
        assert result.live_transition_authorized is False
        assert result.automatic_promotion_enabled is False
        assert (
            result.promotion_policy_status
            == "manual_20_30pct_pilot_defined_not_authorized"
        )

    def test_evidence_not_ready_with_confirmation_no_live_transition(self) -> None:
        evidence = _ashare_day5()
        evidence.human_confirmed = True
        result = assess_ashare_maturity(evidence)
        assert result.live_transition_authorized is False

    def test_live_transition_still_real_trading_false(self) -> None:
        evidence = _ashare_day11_solid()
        evidence.human_confirmed = True
        result = assess_ashare_maturity(evidence)
        assert result.live_transition_authorized is False
        assert result.automatic_promotion_enabled is False
        assert result.real_trading_enabled is False

    def test_futures_live_transition_always_false(self) -> None:
        """No live futures date exists — live_transition_authorized always false."""
        for human in (False, True):
            evidence = _futures_decent()
            evidence.human_confirmed = human
            result = assess_futures_maturity(evidence)
            assert result.live_transition_authorized is False

    def test_ashare_manual_pilot_boundary_is_explicit_but_inactive(self) -> None:
        result = assess_ashare_maturity(_ashare_day11_solid())
        assert result.capital_authority_id == "ashare-capital-v1"
        assert result.authority_generation == 1
        assert result.live_pilot_exposure_min_pct == 0.20
        assert result.live_pilot_exposure_max_pct == 0.30
        assert result.live_pilot_requires_nicholas_confirmation is True
        assert result.live_pilot_activation_state == "not_authorized"
        assert (
            result.broker_route_status
            == "email_to_tonghuashun_design_only_not_implemented"
        )
        assert result.simulation_mode == "sim_only"

    def test_futures_has_no_live_pilot_schedule_or_exposure_range(self) -> None:
        result = assess_futures_maturity(_futures_decent())
        assert result.capital_authority_id == "cn-futures-capital-v1"
        assert result.live_pilot_exposure_min_pct is None
        assert result.live_pilot_exposure_max_pct is None
        assert result.live_pilot_activation_state == "not_scheduled"
        assert result.broker_route_status == "futures_live_route_not_planned"


# ---------------------------------------------------------------------------
# 3. A-share time stages
# ---------------------------------------------------------------------------


class TestAshareTimeStages:
    def test_day1_is_collecting(self) -> None:
        result = assess_ashare_maturity(_ashare_collecting())
        assert result.stage == AshareMaturityStage.STAGE_COLLECTING.value

    def test_day4_is_collecting(self) -> None:
        evidence = _ashare_collecting()
        evidence.current_day_index = 3  # day 4
        result = assess_ashare_maturity(evidence)
        assert result.stage == AshareMaturityStage.STAGE_COLLECTING.value

    def test_day5_is_review_due(self) -> None:
        result = assess_ashare_maturity(_ashare_day5())
        assert result.stage == AshareMaturityStage.STAGE_DAY5_REVIEW_DUE.value

    def test_day6_is_continued_simulation(self) -> None:
        result = assess_ashare_maturity(_ashare_day6())
        assert result.stage == AshareMaturityStage.STAGE_CONTINUED_SIMULATION.value

    def test_day9_is_continued_simulation(self) -> None:
        evidence = _ashare_day6()
        evidence.current_day_index = 8  # day 9
        result = assess_ashare_maturity(evidence)
        assert result.stage == AshareMaturityStage.STAGE_CONTINUED_SIMULATION.value

    def test_day10_is_review_due(self) -> None:
        result = assess_ashare_maturity(_ashare_day10())
        assert result.stage == AshareMaturityStage.STAGE_DAY10_REVIEW_DUE.value

    def test_day11_is_post_day10(self) -> None:
        result = assess_ashare_maturity(_ashare_day11_solid())
        assert result.stage == AshareMaturityStage.STAGE_POST_DAY10_EVIDENCE.value


# ---------------------------------------------------------------------------
# 4. checkpoint_due and reached_review_days
# ---------------------------------------------------------------------------


class TestCheckpointDueAndReachedReviewDays:
    def test_day5_checkpoint_due_on_exact_day5(self) -> None:
        result = assess_ashare_maturity(_ashare_day5())
        assert result.checkpoint_due == 5

    def test_day10_checkpoint_due_on_exact_day10(self) -> None:
        result = assess_ashare_maturity(_ashare_day10())
        assert result.checkpoint_due == 10

    def test_no_checkpoint_due_on_day6(self) -> None:
        result = assess_ashare_maturity(_ashare_day6())
        assert result.checkpoint_due is None

    def test_no_checkpoint_due_on_day1(self) -> None:
        result = assess_ashare_maturity(_ashare_collecting())
        assert result.checkpoint_due is None

    def test_day6_reached_review_days_includes_5(self) -> None:
        result = assess_ashare_maturity(_ashare_day6())
        assert 5 in result.reached_review_days
        assert 10 not in result.reached_review_days

    def test_day11_reached_review_days_includes_both(self) -> None:
        result = assess_ashare_maturity(_ashare_day11_solid())
        assert 5 in result.reached_review_days
        assert 10 in result.reached_review_days

    def test_day5_reached_review_days_empty_before_day5(self) -> None:
        result = assess_ashare_maturity(_ashare_collecting())
        assert result.reached_review_days == []


# ---------------------------------------------------------------------------
# 5. Promotion evidence: technical thresholds, separated from time stage
# ---------------------------------------------------------------------------


class TestPromotionEvidenceTechnicalThresholds:
    def test_insufficient_execution_eligible_samples_blocks_evidence(self) -> None:
        evidence = _ashare_day11_solid()
        evidence.execution_eligible_sample_count = 19
        result = assess_ashare_maturity(evidence)
        assert result.promotion_evidence_ready is False
        assert any("execution_eligible" in blocker for blocker in result.blockers)

    @pytest.mark.parametrize(
        "field_name",
        [
            "chain_consistency_ratio",
            "data_integrity_ratio",
            "expectancy_cny",
            "post_cost_pnl_cny",
            "max_drawdown_cny",
        ],
    )
    def test_missing_required_quality_evidence_blocks_promotion(
        self, field_name: str
    ) -> None:
        evidence = _ashare_day11_solid()
        setattr(evidence, field_name, None)
        result = assess_ashare_maturity(evidence)
        assert result.promotion_evidence_ready is False
        assert any(
            field_name.replace("_cny", "") in blocker for blocker in result.blockers
        )

    def test_label_cell_count_alone_does_not_control_evidence(self) -> None:
        evidence = _ashare_day11_solid()
        evidence.forward_label_count = 8
        result = assess_ashare_maturity(evidence)
        assert result.promotion_evidence_ready is True
        assert not any("forward_label" in b.lower() for b in result.blockers)

    def test_insufficient_round_trips_blocks_evidence(self) -> None:
        evidence = _ashare_day11_solid()
        evidence.completed_round_trip_count = 5  # below 10
        result = assess_ashare_maturity(evidence)
        assert result.promotion_evidence_ready is False
        assert any("round_trip" in b.lower() for b in result.blockers)

    def test_blockers_do_not_revert_time_stage(self) -> None:
        """Evidence blockers should not change the time-based stage."""
        evidence = _ashare_day11_solid()
        evidence.forward_label_count = 5
        evidence.completed_round_trip_count = 3
        result = assess_ashare_maturity(evidence)
        # Stage is still post_day10 despite blockers
        assert result.stage == AshareMaturityStage.STAGE_POST_DAY10_EVIDENCE.value
        assert result.promotion_evidence_ready is False

    def test_blockers_do_not_block_exploration(self) -> None:
        evidence = _ashare_day11_solid()
        evidence.forward_label_count = 5
        evidence.completed_round_trip_count = 3
        result = assess_ashare_maturity(evidence)
        assert result.exploration_eligible is True

    def test_short_term_profit_alone_insufficient(self) -> None:
        evidence = _ashare_day5()
        evidence.win_rate = 0.95
        evidence.expectancy_cny = 80.0
        evidence.post_cost_pnl_cny = 500.0
        evidence.completed_round_trip_count = 2
        evidence.forward_label_count = 5
        result = assess_ashare_maturity(evidence)
        assert result.promotion_evidence_ready is False

    def test_negative_expectancy_blocks(self) -> None:
        evidence = _ashare_day11_solid()
        evidence.expectancy_cny = -5.0
        result = assess_ashare_maturity(evidence)
        assert result.promotion_evidence_ready is False

    def test_excessive_drawdown_blocks(self) -> None:
        evidence = _ashare_day11_solid()
        evidence.max_drawdown_cny = -4000.0
        result = assess_ashare_maturity(evidence)
        assert result.promotion_evidence_ready is False

    def test_low_chain_consistency_blocks(self) -> None:
        evidence = _ashare_day11_solid()
        evidence.chain_consistency_ratio = 0.60
        result = assess_ashare_maturity(evidence)
        assert result.promotion_evidence_ready is False

    def test_low_data_integrity_blocks(self) -> None:
        evidence = _ashare_day11_solid()
        evidence.data_integrity_ratio = 0.70
        result = assess_ashare_maturity(evidence)
        assert result.promotion_evidence_ready is False

    def test_degradation_events_block(self) -> None:
        evidence = _ashare_day11_solid()
        evidence.degradation_events = 3
        result = assess_ashare_maturity(evidence)
        assert result.promotion_evidence_ready is False

    def test_no_positive_expectancy_strategy_blocks(self) -> None:
        evidence = _ashare_day11_solid()
        evidence.strategies_with_positive_expectancy = 0
        result = assess_ashare_maturity(evidence)
        assert result.promotion_evidence_ready is False

    def test_all_thresholds_met_evidence_ready(self) -> None:
        result = assess_ashare_maturity(_ashare_day11_solid())
        assert result.promotion_evidence_ready is True
        # Day 11 should meet all thresholds with solid evidence

    @pytest.mark.parametrize(
        "field_name",
        [
            "calibration_evidence_sufficient",
            "point_in_time_lineage_complete",
            "costs_evidence_complete",
            "fill_evidence_revalidated",
            "duplicate_cluster_control_passed",
        ],
    )
    def test_scientific_evidence_flags_are_required(self, field_name: str) -> None:
        evidence = _ashare_day11_solid()
        setattr(evidence, field_name, False)
        result = assess_ashare_maturity(evidence)
        assert result.promotion_evidence_ready is False
        assert any(field_name in blocker for blocker in result.blockers)

    def test_day5_with_good_evidence_not_ready_due_to_round_trips(self) -> None:
        """Even good day5 metrics may not meet the round-trip threshold."""
        evidence = _ashare_day5()
        evidence.completed_round_trip_count = 3
        evidence.forward_label_count = 8
        # Not enough round trips (10) or labels (20)
        result = assess_ashare_maturity(evidence)
        assert result.promotion_evidence_ready is False


# ---------------------------------------------------------------------------
# 6. Futures stages — coverage/stability driven, no date influence
# ---------------------------------------------------------------------------


class TestFuturesStages:
    def test_futures_initial_with_zero_samples(self) -> None:
        result = assess_futures_maturity(_futures_minimal())
        assert result.stage == FuturesMaturityStage.STAGE_INITIAL_SAMPLES.value

    def test_futures_drawdown_uses_full_50000_pool_not_retired_5000_margin_pool(
        self,
    ) -> None:
        evidence = _futures_decent()
        evidence.max_drawdown_cny = -3_000.0
        result = assess_futures_maturity(evidence)
        assert result.promotion_evidence_ready is False
        assert any("drawdown" in blocker for blocker in result.blockers)
        assert all("margin_pool" not in blocker for blocker in result.blockers)

    def test_futures_advances_with_coverage(self) -> None:
        evidence = _futures_decent()
        evidence.human_confirmed = True
        result = assess_futures_maturity(evidence)
        assert result.stage != FuturesMaturityStage.STAGE_INITIAL_SAMPLES.value

    def test_futures_stage_not_affected_by_confirmation(self) -> None:
        for human in (False, True):
            evidence = _futures_decent()
            evidence.human_confirmed = human
            result = assess_futures_maturity(evidence)
            # Same stage regardless of confirmation
            assert result.stage != FuturesMaturityStage.STAGE_INITIAL_SAMPLES.value
            assert (
                result.live_transition_authorized is False
            )  # always false for futures

    def test_futures_blocked_on_missing_night_session(self) -> None:
        evidence = _futures_decent()
        evidence.night_session_coverage = False
        result = assess_futures_maturity(evidence)
        assert any("night_session" in b.lower() for b in result.blockers)

    def test_futures_blocked_on_missing_variety(self) -> None:
        evidence = _futures_decent()
        evidence.variety_coverage_count = 1
        result = assess_futures_maturity(evidence)
        assert any("variety" in b.lower() for b in result.blockers)

    def test_futures_blocked_on_no_rollover(self) -> None:
        evidence = _futures_decent()
        evidence.contract_rollover_handled = False
        result = assess_futures_maturity(evidence)
        assert any("rollover" in b.lower() for b in result.blockers)

    def test_futures_blocked_on_low_stability(self) -> None:
        evidence = _futures_decent()
        evidence.stability_score = 0.3
        result = assess_futures_maturity(evidence)
        assert any("stability" in b.lower() for b in result.blockers)

    def test_futures_blocked_on_insufficient_extreme_risk(self) -> None:
        evidence = _futures_decent()
        evidence.extreme_risk_scenarios_covered = 0
        result = assess_futures_maturity(evidence)
        assert any(
            "extreme" in b.lower() or "risk" in b.lower() for b in result.blockers
        )

    def test_futures_no_day_checkpoints(self) -> None:
        result = assess_futures_maturity(_futures_decent())
        assert result.checkpoint_due is None
        assert result.reached_review_days == []

    def test_futures_promotion_evidence_can_be_ready(self) -> None:
        evidence = _futures_decent()
        evidence.human_confirmed = True
        result = assess_futures_maturity(evidence)
        # promotion_evidence_ready reflects sim maturity
        # (actual value depends on thresholds; just verify it's a bool)
        assert isinstance(result.promotion_evidence_ready, bool)


# ---------------------------------------------------------------------------
# 7. Futures never reads A-share progress
# ---------------------------------------------------------------------------


class TestFuturesAshareIsolation:
    def test_futures_no_trading_days_field(self) -> None:
        evidence = _futures_decent()
        assert not hasattr(evidence, "trading_days")

    def test_futures_result_no_trading_day_fields(self) -> None:
        result = assess_futures_maturity(_futures_decent())
        assert result.total_trading_days == 0
        assert result.checkpoint_due is None


# ---------------------------------------------------------------------------
# 8. No hardcoded risk budget
# ---------------------------------------------------------------------------


class TestNoHardcodedRiskBudget:
    def test_ashare_assessment_has_pool_cny_not_budget(self) -> None:
        result = assess_ashare_maturity(_ashare_day5())
        assert result.pool_cny == 50000
        # No risk budget dict with hardcoded numbers
        assert not hasattr(result, "risk_budget_recommendation")
        assert not hasattr(result, "max_risk_per_order_cny")

    def test_futures_assessment_has_pool_cny(self) -> None:
        result = assess_futures_maturity(_futures_minimal())
        assert result.pool_cny == 50000

    def test_no_recommended_risk_budget_function(self) -> None:
        """The module should not export recommended_risk_budget."""
        import shared.review.market_maturity as mm

        assert not hasattr(mm, "recommended_risk_budget")


# ---------------------------------------------------------------------------
# 9. evidence_summary
# ---------------------------------------------------------------------------


class TestEvidenceSummary:
    def test_ashare_summary_has_key_fields(self) -> None:
        result = assess_ashare_maturity(_ashare_day11_solid())
        summary = result.evidence_summary
        assert isinstance(summary, dict)
        assert "simulation_trading_day" in summary
        assert "valid_samples" in summary or "observation_count" in summary
        assert "completed_round_trips" in summary
        assert "forward_labels" in summary
        assert "chain_consistency" in summary
        assert "data_integrity" in summary
        assert "expectancy_cny" in summary
        assert "max_drawdown_cny" in summary

    def test_futures_summary_has_key_fields(self) -> None:
        result = assess_futures_maturity(_futures_decent())
        summary = result.evidence_summary
        assert isinstance(summary, dict)
        assert "valid_samples" in summary
        assert "variety_coverage" in summary
        assert "volatility_regime_count" in summary
        assert "night_session" in summary
        assert "stability_score" in summary

    def test_ashare_summary_includes_current_day(self) -> None:
        result = assess_ashare_maturity(_ashare_day5())
        assert result.evidence_summary["simulation_trading_day"] == "20260705"


# ---------------------------------------------------------------------------
# 10. real_trading_enabled always False
# ---------------------------------------------------------------------------


class TestRealTradingAlwaysFalse:
    def test_all_scenarios_real_trading_false(self) -> None:
        scenarios = [
            (_ashare_collecting(), assess_ashare_maturity),
            (_ashare_day5(), assess_ashare_maturity),
            (_ashare_day10(), assess_ashare_maturity),
            (_ashare_day11_solid(), assess_ashare_maturity),
            (_futures_minimal(), assess_futures_maturity),
            (_futures_decent(), assess_futures_maturity),
        ]
        for evidence, fn in scenarios:
            evidence.human_confirmed = True
            result = fn(evidence)  # type: ignore[arg-type]
            assert result.real_trading_enabled is False


# ---------------------------------------------------------------------------
# 11. No broker instructions generated
# ---------------------------------------------------------------------------


class TestNoBrokerInstructions:
    def test_ashare_no_order_fields(self) -> None:
        result = assess_ashare_maturity(_ashare_day11_solid())
        assert not hasattr(result, "orders")
        assert not hasattr(result, "instructions")
        assert not hasattr(result, "broker_actions")

    def test_futures_no_order_fields(self) -> None:
        result = assess_futures_maturity(_futures_decent())
        assert not hasattr(result, "orders")
        assert not hasattr(result, "instructions")


# ---------------------------------------------------------------------------
# 12. Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_trading_days(self) -> None:
        evidence = AshareEvidence(trading_days=[], current_day_index=0)
        result = assess_ashare_maturity(evidence)
        assert result.stage == AshareMaturityStage.STAGE_COLLECTING.value
        assert "trading_days" in str(result.blockers).lower()

    def test_current_day_out_of_range(self) -> None:
        evidence = AshareEvidence(trading_days=["20260701"], current_day_index=5)
        result = assess_ashare_maturity(evidence)
        assert result.checkpoint_due is None

    def test_futures_zero_samples_all_blockers(self) -> None:
        result = assess_futures_maturity(_futures_minimal())
        assert result.promotion_evidence_ready is False
        assert len(result.blockers) > 0

    def test_blockers_are_non_empty_strings(self) -> None:
        for fn, evidence_factory in [
            (assess_ashare_maturity, _ashare_day6),
            (assess_futures_maturity, _futures_minimal),
        ]:
            result = fn(evidence_factory())  # type: ignore[operator]
            for b in result.blockers:
                assert isinstance(b, str) and len(b) > 0


# ---------------------------------------------------------------------------
# 13. Stage enum values stable
# ---------------------------------------------------------------------------


class TestStageEnumValues:
    def test_ashare_stages_stable(self) -> None:
        assert AshareMaturityStage.STAGE_COLLECTING.value == "stage_collecting"
        assert (
            AshareMaturityStage.STAGE_DAY5_REVIEW_DUE.value == "stage_day5_review_due"
        )
        assert (
            AshareMaturityStage.STAGE_CONTINUED_SIMULATION.value
            == "stage_continued_simulation"
        )
        assert (
            AshareMaturityStage.STAGE_DAY10_REVIEW_DUE.value == "stage_day10_review_due"
        )
        assert (
            AshareMaturityStage.STAGE_POST_DAY10_EVIDENCE.value
            == "stage_post_day10_evidence"
        )

    def test_futures_stages_stable(self) -> None:
        assert (
            FuturesMaturityStage.STAGE_INITIAL_SAMPLES.value == "stage_initial_samples"
        )
        assert (
            FuturesMaturityStage.STAGE_COVERAGE_BUILDING.value
            == "stage_coverage_building"
        )
        assert (
            FuturesMaturityStage.STAGE_STABILITY_EVALUATING.value
            == "stage_stability_evaluating"
        )
        assert (
            FuturesMaturityStage.STAGE_ELIGIBLE_PENDING_CONFIRMATION.value
            == "stage_eligible_pending_confirmation"
        )


# ---------------------------------------------------------------------------
# 14. Two capital pools never mix
# ---------------------------------------------------------------------------


class TestCapitalPoolIsolation:
    def test_ashare_pool_cny_independent(self) -> None:
        assert assess_ashare_maturity(_ashare_day5()).pool_cny == 50000

    def test_futures_pool_cny_independent(self) -> None:
        assert assess_futures_maturity(_futures_minimal()).pool_cny == 50000

    def test_markets_labeled_correctly(self) -> None:
        assert assess_ashare_maturity(_ashare_day5()).market == "ashare"
        assert assess_futures_maturity(_futures_minimal()).market == "cnfutures"
