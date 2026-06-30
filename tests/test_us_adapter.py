from __future__ import annotations

import unittest

from US.adapter import USAdapter
from shared.screening.six_dimension_scorer import score_stock


class FakeUSReader:
    def __init__(self) -> None:
        self.assets = [
            {
                "symbol": "aapl",
                "name": "Apple Inc.",
                "exchange": "NASDAQ",
                "status": "active",
                "index_memberships": ["S&P 500", "Nasdaq 100"],
            },
            {
                "symbol": "MSFT",
                "name": "Microsoft Corp.",
                "exchange": "NASDAQ",
                "status": "tradable",
                "indices": "S&P500,Nasdaq 100",
            },
            {
                "symbol": "TSLA",
                "name": "Tesla Inc.",
                "exchange": "NASDAQ",
                "status": "inactive",
                "indices": "S&P 500",
            },
            {
                "symbol": "CASH",
                "name": "Cash Stub",
                "exchange": "NYSE",
                "status": "active",
                "indices": "",
            },
        ]

    def get_assets(self, market: str) -> list[dict[str, object]]:
        return self.assets if market in {"us", "US"} else []


class FakeScoringReader:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def get_regime(self) -> dict[str, object]:
        return {"regime": "growth", "regime_confidence": 1.0}

    def get_events(
        self,
        market: str,
        symbol: str,
        start: object = None,
        end: object = None,
    ) -> list[dict[str, object]]:
        self.calls.append(("events", market, symbol))
        return [{"confidence": 0.7, "direction": "positive"}]

    def get_event_candidates(self) -> list[dict[str, object]]:
        return []

    def get_factors(self, market: str, symbol: str) -> list[dict[str, object]]:
        self.calls.append(("factors", market, symbol))
        return [
            {"factor_name": "value", "event_time": "20260630", "value": 0.75},
            {"factor_name": "growth", "event_time": "20260630", "value": 0.7},
            {"factor_name": "quality", "event_time": "20260630", "value": 0.8},
            {"factor_name": "momentum", "event_time": "20260630", "value": 0.65},
            {"factor_name": "net_mf_amount", "event_time": "20260630", "value": 150000.0},
        ]

    def get_bars_daily(
        self,
        market: str,
        symbol: str,
        start: object = None,
        end: object = None,
    ) -> list[dict[str, object]]:
        self.calls.append(("bars", market, symbol))
        closes = [100.0 + idx for idx in range(25)]
        return [{"trade_date": f"202606{idx + 1:02d}", "close": close} for idx, close in enumerate(closes)]

    def get_sentiment(self) -> list[dict[str, object]]:
        return []


class USAdapterTest(unittest.TestCase):
    def test_universe_filters_to_active_sp500_and_nasdaq100_members(self) -> None:
        adapter = USAdapter(reader=FakeUSReader())

        universe = adapter.get_universe("20260630")

        self.assertEqual(universe, ["AAPL", "MSFT"])

    def test_symbol_mapping_uses_uppercase_us_ticker(self) -> None:
        adapter = USAdapter(reader=FakeUSReader())

        self.assertEqual(adapter.get_market(), "us")
        self.assertEqual(adapter.map_symbol_to_reader("AAPL"), ("us", "AAPL"))
        self.assertEqual(adapter.map_symbol_to_reader("brk.b"), ("us", "BRK.B"))
        self.assertEqual(adapter.get_shadow_account(), "us_shadow")

    def test_strategy_config_loads_five_strategies_and_us_market_rules(self) -> None:
        adapter = USAdapter(reader=FakeUSReader())

        config = adapter.get_strategy_config()

        self.assertEqual(config["market"], "us")
        self.assertEqual(len(config["strategies"]), 5)
        self.assertEqual(
            set(config["strategies"]),
            {"momentum", "value", "earnings_drift", "sector_rotation", "trend"},
        )
        self.assertEqual(config["market_rules"]["settlement"], "T+2")
        self.assertTrue(config["market_rules"]["can_sell_same_day"])
        self.assertIn("premarket", config["market_rules"]["sessions"])
        self.assertIn("after_hours", config["market_rules"]["sessions"])

    def test_six_dimension_score_uses_us_market_for_reader_queries(self) -> None:
        reader = FakeScoringReader()

        scores = score_stock("us", "AAPL", reader, "20260630")

        self.assertGreater(scores["combined"], 0.5)
        self.assertIn(("events", "us", "AAPL"), reader.calls)
        self.assertIn(("factors", "us", "AAPL"), reader.calls)
        self.assertIn(("bars", "us", "AAPL"), reader.calls)


if __name__ == "__main__":
    unittest.main()
