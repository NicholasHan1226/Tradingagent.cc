from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from Ashare.sample_target_monitor import (
    build_sample_target_monitor,
    write_sample_target_monitor,
)


CN_TZ = timezone(timedelta(hours=8))


class AshareSampleTargetMonitorTest(unittest.TestCase):
    def _review_dir(self, tmp: str) -> Path:
        path = Path(tmp) / "review" / "ashare"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _write_json(self, path: Path, payload: dict) -> None:
        path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")

    def test_marks_pass_when_daily_strategy_sample_target_is_met(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            review_dir = self._review_dir(tmp)
            self._write_json(
                review_dir / "portfolio_evolution_latest.json",
                {
                    "trade_date": "20260710",
                    "strategy_sample_count": 6,
                    "today_strategy_sample_count": 1,
                    "pnl": {"total_pnl": 120.0, "equity": 200120.0},
                },
            )
            self._write_json(
                review_dir / "evolution_decision_latest.json",
                {
                    "trade_date": "20260710",
                    "recommended_action": "observe",
                    "policy": {
                        "daily_strategy_sample_target": 1,
                        "today_strategy_sample_count": 1,
                    },
                },
            )

            report = build_sample_target_monitor(
                review_dir=review_dir,
                now=datetime(2026, 7, 10, 11, 45, tzinfo=CN_TZ),
            )

        self.assertEqual(report["overall_status"], "pass")
        self.assertEqual(report["state"], "target_met")
        self.assertEqual(report["recommended_action"], "observe")
        self.assertTrue(report["daily_target"]["target_met"])
        self.assertFalse(report["real_trading_enabled"])

    def test_writes_force_sample_collection_when_daily_target_not_met(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            review_dir = self._review_dir(tmp)
            self._write_json(
                review_dir / "portfolio_evolution_latest.json",
                {
                    "trade_date": "20260710",
                    "strategy_sample_count": 2,
                    "today_strategy_sample_count": 0,
                    "pnl": {"total_pnl": -20.0, "equity": 199980.0},
                },
            )
            self._write_json(
                review_dir / "evolution_decision_latest.json",
                {
                    "trade_date": "20260710",
                    "recommended_action": "force_sample_collection",
                    "policy": {
                        "daily_strategy_sample_target": 1,
                        "today_strategy_sample_count": 0,
                    },
                },
            )

            report = write_sample_target_monitor(
                review_dir=review_dir,
                now=datetime(2026, 7, 10, 14, 30, tzinfo=CN_TZ),
            )
            refreshed_decision = json.loads((review_dir / "evolution_decision_latest.json").read_text(encoding="utf-8"))
            latest_exists = (review_dir / "sample_target_monitor_latest.json").exists()
            log_exists = (review_dir / "sample_target_monitor_log.jsonl").exists()

        self.assertEqual(report["overall_status"], "warn")
        self.assertEqual(report["state"], "sample_debt")
        self.assertEqual(report["recommended_action"], "force_sample_collection")
        self.assertIn("daily_strategy_sample_target_not_met", report["reasons"])
        self.assertEqual(refreshed_decision["recommended_action"], "force_sample_collection")
        self.assertEqual(refreshed_decision["policy"]["today_strategy_sample_count"], 0)
        self.assertTrue(latest_exists)
        self.assertTrue(log_exists)

    def test_fails_after_final_checkpoint_when_daily_target_is_still_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            review_dir = self._review_dir(tmp)
            no_trade_log = Path(tmp) / "ashare_no_trade_explanations.jsonl"
            self._write_json(
                review_dir / "portfolio_evolution_latest.json",
                {
                    "trade_date": "20260710",
                    "strategy_sample_count": 2,
                    "today_strategy_sample_count": 0,
                    "pnl": {"total_pnl": 0.0, "equity": 200000.0},
                },
            )
            no_trade_log.write_text(
                json.dumps(
                    {
                        "trade_date": "20260710",
                        "no_trade_explanation": {
                            "category": "capital_plan_defensive",
                            "capital_plan_decision": "capital_plan_defensive_no_new_buy",
                            "counts": {"candidates": 2, "orders": 0, "risk_rejections": 1},
                        },
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            report = build_sample_target_monitor(
                review_dir=review_dir,
                no_trade_log_path=no_trade_log,
                now=datetime(2026, 7, 10, 15, 31, tzinfo=CN_TZ),
            )

        self.assertEqual(report["overall_status"], "fail")
        self.assertEqual(report["state"], "daily_target_missed")
        self.assertEqual(report["checkpoint"]["name"], "final")
        self.assertIn("capital_plan_defensive", report["blockers"])
        self.assertIn("risk_rejections_present", report["blockers"])
        self.assertEqual(report["recommended_action"], "force_sample_collection")
        self.assertFalse(report["writes_orders"])


if __name__ == "__main__":
    unittest.main()
