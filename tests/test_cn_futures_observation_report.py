from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from CNFutures import observation_report


class CNFuturesObservationReportTest(unittest.TestCase):
    def test_build_observation_report_summarizes_data_simulation_and_evolution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            review_root = root / "review"
            review_path = review_root / "data/cn_futures_sim_reviews.jsonl"
            review_path.parent.mkdir(parents=True)
            review_path.write_text(
                json.dumps(
                    {
                        "date": "20260706",
                        "state": "ok",
                        "record_count": 2,
                        "filled_count": 1,
                        "error_count": 0,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            cn_dir = review_root / "cn_futures"
            cn_dir.mkdir(parents=True)
            (cn_dir / "style_comparison.json").write_text(
                json.dumps(
                    {
                        "style_comparison": [
                            {"style_name": "index_intraday_directional", "win_rate": 0.7, "sharpe": 1.2, "pnl": 3.0},
                            {"style_name": "trend", "win_rate": 0.5, "sharpe": 0.9, "pnl": 2.0},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (cn_dir / "style_weights.json").write_text(
                json.dumps({"real_trading_enabled": False, "styles": {"index_intraday_directional": {"weight": 0.6}}}),
                encoding="utf-8",
            )
            (cn_dir / "evolution_plan.json").write_text(
                json.dumps(
                    {
                        "state": "adjusted",
                        "selection_objective": "win_rate_first_risk_adjusted",
                        "actions": [{"action": "promote"}],
                        "generated_variants": [{"style_name": "index_intraday_directional_g2_precision_20260706"}],
                    }
                ),
                encoding="utf-8",
            )
            live_report = {
                "generated_at": "2026-07-06T01:05:00+00:00",
                "overall_status": "pass",
                "observation_phase": "ready_to_observe",
                "alerts": [],
                "checks": [
                    {
                        "name": "sharedsignals_5min_freshness",
                        "details": {
                            "report": {
                                "status": "fresh",
                                "latest_bar_time": "2026-07-06 09:35:00",
                                "symbol_count": 8,
                                "total_bars": 160,
                                "session": {"current": "day", "in_session": True},
                            }
                        },
                    }
                ],
            }

            with patch.object(observation_report, "run_live_check", return_value=live_report):
                report = observation_report.build_observation_report(review_root=review_root, review_path=review_path)

            self.assertEqual(report["observation_phase"], "ready_to_observe")
            self.assertEqual(report["data"]["freshness_status"], "fresh")
            self.assertEqual(report["simulation"]["filled_count"], 1)
            self.assertEqual(report["styles"]["ranked"][0]["style_name"], "index_intraday_directional")
            self.assertEqual(report["evolution"]["action_count"], 1)
            self.assertEqual(report["dashboard"]["readiness"], "ready_to_observe")
            self.assertEqual(report["dashboard"]["primary_next_step"], "continue_observation")
            self.assertFalse(report["real_trading_enabled"])


if __name__ == "__main__":
    unittest.main()
