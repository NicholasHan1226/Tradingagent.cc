"""Offline tests for the dragon-tiger list conditioning layer study."""

from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from Ashare.event_toplist_prelockup_study import (
    TOPLIST_BUCKETS,
    ToplistStudyError,
    attach_toplist_states,
    classify_reason,
    load_toplist_index,
    toplist_buckets_for_events,
)


def _write_day(cache: Path, day: str, rows: list[dict]) -> None:
    folder = cache / "toplist_daily"
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / f"{day}.csv").open("w", newline="", encoding="utf-8") as h:
        writer = csv.DictWriter(h, fieldnames=["ts_code", "reason"])
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


class ClassifyReasonTest(unittest.TestCase):
    def test_frozen_keyword_mapping(self) -> None:
        self.assertEqual(classify_reason("日跌幅偏离值达到7%的前5只证券"), "sell_dev")
        self.assertEqual(classify_reason("连续三个交易日内，涨幅偏离值累计达到20%"), "rise_dev")
        self.assertEqual(classify_reason("日振幅值达到15%的前5只证券"), "other")
        self.assertEqual(classify_reason(""), "other")
        self.assertEqual(classify_reason(None), "other")


class LoadToplistIndexTest(unittest.TestCase):
    def test_empty_cache_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ToplistStudyError) as ctx:
                load_toplist_index(Path(tmp))
            self.assertIn("toplist_cache_missing", str(ctx.exception))

    def test_rows_indexed_by_symbol_with_ascending_days(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            _write_day(cache, "20260810", [{"ts_code": "000001.SZ", "reason": "跌幅偏离"}])
            _write_day(cache, "20260805", [
                {"ts_code": "000001.SZ", "reason": "涨幅偏离"},
                {"ts_code": "", "reason": "跌幅偏离"},  # skipped: no code
            ])
            index = load_toplist_index(cache)
            days, buckets = index["000001.SZ"]
            self.assertEqual(days, ["20260805", "20260810"])
            self.assertEqual(buckets, ["rise_dev", "sell_dev"])


class AttachToplistStatesTest(unittest.TestCase):
    def _index(self) -> dict:
        return {
            "000001.SZ": (
                ["20260801", "20260810", "20260815"],
                ["rise_dev", "other", "sell_dev"],
            ),
            "600519.SH": (
                ["20260701"],  # far outside every window below
                ["sell_dev"],
            ),
        }

    def test_precedence_and_window_edges(self) -> None:
        signals = [
            # mixed window: sell_dev beats rise_dev/other (frozen precedence)
            {"ts_code": "000001.SZ", "entry_day": "20260820"},
            # window start inclusive: 20260725 entry sees 20260801? no —
            # build a case where the oldest listing sits exactly at -30d
            {"ts_code": "000002.SZ", "entry_day": "20260831"},
            # strictly-prior rule: listing ON the entry day is excluded
            {"ts_code": "600519.SH", "entry_day": "20260701"},
        ]
        stats = attach_toplist_states(signals, self._index())
        self.assertEqual(signals[0]["toplist_bucket"], "sell_dev")
        self.assertEqual(signals[0]["toplist_hits"], 3)
        self.assertEqual(signals[0]["toplist_lag_days"], 5)
        self.assertEqual(stats["attached"], 3)

    def test_inclusive_start_and_exclusive_entry(self) -> None:
        index = {"000003.SZ": (["20260728", "20260827"], ["other", "other"])}
        signals = [{"ts_code": "000003.SZ", "entry_day": "20260827"}]
        attach_toplist_states(signals, index)
        # 20260728 is exactly entry-30d (inclusive); 20260827 is the
        # entry day itself (excluded)
        self.assertEqual(signals[0]["toplist_bucket"], "other")
        self.assertEqual(signals[0]["toplist_hits"], 1)

    def test_no_listing_is_a_label(self) -> None:
        signals = [
            {"ts_code": "999999.SZ", "entry_day": "20260820"},  # no file
            {"ts_code": "600519.SH", "entry_day": "20260915"},  # too old
        ]
        stats = attach_toplist_states(signals, self._index())
        for s in signals:
            self.assertEqual(s["toplist_bucket"], "no_listing")
            self.assertIsNone(s["toplist_lag_days"])
        self.assertEqual(stats["no_listing"], 2)
        self.assertEqual(set(TOPLIST_BUCKETS),
                         {"sell_dev", "rise_dev", "other", "no_listing"})

    def test_bad_entry_day_degrades_to_no_listing(self) -> None:
        signals = [{"ts_code": "000001.SZ", "entry_day": "not-a-day"}]
        stats = attach_toplist_states(signals, self._index())
        self.assertEqual(signals[0]["toplist_bucket"], "no_listing")
        self.assertEqual(stats["attached"], 1)


class SideTableTest(unittest.TestCase):
    def test_pairs_all_labelled_far_outside_is_no_listing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            _write_day(cache, "20260810", [{"ts_code": "000001.SZ", "reason": "跌幅偏离"}])
            labels = toplist_buckets_for_events(
                cache,
                [("000001.SZ", "20260815"), ("600519.SH", "20260815")],
            )
            self.assertEqual(labels[("000001.SZ", "20260815")], "sell_dev")
            self.assertEqual(labels[("600519.SH", "20260815")], "no_listing")


if __name__ == "__main__":
    unittest.main()
