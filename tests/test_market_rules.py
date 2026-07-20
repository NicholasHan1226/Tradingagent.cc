from __future__ import annotations

import unittest
from unittest.mock import patch

from shared.risk import pre_trade_check as pre_trade_check_module
from shared.risk.pre_trade_check import pre_trade_check


class MarketRiskRoutingTest(unittest.TestCase):
    def test_ashare_allows_eighth_position_below_gross_cap(self) -> None:
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
            {"positions": positions, "total_exposure": 0.77, "daily_pnl_pct": 0.0},
        )

        self.assertTrue(result["approved"])
        self.assertAlmostEqual(result["adjusted_weight"], 0.10, places=6)

    def test_ashare_rejects_ninth_position_at_operational_cap(self) -> None:
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
            {"positions": positions, "total_exposure": 0.80, "daily_pnl_pct": 0.0},
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
            {"positions": [], "total_exposure": 0.85, "daily_pnl_pct": 0.0},
        )

        self.assertTrue(result["approved"])
        self.assertAlmostEqual(result["adjusted_weight"], 0.05, places=6)

    def test_ashare_preserves_correlation_and_liquidity_reductions(self) -> None:
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

    def test_crypto_retains_eighty_percent_gross_limit(self) -> None:
        result = pre_trade_check(
            {
                "ts_code": "ETHUSDT",
                "market": "crypto",
                "side": "buy",
                "weight": 0.10,
                "sector": "crypto-2",
            },
            {"positions": [], "total_exposure": 0.75, "daily_pnl_pct": 0.0},
        )

        self.assertTrue(result["approved"])
        self.assertAlmostEqual(result["adjusted_weight"], 0.05, places=6)

    def test_ashare_t_plus_one_fallback_skips_known_holiday(self) -> None:
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

    def test_missing_and_unknown_markets_fail_closed_without_ashare_fallback(
        self,
    ) -> None:
        for market in (None, "", "us", "pm", "hk", "martian"):
            with self.subTest(market=market):
                result = pre_trade_check(
                    {
                        "ts_code": "600000.SH",
                        "market": market,
                        "side": "buy",
                        "weight": 0.01,
                    }
                )
                self.assertFalse(result["approved"])
                self.assertEqual(result["reasons"], ["unsupported_or_missing_market"])
                self.assertNotEqual(result["market"], "ashare")

    def test_cn_futures_requires_its_market_specific_risk_adapter(self) -> None:
        result = pre_trade_check(
            {
                "ts_code": "rb2610.SHFE",
                "market": "cn_futures",
                "side": "open",
                "weight": 0.01,
            }
        )

        self.assertFalse(result["approved"])
        self.assertEqual(result["market"], "cn_futures")
        self.assertEqual(
            result["reasons"],
            ["cn_futures_requires_market_specific_risk_adapter"],
        )


if __name__ == "__main__":
    unittest.main()
