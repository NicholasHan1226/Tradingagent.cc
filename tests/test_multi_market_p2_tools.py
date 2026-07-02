from __future__ import annotations

import unittest

from shared.markets.config_schema import MarketToolConfig
from shared.markets.safety import SafetyViolation


def _bars(prices: list[float], *, start: int = 1) -> list[dict[str, object]]:
    return [
        {"trade_date": f"2026-07-{idx + start:02d}", "close": price}
        for idx, price in enumerate(prices)
    ]


class MultiMarketP2ToolsTest(unittest.TestCase):
    def test_crypto_risk_scores_funding_news_and_volatility_public_data(self) -> None:
        from Crypto.risk import CryptoRiskBackground

        result = CryptoRiskBackground().score(
            symbol="btcusdt",
            funding_rates=[{"funding_rate": 0.0015}, {"funding_rate": -0.001}],
            news_events=[{"sentiment": "negative", "severity": 0.6}],
            bars=_bars([100, 108, 95, 103, 91]),
            as_of="2026-07-05",
        )

        self.assertEqual(result["market"], "crypto")
        self.assertEqual(result["symbol"], "BTCUSDT")
        self.assertGreater(result["risk_score"], 0)
        self.assertEqual(result["capital_layer"], "shadow")
        self.assertTrue(result["public_data_only"])
        self.assertFalse(result["real_execution"])

    def test_crypto_risk_rejects_signed_or_real_payloads(self) -> None:
        from Crypto.risk import CryptoRiskBackground

        with self.assertRaisesRegex(RuntimeError, "real/live execution is rejected"):
            CryptoRiskBackground().score(
                symbol="BTCUSDT",
                news_events=[{"headline": "bad", "api_key": "secret"}],
            )

    def test_crypto_portfolio_builds_correlation_matrix_and_vol_sizing(self) -> None:
        from Crypto.portfolio import CryptoPortfolioOptimizer

        result = CryptoPortfolioOptimizer(correlation_cap=0.99).optimize(
            [
                {"symbol": "BTCUSDT", "score": 0.9},
                {"symbol": "ETHUSDT", "score": 0.8},
            ],
            {
                "BTCUSDT": _bars([100, 110, 105, 115]),
                "ETHUSDT": _bars([50, 55, 52, 58]),
            },
            capital=10000,
        )

        self.assertIn("BTCUSDT", result["correlation_matrix"])
        self.assertEqual(result["currency"], "USDT")
        self.assertTrue(result["positions"])
        self.assertTrue(all(row["target_weight"] <= 0.15 for row in result["positions"]))
        self.assertFalse(result["real_execution"])

    def test_crypto_portfolio_rejects_real_candidates(self) -> None:
        from Crypto.portfolio import CryptoPortfolioOptimizer

        with self.assertRaisesRegex(RuntimeError, "real/live execution is rejected"):
            CryptoPortfolioOptimizer().optimize(
                [{"symbol": "BTCUSDT", "score": 1, "capital_layer": "real"}],
                {"BTCUSDT": _bars([100, 101])},
            )

    def test_crypto_replay_backtests_shadow_rules(self) -> None:
        from Crypto.replay import CryptoHistoricalReplay

        result = CryptoHistoricalReplay().replay(
            {"BTCUSDT": _bars([100, 103, 106, 104])},
            [{"symbol": "BTCUSDT", "lookback": 1, "threshold": 0.01, "size_pct": 0.1}],
            initial_cash=10000,
        )

        self.assertEqual(result["market"], "crypto")
        self.assertGreater(result["trade_count"], 0)
        self.assertEqual({trade["capital_layer"] for trade in result["trades"]}, {"shadow"})
        self.assertFalse(result["real_execution"])

    def test_crypto_replay_rejects_live_rules(self) -> None:
        from Crypto.replay import CryptoHistoricalReplay

        with self.assertRaisesRegex(RuntimeError, "real/live execution is rejected"):
            CryptoHistoricalReplay().replay(
                {"BTCUSDT": _bars([100, 101, 102])},
                [{"symbol": "BTCUSDT", "live": True}],
            )

    def test_us_portfolio_gates_highly_correlated_candidate(self) -> None:
        from US.portfolio import USPortfolioOptimizer

        result = USPortfolioOptimizer(correlation_cap=0.75).gate(
            [{"symbol": "MSFT", "score": 0.9}, {"symbol": "TSLA", "score": 0.7}],
            [{"symbol": "AAPL", "weight": 0.1}],
            {
                "AAPL": _bars([100, 105, 110, 115]),
                "MSFT": _bars([200, 210, 220, 230]),
                "TSLA": _bars([300, 290, 305, 295]),
            },
        )

        self.assertEqual(result["market"], "us")
        self.assertTrue(any(row["symbol"] == "MSFT" for row in result["rejected"]))
        self.assertFalse(result["real_execution"])

    def test_us_portfolio_rejects_live_broker_config_and_payload(self) -> None:
        from US.portfolio import USPortfolioOptimizer

        with self.assertRaises(SafetyViolation):
            USPortfolioOptimizer(config=MarketToolConfig(market="us", safety={"live_broker_enabled": True}))
        with self.assertRaisesRegex(RuntimeError, "real/live execution is rejected"):
            USPortfolioOptimizer().gate(
                [{"symbol": "AAPL", "direct_execution": True}],
                [],
                {"AAPL": _bars([100, 101])},
            )

    def test_us_replay_returns_historical_metrics(self) -> None:
        from US.replay import USHistoricalReplay

        result = USHistoricalReplay().replay(
            {"AAPL": _bars([100, 104, 108, 106])},
            [{"symbol": "AAPL", "lookback": 1, "threshold": 0.02, "size_pct": 0.1}],
        )

        self.assertEqual(result["currency"], "USD")
        self.assertGreater(result["trade_count"], 0)
        self.assertEqual(result["capital_layer"], "shadow")

    def test_us_replay_rejects_real_rule_payloads(self) -> None:
        from US.replay import USHistoricalReplay

        with self.assertRaisesRegex(RuntimeError, "real/live execution is rejected"):
            USHistoricalReplay().replay(
                {"AAPL": _bars([100, 101])},
                [{"symbol": "AAPL", "account_type": "real"}],
            )

    def test_pm_risk_enforces_single_market_and_topic_caps(self) -> None:
        from PM.risk import PMRiskControl

        result = PMRiskControl(single_market_cap=0.05, topic_cap=0.10).evaluate(
            [
                {"market_id": "m1", "topic": "election", "exposure_pct": 0.04},
                {"market_id": "m2", "topic": "election", "exposure_pct": 0.04},
                {"market_id": "m3", "topic": "election", "exposure_pct": 0.04},
                {"market_id": "m4", "topic": "rates", "exposure_pct": 0.06},
            ]
        )

        self.assertEqual(result["currency"], "USDC")
        self.assertEqual(len(result["approved"]), 2)
        self.assertEqual({row["reason"] for row in result["violations"]}, {"correlated_topic_cap", "single_market_cap"})
        self.assertFalse(result["real_execution"])

    def test_pm_risk_rejects_real_positions(self) -> None:
        from PM.risk import PMRiskControl

        with self.assertRaisesRegex(RuntimeError, "real/live execution is rejected"):
            PMRiskControl().evaluate([{"market_id": "m1", "account_type": "real", "exposure_pct": 0.01}])

    def test_hk_portfolio_sizes_hkd_lots_and_sector_caps(self) -> None:
        from HK.portfolio import HKPortfolioOptimizer

        result = HKPortfolioOptimizer(sector_cap=0.12).optimize(
            [
                {"symbol": "700", "sector": "internet", "price_hkd": 380, "lot_size": 100, "target_weight": 0.12, "score": 0.9},
                {"symbol": "9988", "sector": "internet", "price_hkd": 80, "lot_size": 100, "target_weight": 0.12, "score": 0.8},
                {"symbol": "5", "sector": "financials", "price_hkd": 70, "lot_size": 400, "target_weight": 0.08, "score": 0.7},
            ],
            capital_hkd=500000,
        )

        self.assertEqual(result["currency"], "HKD")
        self.assertTrue(any(row["symbol"] == "00700.HK" for row in result["positions"]))
        self.assertTrue(any(row["reason"] == "sector_cap" for row in result["skipped"]))
        self.assertTrue(all(row["shares"] % row["lot_size"] == 0 for row in result["positions"]))
        self.assertFalse(result["real_execution"])

    def test_hk_portfolio_rejects_live_config_and_real_candidate(self) -> None:
        from HK.portfolio import HKPortfolioOptimizer

        with self.assertRaises(SafetyViolation):
            HKPortfolioOptimizer(config=MarketToolConfig(market="hk", safety={"live_broker_enabled": True}))
        with self.assertRaisesRegex(RuntimeError, "real/live execution is rejected"):
            HKPortfolioOptimizer().optimize(
                [{"symbol": "700", "price_hkd": 380, "lot_size": 100, "execution_mode": "live"}]
            )


if __name__ == "__main__":
    unittest.main()
