"""Offline tests for the all-market repurchase announcement fetcher."""

from __future__ import annotations

import csv
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from Ashare import event_repurchase_fetch as fetch


class MonthWindowsTest(unittest.TestCase):
    def test_clips_to_range_and_covers_whole_span(self) -> None:
        wins = fetch.month_windows("20250815", "20251010")
        self.assertEqual(
            wins,
            [("20250815", "20250831"), ("20250901", "20250930"),
             ("20251001", "20251010")],
        )

    def test_year_rollover_and_bad_range(self) -> None:
        self.assertEqual(fetch.month_windows("20251201", "20260201"),
                         [("20251201", "20251231"),
                          ("20260101", "20260131"), ("20260201", "20260201")])
        with self.assertRaisesRegex(fetch.RepurchaseFetchError, "bad_range"):
            fetch.month_windows("20260101", "20250101")


def _fake_call(months: dict[str, list[dict]]):
    """Route by any start_date inside a month key like '202508'."""

    def call(start_date: str, end_date: str):
        for month, rows in months.items():
            if start_date.startswith(month):
                return rows
        return []

    return call


class FetchRepurchaseTest(unittest.TestCase):
    def test_groups_by_ann_date_and_keeps_raw_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            rows = [
                {"ts_code": "600361.SH", "ann_date": "20250801",
                 "end_date": "20250801", "proc": "完成", "exp_date": None,
                 "vol": 100.0, "amount": 1.0, "high_limit": "",
                 "low_limit": ""},
                {"ts_code": "000002.SZ", "ann_date": "20250802",
                 "proc": "预案", "extra_col": "dropped"},
                {"ts_code": "600000.SH", "ann_date": "20250802",
                 "proc": "实施"},
            ]
            stats = fetch.fetch_repurchase(
                cache, start="20250801", end="20250831",
                delay=0, call=_fake_call({"202508": rows}),
            )
            folder = cache / "repurchase_ann"
            self.assertEqual(sorted(p.name for p in folder.glob("*.csv")),
                             ["20250801.csv", "20250802.csv"])
            with (folder / "20250802.csv").open(encoding="utf-8") as handle:
                got = list(csv.DictReader(handle))
            self.assertEqual([r["ts_code"] for r in got],
                             ["000002.SZ", "600000.SH"])
            self.assertEqual(got[0]["proc"], "预案")
            self.assertEqual(stats["files_written"], 2)
            self.assertEqual(stats["rows_seen"], 3)

    def test_resume_skips_existing_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            folder = cache / "repurchase_ann"
            folder.mkdir()
            path = folder / "20250801.csv"
            path.write_text("ann_date,ts_code\n20250801,X\n", encoding="utf-8")
            before = path.read_bytes()
            stats = fetch.fetch_repurchase(
                cache, start="20250801", end="20250831", delay=0,
                call=_fake_call({
                    "202508": [{"ts_code": "Y", "ann_date": "20250801"}],
                }),
            )
            self.assertEqual(path.read_bytes(), before)  # never rewritten
            self.assertEqual(stats["files_written"], 0)
            self.assertEqual(stats["files_skipped"], 1)

    def test_bad_rows_counted_not_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            stats = fetch.fetch_repurchase(
                cache, start="20250801", end="20250831", delay=0,
                call=_fake_call({"202508": [
                    {"ts_code": "A", "ann_date": ""},          # no date
                    {"ts_code": "B", "ann_date": "2025080"},   # short
                    {"ann_date": "20250803"},                  # no code
                    {"ts_code": "C", "ann_date": "20250803"},
                ]}),
            )
            self.assertEqual(stats["bad_rows"], 3)
            self.assertEqual(stats["files_written"], 1)

    def test_api_error_recorded_sweep_continues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)

            def boom(start_date: str, end_date: str):
                if start_date.startswith("202507"):
                    raise RuntimeError("net down")
                return [{"ts_code": "A", "ann_date": f"{start_date[:6]}05"}]

            stats = fetch.fetch_repurchase(
                cache, start="20250701", end="20250831", delay=0, call=boom
            )
            self.assertEqual(len(stats["errors"]), 1)  # type: ignore[arg-type]
            self.assertEqual(stats["files_written"], 1)

    def test_disk_guard_fail_closed(self) -> None:
        # Token fail-closed lives in the shared event_calendar_fetch.call_api
        # the default path now resolves to; this module only owns the disk
        # guard.
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            with unittest.mock.patch.object(
                fetch.shutil, "disk_usage",
                return_value=unittest.mock.Mock(free=0),
            ):
                with self.assertRaisesRegex(
                    fetch.RepurchaseFetchError, "disk_full_guard"
                ):
                    fetch.fetch_repurchase(
                        cache, call=_fake_call({}), delay=0
                    )

    def test_default_path_uses_shared_urllib_helper(self) -> None:
        # SDK-free contract: production must resolve the shared helper (the
        # tushare SDK is deliberately absent from this repo's deps).
        calls: list[tuple[str, dict]] = []

        def fake_call_api(api_name: str, params: dict | None = None):
            calls.append((api_name, params or {}))
            return ["ann_date", "ts_code"], [["20260701", "600000.SH"]]

        import Ashare.event_calendar_fetch as calendar_fetch

        with tempfile.TemporaryDirectory() as tmp:
            with unittest.mock.patch.object(
                calendar_fetch, "call_api", fake_call_api
            ):
                stats = fetch.fetch_repurchase(  # type: ignore[arg-type]
                    Path(tmp), start="20260701", end="20260731", delay=0
                )
        self.assertEqual(calls[0], ("repurchase",
                                    {"start_date": "20260701",
                                     "end_date": "20260731"}))
        self.assertEqual(stats["files_written"], 1)  # type: ignore[index]


if __name__ == "__main__":
    unittest.main()
