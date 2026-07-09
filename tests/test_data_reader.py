#!/usr/bin/env python3
"""Tests for SharedSignals data reader integration."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
import json
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.data.reader import (  # noqa: E402
    _default_shared_signals_db,
    MarketGraphCSVReader,
    SharedSignalsReader,
    TradingagentDataReader,
)
from shared.screening import six_dimension_scorer  # noqa: E402


class FakeMarketGraphAPIClient:
    errors: list[str] = []

    def get_regime(self):
        return {"regime": "growth", "regime_confidence": 0.8}


class FakeMarketGraphImpactAPIClient:
    errors: list[str] = []

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def get_contract_table(self, table_id, **kwargs):
        self.calls.append({"table_id": table_id, **kwargs})
        return {
            "id": table_id,
            "exists": True,
            "row_count": 2,
            "rows": [
                {
                    "event_id": "evt-1",
                    "target_type": "stock",
                    "target_id": "600000.SH",
                    "target_name": "Pufa Bank",
                    "polarity": "positive",
                    "strength": "0.7",
                    "confidence": "0.8",
                    "valid_from": "20260709",
                },
                {
                    "event_id": "evt-2",
                    "target_type": "industry",
                    "target_id": "bank",
                    "polarity": "positive",
                    "strength": "0.7",
                    "confidence": "0.8",
                },
            ],
        }


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
            "INSERT INTO market_bars_intraday VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "Ashare",
                "000001.SZ",
                "2026-07-06 10:50:00",
                "20260706",
                "5min",
                10.4,
                10.5,
                10.3,
                10.45,
                2000,
                20900,
                "test_rt_min",
                "rt_min",
                "2026-07-06T10:51:00",
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
            self.reader.get_bars_intraday("Ashare", "000001.SZ", "5m", "20260706", "20260706")[0]["close"],
            10.45,
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

    def test_tradings_get_factors_canonicalizes_ashare_market_and_symbol(self) -> None:
        api = FakeAPIClient()
        api.get_tushare = lambda *args, **kwargs: []  # type: ignore[method-assign]
        api.get_fundamentals = lambda ts_code, **kwargs: [{"metric": "value", "value": 1.2, "symbol": ts_code}]  # type: ignore[attr-defined]
        api.get_capital_flow = lambda *args, **kwargs: []  # type: ignore[attr-defined]
        trading_reader = TradingagentDataReader(
            api_client=api,
            shared=self.reader,
            marketgraph=MarketGraphCSVReader(Path(self.tmp.name) / "missing_marketgraph"),
        )

        rows = trading_reader.get_factors("ashare", "600000.SH")

        self.assertEqual(rows[0]["factor_name"], "value")

    def test_latest_daily_batch_uses_sharedsignals_tushare_read_model(self) -> None:
        api = FakeAPIClient()
        trading_reader = TradingagentDataReader(
            api_client=api,
            shared=self.reader,
            marketgraph=MarketGraphCSVReader(Path(self.tmp.name) / "missing_marketgraph"),
        )

        rows = trading_reader.get_latest_daily_batch("Ashare", limit=3000)

        self.assertEqual(rows[0]["symbol"], "600000.SH")
        self.assertEqual(api.tushare_calls[-1]["api_name"], "daily")
        self.assertEqual(api.tushare_calls[-1]["limit"], 3000)

    def test_default_sqlite_fallback_is_nonexistent_until_explicitly_configured(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(
                _default_shared_signals_db(),
                Path("/nonexistent/tradingagent-sharedsignals-diagnostic.sqlite"),
            )


class FakeAPIClient:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.tushare_calls: list[dict[str, object]] = []
        self.market_data_calls: list[dict[str, object]] = []
        self.realtime_calls: list[dict[str, object]] = []
        self.pm_price_calls: list[dict[str, object]] = []
        self.event_calls: list[dict[str, object]] = []

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
        if api_name == "fut_basic":
            return [
                {
                    "symbol": "RB2609.SHF",
                    "name": "螺纹钢2609",
                    "market": kwargs.get("market") or "Futures",
                    "exchange": "SHFE",
                    "asset_type": "future",
                    "status": "listed",
                }
            ]
        if api_name == "daily":
            return [
                {
                    "symbol": "600000.SH",
                    "trade_date": "20260708",
                    "market": "Ashare",
                    "close": 10.29,
                    "amount": 888789.3933,
                }
            ]
        return []

    def get_market_data(self, ts_code, start=None, end=None, freq="daily"):
        self.market_data_calls.append({"ts_code": ts_code, "start": start, "end": end, "freq": freq})
        return [{"symbol": ts_code, "trade_date": end or start, "close": 10.29, "amount": 888789.3933}]

    def get_realtime_5min(self, ts_code, date=None, market=None):
        self.realtime_calls.append({"ts_code": ts_code, "date": date, "market": market})
        return [
            {
                "market": market,
                "symbol": ts_code,
                "bar_time": "2026-07-03T14:55:00+08:00",
                "trade_date": date,
                "interval": "5min",
                "close": 3520.0,
                "bid_price": 3519.0,
                "ask_price": 3521.0,
                "bid_size": 12,
                "ask_size": 9,
                "last_trade_date": "20260915",
                "expiry_date": "20260930",
            }
        ]

    def get_pm_prices(self, market_id=None, limit=200):
        self.pm_price_calls.append({"market_id": market_id, "limit": limit})
        return [{"market_id": market_id, "price": 0.42}]

    def get_events(self, start=None, end=None, market=None, symbol=None, subject_code=None, event_type=None):
        self.event_calls.append({
            "start": start,
            "end": end,
            "market": market,
            "symbol": symbol,
            "subject_code": subject_code,
            "event_type": event_type,
        })
        return [{"market": market, "symbol": symbol, "event_hash": "evt-api", "title": "api event"}]


class EmptyShellAPIClient(FakeAPIClient):
    def get_market_data(self, ts_code, start=None, end=None, freq="daily"):
        self.market_data_calls.append({"ts_code": ts_code, "start": start, "end": end, "freq": freq})
        return [{}]

    def get_realtime_5min(self, ts_code, date=None, market=None):
        self.realtime_calls.append({"ts_code": ts_code, "date": date, "market": market})
        return [{}]

    def get_events(self, start=None, end=None, market=None, symbol=None, subject_code=None, event_type=None):
        self.event_calls.append({
            "start": start,
            "end": end,
            "market": market,
            "symbol": symbol,
            "subject_code": subject_code,
            "event_type": event_type,
        })
        return [{}]


class EmptyEventsAPIClient(FakeAPIClient):
    def get_events(self, start=None, end=None, market=None, symbol=None, subject_code=None, event_type=None):
        self.event_calls.append({
            "start": start,
            "end": end,
            "market": market,
            "symbol": symbol,
            "subject_code": subject_code,
            "event_type": event_type,
        })
        return []


class BatchRealtimeAPIClient(FakeAPIClient):
    def get_realtime_5min(self, ts_code, date=None, market=None):
        self.realtime_calls.append({"ts_code": ts_code, "date": date, "market": market})
        return [
            {
                "market": "Ashare",
                "symbol": "003015.SZ",
                "bar_time": "2026-07-09T14:55:00+08:00",
                "trade_date": "20260709",
                "interval": "5min",
                "close": 38.12,
            },
            {
                "market": "Ashare",
                "ts_code": "300759.SZ",
                "bar_time": "2026-07-08T14:55:00+08:00",
                "trade_date": "20260708",
                "interval": "5min",
                "close": 59.87,
            },
            {
                "market": "Ashare",
                "ts_code": "300759.SZ",
                "bar_time": "2026-07-09T14:55:00+08:00",
                "trade_date": "20260709",
                "interval": "5min",
                "close": 61.23,
            },
        ]


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
        return [
            {
                "market": market,
                "symbol": symbol,
                "bar_time": "2026-07-03T14:55:00+08:00",
                "trade_date": end_time or start_time,
                "interval": interval,
                "close": 23350.03,
            }
        ]

    def get_events(self, market=None, symbol="", start_date="", end_date=""):
        return [
            {
                "market": market,
                "symbol": symbol,
                "trade_date": end_date or start_date,
                "event_hash": "evt-fallback",
                "title": "fallback event",
                "direction": "positive",
                "confidence": "0.70",
            }
        ]


class TestTradingagentDataReaderAPI(unittest.TestCase):
    def test_get_assets_uses_sharedsignals_stock_basic_for_ashare(self) -> None:
        api = FakeAPIClient()
        reader = TradingagentDataReader(api_client=api)

        rows = reader.get_assets("ashare")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["symbol"], "000001.SZ")
        self.assertEqual(rows[0]["sector"], "银行")
        self.assertEqual(api.tushare_calls[0]["api_name"], "stock_basic")

    def test_get_assets_uses_sharedsignals_fut_basic_for_futures(self) -> None:
        api = FakeAPIClient()
        reader = TradingagentDataReader(api_client=api)

        rows = reader.get_assets("Futures")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["symbol"], "RB2609.SHF")
        self.assertEqual(rows[0]["market"], "Futures")
        self.assertEqual(api.tushare_calls[0]["api_name"], "fut_basic")
        self.assertEqual(api.tushare_calls[0]["market"], "Futures")
        self.assertEqual(reader.errors, [])

    def test_get_realtime_5min_batch_uses_market_level_api(self) -> None:
        api = FakeAPIClient()
        reader = TradingagentDataReader(api_client=api)

        rows = reader.get_realtime_5min_batch("Futures", "20260709")

        self.assertEqual(rows[0]["symbol"], "")
        self.assertEqual(rows[0]["market"], "Futures")
        self.assertEqual(api.realtime_calls[0], {"ts_code": "", "date": "20260709", "market": "Futures"})

    def test_empty_api_result_does_not_trigger_sqlite_diagnostic_read(self) -> None:
        api = EmptyShellAPIClient()
        reader = TradingagentDataReader(api_client=api)

        rows = reader.get_bars_intraday("Futures", "RB2609.SHF", "5min", "", "20260703")

        self.assertEqual(rows, [])
        self.assertFalse(any("SQLite diagnostic read" in error for error in reader.errors))

    def test_get_bars_daily_single_end_date_uses_same_start_date(self) -> None:
        api = FakeAPIClient()
        reader = TradingagentDataReader(api_client=api)

        rows = reader.get_bars_daily("ashare", "000001.SZ", None, "20260703")

        self.assertEqual(rows[0]["close"], 10.29)
        self.assertEqual(api.market_data_calls[0]["start"], "20260703")
        self.assertEqual(api.market_data_calls[0]["end"], "20260703")

    def test_get_bars_daily_fallback_canonicalizes_ashare_market(self) -> None:
        class StrictAshareShared(FakeSharedBars):
            def get_bars_daily(self, market, symbol, start_date="", end_date=""):
                if market == "Ashare" and symbol == "000001.SZ":
                    return super().get_bars_daily(market, symbol, start_date, end_date)
                return []

        api = EmptyShellAPIClient()
        reader = TradingagentDataReader(shared=StrictAshareShared(), api_client=api)

        rows = reader.get_bars_daily("ashare", "000001.SZ", "20260622", "20260706")

        self.assertEqual(rows[0]["close"], 23350.03)
        self.assertEqual(rows[0]["market"], "Ashare")
        self.assertEqual(rows[0]["symbol"], "000001.SZ")

    def test_get_market_data_falls_back_when_api_returns_empty_shell(self) -> None:
        api = EmptyShellAPIClient()
        reader = TradingagentDataReader(shared=FakeSharedBars(), api_client=api)

        rows = reader.get_market_data("HSI", market="Global", start="20260701", end="20260703")

        self.assertEqual(rows[0]["close"], 23350.03)
        self.assertEqual(rows[0]["market"], "Global")
        self.assertEqual(rows[0]["symbol"], "HSI")

    def test_get_bars_intraday_prefers_sharedsignals_api(self) -> None:
        api = FakeAPIClient()
        reader = TradingagentDataReader(api_client=api)

        rows = reader.get_bars_intraday("Futures", "RB2609.SHF", "5min", "", "20260703")

        self.assertEqual(rows[0]["close"], 3520.0)
        self.assertEqual(rows[0]["bid_price"], 3519.0)
        self.assertEqual(rows[0]["ask_size"], 9)
        self.assertEqual(rows[0]["last_trade_date"], "20260915")
        self.assertEqual(rows[0]["expiry_date"], "20260930")
        self.assertEqual(api.realtime_calls[0], {"ts_code": "RB2609.SHF", "date": "20260703", "market": "Futures"})

    def test_get_bars_intraday_filters_batch_realtime_rows_to_requested_symbol_and_date(self) -> None:
        api = BatchRealtimeAPIClient()
        reader = TradingagentDataReader(api_client=api)

        rows = reader.get_bars_intraday("ashare", "300759.SZ", "5m", "20260709", "20260709")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["symbol"], "300759.SZ")
        self.assertEqual(rows[0]["close"], 61.23)
        self.assertEqual(api.realtime_calls[0], {"ts_code": "300759.SZ", "date": "20260709", "market": "ashare"})

    def test_get_bars_intraday_falls_back_when_api_returns_empty_shell(self) -> None:
        api = EmptyShellAPIClient()
        reader = TradingagentDataReader(shared=FakeSharedBars(), api_client=api)

        rows = reader.get_bars_intraday("Global", "HSI", "5min", "20260703", "20260703")

        self.assertEqual(rows[0]["close"], 23350.03)
        self.assertEqual(rows[0]["market"], "Global")
        self.assertEqual(api.realtime_calls[0], {"ts_code": "HSI", "date": "20260703", "market": "Global"})

    def test_get_events_falls_back_when_api_returns_empty_shell(self) -> None:
        api = EmptyShellAPIClient()
        reader = TradingagentDataReader(shared=FakeSharedBars(), api_client=api)

        rows = reader.get_events("Ashare", "600000", "20260708", "20260708")

        self.assertEqual(rows[0]["event_hash"], "evt-fallback")
        self.assertEqual(rows[0]["direction"], "positive")
        self.assertEqual(api.event_calls[0]["market"], "Ashare")
        self.assertEqual(api.event_calls[0]["symbol"], "600000")
        self.assertEqual(api.event_calls[0]["subject_code"], "600000.SH")

    def test_get_events_falls_back_when_api_returns_empty_result(self) -> None:
        api = EmptyEventsAPIClient()
        reader = TradingagentDataReader(shared=FakeSharedBars(), api_client=api)

        rows = reader.get_events("Ashare", "600000", "20260708", "20260708")

        self.assertEqual(rows[0]["event_hash"], "evt-fallback")
        self.assertEqual(api.event_calls[0]["market"], "Ashare")
        self.assertEqual(api.event_calls[0]["symbol"], "600000")

    def test_get_pm_prices_uses_sharedsignals_api(self) -> None:
        api = FakeAPIClient()
        reader = TradingagentDataReader(api_client=api)

        rows = reader.get_pm_prices(market_id="pm-1", limit=5)

        self.assertEqual(rows, [{"market_id": "pm-1", "price": 0.42}])
        self.assertEqual(api.pm_price_calls, [{"market_id": "pm-1", "limit": 5}])

    def test_hk_suffix_is_preserved_for_read_model_symbol(self) -> None:
        self.assertEqual(
            TradingagentDataReader._market_symbol_from_ts_code("00700.HK", None),
            ("HK", "00700.HK"),
        )

    def test_marketgraph_interface_is_read_through_api(self) -> None:
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return json.dumps(
                    {
                        "data": {
                            "market": "Ashare",
                            "contract_status": "ok",
                            "readiness_summary": {"readiness_status": "weak_evidence"},
                            "tables": {
                                "market_knowledge_edges": {
                                    "rows": [{"market": "Ashare", "impact_score": "0.7"}]
                                }
                            },
                            "is_trading_permission": False,
                            "can_affect_real_money": False,
                        }
                    }
                ).encode("utf-8")

        with patch.dict("os.environ", {"MARKETGRAPH_API_URL": "http://marketgraph.test"}), patch(
            "shared.data.reader.urllib.request.urlopen",
            return_value=FakeResponse(),
        ):
            reader = TradingagentDataReader()
            self.assertEqual(reader.get_market_readiness_summary("Ashare")["readiness_status"], "weak_evidence")
            self.assertEqual(reader.get_market_knowledge_edges("Ashare")[0]["impact_score"], "0.7")


class TestMarketGraphCSVReader(unittest.TestCase):
    def test_csv_reader_no_longer_reads_local_csv(self) -> None:
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
            self.assertIsNone(reader.get_regime())
            self.assertEqual(reader.get_event_candidates(), [])
            self.assertEqual(reader.get_sentiment(), [])

    def test_event_candidates_read_formal_marketgraph_impact_relations(self) -> None:
        client = FakeMarketGraphImpactAPIClient()
        reader = MarketGraphCSVReader(
            Path("/nonexistent"),
            api_client=None,
            marketgraph_client=client,
            api_enabled=False,
        )

        rows = reader.get_event_candidates()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["subject_code"], "600000.SH")
        self.assertEqual(rows[0]["subject_type"], "stock")
        self.assertEqual(rows[0]["status"], "verified")
        self.assertEqual(rows[0]["proposed_impact_hint"], "positive")
        self.assertEqual(rows[0]["event_time"], "20260709")
        self.assertEqual(client.calls[0]["table_id"], "association_impact_relations")
        self.assertEqual(client.calls[0]["include_rows"], True)
        self.assertEqual(client.calls[0]["record_usage"], False)


class FakeScoringReader:
    def get_regime(self):
        return {"regime": "growth", "regime_confidence": "0.5"}

    def get_event_candidates(self):
        return [
            {
                "subject_code": "600000.SH",
                "subject_type": "stock",
                "status": "verified",
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
                "status": "verified",
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


class SharedSignalsMacroScoringReader(EmptyScoringReader):
    def get_macro_factors(self, start=None, end=None):
        return [
            {
                "trade_date": "20260629",
                "regime": "growth",
                "regime_confidence": "0.75",
                "source": "sharedsignals:macro",
            }
        ]

    def get_regime(self):
        raise AssertionError("MarketGraph regime should not be required when SharedSignals macro exists")


class SharedSignalsRawMacroScoringReader(EmptyScoringReader):
    def get_macro_factors(self, start=None, end=None):
        return [
            {
                "trade_date": "20260629",
                "factor_name": "repo_daily:amount",
                "value": 470611.2,
                "source": "sharedsignals:macro",
            },
            {
                "trade_date": "20260629",
                "factor_name": "repo_daily:close",
                "value": 1.4,
                "source": "sharedsignals:macro",
            },
        ]


class SharedSignalsPmiMacroScoringReader(EmptyScoringReader):
    def get_macro_factors(self, start=None, end=None):
        return [
            {
                "factor_name": "cn_pmi:ID",
                "event_time": "20260707",
                "value": 544.0,
                "source": "sharedsignals:macro",
            },
            {
                "factor_name": "cn_pmi:PMI010000",
                "event_time": "20260707",
                "value": 50.3,
                "source": "sharedsignals:macro",
            },
            {
                "factor_name": "cn_pmi:PMI020201",
                "event_time": "20260707",
                "value": 41.6,
                "source": "sharedsignals:macro",
            },
        ]


class SharedSignalsEventScoringReader(EmptyScoringReader):
    def get_events(self, market=None, symbol="", start="", end=""):
        return [
            {
                "market": market,
                "symbol": symbol,
                "subject_code": "600000.SH",
                "status": "verified",
                "confidence": "0.6",
                "proposed_impact_hint": "positive",
                "source": "sharedsignals:events",
            }
        ]

    def get_event_candidates(self):
        raise AssertionError("MarketGraph event candidates should not be required when SharedSignals events exist")


class APIOnlyScoringReader(EmptyScoringReader):
    def get_fundamentals(self, ts_code, end_date=None):
        return [
            {"factor_name": "daily_basic:pe_ttm", "event_time": "20260629", "value": 12.0},
            {"factor_name": "daily_basic:pb", "event_time": "20260629", "value": 1.4},
            {"factor_name": "fina_indicator:roe", "event_time": "20260629", "value": 18.0},
            {"factor_name": "fina_indicator:netprofit_yoy", "event_time": "20260629", "value": 22.0},
        ]

    def get_capital_flow(self, ts_code, start=None, end=None):
        return [
            {"factor_name": "moneyflow:buy_lg_amount", "event_time": "20260629", "value": 90000.0},
            {"factor_name": "moneyflow:sell_lg_amount", "event_time": "20260629", "value": 30000.0},
            {"factor_name": "moneyflow:buy_elg_amount", "event_time": "20260629", "value": 50000.0},
            {"factor_name": "moneyflow:sell_elg_amount", "event_time": "20260629", "value": 10000.0},
        ]


class SharedSignalsSentimentScoringReader(EmptyScoringReader):
    def __init__(self) -> None:
        self.sentiment_calls: list[tuple[str | None, str | None]] = []

    def get_sentiment(self, start=None, end=None):
        self.sentiment_calls.append((start, end))
        return [
            {
                "subject_code": "600000.SH",
                "status": "verified",
                "confidence": "0.6",
                "proposed_impact_hint": "positive",
                "source": "sharedsignals:sentiment",
            },
            {
                "subject_code": "600000.SH",
                "status": "sentiment_signal",
                "confidence": "0.6",
                "proposed_impact_hint": "mixed",
                "source": "sharedsignals:sentiment",
            },
        ]


class SharedSignalsMarketNewsSentimentReader(EmptyScoringReader):
    def get_sentiment(self, start=None, end=None):
        return [
            {
                "trade_date": "20260709",
                "content": "市场消息：海外港口传出爆炸，风险偏好承压。",
                "source": "sharedsignals:sentiment",
            },
            {
                "trade_date": "20260709",
                "content": "A股硬科技板块迎来重大利好。",
                "source": "sharedsignals:sentiment",
            },
        ]


class TestSixDimensionScorerWithReader(unittest.TestCase):
    def test_sharedsignals_macro_feeds_macro_dimension_before_marketgraph(self) -> None:
        scores = six_dimension_scorer.score_stock(
            "600000.SH",
            "20260629",
            data_reader=SharedSignalsMacroScoringReader(),
        )

        self.assertNotIn("macro", scores["missing_evidence_dimensions"])
        self.assertGreater(scores["macro"], 0.5)
        self.assertEqual(scores["evidence_sources"]["macro"]["source"], "SharedSignals macro")

    def test_sharedsignals_macro_uses_supported_liquidity_factor_not_raw_amount(self) -> None:
        scores = six_dimension_scorer.score_stock(
            "600000.SH",
            "20260629",
            data_reader=SharedSignalsRawMacroScoringReader(),
        )

        self.assertNotIn("macro", scores["missing_evidence_dimensions"])
        self.assertGreater(scores["macro"], 0.5)
        self.assertLess(scores["macro"], 1.0)

    def test_sharedsignals_macro_uses_raw_pmi_values(self) -> None:
        scores = six_dimension_scorer.score_stock(
            "600000.SH",
            "20260709",
            data_reader=SharedSignalsPmiMacroScoringReader(),
        )

        self.assertNotIn("macro", scores["missing_evidence_dimensions"])
        self.assertGreater(scores["macro"], 0.5)
        self.assertLess(scores["macro"], 0.6)
        self.assertEqual(scores["evidence_sources"]["macro"]["source"], "SharedSignals macro")

    def test_sharedsignals_event_feeds_event_dimension_before_marketgraph(self) -> None:
        scores = six_dimension_scorer.score_stock(
            "600000.SH",
            "20260629",
            data_reader=SharedSignalsEventScoringReader(),
        )

        self.assertNotIn("event", scores["missing_evidence_dimensions"])
        self.assertGreater(scores["event"], 0.5)
        self.assertEqual(scores["evidence_sources"]["event"]["source"], "SharedSignals events")

    def test_sharedsignals_sentiment_feeds_sentiment_dimension(self) -> None:
        reader = SharedSignalsSentimentScoringReader()

        scores = six_dimension_scorer.score_stock(
            "600000.SH",
            "20260629",
            data_reader=reader,
        )

        self.assertNotIn("sentiment", scores["missing_evidence_dimensions"])
        self.assertGreater(scores["sentiment"], 0.5)
        self.assertEqual(scores["evidence_sources"]["sentiment"]["source"], "SharedSignals sentiment")
        self.assertEqual(reader.sentiment_calls[0], ("20260615", "20260629"))

    def test_sharedsignals_market_news_feeds_weak_market_sentiment(self) -> None:
        scores = six_dimension_scorer.score_stock(
            "600000.SH",
            "20260709",
            data_reader=SharedSignalsMarketNewsSentimentReader(),
        )

        self.assertNotIn("sentiment", scores["missing_evidence_dimensions"])
        self.assertEqual(scores["evidence_sources"]["sentiment"]["source"], "SharedSignals sentiment")
        self.assertEqual(scores["evidence_sources"]["sentiment"]["row_count"], 2)
        self.assertGreaterEqual(scores["sentiment"], 0.0)
        self.assertLessEqual(scores["sentiment"], 1.0)

    def test_marketgraph_api_regime_feeds_macro_dimension(self) -> None:
        reader = TradingagentDataReader(
            api_client=None,
            marketgraph=MarketGraphCSVReader(
                Path("/nonexistent"),
                api_client=None,
                marketgraph_client=FakeMarketGraphAPIClient(),
                api_enabled=False,
            ),
        )

        scores = six_dimension_scorer.score_stock(
            "600000.SH",
            "20260629",
            data_reader=reader,
        )

        self.assertNotIn("macro", scores["missing_evidence_dimensions"])
        self.assertGreater(scores["macro"], 0.5)

    def test_marketgraph_impact_relations_feed_event_dimension(self) -> None:
        reader = TradingagentDataReader(
            api_client=None,
            marketgraph=MarketGraphCSVReader(
                Path("/nonexistent"),
                api_client=None,
                marketgraph_client=FakeMarketGraphImpactAPIClient(),
                api_enabled=False,
            ),
        )

        scores = six_dimension_scorer.score_stock(
            "600000.SH",
            "20260709",
            data_reader=reader,
        )

        self.assertNotIn("event", scores["missing_evidence_dimensions"])
        self.assertGreater(scores["event"], 0.5)
        self.assertEqual(scores["evidence_sources"]["event"]["source"], "MarketGraph event candidates")

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
        self.assertGreater(scores["evidence_coverage"], 0.0)
        self.assertEqual(scores["missing_evidence_dimensions"], [])

    def test_scoring_uses_sharedsignals_fundamentals_and_capital_flow_when_factors_empty(self) -> None:
        scores = six_dimension_scorer.score_stock(
            "600000.SH",
            "20260629",
            data_reader=APIOnlyScoringReader(),
        )

        self.assertGreater(scores["fundamental"], 0.5)
        self.assertGreater(scores["capital"], 0.5)
        self.assertIn("macro", scores["missing_evidence_dimensions"])

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
        self.assertEqual(scores["evidence_coverage"], 0.0)
        self.assertEqual(
            set(scores["missing_evidence_dimensions"]),
            {"macro", "event", "fundamental", "capital", "technical", "sentiment"},
        )


if __name__ == "__main__":
    unittest.main()
