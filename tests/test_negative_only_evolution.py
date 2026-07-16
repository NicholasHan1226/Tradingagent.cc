from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json

import pytest

from shared.models.drift_action_store import DriftActionStore
from shared.models.drift_policy import DriftEvidence, SafeAutomaticAction
from shared.models.evolution_clock import (
    NonProductionFixtureEvolutionClock,
    TrustedEvolutionClock,
)
from shared.models.evolution_loop import (
    EvolutionContractError,
    JsonMetricsArtifactVerifier,
    NegativeOnlyEvolutionController,
    SampleJournalHeadVerifier,
    TRUSTED_METRICS_IMPLEMENTATION_SHA256,
    TRUSTED_METRICS_VERIFIER_ID,
    TRUSTED_METRICS_VERIFIER_VERSION,
    _metrics_verification_proof_sha256,
)
from shared.models.lifecycle import LifecycleRecord, ModelLifecycleState
from shared.review.sample_journal import SampleJournal


UTC = timezone.utc
WINDOW_START = datetime(2026, 1, 1, tzinfo=UTC)
WINDOW_END = datetime(2026, 6, 30, tzinfo=UTC)
EVALUATED_AT = datetime(2026, 7, 1, tzinfo=UTC)
METRICS_IMPLEMENTATION_SHA256 = TRUSTED_METRICS_IMPLEMENTATION_SHA256
LABEL_SNAPSHOT_SHA256 = "b" * 64
COST_SNAPSHOT_SHA256 = "c" * 64
SOURCE_RECEIPT_SHA256 = "d" * 64
HORIZON = "5d"
REGIME = "all_market_states"
AUTHORITY = {
    "capital_authority_id": "ashare-capital-v1",
    "authority_generation": 1,
    "execution_lineage_id": "ashare-sim-fresh-20260712-v1",
}


def _record() -> LifecycleRecord:
    return LifecycleRecord(
        manifest_sha256="2" * 64,
        model_id="rank-model",
        model_version="v1",
        research_snapshot_sha256="4" * 64,
        catalog_version="fixture-catalog-v1",
        validation_plan_sha256="5" * 64,
        validation_evidence_sha256="6" * 64,
        state=ModelLifecycleState.SHADOW,
        recorded_at=datetime(2026, 6, 30, tzinfo=UTC),
        transition_reason="shadow_started",
    )


def _journal_path(tmp_path):
    path = tmp_path / "sample_journal.jsonl"
    journal = SampleJournal(path)
    journal.append_samples(
        [
            {
                "event_id": f"drift-evidence-{index}",
                "record_type": "risk_reject",
                "sample_layer": "risk_reject",
                "style": "trend_breakout",
                "reject_reason": "research_only",
                "event_at": "2026-06-29T12:00:00+00:00",
                "real_trading_enabled": False,
                **AUTHORITY,
            }
            for index in range(40)
        ]
    )
    return path, journal.read_frozen(as_of=WINDOW_END).journal_head_sha256


def _metrics_artifact(
    tmp_path,
    *,
    journal_head_sha256: str,
    calibration_error: float,
    artifact_name: str,
    model_manifest_sha256: str = "2" * 64,
) -> tuple[DriftEvidence, object, object]:
    payload = {
        "schema_version": "tradingagent.drift_metrics_artifact.v2",
        "broker_connected": False,
        "calibration_error": calibration_error,
        "capital_layer": "simulated",
        "data_degraded": False,
        "deployment_mode": "simulated",
        "effective_independent_sample_count": 40,
        "evaluated_at": EVALUATED_AT.isoformat(),
        "journal_head_sha256": journal_head_sha256,
        "live_transition_authorized": False,
        "metrics_implementation_version": "drift-metrics-v1",
        "model_manifest_sha256": model_manifest_sha256,
        "out_of_distribution_score": 0.05,
        "predicted_cost_error_ratio": 0.02,
        "real_trading_enabled": False,
        "window_end": WINDOW_END.isoformat(),
        "window_start": WINDOW_START.isoformat(),
    }
    encoded = (
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    path = tmp_path / f"{artifact_name}.json"
    path.write_bytes(encoded)
    evidence = DriftEvidence(
        calibration_error=calibration_error,
        out_of_distribution_score=0.05,
        predicted_cost_error_ratio=0.02,
        data_degraded=False,
        lineage_verified=True,
        journal_head_sha256=journal_head_sha256,
        model_manifest_sha256=model_manifest_sha256,
        metrics_artifact_sha256=hashlib.sha256(encoded).hexdigest(),
        metrics_implementation_version="drift-metrics-v1",
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        evaluated_at=EVALUATED_AT,
        effective_independent_sample_count=40,
    )
    verification_payload = {
        "schema_version": "tradingagent.drift_metrics_verification_receipt.v1",
        "cost_snapshot_sha256": COST_SNAPSHOT_SHA256,
        "effective_independent_sample_count": 40,
        "evidence_sha256": evidence.sha256(),
        "horizon": HORIZON,
        "journal_head_sha256": journal_head_sha256,
        "label_snapshot_sha256": LABEL_SNAPSHOT_SHA256,
        "metrics_artifact_sha256": evidence.metrics_artifact_sha256,
        "metrics_implementation_sha256": METRICS_IMPLEMENTATION_SHA256,
        "metrics_implementation_version": "drift-metrics-v1",
        "model_manifest_sha256": model_manifest_sha256,
        "regime": REGIME,
        "source_receipt_sha256s": [SOURCE_RECEIPT_SHA256],
        "verified_at": EVALUATED_AT.isoformat(),
        "verifier_id": TRUSTED_METRICS_VERIFIER_ID,
        "verifier_proof_sha256": "0" * 64,
        "verifier_version": TRUSTED_METRICS_VERIFIER_VERSION,
        "window_end": WINDOW_END.isoformat(),
        "window_start": WINDOW_START.isoformat(),
    }
    verification_payload["verifier_proof_sha256"] = _metrics_verification_proof_sha256(
        verification_payload
    )
    verification_path = tmp_path / f"{artifact_name}.verification.json"
    verification_path.write_bytes(
        (
            json.dumps(
                verification_payload,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    )
    return evidence, path, verification_path


def _verified_case(
    tmp_path,
    *,
    calibration_error: float,
    artifact_name: str,
    store=None,
    trusted_now: datetime = datetime(2026, 7, 1, 3, tzinfo=UTC),
):
    journal_path, journal_head = _journal_path(tmp_path)
    evidence, artifact_path, verification_path = _metrics_artifact(
        tmp_path,
        journal_head_sha256=journal_head,
        calibration_error=calibration_error,
        artifact_name=artifact_name,
    )
    controller = NegativeOnlyEvolutionController(
        store or DriftActionStore(tmp_path / "drift-actions"),
        journal_head_verifier=SampleJournalHeadVerifier(journal_path),
        metrics_artifact_verifier=JsonMetricsArtifactVerifier(
            artifact_path,
            verification_path,
            expected_metrics_implementation_sha256=METRICS_IMPLEMENTATION_SHA256,
            expected_label_snapshot_sha256=LABEL_SNAPSHOT_SHA256,
            expected_cost_snapshot_sha256=COST_SNAPSHOT_SHA256,
            expected_source_receipt_sha256s=(SOURCE_RECEIPT_SHA256,),
            expected_horizon=HORIZON,
            expected_regime=REGIME,
        ),
        trusted_clock=NonProductionFixtureEvolutionClock(
            default_instant=trusted_now,
        ),
    )
    return controller, evidence, journal_path, artifact_path, verification_path


def test_controller_requires_both_independent_authority_verifiers(tmp_path) -> None:
    store = DriftActionStore(tmp_path / "drift-actions")

    with pytest.raises(EvolutionContractError, match="journal_head_verifier_required"):
        NegativeOnlyEvolutionController(store)
    with pytest.raises(
        EvolutionContractError, match="metrics_artifact_verifier_required"
    ):
        NegativeOnlyEvolutionController(
            store,
            journal_head_verifier=SampleJournalHeadVerifier(
                tmp_path / "sample_journal.jsonl"
            ),
        )


def test_controller_rejects_arbitrary_metrics_verifier_wiring(tmp_path) -> None:
    class ProducerSelfCertifiedMetricsVerifier:
        def verify(self):
            raise AssertionError("must never be called")

    with pytest.raises(
        EvolutionContractError,
        match="metrics_artifact_verifier_untrusted",
    ):
        NegativeOnlyEvolutionController(
            DriftActionStore(tmp_path / "drift-actions"),
            journal_head_verifier=SampleJournalHeadVerifier(
                tmp_path / "sample_journal.jsonl"
            ),
            metrics_artifact_verifier=ProducerSelfCertifiedMetricsVerifier(),
            trusted_clock=NonProductionFixtureEvolutionClock(
                default_instant=datetime(2026, 7, 1, 3, tzinfo=UTC),
            ),
        )


def test_controller_requires_trusted_clock_with_no_default(tmp_path) -> None:
    journal_path, journal_head = _journal_path(tmp_path)
    _, artifact_path, verification_path = _metrics_artifact(
        tmp_path,
        journal_head_sha256=journal_head,
        calibration_error=0.01,
        artifact_name="missing-clock",
    )

    metrics_verifier = JsonMetricsArtifactVerifier(
        artifact_path,
        verification_path,
        expected_metrics_implementation_sha256=(METRICS_IMPLEMENTATION_SHA256),
        expected_label_snapshot_sha256=LABEL_SNAPSHOT_SHA256,
        expected_cost_snapshot_sha256=COST_SNAPSHOT_SHA256,
        expected_source_receipt_sha256s=(SOURCE_RECEIPT_SHA256,),
        expected_horizon=HORIZON,
        expected_regime=REGIME,
    )
    with pytest.raises(
        EvolutionContractError, match="trusted_evolution_clock_required"
    ):
        NegativeOnlyEvolutionController(
            DriftActionStore(tmp_path / "drift-actions"),
            journal_head_verifier=SampleJournalHeadVerifier(journal_path),
            metrics_artifact_verifier=metrics_verifier,
        )

    class CallerSelectedClock(TrustedEvolutionClock):
        identity_sha256 = "f" * 64
        production_eligible = False

        def now(self, **_: object) -> datetime:
            return datetime(2026, 7, 1, 3, tzinfo=UTC)

    with pytest.raises(
        EvolutionContractError,
        match="trusted_evolution_clock_untrusted",
    ):
        NegativeOnlyEvolutionController(
            DriftActionStore(tmp_path / "drift-actions"),
            journal_head_verifier=SampleJournalHeadVerifier(journal_path),
            metrics_artifact_verifier=metrics_verifier,
            trusted_clock=CallerSelectedClock(),
        )


def test_old_healthy_metrics_fail_closed_against_trusted_current_time(tmp_path) -> None:
    controller, evidence, _, _, _ = _verified_case(
        tmp_path,
        calibration_error=0.01,
        artifact_name="stale-healthy",
        trusted_now=EVALUATED_AT + timedelta(days=15),
    )

    result = controller.evaluate(
        lifecycle=_record(),
        evidence=evidence,
        recorded_at=datetime(2026, 7, 1, 1, tzinfo=UTC),
    )

    assert result.decision.actions == (
        SafeAutomaticAction.STOP_NEW_RISK,
        SafeAutomaticAction.REQUIRE_REVIEW,
    )
    assert result.decision.reasons == ("metrics_evaluation_stale",)
    assert result.effective_risk_multiplier == 0.0
    assert result.active_action_receipt_sha256 is not None
    assert len(result.trusted_clock_identity_sha256) == 64
    assert result.trusted_evaluated_at == EVALUATED_AT + timedelta(days=15)


def test_stale_severe_metrics_preserve_quarantine(tmp_path) -> None:
    controller, evidence, _, _, _ = _verified_case(
        tmp_path,
        calibration_error=0.22,
        artifact_name="stale-severe",
        trusted_now=EVALUATED_AT + timedelta(days=15),
    )

    result = controller.evaluate(
        lifecycle=_record(),
        evidence=evidence,
        recorded_at=datetime(2026, 7, 1, 1, tzinfo=UTC),
    )

    assert result.decision.actions == (
        SafeAutomaticAction.QUARANTINE,
        SafeAutomaticAction.STOP_NEW_RISK,
        SafeAutomaticAction.REQUIRE_REVIEW,
    )
    assert "severe_calibration_drift" in result.decision.reasons
    assert "metrics_evaluation_stale" in result.decision.reasons
    assert result.lifecycle.state is ModelLifecycleState.QUARANTINE


def test_trusted_clock_cannot_precede_recorded_or_evaluated_time(tmp_path) -> None:
    controller, evidence, _, _, _ = _verified_case(
        tmp_path,
        calibration_error=0.01,
        artifact_name="clock-behind",
        trusted_now=datetime(2026, 7, 1, 0, 30, tzinfo=UTC),
    )

    with pytest.raises(EvolutionContractError, match="trusted_evolution_time_invalid"):
        controller.evaluate(
            lifecycle=_record(),
            evidence=evidence,
            recorded_at=datetime(2026, 7, 1, 1, tzinfo=UTC),
        )


def test_moderate_drift_persists_reduce_only_latch_across_healthy_restart(
    tmp_path,
) -> None:
    store = DriftActionStore(tmp_path / "drift-actions")
    first_controller, first_evidence, _, _, _ = _verified_case(
        tmp_path,
        calibration_error=0.09,
        artifact_name="moderate",
        store=store,
    )
    first = first_controller.evaluate(
        lifecycle=_record(),
        evidence=first_evidence,
        recorded_at=datetime(2026, 7, 1, 1, tzinfo=UTC),
    )
    healthy_controller, healthy_evidence, _, _, _ = _verified_case(
        tmp_path,
        calibration_error=0.01,
        artifact_name="healthy",
        store=DriftActionStore(tmp_path / "drift-actions"),
    )
    restarted = healthy_controller.evaluate(
        lifecycle=first.lifecycle,
        evidence=healthy_evidence,
        recorded_at=datetime(2026, 7, 1, 2, tzinfo=UTC),
    )

    assert first.decision.actions == (
        SafeAutomaticAction.REDUCE_ONLY,
        SafeAutomaticAction.REQUIRE_REVIEW,
    )
    assert restarted.decision.actions == ()
    assert restarted.effective_risk_multiplier == 0.5
    assert restarted.active_action_receipt_sha256 == (
        first.active_action_receipt_sha256
    )
    assert restarted.automatic_promotion_enabled is False
    assert restarted.automatic_risk_expansion_enabled is False


def test_severe_drift_automatically_quarantines_but_never_promotes(tmp_path) -> None:
    controller, evidence, _, _, _ = _verified_case(
        tmp_path,
        calibration_error=0.22,
        artifact_name="severe",
    )

    result = controller.evaluate(
        lifecycle=_record(),
        evidence=evidence,
        recorded_at=datetime(2026, 7, 1, 1, tzinfo=UTC),
    )

    assert result.lifecycle.state is ModelLifecycleState.QUARANTINE
    assert result.effective_risk_multiplier == 0.0
    assert result.automatic_promotion_enabled is False
    assert result.automatic_risk_expansion_enabled is False


def test_evolution_rejects_metrics_for_another_model_manifest(tmp_path) -> None:
    controller, evidence, _, _, _ = _verified_case(
        tmp_path,
        calibration_error=0.09,
        artifact_name="wrong-model",
    )
    mismatched = DriftEvidence(
        **{
            **evidence.__dict__,
            "model_manifest_sha256": "9" * 64,
        }
    )

    with pytest.raises(EvolutionContractError, match="model_manifest_mismatch"):
        controller.evaluate(
            lifecycle=_record(),
            evidence=mismatched,
            recorded_at=datetime(2026, 7, 1, 1, tzinfo=UTC),
        )


def test_metrics_artifact_is_read_and_digest_tampering_fails_closed(tmp_path) -> None:
    controller, evidence, _, artifact_path, _ = _verified_case(
        tmp_path,
        calibration_error=0.09,
        artifact_name="tampered-metrics",
    )
    payload = artifact_path.read_text(encoding="utf-8").replace("0.09", "0.19")
    artifact_path.write_text(payload, encoding="utf-8")

    with pytest.raises(
        EvolutionContractError, match="metrics_artifact_sha256_mismatch"
    ):
        controller.evaluate(
            lifecycle=_record(),
            evidence=evidence,
            recorded_at=datetime(2026, 7, 1, 1, tzinfo=UTC),
        )


def test_journal_head_is_recomputed_and_late_mutation_fails_closed(tmp_path) -> None:
    controller, evidence, journal_path, _, _ = _verified_case(
        tmp_path,
        calibration_error=0.09,
        artifact_name="stale-head",
    )
    SampleJournal(journal_path).append_sample(
        {
            "event_id": "late-authority-mutation",
            "record_type": "risk_reject",
            "sample_layer": "risk_reject",
            "style": "event_catalyst",
            "reject_reason": "late_append",
            "event_at": "2026-06-29T13:00:00+00:00",
            "real_trading_enabled": False,
            **AUTHORITY,
        }
    )

    with pytest.raises(EvolutionContractError, match="journal_head_sha256_mismatch"):
        controller.evaluate(
            lifecycle=_record(),
            evidence=evidence,
            recorded_at=datetime(2026, 7, 1, 1, tzinfo=UTC),
        )


def test_metrics_producer_cannot_self_certify_lineage(tmp_path) -> None:
    controller, evidence, _, artifact_path, _ = _verified_case(
        tmp_path,
        calibration_error=0.09,
        artifact_name="self-certified-lineage",
    )
    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    payload["lineage_verified"] = True
    artifact_path.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(EvolutionContractError, match="metrics_artifact_fields_invalid"):
        controller.evaluate(
            lifecycle=_record(),
            evidence=evidence,
            recorded_at=datetime(2026, 7, 1, 1, tzinfo=UTC),
        )


def test_detached_verification_receipt_must_bind_trusted_implementation(
    tmp_path,
) -> None:
    controller, evidence, _, _, verification_path = _verified_case(
        tmp_path,
        calibration_error=0.09,
        artifact_name="wrong-metrics-implementation",
    )
    payload = json.loads(verification_path.read_text(encoding="utf-8"))
    payload["metrics_implementation_sha256"] = "f" * 64
    verification_path.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        EvolutionContractError,
        match="metrics_implementation_sha256_mismatch",
    ):
        controller.evaluate(
            lifecycle=_record(),
            evidence=evidence,
            recorded_at=datetime(2026, 7, 1, 1, tzinfo=UTC),
        )


def test_metrics_implementation_trust_root_is_not_caller_selectable(
    tmp_path,
) -> None:
    _, journal_head = _journal_path(tmp_path)
    _, artifact_path, verification_path = _metrics_artifact(
        tmp_path,
        journal_head_sha256=journal_head,
        calibration_error=0.09,
        artifact_name="caller-selected-implementation",
    )

    with pytest.raises(
        EvolutionContractError,
        match="metrics_implementation_trust_root_mismatch",
    ):
        JsonMetricsArtifactVerifier(
            artifact_path,
            verification_path,
            expected_metrics_implementation_sha256="f" * 64,
            expected_label_snapshot_sha256=LABEL_SNAPSHOT_SHA256,
            expected_cost_snapshot_sha256=COST_SNAPSHOT_SHA256,
            expected_source_receipt_sha256s=(SOURCE_RECEIPT_SHA256,),
            expected_horizon=HORIZON,
            expected_regime=REGIME,
        )


def test_detached_verification_receipt_cannot_swap_label_snapshot(tmp_path) -> None:
    controller, evidence, _, _, verification_path = _verified_case(
        tmp_path,
        calibration_error=0.09,
        artifact_name="wrong-label-snapshot",
    )
    payload = json.loads(verification_path.read_text(encoding="utf-8"))
    payload["label_snapshot_sha256"] = "f" * 64
    verification_path.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        EvolutionContractError,
        match="label_snapshot_sha256_mismatch",
    ):
        controller.evaluate(
            lifecycle=_record(),
            evidence=evidence,
            recorded_at=datetime(2026, 7, 1, 1, tzinfo=UTC),
        )


def test_detached_verification_receipt_rejects_producer_selected_verifier(
    tmp_path,
) -> None:
    controller, evidence, _, _, verification_path = _verified_case(
        tmp_path,
        calibration_error=0.09,
        artifact_name="producer-selected-verifier",
    )
    payload = json.loads(verification_path.read_text(encoding="utf-8"))
    payload["verifier_id"] = "producer-self-certified-metrics-verifier"
    payload["verifier_version"] = "999"
    verification_path.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        EvolutionContractError,
        match="metrics_verifier_identity_mismatch",
    ):
        controller.evaluate(
            lifecycle=_record(),
            evidence=evidence,
            recorded_at=datetime(2026, 7, 1, 1, tzinfo=UTC),
        )


def test_detached_verification_receipt_proof_is_recomputed(tmp_path) -> None:
    controller, evidence, _, _, verification_path = _verified_case(
        tmp_path,
        calibration_error=0.09,
        artifact_name="unbound-verifier-proof",
    )
    payload = json.loads(verification_path.read_text(encoding="utf-8"))
    payload["verifier_proof_sha256"] = "f" * 64
    verification_path.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        EvolutionContractError,
        match="metrics_verifier_proof_sha256_mismatch",
    ):
        controller.evaluate(
            lifecycle=_record(),
            evidence=evidence,
            recorded_at=datetime(2026, 7, 1, 1, tzinfo=UTC),
        )
