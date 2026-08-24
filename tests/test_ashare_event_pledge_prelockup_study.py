"""Offline tests for the pledge pre-lockup conditioning study."""

from __future__ import annotations

import csv
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from Ashare import event_pledge_prelockup_study as study


def _sessions(count: int, start: date = date(2026, 1, 5)) -> list[str]:
    out: list[str] = []
    cursor = start
    while len(out) < count:
        if cursor.weekday() < 5:
            out.append(cursor.strftime("%Y%m%d"))
        cursor += timedelta(days=1)
    return out


DAYS = _sessions(40)
ENTRY = DAYS[39]  # 20260227; frozen window = ["20260128", "20260227")
CODE = "600000.SH"
INSIDE = "20260210"
FIELDS = ["ts_code", "end_date", "pledge_ratio"]


def _idx(days: list[str], ratios: list[float], code: str = CODE):
    return {code: (list(days), list(ratios))}


def _signal(entry_day: str = ENTRY, code: str = CODE):
    return {"ts_code": code, "entry_day": entry_day}


class ClassifyBoundariesTest(unittest.TestCase):
    def test_frozen_boundaries_percent_scale(self) -> None:
        self.assertEqual(study.classify_pledge(20.0), "high")   # inclusive
        self.assertEqual(study.classify_pledge(55.3), "high")
        self.assertEqual(study.classify_pledge(19.9), "mid")
        self.assertEqual(study.classify_pledge(5.0), "mid")     # inclusive
        self.assertEqual(study.classify_pledge(4.9), "low")
        self.assertEqual(study.classify_pledge(0.0), "low")     # released
        self.assertEqual(study.classify_pledge(None), "no_snapshot")


class WindowClassificationTest(unittest.TestCase):
    def test_latest_snapshot_classifies_not_window_max(self) -> None:
        # frozen D2: an older higher-ratio snapshot must not leak —
        # classification follows the LATEST end_date only.
        signals = [_signal()]
        study.attach_pledge_states(
            signals, _idx([INSIDE, "20260220"], [90.0, 6.0])
        )
        self.assertEqual(signals[0]["pledge_bucket"], "mid")
        self.assertEqual(signals[0]["pledge_ratio"], 6.0)
        self.assertEqual(signals[0]["pledge_lag_days"], 7)

    def test_latest_day_tie_resolves_to_max_ratio(self) -> None:
        signals = [_signal()]
        study.attach_pledge_states(
            signals, _idx(["20260220", "20260220"], [3.0, 25.0])
        )
        self.assertEqual(signals[0]["pledge_bucket"], "high")

    def test_window_edges_inclusive_start_exclusive_entry(self) -> None:
        self.assertEqual(ENTRY, "20260227")
        for ann, ratio, want in (
            ("20260128", 25.0, "high"),         # window start inclusive
            ("20260127", 25.0, "no_snapshot"),  # one day too early
            ("20260227", 25.0, "no_snapshot"),  # entry day excluded
        ):
            signals = [_signal()]
            study.attach_pledge_states(signals, _idx([ann], [ratio]))
            self.assertEqual(signals[0]["pledge_bucket"], want, ann)

    def test_empty_window_is_no_snapshot_label(self) -> None:
        signals = [_signal()]
        stats = study.attach_pledge_states(
            signals, _idx(["20251201"], [30.0])
        )
        self.assertEqual(stats["no_snapshot"], 1)
        self.assertIsNone(signals[0]["pledge_ratio"])
        self.assertIsNone(signals[0]["pledge_lag_days"])


class LoaderTest(unittest.TestCase):
    def _write_symbol(self, cache: Path, stem: str,
                      rows: list[list[object]]) -> None:
        with (cache / f"pledgestat_{stem}.csv").open(
                "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(FIELDS + ["extra"])
            writer.writerows(rows)

    def test_zero_files_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(
                study.PledgeStudyError, "pledge_cache_missing"
            ):
                study.load_pledge_index(Path(tmp))

    def test_reads_files_skips_malformed_keeps_ascending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            self._write_symbol(cache, "600000SH", [
                ["600000.SH", "20260205", 12.0, "x"],
                ["600000.SH", "20260201", 30.0, "x"],
            ])
            self._write_symbol(cache, "000001SZ", [
                ["", "20260201", 10.0],            # no code -> skipped
                ["000001.SZ", "20260bad", 10.0],   # bad date -> skipped
                ["000001.SZ", "20260203", "n/a"],  # bad ratio -> skipped
                ["000001.SZ", "20260203", 8.0],
            ])
            index = study.load_pledge_index(cache)
            self.assertEqual(index["600000.SH"],
                             (["20260201", "20260205"], [30.0, 12.0]))
            self.assertEqual(index["000001.SZ"], (["20260203"], [8.0]))

    def test_side_table_lookup_labels_every_pair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            self._write_symbol(cache, "600000SH",
                               [["600000.SH", "20260201", 22.0]])
            buckets = study.pledge_buckets_for_events(
                cache, [(CODE, ENTRY), (CODE, "20250105")]
            )
            self.assertEqual(buckets[(CODE, ENTRY)], "high")
            # far-outside pair still gets the no_snapshot label, not omitted
            self.assertEqual(buckets[(CODE, "20250105")], "no_snapshot")


if __name__ == "__main__":
    unittest.main()
