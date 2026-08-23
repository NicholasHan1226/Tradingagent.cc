"""Offline tests for the pre-lockup leverage-abnormality study."""

from __future__ import annotations

import csv
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from Ashare import event_margin_lockup_window as mlw


def _write_csv(path: Path, fields: list[str], rows: list[list]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(fields)
        writer.writerows(rows)


class LoadEventsTest(unittest.TestCase):
    def test_same_day_rows_collapse_to_max_ratio(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            _write_csv(
                cache / "share_float.csv",
                ["ts_code", "ann_date", "float_date", "float_share", "float_ratio", "holder_name", "share_type"],
                [
                    ["000001.SZ", "20260310", "20260910", "100", "1.5", "A", "定增"],
                    ["000001.SZ", "20260310", "20260910", "300", "4.2", "B", "首发"],
                    ["000001.SZ", "20260101", "20260701", "50", "0.4", "C", "首发"],
                ],
            )
            events, skipped = mlw.load_events(cache)
            self.assertEqual(skipped, 0)
            self.assertEqual(len(events), 2)
            march = [e for e in events if e["ann_date"] == "20260310"][0]
            self.assertEqual(march["float_ratio"], 4.2)

    def test_unparseable_ratio_skipped_and_counted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            _write_csv(
                cache / "share_float.csv",
                ["ts_code", "ann_date", "float_date", "float_share", "float_ratio", "holder_name", "share_type"],
                [
                    ["000001.SZ", "20260310", "20260910", "100", "", "A", "定增"],
                    ["000001.SZ", "20260311", "20260911", "300", "4.2", "B", "首发"],
                ],
            )
            events, skipped = mlw.load_events(cache)
            self.assertEqual(skipped, 1)
            self.assertEqual(len(events), 1)

    def test_missing_cache_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(mlw.MarginWindowError):
                mlw.load_events(Path(tmp))


class PreWindowChangeTest(unittest.TestCase):
    def test_lag_math_and_guards(self) -> None:
        series = [(f"202601{d:02d}", float(d)) for d in range(1, 26)]
        # anchor 20260125 sits at pos 24; base = series[pos-4] -> day 21, value 21.
        self.assertAlmostEqual(mlw.pre_window_change(series, "20260125", lag=4), 25.0 / 21.0 - 1.0)
        self.assertIsNone(mlw.pre_window_change(series[:3], "20260103", lag=20))
        flat_zero = [("20260101", 0.0)] * 21
        self.assertIsNone(mlw.pre_window_change(flat_zero, "20260121"))


class ForwardReturnTest(unittest.TestCase):
    def test_adjusted_forward_return(self) -> None:
        days = [(date(2026, 1, 1) + timedelta(days=i)).strftime("%Y%m%d") for i in range(15)]
        closes = [100.0 + i for i in range(15)]
        factors = [10.0] * 15
        factors[12] = 20.0  # adjustment event mid-window
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            _write_csv(cache / "daily_000001SZ.csv", ["trade_date", "close"], [[d, c] for d, c in zip(days, closes)])
            _write_csv(cache / "adjfactor_000001SZ.csv", ["trade_date", "adj_factor"], [[d, f] for d, f in zip(days, factors)])
            raw, excess = mlw.forward_return(cache, "000001.SZ", days[2], horizon=10)
            self.assertIsNone(excess)
            expected = (closes[12] * factors[12]) / (closes[2] * factors[2]) - 1.0
            assert raw is not None
            self.assertAlmostEqual(raw, expected)

    def test_descending_csv_rows_are_sorted(self) -> None:
        days = [(date(2026, 1, 1) + timedelta(days=i)).strftime("%Y%m%d") for i in range(15)]
        closes = [100.0 + i for i in range(15)]
        factors = [10.0] * 15
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            # Tushare arrives newest-first on disk.
            _write_csv(cache / "daily_000001SZ.csv", ["trade_date", "close"],
                       [[d, c] for d, c in reversed(list(zip(days, closes)))])
            _write_csv(cache / "adjfactor_000001SZ.csv", ["trade_date", "adj_factor"],
                       [[d, f] for d, f in reversed(list(zip(days, factors)))])
            raw, _ = mlw.forward_return(cache, "000001.SZ", days[2], horizon=10)
            expected = closes[12] * factors[12] / (closes[2] * factors[2]) - 1.0
            assert raw is not None
            self.assertAlmostEqual(raw, expected)

    def test_missing_cache_returns_none_pair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = mlw.forward_return(Path(tmp), "000001.SZ", "20260105")
            self.assertEqual(result, (None, None))


class RegimeLabelTest(unittest.TestCase):
    def test_bins_parity(self) -> None:
        days = [(date(2026, 1, 1) + timedelta(days=i)).strftime("%Y%m%d") for i in range(25)]
        pairs = [(date(2026, 1, 1) + timedelta(days=i), 100.0) for i in range(25)]
        pairs[-1] = (pairs[-1][0], 94.0)
        label = mlw.regime_label(pairs, days[-1])
        self.assertEqual(label, "weak")


class TercileTableTest(unittest.TestCase):
    def test_buckets_sorted_with_net_deduction(self) -> None:
        triples = [(float(i), 0.001 * i) for i in range(9)]  # signal == gross fwd
        table = mlw.tercile_table(triples)
        self.assertEqual([row["n"] for row in table], [3, 3, 3])
        cost = 15.0 / 1e4
        top = table[-1]
        self.assertAlmostEqual(top["mean_excess_net"], 0.007 - cost)  # type: ignore[arg-type]
        # Bottom tercile gross mean 0.001 is below the 15bps round-trip cost.
        self.assertLess(table[0]["mean_excess_net"], 0.0)  # type: ignore[operator]
        self.assertLess(table[0]["mean_excess_net"], 0.0)  # type: ignore[operator]


if __name__ == "__main__":
    unittest.main()
