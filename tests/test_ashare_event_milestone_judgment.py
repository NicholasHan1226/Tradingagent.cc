"""Offline tests for the milestone-judgment evaluator."""

from __future__ import annotations

import unittest

from Ashare.event_milestone_judgment import (
    MilestoneJudgmentError,
    dedupe_samples,
    judge,
    rule_arm_sample,
    samples_for_preset,
    stable_event_key,
)


def _sample(date: str, bps: float, regime: str = "weak",
            ratio: str | None = "lt3") -> dict:
    return {
        "event_date": date,
        "post_return_bps": bps,
        "regime": regime,
        "ratio_bucket": ratio,
    }


class RuleArmSampleTest(unittest.TestCase):
    def test_keeps_only_weak_non_band_tagged(self) -> None:
        self.assertTrue(rule_arm_sample(_sample("2026-01-05", 100.0)))
        self.assertFalse(rule_arm_sample(_sample("2026-01-05", 100.0, "strong")))
        self.assertFalse(
            rule_arm_sample(_sample("2026-01-05", 100.0, "weak", "3-5%"))
        )
        self.assertFalse(rule_arm_sample(_sample("2026-01-05", 100.0, "weak", None)))


class JudgeTest(unittest.TestCase):
    def test_keep_requires_all_three_legs(self) -> None:
        samples = [
            _sample(f"2026-01-{d:02d}", 200.0) for d in range(1, 16)
        ] + [
            _sample(f"2026-02-{d:02d}", 50.0) for d in range(1, 16)
        ]
        result = judge(samples, gate_n=30)
        self.assertEqual(result["verdict"], "keep")
        self.assertEqual(result["n"], 30)
        self.assertGreater(result["mean_net_bps"], 0)
        self.assertEqual(result["win_net"], 1.0)
        self.assertTrue(result["halves_consistent"])

    def test_two_half_inconsistency_is_gray_not_keep(self) -> None:
        # strong uniform early half; late half still mostly wins but its
        # four large losses drag the late mean negative (#571-like shape)
        samples = [
            _sample(f"2026-01-{d:02d}", 400.0) for d in range(1, 16)
        ] + [
            _sample(f"2026-02-{d:02d}", -600.0 if d % 3 == 0 else 150.0)
            for d in range(1, 16)
        ]
        result = judge(samples, gate_n=30)
        self.assertGreater(result["mean_net_bps"], 0)
        self.assertGreaterEqual(result["win_net"], 0.52)
        self.assertEqual(result["verdict"], "gray")
        self.assertFalse(result["halves_consistent"])

    def test_fail_on_negative_mean_or_low_win(self) -> None:
        bad_mean = [_sample(f"2026-01-{d:02d}", -10.0) for d in range(1, 31)]
        self.assertEqual(judge(bad_mean, gate_n=30)["verdict"], "fail")
        low_win = [
            _sample(f"2026-01-{d:02d}", 300.0 if d <= 13 else -5.0)
            for d in range(1, 31)
        ]
        # mean > 0 but win rate 13/30 <= 0.45 -> fail
        self.assertEqual(judge(low_win, gate_n=30)["verdict"], "fail")

    def test_gray_band_between_thresholds(self) -> None:
        # evenly interleaved: win .500 sits strictly between FAIL_WIN and
        # KEEP_WIN while both halves stay positive -> gray, not keep/fail
        samples = [
            _sample(f"2026-01-{i + 1:02d}", 400.0 if i % 2 == 0 else -1.0)
            for i in range(30)
        ]
        result = judge(samples, gate_n=30)
        self.assertGreater(result["win_net"], 0.45)
        self.assertLess(result["win_net"], 0.52)
        self.assertTrue(result["halves_consistent"])
        self.assertEqual(result["verdict"], "gray")

    def test_insufficient_below_gate(self) -> None:
        samples = [_sample(f"2026-01-{d:02d}", 500.0) for d in range(1, 11)]
        result = judge(samples, gate_n=30)
        self.assertEqual(result["verdict"], "insufficient")

    def test_cost_deduction_applied(self) -> None:
        flat = [_sample(f"2026-01-{d:02d}", 15.0) for d in range(1, 31)]
        result = judge(flat, gate_n=30, cost_bps=15.0)
        # gross +15 every trade -> net exactly 0 -> fail via mean <= 0
        self.assertEqual(result["mean_net_bps"], 0.0)
        self.assertEqual(result["verdict"], "fail")


class DedupeSamplesTest(unittest.TestCase):
    """#586: lockup ids embed a per-run tracker sequence, so the same
    disclosure event re-appears under fresh ids across runs — and the
    per-run export itself carries one row per rediscovery.  Judgment
    input must be one row per unique event."""

    @staticmethod
    def _eid(seq: int, symbol: str = "600426.SH",
             day: str = "2026-04-01") -> str:
        return (
            "lockup:cn.dataset.share_float:"
            f"tracker-{seq:06d}:{symbol}:{day}:其他激励对象:股权激励限售流通"
        )

    @staticmethod
    def _row(event_id: str, bps: float = 100.0,
             date: str = "2026-04-01", symbol: str = "600426.SH") -> dict:
        return {
            "event_id": event_id,
            "event_date": date,
            "post_return_bps": bps,
            "regime": "weak",
            "ratio_bucket": "lt3",
        }

    def test_rows_without_event_id_are_never_merged(self) -> None:
        rows = [{"event_date": "2026-04-01", "post_return_bps": 100.0},
                {"event_date": "2026-04-02", "post_return_bps": -30.0}]
        self.assertEqual(len(dedupe_samples(rows)), 2)

    def test_same_event_across_run_sequences_collapses(self) -> None:
        rows = [self._row(self._eid(577)), self._row(self._eid(1644))]
        self.assertEqual(stable_event_key(rows[0]["event_id"]),
                         stable_event_key(rows[1]["event_id"]))
        self.assertEqual(len(dedupe_samples(rows)), 1)

    def test_distinct_events_survive(self) -> None:
        rows = [
            self._row(self._eid(1)),
            self._row(self._eid(2, symbol="600050.SH"),
                      symbol="600050.SH"),
            self._row(
                self._eid(3, day="2026-07-06"), date="2026-07-06",
            ),
        ]
        deduped = dedupe_samples(rows)
        self.assertEqual(len(deduped), 3)

    def test_lockup_rule_preset_judges_unique_events(self) -> None:
        state = {"labeled_outcomes": {"lockup": [
            self._row(self._eid(1), 200.0),
            # rediscovery of the same event under a fresh sequence number
            self._row(self._eid(999), 200.0),
            self._row(self._eid(2, day="2026-02-05"), -50.0,
                      date="2026-02-05"),
        ]}}
        picked = samples_for_preset(state, "lockup_rule")
        self.assertEqual(len(picked), 2)
        result = judge(picked, gate_n=2, cost_bps=15.0)
        self.assertEqual(result["n"], 2)


class SamplesForPresetTest(unittest.TestCase):
    def test_lockup_rule_filters_and_raw_passes_through(self) -> None:
        state = {
            "labeled_outcomes": {
                "lockup": [
                    _sample("2026-01-05", 100.0),
                    _sample("2026-01-06", 100.0, "strong"),
                    _sample("2026-01-07", 100.0, "weak", "3-5%"),
                ],
                "other_bucket": [_sample("2026-01-08", 1.0)],
            },
            "prewindow_samples": {
                "earnings_pos": [{"event_date": "2026-08-05",
                                  "pre_return_bps": -476.2}],
            },
        }
        picked = samples_for_preset(state, "lockup_rule")
        self.assertEqual(len(picked), 1)
        self.assertEqual(picked[0]["event_date"], "2026-01-05")
        raw = samples_for_preset(state, "raw:other_bucket")
        self.assertEqual(len(raw), 1)

    def test_earnings_read_labeled_outcomes(self) -> None:
        # Earnings presets judge on post-event outcomes; the tracker's
        # prewindow export (pre_return_bps, positive signal only) is
        # descriptive and must never be the judgment source.
        state = {
            "labeled_outcomes": {
                "earnings_pos": [_sample("2026-08-05", 30.0)],
                "earnings_neg": [_sample("2026-08-06", -12.0)],
            },
        }
        self.assertEqual(
            samples_for_preset(state, "earnings_neg")[0]["post_return_bps"],
            -12.0,
        )
        self.assertEqual(
            samples_for_preset(state, "earnings_pos")[0]["post_return_bps"],
            30.0,
        )

    def test_earnings_judge_works_without_prewindow_export(self) -> None:
        # Rehearsal regression (#582): real exports carry prewindow rows for
        # earnings_pos only, so judgment must not depend on that key at all.
        state = {
            "labeled_outcomes": {
                "earnings_neg": [
                    _sample(f"2026-08-{d:02d}", 60.0) for d in range(1, 13)
                ],
            },
        }
        picked = samples_for_preset(state, "earnings_neg")
        result = judge(picked, gate_n=10, cost_bps=15.0)
        self.assertEqual(result["verdict"], "keep")

    def test_empty_and_unknown_fail_closed(self) -> None:
        with self.assertRaises(MilestoneJudgmentError):
            samples_for_preset({}, "lockup_rule")
        with self.assertRaises(MilestoneJudgmentError):
            samples_for_preset({}, "no_such_preset")


if __name__ == "__main__":
    unittest.main()
