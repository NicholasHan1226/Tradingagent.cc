import json
import tempfile
import unittest
from pathlib import Path

from shared.review import benchmark
from shared.review import daily_review, monthly_review, weekly_review


class ReviewCapitalLayerTest(unittest.TestCase):
    def test_daily_close_groups_pnl_by_capital_layer_and_normalizes_paper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            daily_review.DAILY_LOG = tmpdir / "daily_reviews.jsonl"
            benchmark.LAST_PERIOD_STORE = tmpdir / "last_period_return.json"
            benchmark.BENCHMARK_STORE = tmpdir / "benchmark_history.json"

            trades = [
                {"ts_code": "000001.SZ", "pnl": 100.0, "capital_layer": "real", "strategy": "trend"},
                {"ts_code": "000002.SZ", "pnl": 50.0, "capital_layer": "paper", "strategy": "pullback"},
                {"ts_code": "000003.SZ", "pnl": -20.0, "capital_layer": "sim", "strategy": "event"},
            ]
            positions = [
                {"ts_code": "000001.SZ", "weight": 0.5, "pnl_pct": 0.02, "capital_layer": "real"},
                {"ts_code": "000002.SZ", "weight": 0.2, "pnl_pct": 0.05, "capital_layer": "paper"},
                {"ts_code": "000003.SZ", "weight": 0.1, "pnl_pct": -0.10, "capital_layer": "simulated"},
            ]

            result = daily_review.review_close(trades, positions, benchmark_return=0.0)

            self.assertEqual(set(result["capital_layer_reviews"]), {"real", "shadow", "simulated"})
            self.assertAlmostEqual(result["capital_layer_reviews"]["real"]["pnl"], 100.01)
            self.assertAlmostEqual(result["capital_layer_reviews"]["shadow"]["pnl"], 50.01)
            self.assertAlmostEqual(result["capital_layer_reviews"]["simulated"]["pnl"], -20.01)

            rows = [json.loads(line) for line in daily_review.DAILY_LOG.read_text(encoding="utf-8").splitlines()]
            self.assertEqual({row["capital_layer"] for row in rows}, {"real", "shadow", "simulated"})
            self.assertTrue(all("capital_layer" in row for row in rows))

    def test_weekly_review_separates_strategy_stats_by_capital_layer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            weekly_review.WEEKLY_LOG = tmpdir / "weekly_reviews.jsonl"
            weekly_review.WEEKLY_STATE = tmpdir / "weekly_state.json"

            trades = [
                {"pnl": 10.0, "strategy": "trend", "capital_layer": "real", "dimension": "technical", "condition": "low_vol"},
                {"pnl": -5.0, "strategy": "trend", "capital_layer": "real", "dimension": "technical", "condition": "low_vol"},
                {"pnl": 7.0, "strategy": "trend", "capital_layer": "paper", "dimension": "macro", "condition": "mid_vol"},
            ]

            result = weekly_review.review_week(trades, strategies=["trend"])

            self.assertEqual(result["capital_layer_reviews"]["real"]["week_trade_count"], 2)
            self.assertEqual(result["capital_layer_reviews"]["shadow"]["week_trade_count"], 1)
            self.assertAlmostEqual(result["capital_layer_reviews"]["real"]["week_pnl"], 5.0)
            self.assertAlmostEqual(result["capital_layer_reviews"]["shadow"]["week_pnl"], 7.0)

            rows = [json.loads(line) for line in weekly_review.WEEKLY_LOG.read_text(encoding="utf-8").splitlines()]
            self.assertEqual({row["capital_layer"] for row in rows}, {"real", "shadow"})
            self.assertEqual(rows[0]["strategy_win_rates"]["trend"]["trades"] + rows[1]["strategy_win_rates"]["trend"]["trades"], 3)

    def test_weekly_review_excludes_after_hours_ashare_sim_strategy_sample(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            weekly_review.WEEKLY_LOG = tmpdir / "weekly_reviews.jsonl"
            weekly_review.WEEKLY_STATE = tmpdir / "weekly_state.json"

            trades = [
                {
                    "market": "ashare",
                    "capital_layer": "simulated",
                    "side": "buy",
                    "strategy": "trend",
                    "pnl": 10.0,
                    "candidate_pool_layer": "candidate",
                    "execution_source": "ashare_candidate_layer",
                    "created_at": "2026-07-07T08:26:30+00:00",
                }
            ]

            result = weekly_review.review_week(trades, strategies=["trend"])

            simulated = result["capital_layer_reviews"]["simulated"]
            self.assertEqual(simulated["week_trade_count"], 0)
            self.assertEqual(simulated["strategy_win_rates"]["trend"]["trades"], 0)
            self.assertEqual(simulated["week_pnl"], 0)

    def test_monthly_review_keeps_real_and_shadow_reports_separate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            monthly_review.MONTHLY_LOG = tmpdir / "monthly_reviews.jsonl"

            month_data = {
                "month": "2026-06",
                "trades": [
                    {"pnl": 0.10, "capital_layer": "real", "dimension": "technical", "strategy": "trend", "condition": "breakout"},
                    {"pnl": -0.03, "capital_layer": "paper", "dimension": "event", "strategy": "event_driven", "condition": "high_vol"},
                ],
                "pipeline": {
                    "screening": {"runs": 1, "errors": 0},
                    "adversarial": {"runs": 1, "errors": 0},
                    "risk": {"runs": 1, "errors": 0},
                    "portfolio": {"runs": 1, "errors": 0},
                    "execution": {"runs": 1, "errors": 0},
                    "review": {"runs": 1, "errors": 0},
                    "accounting": {"runs": 1, "errors": 0},
                },
            }

            result = monthly_review.review_month(month_data)

            self.assertAlmostEqual(result["capital_layer_reviews"]["real"]["month_pnl"], 0.10)
            self.assertAlmostEqual(result["capital_layer_reviews"]["shadow"]["month_pnl"], -0.03)
            self.assertEqual(result["capital_layer_reviews"]["shadow"]["capital_layer"], "shadow")

            rows = [json.loads(line) for line in monthly_review.MONTHLY_LOG.read_text(encoding="utf-8").splitlines()]
            self.assertEqual({row["capital_layer"] for row in rows}, {"real", "shadow"})
            self.assertTrue(all("capital_layer" in row for row in rows))


if __name__ == "__main__":
    unittest.main()
