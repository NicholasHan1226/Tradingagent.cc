"""Offline tests for the per-symbol pledge_stat fetcher."""

from __future__ import annotations

import csv
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from Ashare import event_pledge_fetch as fetch


def _write_csv(path: Path, fields: list[str], rows: list[list]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(fields)
        writer.writerows(rows)


FIELDS = ["ts_code", "end_date", "pledge_count", "unrest_pledge",
          "rest_pledge", "total_share", "pledge_ratio"]


class StemsTest(unittest.TestCase):
    def test_stems_come_from_daily_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            _write_csv(cache / "daily_000001SZ.csv",
                       ["trade_date", "close"], [["20260105", 10.0]])
            _write_csv(cache / "daily_600000SH.csv",
                       ["trade_date", "close"], [["20260105", 10.0]])
            _write_csv(cache / "pledgestat_000001SZ.csv",
                       FIELDS, [["000001.SZ", "20260103", 1, 0.0, 0.0,
                                 100.0, 0.5]])
            self.assertEqual(fetch._cached_stems(cache),
                             ["000001SZ", "600000SH"])

    def test_missing_daily_cache_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(fetch.PledgeFetchError,
                                        "daily_cache_missing"):
                fetch.fetch_pledge(Path(tmp))


class FetchSweepTest(unittest.TestCase):
    def _daily(self, cache: Path) -> None:
        _write_csv(cache / "daily_000001SZ.csv",
                   ["trade_date", "close"], [["20260105", 10.0]])
        _write_csv(cache / "daily_600000SH.csv",
                   ["trade_date", "close"], [["20260105", 11.0]])

    def test_fetches_per_symbol_writes_and_is_idempotent(self) -> None:
        calls: list[dict] = []

        def fake_call(api: str, params: dict):
            self.assertEqual(api, "pledge_stat")
            calls.append(dict(params))
            return (
                list(FIELDS),
                [["000001.SZ", "20260103", 2, 500.0, 100.0, 10000.0, 6.0]],
            )

        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            self._daily(cache)
            with unittest.mock.patch(
                "Ashare.event_calendar_fetch.call_api", fake_call
            ):
                summary = fetch.fetch_pledge(cache, delay_seconds=0.0)
            self.assertEqual(summary["fetched"], 2)
            # both sweeps hit the same endpoint with per-symbol params
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0]["ts_code"], "000001.SZ")
            # regression guard (live-verified 2026-08-24): ANY date param
            # silently empties the pledge_stat response — ts_code only.
            self.assertNotIn("start_date", calls[0])
            self.assertNotIn("end_date", calls[0])
            target = cache / "pledgestat_000001SZ.csv"
            self.assertTrue(target.exists())
            # idempotent rerun: everything skipped, no extra calls
            before = len(calls)
            with unittest.mock.patch(
                "Ashare.event_calendar_fetch.call_api", fake_call
            ):
                again = fetch.fetch_pledge(cache, delay_seconds=0.0)
            self.assertEqual(again["skipped_existing"], 2)
            self.assertEqual(again["fetched"], 0)
            self.assertEqual(len(calls), before)

    def test_empty_and_failed_symbols_leave_no_file(self) -> None:
        def fake_call(api: str, params: dict):
            if params["ts_code"] == "000001.SZ":
                raise RuntimeError("api down")
            return (list(FIELDS), [])  # never pledged: empty response

        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            self._daily(cache)
            with unittest.mock.patch(
                "Ashare.event_calendar_fetch.call_api", fake_call
            ):
                summary = fetch.fetch_pledge(cache, delay_seconds=0.0)
            self.assertEqual(summary["failed_symbols"], ["000001SZ"])
            self.assertEqual(summary["empty_symbols"], ["600000SH"])
            self.assertFalse((cache / "pledgestat_000001SZ.csv").exists())
            self.assertFalse((cache / "pledgestat_600000SH.csv").exists())

    def test_disk_guard_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            self._daily(cache)
            with unittest.mock.patch(
                "shutil.disk_usage"
            ) as disk:
                disk.return_value = unittest.mock.Mock(free=1)
                with self.assertRaisesRegex(fetch.PledgeFetchError,
                                            "disk_low"):
                    fetch.fetch_pledge(cache, delay_seconds=0.0)

    def test_stem_to_code_mapping(self) -> None:
        self.assertEqual(fetch.stem_to_code("000001SZ"), "000001.SZ")
        self.assertEqual(fetch.stem_to_code("600000SH"), "600000.SH")


if __name__ == "__main__":
    unittest.main()
