from __future__ import annotations

from datetime import datetime, timezone
import multiprocessing
from pathlib import Path
from queue import Empty
import time

import pytest

from shared.models.drift_action_store import (
    DriftActionStore,
    DriftActionStoreError,
)
from shared.models.drift_policy import (
    DriftDecision,
    DriftEvidence,
    SafeAutomaticAction,
    evaluate_drift,
)


def _record_with_delayed_moderate_replace(
    root: str,
    calibration_error: float,
    moderate_entered,
    errors,
) -> None:
    """Force the historical read/compare/replace race across two processes."""

    original = DriftActionStore._replace_active

    def delayed_replace(self, receipt):
        if receipt.risk_multiplier == 0.5:
            moderate_entered.set()
            time.sleep(0.25)
        return original(self, receipt)

    DriftActionStore._replace_active = delayed_replace
    try:
        decision = evaluate_drift(_evidence(calibration_error=calibration_error))
        DriftActionStore(Path(root)).record(
            decision,
            recorded_at=datetime(2026, 7, 1, 1, tzinfo=timezone.utc),
        )
    except Exception as exc:  # pragma: no cover - relayed to parent assertion
        errors.put(repr(exc))


def _evidence(
    *,
    calibration_error: float,
    effective_independent_sample_count: int = 240,
) -> DriftEvidence:
    return DriftEvidence(
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
        effective_independent_sample_count=effective_independent_sample_count,
    )


def test_negative_action_is_persistent_idempotent_and_read_back_verified(
    tmp_path,
) -> None:
    store = DriftActionStore(tmp_path / "drift-actions")
    decision = evaluate_drift(_evidence(calibration_error=0.09))
    recorded_at = datetime(2026, 7, 1, 1, tzinfo=timezone.utc)

    first = store.record(decision, recorded_at=recorded_at)
    second = DriftActionStore(tmp_path / "drift-actions").record(
        decision,
        recorded_at=recorded_at,
    )

    assert second == first
    assert store.load_active() == first
    assert first.risk_multiplier == 0.5
    assert first.evidence_sha256 == decision.evidence_sha256


def test_negative_action_latch_can_tighten_but_never_loosens_automatically(
    tmp_path,
) -> None:
    store = DriftActionStore(tmp_path / "drift-actions")
    moderate = evaluate_drift(_evidence(calibration_error=0.09))
    severe = evaluate_drift(_evidence(calibration_error=0.22))

    moderate_receipt = store.record(
        moderate,
        recorded_at=datetime(2026, 7, 1, 1, tzinfo=timezone.utc),
    )
    severe_receipt = store.record(
        severe,
        recorded_at=datetime(2026, 7, 1, 2, tzinfo=timezone.utc),
    )
    replayed_moderate = store.record(
        moderate,
        recorded_at=datetime(2026, 7, 1, 3, tzinfo=timezone.utc),
    )

    assert moderate_receipt.risk_multiplier == 0.5
    assert severe_receipt.risk_multiplier == 0.0
    assert replayed_moderate.risk_multiplier == 0.5
    assert store.load_active().risk_multiplier == 0.0
    assert store.load_active().receipt_sha256 == severe_receipt.receipt_sha256


def test_equal_multiplier_latch_upgrades_from_stop_new_risk_to_quarantine(
    tmp_path,
) -> None:
    store = DriftActionStore(tmp_path / "drift-actions")
    stop_new_risk = evaluate_drift(
        _evidence(
            calibration_error=0.01,
            effective_independent_sample_count=20,
        )
    )
    quarantine = evaluate_drift(_evidence(calibration_error=0.22))

    stop_receipt = store.record(
        stop_new_risk,
        recorded_at=datetime(2026, 7, 1, 1, tzinfo=timezone.utc),
    )
    quarantine_receipt = store.record(
        quarantine,
        recorded_at=datetime(2026, 7, 1, 2, tzinfo=timezone.utc),
    )
    replayed_stop = store.record(
        stop_new_risk,
        recorded_at=datetime(2026, 7, 1, 3, tzinfo=timezone.utc),
    )

    assert stop_receipt.risk_multiplier == 0.0
    assert SafeAutomaticAction.QUARANTINE not in stop_receipt.actions
    assert quarantine_receipt.risk_multiplier == 0.0
    assert SafeAutomaticAction.QUARANTINE in quarantine_receipt.actions
    assert replayed_stop.risk_multiplier == 0.0
    assert store.load_active() == quarantine_receipt


def test_equal_multiplier_latch_upgrades_from_reduce_only_to_quarantine(
    tmp_path,
) -> None:
    store = DriftActionStore(tmp_path / "drift-actions")
    reduce_only = DriftDecision(
        actions=(
            SafeAutomaticAction.REDUCE_ONLY,
            SafeAutomaticAction.REQUIRE_REVIEW,
        ),
        risk_multiplier=0.0,
        reasons=("reduce_only_latch",),
        evidence_sha256="4" * 64,
    )
    quarantine = evaluate_drift(_evidence(calibration_error=0.22))

    store.record(
        reduce_only,
        recorded_at=datetime(2026, 7, 1, 1, tzinfo=timezone.utc),
    )
    quarantine_receipt = store.record(
        quarantine,
        recorded_at=datetime(2026, 7, 1, 2, tzinfo=timezone.utc),
    )

    assert store.load_active() == quarantine_receipt


def test_equal_multiplier_incomparable_actions_cannot_drop_active_controls(
    tmp_path,
) -> None:
    store = DriftActionStore(tmp_path / "drift-actions")
    quarantine = evaluate_drift(_evidence(calibration_error=0.22))
    quarantine_receipt = store.record(
        quarantine,
        recorded_at=datetime(2026, 7, 1, 1, tzinfo=timezone.utc),
    )
    incomplete_quarantine = DriftDecision(
        actions=(SafeAutomaticAction.QUARANTINE,),
        risk_multiplier=0.0,
        reasons=("incomplete_quarantine",),
        evidence_sha256="4" * 64,
    )

    incoming_receipt = store.record(
        incomplete_quarantine,
        recorded_at=datetime(2026, 7, 1, 2, tzinfo=timezone.utc),
    )

    assert incoming_receipt.actions == (SafeAutomaticAction.QUARANTINE,)
    assert store.load_active() == quarantine_receipt


def test_lower_multiplier_cannot_drop_an_existing_quarantine_dimension(
    tmp_path,
) -> None:
    store = DriftActionStore(tmp_path / "drift-actions")
    quarantine = DriftDecision(
        actions=(
            SafeAutomaticAction.QUARANTINE,
            SafeAutomaticAction.STOP_NEW_RISK,
            SafeAutomaticAction.REQUIRE_REVIEW,
        ),
        risk_multiplier=0.5,
        reasons=("quarantine_latch",),
        evidence_sha256="5" * 64,
    )
    lower_multiplier_but_lighter_action = DriftDecision(
        actions=(
            SafeAutomaticAction.STOP_NEW_RISK,
            SafeAutomaticAction.REQUIRE_REVIEW,
        ),
        risk_multiplier=0.0,
        reasons=("stop_only",),
        evidence_sha256="6" * 64,
    )

    quarantine_receipt = store.record(
        quarantine,
        recorded_at=datetime(2026, 7, 1, 1, tzinfo=timezone.utc),
    )
    store.record(
        lower_multiplier_but_lighter_action,
        recorded_at=datetime(2026, 7, 1, 2, tzinfo=timezone.utc),
    )

    assert store.load_active() == quarantine_receipt


def test_concurrent_lighter_action_cannot_overwrite_severe_latch(tmp_path) -> None:
    ctx = multiprocessing.get_context("fork")
    root = tmp_path / "drift-actions"
    moderate_entered = ctx.Event()
    errors = ctx.Queue()
    moderate = ctx.Process(
        target=_record_with_delayed_moderate_replace,
        args=(str(root), 0.09, moderate_entered, errors),
    )
    severe = ctx.Process(
        target=_record_with_delayed_moderate_replace,
        args=(str(root), 0.22, moderate_entered, errors),
    )

    moderate.start()
    assert moderate_entered.wait(timeout=5)
    severe.start()
    moderate.join(timeout=10)
    severe.join(timeout=10)

    assert moderate.exitcode == 0
    assert severe.exitcode == 0
    with pytest.raises(Empty):
        errors.get(timeout=0.1)
    assert DriftActionStore(root).load_active().risk_multiplier == 0.0


def test_record_fsyncs_receipt_and_active_parent_directories(
    tmp_path, monkeypatch
) -> None:
    import shared.models.drift_action_store as store_module

    fsynced: list[Path] = []
    monkeypatch.setattr(
        store_module,
        "_fsync_directory",
        lambda path: fsynced.append(Path(path)),
    )
    root = tmp_path / "drift-actions"
    DriftActionStore(root).record(
        evaluate_drift(_evidence(calibration_error=0.09)),
        recorded_at=datetime(2026, 7, 1, 1, tzinfo=timezone.utc),
    )

    assert root / "receipts" in fsynced
    assert root in fsynced


def test_healthy_decision_cannot_clear_persistent_negative_action(tmp_path) -> None:
    store = DriftActionStore(tmp_path / "drift-actions")
    healthy = evaluate_drift(_evidence(calibration_error=0.01))

    with pytest.raises(DriftActionStoreError, match="negative_action_required"):
        store.record(
            healthy,
            recorded_at=datetime(2026, 7, 1, 1, tzinfo=timezone.utc),
        )


def test_tampered_active_receipt_fails_closed(tmp_path) -> None:
    store = DriftActionStore(tmp_path / "drift-actions")
    decision = evaluate_drift(_evidence(calibration_error=0.09))
    store.record(
        decision,
        recorded_at=datetime(2026, 7, 1, 1, tzinfo=timezone.utc),
    )
    active_path = tmp_path / "drift-actions" / "active.json"
    payload = active_path.read_text(encoding="utf-8").replace("0.5", "0.9")
    active_path.write_text(payload, encoding="utf-8")

    with pytest.raises(DriftActionStoreError, match="receipt_digest_mismatch"):
        store.load_active()


def test_symlink_store_root_is_rejected(tmp_path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)

    with pytest.raises(DriftActionStoreError, match="store_root_symlink_forbidden"):
        DriftActionStore(alias)
