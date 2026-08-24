"""Offline tests for the repurchase pre-lockup conditioning study."""

from __future__ import annotations

import csv
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from Ashare import event_repurchase_prelockup_study as study


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


def _idx(days: list[str], procs: list[str], code: str = CODE):
    return {code: (list(days), list(procs))}


def _signal(entry_day: str = ENTRY, code: str = CODE):
    return {"ts_code": code, "entry_day": entry_day}


class ClassifyPriorityTest(unittest.TestCase):
    def test_priority_active_over_all(self) -> None:
        # active wins even when a stopped record is also present (frozen D2)
        self.assertEqual(
            study.classify_repurchase(["完成", "停止", "预案"]), "active")
        self.assertEqual(
            study.classify_repurchase(["停止", "股东大会通过"]), "active")
        self.assertEqual(study.classify_repurchase(["实施"]), "active")

    def test_stopped_beats_done_and_done_only(self) -> None:
        self.assertEqual(study.classify_repurchase(["停止", "完成"]), "stopped")
        self.assertEqual(study.classify_repurchase(["完成", "完成"]), "done")
        self.assertEqual(study.classify_repurchase([]), "no_records")
        # Frozen D2 is EXHAUSTIVE (four buckets): an unknown proc state
        # cannot mint a fifth bucket.  A non-empty window with no active
        # and no stopped record lands in ``done`` by elimination.  The
        # live feed shows exactly five proc states, so this is purely
        # defensive.
        self.assertEqual(study.classify_repurchase(["其他状态"]), "done")


class WindowClassificationTest(unittest.TestCase):
    def test_direction_buckets_from_window(self) -> None:
        cases = [
            (_idx([INSIDE], ["实施"]), "active"),
            (_idx([INSIDE], ["停止"]), "stopped"),
            (_idx([INSIDE], ["完成"]), "done"),
            # two same-day announcements = duplicate day entries, exactly
            # how the row-parallel loader lays them out
            (_idx([INSIDE, INSIDE], ["完成", "预案"]), "active"),
        ]
        for book, want in cases:
            signals = [_signal()]
            stats = study.attach_repurchase_states(signals, book)
            self.assertEqual(signals[0]["repurchase_bucket"], want)
        signals = [_signal()]
        study.attach_repurchase_states(signals, {})
        self.assertEqual(signals[0]["repurchase_bucket"], "no_records")

    def test_window_edges_inclusive_start_exclusive_entry(self) -> None:
        self.assertEqual(ENTRY, "20260227")
        for ann, proc, want in (
            ("20260128", "预案", "active"),      # window start inclusive
            ("20260127", "预案", "no_records"),  # one day too early
            ("20260227", "预案", "no_records"),  # entry day excluded
        ):
            signals = [_signal()]
            study.attach_repurchase_states(
                signals, _idx([ann], [proc])
            )
            self.assertEqual(signals[0]["repurchase_bucket"], want, ann)

    def test_empty_window_is_no_records_label(self) -> None:
        signals = [_signal()]
        stats = study.attach_repurchase_states(
            signals, _idx(["20251201"], ["预案"])
        )
        self.assertEqual(stats["no_records"], 1)
        self.assertIsNone(signals[0]["repurchase_procs"])


class LoaderTest(unittest.TestCase):
    def _write_file(self, cache: Path, name: str,
                    rows: list[list[object]]) -> None:
        folder = cache / "repurchase_ann"
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / f"{name}.csv").open("w", newline="",
                                           encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["ts_code", "ann_date", "end_date", "proc",
                             "amount"])
            for row in rows:
                writer.writerow(row)

    def test_missing_folder_and_empty_cache_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(
                study.RepurchaseStudyError, "repurchase_cache_missing"
            ):
                study.load_repurchase_index(Path(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "repurchase_ann"
            folder.mkdir()
            (folder / "20260102.csv").write_text("", encoding="utf-8")
            with self.assertRaisesRegex(
                study.RepurchaseStudyError, "repurchase_cache_empty"
            ):
                study.load_repurchase_index(Path(tmp))

    def test_reads_files_skips_malformed_keeps_ascending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            self._write_file(cache, "20260201",
                             [[CODE, "20260201", "", "预案", 100.0]])
            self._write_file(cache, "20260205",
                             [["", "20260205", "", "完成", 5.0],   # no code
                              [CODE, "20260205", "", "完成", None]])
            index = study.load_repurchase_index(cache)
            days, procs = index[CODE]
            self.assertEqual(days, ["20260201", "20260205"])
            self.assertEqual(procs, ["预案", "完成"])
            # note: an in-row unparseable ann_date would fall back to the
            # file-stem day (holdertrade family behaviour); the fetcher
            # validates dates before writing, so that path stays
            # defensive-only and is not exercised here.

    def test_side_table_lookup_labels_every_pair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            self._write_file(cache, "20260201",
                             [[CODE, "20260201", "", "实施", 100.0]])
            buckets = study.repurchase_buckets_for_events(
                cache, [(CODE, ENTRY), (CODE, "20250105")]
            )
            self.assertEqual(buckets[(CODE, ENTRY)], "active")
            # far-outside pair still gets the no_records label, not omitted
            self.assertEqual(buckets[(CODE, "20250105")], "no_records")


if __name__ == "__main__":
    unittest.main()
