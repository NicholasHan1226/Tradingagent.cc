from __future__ import annotations

import unittest

from Ashare.capital_plan import plan_capital


class AshareCapitalPlanTest(unittest.TestCase):
    def test_dynamic_plan_expands_to_three_positions_for_strong_candidates(self) -> None:
        plan = plan_capital(
            [],
            200000.0,
            candidates=[
                {"ts_code": "600000.SH", "combined": 0.86},
                {"ts_code": "000001.SZ", "combined": 0.78},
                {"ts_code": "300750.SZ", "combined": 0.72},
            ],
            dynamic=True,
            market_context={
                "trend": "bullish",
                "risk_rejection_rate": 0.0,
                "data_issue_rate": 0.0,
                "recent_win_rate": 0.62,
            },
        )

        data = plan.to_dict()

        self.assertEqual(data["risk_mode"], "aggressive")
        self.assertEqual(data["target_positions"], 3)
        self.assertEqual(data["max_new_positions"], 3)
        self.assertLessEqual(data["cash_reserve_pct"], 0.25)
        self.assertEqual(len(data["suggested_buys"]), 3)
        self.assertTrue(all(50000 <= row["allocation"] <= 70000 for row in data["suggested_buys"]))

    def test_dynamic_plan_moves_to_cash_when_candidates_are_weak(self) -> None:
        plan = plan_capital(
            [],
            200000.0,
            candidates=[
                {"ts_code": "600000.SH", "combined": 0.52},
                {"ts_code": "000001.SZ", "combined": 0.50},
            ],
            dynamic=True,
            market_context={
                "trend": "bearish",
                "risk_rejection_rate": 0.75,
                "data_issue_rate": 0.0,
                "recent_win_rate": 0.42,
            },
        )

        data = plan.to_dict()

        self.assertEqual(data["risk_mode"], "defensive")
        self.assertEqual(data["target_positions"], 0)
        self.assertEqual(data["max_new_positions"], 0)
        self.assertGreaterEqual(data["cash_reserve_pct"], 0.50)
        self.assertEqual(data["suggested_buys"], [])
        self.assertIn("weak_candidate_quality", data["reasons"])

    def test_duplicate_lot_rows_count_as_one_existing_position(self) -> None:
        plan = plan_capital(
            [
                {"ts_code": "600000.SH", "value": 10000.0},
                {"ts_code": "600000.SH", "value": 15000.0},
            ],
            175000.0,
            candidates=[
                {"ts_code": "000001.SZ", "combined": 0.80},
                {"ts_code": "300750.SZ", "combined": 0.76},
            ],
            dynamic=True,
            market_context={"trend": "bullish", "risk_rejection_rate": 0.0, "data_issue_rate": 0.0},
        )

        data = plan.to_dict()

        self.assertEqual(data["target_positions"], 3)
        self.assertEqual(data["max_new_positions"], 2)
        self.assertEqual(len(data["suggested_buys"]), 2)


if __name__ == "__main__":
    unittest.main()
