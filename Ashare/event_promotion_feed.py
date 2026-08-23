"""SampleJournal -> promotion-gate feed: the readiness wiring for L2->L3.

Discovered gap (2026-08-24 readiness check): the frozen promotion evaluator
(``event_catalyst_promotion.evaluate_promotion``) had full test coverage of
its decision engine, yet NOTHING in the codebase constructed
``LabeledObservation`` objects from production shadow-research rows — if a
signal reached KEEP tomorrow, no machine would have fed it to the gate.

This module is that feed, kept deliberately thin:

  * reads the shared SampleJournal (read-only; never writes any ledger);
  * maps ``shadow_research`` rows to labeled observations — raw
    ``post_return`` passes through unsigned because the gate applies the
    hypothesis direction itself;
  * derives deterministic ``window_id``s from ``as_of`` calendar half-years;
  * sorts labels ascending by ``as_of`` so the gate's "recent" demotion
    window really means most-recent;
  * dry-run prints the deterministic decision receipt WITHOUT promoting
    anything — graduation happens only when every frozen threshold passes
    on its own, and this module owns no thresholds beyond reusing the
    single canonical policy below.

Usage::

    python3 Ashare/event_promotion_feed.py [--journal PATH]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from Ashare.event_catalyst_promotion import (  # noqa: E402
    SCORED_HYPOTHESES,
    LabeledObservation,
    PromotionDecision,
    PromotionPolicy,
    evaluate_promotion,
)

JOURNAL_PATH_DEFAULT = Path("shared/review/ashare/sample_journal.jsonl")
RECORD_TYPE_SHADOW = "shadow_research"

# The single canonical frozen policy (mirrors the contract-test payload for
# event-catalyst-promotion-v1; thresholds live here and nowhere else).
DEFAULT_PROMOTION_POLICY = PromotionPolicy(
    policy_id="event-catalyst-promotion-v1",
    min_labeled_observations=8,
    min_distinct_event_clusters=3,
    min_time_windows=2,
    min_window_hit_rate=0.6,
    cost_per_round_trip=0.002,
    demote_recent_labels=4,
    demote_max_hit_rate=0.25,
)


class PromotionFeedError(RuntimeError):
    """Fail-closed feed failure with a stable reason code."""


def _window_id(as_of_text: str) -> str | None:
    """Calendar half-year bucket from an ISO timestamp ('2026H1')."""
    try:
        moment = datetime.fromisoformat(str(as_of_text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return f"{moment.year}H{1 if moment.month <= 6 else 2}"


def load_shadow_labels(
    journal_path: Path,
) -> tuple[list[LabeledObservation], dict[str, int]]:
    """Parse shadow_research rows into gate-ready labels (read-only).

    Non-conforming rows are skipped and COUNTED, never silently dropped:
    unknown hypotheses, missing/non-finite returns, unparseable timestamps,
    and malformed JSON lines each get their own counter."""
    if not journal_path.exists():
        raise PromotionFeedError(f"journal_missing:{journal_path}")
    labels: list[tuple[datetime, LabeledObservation]] = []
    skipped_not_shadow = 0
    skipped_hypothesis = 0
    skipped_post_return = 0
    skipped_timestamp = 0
    skipped_malformed = 0
    with journal_path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                skipped_malformed += 1
                continue
            if not isinstance(row, dict):
                skipped_malformed += 1
                continue
            if row.get("record_type") != RECORD_TYPE_SHADOW:
                skipped_not_shadow += 1
                continue
            hypothesis = row.get("positioning_hypothesis")
            if hypothesis not in SCORED_HYPOTHESES:
                skipped_hypothesis += 1
                continue
            post_return = row.get("post_return")
            if (
                isinstance(post_return, bool)
                or not isinstance(post_return, (int, float))
            ):
                skipped_post_return += 1
                continue
            cluster = row.get("event_cluster_id")
            window = _window_id(row.get("as_of", ""))
            try:
                moment = datetime.fromisoformat(
                    str(row.get("as_of")).replace("Z", "+00:00")
                )
            except (ValueError, TypeError):
                skipped_timestamp += 1
                continue
            if window is None or not isinstance(cluster, str) or not cluster:
                skipped_timestamp += 1
                continue
            labels.append(
                (
                    moment,
                    LabeledObservation(
                        hypothesis=hypothesis,
                        event_cluster=cluster,
                        window_id=window,
                        signed_post_return=float(post_return),
                    ),
                )
            )
    labels.sort(key=lambda item: item[0])  # recent == tail for the demote window
    stats = {
        "labels": len(labels),
        "skipped_not_shadow": skipped_not_shadow,
        "skipped_unknown_hypothesis": skipped_hypothesis,
        "skipped_missing_post_return": skipped_post_return,
        "skipped_bad_timestamp": skipped_timestamp,
        "skipped_malformed_lines": skipped_malformed,
    }
    return [label for _, label in labels], stats


def run_dry_run(
    journal_path: Path,
    *,
    policy: PromotionPolicy = DEFAULT_PROMOTION_POLICY,
    as_of: datetime | None = None,
) -> PromotionDecision:
    """Feed the real journal through the frozen gate and print the receipt."""
    labels, stats = load_shadow_labels(journal_path)
    moment = as_of if as_of is not None else datetime.now(timezone.utc)
    decision = evaluate_promotion(labels, policy=policy, as_of=moment)
    print("## 晋级通道演练（research_only，不构成任何晋级或授权）")
    print(f"- 台账：{journal_path}；标签映射 {stats}")
    print(f"- 策略：{policy.policy_id} (sha256 {policy.policy_sha256[:12]}…)")
    for verdict in decision.verdicts:
        rates = "/".join(f"{rate:.2f}" for rate in verdict.window_hit_rates) or "n/a"
        expectancy = (
            f"{verdict.expectancy_after_cost * 100:+.3f}%"
            if verdict.expectancy_after_cost is not None
            else "n/a"
        )
        print(
            f"- [{verdict.hypothesis}] {verdict.decision} "
            f"(n={verdict.labeled_observations} 簇={verdict.distinct_event_clusters} "
            f"窗口={verdict.time_windows} 窗口胜率={rates} "
            f"费后期望={expectancy}) 原因={verdict.reason_code}"
        )
    print(f"- 回执 sha256={decision.decision_receipt_sha256}")
    return decision


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--journal", type=Path, default=JOURNAL_PATH_DEFAULT)
    args = parser.parse_args()
    run_dry_run(args.journal)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except PromotionFeedError as exc:
        print(f"PROMOTION_FEED_FAILED {exc}", file=sys.stderr)
        sys.exit(1)
