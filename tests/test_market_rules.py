from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shared.portfolio.exit_manager import check_all_exits
from shared.risk.pre_trade_check import pre_trade_check


class MarketRulesTest(unittest.TestCase):
    def test_ashare_t_plus_1_new_position_cannot_exit_same_day(self) -> None:
        today = date.today().isoformat()
        positions = [
            {
                "ts_code": "600519.SH",
                "market": "ashare",
                "quantity": 100,
                "sellable_quantity": 100,
                "avg_price": 10.0,
                "cost_basis": 1000.0,
                "entry_date": today,
                "as_of": today,
                "high_price": 10.0,
                "thesis": "event",
                "capital_layer": "shadow",
            }
        ]

        results = check_all_exits(positions, {"600519.SH": 9.0})

        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["should_exit"])
        self.assertEqual(results[0]["market"], "ashare")
        self.assertFalse(results[0]["executable"])
        self.assertEqual(results[0]["blocked_reason"], "T+1")

    def test_crypto_daily_loss_limit_uses_ten_percent(self) -> None:
        result = pre_trade_check(
            {
                "ts_code": "BTCUSDT",
                "market": "crypto",
                "side": "buy",
                "weight": 0.02,
                "sector": "crypto",
            },
            {"positions": [], "total_exposure": 0.0, "daily_pnl_pct": -0.101},
        )

        self.assertFalse(result["approved"])
        self.assertEqual(result["market"], "crypto")
        self.assertTrue(any("-0.1000" in reason for reason in result["reasons"]))

    def test_us_t_plus_2_new_position_cannot_exit_same_day(self) -> None:
        today = date.today().isoformat()
        positions = [
            {
                "ts_code": "AAPL",
                "market": "us",
                "quantity": 10,
                "sellable_quantity": 10,
                "avg_price": 100.0,
                "cost_basis": 1000.0,
                "entry_date": today,
                "as_of": today,
                "high_price": 100.0,
                "thesis": "growth",
                "capital_layer": "shadow",
            }
        ]

        results = check_all_exits(positions, {"AAPL": 90.0})

        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["should_exit"])
        self.assertEqual(results[0]["market"], "us")
        self.assertFalse(results[0]["executable"])
        self.assertEqual(results[0]["blocked_reason"], "T+2")

    def test_pm_single_market_max_is_twenty_percent(self) -> None:
        result = pre_trade_check(
            {
                "ts_code": "PM-B",
                "market": "pm",
                "side": "buy",
                "weight": 0.11,
                "sector": "prediction_market",
            },
            {
                "positions": [
                    {
                        "ts_code": "PM-A",
                        "market": "pm",
                        "weight": 0.10,
                        "sector": "prediction_market",
                    }
                ],
                "total_exposure": 0.10,
                "daily_pnl_pct": 0.0,
            },
        )

        self.assertFalse(result["approved"])
        self.assertEqual(result["market"], "pm")
        self.assertTrue(any("0.2000" in reason for reason in result["reasons"]))


if __name__ == "__main__":
    unittest.main()
