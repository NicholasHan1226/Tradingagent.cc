from __future__ import annotations

import unittest

from PM.common import PMConfig
from PM.promotion import PMStrategyPromotion
from PM.report import PMDailyReport
from PM.validation import PMForwardValidation
from shared.markets.config_schema import PromotionConfig


def _pm_records() -> list[dict[str, object]]:
    return [
        {
            "trade_date": "2026-07-02",
            "market_id": f"market-{idx}",
            "strategy_name": "calibration_shadow",
            "prediction": 0.8,
            "actual": 1,
            "pnl": 3.0,
            "capital_layer": "shadow",
            "account_type": "shadow",
        }
        for idx in range(3)
    ]


class PMP1ToolsTest(unittest.TestCase):
    def test_daily_report_tracks_brier_and_pnl(self) -> None:
        report = PMDailyReport(records=_pm_records())

        result = report.render_daily("2026-07-02")

        self.assertEqual(result["resolved_count"], 3)
        self.assertAlmostEqual(result["brier_score"], 0.04)
        self.assertEqual(result["pnl"], 9.0)
        self.assertEqual(result["delivery"]["status"], "ready")

    def test_forward_validation_returns_calibration_bins(self) -> None:
        validator = PMForwardValidation(records=_pm_records(), train_end="2026-07-01")

        result = validator.evaluate(as_of="2026-07-02")

        self.assertEqual(result["oos_count"], 3)
        self.assertEqual(result["resolved_count"], 3)
        self.assertAlmostEqual(result["brier_score"], 0.04)
        self.assertEqual(result["calibration"][4]["count"], 3)

    def test_strategy_promotion_research_shadow_sim_tiers(self) -> None:
        config = PMConfig(
            promotion=PromotionConfig(min_shadow_trades=3, min_positive_days_pct=0.55),
        )
        promotion = PMStrategyPromotion(
            config,
            records=_pm_records(),
            train_end="2026-07-01",
        )

        result = promotion.score("calibration_shadow", as_of="2026-07-02")

        self.assertEqual(result["tier"], "tier_4_sim_ready")
        self.assertTrue(result["eligible_for_sim"])
        self.assertEqual(result["target_layer"], "simulated")
        self.assertFalse(result["real_execution"])

    def test_p1_tools_reject_real_execution_payloads(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "real/live execution is rejected"):
            PMForwardValidation(
                records=[
                    {
                        "trade_date": "2026-07-02",
                        "market_id": "real-market",
                        "prediction": 0.6,
                        "actual": 1,
                        "account_type": "real",
                    }
                ]
            )


if __name__ == "__main__":
    unittest.main()
