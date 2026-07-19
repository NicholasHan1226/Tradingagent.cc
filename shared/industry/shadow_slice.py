"""Point-in-time, shadow-only selection of a deliberately thin industry slice.

This module does not emit securities, scores for position sizing, or trading
actions.  It selects one deep-research industry and two watch industries from
coverage-qualified aggregate inputs so a small account can focus research
without turning an unvalidated industry narrative into portfolio authority.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Protocol, Tuple


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class IndustryShadowContractError(ValueError):
    """Raised when the shadow research slice cannot be proven point-in-time."""


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise IndustryShadowContractError(f"{field_name}_invalid")
    return value


def _aware(value: object, field_name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise IndustryShadowContractError(f"{field_name}_must_be_timezone_aware")
    return value.astimezone(timezone.utc)


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class IndustryScoreAuthorityVerification:
    """Detached proof that a score and its coverage receipt were authorized."""

    accepted: bool
    verifier_id: str
    proof_sha256: str
    verified_at: datetime
    decision_time: datetime
    industry_id: str
    score_content_sha256: str
    score_receipt_id: str
    score_receipt_sha256: str
    coverage_authority_receipt_id: str
    coverage_authority_receipt_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.accepted, bool):
            raise IndustryShadowContractError("score_authority_acceptance_invalid")
        for field_name in (
            "verifier_id",
            "industry_id",
            "score_receipt_id",
            "coverage_authority_receipt_id",
        ):
            _text(getattr(self, field_name), field_name)
        for field_name in (
            "proof_sha256",
            "score_content_sha256",
            "score_receipt_sha256",
            "coverage_authority_receipt_sha256",
        ):
            if not _SHA256.fullmatch(getattr(self, field_name)):
                raise IndustryShadowContractError(f"{field_name}_invalid")
        verified_at = _aware(self.verified_at, "verified_at")
        decision_time = _aware(self.decision_time, "decision_time")
        object.__setattr__(self, "verified_at", verified_at)
        object.__setattr__(self, "decision_time", decision_time)
        if verified_at > decision_time:
            raise IndustryShadowContractError(
                "score_authority_verification_from_future"
            )

    def canonical_payload(self) -> dict:
        return {
            "accepted": self.accepted,
            "coverage_authority_receipt_id": (self.coverage_authority_receipt_id),
            "coverage_authority_receipt_sha256": (
                self.coverage_authority_receipt_sha256
            ),
            "decision_time": self.decision_time.isoformat(),
            "industry_id": self.industry_id,
            "proof_sha256": self.proof_sha256,
            "score_content_sha256": self.score_content_sha256,
            "score_receipt_id": self.score_receipt_id,
            "score_receipt_sha256": self.score_receipt_sha256,
            "verified_at": self.verified_at.isoformat(),
            "verifier_id": self.verifier_id,
        }


class IndustryScoreAuthorityVerifier(Protocol):
    """Port for an authority that is independent of the caller's score row."""

    verifier_id: str

    def verify(
        self,
        item: "IndustryShadowInput",
        *,
        decision_time: datetime,
    ) -> IndustryScoreAuthorityVerification:
        """Verify exact score, receipt, coverage receipt, and decision time."""


@dataclass(frozen=True)
class IndustryShadowInput:
    """Aggregate industry observation with no constituent security payload."""

    industry_id: str
    taxonomy_id: str
    taxonomy_version: str
    membership_snapshot_sha256: str
    membership_effective_at: datetime
    membership_available_at: datetime
    expected_member_count: int
    observed_member_count: int
    activity_score: float
    score_method_id: str
    score_method_version: str
    score_observed_at: datetime
    score_available_at: datetime
    score_valid_until: datetime
    score_receipt_id: str
    score_receipt_sha256: str
    coverage_authority_receipt_id: str
    coverage_authority_receipt_sha256: str
    evidence_receipt_ids: Tuple[str, ...]
    source_generation: int
    score_content_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        for field_name in ("industry_id", "taxonomy_id", "taxonomy_version"):
            _text(getattr(self, field_name), field_name)
        if not _SHA256.fullmatch(self.membership_snapshot_sha256):
            raise IndustryShadowContractError("membership_snapshot_sha256_invalid")
        effective = _aware(
            self.membership_effective_at,
            "membership_effective_at",
        )
        available = _aware(
            self.membership_available_at,
            "membership_available_at",
        )
        object.__setattr__(self, "membership_effective_at", effective)
        object.__setattr__(self, "membership_available_at", available)
        if effective > available:
            raise IndustryShadowContractError("membership_time_order_invalid")
        if (
            isinstance(self.expected_member_count, bool)
            or not isinstance(self.expected_member_count, int)
            or self.expected_member_count <= 0
        ):
            raise IndustryShadowContractError("expected_member_count_invalid")
        if (
            isinstance(self.observed_member_count, bool)
            or not isinstance(self.observed_member_count, int)
            or self.observed_member_count < 0
            or self.observed_member_count > self.expected_member_count
        ):
            raise IndustryShadowContractError("observed_member_count_invalid")
        if (
            isinstance(self.activity_score, bool)
            or not isinstance(self.activity_score, (int, float))
            or not math.isfinite(float(self.activity_score))
            or not 0.0 <= float(self.activity_score) <= 1.0
        ):
            raise IndustryShadowContractError("activity_score_invalid")
        for field_name in (
            "score_method_id",
            "score_method_version",
            "score_receipt_id",
            "coverage_authority_receipt_id",
        ):
            _text(getattr(self, field_name), field_name)
        for field_name in (
            "score_receipt_sha256",
            "coverage_authority_receipt_sha256",
        ):
            if not _SHA256.fullmatch(getattr(self, field_name)):
                raise IndustryShadowContractError(f"{field_name}_invalid")
        score_observed_at = _aware(self.score_observed_at, "score_observed_at")
        score_available_at = _aware(
            self.score_available_at,
            "score_available_at",
        )
        score_valid_until = _aware(self.score_valid_until, "score_valid_until")
        object.__setattr__(self, "score_observed_at", score_observed_at)
        object.__setattr__(self, "score_available_at", score_available_at)
        object.__setattr__(self, "score_valid_until", score_valid_until)
        if not score_observed_at <= score_available_at < score_valid_until:
            raise IndustryShadowContractError("activity_score_time_order_invalid")
        if available > score_available_at:
            raise IndustryShadowContractError("coverage_available_after_activity_score")
        receipts = tuple(
            _text(item, "evidence_receipt_id") for item in self.evidence_receipt_ids
        )
        if not receipts or len(receipts) != len(set(receipts)):
            raise IndustryShadowContractError("evidence_receipt_ids_invalid")
        object.__setattr__(self, "evidence_receipt_ids", receipts)
        if (
            isinstance(self.source_generation, bool)
            or not isinstance(self.source_generation, int)
            or self.source_generation <= 0
        ):
            raise IndustryShadowContractError("source_generation_invalid")
        object.__setattr__(
            self,
            "score_content_sha256",
            _canonical_sha256(self._score_content_payload()),
        )

    @property
    def coverage_ratio(self) -> float:
        return self.observed_member_count / self.expected_member_count

    def canonical_payload(self) -> dict:
        return {
            "activity_score": float(self.activity_score),
            "coverage_authority_receipt_id": (self.coverage_authority_receipt_id),
            "coverage_authority_receipt_sha256": (
                self.coverage_authority_receipt_sha256
            ),
            "evidence_receipt_ids": list(self.evidence_receipt_ids),
            "expected_member_count": self.expected_member_count,
            "industry_id": self.industry_id,
            "membership_available_at": self.membership_available_at.isoformat(),
            "membership_effective_at": self.membership_effective_at.isoformat(),
            "membership_snapshot_sha256": self.membership_snapshot_sha256,
            "observed_member_count": self.observed_member_count,
            "score_available_at": self.score_available_at.isoformat(),
            "score_content_sha256": self.score_content_sha256,
            "score_method_id": self.score_method_id,
            "score_method_version": self.score_method_version,
            "score_observed_at": self.score_observed_at.isoformat(),
            "score_receipt_id": self.score_receipt_id,
            "score_receipt_sha256": self.score_receipt_sha256,
            "score_valid_until": self.score_valid_until.isoformat(),
            "source_generation": self.source_generation,
            "taxonomy_id": self.taxonomy_id,
            "taxonomy_version": self.taxonomy_version,
        }

    def _score_content_payload(self) -> dict:
        """Canonical score receipt content; authority is proven separately."""

        return {
            "activity_score": float(self.activity_score),
            "coverage_authority_receipt_id": (self.coverage_authority_receipt_id),
            "coverage_authority_receipt_sha256": (
                self.coverage_authority_receipt_sha256
            ),
            "evidence_receipt_ids": list(self.evidence_receipt_ids),
            "expected_member_count": self.expected_member_count,
            "industry_id": self.industry_id,
            "membership_available_at": self.membership_available_at.isoformat(),
            "membership_effective_at": self.membership_effective_at.isoformat(),
            "membership_snapshot_sha256": self.membership_snapshot_sha256,
            "observed_member_count": self.observed_member_count,
            "score_available_at": self.score_available_at.isoformat(),
            "score_method_id": self.score_method_id,
            "score_method_version": self.score_method_version,
            "score_observed_at": self.score_observed_at.isoformat(),
            "score_receipt_id": self.score_receipt_id,
            "score_receipt_sha256": self.score_receipt_sha256,
            "score_valid_until": self.score_valid_until.isoformat(),
            "source_generation": self.source_generation,
            "taxonomy_id": self.taxonomy_id,
            "taxonomy_version": self.taxonomy_version,
        }


@dataclass(frozen=True)
class IndustryShadowBasket:
    """Content-addressed Phase 1.5 research basket with no position authority."""

    decision_time: datetime
    taxonomy_id: str
    taxonomy_version: str
    minimum_coverage_ratio: float
    deep_research_industry_id: str
    watch_industry_ids: Tuple[str, str]
    selected_inputs: Tuple[IndustryShadowInput, ...]
    authority_proofs: Tuple[IndustryScoreAuthorityVerification, ...]
    basket_sha256: str = field(init=False)
    schema_version: str = "tradingagent.industry_shadow_basket.v2"
    shadow_only: bool = True
    context_only: bool = True
    position_effect_allowed: bool = False
    promotion_eligible: bool = False

    def __post_init__(self) -> None:
        decision_time = _aware(self.decision_time, "decision_time")
        object.__setattr__(self, "decision_time", decision_time)
        if (
            self.shadow_only is not True
            or self.context_only is not True
            or self.position_effect_allowed is not False
            or self.promotion_eligible is not False
        ):
            raise IndustryShadowContractError("shadow_only_boundary_invalid")
        selected_ids = tuple(item.industry_id for item in self.selected_inputs)
        if len(self.selected_inputs) != 3 or len(set(selected_ids)) != 3:
            raise IndustryShadowContractError("selected_industry_set_invalid")
        if selected_ids != (
            self.deep_research_industry_id,
            *self.watch_industry_ids,
        ):
            raise IndustryShadowContractError("selected_industry_order_invalid")
        if len(self.authority_proofs) != len(self.selected_inputs):
            raise IndustryShadowContractError("score_authority_proof_set_invalid")
        for item, proof in zip(self.selected_inputs, self.authority_proofs):
            _validate_score_authority_binding(
                item,
                proof,
                decision_time=decision_time,
            )
        payload = {
            "authority_proofs": [
                proof.canonical_payload() for proof in self.authority_proofs
            ],
            "context_only": True,
            "decision_time": decision_time.isoformat(),
            "deep_research_industry_id": self.deep_research_industry_id,
            "minimum_coverage_ratio": self.minimum_coverage_ratio,
            "position_effect_allowed": False,
            "promotion_eligible": False,
            "schema_version": self.schema_version,
            "selected_inputs": [
                item.canonical_payload() for item in self.selected_inputs
            ],
            "shadow_only": True,
            "taxonomy_id": self.taxonomy_id,
            "taxonomy_version": self.taxonomy_version,
            "watch_industry_ids": list(self.watch_industry_ids),
        }
        object.__setattr__(self, "basket_sha256", _canonical_sha256(payload))

    def to_shadow_payload(self) -> dict:
        """Return a detached research payload that contains no security list."""

        return {
            "basket_sha256": self.basket_sha256,
            "context_only": True,
            "decision_time": self.decision_time.isoformat(),
            "deep_research_industry_id": self.deep_research_industry_id,
            "position_effect_allowed": False,
            "promotion_eligible": False,
            "schema_version": self.schema_version,
            "score_authority_proof_sha256s": [
                proof.proof_sha256 for proof in self.authority_proofs
            ],
            "shadow_only": True,
            "taxonomy_id": self.taxonomy_id,
            "taxonomy_version": self.taxonomy_version,
            "watch_industry_ids": list(self.watch_industry_ids),
        }


def _validate_score_authority_binding(
    item: IndustryShadowInput,
    proof: IndustryScoreAuthorityVerification,
    *,
    decision_time: datetime,
    verifier_id: str | None = None,
) -> None:
    if not isinstance(proof, IndustryScoreAuthorityVerification):
        raise IndustryShadowContractError("score_authority_proof_invalid")
    if verifier_id is not None and proof.verifier_id != verifier_id:
        raise IndustryShadowContractError("score_authority_verifier_mismatch")
    expected = (
        item.industry_id,
        item.score_content_sha256,
        item.score_receipt_id,
        item.score_receipt_sha256,
        item.coverage_authority_receipt_id,
        item.coverage_authority_receipt_sha256,
        decision_time,
    )
    actual = (
        proof.industry_id,
        proof.score_content_sha256,
        proof.score_receipt_id,
        proof.score_receipt_sha256,
        proof.coverage_authority_receipt_id,
        proof.coverage_authority_receipt_sha256,
        proof.decision_time,
    )
    if actual != expected:
        raise IndustryShadowContractError("score_authority_binding_mismatch")
    if proof.accepted is not True:
        raise IndustryShadowContractError("score_authority_rejected")


def build_industry_shadow_basket(
    candidates: Iterable[IndustryShadowInput],
    *,
    decision_time: datetime,
    score_authority_verifier: IndustryScoreAuthorityVerifier | None,
    minimum_coverage_ratio: float = 0.95,
) -> IndustryShadowBasket:
    """Select one deep and two watch industries from qualified aggregates."""

    instant = _aware(decision_time, "decision_time")
    if score_authority_verifier is None:
        raise IndustryShadowContractError("score_authority_verifier_required")
    verifier_id = _text(
        getattr(score_authority_verifier, "verifier_id", None),
        "score_authority_verifier_id",
    )
    verify_score = getattr(score_authority_verifier, "verify", None)
    if not callable(verify_score):
        raise IndustryShadowContractError("score_authority_verifier_invalid")
    if (
        isinstance(minimum_coverage_ratio, bool)
        or not isinstance(minimum_coverage_ratio, (int, float))
        or not math.isfinite(float(minimum_coverage_ratio))
        or not 0.0 < float(minimum_coverage_ratio) <= 1.0
    ):
        raise IndustryShadowContractError("minimum_coverage_ratio_invalid")
    rows = tuple(candidates)
    if any(not isinstance(item, IndustryShadowInput) for item in rows):
        raise IndustryShadowContractError("industry_input_invalid")
    if len({item.industry_id for item in rows}) != len(rows):
        raise IndustryShadowContractError("industry_id_duplicate")
    taxonomies = {(item.taxonomy_id, item.taxonomy_version) for item in rows}
    if len(taxonomies) != 1:
        raise IndustryShadowContractError("taxonomy_mismatch")
    if any(item.membership_available_at > instant for item in rows):
        raise IndustryShadowContractError("future_membership_evidence")
    if any(item.score_available_at > instant for item in rows):
        raise IndustryShadowContractError("future_activity_score")
    if any(item.score_valid_until < instant for item in rows):
        raise IndustryShadowContractError("activity_score_expired")
    proofs = []
    for item in rows:
        try:
            proof = verify_score(item, decision_time=instant)
        except IndustryShadowContractError:
            raise
        except Exception as exc:
            raise IndustryShadowContractError(
                "score_authority_verification_failed"
            ) from exc
        _validate_score_authority_binding(
            item,
            proof,
            decision_time=instant,
            verifier_id=verifier_id,
        )
        proofs.append(proof)
    proofs_by_industry = {item.industry_id: proof for item, proof in zip(rows, proofs)}
    eligible = tuple(
        sorted(
            (
                item
                for item in rows
                if item.coverage_ratio >= float(minimum_coverage_ratio)
            ),
            key=lambda item: (-float(item.activity_score), item.industry_id),
        )
    )
    if len(eligible) < 3:
        raise IndustryShadowContractError("insufficient_eligible_industries")
    selected = eligible[:3]
    taxonomy_id, taxonomy_version = next(iter(taxonomies))
    return IndustryShadowBasket(
        decision_time=instant,
        taxonomy_id=taxonomy_id,
        taxonomy_version=taxonomy_version,
        minimum_coverage_ratio=float(minimum_coverage_ratio),
        deep_research_industry_id=selected[0].industry_id,
        watch_industry_ids=(selected[1].industry_id, selected[2].industry_id),
        selected_inputs=selected,
        authority_proofs=tuple(
            proofs_by_industry[item.industry_id] for item in selected
        ),
    )


__all__ = [
    "IndustryShadowBasket",
    "IndustryShadowContractError",
    "IndustryShadowInput",
    "IndustryScoreAuthorityVerification",
    "IndustryScoreAuthorityVerifier",
    "build_industry_shadow_basket",
]
