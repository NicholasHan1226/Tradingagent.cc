"""Offline tests for the pre-lockup shareholder-count conditioning study."""

from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from Ashare.event_holdernum_prelockup_study import (
    HOLDERNUM_BUCKETS,
    HolderNumStudyError,
    _window_bucket,
    attach_holdernum_states,
    load_holdernum_index,
    holdernum_buckets_for_events,
)


def _write_symbol(cache: Path, stem: str, rows: list[dict]) -> None:
    with (cache / f"holdernum_{stem}.csv").open(
        "w", newline="", encoding="utf-8"
    ) as h:
        writer = csv.DictWriter(
            h, fieldnames=["ts_code", "ann_date", "end_date", "holder_num"]
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


class LoadHoldernumIndexTest(unittest.TestCase):
    def test_empty_cache_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(HolderNumStudyError) as ctx:
                load_holdernum_index(Path(tmp))
            self.assertIn("holdernum_cache_missing", str(ctx.exception))

    def test_null_rows_skipped_and_announcements_deduped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            _write_symbol(cache, "000001SZ", [
                # legal multi-period disclosure: same ann_date, two periods;
                # dedup keeps latest end_date (20260228)
                {"ts_code": "000001.SZ", "ann_date": "20260321",
                 "end_date": "20251231", "holder_num": "441142"},
                {"ts_code": "000001.SZ", "ann_date": "20260321",
                 "end_date": "20260228", "holder_num": "462824"},
                # empty holder_num row: not a snapshot at all
                {"ts_code": "000001.SZ", "ann_date": "20260401",
                 "end_date": "20260331", "holder_num": ""},
                # earlier normal snapshot
                {"ts_code": "000001.SZ", "ann_date": "20251220",
                 "end_date": "20251130", "holder_num": "500000"},
            ])
            index = load_holdernum_index(cache)
            rows = index["000001.SZ"]
            # one row per announcement; same-day pair collapsed keeping
            # the later end_date; null row dropped entirely
            self.assertEqual(
                [(r[0], r[1], r[2]) for r in rows],
                [("20251220", "20251130", 500000.0),
                 ("20260321", "20260228", 462824.0)],
            )


class WindowBucketTest(unittest.TestCase):
    def _book(self) -> list[tuple[str, str, float]]:
        return [
            ("20250701", "20250630", 1_000_000.0),
            ("20251001", "20250930", 900_000.0),   # −10% → contract zone
            ("20260101", "20251231", 880_000.0),   # −2.2% step
        ]

    def test_contract_when_change_drops_past_boundary(self) -> None:
        label, change = _window_bucket(self._book(), "20260315")
        # anchor 880k vs preceding announcement 900k → −2.2% stable zone
        self.assertEqual(label, "stable")
        self.assertAlmostEqual(change, -2.2222, places=3)
        # deeper drop across announcements lands contract
        book_deep = [
            ("20250701", "20250630", 1_000_000.0),
            ("20251001", "20250930", 1_200_000.0),
            ("20260101", "20251231", 850_000.0),
        ]
        label, change = _window_bucket(book_deep, "20260315")
        self.assertEqual(label, "contract")
        self.assertAlmostEqual(change, -29.1667, places=3)

    def test_unsorted_book_handled_defensively(self) -> None:
        shuffled = list(reversed(self._book()))
        self.assertEqual(
            _window_bucket(shuffled, "20260315"),
            _window_bucket(self._book(), "20260315"),
        )

    def test_expand_and_stable_boundaries_are_inclusive(self) -> None:
        # exactly +5% must label expand (>= boundary)
        book_up = [
            ("20250701", "20250630", 100_000.0),
            ("20260101", "20251231", 105_000.0),
        ]
        label, change = _window_bucket(book_up, "20260315")
        self.assertEqual(label, "expand")
        self.assertAlmostEqual(change, 5.0)
        # exactly −5% must label contract (<= boundary)
        book_down = [
            ("20250701", "20250630", 100_000.0),
            ("20260101", "20251231", 95_000.0),
        ]
        self.assertEqual(_window_bucket(book_down, "20260315")[0], "contract")
        # between boundaries → stable
        book_flat = [
            ("20250701", "20250630", 100_000.0),
            ("20260101", "20251231", 103_000.0),
        ]
        self.assertEqual(_window_bucket(book_flat, "20260315")[0], "stable")

    def test_multi_period_same_day_anchor_takes_latest_end_date(self) -> None:
        book = [
            ("20250915", "20250831", 400_000.0),
            # one announcement disclosing two periods; later period wins
            ("20260321", "20251231", 360_000.0),
            ("20260321", "20260228", 380_000.0),
        ]
        label, change = _window_bucket(book, "20260415")
        # anchor = 20260228 count 380k vs prev snapshot 400k → −5%
        self.assertEqual(label, "contract")
        self.assertAlmostEqual(change, -5.0)

    def test_fewer_than_two_snapshots_is_none(self) -> None:
        book = [("20260101", "20251231", 500_000.0)]
        self.assertEqual(_window_bucket(book, "20260315"), (None, None))
        self.assertEqual(_window_bucket(None, "20260315"), (None, None))

    def test_window_edges_inclusive_start_exclusive_entry(self) -> None:
        # anchor announced ON entry day excluded; comparison exactly at
        # entry−365d included
        ed = "20260815"
        ws = "20250815"
        book = [
            (ws, "20250731", 200_000.0),
            ("20260815", "20260731", 210_000.0),  # on entry day: excluded
            ("20260814", "20260731", 180_000.0),  # anchor inside window
        ]
        label, change = _window_bucket(book, ed)
        self.assertEqual(label, "contract")
        self.assertAlmostEqual(change, -10.0)

    def test_non_positive_comparison_row_is_none(self) -> None:
        book = [
            ("20250701", "20250630", 0.0),
            ("20260101", "20251231", 500_000.0),
        ]
        self.assertEqual(_window_bucket(book, "20260315"), (None, None))

    def test_bad_entry_day_degrades_to_none(self) -> None:
        self.assertEqual(
            _window_bucket(self._book(), "not-a-day"), (None, None)
        )


class AttachAndSideTableTest(unittest.TestCase):
    def test_attach_labels_and_no_snapshot_defaults(self) -> None:
        index = {"000001.SZ": [
            ("20250701", "20250630", 1_000_000.0),
            ("20251001", "20250930", 850_000.0),  # −15% contract
        ]}
        signals = [
            {"ts_code": "000001.SZ", "entry_day": "20260115"},
            {"ts_code": "999999.SZ", "entry_day": "20260115"},  # no file
        ]
        stats = attach_holdernum_states(signals, index)
        self.assertEqual(signals[0]["holdernum_bucket"], "contract")
        self.assertEqual(signals[0]["holdernum_anchor_lag_days"], 106)
        self.assertEqual(signals[1]["holdernum_bucket"], "no_snapshot")
        self.assertIsNone(signals[1]["holdernum_change_pct"])
        self.assertEqual(stats["contract"], 1)
        self.assertEqual(stats["no_snapshot"], 1)
        self.assertEqual(
            set(HOLDERNUM_BUCKETS),
            {"contract", "stable", "expand", "no_snapshot"},
        )

    def test_side_table_pairs_labelled_far_outside_is_no_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            _write_symbol(cache, "600519SH", [
                {"ts_code": "600519.SH", "ann_date": "20260801",
                 "end_date": "20260731", "holder_num": "100000"},
                {"ts_code": "600519.SH", "ann_date": "20260805",
                 "end_date": "20260731", "holder_num": "120000"},
            ])
            labels = holdernum_buckets_for_events(
                cache,
                [("600519.SH", "20260815"), ("000001.SZ", "20260815")],
            )
            self.assertEqual(labels[("600519.SH", "20260815")], "expand")
            self.assertEqual(labels[("000001.SZ", "20260815")], "no_snapshot")


if __name__ == "__main__":
    unittest.main()
