"""Offline tests for the multi-family paper baseline (lockup + earnings_neg)."""

from __future__ import annotations

import csv
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from Ashare import event_multifamily_baseline as mfb


def _write_csv(path: Path, fields: list[str], rows: list[list]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(fields)
        writer.writerows(rows)


def _weekday_sessions(count: int, start: date = date(2026, 1, 5)) -> list[str]:
    out: list[str] = []
    cursor = start
    while len(out) < count:
        if cursor.weekday() < 5:
            out.append(cursor.strftime("%Y%m%d"))
        cursor += timedelta(days=1)
    return out


INDEX_DAYS = _weekday_sessions(30)
INDEX_CLOSES = (
    [100.0] * 10
    + [97.0, 94.0, 91.0, 88.0, 85.0, 82.0]
    + [86.0, 90.0, 94.0, 98.0, 102.0, 106.0, 110.0, 114.0]
    + [116.0, 118.0, 120.0, 122.0, 124.0, 126.0]
)


class BuildDisclosureSignalsTest(unittest.TestCase):
    def _books(self) -> dict:
        # Flat at 50 until day 12 (the anchor weekend), then 55 from the
        # first session after it — proves entry lands AFTER the anchor.
        closes = [50.0] * len(INDEX_DAYS)
        anchor_pos = INDEX_DAYS.index(_weekday_sessions(15)[14])
        for i in range(anchor_pos, len(closes)):
            closes[i] = 55.0
        return {"600000.SH": type(
            "B", (), {"days": INDEX_DAYS, "closes": closes,
                      "mark": lambda self, d: None}
        )()}

    def test_weekend_anchor_rolls_forward_not_back(self) -> None:
        books = self._books()
        # Saturday between sessions: first session ON/AFTER is Monday.
        saturday = "20260124"
        assert date(2026, 1, 24).weekday() == 5
        events = [("600000.SH", saturday)]
        signals, stats = mfb.build_disclosure_signals(events, books, [], INDEX_DAYS[-1])
        self.assertEqual(len(signals), 1)
        # Entry price must be the post-anchor level (55), never Friday's 50.
        self.assertEqual(signals[0]["entry_price"], 55.0)
        self.assertEqual(signals[0]["entry_day"], "20260126")

    def test_exit_beyond_span_skipped(self) -> None:
        books = {
            "600000.SH": type(
                "B", (), {"days": INDEX_DAYS[:5],
                          "closes": [50.0] * 5, "mark": lambda self, d: None}
            )()
        }
        events = [("600000.SH", INDEX_DAYS[3])]
        signals, stats = mfb.build_disclosure_signals(events, books, [], INDEX_DAYS[-1])
        self.assertEqual(signals, [])
        self.assertEqual(stats["skipped_truncated"], 1)

    def test_missing_book_counted(self) -> None:
        events = [("999999.SH", INDEX_DAYS[5])]
        signals, stats = mfb.build_disclosure_signals(events, {}, [], INDEX_DAYS[-1])
        self.assertEqual(signals, [])
        self.assertEqual(stats["skipped_no_cache"], 1)


class EndToEndTest(unittest.TestCase):
    def test_study_runs_three_arms(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            _write_csv(cache / "index_000001SH.csv", ["trade_date", "close"],
                       [[d, c] for d, c in zip(INDEX_DAYS, INDEX_CLOSES)])
            _write_csv(cache / "daily_600000SH.csv", ["trade_date", "close"],
                       [[d, c] for d, c in zip(INDEX_DAYS, INDEX_CLOSES)])
            _write_csv(cache / "adjfactor_600000SH.csv",
                       ["trade_date", "adj_factor"],
                       [[d, 1.0] for d in INDEX_DAYS])
            _write_csv(cache / "share_float.csv",
                       ["ts_code", "ann_date", "float_date", "float_ratio"],
                       [["600000.SH", "20260101", INDEX_DAYS[14], "2.0"]])
            _write_csv(cache / "forecast.csv",
                       ["ts_code", "ann_date", "end_date", "type", "update_flag"],
                       [["600000.SH", "20260102", "20260331", "预减", ""]])
            _write_csv(cache / "disclosure.csv",
                       ["ts_code", "ann_date", "end_date", "pre_date", "actual_date"],
                       # pre_date BEFORE ann_date would be skipped; use a
                       # scheduled date after both forecast and announcement.
                       [["600000.SH", "20260105", "20260331",
                         INDEX_DAYS[20], INDEX_DAYS[20]]])
            results = mfb.run_study(cache, cost_bps=15.0)
            arms = results["arms"]
            assert isinstance(arms, dict)
            self.assertEqual(arms["earnings_neg_all"]["signals"], 1)
            self.assertEqual(arms["combined"]["signals"],
                             arms["lockup_rule"]["signals"]
                             + arms["earnings_neg_all"]["signals"])
            for name in ("lockup_rule", "earnings_neg_all", "combined"):
                row = arms[name]
                assert isinstance(row, dict) and "total_net_return" in row
                self.assertGreater(float(row["total_net_return"]), -1.0)


if __name__ == "__main__":
    unittest.main()
