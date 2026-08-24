"""Offline tests for the unlock-batch holder-type (panel #16) study."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from Ashare.event_unlock_holdertype_study import (
    HolderTypeStudyError,
    _double_low,
    attach_holdertype_bucket,
    holdertype_bucket,
    load_holdertype_index,
)


def _signal(code: str, float_date: str, entry_day: str | None = None) -> dict:
    return {
        "ts_code": code,
        "float_date": float_date,
        "entry_day": entry_day or float_date,
    }


class HoldertypeBucketTest(unittest.TestCase):
    def test_placement_precedence_over_all(self) -> None:
        mixed = {"定增股份", "首发原始股", "股权激励限售流通"}
        self.assertEqual(holdertype_bucket(mixed), "placement")

    def test_insider_over_incentive(self) -> None:
        self.assertEqual(
            holdertype_bucket({"首发原始股", "股权激励限售流通"}), "insider")

    def test_public_placement_counts_as_placement(self) -> None:
        self.assertEqual(holdertype_bucket({"公开增发一般股份"}), "placement")
        self.assertEqual(holdertype_bucket({"首发战略配售股份"}), "insider")

    def test_incentive_only(self) -> None:
        self.assertEqual(holdertype_bucket({"股权激励限售流通"}), "incentive")

    def test_other_legacy_fallback_and_empty_no_match(self) -> None:
        self.assertEqual(holdertype_bucket({"股权分置限售股份"}), "other_legacy")
        self.assertEqual(holdertype_bucket({"其他类型"}), "other_legacy")
        self.assertEqual(holdertype_bucket(set()), "no_match")


class LoadHoldertypeIndexTest(unittest.TestCase):
    def _write_csv(self, path: Path, rows: list[list[str]]) -> None:
        header = ("ts_code,ann_date,float_date,float_share,float_ratio,"
                  "holder_name,share_type\n")
        path.write_text(header + "".join(",".join(r) + "\n" for r in rows),
                        encoding="utf-8")

    def test_validity_filter_mirrors_load_events(self) -> None:
        cache = Path(tempfile.mkdtemp())
        self._write_csv(cache / "share_float.csv", [
            ["000001.SZ", "20200101", "20210101", "100", "1.0", "h", "定增股份"],
            # inverted: float before ann -> dropped
            ["000001.SZ", "20210101", "20200201", "100", "1.0", "h", "首发原始股"],
            # before SIM_START -> dropped
            ["000001.SZ", "20160101", "20170201", "100", "1.0", "h", "首发原始股"],
            # bad ratio -> dropped
            ["000001.SZ", "20200101", "20210301", "100", "x", "h", "首发原始股"],
            # same batch second type kept in the set
            ["000001.SZ", "20200105", "20210101", "50", "0.5", "h",
             "股权激励限售流通"],
        ])
        index = load_holdertype_index(cache)
        self.assertEqual(index,
                         {("000001.SZ", "20210101"):
                          {"定增股份", "股权激励限售流通"}})

    def test_missing_cache_fails_closed(self) -> None:
        with self.assertRaises(HolderTypeStudyError) as ctx:
            load_holdertype_index(Path(tempfile.mkdtemp()))
        self.assertEqual(str(ctx.exception), "cache_missing:share_float.csv")


class AttachHoldertypeBucketTest(unittest.TestCase):
    def test_labels_key_on_float_date_not_entry_day(self) -> None:
        signals = [_signal("000001.SZ", "20210101", entry_day="20210104")]
        stats = attach_holdertype_bucket(
            signals, {("000001.SZ", "20210101"): {"定增股份"}})
        self.assertEqual(signals[0]["holdertype_bucket"], "placement")
        self.assertEqual(stats["placement"], 1)

    def test_unknown_batch_counts_no_match(self) -> None:
        signals = [_signal("000002.SZ", "20210101")]
        stats = attach_holdertype_bucket(signals, {})
        self.assertEqual(signals[0]["holdertype_bucket"], "no_match")
        self.assertEqual(stats["no_match"], 1)
        self.assertEqual(stats["attached"], 1)


class DoubleLowGateTest(unittest.TestCase):
    def test_gate_legs_and_threshold(self) -> None:
        baseline = {"n": 300, "mean_net_bps": 100.0, "win_rate": 0.55}
        passing = {"n": 30, "mean_net_bps": 99.9, "win_rate": 0.549}
        thin_n = {"n": 29, "mean_net_bps": 99.9, "win_rate": 0.549}
        mean_high = {"n": 30, "mean_net_bps": 150.0, "win_rate": 0.500}
        self.assertTrue(_double_low(passing, baseline))
        self.assertFalse(_double_low(thin_n, baseline))
        self.assertFalse(_double_low(mean_high, baseline))


if __name__ == "__main__":
    unittest.main()
