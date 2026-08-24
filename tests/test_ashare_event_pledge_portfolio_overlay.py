"""Offline tests for the pledge portfolio overlay scan."""

from __future__ import annotations

import unittest

from Ashare.event_pledge_portfolio_overlay import (
    drop_high_pledge,
    overlay_arms,
)


def _signal(code: str, regime: str, ratio: float, bucket: str):
    return {
        "ts_code": code,
        "entry_day": "20260302",
        "exit_day": "20260331",
        "regime": regime,
        "float_ratio": ratio,
        "pledge_bucket": bucket,
    }


class DropHighPledgeTest(unittest.TestCase):
    def test_drops_only_high_keeps_order(self) -> None:
        signals = [
            _signal("000001.SZ", "weak", 2.0, "mid"),
            _signal("000002.SZ", "weak", 2.0, "high"),
            _signal("000003.SZ", "weak", 2.0, "no_snapshot"),
            _signal("000004.SZ", "weak", 2.0, "high"),
            _signal("000005.SZ", "weak", 2.0, "low"),
        ]
        kept = drop_high_pledge(signals)
        self.assertEqual(
            [s["ts_code"] for s in kept],
            ["000001.SZ", "000003.SZ", "000005.SZ"],
        )
        # reference list untouched (pure filter)
        self.assertEqual(len(signals), 5)


class OverlayArmsTest(unittest.TestCase):
    def test_four_arms_counts_with_rule_filter(self) -> None:
        signals = [
            # rule arm = weak regime AND float_ratio outside the 3-5% band
            _signal("A1.SZ", "weak", 2.0, "mid"),      # rule
            _signal("A2.SZ", "weak", 4.0, "low"),      # weak but avoided band
            _signal("A3.SZ", "strong", 8.0, "mid"),    # strong regime
            _signal("A4.SZ", "weak", 6.0, "high"),     # rule, excluded by overlay
            _signal("A5.SZ", "unknown", 2.0, "high"),  # not rule anyway
        ]
        arms = overlay_arms(signals)
        self.assertEqual(arms["all"], signals)
        self.assertEqual(len(arms["all_ex_high"]), 3)
        self.assertEqual([s["ts_code"] for s in arms["rule"]], ["A1.SZ", "A4.SZ"])
        self.assertEqual(
            [s["ts_code"] for s in arms["rule_ex_high"]], ["A1.SZ"]
        )


if __name__ == "__main__":
    unittest.main()
