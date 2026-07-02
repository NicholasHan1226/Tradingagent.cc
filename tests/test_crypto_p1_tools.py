from __future__ import annotations

import unittest

from Crypto.common import CryptoConfig
from Crypto.promotion import CryptoStrategyPromotion
from Crypto.report import CryptoDailyReport
from Crypto.validation import CryptoForwardValidation


def _crypto_records() -> list[dict[str, object]]:
    symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"]
    return [
        {
            "trade_date": "2026-07-02",
            "symbol": symbol,
            "strategy_name": "momentum_shadow",
            "pnl": 10.0 + idx,
            "return": 0.01,
            "direction_hit": True,
            "triggered": True,
            "belief_score": 0.7,
            "capital_layer": "shadow",
            "account_type": "shadow",
            "status": "triggered",
        }
        for idx, symbol in enumerate(symbols)
    ]


class CryptoP1ToolsTest(unittest.TestCase):
    def test_daily_report_no_trigger_skips_empty_delivery(self) -> None:
        report = CryptoDailyReport(
            records=[
                {
                    "trade_date": "2026-07-02",
                    "symbol": "BTCUSDT",
                    "pnl": 0,
                    "capital_layer": "shadow",
                    "account_type": "shadow",
                    "status": "waiting",
                }
            ]
        )

        result = report.render_daily("2026-07-02")

        self.assertEqual(result["trigger_count"], 0)
        self.assertEqual(result["delivery"]["status"], "no_send")
        self.assertEqual(result["capital_layer"], "shadow")

    def test_forward_validation_reports_oos_quality_and_pnl(self) -> None:
        validator = CryptoForwardValidation(records=_crypto_records(), train_end="2026-07-01")

        result = validator.evaluate(as_of="2026-07-02")

        self.assertEqual(result["oos_count"], 5)
        self.assertEqual(result["win_rate"], 1.0)
        self.assertGreaterEqual(result["sample_quality"]["score"], 65)
        self.assertEqual(result["sample_quality"]["grade"], "thin")
        self.assertEqual(result["account_type"], "shadow")

    def test_strategy_promotion_has_five_tier_sim_ready_gate(self) -> None:
        config = CryptoConfig(
            promotion={"min_shadow_trades": 3, "min_positive_days_pct": 0.6},
        )
        promotion = CryptoStrategyPromotion(
            config,
            records=_crypto_records(),
            train_end="2026-07-01",
        )

        result = promotion.score("momentum_shadow", as_of="2026-07-02")

        self.assertEqual(result["tier"], "tier_4_sim_ready")
        self.assertTrue(result["eligible_for_sim"])
        self.assertEqual(result["target_layer"], "simulated")
        self.assertFalse(result["real_execution"])

    def test_p1_tools_reject_real_execution_payloads(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "real/live execution is rejected"):
            CryptoDailyReport(
                records=[
                    {
                        "trade_date": "2026-07-02",
                        "symbol": "BTCUSDT",
                        "capital_layer": "real",
                    }
                ]
            )


if __name__ == "__main__":
    unittest.main()
