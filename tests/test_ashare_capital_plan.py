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

    # --- New tests for dynamic cash buffer with caps ---

    def test_balanced_mode_reserve_not_exceeding_50000_on_200k_account(self) -> None:
        """均衡模式下 200k 账户储备金不应超过 50000，避免因 30% 固定比例锁死 60000."""
        plan = plan_capital(
            [],
            200000.0,
            candidates=[
                {"ts_code": "600000.SH", "combined": 0.70},
                {"ts_code": "000001.SZ", "combined": 0.68},
            ],
            dynamic=True,
            market_context={
                "trend": "neutral",
                "risk_rejection_rate": 0.10,
                "data_issue_rate": 0.0,
                "recent_win_rate": 0.55,
            },
        )

        data = plan.to_dict()

        self.assertEqual(data["risk_mode"], "balanced")
        self.assertLessEqual(data["cash_reserve"], 50000.0,
                            "Balanced mode must cap cash reserve at 50000")
        # Reserve should be in 20-25% range, i.e. 40000-50000 on a 200k account
        self.assertGreaterEqual(data["cash_reserve"], 40000.0)
        self.assertLessEqual(data["cash_reserve_pct"], 0.25)
        self.assertGreaterEqual(data["cash_reserve_pct"], 0.20)

    def test_balanced_mode_reserve_capped_on_larger_account(self) -> None:
        """均衡模式在大账户上储备金仍应封顶 50000."""
        plan = plan_capital(
            [],
            500000.0,
            candidates=[
                {"ts_code": "600000.SH", "combined": 0.72},
                {"ts_code": "000001.SZ", "combined": 0.66},
            ],
            dynamic=True,
            total_capital=500000.0,
            market_context={
                "trend": "neutral",
                "risk_rejection_rate": 0.10,
                "data_issue_rate": 0.0,
                "recent_win_rate": 0.55,
            },
        )

        data = plan.to_dict()

        self.assertEqual(data["risk_mode"], "balanced")
        self.assertLessEqual(data["cash_reserve"], 50000.0,
                            "Balanced reserve cap must hold even on larger accounts")

    def test_aggressive_mode_reserve_in_15_to_20_pct_range(self) -> None:
        """强机会模式下储备金应在 15-20% 之间."""
        plan = plan_capital(
            [],
            200000.0,
            candidates=[
                {"ts_code": "600000.SH", "combined": 0.88},
                {"ts_code": "000001.SZ", "combined": 0.82},
                {"ts_code": "300750.SZ", "combined": 0.76},
            ],
            dynamic=True,
            market_context={
                "trend": "bullish",
                "risk_rejection_rate": 0.05,
                "data_issue_rate": 0.0,
                "recent_win_rate": 0.65,
            },
        )

        data = plan.to_dict()

        self.assertEqual(data["risk_mode"], "aggressive")
        self.assertGreaterEqual(data["cash_reserve_pct"], 0.15,
                                "Aggressive reserve should be at least 15%")
        self.assertLessEqual(data["cash_reserve_pct"], 0.20,
                            "Aggressive reserve should be at most 20%")

    def test_cautious_mode_has_explicit_reason_and_reserve_range(self) -> None:
        """谨慎模式下储备金 35-50% 且必须有明确原因."""
        plan = plan_capital(
            [],
            200000.0,
            candidates=[
                {"ts_code": "600000.SH", "combined": 0.60},
            ],
            dynamic=True,
            market_context={
                "trend": "neutral",
                "risk_rejection_rate": 0.20,
                "data_issue_rate": 0.0,
                "recent_win_rate": 0.52,
            },
        )

        data = plan.to_dict()

        self.assertEqual(data["risk_mode"], "cautious")
        self.assertGreaterEqual(data["cash_reserve_pct"], 0.35,
                                "Cautious reserve should be at least 35%")
        self.assertLessEqual(data["cash_reserve_pct"], 0.50,
                            "Cautious reserve should be at most 50%")
        self.assertIn("thin_candidate_quality", data["reasons"],
                      "Cautious mode must record explicit reason")

    def test_sample_collection_allows_probe_position_before_min_samples(self) -> None:
        plan = plan_capital(
            [
                {"ts_code": "300759.SZ", "value": 57589.0},
                {"ts_code": "600030.SH", "value": 58800.0},
            ],
            83461.87,
            candidates=[
                {"ts_code": "300418.SZ", "combined": 0.60},
            ],
            dynamic=True,
            market_context={
                "trend": "neutral",
                "risk_rejection_rate": 0.0,
                "data_issue_rate": 0.0,
                "recent_win_rate": 0.50,
                "strategy_sample_valid_count": 2,
                "min_strategy_samples": 5,
            },
        )

        data = plan.to_dict()

        self.assertEqual(data["risk_mode"], "sample_collection")
        self.assertEqual(data["target_positions"], 3)
        self.assertEqual(data["max_new_positions"], 1)
        self.assertEqual(len(data["suggested_buys"]), 1)
        self.assertGreaterEqual(data["suggested_buys"][0]["allocation"], 20000.0)
        self.assertLessEqual(data["suggested_buys"][0]["allocation"], 35000.0)
        self.assertIn("sample_collection_before_min_samples", data["reasons"])

    def test_daily_sample_hard_gate_forces_probe_before_daily_target(self) -> None:
        plan = plan_capital(
            [
                {"ts_code": "300759.SZ", "value": 57589.0},
                {"ts_code": "600030.SH", "value": 58800.0},
            ],
            83461.87,
            candidates=[
                {"ts_code": "300418.SZ", "combined": 0.60},
            ],
            dynamic=True,
            market_context={
                "trend": "neutral",
                "risk_rejection_rate": 0.0,
                "data_issue_rate": 0.0,
                "recent_win_rate": 0.50,
                "strategy_sample_valid_count": 8,
                "min_strategy_samples": 5,
                "daily_sample_hard_gate": True,
                "today_strategy_sample_count": 0,
                "daily_strategy_sample_target": 1,
                "sample_collection_min_score": 0.55,
            },
        )

        data = plan.to_dict()

        self.assertEqual(data["risk_mode"], "sample_collection")
        self.assertEqual(data["max_new_positions"], 1)
        self.assertIn("daily_strategy_sample_target_not_met", data["reasons"])

    def test_sample_collection_respects_max_probe_positions_when_empty(self) -> None:
        plan = plan_capital(
            [],
            200000.0,
            candidates=[
                {"ts_code": "600584.SH", "combined": 0.62},
                {"ts_code": "002371.SZ", "combined": 0.58},
                {"ts_code": "001309.SZ", "combined": 0.56},
            ],
            dynamic=True,
            market_context={
                "trend": "neutral",
                "risk_rejection_rate": 0.0,
                "data_issue_rate": 0.0,
                "recent_win_rate": 0.50,
                "strategy_sample_valid_count": 2,
                "min_strategy_samples": 5,
                "max_probe_positions": 1,
            },
        )

        data = plan.to_dict()

        self.assertEqual(data["risk_mode"], "sample_collection")
        self.assertEqual(data["target_positions"], 1)
        self.assertEqual(data["max_new_positions"], 1)
        self.assertEqual(len(data["suggested_buys"]), 1)

    def test_sample_collection_probe_budget_scales_with_candidate_quality(self) -> None:
        base_context = {
            "trend": "neutral",
            "risk_rejection_rate": 0.0,
            "data_issue_rate": 0.0,
            "recent_win_rate": 0.50,
            "strategy_sample_valid_count": 8,
            "min_strategy_samples": 5,
            "daily_sample_hard_gate": True,
            "today_strategy_sample_count": 0,
            "daily_strategy_sample_target": 1,
            "sample_collection_min_score": 0.55,
            "probe_allocation_min": 20000.0,
            "probe_allocation_max": 35000.0,
        }
        low = plan_capital(
            [
                {"ts_code": "300759.SZ", "value": 57589.0},
                {"ts_code": "600030.SH", "value": 58800.0},
            ],
            83461.87,
            candidates=[{"ts_code": "300418.SZ", "combined": 0.56}],
            dynamic=True,
            market_context=base_context,
        ).to_dict()
        high = plan_capital(
            [
                {"ts_code": "300759.SZ", "value": 57589.0},
                {"ts_code": "600030.SH", "value": 58800.0},
            ],
            83461.87,
            candidates=[{"ts_code": "600584.SH", "combined": 0.72}],
            dynamic=True,
            market_context=base_context,
        ).to_dict()

        self.assertEqual(low["risk_mode"], "sample_collection")
        self.assertEqual(high["risk_mode"], "sample_collection")
        self.assertLess(low["suggested_buys"][0]["allocation"], high["suggested_buys"][0]["allocation"])
        self.assertGreaterEqual(low["suggested_buys"][0]["allocation"], 20000.0)
        self.assertLessEqual(high["suggested_buys"][0]["allocation"], 35000.0)
        self.assertIn("dynamic_probe_budget", high)

    def test_weak_candidates_stay_full_cash_defensive(self) -> None:
        """弱候选/高风险场景仍应保留全现金防守逻辑（已有行为回归测试）."""
        plan = plan_capital(
            [],
            200000.0,
            candidates=[
                {"ts_code": "600000.SH", "combined": 0.48},
            ],
            dynamic=True,
            market_context={
                "trend": "bearish",
                "risk_rejection_rate": 0.65,
                "data_issue_rate": 0.10,
                "recent_win_rate": 0.40,
            },
        )

        data = plan.to_dict()

        self.assertEqual(data["risk_mode"], "defensive")
        self.assertEqual(data["target_positions"], 0)
        self.assertGreaterEqual(data["cash_reserve_pct"], 0.99,
                                "Defensive mode should be near full cash")
        self.assertEqual(data["suggested_buys"], [])


if __name__ == "__main__":
    unittest.main()
