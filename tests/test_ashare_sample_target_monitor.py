from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from Ashare.sample_target_monitor import (
    build_sample_target_monitor,
    write_sample_target_monitor,
)


CN_TZ = timezone(timedelta(hours=8))
EPOCH_STATE = {
    "current_epoch_id": 2,
    "capital_cny": 50_000.0,
    "cutover_timestamp": "2026-07-10T20:56:58+00:00",
}


class AshareSampleTargetMonitorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.epoch_patch = patch(
            "Ashare.sample_target_monitor.read_epoch_state", return_value=EPOCH_STATE
        )
        self.epoch_patch.start()
        self.addCleanup(self.epoch_patch.stop)

    def _review_dir(self, tmp: str) -> Path:
        path = Path(tmp) / "review" / "ashare"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _write_json(self, path: Path, payload: dict) -> None:
        payload.setdefault("capital_epoch", 2)
        payload.setdefault("capital_cny", 50_000.0)
        payload.setdefault("epoch_cutover_timestamp", EPOCH_STATE["cutover_timestamp"])
        payload.setdefault("generated_at", "2026-07-11T03:00:00+00:00")
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
                    "policy": {"today_strategy_sample_count": 1},
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

    def test_missing_daily_fill_records_observation_gap_without_forcing_trade(self) -> None:
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
                    "recommended_action": "observe_and_label_candidates",
                    "policy": {"today_strategy_sample_count": 0},
                },
            )

            report = write_sample_target_monitor(
                review_dir=review_dir,
                now=datetime(2026, 7, 10, 14, 30, tzinfo=CN_TZ),
            )
            refreshed_decision = json.loads((review_dir / "evolution_decision_latest.json").read_text(encoding="utf-8"))
            latest_exists = (review_dir / "sample_target_monitor_latest.json").exists()
            log_exists = (review_dir / "sample_target_monitor_log.jsonl").exists()

        self.assertEqual(report["overall_status"], "pass")
        self.assertEqual(report["state"], "observation_gap")
        self.assertEqual(report["recommended_action"], "observe_and_label_candidates")
        self.assertIn("daily_trade_target_removed", report["reasons"])
        self.assertEqual(refreshed_decision["recommended_action"], "observe_and_label_candidates")
        self.assertEqual(refreshed_decision["policy"]["today_strategy_sample_count"], 0)
        self.assertTrue(latest_exists)
        self.assertTrue(log_exists)

    def test_final_checkpoint_without_fill_stays_observation_only(self) -> None:
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

        self.assertEqual(report["overall_status"], "pass")
        self.assertEqual(report["state"], "observation_gap")
        self.assertEqual(report["checkpoint"]["name"], "final")
        self.assertIn("capital_plan_defensive", report["blockers"])
        self.assertIn("risk_rejections_present", report["blockers"])
        self.assertEqual(report["recommended_action"], "observe_and_label_candidates")
        self.assertFalse(report["writes_orders"])

    def test_rejects_old_epoch_inputs_and_writes_current_epoch_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            review_dir = self._review_dir(tmp)
            old = {
                "capital_epoch": 1,
                "capital_cny": 200_000.0,
                "epoch_cutover_timestamp": "2026-07-01T00:00:00+00:00",
                "generated_at": "2026-07-10T08:00:00+00:00",
                "trade_date": "20260711",
                "strategy_sample_count": 99,
                "today_strategy_sample_count": 9,
                "recommended_action": "expand_risk_candidate",
            }
            self._write_json(review_dir / "portfolio_evolution_latest.json", old)
            self._write_json(review_dir / "evolution_decision_latest.json", old)

            with patch("Ashare.sample_target_monitor.read_epoch_state", return_value=EPOCH_STATE):
                report = write_sample_target_monitor(
                    review_dir=review_dir,
                    now=datetime(2026, 7, 11, 11, 45, tzinfo=CN_TZ),
                )

            persisted = json.loads(
                (review_dir / "sample_target_monitor_latest.json").read_text(encoding="utf-8")
            )

        self.assertEqual(report["capital_epoch"], 2)
        self.assertEqual(report["capital_cny"], 50_000.0)
        self.assertEqual(report["epoch_cutover_timestamp"], EPOCH_STATE["cutover_timestamp"])
        self.assertEqual(report["daily_target"]["strategy_sample_count"], 0)
        self.assertIn("portfolio_evolution_capital_epoch_mismatch", report["blockers"])
        self.assertIn("evolution_decision_capital_epoch_mismatch", report["blockers"])
        self.assertEqual(persisted["capital_epoch"], 2)

    def test_rejects_matching_epoch_with_wrong_capital(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            review_dir = self._review_dir(tmp)
            payload = {
                "capital_epoch": 2,
                "capital_cny": 200_000.0,
                "epoch_cutover_timestamp": EPOCH_STATE["cutover_timestamp"],
                "generated_at": "2026-07-11T03:00:00+00:00",
                "trade_date": "20260711",
                "strategy_sample_count": 99,
                "today_strategy_sample_count": 9,
            }
            self._write_json(review_dir / "portfolio_evolution_latest.json", payload)
            self._write_json(review_dir / "evolution_decision_latest.json", {**payload, "capital_cny": 50_000.0})

            with patch("Ashare.sample_target_monitor.read_epoch_state", return_value=EPOCH_STATE):
                report = build_sample_target_monitor(
                    review_dir=review_dir,
                    now=datetime(2026, 7, 11, 11, 45, tzinfo=CN_TZ),
                )

        self.assertIn("portfolio_evolution_capital_cny_mismatch", report["blockers"])
        self.assertEqual(report["daily_target"]["strategy_sample_count"], 0)


if __name__ == "__main__":
    unittest.main()
