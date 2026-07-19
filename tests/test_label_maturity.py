from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

import shared.review.label_maturity as label_maturity_module
from shared.review.label_maturity import (
    EvidenceUse,
    LabelContractError,
    MarketTruth,
    Paper,
    Shadow,
    UnavailableOracle,
    assess_label_maturity,
)


UTC = timezone.utc
HORIZON_END = datetime(2026, 7, 16, 7, 0, tzinfo=UTC)
AVAILABLE_AT = HORIZON_END + timedelta(minutes=5)
AS_OF = AVAILABLE_AT + timedelta(minutes=1)
DECISION_CUTOFF = HORIZON_END - timedelta(days=5)
PLAN_FROZEN_AT = DECISION_CUTOFF - timedelta(minutes=1)
ADJUSTMENT_TRUTH_VALID_THROUGH = HORIZON_END
ADJUSTMENT_TRUTH_AVAILABLE_AT = HORIZON_END + timedelta(minutes=3)
OOS_PLAN_RECEIPT_ID = "oos-plan-receipt-20260711"
RECEIPT_IDS = (
    "receipt-adjustment-truth-20260716",
    "receipt-bars-20260716",
    "receipt-corporate-actions-20260716",
)


def _plan_receipt_values(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "receipt_id": OOS_PLAN_RECEIPT_ID,
        "registry_id": "ashare-frozen-oos-registry",
        "registry_version": "2026-07-01.v1",
        "validation_plan_id": "ashare-5d-total-return-oos",
        "validation_plan_version": "v1",
        "primary_horizon": "5d",
        "eligible_source_class": "market_truth",
        "frozen_at": PLAN_FROZEN_AT,
        "total_return_definition_version": "ashare-total-return-v1",
        "corporate_action_policy_version": "ashare-corporate-action-v1",
    }
    values.update(overrides)
    return values


def _plan_receipt_payload(values: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "receipt_type": "frozen_oos_validation_plan.v1",
        "receipt_id": values["receipt_id"],
        "registry_id": values["registry_id"],
        "registry_version": values["registry_version"],
        "validation_plan_id": values["validation_plan_id"],
        "validation_plan_version": values["validation_plan_version"],
        "primary_horizon": values["primary_horizon"],
        "eligible_source_class": values["eligible_source_class"],
        "frozen_at": values["frozen_at"].isoformat(),
        "total_return_definition_version": values["total_return_definition_version"],
        "corporate_action_policy_version": values["corporate_action_policy_version"],
    }


def _plan_receipt_sha256(values: dict[str, object] | None = None) -> str:
    encoded = json.dumps(
        _plan_receipt_payload(values or _plan_receipt_values()),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _plan_receipt(**overrides: object) -> object:
    values = _plan_receipt_values(**overrides)
    values["receipt_payload_sha256"] = _plan_receipt_sha256(values)
    return label_maturity_module.FrozenOOSValidationPlanReceipt(**values)


def _market_truth_values(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "label_id": "label-market-1",
        "decision_cluster_id": "cluster-1",
        "horizon": "5d",
        "decision_cutoff": DECISION_CUTOFF,
        "horizon_end": HORIZON_END,
        "available_at": AVAILABLE_AT,
        "value": 0.021,
        "source_receipt_ids": RECEIPT_IDS,
        "source_class": "market_truth",
        "oos_validation_plan_receipt_id": OOS_PLAN_RECEIPT_ID,
        "oos_validation_plan_receipt_sha256": _plan_receipt_sha256(),
        "total_return_definition_version": "ashare-total-return-v1",
        "corporate_action_policy_version": "ashare-corporate-action-v1",
        "adjustment_truth_receipt_id": "receipt-adjustment-truth-20260716",
        "adjustment_truth_payload_sha256": "a" * 64,
        "adjustment_truth_valid_through": ADJUSTMENT_TRUTH_VALID_THROUGH,
        "adjustment_truth_available_at": ADJUSTMENT_TRUTH_AVAILABLE_AT,
    }
    values.update(overrides)
    if "oos_validation_plan_receipt_sha256" not in overrides:
        values["oos_validation_plan_receipt_sha256"] = _plan_receipt_sha256(
            _plan_receipt_values(primary_horizon=values["horizon"])
        )
    return values


def _evidence_payload(values: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "evidence_type": "market_truth_label.v1",
        "label_id": values["label_id"],
        "decision_cluster_id": values["decision_cluster_id"],
        "horizon": values["horizon"],
        "decision_cutoff": values["decision_cutoff"].isoformat(),
        "horizon_end": values["horizon_end"].isoformat(),
        "available_at": values["available_at"].isoformat(),
        "value": float(values["value"]),
        "source_receipt_ids": list(values["source_receipt_ids"]),
        "source_class": values["source_class"],
        "oos_validation_plan_receipt_id": values["oos_validation_plan_receipt_id"],
        "oos_validation_plan_receipt_sha256": values[
            "oos_validation_plan_receipt_sha256"
        ],
        "total_return_definition_version": values["total_return_definition_version"],
        "corporate_action_policy_version": values["corporate_action_policy_version"],
        "adjustment_truth_receipt_id": values["adjustment_truth_receipt_id"],
        "adjustment_truth_payload_sha256": values["adjustment_truth_payload_sha256"],
        "adjustment_truth_valid_through": (
            values["adjustment_truth_valid_through"].isoformat()
            if values["adjustment_truth_valid_through"] is not None
            else None
        ),
        "adjustment_truth_available_at": (
            values["adjustment_truth_available_at"].isoformat()
            if values["adjustment_truth_available_at"] is not None
            else None
        ),
    }


def _evidence_sha256(values: dict[str, object]) -> str:
    encoded = json.dumps(
        _evidence_payload(values),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _market_truth(**overrides: object) -> MarketTruth:
    values = _market_truth_values(**overrides)
    values["evidence_payload_sha256"] = _evidence_sha256(values)
    return MarketTruth(**values)


def _plan_receipt_for(label: MarketTruth, **overrides: object) -> object:
    values: dict[str, object] = {
        "receipt_id": label.oos_validation_plan_receipt_id,
        "primary_horizon": label.horizon,
        "total_return_definition_version": label.total_return_definition_version,
        "corporate_action_policy_version": label.corporate_action_policy_version,
    }
    values.update(overrides)
    return _plan_receipt(**values)


def _authority_proof(
    label: MarketTruth,
    **overrides: object,
) -> object:
    values: dict[str, object] = {
        "proof_id": "proof-market-1",
        "authority_id": "frozen-pit-label-authority",
        "authority_version": "2026-07-16.v1",
        "frozen_at": label.available_at + timedelta(seconds=1),
        "evidence_payload_sha256": label.evidence_payload_sha256,
        "source_receipt_ids": label.source_receipt_ids,
    }
    values.update(overrides)
    return label_maturity_module.FrozenAuthorityProof(**values)


class _FrozenAuthorityVerifier:
    verifier_id = "fixture-frozen-authority-verifier-v1"

    def __init__(
        self,
        *,
        accepted: bool = True,
        expected_proof_id: str = "proof-market-1",
    ) -> None:
        self._accepted = accepted
        self._expected_proof_id = expected_proof_id

    def verify(
        self,
        *,
        proof: object,
        evidence_payload_json: str,
        assessed_as_of: datetime,
    ) -> object:
        del evidence_payload_json
        return label_maturity_module.FrozenAuthorityVerification(
            accepted=self._accepted and proof.proof_id == self._expected_proof_id,
            verifier_id=self.verifier_id,
            proof_id=proof.proof_id,
            authority_id=proof.authority_id,
            authority_version=proof.authority_version,
            evidence_payload_sha256=proof.evidence_payload_sha256,
            verified_at=assessed_as_of,
        )


class _FrozenOOSRegistryVerifier:
    verifier_id = "fixture-frozen-oos-registry-verifier-v1"

    def __init__(
        self,
        *,
        accepted: bool = True,
        expected_receipt_id: str = OOS_PLAN_RECEIPT_ID,
        verified_at: datetime = AS_OF,
    ) -> None:
        self._accepted = accepted
        self._expected_receipt_id = expected_receipt_id
        self._verified_at = verified_at

    def verify(
        self,
        *,
        receipt: object,
        receipt_payload_json: str,
        assessed_as_of: datetime,
    ) -> object:
        del receipt_payload_json, assessed_as_of
        return label_maturity_module.FrozenOOSRegistryVerification(
            accepted=(
                self._accepted and receipt.receipt_id == self._expected_receipt_id
            ),
            verifier_id=self.verifier_id,
            receipt_id=receipt.receipt_id,
            registry_id=receipt.registry_id,
            registry_version=receipt.registry_version,
            receipt_payload_sha256=receipt.receipt_payload_sha256,
            verified_at=self._verified_at,
        )


def test_market_truth_is_the_only_predictive_release_label() -> None:
    label = _market_truth()
    proof = _authority_proof(label)

    assessment = assess_label_maturity(
        label,
        as_of=AS_OF,
        authority_proof=proof,
        authority_verifier=_FrozenAuthorityVerifier(),
        oos_validation_plan_receipt=_plan_receipt_for(label),
        oos_registry_verifier=_FrozenOOSRegistryVerifier(),
    )

    assert label.canonical_evidence_payload() == _evidence_payload(
        _market_truth_values()
    )
    assert label.recompute_evidence_payload_sha256() == label.evidence_payload_sha256
    assert isinstance(assessment, label_maturity_module.LabelMaturityRecord)
    assert assessment.label_class == "market_truth"
    assert assessment.mature is True
    assert assessment.release_evidence_eligible is True
    assert assessment.eligible_uses == (EvidenceUse.PREDICTIVE_VALIDATION,)
    assert assessment.decision_cutoff == DECISION_CUTOFF
    assert assessment.assessed_as_of == AS_OF
    assert assessment.source_receipt_ids == RECEIPT_IDS
    assert assessment.authority_proof == proof
    assert assessment.authority_verifier_id == _FrozenAuthorityVerifier.verifier_id
    assert assessment.authority_verification is not None
    assert assessment.authority_verification.accepted is True
    assert assessment.oos_validation_plan_receipt == _plan_receipt_for(label)
    assert assessment.oos_registry_verifier_id == _FrozenOOSRegistryVerifier.verifier_id
    assert assessment.oos_registry_verification is not None
    assert assessment.oos_registry_verification.accepted is True
    assert assessment.source_class == "market_truth"
    assert assessment.total_return_definition_version == "ashare-total-return-v1"
    assert assessment.corporate_action_policy_version == "ashare-corporate-action-v1"
    assert assessment.adjustment_truth_receipt_id == "receipt-adjustment-truth-20260716"
    assert assessment.evidence_payload_sha256 == _evidence_sha256(
        _market_truth_values()
    )
    assert assessment.canonical_evidence_payload_json == json.dumps(
        _evidence_payload(_market_truth_values()),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    assert (
        assessment.recompute_evidence_payload_sha256()
        == assessment.evidence_payload_sha256
    )


def test_market_truth_without_oos_and_total_return_bindings_fails_closed() -> None:
    label = _market_truth(
        oos_validation_plan_receipt_id=None,
        oos_validation_plan_receipt_sha256=None,
        total_return_definition_version=None,
        corporate_action_policy_version=None,
        adjustment_truth_receipt_id=None,
        adjustment_truth_payload_sha256=None,
        adjustment_truth_valid_through=None,
        adjustment_truth_available_at=None,
    )

    assessment = assess_label_maturity(
        label,
        as_of=AS_OF,
        authority_proof=_authority_proof(label),
        authority_verifier=_FrozenAuthorityVerifier(),
    )

    assert assessment.release_evidence_eligible is False
    assert assessment.eligible_uses == ()
    assert "frozen_oos_validation_plan_receipt_required" in assessment.reasons
    assert "total_return_definition_version_required" in assessment.reasons
    assert "corporate_action_policy_version_required" in assessment.reasons
    assert "adjustment_truth_receipt_required" in assessment.reasons


def test_oos_validation_plan_receipt_hash_cannot_hide_tampering() -> None:
    values = _plan_receipt_values()
    receipt_sha256 = _plan_receipt_sha256(values)
    values["validation_plan_version"] = "tampered-v2"

    with pytest.raises(
        LabelContractError,
        match="oos_validation_plan_receipt_sha256_mismatch",
    ):
        label_maturity_module.FrozenOOSValidationPlanReceipt(
            **values,
            receipt_payload_sha256=receipt_sha256,
        )


def test_oos_plan_definition_mismatch_and_post_decision_freeze_fail_closed() -> None:
    label = _market_truth()
    mismatched_definition = _plan_receipt_for(
        label,
        total_return_definition_version="different-total-return-v2",
    )
    frozen_after_decision = _plan_receipt_for(
        label,
        frozen_at=DECISION_CUTOFF + timedelta(seconds=1),
    )

    mismatch = assess_label_maturity(
        label,
        as_of=AS_OF,
        authority_proof=_authority_proof(label),
        authority_verifier=_FrozenAuthorityVerifier(),
        oos_validation_plan_receipt=mismatched_definition,
        oos_registry_verifier=_FrozenOOSRegistryVerifier(),
    )
    late_freeze = assess_label_maturity(
        label,
        as_of=AS_OF,
        authority_proof=_authority_proof(label),
        authority_verifier=_FrozenAuthorityVerifier(),
        oos_validation_plan_receipt=frozen_after_decision,
        oos_registry_verifier=_FrozenOOSRegistryVerifier(),
    )

    assert mismatch.release_evidence_eligible is False
    assert "frozen_oos_validation_plan_identity_mismatch" in mismatch.reasons
    assert late_freeze.release_evidence_eligible is False
    assert "frozen_oos_validation_plan_after_decision_cutoff" in late_freeze.reasons


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        (
            {"adjustment_truth_valid_through": HORIZON_END - timedelta(seconds=1)},
            "adjustment_truth_does_not_cover_horizon",
        ),
        (
            {"adjustment_truth_available_at": HORIZON_END - timedelta(seconds=1)},
            "adjustment_truth_available_before_valid_through",
        ),
        (
            {"available_at": ADJUSTMENT_TRUTH_AVAILABLE_AT - timedelta(seconds=1)},
            "label_available_before_adjustment_truth",
        ),
        (
            {
                "source_receipt_ids": (
                    "receipt-bars-20260716",
                    "receipt-corporate-actions-20260716",
                )
            },
            "adjustment_truth_receipt_not_in_source_receipts",
        ),
    ],
)
def test_adjustment_truth_pit_chain_fails_closed(
    overrides: dict[str, object],
    error: str,
) -> None:
    with pytest.raises(LabelContractError, match=error):
        _market_truth(**overrides)


def test_adjustment_truth_hash_is_part_of_canonical_market_evidence() -> None:
    values = _market_truth_values()
    evidence_sha256 = _evidence_sha256(values)
    values["adjustment_truth_payload_sha256"] = "b" * 64

    with pytest.raises(LabelContractError, match="evidence_payload_sha256_mismatch"):
        MarketTruth(**values, evidence_payload_sha256=evidence_sha256)


def test_fixture_market_truth_never_becomes_release_evidence() -> None:
    label = _market_truth(source_class="fixture")

    assessment = assess_label_maturity(
        label,
        as_of=AS_OF,
        authority_proof=_authority_proof(label),
        authority_verifier=_FrozenAuthorityVerifier(),
        oos_validation_plan_receipt=_plan_receipt_for(label),
        oos_registry_verifier=_FrozenOOSRegistryVerifier(),
    )

    assert assessment.mature is True
    assert assessment.release_evidence_eligible is False
    assert assessment.eligible_uses == ()
    assert "source_class_not_release_eligible" in assessment.reasons


def test_oos_registry_rejection_and_future_verification_fail_closed() -> None:
    label = _market_truth()
    receipt = _plan_receipt_for(label)

    rejected = assess_label_maturity(
        label,
        as_of=AS_OF,
        authority_proof=_authority_proof(label),
        authority_verifier=_FrozenAuthorityVerifier(),
        oos_validation_plan_receipt=receipt,
        oos_registry_verifier=_FrozenOOSRegistryVerifier(accepted=False),
    )
    verified_in_future = assess_label_maturity(
        label,
        as_of=AS_OF,
        authority_proof=_authority_proof(label),
        authority_verifier=_FrozenAuthorityVerifier(),
        oos_validation_plan_receipt=receipt,
        oos_registry_verifier=_FrozenOOSRegistryVerifier(
            verified_at=AS_OF + timedelta(seconds=1)
        ),
    )

    assert rejected.release_evidence_eligible is False
    assert "frozen_oos_registry_receipt_rejected" in rejected.reasons
    assert verified_in_future.release_evidence_eligible is False
    assert (
        "frozen_oos_registry_verification_not_available_as_of"
        in verified_in_future.reasons
    )


def test_runtime_verifier_is_not_persisted_in_maturity_projection() -> None:
    label = _market_truth()
    assessment = assess_label_maturity(
        label,
        as_of=AS_OF,
        authority_proof=_authority_proof(label),
        authority_verifier=_FrozenAuthorityVerifier(),
        oos_validation_plan_receipt=_plan_receipt_for(label),
        oos_registry_verifier=_FrozenOOSRegistryVerifier(),
    )

    assert "authority_verifier" not in vars(assessment)
    assert "oos_registry_verifier" not in vars(assessment)


def test_label_maturity_record_cannot_self_certify_release_eligibility() -> None:
    with pytest.raises(LabelContractError, match="release_evidence_binding_required"):
        label_maturity_module.LabelMaturityRecord(
            label_id="forged-label",
            label_class="market_truth",
            mature=True,
            release_evidence_eligible=True,
            eligible_uses=(EvidenceUse.PREDICTIVE_VALIDATION,),
            reasons=(),
        )


def test_label_maturity_record_recomputes_maturity_and_release_booleans() -> None:
    label = _market_truth()
    verified = assess_label_maturity(
        label,
        as_of=AS_OF,
        authority_proof=_authority_proof(label),
        authority_verifier=_FrozenAuthorityVerifier(),
        oos_validation_plan_receipt=_plan_receipt_for(label),
        oos_registry_verifier=_FrozenOOSRegistryVerifier(),
    )
    rejected = assess_label_maturity(
        label,
        as_of=AS_OF,
        authority_proof=_authority_proof(label),
        authority_verifier=_FrozenAuthorityVerifier(accepted=False),
        oos_validation_plan_receipt=_plan_receipt_for(label),
        oos_registry_verifier=_FrozenOOSRegistryVerifier(),
    )

    with pytest.raises(LabelContractError, match="maturity_boolean_mismatch"):
        replace(verified, mature=False)

    with pytest.raises(LabelContractError, match="release_eligibility_mismatch"):
        replace(
            rejected,
            release_evidence_eligible=True,
            eligible_uses=(EvidenceUse.PREDICTIVE_VALIDATION,),
            authority_verifier=_FrozenAuthorityVerifier(accepted=False),
            oos_registry_verifier=_FrozenOOSRegistryVerifier(),
        )


def test_self_hashed_canonical_evidence_cannot_hide_payload_tampering() -> None:
    values = _market_truth_values()
    evidence_sha256 = _evidence_sha256(values)
    values["value"] = 0.99

    with pytest.raises(LabelContractError, match="evidence_payload_sha256_mismatch"):
        MarketTruth(**values, evidence_payload_sha256=evidence_sha256)


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"source_receipt_ids": ["receipt-bars-20260716"]}, "immutable_tuple"),
        ({"source_receipt_ids": ()}, "must_be_nonempty"),
        (
            {"source_receipt_ids": ("receipt-z", "receipt-a")},
            "must_be_sorted_unique",
        ),
        (
            {"decision_cutoff": HORIZON_END + timedelta(seconds=1)},
            "decision_cutoff_after_horizon_end",
        ),
    ],
)
def test_market_truth_requires_canonical_receipts_and_decision_cutoff(
    overrides: dict[str, object],
    error: str,
) -> None:
    with pytest.raises(LabelContractError, match=error):
        _market_truth(**overrides)


def test_paper_shadow_and_unavailable_oracle_remain_separate_evidence_classes() -> None:
    paper = Paper(
        label_id="label-paper-1",
        decision_cluster_id="cluster-1",
        horizon="5d",
        horizon_end=HORIZON_END,
        available_at=AVAILABLE_AT,
        value=0.017,
        simulated_fill_id="paper-fill-1",
        execution_lineage_id="ashare-sim-fresh-20260712-v1",
        actual_cost_cny=3.2,
    )
    shadow = Shadow(
        label_id="label-shadow-1",
        decision_cluster_id="cluster-1",
        horizon="5d",
        horizon_end=HORIZON_END,
        available_at=AVAILABLE_AT,
        value=0.019,
        cost_model_version="conservative-cost-v1",
    )
    unavailable = UnavailableOracle(
        label_id="label-unavailable-1",
        decision_cluster_id="cluster-1",
        horizon="5d",
        horizon_end=HORIZON_END,
        available_at=AVAILABLE_AT,
        reason="future_market_truth_not_yet_available",
    )

    paper_result = assess_label_maturity(paper, as_of=AS_OF)
    shadow_result = assess_label_maturity(shadow, as_of=AS_OF)
    unavailable_result = assess_label_maturity(unavailable, as_of=AS_OF)

    assert paper_result.label_class == "paper"
    assert paper_result.eligible_uses == (EvidenceUse.PAPER_EXECUTION_VALIDATION,)
    assert paper_result.release_evidence_eligible is False

    assert shadow_result.label_class == "shadow"
    assert shadow_result.eligible_uses == (EvidenceUse.COUNTERFACTUAL_VALIDATION,)
    assert shadow_result.release_evidence_eligible is False

    assert unavailable_result.label_class == "unavailable_oracle"
    assert unavailable_result.mature is False
    assert unavailable_result.eligible_uses == ()
    assert unavailable_result.release_evidence_eligible is False


def test_future_or_authority_rejected_market_truth_fails_closed() -> None:
    future = _market_truth(
        label_id="label-future",
        decision_cluster_id="cluster-2",
        horizon="1d",
        value=0.01,
    )
    rejected = _market_truth(
        label_id="label-rejected",
        decision_cluster_id="cluster-3",
        horizon="1d",
        value=0.01,
    )

    future_result = assess_label_maturity(
        future, as_of=AVAILABLE_AT - timedelta(seconds=1)
    )
    rejected_result = assess_label_maturity(
        rejected,
        as_of=AS_OF,
        authority_proof=_authority_proof(rejected),
        authority_verifier=_FrozenAuthorityVerifier(accepted=False),
        oos_validation_plan_receipt=_plan_receipt_for(rejected),
        oos_registry_verifier=_FrozenOOSRegistryVerifier(),
    )

    assert future_result.mature is False
    assert "label_not_available_as_of" in future_result.reasons
    assert rejected_result.release_evidence_eligible is False
    assert rejected_result.eligible_uses == ()
    assert "frozen_authority_proof_rejected" in rejected_result.reasons


def test_label_contract_rejects_naive_time_and_non_finite_values() -> None:
    with pytest.raises(LabelContractError, match="timezone_aware"):
        _market_truth(
            label_id="label-naive",
            decision_cluster_id="cluster-4",
            horizon="1d",
            horizon_end=datetime(2026, 7, 16, 7, 0),
            value=0.01,
        )

    with pytest.raises(LabelContractError, match="finite"):
        Shadow(
            label_id="label-nan",
            decision_cluster_id="cluster-5",
            horizon="1d",
            horizon_end=HORIZON_END,
            available_at=AVAILABLE_AT,
            value=float("nan"),
            cost_model_version="conservative-cost-v1",
        )


def test_market_truth_cannot_release_without_explicit_frozen_authority() -> None:
    label = _market_truth()
    assessment = assess_label_maturity(
        label,
        as_of=AS_OF,
        oos_validation_plan_receipt=_plan_receipt_for(label),
        oos_registry_verifier=_FrozenOOSRegistryVerifier(),
    )

    assert assessment.release_evidence_eligible is False
    assert assessment.eligible_uses == ()
    assert "frozen_authority_verifier_required" in assessment.reasons
    assert "frozen_authority_proof_required" in assessment.reasons


def test_self_hashed_receipts_cannot_replace_frozen_authority_proof() -> None:
    label = _market_truth()
    forged = _authority_proof(
        label,
        proof_id="unregistered-caller-proof",
    )

    assessment = assess_label_maturity(
        label,
        as_of=AS_OF,
        authority_proof=forged,
        authority_verifier=_FrozenAuthorityVerifier(),
        oos_validation_plan_receipt=_plan_receipt_for(label),
        oos_registry_verifier=_FrozenOOSRegistryVerifier(),
    )

    assert assessment.release_evidence_eligible is False
    assert assessment.eligible_uses == ()
    assert assessment.reasons == ("frozen_authority_proof_rejected",)


def test_manual_verification_object_cannot_self_certify_release() -> None:
    label = _market_truth()
    proof = _authority_proof(label)
    assessment = assess_label_maturity(label, as_of=AS_OF)
    forged_verification = label_maturity_module.FrozenAuthorityVerification(
        accepted=True,
        verifier_id=_FrozenAuthorityVerifier.verifier_id,
        proof_id=proof.proof_id,
        authority_id=proof.authority_id,
        authority_version=proof.authority_version,
        evidence_payload_sha256=proof.evidence_payload_sha256,
        verified_at=AS_OF,
    )

    with pytest.raises(
        LabelContractError,
        match="explicit_frozen_authority_verifier_required",
    ):
        replace(
            assessment,
            release_evidence_eligible=True,
            eligible_uses=(EvidenceUse.PREDICTIVE_VALIDATION,),
            reasons=(),
            authority_proof=proof,
            authority_verifier_id=_FrozenAuthorityVerifier.verifier_id,
            authority_verification=forged_verification,
        )


def test_record_rechecks_verification_against_injected_frozen_verifier() -> None:
    label = _market_truth()
    proof = _authority_proof(label)
    assessment = assess_label_maturity(label, as_of=AS_OF)
    forged_verification = label_maturity_module.FrozenAuthorityVerification(
        accepted=True,
        verifier_id=_FrozenAuthorityVerifier.verifier_id,
        proof_id=proof.proof_id,
        authority_id=proof.authority_id,
        authority_version=proof.authority_version,
        evidence_payload_sha256=proof.evidence_payload_sha256,
        verified_at=AS_OF,
    )

    with pytest.raises(
        LabelContractError,
        match="frozen_authority_verification_recheck_mismatch",
    ):
        replace(
            assessment,
            release_evidence_eligible=True,
            eligible_uses=(EvidenceUse.PREDICTIVE_VALIDATION,),
            reasons=(),
            authority_proof=proof,
            authority_verifier_id=_FrozenAuthorityVerifier.verifier_id,
            authority_verification=forged_verification,
            authority_verifier=_FrozenAuthorityVerifier(accepted=False),
        )


def test_manual_oos_registry_verification_cannot_self_certify_release() -> None:
    label = _market_truth()
    receipt = _plan_receipt_for(label)
    assessment = assess_label_maturity(
        label,
        as_of=AS_OF,
        authority_proof=_authority_proof(label),
        authority_verifier=_FrozenAuthorityVerifier(),
    )
    forged_verification = label_maturity_module.FrozenOOSRegistryVerification(
        accepted=True,
        verifier_id=_FrozenOOSRegistryVerifier.verifier_id,
        receipt_id=receipt.receipt_id,
        registry_id=receipt.registry_id,
        registry_version=receipt.registry_version,
        receipt_payload_sha256=receipt.receipt_payload_sha256,
        verified_at=AS_OF,
    )

    with pytest.raises(
        LabelContractError,
        match="explicit_frozen_oos_registry_verifier_required",
    ):
        replace(
            assessment,
            release_evidence_eligible=True,
            eligible_uses=(EvidenceUse.PREDICTIVE_VALIDATION,),
            reasons=(),
            oos_validation_plan_receipt=receipt,
            oos_registry_verifier_id=_FrozenOOSRegistryVerifier.verifier_id,
            oos_registry_verification=forged_verification,
            authority_verifier=_FrozenAuthorityVerifier(),
        )


def test_non_market_records_recompute_eligible_uses_and_reasons() -> None:
    paper = Paper(
        label_id="label-paper-forgery",
        decision_cluster_id="cluster-paper-forgery",
        horizon="5d",
        horizon_end=HORIZON_END,
        available_at=AVAILABLE_AT,
        value=0.017,
        simulated_fill_id="paper-fill-forgery",
        execution_lineage_id="ashare-sim-fresh-20260712-v1",
        actual_cost_cny=3.2,
    )
    paper_result = assess_label_maturity(paper, as_of=AS_OF)
    shadow = Shadow(
        label_id="label-shadow-forgery",
        decision_cluster_id="cluster-shadow-forgery",
        horizon="5d",
        horizon_end=HORIZON_END,
        available_at=AVAILABLE_AT,
        value=0.017,
        cost_model_version="conservative-cost-v1",
    )
    shadow_result = assess_label_maturity(shadow, as_of=AS_OF)

    with pytest.raises(LabelContractError, match="eligible_uses_mismatch"):
        replace(
            paper_result,
            eligible_uses=(EvidenceUse.PREDICTIVE_VALIDATION,),
        )
    with pytest.raises(LabelContractError, match="reasons_mismatch"):
        replace(paper_result, reasons=("caller_claimed_clean",))
    with pytest.raises(LabelContractError, match="eligible_uses_mismatch"):
        replace(
            shadow_result,
            eligible_uses=(EvidenceUse.PREDICTIVE_VALIDATION,),
        )
    with pytest.raises(LabelContractError, match="release_eligibility_mismatch"):
        replace(shadow_result, release_evidence_eligible=True)


def test_unavailable_record_cannot_forge_maturity_or_predictive_use() -> None:
    unavailable = UnavailableOracle(
        label_id="label-unavailable-forgery",
        decision_cluster_id="cluster-unavailable-forgery",
        horizon="5d",
        horizon_end=HORIZON_END,
        available_at=AVAILABLE_AT,
        reason="oracle_not_published",
    )
    result = assess_label_maturity(unavailable, as_of=AS_OF)

    with pytest.raises(LabelContractError, match="maturity_boolean_mismatch"):
        replace(result, mature=True)
    with pytest.raises(LabelContractError, match="eligible_uses_mismatch"):
        replace(
            result,
            eligible_uses=(EvidenceUse.PREDICTIVE_VALIDATION,),
        )
    with pytest.raises(LabelContractError, match="reasons_mismatch"):
        replace(result, reasons=("oracle_not_published", "caller_override"))
