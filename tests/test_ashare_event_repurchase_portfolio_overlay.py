"""Offline tests for the repurchase portfolio overlay scan."""

from __future__ import annotations

import unittest

from Ashare.event_repurchase_portfolio_overlay import (
    keep_active,
    overlay_arms,
)


def _signal(code: str, regime: str, ratio: float, bucket: str):
    return {
        "ts_code": code,
        "entry_day": "20260302",
        "exit_day": "20260331",
        "regime": regime,
        "float_ratio": ratio,
        "repurchase_bucket": bucket,
    }


class KeepActiveTest(unittest.TestCase):
    def test_keeps_only_active_in_order(self) -> None:
        signals = [
            _signal("000001.SZ", "weak", 2.0, "active"),
            _signal("000002.SZ", "weak", 2.0, "no_records"),
            _signal("000003.SZ", "weak", 2.0, "done"),
            _signal("000004.SZ", "weak", 2.0, "active"),
        ]
        kept = keep_active(signals)
        self.assertEqual(
            [s["ts_code"] for s in kept], ["000001.SZ", "000004.SZ"]
        )
        # reference list untouched (pure filter)
        self.assertEqual(len(signals), 4)


class OverlayArmsTest(unittest.TestCase):
    def test_four_arms_counts_with_rule_filter(self) -> None:
        signals = [
            # rule arm = weak regime AND float_ratio outside the 3-5% band
            _signal("A1.SZ", "weak", 2.0, "active"),       # rule + active
            _signal("A2.SZ", "weak", 4.0, "active"),       # avoided band
            _signal("A3.SZ", "strong", 8.0, "active"),     # strong regime
            _signal("A4.SZ", "weak", 6.0, "no_records"),   # rule, not active
            _signal("A5.SZ", "unknown", 2.0, "done"),      # not rule anyway
        ]
        arms = overlay_arms(signals)
        self.assertEqual(arms["all"], signals)
        self.assertEqual(
            [s["ts_code"] for s in arms["all_active"]], ["A1.SZ", "A2.SZ", "A3.SZ"]
        )
        self.assertEqual([s["ts_code"] for s in arms["rule"]], ["A1.SZ", "A4.SZ"])
        self.assertEqual([s["ts_code"] for s in arms["rule_active"]], ["A1.SZ"])


if __name__ == "__main__":
    unittest.main()
