"""Offline tests for the stock-level own-margin state study."""

from __future__ import annotations

import csv
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from Ashare import event_margin_own_stock_study as oms


def _sessions(count: int, start: date = date(2026, 1, 5)) -> list[str]:
    out: list[str] = []
    cursor = start
    while len(out) < count:
        if cursor.weekday() < 5:
            out.append(cursor.strftime("%Y%m%d"))
        cursor += timedelta(days=1)
    return out


DAYS = _sessions(30)


def _write_day(cache: Path, day: str, rows: list[tuple[str, float]]) -> None:
    path = cache / "margin_detail_daily" / f"{day}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["trade_date", "ts_code", "rzye"])
        for code, rzye in rows:
            writer.writerow([day, code, rzye])


class LoadSymbolMarginSeriesTest(unittest.TestCase):
    def test_universe_filter_and_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            _write_day(cache, DAYS[0], [("600000.SH", 100.0), ("000001.SZ", 7.0)])
            _write_day(cache, DAYS[1], [("600000.SH", 101.0)])
            series = oms.load_symbol_margin_series(cache, {"600000.SH"})
            self.assertEqual(list(series.keys()), ["600000.SH"])
            self.assertEqual(series["600000.SH"], ([DAYS[0], DAYS[1]], [100.0, 101.0]))
            with self.assertRaisesRegex(
                oms.OwnMarginStudyError, "detail_dir_missing"
            ):
                oms.load_symbol_margin_series(cache / "nope", {"600000.SH"})


class AttachOwnMarginStatesTest(unittest.TestCase):
    def _series(self, values: list[float]) -> dict[str, tuple[list[str], list[float]]]:
        return {"600000.SH": (DAYS[: len(values)], values)}

    def test_strict_prior_reads_last_value_before_entry(self) -> None:
        # Flat 100 through index 19, then 110 at indices 20-21.  Entry sits on
        # DAYS[22]; the usable prior is index 21 vs lookback index 1 → +10%
        # (expansion).  A same-day spike (index 22) must be ignored.
        values = [100.0] * 20 + [110.0, 110.0, 99999.0]
        signals = [{"ts_code": "600000.SH", "entry_day": DAYS[22]}]
        stats = oms.attach_own_margin_states(signals, self._series(values))
        self.assertEqual(stats, {
            "missing_series": 0, "insufficient_history": 0, "attached": 1,
        })
        self.assertEqual(signals[0]["own_state"], "expansion")
        self.assertAlmostEqual(float(signals[0]["own_change"]), 0.10, places=9)

    def test_deleverage_bucket_edge(self) -> None:
        # Exactly −2% lands in neutral (edge inclusive), −2.5% in deleverage.
        values = [100.0] * 20 + [98.0, 97.5]
        signals = [{"ts_code": "600000.SH", "entry_day": DAYS[22]}]
        oms.attach_own_margin_states(signals, self._series(values))
        self.assertEqual(signals[0]["own_state"], "deleverage")

    def test_insufficient_history_and_missing_symbol_counted(self) -> None:
        short = self._series([100.0] * 10)
        signals = [
            {"ts_code": "600000.SH", "entry_day": DAYS[22]},
            {"ts_code": "000001.SZ", "entry_day": DAYS[22]},
        ]
        stats = oms.attach_own_margin_states(signals, short)
        self.assertEqual(stats["insufficient_history"], 1)
        self.assertEqual(stats["missing_series"], 1)
        self.assertEqual(signals[1]["own_state"], "insufficient_history")


if __name__ == "__main__":
    unittest.main()
