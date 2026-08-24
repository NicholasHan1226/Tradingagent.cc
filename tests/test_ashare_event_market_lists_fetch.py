"""Offline tests for the per-day suspension & limit-list fetcher."""

from __future__ import annotations

import csv
import tempfile
import unittest
import unittest.mock
from datetime import date, timedelta
from pathlib import Path

from Ashare import event_market_lists_fetch as fetch


def _write_csv(path: Path, fields: list[str], rows: list[list]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(fields)
        writer.writerows(rows)


def _sessions(count: int) -> list[str]:
    start = date(2026, 1, 5)
    out: list[str] = []
    cursor = start
    while len(out) < count:
        if cursor.weekday() < 5:
            out.append(cursor.strftime("%Y%m%d"))
        cursor += timedelta(days=1)
    return out


SESSIONS = _sessions(4)


class SessionDaysTest(unittest.TestCase):
    def test_range_filters_local_calendar(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            _write_csv(
                cache / "index_000001SH.csv",
                ["trade_date", "close"],
                [[d, 100.0] for d in SESSIONS],
            )
            days = fetch._session_days(cache, SESSIONS[1], SESSIONS[3])
            self.assertEqual(days, SESSIONS[1:4])

    def test_missing_index_cache_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(fetch.MarketListsFetchError,
                                        "index_cache_missing"):
                fetch._session_days(Path(tmp), "20260101", "20260201")


class FetchSweepTest(unittest.TestCase):
    def _index(self, cache: Path) -> None:
        _write_csv(
            cache / "index_000001SH.csv",
            ["trade_date", "close"],
            [[d, 100.0] for d in SESSIONS],
        )

    def test_fetches_both_sources_and_is_idempotent(self) -> None:
        calls: list[tuple[str, str]] = []

        def fake_call_api(api: str, params: dict):
            self.assertEqual(list(params), ["trade_date"])
            calls.append((api, params["trade_date"]))
            rows = {
                "suspend_d": [["600000.SH", params["trade_date"], "S"]],
                "limit_list_d": [[
                    "000001.SZ", params["trade_date"], "U", 10.0,
                ]],
            }[api]
            fields = {
                "suspend_d": ["ts_code", "trade_date", "suspend_type"],
                "limit_list_d": ["ts_code", "trade_date", "limit_type",
                                 "close"],
            }[api]
            return fields, rows

        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            self._index(cache)
            with unittest.mock.patch(
                "Ashare.event_calendar_fetch.call_api", side_effect=fake_call_api
            ):
                summary = fetch.fetch_market_lists(cache)
            # every session x every endpoint was requested exactly once
            self.assertEqual(
                sorted(calls),
                sorted((api, d) for api, _ in fetch.LIST_SOURCES
                       for d in SESSIONS),
            )
            suspend_path = cache / "suspend_daily" / f"{SESSIONS[0]}.csv"
            with suspend_path.open(encoding="utf-8") as handle:
                rows = list(csv.reader(handle))
            self.assertEqual(rows[0],
                             ["ts_code", "trade_date", "suspend_type"])
            self.assertEqual(len(rows), 2)  # header + one raw row
            self.assertTrue(
                (cache / "limitlist_daily" / f"{SESSIONS[2]}.csv").exists()
            )
            self.assertEqual(summary["sources"]["suspend_d"]["fetched"],
                             len(SESSIONS))
            # Rerun hits no network at all.
            calls.clear()
            with unittest.mock.patch(
                "Ashare.event_calendar_fetch.call_api", side_effect=fake_call_api
            ):
                again = fetch.fetch_market_lists(cache)
            self.assertEqual(calls, [])
            self.assertEqual(again["sources"]["suspend_d"]["skipped_existing"],
                             len(SESSIONS))

    def test_empty_and_failed_days_leave_no_file(self) -> None:
        def flaky_call_api(api: str, params: dict):
            day = params["trade_date"]
            if api == "limit_list_d" and day == SESSIONS[0]:
                raise RuntimeError("network down")
            if api == "suspend_d" and day != SESSIONS[1]:
                return ["ts_code", "trade_date"], []  # no rosters that day
            return ["ts_code", "trade_date"], [["600000.SH", day]]

        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            self._index(cache)
            with unittest.mock.patch(
                "Ashare.event_calendar_fetch.call_api", side_effect=flaky_call_api
            ):
                summary = fetch.fetch_market_lists(cache)
            sources = summary["sources"]
            self.assertEqual(sources["suspend_d"]["fetched"], 1)
            self.assertEqual(sources["suspend_d"]["empty_days"],
                             [d for d in SESSIONS if d != SESSIONS[1]])
            self.assertEqual(sources["limit_list_d"]["failed_days"],
                             [SESSIONS[0]])
            # the failed day left no file; the successful suspend day did
            self.assertFalse((cache / "limitlist_daily"
                              / f"{SESSIONS[0]}.csv").exists())
            self.assertEqual(
                sorted(p.name for p in
                       (cache / "suspend_daily").glob("*.csv")),
                [f"{SESSIONS[1]}.csv"],
            )

    def test_disk_guard_fails_closed(self) -> None:
        def fake_call_api(api: str, params: dict):
            return ["ts_code", "trade_date"], [["600000.SH",
                                                params["trade_date"]]]

        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            self._index(cache)
            original_free = fetch.MIN_FREE_BYTES
            fetch.MIN_FREE_BYTES = 10 ** 30  # impossible free-space demand
            try:
                with unittest.mock.patch(
                    "Ashare.event_calendar_fetch.call_api",
                    side_effect=fake_call_api,
                ):
                    with self.assertRaises(fetch.MarketListsFetchError):
                        fetch.fetch_market_lists(cache)
            finally:
                fetch.MIN_FREE_BYTES = original_free

    def test_no_sessions_in_range_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            self._index(cache)
            with self.assertRaisesRegex(fetch.MarketListsFetchError,
                                        "no_sessions_in_range"):
                fetch.fetch_market_lists(cache, start="19990101",
                                         end="19991231")


if __name__ == "__main__":
    unittest.main()
