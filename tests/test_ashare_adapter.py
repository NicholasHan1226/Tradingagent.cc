from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from Ashare.adapter import AshareAdapter
from shared.execution import local_sim_ledger
from shared.screening.six_dimension_scorer import score_stock


class FakeAshareReader:
    def __init__(self) -> None:
        self.daily_calls: list[tuple[str, str, object, object]] = []
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
            {
                "symbol": "600004",
                "name": "No Liquidity Evidence Unit",
                "exchange": "SSE",
                "list_date": "19991110",
                "status": "active",
            },
            {
                "symbol": "600005",
                "name": "",
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
            "600004": "normal",
            "600005": "normal",
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
            "600004": "missing_amount",
            "600005": 90_000,
        }

    def get_assets(self, market: str) -> list[dict[str, object]]:
        return self.assets if market in {"ashare", "Ashare"} else []

    def get_coverage(self, market: str, date: str) -> list[dict[str, object]]:
        return [
            {"symbol": symbol, "coverage_status": status}
            for symbol, status in self.coverage.items()
        ]

    def get_bars_daily(self, market: str, symbol: str, start: object = None, end: object = None) -> list[dict[str, object]]:
        self.daily_calls.append((market, symbol, start, end))
        amount = self.amounts.get(symbol)
        if amount == "missing_amount":
            return [{"trade_date": "20260630", "close": 10.0}]
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
        reader = FakeAshareReader()
        adapter = AshareAdapter(reader=reader)

        universe = adapter.get_universe("20260630")

        self.assertEqual(universe, ["600519", "000001", "600002"])
        self.assertIn(("ashare", "600519", "20260616", "20260630"), reader.daily_calls)

    def test_universe_is_ranked_by_recent_liquidity_before_scoring_limit(self) -> None:
        reader = FakeAshareReader()
        reader.assets = [
            {"symbol": "000001", "name": "Lower Liquidity", "exchange": "SZSE", "list_date": "19910403", "status": "active"},
            {"symbol": "600002", "name": "Higher Liquidity", "exchange": "SSE", "list_date": "19991110", "status": "active"},
            {"symbol": "600519", "name": "High Liquidity", "exchange": "SSE", "list_date": "20010827", "status": "active"},
        ]
        reader.amounts.update({"000001": 55_000, "600002": 80_000, "600519": 120_000})
        adapter = AshareAdapter(reader=reader)

        universe = adapter.get_universe("20260630")

        self.assertEqual(universe, ["600519", "600002", "000001"])

    def test_symbol_mapping_uses_reader_symbol_without_exchange_suffix(self) -> None:
        adapter = AshareAdapter(reader=FakeAshareReader())

        self.assertEqual(adapter.get_market(), "ashare")
        self.assertEqual(adapter.map_symbol_to_reader("600519.SH"), ("ashare", "600519.SH"))
        self.assertEqual(adapter.map_symbol_to_reader("000001"), ("ashare", "000001"))
        self.assertEqual(adapter.get_shadow_account(), "ashare_shadow")

    def test_sim_account_reads_server_local_positions_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            snapshot = Path(tmpdir) / "simulated_ashare_positions.json"
            snapshot.write_text(
                json.dumps(
                    {
                        "source": "server_local_sim_backup",
                        "synced_at": "2026-07-06T07:31:01+00:00",
                        "positions": [
                            {
                                "account": "ashare_sim",
                                "ts_code": "000001.SZ",
                                "quantity": 700,
                                "avg_price": 10.3,
                                "last_price": 10.2,
                                "market_value": 7140.0,
                            },
                            {
                                "account": "other",
                                "ts_code": "000002.SZ",
                                "quantity": 100,
                                "market_value": 1000.0,
                            },
                        ],
                        "pnl": {"ashare_sim": {"cash_available": 123456.78}},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            with patch.object(local_sim_ledger, "LOCAL_SIM_POSITIONS_SNAPSHOT", snapshot):
                account = AshareAdapter(reader=FakeAshareReader()).get_sim_account()

        self.assertEqual(account["account"], "ashare_sim")
        self.assertEqual(account["cash_available"], 123456.78)
        self.assertEqual(len(account["positions"]), 1)
        self.assertEqual(account["positions"][0]["ts_code"], "000001.SZ")
        self.assertEqual(account["positions"][0]["sellable_quantity"], 700)
        self.assertEqual(account["positions"][0]["value"], 7140.0)

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
        self.assertEqual(config["score_universe_limit"], 500)
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
