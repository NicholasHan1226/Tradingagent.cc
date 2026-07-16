"""Pure fail-closed drift policy with only risk-reducing automatic actions."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import FrozenSet, Tuple


class DriftContractError(ValueError):
    """Raised for malformed drift metrics, never for live markers."""


class SafeAutomaticAction(str, Enum):
    QUARANTINE = "quarantine"
    REDUCE_ONLY = "reduce_only"
    REQUIRE_REVIEW = "require_review"
    STOP_NEW_RISK = "stop_new_risk"


ALLOWED_AUTOMATIC_ACTIONS: FrozenSet[SafeAutomaticAction] = frozenset(
    {
        SafeAutomaticAction.QUARANTINE,
        SafeAutomaticAction.REDUCE_ONLY,
        SafeAutomaticAction.REQUIRE_REVIEW,
        SafeAutomaticAction.STOP_NEW_RISK,
    }
)


def _require_nonnegative_finite(value: float, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DriftContractError("%s_must_be_nonnegative_finite" % field_name)
    if not math.isfinite(float(value)) or value < 0:
        raise DriftContractError("%s_must_be_nonnegative_finite" % field_name)


def _require_sha256(value: str, field_name: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise DriftContractError("%s_invalid" % field_name)


def _require_aware(value: datetime, field_name: str) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise DriftContractError("%s_must_be_timezone_aware" % field_name)


MIN_EFFECTIVE_INDEPENDENT_SAMPLES = 40
MAX_METRICS_STALENESS = timedelta(days=14)
MAX_METRICS_EVALUATION_AGE = timedelta(days=14)


@dataclass(frozen=True)
class DriftEvidence:
    calibration_error: float
    out_of_distribution_score: float
    predicted_cost_error_ratio: float
    data_degraded: bool
    lineage_verified: bool
    journal_head_sha256: str
    model_manifest_sha256: str
    metrics_artifact_sha256: str
    metrics_implementation_version: str
    window_start: datetime
    window_end: datetime
    evaluated_at: datetime
    effective_independent_sample_count: int
    capital_layer: str = "simulated"
    deployment_mode: str = "simulated"
    real_trading_enabled: bool = False
    live_transition_authorized: bool = False
    broker_connected: bool = False

    def __post_init__(self) -> None:
        _require_nonnegative_finite(self.calibration_error, "calibration_error")
        _require_nonnegative_finite(
            self.out_of_distribution_score, "out_of_distribution_score"
        )
        _require_nonnegative_finite(
            self.predicted_cost_error_ratio, "predicted_cost_error_ratio"
        )
        if self.out_of_distribution_score > 1:
            raise DriftContractError("out_of_distribution_score_out_of_range")
        if not isinstance(self.data_degraded, bool):
            raise DriftContractError("data_degraded_must_be_boolean")
        if not isinstance(self.lineage_verified, bool):
            raise DriftContractError("lineage_verified_must_be_boolean")
        for field_name in (
            "journal_head_sha256",
            "model_manifest_sha256",
            "metrics_artifact_sha256",
        ):
            _require_sha256(getattr(self, field_name), field_name)
        if (
            not isinstance(self.metrics_implementation_version, str)
            or not self.metrics_implementation_version.strip()
        ):
            raise DriftContractError(
                "metrics_implementation_version_must_be_nonempty_text"
            )
        for field_name in ("window_start", "window_end", "evaluated_at"):
            _require_aware(getattr(self, field_name), field_name)
        if self.window_start > self.window_end or self.window_end > self.evaluated_at:
            raise DriftContractError("window_range_invalid")
        if (
            isinstance(self.effective_independent_sample_count, bool)
            or not isinstance(self.effective_independent_sample_count, int)
            or self.effective_independent_sample_count < 0
        ):
            raise DriftContractError(
                "effective_independent_sample_count_must_be_nonnegative_integer"
            )

    def canonical_payload(self) -> dict:
        return {
            "broker_connected": self.broker_connected,
            "calibration_error": float(self.calibration_error),
            "capital_layer": self.capital_layer,
            "data_degraded": self.data_degraded,
            "deployment_mode": self.deployment_mode,
            "effective_independent_sample_count": self.effective_independent_sample_count,
            "evaluated_at": self.evaluated_at.isoformat(),
            "journal_head_sha256": self.journal_head_sha256,
            "lineage_verified": self.lineage_verified,
            "live_transition_authorized": self.live_transition_authorized,
            "metrics_artifact_sha256": self.metrics_artifact_sha256,
            "metrics_implementation_version": self.metrics_implementation_version,
            "model_manifest_sha256": self.model_manifest_sha256,
            "out_of_distribution_score": float(self.out_of_distribution_score),
            "predicted_cost_error_ratio": float(self.predicted_cost_error_ratio),
            "real_trading_enabled": self.real_trading_enabled,
            "window_end": self.window_end.isoformat(),
            "window_start": self.window_start.isoformat(),
        }

    def sha256(self) -> str:
        encoded = json.dumps(
            self.canonical_payload(),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class DriftDecision:
    actions: Tuple[SafeAutomaticAction, ...]
    risk_multiplier: float
    reasons: Tuple[str, ...]
    evidence_sha256: str
    automatic_promotion_enabled: bool = False
    automatic_risk_expansion_enabled: bool = False
    live_transition_authorized: bool = False

    def __post_init__(self) -> None:
        _require_sha256(self.evidence_sha256, "evidence_sha256")
        if not set(self.actions).issubset(ALLOWED_AUTOMATIC_ACTIONS):
            raise DriftContractError("unsafe_automatic_action")
        if not 0 <= self.risk_multiplier <= 1:
            raise DriftContractError("risk_multiplier_must_not_expand_risk")
        if (
            self.automatic_promotion_enabled is not False
            or self.automatic_risk_expansion_enabled is not False
            or self.live_transition_authorized is not False
        ):
            raise DriftContractError("unsafe_automatic_authority")


def _has_real_or_live_marker(evidence: DriftEvidence) -> bool:
    capital_layer_is_safe = (
        isinstance(evidence.capital_layer, str)
        and evidence.capital_layer.strip().lower() == "simulated"
    )
    deployment_mode_is_safe = isinstance(
        evidence.deployment_mode, str
    ) and evidence.deployment_mode.strip().lower() in {"simulated", "shadow"}
    return (
        evidence.real_trading_enabled is not False
        or evidence.live_transition_authorized is not False
        or evidence.broker_connected is not False
        or not capital_layer_is_safe
        or not deployment_mode_is_safe
    )


def _quarantine(evidence: DriftEvidence, *reasons: str) -> DriftDecision:
    return DriftDecision(
        actions=(
            SafeAutomaticAction.QUARANTINE,
            SafeAutomaticAction.STOP_NEW_RISK,
            SafeAutomaticAction.REQUIRE_REVIEW,
        ),
        risk_multiplier=0.0,
        reasons=tuple(reasons),
        evidence_sha256=evidence.sha256(),
    )


def evaluate_drift(evidence: DriftEvidence) -> DriftDecision:
    """Translate evidence into bounded, risk-reducing recommendations only."""

    if _has_real_or_live_marker(evidence):
        return _quarantine(evidence, "real_or_live_marker_detected")
    severe_reasons = []
    if not evidence.lineage_verified:
        severe_reasons.append("lineage_not_verified")
    if evidence.calibration_error >= 0.15:
        severe_reasons.append("severe_calibration_drift")
    if evidence.out_of_distribution_score >= 0.80:
        severe_reasons.append("severe_out_of_distribution")
    if evidence.predicted_cost_error_ratio >= 1.0:
        severe_reasons.append("severe_cost_model_drift")
    if severe_reasons:
        return _quarantine(evidence, *severe_reasons)
    if evidence.effective_independent_sample_count < MIN_EFFECTIVE_INDEPENDENT_SAMPLES:
        return DriftDecision(
            actions=(
                SafeAutomaticAction.STOP_NEW_RISK,
                SafeAutomaticAction.REQUIRE_REVIEW,
            ),
            risk_multiplier=0.0,
            reasons=("insufficient_independent_samples",),
            evidence_sha256=evidence.sha256(),
        )
    if evidence.evaluated_at - evidence.window_end > MAX_METRICS_STALENESS:
        return DriftDecision(
            actions=(
                SafeAutomaticAction.STOP_NEW_RISK,
                SafeAutomaticAction.REQUIRE_REVIEW,
            ),
            risk_multiplier=0.0,
            reasons=("metrics_window_stale",),
            evidence_sha256=evidence.sha256(),
        )
    if evidence.data_degraded:
        return DriftDecision(
            actions=(
                SafeAutomaticAction.STOP_NEW_RISK,
                SafeAutomaticAction.REQUIRE_REVIEW,
            ),
            risk_multiplier=0.0,
            reasons=("data_degraded",),
            evidence_sha256=evidence.sha256(),
        )
    moderate_reasons = []
    if evidence.calibration_error >= 0.05:
        moderate_reasons.append("moderate_calibration_drift")
    if evidence.out_of_distribution_score >= 0.25:
        moderate_reasons.append("moderate_out_of_distribution")
    if evidence.predicted_cost_error_ratio >= 0.20:
        moderate_reasons.append("moderate_cost_model_drift")
    if moderate_reasons:
        return DriftDecision(
            actions=(
                SafeAutomaticAction.REDUCE_ONLY,
                SafeAutomaticAction.REQUIRE_REVIEW,
            ),
            risk_multiplier=0.5,
            reasons=tuple(moderate_reasons),
            evidence_sha256=evidence.sha256(),
        )
    return DriftDecision(
        actions=(),
        risk_multiplier=1.0,
        reasons=("healthy",),
        evidence_sha256=evidence.sha256(),
    )


def evaluate_drift_as_of(
    evidence: DriftEvidence,
    *,
    as_of: datetime,
) -> DriftDecision:
    """Overlay trusted-current-time freshness without weakening base actions."""

    _require_aware(as_of, "as_of")
    if as_of < evidence.evaluated_at:
        raise DriftContractError("as_of_before_metrics_evaluation")
    base = evaluate_drift(evidence)
    if as_of - evidence.evaluated_at <= MAX_METRICS_EVALUATION_AGE:
        return base

    base_actions = set(base.actions)
    if SafeAutomaticAction.QUARANTINE in base_actions:
        actions = (
            SafeAutomaticAction.QUARANTINE,
            SafeAutomaticAction.STOP_NEW_RISK,
            SafeAutomaticAction.REQUIRE_REVIEW,
        )
    else:
        actions = (
            SafeAutomaticAction.STOP_NEW_RISK,
            SafeAutomaticAction.REQUIRE_REVIEW,
        )
    reasons = tuple(reason for reason in base.reasons if reason != "healthy")
    if "metrics_evaluation_stale" not in reasons:
        reasons = (*reasons, "metrics_evaluation_stale")
    return DriftDecision(
        actions=actions,
        risk_multiplier=0.0,
        reasons=reasons,
        evidence_sha256=base.evidence_sha256,
    )
