"""Offline tests for the order-flow absorption study."""

from __future__ import annotations

import csv
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from Ashare import event_moneyflow_absorption_study as abs_study


def _sessions(count: int, start: date = date(2026, 1, 5)) -> list[str]:
    out: list[str] = []
    cursor = start
    while len(out) < count:
        if cursor.weekday() < 5:
            out.append(cursor.strftime("%Y%m%d"))
        cursor += timedelta(days=1)
    return out


DAYS = _sessions(30)
FIELDS = ["trade_date", "ts_code"] + [
    "buy_sm_amount", "sell_sm_amount", "buy_md_amount", "sell_md_amount",
    "buy_lg_amount", "sell_lg_amount", "buy_elg_amount", "sell_elg_amount",
]


class ClassifyAbsorptionTest(unittest.TestCase):
    def test_edges_inclusive(self) -> None:
        self.assertEqual(abs_study.classify_absorption(-0.10), "outflow")
        self.assertEqual(abs_study.classify_absorption(-0.0999), "balanced")
        self.assertEqual(abs_study.classify_absorption(0.0), "balanced")
        self.assertEqual(abs_study.classify_absorption(0.10), "inflow")
        self.assertEqual(abs_study.classify_absorption(99.0), "inflow")
        self.assertEqual(abs_study.classify_absorption(-99.0), "outflow")


class LoadSymbolMoneyflowTest(unittest.TestCase):
    def _row(self, day: str, code: str, lg_buy: float) -> list:
        return [day, code] + [100.0] * 4 + [lg_buy, 50.0, 0.0, 50.0]

    def test_universe_filter_and_malformed_rows_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            out_dir = cache / "moneyflow_daily"
            out_dir.mkdir(parents=True)
            for i, day in enumerate(DAYS[:3]):
                path = out_dir / f"{day}.csv"
                with path.open("w", newline="", encoding="utf-8") as h:
                    w = csv.writer(h)
                    w.writerow(FIELDS)
                    w.writerow(self._row(day, "600000.SH", 10.0 + i))
                    # malformed numeric and missing fields must not crash
                    w.writerow([day, "000001.SZ"] + ["n/a"] * 8)
                    w.writerow([day, "600000.SH"])
            series = abs_study.load_symbol_moneyflow(cache, {"600000.SH"})
            self.assertEqual(list(series.keys()), ["600000.SH"])
            days, nets, totals = series["600000.SH"]
            self.assertEqual(days, DAYS[:3])
            # net_lg = buy_lg+buy_elg - sell_lg-sell_elg = (10+i)+0-50-50
            self.assertAlmostEqual(nets[0], -90.0)
            self.assertEqual(len(totals), 3)
            with self.assertRaisesRegex(
                abs_study.AbsorptionStudyError, "moneyflow_dir_missing"
            ):
                abs_study.load_symbol_moneyflow(cache / "nope", {"600000.SH"})


class AttachAbsorptionTest(unittest.TestCase):
    def _series(self, nets: list[float], totals: list[float]) -> dict:
        return {"600000.SH": (DAYS[: len(nets)], nets, totals)}

    def test_strict_prior_window_math_and_entry_day_excluded(self) -> None:
        # 25 sessions of data; entry sits on index 24.  Pre-window = indices
        # 19..23 (sum 500), trail = indices 4..23 (avg total 1000) → +0.50.
        nets = [100.0] * 25
        totals = [1000.0] * 25
        signals = [{"ts_code": "600000.SH", "entry_day": DAYS[24]}]
        stats = abs_study.attach_absorption(signals, self._series(nets, totals))
        self.assertEqual(stats["attached"], 1)
        self.assertAlmostEqual(float(signals[0]["absorption_ratio"]), 0.50)
        self.assertEqual(signals[0]["absorption_bucket"], "inflow")

    def test_insufficient_history_and_zero_turnover_counted(self) -> None:
        signals = [
            {"ts_code": "600000.SH", "entry_day": DAYS[29]},  # only 10 sessions
            {"ts_code": "000001.SZ", "entry_day": DAYS[29]},  # no series
        ]
        short = self._series([100.0] * 10, [1000.0] * 10)
        stats = abs_study.attach_absorption(signals, short)
        self.assertEqual(stats["insufficient_history"], 1)
        self.assertEqual(stats["missing_series"], 1)

        zero_turnover = self._series([100.0] * 25, [0.0] * 25)
        solo = [{"ts_code": "600000.SH", "entry_day": DAYS[24]}]
        stats2 = abs_study.attach_absorption(solo, zero_turnover)
        self.assertEqual(stats2["insufficient_history"], 1)


if __name__ == "__main__":
    unittest.main()
