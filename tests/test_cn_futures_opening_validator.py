from __future__ import annotations

import sqlite3
import tempfile
import unittest
import json
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from CNFutures.opening_validator import (
    _opening_30m_review,
    _query_daily_bars_via_reader,
    _query_session_bars_via_api,
    _reader_symbols,
    first_sample_alerts,
    validate_opening,
    validate_pre_open,
)


class CNFuturesOpeningValidatorTest(unittest.TestCase):
    def test_sparse_night_bars_with_product_coverage_hold_are_strategy_hold(self) -> None:
        report = _opening_30m_review(
            bars={"bar_count": 2, "symbol_count": 2},
            latest_review={
                "filled_count": 0,
                "hold_count": 1,
                "hold_reason_summary": {
                    "total": 1,
                    "by_reason": {"insufficient_distinct_product_coverage": 1},
                },
            },
            filled_signal_count=0,
            receipt_count=0,
            elapsed_minutes=60,
            min_symbols=4,
        )

        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["phase"], "strategy_hold")

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

        with patch("CNFutures.opening_validator._query_session_bars_via_api", return_value={"bar_count": 0, "symbol_count": 0, "query_source": "SharedSignals API"}):
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

        with patch("CNFutures.opening_validator._query_session_bars_via_api", return_value={"bar_count": 4, "symbol_count": 4, "query_source": "SharedSignals API"}):
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

        with patch("CNFutures.opening_validator._query_session_bars_via_api", return_value={"bar_count": 4, "symbol_count": 4, "query_source": "SharedSignals API"}):
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

        with patch("CNFutures.opening_validator._query_session_bars_via_api", return_value={"bar_count": 4, "symbol_count": 4, "query_source": "SharedSignals API"}):
            report = first_sample_alerts(
                sqlite_db=db_path,
                now=datetime.fromisoformat("2026-07-06T09:35:00+08:00"),
                min_symbols=4,
                review_path=review,
            )

        self.assertEqual(report["opening_30m_review"]["status"], "pass")
        self.assertEqual(report["opening_30m_review"]["phase"], "strategy_hold")
        self.assertEqual(report["opening_30m_review"]["top_hold_reason"], "below_threshold")
        codes = {alert["code"] for alert in report["alerts"]}
        self.assertNotIn("cn_futures_opening_30m_strategy_hold", codes)
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

        with patch("CNFutures.opening_validator._query_session_bars_via_api", return_value={"bar_count": 4, "symbol_count": 4, "query_source": "SharedSignals API"}):
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

    def test_first_sample_treats_night_session_hold_as_ready_even_with_sparse_bars(self) -> None:
        db_path = self._db([("CU2609.SHF", "2026-07-06 23:30:00")])
        review = Path(tempfile.NamedTemporaryFile(delete=False).name)
        self.addCleanup(lambda: review.unlink(missing_ok=True))
        review.write_text(
            json.dumps(
                {
                    "state": "ok",
                    "cadence": "5min",
                    "filled_count": 0,
                    "hold_count": 4,
                    "hold_reason_summary": {"total": 4, "by_reason": {"night_session_not_allowed": 4}},
                }
            )
            + "\n",
            encoding="utf-8",
        )

        with patch("CNFutures.opening_validator._query_session_bars_via_api", return_value={"bar_count": 1, "symbol_count": 1, "query_source": "SharedSignals API"}):
            report = first_sample_alerts(
                sqlite_db=db_path,
                now=datetime.fromisoformat("2026-07-06T23:40:00+08:00"),
                min_symbols=4,
                review_path=review,
            )

        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["reason"], "first_sample_ready")
        self.assertEqual(report["opening_30m_review"]["phase"], "no_night_session")
        codes = {alert["code"] for alert in report["alerts"]}
        self.assertNotIn("futures_5min_missing_in_session", codes)
        self.assertNotIn("cn_futures_first_sim_sample_missing", codes)

    def test_first_sample_keeps_sparse_bar_warning_when_night_hold_is_not_session_guard(self) -> None:
        db_path = self._db([("CU2609.SHF", "2026-07-06 23:30:00")])
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

        with patch("CNFutures.opening_validator._query_session_bars_via_api", return_value={"bar_count": 1, "symbol_count": 1, "query_source": "SharedSignals API"}):
            report = first_sample_alerts(
                sqlite_db=db_path,
                now=datetime.fromisoformat("2026-07-06T23:40:00+08:00"),
                min_symbols=4,
                review_path=review,
            )

        self.assertEqual(report["status"], "warn")
        self.assertEqual(report["opening_30m_review"]["phase"], "insufficient_5min_data")
        codes = {alert["code"] for alert in report["alerts"]}
        self.assertIn("futures_5min_missing_in_session", codes)
        self.assertIn("cn_futures_first_sim_sample_missing", codes)

    def test_first_sample_treats_sparse_bars_with_paused_styles_as_strategy_hold(self) -> None:
        db_path = self._db([("CU2609.SHF", "2026-07-10 01:00:00")])
        review = Path(tempfile.NamedTemporaryFile(delete=False).name)
        self.addCleanup(lambda: review.unlink(missing_ok=True))
        review.write_text(
            json.dumps(
                {
                    "date": "20260710",
                    "state": "ok",
                    "cadence": "5min",
                    "filled_count": 0,
                    "hold_count": 4,
                    "hold_reason_summary": {"total": 4, "by_reason": {"style_paused": 3, "style_session_not_allowed": 1}},
                }
            )
            + "\n",
            encoding="utf-8",
        )

        with patch("CNFutures.opening_validator._query_session_bars_via_api", return_value={"bar_count": 2, "symbol_count": 2, "query_source": "SharedSignals API"}):
            report = first_sample_alerts(
                sqlite_db=db_path,
                now=datetime.fromisoformat("2026-07-10T01:27:00+08:00"),
                min_symbols=4,
                review_path=review,
            )

        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["opening_30m_review"]["phase"], "strategy_hold")
        codes = {alert["code"] for alert in report["alerts"]}
        self.assertNotIn("futures_5min_missing_in_session", codes)
        self.assertNotIn("cn_futures_first_sim_sample_missing", codes)

    def test_first_sample_warns_when_review_date_does_not_match_trade_date(
        self,
    ) -> None:
        db_path = self._db([("CU2609.SHF", "2026-07-10 01:00:00")])
        review = Path(tempfile.NamedTemporaryFile(delete=False).name)
        self.addCleanup(lambda: review.unlink(missing_ok=True))
        review.write_text(
            json.dumps(
                {
                    "date": "20260708",
                    "state": "ok",
                    "cadence": "5min",
                    "filled_count": 0,
                    "hold_count": 4,
                    "hold_reason_summary": {"total": 4, "by_reason": {"style_paused": 3, "style_session_not_allowed": 1}},
                }
            )
            + "\n",
            encoding="utf-8",
        )

        with patch(
            "CNFutures.opening_validator._query_session_bars_via_api",
            return_value={"bar_count": 2, "symbol_count": 2, "query_source": "SharedSignals API"},
        ):
            report = first_sample_alerts(
                sqlite_db=db_path,
                now=datetime.fromisoformat("2026-07-10T01:27:00+08:00"),
                min_symbols=4,
                review_path=review,
            )

        self.assertEqual(report["status"], "warn")
        self.assertEqual(report["opening_30m_review"]["phase"], "insufficient_5min_data")
        codes = {alert["code"] for alert in report["alerts"]}
        self.assertIn("futures_5min_missing_in_session", codes)
        self.assertIn("cn_futures_first_sim_sample_missing", codes)

    def test_first_sample_warns_when_hold_count_is_zero_despite_strategy_reasons(
        self,
    ) -> None:
        db_path = self._db([("CU2609.SHF", "2026-07-10 01:00:00")])
        review = Path(tempfile.NamedTemporaryFile(delete=False).name)
        self.addCleanup(lambda: review.unlink(missing_ok=True))
        review.write_text(
            json.dumps(
                {
                    "date": "20260710",
                    "state": "ok",
                    "cadence": "5min",
                    "filled_count": 0,
                    "hold_count": 0,
                    "hold_reason_summary": {"total": 0, "by_reason": {}},
                }
            )
            + "\n",
            encoding="utf-8",
        )

        with patch(
            "CNFutures.opening_validator._query_session_bars_via_api",
            return_value={"bar_count": 2, "symbol_count": 2, "query_source": "SharedSignals API"},
        ):
            report = first_sample_alerts(
                sqlite_db=db_path,
                now=datetime.fromisoformat("2026-07-10T01:27:00+08:00"),
                min_symbols=4,
                review_path=review,
            )

        self.assertEqual(report["status"], "warn")
        self.assertEqual(report["opening_30m_review"]["phase"], "insufficient_5min_data")
        codes = {alert["code"] for alert in report["alerts"]}
        self.assertIn("futures_5min_missing_in_session", codes)
        self.assertIn("cn_futures_first_sim_sample_missing", codes)

    def test_first_sample_alerts_uses_sharedsignals_api_before_sqlite(self) -> None:
        db_path = self._db([])

        with patch(
            "CNFutures.opening_validator._query_session_bars_via_api",
            return_value={"bar_count": 4, "symbol_count": 4, "query_source": "SharedSignals API"},
        ):
            report = first_sample_alerts(
                sqlite_db=db_path,
                now=datetime.fromisoformat("2026-07-06T09:35:00+08:00"),
                min_symbols=4,
                review_path=Path("/tmp/nonexistent-cn-futures-review.jsonl"),
            )

        self.assertEqual(report["query_source"], "SharedSignals API")
        self.assertNotIn("futures_5min_check_failed", {alert["code"] for alert in report["alerts"]})


class QuerySessionBarsViaApiTest(unittest.TestCase):
    """Tests for _query_session_bars_via_api — the fix ensures no date param."""

    def _mock_response(self, data: list[dict[str, object]], code: int = 200) -> object:
        class Resp:
            def __init__(self, data: list[dict[str, object]], code: int):
                self._data = data
                self._code = code

            def read(self):
                return json.dumps({"data": self._data}).encode("utf-8")

            def getcode(self):
                return self._code

            def __enter__(self):
                return self

            def __exit__(self, *args: object):
                return None

        return Resp(data, code)

    def _executable_bars(self, symbols: list[str], bar_times: list[str], prices: list[float]) -> list[dict[str, object]]:
        bars: list[dict[str, object]] = []
        for symbol, bar_time, price in zip(symbols, bar_times, prices):
            bars.append({"symbol": symbol, "bar_time": bar_time, "close": price, "market": "Futures"})
        return bars

    def test_night_session_21xx_uses_no_date_param(self) -> None:
        """Night session at 22:35 — API URL must NOT include date param."""
        start = datetime.fromisoformat("2026-07-10T21:00:00+08:00")
        now = datetime.fromisoformat("2026-07-10T22:35:00+08:00")
        bars = self._executable_bars(
            ["IF2609.CFX", "IH2609.CFX", "IC2609.CFX", "IM2609.CFX", "CU2609.SHF", "RB2610.SHF"],
            ["2026-07-10 22:30:00"] * 6,
            [3500.0, 2400.0, 5200.0, 6200.0, 80000.0, 3300.0],
        )
        with patch("urllib.request.urlopen", return_value=self._mock_response(bars)):
            with patch("urllib.request.Request") as mock_req:
                result = _query_session_bars_via_api(start, now, min_symbols=4)
                call_args = mock_req.call_args
                url = call_args[0][0] if call_args else ""
                self.assertNotIn("date=", url, f"URL must not contain date param, got: {url}")
        self.assertIsNone(result.get("error"))
        self.assertEqual(result["symbol_count"], 6)
        self.assertEqual(result["bar_count"], 6)
        self.assertEqual(result["query_source"], "SharedSignals API")

    def test_night_early_session_01xx_uses_no_date_param(self) -> None:
        """Night-early session at 01:25 — API URL must NOT include date param."""
        start = datetime.fromisoformat("2026-07-10T21:00:00+08:00")
        now = datetime.fromisoformat("2026-07-11T01:25:00+08:00")
        bars = self._executable_bars(
            ["CU2609.SHF", "RB2610.SHF", "I2609.DCE", "M2609.DCE"],
            ["2026-07-11 01:00:00"] * 4,
            [80000.0, 3300.0, 700.0, 3500.0],
        )
        with patch("urllib.request.urlopen", return_value=self._mock_response(bars)):
            with patch("urllib.request.Request") as mock_req:
                result = _query_session_bars_via_api(start, now, min_symbols=4)
                call_args = mock_req.call_args
                url = call_args[0][0] if call_args else ""
                self.assertNotIn("date=", url, f"URL must not contain date param, got: {url}")
        self.assertIsNone(result.get("error"))
        self.assertEqual(result["symbol_count"], 4)

    def test_day_session_filters_bars_within_range(self) -> None:
        """Day session bars outside start..now range are excluded."""
        start = datetime.fromisoformat("2026-07-06T09:00:00+08:00")
        now = datetime.fromisoformat("2026-07-06T09:15:00+08:00")
        bars = [
            {"symbol": "IF2609.CFX", "bar_time": "2026-07-06 09:05:00", "close": 3500.0},
            {"symbol": "IF2609.CFX", "bar_time": "2026-07-06 09:10:00", "close": 3510.0},
            {"symbol": "IF2609.CFX", "bar_time": "2026-07-06 08:55:00", "close": 3490.0},  # before start
            {"symbol": "IF2609.CFX", "bar_time": "2026-07-06 09:20:00", "close": 3520.0},  # after now
        ]
        with patch("urllib.request.urlopen", return_value=self._mock_response(bars)):
            result = _query_session_bars_via_api(start, now, min_symbols=2)
        self.assertEqual(result["bar_count"], 2)  # only 09:05 and 09:10
        self.assertEqual(result["first_bar_time"], "2026-07-06 09:05:00")
        self.assertEqual(result["latest_bar_time"], "2026-07-06 09:10:00")

    def test_filters_out_non_executable_contracts(self) -> None:
        """Generic symbols like CU.SHF must be excluded."""
        start = datetime.fromisoformat("2026-07-06T09:00:00+08:00")
        now = datetime.fromisoformat("2026-07-06T09:15:00+08:00")
        bars = [
            {"symbol": "CU.SHF", "bar_time": "2026-07-06 09:05:00", "close": 80000.0},
            {"symbol": "CU2609.SHF", "bar_time": "2026-07-06 09:05:00", "close": 80000.0},
        ]
        with patch("urllib.request.urlopen", return_value=self._mock_response(bars)):
            result = _query_session_bars_via_api(start, now, min_symbols=1)
        self.assertEqual(result["symbol_count"], 1)

    def test_filters_out_zero_or_negative_price(self) -> None:
        """Bars with price <= 0 must be excluded."""
        start = datetime.fromisoformat("2026-07-06T09:00:00+08:00")
        now = datetime.fromisoformat("2026-07-06T09:15:00+08:00")
        bars = [
            {"symbol": "IF2609.CFX", "bar_time": "2026-07-06 09:05:00", "close": 0.0},
            {"symbol": "IH2609.CFX", "bar_time": "2026-07-06 09:05:00", "close": -1.0},
            {"symbol": "IC2609.CFX", "bar_time": "2026-07-06 09:05:00", "close": 5200.0},
        ]
        with patch("urllib.request.urlopen", return_value=self._mock_response(bars)):
            result = _query_session_bars_via_api(start, now, min_symbols=1)
        self.assertEqual(result["symbol_count"], 1)
        self.assertEqual(result["bar_count"], 1)

    def test_api_http_error_returns_error_dict(self) -> None:
        """HTTP error from API must return error with symbol_count=0."""
        start = datetime.fromisoformat("2026-07-06T09:00:00+08:00")
        now = datetime.fromisoformat("2026-07-06T09:15:00+08:00")
        with patch("urllib.request.urlopen", side_effect=OSError("connection refused")):
            result = _query_session_bars_via_api(start, now, min_symbols=4)
        self.assertIsNotNone(result.get("error"))
        self.assertIn("sharedsignals_api_error", result["error"])
        self.assertEqual(result["symbol_count"], 0)
        self.assertEqual(result["bar_count"], 0)

    def test_api_empty_response_fail_closed(self) -> None:
        """Empty API response must fail-closed with bar_count=0."""
        start = datetime.fromisoformat("2026-07-06T09:00:00+08:00")
        now = datetime.fromisoformat("2026-07-06T09:15:00+08:00")
        with patch("urllib.request.urlopen", return_value=self._mock_response([])):
            result = _query_session_bars_via_api(start, now, min_symbols=4)
        self.assertIsNone(result.get("error"))
        self.assertEqual(result["bar_count"], 0)
        self.assertEqual(result["symbol_count"], 0)

    def test_stale_bars_before_session_excluded(self) -> None:
        """Bars with bar_time before session start are excluded."""
        start = datetime.fromisoformat("2026-07-06T21:00:00+08:00")
        now = datetime.fromisoformat("2026-07-06T22:35:00+08:00")
        bars = [
            {"symbol": "IF2609.CFX", "bar_time": "2026-07-06 14:55:00", "close": 3500.0},
            {"symbol": "IF2609.CFX", "bar_time": "2026-07-06 21:05:00", "close": 3510.0},
        ]
        with patch("urllib.request.urlopen", return_value=self._mock_response(bars)):
            result = _query_session_bars_via_api(start, now, min_symbols=1)
        self.assertEqual(result["bar_count"], 1)
        self.assertEqual(result["latest_bar_time"], "2026-07-06 21:05:00")

    def test_future_bars_excluded(self) -> None:
        """Bars with bar_time after 'now' are excluded."""
        start = datetime.fromisoformat("2026-07-06T09:00:00+08:00")
        now = datetime.fromisoformat("2026-07-06T09:15:00+08:00")
        bars = [
            {"symbol": "IF2609.CFX", "bar_time": "2026-07-06 09:20:00", "close": 3500.0},
            {"symbol": "IF2609.CFX", "bar_time": "2026-07-06 09:10:00", "close": 3490.0},
        ]
        with patch("urllib.request.urlopen", return_value=self._mock_response(bars)):
            result = _query_session_bars_via_api(start, now, min_symbols=1)
        self.assertEqual(result["bar_count"], 1)

    def test_min_symbols_not_met_but_bars_still_reported(self) -> None:
        """When symbol_count < min_symbols, bars are still returned (caller decides)."""
        start = datetime.fromisoformat("2026-07-06T09:00:00+08:00")
        now = datetime.fromisoformat("2026-07-06T09:15:00+08:00")
        bars = [
            {"symbol": "IF2609.CFX", "bar_time": "2026-07-06 09:05:00", "close": 3500.0},
        ]
        with patch("urllib.request.urlopen", return_value=self._mock_response(bars)):
            result = _query_session_bars_via_api(start, now, min_symbols=4)
        self.assertEqual(result["symbol_count"], 1)
        self.assertIsNone(result.get("error"))

    def test_json_decode_error_returns_error(self) -> None:
        """Malformed JSON from API must return error."""
        start = datetime.fromisoformat("2026-07-06T09:00:00+08:00")
        now = datetime.fromisoformat("2026-07-06T09:15:00+08:00")

        class BadResp:
            def read(self):
                return b"not json"

            def __enter__(self):
                return self

            def __exit__(self, *args: object):
                return None

        with patch("urllib.request.urlopen", return_value=BadResp()):
            result = _query_session_bars_via_api(start, now, min_symbols=4)
        self.assertIsNotNone(result.get("error"))
        self.assertEqual(result["bar_count"], 0)

    def test_night_session_trade_date_uses_active_trade_date(self) -> None:
        """Night-session signal and receipt lookups use the exchange trade date."""
        now = datetime.fromisoformat("2026-07-10T22:35:00+08:00")
        bars = {
            "bar_count": 4,
            "symbol_count": 4,
            "latest_bar_time": "2026-07-10 22:35:00",
            "query_source": "SharedSignals API",
        }
        with tempfile.TemporaryDirectory() as tmp, patch(
            "CNFutures.opening_validator._query_session_bars_via_api",
            return_value=bars,
        ), patch(
            "CNFutures.opening_validator._read_latest_review",
            return_value={},
        ), patch(
            "CNFutures.opening_validator._count_filled_signals",
            return_value=0,
        ) as count_signals, patch(
            "CNFutures.opening_validator._count_market_receipts",
            return_value=0,
        ) as count_receipts:
            first_sample_alerts(
                sqlite_db=Path(tmp) / "unused.db",
                review_path=Path(tmp) / "review.jsonl",
                signals_dir=Path(tmp) / "signals",
                receipt_path=Path(tmp) / "receipts.jsonl",
                now=now,
                min_symbols=4,
            )

        self.assertEqual(count_signals.call_args.args[1], "20260711")
        self.assertEqual(count_receipts.call_args.args[1], "20260711")


if __name__ == "__main__":
    unittest.main()
