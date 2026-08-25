"""Offline tests for the per-symbol daily_basic fetcher."""

from __future__ import annotations

import contextlib
import csv
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

from Ashare import event_dailybasic_fetch as fetch
from Ashare.event_calendar_fetch import PAGE_LIMIT, _shift_date


def _write_csv(path: Path, fields: list[str], rows: list[list]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(fields)
        writer.writerows(rows)


TODAY = time.strftime("%Y%m%d")
FIELDS = ["ts_code", "trade_date", "close", "turnover_rate",
          "turnover_rate_f", "volume_ratio", "pe_ttm", "pb",
          "total_share", "float_share", "free_share", "total_mv", "circ_mv"]


class StemsTest(unittest.TestCase):
    def test_stems_come_from_daily_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            _write_csv(cache / "daily_000001SZ.csv",
                       ["trade_date", "close"], [["20260105", 10.0]])
            _write_csv(cache / "daily_600000SH.csv",
                       ["trade_date", "close"], [["20260105", 10.0]])
            _write_csv(cache / "adjfactor_000001SZ.csv",
                       ["trade_date", "adj_factor"], [["20260105", 1.0]])
            self.assertEqual(fetch._cached_stems(cache),
                             ["000001SZ", "600000SH"])

    def test_missing_daily_cache_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(fetch.DailybasicFetchError,
                                        "daily_cache_missing"):
                fetch.fetch_dailybasic(Path(tmp))


class FetchSweepTest(unittest.TestCase):
    def _daily(self, cache: Path) -> None:
        _write_csv(cache / "daily_000001SZ.csv",
                   ["trade_date", "close"], [["20260105", 10.0]])
        _write_csv(cache / "daily_600000SH.csv",
                   ["trade_date", "close"], [["20260105", 11.0]])

    def test_fetches_per_symbol_writes_and_is_idempotent(self) -> None:
        calls: list[str] = []

        def fake_call_api(api: str, params: dict):
            self.assertEqual(api, "daily_basic")
            calls.append(params["ts_code"])
            return (
                FIELDS,
                [[params["ts_code"], TODAY, 10.0, 1.23, 1.5, 0.9,
                  12.3, 1.1, 100.0, 80.0, 60.0, 1000.0, 800.0]],
            )

        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            self._daily(cache)
            with unittest.mock.patch(
                "Ashare.event_calendar_fetch.call_api",
                side_effect=fake_call_api,
            ):
                summary = fetch.fetch_dailybasic(cache)
            self.assertEqual(sorted(calls), ["000001.SZ", "600000.SH"])
            self.assertEqual(summary["fetched"], 2)
            path = cache / "dailybasic_000001SZ.csv"
            with path.open(encoding="utf-8") as handle:
                rows = list(csv.reader(handle))
            self.assertEqual(rows[0], FIELDS)
            self.assertEqual(rows[1][4], "1.5")  # raw row stays raw
            # Rerun hits no network at all.
            calls.clear()
            with unittest.mock.patch(
                "Ashare.event_calendar_fetch.call_api",
                side_effect=fake_call_api,
            ):
                again = fetch.fetch_dailybasic(cache)
            self.assertEqual(calls, [])
            self.assertEqual(again["fetched"], 0)
            self.assertEqual(again["skipped_existing"], 2)

    def test_empty_and_failed_symbols_leave_no_file(self) -> None:
        def flaky_call_api(api: str, params: dict):
            if params["ts_code"] == "000001.SZ":
                raise RuntimeError("network down")
            return FIELDS, []  # empty history

        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            self._daily(cache)
            with unittest.mock.patch(
                "Ashare.event_calendar_fetch.call_api",
                side_effect=flaky_call_api,
            ):
                summary = fetch.fetch_dailybasic(cache)
            self.assertFalse(any(cache.glob("dailybasic_*.csv")))
            self.assertEqual(summary["failed_symbols"], ["000001SZ"])
            self.assertEqual(summary["empty_symbols"], ["600000SH"])
            self.assertEqual(summary["fetched"], 0)

    def test_disk_guard_fails_closed(self) -> None:
        def fake_call_api(api: str, params: dict):
            return FIELDS, [[params["ts_code"], "20260821"] + [1.0] * 11]

        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            self._daily(cache)
            original_free = fetch.MIN_FREE_BYTES
            fetch.MIN_FREE_BYTES = 10 ** 30  # impossible free-space demand
            try:
                with unittest.mock.patch(
                    "Ashare.event_calendar_fetch.call_api",
                    side_effect=fake_call_api,
                ):
                    with self.assertRaises(fetch.DailybasicFetchError):
                        fetch.fetch_dailybasic(cache)
            finally:
                fetch.MIN_FREE_BYTES = original_free

    def test_stem_to_code_mapping(self) -> None:
        self.assertEqual(fetch.stem_to_code("000001SZ"), "000001.SZ")
        self.assertEqual(fetch.stem_to_code("600000SH"), "600000.SH")


class FreshnessRefreshTest(unittest.TestCase):
    """Stale shards re-pull in full and overwrite; fresh shards skip (#543).

    The valuation labeler snaps entry days to each shard's own last
    session, so a shard frozen at creation time would silently age every
    later label — the same failure class as #542's frozen bar shards.
    """

    def _daily(self, cache: Path) -> None:
        _write_csv(cache / "daily_000001SZ.csv",
                   ["trade_date", "close"], [["20260105", 10.0]])

    def test_stale_shard_is_refreshed_and_overwritten(self) -> None:
        calls: list[str] = []

        def fake_call_api(api: str, params: dict):
            self.assertEqual(api, "daily_basic")
            calls.append(params["ts_code"])
            return (
                FIELDS,
                [[params["ts_code"], TODAY, 11.0, 2.34, 2.5, 1.9,
                  23.4, 2.2, 200.0, 160.0, 120.0, 2000.0, 1600.0]],
            )

        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            self._daily(cache)
            # Shard frozen in early 2020 -> far beyond any freshness ceiling.
            _write_csv(cache / "dailybasic_000001SZ.csv", FIELDS,
                       [["000001.SZ", "20200102"] + [1.0] * 11])
            with unittest.mock.patch(
                "Ashare.event_calendar_fetch.call_api",
                side_effect=fake_call_api,
            ):
                summary = fetch.fetch_dailybasic(cache)
            self.assertEqual(calls, ["000001.SZ"])
            self.assertEqual(summary["refresh_todo"], 1)
            self.assertEqual(summary["fetched"], 1)
            self.assertEqual(summary["skipped_existing"], 0)
            with (cache / "dailybasic_000001SZ.csv").open(
                encoding="utf-8"
            ) as handle:
                rows = list(csv.reader(handle))
            self.assertEqual(rows[1][1], TODAY)  # stale row fully replaced

    def test_fresh_shard_skips_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            self._daily(cache)
            _write_csv(cache / "dailybasic_000001SZ.csv", FIELDS,
                       [["000001.SZ", TODAY] + [1.0] * 11])
            summary = fetch.fetch_dailybasic(cache, delay_seconds=0)
            self.assertEqual(summary["skipped_existing"], 1)
            self.assertEqual(summary["refresh_todo"], 0)
            self.assertEqual(summary["fetched"], 0)

    def test_explicit_end_overrides_dynamic_default(self) -> None:
        def fake_call_api(api: str, params: dict):
            self.assertEqual(params["end_date"], "20260101")
            return FIELDS, []

        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            self._daily(cache)
            with unittest.mock.patch(
                "Ashare.event_calendar_fetch.call_api",
                side_effect=fake_call_api,
            ):
                summary = fetch.fetch_dailybasic(cache, end="20260101")
            self.assertEqual(summary["empty_symbols"], ["000001SZ"])





class TopupTest(unittest.TestCase):
    """Recently stale shards refresh via whole-market session pulls.

    One ``daily_basic`` trade_date call covers every cached symbol for one
    session, so a weekly gap costs one call per missing session instead of
    one per symbol (#560 follow-up to the #543 freshness gate).  Patches
    BOTH call_api bindings the sweep touches: the fetcher's function-local
    import (event_calendar_fetch) and trading_days' module global
    (event_calendar_expand_samples) — missing either silently reaches the
    real network.
    """

    def _seed_recent_stale(self, cache: Path) -> str:
        # Beyond the 6-day freshness ceiling, inside the 30-day top-up
        # window.
        last = _shift_date(TODAY, -10)
        _write_csv(cache / "daily_000001SZ.csv",
                   ["trade_date", "close"], [[last, 10.0]])
        _write_csv(cache / "dailybasic_000001SZ.csv", FIELDS,
                   [["000001.SZ", last] + [1.0] * 11])
        return last

    @staticmethod
    def _fake_call(last: str, fail_days=()):
        d1 = _shift_date(last, 1)
        d2 = _shift_date(last, 3)
        sessions = [(d1, "1"), (d2, "1"), (TODAY, "1")]
        calls: list[tuple] = []

        def fake(api: str, params: dict):
            calls.append((api, dict(params)))
            if api == "trade_cal":
                return ["cal_date", "is_open"], [list(s) for s in sessions]
            assert api == "daily_basic"
            day = params["trade_date"]
            if day in fail_days:
                raise RuntimeError("network down")
            return FIELDS, [
                ["999999.SZ", day] + [9.0] * 11,   # unknown symbol ignored
                ["000001.SZ", day] + [2.0] * 11,
            ]
        return fake, calls

    def test_topup_projects_market_rows_newest_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            last = self._seed_recent_stale(cache)
            fake, _calls = self._fake_call(last)
            with \
                unittest.mock.patch(
                    "Ashare.event_calendar_fetch.call_api",
                    side_effect=fake,
                ), \
                unittest.mock.patch(
                    "Ashare.event_calendar_expand_samples.call_api",
                    side_effect=fake,
                ):
                summary = fetch.fetch_dailybasic(cache)
            with (cache / "dailybasic_000001SZ.csv").open(
                encoding="utf-8"
            ) as handle:
                rows = list(csv.reader(handle))
            self.assertEqual(rows[0], FIELDS)
            dates = [r[1] for r in rows[1:]]
            expected = [TODAY, _shift_date(last, 3), _shift_date(last, 1),
                        last]
            self.assertEqual(dates, expected)
            self.assertEqual(summary["topup_projected"], 1)
            self.assertEqual(summary["refresh_todo"], 1)
            self.assertEqual(summary["failed_dates"], [])
            self.assertEqual(summary["failed_symbols"], [])
            # Rerun hits no network at all: the shard is current.
            fake2, calls2 = self._fake_call(last)
            with \
                unittest.mock.patch(
                    "Ashare.event_calendar_fetch.call_api",
                    side_effect=fake2,
                ), \
                unittest.mock.patch(
                    "Ashare.event_calendar_expand_samples.call_api",
                    side_effect=fake2,
                ):
                again = fetch.fetch_dailybasic(cache)
            data_calls = [c for c in calls2 if c[0] != "trade_cal"]
            self.assertEqual(data_calls, [])
            self.assertEqual(again["skipped_existing"], 1)

    def test_failed_session_recorded_not_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            last = self._seed_recent_stale(cache)
            d1 = _shift_date(last, 1)
            fake, _calls = self._fake_call(last, fail_days=(d1,))
            with \
                unittest.mock.patch(
                    "Ashare.event_calendar_fetch.call_api",
                    side_effect=fake,
                ), \
                unittest.mock.patch(
                    "Ashare.event_calendar_expand_samples.call_api",
                    side_effect=fake,
                ):
                summary = fetch.fetch_dailybasic(cache)
            self.assertEqual(summary["failed_dates"], [d1])
            self.assertEqual(summary["topup_projected"], 1)
            # The exit rule treats a failed session like a failed symbol.
            failed = bool(summary["failed_symbols"]) or \
                bool(summary["failed_dates"])
            self.assertTrue(failed)

    def test_cap_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            self._seed_recent_stale(cache)
            capped_rows = [["999999.SZ", "20260821"] + [1.0] * 11] * \
                PAGE_LIMIT

            def fake(api: str, params: dict):
                if api == "trade_cal":
                    return ["cal_date", "is_open"], [["20260821", "1"]]
                return FIELDS, capped_rows

            with \
                unittest.mock.patch(
                    "Ashare.event_calendar_fetch.call_api",
                    side_effect=fake,
                ), \
                unittest.mock.patch(
                    "Ashare.event_calendar_expand_samples.call_api",
                    side_effect=fake,
                ):
                with self.assertRaisesRegex(fetch.DailybasicFetchError,
                                            "date_capped"):
                    fetch.fetch_dailybasic(cache)


if __name__ == "__main__":
    unittest.main()
