"""Offline tests for the macro release-window conditioning study."""

from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from Ashare.event_macro_release_window_study import (
    MACRO_BUCKETS,
    MacroStudyError,
    _add_month,
    _double_high,
    attach_macro_states,
    label_for_entry,
    presumed_release_days,
)


def _write_macro(cache: Path, stem: str, fields: list[str],
                 rows: list[list]) -> None:
    with (cache / f"macro_{stem}.csv").open(
        "w", newline="", encoding="utf-8"
    ) as handle:
        writer = csv.writer(handle)
        writer.writerow(fields)
        writer.writerows(rows)


def _full_cache(cache: Path) -> None:
    _write_macro(cache, "gdp",
                 ["quarter", "gdp", "gdp_yoy"],
                 [["2025Q4", 1.0, 4.0], ["2026Q1", 1.0, 4.0]])
    _write_macro(cache, "cpi",
                 ["month", "nt_val", "nt_yoy"],
                 [["202512", 100.1, 0.1], ["202601", "", ""]])  # placeholder
    _write_macro(cache, "ppi", ["month", "ppi_yoy"], [["202512", 3.0]])
    _write_macro(cache, "money", ["month", "m2_yoy"], [["202512", 7.0]])


class AddMonthTest(unittest.TestCase):
    def test_year_rollover(self) -> None:
        self.assertEqual(_add_month("202612", 1), "202701")
        self.assertEqual(_add_month("202501", -1), "202412")
        self.assertEqual(_add_month("202607", 1), "202608")


class PresumedReleaseDaysTest(unittest.TestCase):
    def test_union_shift_and_placeholder_qa(self) -> None:
        # dense synthetic calendar covering every presumed day's neighbourhood
        dense = {f"202601{d:02d}" for d in range(1, 29)} | {
            f"202602{d:02d}" for d in range(1, 28)} | {
            f"202603{d:02d}" for d in range(1, 32)} | {
            f"202604{d:02d}" for d in range(1, 31)}
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            _full_cache(cache)
            release, placeholders = presumed_release_days(cache, dense)
            self.assertEqual(release["20260109"], {"cpi", "ppi"})  # union same day
            self.assertEqual(release["20260111"], {"money"})
            self.assertEqual(release["20260117"], {"gdp"})
            # placeholder month key still contributes its presumed day
            self.assertEqual(release["20260209"], {"cpi"})
            self.assertEqual(placeholders, {"gdp": 0, "cpi": 1,
                                            "ppi": 0, "money": 0})

    def test_off_calendar_shifts_forward(self) -> None:
        sparse = {"20260112", "20260113", "20260117"}
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            _write_macro(cache, "gdp", ["quarter", "gdp"],
                         [["2025Q4", 1.0]])
            _write_macro(cache, "cpi", ["month", "nt_val"], [["202512", 1.0]])
            _write_macro(cache, "ppi", ["month", "ppi_yoy"], [["202512", 1.0]])
            _write_macro(cache, "money", ["month", "m2_yoy"],
                         [["202512", 1.0]])
            release, _ph = presumed_release_days(cache, sparse)
            # 20260109/11 land off this synthetic calendar -> next td 01-12
            self.assertEqual(set(release), {"20260112", "20260117"})

    def test_out_of_span_periods_skipped_silently(self) -> None:
        # caches reach back decades before the calendar: deep-history keys
        # must be skipped (not error), matching the frozen coverage method
        sparse = {"20260112", "20260113", "20260117"}
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            _write_macro(cache, "gdp", ["quarter", "gdp"],
                         [["2017Q3", 1.0], ["2025Q4", 1.0]])  # one out-of-span
            _write_macro(cache, "cpi", ["month", "nt_val"], [["201506", 1.0]])
            _write_macro(cache, "ppi", ["month", "ppi_yoy"], [["202512", 1.0]])
            _write_macro(cache, "money", ["month", "m2_yoy"],
                         [["202512", 1.0]])
            release, _ph = presumed_release_days(
                cache, sparse, span_start="20260101", span_end="20260131")
            self.assertEqual(set(release), {"20260112", "20260117"})

    def test_missing_file_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            _write_macro(cache, "cpi", ["month", "nt_val"], [["202512", 1.0]])
            with self.assertRaisesRegex(MacroStudyError,
                                        "macro_cache_missing:gdp"):
                presumed_release_days(cache, {"20260109"})


class LabelForEntryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.days = ["20260108", "20260109", "20260110",
                     "20260111", "20260112"]
        self.pos_of = {d: i for i, d in enumerate(self.days)}

    def test_four_buckets(self) -> None:
        f = label_for_entry
        self.assertEqual(f("20260111", {"20260112"},
                           self.days, self.pos_of), "ante")
        self.assertEqual(f("20260112", {"20260112"},
                           self.days, self.pos_of), "same_day")
        self.assertEqual(f("20260110", {"20260109"},
                           self.days, self.pos_of), "post")
        self.assertEqual(f("20260110", {"20260102"},
                           self.days, self.pos_of), "outside")

    def test_overlap_precedence_ante_wins(self) -> None:
        self.assertEqual(
            label_for_entry("20260111", {"20260110", "20260112"},
                            self.days, self.pos_of),
            "ante",
        )


class AttachStatesTest(unittest.TestCase):
    def test_unique_labels_and_stats(self) -> None:
        days = ["20260108", "20260109", "20260110", "20260111",
                "20260112", "20260113", "20260114"]
        pos_of = {d: i for i, d in enumerate(days)}
        signals = [{"entry_day": "20260111"},   # ante vs release 01-12
                   {"entry_day": "20260112"},   # same_day
                   {"entry_day": "20260110"},   # post vs release 01-09
                   {"entry_day": "20260114"}]   # outside (quiet tail)
        stats = attach_macro_states(signals, {"20260109": {"cpi"},
                                              "20260112": {"m2"}},
                                    days, pos_of)
        self.assertEqual([s["macro_bucket"] for s in signals],
                         ["ante", "same_day", "post", "outside"])
        self.assertEqual(stats["attached"], 4)
        for bucket in MACRO_BUCKETS:
            self.assertEqual(stats[bucket], 1)


class DoubleHighTest(unittest.TestCase):
    def test_gate_requires_n_mean_and_win(self) -> None:
        baseline = {"n": 357, "mean_net_bps": 100.0, "win_rate": 0.55}
        good = {"n": 37, "mean_net_bps": 120.0, "win_rate": 0.60}
        low_n = {**good, "n": 29}
        win_tie = {**good, "win_rate": 0.55}
        mean_low = {**good, "mean_net_bps": 90.0}
        self.assertTrue(_double_high(good, baseline))
        self.assertFalse(_double_high(low_n, baseline))
        self.assertFalse(_double_high(win_tie, baseline))
        self.assertFalse(_double_high(mean_low, baseline))


if __name__ == "__main__":
    unittest.main()
