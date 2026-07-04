from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from CNFutures.opening_validator import validate_opening


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
        conn.executemany(
            "INSERT INTO market_bars_intraday VALUES (?, ?, ?, ?, ?)",
            [("Futures", symbol, bar_time, "5min", "tushare_rt_fut_min") for symbol, bar_time in rows],
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


if __name__ == "__main__":
    unittest.main()
