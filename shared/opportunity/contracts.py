"""Immutable, point-in-time contracts for shadow opportunity research."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Iterable, Tuple

from shared.universe.policy import InstrumentRole, classify_instrument


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class OpportunityContractError(ValueError):
    """Raised when an opportunity cannot be proven safe and point-in-time."""


class OpportunityScope(str, Enum):
    MARKET = "market"
    SECTOR = "sector"
    STOCK = "stock"


class OpportunityState(str, Enum):
    LATENT = "latent"
    FORMING = "forming"
    READY = "ready"
    TRIGGERED = "triggered"
    ACTIVE = "active"
    DECAYING = "decaying"
    INVALIDATED = "invalidated"


_ALLOWED_TRANSITIONS = {
    OpportunityState.LATENT: frozenset(
        {OpportunityState.FORMING, OpportunityState.INVALIDATED}
    ),
    OpportunityState.FORMING: frozenset(
        {
            OpportunityState.READY,
            OpportunityState.DECAYING,
            OpportunityState.INVALIDATED,
        }
    ),
    OpportunityState.READY: frozenset(
        {
            OpportunityState.TRIGGERED,
            OpportunityState.DECAYING,
            OpportunityState.INVALIDATED,
        }
    ),
    OpportunityState.TRIGGERED: frozenset(
        {
            OpportunityState.ACTIVE,
            OpportunityState.DECAYING,
            OpportunityState.INVALIDATED,
        }
    ),
    OpportunityState.ACTIVE: frozenset(
        {OpportunityState.DECAYING, OpportunityState.INVALIDATED}
    ),
    OpportunityState.DECAYING: frozenset(
        {OpportunityState.FORMING, OpportunityState.INVALIDATED}
    ),
    OpportunityState.INVALIDATED: frozenset(),
}


def _text(value: object, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\x00" in value
    ):
        raise OpportunityContractError(f"{field_name}_invalid")
    return value


def _aware(value: object, field_name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise OpportunityContractError(f"{field_name}_timezone_required")
    return value.astimezone(timezone.utc)


def _parse_aware(value: object, field_name: str) -> datetime:
    if not isinstance(value, str) or not value or value != value.strip():
        raise OpportunityContractError(f"{field_name}_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OpportunityContractError(f"{field_name}_invalid") from exc
    return _aware(parsed, field_name)


def _sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sha256_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise OpportunityContractError(f"{field_name}_invalid")
    return value


def _score(value: object, field_name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not 0.0 <= float(value) <= 1.0
    ):
        raise OpportunityContractError(f"{field_name}_invalid")
    return float(value)


def _texts(
    values: Iterable[str], field_name: str, *, required: bool
) -> Tuple[str, ...]:
    if not isinstance(values, tuple):
        raise OpportunityContractError(f"{field_name}_invalid")
    normalized = tuple(_text(value, field_name) for value in values)
    if (required and not normalized) or len(normalized) != len(set(normalized)):
        raise OpportunityContractError(f"{field_name}_invalid")
    return normalized


@dataclass(frozen=True)
class OpportunityEvidenceRef:
    """One typed evidence reference with explicit availability and expiry."""

    evidence_id: str
    dataset_id: str
    receipt_id: str
    lineage_id: str
    evidence_group_id: str
    data_through: datetime
    available_at: datetime
    expires_at: datetime
    payload_sha256: str

    def __post_init__(self) -> None:
        for field_name in (
            "evidence_id",
            "dataset_id",
            "receipt_id",
            "lineage_id",
            "evidence_group_id",
        ):
            _text(getattr(self, field_name), field_name)
        _sha256_text(self.payload_sha256, "payload_sha256")
        data_through = _aware(self.data_through, "data_through")
        available_at = _aware(self.available_at, "available_at")
        expires_at = _aware(self.expires_at, "expires_at")
        object.__setattr__(self, "data_through", data_through)
        object.__setattr__(self, "available_at", available_at)
        object.__setattr__(self, "expires_at", expires_at)
        if not data_through <= available_at < expires_at:
            raise OpportunityContractError("evidence_time_order_invalid")

    def canonical_payload(self) -> dict[str, object]:
        return {
            "available_at": self.available_at.isoformat(),
            "data_through": self.data_through.isoformat(),
            "dataset_id": self.dataset_id,
            "evidence_group_id": self.evidence_group_id,
            "evidence_id": self.evidence_id,
            "expires_at": self.expires_at.isoformat(),
            "lineage_id": self.lineage_id,
            "payload_sha256": self.payload_sha256,
            "receipt_id": self.receipt_id,
        }

    @classmethod
    def from_payload(cls, payload: object) -> "OpportunityEvidenceRef":
        expected = {
            "available_at",
            "data_through",
            "dataset_id",
            "evidence_group_id",
            "evidence_id",
            "expires_at",
            "lineage_id",
            "payload_sha256",
            "receipt_id",
        }
        if not isinstance(payload, dict) or set(payload) != expected:
            raise OpportunityContractError("evidence_payload_invalid")
        return cls(
            evidence_id=payload["evidence_id"],
            dataset_id=payload["dataset_id"],
            receipt_id=payload["receipt_id"],
            lineage_id=payload["lineage_id"],
            evidence_group_id=payload["evidence_group_id"],
            data_through=_parse_aware(payload["data_through"], "data_through"),
            available_at=_parse_aware(payload["available_at"], "available_at"),
            expires_at=_parse_aware(payload["expires_at"], "expires_at"),
            payload_sha256=payload["payload_sha256"],
        )


def _validate_entity(scope: OpportunityScope, entity_id: str) -> bool:
    if scope is OpportunityScope.STOCK:
        eligibility = classify_instrument(entity_id, instrument_type="common_stock")
        if eligibility.role is not InstrumentRole.MAINBOARD_COMMON_STOCK:
            raise OpportunityContractError(
                "stock_scope_requires_mainboard_common_stock"
            )
        return False
    if scope is OpportunityScope.SECTOR:
        eligibility = classify_instrument(
            entity_id,
            instrument_type="sector_aggregate",
        )
        if not eligibility.context_only:
            raise OpportunityContractError("sector_scope_requires_context_aggregate")
        return True
    eligibility = classify_instrument(entity_id, instrument_type="index")
    if not eligibility.context_only:
        raise OpportunityContractError("market_scope_requires_context_index")
    return True


@dataclass(frozen=True)
class OpportunitySnapshot:
    """Content-addressed opportunity state with no candidate/order authority."""

    opportunity_id: str
    scope: OpportunityScope
    entity_id: str
    thesis_id: str
    state: OpportunityState
    decision_time: datetime
    discovered_at: datetime
    trigger_window_start: datetime
    trigger_window_end: datetime
    horizon: str
    uncalibrated_hazard_score: float
    priced_in_score: float
    evidence_refs: Tuple[OpportunityEvidenceRef, ...]
    invalidation_conditions: Tuple[str, ...]
    reason_codes: Tuple[str, ...]
    previous_snapshot_sha256: str | None = None
    schema_version: str = "tradingagent.opportunity_snapshot.v1"
    score_semantics: str = "uncalibrated_hazard_score"
    shadow_only: bool = True
    context_only: bool = field(init=False)
    trade_candidate_emission_allowed: bool = False
    position_effect_allowed: bool = False
    order_effect_allowed: bool = False
    promotion_eligible: bool = False
    snapshot_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        for field_name in ("opportunity_id", "entity_id", "thesis_id", "horizon"):
            _text(getattr(self, field_name), field_name)
        if not isinstance(self.scope, OpportunityScope):
            raise OpportunityContractError("opportunity_scope_invalid")
        if not isinstance(self.state, OpportunityState):
            raise OpportunityContractError("opportunity_state_invalid")
        context_only = _validate_entity(self.scope, self.entity_id)
        object.__setattr__(self, "context_only", context_only)
        decision_time = _aware(self.decision_time, "decision_time")
        discovered_at = _aware(self.discovered_at, "discovered_at")
        trigger_start = _aware(self.trigger_window_start, "trigger_window_start")
        trigger_end = _aware(self.trigger_window_end, "trigger_window_end")
        object.__setattr__(self, "decision_time", decision_time)
        object.__setattr__(self, "discovered_at", discovered_at)
        object.__setattr__(self, "trigger_window_start", trigger_start)
        object.__setattr__(self, "trigger_window_end", trigger_end)
        if not discovered_at <= decision_time <= trigger_end:
            raise OpportunityContractError("opportunity_time_order_invalid")
        if trigger_start > trigger_end:
            raise OpportunityContractError("trigger_window_invalid")
        object.__setattr__(
            self,
            "uncalibrated_hazard_score",
            _score(self.uncalibrated_hazard_score, "uncalibrated_hazard_score"),
        )
        object.__setattr__(
            self,
            "priced_in_score",
            _score(self.priced_in_score, "priced_in_score"),
        )
        if not isinstance(self.evidence_refs, tuple) or not self.evidence_refs:
            raise OpportunityContractError("evidence_refs_invalid")
        if any(
            not isinstance(item, OpportunityEvidenceRef) for item in self.evidence_refs
        ):
            raise OpportunityContractError("evidence_refs_invalid")
        evidence_ids = tuple(item.evidence_id for item in self.evidence_refs)
        if len(evidence_ids) != len(set(evidence_ids)):
            raise OpportunityContractError("evidence_refs_duplicate")
        evidence_group_ids = tuple(
            item.evidence_group_id for item in self.evidence_refs
        )
        if len(evidence_group_ids) != len(set(evidence_group_ids)):
            raise OpportunityContractError("evidence_group_conflict")
        for evidence in self.evidence_refs:
            if evidence.available_at > decision_time:
                raise OpportunityContractError("evidence_from_future")
            if evidence.expires_at < decision_time:
                raise OpportunityContractError("evidence_expired")
        object.__setattr__(
            self,
            "evidence_refs",
            tuple(sorted(self.evidence_refs, key=lambda item: item.evidence_id)),
        )
        object.__setattr__(
            self,
            "invalidation_conditions",
            _texts(
                self.invalidation_conditions,
                "invalidation_conditions",
                required=True,
            ),
        )
        object.__setattr__(
            self,
            "reason_codes",
            _texts(self.reason_codes, "reason_codes", required=True),
        )
        if self.previous_snapshot_sha256 is not None:
            _sha256_text(
                self.previous_snapshot_sha256,
                "previous_snapshot_sha256",
            )
        if (
            self.schema_version != "tradingagent.opportunity_snapshot.v1"
            or self.score_semantics != "uncalibrated_hazard_score"
            or self.shadow_only is not True
            or self.trade_candidate_emission_allowed is not False
            or self.position_effect_allowed is not False
            or self.order_effect_allowed is not False
            or self.promotion_eligible is not False
        ):
            raise OpportunityContractError("opportunity_shadow_boundary_invalid")
        object.__setattr__(self, "snapshot_sha256", _sha256(self.canonical_payload()))

    @classmethod
    def create(cls, **values: object) -> "OpportunitySnapshot":
        return cls(**values)  # type: ignore[arg-type]

    @classmethod
    def from_payload(cls, payload: object) -> "OpportunitySnapshot":
        expected = {
            "context_only",
            "decision_time",
            "discovered_at",
            "entity_id",
            "evidence_refs",
            "horizon",
            "invalidation_conditions",
            "opportunity_id",
            "order_effect_allowed",
            "position_effect_allowed",
            "previous_snapshot_sha256",
            "priced_in_score",
            "promotion_eligible",
            "reason_codes",
            "schema_version",
            "scope",
            "score_semantics",
            "shadow_only",
            "state",
            "thesis_id",
            "trade_candidate_emission_allowed",
            "trigger_window_end",
            "trigger_window_start",
            "uncalibrated_hazard_score",
        }
        if not isinstance(payload, dict) or set(payload) != expected:
            raise OpportunityContractError("opportunity_payload_invalid")
        evidence_rows = payload["evidence_refs"]
        if not isinstance(evidence_rows, list):
            raise OpportunityContractError("evidence_payload_invalid")
        invalidation = payload["invalidation_conditions"]
        reasons = payload["reason_codes"]
        if not isinstance(invalidation, list) or not isinstance(reasons, list):
            raise OpportunityContractError("opportunity_payload_invalid")
        try:
            scope = OpportunityScope(payload["scope"])
            state = OpportunityState(payload["state"])
        except (TypeError, ValueError) as exc:
            raise OpportunityContractError("opportunity_payload_invalid") from exc
        snapshot = cls(
            opportunity_id=payload["opportunity_id"],
            scope=scope,
            entity_id=payload["entity_id"],
            thesis_id=payload["thesis_id"],
            state=state,
            decision_time=_parse_aware(payload["decision_time"], "decision_time"),
            discovered_at=_parse_aware(payload["discovered_at"], "discovered_at"),
            trigger_window_start=_parse_aware(
                payload["trigger_window_start"],
                "trigger_window_start",
            ),
            trigger_window_end=_parse_aware(
                payload["trigger_window_end"],
                "trigger_window_end",
            ),
            horizon=payload["horizon"],
            uncalibrated_hazard_score=payload["uncalibrated_hazard_score"],
            priced_in_score=payload["priced_in_score"],
            evidence_refs=tuple(
                OpportunityEvidenceRef.from_payload(item) for item in evidence_rows
            ),
            invalidation_conditions=tuple(invalidation),
            reason_codes=tuple(reasons),
            previous_snapshot_sha256=payload["previous_snapshot_sha256"],
            schema_version=payload["schema_version"],
            score_semantics=payload["score_semantics"],
            shadow_only=payload["shadow_only"],
            trade_candidate_emission_allowed=payload[
                "trade_candidate_emission_allowed"
            ],
            position_effect_allowed=payload["position_effect_allowed"],
            order_effect_allowed=payload["order_effect_allowed"],
            promotion_eligible=payload["promotion_eligible"],
        )
        if snapshot.context_only != payload["context_only"]:
            raise OpportunityContractError("opportunity_context_binding_invalid")
        return snapshot

    def canonical_payload(self) -> dict[str, object]:
        return {
            "context_only": self.context_only,
            "decision_time": self.decision_time.isoformat(),
            "discovered_at": self.discovered_at.isoformat(),
            "entity_id": self.entity_id,
            "evidence_refs": [item.canonical_payload() for item in self.evidence_refs],
            "horizon": self.horizon,
            "invalidation_conditions": list(self.invalidation_conditions),
            "opportunity_id": self.opportunity_id,
            "order_effect_allowed": False,
            "position_effect_allowed": False,
            "previous_snapshot_sha256": self.previous_snapshot_sha256,
            "priced_in_score": self.priced_in_score,
            "promotion_eligible": False,
            "reason_codes": list(self.reason_codes),
            "schema_version": self.schema_version,
            "scope": self.scope.value,
            "score_semantics": self.score_semantics,
            "shadow_only": True,
            "state": self.state.value,
            "thesis_id": self.thesis_id,
            "trade_candidate_emission_allowed": False,
            "trigger_window_end": self.trigger_window_end.isoformat(),
            "trigger_window_start": self.trigger_window_start.isoformat(),
            "uncalibrated_hazard_score": self.uncalibrated_hazard_score,
        }


def transition_opportunity(
    previous: OpportunitySnapshot,
    *,
    target_state: OpportunityState,
    decision_time: datetime,
    new_evidence_refs: Tuple[OpportunityEvidenceRef, ...],
    reason_codes: Tuple[str, ...],
) -> OpportunitySnapshot:
    """Advance one opportunity only when new point-in-time evidence exists."""

    if not isinstance(previous, OpportunitySnapshot):
        raise OpportunityContractError("previous_opportunity_invalid")
    if previous.state is OpportunityState.INVALIDATED:
        raise OpportunityContractError("opportunity_state_terminal")
    if target_state not in _ALLOWED_TRANSITIONS[previous.state]:
        raise OpportunityContractError("opportunity_transition_invalid")
    instant = _aware(decision_time, "decision_time")
    if instant < previous.decision_time:
        raise OpportunityContractError("opportunity_transition_time_invalid")
    if not isinstance(new_evidence_refs, tuple) or not new_evidence_refs:
        raise OpportunityContractError("transition_requires_new_evidence")
    if any(not isinstance(item, OpportunityEvidenceRef) for item in new_evidence_refs):
        raise OpportunityContractError("transition_evidence_invalid")
    existing_ids = {item.evidence_id for item in previous.evidence_refs}
    if any(item.evidence_id in existing_ids for item in new_evidence_refs):
        raise OpportunityContractError("transition_requires_new_evidence")
    if not any(
        item.available_at > previous.decision_time for item in new_evidence_refs
    ):
        raise OpportunityContractError("transition_requires_new_pit_evidence")
    current = OpportunitySnapshot(
        opportunity_id=previous.opportunity_id,
        scope=previous.scope,
        entity_id=previous.entity_id,
        thesis_id=previous.thesis_id,
        state=target_state,
        decision_time=instant,
        discovered_at=previous.discovered_at,
        trigger_window_start=previous.trigger_window_start,
        trigger_window_end=previous.trigger_window_end,
        horizon=previous.horizon,
        uncalibrated_hazard_score=previous.uncalibrated_hazard_score,
        priced_in_score=previous.priced_in_score,
        evidence_refs=previous.evidence_refs + new_evidence_refs,
        invalidation_conditions=previous.invalidation_conditions,
        reason_codes=reason_codes,
        previous_snapshot_sha256=previous.snapshot_sha256,
    )
    validate_opportunity_transition(previous, current)
    return current


def validate_opportunity_transition(
    previous: OpportunitySnapshot,
    current: OpportunitySnapshot,
) -> None:
    """Verify an externally constructed state change before it enters a ledger."""

    if not isinstance(previous, OpportunitySnapshot) or not isinstance(
        current, OpportunitySnapshot
    ):
        raise OpportunityContractError("opportunity_transition_snapshot_invalid")
    if previous.state is OpportunityState.INVALIDATED:
        raise OpportunityContractError("opportunity_state_terminal")
    if current.state not in _ALLOWED_TRANSITIONS[previous.state]:
        raise OpportunityContractError("opportunity_transition_invalid")
    if current.previous_snapshot_sha256 != previous.snapshot_sha256:
        raise OpportunityContractError("opportunity_transition_branch_mismatch")
    if current.decision_time < previous.decision_time:
        raise OpportunityContractError("opportunity_transition_time_invalid")
    immutable_previous = (
        previous.opportunity_id,
        previous.scope,
        previous.entity_id,
        previous.thesis_id,
        previous.discovered_at,
        previous.trigger_window_start,
        previous.trigger_window_end,
        previous.horizon,
        previous.uncalibrated_hazard_score,
        previous.priced_in_score,
        previous.invalidation_conditions,
        previous.schema_version,
        previous.score_semantics,
        previous.shadow_only,
        previous.context_only,
        previous.trade_candidate_emission_allowed,
        previous.position_effect_allowed,
        previous.order_effect_allowed,
        previous.promotion_eligible,
    )
    immutable_current = (
        current.opportunity_id,
        current.scope,
        current.entity_id,
        current.thesis_id,
        current.discovered_at,
        current.trigger_window_start,
        current.trigger_window_end,
        current.horizon,
        current.uncalibrated_hazard_score,
        current.priced_in_score,
        current.invalidation_conditions,
        current.schema_version,
        current.score_semantics,
        current.shadow_only,
        current.context_only,
        current.trade_candidate_emission_allowed,
        current.position_effect_allowed,
        current.order_effect_allowed,
        current.promotion_eligible,
    )
    if immutable_current != immutable_previous:
        raise OpportunityContractError("opportunity_transition_identity_mismatch")
    previous_evidence = {
        item.evidence_id: item.canonical_payload() for item in previous.evidence_refs
    }
    current_evidence = {
        item.evidence_id: item.canonical_payload() for item in current.evidence_refs
    }
    if any(
        evidence_id not in current_evidence or current_evidence[evidence_id] != payload
        for evidence_id, payload in previous_evidence.items()
    ):
        raise OpportunityContractError("opportunity_transition_evidence_mutated")
    new_evidence = tuple(
        item
        for item in current.evidence_refs
        if item.evidence_id not in previous_evidence
    )
    if not new_evidence:
        raise OpportunityContractError("transition_requires_new_evidence")
    if not any(item.available_at > previous.decision_time for item in new_evidence):
        raise OpportunityContractError("transition_requires_new_pit_evidence")


__all__ = [
    "OpportunityContractError",
    "OpportunityEvidenceRef",
    "OpportunityScope",
    "OpportunitySnapshot",
    "OpportunityState",
    "transition_opportunity",
    "validate_opportunity_transition",
]
