"""Offline tests for the portfolio-level paper baseline simulator."""

from __future__ import annotations

import csv
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from Ashare import event_paper_baseline_sim as sim


def _write_csv(path: Path, fields: list[str], rows: list[list]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(fields)
        writer.writerows(rows)


def _sessions(count: int, start: date = date(2026, 1, 5)) -> list[str]:
    """Weekday session labels (calendar days stepped daily is fine offline)."""
    return [(start + timedelta(days=i)).strftime("%Y%m%d") for i in range(count)]


INDEX_DAYS = _sessions(30)
# 10 sessions flat, decline into a trough, then a strong recovery.
INDEX_CLOSES = (
    [100.0] * 10
    + [97.0, 94.0, 91.0, 88.0, 85.0, 82.0]
    + [86.0, 90.0, 94.0, 98.0, 102.0, 106.0, 110.0, 114.0]
    + [116.0, 118.0, 120.0, 122.0, 124.0, 126.0]
)


def _index_cache(cache: Path) -> None:
    _write_csv(
        cache / "index_000001SH.csv",
        ["trade_date", "close"],
        [[d, c] for d, c in zip(INDEX_DAYS, INDEX_CLOSES)],
    )


class LoadEventsTest(unittest.TestCase):
    def test_collapse_bad_and_inverted_rows_counted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            _write_csv(
                cache / "share_float.csv",
                ["ts_code", "ann_date", "float_date", "float_ratio"],
                [
                    ["600000.SH", "20260101", "20260114", "1.5"],
                    ["600000.SH", "20260102", "20260114", "4.2"],
                    ["600001.SH", "20260103", "20260120", "", ],
                    ["600002.SH", "20260130", "20260110", "2.0"],  # inverted
                    ["600003.SH", "20171201", "20171215", "2.0"],  # pre-start
                ],
            )
            events, stats = sim.load_events(cache)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["float_ratio"], 4.2)
            self.assertEqual(stats["skipped_bad_ratio_rows"], 1)
            self.assertEqual(stats["skipped_inverted_rows"], 1)

    def test_missing_cache_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(sim.BaselineSimError):
                sim.load_events(Path(tmp))


class BuildSignalsTest(unittest.TestCase):
    def _book(self, code: str, closes: list[float], factors: list[float] | None = None,
              days: list[str] | None = None) -> None:
        raise NotImplementedError

    def test_classification_window_ends_before_event_session(self) -> None:
        # Event at index position 14; stock mirrors the index path so
        # pre = c[13]/c[3]-1 = -12% (sell_off).  Doubling the EVENT-day close
        # must not change the classification: the window stops at pos-1.
        base = INDEX_CLOSES[:]
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            _index_cache(cache)
            for label, closes in (("base", base), ("jumped", base[:14] + [170.0] + base[15:])):
                stem = "600000SH" if label == "base" else "600001SH"
                _write_csv(cache / f"daily_{stem}.csv", ["trade_date", "close"],
                           [[d, c] for d, c in zip(INDEX_DAYS, closes)])
                _write_csv(cache / f"adjfactor_{stem}.csv", ["trade_date", "adj_factor"],
                           [[d, 1.0] for d in INDEX_DAYS])
            books, uncovered = sim.load_stock_books(cache)
            self.assertEqual(uncovered, 0)
            events = [
                {"ts_code": "600000.SH", "float_date": INDEX_DAYS[14], "float_ratio": 2.0},
                {"ts_code": "600001.SH", "float_date": INDEX_DAYS[14], "float_ratio": 2.0},
            ]
            index_pairs = [
                (date(2026, 1, 5) + timedelta(days=i), c)
                for i, c in enumerate(INDEX_CLOSES)
            ]
            signals, stats = sim.build_signals(events, books, index_pairs, INDEX_DAYS[-1])
            self.assertEqual(stats["skipped_truncated"], 0)
            by_code = {s["ts_code"]: s for s in signals}
            self.assertEqual(len(by_code), 2)
            self.assertAlmostEqual(by_code["600000.SH"]["pre_return"], 88.0 / 100.0 - 1.0)
            self.assertAlmostEqual(by_code["600001.SH"]["pre_return"], 88.0 / 100.0 - 1.0)
            self.assertEqual(by_code["600000.SH"]["regime"], "weak")
            self.assertEqual(by_code["600000.SH"]["entry_day"], INDEX_DAYS[14])
            self.assertEqual(by_code["600000.SH"]["exit_day"], INDEX_DAYS[19])

    def test_threshold_inclusive_and_adjusted_entry_price(self) -> None:
        days = _sessions(30)
        closes = [100.0] * 30
        closes[20] = 97.0  # pre at pos 21: c[20]/c[10]-1 = -3% exactly
        closes[21] = 96.0
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            _write_csv(cache / f"daily_600000SH.csv", ["trade_date", "close"],
                       [[d, c] for d, c in zip(days, closes)])
            _write_csv(cache / f"adjfactor_600000SH.csv", ["trade_date", "adj_factor"],
                       [[d, 2.0] for d in days])
            books, _ = sim.load_stock_books(cache)
            events = [{"ts_code": "600000.SH", "float_date": days[21], "float_ratio": 2.0}]
            signals, stats = sim.build_signals(events, books, [], days[-1])
            self.assertEqual(len(signals), 1)  # <= threshold stays a signal
            self.assertAlmostEqual(signals[0]["entry_price"], 192.0)

    def test_truncated_exit_skipped(self) -> None:
        days = _sessions(30)
        closes = [100.0] * 30
        closes[20] = 90.0
        closes[21] = 89.0
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            _write_csv(cache / f"daily_600000SH.csv", ["trade_date", "close"],
                       [[d, c] for d, c in zip(days, closes)])
            _write_csv(cache / f"adjfactor_600000SH.csv", ["trade_date", "adj_factor"],
                       [[d, 1.0] for d in days])
            books, _ = sim.load_stock_books(cache)
            # last_global_day forces the exit leg out of the span.
            events = [{"ts_code": "600000.SH", "float_date": days[21], "float_ratio": 2.0}]
            signals, stats = sim.build_signals(events, books, [], days[22])
            self.assertEqual(signals, [])
            self.assertEqual(stats["skipped_truncated"], 1)


class RuleArmFilterTest(unittest.TestCase):
    def test_weak_only_known_ratio_outside_band(self) -> None:
        weak = {"regime": "weak", "float_ratio": 2.0}
        self.assertTrue(sim.rule_arm_filter(weak))
        self.assertFalse(sim.rule_arm_filter({"regime": "weak", "float_ratio": 3.5}))
        self.assertFalse(sim.rule_arm_filter({"regime": "weak", "float_ratio": None}))
        self.assertFalse(sim.rule_arm_filter({"regime": "strong", "float_ratio": 2.0}))
        self.assertFalse(sim.rule_arm_filter({"regime": "unknown", "float_ratio": 2.0}))


class RunPortfolioTest(unittest.TestCase):
    def _books_two(self, q1: float, q2: float) -> dict[str, sim.StockBook]:
        days = INDEX_DAYS
        a = [100.0] * len(days)
        b = [200.0] * len(days)
        a[days.index(INDEX_DAYS[10])] = 100.0
        b[days.index(INDEX_DAYS[10])] = 200.0
        # Sell_off classification needs the pre-window drop; give both books
        # a deep early drop so their signals qualify.
        for series in (a, b):
            for i in range(len(series)):
                series[i] = series[i] * 0.5 if i >= 5 else series[i]
        a[19] = q1
        b[19] = q2
        return {
            "600000.SH": sim.StockBook(days, a),
            "600001.SH": sim.StockBook(days, b),
        }

    def test_equal_split_cost_invariant(self) -> None:
        days = INDEX_DAYS
        books = self._books_two(q1=55.0, q2=110.0)
        signals = []
        for code, price in (("600000.SH", 50.0), ("600001.SH", 100.0)):
            signals.append({
                "ts_code": code,
                "float_date": days[10],
                "entry_day": days[10],
                "exit_day": days[15],
                "entry_price": price,
                "exit_price": 55.0 if code.endswith("0.SH") else 110.0,
                "float_ratio": 2.0,
                "pre_return": -0.10,
                "regime": "sideways",
            })
        cost_rate = (15.0 / 2.0) / 1e4
        run_cost = sim.run_portfolio(signals, days, books, initial_cash=20000.0)
        run_free = sim.run_portfolio(signals, days, books, initial_cash=20000.0, cost_bps=0.0)
        self.assertEqual(run_cost["closed_positions"], 2)
        self.assertEqual(run_free["closed_positions"], 2)
        eq_cost = run_cost["nav"][-1][1]
        eq_free = run_free["nav"][-1][1]
        # Full deployment then full exit compounds the per-side fee twice:
        # eq_cost = (1-rate)^2 * zero-cost gross proceeds.
        gross_free = 0.0
        for signal in signals:
            gross_free += (10000.0 / signal["entry_price"]) * signal["exit_price"]
        self.assertAlmostEqual(eq_cost, (1.0 - cost_rate) ** 2 * gross_free, places=6)
        self.assertAlmostEqual(eq_free, gross_free, places=6)

    def test_insufficient_cash_skips_counted(self) -> None:
        books = self._books_two(q1=55.0, q2=110.0)
        signals = [{
            "ts_code": "600000.SH",
            "float_date": INDEX_DAYS[10],
            "entry_day": INDEX_DAYS[10],
            "exit_day": INDEX_DAYS[15],
            "entry_price": 50.0,
            "exit_price": 55.0,
            "float_ratio": 2.0,
            "pre_return": -0.10,
            "regime": "sideways",
        }]
        run = sim.run_portfolio(signals, INDEX_DAYS, books, initial_cash=4000.0)
        self.assertEqual(run["skipped_no_cash"], 1)
        self.assertEqual(run["closed_positions"], 0)
        for _, equity in run["nav"]:
            self.assertAlmostEqual(equity, 4000.0)

    def test_exits_free_cash_before_same_day_entries(self) -> None:
        books = self._books_two(q1=55.0, q2=110.0)
        common = {"float_ratio": 2.0, "pre_return": -0.10, "regime": "sideways"}
        first = {
            "ts_code": "600000.SH",
            "float_date": INDEX_DAYS[5],
            "entry_day": INDEX_DAYS[5],
            "exit_day": INDEX_DAYS[10],
            "entry_price": 50.0,
            "exit_price": 55.0,
            **common,
        }
        second = {
            "ts_code": "600001.SH",
            "float_date": INDEX_DAYS[10],
            "entry_day": INDEX_DAYS[10],
            "exit_day": INDEX_DAYS[15],
            "entry_price": 100.0,
            "exit_price": 110.0,
            **common,
        }
        run = sim.run_portfolio([first, second], INDEX_DAYS, books, initial_cash=8000.0)
        self.assertEqual(run["closed_positions"], 2)
        self.assertEqual(run["skipped_no_cash"], 0)


class MetricsTest(unittest.TestCase):
    def test_monthly_returns_use_base_for_first_month(self) -> None:
        nav = [("20260128", 100.0), ("20260130", 110.0), ("20260202", 121.0)]
        months = sim.monthly_net_returns(nav, base=100.0)
        self.assertEqual([m for m, _ in months], ["202601", "202602"])
        self.assertAlmostEqual(months[0][1], 0.10)
        self.assertAlmostEqual(months[1][1], 0.10)

    def test_monthly_default_base_is_first_point(self) -> None:
        nav = [("20260130", 110.0), ("20260202", 99.0)]
        months = sim.monthly_net_returns(nav)
        self.assertAlmostEqual(months[0][1], 0.0)
        self.assertAlmostEqual(months[1][1], 99.0 / 110.0 - 1.0)

    def test_max_drawdown(self) -> None:
        nav = [("a", 100.0), ("b", 120.0), ("c", 90.0), ("d", 95.0)]
        self.assertAlmostEqual(sim.max_drawdown(nav), 90.0 / 120.0 - 1.0)

    def test_benchmark_return(self) -> None:
        pairs = [
            (date(2026, 1, 5) + timedelta(days=i), c)
            for i, c in enumerate(INDEX_CLOSES)
        ]
        value = sim.benchmark_return(pairs, INDEX_DAYS[0], INDEX_DAYS[-1])
        assert value is not None
        self.assertAlmostEqual(value, 126.0 / 100.0 - 1.0)


class EndToEndTest(unittest.TestCase):
    def test_run_study_produces_both_arms_and_nav_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            _index_cache(cache)
            # Stock A mirrors the index (sell_off into the weak trough).
            _write_csv(cache / "daily_600000SH.csv", ["trade_date", "close"],
                       [[d, c] for d, c in zip(INDEX_DAYS, INDEX_CLOSES)])
            _write_csv(cache / "adjfactor_600000SH.csv", ["trade_date", "adj_factor"],
                       [[d, 1.0] for d in INDEX_DAYS])
            # Stock B crashes late into the STRONG phase (sell_off, wrong regime).
            b = [50.0] * 30
            b[21] = 45.0
            b[22] = 43.0
            _write_csv(cache / "daily_600001SH.csv", ["trade_date", "close"],
                       [[d, c] for d, c in zip(INDEX_DAYS, b)])
            _write_csv(cache / "adjfactor_600001SH.csv", ["trade_date", "adj_factor"],
                       [[d, 1.0] for d in INDEX_DAYS])
            _write_csv(
                cache / "share_float.csv",
                ["ts_code", "ann_date", "float_date", "float_ratio"],
                [
                    ["600000.SH", "20260101", INDEX_DAYS[14], "2.0"],
                    ["600001.SH", "20260108", INDEX_DAYS[22], "4.0"],  # band-excluded
                    ["600002.SH", "20260109", INDEX_DAYS[22], ""],     # bad ratio
                ],
            )
            results = sim.run_study(cache)
            arms = results["arms"]
            assert isinstance(arms, dict)
            self.assertEqual(arms["all"]["signals"], 2)
            self.assertEqual(arms["rule"]["signals"], 1)  # weak + ratio outside band
            self.assertTrue(results["research_only"])  # type: ignore[index]
            self.assertTrue(results["not_promotion_evidence"])  # type: ignore[index]
            for name in ("all", "rule"):
                path = Path(str(arms[name]["nav_path"]))  # type: ignore[index]
                self.assertTrue(path.exists())
                with path.open(encoding="utf-8") as handle:
                    header = next(csv.reader(handle))
                self.assertEqual(header, ["trade_date", "equity_cny", "research_only"])
            # Both arms stay solvent and finite.
            for name in ("all", "rule"):
                total = arms[name]["total_net_return"]  # type: ignore[index]
                self.assertGreater(float(total), -1.0)


class RefreshShareFloatTest(unittest.TestCase):
    def test_fetches_only_missing_codes_and_is_idempotent(self) -> None:
        import unittest.mock

        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            _write_csv(
                cache / "share_float.csv",
                ["ts_code", "ann_date", "float_date", "float_ratio"],
                [["600000.SH", "20260101", "20260114", "2.0"]],
            )
            _write_csv(cache / "daily_600001SH.csv", ["trade_date", "close"],
                       [[d, "10.0"] for d in INDEX_DAYS])
            _write_csv(cache / "adjfactor_600001SH.csv", ["trade_date", "adj_factor"],
                       [[d, "1.0"] for d in INDEX_DAYS])
            calls: list[str] = []

            def fake_call_api(api: str, params: dict):
                calls.append(params["ts_code"])
                if len(calls) > 1:
                    raise AssertionError("network hit on second refresh")
                return ["ts_code"], [
                    ["600001.SH", "20260102", "20260702", "300", "4.0", "B", "首发"]
                ]

            with unittest.mock.patch(
                "Ashare.event_calendar_fetch.call_api", side_effect=fake_call_api
            ):
                fetched = sim.refresh_share_float(cache)
                self.assertEqual(fetched, 1)
                self.assertEqual(calls, ["600001.SH"])
                # Second pass finds nothing missing and never hits the network.
                self.assertEqual(sim.refresh_share_float(cache), 0)
            with cache.joinpath("share_float.csv").open(encoding="utf-8") as handle:
                rows = list(csv.reader(handle))
            self.assertEqual(len(rows), 3)  # header + original + appended
            self.assertEqual(rows[2][0], "600001.SH")

    def test_missing_cache_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(sim.BaselineSimError):
                sim.refresh_share_float(Path(tmp))


if __name__ == "__main__":
    unittest.main()
