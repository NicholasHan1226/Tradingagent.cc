from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from CNFutures.opening_validator import first_sample_alerts, validate_opening, validate_pre_open


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

        report = validate_pre_open(
            sqlite_db=db_path,
            now=datetime.fromisoformat("2026-07-06T08:55:00+08:00"),
            min_symbols=4,
        )

        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["reason"], "pre_open_acceptance_passed")
        self.assertEqual(report["session"], "day")
        self.assertFalse(report["real_trading_enabled"])

    def test_pre_open_warns_when_daily_bars_are_missing(self) -> None:
        db_path = self._db_with_daily([])

        report = validate_pre_open(
            sqlite_db=db_path,
            now=datetime.fromisoformat("2026-07-06T08:55:00+08:00"),
            min_symbols=4,
        )

        self.assertEqual(report["status"], "warn")
        self.assertEqual(report["reason"], "pre_open_daily_bars_missing")

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


if __name__ == "__main__":
    unittest.main()
