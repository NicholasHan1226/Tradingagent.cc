"""Offline tests for the margin-flow regime study.

Everything stays offline: the Tushare fetcher is monkeypatched and the
index series is a synthetic CSV written into a temp cache directory.
"""

from __future__ import annotations

import csv
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from Ashare import event_margin_flow_research as mfr


def _session_dates(count: int, start: date = date(2026, 1, 1)) -> list[date]:
    return [start + timedelta(days=i) for i in range(count)]


def _agg_fields() -> list[str]:
    return [
        "trade_date",
        "exchange_id",
        "rzye",
        "rzmre",
        "rzche",
        "rqye",
        "rqmcl",
        "rzrqye",
        "rqyl",
    ]


def _agg_row(day: str, exchange: str, rzye: float, rzrqye: float) -> list:
    return [day, exchange, rzye, 1.0, 1.0, 1.0, 1.0, rzrqye, None]


class DedupeMarginRowsTest(unittest.TestCase):
    def test_drops_duplicates_and_sorts(self) -> None:
        fields = _agg_fields()
        rows = [
            _agg_row("20260105", "SSE", 1.0, 2.0),
            _agg_row("20260104", "SZSE", 3.0, 4.0),
            _agg_row("20260105", "SSE", 1.0, 2.0),  # range-page overlap dup
            _agg_row("20260105", "BSE", 5.0, 6.0),
        ]
        out_fields, kept, dups = mfr.dedupe_margin_rows(fields, rows)
        self.assertEqual(out_fields, fields)
        self.assertEqual(dups, 1)
        keys = [(r[0], r[1]) for r in kept]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(len(kept), 3)


class DailyTotalsTest(unittest.TestCase):
    def test_sums_exchanges_per_day_with_none_guard(self) -> None:
        fields = _agg_fields()
        rows = [
            _agg_row("20260105", "SSE", 100.0, 10.0),
            _agg_row("20260105", "SZSE", 50.0, None),
            _agg_row("20260106", "SSE", 70.0, 7.0),
        ]
        totals = mfr.daily_totals(fields, rows)
        self.assertEqual(totals, [("20260105", 150.0, 10.0), ("20260106", 70.0, 7.0)])


class MarginFeaturesTest(unittest.TestCase):
    def test_lag_math_and_warmup_skip(self) -> None:
        totals = [("20260101", 100.0, 1.0)]
        for i in range(1, 25):
            day = (_session_dates(30)[i]).strftime("%Y%m%d")
            totals.append((day, float(i + 1) * 100.0, 1.0))
        features = mfr.margin_features(totals)
        # pos<20 skipped entirely.
        self.assertEqual(len(features), 5)
        first_day, short_chg, long_chg = features[0]
        self.assertEqual(first_day, "20260121")
        # long base is 20 sessions back = totals[0] (100.0); value 2100.0.
        self.assertAlmostEqual(long_chg, 2100.0 / 100.0 - 1.0)
        # short base is 5 sessions back = totals[15] (1600.0).
        self.assertAlmostEqual(short_chg, 2100.0 / 1600.0 - 1.0)

    def test_nonpositive_base_fails_closed(self) -> None:
        totals = [("20260101", 0.0, 1.0)] * 21
        with self.assertRaises(mfr.MarginStudyError):
            mfr.margin_features(totals)


class ForwardReturnMapTest(unittest.TestCase):
    def test_horizon_return_keyed_by_date(self) -> None:
        pairs = [(date(2026, 1, 1 + i), 100.0 + i * 10.0) for i in range(15)]
        fwd = mfr.forward_return_map(pairs, horizon=10)
        self.assertAlmostEqual(fwd[date(2026, 1, 1)], 200.0 / 100.0 - 1.0)
        self.assertNotIn(date(2026, 1, 6), fwd)  # tail has no full horizon


class RegimeByDayTest(unittest.TestCase):
    def _flat_then_move(self, move: float) -> list[tuple[date, float]]:
        days = _session_dates(25)
        pairs = [(day, 100.0) for day in days]
        pairs[-1] = (pairs[-1][0], 100.0 * (1.0 + move))
        return pairs

    def test_bins_match_tracker_thresholds(self) -> None:
        weak = mfr.regime_by_day(self._flat_then_move(-0.05))
        sideways = mfr.regime_by_day(self._flat_then_move(0.0))
        strong = mfr.regime_by_day(self._flat_then_move(0.05))
        last = _session_dates(25)[-1]
        self.assertEqual(weak[last], "weak")
        self.assertEqual(sideways[last], "sideways")
        self.assertEqual(strong[last], "strong")


class PearsonTest(unittest.TestCase):
    def test_perfect_and_short_sample(self) -> None:
        self.assertAlmostEqual(mfr.pearson([1.0, 2.0, 3.0], [2.0, 4.0, 6.0]), 1.0)
        self.assertIsNone(mfr.pearson([1.0], [1.0]))
        self.assertIsNone(mfr.pearson([1.0, 1.0, 1.0], [1.0, 2.0, 3.0]))


class QuantileSpreadTest(unittest.TestCase):
    def test_buckets_and_spread(self) -> None:
        pairs = [(float(i), float(i)) for i in range(10)]  # signal == forward
        result = mfr.quantile_spread(pairs)
        self.assertEqual(result["n"], 10)
        sizes = [row["n"] for row in result["buckets"]]
        assert isinstance(sizes, list)
        self.assertEqual(sum(sizes), 10)
        self.assertTrue(max(sizes) - min(sizes) <= 1)
        spread = result["spread"]
        assert isinstance(spread, float)
        self.assertGreater(spread, 0.0)


class RunStudyEndToEndTest(unittest.TestCase):
    def test_offline_run_with_patched_fetch(self) -> None:
        fields = _agg_fields()
        rows: list[list] = []
        # 40 sessions of growing margin across two exchanges.
        days = _session_dates(40)
        for i, day in enumerate(days):
            key = day.strftime("%Y%m%d")
            rows.append(_agg_row(key, "SSE", 1000.0 + i, 10.0 + i))
            rows.append(_agg_row(key, "SZSE", 500.0 + i, 5.0 + i))
        rows.append(rows[-1])  # duplicate page-overlap row

        index_days = _session_dates(50)
        index_pairs = [(day, 3000.0 + i * 5.0) for i, day in enumerate(index_days)]
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            with (cache / "index_000001SH.csv").open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(["trade_date", "close"])
                for day, close in index_pairs:
                    writer.writerow([day.strftime("%Y%m%d"), close])

            original_fetch = mfr.fetch_ranged

            def fake_fetch(api: str, start: str, end: str, depth: int = 0):
                assert api == "margin" and start <= end
                return list(fields), [list(r) for r in rows]

            mfr.fetch_ranged = fake_fetch  # type: ignore[assignment]
            try:
                summary = mfr.run_study(cache=cache, refresh=True)
            finally:
                mfr.fetch_ranged = original_fetch  # type: ignore[assignment]

            self.assertTrue((cache / f"{mfr.AGGREGATE_NAME}.csv").exists())
            self.assertTrue(summary["research_only"])
            self.assertEqual(summary["days_total"], 40)
            self.assertGreater(int(summary["days_joined"]), 0)

            # Cached second run must not hit the fetcher at all.
            def exploding_fetch(api: str, start: str, end: str, depth: int = 0):
                raise AssertionError("fetcher called on cached run")

            mfr.fetch_ranged = exploding_fetch  # type: ignore[assignment]
            try:
                again = mfr.run_study(cache=cache)
            finally:
                mfr.fetch_ranged = original_fetch  # type: ignore[assignment]
            self.assertEqual(again["days_total"], 40)


if __name__ == "__main__":
    unittest.main()
