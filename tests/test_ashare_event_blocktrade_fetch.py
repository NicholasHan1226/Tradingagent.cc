"""Offline tests for the all-market block_trade daily fetcher."""

from __future__ import annotations

import csv
import tempfile
import unittest
import unittest.mock
from datetime import date, timedelta
from pathlib import Path

from Ashare import event_blocktrade_fetch as fetch


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


SESSIONS = _sessions(6)


class SessionDaysTest(unittest.TestCase):
    def test_range_filters_local_calendar(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            _write_csv(
                cache / "index_000001SH.csv",
                ["trade_date", "close"],
                [[d, 100.0] for d in SESSIONS],
            )
            days = fetch._session_days(cache, SESSIONS[1], SESSIONS[4])
            self.assertEqual(days, SESSIONS[1:5])

    def test_missing_index_cache_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(fetch.BlocktradeFetchError):
                fetch._session_days(Path(tmp), "20260101", "20260201")


class FetchSweepTest(unittest.TestCase):
    def _index(self, cache: Path) -> None:
        _write_csv(
            cache / "index_000001SH.csv",
            ["trade_date", "close"],
            [[d, 100.0] for d in SESSIONS],
        )

    def test_fetches_writes_and_is_idempotent(self) -> None:
        calls: list[str] = []

        def fake_call_api(api: str, params: dict):
            self.assertEqual(api, "block_trade")
            calls.append(params["trade_date"])
            return (
                ["ts_code", "trade_date", "price", "vol", "amount",
                 "buyer", "seller"],
                [[
                    "600000.SH", params["trade_date"], 10.0, 500.0,
                    5000.0, "机构专用", "机构专用",
                ]],
            )

        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            self._index(cache)
            with unittest.mock.patch(
                "Ashare.event_calendar_fetch.call_api", side_effect=fake_call_api
            ):
                summary = fetch.fetch_blocktrade_daily(cache)
            self.assertEqual(summary["fetched"] + summary["skipped_existing"],
                             len(SESSIONS))
            day_path = cache / "blocktrade_daily" / f"{SESSIONS[0]}.csv"
            with day_path.open(encoding="utf-8") as handle:
                rows = list(csv.reader(handle))
            self.assertEqual(
                rows[0],
                ["ts_code", "trade_date", "price", "vol", "amount",
                 "buyer", "seller"],
            )
            self.assertEqual(len(rows), 2)  # header + one raw print
            # Rerun hits no network at all.
            calls.clear()
            with unittest.mock.patch(
                "Ashare.event_calendar_fetch.call_api", side_effect=fake_call_api
            ):
                again = fetch.fetch_blocktrade_daily(cache)
            self.assertEqual(calls, [])
            self.assertEqual(again["fetched"], 0)

    def test_empty_and_failed_days_leave_no_file(self) -> None:
        def flaky_call_api(api: str, params: dict):
            if params["trade_date"] == SESSIONS[0]:
                raise RuntimeError("network down")
            return ["ts_code", "trade_date"], []  # empty response

        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            self._index(cache)
            with unittest.mock.patch(
                "Ashare.event_calendar_fetch.call_api",
                side_effect=flaky_call_api,
            ):
                summary = fetch.fetch_blocktrade_daily(cache)
            out_dir = cache / "blocktrade_daily"
            self.assertFalse(any(out_dir.glob("*.csv")))
            self.assertEqual(summary["failed_days"], [SESSIONS[0]])
            self.assertEqual(summary["empty_days"], SESSIONS[1:])
            self.assertEqual(summary["fetched"], 0)

    def test_disk_guard_fails_closed(self) -> None:
        def fake_call_api(api: str, params: dict):
            return ["ts_code", "trade_date"], [
                [params["trade_date"], "600000.SH"]
            ]

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
                    with self.assertRaises(fetch.BlocktradeFetchError):
                        fetch.fetch_blocktrade_daily(cache)
            finally:
                fetch.MIN_FREE_BYTES = original_free


if __name__ == "__main__":
    unittest.main()
