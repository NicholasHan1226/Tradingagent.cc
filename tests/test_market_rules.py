# ruff: noqa: E402
from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shared.portfolio.exit_manager import check_all_exits
from shared.portfolio import exit_manager
from shared.risk.pre_trade_check import pre_trade_check
from shared.risk import pre_trade_check as pre_trade_check_module


class MarketRulesTest(unittest.TestCase):
    def test_ashare_allows_eighth_distinct_position_below_ninety_percent_gross_cap(
        self,
    ) -> None:
        positions = [
            {
                "ts_code": f"60000{index}.SH",
                "market": "ashare",
                "weight": 0.11,
                "sector": f"sector-{index}",
            }
            for index in range(1, 8)
        ]

        result = pre_trade_check(
            {
                "ts_code": "600008.SH",
                "market": "ashare",
                "side": "buy",
                "weight": 0.10,
                "sector": "sector-8",
                "turnover_wan": 10_000,
            },
            {
                "positions": positions,
                "total_exposure": 0.77,
                "daily_pnl_pct": 0.0,
            },
        )

        self.assertTrue(result["approved"])
        self.assertAlmostEqual(result["adjusted_weight"], 0.10, places=6)

    def test_ashare_rejects_ninth_distinct_position_at_operational_cap(self) -> None:
        positions = [
            {
                "ts_code": f"60000{index}.SH",
                "market": "ashare",
                "weight": 0.10,
                "sector": f"sector-{index}",
            }
            for index in range(1, 9)
        ]

        result = pre_trade_check(
            {
                "ts_code": "600009.SH",
                "market": "ashare",
                "side": "buy",
                "weight": 0.05,
                "sector": "sector-9",
                "turnover_wan": 10_000,
            },
            {
                "positions": positions,
                "total_exposure": 0.80,
                "daily_pnl_pct": 0.0,
            },
        )

        self.assertFalse(result["approved"])
        self.assertTrue(any("上限 8" in reason for reason in result["reasons"]))

    def test_ashare_gross_exposure_is_capped_at_ninety_percent(self) -> None:
        result = pre_trade_check(
            {
                "ts_code": "600008.SH",
                "market": "ashare",
                "side": "buy",
                "weight": 0.10,
                "sector": "sector-8",
                "turnover_wan": 10_000,
            },
            {
                "positions": [],
                "total_exposure": 0.85,
                "daily_pnl_pct": 0.0,
            },
        )

        self.assertTrue(result["approved"])
        self.assertAlmostEqual(result["adjusted_weight"], 0.05, places=6)

    def test_ashare_capacity_change_preserves_correlation_and_liquidity_reductions(
        self,
    ) -> None:
        result = pre_trade_check(
            {
                "ts_code": "600008.SH",
                "market": "ashare",
                "side": "buy",
                "weight": 0.10,
                "sector": "sector-8",
                "turnover_wan": 1_000,
            },
            {
                "positions": [],
                "total_exposure": 0.50,
                "daily_pnl_pct": 0.0,
                "correlations": {"600001.SH|600008.SH": 0.90},
            },
        )

        self.assertTrue(result["approved"])
        self.assertLess(result["adjusted_weight"], 0.10)
        self.assertTrue(any("相关性降权" in item for item in result["adjustments"]))
        self.assertTrue(any("流动性降权" in item for item in result["adjustments"]))

    def test_crypto_retains_global_eighty_percent_gross_exposure_limit(self) -> None:
        result = pre_trade_check(
            {
                "ts_code": "ETHUSDT",
                "market": "crypto",
                "side": "buy",
                "weight": 0.10,
                "sector": "crypto-2",
            },
            {
                "positions": [],
                "total_exposure": 0.75,
                "daily_pnl_pct": 0.0,
            },
        )

        self.assertTrue(result["approved"])
        self.assertAlmostEqual(result["adjusted_weight"], 0.05, places=6)

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

    def test_ashare_t_plus_1_fallback_skips_known_2026_holidays(self) -> None:
        with patch.object(pre_trade_check_module, "_t_plus_1", None):
            result = pre_trade_check(
                {
                    "ts_code": "600519.SH",
                    "market": "ashare",
                    "side": "sell",
                    "weight": 0.02,
                    "entry_date": "2026-09-24",
                    "trade_date": "2026-09-25",
                },
                {
                    "positions": [
                        {
                            "ts_code": "600519.SH",
                            "market": "ashare",
                            "weight": 0.02,
                            "entry_date": "2026-09-24",
                        }
                    ],
                    "total_exposure": 0.02,
                },
            )

        self.assertFalse(result["approved"])
        self.assertTrue(any("T+1" in reason for reason in result["reasons"]))

    def test_exit_manager_fallback_skips_known_2026_holidays(self) -> None:
        positions = [
            {
                "ts_code": "600519.SH",
                "market": "ashare",
                "quantity": 100,
                "sellable_quantity": 100,
                "avg_price": 10.0,
                "cost_basis": 1000.0,
                "entry_date": "2026-09-24",
                "as_of": "2026-09-25",
                "high_price": 10.0,
                "thesis": "event",
                "capital_layer": "shadow",
            }
        ]

        with patch.object(exit_manager, "_t_plus_1", None):
            results = check_all_exits(positions, {"600519.SH": 9.0})

        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["should_exit"])
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
