"""Offline tests for the dividend/split announcement daily fetcher."""

from __future__ import annotations

import csv
import os
import tempfile
import unittest
from pathlib import Path

from Ashare.event_dividend_fetch import (
    DIVIDEND_DIRNAME,
    FIELDS,
    DividendFetchError,
    calendar_days,
    fetch_dividends,
)
import pytest


def _row(day: str, code: str, proc: str = "实施") -> dict:
    return {
        "ts_code": code,
        "end_date": "20260630",
        "ann_date": day,
        "div_proc": proc,
        "stk_div": 0.0,
        "stk_bo_rate": None,
        "stk_co_rate": None,
        "cash_div": 0.1,
        "cash_div_tax": 0.1,
        "record_date": "20260827",
        "ex_date": "20260828",
        "pay_date": "20260828",
        "div_listdate": None,
        "imp_ann_date": "20260822",
    }


class CalendarDaysTest(unittest.TestCase):
    def test_includes_weekends_and_is_ascending(self) -> None:
        days = calendar_days("20240105", "20240108")
        # 0106/0107 are Sat/Sun — announcement flow does not stop for them.
        self.assertEqual(
            days, ["20240105", "20240106", "20240107", "20240108"]
        )

    def test_bad_range_fails_closed(self) -> None:
        with self.assertRaises(DividendFetchError):
            calendar_days("20240110", "20240101")

    def test_bad_date_fails_closed(self) -> None:
        with self.assertRaises(DividendFetchError):
            calendar_days("20240132", "20240201")


class FetchDividendsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cache = Path(tempfile.mkdtemp())

    def test_writes_one_file_per_day_with_rows(self) -> None:
        def call(day: str) -> list[dict]:
            return [
                _row(day, "000017.SZ"),
                _row(day, "600519.SH", proc="预案"),
            ]

        stats = fetch_dividends(
            self.cache, days=["20260820", "20260821"], call=call
        )
        folder = self.cache / DIVIDEND_DIRNAME
        files = sorted(p.name for p in folder.glob("*.csv"))
        self.assertEqual(files, ["20260820.csv", "20260821.csv"])
        self.assertEqual(stats["files_written"], 2)
        self.assertEqual(stats["rows_seen"], 4)
        with (folder / "20260821.csv").open(encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
            self.assertEqual([r["div_proc"] for r in rows], ["实施", "预案"])
            self.assertEqual(list(rows[0].keys()), FIELDS)

    def test_idempotent_skip_existing(self) -> None:
        calls = {"n": 0}

        def call(day: str) -> list[dict]:
            calls["n"] += 1
            return [_row(day, "000017.SZ")]

        fetch_dividends(self.cache, days=["20260821"], call=call)
        stats = fetch_dividends(self.cache, days=["20260821"], call=call)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(stats["files_written"], 0)
        self.assertEqual(stats["files_skipped"], 1)

    def test_empty_day_writes_nothing_and_is_counted(self) -> None:
        stats = fetch_dividends(
            self.cache, days=["20260821"], call=lambda day: []
        )
        self.assertEqual(stats["empty_days"], 1)
        self.assertEqual(list((self.cache / DIVIDEND_DIRNAME).glob("*.csv")), [])

    def test_rows_without_code_or_mismatched_ann_date_counted_bad(self) -> None:
        def call(day: str) -> list[dict]:
            no_code = _row(day, "")
            stray = _row(day, "600519.SH")
            stray["ann_date"] = "20260101"  # disagrees with queried day
            return [no_code, stray, _row(day, "000001.SZ")]

        stats = fetch_dividends(self.cache, days=["20260821"], call=call)
        self.assertEqual(stats["bad_rows"], 2)
        self.assertEqual(stats["rows_seen"], 3)
        self.assertEqual(stats["empty_days"], 0)

    def test_error_recorded_sweep_continues_exit_semantics(self) -> None:
        def call(day: str) -> list[dict]:
            if day == "20260820":
                raise RuntimeError("transport:boom")
            return [_row(day, "000017.SZ")]

        stats = fetch_dividends(
            self.cache, days=["20260820", "20260821"], call=call
        )
        self.assertEqual(len(stats["errors"]), 1)
        self.assertIn("20260820", stats["errors"][0])
        self.assertEqual(stats["files_written"], 1)
        self.assertTrue(bool(stats["errors"]))

    def test_production_path_requires_token_before_any_provider_call(self) -> None:
        previous = os.environ.pop("TUSHARE_MCP_TOKEN", None)
        try:
            with pytest.raises(DividendFetchError, match="token_missing"):
                fetch_dividends(self.cache, days=["20260821"])
        finally:
            if previous is not None:
                os.environ["TUSHARE_MCP_TOKEN"] = previous

    def test_atomic_write_leaves_no_partial_file(self) -> None:
        fetch_dividends(self.cache, days=["20260821"], call=lambda day: [_row(day, "000017.SZ")])
        folder = self.cache / DIVIDEND_DIRNAME
        self.assertTrue((folder / "20260821.csv").is_file())
        self.assertEqual(list(folder.glob("*.partial")), [])


if __name__ == "__main__":
    unittest.main()
