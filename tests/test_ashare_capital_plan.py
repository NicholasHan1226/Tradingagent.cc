from __future__ import annotations

import inspect
import unittest

import Ashare.capital_plan as capital_plan_module
from Ashare.capital_plan import plan_capital, suggest_reverse_repo
from shared.capital.market_policy import MarketPolicy
from shared.risk.patrol import patrol


def _candidates(count: int) -> list[dict[str, object]]:
    return [
        {
            "ts_code": f"{index:06d}.SZ",
            "combined": round(0.90 - index * 0.01, 4),
            "data_qualified": True,
            "execution_eligible": True,
        }
        for index in range(1, count + 1)
    ]


class AshareCapitalAuthorityTest(unittest.TestCase):
    def test_plan_imports_only_the_per_market_authority(self) -> None:
        source = inspect.getsource(capital_plan_module)

        self.assertIn('MarketPolicy.load("ashare")', source)
        self.assertNotIn("from shared.capital.policy", source)
        self.assertNotIn("default_sim_capital", source)

    def test_policy_identity_and_sim_only_boundary_are_auditable(self) -> None:
        policy = MarketPolicy.load("ashare")
        data = plan_capital([], 50_000.0).to_dict()

        self.assertEqual(data["capital_authority_id"], policy.capital_authority_id)
        self.assertEqual(data["authority_generation"], policy.authority_generation)
        self.assertEqual(data["cutover_state"], "fresh_start_approved")
        self.assertEqual(data["initial_equity_cny"], 50_000.0)
        self.assertEqual(data["capital_layer"], "simulated")
        self.assertFalse(data["real_trading_enabled"])

    def test_historical_total_capital_argument_cannot_mint_capacity(self) -> None:
        data = plan_capital(
            [],
            200_000.0,
            candidates=_candidates(8),
            dynamic=True,
            total_capital=200_000.0,
        ).to_dict()

        self.assertEqual(data["initial_equity_cny"], 50_000.0)
        self.assertEqual(data["stock_exposure_limit_cny"], 45_000.0)
        self.assertLessEqual(
            sum(row["allocation"] for row in data["suggested_buys"]), 45_000.0
        )
        self.assertTrue(
            all(row["allocation"] <= 7_500.0 for row in data["suggested_buys"])
        )
        self.assertIn("noncanonical_total_capital_ignored", data["notes"][0])


class AshareCapitalCapacityTest(unittest.TestCase):
    def test_seven_distinct_candidates_fit_the_50k_account(self) -> None:
        data = plan_capital(
            [], 50_000.0, candidates=_candidates(7), dynamic=True
        ).to_dict()

        self.assertEqual(data["stock_exposure_limit_cny"], 45_000.0)
        self.assertEqual(data["position_capacity"], 8)
        self.assertEqual(data["remaining_position_slots"], 8)
        self.assertEqual(len(data["suggested_buys"]), 7)
        self.assertLessEqual(
            sum(row["allocation"] for row in data["suggested_buys"]), 45_000.0
        )
        self.assertTrue(
            all(row["allocation"] <= 7_500.0 for row in data["suggested_buys"])
        )

    def test_eight_candidates_share_the_45k_worst_case_budget(self) -> None:
        data = plan_capital(
            [], 50_000.0, candidates=_candidates(8), dynamic=True
        ).to_dict()

        self.assertEqual(len(data["suggested_buys"]), 8)
        self.assertEqual(
            sum(row["allocation"] for row in data["suggested_buys"]), 45_000.0
        )
        self.assertEqual(data["planned_stock_exposure_cny"], 45_000.0)
        self.assertEqual(data["planned_stock_utilization_rate"], 0.90)

    def test_ninth_distinct_candidate_is_rejected_by_capacity(self) -> None:
        data = plan_capital(
            [], 50_000.0, candidates=_candidates(9), dynamic=True
        ).to_dict()

        self.assertEqual(len(data["suggested_buys"]), 8)
        self.assertEqual(data["execution_eligible_candidate_count"], 9)
        rejected = {row["symbol"]: row["code"] for row in data["candidate_rejections"]}
        self.assertEqual(rejected["000009.SZ"], "position_capacity_reached")

    def test_duplicate_position_rows_count_as_one_slot_but_sum_market_value(
        self,
    ) -> None:
        data = plan_capital(
            [
                {"ts_code": "000001.SZ", "market_value": 2_000.0},
                {"ts_code": "000001.SZ", "market_value": 3_000.0},
            ],
            45_000.0,
            candidates=_candidates(2),
            dynamic=True,
        ).to_dict()

        self.assertEqual(data["existing_position_count"], 1)
        self.assertEqual(data["remaining_position_slots"], 7)
        self.assertEqual(data["deployed_market_value_cny"], 5_000.0)

    def test_symbol_cap_aggregates_position_pending_and_new_order(self) -> None:
        data = plan_capital(
            [{"ts_code": "000001.SZ", "market_value": 6_500.0}],
            43_500.0,
            candidates=[
                {
                    "ts_code": "000001.SZ",
                    "combined": 0.90,
                    "requested_budget_cny": 2_000.0,
                },
                {
                    "ts_code": "000002.SZ",
                    "combined": 0.89,
                    "requested_budget_cny": 6_000.0,
                },
            ],
            dynamic=True,
            market_context={
                "pending_buy_reservations": [
                    {"symbol": "000001.SZ", "reserved_cny": 1_000.0}
                ]
            },
        ).to_dict()

        self.assertNotIn("000001.SZ", data["position_budget_by_symbol"])
        self.assertEqual(data["position_budget_by_symbol"]["000002.SZ"], 6_000.0)
        rejected = {row["symbol"]: row["code"] for row in data["candidate_rejections"]}
        self.assertEqual(rejected["000001.SZ"], "single_name_aggregate_limit_reached")

    def test_rejected_candidate_does_not_prevent_a_later_substitute(self) -> None:
        data = plan_capital(
            [],
            50_000.0,
            candidates=[
                {"ts_code": "000001.SZ", "data_qualified": False},
                {"ts_code": "000002.SZ", "execution_eligible": False},
                {"ts_code": "000003.SZ", "requested_budget_cny": 5_000.0},
            ],
            dynamic=True,
        ).to_dict()

        self.assertEqual(data["qualified_candidate_count"], 2)
        self.assertEqual(data["execution_eligible_candidate_count"], 1)
        self.assertEqual(list(data["position_budget_by_symbol"]), ["000003.SZ"])

    def test_portfolio_remaining_budget_is_enforced_after_current_commitments(
        self,
    ) -> None:
        holdings = [
            {"ts_code": f"00001{index}.SZ", "market_value": 7_500.0}
            for index in range(5)
        ]
        holdings.append({"ts_code": "000019.SZ", "market_value": 2_500.0})
        data = plan_capital(
            holdings,
            10_000.0,
            candidates=[
                {"ts_code": "600001.SH", "requested_budget_cny": 7_500.0},
                {"ts_code": "600002.SH", "requested_budget_cny": 7_500.0},
            ],
            dynamic=True,
        ).to_dict()

        self.assertLessEqual(sum(data["position_budget_by_symbol"].values()), 5_000.0)
        self.assertLessEqual(data["planned_stock_exposure_cny"], 45_000.0)

    def test_reaching_45k_stock_exposure_zeros_new_buy_capacity(self) -> None:
        holdings = [
            {"ts_code": f"00001{index}.SZ", "market_value": 7_500.0}
            for index in range(6)
        ]
        data = plan_capital(
            holdings,
            5_000.0,
            candidates=[{"ts_code": "600001.SH", "requested_budget_cny": 1_000.0}],
            dynamic=True,
        ).to_dict()

        self.assertEqual(data["max_new_positions"], 0)
        self.assertEqual(
            data["capacity_reason"], "portfolio_stock_exposure_limit_reached"
        )
        self.assertEqual(data["suggested_buys"], [])

    def test_candidate_budget_is_not_padded_to_force_deployment(self) -> None:
        data = plan_capital(
            [],
            50_000.0,
            candidates=[
                {"ts_code": "000001.SZ", "requested_budget_cny": 1_500.0},
                {"ts_code": "000002.SZ", "requested_budget_cny": 2_000.0},
            ],
            dynamic=True,
        ).to_dict()

        self.assertEqual(sum(data["position_budget_by_symbol"].values()), 3_500.0)
        self.assertIn(
            "quality_or_candidate_budget_not_forced",
            {row["code"] for row in data["undeployed_reasons"]},
        )


class AshareCapitalUtilizationTest(unittest.TestCase):
    def test_no_fixed_reserve_only_explicit_operating_cash(self) -> None:
        baseline = plan_capital([], 50_000.0, candidates=[], dynamic=True).to_dict()
        explicit = plan_capital(
            [],
            50_000.0,
            candidates=[],
            dynamic=True,
            market_context={
                "frozen_cash_cny": 1_200.0,
                "expected_execution_cost_buffer_cny": 300.0,
                "lot_rounding_cash_cny": 80.0,
            },
        ).to_dict()

        self.assertEqual(baseline["cash_reserve"], 0.0)
        self.assertEqual(explicit["cash_reserve"], 1_580.0)
        self.assertEqual(explicit["dynamic_operating_cash_cny"], 1_580.0)
        self.assertEqual(explicit["deployable_cash_cny"], 45_000.0)

    def test_current_utilization_does_not_count_unreserved_suggestions(self) -> None:
        data = plan_capital(
            [], 50_000.0, candidates=_candidates(8), dynamic=True
        ).to_dict()

        self.assertEqual(data["deployed_utilization_rate"], 0.0)
        self.assertEqual(data["committed_utilization_rate"], 0.0)
        self.assertEqual(data["undeployed_capital_cny"], 50_000.0)
        self.assertEqual(data["planned_stock_utilization_rate"], 0.90)
        self.assertIn(
            "planned_not_reserved", {row["code"] for row in data["undeployed_reasons"]}
        )

    def test_pending_reservations_are_committed_but_not_deployed(self) -> None:
        data = plan_capital(
            [],
            45_000.0,
            candidates=[],
            market_context={"pending_buy_reserved_cny": 5_000.0},
        ).to_dict()

        self.assertEqual(data["deployed_market_value_cny"], 0.0)
        self.assertEqual(data["pending_buy_reserved_cny"], 5_000.0)
        self.assertEqual(data["committed_stock_exposure_cny"], 5_000.0)
        self.assertEqual(data["committed_utilization_rate"], 0.10)

    def test_undeployed_reasons_are_structured_and_observation_is_not_suppressed(
        self,
    ) -> None:
        data = plan_capital(
            [],
            50_000.0,
            candidates=[{"ts_code": "000001.SZ", "data_qualified": False}],
            dynamic=True,
        ).to_dict()

        self.assertEqual(data["qualified_candidate_count"], 0)
        self.assertEqual(data["execution_eligible_candidate_count"], 0)
        self.assertEqual(data["sample_intent"], "observation")
        for reason in data["undeployed_reasons"]:
            self.assertEqual(set(reason), {"code", "amount_cny", "details"})
        self.assertIn(
            "no_execution_eligible_candidates",
            {row["code"] for row in data["undeployed_reasons"]},
        )

    def test_cash_management_is_separate_suggestion_only_attribution(self) -> None:
        data = plan_capital([], 50_000.0, candidates=[], dynamic=True).to_dict()
        cash = data["cash_management"]

        self.assertFalse(cash["auto_order"])
        self.assertTrue(cash["excluded_from_stock_alpha"])
        self.assertEqual(cash["attribution_bucket"], "cash_management_yield")
        self.assertEqual(cash["suggestion"]["status"], "suggestion_only")
        self.assertFalse(cash["suggestion"]["auto_order"])

    def test_reverse_repo_helper_never_creates_an_order(self) -> None:
        suggestion = suggest_reverse_repo(5_999.0)

        self.assertEqual(suggestion["amount"], 5_000.0)
        self.assertEqual(suggestion["action"], "suggest_lend")
        self.assertFalse(suggestion["auto_order"])
        self.assertTrue(suggestion["excluded_from_stock_alpha"])


class AshareExplorationPlanTest(unittest.TestCase):
    def _sample_debt_context(self, **overrides: object) -> dict[str, object]:
        return {
            "strategy_sample_valid_count": 0,
            "min_strategy_samples": 20,
            **overrides,
        }

    def test_sample_debt_cannot_be_the_only_zero_exploration_reason(self) -> None:
        data = plan_capital(
            [],
            50_000.0,
            candidates=[
                {
                    "ts_code": "000001.SZ",
                    "combined": 0.20,
                    "data_qualified": True,
                    "execution_eligible": True,
                }
            ],
            dynamic=True,
            market_context=self._sample_debt_context(),
        ).to_dict()

        self.assertEqual(data["risk_mode"], "sample_collection")
        self.assertEqual(data["sample_intent"], "exploration")
        self.assertEqual(data["max_new_positions"], 1)
        self.assertEqual(len(data["suggested_buys"]), 1)
        self.assertNotIn(
            "sample_insufficient", {row["code"] for row in data["undeployed_reasons"]}
        )

    def test_exploration_is_capped_at_one_candidate_and_7500_total(self) -> None:
        data = plan_capital(
            [],
            50_000.0,
            candidates=_candidates(5),
            dynamic=True,
            market_context=self._sample_debt_context(),
        ).to_dict()

        self.assertEqual(len(data["suggested_buys"]), 1)
        self.assertLessEqual(data["suggested_buys"][0]["allocation"], 7_500.0)
        self.assertEqual(data["exploration_limits"]["max_new_positions_per_day"], 1)
        self.assertEqual(
            data["exploration_limits"]["total_exposure_limit_cny"], 7_500.0
        )

    def test_existing_exploration_exposure_reduces_next_budget(self) -> None:
        data = plan_capital(
            [
                {
                    "ts_code": "000001.SZ",
                    "market_value": 7_000.0,
                    "sample_intent": "exploration",
                }
            ],
            43_000.0,
            candidates=[{"ts_code": "000002.SZ", "combined": 0.8}],
            dynamic=True,
            market_context=self._sample_debt_context(),
        ).to_dict()

        self.assertEqual(data["exploration_limits"]["remaining_exposure_cny"], 500.0)
        self.assertLessEqual(data["suggested_buys"][0]["allocation"], 500.0)

    def test_daily_loss_and_daily_new_position_gates_remain_hard(self) -> None:
        loss_blocked = plan_capital(
            [],
            50_000.0,
            candidates=_candidates(1),
            dynamic=True,
            market_context=self._sample_debt_context(exploration_daily_loss_cny=225.0),
        ).to_dict()
        count_blocked = plan_capital(
            [],
            50_000.0,
            candidates=_candidates(1),
            dynamic=True,
            market_context=self._sample_debt_context(
                existing_exploration_new_positions=1
            ),
        ).to_dict()

        self.assertEqual(
            loss_blocked["capacity_reason"], "exploration_daily_loss_limit_reached"
        )
        self.assertEqual(
            count_blocked["capacity_reason"],
            "exploration_daily_new_position_limit_reached",
        )
        self.assertEqual(loss_blocked["suggested_buys"], [])
        self.assertEqual(count_blocked["suggested_buys"], [])

    def test_hard_risk_gate_blocks_trade_but_keeps_observation_contract(self) -> None:
        data = plan_capital(
            [],
            50_000.0,
            candidates=_candidates(1),
            dynamic=True,
            market_context=self._sample_debt_context(
                hard_gate_blockers=["stale_price_evidence"]
            ),
        ).to_dict()

        self.assertEqual(data["suggested_buys"], [])
        self.assertEqual(data["sample_intent"], "observation")
        self.assertEqual(data["capacity_reason"], "stale_price_evidence")
        self.assertEqual(data["qualified_candidate_count"], 1)

    def test_upstream_exploration_propensity_is_preserved(self) -> None:
        data = plan_capital(
            [],
            50_000.0,
            candidates=[
                {
                    "ts_code": "000001.SZ",
                    "sample_intent": "exploration",
                    "combined": 0.5,
                    "selection_method": "epsilon_greedy_top_k",
                    "selection_propensity": 0.125,
                }
            ],
            dynamic=True,
        ).to_dict()

        row = data["suggested_buys"][0]
        self.assertEqual(row["selection_method"], "epsilon_greedy_top_k")
        self.assertEqual(row["selection_propensity"], 0.125)

    def test_explicit_exploration_candidate_is_not_crowded_out_by_normal_rows(
        self,
    ) -> None:
        normal = _candidates(8)
        data = plan_capital(
            [],
            50_000.0,
            candidates=[
                *normal,
                {
                    "ts_code": "600000.SH",
                    "sample_intent": "exploration",
                    "combined": 0.40,
                    "selection_propensity": 0.10,
                },
            ],
            dynamic=True,
        ).to_dict()

        planned = [row["code"] for row in data["suggested_buys"]]
        self.assertIn("600000.SH", planned)
        self.assertEqual(len(planned), 8)

    def test_explicit_exploration_and_exploitation_keep_separate_budgets(self) -> None:
        data = plan_capital(
            [],
            50_000.0,
            candidates=[
                {
                    "ts_code": "000001.SZ",
                    "sample_intent": "exploitation",
                    "combined": 0.90,
                },
                {
                    "ts_code": "000002.SZ",
                    "sample_intent": "exploitation",
                    "combined": 0.85,
                },
                {
                    "ts_code": "600000.SH",
                    "sample_intent": "exploration",
                    "combined": 0.40,
                    "selection_propensity": 0.10,
                },
            ],
            dynamic=True,
        ).to_dict()

        intents = [row["sample_intent"] for row in data["suggested_buys"]]
        self.assertEqual(intents.count("exploration"), 1)
        self.assertEqual(intents.count("exploitation"), 2)
        exploration_budget = sum(
            row["allocation"]
            for row in data["suggested_buys"]
            if row["sample_intent"] == "exploration"
        )
        self.assertLessEqual(exploration_budget, 7_500.0)
        self.assertEqual(data["sample_intent"], "mixed")
        exploitation_rows = [
            row
            for row in data["suggested_buys"]
            if row["sample_intent"] == "exploitation"
        ]
        self.assertTrue(
            all(
                row["selection_method"] == "ranked_candidate_order"
                for row in exploitation_rows
            )
        )

    def test_exploration_loss_gate_does_not_suppress_safe_exploitation(self) -> None:
        data = plan_capital(
            [],
            50_000.0,
            candidates=[
                {
                    "ts_code": "000001.SZ",
                    "sample_intent": "exploitation",
                    "combined": 0.90,
                },
                {
                    "ts_code": "600000.SH",
                    "sample_intent": "exploration",
                    "combined": 0.40,
                },
            ],
            dynamic=True,
            market_context={"exploration_daily_loss_cny": 225.0},
        ).to_dict()

        self.assertEqual(
            [row["sample_intent"] for row in data["suggested_buys"]],
            ["exploitation"],
        )
        self.assertEqual(data["sample_intent"], "exploitation")
        rejected = {row["symbol"]: row["code"] for row in data["candidate_rejections"]}
        self.assertEqual(rejected["600000.SH"], "exploration_daily_loss_limit_reached")

    def test_evolution_advice_cannot_expand_risk_or_promote(self) -> None:
        baseline = plan_capital(
            [], 50_000.0, candidates=_candidates(8), dynamic=True
        ).to_dict()
        advised = plan_capital(
            [],
            50_000.0,
            candidates=_candidates(8),
            dynamic=True,
            market_context={"evolution_recommended_action": "expand_risk_candidate"},
        ).to_dict()

        self.assertFalse(advised["automatic_promotion_enabled"])
        self.assertFalse(advised["automatic_risk_expansion_enabled"])
        self.assertEqual(advised["position_capacity"], baseline["position_capacity"])
        self.assertEqual(
            advised["stock_exposure_limit_cny"], baseline["stock_exposure_limit_cny"]
        )


class AsharePatrolMarketLimitTest(unittest.TestCase):
    def test_ashare_accepts_eight_positions_and_90pct_exposure(self) -> None:
        positions = [{"ts_code": f"{index:06d}.SZ"} for index in range(1, 9)]
        result = patrol(
            {"market": "ashare", "positions": positions, "total_exposure": 0.90}
        )

        alert_types = {row["type"] for row in result["alerts"]}
        self.assertNotIn("position_count_breach", alert_types)
        self.assertNotIn("exposure_breach", alert_types)

    def test_ashare_ninth_position_and_above_90pct_are_breaches(self) -> None:
        positions = [{"ts_code": f"{index:06d}.SZ"} for index in range(1, 10)]
        result = patrol(
            {"market": "a_share", "positions": positions, "total_exposure": 0.901}
        )

        alert_types = {row["type"] for row in result["alerts"]}
        self.assertIn("position_count_breach", alert_types)
        self.assertIn("exposure_breach", alert_types)

    def test_ashare_override_does_not_leak_into_unspecified_or_us_market(self) -> None:
        positions = [{"ts_code": f"{index:06d}.SZ"} for index in range(1, 9)]
        unspecified = patrol({"positions": positions[:6], "total_exposure": 0.85})
        us = patrol({"market": "us", "positions": positions, "total_exposure": 0.85})

        unspecified_types = {row["type"] for row in unspecified["alerts"]}
        us_types = {row["type"] for row in us["alerts"]}
        self.assertIn("position_count_breach", unspecified_types)
        self.assertIn("exposure_breach", unspecified_types)
        self.assertNotIn("position_count_breach", us_types)
        self.assertIn("exposure_breach", us_types)


if __name__ == "__main__":
    unittest.main()
