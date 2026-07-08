#!/usr/bin/env python3
"""Tests for China futures automated simulation lanes."""

from __future__ import annotations

import json
import os
import subprocess
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

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

    def get_bars_intraday(
        self,
        market: str,
        symbol: str,
        interval: str = "5min",
        start: object = None,
        end: object = None,
    ) -> list[dict[str, object]]:
        self.last_intraday_market = market
        self.last_intraday_symbol = symbol
        rows = {
            "rb2601": [
                {"trade_date": "20260703", "bar_time": "2026-07-03 14:45:00", "close": 3450, "volume": 1000},
                {"trade_date": "20260703", "bar_time": "2026-07-03 14:50:00", "close": 3500, "volume": 1300},
                {"trade_date": "20260703", "bar_time": "2026-07-03 14:55:00", "close": 3560, "volume": 1800},
            ],
            "cu2601": [
                {"trade_date": "20260703", "bar_time": "2026-07-03 14:45:00", "close": 70000, "volume": 1000},
                {"trade_date": "20260703", "bar_time": "2026-07-03 14:50:00", "close": 69950, "volume": 900},
                {"trade_date": "20260703", "bar_time": "2026-07-03 14:55:00", "close": 69850, "volume": 800},
            ],
        }
        if market != "Futures" or interval != "5min":
            return []
        return rows.get(symbol, [])


class FakeMixedFuturesReader(FakeFuturesReader):
    def get_assets(self, market: str) -> list[dict[str, object]]:
        if market != "Futures":
            return []
        return [
            {"symbol": "IF2601.CFFEX", "name": "沪深300股指2601", "exchange": "CFFEX", "status": "listed"},
            {"symbol": "rb2601", "name": "螺纹钢2601", "exchange": "SHFE", "status": "listed"},
        ]

    def get_bars_intraday(
        self,
        market: str,
        symbol: str,
        interval: str = "5min",
        start: object = None,
        end: object = None,
    ) -> list[dict[str, object]]:
        if market != "Futures" or interval != "5min":
            return []
        rows = {
            "IF2601.CFFEX": [
                {"trade_date": "20260706", "bar_time": "2026-07-06 14:10:00", "close": 3500, "volume": 1000},
                {"trade_date": "20260706", "bar_time": "2026-07-06 14:15:00", "close": 3502, "volume": 1000},
                {"trade_date": "20260706", "bar_time": "2026-07-06 14:20:00", "close": 3505, "volume": 1100},
                {"trade_date": "20260706", "bar_time": "2026-07-06 14:25:00", "close": 3512, "volume": 1400},
                {"trade_date": "20260706", "bar_time": "2026-07-06 14:30:00", "close": 3520, "volume": 1600},
            ],
            "rb2601": [
                {"trade_date": "20260706", "bar_time": "2026-07-06 14:10:00", "close": 3450, "volume": 1000},
                {"trade_date": "20260706", "bar_time": "2026-07-06 14:15:00", "close": 3460, "volume": 1100},
                {"trade_date": "20260706", "bar_time": "2026-07-06 14:20:00", "close": 3470, "volume": 1200},
                {"trade_date": "20260706", "bar_time": "2026-07-06 14:25:00", "close": 3485, "volume": 1300},
                {"trade_date": "20260706", "bar_time": "2026-07-06 14:30:00", "close": 3500, "volume": 1500},
            ],
        }
        return rows.get(symbol, [])


class DateScopedFuturesReader(FakeFuturesReader):
    calls: list[tuple[object, object]] = []

    def get_bars_intraday(
        self,
        market: str,
        symbol: str,
        interval: str = "5min",
        start: object = None,
        end: object = None,
    ) -> list[dict[str, object]]:
        self.calls.append((start, end))
        if market != "Futures" or symbol != "rb2601" or interval != "5min" or start != "20260703" or end != "20260703":
            return []
        return [
            {"trade_date": "20260703", "bar_time": "2026-07-03 14:50:00", "close": 3500, "volume": 1300},
            {"trade_date": "20260703", "bar_time": "2026-07-03 14:55:00", "close": 3560, "volume": 1800},
        ]


class DateScopedUniverseReader(FakeFuturesReader):
    def __init__(self) -> None:
        self.calls: list[tuple[str, object, object]] = []

    def get_assets(self, market: str) -> list[dict[str, object]]:
        if market != "Futures":
            return []
        return [
            {"symbol": "CU.SHF", "name": "沪铜连续", "exchange": "SHFE", "status": "listed"},
            {"symbol": "CU0001.SHF", "name": "沪铜旧合约", "exchange": "SHFE", "status": "listed"},
            {"symbol": "CU2607.SHF", "name": "沪铜2607", "exchange": "SHFE", "status": "listed"},
            {"symbol": "RB2607.SHF", "name": "螺纹钢2607", "exchange": "SHFE", "status": "listed"},
        ]

    def get_bars_intraday(
        self,
        market: str,
        symbol: str,
        interval: str = "5min",
        start: object = None,
        end: object = None,
    ) -> list[dict[str, object]]:
        self.calls.append((symbol, start, end))
        if market != "Futures" or interval != "5min" or start != "20260707" or end != "20260707":
            return []
        if symbol in {"CU2607.SHF", "RB2607.SHF"}:
            return [{"trade_date": "20260707", "bar_time": "2026-07-07 14:30:00", "close": 3500, "volume": 1200}]
        return []


class SQLiteSharedQuery:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    def _query(self, sql: str, params: tuple[object, ...] = ()) -> list[dict[str, object]]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()


class ReadModelIntradayReader(FakeFuturesReader):
    def __init__(self, db_path: Path) -> None:
        self.shared = SQLiteSharedQuery(db_path)


class CNFuturesAutomationTest(unittest.TestCase):
    def test_adapter_default_reader_uses_tradingagent_data_reader(self) -> None:
        from CNFutures.adapter import CNFuturesAdapter
        from shared.data.reader import TradingagentDataReader

        adapter = CNFuturesAdapter()

        self.assertIsInstance(adapter.reader, TradingagentDataReader)

    def test_adapter_reads_futures_assets_without_using_trading_logic_upstream(self) -> None:
        from CNFutures.adapter import CNFuturesAdapter

        adapter = CNFuturesAdapter(reader=FakeFuturesReader(), universe_filter={"max_symbols": 1})

        self.assertEqual(adapter.get_market(), "cn_futures")
        self.assertEqual(adapter.map_symbol_to_reader("rb2601"), ("Futures", "rb2601"))
        self.assertEqual(adapter.map_symbol_to_reader("RB2601.SHF"), ("Futures", "RB2601.SHF"))
        self.assertEqual(adapter.get_universe("20260703"), ["rb2601"])
        self.assertEqual(adapter.get_sim_account()["account"], "cn_futures_sim")
        self.assertEqual(adapter.get_strategy_config()["capital_layer"], "simulated")

    def test_intraday_bars_are_requested_for_the_exact_trade_date(self) -> None:
        from CNFutures.sim_runner import _read_intraday_bars

        reader = DateScopedFuturesReader()

        bars = _read_intraday_bars(reader, "rb2601", "20260703")

        self.assertEqual(reader.calls[0], ("20260703", "20260703"))
        self.assertEqual(len(bars), 2)

    def test_intraday_universe_requires_current_contract_bars(self) -> None:
        from CNFutures.adapter import CNFuturesAdapter

        reader = DateScopedUniverseReader()
        adapter = CNFuturesAdapter(reader=reader, universe_filter={"max_symbols": 4, "products": ("cu", "rb")})

        self.assertEqual(adapter.get_intraday_universe("20260707"), ["CU2607.SHF", "RB2607.SHF"])
        self.assertNotIn(("CU0001.SHF", "", "20260707"), reader.calls)
        self.assertTrue(all(call[1:] == ("20260707", "20260707") for call in reader.calls))

    def test_intraday_universe_read_model_uses_latest_bar_batch(self) -> None:
        from CNFutures.adapter import CNFuturesAdapter

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "marketdata.sqlite"
            conn = sqlite3.connect(db_path)
            conn.execute(
                """
                CREATE TABLE market_bars_intraday (
                    market TEXT,
                    symbol TEXT,
                    bar_time TEXT,
                    trade_date TEXT,
                    interval TEXT,
                    close REAL
                )
                """
            )
            conn.executemany(
                "INSERT INTO market_bars_intraday VALUES (?, ?, ?, ?, ?, ?)",
                [
                    ("Futures", "CU2607.SHF", "2026-07-07 14:30:00", "20260707", "5min", 71000.0),
                    ("Futures", "CU.SHF", "2026-07-07 14:35:00", "20260707", "5min", 71050.0),
                    ("Futures", "RB2607.SHF", "2026-07-07 14:35:00", "20260707", "5min", 3500.0),
                    ("Futures", "RB2608.SHF", "2026-07-07 14:35:00", "20260707", "5min", 3520.0),
                ],
            )
            conn.commit()
            conn.close()
            adapter = CNFuturesAdapter(
                reader=ReadModelIntradayReader(db_path),
                universe_filter={"max_symbols": 4, "products": ("cu", "rb")},
            )

            self.assertEqual(adapter.get_intraday_universe("20260707"), ["RB2607.SHF", "RB2608.SHF"])

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
                now=datetime.fromisoformat("2026-07-03 14:56:00"),
            )

            self.assertEqual(result["state"], "ok")
            self.assertEqual(result["cadence"], "5min")
            self.assertEqual(result["capital_layer"], "simulated")
            self.assertEqual(result["market"], "cn_futures")
            self.assertEqual(result["style_count"], 2)
            self.assertEqual(result["filled_count"], 2)
            self.assertEqual(result["real_trading_enabled"], False)
            self.assertEqual({row["style"] for row in result["records"]}, {"trend", "breakout"})
            self.assertTrue(all(row["cadence"] == "5min" for row in result["records"]))
            self.assertTrue(all(row["bar_time"] == "2026-07-03 14:55:00" for row in result["records"]))
            self.assertTrue(all(row["forward_outcome"]["status"] == "pending_future_bars" for row in result["records"]))
            self.assertTrue(all(row["exit_plan"]["prediction_horizon_bars"] >= 1 for row in result["records"]))
            self.assertTrue(all(row["scenario_tags"]["session"] == "day" for row in result["records"]))
            self.assertTrue(all(row["order"]["order_id"].endswith("-202607031455") for row in result["records"]))
            self.assertTrue(all(row["order"]["scenario_tags"]["session"] == "day" for row in result["records"]))
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
            self.assertEqual(
                review_rows[0]["score_summary"]["style_scores"]["trend"]["status"],
                "sample_insufficient",
            )

    def test_multi_style_runner_reports_market_closed_after_session(self) -> None:
        from CNFutures.adapter import CNFuturesAdapter
        from CNFutures.sim_runner import run_multi_style_simulation

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            adapter = CNFuturesAdapter(
                reader=FakeFuturesReader(),
                universe_filter={"max_symbols": 1},
                styles={"trend": {"name": "trend", "signal_threshold": 0.01}},
            )

            result = run_multi_style_simulation(
                adapter,
                "20260703",
                FakeFuturesReader(),
                signals_dir=tmp_path / "signals",
                review_path=tmp_path / "cn_futures_reviews.jsonl",
                now=datetime.fromisoformat("2026-07-03 15:20:00"),
                max_intraday_bar_age_minutes=10,
            )

            self.assertEqual(result["state"], "market_closed")
            self.assertEqual(result["filled_count"], 0)
            self.assertEqual(result["errors"], [])

    def test_multi_style_runner_reports_market_closed_during_lunch_break(self) -> None:
        from CNFutures.adapter import CNFuturesAdapter
        from CNFutures.sim_runner import run_multi_style_simulation

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            adapter = CNFuturesAdapter(
                reader=FakeFuturesReader(),
                universe_filter={"max_symbols": 1},
                styles={"trend": {"name": "trend", "signal_threshold": 0.01}},
            )

            result = run_multi_style_simulation(
                adapter,
                "20260703",
                FakeFuturesReader(),
                signals_dir=tmp_path / "signals",
                review_path=tmp_path / "cn_futures_reviews.jsonl",
                now=datetime.fromisoformat("2026-07-03 12:00:00"),
                max_intraday_bar_age_minutes=10,
            )

            self.assertEqual(result["state"], "market_closed")
            self.assertEqual(result["filled_count"], 0)
            self.assertEqual(result["errors"], [])

    def test_multi_style_runner_rejects_stale_intraday_bars_inside_session(self) -> None:
        from CNFutures.adapter import CNFuturesAdapter
        from CNFutures.sim_runner import run_multi_style_simulation

        class StaleInSessionReader(FakeFuturesReader):
            def get_bars_intraday(
                self,
                market: str,
                symbol: str,
                interval: str = "5min",
                start: object = None,
                end: object = None,
            ) -> list[dict[str, object]]:
                if market != "Futures" or interval != "5min":
                    return []
                return [
                    {"trade_date": "20260703", "bar_time": "2026-07-03 14:30:00", "close": 3400, "volume": 1000},
                    {"trade_date": "20260703", "bar_time": "2026-07-03 14:35:00", "close": 3450, "volume": 1000},
                    {"trade_date": "20260703", "bar_time": "2026-07-03 14:40:00", "close": 3500, "volume": 1000},
                ]

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reader = StaleInSessionReader()
            adapter = CNFuturesAdapter(
                reader=reader,
                universe_filter={"max_symbols": 1},
                styles={"trend": {"name": "trend", "signal_threshold": 0.01}},
            )

            result = run_multi_style_simulation(
                adapter,
                "20260703",
                reader,
                signals_dir=tmp_path / "signals",
                review_path=tmp_path / "cn_futures_reviews.jsonl",
                now=datetime.fromisoformat("2026-07-03 14:56:00"),
                max_intraday_bar_age_minutes=10,
            )

            self.assertEqual(result["state"], "degraded")
            self.assertEqual(result["filled_count"], 0)
            self.assertEqual(result["errors"][0]["error"], "stale_intraday_bar")

    def test_multi_style_runner_records_hold_reason_summary(self) -> None:
        from CNFutures.adapter import CNFuturesAdapter
        from CNFutures.sim_runner import run_multi_style_simulation

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            adapter = CNFuturesAdapter(
                reader=FakeFuturesReader(),
                universe_filter={"max_symbols": 1},
                styles={"trend": {"name": "trend", "signal_threshold": 0.50}},
            )

            result = run_multi_style_simulation(
                adapter,
                "20260703",
                FakeFuturesReader(),
                signals_dir=tmp_path / "signals",
                review_path=tmp_path / "cn_futures_reviews.jsonl",
                now=datetime.fromisoformat("2026-07-03 14:56:00"),
            )

            self.assertEqual(result["filled_count"], 0)
            self.assertEqual(result["hold_count"], 1)
            self.assertEqual(result["hold_reason_summary"]["by_reason"]["below_threshold"], 1)
            review_rows = [
                json.loads(line)
                for line in (tmp_path / "cn_futures_reviews.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(review_rows[0]["hold_reason_summary"]["by_reason"]["below_threshold"], 1)

    def test_multi_style_runner_skips_paused_evolved_styles(self) -> None:
        from CNFutures.adapter import CNFuturesAdapter
        from CNFutures.sim_runner import run_multi_style_simulation

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            adapter = CNFuturesAdapter(
                reader=FakeFuturesReader(),
                universe_filter={"max_symbols": 1},
                styles={
                    "trend": {"name": "trend", "signal_threshold": 0.01, "risk_per_trade": 0.03, "status": "active"},
                    "blocked": {"name": "blocked", "signal_threshold": 0.01, "risk_per_trade": 0.03, "status": "paused", "enabled": False},
                },
            )

            result = run_multi_style_simulation(
                adapter,
                "20260703",
                FakeFuturesReader(),
                signals_dir=tmp_path / "signals",
                review_path=tmp_path / "cn_futures_reviews.jsonl",
                now=datetime.fromisoformat("2026-07-03 14:56:00"),
            )

            self.assertEqual(result["state"], "ok")
            self.assertEqual(result["style_count"], 2)
            self.assertEqual(result["filled_count"], 1)
            self.assertEqual({row["style"] for row in result["records"]}, {"trend"})
            self.assertEqual(result["errors"], [])
            self.assertEqual(result["hold_reason_summary"]["by_reason"]["style_paused"], 1)

    def test_index_intraday_directional_style_only_trades_index_products(self) -> None:
        from CNFutures.adapter import CNFuturesAdapter
        from CNFutures.sim_runner import run_multi_style_simulation

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reader = FakeMixedFuturesReader()
            adapter = CNFuturesAdapter(
                reader=reader,
                universe_filter={"max_symbols": 2, "products": ("if", "rb")},
                styles={
                    "trend": {
                        "name": "trend",
                        "signal_threshold": 0.001,
                        "risk_per_trade": 0.03,
                        "products": ["rb"],
                    },
                    "index_intraday_directional": {
                        "name": "index_intraday_directional",
                        "style_family": "index_intraday_directional",
                        "signal_threshold": 0.001,
                        "risk_per_trade": 0.01,
                        "max_margin_usage": 0.80,
                        "products": ["if", "ih", "ic", "im"],
                        "momentum_lookback_bars": 3,
                        "moving_average_bars": 4,
                        "no_overnight": True,
                        "flatten_before_session_close_minutes": 10,
                    },
                },
            )

            result = run_multi_style_simulation(
                adapter,
                "20260706",
                reader,
                signals_dir=tmp_path / "signals",
                review_path=tmp_path / "cn_futures_reviews.jsonl",
                now=datetime.fromisoformat("2026-07-06 14:31:00"),
            )

            pairs = {(row["style"], row["symbol"]) for row in result["records"]}
            self.assertIn(("index_intraday_directional", "IF2601.CFFEX"), pairs)
            self.assertIn(("trend", "rb2601"), pairs)
            self.assertNotIn(("index_intraday_directional", "rb2601"), pairs)
            self.assertNotIn(("trend", "IF2601.CFFEX"), pairs)

    def test_index_intraday_directional_style_skips_outside_day_session_runtime(self) -> None:
        from CNFutures.adapter import CNFuturesAdapter
        from CNFutures.sim_runner import run_multi_style_simulation

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reader = FakeMixedFuturesReader()
            adapter = CNFuturesAdapter(
                reader=reader,
                universe_filter={"max_symbols": 1, "products": ("if",)},
                styles={
                    "index_intraday_directional": {
                        "name": "index_intraday_directional",
                        "style_family": "index_intraday_directional",
                        "signal_threshold": 0.001,
                        "risk_per_trade": 0.01,
                        "products": ["if", "ih", "ic", "im"],
                        "momentum_lookback_bars": 3,
                        "moving_average_bars": 4,
                        "no_overnight": True,
                    },
                },
            )

            result = run_multi_style_simulation(
                adapter,
                "20260706",
                reader,
                signals_dir=tmp_path / "signals",
                review_path=tmp_path / "cn_futures_reviews.jsonl",
                now=datetime.fromisoformat("2026-07-06 21:01:00"),
            )

            self.assertEqual(result["state"], "ok")
            self.assertEqual(result["filled_count"], 0)
            self.assertEqual(result["records"], [])
            self.assertEqual(result["errors"], [])
            self.assertEqual(result["hold_count"], 1)
            self.assertEqual(result["hold_reason_summary"]["by_reason"]["style_session_not_allowed"], 1)

    def test_multi_style_runner_blocks_repeated_same_side_exposure(self) -> None:
        from CNFutures.adapter import CNFuturesAdapter
        from CNFutures.sim_runner import run_multi_style_simulation

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            adapter = CNFuturesAdapter(
                reader=FakeFuturesReader(),
                universe_filter={"max_symbols": 1},
                styles={"trend": {"name": "trend", "signal_threshold": 0.01}},
            )
            common = {
                "adapter": adapter,
                "date": "20260703",
                "reader": FakeFuturesReader(),
                "signals_dir": tmp_path / "signals",
                "review_path": tmp_path / "cn_futures_reviews.jsonl",
                "now": datetime.fromisoformat("2026-07-03 14:56:00"),
            }

            first = run_multi_style_simulation(**common)
            second = run_multi_style_simulation(**common)

            self.assertEqual(first["filled_count"], 1)
            self.assertEqual(second["filled_count"], 0)
            self.assertEqual(second["errors"][0]["error"], "repeated_same_side_exposure")

    def test_multi_style_runner_estimates_realized_pnl_on_reversal(self) -> None:
        from CNFutures.adapter import CNFuturesAdapter
        from CNFutures.sim_runner import run_multi_style_simulation

        class ReversalReader(FakeFuturesReader):
            bars: list[dict[str, object]] = []

            def get_assets(self, market: str) -> list[dict[str, object]]:
                return [{"symbol": "rb2601", "name": "螺纹钢2601", "exchange": "SHFE", "status": "listed"}] if market == "Futures" else []

            def get_bars_intraday(
                self,
                market: str,
                symbol: str,
                interval: str = "5min",
                start: object = None,
                end: object = None,
            ) -> list[dict[str, object]]:
                return list(self.bars) if market == "Futures" and symbol == "rb2601" and interval == "5min" else []

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reader = ReversalReader()
            adapter = CNFuturesAdapter(
                reader=reader,
                universe_filter={"max_symbols": 1, "products": ("rb",)},
                styles={"trend": {"name": "trend", "signal_threshold": 0.001, "risk_per_trade": 0.03, "slippage_bps": 0.0}},
            )
            common = {
                "adapter": adapter,
                "date": "20260703",
                "reader": reader,
                "signals_dir": tmp_path / "signals",
                "review_path": tmp_path / "cn_futures_reviews.jsonl",
            }
            reader.bars = [
                {"trade_date": "20260703", "bar_time": "2026-07-03 14:10:00", "close": 3400, "volume": 1000},
                {"trade_date": "20260703", "bar_time": "2026-07-03 14:15:00", "close": 3450, "volume": 1000},
                {"trade_date": "20260703", "bar_time": "2026-07-03 14:20:00", "close": 3500, "volume": 1000},
            ]
            first = run_multi_style_simulation(**common, now=datetime.fromisoformat("2026-07-03 14:21:00"))
            reader.bars = [
                {"trade_date": "20260703", "bar_time": "2026-07-03 14:25:00", "close": 3500, "volume": 1000},
                {"trade_date": "20260703", "bar_time": "2026-07-03 14:30:00", "close": 3450, "volume": 1000},
                {"trade_date": "20260703", "bar_time": "2026-07-03 14:35:00", "close": 3400, "volume": 1000},
            ]
            second = run_multi_style_simulation(**common, now=datetime.fromisoformat("2026-07-03 14:36:00"))

            self.assertEqual(first["filled_count"], 1)
            self.assertEqual(second["filled_count"], 1)
            performance = second["records"][0]["performance"]
            self.assertEqual(performance["method"], "same_day_reversal_estimate")
            self.assertIn("realized_pnl", performance)

    def test_multi_style_runner_writes_partial_signal_state_for_low_volume_fill(self) -> None:
        from CNFutures.adapter import CNFuturesAdapter
        from CNFutures.sim_runner import run_multi_style_simulation

        class LowVolumeReader(FakeFuturesReader):
            def get_bars_intraday(
                self,
                market: str,
                symbol: str,
                interval: str = "5min",
                start: object = None,
                end: object = None,
            ) -> list[dict[str, object]]:
                if market != "Futures" or symbol != "rb2601" or interval != "5min":
                    return []
                return [
                    {"trade_date": "20260703", "bar_time": "2026-07-03 14:45:00", "close": 3400, "volume": 10},
                    {"trade_date": "20260703", "bar_time": "2026-07-03 14:50:00", "close": 3450, "volume": 10},
                    {"trade_date": "20260703", "bar_time": "2026-07-03 14:55:00", "close": 3500, "volume": 10},
                ]

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reader = LowVolumeReader()
            adapter = CNFuturesAdapter(
                reader=reader,
                universe_filter={"max_symbols": 1},
                styles={
                    "trend": {
                        "name": "trend",
                        "signal_threshold": 0.001,
                        "risk_per_trade": 0.50,
                        "max_margin_usage": 0.80,
                        "volume_participation": 0.10,
                    },
                },
            )

            result = run_multi_style_simulation(
                adapter,
                "20260703",
                reader,
                signals_dir=tmp_path / "signals",
                review_path=tmp_path / "cn_futures_reviews.jsonl",
                now=datetime.fromisoformat("2026-07-03 14:56:00"),
            )

            self.assertEqual(result["record_count"], 1)
            self.assertEqual(result["records"][0]["receipt"]["status"], "partial")
            self.assertEqual(result["records"][0]["signal_result"]["status"], "partial")
            self.assertEqual(len(list((tmp_path / "signals" / "partial").glob("SIM-CNF-*.json"))), 1)

    def test_multi_style_runner_writes_position_snapshot_and_blocks_margin_cap(self) -> None:
        from CNFutures.adapter import CNFuturesAdapter
        from CNFutures.sim_runner import run_multi_style_simulation

        class TwoContractReader(FakeFuturesReader):
            def get_assets(self, market: str) -> list[dict[str, object]]:
                return [
                    {"symbol": "rb2601", "name": "螺纹钢2601", "exchange": "SHFE", "status": "listed"},
                    {"symbol": "rb2605", "name": "螺纹钢2605", "exchange": "SHFE", "status": "listed"},
                ] if market == "Futures" else []

            def get_bars_intraday(
                self,
                market: str,
                symbol: str,
                interval: str = "5min",
                start: object = None,
                end: object = None,
            ) -> list[dict[str, object]]:
                if market != "Futures" or interval != "5min":
                    return []
                return [
                    {"trade_date": "20260703", "bar_time": "2026-07-03 14:45:00", "close": 3400, "volume": 1000},
                    {"trade_date": "20260703", "bar_time": "2026-07-03 14:50:00", "close": 3450, "volume": 1000},
                    {"trade_date": "20260703", "bar_time": "2026-07-03 14:55:00", "close": 3500, "volume": 1000},
                ]

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reader = TwoContractReader()
            adapter = CNFuturesAdapter(
                reader=reader,
                universe_filter={"max_symbols": 2, "products": ("rb",)},
                styles={
                    "trend": {
                        "name": "trend",
                        "signal_threshold": 0.001,
                        "risk_per_trade": 0.08,
                        "max_margin_usage": 0.10,
                        "slippage_bps": 0.0,
                    },
                },
            )

            result = run_multi_style_simulation(
                adapter,
                "20260703",
                reader,
                signals_dir=tmp_path / "signals",
                review_path=tmp_path / "cn_futures_reviews.jsonl",
                now=datetime.fromisoformat("2026-07-03 14:56:00"),
            )

            self.assertEqual(result["filled_count"], 1)
            self.assertEqual(result["errors"][0]["error"], "margin_cap_exceeded")
            snapshot_path = tmp_path / "signals" / "positions" / "cn_futures_sim_positions.json"
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            self.assertEqual(snapshot["position_count"], 1)
            self.assertEqual(snapshot["positions"][0]["symbol"], "rb2601")
            self.assertGreater(snapshot["positions"][0]["margin_required"], 0)

    def test_index_intraday_directional_forces_flatten_near_close(self) -> None:
        from CNFutures.adapter import CNFuturesAdapter
        from CNFutures.sim_runner import run_multi_style_simulation

        class ClosingReader(FakeMixedFuturesReader):
            def get_assets(self, market: str) -> list[dict[str, object]]:
                return [{"symbol": "IF2601.CFFEX", "name": "沪深300股指2601", "exchange": "CFFEX", "status": "listed"}] if market == "Futures" else []

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reader = ClosingReader()
            adapter = CNFuturesAdapter(
                reader=reader,
                universe_filter={"max_symbols": 1, "products": ("if",)},
                styles={
                    "index_intraday_directional": {
                        "name": "index_intraday_directional",
                        "style_family": "index_intraday_directional",
                        "signal_threshold": 0.001,
                        "risk_per_trade": 0.01,
                        "max_margin_usage": 0.80,
                        "products": ["if"],
                        "momentum_lookback_bars": 3,
                        "moving_average_bars": 4,
                        "no_overnight": True,
                        "flatten_before_session_close_minutes": 10,
                        "slippage_bps": 0.0,
                    },
                },
            )

            first = run_multi_style_simulation(
                adapter,
                "20260706",
                reader,
                signals_dir=tmp_path / "signals",
                review_path=tmp_path / "cn_futures_reviews.jsonl",
                now=datetime.fromisoformat("2026-07-06 14:31:00"),
            )
            second = run_multi_style_simulation(
                adapter,
                "20260706",
                reader,
                signals_dir=tmp_path / "signals",
                review_path=tmp_path / "cn_futures_reviews.jsonl",
                now=datetime.fromisoformat("2026-07-06 14:51:00"),
            )

            self.assertEqual(first["filled_count"], 1)
            self.assertEqual(second["filled_count"], 1)
            self.assertEqual(second["records"][0]["order"]["intent"], "flatten_no_overnight")
            self.assertEqual(second["records"][0]["order"]["side"], "sell")
            snapshot = json.loads((tmp_path / "signals" / "positions" / "cn_futures_sim_positions.json").read_text(encoding="utf-8"))
            self.assertEqual(snapshot["position_count"], 0)

    def test_multi_style_runner_blocks_contracts_inside_rollover_guard(self) -> None:
        from CNFutures.adapter import CNFuturesAdapter
        from CNFutures.sim_runner import run_multi_style_simulation

        class ExpiringReader(FakeFuturesReader):
            def get_assets(self, market: str) -> list[dict[str, object]]:
                return [{"symbol": "rb2607", "name": "螺纹钢2607", "exchange": "SHFE", "status": "listed"}] if market == "Futures" else []

            def get_bars_intraday(
                self,
                market: str,
                symbol: str,
                interval: str = "5min",
                start: object = None,
                end: object = None,
            ) -> list[dict[str, object]]:
                return [
                    {"trade_date": "20260703", "bar_time": "2026-07-03 14:45:00", "close": 3400, "volume": 1000},
                    {"trade_date": "20260703", "bar_time": "2026-07-03 14:50:00", "close": 3450, "volume": 1000},
                    {"trade_date": "20260703", "bar_time": "2026-07-03 14:55:00", "close": 3500, "volume": 1000},
                ] if market == "Futures" and interval == "5min" else []

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reader = ExpiringReader()
            adapter = CNFuturesAdapter(
                reader=reader,
                universe_filter={"max_symbols": 1, "products": ("rb",)},
                styles={
                    "trend": {
                        "name": "trend",
                        "signal_threshold": 0.001,
                        "risk_per_trade": 0.03,
                        "rollover_min_days_to_contract_month_start": 5,
                    },
                },
            )

            result = run_multi_style_simulation(
                adapter,
                "20260703",
                reader,
                signals_dir=tmp_path / "signals",
                review_path=tmp_path / "cn_futures_reviews.jsonl",
                now=datetime.fromisoformat("2026-07-03 14:56:00"),
            )

            self.assertEqual(result["filled_count"], 0)
            self.assertEqual(result["errors"][0]["error"], "contract_rollover_guard")

    def test_multi_style_runner_passes_order_book_fields_to_executor(self) -> None:
        from CNFutures.adapter import CNFuturesAdapter
        from CNFutures.sim_runner import run_multi_style_simulation

        class DepthReader(FakeFuturesReader):
            def get_assets(self, market: str) -> list[dict[str, object]]:
                return [{"symbol": "rb2601", "name": "螺纹钢2601", "exchange": "SHFE", "status": "listed"}] if market == "Futures" else []

            def get_bars_intraday(
                self,
                market: str,
                symbol: str,
                interval: str = "5min",
                start: object = None,
                end: object = None,
            ) -> list[dict[str, object]]:
                return [
                    {"trade_date": "20260703", "bar_time": "2026-07-03 14:45:00", "close": 3400, "volume": 1000},
                    {"trade_date": "20260703", "bar_time": "2026-07-03 14:50:00", "close": 3450, "volume": 1000},
                    {
                        "trade_date": "20260703",
                        "bar_time": "2026-07-03 14:55:00",
                        "close": 3500,
                        "volume": 1000,
                        "ask_price": 3502.0,
                        "ask_size": 1,
                    },
                ] if market == "Futures" and symbol == "rb2601" and interval == "5min" else []

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reader = DepthReader()
            adapter = CNFuturesAdapter(
                reader=reader,
                universe_filter={"max_symbols": 1, "products": ("rb",)},
                styles={"trend": {"name": "trend", "signal_threshold": 0.001, "risk_per_trade": 0.50, "max_margin_usage": 0.80, "slippage_bps": 0.0}},
            )

            result = run_multi_style_simulation(
                adapter,
                "20260703",
                reader,
                signals_dir=tmp_path / "signals",
                review_path=tmp_path / "cn_futures_reviews.jsonl",
                now=datetime.fromisoformat("2026-07-03 14:56:00"),
            )

            self.assertEqual(result["record_count"], 1)
            self.assertEqual(result["records"][0]["receipt"]["status"], "partial")
            self.assertEqual(result["records"][0]["receipt"]["avg_price"], 3502.0)
            self.assertEqual(result["records"][0]["receipt"]["raw_response"]["execution_price_source"], "order_book_ask")

    def test_multi_style_runner_preserves_expiry_metadata_from_intraday_bar(self) -> None:
        from CNFutures.adapter import CNFuturesAdapter
        from CNFutures.sim_runner import run_multi_style_simulation

        class ExpiryReader(FakeFuturesReader):
            def get_assets(self, market: str) -> list[dict[str, object]]:
                return [{"symbol": "rb2601", "name": "螺纹钢2601", "exchange": "SHFE", "status": "listed"}] if market == "Futures" else []

            def get_bars_intraday(
                self,
                market: str,
                symbol: str,
                interval: str = "5min",
                start: object = None,
                end: object = None,
            ) -> list[dict[str, object]]:
                return [
                    {"trade_date": "20260703", "bar_time": "2026-07-03 14:45:00", "close": 3400, "volume": 1000},
                    {"trade_date": "20260703", "bar_time": "2026-07-03 14:50:00", "close": 3450, "volume": 1000},
                    {
                        "trade_date": "20260703",
                        "bar_time": "2026-07-03 14:55:00",
                        "close": 3500,
                        "volume": 1000,
                        "last_trade_date": "20261215",
                        "expiry_date": "20261231",
                    },
                ] if market == "Futures" and symbol == "rb2601" and interval == "5min" else []

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reader = ExpiryReader()
            adapter = CNFuturesAdapter(
                reader=reader,
                universe_filter={"max_symbols": 1, "products": ("rb",)},
                styles={"trend": {"name": "trend", "signal_threshold": 0.001, "risk_per_trade": 0.03}},
            )

            result = run_multi_style_simulation(
                adapter,
                "20260703",
                reader,
                signals_dir=tmp_path / "signals",
                review_path=tmp_path / "cn_futures_reviews.jsonl",
                now=datetime.fromisoformat("2026-07-03 14:56:00"),
            )

            self.assertEqual(result["record_count"], 1)
            order = result["records"][0]["order"]
            raw_response = result["records"][0]["receipt"]["raw_response"]
            self.assertEqual(order["last_trade_date"], "20261215")
            self.assertEqual(order["expiry_date"], "20261231")
            self.assertEqual(raw_response["last_trade_date"], "20261215")
            self.assertEqual(raw_response["expiry_date"], "20261231")

    def test_adapter_falls_back_to_sharedsignals_sqlite_for_futures_assets(self) -> None:
        from CNFutures.adapter import CNFuturesAdapter

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "marketdata.sqlite"
            conn = sqlite3.connect(db_path)
            conn.execute(
                """
                CREATE TABLE market_assets (
                    market TEXT,
                    symbol TEXT,
                    name TEXT,
                    status TEXT
                )
                """
            )
            conn.execute(
                "INSERT INTO market_assets VALUES (?, ?, ?, ?)",
                ("Futures", "RB2601.SHF", "螺纹钢2601", None),
            )
            conn.commit()
            old_db = os.environ.get("SHARED_SIGNALS_DB")
            os.environ["SHARED_SIGNALS_DB"] = str(db_path)
            try:
                adapter = CNFuturesAdapter(reader=object(), universe_filter={"max_symbols": 1})
                self.assertEqual(adapter.get_universe("20260703"), ["RB2601.SHF"])
            finally:
                if old_db is None:
                    os.environ.pop("SHARED_SIGNALS_DB", None)
                else:
                    os.environ["SHARED_SIGNALS_DB"] = old_db

    def test_run_simulation_script_can_be_executed_directly(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "CNFutures" / "run_simulation.py"),
                "--help",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--max-intraday-bar-age-minutes", result.stdout)

    def test_run_simulation_cli_treats_market_closed_as_normal(self) -> None:
        from CNFutures import run_simulation

        class Args:
            date = "20260703"
            signals_dir = Path("/tmp/cn_futures_signals")
            review_path = Path("/tmp/cn_futures_reviews.jsonl")
            max_symbols = None
            cadence = "5min"
            max_intraday_bar_age_minutes = 10.0
            json = True

        with (
            patch("CNFutures.run_simulation._parse_args", return_value=Args()),
            patch("CNFutures.run_simulation.CNFuturesAdapter") as adapter_class,
            patch("CNFutures.run_simulation.run_multi_style_simulation") as run_sim,
        ):
            adapter = adapter_class.return_value
            adapter.reader = object()
            run_sim.return_value = {
                "market": "cn_futures",
                "reader_market": "Futures",
                "date": "20260703",
                "cadence": "5min",
                "state": "market_closed",
                "capital_layer": "simulated",
                "account_type": "simulated",
                "universe_count": 1,
                "style_count": 1,
                "record_count": 0,
                "filled_count": 0,
                "hold_count": 0,
                "errors": [],
                "records": [],
                "max_intraday_bar_age_minutes": 10.0,
            }

            self.assertEqual(run_simulation.main(), 0)

    def test_run_simulation_cli_does_not_hide_market_closed_errors(self) -> None:
        from CNFutures import run_simulation

        class Args:
            date = "20260703"
            signals_dir = Path("/tmp/cn_futures_signals")
            review_path = Path("/tmp/cn_futures_reviews.jsonl")
            max_symbols = None
            cadence = "5min"
            max_intraday_bar_age_minutes = 10.0
            json = True

        with (
            patch("CNFutures.run_simulation._parse_args", return_value=Args()),
            patch("CNFutures.run_simulation.CNFuturesAdapter") as adapter_class,
            patch("CNFutures.run_simulation.run_multi_style_simulation") as run_sim,
        ):
            adapter = adapter_class.return_value
            adapter.reader = object()
            run_sim.return_value = {
                "market": "cn_futures",
                "reader_market": "Futures",
                "date": "20260703",
                "cadence": "5min",
                "state": "market_closed",
                "capital_layer": "simulated",
                "account_type": "simulated",
                "universe_count": 1,
                "style_count": 1,
                "record_count": 0,
                "filled_count": 0,
                "hold_count": 0,
                "error_count": 1,
                "errors": [{"stage": "clock", "error": "bad_session_state"}],
                "records": [],
                "max_intraday_bar_age_minutes": 10.0,
            }

            self.assertEqual(run_simulation.main(), 2)

    def test_adapter_prefers_contracts_with_available_daily_bars(self) -> None:
        from CNFutures.adapter import CNFuturesAdapter

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "marketdata.sqlite"
            conn = sqlite3.connect(db_path)
            conn.execute(
                """
                CREATE TABLE market_assets (
                    market TEXT,
                    symbol TEXT,
                    name TEXT,
                    status TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE market_bars_daily (
                    market TEXT,
                    symbol TEXT,
                    trade_date TEXT,
                    close REAL
                )
                """
            )
            conn.executemany(
                "INSERT INTO market_assets VALUES (?, ?, ?, ?)",
                [
                    ("Futures", "CU0001.SHF", "old copper", None),
                    ("Futures", "CU2609.SHF", "copper current", None),
                    ("Futures", "RB2609.SHF", "rebar current", None),
                ],
            )
            conn.executemany(
                "INSERT INTO market_bars_daily VALUES (?, ?, ?, ?)",
                [
                    ("Futures", "CU2609.SHF", "20260703", 71000.0),
                    ("Futures", "RB2609.SHF", "20260703", 3500.0),
                ],
            )
            conn.commit()
            old_db = os.environ.get("SHARED_SIGNALS_DB")
            os.environ["SHARED_SIGNALS_DB"] = str(db_path)
            try:
                with patch("CNFutures.adapter.TradingagentDataReader", None):
                    adapter = CNFuturesAdapter(reader=None, universe_filter={"max_symbols": 2})

                self.assertEqual(adapter.get_universe("20260703"), ["CU2609.SHF", "RB2609.SHF"])
                self.assertEqual(adapter.get_universe("20260704"), ["CU2609.SHF", "RB2609.SHF"])
            finally:
                if old_db is None:
                    os.environ.pop("SHARED_SIGNALS_DB", None)
                else:
                    os.environ["SHARED_SIGNALS_DB"] = old_db

    def test_adapter_prefers_contracts_with_available_intraday_bars(self) -> None:
        from CNFutures.adapter import CNFuturesAdapter

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "marketdata.sqlite"
            conn = sqlite3.connect(db_path)
            conn.execute(
                """
                CREATE TABLE market_assets (
                    market TEXT,
                    symbol TEXT,
                    name TEXT,
                    status TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE market_bars_intraday (
                    market TEXT,
                    symbol TEXT,
                    bar_time TEXT,
                    trade_date TEXT,
                    interval TEXT,
                    close REAL
                )
                """
            )
            conn.executemany(
                "INSERT INTO market_assets VALUES (?, ?, ?, ?)",
                [
                    ("Futures", "CU2609.SHF", "copper current", None),
                    ("Futures", "RB2609.SHF", "rebar current", None),
                ],
            )
            conn.executemany(
                "INSERT INTO market_bars_intraday VALUES (?, ?, ?, ?, ?, ?)",
                [
                    ("Futures", "CU2609.SHF", "2026-07-03 14:55:00", "20260703", "5min", 71000.0),
                    ("Futures", "RB2609.SHF", "2026-07-03 14:55:00", "20260703", "5min", 3500.0),
                ],
            )
            conn.commit()
            old_db = os.environ.get("SHARED_SIGNALS_DB")
            os.environ["SHARED_SIGNALS_DB"] = str(db_path)
            try:
                with patch("CNFutures.adapter.TradingagentDataReader", None):
                    adapter = CNFuturesAdapter(reader=None, universe_filter={"max_symbols": 2})

                self.assertEqual(adapter.get_intraday_universe("20260703"), ["CU2609.SHF", "RB2609.SHF"])
                rows = adapter.get_bars_intraday("Futures", "RB2609.SHF", "5min", end="20260703")
                self.assertEqual(rows[-1]["bar_time"], "2026-07-03 14:55:00")
            finally:
                if old_db is None:
                    os.environ.pop("SHARED_SIGNALS_DB", None)
                else:
                    os.environ["SHARED_SIGNALS_DB"] = old_db

    def test_review_scoring_marks_small_open_only_samples_insufficient(self) -> None:
        from CNFutures.review import score_records

        records = [
            {
                "style": "trend",
                "receipt": {
                    "status": "filled",
                    "fee": 10.0,
                    "raw_response": {"margin_required": 5000.0, "notional": 50000.0},
                },
            }
        ]

        scores = score_records(records, min_sample_trades=5)
        trend = scores["style_scores"]["trend"]

        self.assertEqual(trend["trade_count"], 1)
        self.assertEqual(trend["filled_count"], 1)
        self.assertEqual(trend["status"], "sample_insufficient")
        self.assertEqual(trend["score"], 0.0)
        self.assertIn("pnl_samples=0", trend["sample_warning"])

    def test_review_scoring_uses_realized_pnl_when_sample_is_sufficient(self) -> None:
        from CNFutures.review import score_records

        records = []
        for index, pnl in enumerate([10, -2, 8, 4], start=1):
            records.append(
                {
                    "style": "trend",
                    "order": {"order_id": f"SIM-CNF-{index}"},
                    "receipt": {
                        "status": "filled",
                        "fee": 1.0,
                        "raw_response": {"margin_required": 1000.0, "notional": 10000.0},
                    },
                    "performance": {"realized_pnl": pnl},
                }
            )

        scores = score_records(records, min_sample_trades=4)
        trend = scores["style_scores"]["trend"]

        self.assertEqual(trend["status"], "eligible_for_candidate_pool")
        self.assertEqual(trend["pnl_sample_count"], 4)
        self.assertEqual(trend["wins"], 3)
        self.assertEqual(trend["losses"], 1)
        self.assertGreater(trend["score"], 0)

    def test_cn_futures_live_gateway_rejects_real_orders_fail_closed(self) -> None:
        from CNFutures.live_gateway import get_live_gateway_status, submit_real_order
        from shared.markets.safety import SafetyViolation

        status = get_live_gateway_status()

        self.assertFalse(status["real_trading_enabled"])
        self.assertFalse(status["broker_adapter_ready"])
        with self.assertRaisesRegex(SafetyViolation, "fail-closed"):
            submit_real_order({"symbol": "RB2609.SHF", "side": "buy", "quantity": 1}, approval_token="token")


if __name__ == "__main__":
    unittest.main()
