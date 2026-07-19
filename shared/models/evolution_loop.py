"""Negative-only model evolution controller for simulated research."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from .drift_action_store import DriftActionStore
from .drift_policy import (
    DriftDecision,
    DriftEvidence,
    SafeAutomaticAction,
    evaluate_drift_as_of,
)
from .evolution_clock import (
    NonProductionFixtureEvolutionClock,
    TrustedEvolutionClock,
)
from .lifecycle import (
    LifecycleActor,
    LifecycleRecord,
    ModelLifecycleState,
    transition_model,
)
from shared.review.sample_journal import SampleJournal


class EvolutionContractError(ValueError):
    """Raised when evolution evidence is not bound to the active model."""


@dataclass(frozen=True)
class VerifiedJournalHead:
    journal_head_sha256: str
    journal_head_event_count: int
    data_as_of: str


class JournalHeadVerifier(Protocol):
    def verify(self, *, as_of: datetime) -> VerifiedJournalHead: ...


class MetricsArtifactVerifier(Protocol):
    def verify(self) -> VerifiedMetricsArtifact: ...


@dataclass(frozen=True)
class VerifiedMetricsArtifact:
    """Metrics accepted only after a detached authority receipt is verified."""

    evidence: DriftEvidence
    verification_receipt_sha256: str
    metrics_implementation_sha256: str
    label_snapshot_sha256: str
    cost_snapshot_sha256: str
    horizon: str
    regime: str
    source_receipt_sha256s: tuple[str, ...]


class SampleJournalHeadVerifier:
    """Recompute the PIT journal head from the append-only authority."""

    def __init__(self, journal_path: Path) -> None:
        self._journal = SampleJournal(journal_path)

    def verify(self, *, as_of: datetime) -> VerifiedJournalHead:
        view = self._journal.read_frozen(as_of=as_of)
        return VerifiedJournalHead(
            journal_head_sha256=view.journal_head_sha256,
            journal_head_event_count=view.journal_head_event_count,
            data_as_of=view.data_as_of,
        )


_METRICS_ARTIFACT_SCHEMA = "tradingagent.drift_metrics_artifact.v2"
_METRICS_ARTIFACT_FIELDS = {
    "broker_connected",
    "calibration_error",
    "capital_layer",
    "data_degraded",
    "deployment_mode",
    "effective_independent_sample_count",
    "evaluated_at",
    "journal_head_sha256",
    "live_transition_authorized",
    "metrics_implementation_version",
    "model_manifest_sha256",
    "out_of_distribution_score",
    "predicted_cost_error_ratio",
    "real_trading_enabled",
    "schema_version",
    "window_end",
    "window_start",
}
_METRICS_VERIFICATION_SCHEMA = "tradingagent.drift_metrics_verification_receipt.v1"
TRUSTED_METRICS_VERIFIER_ID = "tradingagent-frozen-metrics-verifier"
TRUSTED_METRICS_VERIFIER_VERSION = "1"
TRUSTED_METRICS_IMPLEMENTATION_VERSION = "drift-metrics-v1"
TRUSTED_METRICS_IMPLEMENTATION_SHA256 = (
    "49f0b1c57960e6e3e4e1a23fe4ad7906dd508fdce46f2a312269d06b22010bb5"
)
_METRICS_VERIFICATION_FIELDS = {
    "schema_version",
    "cost_snapshot_sha256",
    "effective_independent_sample_count",
    "evidence_sha256",
    "horizon",
    "journal_head_sha256",
    "label_snapshot_sha256",
    "metrics_artifact_sha256",
    "metrics_implementation_sha256",
    "metrics_implementation_version",
    "model_manifest_sha256",
    "regime",
    "source_receipt_sha256s",
    "verified_at",
    "verifier_id",
    "verifier_proof_sha256",
    "verifier_version",
    "window_end",
    "window_start",
}


def _metrics_verification_proof_sha256(receipt: dict) -> str:
    """Recompute the detached receipt's complete content binding.

    This is an integrity proof, not a signature.  Authority comes from the
    exact verifier implementation and frozen expected inputs required below;
    production wiring must not treat this hash as external authentication.
    """

    if set(receipt) != _METRICS_VERIFICATION_FIELDS:
        raise EvolutionContractError("metrics_verification_receipt_fields_invalid")
    identity = {
        key: receipt[key]
        for key in sorted(_METRICS_VERIFICATION_FIELDS)
        if key != "verifier_proof_sha256"
    }
    encoded = json.dumps(
        identity,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _require_sha256(value: object, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise EvolutionContractError(f"{field_name}_invalid")
    return value


def _require_nonempty_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvolutionContractError(f"{field_name}_invalid")
    return value


def _canonical_json_payload(
    raw: bytes,
    *,
    expected_fields: set[str],
    expected_schema: str,
    artifact_name: str,
) -> dict:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvolutionContractError(f"{artifact_name}_invalid_json") from exc
    if not isinstance(payload, dict) or set(payload) != expected_fields:
        raise EvolutionContractError(f"{artifact_name}_fields_invalid")
    canonical = (
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    if raw != canonical:
        raise EvolutionContractError(f"{artifact_name}_not_canonical")
    if payload["schema_version"] != expected_schema:
        raise EvolutionContractError(f"{artifact_name}_schema_invalid")
    return payload


def _read_regular_artifact(path: Path) -> bytes:
    candidate = Path(path).absolute()
    for component in (candidate, *candidate.parents):
        if component.exists() or component.is_symlink():
            if component.is_symlink():
                raise EvolutionContractError("metrics_artifact_symlink_forbidden")
    flags = os.O_RDONLY | int(getattr(os, "O_NOFOLLOW", 0))
    try:
        fd = os.open(candidate, flags)
    except OSError as exc:
        raise EvolutionContractError("metrics_artifact_unreadable") from exc
    try:
        before = os.fstat(fd)
        path_stat = os.stat(candidate, follow_symlinks=False)
        if (
            not stat.S_ISREG(before.st_mode)
            or not stat.S_ISREG(path_stat.st_mode)
            or (before.st_dev, before.st_ino) != (path_stat.st_dev, path_stat.st_ino)
        ):
            raise EvolutionContractError("metrics_artifact_identity_invalid")
        chunks = []
        while True:
            block = os.read(fd, 1024 * 1024)
            if not block:
                break
            chunks.append(block)
        after = os.fstat(fd)
        final_path_stat = os.stat(candidate, follow_symlinks=False)
        if (before.st_dev, before.st_ino, before.st_size) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
        ) or (after.st_dev, after.st_ino) != (
            final_path_stat.st_dev,
            final_path_stat.st_ino,
        ):
            raise EvolutionContractError("metrics_artifact_changed_during_read")
        return b"".join(chunks)
    except OSError as exc:
        raise EvolutionContractError("metrics_artifact_unreadable") from exc
    finally:
        os.close(fd)


class JsonMetricsArtifactVerifier:
    """Bind producer metrics to a detached, explicitly trusted verifier receipt."""

    verifier_id = TRUSTED_METRICS_VERIFIER_ID
    verifier_version = TRUSTED_METRICS_VERIFIER_VERSION
    metrics_implementation_sha256 = TRUSTED_METRICS_IMPLEMENTATION_SHA256
    metrics_implementation_version = TRUSTED_METRICS_IMPLEMENTATION_VERSION

    def __init__(
        self,
        artifact_path: Path,
        verification_receipt_path: Path,
        *,
        expected_metrics_implementation_sha256: str,
        expected_label_snapshot_sha256: str,
        expected_cost_snapshot_sha256: str,
        expected_source_receipt_sha256s: tuple[str, ...],
        expected_horizon: str,
        expected_regime: str,
    ) -> None:
        self._artifact_path = Path(artifact_path)
        self._verification_receipt_path = Path(verification_receipt_path)
        selected_implementation_sha256 = _require_sha256(
            expected_metrics_implementation_sha256,
            "expected_metrics_implementation_sha256",
        )
        if not hmac.compare_digest(
            selected_implementation_sha256,
            TRUSTED_METRICS_IMPLEMENTATION_SHA256,
        ):
            raise EvolutionContractError("metrics_implementation_trust_root_mismatch")
        self._expected_metrics_implementation_sha256 = (
            TRUSTED_METRICS_IMPLEMENTATION_SHA256
        )
        self._expected_label_snapshot_sha256 = _require_sha256(
            expected_label_snapshot_sha256,
            "expected_label_snapshot_sha256",
        )
        self._expected_cost_snapshot_sha256 = _require_sha256(
            expected_cost_snapshot_sha256,
            "expected_cost_snapshot_sha256",
        )
        if (
            not isinstance(expected_source_receipt_sha256s, tuple)
            or not expected_source_receipt_sha256s
            or any(
                not isinstance(item, str) for item in expected_source_receipt_sha256s
            )
            or len(expected_source_receipt_sha256s)
            != len(set(expected_source_receipt_sha256s))
        ):
            raise EvolutionContractError("expected_source_receipt_sha256s_invalid")
        for source_receipt in expected_source_receipt_sha256s:
            _require_sha256(source_receipt, "expected_source_receipt_sha256")
        self._expected_source_receipt_sha256s = expected_source_receipt_sha256s
        self._expected_horizon = _require_nonempty_text(
            expected_horizon,
            "expected_horizon",
        )
        self._expected_regime = _require_nonempty_text(
            expected_regime,
            "expected_regime",
        )

    def verify(self) -> VerifiedMetricsArtifact:
        raw = _read_regular_artifact(self._artifact_path)
        payload = _canonical_json_payload(
            raw,
            expected_fields=_METRICS_ARTIFACT_FIELDS,
            expected_schema=_METRICS_ARTIFACT_SCHEMA,
            artifact_name="metrics_artifact",
        )
        try:
            parsed = {
                **payload,
                "window_start": datetime.fromisoformat(
                    payload["window_start"].replace("Z", "+00:00")
                ),
                "window_end": datetime.fromisoformat(
                    payload["window_end"].replace("Z", "+00:00")
                ),
                "evaluated_at": datetime.fromisoformat(
                    payload["evaluated_at"].replace("Z", "+00:00")
                ),
            }
            parsed.pop("schema_version")
            evidence = DriftEvidence(
                **parsed,
                lineage_verified=True,
                metrics_artifact_sha256=hashlib.sha256(raw).hexdigest(),
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise EvolutionContractError("metrics_artifact_payload_invalid") from exc

        verification_raw = _read_regular_artifact(self._verification_receipt_path)
        receipt = _canonical_json_payload(
            verification_raw,
            expected_fields=_METRICS_VERIFICATION_FIELDS,
            expected_schema=_METRICS_VERIFICATION_SCHEMA,
            artifact_name="metrics_verification_receipt",
        )
        for field_name in (
            "cost_snapshot_sha256",
            "evidence_sha256",
            "journal_head_sha256",
            "label_snapshot_sha256",
            "metrics_artifact_sha256",
            "metrics_implementation_sha256",
            "model_manifest_sha256",
            "verifier_proof_sha256",
        ):
            _require_sha256(receipt[field_name], field_name)
        horizon = _require_nonempty_text(receipt["horizon"], "horizon")
        regime = _require_nonempty_text(receipt["regime"], "regime")
        _require_nonempty_text(
            receipt["metrics_implementation_version"],
            "metrics_implementation_version",
        )
        verifier_id = _require_nonempty_text(receipt["verifier_id"], "verifier_id")
        verifier_version = _require_nonempty_text(
            receipt["verifier_version"],
            "verifier_version",
        )
        if verifier_id != self.verifier_id or verifier_version != self.verifier_version:
            raise EvolutionContractError("metrics_verifier_identity_mismatch")
        source_receipts = receipt["source_receipt_sha256s"]
        if (
            not isinstance(source_receipts, list)
            or not source_receipts
            or any(not isinstance(item, str) for item in source_receipts)
        ):
            raise EvolutionContractError("source_receipt_sha256s_invalid")
        if len(source_receipts) != len(set(source_receipts)):
            raise EvolutionContractError("source_receipt_sha256s_invalid")
        for source_receipt in source_receipts:
            _require_sha256(source_receipt, "source_receipt_sha256")
        try:
            verified_at = datetime.fromisoformat(
                receipt["verified_at"].replace("Z", "+00:00")
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise EvolutionContractError("metrics_verified_at_invalid") from exc
        if (
            verified_at.tzinfo is None
            or verified_at.utcoffset() is None
            or verified_at < evidence.window_end
            or verified_at > evidence.evaluated_at
        ):
            raise EvolutionContractError("metrics_verified_at_invalid")

        if receipt["metrics_artifact_sha256"] != evidence.metrics_artifact_sha256:
            raise EvolutionContractError("metrics_artifact_sha256_mismatch")
        if receipt["evidence_sha256"] != evidence.sha256():
            raise EvolutionContractError("metrics_evidence_sha256_mismatch")
        if receipt["journal_head_sha256"] != evidence.journal_head_sha256:
            raise EvolutionContractError("metrics_journal_head_sha256_mismatch")
        if receipt["model_manifest_sha256"] != evidence.model_manifest_sha256:
            raise EvolutionContractError("metrics_model_manifest_sha256_mismatch")
        if (
            receipt["metrics_implementation_version"]
            != evidence.metrics_implementation_version
        ):
            raise EvolutionContractError("metrics_implementation_version_mismatch")
        if (
            receipt["metrics_implementation_version"]
            != self.metrics_implementation_version
        ):
            raise EvolutionContractError("metrics_implementation_version_untrusted")
        if (
            receipt["effective_independent_sample_count"]
            != evidence.effective_independent_sample_count
        ):
            raise EvolutionContractError("metrics_sample_count_mismatch")
        if receipt["window_start"] != evidence.window_start.isoformat():
            raise EvolutionContractError("metrics_window_start_mismatch")
        if receipt["window_end"] != evidence.window_end.isoformat():
            raise EvolutionContractError("metrics_window_end_mismatch")
        if (
            receipt["metrics_implementation_sha256"]
            != self._expected_metrics_implementation_sha256
        ):
            raise EvolutionContractError("metrics_implementation_sha256_mismatch")
        if receipt["label_snapshot_sha256"] != self._expected_label_snapshot_sha256:
            raise EvolutionContractError("label_snapshot_sha256_mismatch")
        if receipt["cost_snapshot_sha256"] != self._expected_cost_snapshot_sha256:
            raise EvolutionContractError("cost_snapshot_sha256_mismatch")
        if tuple(source_receipts) != self._expected_source_receipt_sha256s:
            raise EvolutionContractError("source_receipt_sha256s_mismatch")
        if horizon != self._expected_horizon:
            raise EvolutionContractError("metrics_horizon_mismatch")
        if regime != self._expected_regime:
            raise EvolutionContractError("metrics_regime_mismatch")

        expected_proof_sha256 = _metrics_verification_proof_sha256(receipt)
        if not hmac.compare_digest(
            receipt["verifier_proof_sha256"],
            expected_proof_sha256,
        ):
            raise EvolutionContractError("metrics_verifier_proof_sha256_mismatch")

        return VerifiedMetricsArtifact(
            evidence=evidence,
            verification_receipt_sha256=hashlib.sha256(verification_raw).hexdigest(),
            metrics_implementation_sha256=receipt["metrics_implementation_sha256"],
            label_snapshot_sha256=receipt["label_snapshot_sha256"],
            cost_snapshot_sha256=receipt["cost_snapshot_sha256"],
            horizon=horizon,
            regime=regime,
            source_receipt_sha256s=tuple(source_receipts),
        )


@dataclass(frozen=True)
class EvolutionResult:
    decision: DriftDecision
    lifecycle: LifecycleRecord
    effective_risk_multiplier: float
    active_action_receipt_sha256: str | None
    trusted_clock_identity_sha256: str
    trusted_evaluated_at: datetime
    automatic_promotion_enabled: bool = False
    automatic_risk_expansion_enabled: bool = False

    def __post_init__(self) -> None:
        if (
            not isinstance(self.trusted_clock_identity_sha256, str)
            or len(self.trusted_clock_identity_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in self.trusted_clock_identity_sha256
            )
        ):
            raise EvolutionContractError("trusted_clock_identity_invalid")
        if (
            not isinstance(self.trusted_evaluated_at, datetime)
            or self.trusted_evaluated_at.tzinfo is None
            or self.trusted_evaluated_at.utcoffset() is None
        ):
            raise EvolutionContractError("trusted_evaluated_at_invalid")
        if (
            self.automatic_promotion_enabled is not False
            or self.automatic_risk_expansion_enabled is not False
            or not 0 <= self.effective_risk_multiplier <= 1
        ):
            raise EvolutionContractError("negative_only_authority_violated")


class NegativeOnlyEvolutionController:
    """Evaluate verified drift and persist only risk-reducing consequences."""

    def __init__(
        self,
        action_store: DriftActionStore,
        *,
        journal_head_verifier: JournalHeadVerifier | None = None,
        metrics_artifact_verifier: MetricsArtifactVerifier | None = None,
        trusted_clock: TrustedEvolutionClock | None = None,
    ) -> None:
        if not isinstance(action_store, DriftActionStore):
            raise EvolutionContractError("drift_action_store_required")
        if journal_head_verifier is None or not callable(
            getattr(journal_head_verifier, "verify", None)
        ):
            raise EvolutionContractError("journal_head_verifier_required")
        if metrics_artifact_verifier is None or not callable(
            getattr(metrics_artifact_verifier, "verify", None)
        ):
            raise EvolutionContractError("metrics_artifact_verifier_required")
        if type(metrics_artifact_verifier) is not JsonMetricsArtifactVerifier:
            raise EvolutionContractError("metrics_artifact_verifier_untrusted")
        if not isinstance(trusted_clock, TrustedEvolutionClock):
            raise EvolutionContractError("trusted_evolution_clock_required")
        if type(trusted_clock) is not NonProductionFixtureEvolutionClock:
            raise EvolutionContractError("trusted_evolution_clock_untrusted")
        self._action_store = action_store
        self._journal_head_verifier = journal_head_verifier
        self._metrics_artifact_verifier = metrics_artifact_verifier
        self._trusted_clock = trusted_clock

    def evaluate(
        self,
        *,
        lifecycle: LifecycleRecord,
        evidence: DriftEvidence,
        recorded_at: datetime,
    ) -> EvolutionResult:
        if not isinstance(lifecycle, LifecycleRecord):
            raise EvolutionContractError("lifecycle_record_required")
        if not isinstance(evidence, DriftEvidence):
            raise EvolutionContractError("drift_evidence_required")
        if evidence.model_manifest_sha256 != lifecycle.manifest_sha256:
            raise EvolutionContractError("model_manifest_mismatch")
        if (
            recorded_at.tzinfo is None
            or recorded_at.utcoffset() is None
            or recorded_at < evidence.evaluated_at
            or recorded_at < lifecycle.recorded_at
        ):
            raise EvolutionContractError("evolution_recorded_at_invalid")

        verified_head = self._journal_head_verifier.verify(as_of=evidence.window_end)
        if not isinstance(verified_head, VerifiedJournalHead):
            raise EvolutionContractError("journal_head_verification_invalid")
        if verified_head.journal_head_sha256 != evidence.journal_head_sha256:
            raise EvolutionContractError("journal_head_sha256_mismatch")
        if (
            evidence.effective_independent_sample_count
            > verified_head.journal_head_event_count
        ):
            raise EvolutionContractError("effective_sample_count_exceeds_journal")

        verified_artifact = self._metrics_artifact_verifier.verify()
        if not isinstance(verified_artifact, VerifiedMetricsArtifact):
            raise EvolutionContractError("metrics_artifact_verification_invalid")
        verified_metrics = verified_artifact.evidence
        if verified_metrics.metrics_artifact_sha256 != evidence.metrics_artifact_sha256:
            raise EvolutionContractError("metrics_artifact_sha256_mismatch")
        if verified_metrics != evidence:
            raise EvolutionContractError("metrics_artifact_evidence_mismatch")

        try:
            trusted_now = self._trusted_clock.now(
                model_manifest_sha256=evidence.model_manifest_sha256,
                evidence_sha256=evidence.sha256(),
            )
        except Exception as exc:
            raise EvolutionContractError("trusted_evolution_clock_unavailable") from exc
        if (
            not isinstance(trusted_now, datetime)
            or trusted_now.tzinfo is None
            or trusted_now.utcoffset() is None
            or trusted_now < evidence.evaluated_at
            or trusted_now < lifecycle.recorded_at
            or trusted_now < recorded_at
        ):
            raise EvolutionContractError("trusted_evolution_time_invalid")

        decision = evaluate_drift_as_of(evidence, as_of=trusted_now)
        if decision.actions:
            self._action_store.record(
                decision,
                recorded_at=trusted_now,
            )

        next_lifecycle = lifecycle
        if (
            SafeAutomaticAction.QUARANTINE in decision.actions
            and lifecycle.state
            not in {ModelLifecycleState.QUARANTINE, ModelLifecycleState.RETIRED}
        ):
            next_lifecycle = transition_model(
                lifecycle,
                target=ModelLifecycleState.QUARANTINE,
                actor=LifecycleActor.AUTOMATION,
                recorded_at=trusted_now,
                reason=f"drift:{decision.evidence_sha256}",
            )

        active = self._action_store.load_active(required=False)
        effective_multiplier = min(
            decision.risk_multiplier,
            active.risk_multiplier if active is not None else 1.0,
        )
        return EvolutionResult(
            decision=decision,
            lifecycle=next_lifecycle,
            effective_risk_multiplier=effective_multiplier,
            active_action_receipt_sha256=(
                active.receipt_sha256 if active is not None else None
            ),
            trusted_clock_identity_sha256=self._trusted_clock.identity_sha256,
            trusted_evaluated_at=trusted_now,
        )


__all__ = [
    "EvolutionContractError",
    "EvolutionResult",
    "JournalHeadVerifier",
    "JsonMetricsArtifactVerifier",
    "MetricsArtifactVerifier",
    "NegativeOnlyEvolutionController",
    "SampleJournalHeadVerifier",
    "VerifiedMetricsArtifact",
    "VerifiedJournalHead",
]
