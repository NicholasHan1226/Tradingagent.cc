"""Frozen, high-recall opportunity radar that emits shadow research only."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol, Tuple

from .contracts import (
    OpportunityContractError,
    OpportunityEvidenceRef,
    OpportunityScope,
    OpportunitySnapshot,
    OpportunityState,
)


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
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
    return hashlib.sha256(
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _require_sha(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise OpportunityContractError(f"{field_name}_invalid")
    return value


@dataclass(frozen=True)
class OpportunityScanRow:
    """One scanned denominator row; detection is optional and explicit."""

    scope: OpportunityScope
    entity_id: str
    thesis_id: str
    state: OpportunityState | None
    uncalibrated_hazard_score: float | None
    priced_in_score: float | None
    trigger_window_start: datetime | None
    trigger_window_end: datetime | None
    horizon: str | None
    evidence_refs: Tuple[OpportunityEvidenceRef, ...]
    invalidation_conditions: Tuple[str, ...]
    reason_codes: Tuple[str, ...]

    def __post_init__(self) -> None:
        _text(self.entity_id, "entity_id")
        _text(self.thesis_id, "thesis_id")
        if not isinstance(self.scope, OpportunityScope):
            raise OpportunityContractError("opportunity_scope_invalid")
        if not isinstance(self.reason_codes, tuple) or not self.reason_codes:
            raise OpportunityContractError("reason_codes_invalid")
        for reason in self.reason_codes:
            _text(reason, "reason_code")
        detected = self.state is not None
        required = (
            self.uncalibrated_hazard_score,
            self.priced_in_score,
            self.trigger_window_start,
            self.trigger_window_end,
            self.horizon,
        )
        if detected != all(value is not None for value in required):
            raise OpportunityContractError("scan_detection_fields_inconsistent")
        if detected:
            if not isinstance(self.state, OpportunityState):
                raise OpportunityContractError("opportunity_state_invalid")
            for value, field_name in (
                (self.uncalibrated_hazard_score, "uncalibrated_hazard_score"),
                (self.priced_in_score, "priced_in_score"),
            ):
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(float(value))
                    or not 0.0 <= float(value) <= 1.0
                ):
                    raise OpportunityContractError(f"{field_name}_invalid")
            _aware(self.trigger_window_start, "trigger_window_start")
            _aware(self.trigger_window_end, "trigger_window_end")
            _text(self.horizon, "horizon")
            if not self.evidence_refs or not self.invalidation_conditions:
                raise OpportunityContractError("detected_opportunity_evidence_missing")
        elif any(
            (
                self.evidence_refs,
                self.invalidation_conditions,
            )
        ):
            raise OpportunityContractError("undetected_row_carries_opportunity_data")
        # Reuse the canonical snapshot entity policy without fabricating one.
        from .contracts import _validate_entity

        _validate_entity(self.scope, self.entity_id)

    def canonical_payload(self) -> dict[str, object]:
        return {
            "entity_id": self.entity_id,
            "evidence_refs": [
                item.canonical_payload()
                for item in sorted(
                    self.evidence_refs,
                    key=lambda evidence: evidence.evidence_id,
                )
            ],
            "horizon": self.horizon,
            "invalidation_conditions": list(self.invalidation_conditions),
            "priced_in_score": self.priced_in_score,
            "reason_codes": list(self.reason_codes),
            "scope": self.scope.value,
            "state": self.state.value if self.state is not None else None,
            "thesis_id": self.thesis_id,
            "trigger_window_end": (
                self.trigger_window_end.astimezone(timezone.utc).isoformat()
                if self.trigger_window_end is not None
                else None
            ),
            "trigger_window_start": (
                self.trigger_window_start.astimezone(timezone.utc).isoformat()
                if self.trigger_window_start is not None
                else None
            ),
            "uncalibrated_hazard_score": self.uncalibrated_hazard_score,
        }


@dataclass(frozen=True)
class OpportunityCoverageVerification:
    """Detached proof that the radar denominator was fully scanned."""

    accepted: bool
    verifier_id: str
    production_eligible: bool
    proof_sha256: str
    verified_at: datetime
    decision_time: datetime
    detector_id: str
    detector_version: str
    universe_snapshot_sha256: str
    scan_rows_sha256: str
    scanned_entity_ids_sha256: str
    expected_entity_count: int
    observed_entity_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.accepted, bool):
            raise OpportunityContractError("coverage_acceptance_invalid")
        if self.production_eligible is not False:
            raise OpportunityContractError("coverage_production_authority_forbidden")
        for field_name in ("verifier_id", "detector_id", "detector_version"):
            _text(getattr(self, field_name), field_name)
        for field_name in (
            "proof_sha256",
            "universe_snapshot_sha256",
            "scan_rows_sha256",
            "scanned_entity_ids_sha256",
        ):
            _require_sha(getattr(self, field_name), field_name)
        verified_at = _aware(self.verified_at, "verified_at")
        decision_time = _aware(self.decision_time, "decision_time")
        object.__setattr__(self, "verified_at", verified_at)
        object.__setattr__(self, "decision_time", decision_time)
        if verified_at > decision_time:
            raise OpportunityContractError("coverage_verification_from_future")
        for field_name in ("expected_entity_count", "observed_entity_count"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise OpportunityContractError(f"{field_name}_invalid")

    def canonical_payload(self) -> dict[str, object]:
        return {
            "accepted": self.accepted,
            "decision_time": self.decision_time.isoformat(),
            "detector_id": self.detector_id,
            "detector_version": self.detector_version,
            "expected_entity_count": self.expected_entity_count,
            "observed_entity_count": self.observed_entity_count,
            "production_eligible": False,
            "proof_sha256": self.proof_sha256,
            "scan_rows_sha256": self.scan_rows_sha256,
            "scanned_entity_ids_sha256": self.scanned_entity_ids_sha256,
            "universe_snapshot_sha256": self.universe_snapshot_sha256,
            "verified_at": self.verified_at.isoformat(),
            "verifier_id": self.verifier_id,
        }

    @classmethod
    def from_payload(cls, payload: object) -> "OpportunityCoverageVerification":
        expected = {
            "accepted",
            "decision_time",
            "detector_id",
            "detector_version",
            "expected_entity_count",
            "observed_entity_count",
            "production_eligible",
            "proof_sha256",
            "scan_rows_sha256",
            "scanned_entity_ids_sha256",
            "universe_snapshot_sha256",
            "verified_at",
            "verifier_id",
        }
        if not isinstance(payload, dict) or set(payload) != expected:
            raise OpportunityContractError("coverage_payload_invalid")
        return cls(
            accepted=payload["accepted"],
            verifier_id=payload["verifier_id"],
            production_eligible=payload["production_eligible"],
            proof_sha256=payload["proof_sha256"],
            verified_at=_parse_aware(payload["verified_at"], "verified_at"),
            decision_time=_parse_aware(payload["decision_time"], "decision_time"),
            detector_id=payload["detector_id"],
            detector_version=payload["detector_version"],
            universe_snapshot_sha256=payload["universe_snapshot_sha256"],
            scan_rows_sha256=payload["scan_rows_sha256"],
            scanned_entity_ids_sha256=payload["scanned_entity_ids_sha256"],
            expected_entity_count=payload["expected_entity_count"],
            observed_entity_count=payload["observed_entity_count"],
        )


class OpportunityCoverageVerifier(Protocol):
    verifier_id: str
    production_eligible: bool

    def verify(self, **request: object) -> OpportunityCoverageVerification:
        """Verify the exact denominator and scan payload."""


@dataclass(frozen=True)
class OpportunityBatch:
    decision_time: datetime
    detector_id: str
    detector_version: str
    universe_snapshot_sha256: str
    scan_rows_sha256: str
    scanned_entity_ids: Tuple[str, ...]
    opportunities: Tuple[OpportunitySnapshot, ...]
    coverage: OpportunityCoverageVerification
    schema_version: str = "tradingagent.opportunity_batch.v1"
    shadow_only: bool = True
    production_eligible: bool = False
    candidate_emission_allowed: bool = False
    position_effect_allowed: bool = False
    order_effect_allowed: bool = False
    promotion_eligible: bool = False
    batch_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        instant = _aware(self.decision_time, "decision_time")
        object.__setattr__(self, "decision_time", instant)
        _text(self.detector_id, "detector_id")
        _text(self.detector_version, "detector_version")
        _require_sha(self.universe_snapshot_sha256, "universe_snapshot_sha256")
        _require_sha(self.scan_rows_sha256, "scan_rows_sha256")
        if (
            not isinstance(self.scanned_entity_ids, tuple)
            or not self.scanned_entity_ids
        ):
            raise OpportunityContractError("scanned_entity_ids_invalid")
        scanned = tuple(
            _text(item, "scanned_entity_id") for item in self.scanned_entity_ids
        )
        if len(scanned) != len(set(scanned)):
            raise OpportunityContractError("scanned_entity_ids_invalid")
        object.__setattr__(self, "scanned_entity_ids", scanned)
        if not isinstance(self.coverage, OpportunityCoverageVerification):
            raise OpportunityContractError("opportunity_batch_coverage_invalid")
        entity_ids_sha = _sha256(list(scanned))
        if self.coverage.scanned_entity_ids_sha256 != entity_ids_sha:
            raise OpportunityContractError(
                "opportunity_batch_denominator_binding_invalid"
            )
        coverage_binding = (
            self.coverage.accepted,
            self.coverage.production_eligible,
            self.coverage.decision_time,
            self.coverage.detector_id,
            self.coverage.detector_version,
            self.coverage.universe_snapshot_sha256,
            self.coverage.scan_rows_sha256,
            self.coverage.scanned_entity_ids_sha256,
            self.coverage.expected_entity_count,
            self.coverage.observed_entity_count,
        )
        expected_coverage_binding = (
            True,
            False,
            instant,
            self.detector_id,
            self.detector_version,
            self.universe_snapshot_sha256,
            self.scan_rows_sha256,
            entity_ids_sha,
            len(scanned),
            len(scanned),
        )
        if coverage_binding != expected_coverage_binding:
            raise OpportunityContractError("opportunity_batch_coverage_binding_invalid")
        if not isinstance(self.opportunities, tuple):
            raise OpportunityContractError("opportunity_batch_opportunities_invalid")
        if any(
            not isinstance(item, OpportunitySnapshot) for item in self.opportunities
        ):
            raise OpportunityContractError("opportunity_batch_opportunities_invalid")
        opportunity_ids = tuple(item.opportunity_id for item in self.opportunities)
        if len(opportunity_ids) != len(set(opportunity_ids)):
            raise OpportunityContractError("opportunity_batch_opportunities_invalid")
        for opportunity in self.opportunities:
            if (
                opportunity.entity_id not in scanned
                or opportunity.decision_time != instant
                or opportunity.discovered_at != instant
                or opportunity.previous_snapshot_sha256 is not None
            ):
                raise OpportunityContractError(
                    "opportunity_batch_opportunity_binding_invalid"
                )
        object.__setattr__(
            self,
            "opportunities",
            tuple(sorted(self.opportunities, key=lambda item: item.opportunity_id)),
        )
        if (
            self.schema_version != "tradingagent.opportunity_batch.v1"
            or self.shadow_only is not True
            or self.production_eligible is not False
            or self.candidate_emission_allowed is not False
            or self.position_effect_allowed is not False
            or self.order_effect_allowed is not False
            or self.promotion_eligible is not False
        ):
            raise OpportunityContractError("opportunity_batch_boundary_invalid")
        object.__setattr__(self, "batch_sha256", _sha256(self.canonical_payload()))

    @classmethod
    def from_payload(cls, payload: object) -> "OpportunityBatch":
        expected = {
            "candidate_emission_allowed",
            "coverage",
            "decision_time",
            "detector_id",
            "detector_version",
            "opportunities",
            "order_effect_allowed",
            "position_effect_allowed",
            "production_eligible",
            "promotion_eligible",
            "scan_rows_sha256",
            "scanned_entity_ids",
            "schema_version",
            "shadow_only",
            "universe_snapshot_sha256",
        }
        if not isinstance(payload, dict) or set(payload) != expected:
            raise OpportunityContractError("opportunity_batch_payload_invalid")
        opportunities = payload["opportunities"]
        scanned_entity_ids = payload["scanned_entity_ids"]
        if not isinstance(opportunities, list) or not isinstance(
            scanned_entity_ids, list
        ):
            raise OpportunityContractError("opportunity_batch_payload_invalid")
        return cls(
            decision_time=_parse_aware(payload["decision_time"], "decision_time"),
            detector_id=payload["detector_id"],
            detector_version=payload["detector_version"],
            universe_snapshot_sha256=payload["universe_snapshot_sha256"],
            scan_rows_sha256=payload["scan_rows_sha256"],
            scanned_entity_ids=tuple(scanned_entity_ids),
            opportunities=tuple(
                OpportunitySnapshot.from_payload(item) for item in opportunities
            ),
            coverage=OpportunityCoverageVerification.from_payload(payload["coverage"]),
            schema_version=payload["schema_version"],
            shadow_only=payload["shadow_only"],
            production_eligible=payload["production_eligible"],
            candidate_emission_allowed=payload["candidate_emission_allowed"],
            position_effect_allowed=payload["position_effect_allowed"],
            order_effect_allowed=payload["order_effect_allowed"],
            promotion_eligible=payload["promotion_eligible"],
        )

    def canonical_payload(self) -> dict[str, object]:
        return {
            "candidate_emission_allowed": False,
            "coverage": self.coverage.canonical_payload(),
            "decision_time": self.decision_time.isoformat(),
            "detector_id": self.detector_id,
            "detector_version": self.detector_version,
            "opportunities": [item.canonical_payload() for item in self.opportunities],
            "order_effect_allowed": False,
            "position_effect_allowed": False,
            "production_eligible": False,
            "promotion_eligible": False,
            "scan_rows_sha256": self.scan_rows_sha256,
            "scanned_entity_ids": list(self.scanned_entity_ids),
            "schema_version": self.schema_version,
            "shadow_only": True,
            "universe_snapshot_sha256": self.universe_snapshot_sha256,
        }


class FrozenOpportunityRadar:
    """Deterministic detector wrapper with an explicit coverage verifier."""

    def __init__(self, *, detector_id: str, detector_version: str) -> None:
        self.detector_id = _text(detector_id, "detector_id")
        self.detector_version = _text(detector_version, "detector_version")

    def scan(
        self,
        rows: Tuple[OpportunityScanRow, ...],
        *,
        decision_time: datetime,
        universe_snapshot_sha256: str,
        coverage_verifier: OpportunityCoverageVerifier | None,
    ) -> OpportunityBatch:
        instant = _aware(decision_time, "decision_time")
        universe_sha = _require_sha(
            universe_snapshot_sha256,
            "universe_snapshot_sha256",
        )
        if not isinstance(rows, tuple) or not rows:
            raise OpportunityContractError("scan_rows_invalid")
        if any(not isinstance(row, OpportunityScanRow) for row in rows):
            raise OpportunityContractError("scan_rows_invalid")
        ordered = tuple(sorted(rows, key=lambda row: (row.scope.value, row.entity_id)))
        entity_ids = tuple(row.entity_id for row in ordered)
        if len(entity_ids) != len(set(entity_ids)):
            raise OpportunityContractError("scan_entity_duplicate")
        scan_rows_sha = _sha256([row.canonical_payload() for row in ordered])
        entity_ids_sha = _sha256(list(entity_ids))
        if coverage_verifier is None:
            raise OpportunityContractError("coverage_verifier_required")
        verifier_id = _text(
            getattr(coverage_verifier, "verifier_id", None),
            "coverage_verifier_id",
        )
        if getattr(coverage_verifier, "production_eligible", None) is not False:
            raise OpportunityContractError("coverage_verifier_boundary_invalid")
        verify = getattr(coverage_verifier, "verify", None)
        if not callable(verify):
            raise OpportunityContractError("coverage_verifier_invalid")
        try:
            proof = verify(
                detector_id=self.detector_id,
                detector_version=self.detector_version,
                decision_time=instant,
                universe_snapshot_sha256=universe_sha,
                scan_rows_sha256=scan_rows_sha,
                scanned_entity_ids=entity_ids,
                scanned_entity_ids_sha256=entity_ids_sha,
            )
        except OpportunityContractError:
            raise
        except Exception as exc:
            raise OpportunityContractError("coverage_verification_failed") from exc
        if not isinstance(proof, OpportunityCoverageVerification):
            raise OpportunityContractError("coverage_proof_invalid")
        if proof.verifier_id != verifier_id:
            raise OpportunityContractError("coverage_verifier_mismatch")
        expected_binding = (
            self.detector_id,
            self.detector_version,
            instant,
            universe_sha,
            scan_rows_sha,
            entity_ids_sha,
            len(entity_ids),
        )
        actual_binding = (
            proof.detector_id,
            proof.detector_version,
            proof.decision_time,
            proof.universe_snapshot_sha256,
            proof.scan_rows_sha256,
            proof.scanned_entity_ids_sha256,
            proof.observed_entity_count,
        )
        if actual_binding != expected_binding or proof.accepted is not True:
            raise OpportunityContractError("coverage_proof_binding_mismatch")
        if proof.expected_entity_count != len(entity_ids):
            raise OpportunityContractError("coverage_denominator_mismatch")

        opportunities = []
        for row in ordered:
            if row.state is None:
                continue
            opportunity_id = (
                "opportunity-"
                + _sha256(
                    {
                        "detector_id": self.detector_id,
                        "detector_version": self.detector_version,
                        "entity_id": row.entity_id,
                        "scope": row.scope.value,
                        "thesis_id": row.thesis_id,
                    }
                )[:24]
            )
            opportunities.append(
                OpportunitySnapshot.create(
                    opportunity_id=opportunity_id,
                    scope=row.scope,
                    entity_id=row.entity_id,
                    thesis_id=row.thesis_id,
                    state=row.state,
                    decision_time=instant,
                    discovered_at=instant,
                    trigger_window_start=row.trigger_window_start,
                    trigger_window_end=row.trigger_window_end,
                    horizon=row.horizon,
                    uncalibrated_hazard_score=row.uncalibrated_hazard_score,
                    priced_in_score=row.priced_in_score,
                    evidence_refs=row.evidence_refs,
                    invalidation_conditions=row.invalidation_conditions,
                    reason_codes=row.reason_codes,
                )
            )
        return OpportunityBatch(
            decision_time=instant,
            detector_id=self.detector_id,
            detector_version=self.detector_version,
            universe_snapshot_sha256=universe_sha,
            scan_rows_sha256=scan_rows_sha,
            scanned_entity_ids=entity_ids,
            opportunities=tuple(opportunities),
            coverage=proof,
        )


__all__ = [
    "FrozenOpportunityRadar",
    "OpportunityBatch",
    "OpportunityCoverageVerification",
    "OpportunityCoverageVerifier",
    "OpportunityScanRow",
]
