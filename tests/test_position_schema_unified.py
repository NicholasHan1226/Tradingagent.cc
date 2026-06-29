from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shared.accounting.position_schema import Position, from_ledger
from shared.portfolio.exit_manager import check_all_exits
from shared.risk.position_monitor import check_positions


class PositionSchemaUnifiedTest(unittest.TestCase):
    def test_check_positions_triggers_stop_loss_for_unified_position(self) -> None:
        position = Position(
            ts_code="000001.SZ",
            quantity=100,
            sellable_quantity=100,
            avg_price=10.0,
            cost_basis=1000.0,
            entry_date="2026-06-20",
            high_price=10.5,
            thesis="growth",
            capital_layer="shadow",
        )

        signals = check_positions([position], {"000001.SZ": 9.1})

        self.assertTrue(any(signal["action"] == "stop_loss" for signal in signals))

    def test_check_all_exits_triggers_trailing_stop_for_profitable_drawdown(self) -> None:
        position = Position(
            ts_code="000002.SZ",
            quantity=100,
            sellable_quantity=100,
            avg_price=10.0,
            cost_basis=1000.0,
            entry_date="2026-06-01",
            high_price=13.0,
            thesis="growth",
            capital_layer="simulated",
        )

        results = check_all_exits([position], {"000002.SZ": 11.0})

        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["should_exit"])
        self.assertEqual(results[0]["exit_type"], "trailing_stop")
        self.assertTrue(results[0]["executable"])

    def test_unsellable_new_position_is_marked_not_executable(self) -> None:
        position = from_ledger({
            "ts_code": "000003.SZ",
            "quantity": 100,
            "avg_price": 10.0,
            "cost_basis": 1000.0,
            "entry_date": date.today().isoformat(),
            "high_price": 10.0,
            "thesis": "event",
            "capital_layer": "shadow",
        })

        self.assertEqual(position.sellable_quantity, 0)

        results = check_all_exits([position], {"000003.SZ": 9.0})

        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["should_exit"])
        self.assertFalse(results[0]["executable"])
        self.assertIn("sellable_quantity=0", results[0]["blocked_reason"])


if __name__ == "__main__":
    unittest.main()
