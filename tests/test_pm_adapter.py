from __future__ import annotations

import unittest

from PM.adapter import PMAdapter
from PM.scoring import score_market
from shared.markets.sim_capital import default_sim_capital


class MockPMReader:
    def get_pm_universe(self) -> list[str]:
        return ["will-btc-hit-100k", "will-fed-cut-rates"]

    def get_pm_markets(self, active_only: bool = True) -> list[dict[str, object]]:
        rows = [
            {
                "market_id": "will-btc-hit-100k",
                "title": "Will BTC hit 100k?",
                "description": "Resolves from public BTC/USD price data.",
                "category": "crypto",
                "status": "ACTIVE",
                "volume": 25000,
                "liquidity": 18000,
                "end_date": "2026-07-15",
                "resolution_source": "Coinbase BTC/USD",
                "model_probability": 0.72,
                "sentiment_score": 0.65,
            },
            {
                "market_id": "resolved-market",
                "status": "RESOLVED",
            },
        ]
        if not active_only:
            return rows
        return [row for row in rows if row.get("status") == "ACTIVE"]

    def get_pm_prices(self, market_id: str, start: object = None, end: object = None) -> list[dict[str, object]]:
        if market_id != "will-btc-hit-100k":
            return []
        return [
            {
                "market_id": market_id,
                "timestamp": "2026-06-30T10:00:00",
                "last_price": 0.60,
                "bid_ask_spread": 0.015,
                "model_probability": 0.72,
                "sentiment_score": 0.65,
            }
        ]


class PMAdapterTest(unittest.TestCase):
    def test_universe_comes_from_pm_reader(self) -> None:
        adapter = PMAdapter(reader=MockPMReader())

        self.assertEqual(
            adapter.get_universe("20260630"),
            ["will-btc-hit-100k", "will-fed-cut-rates"],
        )

    def test_symbol_mapping_uses_pm_market_id(self) -> None:
        adapter = PMAdapter(reader=MockPMReader())

        self.assertEqual(
            adapter.map_symbol_to_reader("will-btc-hit-100k"),
            ("pm", "will-btc-hit-100k"),
        )
        self.assertEqual(adapter.get_market(), "pm")
        self.assertEqual(adapter.get_shadow_account(), "pm_shadow")

    def test_strategy_config_contains_six_pm_strategies(self) -> None:
        config = PMAdapter(reader=MockPMReader()).get_strategy_config()

        self.assertEqual(config["shadow_capital"], default_sim_capital("pm"))
        self.assertTrue(config["probability_unit"])
        self.assertEqual(
            set(config["strategies"]),
            {
                "probability_arbitrage",
                "event_driven",
                "kelly_sizing",
                "nlp_sentiment",
                "early_exit",
                "calibration_arbitrage",
            },
        )

    def test_pm_scoring_uses_probability_dimensions(self) -> None:
        scores = score_market("will-btc-hit-100k", "20260630", data_reader=MockPMReader())

        self.assertGreater(scores["combined"], 0.5)
        self.assertLessEqual(scores["combined"], 1.0)
        for key in (
            "probability_value",
            "liquidity",
            "event_clarity",
            "time_to_settlement",
            "sentiment",
        ):
            self.assertIn(key, scores)
            self.assertGreaterEqual(scores[key], 0.0)
            self.assertLessEqual(scores[key], 1.0)

        for six_dim_key in ("macro", "fundamental", "capital", "technical"):
            self.assertNotIn(six_dim_key, scores)


if __name__ == "__main__":
    unittest.main()
