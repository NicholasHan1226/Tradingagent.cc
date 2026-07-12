from __future__ import annotations

import random
import unittest

from shared.execution.sim_engine import SimExecutionEngine, SimOrder


class SimEngineSmokeTest(unittest.TestCase):
    def test_import_init_and_submit_order_no_crash(self) -> None:
        engine = SimExecutionEngine("ashare", rng=random.Random(1))
        order = SimOrder(
            symbol="600000.SH",
            side="buy",
            quantity=100,
            limit_price=10.0,
            market="ashare",
        )

        record = engine.submit_order(order, {"last_price": 10.0, "available_qty": 1000})

        self.assertIn(record.state, {"partial", "filled"})
        self.assertEqual(record.order.symbol, "600000.SH")
        self.assertEqual(record.as_dict()["capital_layer"], "simulated")

    def test_limit_buy_uses_ask_for_marketability(self) -> None:
        engine = SimExecutionEngine("ashare", rng=random.Random(1))
        order = SimOrder(
            symbol="600000.SH",
            side="buy",
            quantity=100,
            limit_price=10.05,
            market="ashare",
            order_type="limit",
        )

        record = engine.submit_order(
            order, {"last_price": 10.0, "ask_price": 10.1, "ask_size": 1000}
        )

        self.assertEqual(record.state, "open")
        self.assertEqual(record.reason, "limit_not_marketable")

    def test_market_buy_uses_ask_size_for_partial_fill(self) -> None:
        engine = SimExecutionEngine("ashare", rng=random.Random(1))
        order = SimOrder(
            symbol="600000.SH",
            side="buy",
            quantity=300,
            limit_price=10.0,
            market="ashare",
        )

        record = engine.submit_order(order, {"ask_price": 10.0, "ask_size": 150})

        self.assertEqual(record.state, "partial")
        self.assertEqual(record.filled_qty, 100)
        self.assertGreaterEqual(record.avg_fill_price, 10.0)

    def test_ashare_buy_rejects_non_lot_quantity(self) -> None:
        engine = SimExecutionEngine("ashare", rng=random.Random(1))
        order = SimOrder(
            symbol="600000.SH",
            side="buy",
            quantity=120,
            limit_price=10.0,
            market="ashare",
        )

        record = engine.submit_order(order, {"last_price": 10.0, "available_qty": 1000})

        self.assertEqual(record.state, "rejected")
        self.assertEqual(record.reason, "buy_quantity_not_lot_aligned")

    def test_ashare_sell_rejects_t1_unsellable_quantity(self) -> None:
        engine = SimExecutionEngine("ashare", rng=random.Random(1))
        order = SimOrder(
            symbol="600000.SH",
            side="sell",
            quantity=100,
            limit_price=10.0,
            market="ashare",
        )

        record = engine.submit_order(
            order, {"bid_price": 10.0, "bid_size": 1000, "sellable_qty": 0}
        )

        self.assertEqual(record.state, "rejected")
        self.assertEqual(record.reason, "insufficient_sellable_qty_t1")

    def test_ashare_rejects_price_above_limit(self) -> None:
        engine = SimExecutionEngine("ashare", rng=random.Random(1))
        order = SimOrder(
            symbol="600000.SH",
            side="buy",
            quantity=100,
            limit_price=11.2,
            market="ashare",
            order_type="limit",
        )

        record = engine.submit_order(
            order, {"previous_close": 10.0, "last_price": 10.0, "available_qty": 1000}
        )

        self.assertEqual(record.state, "rejected")
        self.assertEqual(record.reason, "price_above_upper_limit")

    def test_main_board_risk_warning_uses_ten_percent_after_2026_07_06(self) -> None:
        engine = SimExecutionEngine("ashare", rng=random.Random(1))
        order = SimOrder(
            symbol="600000.SH",
            side="buy",
            quantity=100,
            limit_price=10.6,
            market="ashare",
            order_type="limit",
        )

        record = engine.submit_order(
            order,
            {
                "previous_close": 10.0,
                "ask_price": 10.6,
                "ask_size": 1000,
                "risk_warning": True,
            },
        )

        self.assertEqual(record.state, "filled")
        self.assertEqual(
            record.execution_reality_model_version,
            "ashare-execution-reality-20260706-v1",
        )

    def test_continuous_auction_price_cage_rejects_out_of_range_limit(self) -> None:
        engine = SimExecutionEngine("ashare", rng=random.Random(1))
        order = SimOrder(
            symbol="600000.SH",
            side="buy",
            quantity=100,
            limit_price=10.3,
            market="ashare",
            order_type="limit",
        )

        record = engine.submit_order(
            order,
            {
                "previous_close": 10.0,
                "ask_price": 10.0,
                "ask_size": 1000,
                "market_session": "continuous_auction_pm",
                "price_cage_reference": 10.0,
            },
        )

        self.assertEqual(record.state, "rejected")
        self.assertEqual(record.reason, "price_above_continuous_auction_cage")

    def test_ashare_fee_model_includes_transfer_fee_and_five_bps_sell_stamp(
        self,
    ) -> None:
        engine = SimExecutionEngine("ashare", rng=random.Random(1))
        engine.positions["600000.SH"] = engine.position("600000.SH")
        engine.positions["600000.SH"].current_holdings = 100
        engine.positions["600000.SH"].avg_cost = 9.0
        order = SimOrder(
            symbol="600000.SH",
            side="sell",
            quantity=100,
            limit_price=10.0,
            market="ashare",
            order_type="limit",
        )

        record = engine.submit_order(
            order,
            {"bid_price": 10.0, "bid_size": 1000, "sellable_qty": 100},
        )

        notional = record.filled_qty * record.avg_fill_price
        self.assertEqual(record.state, "filled")
        self.assertEqual(record.fees["stamp_duty"], round(notional * 5 / 10_000, 8))
        self.assertEqual(record.fees["transfer_fee"], round(notional * 0.1 / 10_000, 8))
        self.assertEqual(
            record.fees["execution_reality_model_version"],
            "ashare-execution-reality-20260706-v1",
        )

    def test_after_hours_fixed_price_is_a_distinct_unsupported_order_type(self) -> None:
        engine = SimExecutionEngine("ashare", rng=random.Random(1))
        order = SimOrder(
            symbol="600000.SH",
            side="buy",
            quantity=100,
            limit_price=10.0,
            market="ashare",
            order_type="after_hours_fixed_price",
        )

        record = engine.submit_order(order, {"official_closing_price": 10.0})

        self.assertEqual(record.state, "rejected")
        self.assertEqual(record.reason, "after_hours_fixed_price_match_not_implemented")

    def test_cancel_compare_and_set_exposes_stale_state_race(self) -> None:
        engine = SimExecutionEngine("ashare", rng=random.Random(1))
        order = SimOrder(
            symbol="600000.SH",
            side="buy",
            quantity=100,
            limit_price=9.5,
            market="ashare",
            order_type="limit",
        )
        record = engine.submit_order(order, {"ask_price": 10.0, "ask_size": 1000})

        stale = engine.cancel_order(order.order_id, expected_state_version=0)

        self.assertEqual(stale.state, "open")
        self.assertEqual(stale.last_cancel_result["outcome"], "state_version_conflict")
        self.assertEqual(
            stale.last_cancel_result["cancel_policy_version"],
            "ashare-cancel-cas-20260706-v1",
        )

        cancelled = engine.cancel_order(
            order.order_id,
            expected_state_version=record.state_version,
        )
        self.assertEqual(cancelled.state, "cancelled")
        self.assertEqual(cancelled.last_cancel_result["outcome"], "cancelled")

    def test_market_order_checks_execution_price_not_dummy_limit(self) -> None:
        engine = SimExecutionEngine("ashare", rng=random.Random(1))
        order = SimOrder(
            symbol="600000.SH",
            side="buy",
            quantity=100,
            limit_price=99.0,
            market="ashare",
            order_type="market",
        )

        record = engine.submit_order(
            order, {"previous_close": 10.0, "ask_price": 10.0, "ask_size": 1000}
        )

        self.assertEqual(record.state, "filled")
        self.assertNotEqual(record.reason, "price_above_upper_limit")

    def test_bar_volume_caps_fill_when_no_order_book_size(self) -> None:
        engine = SimExecutionEngine("ashare", rng=random.Random(1))
        order = SimOrder(
            symbol="600000.SH",
            side="buy",
            quantity=300,
            limit_price=10.0,
            market="ashare",
        )

        record = engine.submit_order(order, {"ask_price": 10.0, "bar_volume": 1500})

        self.assertEqual(record.state, "partial")
        self.assertEqual(record.filled_qty, 100)

    def test_counterparty_profile_changes_liquidity_and_impact(self) -> None:
        normal = SimExecutionEngine("ashare", rng=random.Random(1)).submit_order(
            SimOrder(
                symbol="600000.SH",
                side="buy",
                quantity=300,
                limit_price=10.0,
                market="ashare",
            ),
            {"ask_price": 10.0, "ask_size": 300, "volatility_bps": 10},
        )
        panic = SimExecutionEngine("ashare", rng=random.Random(1)).submit_order(
            SimOrder(
                symbol="600000.SH",
                side="buy",
                quantity=300,
                limit_price=10.0,
                market="ashare",
            ),
            {
                "ask_price": 10.0,
                "ask_size": 300,
                "volatility_bps": 10,
                "counterparty_profile": "retail_panic",
            },
        )

        self.assertLess(panic.filled_qty, normal.filled_qty)
        self.assertGreater(panic.fills[0].slippage_bps, normal.fills[0].slippage_bps)

    def test_buy_rejects_insufficient_cash(self) -> None:
        engine = SimExecutionEngine("ashare", rng=random.Random(1))
        order = SimOrder(
            symbol="600000.SH",
            side="buy",
            quantity=100,
            limit_price=10.0,
            market="ashare",
        )

        record = engine.submit_order(
            order, {"ask_price": 10.0, "ask_size": 1000, "cash_available": 500.0}
        )

        self.assertEqual(record.state, "rejected")
        self.assertEqual(record.reason, "insufficient_cash")

    def test_limit_buy_fill_does_not_cross_limit_after_tick_rounding(self) -> None:
        engine = SimExecutionEngine("ashare", rng=random.Random(1))
        order = SimOrder(
            symbol="600000.SH",
            side="buy",
            quantity=100,
            limit_price=10.05,
            market="ashare",
            order_type="limit",
        )

        record = engine.submit_order(
            order, {"ask_price": 10.05, "ask_size": 1000, "volatility_bps": 20}
        )

        self.assertEqual(record.state, "filled")
        self.assertLessEqual(record.avg_fill_price, order.limit_price)

    def test_pm_rejects_probability_outside_bounds(self) -> None:
        engine = SimExecutionEngine("pm", rng=random.Random(1))
        order = SimOrder(
            symbol="market-1",
            side="buy",
            quantity=10,
            limit_price=1.01,
            market="pm",
            order_type="limit",
        )

        record = engine.submit_order(order, {"last_price": 0.5, "available_qty": 100})

        self.assertEqual(record.state, "rejected")
        self.assertEqual(record.reason, "price_above_max_probability")


if __name__ == "__main__":
    unittest.main()
