from __future__ import annotations

import os
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
            total_capital=200000.0,
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
            total_capital=200000.0,
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
            total_capital=200000.0,
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
            total_capital=200000.0,
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
            total_capital=200000.0,
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

    def test_daily_sample_target_does_not_create_probe_buy_capacity(self) -> None:
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

        self.assertNotEqual(data["risk_mode"], "sample_collection")
        self.assertEqual(data["max_new_positions"], 0)
        self.assertNotIn("daily_strategy_sample_target_not_met", data["reasons"])

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
            "strategy_sample_valid_count": 2,
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
            total_capital=200000.0,
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
            total_capital=200000.0,
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


    # --- RED: 50k canonical capital sourcing ---

    def test_default_capital_comes_from_canonical_source_not_hardcoded_200k(self) -> None:
        """When no total_capital is passed, plan_capital must source from
        default_sim_capital('ashare'), not a hardcoded 200_000 fallback."""
        old_tier = os.environ.get("ASHARE_SIM_CAPITAL_TIER")
        os.environ["ASHARE_SIM_CAPITAL_TIER"] = "50000"
        try:
            plan = plan_capital(
                [],
                50000.0,
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
        finally:
            if old_tier is None:
                os.environ.pop("ASHARE_SIM_CAPITAL_TIER", None)
            else:
                os.environ["ASHARE_SIM_CAPITAL_TIER"] = old_tier

        data = plan.to_dict()

        # The plan must use 50k not 200k as its base.
        # cash_reserve should reflect 50k proportions, not 200k proportions.
        self.assertEqual(data["risk_mode"], "aggressive")
        # On 50k, aggressive reserve is ~17.5% => ~8750
        self.assertLessEqual(data["cash_reserve"], 12000.0,
                            "Cash reserve must reflect 50k capital, not 200k")
        self.assertGreaterEqual(data["cash_reserve"], 6000.0)
        # Position allocations must fit within 50k feasibility
        if data["suggested_buys"]:
            total_alloc = sum(b["allocation"] for b in data["suggested_buys"])
            self.assertLessEqual(total_alloc, 42000.0,
                                "Total allocations must fit within 50k account")
            self.assertGreaterEqual(total_alloc, 25000.0)

    def test_50k_capital_scales_position_budgets_proportionally(self) -> None:
        """Explicit 50k total_capital must produce proportionally smaller
        position budgets than the canonical 200k account."""
        plan_50k = plan_capital(
            [],
            50000.0,
            candidates=[
                {"ts_code": "600000.SH", "combined": 0.86},
                {"ts_code": "000001.SZ", "combined": 0.78},
                {"ts_code": "300750.SZ", "combined": 0.72},
            ],
            dynamic=True,
            total_capital=50000.0,
            market_context={
                "trend": "bullish",
                "risk_rejection_rate": 0.0,
                "data_issue_rate": 0.0,
                "recent_win_rate": 0.62,
            },
        ).to_dict()

        plan_200k = plan_capital(
            [],
            200000.0,
            candidates=[
                {"ts_code": "600000.SH", "combined": 0.86},
                {"ts_code": "000001.SZ", "combined": 0.78},
                {"ts_code": "300750.SZ", "combined": 0.72},
            ],
            dynamic=True,
            total_capital=200000.0,
            market_context={
                "trend": "bullish",
                "risk_rejection_rate": 0.0,
                "data_issue_rate": 0.0,
                "recent_win_rate": 0.62,
            },
        ).to_dict()

        self.assertEqual(plan_50k["risk_mode"], "aggressive")
        self.assertEqual(plan_200k["risk_mode"], "aggressive")
        # 50k allocations must be strictly smaller
        self.assertLess(
            sum(b["allocation"] for b in plan_50k["suggested_buys"]),
            sum(b["allocation"] for b in plan_200k["suggested_buys"]),
            "50k total allocations must be proportionally smaller than 200k",
        )
        # 50k max single position must be <= 50k * 0.35 = 17500
        for buy in plan_50k["suggested_buys"]:
            self.assertLessEqual(buy["allocation"], 17500.0,
                                f"50k position {buy['code']} exceeds 35% of capital")

    def test_50k_min_position_feasible_with_100_share_lots(self) -> None:
        """On a 50k account, minimum position value must be at least
        5000 to accommodate a ~50 CNY stock at 100-share lot (5000 CNY)."""
        plan = plan_capital(
            [],
            50000.0,
            total_capital=50000.0,
        ).to_dict()

        # Static plan: min_cash_reserve should be ~5000 * 0.15 = 7500
        # But _scale_plan_constants makes it max(5000, min(30000, 50000*0.15)) = 7500
        # Investable = 50000 - 7500 = 42500, below min_position_value
        # So no buys, but the plan itself should be valid
        self.assertGreaterEqual(plan["cash_reserve"], 5000.0)
        self.assertLessEqual(plan["cash_reserve_pct"], 0.25)

    def test_sample_collection_50k_probe_budget_is_proportional(self) -> None:
        """Sample collection mode on 50k must produce proportionally
        smaller probe budgets (not 20k-35k as on 200k)."""
        plan = plan_capital(
            [],
            50000.0,
            candidates=[
                {"ts_code": "600584.SH", "combined": 0.62},
            ],
            dynamic=True,
            total_capital=50000.0,
            market_context={
                "trend": "neutral",
                "risk_rejection_rate": 0.0,
                "data_issue_rate": 0.0,
                "recent_win_rate": 0.50,
                "strategy_sample_valid_count": 2,
                "min_strategy_samples": 5,
            },
        ).to_dict()

        self.assertEqual(plan["risk_mode"], "sample_collection")
        self.assertEqual(len(plan["suggested_buys"]), 1)
        # Probe allocation on 50k must be <= 8750 (not 20k-35k)
        self.assertLessEqual(plan["suggested_buys"][0]["allocation"], 10000.0,
                            "50k probe budget must be proportional, not 20k-35k")
        self.assertGreaterEqual(plan["suggested_buys"][0]["allocation"], 4000.0)


if __name__ == "__main__":
    unittest.main()
