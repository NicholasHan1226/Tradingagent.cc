"""Offline tests for the dragon-tiger branch-seat daily fetcher."""

from __future__ import annotations

import csv
import os
import tempfile
import unittest
from pathlib import Path

from Ashare.event_topinst_fetch import (
    FIELDS,
    TOPINST_DIRNAME,
    TopinstFetchError,
    fetch_topinst,
    resolve_trading_days,
)
import pytest


def _row(day: str, code: str, exalter: str = "机构专用") -> dict:
    return {
        "trade_date": day,
        "ts_code": code,
        "exalter": exalter,
        "buy": 100.0,
        "buy_rate": 5.0,
        "sell": 0.0,
        "sell_rate": 0.0,
        "net_buy": 100.0,
        "side": "1",
        "reason": "日涨幅偏离值达到7%的前5只证券",
    }


class ResolveTradingDaysTest(unittest.TestCase):
    def test_filters_and_sorts_index_calendar(self) -> None:
        tmp = self._tmp_index_cache()
        days = resolve_trading_days(tmp, "20240102", "20240110")
        self.assertEqual(days, ["20240102", "20240103", "20240105", "20240108"])

    def test_bad_range_fails_closed(self) -> None:
        with self.assertRaises(TopinstFetchError):
            resolve_trading_days(self._tmp_index_cache(), "20240110", "20240101")

    def test_missing_index_csv_fails_closed(self) -> None:
        with self.assertRaises(TopinstFetchError) as ctx:
            resolve_trading_days(
                Path(__file__).resolve().parent / "no_such_dir",
                "20240101",
                "20240131",
            )
        self.assertIn("calendar_unavailable", str(ctx.exception))

    def _tmp_index_cache(self) -> Path:
        tmp = Path(tempfile.mkdtemp())
        path = tmp / "index_000001SH.csv"
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["ts_code", "trade_date", "close"])
            for day in ("20240102", "20240103", "20240105", "20240108"):
                writer.writerow(["000001.SH", day, "3000.0"])
        return tmp


class FetchTopinstTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cache = Path(tempfile.mkdtemp())

    def test_writes_one_file_per_day_with_rows(self) -> None:
        def call(day: str) -> list[dict]:
            return [
                _row(day, "000017.SZ"),
                _row(day, "600519.SH", "华泰证券股份有限公司南京分公司"),
            ]

        stats = fetch_topinst(
            self.cache, days=["20260820", "20260821"], call=call
        )
        folder = self.cache / TOPINST_DIRNAME
        files = sorted(p.name for p in folder.glob("*.csv"))
        self.assertEqual(files, ["20260820.csv", "20260821.csv"])
        self.assertEqual(stats["files_written"], 2)
        self.assertEqual(stats["rows_seen"], 4)
        with (folder / "20260821.csv").open(encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["exalter"], "机构专用")
            self.assertEqual(list(rows[0].keys()), FIELDS)

    def test_idempotent_skip_existing(self) -> None:
        calls = {"n": 0}

        def call(day: str) -> list[dict]:
            calls["n"] += 1
            return [_row(day, "000017.SZ")]

        fetch_topinst(self.cache, days=["20260821"], call=call)
        stats = fetch_topinst(self.cache, days=["20260821"], call=call)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(stats["files_written"], 0)
        self.assertEqual(stats["files_skipped"], 1)

    def test_empty_real_day_writes_nothing_and_is_counted(self) -> None:
        stats = fetch_topinst(
            self.cache, days=["20260821"], call=lambda day: []
        )
        self.assertEqual(stats["empty_days"], 1)
        self.assertEqual(list((self.cache / TOPINST_DIRNAME).glob("*.csv")), [])

    def test_rows_without_seat_name_counted_bad(self) -> None:
        def call(day: str) -> list[dict]:
            no_seat = _row(day, "000017.SZ")
            no_seat["exalter"] = ""
            return [no_seat, _row(day, "600519.SH")]

        stats = fetch_topinst(self.cache, days=["20260821"], call=call)
        self.assertEqual(stats["bad_rows"], 1)
        self.assertEqual(stats["rows_seen"], 2)
        self.assertEqual(stats["empty_days"], 0)

    def test_error_recorded_sweep_continues_exit_semantics(self) -> None:
        def call(day: str) -> list[dict]:
            if day == "20260820":
                raise RuntimeError("transport:boom")
            return [_row(day, "000017.SZ")]

        stats = fetch_topinst(
            self.cache, days=["20260820", "20260821"], call=call
        )
        self.assertEqual(len(stats["errors"]), 1)
        self.assertIn("20260820", stats["errors"][0])
        self.assertEqual(stats["files_written"], 1)
        self.assertTrue(bool(stats["errors"]))

    def test_production_path_requires_token_before_any_provider_call(self) -> None:
        previous = os.environ.pop("TUSHARE_MCP_TOKEN", None)
        try:
            with pytest.raises(TopinstFetchError, match="token_missing"):
                fetch_topinst(self.cache, days=["20260821"])
        finally:
            if previous is not None:
                os.environ["TUSHARE_MCP_TOKEN"] = previous

    def test_atomic_write_leaves_no_partial_file(self) -> None:
        fetch_topinst(self.cache, days=["20260821"], call=lambda day: [_row(day, "000017.SZ")])
        folder = self.cache / TOPINST_DIRNAME
        self.assertTrue((folder / "20260821.csv").is_file())
        self.assertEqual(list(folder.glob("*.partial")), [])


if __name__ == "__main__":
    unittest.main()
