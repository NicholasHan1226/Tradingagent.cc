#!/usr/bin/env python3
"""Tests for China futures automated simulation lanes."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class FakeFuturesReader:
    def get_assets(self, market: str) -> list[dict[str, object]]:
        if market != "Futures":
            return []
        return [
            {"symbol": "rb2601", "name": "螺纹钢2601", "exchange": "SHFE", "status": "listed"},
            {"symbol": "cu2601", "name": "沪铜2601", "exchange": "SHFE", "status": "listed"},
        ]

    def get_bars_daily(
        self,
        market: str,
        symbol: str,
        start: object = None,
        end: object = None,
    ) -> list[dict[str, object]]:
        self.last_market = market
        self.last_symbol = symbol
        rows = {
            "rb2601": [
                {"trade_date": "20260701", "close": 3400, "volume": 1000},
                {"trade_date": "20260702", "close": 3450, "volume": 1300},
                {"trade_date": "20260703", "close": 3520, "volume": 1800},
            ],
            "cu2601": [
                {"trade_date": "20260701", "close": 70000, "volume": 1000},
                {"trade_date": "20260702", "close": 69950, "volume": 900},
                {"trade_date": "20260703", "close": 69850, "volume": 800},
            ],
        }
        return rows.get(symbol, [])


class CNFuturesAutomationTest(unittest.TestCase):
    def test_adapter_reads_futures_assets_without_using_trading_logic_upstream(self) -> None:
        from CNFutures.adapter import CNFuturesAdapter

        adapter = CNFuturesAdapter(reader=FakeFuturesReader(), universe_filter={"max_symbols": 1})

        self.assertEqual(adapter.get_market(), "cn_futures")
        self.assertEqual(adapter.map_symbol_to_reader("rb2601"), ("Futures", "rb2601"))
        self.assertEqual(adapter.get_universe("20260703"), ["rb2601"])
        self.assertEqual(adapter.get_sim_account()["account"], "cn_futures_sim")
        self.assertEqual(adapter.get_strategy_config()["capital_layer"], "simulated")

    def test_multi_style_runner_executes_only_simulated_lanes_and_writes_review(self) -> None:
        import CNFutures.sim_executor  # noqa: F401
        from CNFutures.adapter import CNFuturesAdapter
        from CNFutures.sim_runner import run_multi_style_simulation

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            adapter = CNFuturesAdapter(
                reader=FakeFuturesReader(),
                universe_filter={"max_symbols": 1},
                styles={
                    "trend": {"name": "trend", "signal_threshold": 0.01, "risk_per_trade": 0.03},
                    "breakout": {"name": "breakout", "signal_threshold": 0.015, "risk_per_trade": 0.02},
                },
            )

            result = run_multi_style_simulation(
                adapter,
                "20260703",
                FakeFuturesReader(),
                signals_dir=tmp_path / "signals",
                review_path=tmp_path / "cn_futures_reviews.jsonl",
            )

            self.assertEqual(result["state"], "ok")
            self.assertEqual(result["capital_layer"], "simulated")
            self.assertEqual(result["market"], "cn_futures")
            self.assertEqual(result["style_count"], 2)
            self.assertEqual(result["filled_count"], 2)
            self.assertEqual(result["real_trading_enabled"], False)
            self.assertEqual({row["style"] for row in result["records"]}, {"trend", "breakout"})
            self.assertTrue(all(row["receipt"]["capital_layer"] == "simulated" for row in result["records"]))
            self.assertTrue(all(row["signal_card"]["account_type"] == "simulated" for row in result["records"]))
            filled_files = list((tmp_path / "signals" / "filled").glob("SIM-CNF-*.json"))
            self.assertEqual(len(filled_files), 2)

            review_rows = [
                json.loads(line)
                for line in (tmp_path / "cn_futures_reviews.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(len(review_rows), 1)
            self.assertEqual(review_rows[0]["filled_count"], 2)
            self.assertEqual(review_rows[0]["styles"]["trend"]["filled_count"], 1)
            self.assertEqual(review_rows[0]["styles"]["breakout"]["filled_count"], 1)


if __name__ == "__main__":
    unittest.main()
