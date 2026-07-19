#!/usr/bin/env python3
"""Dataset-scoped evidence decisions for SharedSignals V1 responses."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

from .sharedsignals_v1 import QueryEnvelope


class EvidenceAction(str, Enum):
    ACCEPT = "accept"
    DEWEIGHT = "deweight"
    REJECT = "reject"


@dataclass(frozen=True)
class DatasetEvidencePolicy:
    """Explicit policy for one canonical dataset.

    Failed/invalid/incomplete evidence is always rejected.  Only degraded or
    stale evidence can be deliberately deweighted, and deweighting is opt-in.
    """

    dataset_id: str
    degraded_action: EvidenceAction = EvidenceAction.REJECT
    stale_action: EvidenceAction = EvidenceAction.REJECT
    degraded_weight: float = 0.25
    stale_weight: float = 0.10

    def __post_init__(self) -> None:
        if not isinstance(self.dataset_id, str) or not self.dataset_id.strip():
            raise ValueError("dataset_id must be a non-empty string")
        if self.dataset_id != self.dataset_id.strip():
            raise ValueError("dataset_id must not contain outer whitespace")
        for field_name, action in (
            ("degraded_action", self.degraded_action),
            ("stale_action", self.stale_action),
        ):
            if not isinstance(action, EvidenceAction):
                raise ValueError(f"{field_name} must be an EvidenceAction")
            if action is EvidenceAction.ACCEPT:
                raise ValueError(f"{field_name} cannot fully accept impaired evidence")
        if self.degraded_action is EvidenceAction.DEWEIGHT and not (
            0.0 < self.degraded_weight < 1.0
        ):
            raise ValueError("degraded_weight must be between 0 and 1")
        if self.stale_action is EvidenceAction.DEWEIGHT and not (
            0.0 < self.stale_weight < 1.0
        ):
            raise ValueError("stale_weight must be between 0 and 1")


@dataclass(frozen=True)
class EvidenceDecision:
    dataset_id: str
    receipt_id: str | None
    effective_state: str
    action: EvidenceAction
    eligible: bool
    weight: float
    reasons: tuple[str, ...]


def _state(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().lower()


def _is_native_bool(value: object, expected: bool) -> bool:
    return type(value) is bool and value is expected


def _has_malformed_bool(container: Mapping[str, object], key: str) -> bool:
    return key in container and type(container[key]) is not bool


def _deduplicate_reasons(reasons: tuple[str, ...]) -> tuple[str, ...]:
    result: list[str] = []
    for reason in reasons:
        if reason not in result:
            result.append(reason)
    return tuple(result)


def _effective_state(envelope: QueryEnvelope) -> str:
    metadata = envelope.metadata
    top_state = _state(metadata.state)
    quality_state = _state(metadata.quality.get("state"))
    freshness_state = _state(metadata.freshness.get("state"))
    lineage_state = (
        _state(metadata.lineage.get("state"))
        if isinstance(metadata.lineage, Mapping)
        else ""
    )

    failed_states = {
        "failed",
        "error",
        "invalid",
        "unavailable",
        "unobserved",
        "paused",
        "empty",
    }
    nested_failed_states = {
        "failed",
        "error",
        "invalid",
        "unavailable",
    }
    if top_state in failed_states or any(
        nested_state in nested_failed_states
        for nested_state in (freshness_state, quality_state, lineage_state)
    ):
        return "failed"
    if top_state in {"stale", "expired"}:
        return "stale"
    if metadata.lineage is None:
        return "failed"

    nested_allowed_states = {
        "freshness": {
            "fresh",
            "ready",
            "healthy",
            "ok",
            "available",
            "stale",
            "expired",
            "degraded",
            "partial",
            "warning",
            "failed",
            "error",
            "invalid",
            "unavailable",
        },
        "quality": {
            "valid",
            "ready",
            "healthy",
            "ok",
            "available",
            "degraded",
            "partial",
            "warning",
            "failed",
            "error",
            "invalid",
            "unavailable",
        },
        "lineage": {
            "complete",
            "ready",
            "healthy",
            "ok",
            "available",
            "degraded",
            "partial",
            "warning",
            "failed",
            "error",
            "invalid",
            "unavailable",
        },
    }
    for name, raw_state in (
        ("freshness", metadata.freshness.get("state")),
        ("quality", metadata.quality.get("state")),
        ("lineage", metadata.lineage.get("state")),
    ):
        if raw_state is not None:
            normalized = _state(raw_state)
            if not normalized or normalized not in nested_allowed_states[name]:
                return "failed"

    if any(
        (
            _has_malformed_bool(metadata.freshness, "stale"),
            _has_malformed_bool(metadata.freshness, "fresh"),
            _has_malformed_bool(metadata.quality, "valid"),
            _has_malformed_bool(metadata.lineage, "complete"),
            _has_malformed_bool(metadata.lineage, "provider_neutral"),
        )
    ):
        return "failed"
    if metadata.quality.get("valid") is False:
        return "failed"
    if not _is_native_bool(metadata.lineage.get("complete"), True):
        return "failed"
    if not _is_native_bool(metadata.lineage.get("provider_neutral"), True):
        return "failed"

    stale_states = {"stale", "expired"}
    if top_state in stale_states or freshness_state in stale_states:
        return "stale"
    if _is_native_bool(metadata.freshness.get("stale"), True):
        return "stale"
    if _is_native_bool(metadata.freshness.get("fresh"), False):
        return "stale"

    degraded_states = {"degraded", "partial", "warning"}
    if (
        metadata.degraded
        or top_state in degraded_states
        or quality_state in degraded_states
        or freshness_state in degraded_states
        or lineage_state in degraded_states
    ):
        return "degraded"

    if top_state in {"ready", "healthy", "ok", "available"}:
        return "ready"
    return "unknown"


def _has_complete_source_proof(envelope: QueryEnvelope) -> bool:
    metadata = envelope.metadata
    return bool(
        isinstance(metadata.receipt_id, str)
        and metadata.receipt_id
        and isinstance(metadata.data_through, str)
        and metadata.data_through
        and isinstance(metadata.observed_at, str)
        and metadata.observed_at
        and isinstance(metadata.lineage, Mapping)
        and metadata.lineage
    )


class DataEvidenceGate:
    """Convert validated envelopes into explicit dataset-scoped decisions."""

    def __init__(self, policies: Mapping[str, DatasetEvidencePolicy]) -> None:
        normalized: dict[str, DatasetEvidencePolicy] = {}
        for dataset_id, policy in policies.items():
            if not isinstance(dataset_id, str) or not isinstance(
                policy, DatasetEvidencePolicy
            ):
                raise ValueError("policies must map dataset IDs to policies")
            if dataset_id != policy.dataset_id:
                raise ValueError("policy key must match policy.dataset_id")
            normalized[dataset_id] = policy
        self._policies = normalized

    @staticmethod
    def _decision(
        envelope: QueryEnvelope,
        *,
        state: str,
        action: EvidenceAction,
        weight: float,
        gate_reason: str,
    ) -> EvidenceDecision:
        reasons = envelope.metadata.reasons
        if gate_reason:
            reasons = (*reasons, gate_reason)
        reasons = _deduplicate_reasons(reasons)
        return EvidenceDecision(
            dataset_id=envelope.dataset_id,
            receipt_id=envelope.metadata.receipt_id,
            effective_state=state,
            action=action,
            eligible=action is not EvidenceAction.REJECT,
            weight=weight if action is not EvidenceAction.REJECT else 0.0,
            reasons=reasons,
        )

    def evaluate(self, envelope: QueryEnvelope) -> EvidenceDecision:
        if not isinstance(envelope, QueryEnvelope):
            raise TypeError("envelope must be a validated QueryEnvelope")
        policy = self._policies.get(envelope.dataset_id)
        if policy is None:
            return self._decision(
                envelope,
                state="unknown",
                action=EvidenceAction.REJECT,
                weight=0.0,
                gate_reason="dataset_policy_missing",
            )

        state = _effective_state(envelope)
        if not _has_complete_source_proof(envelope):
            top_state = _state(envelope.metadata.state)
            hard_failed = {
                "failed",
                "error",
                "invalid",
                "unavailable",
                "unobserved",
                "paused",
                "empty",
            }
            return self._decision(
                envelope,
                state=state,
                action=EvidenceAction.REJECT,
                weight=0.0,
                gate_reason=(
                    "dataset_failed"
                    if top_state in hard_failed
                    else "dataset_evidence_incomplete"
                ),
            )
        if state == "ready":
            return self._decision(
                envelope,
                state=state,
                action=EvidenceAction.ACCEPT,
                weight=1.0,
                gate_reason="",
            )
        if state == "degraded":
            return self._decision(
                envelope,
                state=state,
                action=policy.degraded_action,
                weight=policy.degraded_weight,
                gate_reason="dataset_degraded",
            )
        if state == "stale":
            return self._decision(
                envelope,
                state=state,
                action=policy.stale_action,
                weight=policy.stale_weight,
                gate_reason="dataset_stale",
            )
        if state == "failed":
            return self._decision(
                envelope,
                state=state,
                action=EvidenceAction.REJECT,
                weight=0.0,
                gate_reason="dataset_failed",
            )
        return self._decision(
            envelope,
            state="unknown",
            action=EvidenceAction.REJECT,
            weight=0.0,
            gate_reason="dataset_state_unknown",
        )


__all__ = [
    "DataEvidenceGate",
    "DatasetEvidencePolicy",
    "EvidenceAction",
    "EvidenceDecision",
]
