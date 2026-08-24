"""Offline tests for the dragon-tiger list daily fetcher."""

from __future__ import annotations

import csv
import unittest
from pathlib import Path

from Ashare.event_toplist_fetch import (
    FIELDS,
    TOPLIST_DIRNAME,
    ToplistFetchError,
    fetch_toplist,
    resolve_trading_days,
)


def _row(day: str, code: str) -> dict:
    return {
        "trade_date": day,
        "ts_code": code,
        "name": "X",
        "close": 10.0,
        "pct_change": -7.1,
        "turnover_rate": 3.2,
        "amount": 1000.0,
        "l_sell": 500.0,
        "l_buy": 100.0,
        "l_amount": 600.0,
        "net_amount": -400.0,
        "net_rate": -5.0,
        "amount_rate": 8.0,
        "float_values": 900.0,
        "reason": "日跌幅偏离值达到7%的前5只证券",
    }


class ResolveTradingDaysTest(unittest.TestCase):
    def test_filters_and_sorts_index_calendar(self) -> None:
        cache = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
        self.assertFalse(
            (cache / "index_000001SH.csv").exists(),
            "fixture must not exist; fabricate in tmp instead",
        )
        tmp = self._tmp_index_cache()
        days = resolve_trading_days(tmp, "20240102", "20240110")
        self.assertEqual(days, ["20240102", "20240103", "20240105", "20240108"])

    def test_bad_range_fails_closed(self) -> None:
        with self.assertRaises(ToplistFetchError):
            resolve_trading_days(self._tmp_index_cache(), "20240110", "20240101")

    def test_missing_index_csv_fails_closed(self) -> None:
        with self.assertRaises(ToplistFetchError) as ctx:
            resolve_trading_days(
                Path(__file__).resolve().parent / "no_such_dir", "20240101", "20240131"
            )
        self.assertIn("calendar_unavailable", str(ctx.exception))

    def _tmp_index_cache(self) -> Path:
        import tempfile

        tmp = Path(tempfile.mkdtemp())
        path = tmp / "index_000001SH.csv"
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["ts_code", "trade_date", "close"])
            for day in ("20240102", "20240103", "20240105", "20240108"):
                writer.writerow(["000001.SH", day, "3000.0"])
        return tmp


class FetchToplistTest(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self.cache = Path(tempfile.mkdtemp())

    def test_writes_one_file_per_day_with_rows(self) -> None:
        def call(day: str) -> list[dict]:
            return [_row(day, "000017.SZ"), _row(day, "600519.SH")]

        stats = fetch_toplist(
            self.cache, days=["20260820", "20260821"], call=call
        )
        folder = self.cache / TOPLIST_DIRNAME
        files = sorted(p.name for p in folder.glob("*.csv"))
        self.assertEqual(files, ["20260820.csv", "20260821.csv"])
        self.assertEqual(stats["files_written"], 2)
        self.assertEqual(stats["rows_seen"], 4)
        with (folder / "20260821.csv").open(encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["ts_code"], "000017.SZ")
            self.assertEqual(list(rows[0].keys()), FIELDS)

    def test_idempotent_skip_existing(self) -> None:
        calls = {"n": 0}

        def call(day: str) -> list[dict]:
            calls["n"] += 1
            return [_row(day, "000017.SZ")]

        fetch_toplist(self.cache, days=["20260821"], call=call)
        stats = fetch_toplist(self.cache, days=["20260821"], call=call)
        self.assertEqual(calls["n"], 2)  # endpoint still queried...
        self.assertEqual(stats["files_written"], 0)
        self.assertEqual(stats["files_skipped"], 1)  # ...but file preserved
        self.assertEqual(stats["empty_days"], 0)

    def test_empty_real_day_writes_nothing_and_is_counted(self) -> None:
        def call(day: str) -> list[dict]:
            return []

        stats = fetch_toplist(self.cache, days=["20260821"], call=call)
        self.assertEqual(stats["empty_days"], 1)
        self.assertEqual(list((self.cache / TOPLIST_DIRNAME).glob("*.csv")), [])

    def test_bad_rows_counted_not_written(self) -> None:
        def call(day: str) -> list[dict]:
            bad = dict(_row(day, "000017.SZ"))
            bad["trade_date"] = "19990101"  # mismatched day key
            return [bad, {"ts_code": "", "trade_date": day}]

        stats = fetch_toplist(self.cache, days=["20260821"], call=call)
        self.assertEqual(stats["bad_rows"], 2)
        self.assertEqual(stats["empty_days"], 1)
        self.assertEqual(stats["files_written"], 0)

    def test_error_recorded_sweep_continues_exit_semantics(self) -> None:
        def call(day: str) -> list[dict]:
            if day == "20260820":
                raise RuntimeError("transport:boom")
            return [_row(day, "000017.SZ")]

        stats = fetch_toplist(
            self.cache, days=["20260820", "20260821"], call=call
        )
        self.assertEqual(len(stats["errors"]), 1)
        self.assertIn("20260820", stats["errors"][0])
        self.assertEqual(stats["files_written"], 1)
        # main() maps non-empty errors to exit code 1 (documented here)
        self.assertTrue(bool(stats["errors"]))


if __name__ == "__main__":
    unittest.main()
