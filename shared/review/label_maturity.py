"""Point-in-time label maturity contracts for safe offline model review.

The four label classes are deliberately separate.  A paper fill or a shadow
counterfactual may be useful evidence, but neither is market truth and neither
can silently satisfy predictive release evidence.  An unavailable oracle is a
first-class audit result, never a zero-valued label.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
from dataclasses import InitVar, dataclass
from datetime import datetime
from enum import Enum
from typing import Optional, Protocol, Tuple, Union


class LabelContractError(ValueError):
    """Raised when a label cannot satisfy the fail-closed contract."""


class EvidenceUse(str, Enum):
    """Evidence roles kept separate to prevent sample-class leakage."""

    PREDICTIVE_VALIDATION = "predictive_validation"
    PAPER_EXECUTION_VALIDATION = "paper_execution_validation"
    COUNTERFACTUAL_VALIDATION = "counterfactual_validation"


_ALLOWED_HORIZONS = frozenset({"m30", "m60", "close", "1d", "3d", "5d"})


def _require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise LabelContractError("%s_must_be_nonempty_text" % field_name)


def _require_aware(value: datetime, field_name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise LabelContractError("%s_must_be_timezone_aware" % field_name)
    if value.utcoffset() is None:
        raise LabelContractError("%s_must_be_timezone_aware" % field_name)


def _validate_common(
    *,
    label_id: str,
    decision_cluster_id: str,
    horizon: str,
    horizon_end: datetime,
    available_at: datetime,
) -> None:
    _require_text(label_id, "label_id")
    _require_text(decision_cluster_id, "decision_cluster_id")
    if horizon not in _ALLOWED_HORIZONS:
        raise LabelContractError("unsupported_horizon")
    _require_aware(horizon_end, "horizon_end")
    _require_aware(available_at, "available_at")
    if available_at < horizon_end:
        raise LabelContractError("available_at_precedes_horizon_end")


def _require_finite(value: float, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LabelContractError("%s_must_be_finite" % field_name)
    if not math.isfinite(float(value)):
        raise LabelContractError("%s_must_be_finite" % field_name)


def _require_sha256(value: str, field_name: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise LabelContractError("%s_invalid" % field_name)


def _validate_receipt_ids(receipt_ids: Tuple[str, ...]) -> None:
    if not isinstance(receipt_ids, tuple):
        raise LabelContractError("source_receipt_ids_must_be_immutable_tuple")
    if not receipt_ids:
        raise LabelContractError("source_receipt_ids_must_be_nonempty")
    for receipt_id in receipt_ids:
        _require_text(receipt_id, "source_receipt_id")
        if receipt_id != receipt_id.strip():
            raise LabelContractError("source_receipt_ids_must_be_canonical_text")
    if receipt_ids != tuple(sorted(set(receipt_ids))):
        raise LabelContractError("source_receipt_ids_must_be_sorted_unique")


@dataclass(frozen=True)
class FrozenAuthorityProof:
    """Opaque proof identity issued by a separately frozen label authority."""

    proof_id: str
    authority_id: str
    authority_version: str
    frozen_at: datetime
    evidence_payload_sha256: str
    source_receipt_ids: Tuple[str, ...]

    def __post_init__(self) -> None:
        _require_text(self.proof_id, "proof_id")
        _require_text(self.authority_id, "authority_id")
        _require_text(self.authority_version, "authority_version")
        _require_aware(self.frozen_at, "frozen_at")
        _require_sha256(self.evidence_payload_sha256, "evidence_payload_sha256")
        _validate_receipt_ids(self.source_receipt_ids)


@dataclass(frozen=True)
class FrozenAuthorityVerification:
    """Auditable result returned by an injected frozen-authority verifier."""

    accepted: bool
    verifier_id: str
    proof_id: str
    authority_id: str
    authority_version: str
    evidence_payload_sha256: str
    verified_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.accepted, bool):
            raise LabelContractError("authority_accepted_must_be_boolean")
        _require_text(self.verifier_id, "verifier_id")
        _require_text(self.proof_id, "proof_id")
        _require_text(self.authority_id, "authority_id")
        _require_text(self.authority_version, "authority_version")
        _require_sha256(self.evidence_payload_sha256, "evidence_payload_sha256")
        _require_aware(self.verified_at, "verified_at")


class FrozenAuthorityVerifier(Protocol):
    """Caller-owned trust boundary; there is deliberately no default verifier."""

    verifier_id: str

    def verify(
        self,
        *,
        proof: FrozenAuthorityProof,
        evidence_payload_json: str,
        assessed_as_of: datetime,
    ) -> FrozenAuthorityVerification: ...


@dataclass(frozen=True)
class FrozenOOSValidationPlanReceipt:
    """Immutable pre-decision registry receipt for one OOS label plan."""

    receipt_id: str
    registry_id: str
    registry_version: str
    validation_plan_id: str
    validation_plan_version: str
    primary_horizon: str
    eligible_source_class: str
    frozen_at: datetime
    total_return_definition_version: str
    corporate_action_policy_version: str
    receipt_payload_sha256: str

    def __post_init__(self) -> None:
        _require_text(self.receipt_id, "oos_validation_plan_receipt_id")
        _require_text(self.registry_id, "oos_registry_id")
        _require_text(self.registry_version, "oos_registry_version")
        _require_text(self.validation_plan_id, "validation_plan_id")
        _require_text(self.validation_plan_version, "validation_plan_version")
        if self.primary_horizon not in _ALLOWED_HORIZONS:
            raise LabelContractError("unsupported_oos_primary_horizon")
        _require_text(self.eligible_source_class, "eligible_source_class")
        _require_aware(self.frozen_at, "oos_validation_plan_frozen_at")
        _require_text(
            self.total_return_definition_version,
            "total_return_definition_version",
        )
        _require_text(
            self.corporate_action_policy_version,
            "corporate_action_policy_version",
        )
        _require_sha256(
            self.receipt_payload_sha256,
            "oos_validation_plan_receipt_sha256",
        )
        if not hmac.compare_digest(
            self.receipt_payload_sha256,
            self.recompute_receipt_payload_sha256(),
        ):
            raise LabelContractError("oos_validation_plan_receipt_sha256_mismatch")

    def canonical_receipt_payload(self) -> dict:
        return {
            "schema_version": 1,
            "receipt_type": "frozen_oos_validation_plan.v1",
            "receipt_id": self.receipt_id,
            "registry_id": self.registry_id,
            "registry_version": self.registry_version,
            "validation_plan_id": self.validation_plan_id,
            "validation_plan_version": self.validation_plan_version,
            "primary_horizon": self.primary_horizon,
            "eligible_source_class": self.eligible_source_class,
            "frozen_at": self.frozen_at.isoformat(),
            "total_return_definition_version": (self.total_return_definition_version),
            "corporate_action_policy_version": (self.corporate_action_policy_version),
        }

    def canonical_receipt_payload_json(self) -> str:
        return json.dumps(
            self.canonical_receipt_payload(),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )

    def recompute_receipt_payload_sha256(self) -> str:
        return hashlib.sha256(
            self.canonical_receipt_payload_json().encode("utf-8")
        ).hexdigest()


@dataclass(frozen=True)
class FrozenOOSRegistryVerification:
    """Verifier-owned acceptance of a frozen OOS registry receipt."""

    accepted: bool
    verifier_id: str
    receipt_id: str
    registry_id: str
    registry_version: str
    receipt_payload_sha256: str
    verified_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.accepted, bool):
            raise LabelContractError("oos_registry_accepted_must_be_boolean")
        _require_text(self.verifier_id, "oos_registry_verifier_id")
        _require_text(self.receipt_id, "oos_validation_plan_receipt_id")
        _require_text(self.registry_id, "oos_registry_id")
        _require_text(self.registry_version, "oos_registry_version")
        _require_sha256(
            self.receipt_payload_sha256,
            "oos_validation_plan_receipt_sha256",
        )
        _require_aware(self.verified_at, "oos_registry_verified_at")


class FrozenOOSRegistryVerifier(Protocol):
    """Caller-owned OOS registry trust boundary; no default is provided."""

    verifier_id: str

    def verify(
        self,
        *,
        receipt: FrozenOOSValidationPlanReceipt,
        receipt_payload_json: str,
        assessed_as_of: datetime,
    ) -> FrozenOOSRegistryVerification: ...


@dataclass(frozen=True)
class MarketTruth:
    """Observed market outcome with point-in-time source evidence."""

    label_id: str
    decision_cluster_id: str
    horizon: str
    horizon_end: datetime
    available_at: datetime
    value: float
    decision_cutoff: datetime
    source_receipt_ids: Tuple[str, ...]
    evidence_payload_sha256: str
    source_class: Optional[str] = None
    oos_validation_plan_receipt_id: Optional[str] = None
    oos_validation_plan_receipt_sha256: Optional[str] = None
    total_return_definition_version: Optional[str] = None
    corporate_action_policy_version: Optional[str] = None
    adjustment_truth_receipt_id: Optional[str] = None
    adjustment_truth_payload_sha256: Optional[str] = None
    adjustment_truth_valid_through: Optional[datetime] = None
    adjustment_truth_available_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        _validate_common(
            label_id=self.label_id,
            decision_cluster_id=self.decision_cluster_id,
            horizon=self.horizon,
            horizon_end=self.horizon_end,
            available_at=self.available_at,
        )
        _require_finite(self.value, "value")
        _require_aware(self.decision_cutoff, "decision_cutoff")
        if self.decision_cutoff > self.horizon_end:
            raise LabelContractError("decision_cutoff_after_horizon_end")
        _validate_receipt_ids(self.source_receipt_ids)
        _require_sha256(self.evidence_payload_sha256, "evidence_payload_sha256")
        self._validate_total_return_binding()
        if not hmac.compare_digest(
            self.evidence_payload_sha256,
            self.recompute_evidence_payload_sha256(),
        ):
            raise LabelContractError("evidence_payload_sha256_mismatch")

    def _validate_total_return_binding(self) -> None:
        optional_text = (
            (self.source_class, "source_class"),
            (
                self.oos_validation_plan_receipt_id,
                "oos_validation_plan_receipt_id",
            ),
            (
                self.total_return_definition_version,
                "total_return_definition_version",
            ),
            (
                self.corporate_action_policy_version,
                "corporate_action_policy_version",
            ),
            (self.adjustment_truth_receipt_id, "adjustment_truth_receipt_id"),
        )
        for value, field_name in optional_text:
            if value is not None:
                _require_text(value, field_name)
        if self.oos_validation_plan_receipt_sha256 is not None:
            _require_sha256(
                self.oos_validation_plan_receipt_sha256,
                "oos_validation_plan_receipt_sha256",
            )
        if self.adjustment_truth_payload_sha256 is not None:
            _require_sha256(
                self.adjustment_truth_payload_sha256,
                "adjustment_truth_payload_sha256",
            )
        if self.adjustment_truth_valid_through is not None:
            _require_aware(
                self.adjustment_truth_valid_through,
                "adjustment_truth_valid_through",
            )
            if self.adjustment_truth_valid_through < self.horizon_end:
                raise LabelContractError("adjustment_truth_does_not_cover_horizon")
        if self.adjustment_truth_available_at is not None:
            _require_aware(
                self.adjustment_truth_available_at,
                "adjustment_truth_available_at",
            )
            if self.adjustment_truth_available_at > self.available_at:
                raise LabelContractError("label_available_before_adjustment_truth")
        if (
            self.adjustment_truth_valid_through is not None
            and self.adjustment_truth_available_at is not None
            and self.adjustment_truth_available_at < self.adjustment_truth_valid_through
        ):
            raise LabelContractError("adjustment_truth_available_before_valid_through")
        if (
            self.adjustment_truth_receipt_id is not None
            and self.adjustment_truth_receipt_id not in self.source_receipt_ids
        ):
            raise LabelContractError("adjustment_truth_receipt_not_in_source_receipts")

    def canonical_evidence_payload(self) -> dict:
        """Return the complete evidence identity used for PIT label review."""

        return {
            "schema_version": 1,
            "evidence_type": "market_truth_label.v1",
            "label_id": self.label_id,
            "decision_cluster_id": self.decision_cluster_id,
            "horizon": self.horizon,
            "decision_cutoff": self.decision_cutoff.isoformat(),
            "horizon_end": self.horizon_end.isoformat(),
            "available_at": self.available_at.isoformat(),
            "value": float(self.value),
            "source_receipt_ids": list(self.source_receipt_ids),
            "source_class": self.source_class,
            "oos_validation_plan_receipt_id": (self.oos_validation_plan_receipt_id),
            "oos_validation_plan_receipt_sha256": (
                self.oos_validation_plan_receipt_sha256
            ),
            "total_return_definition_version": (self.total_return_definition_version),
            "corporate_action_policy_version": (self.corporate_action_policy_version),
            "adjustment_truth_receipt_id": self.adjustment_truth_receipt_id,
            "adjustment_truth_payload_sha256": (self.adjustment_truth_payload_sha256),
            "adjustment_truth_valid_through": (
                self.adjustment_truth_valid_through.isoformat()
                if self.adjustment_truth_valid_through is not None
                else None
            ),
            "adjustment_truth_available_at": (
                self.adjustment_truth_available_at.isoformat()
                if self.adjustment_truth_available_at is not None
                else None
            ),
        }

    def recompute_evidence_payload_sha256(self) -> str:
        return hashlib.sha256(
            self.canonical_evidence_payload_json().encode("utf-8")
        ).hexdigest()

    def canonical_evidence_payload_json(self) -> str:
        return json.dumps(
            self.canonical_evidence_payload(),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )


@dataclass(frozen=True)
class Paper:
    """Outcome from simulated execution; never aliases market truth."""

    label_id: str
    decision_cluster_id: str
    horizon: str
    horizon_end: datetime
    available_at: datetime
    value: float
    simulated_fill_id: str
    execution_lineage_id: str
    actual_cost_cny: float

    def __post_init__(self) -> None:
        _validate_common(
            label_id=self.label_id,
            decision_cluster_id=self.decision_cluster_id,
            horizon=self.horizon,
            horizon_end=self.horizon_end,
            available_at=self.available_at,
        )
        _require_finite(self.value, "value")
        _require_text(self.simulated_fill_id, "simulated_fill_id")
        _require_text(self.execution_lineage_id, "execution_lineage_id")
        _require_finite(self.actual_cost_cny, "actual_cost_cny")
        if self.actual_cost_cny < 0:
            raise LabelContractError("actual_cost_cny_must_be_nonnegative")


@dataclass(frozen=True)
class Shadow:
    """Counterfactual outcome costed by an explicit conservative model."""

    label_id: str
    decision_cluster_id: str
    horizon: str
    horizon_end: datetime
    available_at: datetime
    value: float
    cost_model_version: str

    def __post_init__(self) -> None:
        _validate_common(
            label_id=self.label_id,
            decision_cluster_id=self.decision_cluster_id,
            horizon=self.horizon,
            horizon_end=self.horizon_end,
            available_at=self.available_at,
        )
        _require_finite(self.value, "value")
        _require_text(self.cost_model_version, "cost_model_version")


@dataclass(frozen=True)
class UnavailableOracle:
    """Audit record proving that a future/required label is unavailable."""

    label_id: str
    decision_cluster_id: str
    horizon: str
    horizon_end: datetime
    available_at: datetime
    reason: str

    def __post_init__(self) -> None:
        _validate_common(
            label_id=self.label_id,
            decision_cluster_id=self.decision_cluster_id,
            horizon=self.horizon,
            horizon_end=self.horizon_end,
            available_at=self.available_at,
        )
        _require_text(self.reason, "reason")


LabelObservation = Union[MarketTruth, Paper, Shadow, UnavailableOracle]


@dataclass(frozen=True)
class LabelMaturityRecord:
    """Immutable projection that recomputes every claimed eligibility field."""

    label_id: str
    label_class: str
    mature: bool
    release_evidence_eligible: bool
    eligible_uses: Tuple[EvidenceUse, ...]
    reasons: Tuple[str, ...]
    horizon_end: Optional[datetime] = None
    available_at: Optional[datetime] = None
    decision_cutoff: Optional[datetime] = None
    assessed_as_of: Optional[datetime] = None
    source_receipt_ids: Tuple[str, ...] = ()
    evidence_payload_sha256: Optional[str] = None
    canonical_evidence_payload_json: Optional[str] = None
    unavailable_reason: Optional[str] = None
    authority_proof: Optional[FrozenAuthorityProof] = None
    authority_verifier_id: Optional[str] = None
    authority_verification: Optional[FrozenAuthorityVerification] = None
    source_class: Optional[str] = None
    oos_validation_plan_receipt_id: Optional[str] = None
    oos_validation_plan_receipt_sha256: Optional[str] = None
    total_return_definition_version: Optional[str] = None
    corporate_action_policy_version: Optional[str] = None
    adjustment_truth_receipt_id: Optional[str] = None
    adjustment_truth_payload_sha256: Optional[str] = None
    adjustment_truth_valid_through: Optional[datetime] = None
    adjustment_truth_available_at: Optional[datetime] = None
    oos_validation_plan_receipt: Optional[FrozenOOSValidationPlanReceipt] = None
    oos_registry_verifier_id: Optional[str] = None
    oos_registry_verification: Optional[FrozenOOSRegistryVerification] = None
    authority_verifier: InitVar[Optional[FrozenAuthorityVerifier]] = None
    oos_registry_verifier: InitVar[Optional[FrozenOOSRegistryVerifier]] = None

    def __post_init__(
        self,
        authority_verifier: Optional[FrozenAuthorityVerifier],
        oos_registry_verifier: Optional[FrozenOOSRegistryVerifier],
    ) -> None:
        _require_text(self.label_id, "label_id")
        if self.label_class not in {
            "market_truth",
            "paper",
            "shadow",
            "unavailable_oracle",
        }:
            raise LabelContractError("unsupported_label_class")
        if not isinstance(self.mature, bool):
            raise LabelContractError("mature_must_be_boolean")
        if not isinstance(self.release_evidence_eligible, bool):
            raise LabelContractError("release_evidence_eligible_must_be_boolean")
        if not isinstance(self.eligible_uses, tuple):
            raise LabelContractError("eligible_uses_must_be_immutable_tuple")
        if not isinstance(self.reasons, tuple):
            raise LabelContractError("reasons_must_be_immutable_tuple")
        for reason in self.reasons:
            _require_text(reason, "reason")

        if self.label_class == "market_truth" and (
            self.decision_cutoff is None
            or not self.source_receipt_ids
            or self.evidence_payload_sha256 is None
            or self.canonical_evidence_payload_json is None
        ):
            raise LabelContractError("release_evidence_binding_required")
        if (
            self.horizon_end is None
            or self.available_at is None
            or self.assessed_as_of is None
        ):
            raise LabelContractError("maturity_evidence_binding_required")
        _require_aware(self.horizon_end, "horizon_end")
        _require_aware(self.available_at, "available_at")
        _require_aware(self.assessed_as_of, "assessed_as_of")
        if self.available_at < self.horizon_end:
            raise LabelContractError("available_at_precedes_horizon_end")

        expected_reasons = list(
            _time_reasons(
                horizon_end=self.horizon_end,
                available_at=self.available_at,
                as_of=self.assessed_as_of,
            )
        )
        computed_mature = (
            self.label_class != "unavailable_oracle" and not expected_reasons
        )
        if self.mature is not computed_mature:
            raise LabelContractError("maturity_boolean_mismatch")

        computed_release_eligibility = False
        expected_uses: Tuple[EvidenceUse, ...] = ()
        if self.label_class == "market_truth":
            payload = self._validate_market_truth_binding()
            if self.authority_verification is not None and authority_verifier is None:
                raise LabelContractError("explicit_frozen_authority_verifier_required")
            if self.authority_verification is not None:
                verifier_id = getattr(authority_verifier, "verifier_id", None)
                if verifier_id != self.authority_verifier_id:
                    raise LabelContractError(
                        "frozen_authority_verification_recheck_mismatch"
                    )
                try:
                    rechecked = authority_verifier.verify(
                        proof=self.authority_proof,
                        evidence_payload_json=self.canonical_evidence_payload_json,
                        assessed_as_of=self.assessed_as_of,
                    )
                except Exception as exc:
                    raise LabelContractError(
                        "frozen_authority_verification_recheck_failed"
                    ) from exc
                if rechecked != self.authority_verification:
                    raise LabelContractError(
                        "frozen_authority_verification_recheck_mismatch"
                    )
            authority_accepted, authority_reasons = _authority_result(
                evidence_payload_sha256=self.evidence_payload_sha256,
                source_receipt_ids=self.source_receipt_ids,
                available_at=self.available_at,
                assessed_as_of=self.assessed_as_of,
                authority_proof=self.authority_proof,
                authority_verifier_id=self.authority_verifier_id,
                authority_verification=self.authority_verification,
            )
            expected_reasons.extend(authority_reasons)
            if (
                self.oos_registry_verification is not None
                and oos_registry_verifier is None
            ):
                raise LabelContractError(
                    "explicit_frozen_oos_registry_verifier_required"
                )
            if self.oos_registry_verification is not None:
                verifier_id = getattr(oos_registry_verifier, "verifier_id", None)
                if verifier_id != self.oos_registry_verifier_id:
                    raise LabelContractError(
                        "frozen_oos_registry_verification_recheck_mismatch"
                    )
                try:
                    rechecked = oos_registry_verifier.verify(
                        receipt=self.oos_validation_plan_receipt,
                        receipt_payload_json=(
                            self.oos_validation_plan_receipt.canonical_receipt_payload_json()
                        ),
                        assessed_as_of=self.assessed_as_of,
                    )
                except Exception as exc:
                    raise LabelContractError(
                        "frozen_oos_registry_verification_recheck_failed"
                    ) from exc
                if rechecked != self.oos_registry_verification:
                    raise LabelContractError(
                        "frozen_oos_registry_verification_recheck_mismatch"
                    )
            oos_accepted, oos_reasons = _oos_registry_result(
                source_class=self.source_class,
                horizon=payload.get("horizon"),
                decision_cutoff=self.decision_cutoff,
                assessed_as_of=self.assessed_as_of,
                oos_validation_plan_receipt_id=(self.oos_validation_plan_receipt_id),
                oos_validation_plan_receipt_sha256=(
                    self.oos_validation_plan_receipt_sha256
                ),
                total_return_definition_version=(self.total_return_definition_version),
                corporate_action_policy_version=(self.corporate_action_policy_version),
                adjustment_truth_receipt_id=self.adjustment_truth_receipt_id,
                adjustment_truth_payload_sha256=(self.adjustment_truth_payload_sha256),
                adjustment_truth_valid_through=(self.adjustment_truth_valid_through),
                adjustment_truth_available_at=(self.adjustment_truth_available_at),
                label_available_at=self.available_at,
                horizon_end=self.horizon_end,
                source_receipt_ids=self.source_receipt_ids,
                receipt=self.oos_validation_plan_receipt,
                verifier_id=self.oos_registry_verifier_id,
                verification=self.oos_registry_verification,
            )
            expected_reasons.extend(oos_reasons)
            computed_release_eligibility = (
                computed_mature and authority_accepted and oos_accepted
            )
            if computed_release_eligibility:
                expected_uses = (EvidenceUse.PREDICTIVE_VALIDATION,)
        elif self.label_class == "paper":
            if computed_mature:
                expected_uses = (EvidenceUse.PAPER_EXECUTION_VALIDATION,)
        elif self.label_class == "shadow":
            if computed_mature:
                expected_uses = (EvidenceUse.COUNTERFACTUAL_VALIDATION,)
        else:
            _require_text(self.unavailable_reason, "unavailable_reason")
            expected_reasons.append(self.unavailable_reason)

        if self.release_evidence_eligible is not computed_release_eligibility:
            raise LabelContractError("release_eligibility_mismatch")
        if self.eligible_uses != expected_uses:
            raise LabelContractError("eligible_uses_mismatch")
        if self.reasons != tuple(expected_reasons):
            raise LabelContractError("reasons_mismatch")

    def _validate_market_truth_binding(self) -> dict:
        if self.decision_cutoff is None:
            raise LabelContractError("release_evidence_binding_required")
        _require_aware(self.decision_cutoff, "decision_cutoff")
        _validate_receipt_ids(self.source_receipt_ids)
        if self.evidence_payload_sha256 is None:
            raise LabelContractError("release_evidence_binding_required")
        _require_sha256(self.evidence_payload_sha256, "evidence_payload_sha256")
        if not isinstance(self.canonical_evidence_payload_json, str):
            raise LabelContractError("canonical_evidence_payload_json_invalid")
        try:
            payload = json.loads(self.canonical_evidence_payload_json)
        except (TypeError, ValueError) as exc:
            raise LabelContractError("canonical_evidence_payload_json_invalid") from exc
        if (
            not isinstance(payload, dict)
            or json.dumps(
                payload,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
            != self.canonical_evidence_payload_json
        ):
            raise LabelContractError("canonical_evidence_payload_json_not_canonical")
        if not hmac.compare_digest(
            self.evidence_payload_sha256,
            self.recompute_evidence_payload_sha256(),
        ):
            raise LabelContractError("evidence_payload_sha256_mismatch")
        try:
            payload_cutoff = datetime.fromisoformat(payload["decision_cutoff"])
            payload_horizon_end = datetime.fromisoformat(payload["horizon_end"])
            payload_available_at = datetime.fromisoformat(payload["available_at"])
        except (KeyError, TypeError, ValueError) as exc:
            raise LabelContractError(
                "canonical_evidence_payload_fields_invalid"
            ) from exc
        _require_aware(payload_cutoff, "decision_cutoff")
        _require_aware(payload_horizon_end, "horizon_end")
        _require_aware(payload_available_at, "available_at")
        if (
            payload.get("schema_version") != 1
            or payload.get("evidence_type") != "market_truth_label.v1"
            or payload.get("label_id") != self.label_id
            or payload_cutoff != self.decision_cutoff
            or payload_horizon_end != self.horizon_end
            or payload_available_at != self.available_at
            or payload.get("source_receipt_ids") != list(self.source_receipt_ids)
        ):
            raise LabelContractError("canonical_evidence_payload_identity_mismatch")
        if payload_cutoff > payload_horizon_end:
            raise LabelContractError("decision_cutoff_after_horizon_end")
        binding_fields = (
            ("source_class", self.source_class),
            (
                "oos_validation_plan_receipt_id",
                self.oos_validation_plan_receipt_id,
            ),
            (
                "oos_validation_plan_receipt_sha256",
                self.oos_validation_plan_receipt_sha256,
            ),
            (
                "total_return_definition_version",
                self.total_return_definition_version,
            ),
            (
                "corporate_action_policy_version",
                self.corporate_action_policy_version,
            ),
            ("adjustment_truth_receipt_id", self.adjustment_truth_receipt_id),
            (
                "adjustment_truth_payload_sha256",
                self.adjustment_truth_payload_sha256,
            ),
        )
        if any(
            payload.get(field_name) != value for field_name, value in binding_fields
        ):
            raise LabelContractError("canonical_evidence_payload_identity_mismatch")
        for field_name, value in (
            (
                "adjustment_truth_valid_through",
                self.adjustment_truth_valid_through,
            ),
            (
                "adjustment_truth_available_at",
                self.adjustment_truth_available_at,
            ),
        ):
            payload_value = payload.get(field_name)
            if payload_value is None and value is None:
                continue
            if payload_value is None or value is None:
                raise LabelContractError("canonical_evidence_payload_identity_mismatch")
            try:
                payload_time = datetime.fromisoformat(payload_value)
            except (TypeError, ValueError) as exc:
                raise LabelContractError(
                    "canonical_evidence_payload_fields_invalid"
                ) from exc
            if payload_time != value:
                raise LabelContractError("canonical_evidence_payload_identity_mismatch")
        return payload

    def recompute_evidence_payload_sha256(self) -> Optional[str]:
        if self.canonical_evidence_payload_json is None:
            return None
        return hashlib.sha256(
            self.canonical_evidence_payload_json.encode("utf-8")
        ).hexdigest()


# Backward-compatible name for readers of the initial local contract.
LabelMaturityAssessment = LabelMaturityRecord


def _time_reasons(
    *,
    horizon_end: datetime,
    available_at: datetime,
    as_of: datetime,
) -> Tuple[str, ...]:
    reasons = []
    if available_at > as_of:
        reasons.append("label_not_available_as_of")
    if horizon_end > as_of:
        reasons.append("horizon_not_complete_as_of")
    return tuple(reasons)


def _authority_result(
    *,
    evidence_payload_sha256: Optional[str],
    source_receipt_ids: Tuple[str, ...],
    available_at: datetime,
    assessed_as_of: datetime,
    authority_proof: Optional[FrozenAuthorityProof],
    authority_verifier_id: Optional[str],
    authority_verification: Optional[FrozenAuthorityVerification],
) -> Tuple[bool, Tuple[str, ...]]:
    reasons = []
    if authority_proof is None:
        reasons.append("frozen_authority_proof_required")
    if authority_verifier_id is None:
        reasons.append("frozen_authority_verifier_required")
    else:
        _require_text(authority_verifier_id, "authority_verifier_id")
    if reasons:
        if authority_verification is not None:
            raise LabelContractError("authority_verification_without_complete_binding")
        return False, tuple(reasons)

    if not isinstance(authority_proof, FrozenAuthorityProof):
        raise LabelContractError("frozen_authority_proof_invalid")
    if (
        evidence_payload_sha256 is None
        or not hmac.compare_digest(
            authority_proof.evidence_payload_sha256,
            evidence_payload_sha256,
        )
        or authority_proof.source_receipt_ids != source_receipt_ids
    ):
        return False, ("frozen_authority_proof_identity_mismatch",)
    if authority_proof.frozen_at < available_at:
        return False, ("frozen_authority_proof_time_invalid",)
    if authority_proof.frozen_at > assessed_as_of:
        return False, ("frozen_authority_proof_not_available_as_of",)
    if authority_verification is None:
        return False, ("frozen_authority_verification_unavailable",)
    if not isinstance(authority_verification, FrozenAuthorityVerification):
        raise LabelContractError("frozen_authority_verification_invalid")
    if (
        authority_verification.verifier_id != authority_verifier_id
        or authority_verification.proof_id != authority_proof.proof_id
        or authority_verification.authority_id != authority_proof.authority_id
        or authority_verification.authority_version != authority_proof.authority_version
        or not hmac.compare_digest(
            authority_verification.evidence_payload_sha256,
            evidence_payload_sha256,
        )
        or authority_verification.verified_at < authority_proof.frozen_at
        or authority_verification.verified_at > assessed_as_of
    ):
        return False, ("frozen_authority_verification_binding_mismatch",)
    if not authority_verification.accepted:
        return False, ("frozen_authority_proof_rejected",)
    return True, ()


def _oos_registry_result(
    *,
    source_class: Optional[str],
    horizon: object,
    decision_cutoff: datetime,
    assessed_as_of: datetime,
    oos_validation_plan_receipt_id: Optional[str],
    oos_validation_plan_receipt_sha256: Optional[str],
    total_return_definition_version: Optional[str],
    corporate_action_policy_version: Optional[str],
    adjustment_truth_receipt_id: Optional[str],
    adjustment_truth_payload_sha256: Optional[str],
    adjustment_truth_valid_through: Optional[datetime],
    adjustment_truth_available_at: Optional[datetime],
    label_available_at: datetime,
    horizon_end: datetime,
    source_receipt_ids: Tuple[str, ...],
    receipt: Optional[FrozenOOSValidationPlanReceipt],
    verifier_id: Optional[str],
    verification: Optional[FrozenOOSRegistryVerification],
) -> Tuple[bool, Tuple[str, ...]]:
    reasons = []
    if source_class is None:
        reasons.append("source_class_required")
    elif source_class != "market_truth":
        reasons.append("source_class_not_release_eligible")
    if (
        oos_validation_plan_receipt_id is None
        or oos_validation_plan_receipt_sha256 is None
    ):
        reasons.append("frozen_oos_validation_plan_receipt_required")
    if total_return_definition_version is None:
        reasons.append("total_return_definition_version_required")
    if corporate_action_policy_version is None:
        reasons.append("corporate_action_policy_version_required")
    if (
        adjustment_truth_receipt_id is None
        or adjustment_truth_payload_sha256 is None
        or adjustment_truth_valid_through is None
        or adjustment_truth_available_at is None
    ):
        reasons.append("adjustment_truth_receipt_required")
    if reasons and any(reason.endswith("_required") for reason in reasons):
        if verification is not None:
            raise LabelContractError(
                "oos_registry_verification_without_complete_binding"
            )
        return False, tuple(reasons)

    _require_text(source_class, "source_class")
    _require_text(
        oos_validation_plan_receipt_id,
        "oos_validation_plan_receipt_id",
    )
    _require_sha256(
        oos_validation_plan_receipt_sha256,
        "oos_validation_plan_receipt_sha256",
    )
    _require_text(
        total_return_definition_version,
        "total_return_definition_version",
    )
    _require_text(
        corporate_action_policy_version,
        "corporate_action_policy_version",
    )
    _require_text(adjustment_truth_receipt_id, "adjustment_truth_receipt_id")
    _require_sha256(
        adjustment_truth_payload_sha256,
        "adjustment_truth_payload_sha256",
    )
    _require_aware(
        adjustment_truth_valid_through,
        "adjustment_truth_valid_through",
    )
    _require_aware(
        adjustment_truth_available_at,
        "adjustment_truth_available_at",
    )
    if adjustment_truth_receipt_id not in source_receipt_ids:
        reasons.append("adjustment_truth_receipt_not_in_source_receipts")
    if adjustment_truth_valid_through < horizon_end:
        reasons.append("adjustment_truth_does_not_cover_horizon")
    if adjustment_truth_available_at < adjustment_truth_valid_through:
        reasons.append("adjustment_truth_available_before_valid_through")
    if adjustment_truth_available_at > label_available_at:
        reasons.append("label_available_before_adjustment_truth")

    if receipt is None:
        reasons.append("frozen_oos_validation_plan_receipt_required")
    if verifier_id is None:
        reasons.append("frozen_oos_registry_verifier_required")
    else:
        _require_text(verifier_id, "oos_registry_verifier_id")
    if receipt is None or verifier_id is None:
        if verification is not None:
            raise LabelContractError(
                "oos_registry_verification_without_complete_binding"
            )
        return False, tuple(reasons)
    if not isinstance(receipt, FrozenOOSValidationPlanReceipt):
        raise LabelContractError("frozen_oos_validation_plan_receipt_invalid")

    if (
        receipt.receipt_id != oos_validation_plan_receipt_id
        or not hmac.compare_digest(
            receipt.receipt_payload_sha256,
            oos_validation_plan_receipt_sha256,
        )
        or receipt.primary_horizon != horizon
        or receipt.eligible_source_class != source_class
        or (receipt.total_return_definition_version != total_return_definition_version)
        or (receipt.corporate_action_policy_version != corporate_action_policy_version)
    ):
        reasons.append("frozen_oos_validation_plan_identity_mismatch")
    if receipt.frozen_at > decision_cutoff:
        reasons.append("frozen_oos_validation_plan_after_decision_cutoff")
    if receipt.frozen_at > assessed_as_of:
        reasons.append("frozen_oos_validation_plan_not_available_as_of")
    if reasons:
        if verification is not None:
            raise LabelContractError(
                "oos_registry_verification_without_valid_plan_binding"
            )
        return False, tuple(reasons)

    if verification is None:
        return False, ("frozen_oos_registry_verification_unavailable",)
    if not isinstance(verification, FrozenOOSRegistryVerification):
        raise LabelContractError("frozen_oos_registry_verification_invalid")
    if (
        verification.verifier_id != verifier_id
        or verification.receipt_id != receipt.receipt_id
        or verification.registry_id != receipt.registry_id
        or verification.registry_version != receipt.registry_version
        or not hmac.compare_digest(
            verification.receipt_payload_sha256,
            receipt.receipt_payload_sha256,
        )
        or verification.verified_at < receipt.frozen_at
    ):
        return False, ("frozen_oos_registry_verification_binding_mismatch",)
    if verification.verified_at > assessed_as_of:
        return False, ("frozen_oos_registry_verification_not_available_as_of",)
    if not verification.accepted:
        return False, ("frozen_oos_registry_receipt_rejected",)
    return True, ()


def assess_label_maturity(
    label: LabelObservation,
    *,
    as_of: datetime,
    authority_proof: Optional[FrozenAuthorityProof] = None,
    authority_verifier: Optional[FrozenAuthorityVerifier] = None,
    oos_validation_plan_receipt: Optional[FrozenOOSValidationPlanReceipt] = None,
    oos_registry_verifier: Optional[FrozenOOSRegistryVerifier] = None,
) -> LabelMaturityRecord:
    """Assess one label without persisting data or mutating label authority."""

    _require_aware(as_of, "as_of")
    reasons = list(
        _time_reasons(
            horizon_end=label.horizon_end,
            available_at=label.available_at,
            as_of=as_of,
        )
    )

    if isinstance(label, UnavailableOracle):
        return LabelMaturityRecord(
            label_id=label.label_id,
            label_class="unavailable_oracle",
            mature=False,
            release_evidence_eligible=False,
            eligible_uses=(),
            reasons=tuple((*reasons, label.reason)),
            horizon_end=label.horizon_end,
            available_at=label.available_at,
            assessed_as_of=as_of,
            unavailable_reason=label.reason,
        )

    mature = not reasons
    if isinstance(label, MarketTruth):
        verifier_id = None
        verification = None
        if authority_verifier is not None:
            verifier_id = getattr(authority_verifier, "verifier_id", None)
            _require_text(verifier_id, "authority_verifier_id")
        if authority_proof is not None and authority_verifier is not None:
            preliminary_accepted, preliminary_reasons = _authority_result(
                evidence_payload_sha256=label.evidence_payload_sha256,
                source_receipt_ids=label.source_receipt_ids,
                available_at=label.available_at,
                assessed_as_of=as_of,
                authority_proof=authority_proof,
                authority_verifier_id=verifier_id,
                authority_verification=None,
            )
            del preliminary_accepted
            if preliminary_reasons == ("frozen_authority_verification_unavailable",):
                try:
                    verification = authority_verifier.verify(
                        proof=authority_proof,
                        evidence_payload_json=label.canonical_evidence_payload_json(),
                        assessed_as_of=as_of,
                    )
                except Exception:
                    verification = None
                if verification is not None and not isinstance(
                    verification,
                    FrozenAuthorityVerification,
                ):
                    raise LabelContractError("frozen_authority_verification_invalid")
        authority_accepted, authority_reasons = _authority_result(
            evidence_payload_sha256=label.evidence_payload_sha256,
            source_receipt_ids=label.source_receipt_ids,
            available_at=label.available_at,
            assessed_as_of=as_of,
            authority_proof=authority_proof,
            authority_verifier_id=verifier_id,
            authority_verification=verification,
        )
        reasons.extend(authority_reasons)
        oos_verifier_id = None
        oos_verification = None
        if oos_registry_verifier is not None:
            oos_verifier_id = getattr(oos_registry_verifier, "verifier_id", None)
            _require_text(oos_verifier_id, "oos_registry_verifier_id")
        if (
            oos_validation_plan_receipt is not None
            and oos_registry_verifier is not None
        ):
            preliminary_oos_accepted, preliminary_oos_reasons = _oos_registry_result(
                source_class=label.source_class,
                horizon=label.horizon,
                decision_cutoff=label.decision_cutoff,
                assessed_as_of=as_of,
                oos_validation_plan_receipt_id=(label.oos_validation_plan_receipt_id),
                oos_validation_plan_receipt_sha256=(
                    label.oos_validation_plan_receipt_sha256
                ),
                total_return_definition_version=(label.total_return_definition_version),
                corporate_action_policy_version=(label.corporate_action_policy_version),
                adjustment_truth_receipt_id=(label.adjustment_truth_receipt_id),
                adjustment_truth_payload_sha256=(label.adjustment_truth_payload_sha256),
                adjustment_truth_valid_through=(label.adjustment_truth_valid_through),
                adjustment_truth_available_at=(label.adjustment_truth_available_at),
                label_available_at=label.available_at,
                horizon_end=label.horizon_end,
                source_receipt_ids=label.source_receipt_ids,
                receipt=oos_validation_plan_receipt,
                verifier_id=oos_verifier_id,
                verification=None,
            )
            del preliminary_oos_accepted
            if preliminary_oos_reasons == (
                "frozen_oos_registry_verification_unavailable",
            ):
                try:
                    oos_verification = oos_registry_verifier.verify(
                        receipt=oos_validation_plan_receipt,
                        receipt_payload_json=(
                            oos_validation_plan_receipt.canonical_receipt_payload_json()
                        ),
                        assessed_as_of=as_of,
                    )
                except Exception:
                    oos_verification = None
                if oos_verification is not None and not isinstance(
                    oos_verification,
                    FrozenOOSRegistryVerification,
                ):
                    raise LabelContractError("frozen_oos_registry_verification_invalid")
        oos_accepted, oos_reasons = _oos_registry_result(
            source_class=label.source_class,
            horizon=label.horizon,
            decision_cutoff=label.decision_cutoff,
            assessed_as_of=as_of,
            oos_validation_plan_receipt_id=(label.oos_validation_plan_receipt_id),
            oos_validation_plan_receipt_sha256=(
                label.oos_validation_plan_receipt_sha256
            ),
            total_return_definition_version=(label.total_return_definition_version),
            corporate_action_policy_version=(label.corporate_action_policy_version),
            adjustment_truth_receipt_id=label.adjustment_truth_receipt_id,
            adjustment_truth_payload_sha256=(label.adjustment_truth_payload_sha256),
            adjustment_truth_valid_through=(label.adjustment_truth_valid_through),
            adjustment_truth_available_at=label.adjustment_truth_available_at,
            label_available_at=label.available_at,
            horizon_end=label.horizon_end,
            source_receipt_ids=label.source_receipt_ids,
            receipt=oos_validation_plan_receipt,
            verifier_id=oos_verifier_id,
            verification=oos_verification,
        )
        reasons.extend(oos_reasons)
        release_eligible = mature and authority_accepted and oos_accepted
        return LabelMaturityRecord(
            label_id=label.label_id,
            label_class="market_truth",
            mature=mature,
            release_evidence_eligible=release_eligible,
            eligible_uses=(
                (EvidenceUse.PREDICTIVE_VALIDATION,) if release_eligible else ()
            ),
            reasons=tuple(reasons),
            horizon_end=label.horizon_end,
            available_at=label.available_at,
            decision_cutoff=label.decision_cutoff,
            assessed_as_of=as_of,
            source_receipt_ids=label.source_receipt_ids,
            evidence_payload_sha256=label.evidence_payload_sha256,
            canonical_evidence_payload_json=label.canonical_evidence_payload_json(),
            authority_proof=authority_proof,
            authority_verifier_id=verifier_id,
            authority_verification=verification,
            source_class=label.source_class,
            oos_validation_plan_receipt_id=(label.oos_validation_plan_receipt_id),
            oos_validation_plan_receipt_sha256=(
                label.oos_validation_plan_receipt_sha256
            ),
            total_return_definition_version=(label.total_return_definition_version),
            corporate_action_policy_version=(label.corporate_action_policy_version),
            adjustment_truth_receipt_id=label.adjustment_truth_receipt_id,
            adjustment_truth_payload_sha256=(label.adjustment_truth_payload_sha256),
            adjustment_truth_valid_through=(label.adjustment_truth_valid_through),
            adjustment_truth_available_at=label.adjustment_truth_available_at,
            oos_validation_plan_receipt=oos_validation_plan_receipt,
            oos_registry_verifier_id=oos_verifier_id,
            oos_registry_verification=oos_verification,
            authority_verifier=authority_verifier,
            oos_registry_verifier=oos_registry_verifier,
        )
    if isinstance(label, Paper):
        return LabelMaturityRecord(
            label_id=label.label_id,
            label_class="paper",
            mature=mature,
            release_evidence_eligible=False,
            eligible_uses=(EvidenceUse.PAPER_EXECUTION_VALIDATION,) if mature else (),
            reasons=tuple(reasons),
            horizon_end=label.horizon_end,
            available_at=label.available_at,
            assessed_as_of=as_of,
        )
    if isinstance(label, Shadow):
        return LabelMaturityRecord(
            label_id=label.label_id,
            label_class="shadow",
            mature=mature,
            release_evidence_eligible=False,
            eligible_uses=(EvidenceUse.COUNTERFACTUAL_VALIDATION,) if mature else (),
            reasons=tuple(reasons),
            horizon_end=label.horizon_end,
            available_at=label.available_at,
            assessed_as_of=as_of,
        )
    raise LabelContractError("unsupported_label_class")
