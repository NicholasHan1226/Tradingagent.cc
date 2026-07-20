from __future__ import annotations

import unittest


def _bars(prices: list[float], *, start: int = 1) -> list[dict[str, object]]:
    return [
        {"trade_date": f"2026-07-{idx + start:02d}", "close": price}
        for idx, price in enumerate(prices)
    ]


class CryptoP2ToolsTest(unittest.TestCase):
    def test_risk_scores_public_funding_news_and_volatility(self) -> None:
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

    def test_risk_rejects_signed_or_real_payloads(self) -> None:
        from Crypto.risk import CryptoRiskBackground

        with self.assertRaisesRegex(RuntimeError, "real/live execution is rejected"):
            CryptoRiskBackground().score(
                symbol="BTCUSDT",
                news_events=[{"headline": "bad", "api_key": "secret"}],
            )

    def test_portfolio_builds_correlation_matrix_and_volatility_sizing(self) -> None:
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
            capital=10_000,
        )

        self.assertIn("BTCUSDT", result["correlation_matrix"])
        self.assertEqual(result["currency"], "USDT")
        self.assertTrue(result["positions"])
        self.assertTrue(
            all(row["target_weight"] <= 0.15 for row in result["positions"])
        )
        self.assertFalse(result["real_execution"])

    def test_portfolio_rejects_real_candidates(self) -> None:
        from Crypto.portfolio import CryptoPortfolioOptimizer

        with self.assertRaisesRegex(RuntimeError, "real/live execution is rejected"):
            CryptoPortfolioOptimizer().optimize(
                [{"symbol": "BTCUSDT", "score": 1, "capital_layer": "real"}],
                {"BTCUSDT": _bars([100, 101])},
            )

    def test_replay_backtests_shadow_rules(self) -> None:
        from Crypto.replay import CryptoHistoricalReplay

        result = CryptoHistoricalReplay().replay(
            {"BTCUSDT": _bars([100, 103, 106, 104])},
            [
                {
                    "symbol": "BTCUSDT",
                    "lookback": 1,
                    "threshold": 0.01,
                    "size_pct": 0.1,
                }
            ],
            initial_cash=10_000,
        )

        self.assertEqual(result["market"], "crypto")
        self.assertGreater(result["trade_count"], 0)
        self.assertEqual(
            {trade["capital_layer"] for trade in result["trades"]}, {"shadow"}
        )
        self.assertFalse(result["real_execution"])

    def test_replay_rejects_live_rules(self) -> None:
        from Crypto.replay import CryptoHistoricalReplay

        with self.assertRaisesRegex(RuntimeError, "real/live execution is rejected"):
            CryptoHistoricalReplay().replay(
                {"BTCUSDT": _bars([100, 101, 102])},
                [{"symbol": "BTCUSDT", "live": True}],
            )


if __name__ == "__main__":
    unittest.main()
