"""Fully automatic promotion-gate runner for the event-catalyst shadow factor.

Closes the last open loop in the automatic promotion chain:

    SampleJournal (shadow_research facts)
        -> labeled observations
        -> frozen PromotionPolicy evaluation
        -> PromotionDecision appended back to the journal as an auditable,
           idempotent ``promotion_decision`` research fact

Rules:

* the policy is a frozen, pre-registered JSON artifact; the embedded
  ``policy_sha256`` must match the recomputed hash or the run fails closed —
  no threshold can be tuned at run time;
* the runner is deterministic and performs no network, broker, or
  scheduling work; it reads the journal, evaluates, and appends one
  decision record;
* decision records carry no capital authority fields; promotion graduates a
  hypothesis to ``validated_factor`` research status only — execution and
  real-trading locks stay hard-false;
* re-running with identical inputs mints the identical decision receipt, so
  the journal append is idempotent.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

from Ashare.event_catalyst_journal import (
    EVENT_CATALYST_JOURNAL_CONTRACT,
    SHADOW_RESEARCH_STYLE,
)
from Ashare.event_catalyst_promotion import (
    EVENT_CATALYST_PROMOTION_CONTRACT,
    LabeledObservation,
    PromotionDecision,
    PromotionPolicy,
    SCORED_HYPOTHESES,
    evaluate_promotion,
)
from shared.review.sample_journal import SampleJournal


EVENT_CATALYST_RUNNER_CONTRACT = (
    "tradingagent.ashare.event_catalyst_promotion_runner.v1"
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_JOURNAL_PATH = (
    REPO_ROOT / "shared" / "review" / "ashare" / "sample_journal.jsonl"
)
DEFAULT_POLICY_PATH = (
    REPO_ROOT
    / "Ashare"
    / "policies"
    / "event_catalyst_promotion_v1.json"
)

_POLICY_FIELDS = (
    "policy_id",
    "min_labeled_observations",
    "min_distinct_event_clusters",
    "min_time_windows",
    "min_window_hit_rate",
    "cost_per_round_trip",
    "demote_recent_labels",
    "demote_max_hit_rate",
)


class EventCatalystRunnerError(ValueError):
    """Fail-closed runner failure with a stable reason code."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def load_frozen_policy(path: Path | str) -> PromotionPolicy:
    """Load a frozen policy artifact and verify its content hash.

    The artifact embeds the ``policy_sha256`` it was registered with; any
    edit to a threshold without re-registration fails closed as drift.
    """

    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise EventCatalystRunnerError(
            "event_catalyst_runner_policy_unreadable"
        ) from exc
    if not isinstance(raw, Mapping):
        raise EventCatalystRunnerError(
            "event_catalyst_runner_policy_unreadable"
        )
    missing = [name for name in _POLICY_FIELDS if name not in raw]
    if missing:
        raise EventCatalystRunnerError(
            "event_catalyst_runner_policy_field_missing"
        )
    try:
        policy = PromotionPolicy(
            **{name: raw[name] for name in _POLICY_FIELDS}
        )
    except Exception as exc:  # policy contract rejects with reason codes
        raise EventCatalystRunnerError(
            getattr(exc, "reason_code", "event_catalyst_runner_policy_invalid")
        ) from exc
    registered = str(raw.get("policy_sha256") or "").strip().lower()
    if registered != policy.policy_sha256:
        raise EventCatalystRunnerError(
            "event_catalyst_runner_policy_drift"
        )
    return policy


def labels_from_journal_records(
    records: Sequence[Mapping[str, Any]],
) -> tuple[LabeledObservation, ...]:
    """Project journaled shadow_research facts into gate labels.

    Only catalyst-shadow records from the journal bridge are consumed.
    Hypotheses the gate deliberately does not score
    (``reduce_on_event_confirmation``, ``no_signal``) are skipped, and any
    scored record with a missing cluster, window, or return fails closed —
    a malformed fact must never silently shrink the evidence base.
    """

    labels: list[LabeledObservation] = []
    for record in records:
        if not isinstance(record, Mapping):
            raise EventCatalystRunnerError(
                "event_catalyst_runner_label_invalid"
            )
        if str(record.get("record_type") or "") != "shadow_research":
            continue
        if (
            str(record.get("bridge_contract") or "")
            != EVENT_CATALYST_JOURNAL_CONTRACT
        ):
            continue
        hypothesis = str(record.get("positioning_hypothesis") or "")
        if hypothesis not in SCORED_HYPOTHESES:
            continue
        cluster = str(record.get("event_cluster_id") or "").strip()
        as_of = str(record.get("as_of") or "").strip()
        post_return = record.get("post_return")
        if (
            not cluster
            or len(as_of) < 10
            or isinstance(post_return, bool)
            or not isinstance(post_return, (int, float))
        ):
            raise EventCatalystRunnerError(
                "event_catalyst_runner_label_invalid"
            )
        labels.append(
            LabeledObservation(
                hypothesis=hypothesis,
                event_cluster=cluster,
                window_id=as_of[:10],
                signed_post_return=float(post_return),
            )
        )
    return tuple(labels)


def decision_record_from_decision(
    decision: PromotionDecision,
    *,
    label_count: int,
) -> dict[str, Any]:
    """Serialize one gate decision into a journal-ready research fact."""

    if not isinstance(decision, PromotionDecision):
        raise EventCatalystRunnerError(
            "event_catalyst_runner_decision_invalid"
        )
    return {
        "record_type": "promotion_decision",
        "sample_layers": ["shadow_research"],
        "sample_intent": "shadow",
        "style": SHADOW_RESEARCH_STYLE,
        "market": "CN-A",
        "research_contract": EVENT_CATALYST_PROMOTION_CONTRACT,
        "runner_contract": EVENT_CATALYST_RUNNER_CONTRACT,
        "journal_event_id": (
            f"promotion:{decision.decision_receipt_sha256[:32]}"
        ),
        "policy_sha256": decision.policy_sha256,
        "decision_receipt_sha256": decision.decision_receipt_sha256,
        "label_count": int(label_count),
        "as_of": decision.as_of.isoformat(),
        "evidence_available_at": decision.as_of.isoformat(),
        "verdicts": [
            {
                "hypothesis": verdict.hypothesis,
                "decision": verdict.decision,
                "reason_code": verdict.reason_code,
                "labeled_observations": verdict.labeled_observations,
                "distinct_event_clusters": verdict.distinct_event_clusters,
                "time_windows": verdict.time_windows,
                "expectancy_after_cost": verdict.expectancy_after_cost,
                "window_hit_rates": list(verdict.window_hit_rates),
                "recent_hit_rate": verdict.recent_hit_rate,
            }
            for verdict in decision.verdicts
        ],
    }


def run_promotion_gate(
    *,
    journal_path: Path | str = DEFAULT_JOURNAL_PATH,
    policy_path: Path | str = DEFAULT_POLICY_PATH,
    as_of: datetime | None = None,
    journal: SampleJournal | None = None,
    append_decision: bool = True,
) -> dict[str, Any]:
    """Evaluate the frozen policy against journaled facts; return a summary."""

    policy = load_frozen_policy(policy_path)
    if as_of is None:
        as_of = datetime.now(timezone.utc)
    if as_of.tzinfo is None:
        raise EventCatalystRunnerError("event_catalyst_runner_as_of_invalid")
    if journal is None:
        journal = SampleJournal(journal_path)
    events = journal.read_events()
    labels = labels_from_journal_records(events)
    decision = evaluate_promotion(labels, policy=policy, as_of=as_of)
    record = decision_record_from_decision(decision, label_count=len(labels))
    append_status = "skipped"
    if append_decision:
        results = journal.append_samples([record])
        append_status = str(results[0].get("status") or "appended")
    return {
        "contract": EVENT_CATALYST_RUNNER_CONTRACT,
        "as_of": as_of.isoformat(),
        "policy_id": policy.policy_id,
        "policy_sha256": policy.policy_sha256,
        "journal_event_count": len(events),
        "label_count": len(labels),
        "decision_receipt_sha256": decision.decision_receipt_sha256,
        "decision_append_status": append_status,
        "verdicts": record["verdicts"],
        "execution_eligible": False,
        "real_trading_enabled": False,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate the frozen event-catalyst promotion policy "
        "against the A-share SampleJournal and journal the decision."
    )
    parser.add_argument("--journal-path", default=str(DEFAULT_JOURNAL_PATH))
    parser.add_argument("--policy-path", default=str(DEFAULT_POLICY_PATH))
    parser.add_argument(
        "--as-of",
        default=None,
        help="ISO timestamp; defaults to now (UTC).",
    )
    parser.add_argument(
        "--no-append",
        action="store_true",
        help="Evaluate only; do not journal the decision record.",
    )
    args = parser.parse_args(argv)
    as_of = None
    if args.as_of:
        try:
            as_of = datetime.fromisoformat(args.as_of)
        except ValueError:
            print(
                json.dumps(
                    {"error": "event_catalyst_runner_as_of_invalid"},
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
            return 2
    try:
        summary = run_promotion_gate(
            journal_path=args.journal_path,
            policy_path=args.policy_path,
            as_of=as_of,
            append_decision=not args.no_append,
        )
    except Exception as exc:
        reason = getattr(exc, "reason_code", exc.__class__.__name__)
        print(
            json.dumps({"error": reason}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
