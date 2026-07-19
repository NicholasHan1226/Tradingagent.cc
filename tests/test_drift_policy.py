from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from shared.models.drift_policy import (
    ALLOWED_AUTOMATIC_ACTIONS,
    DriftContractError,
    DriftEvidence,
    SafeAutomaticAction,
    evaluate_drift,
    evaluate_drift_as_of,
)


def _evidence(**overrides: object) -> DriftEvidence:
    payload: dict[str, object] = {
        "calibration_error": 0.01,
        "out_of_distribution_score": 0.05,
        "predicted_cost_error_ratio": 0.02,
        "data_degraded": False,
        "lineage_verified": True,
        "journal_head_sha256": "1" * 64,
        "model_manifest_sha256": "2" * 64,
        "metrics_artifact_sha256": "3" * 64,
        "metrics_implementation_version": "drift-metrics-v1",
        "window_start": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "window_end": datetime(2026, 6, 30, tzinfo=timezone.utc),
        "evaluated_at": datetime(2026, 7, 1, tzinfo=timezone.utc),
        "effective_independent_sample_count": 240,
    }
    payload.update(overrides)
    return DriftEvidence(**payload)  # type: ignore[arg-type]


def test_moderate_drift_can_only_reduce_risk_and_require_review() -> None:
    decision = evaluate_drift(
        _evidence(
            calibration_error=0.09,
            out_of_distribution_score=0.35,
            predicted_cost_error_ratio=0.25,
        )
    )

    assert decision.actions == (
        SafeAutomaticAction.REDUCE_ONLY,
        SafeAutomaticAction.REQUIRE_REVIEW,
    )
    assert set(decision.actions).issubset(ALLOWED_AUTOMATIC_ACTIONS)
    assert decision.risk_multiplier <= 1.0


def test_severe_drift_quarantines_and_stops_new_risk() -> None:
    decision = evaluate_drift(
        _evidence(
            calibration_error=0.22,
            out_of_distribution_score=0.92,
            predicted_cost_error_ratio=1.4,
            data_degraded=True,
            lineage_verified=False,
        )
    )

    assert decision.actions == (
        SafeAutomaticAction.QUARANTINE,
        SafeAutomaticAction.STOP_NEW_RISK,
        SafeAutomaticAction.REQUIRE_REVIEW,
    )
    assert decision.risk_multiplier == 0.0
    assert decision.automatic_promotion_enabled is False
    assert decision.automatic_risk_expansion_enabled is False
    assert decision.live_transition_authorized is False


def test_any_real_or_live_marker_fails_closed_to_quarantine() -> None:
    markers = (
        {"real_trading_enabled": True},
        {"live_transition_authorized": True},
        {"broker_connected": True},
        {"deployment_mode": "live"},
        {"capital_layer": "real"},
        {"deployment_mode": None},
        {"capital_layer": None},
    )

    for marker in markers:
        decision = evaluate_drift(_evidence(**marker))
        assert decision.actions == (
            SafeAutomaticAction.QUARANTINE,
            SafeAutomaticAction.STOP_NEW_RISK,
            SafeAutomaticAction.REQUIRE_REVIEW,
        )
        assert "real_or_live_marker_detected" in decision.reasons


def test_healthy_evidence_does_not_create_an_automatic_promotion_action() -> None:
    evidence = _evidence()
    decision = evaluate_drift(evidence)

    assert decision.actions == ()
    assert decision.risk_multiplier == 1.0
    assert decision.evidence_sha256 == evidence.sha256()
    assert decision.automatic_promotion_enabled is False
    assert decision.automatic_risk_expansion_enabled is False
    assert decision.live_transition_authorized is False


@pytest.mark.parametrize(
    ("field_name", "bad_value"),
    (
        ("journal_head_sha256", "missing"),
        ("model_manifest_sha256", "A" * 64),
        ("metrics_artifact_sha256", ""),
        ("metrics_implementation_version", ""),
        ("window_start", datetime(2026, 1, 1)),
        ("evaluated_at", datetime(2026, 7, 1)),
    ),
)
def test_drift_evidence_rejects_unverifiable_metric_provenance(
    field_name: str,
    bad_value: object,
) -> None:
    with pytest.raises(DriftContractError):
        _evidence(**{field_name: bad_value})


def test_insufficient_independent_sample_count_stops_new_risk() -> None:
    evidence = _evidence(effective_independent_sample_count=39)

    decision = evaluate_drift(evidence)

    assert decision.actions == (
        SafeAutomaticAction.STOP_NEW_RISK,
        SafeAutomaticAction.REQUIRE_REVIEW,
    )
    assert decision.risk_multiplier == 0.0
    assert decision.reasons == ("insufficient_independent_samples",)
    assert decision.evidence_sha256 == evidence.sha256()


def test_stale_or_reversed_metric_window_fails_closed() -> None:
    with pytest.raises(DriftContractError, match="window_range_invalid"):
        _evidence(
            window_start=datetime(2026, 6, 30, tzinfo=timezone.utc),
            window_end=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )

    evidence = _evidence(
        evaluated_at=datetime(2026, 8, 15, tzinfo=timezone.utc),
    )
    decision = evaluate_drift(evidence)
    assert decision.actions == (
        SafeAutomaticAction.STOP_NEW_RISK,
        SafeAutomaticAction.REQUIRE_REVIEW,
    )
    assert decision.reasons == ("metrics_window_stale",)


def test_metrics_evaluation_age_is_checked_against_trusted_as_of() -> None:
    evidence = _evidence()

    boundary = evaluate_drift_as_of(
        evidence,
        as_of=evidence.evaluated_at + timedelta(days=14),
    )
    stale = evaluate_drift_as_of(
        evidence,
        as_of=evidence.evaluated_at + timedelta(days=14, microseconds=1),
    )

    assert boundary.actions == ()
    assert stale.actions == (
        SafeAutomaticAction.STOP_NEW_RISK,
        SafeAutomaticAction.REQUIRE_REVIEW,
    )
    assert stale.risk_multiplier == 0.0
    assert stale.reasons == ("metrics_evaluation_stale",)


def test_stale_overlay_never_weakens_intrinsic_quarantine() -> None:
    evidence = _evidence(calibration_error=0.22)

    decision = evaluate_drift_as_of(
        evidence,
        as_of=evidence.evaluated_at + timedelta(days=15),
    )

    assert decision.actions == (
        SafeAutomaticAction.QUARANTINE,
        SafeAutomaticAction.STOP_NEW_RISK,
        SafeAutomaticAction.REQUIRE_REVIEW,
    )
    assert "severe_calibration_drift" in decision.reasons
    assert "metrics_evaluation_stale" in decision.reasons


@pytest.mark.parametrize(
    "as_of",
    (
        datetime(2026, 7, 1),
        datetime(2026, 6, 30, 23, 59, tzinfo=timezone.utc),
    ),
)
def test_trusted_as_of_must_be_aware_and_not_precede_evaluation(
    as_of: datetime,
) -> None:
    with pytest.raises(DriftContractError):
        evaluate_drift_as_of(_evidence(), as_of=as_of)
