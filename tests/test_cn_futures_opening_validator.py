from __future__ import annotations

import sqlite3
import tempfile
import unittest
import json
from datetime import datetime
from pathlib import Path

from CNFutures.opening_validator import (
    _query_daily_bars_via_reader,
    _reader_symbols,
    first_sample_alerts,
    validate_opening,
    validate_pre_open,
)


class CNFuturesOpeningValidatorTest(unittest.TestCase):
    def _db(self, rows: list[tuple[str, str]]) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        path = root / "marketdata.sqlite"
        conn = sqlite3.connect(path)
        conn.execute(
            """
            CREATE TABLE market_bars_intraday (
                market TEXT,
                symbol TEXT,
                bar_time TEXT,
                interval TEXT,
                provider TEXT
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
            "INSERT INTO market_bars_intraday VALUES (?, ?, ?, ?, ?)",
            [("Futures", symbol, bar_time, "5min", "tushare_rt_fut_min") for symbol, bar_time in rows],
        )
        conn.commit()
        conn.close()
        return path

    def _db_with_daily(self, rows: list[tuple[str, str, float]]) -> Path:
        path = self._db([])
        conn = sqlite3.connect(path)
        conn.executemany(
            "INSERT INTO market_bars_daily VALUES (?, ?, ?, ?)",
            [("Futures", symbol, trade_date, close) for symbol, trade_date, close in rows],
        )
        conn.commit()
        conn.close()
        return path

    def _add_intraday(self, path: Path, rows: list[tuple[str, str]]) -> None:
        conn = sqlite3.connect(path)
        conn.executemany(
            "INSERT INTO market_bars_intraday VALUES (?, ?, ?, ?, ?)",
            [("Futures", symbol, bar_time, "5min", "tushare_rt_fut_min") for symbol, bar_time in rows],
        )
        conn.commit()
        conn.close()

    def test_reader_symbols_skip_expired_and_generic_contracts(self) -> None:
        class Reader:
            def get_assets(self, market: str) -> list[dict[str, object]]:
                self.market = market
                return [
                    {"symbol": "A.DCE", "exchange": "DCE"},
                    {"symbol": "A0001.DCE", "exchange": "DCE", "list_date": "19990118", "expiry_date": "20000118", "last_trade_date": "20000125"},
                    {"symbol": "CU2608.SHF", "exchange": "SHF", "list_date": "20250818", "expiry_date": "20260817", "last_trade_date": "20260819"},
                    {"symbol": "CU2609.SHF", "exchange": "SHF", "list_date": "20250915", "expiry_date": "20260915", "last_trade_date": "20260917"},
                ]

        reader = Reader()
        symbols = _reader_symbols(reader, limit=4, as_of="20260708")

        self.assertEqual(symbols, ["CU2608.SHF", "CU2609.SHF"])
        self.assertEqual(reader.market, "Futures")

    def test_passes_when_day_opening_has_symbol_coverage(self) -> None:
        db_path = self._db(
            [
                ("IF2609.CFX", "2026-07-06 09:05:00"),
                ("IH2609.CFX", "2026-07-06 09:05:00"),
                ("IC2609.CFX", "2026-07-06 09:05:00"),
                ("IM2609.CFX", "2026-07-06 09:05:00"),
            ]
        )

        report = validate_opening(
            sqlite_db=db_path,
            now=datetime.fromisoformat("2026-07-06T09:08:00+08:00"),
            min_symbols=4,
        )

        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["session"], "day")
        self.assertEqual(report["symbol_count"], 4)
        self.assertEqual(report["data_source"], "SharedSignals read_model")
        self.assertTrue(report["read_only"])

    def test_warns_when_session_has_no_bars(self) -> None:
        db_path = self._db([])

        report = validate_opening(
            sqlite_db=db_path,
            now=datetime.fromisoformat("2026-07-06T09:08:00+08:00"),
            min_symbols=4,
        )

        self.assertEqual(report["status"], "warn")
        self.assertEqual(report["reason"], "opening_session_has_no_5min_bars")

    def test_sqlite_read_model_accepts_5m_interval_without_provider_lock(self) -> None:
        db_path = self._db([])
        conn = sqlite3.connect(db_path)
        conn.executemany(
            "INSERT INTO market_bars_intraday VALUES (?, ?, ?, ?, ?)",
            [
                ("Futures", "IF2609.CFX", "2026-07-06 09:05:00", "5m", "sharedsignals_reader"),
                ("Futures", "IH2609.CFX", "2026-07-06 09:05:00", "5m", "sharedsignals_reader"),
                ("Futures", "IC2609.CFX", "2026-07-06 09:05:00", "5m", "sharedsignals_reader"),
                ("Futures", "IM2609.CFX", "2026-07-06 09:05:00", "5m", "sharedsignals_reader"),
            ],
        )
        conn.commit()
        conn.close()

        report = validate_opening(
            sqlite_db=db_path,
            now=datetime.fromisoformat("2026-07-06T09:08:00+08:00"),
            min_symbols=4,
        )

        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["query_source"], "SharedSignals read_model/sqlite")
        self.assertEqual(report["symbol_count"], 4)

    def test_warns_outside_session_without_failing(self) -> None:
        db_path = self._db([])

        report = validate_opening(
            sqlite_db=db_path,
            now=datetime.fromisoformat("2026-07-06T16:00:00+08:00"),
            min_symbols=4,
        )

        self.assertEqual(report["status"], "warn")
        self.assertEqual(report["reason"], "outside_cn_futures_session")

    def test_pre_open_passes_when_daily_bars_are_ready(self) -> None:
        db_path = self._db_with_daily(
            [
                ("IF2609.CFX", "20260706", 3500.0),
                ("IH2609.CFX", "20260706", 2400.0),
                ("IC2609.CFX", "20260706", 5200.0),
                ("IM2609.CFX", "20260706", 6200.0),
            ]
        )
        self._add_intraday(
            db_path,
            [
                ("IF2609.CFX", "2026-07-05 14:55:00"),
                ("IH2609.CFX", "2026-07-05 14:55:00"),
                ("IC2609.CFX", "2026-07-05 14:55:00"),
                ("IM2609.CFX", "2026-07-05 14:55:00"),
            ],
        )

        report = validate_pre_open(
            sqlite_db=db_path,
            now=datetime.fromisoformat("2026-07-06T08:55:00+08:00"),
            min_symbols=4,
        )

        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["reason"], "pre_open_acceptance_passed")
        self.assertEqual(report["session"], "day")
        self.assertEqual(report["executable_symbol_count"], 4)
        self.assertEqual(report["product_coverage"], ["ic", "if", "ih", "im"])
        self.assertTrue(report["intraday_readiness"]["reachable"])
        self.assertFalse(report["real_trading_enabled"])

    def test_pre_open_reader_daily_query_uses_recent_window(self) -> None:
        class Reader:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str, str, str]] = []

            def get_assets(self, market: str) -> list[dict[str, object]]:
                self.market = market
                return [
                    {"symbol": "IF2609.CFX", "list_date": "20250901", "expiry_date": "20260918", "last_trade_date": "20260918"},
                    {"symbol": "IH2609.CFX", "list_date": "20250901", "expiry_date": "20260918", "last_trade_date": "20260918"},
                    {"symbol": "IC2609.CFX", "list_date": "20250901", "expiry_date": "20260918", "last_trade_date": "20260918"},
                    {"symbol": "IM2609.CFX", "list_date": "20250901", "expiry_date": "20260918", "last_trade_date": "20260918"},
                ]

            def get_bars_daily(self, market: str, symbol: str, start: str, end: str) -> list[dict[str, object]]:
                self.calls.append((market, symbol, start, end))
                if start == end:
                    return []
                return [{"trade_date": "20260707", "close": 3500.0}]

        reader = Reader()
        report = _query_daily_bars_via_reader(reader, "20260708", min_symbols=4)

        self.assertEqual(report["query_source"], "TradingagentDataReader")
        self.assertEqual(report["symbol_count"], 4)
        self.assertEqual(reader.market, "Futures")
        self.assertTrue(all(call[2] == "20260608" and call[3] == "20260708" for call in reader.calls))

    def test_pre_open_warns_when_daily_bars_are_missing(self) -> None:
        db_path = self._db_with_daily([])

        report = validate_pre_open(
            sqlite_db=db_path,
            now=datetime.fromisoformat("2026-07-06T08:55:00+08:00"),
            min_symbols=4,
        )

        self.assertEqual(report["status"], "warn")
        self.assertEqual(report["reason"], "pre_open_executable_daily_bars_missing")

    def test_pre_open_warns_when_daily_bars_are_stale(self) -> None:
        db_path = self._db_with_daily(
            [
                ("IF2609.CFX", "20200101", 3500.0),
                ("IH2609.CFX", "20200101", 2400.0),
                ("IC2609.CFX", "20200101", 5200.0),
                ("IM2609.CFX", "20200101", 6200.0),
            ]
        )

        report = validate_pre_open(
            sqlite_db=db_path,
            now=datetime.fromisoformat("2026-07-06T08:55:00+08:00"),
            min_symbols=4,
        )

        self.assertEqual(report["status"], "warn")
        self.assertEqual(report["reason"], "pre_open_executable_daily_bars_missing")
        self.assertEqual(report["symbol_count"], 0)

    def test_pre_open_filters_generic_contracts_before_acceptance(self) -> None:
        db_path = self._db_with_daily(
            [
                ("CU.SHF", "20260706", 80000.0),
                ("RB.SHF", "20260706", 3300.0),
                ("IF2609.CFX", "20260706", 3500.0),
            ]
        )
        self._add_intraday(db_path, [("IF2609.CFX", "2026-07-05 14:55:00")])

        report = validate_pre_open(
            sqlite_db=db_path,
            now=datetime.fromisoformat("2026-07-06T08:55:00+08:00"),
            min_symbols=2,
        )

        self.assertEqual(report["status"], "warn")
        self.assertEqual(report["reason"], "pre_open_executable_daily_bars_missing")
        self.assertEqual(report["raw_symbol_count"], 3)
        self.assertEqual(report["executable_symbol_count"], 1)
        self.assertEqual(report["executable_symbols_sample"], ["IF2609.CFX"])

    def test_first_sample_alerts_when_opening_bars_are_missing(self) -> None:
        db_path = self._db([])

        report = first_sample_alerts(
            sqlite_db=db_path,
            now=datetime.fromisoformat("2026-07-06T09:10:00+08:00"),
            min_symbols=4,
        )

        self.assertEqual(report["status"], "warn")
        codes = {alert["code"] for alert in report["alerts"]}
        self.assertIn("futures_5min_missing_in_session", codes)

    def test_first_sample_alerts_when_bars_exist_but_no_sim_sample(self) -> None:
        db_path = self._db(
            [
                ("IF2609.CFX", "2026-07-06 09:05:00"),
                ("IH2609.CFX", "2026-07-06 09:05:00"),
                ("IC2609.CFX", "2026-07-06 09:05:00"),
                ("IM2609.CFX", "2026-07-06 09:05:00"),
            ]
        )

        report = first_sample_alerts(
            sqlite_db=db_path,
            now=datetime.fromisoformat("2026-07-06T09:10:00+08:00"),
            min_symbols=4,
            review_path=Path("/tmp/nonexistent-cn-futures-review.jsonl"),
        )

        self.assertEqual(report["status"], "warn")
        codes = {alert["code"] for alert in report["alerts"]}
        self.assertIn("cn_futures_first_sim_sample_missing", codes)
        self.assertEqual(report["opening_30m_review"]["status"], "pass")
        self.assertEqual(report["opening_30m_review"]["phase"], "accumulating_opening_30m")

    def test_opening_30m_review_warns_when_no_simulated_trade_after_window(self) -> None:
        db_path = self._db(
            [
                ("IF2609.CFX", "2026-07-06 09:05:00"),
                ("IH2609.CFX", "2026-07-06 09:05:00"),
                ("IC2609.CFX", "2026-07-06 09:10:00"),
                ("IM2609.CFX", "2026-07-06 09:10:00"),
            ]
        )

        report = first_sample_alerts(
            sqlite_db=db_path,
            now=datetime.fromisoformat("2026-07-06T09:35:00+08:00"),
            min_symbols=4,
            review_path=Path("/tmp/nonexistent-cn-futures-review.jsonl"),
        )

        self.assertEqual(report["opening_30m_review"]["status"], "warn")
        self.assertEqual(report["opening_30m_review"]["phase"], "no_simulated_trade")
        codes = {alert["code"] for alert in report["alerts"]}
        self.assertIn("cn_futures_opening_30m_no_simulated_trade", codes)

    def test_opening_30m_review_distinguishes_strategy_hold_from_missing_sample(self) -> None:
        db_path = self._db(
            [
                ("IF2609.CFX", "2026-07-06 09:05:00"),
                ("IH2609.CFX", "2026-07-06 09:05:00"),
                ("IC2609.CFX", "2026-07-06 09:10:00"),
                ("IM2609.CFX", "2026-07-06 09:10:00"),
            ]
        )
        review = Path(tempfile.NamedTemporaryFile(delete=False).name)
        self.addCleanup(lambda: review.unlink(missing_ok=True))
        review.write_text(
            json.dumps(
                {
                    "state": "ok",
                    "cadence": "5min",
                    "filled_count": 0,
                    "hold_count": 4,
                    "hold_reason_summary": {"total": 4, "by_reason": {"below_threshold": 4}},
                }
            )
            + "\n",
            encoding="utf-8",
        )

        report = first_sample_alerts(
            sqlite_db=db_path,
            now=datetime.fromisoformat("2026-07-06T09:35:00+08:00"),
            min_symbols=4,
            review_path=review,
        )

        self.assertEqual(report["opening_30m_review"]["status"], "warn")
        self.assertEqual(report["opening_30m_review"]["phase"], "strategy_hold")
        self.assertEqual(report["opening_30m_review"]["top_hold_reason"], "below_threshold")
        codes = {alert["code"] for alert in report["alerts"]}
        self.assertIn("cn_futures_opening_30m_strategy_hold", codes)
        self.assertNotIn("cn_futures_first_sim_sample_missing", codes)

    def test_opening_30m_review_distinguishes_no_night_session_from_missing_sample(self) -> None:
        db_path = self._db(
            [
                ("IF2609.CFX", "2026-07-06 21:05:00"),
                ("IH2609.CFX", "2026-07-06 21:05:00"),
                ("IC2609.CFX", "2026-07-06 21:10:00"),
                ("IM2609.CFX", "2026-07-06 21:10:00"),
            ]
        )
        review = Path(tempfile.NamedTemporaryFile(delete=False).name)
        self.addCleanup(lambda: review.unlink(missing_ok=True))
        review.write_text(
            json.dumps(
                {
                    "state": "ok",
                    "cadence": "5min",
                    "filled_count": 0,
                    "hold_count": 4,
                    "hold_reason_summary": {"total": 4, "by_reason": {"style_session_not_allowed": 4}},
                }
            )
            + "\n",
            encoding="utf-8",
        )

        report = first_sample_alerts(
            sqlite_db=db_path,
            now=datetime.fromisoformat("2026-07-06T21:35:00+08:00"),
            min_symbols=4,
            review_path=review,
        )

        self.assertEqual(report["opening_30m_review"]["status"], "pass")
        self.assertEqual(report["opening_30m_review"]["phase"], "no_night_session")
        self.assertEqual(report["opening_30m_review"]["top_hold_reason"], "style_session_not_allowed")
        codes = {alert["code"] for alert in report["alerts"]}
        self.assertNotIn("cn_futures_opening_30m_no_night_session", codes)
        self.assertNotIn("cn_futures_first_sim_sample_missing", codes)


if __name__ == "__main__":
    unittest.main()
