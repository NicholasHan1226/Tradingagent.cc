from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from Ashare.sample_learning import (
    build_hypothesis_id,
    build_sample_learning_report,
    write_sample_learning_report,
)


class AshareSampleLearningTest(unittest.TestCase):
    def _write_json(self, path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _write_jsonl(self, path: Path, rows: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")

    def test_build_hypothesis_id_is_stable_and_human_readable(self) -> None:
        hypothesis_id = build_hypothesis_id(
            trade_date="20260710",
            symbol="600584.SH",
            side="buy",
            execution_source="ashare_candidate_layer",
            candidate_pool_layer="candidate",
            score=0.6118,
        )

        self.assertEqual(hypothesis_id, "ashare-20260710-buy-600584.SH-candidate-s061")

    def test_sample_learning_report_combines_quality_attribution_accounts_and_factors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            review_dir = root / "shared" / "review" / "ashare"
            trades_path = root / "shared" / "logs" / "local_sim" / "local_sim_trades.jsonl"
            no_trade_path = root / "shared" / "logs" / "ashare_no_trade_explanations.jsonl"
            hypothesis_id = "ashare-20260710-buy-600584.SH-candidate-s061"
            self._write_jsonl(
                trades_path,
                [
                    {
                        "trade_id": "T1",
                        "order_id": "O1",
                        "market": "ashare",
                        "account": "ashare_sim",
                        "trade_date": "20260710",
                        "ts_code": "600584.SH",
                        "side": "buy",
                        "quantity": 300,
                        "filled_price": 94.11,
                        "capital_layer": "simulated",
                        "candidate_pool_layer": "candidate",
                        "execution_source": "ashare_candidate_layer",
                        "fill_price_source_class": "market_data",
                        "fill_price_source": "market_snapshot.ask_price",
                        "trade_timestamp_bj": "2026-07-10T10:01:00+08:00",
                        "hypothesis_id": hypothesis_id,
                        "research_hypothesis": {
                            "hypothesis_id": hypothesis_id,
                            "factor_snapshot": {
                                "combined": 0.6118,
                                "capital": 1.0,
                                "technical": 1.0,
                                "fundamental": 0.4511,
                            },
                        },
                    },
                    {
                        "trade_id": "T2",
                        "order_id": "O2",
                        "market": "ashare",
                        "account": "ashare_sim",
                        "trade_date": "20260710",
                        "ts_code": "000001.SZ",
                        "side": "buy",
                        "quantity": 100,
                        "filled_price": 10.0,
                        "capital_layer": "simulated",
                        "candidate_pool_layer": "watch",
                        "execution_source": "manual_test",
                        "trade_timestamp_bj": "2026-07-10T15:20:00+08:00",
                    },
                ],
            )
            self._write_json(
                review_dir / "forward_validation_latest.json",
                {
                    "labels": [
                        {
                            "trade_id": "T1",
                            "status": "labeled",
                            "labels": {
                                "close": {"status": "labeled", "return_pct": 0.012},
                                "next_day": {"status": "pending"},
                            },
                        }
                    ]
                },
            )
            self._write_json(
                review_dir / "sample_target_monitor_latest.json",
                {
                    "trade_date": "20260710",
                    "overall_status": "warn",
                    "state": "sample_debt",
                    "blockers": ["capital_plan_defensive"],
                    "daily_target": {"target": 1, "today_strategy_sample_count": 0},
                },
            )
            self._write_json(
                review_dir / "tier_experiments_latest.json",
                {
                    "accounts": [
                        {"account": "ashare_50000", "capital": 50000, "trade_count": 1, "pnl": {"total_pnl": 12.0}},
                        {"account": "ashare_100000", "capital": 100000, "trade_count": 1, "pnl": {"total_pnl": -5.0}},
                    ]
                },
            )
            self._write_jsonl(
                no_trade_path,
                [
                    {
                        "trade_date": "20260710",
                        "no_trade_explanation": {
                            "category": "capital_plan_defensive",
                            "capital_plan_decision": {"risk_mode": "defensive", "position_capacity": 0},
                            "counts": {"candidates": 2, "orders": 0, "risk_rejections": 1},
                        },
                    }
                ],
            )

            report = build_sample_learning_report(
                trade_date="20260710",
                review_dir=review_dir,
                local_trades_path=trades_path,
                no_trade_log_path=no_trade_path,
                min_factor_samples=3,
            )
            written = write_sample_learning_report(
                trade_date="20260710",
                review_dir=review_dir,
                local_trades_path=trades_path,
                no_trade_log_path=no_trade_path,
                min_factor_samples=3,
            )
            latest_exists = (review_dir / "sample_learning_latest.json").exists()

        self.assertEqual(report["overall_status"], "warn")
        self.assertEqual(report["sample_quality"]["tier_counts"]["high_quality_strategy_sample"], 1)
        self.assertEqual(report["sample_quality"]["tier_counts"]["chain_validation_sample"], 1)
        self.assertEqual(report["hypothesis_registry"]["hypothesis_count"], 1)
        self.assertEqual(report["postclose_attribution"]["primary_blocker"], "capital_plan_defensive")
        self.assertGreater(report["dynamic_probe_budget"]["recommended_allocation"], 20000.0)
        self.assertEqual(report["account_objectives"]["ashare_50000"]["primary_goal"], "capital_efficiency")
        self.assertEqual(report["account_objectives"]["ashare_200000"]["primary_goal"], "drawdown_controlled_growth")
        self.assertEqual(report["factor_research"]["status"], "sample_debt")
        self.assertIn("combined", report["factor_research"]["factors"])
        self.assertFalse(report["writes_orders"])
        self.assertTrue(latest_exists)
        self.assertEqual(written["latest_path"], str(review_dir / "sample_learning_latest.json"))


if __name__ == "__main__":
    unittest.main()
