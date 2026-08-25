"""Offline tests for the per-symbol top10_holders fetcher."""

from __future__ import annotations

import csv
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from Ashare import event_top10_holders_fetch as fetch


def _write_csv(path: Path, fields: list[str], rows: list[list]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(fields)
        writer.writerows(rows)


FIELDS = ["ts_code", "ann_date", "end_date", "holder_name",
          "hold_amount", "holder_type"]


class StemsTest(unittest.TestCase):
    def test_stems_come_from_daily_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            _write_csv(cache / "daily_000001SZ.csv",
                       ["trade_date", "close"], [["20260105", 10.0]])
            _write_csv(cache / "daily_600000SH.csv",
                       ["trade_date", "close"], [["20260105", 10.0]])
            _write_csv(cache / "top10_000001SZ.csv",
                       FIELDS, [["000001.SZ", "20260815", "20260630",
                                 "HOLDER", 100, "一般企业"]])
            self.assertEqual(fetch._cached_stems(cache),
                             ["000001SZ", "600000SH"])

    def test_missing_daily_cache_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(fetch.TopTenHoldersFetchError,
                                        "daily_cache_missing"):
                fetch.fetch_top10_holders(Path(tmp))


class FetchSweepTest(unittest.TestCase):
    def _daily(self, cache: Path) -> None:
        _write_csv(cache / "daily_000001SZ.csv",
                   ["trade_date", "close"], [["20260105", 10.0]])
        _write_csv(cache / "daily_600000SH.csv",
                   ["trade_date", "close"], [["20260105", 11.0]])

    def test_fetches_per_symbol_writes_and_is_idempotent(self) -> None:
        calls: list[dict] = []

        def fake_call(api: str, params: dict):
            self.assertEqual(api, "top10_holders")
            calls.append(dict(params))
            return (
                list(FIELDS),
                [["000001.SZ", "20260815", "20260630", "HOLDER", 100,
                  "一般企业"]],
            )

        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            self._daily(cache)
            with unittest.mock.patch(
                "Ashare.event_calendar_fetch.call_api", fake_call
            ):
                summary = fetch.fetch_top10_holders(cache, delay_seconds=0.0)
            self.assertEqual(calls, [{"ts_code": "000001.SZ"},
                                     {"ts_code": "600000.SH"}])
            self.assertEqual(summary["fetched"], 2)
            self.assertEqual(summary["skipped_existing"], 0)
            target = cache / "top10_000001SZ.csv"
            self.assertTrue(target.exists())
            self.assertFalse((cache / "top10_000001SZ.partial").exists())
            with target.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.reader(handle))
            self.assertEqual(rows[0], FIELDS)
            self.assertEqual(len(rows), 2)
            # idempotent rerun skips everything and does not call the API
            with unittest.mock.patch(
                "Ashare.event_calendar_fetch.call_api", fake_call
            ):
                summary2 = fetch.fetch_top10_holders(cache, delay_seconds=0.0)
            self.assertEqual(summary2["skipped_existing"], 2)
            self.assertEqual(summary2["fetched"], 0)
            self.assertEqual(len(calls), 2)

    def test_empty_response_leaves_no_file(self) -> None:
        def fake_call(api: str, params: dict):
            return list(FIELDS), []

        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            self._daily(cache)
            with unittest.mock.patch(
                "Ashare.event_calendar_fetch.call_api", fake_call
            ):
                summary = fetch.fetch_top10_holders(cache, delay_seconds=0.0)
            self.assertEqual(summary["empty_symbols"], ["000001SZ",
                                                        "600000SH"])
            self.assertFalse((cache / "top10_000001SZ.csv").exists())

    def test_failure_records_symbol_and_keeps_sweeping(self) -> None:
        state = {"n": 0}

        def fake_call(api: str, params: dict):
            state["n"] += 1
            if state["n"] == 1:
                raise RuntimeError("boom")
            return (list(FIELDS),
                    [["600000.SH", "20260815", "20260630", "H", 1,
                      "自然人"]])

        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            self._daily(cache)
            with unittest.mock.patch(
                "Ashare.event_calendar_fetch.call_api", fake_call
            ):
                summary = fetch.fetch_top10_holders(cache, delay_seconds=0.0)
            self.assertEqual(summary["failed_symbols"], ["000001SZ"])
            self.assertEqual(summary["fetched"], 1)
            self.assertTrue((cache / "top10_600000SH.csv").exists())


if __name__ == "__main__":
    unittest.main()
