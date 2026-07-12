#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Market Maturity State Machine — pure assessment, always real_trading_enabled=false.

Two independent capital pools, each 50,000 CNY (identifier only):
  - A-shares: Time-stage driven by trading-day count.
    Day 1-4 collecting, day 5 review due, day 6-9 continued, day 10 review due,
    day 11+ post-day10 evidence.  Stage is never blocked by evidence shortfalls
    or missing human confirmation.
  - CNFutures: Coverage/stability-driven.  Never reads A-share days, never
    pinned to a low stage by human_confirmed=false.

The A-share live pilot boundary is defined but never auto-activated: after the
minimum simulation window and scientific review, Nicholas must explicitly
authorize a 20%-30% gross-exposure pilot.  The account remains a full 50,000 CNY
account; the pilot limit controls orders, not account ownership.  The proposed
email -> Tonghuashun manual route is design-only and is not implemented here.

Exploration eligibility (sim-only) is intentionally separated from promotion
evidence readiness.  Insufficient samples never close observation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, Sequence


# ---------------------------------------------------------------------------
# Stage enums
# ---------------------------------------------------------------------------


class AshareMaturityStage(str, Enum):
    """A-share time-stage — driven exclusively by trading-day count."""

    STAGE_COLLECTING = "stage_collecting"
    STAGE_DAY5_REVIEW_DUE = "stage_day5_review_due"
    STAGE_CONTINUED_SIMULATION = "stage_continued_simulation"
    STAGE_DAY10_REVIEW_DUE = "stage_day10_review_due"
    STAGE_POST_DAY10_EVIDENCE = "stage_post_day10_evidence"


class FuturesMaturityStage(str, Enum):
    """CNFutures maturity stages — coverage/stability-driven, no date gates."""

    STAGE_INITIAL_SAMPLES = "stage_initial_samples"
    STAGE_COVERAGE_BUILDING = "stage_coverage_building"
    STAGE_STABILITY_EVALUATING = "stage_stability_evaluating"
    STAGE_ELIGIBLE_PENDING_CONFIRMATION = "stage_eligible_pending_confirmation"


# ---------------------------------------------------------------------------
# Evidence dataclasses
# ---------------------------------------------------------------------------


@dataclass
class AshareEvidence:
    """Stratified A-share evidence for maturity assessment.

    All monetary fields are in CNY.  Expectancy and PnL MUST be cost-and-
    slippage-adjusted.  Trading days must be a valid sequence of actual
    trading dates in chronological order.
    """

    trading_days: Sequence[str]
    current_day_index: int  # 0-based index into trading_days
    capital_authority_id: str = "ashare-capital-v1"
    authority_generation: int = 1
    execution_lineage_id: str = "ashare-sim-fresh-20260712-v1"

    # --- Stratified sample counts ---
    observation_counterfactual_count: int = 0
    execution_eligible_sample_count: int = 0
    exploration_fill_count: int = 0
    exploitation_fill_count: int = 0
    completed_round_trip_count: int = 0
    exit_stop_count: int = 0
    risk_reject_count: int = 0
    forward_label_count: int = 0  # diagnostic label cells; never an independent N
    primary_horizon_raw_n: int = 0
    unique_decision_cluster_count: int = 0
    independent_trading_day_count: int = 0
    n_eff: float = 0.0
    primary_horizon_policy_version: str = "ashare-primary-horizon-v1"

    # --- KPI metrics (cost-and-slippage adjusted) ---
    win_rate: Optional[float] = None
    expectancy_cny: Optional[float] = None
    post_cost_pnl_cny: Optional[float] = None
    max_drawdown_cny: Optional[float] = None
    max_drawdown_source: str = ""
    # --- Quality / integrity ---
    chain_consistency_ratio: Optional[float] = None  # 0.0–1.0
    data_integrity_ratio: Optional[float] = None  # 0.0–1.0

    # --- Degradation / failure evidence ---
    degradation_events: int = 0

    # --- Scientific evidence gates (evaluated by their canonical owners) ---
    calibration_evidence_sufficient: bool = False
    point_in_time_lineage_complete: bool = False
    costs_evidence_complete: bool = False
    fill_evidence_revalidated: bool = False
    duplicate_cluster_control_passed: bool = False

    # --- Strategy diversity ---
    strategy_count: int = 0
    strategies_with_positive_expectancy: int = 0

    # --- Human evidence (does not enable live while policy is pending) ---
    human_confirmed: bool = False


@dataclass
class FuturesEvidence:
    """CNFutures evidence for maturity assessment.

    Futures maturity is driven by coverage breadth, regime diversity, and
    stability — never by calendar days or A-share progress.
    """

    # --- Sample counts ---
    valid_sample_count: int = 0
    completed_round_trip_count: int = 0
    capital_authority_id: str = "cn-futures-capital-v1"
    authority_generation: int = 1

    # --- Coverage dimensions ---
    variety_coverage_count: int = 0  # distinct contract varieties traded
    volatility_regime_count: int = 0  # distinct volatility regimes seen
    night_session_coverage: bool = False  # night-session samples present
    contract_rollover_handled: bool = False  # at least one rollover handled
    extreme_risk_scenarios_covered: int = 0  # limit-gap / gap-open / circuit-breaker

    # --- KPI metrics ---
    win_rate: Optional[float] = None
    expectancy_cny: Optional[float] = None
    post_cost_pnl_cny: Optional[float] = None
    max_drawdown_cny: Optional[float] = None

    # --- Stability ---
    stability_score: Optional[float] = None  # 0.0–1.0, higher = more stable

    # --- Human evidence (does not enable live while policy is pending) ---
    human_confirmed: bool = False


# ---------------------------------------------------------------------------
# Assessment result
# ---------------------------------------------------------------------------


@dataclass
class MaturityAssessment:
    """Output of a single maturity evaluation.

    *Never* contains executable order fields or broker instructions.
    ``real_trading_enabled`` is always False.
    """

    market: str
    stage: str
    real_trading_enabled: bool = False
    exploration_eligible: bool = False
    promotion_evidence_ready: bool = False  # technical evidence sufficient
    live_transition_authorized: bool = False
    automatic_promotion_enabled: bool = False
    promotion_policy_status: str = "manual_review_only"
    capital_authority_id: str = ""
    authority_generation: int = 0
    simulation_mode: str = "sim_only"
    live_pilot_exposure_min_pct: Optional[float] = None
    live_pilot_exposure_max_pct: Optional[float] = None
    live_pilot_requires_nicholas_confirmation: bool = True
    live_pilot_activation_state: str = "not_authorized"
    broker_route_status: str = "not_implemented"
    blockers: list[str] = field(default_factory=list)
    evidence_summary: dict[str, Any] = field(default_factory=dict)
    checkpoint_due: Optional[int] = None  # only for A-shares: 5 or 10
    reached_review_days: list[int] = field(default_factory=list)
    total_trading_days: int = 0
    pool_cny: int = 50_000
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# A-share evidence thresholds (promotion evidence readiness)
_MIN_UNIQUE_DECISION_CLUSTERS = 20
_MIN_INDEPENDENT_TRADING_DAYS = 5
_MIN_EFFECTIVE_SAMPLE_SIZE = 10.0
_MIN_EXECUTION_ELIGIBLE_SAMPLES = 20
_MIN_COMPLETED_ROUND_TRIPS = 10
_MIN_CHAIN_CONSISTENCY = 0.85
_MIN_DATA_INTEGRITY = 0.90
_MIN_WIN_RATE = 0.45
_MAX_DRAWDOWN_RATIO = 0.05  # 5% of 50k = 2,500 CNY
_MIN_OBSERVATION_COUNT = 3  # for exploration eligibility
_MIN_STRATEGIES = 1
_MIN_POSITIVE_EXPECTANCY_STRATEGIES = 1

# Futures thresholds
_FUTURES_MIN_SAMPLES = 5
_FUTURES_MIN_ROUND_TRIPS = 3
_FUTURES_MIN_VARIETY = 2
_FUTURES_MIN_REGIME = 2
_FUTURES_MIN_EXTREME_RISK = 1
_FUTURES_MIN_STABILITY = 0.55
_FUTURES_MIN_WIN_RATE = 0.40


# ---------------------------------------------------------------------------
# A-share time stage (purely day-count driven)
# ---------------------------------------------------------------------------


def _ashare_time_stage(
    evidence: AshareEvidence,
) -> tuple[str, Optional[int], list[int]]:
    """Determine A-share stage, checkpoint_due, and reached_review_days from
    trading-day index alone.  Never considers evidence quality or confirmation.
    """
    idx = evidence.current_day_index
    reached: list[int] = []

    if idx >= 4:
        reached.append(5)
    if idx >= 9:
        reached.append(10)

    if idx == 4:
        return AshareMaturityStage.STAGE_DAY5_REVIEW_DUE.value, 5, reached
    elif idx == 9:
        return AshareMaturityStage.STAGE_DAY10_REVIEW_DUE.value, 10, reached
    elif idx >= 10:
        return AshareMaturityStage.STAGE_POST_DAY10_EVIDENCE.value, None, reached
    elif idx >= 5:
        return AshareMaturityStage.STAGE_CONTINUED_SIMULATION.value, None, reached
    else:
        return AshareMaturityStage.STAGE_COLLECTING.value, None, reached


# ---------------------------------------------------------------------------
# A-share maturity assessment
# ---------------------------------------------------------------------------


def assess_ashare_maturity(evidence: AshareEvidence) -> MaturityAssessment:
    """Evaluate A-share maturity.

    Stage is purely time-driven by trading-day index.  Evidence assessment
    is independent — blockers describe what prevents ``promotion_evidence_ready``
    but never revert the time stage, and never block exploration.

    The live pilot boundary is known, but this assessment never treats technical
    maturity or ``human_confirmed`` as the separate explicit activation order.
    """
    total_days = len(evidence.trading_days)
    day = _current_day_or_none(evidence)
    blockers: list[str] = []
    notes: list[str] = []

    # --- Structural checks ---
    if total_days == 0:
        blockers.append("empty_trading_days")
    if day is None:
        blockers.append("current_day_index_out_of_range")

    # --- Time stage (never blocked by evidence or confirmation) ---
    stage, checkpoint_due, reached_review_days = _ashare_time_stage(evidence)

    # --- Exploration eligibility ---
    exploration_eligible = (
        total_days > 0
        and day is not None
        and evidence.observation_counterfactual_count >= _MIN_OBSERVATION_COUNT
    )

    # --- Promotion evidence assessment ---
    evidence_blockers = _ashare_evidence_blockers(evidence)
    blockers.extend(evidence_blockers)

    promotion_evidence_ready = (
        len(evidence_blockers) == 0 and total_days > 0 and day is not None
    )

    # --- Live pilot has a defined boundary, but remains explicitly inactive ---
    live_transition_authorized = False
    notes.append("manual_live_pilot_requires_separate_nicholas_activation")
    notes.append("email_to_tonghuashun_route_design_only_not_implemented")

    # --- Checkpoint notes ---
    if checkpoint_due == 5:
        notes.append("day_5_review_due")
    elif checkpoint_due == 10:
        notes.append("day_10_review_due")

    # --- Evidence summary ---
    evidence_summary = _build_ashare_summary(evidence, day)

    return MaturityAssessment(
        market="ashare",
        stage=stage,
        real_trading_enabled=False,
        exploration_eligible=exploration_eligible,
        promotion_evidence_ready=promotion_evidence_ready,
        live_transition_authorized=live_transition_authorized,
        automatic_promotion_enabled=False,
        promotion_policy_status="manual_20_30pct_pilot_defined_not_authorized",
        capital_authority_id=evidence.capital_authority_id,
        authority_generation=evidence.authority_generation,
        simulation_mode="sim_only",
        live_pilot_exposure_min_pct=0.20,
        live_pilot_exposure_max_pct=0.30,
        live_pilot_requires_nicholas_confirmation=True,
        live_pilot_activation_state="not_authorized",
        broker_route_status="email_to_tonghuashun_design_only_not_implemented",
        blockers=blockers,
        evidence_summary=evidence_summary,
        checkpoint_due=checkpoint_due,
        reached_review_days=reached_review_days,
        total_trading_days=total_days,
        pool_cny=50_000,
        notes=notes,
    )


def _ashare_evidence_blockers(evidence: AshareEvidence) -> list[str]:
    """Return blockers that prevent promotion_evidence_ready.

    These describe technical evidence shortfalls only — never time stage
    or human confirmation.
    """
    blockers: list[str] = []

    if evidence.capital_authority_id != "ashare-capital-v1":
        blockers.append("capital_authority_id_mismatch")
    if evidence.authority_generation != 1:
        blockers.append("authority_generation_mismatch")
    if not str(evidence.execution_lineage_id or "").strip():
        blockers.append("missing_execution_lineage_id")

    if evidence.execution_eligible_sample_count < _MIN_EXECUTION_ELIGIBLE_SAMPLES:
        blockers.append(
            "insufficient_execution_eligible_samples_%d_of_%d"
            % (
                evidence.execution_eligible_sample_count,
                _MIN_EXECUTION_ELIGIBLE_SAMPLES,
            )
        )

    if evidence.unique_decision_cluster_count < _MIN_UNIQUE_DECISION_CLUSTERS:
        blockers.append(
            "insufficient_unique_decision_clusters_%d_of_%d"
            % (
                evidence.unique_decision_cluster_count,
                _MIN_UNIQUE_DECISION_CLUSTERS,
            )
        )
    if evidence.independent_trading_day_count < _MIN_INDEPENDENT_TRADING_DAYS:
        blockers.append(
            "insufficient_independent_trading_days_%d_of_%d"
            % (
                evidence.independent_trading_day_count,
                _MIN_INDEPENDENT_TRADING_DAYS,
            )
        )
    if evidence.n_eff < _MIN_EFFECTIVE_SAMPLE_SIZE:
        blockers.append(
            "insufficient_effective_sample_size_%.2f_of_%.2f"
            % (evidence.n_eff, _MIN_EFFECTIVE_SAMPLE_SIZE)
        )

    if evidence.completed_round_trip_count < _MIN_COMPLETED_ROUND_TRIPS:
        blockers.append(
            "insufficient_completed_round_trips_%d_of_%d"
            % (evidence.completed_round_trip_count, _MIN_COMPLETED_ROUND_TRIPS)
        )

    if evidence.chain_consistency_ratio is None:
        blockers.append("missing_chain_consistency_ratio_evidence")
    elif evidence.chain_consistency_ratio < _MIN_CHAIN_CONSISTENCY:
        blockers.append(
            "low_chain_consistency_%.2f_below_%.2f"
            % (evidence.chain_consistency_ratio, _MIN_CHAIN_CONSISTENCY)
        )

    if evidence.data_integrity_ratio is None:
        blockers.append("missing_data_integrity_ratio_evidence")
    elif evidence.data_integrity_ratio < _MIN_DATA_INTEGRITY:
        blockers.append(
            "low_data_integrity_%.2f_below_%.2f"
            % (evidence.data_integrity_ratio, _MIN_DATA_INTEGRITY)
        )

    if evidence.win_rate is not None and evidence.win_rate < _MIN_WIN_RATE:
        blockers.append(
            "win_rate_below_threshold_%.2f_below_%.2f"
            % (evidence.win_rate, _MIN_WIN_RATE)
        )

    if evidence.expectancy_cny is None:
        blockers.append("missing_expectancy_evidence")
    elif evidence.expectancy_cny <= 0:
        blockers.append("negative_or_zero_expectancy_cny")

    if evidence.post_cost_pnl_cny is None:
        blockers.append("missing_post_cost_pnl_evidence")
    elif evidence.post_cost_pnl_cny <= 0:
        blockers.append("negative_or_zero_post_cost_pnl_cny")

    if evidence.max_drawdown_source != "account_daily_mtm_equity":
        blockers.append("missing_account_daily_mtm_drawdown_source")
    if evidence.max_drawdown_cny is None:
        blockers.append("missing_max_drawdown_evidence")
    else:
        drawdown_abs = abs(evidence.max_drawdown_cny)
        if drawdown_abs > _MAX_DRAWDOWN_RATIO * 50_000:
            blockers.append(
                "excessive_drawdown_%.0f_cny_exceeds_%.0f_cny"
                % (drawdown_abs, _MAX_DRAWDOWN_RATIO * 50_000)
            )

    if evidence.strategy_count < _MIN_STRATEGIES:
        blockers.append(
            "insufficient_strategy_diversity_%d_strategies" % evidence.strategy_count
        )
    elif (
        evidence.strategies_with_positive_expectancy
        < _MIN_POSITIVE_EXPECTANCY_STRATEGIES
    ):
        blockers.append("no_strategy_with_positive_expectancy")

    if evidence.degradation_events > 0:
        blockers.append("degradation_events_detected_%d" % evidence.degradation_events)

    scientific_checks = {
        "calibration_evidence_sufficient": evidence.calibration_evidence_sufficient,
        "point_in_time_lineage_complete": evidence.point_in_time_lineage_complete,
        "costs_evidence_complete": evidence.costs_evidence_complete,
        "fill_evidence_revalidated": evidence.fill_evidence_revalidated,
        "duplicate_cluster_control_passed": evidence.duplicate_cluster_control_passed,
    }
    for field_name, passed in scientific_checks.items():
        if not passed:
            blockers.append("required_scientific_evidence_failed:%s" % field_name)

    return blockers


# ---------------------------------------------------------------------------
# CNFutures maturity assessment
# ---------------------------------------------------------------------------


def _futures_stage(evidence: FuturesEvidence, blockers: list[str]) -> str:
    """Determine futures stage from coverage and stability evidence.

    Stage is never influenced by human_confirmed or calendar days.
    """
    if evidence.valid_sample_count < _FUTURES_MIN_SAMPLES:
        return FuturesMaturityStage.STAGE_INITIAL_SAMPLES.value

    if blockers:
        # Partial coverage: determine how far along
        checks = _futures_coverage_checks(evidence)
        if checks >= 4:
            return FuturesMaturityStage.STAGE_STABILITY_EVALUATING.value
        elif checks >= 2:
            return FuturesMaturityStage.STAGE_COVERAGE_BUILDING.value
        else:
            return FuturesMaturityStage.STAGE_INITIAL_SAMPLES.value

    # No blockers — evaluate full readiness
    checks = _futures_coverage_checks(evidence)
    stability_ok = (
        evidence.stability_score is not None
        and evidence.stability_score >= _FUTURES_MIN_STABILITY
    )
    if checks >= 5 and stability_ok:
        return FuturesMaturityStage.STAGE_ELIGIBLE_PENDING_CONFIRMATION.value
    elif checks >= 4 or stability_ok:
        return FuturesMaturityStage.STAGE_STABILITY_EVALUATING.value
    elif checks >= 2:
        return FuturesMaturityStage.STAGE_COVERAGE_BUILDING.value
    else:
        return FuturesMaturityStage.STAGE_INITIAL_SAMPLES.value


def _futures_coverage_checks(evidence: FuturesEvidence) -> int:
    """Count how many coverage gates are passed (0–5)."""
    return sum(
        [
            evidence.variety_coverage_count >= _FUTURES_MIN_VARIETY,
            evidence.volatility_regime_count >= _FUTURES_MIN_REGIME,
            evidence.night_session_coverage,
            evidence.contract_rollover_handled,
            evidence.extreme_risk_scenarios_covered >= _FUTURES_MIN_EXTREME_RISK,
        ]
    )


def assess_futures_maturity(evidence: FuturesEvidence) -> MaturityAssessment:
    """Evaluate CNFutures maturity from coverage and stability evidence.

    Stage is driven by coverage breadth and stability — never by calendar
    days, A-share progress, or human confirmation.

    ``live_transition_authorized`` is always False for futures (no live
    futures date exists).  ``promotion_evidence_ready`` reflects sim
    maturity evidence alone.
    """
    blockers: list[str] = []
    notes: list[str] = []

    if evidence.capital_authority_id != "cn-futures-capital-v1":
        blockers.append("capital_authority_id_mismatch")
    if evidence.authority_generation != 1:
        blockers.append("authority_generation_mismatch")

    # --- Sample sufficiency ---
    if evidence.valid_sample_count < _FUTURES_MIN_SAMPLES:
        blockers.append(
            "insufficient_valid_samples_%d_of_%d"
            % (evidence.valid_sample_count, _FUTURES_MIN_SAMPLES)
        )

    if evidence.completed_round_trip_count < _FUTURES_MIN_ROUND_TRIPS:
        blockers.append(
            "insufficient_completed_round_trips_%d_of_%d"
            % (evidence.completed_round_trip_count, _FUTURES_MIN_ROUND_TRIPS)
        )

    # --- Coverage gates ---
    if evidence.variety_coverage_count < _FUTURES_MIN_VARIETY:
        blockers.append(
            "insufficient_variety_coverage_%d_of_%d"
            % (evidence.variety_coverage_count, _FUTURES_MIN_VARIETY)
        )

    if evidence.volatility_regime_count < _FUTURES_MIN_REGIME:
        blockers.append(
            "insufficient_volatility_regime_coverage_%d_of_%d"
            % (evidence.volatility_regime_count, _FUTURES_MIN_REGIME)
        )

    if not evidence.night_session_coverage:
        blockers.append("missing_night_session_coverage")

    if not evidence.contract_rollover_handled:
        blockers.append("missing_contract_rollover_handling")

    if evidence.extreme_risk_scenarios_covered < _FUTURES_MIN_EXTREME_RISK:
        blockers.append(
            "insufficient_extreme_risk_scenarios_%d_of_%d"
            % (evidence.extreme_risk_scenarios_covered, _FUTURES_MIN_EXTREME_RISK)
        )

    # --- KPI gates ---
    if evidence.win_rate is not None and evidence.win_rate < _FUTURES_MIN_WIN_RATE:
        blockers.append(
            "win_rate_below_threshold_%.2f_below_%.2f"
            % (evidence.win_rate, _FUTURES_MIN_WIN_RATE)
        )

    if evidence.expectancy_cny is None:
        blockers.append("missing_expectancy_evidence")
    elif evidence.expectancy_cny <= 0:
        blockers.append("negative_or_zero_expectancy_cny")

    if evidence.post_cost_pnl_cny is None:
        blockers.append("missing_post_cost_pnl_evidence")
    elif evidence.post_cost_pnl_cny <= 0:
        blockers.append("negative_or_zero_post_cost_pnl_cny")

    if evidence.max_drawdown_cny is None:
        blockers.append("missing_max_drawdown_evidence")
    else:
        drawdown_abs = abs(evidence.max_drawdown_cny)
        if drawdown_abs > _MAX_DRAWDOWN_RATIO * 50_000:
            blockers.append(
                "excessive_drawdown_%.0f_cny_exceeds_%.0f_cny"
                % (drawdown_abs, _MAX_DRAWDOWN_RATIO * 50_000)
            )

    # --- Stability ---
    if (
        evidence.stability_score is not None
        and evidence.stability_score < _FUTURES_MIN_STABILITY
    ):
        blockers.append(
            "low_stability_score_%.2f_below_%.2f"
            % (evidence.stability_score, _FUTURES_MIN_STABILITY)
        )

    # --- Exploration eligibility ---
    exploration_eligible = evidence.valid_sample_count >= _FUTURES_MIN_SAMPLES

    # --- Stage (never blocked by confirmation or dates) ---
    stage = _futures_stage(evidence, blockers)

    # --- Promotion evidence readiness ---
    promotion_evidence_ready = len(blockers) == 0

    # --- Live transition: always false for futures (no live date) ---
    live_transition_authorized = False

    # --- Evidence summary ---
    evidence_summary = {
        "valid_samples": evidence.valid_sample_count,
        "completed_round_trips": evidence.completed_round_trip_count,
        "variety_coverage": evidence.variety_coverage_count,
        "volatility_regime_count": evidence.volatility_regime_count,
        "night_session": evidence.night_session_coverage,
        "contract_rollover": evidence.contract_rollover_handled,
        "extreme_risk_scenarios": evidence.extreme_risk_scenarios_covered,
        "win_rate": evidence.win_rate,
        "expectancy_cny": evidence.expectancy_cny,
        "max_drawdown_cny": evidence.max_drawdown_cny,
        "stability_score": evidence.stability_score,
    }

    return MaturityAssessment(
        market="cnfutures",
        stage=stage,
        real_trading_enabled=False,
        exploration_eligible=exploration_eligible,
        promotion_evidence_ready=promotion_evidence_ready,
        live_transition_authorized=live_transition_authorized,
        automatic_promotion_enabled=False,
        promotion_policy_status="not_scheduled_for_futures",
        capital_authority_id=evidence.capital_authority_id,
        authority_generation=evidence.authority_generation,
        simulation_mode="sim_only",
        live_pilot_exposure_min_pct=None,
        live_pilot_exposure_max_pct=None,
        live_pilot_requires_nicholas_confirmation=True,
        live_pilot_activation_state="not_scheduled",
        broker_route_status="futures_live_route_not_planned",
        blockers=blockers,
        evidence_summary=evidence_summary,
        checkpoint_due=None,
        reached_review_days=[],
        total_trading_days=0,
        pool_cny=50_000,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _current_day_or_none(evidence: AshareEvidence) -> Optional[str]:
    try:
        return str(evidence.trading_days[evidence.current_day_index])
    except (IndexError, TypeError):
        return None


def _build_ashare_summary(
    evidence: AshareEvidence, day: Optional[str]
) -> dict[str, Any]:
    return {
        "simulation_trading_day": day,
        "total_trading_days": len(evidence.trading_days),
        "observation_count": evidence.observation_counterfactual_count,
        "execution_eligible_samples": evidence.execution_eligible_sample_count,
        "completed_round_trips": evidence.completed_round_trip_count,
        "ready_label_cells": evidence.forward_label_count,
        "forward_labels": evidence.forward_label_count,
        "primary_horizon_raw_N": evidence.primary_horizon_raw_n,
        "unique_decision_clusters": evidence.unique_decision_cluster_count,
        "independent_trading_days": evidence.independent_trading_day_count,
        "N_eff": evidence.n_eff,
        "primary_horizon_policy_version": evidence.primary_horizon_policy_version,
        "chain_consistency": evidence.chain_consistency_ratio,
        "data_integrity": evidence.data_integrity_ratio,
        "win_rate": evidence.win_rate,
        "expectancy_cny": evidence.expectancy_cny,
        "post_cost_pnl_cny": evidence.post_cost_pnl_cny,
        "max_drawdown_cny": evidence.max_drawdown_cny,
        "max_drawdown_source": evidence.max_drawdown_source,
        "degradation_events": evidence.degradation_events,
        "calibration_evidence_sufficient": evidence.calibration_evidence_sufficient,
        "point_in_time_lineage_complete": evidence.point_in_time_lineage_complete,
        "costs_evidence_complete": evidence.costs_evidence_complete,
        "fill_evidence_revalidated": evidence.fill_evidence_revalidated,
        "duplicate_cluster_control_passed": evidence.duplicate_cluster_control_passed,
        "strategy_count": evidence.strategy_count,
        "strategies_with_positive_expectancy": evidence.strategies_with_positive_expectancy,
    }


__all__ = [
    "AshareEvidence",
    "AshareMaturityStage",
    "FuturesEvidence",
    "FuturesMaturityStage",
    "MaturityAssessment",
    "assess_ashare_maturity",
    "assess_futures_maturity",
]
