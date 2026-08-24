"""Offline tests for the pre-lockup chips (winner_rate) study."""

from __future__ import annotations

import csv
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from Ashare import event_chips_prelockup_study as study


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


def _write_cyqperf(cache: Path, code: str, days: list[str],
                   winner_rates_pct: list[float]) -> None:
    stem = f"cyqperf_{code[:6]}{code[7:]}"
    with (cache / f"{stem}.csv").open("w", newline="",
                                      encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["ts_code", "trade_date", "his_low", "his_high",
                         "cost_5pct", "cost_15pct", "cost_50pct",
                         "cost_85pct", "cost_95pct", "weight_avg",
                         "winner_rate"])
        for d, wr in zip(days, winner_rates_pct):
            writer.writerow([code, d, 9.0, 11.0, 9.5, 9.8, 10.0, 10.3,
                             10.6, 10.05, wr])


class ClassifyWinnerRateTest(unittest.TestCase):
    def test_fixed_edges_strict(self) -> None:
        self.assertEqual(study.classify_winner_rate(0.29), "underwater")
        self.assertEqual(study.classify_winner_rate(0.3), "mid")
        self.assertEqual(study.classify_winner_rate(0.69), "mid")
        self.assertEqual(study.classify_winner_rate(0.7), "profit")
        self.assertEqual(study.classify_winner_rate(0.95), "profit")


class WinnerSeriesTest(unittest.TestCase):
    def test_universe_filter_malformed_skip_fail_closed_percent_scale(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            _write_cyqperf(cache, CODE, DAYS[:3], [55.5, "", 80.0])
            _write_cyqperf(cache, "000001.SZ", DAYS[:3], [50.0, 50.0, 50.0])
            series = study._winner_series(cache, {CODE})
            # only the requested symbol; malformed row skipped; feed percent
            # converted to a fraction
            self.assertEqual(list(series.keys()), [CODE])
            days, values = series[CODE]
            self.assertEqual(days, [DAYS[0], DAYS[2]])
            self.assertAlmostEqual(values[0], 0.555)
            self.assertAlmostEqual(values[1], 0.8)
            with self.assertRaisesRegex(
                study.ChipsStudyError, "cyq_cache_missing"
            ):
                study._winner_series(cache / "e", {CODE})

    def test_newest_first_file_is_normalized_ascending(self) -> None:
        # Tushare delivers newest-first; a descending file must still yield
        # ascending session order with values paired to their own days.
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            _write_cyqperf(cache, CODE, list(reversed(DAYS[:25])),
                           [float(v) for v in range(25, 0, -1)])
            series = study._winner_series(cache, {CODE})
            days, values = series[CODE]
            self.assertEqual(days, DAYS[:25])
            self.assertEqual(
                values, [float(i) / 100.0 for i in range(1, 26)]
            )


class AttachChipsStatesTest(unittest.TestCase):
    def test_attach_paths_and_staleness_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            _write_cyqperf(cache, CODE, DAYS, [40.0] * 29 + [75.0])
            _write_cyqperf(cache, "000002.SZ", DAYS, [20.0] * 30)
            signals = [
                # entry DAYS[29]: latest strictly-prior session is DAYS[28]
                {"ts_code": CODE, "entry_day": DAYS[29]},
                {"ts_code": "000002.SZ", "entry_day": DAYS[15]},  # underwater
                {"ts_code": CODE, "entry_day": DAYS[0]},   # no prior session
                {"ts_code": "000004.SZ", "entry_day": DAYS[15]},  # no file
            ]
            stats = study.attach_chips_states(
                signals,
                study._winner_series(
                    cache, {CODE, "000002.SZ"}
                ),
            )
            self.assertAlmostEqual(float(signals[0]["winner_rate"]), 0.40)
            self.assertEqual(signals[0]["chips_bucket"], "mid")
            self.assertEqual(signals[1]["chips_bucket"], "underwater")
            self.assertEqual(stats["no_prior_session"], 1)
            self.assertEqual(stats["missing_cyq"], 1)
            self.assertEqual(stats["attached"], 2)

    def test_stale_anchor_after_long_suspension_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            # history stops at DAYS[5]; entry far beyond the staleness cap
            _write_cyqperf(cache, CODE, DAYS[:6], [50.0] * 6)
            signals = [{"ts_code": CODE, "entry_day": DAYS[29]}]
            stats = study.attach_chips_states(signals,
                                              study._winner_series(cache,
                                                                   {CODE}))
            self.assertEqual(stats["stale_chips"], 1)
            self.assertNotIn("chips_bucket", signals[0])

    def test_short_suspension_gap_rolls_back_within_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            # sessions exist up to index 25 then a 3-session gap; the entry
            # sits after the gap and rolls back to DAYS[25]
            gap_free = DAYS[:26] + DAYS[29:]
            _write_cyqperf(cache, CODE, gap_free, [60.0] * 29)
            signals = [{"ts_code": CODE, "entry_day": DAYS[29]}]
            stats = study.attach_chips_states(signals,
                                              study._winner_series(cache,
                                                                   {CODE}))
            self.assertEqual(stats["attached"], 1)
            self.assertEqual(signals[0]["chips_bucket"], "mid")


class BucketsForEventsTest(unittest.TestCase):
    def test_lookup_omits_unlabeled_and_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            _write_cyqperf(cache, CODE, DAYS, [80.0] * 30)
            buckets = study.chips_buckets_for_events(cache, [
                (CODE, DAYS[24]),
                (CODE, DAYS[0]),          # no prior session -> omitted
                ("000003.SZ", DAYS[24]),  # no cyqperf file -> omitted
            ])
            self.assertEqual(buckets[(CODE, DAYS[24])], "profit")
            self.assertNotIn((CODE, DAYS[0]), buckets)
            self.assertNotIn(("000003.SZ", DAYS[24]), buckets)
            with self.assertRaisesRegex(
                study.ChipsStudyError, "cyq_cache_missing"
            ):
                study.chips_buckets_for_events(
                    cache / "e", [(CODE, DAYS[24])]
                )


if __name__ == "__main__":
    unittest.main()
