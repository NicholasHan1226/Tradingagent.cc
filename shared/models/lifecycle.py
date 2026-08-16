"""Scientific validation plan and model lifecycle state machine.

Human reviewers may follow every manual transition.  Automation may follow the
same forward edges (never RETIRED) and may additionally enter CURRENT, but only
when the transition is bound to a valid promotion evidence reference.  All
records stay simulation-only: real trading, live transition and automatic risk
expansion remain permanently disabled.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import date, datetime
from enum import Enum
from typing import Dict, Optional, Protocol, Tuple

from .release_manifest import ModelReleaseManifest


_ASHARE_MARKETS = frozenset({"ashare", "a_share", "a-share", "a股", "cn", "china"})

PROMOTION_EVIDENCE_REFERENCE_PREFIX = "promotion-evidence:"


def is_promotion_evidence_reference(value: object) -> bool:
    """Return True for a ``promotion-evidence:<sha256>`` reference string."""

    if not isinstance(value, str) or not value.startswith(
        PROMOTION_EVIDENCE_REFERENCE_PREFIX
    ):
        return False
    digest = value[len(PROMOTION_EVIDENCE_REFERENCE_PREFIX) :]
    return len(digest) == 64 and all(
        char in "0123456789abcdef" for char in digest
    )


def promotion_evidence_reference(evidence_sha256: str) -> str:
    """Bind a content-addressed evidence digest into a promotion reference."""

    _require_sha256(evidence_sha256, "evidence_sha256")
    return PROMOTION_EVIDENCE_REFERENCE_PREFIX + evidence_sha256


class LifecycleContractError(ValueError):
    """Raised when lifecycle evidence or a transition is unsafe."""


def _require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise LifecycleContractError("%s_must_be_nonempty_text" % field_name)


def _require_sha256(value: str, field_name: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise LifecycleContractError("%s_invalid" % field_name)


@dataclass(frozen=True)
class TradingSessionCalendarAuthority:
    """Content-addressed exchange-session calendar bound to a source receipt."""

    market: str
    calendar_id: str
    calendar_version: str
    source_dataset_id: str
    source_receipt_id: str
    source_receipt_sha256: str
    available_at: datetime
    sessions: Tuple[date, ...]

    def __post_init__(self) -> None:
        for field_name in (
            "market",
            "calendar_id",
            "calendar_version",
            "source_dataset_id",
            "source_receipt_id",
        ):
            _require_text(getattr(self, field_name), field_name)
        _require_sha256(
            self.source_receipt_sha256,
            "source_receipt_sha256",
        )
        if self.available_at.tzinfo is None or self.available_at.utcoffset() is None:
            raise LifecycleContractError("calendar_available_at_must_be_timezone_aware")
        if not isinstance(self.sessions, tuple) or not self.sessions:
            raise LifecycleContractError("calendar_sessions_must_be_nonempty_tuple")
        previous: Optional[date] = None
        for session in self.sessions:
            if not isinstance(session, date) or isinstance(session, datetime):
                raise LifecycleContractError("calendar_session_date_invalid")
            if previous is not None and session <= previous:
                raise LifecycleContractError(
                    "calendar_sessions_must_be_strictly_increasing"
                )
            previous = session

    @property
    def session_count(self) -> int:
        return len(self.sessions)

    def canonical_payload(self) -> dict:
        return {
            "market": self.market.strip().lower(),
            "calendar_id": self.calendar_id,
            "calendar_version": self.calendar_version,
            "source_dataset_id": self.source_dataset_id,
            "source_receipt_id": self.source_receipt_id,
            "source_receipt_sha256": self.source_receipt_sha256,
            "available_at": self.available_at.isoformat(),
            "sessions": tuple(session.isoformat() for session in self.sessions),
        }

    @property
    def calendar_sha256(self) -> str:
        encoded = json.dumps(
            self.canonical_payload(),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class TradingSessionCalendarAuthorityVerification:
    """Detached verification receipt issued by an independent calendar port."""

    accepted: bool
    verifier_id: str
    verifier_version: str
    proof_sha256: str
    verified_at: datetime
    frozen_at: datetime
    calendar_sha256: str
    source_receipt_id: str
    source_receipt_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.accepted, bool):
            raise LifecycleContractError("calendar_authority_acceptance_invalid")
        for field_name in (
            "verifier_id",
            "verifier_version",
            "source_receipt_id",
        ):
            _require_text(getattr(self, field_name), field_name)
        for field_name in (
            "proof_sha256",
            "calendar_sha256",
            "source_receipt_sha256",
        ):
            _require_sha256(getattr(self, field_name), field_name)
        for field_name in ("verified_at", "frozen_at"):
            instant = getattr(self, field_name)
            if instant.tzinfo is None or instant.utcoffset() is None:
                raise LifecycleContractError(f"{field_name}_must_be_timezone_aware")
        if self.verified_at > self.frozen_at:
            raise LifecycleContractError("calendar_authority_verification_from_future")

    def canonical_payload(self) -> dict:
        return {
            "accepted": self.accepted,
            "calendar_sha256": self.calendar_sha256,
            "frozen_at": self.frozen_at.isoformat(),
            "proof_sha256": self.proof_sha256,
            "source_receipt_id": self.source_receipt_id,
            "source_receipt_sha256": self.source_receipt_sha256,
            "verified_at": self.verified_at.isoformat(),
            "verifier_id": self.verifier_id,
            "verifier_version": self.verifier_version,
        }


class TradingSessionCalendarAuthorityVerifier(Protocol):
    """No-default port for verifying a calendar against external authority."""

    verifier_id: str
    verifier_version: str

    def verify(
        self,
        calendar: TradingSessionCalendarAuthority,
        *,
        frozen_at: datetime,
    ) -> TradingSessionCalendarAuthorityVerification:
        """Return a detached proof bound to the exact calendar and freeze time."""


def _validate_calendar_authority_binding(
    calendar: TradingSessionCalendarAuthority,
    proof: TradingSessionCalendarAuthorityVerification,
    *,
    frozen_at: datetime,
    verifier_id: Optional[str] = None,
    verifier_version: Optional[str] = None,
) -> None:
    if not isinstance(proof, TradingSessionCalendarAuthorityVerification):
        raise LifecycleContractError("calendar_authority_verification_invalid")
    if verifier_id is not None and proof.verifier_id != verifier_id:
        raise LifecycleContractError("calendar_authority_verifier_mismatch")
    if verifier_version is not None and proof.verifier_version != verifier_version:
        raise LifecycleContractError("calendar_authority_verifier_version_mismatch")
    expected = (
        calendar.calendar_sha256,
        calendar.source_receipt_id,
        calendar.source_receipt_sha256,
        frozen_at,
    )
    actual = (
        proof.calendar_sha256,
        proof.source_receipt_id,
        proof.source_receipt_sha256,
        proof.frozen_at,
    )
    if actual != expected:
        raise LifecycleContractError("calendar_authority_binding_mismatch")
    if proof.accepted is not True:
        raise LifecycleContractError("calendar_authority_rejected")


@dataclass(frozen=True)
class ValidationPlan:
    """Frozen split plan preventing lookahead and OOS test-set mining."""

    train_start: date
    train_end: date
    validation_start: date
    validation_end: date
    test_start: date
    test_end: date
    purge_days: int
    embargo_days: int
    label_horizon_days: int
    max_feature_lookback_days: int
    event_cluster_embargo_days: int
    decision_cluster_key: str
    decision_cluster_deduplicated: bool
    registered_trial_count: int
    multiple_testing_trial_budget: int
    pbo_required: bool
    deflated_sharpe_required: bool
    oos_reuse_count: int
    max_oos_reuse_count: int
    oos_used_for_tuning: bool
    oos_authority_receipt_sha256: str
    experiment_family_id: str
    experiment_id: str
    frozen_test_set_id: str
    frozen_at: datetime
    market: str = "generic"
    trading_session_calendar: Optional[TradingSessionCalendarAuthority] = None
    trading_session_calendar_verification: Optional[
        TradingSessionCalendarAuthorityVerification
    ] = None

    def __post_init__(self) -> None:
        _require_text(self.market, "market")
        normalized_market = self.market.strip().lower()
        if self.frozen_at.tzinfo is None or self.frozen_at.utcoffset() is None:
            raise LifecycleContractError("frozen_at_must_be_timezone_aware")
        if (
            normalized_market in _ASHARE_MARKETS
            and self.trading_session_calendar is None
        ):
            raise LifecycleContractError("ashare_trading_session_calendar_required")
        if self.trading_session_calendar is not None:
            if not isinstance(
                self.trading_session_calendar,
                TradingSessionCalendarAuthority,
            ):
                raise LifecycleContractError("trading_session_calendar_type_invalid")
            if (
                self.trading_session_calendar.market.strip().lower()
                != normalized_market
            ):
                raise LifecycleContractError("trading_session_calendar_market_mismatch")
            if self.trading_session_calendar.available_at > self.frozen_at:
                raise LifecycleContractError(
                    "calendar_available_after_validation_plan_freeze"
                )
            if self.trading_session_calendar_verification is None:
                raise LifecycleContractError(
                    "trading_session_calendar_verification_required"
                )
            _validate_calendar_authority_binding(
                self.trading_session_calendar,
                self.trading_session_calendar_verification,
                frozen_at=self.frozen_at,
            )
        elif self.trading_session_calendar_verification is not None:
            raise LifecycleContractError(
                "calendar_authority_verification_without_calendar"
            )
        if not (
            self.train_start <= self.train_end
            and self.validation_start <= self.validation_end
            and self.test_start <= self.test_end
        ):
            raise LifecycleContractError("time_split_range_invalid")
        if isinstance(self.purge_days, bool) or self.purge_days < 0:
            raise LifecycleContractError("purge_days_invalid")
        if isinstance(self.embargo_days, bool) or self.embargo_days < 0:
            raise LifecycleContractError("embargo_days_invalid")
        for field_name in (
            "label_horizon_days",
            "max_feature_lookback_days",
            "event_cluster_embargo_days",
        ):
            value = getattr(self, field_name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < (1 if field_name == "label_horizon_days" else 0)
            ):
                raise LifecycleContractError(f"{field_name}_invalid")
        required_purge = max(
            self.label_horizon_days,
            self.max_feature_lookback_days,
        )
        if self.purge_days < required_purge:
            raise LifecycleContractError("purge_horizon_insufficient")
        required_embargo = max(
            self.label_horizon_days,
            self.event_cluster_embargo_days,
        )
        if self.embargo_days < required_embargo:
            raise LifecycleContractError("embargo_horizon_insufficient")
        if normalized_market in _ASHARE_MARKETS:
            calendar = self.trading_session_calendar
            assert calendar is not None
            sessions = frozenset(calendar.sessions)
            for field_name in (
                "train_start",
                "train_end",
                "validation_start",
                "validation_end",
                "test_start",
                "test_end",
            ):
                if getattr(self, field_name) not in sessions:
                    raise LifecycleContractError(
                        "%s_must_be_trading_session" % field_name
                    )
            purge_gap = sum(
                1
                for session in calendar.sessions
                if self.train_end < session < self.validation_start
            )
            if purge_gap < self.purge_days:
                raise LifecycleContractError("purge_gap_insufficient_trading_sessions")
            embargo_gap = sum(
                1
                for session in calendar.sessions
                if self.validation_end < session < self.test_start
            )
            if embargo_gap < self.embargo_days:
                raise LifecycleContractError(
                    "embargo_gap_insufficient_trading_sessions"
                )
        else:
            if (self.validation_start - self.train_end).days <= self.purge_days:
                raise LifecycleContractError("purge_gap_insufficient")
            if (self.test_start - self.validation_end).days <= self.embargo_days:
                raise LifecycleContractError("embargo_gap_insufficient")
        _require_text(self.decision_cluster_key, "decision_cluster_key")
        if self.decision_cluster_deduplicated is not True:
            raise LifecycleContractError("decision_cluster_dedup_required")
        if (
            isinstance(self.registered_trial_count, bool)
            or not isinstance(self.registered_trial_count, int)
            or self.registered_trial_count <= 0
        ):
            raise LifecycleContractError("registered_trial_count_invalid")
        if (
            isinstance(self.multiple_testing_trial_budget, bool)
            or not isinstance(self.multiple_testing_trial_budget, int)
            or self.multiple_testing_trial_budget <= 0
        ):
            raise LifecycleContractError("multiple_testing_trial_budget_invalid")
        if self.registered_trial_count > self.multiple_testing_trial_budget:
            raise LifecycleContractError("multiple_testing_budget_exceeded")
        if self.pbo_required is not True:
            raise LifecycleContractError("pbo_control_required")
        if self.deflated_sharpe_required is not True:
            raise LifecycleContractError("deflated_sharpe_control_required")
        if (
            isinstance(self.oos_reuse_count, bool)
            or not isinstance(self.oos_reuse_count, int)
            or self.oos_reuse_count < 0
        ):
            raise LifecycleContractError("oos_reuse_count_invalid")
        if (
            isinstance(self.max_oos_reuse_count, bool)
            or not isinstance(self.max_oos_reuse_count, int)
            or self.max_oos_reuse_count < 0
        ):
            raise LifecycleContractError("max_oos_reuse_count_invalid")
        if self.oos_used_for_tuning is not False:
            raise LifecycleContractError("oos_tuning_forbidden")
        if self.oos_reuse_count > self.max_oos_reuse_count:
            raise LifecycleContractError("oos_reuse_exceeded")
        _require_sha256(
            self.oos_authority_receipt_sha256,
            "oos_authority_receipt_sha256",
        )
        for field_name in (
            "experiment_family_id",
            "experiment_id",
            "frozen_test_set_id",
        ):
            _require_text(getattr(self, field_name), field_name)

    @property
    def time_split(self) -> Dict[str, Tuple[date, date]]:
        return {
            "train": (self.train_start, self.train_end),
            "validation": (self.validation_start, self.validation_end),
            "test": (self.test_start, self.test_end),
        }

    def canonical_payload(self) -> dict:
        calendar_payload = None
        if self.trading_session_calendar is not None:
            calendar_payload = {
                **self.trading_session_calendar.canonical_payload(),
                "calendar_sha256": self.trading_session_calendar.calendar_sha256,
                "session_count": self.trading_session_calendar.session_count,
            }
        return {
            "decision_cluster_deduplicated": self.decision_cluster_deduplicated,
            "decision_cluster_key": self.decision_cluster_key,
            "embargo_days": self.embargo_days,
            "event_cluster_embargo_days": self.event_cluster_embargo_days,
            "experiment_family_id": self.experiment_family_id,
            "experiment_id": self.experiment_id,
            "frozen_test_set_id": self.frozen_test_set_id,
            "frozen_at": self.frozen_at.isoformat(),
            "label_horizon_days": self.label_horizon_days,
            "max_feature_lookback_days": self.max_feature_lookback_days,
            "max_oos_reuse_count": self.max_oos_reuse_count,
            "market": self.market.strip().lower(),
            "multiple_testing_trial_budget": self.multiple_testing_trial_budget,
            "deflated_sharpe_required": self.deflated_sharpe_required,
            "oos_authority_receipt_sha256": self.oos_authority_receipt_sha256,
            "oos_reuse_count": self.oos_reuse_count,
            "oos_used_for_tuning": self.oos_used_for_tuning,
            "pbo_required": self.pbo_required,
            "purge_days": self.purge_days,
            "registered_trial_count": self.registered_trial_count,
            "split_gap_unit": (
                "trading_sessions"
                if self.market.strip().lower() in _ASHARE_MARKETS
                else "calendar_days"
            ),
            "test": (self.test_start.isoformat(), self.test_end.isoformat()),
            "train": (self.train_start.isoformat(), self.train_end.isoformat()),
            "validation": (
                self.validation_start.isoformat(),
                self.validation_end.isoformat(),
            ),
            "trading_session_calendar": calendar_payload,
            "trading_session_calendar_verification": (
                self.trading_session_calendar_verification.canonical_payload()
                if self.trading_session_calendar_verification is not None
                else None
            ),
        }

    def sha256(self) -> str:
        encoded = json.dumps(
            self.canonical_payload(),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def build_validation_plan(
    *,
    calendar_authority_verifier: Optional[TradingSessionCalendarAuthorityVerifier],
    **plan_fields: object,
) -> ValidationPlan:
    """Build a plan only after independently verifying its calendar authority."""

    if "trading_session_calendar_verification" in plan_fields:
        raise LifecycleContractError(
            "calendar_authority_verification_must_be_generated_by_builder"
        )
    market = plan_fields.get("market", "generic")
    _require_text(market, "market")  # type: ignore[arg-type]
    normalized_market = str(market).strip().lower()
    calendar = plan_fields.get("trading_session_calendar")
    frozen_at = plan_fields.get("frozen_at")
    if not isinstance(frozen_at, datetime):
        raise LifecycleContractError("frozen_at_must_be_timezone_aware")
    proof: Optional[TradingSessionCalendarAuthorityVerification] = None
    if calendar is not None or normalized_market in _ASHARE_MARKETS:
        if not isinstance(calendar, TradingSessionCalendarAuthority):
            if normalized_market in _ASHARE_MARKETS:
                raise LifecycleContractError("ashare_trading_session_calendar_required")
            raise LifecycleContractError("trading_session_calendar_type_invalid")
        if calendar_authority_verifier is None:
            raise LifecycleContractError("calendar_authority_verifier_required")
        verifier_id = getattr(calendar_authority_verifier, "verifier_id", None)
        verifier_version = getattr(
            calendar_authority_verifier,
            "verifier_version",
            None,
        )
        _require_text(verifier_id, "calendar_authority_verifier_id")
        _require_text(
            verifier_version,
            "calendar_authority_verifier_version",
        )
        verify = getattr(calendar_authority_verifier, "verify", None)
        if not callable(verify):
            raise LifecycleContractError("calendar_authority_verifier_invalid")
        try:
            proof = verify(calendar, frozen_at=frozen_at)
        except LifecycleContractError:
            raise
        except Exception as exc:
            raise LifecycleContractError(
                "calendar_authority_verification_failed"
            ) from exc
        _validate_calendar_authority_binding(
            calendar,
            proof,
            frozen_at=frozen_at,
            verifier_id=verifier_id,
            verifier_version=verifier_version,
        )
    return ValidationPlan(
        **plan_fields,  # type: ignore[arg-type]
        trading_session_calendar_verification=proof,
    )


class ModelLifecycleState(str, Enum):
    DRAFT = "draft"
    BACKTEST = "backtest"
    SHADOW = "shadow"
    REVIEW = "review"
    CURRENT = "current"
    QUARANTINE = "quarantine"
    RETIRED = "retired"


class LifecycleActor(str, Enum):
    HUMAN_REVIEWER = "human_reviewer"
    AUTOMATION = "automation"


_MANUAL_TRANSITIONS = {
    ModelLifecycleState.DRAFT: frozenset(
        {
            ModelLifecycleState.BACKTEST,
            ModelLifecycleState.QUARANTINE,
            ModelLifecycleState.RETIRED,
        }
    ),
    ModelLifecycleState.BACKTEST: frozenset(
        {
            ModelLifecycleState.SHADOW,
            ModelLifecycleState.QUARANTINE,
            ModelLifecycleState.RETIRED,
        }
    ),
    ModelLifecycleState.SHADOW: frozenset(
        {
            ModelLifecycleState.REVIEW,
            ModelLifecycleState.QUARANTINE,
            ModelLifecycleState.RETIRED,
        }
    ),
    ModelLifecycleState.REVIEW: frozenset(
        {
            ModelLifecycleState.CURRENT,
            ModelLifecycleState.QUARANTINE,
            ModelLifecycleState.RETIRED,
        }
    ),
    ModelLifecycleState.CURRENT: frozenset(
        {ModelLifecycleState.QUARANTINE, ModelLifecycleState.RETIRED}
    ),
    ModelLifecycleState.QUARANTINE: frozenset({ModelLifecycleState.RETIRED}),
    ModelLifecycleState.RETIRED: frozenset(),
}


@dataclass(frozen=True)
class LifecycleRecord:
    manifest_sha256: str
    model_id: str
    model_version: str
    research_snapshot_sha256: str
    catalog_version: str
    validation_plan_sha256: str
    validation_evidence_sha256: str
    state: ModelLifecycleState
    recorded_at: datetime
    transition_reason: str
    approval_reference: Optional[str] = None
    capital_layer: str = "simulated"
    account_type: str = "simulated"
    real_trading_enabled: bool = False
    live_transition_authorized: bool = False
    automatic_promotion_enabled: bool = False
    automatic_risk_expansion_enabled: bool = False

    def __post_init__(self) -> None:
        if self.recorded_at.tzinfo is None or self.recorded_at.utcoffset() is None:
            raise LifecycleContractError("recorded_at_must_be_timezone_aware")
        _require_text(self.model_id, "model_id")
        _require_text(self.model_version, "model_version")
        _require_text(self.catalog_version, "catalog_version")
        _require_text(self.transition_reason, "transition_reason")
        _require_sha256(self.manifest_sha256, "manifest_sha256")
        _require_sha256(self.research_snapshot_sha256, "research_snapshot_sha256")
        _require_sha256(self.validation_plan_sha256, "validation_plan_sha256")
        _require_sha256(
            self.validation_evidence_sha256,
            "validation_evidence_sha256",
        )
        if not isinstance(self.state, ModelLifecycleState):
            raise LifecycleContractError("lifecycle_state_invalid")
        if (
            self.capital_layer != "simulated"
            or self.account_type != "simulated"
            or self.real_trading_enabled is not False
            or self.live_transition_authorized is not False
            or not isinstance(self.automatic_promotion_enabled, bool)
            or self.automatic_risk_expansion_enabled is not False
        ):
            raise LifecycleContractError("simulation_only_contract_violated")
        if self.state is ModelLifecycleState.CURRENT and not self.approval_reference:
            raise LifecycleContractError("current_requires_manual_approval_reference")

    @classmethod
    def draft(
        cls,
        *,
        manifest: ModelReleaseManifest,
        recorded_at: datetime,
    ) -> "LifecycleRecord":
        if not isinstance(manifest, ModelReleaseManifest):
            raise LifecycleContractError("manifest_type_invalid")
        return cls(
            manifest_sha256=manifest.sha256(),
            model_id=manifest.model_id,
            model_version=manifest.model_version,
            research_snapshot_sha256=manifest.research_snapshot_sha256,
            catalog_version=manifest.catalog_version,
            validation_plan_sha256=manifest.validation_plan_sha256,
            validation_evidence_sha256=manifest.validation_evidence_sha256,
            state=ModelLifecycleState.DRAFT,
            recorded_at=recorded_at,
            transition_reason="manifest_registered",
        )


def transition_model(
    record: LifecycleRecord,
    *,
    target: ModelLifecycleState,
    actor: LifecycleActor,
    recorded_at: datetime,
    reason: str,
    approval_reference: Optional[str] = None,
) -> LifecycleRecord:
    """Return a new immutable record.

    Automation may follow the forward manual edges (never RETIRED) and may
    enter CURRENT only when bound to a valid promotion evidence reference.
    """

    if not isinstance(record, LifecycleRecord):
        raise LifecycleContractError("lifecycle_record_invalid")
    if not isinstance(target, ModelLifecycleState):
        raise LifecycleContractError("lifecycle_target_invalid")
    if not isinstance(actor, LifecycleActor):
        raise LifecycleContractError("lifecycle_actor_invalid")
    if recorded_at.tzinfo is None or recorded_at.utcoffset() is None:
        raise LifecycleContractError("recorded_at_must_be_timezone_aware")
    if recorded_at < record.recorded_at:
        raise LifecycleContractError("transition_time_precedes_current_record")
    _require_text(reason, "reason")

    if actor is LifecycleActor.AUTOMATION:
        automatic_targets = _MANUAL_TRANSITIONS[record.state] - {
            ModelLifecycleState.RETIRED,
        }
        if target not in automatic_targets:
            raise LifecycleContractError("automatic_action_forbidden")
        if target is ModelLifecycleState.CURRENT and not (
            is_promotion_evidence_reference(approval_reference)
        ):
            raise LifecycleContractError(
                "automatic_current_requires_promotion_evidence_reference"
            )
    elif target not in _MANUAL_TRANSITIONS[record.state]:
        raise LifecycleContractError("lifecycle_transition_forbidden")

    if target is ModelLifecycleState.CURRENT:
        if actor is LifecycleActor.HUMAN_REVIEWER and not approval_reference:
            raise LifecycleContractError("current_requires_manual_approval_reference")

    return replace(
        record,
        state=target,
        recorded_at=recorded_at,
        transition_reason=reason,
        approval_reference=approval_reference,
        automatic_promotion_enabled=(
            record.automatic_promotion_enabled
            or (
                actor is LifecycleActor.AUTOMATION
                and target is ModelLifecycleState.CURRENT
            )
        ),
    )
