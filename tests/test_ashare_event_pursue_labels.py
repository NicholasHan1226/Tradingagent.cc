"""Offline tests for the shared Pursue-label attachment helper."""

from __future__ import annotations

import unittest
from unittest import mock

from Ashare import event_pursue_labels as pl


class ConstantsTest(unittest.TestCase):
    def test_frozen_edges_match_study_preregistrations(self) -> None:
        self.assertEqual(pl.PURSUE_VALUATION, "low_le25")
        self.assertEqual(pl.PURSUE_HOLDERTYPE, "incentive")


class AttachTest(unittest.TestCase):
    def test_attaches_and_defaults_unlabeled(self) -> None:
        signals = [
            {"ts_code": "A.SZ", "float_date": "20260402", "x": 1},
            {"ts_code": "B.SZ", "float_date": "20260403", "x": 2},
        ]
        with mock.patch.object(
            pl, "valuation_buckets_for_entries",
            return_value={("A.SZ", "20260402"): "low_le25"},
        ), mock.patch.object(
            pl, "holdertype_buckets_for_entries",
            return_value={("A.SZ", "20260402"): "incentive"},
        ):
            pl.attach_pursue_labels(signals, cache=object())
        self.assertEqual(signals[0]["valuation_bucket"], "low_le25")
        self.assertEqual(signals[0]["holdertype_bucket"], "incentive")
        # missing shard / unknown batch -> unlabeled, not error
        self.assertEqual(signals[1]["valuation_bucket"], "unlabeled")
        self.assertEqual(signals[1]["holdertype_bucket"], "unlabeled")


if __name__ == "__main__":
    unittest.main()
