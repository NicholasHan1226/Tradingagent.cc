#!/usr/bin/env python3
"""Integration tests for T+1 checks across execution router and position ledger."""

from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from Ashare.t_plus_1 import next_trading_day
from shared.accounting import position_ledger
from shared.execution import execution_router


class TPlusOneIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

        ledger_dir = self.root / "logs"
        position_csv = ledger_dir / "position_ledger.csv"
        self._patch_module_attr(position_ledger, "LEDGER_DIR", ledger_dir)
        self._patch_module_attr(position_ledger, "POSITION_CSV", position_csv)
        self._patch_module_attr(position_ledger, "POSITION_LOCK", position_csv.with_suffix(".csv.lock"))

        self._patch_module_attr(execution_router, "ROUTER_LOG", self.root / "router_decisions.jsonl")
        self._patch_module_attr(execution_router, "SHADOW_EXECUTION_LOG", self.root / "simulated_execution_log.jsonl")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _patch_module_attr(self, module: object, name: str, value: Path) -> None:
        patcher = patch.object(module, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _route_shadow(self, **overrides: object) -> dict[str, object]:
        order = {
            "order_id": "TPLUS1-ORDER",
            "ts_code": "600000.SH",
            "side": "sell",
            "quantity": 100,
            "price": 10.0,
            "capital_layer": "shadow",
            "trade_date": "2026-06-30",
            "timestamp": "2026-06-30T10:00:00",
            "strategy_name": "tplus1_test",
        }
        order.update(overrides)
        return execution_router.route(order, "shadow")

    def test_same_day_buy_then_sell_is_rejected(self) -> None:
        buy_result = self._route_shadow(
            order_id="BUY-SAME-DAY",
            side="buy",
            trade_date="2026-06-30",
            timestamp="2026-06-30T09:35:00",
        )
        self.assertTrue(buy_result["executed"])

        position_ledger.open_position(
            "600000.SH",
            100,
            10.0,
            order_id="BUY-SAME-DAY",
            capital_layer="shadow",
            entry_date="2026-06-30",
        )

        sell_result = self._route_shadow(
            order_id="SELL-SAME-DAY",
            side="sell",
            trade_date="2026-06-30",
            timestamp="2026-06-30T10:15:00",
        )

        self.assertFalse(sell_result["executed"])
        self.assertEqual(sell_result["message"], "T+1 not satisfied")
        self.assertEqual(sell_result["result"]["status"], "blocked_t_plus_1")

    def test_sell_passes_on_next_trading_day(self) -> None:
        entry_date = "2026-06-26"
        trade_date = next_trading_day(entry_date).isoformat()
        position_ledger.open_position(
            "600000.SH",
            100,
            10.0,
            order_id="BUY-PREV-DAY",
            capital_layer="shadow",
            entry_date=entry_date,
        )

        sell_result = self._route_shadow(
            order_id="SELL-NEXT-TRADING-DAY",
            side="reduce",
            trade_date=trade_date,
            timestamp=f"{trade_date}T10:00:00",
        )

        self.assertTrue(sell_result["executed"])
        self.assertEqual(sell_result["channel"], "shadow_broker")
        self.assertEqual(sell_result["result"]["status"], "shadow_recorded")

    def test_missing_entry_date_is_rejected_conservatively(self) -> None:
        position_ledger.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
        legacy_headers = [header for header in position_ledger.CSV_HEADERS if header != "entry_date"]
        with open(position_ledger.POSITION_CSV, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(legacy_headers)
            writer.writerow(
                [
                    "LEGACY-1",
                    "2026-06-30T09:30:00",
                    "open",
                    "shadow",
                    "N",
                    "600000.SH",
                    "100",
                    "10.0",
                    "1000.0",
                    "100",
                    "1000.0",
                    "10.0",
                    "0.0",
                    "LEGACY-BUY",
                    "",
                    "legacy row without entry_date",
                ]
            )

        sell_result = self._route_shadow(
            order_id="SELL-LEGACY",
            side="sell",
            trade_date="2026-07-01",
            timestamp="2026-07-01T10:00:00",
        )

        self.assertFalse(sell_result["executed"])
        self.assertEqual(sell_result["message"], "T+1 not satisfied")
        self.assertEqual(sell_result["result"]["status"], "blocked_t_plus_1")

    def test_position_ledger_reduce_requires_trade_date_for_a_share(self) -> None:
        position_ledger.open_position(
            "600002.SH",
            200,
            10.0,
            order_id="LEDGER-BUY",
            capital_layer="shadow",
            entry_date="2026-06-30",
        )

        with self.assertRaisesRegex(ValueError, "requires trade_date"):
            position_ledger.reduce_position(
                "600002.SH",
                100,
                10.5,
                order_id="LEDGER-SELL",
                capital_layer="shadow",
            )

    def test_position_ledger_reduce_blocks_same_day_a_share(self) -> None:
        position_ledger.open_position(
            "600003.SH",
            200,
            10.0,
            order_id="LEDGER-BUY",
            capital_layer="shadow",
            entry_date="2026-06-30",
        )

        with self.assertRaisesRegex(ValueError, "T\\+1 not satisfied"):
            position_ledger.reduce_position(
                "600003.SH",
                100,
                10.5,
                order_id="LEDGER-SELL",
                capital_layer="shadow",
                trade_date="2026-06-30",
            )

    def test_position_ledger_close_passes_on_next_trading_day(self) -> None:
        entry_date = "2026-06-26"
        trade_date = next_trading_day(entry_date).isoformat()
        position_ledger.open_position(
            "600004.SH",
            100,
            10.0,
            order_id="LEDGER-BUY",
            capital_layer="shadow",
            entry_date=entry_date,
        )

        result = position_ledger.close_position(
            "600004.SH",
            10.5,
            order_id="LEDGER-CLOSE",
            capital_layer="shadow",
            trade_date=trade_date,
        )

        self.assertEqual(result["event_type"], "close")
        self.assertEqual(result["sellable_date"], trade_date)


if __name__ == "__main__":
    unittest.main()
