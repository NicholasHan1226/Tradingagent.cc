"""Offline tests for the Pursue-pair (valuation x holdertype) overlay."""

from __future__ import annotations

import unittest

from Ashare.event_valholdtype_portfolio_overlay import (
    PURSUE_HOLDERTYPE,
    PURSUE_VALUATION,
    overlay_arms,
)


def _signal(code: str, regime: str, ratio: float, val: str, hold: str):
    return {
        "ts_code": code,
        "entry_day": "20260302",
        "exit_day": "20260331",
        "regime": regime,
        "float_ratio": ratio,
        "valuation_bucket": val,
        "holdertype_bucket": hold,
    }


class OverlayArmsTest(unittest.TestCase):
    def test_four_arms_counts_with_rule_and_labels(self) -> None:
        signals = [
            # rule arm = weak regime AND float_ratio outside the 3-5% band
            _signal("A1.SZ", "weak", 2.0, PURSUE_VALUATION, PURSUE_HOLDERTYPE),
            _signal("A2.SZ", "weak", 2.0, PURSUE_VALUATION, "no_match"),
            _signal("A3.SZ", "weak", 2.0, "mid", PURSUE_HOLDERTYPE),
            _signal("A4.SZ", "weak", 4.0, PURSUE_VALUATION, PURSUE_HOLDERTYPE),
            _signal("A5.SZ", "strong", 2.0, PURSUE_VALUATION, PURSUE_HOLDERTYPE),
            _signal("A6.SZ", "weak", 2.0, "unlabeled", "unlabeled"),
        ]
        arms = overlay_arms(signals)
        self.assertEqual(
            [s["ts_code"] for s in arms["rule"]],
            ["A1.SZ", "A2.SZ", "A3.SZ", "A6.SZ"],
        )
        self.assertEqual([s["ts_code"] for s in arms["rule_val"]],
                         ["A1.SZ", "A2.SZ"])
        self.assertEqual([s["ts_code"] for s in arms["rule_holdertype"]],
                         ["A1.SZ", "A3.SZ"])
        # pair requires BOTH labels; unlabeled counts as excluded
        self.assertEqual(arms["rule_pair"], [signals[0]])

    def test_unlabeled_excluded_from_pursue_arms(self) -> None:
        signals = [
            _signal("B1.SZ", "weak", 6.0, "unlabeled", PURSUE_HOLDERTYPE),
            _signal("B2.SZ", "weak", 6.0, PURSUE_VALUATION, "unlabeled"),
        ]
        arms = overlay_arms(signals)
        self.assertEqual(len(arms["rule"]), 2)
        # each single-condition arm keeps only the side that IS labeled
        self.assertEqual([s["ts_code"] for s in arms["rule_val"]], ["B2.SZ"])
        self.assertEqual([s["ts_code"] for s in arms["rule_holdertype"]],
                         ["B1.SZ"])
        # pair requires BOTH labels; either side unlabeled excludes
        self.assertEqual(arms["rule_pair"], [])

    def test_pursue_constants_frozen(self) -> None:
        # frozen ex-ante edges from the two study engines' preregistrations
        self.assertEqual(PURSUE_VALUATION, "low_le25")
        self.assertEqual(PURSUE_HOLDERTYPE, "incentive")


if __name__ == "__main__":
    unittest.main()
