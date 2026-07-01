from __future__ import annotations

import unittest

from shared.portfolio.constructor import build_portfolio, construct


class PortfolioMethodsTest(unittest.TestCase):
    def test_volatility_targeted_normalizes_and_keeps_hard_limits(self) -> None:
        orders = [
            {
                "ts_code": f"CRYPTO{i}",
                "volatility": vol,
                "volatility_baseline": 0.80,
                "sector": "crypto",
                "price": 1.0,
                "capital_layer": "shadow",
            }
            for i, vol in enumerate((0.35, 0.45, 0.55, 0.70, 0.90, 1.10), start=1)
        ]

        portfolio = construct(orders, 1_000_000_000.0, method="volatility_targeted", regime="crypto_24_7")

        self.assertEqual(portfolio["method"], "volatility_targeted")
        self.assertEqual(portfolio["capital_layer"], "shadow")
        self.assertAlmostEqual(portfolio["total_weight"], 0.80, places=6)
        self.assertEqual(len(portfolio["positions"]), len(orders))
        self.assertTrue(all(position["weight"] <= 0.15 for position in portfolio["positions"]))
        self.assertTrue(all(position["capital_layer"] == "shadow" for position in portfolio["positions"]))

    def test_pm_probability_weighted_normalizes_and_keeps_hard_limits(self) -> None:
        orders = [
            {
                "ts_code": f"PM{i}",
                "probability": probability,
                "market_price": price,
                "price": 1.0,
                "sector": "prediction_market",
                "capital_layer": "shadow",
            }
            for i, (probability, price) in enumerate(
                (
                    (0.82, 0.57),
                    (0.74, 0.60),
                    (0.68, 0.52),
                    (0.61, 0.49),
                    (0.38, 0.50),
                    (0.29, 0.43),
                    (0.91, 0.78),
                    (0.56, 0.51),
                ),
                start=1,
            )
        ]

        portfolio = build_portfolio(orders, 1_000_000_000.0, method="pm_probability_weighted", regime="24_7_probability_market")

        self.assertEqual(portfolio["method"], "pm_probability_weighted")
        self.assertEqual(portfolio["capital_layer"], "shadow")
        self.assertAlmostEqual(portfolio["total_weight"], 0.80, places=6)
        self.assertEqual(len(portfolio["positions"]), len(orders))
        self.assertTrue(all(position["weight"] <= 0.15 for position in portfolio["positions"]))
        self.assertTrue(all(position["capital_layer"] == "shadow" for position in portfolio["positions"]))

    def test_crypto_lot_size_allows_fractional_high_price_positions(self) -> None:
        orders = [
            {
                "ts_code": "BTCUSDT",
                "volatility": 0.80,
                "volatility_baseline": 0.80,
                "sector": "crypto",
                "price": 100000.0,
                "lot_size": 0.0001,
                "capital_layer": "shadow",
            }
        ]

        portfolio = construct(orders, 10000.0, method="volatility_targeted", regime="crypto_24_7")

        self.assertEqual(len(portfolio["positions"]), 1)
        position = portfolio["positions"][0]
        self.assertGreater(position["shares"], 0)
        self.assertLess(position["shares"], 1)
        self.assertAlmostEqual(position["amount"], 1500.0, places=2)


if __name__ == "__main__":
    unittest.main()
