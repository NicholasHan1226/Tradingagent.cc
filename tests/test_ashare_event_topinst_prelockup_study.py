"""Offline tests for the pre-lockup branch-seat confirmation study."""

from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from Ashare.event_topinst_prelockup_study import (
    TOPINST_BUCKETS,
    TopinstStudyError,
    attach_topinst_states,
    classify_seat,
    load_topinst_index,
    topinst_buckets_for_events,
)


def _write_day(cache: Path, day: str, rows: list[dict]) -> None:
    folder = cache / "topinst_daily"
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / f"{day}.csv").open("w", newline="", encoding="utf-8") as h:
        writer = csv.DictWriter(h, fieldnames=["ts_code", "exalter", "net_buy"])
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


class ClassifySeatTest(unittest.TestCase):
    def test_frozen_identity_mapping(self) -> None:
        self.assertEqual(classify_seat("机构专用"), "inst")
        self.assertEqual(classify_seat("深股通专用"), "connect")
        self.assertEqual(classify_seat("沪股通专用"), "connect")
        name = "东方财富证券股份有限公司拉萨团结路第二证券营业部"
        self.assertEqual(classify_seat(name), "branch")
        # near-miss names must not leak into inst
        self.assertEqual(classify_seat("机构专用席位"), "branch")
        self.assertEqual(classify_seat(""), "branch")
        self.assertEqual(classify_seat(None), "branch")


class LoadTopinstIndexTest(unittest.TestCase):
    def test_empty_cache_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(TopinstStudyError) as ctx:
                load_topinst_index(Path(tmp))
            self.assertIn("topinst_cache_missing", str(ctx.exception))

    def test_rows_indexed_with_per_day_inst_sum_ascending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            _write_day(cache, "20260810", [
                {"ts_code": "000001.SZ", "exalter": "机构专用", "net_buy": "100"},
                {"ts_code": "000001.SZ", "exalter": "深股通专用", "net_buy": "500"},
            ])
            _write_day(cache, "20260805", [
                {"ts_code": "000001.SZ", "exalter": "机构专用", "net_buy": "-30"},
                # codeless row skipped entirely
                {"ts_code": "", "exalter": "机构专用", "net_buy": "999"},
                # unparseable net_buy contributes 0.0 but still a listing day
                {"ts_code": "600519.SH", "exalter": "机构专用", "net_buy": "n/a"},
            ])
            index = load_topinst_index(cache)
            days, sums = index["000001.SZ"]
            self.assertEqual(days, ["20260805", "20260810"])
            self.assertEqual(sums, [-30.0, 100.0])  # connect excluded from sum
            days2, sums2 = index["600519.SH"]
            self.assertEqual(days2, ["20260805"])
            self.assertEqual(sums2, [0.0])


class AttachTopinstStatesTest(unittest.TestCase):
    def _index(self) -> dict:
        return {
            # mixed signs across the window; total +150 → inst_netbuy
            "000001.SZ": (
                ["20260801", "20260810", "20260815"],
                [50.0, -20.0, 120.0],
            ),
            "600519.SH": (["20260701"], [-80.0]),  # far outside windows below
        }

    def test_window_aggregation_and_edges(self) -> None:
        signals = [{"ts_code": "000001.SZ", "entry_day": "20260820"}]
        stats = attach_topinst_states(signals, self._index())
        self.assertEqual(signals[0]["topinst_bucket"], "inst_netbuy")
        self.assertEqual(signals[0]["topinst_hits"], 3)
        self.assertEqual(signals[0]["topinst_inst_netbuy_sum"], 150.0)
        self.assertEqual(signals[0]["topinst_lag_days"], 5)
        self.assertEqual(stats["attached"], 1)

    def test_inclusive_start_and_exclusive_entry(self) -> None:
        index = {"000003.SZ": (["20260728", "20260827"], [-10.0, 5.0])}
        signals = [{"ts_code": "000003.SZ", "entry_day": "20260827"}]
        attach_topinst_states(signals, index)
        # 20260728 is exactly entry-30d (inclusive); 20260827 is the
        # entry day itself (excluded) → window sum −10
        self.assertEqual(signals[0]["topinst_bucket"], "inst_netsell")
        self.assertEqual(signals[0]["topinst_hits"], 1)

    def test_zero_sum_and_inst_free_listings_are_listed_no_inst(self) -> None:
        index = {
            # exact-zero Σ degenerate case
            "000004.SZ": (["20260801"], [0.0]),
            # connect-only listing: day marked, direction contribution zero
            "000005.SZ": (["20260802"], [0.0]),
        }
        signals = [
            {"ts_code": "000004.SZ", "entry_day": "20260815"},
            {"ts_code": "000005.SZ", "entry_day": "20260815"},
        ]
        stats = attach_topinst_states(signals, index)
        for s in signals:
            self.assertEqual(s["topinst_bucket"], "listed_no_inst")

    def test_no_listing_is_a_label(self) -> None:
        signals = [
            {"ts_code": "999999.SZ", "entry_day": "20260820"},  # no file
            {"ts_code": "600519.SH", "entry_day": "20260915"},  # too old
        ]
        stats = attach_topinst_states(signals, self._index())
        for s in signals:
            self.assertEqual(s["topinst_bucket"], "no_listing")
            self.assertIsNone(s["topinst_lag_days"])
            self.assertIsNone(s["topinst_inst_netbuy_sum"])
        self.assertEqual(stats["no_listing"], 2)
        self.assertEqual(
            set(TOPINST_BUCKETS),
            {"inst_netbuy", "inst_netsell", "listed_no_inst", "no_listing"},
        )

    def test_bad_entry_day_degrades_to_no_listing(self) -> None:
        signals = [{"ts_code": "000001.SZ", "entry_day": "not-a-day"}]
        stats = attach_topinst_states(signals, self._index())
        self.assertEqual(signals[0]["topinst_bucket"], "no_listing")
        self.assertEqual(stats["attached"], 1)


class SideTableTest(unittest.TestCase):
    def test_pairs_all_labelled_far_outside_is_no_listing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            _write_day(cache, "20260810", [
                {"ts_code": "000001.SZ", "exalter": "机构专用", "net_buy": "42"},
            ])
            labels = topinst_buckets_for_events(
                cache,
                [("000001.SZ", "20260815"), ("600519.SH", "20260815")],
            )
            self.assertEqual(labels[("000001.SZ", "20260815")], "inst_netbuy")
            self.assertEqual(labels[("600519.SH", "20260815")], "no_listing")


if __name__ == "__main__":
    unittest.main()
