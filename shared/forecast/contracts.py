"""Scientific contracts for uncalibrated forecasts and detached calibration.

The V1 capital path remains the frozen deterministic Champion.  Objects in
this module are research artifacts only and never supply target weights,
quantities, risk multipliers or orders.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol, Tuple

from shared.opportunity.contracts import OpportunitySnapshot


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
SUPPORTED_HORIZONS = ("m30", "m60", "close", "1d", "3d", "5d")
_HORIZON_ORDER = {value: index for index, value in enumerate(SUPPORTED_HORIZONS)}
MIN_CALIBRATION_EFFECTIVE_SAMPLES = 40


class ForecastContractError(ValueError):
    """Raised when forecast or calibration evidence is not safely bound."""


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ForecastContractError(f"{field_name}_invalid")
    return value


def _aware(value: object, field_name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ForecastContractError(f"{field_name}_timezone_required")
    return value.astimezone(timezone.utc)


def _sha(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _sha_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ForecastContractError(f"{field_name}_invalid")
    return value


def _finite(value: object, field_name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise ForecastContractError(f"{field_name}_invalid")
    return float(value)


def _probability(value: object, field_name: str) -> float:
    normalized = _finite(value, field_name)
    if not 0.0 <= normalized <= 1.0:
        raise ForecastContractError(f"{field_name}_invalid")
    return normalized


def _horizon(value: object) -> str:
    horizon = _text(value, "horizon")
    if horizon not in _HORIZON_ORDER:
        raise ForecastContractError("forecast_horizon_not_supported")
    return horizon


@dataclass(frozen=True)
class HorizonForecast:
    """Uncalibrated return quantiles for one existing label horizon."""

    horizon: str
    q10: float
    q25: float
    q50: float
    q75: float
    q90: float
    positive_probability: None = None
    outperform_probability: None = None
    score_semantics: str = "uncalibrated_return_quantiles"

    def __post_init__(self) -> None:
        object.__setattr__(self, "horizon", _horizon(self.horizon))
        values = tuple(
            _finite(getattr(self, field_name), field_name)
            for field_name in ("q10", "q25", "q50", "q75", "q90")
        )
        if tuple(sorted(values)) != values:
            raise ForecastContractError("return_quantiles_not_monotonic")
        for field_name, value in zip(("q10", "q25", "q50", "q75", "q90"), values):
            object.__setattr__(self, field_name, value)
        if (
            self.positive_probability is not None
            or self.outperform_probability is not None
        ):
            raise ForecastContractError("uncalibrated_probability_forbidden")
        if self.score_semantics != "uncalibrated_return_quantiles":
            raise ForecastContractError("forecast_score_semantics_invalid")

    def canonical_payload(self) -> dict[str, object]:
        return {
            "horizon": self.horizon,
            "outperform_probability": None,
            "positive_probability": None,
            "q10": self.q10,
            "q25": self.q25,
            "q50": self.q50,
            "q75": self.q75,
            "q90": self.q90,
            "score_semantics": self.score_semantics,
        }


@dataclass(frozen=True)
class EventHazardEstimate:
    """An explicitly uncalibrated event score with censoring semantics."""

    event_definition_id: str
    event_definition_version: str
    window_start: datetime
    window_end: datetime
    censoring_policy_id: str
    competing_risk_policy_id: str
    uncalibrated_hazard_score: float
    event_probability: None = None
    score_semantics: str = "uncalibrated_event_hazard_score"

    def __post_init__(self) -> None:
        for field_name in (
            "event_definition_id",
            "event_definition_version",
            "censoring_policy_id",
            "competing_risk_policy_id",
        ):
            _text(getattr(self, field_name), field_name)
        start = _aware(self.window_start, "window_start")
        end = _aware(self.window_end, "window_end")
        object.__setattr__(self, "window_start", start)
        object.__setattr__(self, "window_end", end)
        if start >= end:
            raise ForecastContractError("event_hazard_window_invalid")
        object.__setattr__(
            self,
            "uncalibrated_hazard_score",
            _probability(
                self.uncalibrated_hazard_score,
                "uncalibrated_hazard_score",
            ),
        )
        if self.event_probability is not None:
            raise ForecastContractError("uncalibrated_event_probability_forbidden")
        if self.score_semantics != "uncalibrated_event_hazard_score":
            raise ForecastContractError("event_hazard_semantics_invalid")

    def canonical_payload(self) -> dict[str, object]:
        return {
            "censoring_policy_id": self.censoring_policy_id,
            "competing_risk_policy_id": self.competing_risk_policy_id,
            "event_definition_id": self.event_definition_id,
            "event_definition_version": self.event_definition_version,
            "event_probability": None,
            "score_semantics": self.score_semantics,
            "uncalibrated_hazard_score": self.uncalibrated_hazard_score,
            "window_end": self.window_end.isoformat(),
            "window_start": self.window_start.isoformat(),
        }


@dataclass(frozen=True)
class MultiHorizonForecastSnapshot:
    """Content-addressed shadow forecast bound to frozen research evidence."""

    opportunity: OpportunitySnapshot
    generated_at: datetime
    research_snapshot_sha256: str
    model_release_manifest_sha256: str
    validation_plan_sha256: str
    frozen_oos_receipt_sha256: str
    model_id: str
    model_version: str
    forecasts: Tuple[HorizonForecast, ...]
    event_hazard: EventHazardEstimate | None = None
    schema_version: str = "tradingagent.multihorizon_forecast_snapshot.v1"
    score_semantics: str = "uncalibrated_return_quantiles"
    calibrated: bool = False
    shadow_only: bool = True
    position_effect_allowed: bool = False
    order_effect_allowed: bool = False
    promotion_eligible: bool = False
    snapshot_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.opportunity, OpportunitySnapshot):
            raise ForecastContractError("opportunity_snapshot_required")
        generated_at = _aware(self.generated_at, "generated_at")
        object.__setattr__(self, "generated_at", generated_at)
        if generated_at != self.opportunity.decision_time:
            raise ForecastContractError("forecast_generation_time_mismatch")
        for field_name in (
            "research_snapshot_sha256",
            "model_release_manifest_sha256",
            "validation_plan_sha256",
            "frozen_oos_receipt_sha256",
        ):
            _sha_text(getattr(self, field_name), field_name)
        _text(self.model_id, "model_id")
        _text(self.model_version, "model_version")
        if not isinstance(self.forecasts, tuple) or not self.forecasts:
            raise ForecastContractError("forecast_set_invalid")
        if any(not isinstance(item, HorizonForecast) for item in self.forecasts):
            raise ForecastContractError("forecast_set_invalid")
        horizons = tuple(item.horizon for item in self.forecasts)
        if len(horizons) != len(set(horizons)):
            raise ForecastContractError("forecast_horizon_duplicate")
        ordered = tuple(
            sorted(self.forecasts, key=lambda item: _HORIZON_ORDER[item.horizon])
        )
        object.__setattr__(self, "forecasts", ordered)
        if self.event_hazard is not None:
            if not isinstance(self.event_hazard, EventHazardEstimate):
                raise ForecastContractError("event_hazard_invalid")
            if self.event_hazard.window_start < self.opportunity.decision_time:
                raise ForecastContractError("event_hazard_uses_predecision_window")
        if (
            self.schema_version != "tradingagent.multihorizon_forecast_snapshot.v1"
            or self.score_semantics != "uncalibrated_return_quantiles"
            or self.calibrated is not False
            or self.shadow_only is not True
            or self.position_effect_allowed is not False
            or self.order_effect_allowed is not False
            or self.promotion_eligible is not False
        ):
            raise ForecastContractError("forecast_shadow_boundary_invalid")
        object.__setattr__(self, "snapshot_sha256", _sha(self.canonical_payload()))

    def canonical_payload(self) -> dict[str, object]:
        return {
            "calibrated": False,
            "event_hazard": (
                self.event_hazard.canonical_payload()
                if self.event_hazard is not None
                else None
            ),
            "forecasts": [item.canonical_payload() for item in self.forecasts],
            "frozen_oos_receipt_sha256": self.frozen_oos_receipt_sha256,
            "generated_at": self.generated_at.isoformat(),
            "model_id": self.model_id,
            "model_release_manifest_sha256": self.model_release_manifest_sha256,
            "model_version": self.model_version,
            "opportunity_snapshot_sha256": self.opportunity.snapshot_sha256,
            "order_effect_allowed": False,
            "position_effect_allowed": False,
            "promotion_eligible": False,
            "research_snapshot_sha256": self.research_snapshot_sha256,
            "schema_version": self.schema_version,
            "score_semantics": self.score_semantics,
            "shadow_only": True,
            "validation_plan_sha256": self.validation_plan_sha256,
        }


@dataclass(frozen=True)
class CalibratedHorizonProbability:
    horizon: str
    positive_probability: float
    outperform_probability: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "horizon", _horizon(self.horizon))
        object.__setattr__(
            self,
            "positive_probability",
            _probability(self.positive_probability, "positive_probability"),
        )
        object.__setattr__(
            self,
            "outperform_probability",
            _probability(self.outperform_probability, "outperform_probability"),
        )

    def canonical_payload(self) -> dict[str, object]:
        return {
            "horizon": self.horizon,
            "outperform_probability": self.outperform_probability,
            "positive_probability": self.positive_probability,
        }

    @staticmethod
    def set_sha256(values: Tuple["CalibratedHorizonProbability", ...]) -> str:
        if not isinstance(values, tuple) or not values:
            raise ForecastContractError("calibrated_probability_set_invalid")
        if any(not isinstance(item, CalibratedHorizonProbability) for item in values):
            raise ForecastContractError("calibrated_probability_set_invalid")
        ordered = sorted(values, key=lambda item: _HORIZON_ORDER[item.horizon])
        return _sha([item.canonical_payload() for item in ordered])


@dataclass(frozen=True)
class CalibrationAuthorityVerification:
    accepted: bool
    verifier_id: str
    production_eligible: bool
    proof_sha256: str
    verified_at: datetime
    forecast_snapshot_sha256: str
    probability_set_sha256: str
    validation_plan_sha256: str
    frozen_oos_receipt_sha256: str
    effective_independent_sample_count: int
    brier_score: float
    log_loss: float
    expected_calibration_error: float
    valid_until: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.accepted, bool):
            raise ForecastContractError("calibration_acceptance_invalid")
        if self.production_eligible is not False:
            raise ForecastContractError("calibration_production_authority_forbidden")
        _text(self.verifier_id, "verifier_id")
        for field_name in (
            "proof_sha256",
            "forecast_snapshot_sha256",
            "probability_set_sha256",
            "validation_plan_sha256",
            "frozen_oos_receipt_sha256",
        ):
            _sha_text(getattr(self, field_name), field_name)
        verified_at = _aware(self.verified_at, "verified_at")
        valid_until = _aware(self.valid_until, "valid_until")
        object.__setattr__(self, "verified_at", verified_at)
        object.__setattr__(self, "valid_until", valid_until)
        if verified_at >= valid_until:
            raise ForecastContractError("calibration_validity_window_invalid")
        if (
            isinstance(self.effective_independent_sample_count, bool)
            or not isinstance(self.effective_independent_sample_count, int)
            or self.effective_independent_sample_count < 0
        ):
            raise ForecastContractError("calibration_sample_count_invalid")
        object.__setattr__(
            self,
            "brier_score",
            _probability(self.brier_score, "brier_score"),
        )
        log_loss = _finite(self.log_loss, "log_loss")
        if log_loss < 0:
            raise ForecastContractError("log_loss_invalid")
        object.__setattr__(self, "log_loss", log_loss)
        object.__setattr__(
            self,
            "expected_calibration_error",
            _probability(
                self.expected_calibration_error,
                "expected_calibration_error",
            ),
        )

    def canonical_payload(self) -> dict[str, object]:
        return {
            "accepted": self.accepted,
            "brier_score": self.brier_score,
            "effective_independent_sample_count": (
                self.effective_independent_sample_count
            ),
            "expected_calibration_error": self.expected_calibration_error,
            "forecast_snapshot_sha256": self.forecast_snapshot_sha256,
            "frozen_oos_receipt_sha256": self.frozen_oos_receipt_sha256,
            "log_loss": self.log_loss,
            "probability_set_sha256": self.probability_set_sha256,
            "production_eligible": False,
            "proof_sha256": self.proof_sha256,
            "valid_until": self.valid_until.isoformat(),
            "validation_plan_sha256": self.validation_plan_sha256,
            "verified_at": self.verified_at.isoformat(),
            "verifier_id": self.verifier_id,
        }


class CalibrationAuthorityVerifier(Protocol):
    verifier_id: str
    production_eligible: bool

    def verify(
        self,
        snapshot: MultiHorizonForecastSnapshot,
        probabilities: Tuple[CalibratedHorizonProbability, ...],
        *,
        as_of: datetime,
    ) -> CalibrationAuthorityVerification:
        """Verify the exact frozen OOS calibration artifact."""


@dataclass(frozen=True)
class CalibratedForecastResearchArtifact:
    forecast_snapshot: MultiHorizonForecastSnapshot
    probabilities: Tuple[CalibratedHorizonProbability, ...]
    calibration_proof: CalibrationAuthorityVerification
    as_of: datetime
    schema_version: str = "tradingagent.calibrated_forecast_research.v1"
    calibrated: bool = True
    research_only: bool = True
    production_eligible: bool = False
    position_effect_allowed: bool = False
    order_effect_allowed: bool = False
    promotion_eligible: bool = False
    forecast_snapshot_sha256: str = field(init=False)
    artifact_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.forecast_snapshot, MultiHorizonForecastSnapshot):
            raise ForecastContractError("forecast_snapshot_required")
        instant = _aware(self.as_of, "as_of")
        object.__setattr__(self, "as_of", instant)
        if instant < self.forecast_snapshot.generated_at:
            raise ForecastContractError("calibration_as_of_before_forecast")
        if not isinstance(self.probabilities, tuple) or not self.probabilities:
            raise ForecastContractError("calibrated_probability_set_invalid")
        if any(
            not isinstance(item, CalibratedHorizonProbability)
            for item in self.probabilities
        ):
            raise ForecastContractError("calibrated_probability_set_invalid")
        ordered = tuple(
            sorted(
                self.probabilities,
                key=lambda item: _HORIZON_ORDER[item.horizon],
            )
        )
        probability_horizons = tuple(item.horizon for item in ordered)
        if len(probability_horizons) != len(set(probability_horizons)):
            raise ForecastContractError("calibrated_probability_horizon_duplicate")
        forecast_horizons = tuple(
            item.horizon for item in self.forecast_snapshot.forecasts
        )
        if probability_horizons != forecast_horizons:
            raise ForecastContractError("calibrated_probability_horizon_mismatch")
        object.__setattr__(self, "probabilities", ordered)
        if not isinstance(self.calibration_proof, CalibrationAuthorityVerification):
            raise ForecastContractError("calibration_proof_invalid")
        probability_sha = CalibratedHorizonProbability.set_sha256(ordered)
        expected_proof_binding = (
            True,
            False,
            self.forecast_snapshot.snapshot_sha256,
            probability_sha,
            self.forecast_snapshot.validation_plan_sha256,
            self.forecast_snapshot.frozen_oos_receipt_sha256,
        )
        actual_proof_binding = (
            self.calibration_proof.accepted,
            self.calibration_proof.production_eligible,
            self.calibration_proof.forecast_snapshot_sha256,
            self.calibration_proof.probability_set_sha256,
            self.calibration_proof.validation_plan_sha256,
            self.calibration_proof.frozen_oos_receipt_sha256,
        )
        if actual_proof_binding != expected_proof_binding:
            raise ForecastContractError("calibration_proof_binding_mismatch")
        if (
            self.calibration_proof.verified_at > instant
            or self.calibration_proof.valid_until < instant
        ):
            raise ForecastContractError("calibration_proof_time_invalid")
        if (
            self.calibration_proof.effective_independent_sample_count
            < MIN_CALIBRATION_EFFECTIVE_SAMPLES
        ):
            raise ForecastContractError("calibration_sample_count_insufficient")
        if (
            self.schema_version != "tradingagent.calibrated_forecast_research.v1"
            or self.calibrated is not True
            or self.research_only is not True
            or self.production_eligible is not False
            or self.position_effect_allowed is not False
            or self.order_effect_allowed is not False
            or self.promotion_eligible is not False
        ):
            raise ForecastContractError("calibrated_research_boundary_invalid")
        object.__setattr__(
            self,
            "forecast_snapshot_sha256",
            self.forecast_snapshot.snapshot_sha256,
        )
        object.__setattr__(self, "artifact_sha256", _sha(self.canonical_payload()))

    def canonical_payload(self) -> dict[str, object]:
        return {
            "as_of": self.as_of.isoformat(),
            "calibrated": True,
            "calibration_proof": self.calibration_proof.canonical_payload(),
            "forecast_snapshot_sha256": self.forecast_snapshot_sha256,
            "order_effect_allowed": False,
            "position_effect_allowed": False,
            "probabilities": [item.canonical_payload() for item in self.probabilities],
            "production_eligible": False,
            "promotion_eligible": False,
            "research_only": True,
            "schema_version": self.schema_version,
        }


def attach_calibrated_probabilities(
    snapshot: MultiHorizonForecastSnapshot,
    probabilities: Tuple[CalibratedHorizonProbability, ...],
    *,
    as_of: datetime,
    calibration_verifier: CalibrationAuthorityVerifier | None,
) -> CalibratedForecastResearchArtifact:
    """Attach probabilities only after a detached, frozen-OOS verification."""

    if not isinstance(snapshot, MultiHorizonForecastSnapshot):
        raise ForecastContractError("forecast_snapshot_required")
    instant = _aware(as_of, "as_of")
    if instant < snapshot.generated_at:
        raise ForecastContractError("calibration_as_of_before_forecast")
    if calibration_verifier is None:
        raise ForecastContractError("calibration_authority_verifier_required")
    verifier_id = _text(
        getattr(calibration_verifier, "verifier_id", None),
        "calibration_verifier_id",
    )
    if getattr(calibration_verifier, "production_eligible", None) is not False:
        raise ForecastContractError("calibration_verifier_boundary_invalid")
    verify = getattr(calibration_verifier, "verify", None)
    if not callable(verify):
        raise ForecastContractError("calibration_verifier_invalid")
    probability_sha = CalibratedHorizonProbability.set_sha256(probabilities)
    probability_horizons = tuple(
        item.horizon
        for item in sorted(probabilities, key=lambda item: _HORIZON_ORDER[item.horizon])
    )
    forecast_horizons = tuple(item.horizon for item in snapshot.forecasts)
    if probability_horizons != forecast_horizons:
        raise ForecastContractError("calibrated_probability_horizon_mismatch")
    try:
        proof = verify(snapshot, probabilities, as_of=instant)
    except ForecastContractError:
        raise
    except Exception as exc:
        raise ForecastContractError("calibration_verification_failed") from exc
    if not isinstance(proof, CalibrationAuthorityVerification):
        raise ForecastContractError("calibration_proof_invalid")
    expected = (
        verifier_id,
        snapshot.snapshot_sha256,
        probability_sha,
        snapshot.validation_plan_sha256,
        snapshot.frozen_oos_receipt_sha256,
    )
    actual = (
        proof.verifier_id,
        proof.forecast_snapshot_sha256,
        proof.probability_set_sha256,
        proof.validation_plan_sha256,
        proof.frozen_oos_receipt_sha256,
    )
    if actual != expected or proof.accepted is not True:
        raise ForecastContractError("calibration_proof_binding_mismatch")
    if proof.verified_at > instant or proof.valid_until < instant:
        raise ForecastContractError("calibration_proof_time_invalid")
    if proof.effective_independent_sample_count < MIN_CALIBRATION_EFFECTIVE_SAMPLES:
        raise ForecastContractError("calibration_sample_count_insufficient")
    ordered = tuple(
        sorted(probabilities, key=lambda item: _HORIZON_ORDER[item.horizon])
    )
    return CalibratedForecastResearchArtifact(
        forecast_snapshot=snapshot,
        probabilities=ordered,
        calibration_proof=proof,
        as_of=instant,
    )


__all__ = [
    "CalibrationAuthorityVerification",
    "CalibrationAuthorityVerifier",
    "CalibratedForecastResearchArtifact",
    "CalibratedHorizonProbability",
    "EventHazardEstimate",
    "ForecastContractError",
    "HorizonForecast",
    "MIN_CALIBRATION_EFFECTIVE_SAMPLES",
    "MultiHorizonForecastSnapshot",
    "SUPPORTED_HORIZONS",
    "attach_calibrated_probabilities",
]
