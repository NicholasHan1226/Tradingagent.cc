"""Automatic, pre-registered promotion gate for the event-catalyst factor.

Nicholas has explicitly required promotion to run unattended.  This module
implements that requirement as *automation of a frozen rule-set*, not as
model discretion: every threshold lives in a versioned, content-addressed
``PromotionPolicy``; the evaluator is a pure deterministic function over
labeled shadow observations; and the machine can only execute what the frozen
policy already says — it cannot invent criteria, relax a threshold, or
overlook missing evidence.

Hard boundaries that this module never crosses:

* the promotion target is research-ladder graduation only
  (``shadow`` -> ``validated_factor``); it grants no candidate, execution,
  training, promotion-beyond-research, risk, position, order, or real-trading
  authority;
* ``real_trading_enabled`` is always ``False`` and live transition is outside
  this module's scope forever;
* auto-demotion is symmetric and automatic: if realized labels turn against
  a hypothesis, the gate demotes without waiting for anyone.

Statistical discipline baked into the gate:

* effective sample size counts distinct *event clusters*, not rows, because
  symbols sharing one event are cross-sectionally correlated;
* the labeled history must split into at least two disjoint time windows and
  the hypothesis must hold its sign in every window (walk-forward
  consistency);
* expectancy must clear zero after the policy's per-round-trip cost
  assumption;
* evaluation is deterministic: same labels + same policy -> same decision
  and same receipt.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import math
from types import MappingProxyType
from typing import Any, Mapping, Sequence


EVENT_CATALYST_PROMOTION_CONTRACT = (
    "tradingagent.ashare.event_catalyst_promotion.v1"
)

DECISIONS = (
    "keep_shadow",
    "graduate_to_validated_factor",
    "auto_demote",
)
_HYPOTHESIS_DIRECTION = MappingProxyType(
    {
        # Expected sign of post-event return for the hypothesis to be right.
        "realize_on_event": -1.0,
        "hold_through_event": +1.0,
        "reduce_on_event_confirmation": 0.0,  # confirmation-gated; not scored
        "no_signal": 0.0,  # absence of signal; not scored
    }
)
SCORED_HYPOTHESES = tuple(
    hypothesis
    for hypothesis, direction in _HYPOTHESIS_DIRECTION.items()
    if direction != 0.0
)

_SHA256_HEX = frozenset("0123456789abcdef")


class EventCatalystPromotionError(ValueError):
    """Fail-closed gate failure with a stable reason code."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise EventCatalystPromotionError(
            "event_catalyst_promotion_payload_not_canonical"
        ) from exc


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _text(value: object, reason: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\x00" in value
    ):
        raise EventCatalystPromotionError(reason)
    return value


def _aware(value: object, reason: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise EventCatalystPromotionError(reason)
    return value


@dataclass(frozen=True)
class PromotionPolicy:
    """Frozen, pre-registered promotion thresholds; versioned and hashed."""

    policy_id: str
    min_labeled_observations: int
    min_distinct_event_clusters: int
    min_time_windows: int
    min_window_hit_rate: float
    cost_per_round_trip: float
    demote_recent_labels: int
    demote_max_hit_rate: float

    def __post_init__(self) -> None:
        _text(self.policy_id, "event_catalyst_policy_id_invalid")
        for field_name in (
            "min_labeled_observations",
            "min_distinct_event_clusters",
            "min_time_windows",
            "demote_recent_labels",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise EventCatalystPromotionError(
                    "event_catalyst_policy_threshold_invalid"
                )
        if self.min_time_windows < 2:
            raise EventCatalystPromotionError(
                "event_catalyst_policy_windows_invalid"
            )
        for field_name in (
            "min_window_hit_rate",
            "cost_per_round_trip",
            "demote_max_hit_rate",
        ):
            value = getattr(self, field_name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or float(value) < 0
            ):
                raise EventCatalystPromotionError(
                    "event_catalyst_policy_threshold_invalid"
                )
        if not 0 <= float(self.min_window_hit_rate) <= 1:
            raise EventCatalystPromotionError(
                "event_catalyst_policy_hit_rate_invalid"
            )
        if not 0 <= float(self.demote_max_hit_rate) <= 1:
            raise EventCatalystPromotionError(
                "event_catalyst_policy_hit_rate_invalid"
            )

    @property
    def policy_sha256(self) -> str:
        return _sha256(
            {
                "policy_id": self.policy_id,
                "min_labeled_observations": self.min_labeled_observations,
                "min_distinct_event_clusters": self.min_distinct_event_clusters,
                "min_time_windows": self.min_time_windows,
                "min_window_hit_rate": float(self.min_window_hit_rate),
                "cost_per_round_trip": float(self.cost_per_round_trip),
                "demote_recent_labels": self.demote_recent_labels,
                "demote_max_hit_rate": float(self.demote_max_hit_rate),
            }
        )


@dataclass(frozen=True)
class LabeledObservation:
    """One labeled shadow label consumed by the gate.

    ``signed_post_return`` must already be cost-free; the gate subtracts the
    policy cost itself.  ``event_cluster`` groups symbols that share one
    underlying event so the effective sample size counts independent events.
    """

    hypothesis: str
    event_cluster: str
    window_id: str
    signed_post_return: float

    def __post_init__(self) -> None:
        if self.hypothesis not in SCORED_HYPOTHESES:
            raise EventCatalystPromotionError(
                "event_catalyst_label_hypothesis_invalid"
            )
        _text(self.event_cluster, "event_catalyst_label_cluster_invalid")
        _text(self.window_id, "event_catalyst_label_window_invalid")
        if (
            isinstance(self.signed_post_return, bool)
            or not isinstance(self.signed_post_return, (int, float))
            or not math.isfinite(float(self.signed_post_return))
        ):
            raise EventCatalystPromotionError(
                "event_catalyst_label_return_invalid"
            )


@dataclass(frozen=True)
class HypothesisVerdict:
    hypothesis: str
    decision: str
    labeled_observations: int
    distinct_event_clusters: int
    time_windows: int
    expectancy_after_cost: float | None
    window_hit_rates: tuple[float, ...]
    recent_hit_rate: float | None
    reason_code: str

    def __post_init__(self) -> None:
        if self.hypothesis not in SCORED_HYPOTHESES:
            raise EventCatalystPromotionError(
                "event_catalyst_verdict_hypothesis_invalid"
            )
        if self.decision not in DECISIONS:
            raise EventCatalystPromotionError(
                "event_catalyst_verdict_decision_invalid"
            )
        _text(self.reason_code, "event_catalyst_verdict_reason_invalid")
        for field_name in (
            "labeled_observations",
            "distinct_event_clusters",
            "time_windows",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise EventCatalystPromotionError(
                    "event_catalyst_verdict_count_invalid"
                )
        for field_name in ("expectancy_after_cost", "recent_hit_rate"):
            value = getattr(self, field_name)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                raise EventCatalystPromotionError(
                    "event_catalyst_verdict_metric_invalid"
                )
        for rate in self.window_hit_rates:
            if (
                isinstance(rate, bool)
                or not isinstance(rate, (int, float))
                or not math.isfinite(float(rate))
                or not 0 <= float(rate) <= 1
            ):
                raise EventCatalystPromotionError(
                    "event_catalyst_verdict_metric_invalid"
                )


@dataclass(frozen=True)
class PromotionDecision:
    """Deterministic, receipt-bound gate output; research-ladder only."""

    contract: str
    as_of: datetime
    policy_sha256: str
    verdicts: tuple[HypothesisVerdict, ...]
    decision_receipt_sha256: str
    execution_eligible: bool = False
    real_trading_enabled: bool = False

    def __post_init__(self) -> None:
        if self.contract != EVENT_CATALYST_PROMOTION_CONTRACT:
            raise EventCatalystPromotionError(
                "event_catalyst_decision_contract_invalid"
            )
        _aware(self.as_of, "event_catalyst_decision_as_of_invalid")
        for field_name in ("policy_sha256", "decision_receipt_sha256"):
            value = _text(
                getattr(self, field_name),
                "event_catalyst_decision_receipt_invalid",
            )
            if len(value) != 64 or any(c not in _SHA256_HEX for c in value):
                raise EventCatalystPromotionError(
                    "event_catalyst_decision_receipt_invalid"
                )
        if self.execution_eligible is not False or (
            self.real_trading_enabled is not False
        ):
            raise EventCatalystPromotionError(
                "event_catalyst_decision_authority_invalid"
            )


def _evaluate_hypothesis(
    hypothesis: str,
    labels: tuple[LabeledObservation, ...],
    *,
    policy: PromotionPolicy,
) -> HypothesisVerdict:
    direction = _HYPOTHESIS_DIRECTION[hypothesis]
    if not labels:
        return HypothesisVerdict(
            hypothesis=hypothesis,
            decision="keep_shadow",
            labeled_observations=0,
            distinct_event_clusters=0,
            time_windows=0,
            expectancy_after_cost=None,
            window_hit_rates=(),
            recent_hit_rate=None,
            reason_code="no_labeled_observations",
        )
    clusters = {label.event_cluster for label in labels}
    windows = sorted({label.window_id for label in labels})
    correct = tuple(
        direction * label.signed_post_return > 0 for label in labels
    )
    window_hit_rates = tuple(
        sum(
            1
            for label, ok in zip(labels, correct)
            if label.window_id == window and ok
        )
        / max(
            1,
            sum(1 for label in labels if label.window_id == window),
        )
        for window in windows
    )
    expectancy = (
        sum(direction * label.signed_post_return for label in labels)
        / len(labels)
        - float(policy.cost_per_round_trip)
    )
    recent = labels[-policy.demote_recent_labels :]
    recent_hit_rate = None
    if len(recent) >= policy.demote_recent_labels:
        recent_correct = [
            direction * label.signed_post_return > 0 for label in recent
        ]
        recent_hit_rate = sum(recent_correct) / len(recent_correct)
        if recent_hit_rate <= policy.demote_max_hit_rate:
            return HypothesisVerdict(
                hypothesis=hypothesis,
                decision="auto_demote",
                labeled_observations=len(labels),
                distinct_event_clusters=len(clusters),
                time_windows=len(windows),
                expectancy_after_cost=expectancy,
                window_hit_rates=window_hit_rates,
                recent_hit_rate=recent_hit_rate,
                reason_code="recent_labels_contradict_hypothesis",
            )
    if len(labels) < policy.min_labeled_observations:
        reason = "insufficient_labeled_observations"
    elif len(clusters) < policy.min_distinct_event_clusters:
        reason = "insufficient_event_clusters"
    elif len(windows) < policy.min_time_windows:
        reason = "insufficient_time_windows"
    elif expectancy <= 0:
        reason = "expectancy_not_positive_after_cost"
    elif any(rate < policy.min_window_hit_rate for rate in window_hit_rates):
        reason = "window_consistency_failed"
    else:
        return HypothesisVerdict(
            hypothesis=hypothesis,
            decision="graduate_to_validated_factor",
            labeled_observations=len(labels),
            distinct_event_clusters=len(clusters),
            time_windows=len(windows),
            expectancy_after_cost=expectancy,
            window_hit_rates=window_hit_rates,
            recent_hit_rate=recent_hit_rate,
            reason_code="policy_gates_all_passed",
        )
    return HypothesisVerdict(
        hypothesis=hypothesis,
        decision="keep_shadow",
        labeled_observations=len(labels),
        distinct_event_clusters=len(clusters),
        time_windows=len(windows),
        expectancy_after_cost=expectancy,
        window_hit_rates=window_hit_rates,
        recent_hit_rate=recent_hit_rate,
        reason_code=reason,
    )


def evaluate_promotion(
    labels: Sequence[LabeledObservation],
    *,
    policy: PromotionPolicy,
    as_of: datetime,
) -> PromotionDecision:
    """Evaluate every scored hypothesis against the frozen policy.

    Pure and deterministic: identical labels, policy and ``as_of`` always
    produce the identical decision receipt.  Any malformed label or naive
    timestamp fails closed before any verdict is computed.
    """

    if not isinstance(policy, PromotionPolicy):
        raise EventCatalystPromotionError("event_catalyst_policy_invalid")
    as_of = _aware(as_of, "event_catalyst_as_of_invalid")
    if not isinstance(labels, Sequence) or isinstance(labels, (str, bytes)):
        raise EventCatalystPromotionError("event_catalyst_labels_invalid")
    validated: list[LabeledObservation] = []
    for label in labels:
        if not isinstance(label, LabeledObservation):
            raise EventCatalystPromotionError("event_catalyst_labels_invalid")
        validated.append(label)
    verdicts = tuple(
        _evaluate_hypothesis(
            hypothesis,
            tuple(
                label for label in validated if label.hypothesis == hypothesis
            ),
            policy=policy,
        )
        for hypothesis in SCORED_HYPOTHESES
    )
    receipt = _sha256(
        {
            "contract": EVENT_CATALYST_PROMOTION_CONTRACT,
            "as_of": as_of.isoformat(),
            "policy_sha256": policy.policy_sha256,
            "labels": [
                {
                    "hypothesis": label.hypothesis,
                    "event_cluster": label.event_cluster,
                    "window_id": label.window_id,
                    "signed_post_return": label.signed_post_return,
                }
                for label in validated
            ],
            "verdicts": [
                {
                    "hypothesis": verdict.hypothesis,
                    "decision": verdict.decision,
                    "reason_code": verdict.reason_code,
                }
                for verdict in verdicts
            ],
        }
    )
    return PromotionDecision(
        contract=EVENT_CATALYST_PROMOTION_CONTRACT,
        as_of=as_of,
        policy_sha256=policy.policy_sha256,
        verdicts=verdicts,
        decision_receipt_sha256=receipt,
    )
