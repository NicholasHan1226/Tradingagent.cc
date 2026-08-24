"""Offline tests for the unlock supply band rebuild study."""

from __future__ import annotations

import csv
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from Ashare import event_unlock_supply_study as study


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
                      floats: list[float], total: float = 1000.0) -> None:
    stem = f"dailybasic_{code[:6]}{code[7:]}"
    with (cache / f"{stem}.csv").open("w", newline="",
                                      encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["ts_code", "trade_date", "close", "total_share",
                         "float_share"])
        for d, fl in zip(days, floats):
            writer.writerow([code, d, 10.0, total, fl])


class ClassifySupplyTest(unittest.TestCase):
    def test_fixed_edges_strict(self) -> None:
        self.assertEqual(study.classify_supply(0.099), "small")
        self.assertEqual(study.classify_supply(0.10), "mid")
        self.assertEqual(study.classify_supply(0.299), "mid")
        self.assertEqual(study.classify_supply(0.30), "large")


class CircSeriesTest(unittest.TestCase):
    def test_universe_filter_malformed_skip_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            stem = f"dailybasic_{CODE[:6]}{CODE[7:]}"
            with (cache / f"{stem}.csv").open("w", newline="",
                                              encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(["ts_code", "trade_date", "close",
                                 "total_share", "float_share"])
                writer.writerow([CODE, DAYS[0], 10.0, 1000.0, 250.0])  # ok
                writer.writerow([CODE, DAYS[1], 10.0, "bad", 250.0])   # skip
                writer.writerow([CODE, DAYS[2], 10.0, 1000.0, -5.0])   # skip
                writer.writerow([CODE, DAYS[3], 10.0, 0.0, 250.0])     # skip
            _write_dailybasic(cache, "000001.SZ", DAYS[:2], [500.0])
            series = study._circ_series(cache, {CODE})
            self.assertEqual(list(series.keys()), [CODE])
            days, values = series[CODE]
            self.assertEqual(days, [DAYS[0]])
            self.assertAlmostEqual(values[0], 0.25)
            with self.assertRaisesRegex(
                study.SupplyStudyError, "dailybasic_cache_missing"
            ):
                study._circ_series(cache / "e", {CODE})

    def test_newest_first_file_is_normalized_ascending(self) -> None:
        # Tushare delivers newest-first; a descending file must still yield
        # ascending session order with fractions paired to their own days.
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            days_desc = list(reversed(DAYS[:25]))
            floats = [
                100.0 + i if i % 2 == 0 else 200.0 + i
                for i in range(25)
            ]
            _write_dailybasic(cache, CODE, days_desc,
                              list(reversed(floats)))
            series = study._circ_series(cache, {CODE})
            days, values = series[CODE]
            self.assertEqual(days, DAYS[:25])
            for got, want in zip(values, floats):
                self.assertAlmostEqual(got, want / 1000.0)


class AttachSupplyStatesTest(unittest.TestCase):
    def test_attach_paths_and_buckets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            _write_dailybasic(cache, CODE, DAYS, [250.0] * 30)   # circ .25
            _write_dailybasic(cache, "000002.SZ", DAYS, [900.0] * 30)
            signals = [
                # ratio 2.0 over circ .25 -> 8% of float -> small
                {"ts_code": CODE, "entry_day": DAYS[29],
                 "float_ratio": 2.0},
                # entry at first session: no strictly-prior anchor
                {"ts_code": CODE, "entry_day": DAYS[0], "float_ratio": 2.0},
                # no dailybasic file at all
                {"ts_code": "000004.SZ", "entry_day": DAYS[15],
                 "float_ratio": 2.0},
                # bad announcement ratio
                {"ts_code": CODE, "entry_day": DAYS[20], "float_ratio": None},
                # ratio 10 over circ .9 -> ~11.1% -> mid
                {"ts_code": "000002.SZ", "entry_day": DAYS[15],
                 "float_ratio": 10.0},
            ]
            stats = study.attach_supply_states(
                signals, study._circ_series(
                    cache, {CODE, "000002.SZ"}
                )
            )
            self.assertAlmostEqual(float(signals[0]["supply_over_float"]),
                                   0.08)
            self.assertEqual(signals[0]["supply_bucket"], "small")
            self.assertEqual(signals[4]["supply_bucket"], "mid")
            self.assertEqual(stats["no_prior_session"], 1)
            self.assertEqual(stats["missing_dailybasic"], 1)
            self.assertEqual(stats["bad_ratio"], 1)
            self.assertEqual(stats["attached"], 2)

    def test_stale_anchor_after_long_suspension_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            # history stops at DAYS[5]; entry far beyond the staleness cap
            _write_dailybasic(cache, CODE, DAYS[:6], [250.0] * 6)
            signals = [{"ts_code": CODE, "entry_day": DAYS[29],
                        "float_ratio": 2.0}]
            stats = study.attach_supply_states(
                signals, study._circ_series(cache, {CODE})
            )
            self.assertEqual(stats["stale_supply"], 1)
            self.assertNotIn("supply_bucket", signals[0])

    def test_short_suspension_gap_rolls_back_within_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            gap_free = DAYS[:26] + DAYS[29:]
            _write_dailybasic(cache, CODE, gap_free, [500.0] * 29)
            signals = [{"ts_code": CODE, "entry_day": DAYS[29],
                        "float_ratio": 10.0}]
            stats = study.attach_supply_states(
                signals, study._circ_series(cache, {CODE})
            )
            self.assertEqual(stats["attached"], 1)
            # 10 / 0.5 = 20% of float -> mid
            self.assertEqual(signals[0]["supply_bucket"], "mid")


class TercileMeansTest(unittest.TestCase):
    def test_extremes_equal_sized_and_remainder_in_middle(self) -> None:
        n = 10
        vals = [float(i) for i in range(n)]
        rets = [v / 100.0 for v in vals]
        cell = study._tercile_means(vals, rets)
        bot, top = cell["bottom"], cell["top"]
        assert bot is not None and top is not None
        self.assertEqual(bot["n"], 3)
        self.assertEqual(top["n"], 3)
        self.assertAlmostEqual(float(bot["mean_net_bps"]),
                               (0.0 + 0.01 + 0.02) * 1e4 / 3)
        self.assertAlmostEqual(float(top["mean_net_bps"]),
                               (0.07 + 0.08 + 0.09) * 1e4 / 3)

    def test_too_few_rows_yields_none_cells(self) -> None:
        self.assertIsNone(study._tercile_means([1.0], [0.1])["bottom"])


class OldBandTest(unittest.TestCase):
    def test_legacy_band_edges(self) -> None:
        self.assertEqual(study._old_band(2.99), "<3")
        self.assertEqual(study._old_band(3.0), "3-5")
        self.assertEqual(study._old_band(5.0), ">=5")


if __name__ == "__main__":
    unittest.main()
