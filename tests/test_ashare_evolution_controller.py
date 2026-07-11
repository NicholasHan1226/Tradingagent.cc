from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from Ashare.evolution_controller import build_evolution_decision, decision_market_context, write_evolution_decision


def _qualified_evidence(**overrides):
    payload = {
        "capital_epoch": 2,
        "trade_date": "20260711",
        "strategy_sample_count": 20,
        "actions": [{"action": "observe", "reason": "non_positive_realized_pnl"}],
        "pnl": {"total_pnl": 100.0, "realized_pnl": 0.0, "equity": 50_100.0},
        "evolution_evidence": {
            "eligible_sample_count": 20,
            "realized_round_trip_count": 10,
            "forward_label_count": 20,
        },
    }
    payload.update(overrides)
    return payload


def test_unrealized_profit_never_expands_risk():
    decision = build_evolution_decision(
        _qualified_evidence(), target_trade_date="20260711", current_epoch_id=2
    )
    assert decision["recommended_action"] == "observe_and_label_candidates"
    assert "non_positive_realized_pnl" in decision["reasons"]


def test_stale_epoch_never_enters_capital_plan_context():
    decision = build_evolution_decision(
        _qualified_evidence(capital_epoch=1), target_trade_date="20260711", current_epoch_id=2
    )
    context = decision_market_context(decision, target_trade_date="20260711", current_epoch_id=2)
    assert context["evidence_usable"] is False
    assert context["evidence_rejection_reason"] == "capital_epoch_mismatch"
    assert context["strategy_sample_valid_count"] == 0.0


class AshareEvolutionControllerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.review_dir = Path(self.tmp.name)

    def test_missing_daily_fill_keeps_decision_in_observation_mode(self) -> None:
        portfolio = {
            "market": "ashare",
            "trade_date": "20260710",
            "state": "sample_insufficient",
            "strategy_sample_count": 2,
            "today_strategy_sample_count": 0,
            "pnl": {"total_pnl": -149.13, "equity": 199850.87},
            "rankings": [
                {"style_name": "ashare_portfolio", "trades": 2, "pnl": -149.13},
                {"style_name": "ashare_50000", "trades": 2, "pnl": -10.0},
                {"style_name": "ashare_100000", "trades": 2, "pnl": -13.84},
            ],
        }

        decision = build_evolution_decision(
            portfolio,
            min_strategy_samples=5,
        )

        self.assertEqual(decision["state"], "evidence_pending")
        self.assertEqual(decision["recommended_action"], "observe_and_label_candidates")
        self.assertEqual(decision["policy"]["min_strategy_samples"], 5)
        self.assertIn("daily_trade_target_removed", decision["reasons"])
        self.assertFalse(decision["real_trading_enabled"])
        self.assertIn("candidate_layer_required", decision["guardrails"])

    def test_stale_portfolio_date_resets_today_sample_count(self) -> None:
        portfolio = {
            "market": "ashare",
            "capital_epoch": 2,
            "trade_date": "20260709",
            "state": "sample_insufficient",
            "strategy_sample_count": 2,
            "today_strategy_sample_count": 2,
            "pnl": {"total_pnl": -149.13, "equity": 199850.87},
        }

        decision = build_evolution_decision(
            portfolio,
            target_trade_date="20260710",
            min_strategy_samples=5,
        )

        self.assertEqual(decision["state"], "evidence_pending")
        self.assertEqual(decision["recommended_action"], "observe_and_label_candidates")
        self.assertEqual(decision["policy"]["today_strategy_sample_count"], 0)
        self.assertIn("portfolio_evolution_trade_date_stale", decision["reasons"])

    def test_decision_market_context_exposes_capital_plan_inputs(self) -> None:
        decision = {
            "capital_epoch": 2,
            "evidence_trade_date": "20260711",
            "recommended_action": "observe_and_label_candidates",
            "policy": {
                "today_strategy_sample_count": 0,
                "min_strategy_samples": 5,
                "strategy_sample_count": 8,
                "sample_collection_min_score": 0.55,
            },
        }

        context = decision_market_context(decision, target_trade_date="20260711", current_epoch_id=2)

        self.assertEqual(context["today_strategy_sample_count"], 0)
        self.assertEqual(context["sample_collection_min_score"], 0.55)

    def test_write_evolution_decision_persists_latest_and_log(self) -> None:
        portfolio = {
            "market": "ashare",
            "capital_epoch": 2,
            "trade_date": "20260710",
            "strategy_sample_count": 5,
            "today_strategy_sample_count": 1,
            "pnl": {"total_pnl": 120.0, "equity": 200120.0},
            "rankings": [{"style_name": "ashare_portfolio", "trades": 5, "pnl": 120.0}],
        }

        decision = write_evolution_decision(
            portfolio,
            review_dir=self.review_dir,
            min_strategy_samples=5,
        )

        latest = self.review_dir / "evolution_decision_latest.json"
        log = self.review_dir / "evolution_decision_log.jsonl"
        self.assertTrue(latest.exists())
        self.assertTrue(log.exists())
        self.assertEqual(json.loads(latest.read_text(encoding="utf-8"))["recommended_action"], decision["recommended_action"])
        self.assertEqual(len(log.read_text(encoding="utf-8").splitlines()), 1)

    def test_positive_mark_to_market_cannot_expand_risk_without_verified_evidence(self) -> None:
        portfolio = {
            "market": "ashare",
            "capital_epoch": 2,
            "trade_date": "20260710",
            "strategy_sample_count": 30,
            "today_strategy_sample_count": 1,
            "pnl": {"total_pnl": 1200.0, "realized_pnl": 800.0, "equity": 201200.0},
            "evolution_evidence": {
                "eligible_sample_count": 4,
                "realized_round_trip_count": 1,
                "forward_label_count": 4,
            },
        }

        decision = build_evolution_decision(
            portfolio,
            target_trade_date="20260710",
            min_strategy_samples=20,
        )

        self.assertEqual(decision["state"], "evidence_pending")
        self.assertEqual(decision["recommended_action"], "observe_and_label_candidates")
        self.assertIn("insufficient_verified_execution_evidence", decision["reasons"])

    def test_small_verified_sample_set_cannot_expand_risk(self) -> None:
        portfolio = {
            "market": "ashare",
            "capital_epoch": 2,
            "trade_date": "20260710",
            "strategy_sample_count": 24,
            "pnl": {"total_pnl": 1200.0, "realized_pnl": 800.0, "equity": 201200.0},
            "evolution_evidence": {
                "eligible_sample_count": 5,
                "realized_round_trip_count": 3,
                "forward_label_count": 5,
            },
        }

        decision = build_evolution_decision(
            portfolio,
            target_trade_date="20260710",
            min_strategy_samples=5,
        )

        self.assertEqual(decision["state"], "evidence_pending")
        self.assertEqual(decision["recommended_action"], "observe_and_label_candidates")
        self.assertEqual(decision["policy"]["min_evolution_evidence_samples"], 20)
        self.assertIn("insufficient_verified_execution_evidence", decision["reasons"])


if __name__ == "__main__":
    unittest.main()
