#!/usr/bin/env python3
"""Tests for SharedSignals data reader integration."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.data.reader import (  # noqa: E402
    MarketGraphCSVReader,
    SharedSignalsReader,
    TradingagentDataReader,
)
from shared.screening import six_dimension_scorer  # noqa: E402


class TestSharedSignalsReader(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "marketdata.sqlite"
        self._build_db(self.db_path)
        self.reader = SharedSignalsReader(self.db_path)

    def tearDown(self) -> None:
        self.reader.close()
        self.tmp.cleanup()

    def _build_db(self, path: Path) -> None:
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE market_assets (
                market TEXT NOT NULL,
                symbol TEXT NOT NULL,
                name TEXT,
                asset_type TEXT,
                exchange TEXT,
                sector TEXT,
                list_date TEXT,
                status TEXT,
                provider TEXT,
                source_file TEXT,
                updated_at TEXT,
                raw_json TEXT,
                PRIMARY KEY (market, symbol)
            );
            CREATE TABLE market_bars_daily (
                market TEXT NOT NULL,
                symbol TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                volume REAL,
                amount REAL,
                provider TEXT,
                source_file TEXT,
                collected_at TEXT,
                raw_json TEXT,
                PRIMARY KEY (market, symbol, trade_date, provider)
            );
            CREATE TABLE market_bars_intraday (
                market TEXT NOT NULL,
                symbol TEXT NOT NULL,
                bar_time TEXT NOT NULL,
                trade_date TEXT,
                interval TEXT,
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                volume REAL,
                amount REAL,
                provider TEXT,
                source_file TEXT,
                collected_at TEXT,
                raw_json TEXT,
                PRIMARY KEY (market, symbol, bar_time, interval, provider)
            );
            CREATE TABLE market_events (
                event_hash TEXT PRIMARY KEY,
                provider TEXT,
                event_type TEXT,
                event_time TEXT,
                trade_date TEXT,
                market TEXT,
                symbol TEXT,
                title TEXT,
                content TEXT,
                url TEXT,
                source TEXT,
                source_file TEXT,
                collected_at TEXT,
                raw_json TEXT
            );
            CREATE TABLE market_factors (
                factor_hash TEXT PRIMARY KEY,
                market TEXT,
                symbol TEXT,
                factor_name TEXT,
                event_time TEXT,
                value REAL,
                provider TEXT,
                source_file TEXT,
                collected_at TEXT,
                raw_json TEXT
            );
            CREATE TABLE market_coverage_status (
                market TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                symbol TEXT NOT NULL,
                coverage_status TEXT NOT NULL,
                reason TEXT,
                provider TEXT,
                source_file TEXT,
                updated_at TEXT,
                raw_json TEXT,
                PRIMARY KEY (market, trade_date, symbol)
            );
            """
        )
        conn.execute(
            "INSERT INTO market_assets VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "Ashare",
                "600000",
                "Pufa Bank",
                "stock",
                "SSE",
                "bank",
                "19991110",
                "active",
                "test",
                "seed",
                "2026-06-29",
                "{}",
            ),
        )
        conn.execute(
            "INSERT INTO market_bars_daily VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "Ashare",
                "600000",
                "20260629",
                10.0,
                10.5,
                9.8,
                10.2,
                10000,
                102000,
                "test",
                "seed",
                "2026-06-29T16:00:00",
                "{}",
            ),
        )
        conn.execute(
            "INSERT INTO market_bars_intraday VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "Ashare",
                "600000",
                "2026-06-29T09:35:00",
                "2026-06-29",
                "5m",
                10.1,
                10.2,
                10.0,
                10.15,
                1000,
                10150,
                "test",
                "seed",
                "2026-06-29T09:36:00",
                "{}",
            ),
        )
        conn.execute(
            "INSERT INTO market_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "evt-1",
                "test",
                "news",
                "2026-06-29T08:00:00",
                "20260629",
                "Ashare",
                "600000",
                "title",
                "content",
                "https://example.test",
                "source",
                "seed",
                "2026-06-29T08:01:00",
                "{}",
            ),
        )
        conn.execute(
            "INSERT INTO market_factors VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "factor-1",
                "Ashare",
                "600000",
                "value",
                "20260629",
                0.8,
                "test",
                "seed",
                "2026-06-29T16:00:00",
                "{}",
            ),
        )
        conn.execute(
            "INSERT INTO market_coverage_status VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "Ashare",
                "20260629",
                "600000",
                "ok",
                "",
                "test",
                "seed",
                "2026-06-29T16:00:00",
                "{}",
            ),
        )
        conn.commit()
        conn.close()

    def test_sqlite_get_methods(self) -> None:
        self.assertEqual(
            self.reader.get_asset("Ashare", "600000")["name"],
            "Pufa Bank",
        )
        self.assertEqual(
            self.reader.get_bars_daily("Ashare", "600000", "20260628", "20260630")[0]["close"],
            10.2,
        )
        self.assertEqual(
            self.reader.get_bars_intraday("Ashare", "600000", "5m", "2026-06-29", "2026-06-29")[0]["close"],
            10.15,
        )
        self.assertEqual(
            self.reader.get_events("Ashare", "600000", "20260629", "20260629")[0]["event_hash"],
            "evt-1",
        )
        self.assertEqual(
            self.reader.get_factors("Ashare", "600000")[0]["factor_name"],
            "value",
        )
        self.assertEqual(
            self.reader.get_coverage("Ashare", "20260629")[0]["coverage_status"],
            "ok",
        )

    def test_tradings_reader_fail_safe_missing_data(self) -> None:
        missing_reader = TradingagentDataReader(
            shared=SharedSignalsReader(Path(self.tmp.name) / "missing.sqlite"),
            marketgraph=MarketGraphCSVReader(Path(self.tmp.name) / "missing_marketgraph"),
        )

        self.assertEqual(missing_reader.get_bars_daily("Ashare", "600000"), [])
        self.assertIsNone(missing_reader.get_asset("Ashare", "600000"))
        self.assertEqual(missing_reader.get_event_candidates(), [])
        self.assertTrue(missing_reader.stale)
        self.assertGreaterEqual(len(missing_reader.errors), 1)


class FakeAPIClient:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.tushare_calls: list[dict[str, object]] = []
        self.market_data_calls: list[dict[str, object]] = []

    def get_tushare(self, api_name, ts_code=None, start_date=None, end_date=None, **kwargs):
        self.tushare_calls.append({
            "api_name": api_name,
            "ts_code": ts_code,
            "start_date": start_date,
            "end_date": end_date,
            **kwargs,
        })
        if api_name == "stock_basic":
            return [
                {
                    "symbol": "000001.SZ",
                    "name": "平安银行",
                    "market": "Ashare",
                    "exchange": "SZSE",
                    "industry": "银行",
                    "list_date": "19910403",
                }
            ]
        return []

    def get_market_data(self, ts_code, start=None, end=None, freq="daily"):
        self.market_data_calls.append({"ts_code": ts_code, "start": start, "end": end, "freq": freq})
        return [{"symbol": ts_code, "trade_date": end or start, "close": 10.29, "amount": 888789.3933}]


class EmptyShellAPIClient(FakeAPIClient):
    def get_market_data(self, ts_code, start=None, end=None, freq="daily"):
        self.market_data_calls.append({"ts_code": ts_code, "start": start, "end": end, "freq": freq})
        return [{}]


class FakeSharedBars:
    last_error = None

    def get_bars_daily(self, market, symbol, start_date="", end_date=""):
        return [
            {
                "market": market,
                "symbol": symbol,
                "trade_date": end_date or start_date,
                "close": 23350.03,
            }
        ]

    def get_bars_intraday(self, market, symbol, interval="5m", start_time="", end_time=""):
        return []


class TestTradingagentDataReaderAPI(unittest.TestCase):
    def test_get_assets_uses_sharedsignals_stock_basic_for_ashare(self) -> None:
        api = FakeAPIClient()
        reader = TradingagentDataReader(api_client=api)

        rows = reader.get_assets("ashare")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["symbol"], "000001.SZ")
        self.assertEqual(rows[0]["sector"], "银行")
        self.assertEqual(api.tushare_calls[0]["api_name"], "stock_basic")

    def test_get_bars_daily_single_end_date_uses_same_start_date(self) -> None:
        api = FakeAPIClient()
        reader = TradingagentDataReader(api_client=api)

        rows = reader.get_bars_daily("ashare", "000001.SZ", None, "20260703")

        self.assertEqual(rows[0]["close"], 10.29)
        self.assertEqual(api.market_data_calls[0]["start"], "20260703")
        self.assertEqual(api.market_data_calls[0]["end"], "20260703")

    def test_get_market_data_falls_back_when_api_returns_empty_shell(self) -> None:
        api = EmptyShellAPIClient()
        reader = TradingagentDataReader(shared=FakeSharedBars(), api_client=api)

        rows = reader.get_market_data("HSI", market="Global", start="20260701", end="20260703")

        self.assertEqual(rows[0]["close"], 23350.03)
        self.assertEqual(rows[0]["market"], "Global")
        self.assertEqual(rows[0]["symbol"], "HSI")

    def test_hk_suffix_is_preserved_for_read_model_symbol(self) -> None:
        self.assertEqual(
            TradingagentDataReader._market_symbol_from_ts_code("00700.HK", None),
            ("HK", "00700.HK"),
        )


class TestMarketGraphCSVReader(unittest.TestCase):
    def test_csv_reader_methods(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            intake = root / "intake"
            intake.mkdir()
            (root / "all_weather_regime.csv").write_text(
                "generated_at,regime,regime_confidence\n"
                "2026-06-28T16:00:00,recession,0.4\n"
                "2026-06-29T16:00:00,growth,0.8\n",
                encoding="utf-8",
            )
            (intake / "event_candidates.csv").write_text(
                "subject_code,subject_type,status,confidence,proposed_impact_hint\n"
                "600000.SH,stock,promoted,0.6,positive: earnings\n",
                encoding="utf-8",
            )
            (intake / "sentiment_signals.csv").write_text(
                "subject_code,status,confidence,proposed_impact_hint\n"
                "600000.SH,sentiment_signal,0.5,mixed\n",
                encoding="utf-8",
            )

            reader = MarketGraphCSVReader(root)
            self.assertEqual(reader.get_regime()["regime"], "growth")
            self.assertEqual(len(reader.get_event_candidates()), 1)
            self.assertEqual(reader.get_sentiment()[0]["status"], "sentiment_signal")


class FakeScoringReader:
    def get_regime(self):
        return {"regime": "growth", "regime_confidence": "0.5"}

    def get_event_candidates(self):
        return [
            {
                "subject_code": "600000.SH",
                "subject_type": "stock",
                "status": "promoted",
                "confidence": "0.6",
                "proposed_impact_hint": "positive: policy",
            },
            {
                "subject_code": "600000.SH",
                "subject_type": "stock",
                "status": "needs_review",
                "confidence": "0.3",
                "proposed_impact_hint": "negative: dilution",
            },
        ]

    def get_factors(self, market, symbol):
        if symbol != "600000":
            return []
        return [
            {"factor_name": "value", "event_time": "20260629", "value": 0.8},
            {"factor_name": "growth", "event_time": "20260629", "value": 0.6},
            {"factor_name": "quality", "event_time": "20260629", "value": 0.4},
            {"factor_name": "momentum", "event_time": "20260629", "value": 0.2},
            {"factor_name": "net_mf_amount", "event_time": "20260629", "value": 10000.0},
        ]

    def get_bars_daily(self, market, symbol, start, end):
        if symbol != "600000":
            return []
        closes = [100.0] * 15 + [101.0, 102.0, 103.0, 104.0, 105.0]
        return [
            {"trade_date": f"202606{10 + idx:02d}", "close": close}
            for idx, close in enumerate(closes)
        ]

    def get_sentiment(self):
        return [
            {
                "subject_code": "600000.SH",
                "status": "sentiment_signal",
                "confidence": "0.3",
                "proposed_impact_hint": "positive",
            },
            {
                "subject_code": "600000.SH",
                "status": "promoted",
                "confidence": "0.3",
                "proposed_impact_hint": "mixed",
            },
        ]


class EmptyScoringReader:
    def get_regime(self):
        return None

    def get_event_candidates(self):
        return []

    def get_factors(self, market, symbol):
        return []

    def get_bars_daily(self, market, symbol, start, end):
        return []

    def get_sentiment(self):
        return []


class TestSixDimensionScorerWithReader(unittest.TestCase):
    def test_scoring_uses_reader_and_preserves_formula(self) -> None:
        scores = six_dimension_scorer.score_stock(
            "600000.SH",
            "20260629",
            data_reader=FakeScoringReader(),
        )

        self.assertAlmostEqual(scores["macro"], 0.6)
        self.assertAlmostEqual(scores["event"], 2 / 3)
        self.assertAlmostEqual(scores["fundamental"], 0.54)
        self.assertAlmostEqual(scores["capital"], 0.7)
        self.assertAlmostEqual(scores["technical"], 0.85)
        self.assertAlmostEqual(scores["sentiment"], 0.75)
        self.assertAlmostEqual(scores["combined"], 0.6658333333333333)

    def test_scoring_missing_data_degrades_to_neutral(self) -> None:
        scores = six_dimension_scorer.score_stock(
            "600000.SH",
            "20260629",
            data_reader=EmptyScoringReader(),
        )

        for key in (
            "macro",
            "event",
            "fundamental",
            "capital",
            "technical",
            "sentiment",
            "combined",
        ):
            self.assertEqual(scores[key], 0.5)


if __name__ == "__main__":
    unittest.main()
