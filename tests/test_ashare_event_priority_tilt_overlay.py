"""Offline tests for the priority-tilt overlay scan."""

from __future__ import annotations

import unittest

from Ashare.event_priority_tilt_overlay import (
    PURSUE_HOLDERTYPE,
    PURSUE_VALUATION,
    tilt_order,
    tilt_rank,
)


def _signal(code: str, val: str, hold: str):
    return {"ts_code": code, "valuation_bucket": val,
            "holdertype_bucket": hold}


class TiltRankTest(unittest.TestCase):
    def test_frozen_ranking(self) -> None:
        self.assertEqual(
            tilt_rank(_signal("A", PURSUE_VALUATION, PURSUE_HOLDERTYPE)), 0)
        self.assertEqual(tilt_rank(_signal("B", PURSUE_VALUATION, "mid")), 1)
        self.assertEqual(tilt_rank(_signal("C", "high_ge75",
                                           PURSUE_HOLDERTYPE)), 1)
        self.assertEqual(tilt_rank(_signal("D", "unlabeled",
                                           "no_match")), 2)


class TiltOrderTest(unittest.TestCase):
    def test_stable_pair_first_original_order_inside_ranks(self) -> None:
        signals = [
            _signal("R2", "mid", "other"),      # rank 2
            _signal("P1", PURSUE_VALUATION, PURSUE_HOLDERTYPE),   # rank 0
            _signal("S1", PURSUE_VALUATION, "no_match"),          # rank 1
            _signal("R1", "unlabeled", "unlabeled"),               # rank 2
            _signal("P2", PURSUE_VALUATION, PURSUE_HOLDERTYPE),   # rank 0
        ]
        ordered = tilt_order(signals)
        self.assertEqual([s["ts_code"] for s in ordered],
                         ["P1", "P2", "S1", "R2", "R1"])
        # input list untouched
        self.assertEqual(signals[0]["ts_code"], "R2")

    def test_pursue_constants_match_valhold_overlay(self) -> None:
        from Ashare.event_valholdtype_portfolio_overlay import (
            PURSUE_HOLDERTYPE as VH_HOLD,
            PURSUE_VALUATION as VH_VAL,
        )
        self.assertEqual((PURSUE_VALUATION, PURSUE_HOLDERTYPE),
                         (VH_VAL, VH_HOLD))


if __name__ == "__main__":
    unittest.main()
