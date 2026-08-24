"""Offline tests for the dividend stance conditioning layer (panel #14)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from Ashare.event_dividend_stance_study import (
    DIVIDEND_BUCKETS,
    DividendStudyError,
    _double_low,
    attach_dividend_stance,
    load_dividend_plan_index,
    _stance_bucket,
)


def _plan(ann: str, split: bool = False, cash: bool = False) -> list:
    return [ann, split, cash]


class LoadDividendPlanIndexTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.ddir = self.tmp / "dividend_daily"
        self.ddir.mkdir()

    def test_empty_cache_fails_closed(self) -> None:
        empty = Path(tempfile.mkdtemp())
        with self.assertRaises(DividendStudyError):
            load_dividend_plan_index(empty)

    def test_stage_rows_aggregate_plan_level(self) -> None:
        # A paying plan whose first stage rows carry empty amounts must
        # still classify as cash-bearing with the earliest ann_date.
        (self.ddir / "20260421.csv").write_text(
            "ts_code,end_date,ann_date,div_proc,stk_div,cash_div\n"
            "000952.SZ,20171231,20260421,股东大会未通过,,\n",
            encoding="utf-8",
        )
        (self.ddir / "20260517.csv").write_text(
            "ts_code,end_date,ann_date,div_proc,stk_div,cash_div\n"
            "000952.SZ,20171231,20260517,实施,,0.11\n"
            "000001.SZ,20171231,20260517,实施,0.0,\n",
            encoding="utf-8",
        )
        index = load_dividend_plan_index(self.tmp)
        plan = index["000952.SZ"]["20171231"]
        self.assertEqual(plan[0], "20260421")  # min ann kept
        self.assertTrue(plan[2])  # cash bearing ORed across stages
        self.assertFalse(plan[1])
        # all-zero plan classifies as no-dist bearing flags
        zero = index["000001.SZ"]["20171231"]
        self.assertEqual(zero[1:], [False, False])

    def test_split_flag_wins_over_cash_in_aggregation(self) -> None:
        (self.ddir / "20260601.csv").write_text(
            "ts_code,end_date,ann_date,div_proc,stk_div,cash_div\n"
            "600001.SH,20251231,20260601,预案,0.0,\n"
            "600002.SH,20251231,20260601,预案,,0.05\n",
            encoding="utf-8",
        )
        (self.ddir / "20260701.csv").write_text(
            "ts_code,end_date,ann_date,div_proc,stk_div,cash_div\n"
            "600001.SH,20251231,20260701,实施,10.0,0.2\n",
            encoding="utf-8",
        )
        index = load_dividend_plan_index(self.tmp)
        # 600001: first row zero, implementation row bears split AND cash
        self.assertEqual(index["600001.SH"]["20251231"],
                         ["20260601", True, True])
        # 600002: cash-only
        self.assertEqual(index["600002.SH"]["20251231"],
                         ["20260601", False, True])


class StanceBucketTest(unittest.TestCase):
    def test_no_records_when_no_plan_in_window(self) -> None:
        bucket, staleness = _stance_bucket({}, "20260820")
        self.assertEqual(bucket, "no_records")
        self.assertIsNone(staleness)

    def test_latest_plan_wins_on_multiple_plans(self) -> None:
        plans = {
            "20251231": _plan("20260410", cash=True),
            "20260630": _plan("20260815"),  # no_dist announced later
        }
        bucket, _ = _stance_bucket(plans, "20260820")
        self.assertEqual(bucket, "no_dist")

    def test_window_edges_start_inclusive_entry_exclusive(self) -> None:
        plans_edge = {"20251231": _plan("20250821", cash=True)}
        # exactly 365 days before entry -> inside window
        bucket, _ = _stance_bucket(plans_edge, "20260821")
        self.assertEqual(bucket, "cash_only")
        # announcement ON entry day is excluded (look-ahead guard)
        plans_entry = {"20251231": _plan("20260821", cash=True)}
        bucket, _ = _stance_bucket(plans_entry, "20260821")
        self.assertEqual(bucket, "no_records")

    def test_unsorted_plan_dict_handled_defensively(self) -> None:
        plans = {
            "A": _plan("20260810", split=True),
            "B": _plan("20260301", cash=True),
        }
        bucket, staleness = _stance_bucket(plans, "20260820")
        self.assertEqual(bucket, "split")
        self.assertEqual(staleness, 10)

    def test_bucket_precedence_split_over_cash(self) -> None:
        plans = {"20251231": _plan("20260801", split=True, cash=True)}
        bucket, _ = _stance_bucket(plans, "20260820")
        self.assertEqual(bucket, "split")


class AttachAndGateTest(unittest.TestCase):
    def test_attach_labels_and_defaults(self) -> None:
        index = {
            "000001.SZ": {"20251231": _plan("20260801", cash=True)},
            "000002.SZ": {},
        }
        signals = [
            {"ts_code": "000001.SZ", "entry_day": "20260820"},
            {"ts_code": "000002.SZ", "entry_day": "20260820"},
        ]
        stats = attach_dividend_stance(signals, index)
        self.assertEqual(signals[0]["dividend_bucket"], "cash_only")
        self.assertEqual(signals[0]["dividend_stance_lag_days"], 19)
        self.assertEqual(signals[1]["dividend_bucket"], "no_records")
        self.assertIsNone(signals[1]["dividend_stance_lag_days"])
        self.assertEqual(stats["attached"], 2)
        for bucket in DIVIDEND_BUCKETS:
            self.assertIn(bucket, stats)

    def test_double_low_gate_requires_both_legs_and_n(self) -> None:
        cell_bad = {"n": 50, "mean_net_bps": -100.0, "win_rate": 0.40}
        base = {"n": 357, "mean_net_bps": 146.9, "win_rate": 0.585}
        self.assertTrue(_double_low(cell_bad, base))
        # win-rate leg flips -> gate fails
        cell_win_high = {"n": 50, "mean_net_bps": -100.0, "win_rate": 0.70}
        self.assertFalse(_double_low(cell_win_high, base))
        # mean leg flips -> gate fails
        cell_mean_high = {"n": 50, "mean_net_bps": 300.0, "win_rate": 0.40}
        self.assertFalse(_double_low(cell_mean_high, base))
        # n below family gate -> fails even if legs hold
        cell_small = {"n": 29, "mean_net_bps": -100.0, "win_rate": 0.40}
        self.assertFalse(_double_low(cell_small, base))


if __name__ == "__main__":
    unittest.main()
