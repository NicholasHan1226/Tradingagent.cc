from __future__ import annotations

import random
import unittest

from shared.execution.sim_engine import SimExecutionEngine, SimOrder


class SimEngineSmokeTest(unittest.TestCase):
    def test_import_init_and_submit_order_no_crash(self) -> None:
        engine = SimExecutionEngine("ashare", rng=random.Random(1))
        order = SimOrder(symbol="600000.SH", side="buy", quantity=100, limit_price=10.0, market="ashare")

        record = engine.submit_order(order, {"last_price": 10.0, "available_qty": 1000})

        self.assertIn(record.state, {"partial", "filled"})
        self.assertEqual(record.order.symbol, "600000.SH")
        self.assertEqual(record.as_dict()["capital_layer"], "simulated")

    def test_limit_buy_uses_ask_for_marketability(self) -> None:
        engine = SimExecutionEngine("ashare", rng=random.Random(1))
        order = SimOrder(symbol="600000.SH", side="buy", quantity=100, limit_price=10.05, market="ashare", order_type="limit")

        record = engine.submit_order(order, {"last_price": 10.0, "ask_price": 10.1, "ask_size": 1000})

        self.assertEqual(record.state, "open")
        self.assertEqual(record.reason, "limit_not_marketable")

    def test_market_buy_uses_ask_size_for_partial_fill(self) -> None:
        engine = SimExecutionEngine("ashare", rng=random.Random(1))
        order = SimOrder(symbol="600000.SH", side="buy", quantity=300, limit_price=10.0, market="ashare")

        record = engine.submit_order(order, {"ask_price": 10.0, "ask_size": 150})

        self.assertEqual(record.state, "partial")
        self.assertEqual(record.filled_qty, 100)
        self.assertGreaterEqual(record.avg_fill_price, 10.0)

    def test_ashare_buy_rejects_non_lot_quantity(self) -> None:
        engine = SimExecutionEngine("ashare", rng=random.Random(1))
        order = SimOrder(symbol="600000.SH", side="buy", quantity=120, limit_price=10.0, market="ashare")

        record = engine.submit_order(order, {"last_price": 10.0, "available_qty": 1000})

        self.assertEqual(record.state, "rejected")
        self.assertEqual(record.reason, "buy_quantity_not_lot_aligned")

    def test_ashare_sell_rejects_t1_unsellable_quantity(self) -> None:
        engine = SimExecutionEngine("ashare", rng=random.Random(1))
        order = SimOrder(symbol="600000.SH", side="sell", quantity=100, limit_price=10.0, market="ashare")

        record = engine.submit_order(order, {"bid_price": 10.0, "bid_size": 1000, "sellable_qty": 0})

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

        record = engine.submit_order(order, {"previous_close": 10.0, "last_price": 10.0, "available_qty": 1000})

        self.assertEqual(record.state, "rejected")
        self.assertEqual(record.reason, "price_above_upper_limit")

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

        record = engine.submit_order(order, {"previous_close": 10.0, "ask_price": 10.0, "ask_size": 1000})

        self.assertEqual(record.state, "filled")
        self.assertNotEqual(record.reason, "price_above_upper_limit")

    def test_bar_volume_caps_fill_when_no_order_book_size(self) -> None:
        engine = SimExecutionEngine("ashare", rng=random.Random(1))
        order = SimOrder(symbol="600000.SH", side="buy", quantity=300, limit_price=10.0, market="ashare")

        record = engine.submit_order(order, {"ask_price": 10.0, "bar_volume": 1500})

        self.assertEqual(record.state, "partial")
        self.assertEqual(record.filled_qty, 100)

    def test_counterparty_profile_changes_liquidity_and_impact(self) -> None:
        normal = SimExecutionEngine("ashare", rng=random.Random(1)).submit_order(
            SimOrder(symbol="600000.SH", side="buy", quantity=300, limit_price=10.0, market="ashare"),
            {"ask_price": 10.0, "ask_size": 300, "volatility_bps": 10},
        )
        panic = SimExecutionEngine("ashare", rng=random.Random(1)).submit_order(
            SimOrder(symbol="600000.SH", side="buy", quantity=300, limit_price=10.0, market="ashare"),
            {"ask_price": 10.0, "ask_size": 300, "volatility_bps": 10, "counterparty_profile": "retail_panic"},
        )

        self.assertLess(panic.filled_qty, normal.filled_qty)
        self.assertGreater(panic.fills[0].slippage_bps, normal.fills[0].slippage_bps)

    def test_buy_rejects_insufficient_cash(self) -> None:
        engine = SimExecutionEngine("ashare", rng=random.Random(1))
        order = SimOrder(symbol="600000.SH", side="buy", quantity=100, limit_price=10.0, market="ashare")

        record = engine.submit_order(order, {"ask_price": 10.0, "ask_size": 1000, "cash_available": 500.0})

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

        record = engine.submit_order(order, {"ask_price": 10.05, "ask_size": 1000, "volatility_bps": 20})

        self.assertEqual(record.state, "filled")
        self.assertLessEqual(record.avg_fill_price, order.limit_price)

    def test_pm_rejects_probability_outside_bounds(self) -> None:
        engine = SimExecutionEngine("pm", rng=random.Random(1))
        order = SimOrder(symbol="market-1", side="buy", quantity=10, limit_price=1.01, market="pm", order_type="limit")

        record = engine.submit_order(order, {"last_price": 0.5, "available_qty": 100})

        self.assertEqual(record.state, "rejected")
        self.assertEqual(record.reason, "price_above_max_probability")


if __name__ == "__main__":
    unittest.main()
