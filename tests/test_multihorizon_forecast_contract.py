from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from shared.forecast.contracts import (
    CalibrationAuthorityVerification,
    CalibratedForecastResearchArtifact,
    CalibratedHorizonProbability,
    EventHazardEstimate,
    ForecastContractError,
    HorizonForecast,
    MultiHorizonForecastSnapshot,
    attach_calibrated_probabilities,
)
from shared.opportunity.contracts import (
    OpportunityEvidenceRef,
    OpportunityScope,
    OpportunitySnapshot,
    OpportunityState,
)


UTC = timezone.utc
DECISION_TIME = datetime(2026, 7, 16, 1, 30, tzinfo=UTC)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _opportunity() -> OpportunitySnapshot:
    evidence = OpportunityEvidenceRef(
        evidence_id="evidence-a",
        dataset_id="ashare.daily.market.v1",
        receipt_id="receipt-a",
        lineage_id="lineage-a",
        evidence_group_id="price-volume",
        data_through=DECISION_TIME - timedelta(minutes=20),
        available_at=DECISION_TIME - timedelta(minutes=10),
        expires_at=DECISION_TIME + timedelta(hours=3),
        payload_sha256="a" * 64,
    )
    return OpportunitySnapshot.create(
        opportunity_id="opportunity-600000",
        scope=OpportunityScope.STOCK,
        entity_id="600000.SH",
        thesis_id="thesis-600000",
        state=OpportunityState.FORMING,
        decision_time=DECISION_TIME,
        discovered_at=DECISION_TIME,
        trigger_window_start=DECISION_TIME,
        trigger_window_end=DECISION_TIME + timedelta(days=5),
        horizon="5d",
        uncalibrated_hazard_score=0.62,
        priced_in_score=0.25,
        evidence_refs=(evidence,),
        invalidation_conditions=("evidence_expires",),
        reason_codes=("leading_evidence_detected",),
    )


def _forecast(horizon: str, scale: float) -> HorizonForecast:
    return HorizonForecast(
        horizon=horizon,
        q10=-0.05 * scale,
        q25=-0.01 * scale,
        q50=0.01 * scale,
        q75=0.03 * scale,
        q90=0.07 * scale,
    )


def _snapshot() -> MultiHorizonForecastSnapshot:
    opportunity = _opportunity()
    return MultiHorizonForecastSnapshot(
        opportunity=opportunity,
        generated_at=DECISION_TIME,
        research_snapshot_sha256="b" * 64,
        model_release_manifest_sha256="c" * 64,
        validation_plan_sha256="d" * 64,
        frozen_oos_receipt_sha256="e" * 64,
        model_id="shadow-multihorizon-model",
        model_version="1",
        forecasts=(
            _forecast("1d", 0.5),
            _forecast("3d", 0.8),
            _forecast("5d", 1.0),
        ),
        event_hazard=EventHazardEstimate(
            event_definition_id="customer-certification",
            event_definition_version="1",
            window_start=DECISION_TIME,
            window_end=DECISION_TIME + timedelta(days=30),
            censoring_policy_id="right-censor-at-window-end-v1",
            competing_risk_policy_id="delay-failure-competing-risk-v1",
            uncalibrated_hazard_score=0.58,
        ),
    )


def test_multihorizon_snapshot_is_uncalibrated_shadow_research_only() -> None:
    snapshot = _snapshot()

    assert tuple(item.horizon for item in snapshot.forecasts) == ("1d", "3d", "5d")
    assert snapshot.score_semantics == "uncalibrated_return_quantiles"
    assert all(item.positive_probability is None for item in snapshot.forecasts)
    assert snapshot.shadow_only is True
    assert snapshot.position_effect_allowed is False
    assert snapshot.order_effect_allowed is False
    assert snapshot.promotion_eligible is False
    assert snapshot.event_hazard is not None
    assert snapshot.event_hazard.event_probability is None
    assert len(snapshot.snapshot_sha256) == 64


def test_quantiles_horizons_and_generation_time_fail_closed() -> None:
    with pytest.raises(ForecastContractError, match="return_quantiles_not_monotonic"):
        HorizonForecast(
            horizon="1d",
            q10=-0.01,
            q25=0.02,
            q50=0.01,
            q75=0.03,
            q90=0.04,
        )

    base = _snapshot()
    with pytest.raises(ForecastContractError, match="forecast_horizon_duplicate"):
        replace(base, forecasts=(_forecast("1d", 1.0), _forecast("1d", 1.2)))
    with pytest.raises(
        ForecastContractError, match="forecast_generation_time_mismatch"
    ):
        replace(base, generated_at=DECISION_TIME + timedelta(seconds=1))
    with pytest.raises(
        ForecastContractError, match="forecast_generation_time_mismatch"
    ):
        replace(base, generated_at=DECISION_TIME - timedelta(seconds=1))
    with pytest.raises(ForecastContractError, match="forecast_horizon_not_supported"):
        replace(base, forecasts=(_forecast("20d", 1.0),))


class _CalibrationVerifier:
    verifier_id = "fixture-calibration-verifier-v1"
    production_eligible = False

    def __init__(self, snapshot: MultiHorizonForecastSnapshot) -> None:
        self.snapshot = snapshot

    def verify(
        self,
        snapshot: MultiHorizonForecastSnapshot,
        probabilities: tuple[CalibratedHorizonProbability, ...],
        *,
        as_of: datetime,
    ) -> CalibrationAuthorityVerification:
        payload_sha = CalibratedHorizonProbability.set_sha256(probabilities)
        return CalibrationAuthorityVerification(
            accepted=snapshot.snapshot_sha256 == self.snapshot.snapshot_sha256,
            verifier_id=self.verifier_id,
            production_eligible=False,
            proof_sha256=_sha(snapshot.snapshot_sha256 + payload_sha),
            verified_at=as_of,
            forecast_snapshot_sha256=self.snapshot.snapshot_sha256,
            probability_set_sha256=payload_sha,
            validation_plan_sha256=self.snapshot.validation_plan_sha256,
            frozen_oos_receipt_sha256=self.snapshot.frozen_oos_receipt_sha256,
            effective_independent_sample_count=80,
            brier_score=0.18,
            log_loss=0.61,
            expected_calibration_error=0.04,
            valid_until=as_of + timedelta(days=7),
        )


def _probabilities() -> tuple[CalibratedHorizonProbability, ...]:
    return (
        CalibratedHorizonProbability(
            horizon="1d",
            positive_probability=0.55,
            outperform_probability=0.52,
        ),
        CalibratedHorizonProbability(
            horizon="3d",
            positive_probability=0.59,
            outperform_probability=0.56,
        ),
        CalibratedHorizonProbability(
            horizon="5d",
            positive_probability=0.62,
            outperform_probability=0.58,
        ),
    )


def test_probability_attachment_requires_detached_calibration_authority() -> None:
    snapshot = _snapshot()
    with pytest.raises(
        ForecastContractError,
        match="calibration_authority_verifier_required",
    ):
        attach_calibrated_probabilities(
            snapshot,
            _probabilities(),
            as_of=DECISION_TIME,
            calibration_verifier=None,
        )

    artifact = attach_calibrated_probabilities(
        snapshot,
        _probabilities(),
        as_of=DECISION_TIME,
        calibration_verifier=_CalibrationVerifier(snapshot),
    )
    assert artifact.calibrated is True
    assert artifact.research_only is True
    assert artifact.production_eligible is False
    assert artifact.position_effect_allowed is False
    assert artifact.order_effect_allowed is False
    assert artifact.promotion_eligible is False

    with pytest.raises(
        ForecastContractError,
        match="calibration_as_of_before_forecast",
    ):
        attach_calibrated_probabilities(
            snapshot,
            _probabilities(),
            as_of=DECISION_TIME - timedelta(seconds=1),
            calibration_verifier=_CalibrationVerifier(snapshot),
        )


def test_calibration_proof_cannot_be_rebound_or_use_too_few_samples() -> None:
    snapshot = _snapshot()

    class _BadVerifier(_CalibrationVerifier):
        def verify(self, *args: object, **kwargs: object):
            proof = super().verify(*args, **kwargs)
            return replace(
                proof,
                forecast_snapshot_sha256="f" * 64,
                effective_independent_sample_count=10,
            )

    with pytest.raises(
        ForecastContractError,
        match="calibration_proof_binding_mismatch",
    ):
        attach_calibrated_probabilities(
            snapshot,
            _probabilities(),
            as_of=DECISION_TIME,
            calibration_verifier=_BadVerifier(snapshot),
        )


def test_event_hazard_requires_explicit_censoring_and_competing_risk_policy() -> None:
    with pytest.raises(ForecastContractError, match="censoring_policy_id_invalid"):
        EventHazardEstimate(
            event_definition_id="customer-certification",
            event_definition_version="1",
            window_start=DECISION_TIME,
            window_end=DECISION_TIME + timedelta(days=30),
            censoring_policy_id="",
            competing_risk_policy_id="delay-failure-competing-risk-v1",
            uncalibrated_hazard_score=0.58,
        )


def test_calibrated_artifact_direct_construction_rechecks_all_bindings() -> None:
    snapshot = _snapshot()
    probabilities = _probabilities()
    proof = _CalibrationVerifier(snapshot).verify(
        snapshot,
        probabilities,
        as_of=DECISION_TIME,
    )

    with pytest.raises(
        ForecastContractError,
        match="calibration_proof_binding_mismatch",
    ):
        CalibratedForecastResearchArtifact(
            forecast_snapshot=snapshot,
            probabilities=probabilities,
            calibration_proof=replace(proof, probability_set_sha256="f" * 64),
            as_of=DECISION_TIME,
        )

    with pytest.raises(
        ForecastContractError,
        match="calibrated_probability_horizon_mismatch",
    ):
        CalibratedForecastResearchArtifact(
            forecast_snapshot=snapshot,
            probabilities=probabilities[:-1],
            calibration_proof=proof,
            as_of=DECISION_TIME,
        )
