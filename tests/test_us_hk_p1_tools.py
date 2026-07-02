from __future__ import annotations

import unittest

from shared.markets.config_schema import MarketToolConfig
from shared.markets.safety import SafetyViolation


class P1ToolsTest(unittest.TestCase):
    def test_us_daily_report_renders_markdown_shadow_summary(self) -> None:
        from US.common import USConfig
        from US.report import USDailyReport

        report = USDailyReport(config=USConfig())
        result = report.render_daily(
            "2026-07-02",
            shadow_records=[
                {"symbol": "AAPL", "strategy_name": "earnings_drift", "side": "buy", "score": 0.74},
                {"symbol": "MSFT", "strategy_name": "momentum", "side": "hold", "score": 0.61},
            ],
            validation={"status": "ok", "passed": True},
        )

        self.assertEqual(result["market"], "us")
        self.assertEqual(result["currency"], "USD")
        self.assertEqual(result["capital_layer"], "shadow")
        self.assertIn("# US Daily Shadow Report - 2026-07-02", result["markdown"])
        self.assertIn("| AAPL | earnings_drift | buy | 0.7400 |", result["markdown"])
        self.assertFalse(report.delivery_policy(result)["real_execution"])
        with self.assertRaisesRegex(RuntimeError, "real/live execution is rejected"):
            report.render_daily(
                "2026-07-02",
                shadow_records=[{"symbol": "AAPL", "strategy_name": "bad", "account_type": "real"}],
            )

    def test_us_forward_validation_tracks_oos_earnings_and_momentum_funnel(self) -> None:
        from US.common import USConfig
        from US.validation import USForwardValidation

        validator = USForwardValidation(config=USConfig(), train_end="20260701")
        result = validator.validate(
            [
                {"strategy_name": "old_train", "signal_date": "2026-07-01", "as_of": "2026-07-02", "return_pct": 0.99},
                {"strategy_name": "earnings_drift", "signal_date": "2026-7-2", "as_of": "2026-07-02", "return_pct": 0.08},
                {"strategy_name": "momentum", "signal_date": "20260702", "as_of": "2026-07-02", "return_pct": -0.01},
                {"strategy_name": "value", "signal_date": "2026-07-02", "as_of": "2026-07-02", "return_pct": float("nan")},
                {"strategy_name": "future", "signal_date": "2026-07-03", "as_of": "2026-07-03", "return_pct": 0.99},
            ],
            as_of="2026-07-02",
        )

        self.assertEqual(result["market"], "us")
        self.assertEqual(result["validation_type"], "out_of_sample")
        self.assertEqual(result["funnel"]["earnings"], 1)
        self.assertEqual(result["funnel"]["momentum"], 1)
        self.assertEqual(result["total"], 3)
        self.assertAlmostEqual(result["positive_rate"], 1 / 3)
        self.assertAlmostEqual(result["avg_return_pct"], round((0.08 - 0.01) / 3, 6))
        with self.assertRaisesRegex(RuntimeError, "real/live execution is rejected"):
            validator.validate(
                [{"strategy_name": "momentum", "return_pct": 0.1, "capital_layer": "real"}],
                as_of="2026-07-02",
            )

    def test_us_strategy_promotion_uses_five_shadow_first_tiers_and_rejects_real_config(self) -> None:
        from US.common import USConfig
        from US.promotion import USStrategyPromotion

        promoter = USStrategyPromotion(config=USConfig())
        result = promoter.evaluate(
            {
                "strategy_name": "earnings_drift",
                "shadow_trades": 35,
                "positive_days_pct": 0.63,
                "oos_return_pct": 0.07,
                "drawdown_pct": -0.04,
            }
        )

        self.assertEqual(promoter.tiers, ("research", "shadow_candidate", "shadow", "sim_candidate", "sim"))
        self.assertEqual(result["current_tier"], "sim_candidate")
        self.assertEqual(result["next_tier"], "sim")
        self.assertFalse(result["real_execution"])

        with self.assertRaises(SafetyViolation):
            USStrategyPromotion(config=MarketToolConfig(market="us", safety={"real_money_enabled": True}))

    def test_hk_daily_report_includes_hkd_and_lot_sizes(self) -> None:
        from HK.common import HKConfig
        from HK.report import HKDailyReport

        report = HKDailyReport(config=HKConfig(), lot_sizes={"00700.HK": 100, "09988.HK": 100})
        result = report.render_daily(
            "2026-07-02",
            shadow_records=[
                {"symbol": "00700.HK", "strategy_name": "hk_momentum", "side": "buy", "score": 0.7},
                {"symbol": "09988.HK", "strategy_name": "hk_value", "side": "hold", "score": 0.55},
            ],
        )

        self.assertEqual(result["market"], "hk")
        self.assertEqual(result["currency"], "HKD")
        self.assertIn("# HK Daily Shadow Report - 2026-07-02", result["markdown"])
        self.assertIn("| 00700.HK | 100 | hk_momentum | buy | 0.7000 |", result["markdown"])
        self.assertFalse(report.delivery_policy(result)["real_execution"])
        with self.assertRaisesRegex(RuntimeError, "real/live execution is rejected"):
            report.render_daily(
                "2026-07-02",
                shadow_records=[{"symbol": "00700.HK", "strategy_name": "bad", "execution_mode": "live"}],
            )

    def test_hk_forward_validation_reports_hkd_oos_performance(self) -> None:
        from HK.common import HKConfig
        from HK.validation import HKForwardValidation

        validator = HKForwardValidation(config=HKConfig(), train_end="2026-07-01")
        result = validator.validate(
            [
                {"strategy_name": "old_train", "signal_date": "20260701", "as_of": "2026-07-02", "return_pct": 0.9, "pnl": 9999},
                {"strategy_name": "hk_momentum", "signal_date": "2026-7-2", "as_of": "2026-07-02", "return_pct": 0.05, "pnl": 1200},
                {"strategy_name": "hk_value", "signal_date": "20260702", "as_of": "2026-07-02", "return_pct": -0.03, "pnl": -500},
                {"strategy_name": "hk_nan", "signal_date": "2026-07-02", "as_of": "2026-07-02", "return_pct": float("nan"), "pnl": float("nan")},
                {"strategy_name": "future", "signal_date": "2026-07-03", "as_of": "2026-07-03", "return_pct": 0.8, "pnl": 8888},
            ],
            as_of="2026-07-02",
        )

        self.assertEqual(result["market"], "hk")
        self.assertEqual(result["currency"], "HKD")
        self.assertEqual(result["validation_type"], "out_of_sample")
        self.assertEqual(result["total_pnl"], 700)
        self.assertEqual(result["total"], 3)
        self.assertAlmostEqual(result["positive_rate"], 1 / 3)
        self.assertAlmostEqual(result["avg_return_pct"], round((0.05 - 0.03) / 3, 6))
        with self.assertRaisesRegex(RuntimeError, "real/live execution is rejected"):
            validator.validate(
                [{"strategy_name": "hk_value", "return_pct": 0.1, "live_broker_enabled": True}],
                as_of="2026-07-02",
            )

    def test_hk_strategy_promotion_uses_five_tiers_and_rejects_live_broker(self) -> None:
        from HK.common import HKConfig
        from HK.promotion import HKStrategyPromotion

        promoter = HKStrategyPromotion(config=HKConfig())
        result = promoter.evaluate(
            {
                "strategy_name": "hk_momentum",
                "shadow_trades": 12,
                "positive_days_pct": 0.58,
                "oos_return_pct": 0.03,
                "drawdown_pct": -0.06,
            }
        )

        self.assertEqual(promoter.tiers, ("research", "shadow_candidate", "shadow", "sim_candidate", "sim"))
        self.assertEqual(result["current_tier"], "shadow")
        self.assertEqual(result["next_tier"], "sim_candidate")
        self.assertEqual(result["currency"], "HKD")

        with self.assertRaises(SafetyViolation):
            HKStrategyPromotion(config=MarketToolConfig(market="hk", safety={"live_broker_enabled": True}))


if __name__ == "__main__":
    unittest.main()
