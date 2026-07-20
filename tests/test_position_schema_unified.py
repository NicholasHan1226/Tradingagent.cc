from __future__ import annotations

# The repository root is inserted before importing project modules below.
# ruff: noqa: E402

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shared.accounting.position_schema import Position
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


if __name__ == "__main__":
    unittest.main()
