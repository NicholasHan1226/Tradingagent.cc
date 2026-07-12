from __future__ import annotations

import json
from pathlib import Path

import pytest

from Ashare.evolution_controller import (
    build_evolution_decision,
    decision_market_context,
    write_evolution_decision,
)


AUTHORITY = {
    "capital_authority_id": "ashare-capital-v1",
    "authority_generation": 1,
    "execution_lineage_id": "ashare-sim-fresh-20260712-v1",
}


def _sample_kpi(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "report_type": "sample_journal_kpi",
        "evidence_source": "sample_journal_kpi",
        "market": "ashare",
        "trade_date": "20260713",
        "authority_scope": dict(AUTHORITY),
        "journal_event_count": 80,
        "sample_size_evidence": {
            "ready_label_cell_count": 113,
            "raw_N": 24,
            "unique_decision_cluster_count": 20,
            "independent_trading_day_count": 10,
            "N_eff": 20.0,
        },
        "account_drawdown_evidence": {
            "status": "available",
            "max_drawdown_cny": 180.0,
        },
        "styles": {
            "trend_breakout": {
                "prediction_count": 24,
                "exploration_fill_count": 3,
                "exploitation_fill_count": 0,
                "completed_round_trip_count": 10,
                "post_cost_pnl_cny": 120.0,
                "max_drawdown_cny": 180.0,
                "forward_label_counts": {
                    "m30": {"ready": 20},
                    "m60": {"ready": 20},
                    "close": {"ready": 20},
                    "next_day": {"ready": 20},
                    "3d": {"ready": 18},
                    "5d": {"ready": 15},
                },
            }
        },
        "scientific_evidence": {
            "point_in_time_lineage_complete": True,
            "costs_evidence_complete": True,
            "fill_evidence_revalidated": True,
            "duplicate_cluster_control_passed": True,
            "calibration_evidence_sufficient": True,
            "promotion_evidence_ready": True,
        },
    }
    payload.update(overrides)
    return payload


def test_legacy_portfolio_evolution_is_not_an_evolution_authority() -> None:
    decision = build_evolution_decision(
        {
            "report_type": "portfolio_evolution",
            "trade_date": "20260713",
            "realized_pnl": 1_000.0,
            "recommended_action": "expand_risk_candidate",
        },
        authority_scope=AUTHORITY,
        target_trade_date="20260713",
    )

    assert decision["state"] == "evidence_rejected"
    assert decision["recommended_action"] == "observe_and_label_candidates"
    assert "source_not_sample_journal_kpi" in decision["reasons"]
    assert decision["automatic_promotion_enabled"] is False
    assert decision["automatic_risk_expansion_enabled"] is False


def test_rich_positive_sample_evidence_can_only_become_manual_review_candidate() -> (
    None
):
    decision = build_evolution_decision(
        _sample_kpi(),
        authority_scope=AUTHORITY,
        target_trade_date="20260713",
    )

    assert decision["state"] == "manual_review_candidate"
    assert decision["recommended_action"] == "manual_review_only"
    assert decision["automatic_promotion_enabled"] is False
    assert decision["automatic_risk_expansion_enabled"] is False
    assert decision["live_transition_authorized"] is False
    assert "expand_risk_candidate" not in json.dumps(decision)


def test_authority_generation_mismatch_cannot_enter_context_or_write(
    tmp_path: Path,
) -> None:
    evidence = _sample_kpi(authority_scope={**AUTHORITY, "authority_generation": 2})
    decision = build_evolution_decision(
        evidence,
        authority_scope=AUTHORITY,
        target_trade_date="20260713",
    )

    context = decision_market_context(
        decision,
        target_trade_date="20260713",
        authority_scope=AUTHORITY,
    )
    assert decision["state"] == "evidence_rejected"
    assert "authority_scope_mismatch" in decision["reasons"]
    assert context["evidence_usable"] is False
    assert context["strategy_sample_valid_count"] == 0

    with pytest.raises(ValueError, match="authority_scope_mismatch"):
        write_evolution_decision(
            evidence,
            authority_scope=AUTHORITY,
            review_dir=tmp_path,
            target_trade_date="20260713",
        )
    assert not (tmp_path / "evolution_decision_latest.json").exists()


def test_insufficient_maturity_never_suppresses_observation_or_safe_exploration() -> (
    None
):
    evidence = _sample_kpi(
        styles={
            "trend_breakout": {
                "prediction_count": 2,
                "exploration_fill_count": 0,
                "completed_round_trip_count": 0,
                "forward_label_counts": {},
            }
        },
        scientific_evidence={
            "promotion_evidence_ready": False,
            "point_in_time_lineage_complete": False,
        },
    )
    decision = build_evolution_decision(
        evidence,
        authority_scope=AUTHORITY,
        target_trade_date="20260713",
    )
    context = decision_market_context(
        decision,
        target_trade_date="20260713",
        authority_scope=AUTHORITY,
    )

    assert decision["state"] == "evidence_pending"
    assert decision["recommended_action"] == "observe_and_label_candidates"
    assert decision["policy"]["observation_enabled"] is True
    assert decision["policy"]["safe_exploration_enabled"] is True
    assert context["observation_enabled"] is True
    assert context["safe_exploration_enabled"] is True
    assert context["automatic_risk_expansion_enabled"] is False


def test_many_label_cells_cannot_replace_independent_decision_clusters() -> None:
    evidence = _sample_kpi(
        sample_size_evidence={
            "ready_label_cell_count": 240,
            "raw_N": 40,
            "unique_decision_cluster_count": 1,
            "independent_trading_day_count": 1,
            "N_eff": 1.0,
        }
    )

    decision = build_evolution_decision(
        evidence,
        authority_scope=AUTHORITY,
        target_trade_date="20260713",
    )

    assert decision["state"] == "evidence_pending"
    assert decision["metrics"]["ready_label_cell_count"] == 113
    assert decision["metrics"]["unique_decision_cluster_count"] == 1
    assert "insufficient_unique_decision_clusters" in decision["reasons"]
    assert decision["policy"]["observation_enabled"] is True
    assert decision["policy"]["safe_exploration_enabled"] is True


def test_stale_kpi_is_not_usable_but_observation_remains_enabled() -> None:
    decision = build_evolution_decision(
        _sample_kpi(trade_date="20260712"),
        authority_scope=AUTHORITY,
        target_trade_date="20260713",
    )
    context = decision_market_context(
        decision,
        target_trade_date="20260713",
        authority_scope=AUTHORITY,
    )

    assert "sample_journal_kpi_trade_date_stale" in decision["reasons"]
    assert context["evidence_usable"] is False
    assert context["observation_enabled"] is True


def test_writer_persists_only_sample_journal_authority_decisions(
    tmp_path: Path,
) -> None:
    decision = write_evolution_decision(
        _sample_kpi(),
        authority_scope=AUTHORITY,
        review_dir=tmp_path,
        target_trade_date="20260713",
    )

    latest = json.loads(
        (tmp_path / "evolution_decision_latest.json").read_text(encoding="utf-8")
    )
    log_rows = (
        (tmp_path / "evolution_decision_log.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    assert latest == decision
    assert len(log_rows) == 1
    assert latest["evidence_source"] == "sample_journal_kpi"
    assert latest["authority_scope"] == AUTHORITY
    assert latest["real_trading_enabled"] is False
