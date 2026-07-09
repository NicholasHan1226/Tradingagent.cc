from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from Ashare.evolution_controller import build_evolution_decision, decision_market_context, write_evolution_decision


class AshareEvolutionControllerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.review_dir = Path(self.tmp.name)

    def test_sample_debt_forces_daily_sample_collection_policy(self) -> None:
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
            daily_strategy_sample_target=1,
            min_strategy_samples=5,
        )

        self.assertEqual(decision["state"], "sample_debt")
        self.assertEqual(decision["recommended_action"], "force_sample_collection")
        self.assertTrue(decision["policy"]["daily_sample_hard_gate"])
        self.assertEqual(decision["policy"]["daily_strategy_sample_target"], 1)
        self.assertEqual(decision["policy"]["min_strategy_samples"], 5)
        self.assertIn("daily_strategy_sample_target_not_met", decision["reasons"])
        self.assertFalse(decision["real_trading_enabled"])
        self.assertIn("candidate_layer_required", decision["guardrails"])

    def test_decision_market_context_exposes_capital_plan_inputs(self) -> None:
        decision = {
            "recommended_action": "force_sample_collection",
            "policy": {
                "daily_sample_hard_gate": True,
                "daily_strategy_sample_target": 1,
                "today_strategy_sample_count": 0,
                "min_strategy_samples": 5,
                "strategy_sample_count": 8,
                "sample_collection_min_score": 0.55,
            },
        }

        context = decision_market_context(decision)

        self.assertTrue(context["daily_sample_hard_gate"])
        self.assertEqual(context["daily_strategy_sample_target"], 1)
        self.assertEqual(context["today_strategy_sample_count"], 0)
        self.assertEqual(context["sample_collection_min_score"], 0.55)

    def test_write_evolution_decision_persists_latest_and_log(self) -> None:
        portfolio = {
            "market": "ashare",
            "trade_date": "20260710",
            "strategy_sample_count": 5,
            "today_strategy_sample_count": 1,
            "pnl": {"total_pnl": 120.0, "equity": 200120.0},
            "rankings": [{"style_name": "ashare_portfolio", "trades": 5, "pnl": 120.0}],
        }

        decision = write_evolution_decision(
            portfolio,
            review_dir=self.review_dir,
            daily_strategy_sample_target=1,
            min_strategy_samples=5,
        )

        latest = self.review_dir / "evolution_decision_latest.json"
        log = self.review_dir / "evolution_decision_log.jsonl"
        self.assertTrue(latest.exists())
        self.assertTrue(log.exists())
        self.assertEqual(json.loads(latest.read_text(encoding="utf-8"))["recommended_action"], decision["recommended_action"])
        self.assertEqual(len(log.read_text(encoding="utf-8").splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
