"""Immutable, externally verified risk-group evidence for the 50k A-share plan.

This module does not choose caps and does not provide a verifier.  Callers must
provide a human-reviewed policy plus detached policy/exposure verifier ports.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Iterable, Protocol


THESIS_RISK_DIMENSIONS = (
    "industry",
    "thesis",
    "raw_material",
    "policy_event",
    "crowding",
    "model_family",
)
_THESIS_RISK_DIMENSION_SET = frozenset(THESIS_RISK_DIMENSIONS)
_EXPOSURE_KINDS = frozenset({"candidate", "position", "pending"})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _aware(value: object, *, field_name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{field_name}_timezone_required")
    return value


def _nonempty(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field_name}_invalid")
    return value


def _sha256(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{field_name}_invalid")
    return value


def _money(value: object, *, field_name: str, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name}_invalid")
    amount = float(value)
    minimum = 0.0 if not positive else 1e-12
    if not math.isfinite(amount) or amount < minimum:
        raise ValueError(f"{field_name}_invalid")
    return amount


@dataclass(frozen=True)
class ThesisRiskGroups:
    industry: str
    thesis: str
    raw_material: str
    policy_event: str
    crowding: str
    model_family: str

    def __post_init__(self) -> None:
        for dimension, group_id in self.items():
            _nonempty(group_id, field_name=f"thesis_risk_group_{dimension}")

    def items(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            (dimension, getattr(self, dimension))
            for dimension in THESIS_RISK_DIMENSIONS
        )


@dataclass(frozen=True)
class ThesisRiskDimensionCap:
    dimension: str
    max_exposure_cny: float

    def __post_init__(self) -> None:
        if self.dimension not in _THESIS_RISK_DIMENSION_SET:
            raise ValueError("thesis_risk_cap_dimension_invalid")
        _money(
            self.max_exposure_cny,
            field_name="thesis_risk_cap_cny",
            positive=True,
        )


@dataclass(frozen=True)
class ThesisRiskPolicy:
    policy_id: str
    reviewed_by: str
    review_reference: str
    effective_at: datetime
    valid_until: datetime
    dimension_caps: tuple[ThesisRiskDimensionCap, ...]
    policy_sha256: str = ""

    def __post_init__(self) -> None:
        _nonempty(self.policy_id, field_name="thesis_risk_policy_id")
        try:
            _nonempty(self.reviewed_by, field_name="thesis_risk_policy_reviewed_by")
            _nonempty(
                self.review_reference,
                field_name="thesis_risk_policy_review_reference",
            )
        except ValueError as exc:
            raise ValueError("thesis_risk_policy_review_invalid") from exc
        effective_at = _aware(
            self.effective_at,
            field_name="thesis_risk_policy_effective_at",
        )
        valid_until = _aware(
            self.valid_until,
            field_name="thesis_risk_policy_valid_until",
        )
        if valid_until < effective_at:
            raise ValueError("thesis_risk_policy_validity_invalid")
        if not isinstance(self.dimension_caps, tuple) or any(
            not isinstance(cap, ThesisRiskDimensionCap) for cap in self.dimension_caps
        ):
            raise ValueError("thesis_risk_policy_dimensions_invalid")
        dimensions = tuple(cap.dimension for cap in self.dimension_caps)
        if (
            len(dimensions) != len(THESIS_RISK_DIMENSIONS)
            or set(dimensions) != _THESIS_RISK_DIMENSION_SET
        ):
            raise ValueError("thesis_risk_policy_dimensions_invalid")
        computed = _canonical_sha256(self._content_payload())
        if self.policy_sha256 and self.policy_sha256 != computed:
            raise ValueError("thesis_risk_policy_hash_mismatch")
        object.__setattr__(self, "policy_sha256", computed)

    def _content_payload(self) -> dict[str, object]:
        return {
            "policy_id": self.policy_id,
            "reviewed_by": self.reviewed_by,
            "review_reference": self.review_reference,
            "effective_at": self.effective_at.astimezone(timezone.utc).isoformat(),
            "valid_until": self.valid_until.astimezone(timezone.utc).isoformat(),
            "dimension_caps": [
                asdict(cap)
                for cap in sorted(
                    self.dimension_caps,
                    key=lambda item: item.dimension,
                )
            ],
        }

    def cap_for(self, dimension: str) -> float:
        if dimension not in _THESIS_RISK_DIMENSION_SET:
            raise ValueError("thesis_risk_cap_dimension_invalid")
        return next(
            float(cap.max_exposure_cny)
            for cap in self.dimension_caps
            if cap.dimension == dimension
        )


@dataclass(frozen=True)
class ThesisRiskPolicyVerification:
    verifier_id: str
    verifier_version: str
    policy_id: str
    policy_sha256: str
    reviewed_by: str
    review_reference: str
    verified_at: datetime
    valid_until: datetime
    promotion_eligible: bool
    proof_sha256: str

    def __post_init__(self) -> None:
        for field_name, value in (
            ("verifier_id", self.verifier_id),
            ("verifier_version", self.verifier_version),
            ("policy_id", self.policy_id),
            ("reviewed_by", self.reviewed_by),
            ("review_reference", self.review_reference),
        ):
            _nonempty(value, field_name=f"thesis_risk_policy_proof_{field_name}")
        _sha256(
            self.policy_sha256,
            field_name="thesis_risk_policy_proof_policy_sha256",
        )
        verified_at = _aware(
            self.verified_at,
            field_name="thesis_risk_policy_proof_verified_at",
        )
        valid_until = _aware(
            self.valid_until,
            field_name="thesis_risk_policy_proof_valid_until",
        )
        if valid_until < verified_at:
            raise ValueError("thesis_risk_policy_proof_validity_invalid")
        if type(self.promotion_eligible) is not bool:
            raise ValueError("thesis_risk_policy_proof_promotion_invalid")
        _sha256(self.proof_sha256, field_name="thesis_risk_policy_proof_sha256")
        if self.proof_sha256 != _canonical_sha256(self._proof_payload()):
            raise ValueError("thesis_risk_policy_proof_hash_mismatch")

    def _proof_payload(self) -> dict[str, object]:
        return {
            "verifier_id": self.verifier_id,
            "verifier_version": self.verifier_version,
            "policy_id": self.policy_id,
            "policy_sha256": self.policy_sha256,
            "reviewed_by": self.reviewed_by,
            "review_reference": self.review_reference,
            "verified_at": self.verified_at.astimezone(timezone.utc).isoformat(),
            "valid_until": self.valid_until.astimezone(timezone.utc).isoformat(),
            "promotion_eligible": self.promotion_eligible,
        }

    @classmethod
    def create(
        cls,
        *,
        policy: ThesisRiskPolicy,
        verifier_id: str,
        verifier_version: str,
        verified_at: datetime,
        valid_until: datetime,
        promotion_eligible: bool,
    ) -> ThesisRiskPolicyVerification:
        values = {
            "verifier_id": verifier_id,
            "verifier_version": verifier_version,
            "policy_id": policy.policy_id,
            "policy_sha256": policy.policy_sha256,
            "reviewed_by": policy.reviewed_by,
            "review_reference": policy.review_reference,
            "verified_at": verified_at,
            "valid_until": valid_until,
            "promotion_eligible": promotion_eligible,
        }
        payload = {
            **values,
            "verified_at": verified_at.astimezone(timezone.utc).isoformat(),
            "valid_until": valid_until.astimezone(timezone.utc).isoformat(),
        }
        return cls(**values, proof_sha256=_canonical_sha256(payload))


class ThesisRiskPolicyVerifier(Protocol):
    def verify(
        self,
        policy: ThesisRiskPolicy,
        *,
        decision_time: datetime,
    ) -> ThesisRiskPolicyVerification: ...


def verify_thesis_risk_policy(
    *,
    policy: ThesisRiskPolicy,
    verifier: ThesisRiskPolicyVerifier,
    decision_time: datetime,
) -> ThesisRiskPolicyVerification:
    if not isinstance(policy, ThesisRiskPolicy):
        raise ValueError("thesis_risk_policy_required")
    resolved_decision_time = _aware(
        decision_time,
        field_name="thesis_risk_decision_time",
    )
    verify = getattr(verifier, "verify", None)
    if not callable(verify):
        raise ValueError("thesis_risk_policy_verifier_required")
    try:
        proof = verify(policy, decision_time=resolved_decision_time)
    except Exception as exc:
        raise ValueError("thesis_risk_policy_verification_failed") from exc
    if not isinstance(proof, ThesisRiskPolicyVerification):
        raise ValueError("thesis_risk_policy_verification_failed")
    expected = {
        "policy_id": policy.policy_id,
        "policy_sha256": policy.policy_sha256,
        "reviewed_by": policy.reviewed_by,
        "review_reference": policy.review_reference,
    }
    if any(getattr(proof, name) != value for name, value in expected.items()):
        raise ValueError("thesis_risk_policy_proof_binding_mismatch")
    if not policy.effective_at <= resolved_decision_time <= policy.valid_until:
        raise ValueError("thesis_risk_policy_not_effective")
    if proof.verified_at > resolved_decision_time:
        raise ValueError("thesis_risk_policy_proof_after_decision")
    if proof.valid_until < resolved_decision_time:
        raise ValueError("thesis_risk_policy_proof_expired")
    return proof


@dataclass(frozen=True)
class ThesisRiskExposureReceipt:
    exposure_id: str
    exposure_kind: str
    symbol: str
    groups: ThesisRiskGroups
    notional_cny: float
    as_of: datetime
    available_at: datetime
    source_dataset_id: str
    source_receipt_id: str
    source_lineage_sha256: str
    source_content_sha256: str
    binding_reference_id: str
    binding_sha256: str
    receipt_sha256: str
    pending_action: str | None = None

    def __post_init__(self) -> None:
        for field_name, value in (
            ("exposure_id", self.exposure_id),
            ("symbol", self.symbol),
            ("source_dataset_id", self.source_dataset_id),
            ("source_receipt_id", self.source_receipt_id),
            ("binding_reference_id", self.binding_reference_id),
        ):
            _nonempty(value, field_name=f"thesis_risk_{field_name}")
        if self.exposure_kind not in _EXPOSURE_KINDS:
            raise ValueError("thesis_risk_exposure_kind_invalid")
        if self.exposure_kind == "pending":
            if self.pending_action not in {"open", "increase"}:
                raise ValueError("pending_thesis_risk_action_invalid")
        elif self.pending_action is not None:
            raise ValueError("nonpending_thesis_risk_action_invalid")
        if not isinstance(self.groups, ThesisRiskGroups):
            raise ValueError("thesis_risk_groups_required")
        notional = _money(
            self.notional_cny,
            field_name="thesis_risk_notional_cny",
        )
        if self.exposure_kind == "candidate" and notional != 0.0:
            raise ValueError("candidate_thesis_risk_notional_must_be_zero")
        if self.exposure_kind in {"position", "pending"} and notional <= 0.0:
            raise ValueError("thesis_risk_existing_notional_must_be_positive")
        as_of = _aware(self.as_of, field_name="thesis_risk_as_of")
        available_at = _aware(
            self.available_at,
            field_name="thesis_risk_available_at",
        )
        if as_of > available_at:
            raise ValueError("thesis_risk_available_before_as_of")
        for field_name, value in (
            ("source_lineage_sha256", self.source_lineage_sha256),
            ("source_content_sha256", self.source_content_sha256),
            ("binding_sha256", self.binding_sha256),
            ("receipt_sha256", self.receipt_sha256),
        ):
            _sha256(value, field_name=f"thesis_risk_{field_name}")
        if self.receipt_sha256 != self.compute_receipt_sha256(
            exposure_id=self.exposure_id,
            exposure_kind=self.exposure_kind,
            symbol=self.symbol,
            groups=self.groups,
            notional_cny=self.notional_cny,
            as_of=self.as_of,
            available_at=self.available_at,
            source_dataset_id=self.source_dataset_id,
            source_receipt_id=self.source_receipt_id,
            source_lineage_sha256=self.source_lineage_sha256,
            source_content_sha256=self.source_content_sha256,
            binding_reference_id=self.binding_reference_id,
            binding_sha256=self.binding_sha256,
            pending_action=self.pending_action,
        ):
            raise ValueError("thesis_risk_receipt_hash_mismatch")

    @staticmethod
    def compute_receipt_sha256(
        *,
        exposure_id: str,
        exposure_kind: str,
        symbol: str,
        groups: ThesisRiskGroups,
        notional_cny: float,
        as_of: datetime,
        available_at: datetime,
        source_dataset_id: str,
        source_receipt_id: str,
        source_lineage_sha256: str,
        source_content_sha256: str,
        binding_reference_id: str,
        binding_sha256: str,
        pending_action: str | None = None,
    ) -> str:
        return _canonical_sha256(
            {
                "exposure_id": exposure_id,
                "exposure_kind": exposure_kind,
                "symbol": symbol,
                "groups": asdict(groups),
                "notional_cny": notional_cny,
                "as_of": as_of.astimezone(timezone.utc).isoformat(),
                "available_at": available_at.astimezone(timezone.utc).isoformat(),
                "source_dataset_id": source_dataset_id,
                "source_receipt_id": source_receipt_id,
                "source_lineage_sha256": source_lineage_sha256,
                "source_content_sha256": source_content_sha256,
                "binding_reference_id": binding_reference_id,
                "binding_sha256": binding_sha256,
                "pending_action": pending_action,
            }
        )

    @classmethod
    def create(cls, **values: object) -> ThesisRiskExposureReceipt:
        if values.get("exposure_kind") == "pending" and "pending_action" not in values:
            raise ValueError("pending_thesis_risk_action_required")
        return cls(
            **values,
            receipt_sha256=cls.compute_receipt_sha256(**values),
        )


@dataclass(frozen=True)
class ThesisRiskExposureVerification:
    verifier_id: str
    verifier_version: str
    exposure_id: str
    exposure_receipt_sha256: str
    authority_notional_cny: float
    authority_binding_reference_id: str
    authority_binding_sha256: str
    verified_at: datetime
    valid_until: datetime
    promotion_eligible: bool
    proof_sha256: str

    def __post_init__(self) -> None:
        for field_name, value in (
            ("verifier_id", self.verifier_id),
            ("verifier_version", self.verifier_version),
            ("exposure_id", self.exposure_id),
            (
                "authority_binding_reference_id",
                self.authority_binding_reference_id,
            ),
        ):
            _nonempty(value, field_name=f"thesis_risk_exposure_proof_{field_name}")
        _sha256(
            self.exposure_receipt_sha256,
            field_name="thesis_risk_exposure_proof_receipt_sha256",
        )
        _money(
            self.authority_notional_cny,
            field_name="thesis_risk_exposure_proof_notional_cny",
        )
        _sha256(
            self.authority_binding_sha256,
            field_name="thesis_risk_exposure_proof_binding_sha256",
        )
        verified_at = _aware(
            self.verified_at,
            field_name="thesis_risk_exposure_proof_verified_at",
        )
        valid_until = _aware(
            self.valid_until,
            field_name="thesis_risk_exposure_proof_valid_until",
        )
        if valid_until < verified_at:
            raise ValueError("thesis_risk_exposure_proof_validity_invalid")
        if type(self.promotion_eligible) is not bool:
            raise ValueError("thesis_risk_exposure_proof_promotion_invalid")
        _sha256(self.proof_sha256, field_name="thesis_risk_exposure_proof_sha256")
        if self.proof_sha256 != _canonical_sha256(self._proof_payload()):
            raise ValueError("thesis_risk_exposure_proof_hash_mismatch")

    def _proof_payload(self) -> dict[str, object]:
        return {
            "verifier_id": self.verifier_id,
            "verifier_version": self.verifier_version,
            "exposure_id": self.exposure_id,
            "exposure_receipt_sha256": self.exposure_receipt_sha256,
            "authority_notional_cny": self.authority_notional_cny,
            "authority_binding_reference_id": (self.authority_binding_reference_id),
            "authority_binding_sha256": self.authority_binding_sha256,
            "verified_at": self.verified_at.astimezone(timezone.utc).isoformat(),
            "valid_until": self.valid_until.astimezone(timezone.utc).isoformat(),
            "promotion_eligible": self.promotion_eligible,
        }

    @classmethod
    def create(
        cls,
        *,
        receipt: ThesisRiskExposureReceipt,
        verifier_id: str,
        verifier_version: str,
        verified_at: datetime,
        valid_until: datetime,
        promotion_eligible: bool,
        authority_notional_cny: float,
        authority_binding_reference_id: str,
        authority_binding_sha256: str,
    ) -> ThesisRiskExposureVerification:
        values = {
            "verifier_id": verifier_id,
            "verifier_version": verifier_version,
            "exposure_id": receipt.exposure_id,
            "exposure_receipt_sha256": receipt.receipt_sha256,
            "authority_notional_cny": authority_notional_cny,
            "authority_binding_reference_id": authority_binding_reference_id,
            "authority_binding_sha256": authority_binding_sha256,
            "verified_at": verified_at,
            "valid_until": valid_until,
            "promotion_eligible": promotion_eligible,
        }
        payload = {
            **values,
            "verified_at": verified_at.astimezone(timezone.utc).isoformat(),
            "valid_until": valid_until.astimezone(timezone.utc).isoformat(),
        }
        return cls(**values, proof_sha256=_canonical_sha256(payload))


class ThesisRiskExposureVerifier(Protocol):
    def verify(
        self,
        receipt: ThesisRiskExposureReceipt,
        *,
        decision_time: datetime,
    ) -> ThesisRiskExposureVerification: ...


def verify_thesis_risk_exposure(
    *,
    receipt: ThesisRiskExposureReceipt,
    verifier: ThesisRiskExposureVerifier,
    decision_time: datetime,
) -> ThesisRiskExposureVerification:
    if not isinstance(receipt, ThesisRiskExposureReceipt):
        raise ValueError("thesis_risk_exposure_receipt_invalid")
    resolved_decision_time = _aware(
        decision_time,
        field_name="thesis_risk_decision_time",
    )
    if receipt.available_at > resolved_decision_time:
        raise ValueError("thesis_risk_exposure_not_available")
    verify = getattr(verifier, "verify", None)
    if not callable(verify):
        raise ValueError("thesis_risk_exposure_verifier_required")
    try:
        proof = verify(receipt, decision_time=resolved_decision_time)
    except Exception as exc:
        raise ValueError("thesis_risk_exposure_verification_failed") from exc
    if not isinstance(proof, ThesisRiskExposureVerification):
        raise ValueError("thesis_risk_exposure_verification_failed")
    expected = {
        "exposure_id": receipt.exposure_id,
        "exposure_receipt_sha256": receipt.receipt_sha256,
        "authority_notional_cny": receipt.notional_cny,
        "authority_binding_reference_id": receipt.binding_reference_id,
        "authority_binding_sha256": receipt.binding_sha256,
    }
    if any(getattr(proof, name) != value for name, value in expected.items()):
        raise ValueError("thesis_risk_exposure_proof_binding_mismatch")
    if proof.verified_at > resolved_decision_time:
        raise ValueError("thesis_risk_exposure_proof_after_decision")
    if proof.valid_until < resolved_decision_time:
        raise ValueError("thesis_risk_exposure_proof_expired")
    return proof


@dataclass(frozen=True)
class ThesisRiskExposureSetReceipt:
    """Content-addressed membership receipt for the complete risk book."""

    exposure_set_id: str
    decision_time: datetime
    as_of: datetime
    available_at: datetime
    source_id: str
    source_generation: int
    source_lineage_sha256: str
    exposure_receipt_sha256s: tuple[str, ...]
    candidate_count: int
    position_count: int
    pending_count: int
    receipt_sha256: str

    def __post_init__(self) -> None:
        _nonempty(
            self.exposure_set_id,
            field_name="thesis_risk_exposure_set_id",
        )
        _nonempty(self.source_id, field_name="thesis_risk_exposure_set_source_id")
        decision_time = _aware(
            self.decision_time,
            field_name="thesis_risk_exposure_set_decision_time",
        )
        as_of = _aware(
            self.as_of,
            field_name="thesis_risk_exposure_set_as_of",
        )
        available_at = _aware(
            self.available_at,
            field_name="thesis_risk_exposure_set_available_at",
        )
        if as_of > available_at or available_at > decision_time:
            raise ValueError("thesis_risk_exposure_set_time_invalid")
        if (
            isinstance(self.source_generation, bool)
            or not isinstance(self.source_generation, int)
            or self.source_generation < 1
        ):
            raise ValueError("thesis_risk_exposure_set_generation_invalid")
        _sha256(
            self.source_lineage_sha256,
            field_name="thesis_risk_exposure_set_lineage_sha256",
        )
        if (
            not isinstance(self.exposure_receipt_sha256s, tuple)
            or tuple(sorted(self.exposure_receipt_sha256s))
            != self.exposure_receipt_sha256s
            or len(set(self.exposure_receipt_sha256s))
            != len(self.exposure_receipt_sha256s)
            or any(
                not _SHA256_RE.fullmatch(value)
                for value in self.exposure_receipt_sha256s
            )
        ):
            raise ValueError("thesis_risk_exposure_set_members_invalid")
        for field_name, value in (
            ("candidate_count", self.candidate_count),
            ("position_count", self.position_count),
            ("pending_count", self.pending_count),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"thesis_risk_exposure_set_{field_name}_invalid")
        if self.candidate_count + self.position_count + self.pending_count != len(
            self.exposure_receipt_sha256s
        ):
            raise ValueError("thesis_risk_exposure_set_kind_counts_invalid")
        _sha256(
            self.receipt_sha256,
            field_name="thesis_risk_exposure_set_receipt_sha256",
        )
        if self.receipt_sha256 != self.compute_receipt_sha256(
            exposure_set_id=self.exposure_set_id,
            decision_time=self.decision_time,
            as_of=self.as_of,
            available_at=self.available_at,
            source_id=self.source_id,
            source_generation=self.source_generation,
            source_lineage_sha256=self.source_lineage_sha256,
            exposure_receipt_sha256s=self.exposure_receipt_sha256s,
            candidate_count=self.candidate_count,
            position_count=self.position_count,
            pending_count=self.pending_count,
        ):
            raise ValueError("thesis_risk_exposure_set_hash_mismatch")

    @staticmethod
    def compute_receipt_sha256(**values: object) -> str:
        payload = dict(values)
        for field_name in ("decision_time", "as_of", "available_at"):
            value = payload[field_name]
            if not isinstance(value, datetime):
                raise ValueError(f"thesis_risk_exposure_set_{field_name}_invalid")
            payload[field_name] = value.astimezone(timezone.utc).isoformat()
        payload["exposure_receipt_sha256s"] = list(payload["exposure_receipt_sha256s"])
        return _canonical_sha256(payload)

    @classmethod
    def create(
        cls,
        *,
        exposure_set_id: str,
        receipts: Iterable[ThesisRiskExposureReceipt],
        decision_time: datetime,
        as_of: datetime,
        available_at: datetime,
        source_id: str,
        source_generation: int,
        source_lineage_sha256: str,
    ) -> ThesisRiskExposureSetReceipt:
        frozen = tuple(receipts)
        if any(not isinstance(row, ThesisRiskExposureReceipt) for row in frozen):
            raise ValueError("thesis_risk_exposure_receipt_invalid")
        values = {
            "exposure_set_id": exposure_set_id,
            "decision_time": decision_time,
            "as_of": as_of,
            "available_at": available_at,
            "source_id": source_id,
            "source_generation": source_generation,
            "source_lineage_sha256": source_lineage_sha256,
            "exposure_receipt_sha256s": tuple(
                sorted(row.receipt_sha256 for row in frozen)
            ),
            "candidate_count": sum(row.exposure_kind == "candidate" for row in frozen),
            "position_count": sum(row.exposure_kind == "position" for row in frozen),
            "pending_count": sum(row.exposure_kind == "pending" for row in frozen),
        }
        return cls(
            **values,
            receipt_sha256=cls.compute_receipt_sha256(**values),
        )


@dataclass(frozen=True)
class ThesisRiskExposureSetVerification:
    verifier_id: str
    verifier_version: str
    exposure_set_id: str
    exposure_set_receipt_sha256: str
    source_generation: int
    source_lineage_sha256: str
    verified_at: datetime
    valid_until: datetime
    promotion_eligible: bool
    proof_sha256: str

    def __post_init__(self) -> None:
        for field_name, value in (
            ("verifier_id", self.verifier_id),
            ("verifier_version", self.verifier_version),
            ("exposure_set_id", self.exposure_set_id),
        ):
            _nonempty(value, field_name=f"thesis_risk_set_proof_{field_name}")
        for field_name, value in (
            ("exposure_set_receipt_sha256", self.exposure_set_receipt_sha256),
            ("source_lineage_sha256", self.source_lineage_sha256),
            ("proof_sha256", self.proof_sha256),
        ):
            _sha256(value, field_name=f"thesis_risk_set_proof_{field_name}")
        if (
            isinstance(self.source_generation, bool)
            or not isinstance(self.source_generation, int)
            or self.source_generation < 1
        ):
            raise ValueError("thesis_risk_set_proof_generation_invalid")
        verified_at = _aware(
            self.verified_at,
            field_name="thesis_risk_set_proof_verified_at",
        )
        valid_until = _aware(
            self.valid_until,
            field_name="thesis_risk_set_proof_valid_until",
        )
        if valid_until < verified_at:
            raise ValueError("thesis_risk_set_proof_validity_invalid")
        if type(self.promotion_eligible) is not bool:
            raise ValueError("thesis_risk_set_proof_promotion_invalid")
        if self.proof_sha256 != _canonical_sha256(self._proof_payload()):
            raise ValueError("thesis_risk_exposure_set_proof_hash_mismatch")

    def _proof_payload(self) -> dict[str, object]:
        return {
            "verifier_id": self.verifier_id,
            "verifier_version": self.verifier_version,
            "exposure_set_id": self.exposure_set_id,
            "exposure_set_receipt_sha256": self.exposure_set_receipt_sha256,
            "source_generation": self.source_generation,
            "source_lineage_sha256": self.source_lineage_sha256,
            "verified_at": self.verified_at.astimezone(timezone.utc).isoformat(),
            "valid_until": self.valid_until.astimezone(timezone.utc).isoformat(),
            "promotion_eligible": self.promotion_eligible,
        }

    @classmethod
    def create(
        cls,
        *,
        receipt: ThesisRiskExposureSetReceipt,
        verifier_id: str,
        verifier_version: str,
        verified_at: datetime,
        valid_until: datetime,
        promotion_eligible: bool,
    ) -> ThesisRiskExposureSetVerification:
        values = {
            "verifier_id": verifier_id,
            "verifier_version": verifier_version,
            "exposure_set_id": receipt.exposure_set_id,
            "exposure_set_receipt_sha256": receipt.receipt_sha256,
            "source_generation": receipt.source_generation,
            "source_lineage_sha256": receipt.source_lineage_sha256,
            "verified_at": verified_at,
            "valid_until": valid_until,
            "promotion_eligible": promotion_eligible,
        }
        payload = {
            **values,
            "verified_at": verified_at.astimezone(timezone.utc).isoformat(),
            "valid_until": valid_until.astimezone(timezone.utc).isoformat(),
        }
        return cls(**values, proof_sha256=_canonical_sha256(payload))


class ThesisRiskExposureSetVerifier(Protocol):
    def verify(
        self,
        receipt: ThesisRiskExposureSetReceipt,
        *,
        decision_time: datetime,
    ) -> ThesisRiskExposureSetVerification: ...


def verify_thesis_risk_exposure_set(
    *,
    receipt: ThesisRiskExposureSetReceipt,
    receipts: Iterable[ThesisRiskExposureReceipt],
    verifier: ThesisRiskExposureSetVerifier,
    decision_time: datetime,
) -> ThesisRiskExposureSetVerification:
    if not isinstance(receipt, ThesisRiskExposureSetReceipt):
        raise ValueError("thesis_risk_exposure_set_receipt_required")
    resolved_decision_time = _aware(
        decision_time,
        field_name="thesis_risk_decision_time",
    )
    frozen = tuple(receipts)
    if any(not isinstance(row, ThesisRiskExposureReceipt) for row in frozen):
        raise ValueError("thesis_risk_exposure_set_membership_mismatch")
    supplied_hashes = tuple(sorted(row.receipt_sha256 for row in frozen))
    supplied_counts = {
        kind: sum(row.exposure_kind == kind for row in frozen)
        for kind in _EXPOSURE_KINDS
    }
    if (
        supplied_hashes != receipt.exposure_receipt_sha256s
        or supplied_counts["candidate"] != receipt.candidate_count
        or supplied_counts["position"] != receipt.position_count
        or supplied_counts["pending"] != receipt.pending_count
    ):
        raise ValueError("thesis_risk_exposure_set_membership_mismatch")
    if frozen and (
        receipt.as_of < max(row.as_of for row in frozen)
        or receipt.available_at < max(row.available_at for row in frozen)
    ):
        raise ValueError("thesis_risk_exposure_set_time_invalid")
    if (
        receipt.decision_time != resolved_decision_time
        or receipt.available_at > resolved_decision_time
    ):
        raise ValueError("thesis_risk_exposure_set_time_invalid")
    verify = getattr(verifier, "verify", None)
    if not callable(verify):
        raise ValueError("thesis_risk_exposure_set_verifier_required")
    try:
        proof = verify(receipt, decision_time=resolved_decision_time)
    except Exception as exc:
        raise ValueError("thesis_risk_exposure_set_verification_failed") from exc
    if not isinstance(proof, ThesisRiskExposureSetVerification):
        raise ValueError("thesis_risk_exposure_set_verification_failed")
    expected = {
        "exposure_set_id": receipt.exposure_set_id,
        "exposure_set_receipt_sha256": receipt.receipt_sha256,
        "source_generation": receipt.source_generation,
        "source_lineage_sha256": receipt.source_lineage_sha256,
    }
    if any(getattr(proof, key) != value for key, value in expected.items()):
        raise ValueError("thesis_risk_exposure_set_proof_binding_mismatch")
    if proof.verified_at > resolved_decision_time:
        raise ValueError("thesis_risk_exposure_set_proof_after_decision")
    if proof.valid_until < resolved_decision_time:
        raise ValueError("thesis_risk_exposure_set_proof_expired")
    return proof


def _runtime_authority_content_payload(
    *,
    decision_time: datetime,
    policy: ThesisRiskPolicy,
    policy_proof: ThesisRiskPolicyVerification,
    exposure_receipts: tuple[ThesisRiskExposureReceipt, ...],
    exposure_proofs: tuple[ThesisRiskExposureVerification, ...],
    exposure_set_receipt: ThesisRiskExposureSetReceipt,
    exposure_set_proof: ThesisRiskExposureSetVerification,
    initial_exposures: tuple[tuple[str, str, float], ...],
) -> dict[str, object]:
    return {
        "decision_time": decision_time.astimezone(timezone.utc).isoformat(),
        "policy_id": policy.policy_id,
        "policy_sha256": policy.policy_sha256,
        "policy_proof_sha256": policy_proof.proof_sha256,
        "exposure_receipt_sha256s": [row.receipt_sha256 for row in exposure_receipts],
        "exposure_proof_sha256s": sorted(row.proof_sha256 for row in exposure_proofs),
        "exposure_set_id": exposure_set_receipt.exposure_set_id,
        "exposure_set_sha256": exposure_set_receipt.receipt_sha256,
        "exposure_set_proof_sha256": exposure_set_proof.proof_sha256,
        "initial_group_exposures": [
            {
                "dimension": dimension,
                "group_id": group_id,
                "exposure_cny": exposure,
            }
            for dimension, group_id, exposure in initial_exposures
        ],
    }


@dataclass(frozen=True)
class ThesisRiskRuntimeAuthority:
    """Verified immutable authority shared by optimizer, stage, and day-loop."""

    decision_time: datetime
    policy: ThesisRiskPolicy
    policy_proof: ThesisRiskPolicyVerification
    exposure_receipts: tuple[ThesisRiskExposureReceipt, ...]
    exposure_proofs: tuple[ThesisRiskExposureVerification, ...]
    exposure_set_receipt: ThesisRiskExposureSetReceipt
    exposure_set_proof: ThesisRiskExposureSetVerification
    initial_group_exposures: tuple[tuple[str, str, float], ...]
    authority_sha256: str

    def __post_init__(self) -> None:
        decision_time = _aware(
            self.decision_time,
            field_name="thesis_risk_authority_decision_time",
        )
        if not isinstance(self.policy, ThesisRiskPolicy) or not isinstance(
            self.policy_proof,
            ThesisRiskPolicyVerification,
        ):
            raise ValueError("thesis_risk_authority_policy_invalid")
        if (
            self.policy_proof.policy_id != self.policy.policy_id
            or self.policy_proof.policy_sha256 != self.policy.policy_sha256
            or not (
                self.policy.effective_at <= decision_time <= self.policy.valid_until
            )
            or not (
                self.policy_proof.verified_at
                <= decision_time
                <= self.policy_proof.valid_until
            )
        ):
            raise ValueError("thesis_risk_authority_policy_binding_invalid")
        if (
            not isinstance(self.exposure_receipts, tuple)
            or any(
                not isinstance(row, ThesisRiskExposureReceipt)
                for row in self.exposure_receipts
            )
            or tuple(sorted(self.exposure_receipts, key=lambda row: row.receipt_sha256))
            != self.exposure_receipts
        ):
            raise ValueError("thesis_risk_authority_receipts_invalid")
        if (
            not isinstance(self.exposure_proofs, tuple)
            or any(
                not isinstance(row, ThesisRiskExposureVerification)
                for row in self.exposure_proofs
            )
            or {row.exposure_receipt_sha256 for row in self.exposure_proofs}
            != {row.receipt_sha256 for row in self.exposure_receipts}
            or len(self.exposure_proofs) != len(self.exposure_receipts)
            or len({row.exposure_receipt_sha256 for row in self.exposure_proofs})
            != len(self.exposure_proofs)
        ):
            raise ValueError("thesis_risk_authority_exposure_proofs_invalid")
        receipt_by_hash = {row.receipt_sha256: row for row in self.exposure_receipts}
        groups_by_symbol: dict[str, tuple[ThesisRiskGroups, str]] = {}
        for receipt in self.exposure_receipts:
            existing_groups, existing_kind = groups_by_symbol.setdefault(
                receipt.symbol,
                (receipt.groups, receipt.exposure_kind),
            )
            if existing_groups != receipt.groups:
                kinds = {existing_kind, receipt.exposure_kind}
                if "pending" in kinds:
                    raise ValueError("pending_thesis_risk_groups_mismatch")
                if kinds == {"candidate", "position"}:
                    raise ValueError("candidate_position_thesis_risk_groups_mismatch")
                raise ValueError("thesis_risk_authority_symbol_groups_invalid")
        for proof in self.exposure_proofs:
            receipt = receipt_by_hash[proof.exposure_receipt_sha256]
            if (
                proof.exposure_id != receipt.exposure_id
                or proof.authority_notional_cny != receipt.notional_cny
                or proof.authority_binding_reference_id != receipt.binding_reference_id
                or proof.authority_binding_sha256 != receipt.binding_sha256
                or not (proof.verified_at <= decision_time <= proof.valid_until)
            ):
                raise ValueError("thesis_risk_authority_exposure_proofs_invalid")
        if (
            not isinstance(self.exposure_set_receipt, ThesisRiskExposureSetReceipt)
            or not isinstance(
                self.exposure_set_proof,
                ThesisRiskExposureSetVerification,
            )
            or self.exposure_set_receipt.exposure_receipt_sha256s
            != tuple(row.receipt_sha256 for row in self.exposure_receipts)
            or self.exposure_set_proof.exposure_set_receipt_sha256
            != self.exposure_set_receipt.receipt_sha256
            or self.exposure_set_receipt.decision_time != decision_time
            or self.exposure_set_receipt.available_at > decision_time
            or not (
                self.exposure_set_proof.verified_at
                <= decision_time
                <= self.exposure_set_proof.valid_until
            )
            or (
                bool(self.exposure_receipts)
                and (
                    self.exposure_set_receipt.as_of
                    < max(row.as_of for row in self.exposure_receipts)
                    or self.exposure_set_receipt.available_at
                    < max(row.available_at for row in self.exposure_receipts)
                )
            )
        ):
            raise ValueError("thesis_risk_authority_set_binding_invalid")
        expected_initial = tuple(
            (dimension, group_id, round(exposure, 6))
            for (dimension, group_id), exposure in sorted(
                initial_group_exposures(self.exposure_receipts).items()
            )
        )
        if self.initial_group_exposures != expected_initial:
            raise ValueError("thesis_risk_authority_initial_exposure_invalid")
        _sha256(
            self.authority_sha256,
            field_name="thesis_risk_runtime_authority_sha256",
        )
        if self.authority_sha256 != _canonical_sha256(self._content_payload()):
            raise ValueError("thesis_risk_runtime_authority_hash_mismatch")

    def _content_payload(self) -> dict[str, object]:
        return _runtime_authority_content_payload(
            decision_time=self.decision_time,
            policy=self.policy,
            policy_proof=self.policy_proof,
            exposure_receipts=self.exposure_receipts,
            exposure_proofs=self.exposure_proofs,
            exposure_set_receipt=self.exposure_set_receipt,
            exposure_set_proof=self.exposure_set_proof,
            initial_exposures=self.initial_group_exposures,
        )


def build_thesis_risk_runtime_authority(
    *,
    policy: ThesisRiskPolicy,
    policy_verifier: ThesisRiskPolicyVerifier,
    exposure_receipts: Iterable[ThesisRiskExposureReceipt],
    exposure_verifier: ThesisRiskExposureVerifier,
    exposure_set_receipt: ThesisRiskExposureSetReceipt,
    exposure_set_verifier: ThesisRiskExposureSetVerifier,
    decision_time: datetime,
) -> ThesisRiskRuntimeAuthority:
    resolved_decision_time = _aware(
        decision_time,
        field_name="thesis_risk_decision_time",
    )
    policy_proof = verify_thesis_risk_policy(
        policy=policy,
        verifier=policy_verifier,
        decision_time=resolved_decision_time,
    )
    receipts = tuple(sorted(exposure_receipts, key=lambda row: row.receipt_sha256))
    proofs = tuple(
        verify_thesis_risk_exposure(
            receipt=receipt,
            verifier=exposure_verifier,
            decision_time=resolved_decision_time,
        )
        for receipt in receipts
    )
    set_proof = verify_thesis_risk_exposure_set(
        receipt=exposure_set_receipt,
        receipts=receipts,
        verifier=exposure_set_verifier,
        decision_time=resolved_decision_time,
    )
    initial = tuple(
        (dimension, group_id, round(exposure, 6))
        for (dimension, group_id), exposure in sorted(
            initial_group_exposures(receipts).items()
        )
    )
    values = {
        "decision_time": resolved_decision_time,
        "policy": policy,
        "policy_proof": policy_proof,
        "exposure_receipts": receipts,
        "exposure_proofs": proofs,
        "exposure_set_receipt": exposure_set_receipt,
        "exposure_set_proof": set_proof,
        "initial_group_exposures": initial,
    }
    return ThesisRiskRuntimeAuthority(
        **values,
        authority_sha256=_canonical_sha256(
            _runtime_authority_content_payload(
                decision_time=resolved_decision_time,
                policy=policy,
                policy_proof=policy_proof,
                exposure_receipts=receipts,
                exposure_proofs=proofs,
                exposure_set_receipt=exposure_set_receipt,
                exposure_set_proof=set_proof,
                initial_exposures=initial,
            )
        ),
    )


@dataclass(frozen=True)
class ThesisRiskGroupEffect:
    dimension: str
    group_id: str
    pre_exposure_cny: float
    requested_delta_cny: float
    requested_post_exposure_cny: float
    delta_cny: float
    post_exposure_cny: float
    cap_cny: float
    policy_proof_sha256: str

    def __post_init__(self) -> None:
        if self.dimension not in _THESIS_RISK_DIMENSION_SET:
            raise ValueError("thesis_risk_effect_dimension_invalid")
        _nonempty(self.group_id, field_name="thesis_risk_effect_group_id")
        for field_name, value in (
            ("pre_exposure_cny", self.pre_exposure_cny),
            ("requested_post_exposure_cny", self.requested_post_exposure_cny),
            ("post_exposure_cny", self.post_exposure_cny),
            ("cap_cny", self.cap_cny),
        ):
            _money(value, field_name=f"thesis_risk_effect_{field_name}")
        for field_name, value in (
            ("requested_delta_cny", self.requested_delta_cny),
            ("delta_cny", self.delta_cny),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                raise ValueError(f"thesis_risk_effect_{field_name}_invalid")
        if self.cap_cny <= 0.0:
            raise ValueError("thesis_risk_effect_cap_cny_invalid")
        if (
            abs(
                self.requested_post_exposure_cny
                - max(0.0, self.pre_exposure_cny + self.requested_delta_cny)
            )
            > 1e-6
            or abs(
                self.post_exposure_cny
                - max(0.0, self.pre_exposure_cny + self.delta_cny)
            )
            > 1e-6
        ):
            raise ValueError("thesis_risk_effect_exposure_math_invalid")
        _sha256(
            self.policy_proof_sha256,
            field_name="thesis_risk_effect_policy_proof_sha256",
        )


def initial_group_exposures(
    receipts: Iterable[ThesisRiskExposureReceipt],
) -> dict[tuple[str, str], float]:
    result: dict[tuple[str, str], float] = {}
    for receipt in receipts:
        if receipt.exposure_kind not in {"position", "pending"}:
            continue
        for dimension, group_id in receipt.groups.items():
            key = (dimension, group_id)
            result[key] = round(result.get(key, 0.0) + receipt.notional_cny, 6)
    return result


def apply_group_delta(
    *,
    exposures: dict[tuple[str, str], float],
    groups: ThesisRiskGroups,
    requested_delta_cny: float,
    policy: ThesisRiskPolicy,
    policy_proof_sha256: str,
    enforce_cap: bool,
) -> tuple[tuple[ThesisRiskGroupEffect, ...], bool]:
    _sha256(
        policy_proof_sha256,
        field_name="thesis_risk_policy_proof_sha256",
    )
    requested = float(requested_delta_cny)
    if not math.isfinite(requested):
        raise ValueError("thesis_risk_requested_delta_invalid")
    requested_rows: list[tuple[str, str, float, float, float]] = []
    cap_exceeded = False
    for dimension, group_id in groups.items():
        pre = float(exposures.get((dimension, group_id), 0.0))
        requested_post = max(0.0, round(pre + requested, 6))
        cap = policy.cap_for(dimension)
        if enforce_cap and requested > 0 and requested_post > cap + 1e-9:
            cap_exceeded = True
        requested_rows.append((dimension, group_id, pre, requested_post, cap))

    applied_delta = 0.0 if cap_exceeded else requested
    effects: list[ThesisRiskGroupEffect] = []
    for dimension, group_id, pre, requested_post, cap in requested_rows:
        post = max(0.0, round(pre + applied_delta, 6))
        exposures[(dimension, group_id)] = post
        effects.append(
            ThesisRiskGroupEffect(
                dimension=dimension,
                group_id=group_id,
                pre_exposure_cny=pre,
                requested_delta_cny=requested,
                requested_post_exposure_cny=requested_post,
                delta_cny=applied_delta,
                post_exposure_cny=post,
                cap_cny=cap,
                policy_proof_sha256=policy_proof_sha256,
            )
        )
    return tuple(effects), cap_exceeded


__all__ = [
    "THESIS_RISK_DIMENSIONS",
    "ThesisRiskDimensionCap",
    "ThesisRiskExposureReceipt",
    "ThesisRiskExposureSetReceipt",
    "ThesisRiskExposureSetVerification",
    "ThesisRiskExposureSetVerifier",
    "ThesisRiskExposureVerification",
    "ThesisRiskExposureVerifier",
    "ThesisRiskGroupEffect",
    "ThesisRiskGroups",
    "ThesisRiskPolicy",
    "ThesisRiskPolicyVerification",
    "ThesisRiskPolicyVerifier",
    "ThesisRiskRuntimeAuthority",
    "apply_group_delta",
    "build_thesis_risk_runtime_authority",
    "initial_group_exposures",
    "verify_thesis_risk_exposure",
    "verify_thesis_risk_exposure_set",
    "verify_thesis_risk_policy",
]
