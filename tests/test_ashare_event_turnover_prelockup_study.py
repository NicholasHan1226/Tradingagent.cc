"""Offline tests for the pre-lockup turnover study."""

from __future__ import annotations

import csv
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from Ashare import event_turnover_prelockup_study as study


def _sessions(count: int, start: date = date(2026, 1, 5)) -> list[str]:
    out: list[str] = []
    cursor = start
    while len(out) < count:
        if cursor.weekday() < 5:
            out.append(cursor.strftime("%Y%m%d"))
        cursor += timedelta(days=1)
    return out


DAYS = _sessions(30)
CODE = "600000.SH"


def _write_dailybasic(cache: Path, code: str, days: list[str],
                      values: list[float]) -> None:
    stem = f"dailybasic_{code[:6]}{code[7:]}"
    with (cache / f"{stem}.csv").open("w", newline="",
                                      encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["ts_code", "trade_date", "close",
                         "turnover_rate", "turnover_rate_f",
                         "float_share", "total_share"])
        for d, v in zip(days, values):
            writer.writerow([code, d, 10.0, v, v, 800.0, 1000.0])


def _step_series(low: float = 1.0, high: float = 3.0) -> list[float]:
    """30 sessions: first 20 at ``low``, last 10 at ``high``."""
    return [low] * 20 + [high] * 10


class ClassifyTurnoverStateTest(unittest.TestCase):
    def test_fixed_edges_inclusive(self) -> None:
        self.assertEqual(study.classify_turnover_state(0.5), "shrink")
        self.assertEqual(study.classify_turnover_state(0.7), "shrink")
        self.assertEqual(study.classify_turnover_state(0.71), "normal")
        self.assertEqual(study.classify_turnover_state(1.49), "normal")
        self.assertEqual(study.classify_turnover_state(1.5), "surge")
        self.assertEqual(study.classify_turnover_state(2.0), "surge")


class TurnoverSeriesTest(unittest.TestCase):
    def test_universe_filter_malformed_skip_and_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            _write_dailybasic(cache, CODE, DAYS[:3], [1.0, "", 2.0])
            _write_dailybasic(cache, "000001.SZ", DAYS[:3], [1.0, 1.0, 1.0])
            series = study._turnover_series(cache, {CODE})
            # only the requested symbol is loaded; its malformed row skipped
            self.assertEqual(list(series.keys()), [CODE])
            days, values = series[CODE]
            self.assertEqual(days, [DAYS[0], DAYS[2]])
            self.assertEqual(values, [1.0, 2.0])
            with self.assertRaisesRegex(
                study.TurnoverStudyError, "dailybasic_cache_missing"
            ):
                study._turnover_series(cache / "e", {CODE})

    def test_newest_first_file_is_normalized_ascending(self) -> None:
        # Tushare delivers newest-first; a descending file must still yield
        # ascending session order (the #437 bisect lesson, guarded here).
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            _write_dailybasic(cache, CODE, list(reversed(DAYS[:25])),
                              [float(v) for v in range(25, 0, -1)])
            series = study._turnover_series(cache, {CODE})
            days, values = series[CODE]
            self.assertEqual(days, DAYS[:25])
            self.assertEqual(values, [float(i) for i in range(1, 26)])

    def test_turnover_rate_f_empty_falls_back_to_rate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            path = cache / f"dailybasic_{CODE[:6]}{CODE[7:]}.csv"
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(["trade_date", "turnover_rate_f",
                                 "turnover_rate"])
                writer.writerow([DAYS[0], "", 2.5])
                writer.writerow([DAYS[1], 3.0, 9.9])
            series = study._turnover_series(cache, {CODE})
            self.assertEqual(series[CODE], (DAYS[:2], [2.5, 3.0]))


class AttachTurnoverStatesTest(unittest.TestCase):
    def test_ratio_math_buckets_and_insufficient_paths(self) -> None:
        # Fixture chosen for EXACT ratios: flat 1.0 for 25 sessions then
        # 6.0 for the last 5.  entry DAYS[29]: window (6*1+4*6)/10 = 3.0,
        # baseline (16*1+4*6)/20 = 2.0 -> ratio exactly 1.5 (edge incl.);
        # entry DAYS[26]: window (9*1+1*6)/10 = 1.5, baseline (19*1+6)/20
        # = 1.25 -> ratio exactly 1.2 (normal).
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            _write_dailybasic(cache, CODE, DAYS, [1.0] * 25 + [6.0] * 5)
            _write_dailybasic(cache, "000002.SZ", DAYS, [1.0] * 30)
            _write_dailybasic(cache, "000003.SZ", DAYS, [0.0] * 30)
            signals = [
                {"ts_code": CODE, "entry_day": DAYS[29]},   # 1.5 -> surge
                {"ts_code": CODE, "entry_day": DAYS[26]},   # 1.2 -> normal
                {"ts_code": CODE, "entry_day": DAYS[5]},    # short history
                {"ts_code": "000002.SZ", "entry_day": DAYS[24]},  # 1.0
                {"ts_code": "000004.SZ", "entry_day": DAYS[24]},  # no file
            ]
            stats = study.attach_turnover_states(
                signals,
                study._turnover_series(
                    cache, {CODE, "000002.SZ", "000003.SZ"}
                ),
            )
            self.assertAlmostEqual(float(signals[0]["turnover_ratio"]), 1.5)
            self.assertEqual(signals[0]["turnover_bucket"], "surge")
            self.assertAlmostEqual(float(signals[1]["turnover_ratio"]), 1.2)
            self.assertEqual(signals[1]["turnover_bucket"], "normal")
            self.assertEqual(signals[2].get("turnover_bucket"), None)
            self.assertEqual(signals[3]["turnover_bucket"], "normal")
            self.assertEqual(stats["insufficient_history"], 1)
            self.assertEqual(stats["missing_dailybasic"], 1)
            self.assertEqual(stats["attached"], 3)

    def test_shrink_bucket_and_flat_baseline_counted(self) -> None:
        # Shrink fixture: hot tape 10.0 x15 cooling to 1.0 x15.  entry
        # DAYS[29]: window ten 1.0 = 1.0; baseline (6*10+14*1)/20 = 3.7 ->
        # ratio 10/37 ~ 0.27 (well under the 0.7 edge).
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            _write_dailybasic(cache, CODE, DAYS, [10.0] * 15 + [1.0] * 15)
            series = study._turnover_series(cache, {CODE})
            signals = [{"ts_code": CODE, "entry_day": DAYS[29]}]
            stats = study.attach_turnover_states(signals, series)
            self.assertAlmostEqual(float(signals[0]["turnover_ratio"]),
                                   10.0 / 37.0)
            self.assertEqual(signals[0]["turnover_bucket"], "shrink")
            self.assertEqual(stats["attached"], 1)
            # All-zero history counts as a flat baseline, never labeled.
            with tempfile.TemporaryDirectory() as tmp2:
                cache2 = Path(tmp2)
                _write_dailybasic(cache2, CODE, DAYS, [0.0] * 30)
                solo = [{"ts_code": CODE, "entry_day": DAYS[24]}]
                stats2 = study.attach_turnover_states(
                    solo, study._turnover_series(cache2, {CODE}))
                self.assertEqual(stats2["flat_baseline"], 1)


class BucketsForEventsTest(unittest.TestCase):
    def test_lookup_rolls_anchor_and_omits_unlabeled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            _write_dailybasic(cache, CODE, DAYS, _step_series())
            _write_dailybasic(cache, "000002.SZ", DAYS, [1.0] * 30)
            buckets = study.turnover_buckets_for_events(cache, [
                (CODE, DAYS[24]),         # exact session -> normal (4/3)
                (CODE, DAYS[23]),         # anchor rolls into same bucket
                ("000003.SZ", DAYS[24]),  # no dailybasic file -> omitted
            ])
            self.assertEqual(buckets[(CODE, DAYS[24])], "normal")
            self.assertEqual(buckets[(CODE, DAYS[23])], "normal")
            self.assertNotIn(("000003.SZ", DAYS[24]), buckets)
            with self.assertRaisesRegex(
                study.TurnoverStudyError, "dailybasic_cache_missing"
            ):
                study.turnover_buckets_for_events(
                    cache / "e", [(CODE, DAYS[24])]
                )


if __name__ == "__main__":
    unittest.main()
