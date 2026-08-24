"""Offline tests for the pre-unlock price-structure (r63) study."""

from __future__ import annotations

import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from Ashare.event_paper_baseline_sim import StockBook
from Ashare.event_pricestruct_runup_study import (
    PriceStructureStudyError,
    _double_low,
    _load_books,
    _r63_label,
    attach_runup_bucket,
)


def _days(count: int, start: str = "20200101") -> list[str]:
    cursor = date(int(start[:4]), int(start[4:6]), int(start[6:8]))
    out = []
    for _ in range(count):
        out.append(cursor.strftime("%Y%m%d"))
        cursor += timedelta(days=1)
    return out


def _signal(code: str, entry_day: str) -> dict:
    return {"ts_code": code, "entry_day": entry_day}


class R63LabelTest(unittest.TestCase):
    def test_arithmetic_exact_runup_pos(self) -> None:
        closes = [100.0] * 63 + [110.0]
        book = StockBook(_days(64), closes)
        bucket, value = _r63_label(book, "20200405")
        self.assertEqual(bucket, "runup_pos")
        self.assertAlmostEqual(value, 0.10)

    def test_entry_day_close_is_excluded(self) -> None:
        base_closes = [100.0] * 64
        book = StockBook(_days(65), base_closes)
        # Entry day IS the 65th session; its own close must never enter
        # the window or the previous-close slot.
        bucket, value = _r63_label(book, _days(65)[64])
        self.assertEqual(bucket, "drift_down")
        self.assertAlmostEqual(value, 0.0)

    def test_zero_return_is_drift_down_strict_positive(self) -> None:
        book = StockBook(_days(64), [55.0] * 64)
        bucket, value = _r63_label(book, "20200405")
        self.assertEqual(bucket, "drift_down")
        self.assertAlmostEqual(value, 0.0)

    def test_deep_dd_line_is_inclusive(self) -> None:
        # 0.75 is exactly representable in binary, so the comparison against
        # the fixed -20% line is deterministic (an "exact -20%" close would
        # be float-fragile: 80/100-1 == -0.19999...96).
        closes = [100.0] * 63 + [75.0]  # -25%
        book = StockBook(_days(64), closes)
        bucket, value = _r63_label(book, "20200405")
        self.assertEqual(bucket, "deep_dd")
        self.assertAlmostEqual(value, -0.25)

    def test_just_above_deep_line_is_drift_down(self) -> None:
        closes = [100.0] * 63 + [81.0]  # -19%
        book = StockBook(_days(64), closes)
        bucket, _ = _r63_label(book, "20200405")
        self.assertEqual(bucket, "drift_down")

    def test_short_history_when_fewer_than_64_sessions(self) -> None:
        book = StockBook(_days(63), [100.0] * 63)
        bucket, value = _r63_label(book, "20200404")
        self.assertEqual(bucket, "short_history")
        self.assertIsNone(value)


class AttachRunupBucketTest(unittest.TestCase):
    def test_missing_symbol_counts_short_history(self) -> None:
        signals = [_signal("000001.SZ", "20210101")]
        stats = attach_runup_bucket(signals, {})
        self.assertEqual(signals[0]["runup_bucket"], "short_history")
        self.assertIsNone(signals[0]["r63_value"])
        self.assertEqual(stats["short_history"], 1)
        self.assertEqual(stats["attached"], 1)

    def test_attach_annotates_and_counts(self) -> None:
        up_book = StockBook(_days(64), [100.0] * 63 + [120.0])
        dd_book = StockBook(_days(64), [100.0] * 63 + [70.0])
        signals = [
            _signal("000001.SZ", _days(65)[64]),
            _signal("000002.SZ", _days(65)[64]),
            _signal("000003.SZ", "20190101"),  # not in any book
        ]
        stats = attach_runup_bucket(signals, {
            "000001.SZ": up_book,
            "000002.SZ": dd_book,
        })
        self.assertEqual(signals[0]["runup_bucket"], "runup_pos")
        self.assertAlmostEqual(signals[0]["r63_value"], 0.20)
        self.assertEqual(signals[1]["runup_bucket"], "deep_dd")
        self.assertEqual(signals[2]["runup_bucket"], "short_history")
        self.assertEqual(stats["runup_pos"], 1)
        self.assertEqual(stats["deep_dd"], 1)
        self.assertEqual(stats["short_history"], 1)
        self.assertEqual(stats["drift_down"], 0)


class DoubleLowGateTest(unittest.TestCase):
    def test_both_legs_lower_passes_at_n_threshold(self) -> None:
        cell = {"n": 30, "mean_net_bps": -50.0, "win_rate": 0.40}
        baseline = {"n": 300, "mean_net_bps": 100.0, "win_rate": 0.55}
        self.assertTrue(_double_low(cell, baseline))

    def test_below_n_threshold_fails(self) -> None:
        cell = {"n": 29, "mean_net_bps": -50.0, "win_rate": 0.40}
        baseline = {"n": 300, "mean_net_bps": 100.0, "win_rate": 0.55}
        self.assertFalse(_double_low(cell, baseline))

    def test_contradictory_legs_fail(self) -> None:
        baseline = {"n": 300, "mean_net_bps": 100.0, "win_rate": 0.55}
        mean_only = {"n": 30, "mean_net_bps": 50.0, "win_rate": 0.60}
        win_only = {"n": 30, "mean_net_bps": 150.0, "win_rate": 0.50}
        self.assertFalse(_double_low(mean_only, baseline))
        self.assertFalse(_double_low(win_only, baseline))


class LoadBooksFailClosedTest(unittest.TestCase):
    def test_empty_cache_raises_module_error(self) -> None:
        with self.assertRaises(PriceStructureStudyError) as ctx:
            _load_books(Path(tempfile.mkdtemp()))
        self.assertEqual(str(ctx.exception), "stock_books_empty")


if __name__ == "__main__":
    unittest.main()
