"""Offline tests for the SampleJournal -> promotion-gate feed."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from Ashare import event_promotion_feed as feed
from Ashare.event_catalyst_promotion import (
    LabeledObservation,
    PromotionPolicy,
)


def _row(hypothesis="hold_through_event", post=0.03, as_of="2026-08-23T10:00:00+00:00",
         cluster="lockup:evt:600050.SH", record_type="shadow_research"):
    return {
        "record_type": record_type,
        "positioning_hypothesis": hypothesis,
        "post_return": post,
        "event_cluster_id": cluster,
        "as_of": as_of,
    }


class WindowIdTest(unittest.TestCase):
    def test_half_year_buckets_and_garbage(self) -> None:
        self.assertEqual(feed._window_id("2026-02-01T00:00:00+00:00"), "2026H1")
        self.assertEqual(feed._window_id("2026-07-15T00:00:00+00:00"), "2026H2")
        self.assertIsNone(feed._window_id("not-a-date"))


class LoadShadowLabelsTest(unittest.TestCase):
    def test_maps_sorts_and_counts_skips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "journal.jsonl"
            rows = [
                _row(post=0.05, as_of="2026-07-01T00:00:00+00:00"),
                "{not json",                                   # malformed
                _row(record_type="paper_fill"),                 # not shadow
                _row(hypothesis="reduce_on_event_confirmation"),  # unscored
                _row(post=None),                                # no return
                _row(as_of="garbage"),                          # bad timestamp
                _row(post=-0.02, as_of="2026-02-01T00:00:00+00:00"),
            ]
            path.write_text(
                "\n".join(
                    r if isinstance(r, str) else json.dumps(r) for r in rows
                )
                + "\n",
                encoding="utf-8",
            )
            labels, stats = feed.load_shadow_labels(path)
            self.assertEqual(len(labels), 2)
            # Ascending as_of: the June row must come last (recent == tail).
            self.assertEqual(labels[0].window_id, "2026H1")
            self.assertEqual(labels[0].signed_post_return, -0.02)
            self.assertEqual(labels[1].signed_post_return, 0.05)
            self.assertEqual(labels[1].window_id, "2026H2")
            self.assertEqual(stats["skipped_malformed_lines"], 1)
            self.assertEqual(stats["skipped_not_shadow"], 1)
            self.assertEqual(stats["skipped_unknown_hypothesis"], 1)
            self.assertEqual(stats["skipped_missing_post_return"], 1)
            self.assertEqual(stats["skipped_bad_timestamp"], 1)

    def test_missing_journal_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(feed.PromotionFeedError):
                feed.load_shadow_labels(Path(tmp) / "absent.jsonl")


class DryRunTest(unittest.TestCase):
    def test_insufficient_real_shaped_labels_keep_shadow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "journal.jsonl"
            rows = [_row(cluster=f"evt:{i}", post=0.01 * i) for i in range(3)]
            path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
            decision = feed.run_dry_run(path, as_of=datetime(2026, 8, 24, tzinfo=timezone.utc))
            verdict = decision.verdicts[1]  # hold_through_event
            self.assertEqual(verdict.decision, "keep_shadow")
            self.assertEqual(verdict.reason_code, "insufficient_labeled_observations")

    def test_wiring_reaches_graduation_when_all_gates_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "journal.jsonl"
            rows = []
            for i in range(8):
                # Every label hypothesis-correct (positive raw return) so no
                # demotion window trips; windows split across half-years.
                rows.append(
                    _row(
                        cluster=f"evt:{i % 4}",
                        post=0.10,
                        as_of=(
                            "2026-02-01T00:00:00+00:00" if i < 4
                            else "2026-09-01T00:00:00+00:00"
                        ),
                    )
                )
            path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
            moment = datetime(2026, 9, 2, tzinfo=timezone.utc)
            first = feed.run_dry_run(path, as_of=moment)
            second = feed.run_dry_run(path, as_of=moment)
            verdict = first.verdicts[1]
            self.assertEqual(verdict.decision, "graduate_to_validated_factor")
            # Deterministic: same inputs -> identical receipt.
            self.assertEqual(
                first.decision_receipt_sha256, second.decision_receipt_sha256
            )

    def test_default_policy_matches_contract_payload(self) -> None:
        policy = feed.DEFAULT_PROMOTION_POLICY
        assert isinstance(policy, PromotionPolicy)
        self.assertEqual(policy.policy_id, "event-catalyst-promotion-v1")
        self.assertEqual(policy.min_time_windows, 2)
        self.assertAlmostEqual(policy.cost_per_round_trip, 0.002)


if __name__ == "__main__":
    unittest.main()
