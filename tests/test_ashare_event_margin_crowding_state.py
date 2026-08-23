"""Offline tests for the market-level margin-crowding state study."""

from __future__ import annotations

import csv
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from Ashare import event_margin_crowding_state as mcs


def _write_csv(path: Path, fields: list[str], rows: list[list]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(fields)
        writer.writerows(rows)


def _weekday_sessions(count: int, start: date) -> list[str]:
    days: list[str] = []
    cursor = start
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor.strftime("%Y%m%d"))
        cursor += timedelta(days=1)
    return days


INDEX_DAYS = _weekday_sessions(30, date(2026, 1, 5))
INDEX_CLOSES = (
    [100.0] * 10
    + [97.0, 94.0, 91.0, 88.0, 85.0, 82.0]
    + [86.0, 90.0, 94.0, 98.0, 102.0, 106.0, 110.0, 114.0]
    + [116.0, 118.0, 120.0, 122.0, 124.0, 126.0]
)
# Margin grid ends BEFORE the index window: every entry day then resolves
# to the final margin feature session (strictly-prior publication lag).
MARGIN_DAYS = _weekday_sessions(45, date(2025, 11, 3))
MARGIN_RZYE = [100.0] * 25 + [100.0 + 2.0 * i for i in range(1, 21)]


class ClassifyMarginStateTest(unittest.TestCase):
    def test_fixed_bucket_edges(self) -> None:
        self.assertEqual(mcs.classify_margin_state(-0.05), "deleverage")
        self.assertEqual(mcs.classify_margin_state(-0.02), "neutral")  # low-inclusive
        self.assertEqual(mcs.classify_margin_state(0.0), "neutral")
        self.assertEqual(mcs.classify_margin_state(0.0199), "neutral")
        self.assertEqual(mcs.classify_margin_state(0.02), "expansion")


class AttachMarginStatesTest(unittest.TestCase):
    def test_uses_latest_strictly_prior_session(self) -> None:
        days = ["20260101", "20260102"]
        states = {"20260101": -0.05, "20260102": 0.05}
        signals = [
            {"entry_day": "20260102"},  # same-day value not yet published
            {"entry_day": "20260101"},  # nothing strictly prior
        ]
        _, missing = mcs.attach_margin_states(signals, days, states)
        self.assertEqual(signals[0]["margin_state"], "deleverage")
        self.assertEqual(signals[0]["margin_state_day"], "20260101")
        self.assertIsNone(signals[1]["margin_state"])
        self.assertEqual(missing, 1)

    def test_classification_applied_from_change(self) -> None:
        days = ["20260101"]
        signals = [{"entry_day": "20260105"}]
        mcs.attach_margin_states(signals, days, {"20260101": 0.03})
        self.assertEqual(signals[0]["margin_state"], "expansion")


class CrossTabTest(unittest.TestCase):
    def test_buckets_math_and_missing_excluded(self) -> None:
        signals = [
            {"margin_state": "deleverage", "exit_price": 110.0, "entry_price": 100.0},
            {"margin_state": "deleverage", "exit_price": 90.0, "entry_price": 100.0},
            {"margin_state": "expansion", "exit_price": 105.0, "entry_price": 100.0},
            {"margin_state": None, "exit_price": 500.0, "entry_price": 100.0},
        ]
        tab = mcs.cross_tab(signals, cost_bps=0.0)
        self.assertEqual(tab["deleverage"]["n"], 2)
        self.assertAlmostEqual(tab["deleverage"]["mean_net_bps"], 0.0)  # +10% and -10%
        self.assertEqual(tab["deleverage"]["win_rate"], 0.5)
        self.assertEqual(tab["expansion"]["n"], 1)
        self.assertAlmostEqual(tab["expansion"]["mean_net_bps"], 500.0)
        self.assertEqual(tab["neutral"]["n"], 0)
        self.assertIsNone(tab["neutral"]["mean_net_bps"])


class RunStudySmokeTest(unittest.TestCase):
    def test_end_to_end_tabs_and_conditioned_portfolio(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            _write_csv(
                cache / "index_000001SH.csv",
                ["trade_date", "close"],
                [[d, c] for d, c in zip(INDEX_DAYS, INDEX_CLOSES)],
            )
            _write_csv(cache / "daily_600000SH.csv", ["trade_date", "close"],
                       [[d, c] for d, c in zip(INDEX_DAYS, INDEX_CLOSES)])
            _write_csv(cache / "adjfactor_600000SH.csv", ["trade_date", "adj_factor"],
                       [[d, 1.0] for d in INDEX_DAYS])
            _write_csv(
                cache / "share_float.csv",
                ["ts_code", "ann_date", "float_date", "float_ratio"],
                [["600000.SH", "20260101", INDEX_DAYS[14], "2.0"]],
            )
            _write_csv(
                cache / "margin_aggregate.csv",
                ["trade_date", "exchange_id", "rzye", "rzmre", "rzche",
                 "rqye", "rqmcl", "rzrqye", "rqyl"],
                [
                    [d, "SSE", f"{v:.1f}", "0", "0", "0", "0", "0", ""]
                    for d, v in zip(MARGIN_DAYS, MARGIN_RZYE)
                ],
            )
            results = mcs.run_study(cache, cost_bps=15.0)
            self.assertTrue(results["research_only"])  # type: ignore[index]
            full_tab = results["cross_tab_full"]  # type: ignore[index]
            assert isinstance(full_tab, dict)
            # The mirrored-index signal lands in exactly one state bucket.
            total_n = sum(int(cell["n"]) for cell in full_tab.values())
            self.assertEqual(total_n, 1)
            baseline = results["portfolio_rule_baseline"]  # type: ignore[index]
            assert isinstance(baseline, dict)
            self.assertEqual(baseline["signals"], 1)
            # Conditioned run keys exist regardless of which bucket won.
            self.assertTrue(
                any(str(k).startswith("portfolio_rule_margin_") for k in results)
            )


if __name__ == "__main__":
    unittest.main()
