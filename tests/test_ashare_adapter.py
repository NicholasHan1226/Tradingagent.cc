from __future__ import annotations

import unittest

from Ashare.adapter import AshareAdapter
from shared.screening.six_dimension_scorer import score_stock


class FakeAshareReader:
    def __init__(self) -> None:
        self.assets = [
            {
                "symbol": "600519",
                "name": "Kweichow Moutai",
                "exchange": "SSE",
                "list_date": "20010827",
                "status": "active",
            },
            {
                "symbol": "000001",
                "name": "Ping An Bank",
                "exchange": "SZSE",
                "list_date": "19910403",
                "status": "active",
            },
            {
                "symbol": "000002",
                "name": "ST Unit",
                "exchange": "SZSE",
                "list_date": "19910101",
                "status": "active",
            },
            {
                "symbol": "688001",
                "name": "New Unit",
                "exchange": "SSE",
                "list_date": "20260615",
                "status": "active",
            },
            {
                "symbol": "430001",
                "name": "BSE Unit",
                "exchange": "BSE",
                "list_date": "20200101",
                "status": "active",
            },
            {
                "symbol": "200521.SZ",
                "name": "B Share Unit",
                "exchange": "SZSE",
                "list_date": "20000101",
                "status": "active",
            },
            {
                "symbol": "900901.SH",
                "name": "Shanghai B Share Unit",
                "exchange": "SSE",
                "list_date": "20000101",
                "status": "active",
            },
            {
                "symbol": "600000",
                "name": "Suspended Unit",
                "exchange": "SSE",
                "list_date": "19991110",
                "status": "active",
            },
            {
                "symbol": "600001",
                "name": "Illiquid Unit",
                "exchange": "SSE",
                "list_date": "19991110",
                "status": "active",
            },
            {
                "symbol": "600002",
                "name": "Tushare Amount Unit",
                "exchange": "SSE",
                "list_date": "19991110",
                "status": "active",
            },
            {
                "symbol": "600003",
                "name": "Missing Daily Bar Unit",
                "exchange": "SSE",
                "list_date": "19991110",
                "status": "active",
            },
        ]
        self.coverage = {
            "600519": "normal",
            "000001": "ok",
            "000002": "normal",
            "688001": "normal",
            "430001": "normal",
            "200521.SZ": "normal",
            "900901.SH": "normal",
            "600000": "suspended",
            "600001": "normal",
            "600002": "normal",
            "600003": "normal",
        }
        # Tushare daily amount is stored in thousand CNY in the read model.
        self.amounts = {
            "600519": 90_000,
            "000001": 70_000,
            "000002": 80_000,
            "688001": 80_000,
            "430001": 80_000,
            "200521.SZ": 80_000,
            "900901.SH": 80_000,
            "600000": 80_000,
            "600001": 10_000,
            "600002": 60_000,
        }

    def get_assets(self, market: str) -> list[dict[str, object]]:
        return self.assets if market in {"ashare", "Ashare"} else []

    def get_coverage(self, market: str, date: str) -> list[dict[str, object]]:
        return [
            {"symbol": symbol, "coverage_status": status}
            for symbol, status in self.coverage.items()
        ]

    def get_bars_daily(self, market: str, symbol: str, start: object = None, end: object = None) -> list[dict[str, object]]:
        amount = self.amounts.get(symbol)
        if amount is None:
            return []
        return [{"trade_date": "20260630", "close": 10.0, "amount": amount}]


class FakeScoringReader:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def get_regime(self) -> dict[str, object]:
        return {"regime": "growth", "regime_confidence": 1.0}

    def get_events(self, market: str, symbol: str, start: object = None, end: object = None) -> list[dict[str, object]]:
        self.calls.append(("events", market, symbol))
        return [{"confidence": 0.6, "direction": "positive"}]

    def get_event_candidates(self) -> list[dict[str, object]]:
        return []

    def get_factors(self, market: str, symbol: str) -> list[dict[str, object]]:
        self.calls.append(("factors", market, symbol))
        return [
            {"factor_name": "value", "event_time": "20260630", "value": 0.8},
            {"factor_name": "growth", "event_time": "20260630", "value": 0.7},
            {"factor_name": "quality", "event_time": "20260630", "value": 0.6},
            {"factor_name": "momentum", "event_time": "20260630", "value": 0.5},
            {"factor_name": "net_mf_amount", "event_time": "20260630", "value": 10000.0},
        ]

    def get_bars_daily(self, market: str, symbol: str, start: object = None, end: object = None) -> list[dict[str, object]]:
        self.calls.append(("bars", market, symbol))
        closes = [10.0] * 15 + [10.2, 10.4, 10.6, 10.8, 11.0]
        return [{"trade_date": f"202606{idx + 1:02d}", "close": close} for idx, close in enumerate(closes)]

    def get_sentiment(self) -> list[dict[str, object]]:
        return []


class AshareAdapterTest(unittest.TestCase):
    def test_universe_filter_excludes_st_suspended_new_illiquid_and_bse(self) -> None:
        adapter = AshareAdapter(reader=FakeAshareReader())

        universe = adapter.get_universe("20260630")

        self.assertEqual(universe, ["600519", "000001", "600002"])

    def test_symbol_mapping_uses_reader_symbol_without_exchange_suffix(self) -> None:
        adapter = AshareAdapter(reader=FakeAshareReader())

        self.assertEqual(adapter.get_market(), "ashare")
        self.assertEqual(adapter.map_symbol_to_reader("600519.SH"), ("ashare", "600519.SH"))
        self.assertEqual(adapter.map_symbol_to_reader("000001"), ("ashare", "000001"))
        self.assertEqual(adapter.get_shadow_account(), "ashare_shadow")

    def test_strategy_config_loads_eight_strategies_and_market_rules(self) -> None:
        adapter = AshareAdapter(reader=FakeAshareReader())

        config = adapter.get_strategy_config()

        self.assertEqual(config["market"], "ashare")
        self.assertEqual(len(config["strategies"]), 8)
        self.assertIn("short_breakout", config["strategies"])
        self.assertIn("trend_follow", config["strategies"])
        self.assertEqual(config["market_rules"]["settlement"], "T+1")
        self.assertIn("opening_auction", config["market_rules"]["sessions"])
        self.assertEqual(config["sim_capital"], 200_000.0)
        self.assertEqual(config["default_price"], 0.0)
        self.assertTrue(config["universe_filter"]["exclude_non_a_share"])

    def test_six_dimension_score_uses_injected_market_for_reader_queries(self) -> None:
        reader = FakeScoringReader()

        scores = score_stock("ashare", "600519.SH", reader, "20260630")

        self.assertGreater(scores["combined"], 0.5)
        self.assertIn(("events", "ashare", "600519"), reader.calls)
        self.assertIn(("factors", "ashare", "600519"), reader.calls)
        self.assertIn(("bars", "ashare", "600519"), reader.calls)


if __name__ == "__main__":
    unittest.main()
