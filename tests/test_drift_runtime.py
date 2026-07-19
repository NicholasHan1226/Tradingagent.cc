from __future__ import annotations

from datetime import datetime, timezone

import pytest

from shared.models.drift_action_store import DriftActionStore
from shared.models.drift_policy import DriftEvidence, evaluate_drift
from shared.models.drift_runtime import (
    DriftRuntimeContractError,
    DriftRuntimeRiskAdapter,
)


def _decision(calibration_error: float):
    evidence = DriftEvidence(
        calibration_error=calibration_error,
        out_of_distribution_score=0.05,
        predicted_cost_error_ratio=0.02,
        data_degraded=False,
        lineage_verified=True,
        journal_head_sha256="1" * 64,
        model_manifest_sha256="2" * 64,
        metrics_artifact_sha256="3" * 64,
        metrics_implementation_version="drift-metrics-v1",
        window_start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        window_end=datetime(2026, 6, 30, tzinfo=timezone.utc),
        evaluated_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        effective_independent_sample_count=240,
    )
    return evaluate_drift(evidence)


def test_runtime_adapter_requires_explicit_drift_authority() -> None:
    with pytest.raises(DriftRuntimeContractError, match="action_store_required"):
        DriftRuntimeRiskAdapter(None)


def test_runtime_adapter_exposes_neutral_constraints_only_for_empty_injected_store(
    tmp_path,
) -> None:
    adapter = DriftRuntimeRiskAdapter(DriftActionStore(tmp_path / "drift-actions"))

    constraint = adapter.snapshot()
    applied = adapter.apply(
        proposed_risk_multiplier=0.8,
        increases_gross_exposure=True,
    )

    assert constraint.max_risk_multiplier == 1.0
    assert constraint.stop_new_orders is False
    assert constraint.active_action_receipt_sha256 is None
    assert applied.order_allowed is True
    assert applied.effective_risk_multiplier == 0.8


def test_runtime_adapter_blocks_new_risk_and_caps_multiplier_from_active_latch(
    tmp_path,
) -> None:
    store = DriftActionStore(tmp_path / "drift-actions")
    receipt = store.record(
        _decision(0.09),
        recorded_at=datetime(2026, 7, 1, 1, tzinfo=timezone.utc),
    )
    adapter = DriftRuntimeRiskAdapter(store)

    constraint = adapter.snapshot()
    increase = adapter.apply(
        proposed_risk_multiplier=0.9,
        increases_gross_exposure=True,
    )
    reduction = adapter.apply(
        proposed_risk_multiplier=0.9,
        increases_gross_exposure=False,
    )

    assert constraint.max_risk_multiplier == 0.5
    assert constraint.stop_new_orders is True
    assert constraint.reduce_only is True
    assert constraint.review_required is True
    assert constraint.active_action_receipt_sha256 == receipt.receipt_sha256
    assert constraint.to_day_loop_risk_context() == {
        "schema_version": "tradingagent.drift_runtime_constraint.v1",
        "active_action_receipt_sha256": receipt.receipt_sha256,
        "risk_multiplier_cap": 0.5,
        "stop_new_orders": True,
        "reduce_only": True,
        "quarantined": False,
        "review_required": True,
        "reason_codes": ["moderate_calibration_drift"],
    }
    assert increase.order_allowed is False
    assert increase.effective_risk_multiplier == 0.5
    assert reduction.order_allowed is True
    assert reduction.effective_risk_multiplier == 0.5


def test_runtime_adapter_maps_severe_latch_to_zero_risk(tmp_path) -> None:
    store = DriftActionStore(tmp_path / "drift-actions")
    store.record(
        _decision(0.22),
        recorded_at=datetime(2026, 7, 1, 1, tzinfo=timezone.utc),
    )

    applied = DriftRuntimeRiskAdapter(store).apply(
        proposed_risk_multiplier=1.0,
        increases_gross_exposure=False,
    )

    assert applied.constraint.quarantined is True
    assert applied.constraint.stop_new_orders is True
    assert applied.effective_risk_multiplier == 0.0
