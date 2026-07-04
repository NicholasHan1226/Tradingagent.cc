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


if __name__ == "__main__":
    unittest.main()
