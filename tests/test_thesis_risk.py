from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

import shared.portfolio.small_account_optimizer as optimizer_module
import shared.portfolio.thesis_risk as thesis_risk_module
from shared.portfolio.champion import fixture_rank_evidence
from shared.portfolio.small_account_optimizer import (
    AccountAuthoritySnapshot,
    AccountPositionSnapshot,
    CandidateAllocationInput,
    PositionReductionIntent,
    optimize_small_account,
)
from shared.portfolio.thesis_risk import (
    THESIS_RISK_DIMENSIONS,
    ThesisRiskDimensionCap,
    ThesisRiskExposureReceipt,
    ThesisRiskExposureVerification,
    ThesisRiskGroups,
    ThesisRiskPolicy,
    ThesisRiskPolicyVerification,
)


DECISION_TIME = datetime(2026, 7, 16, 6, 55, tzinfo=timezone.utc)
OBSERVED_AT = DECISION_TIME - timedelta(minutes=2)
VALID_UNTIL = DECISION_TIME + timedelta(minutes=5)


class _AccountVerifier:
    def verify(self, snapshot, *, decision_time):
        return optimizer_module.AccountAuthorityVerification.create(
            snapshot=snapshot,
            verifier_id="thesis-risk-test-account",
            verifier_version="1",
            verified_at=snapshot.account_as_of,
            valid_until=VALID_UNTIL,
            promotion_eligible=False,
        )


class _PolicyVerifier:
    def __init__(self, expected: ThesisRiskPolicy) -> None:
        self.expected = expected

    def verify(self, policy, *, decision_time):
        if policy != self.expected:
            raise ValueError("policy_not_authoritative")
        return ThesisRiskPolicyVerification.create(
            policy=policy,
            verifier_id="thesis-risk-test-policy-verifier",
            verifier_version="1",
            verified_at=decision_time - timedelta(seconds=1),
            valid_until=VALID_UNTIL,
            promotion_eligible=False,
        )


class _ExposureVerifier:
    def __init__(
        self,
        expected: tuple[ThesisRiskExposureReceipt, ...],
        *,
        valid_until: datetime = VALID_UNTIL,
    ) -> None:
        self.expected = {receipt.exposure_id: receipt for receipt in expected}
        self.valid_until = valid_until

    def verify(self, receipt, *, decision_time):
        if self.expected.get(receipt.exposure_id) != receipt:
            raise ValueError("exposure_not_authoritative")
        return ThesisRiskExposureVerification.create(
            receipt=receipt,
            verifier_id="thesis-risk-test-exposure-verifier",
            verifier_version="1",
            verified_at=decision_time - timedelta(seconds=1),
            valid_until=self.valid_until,
            promotion_eligible=False,
            authority_notional_cny=receipt.notional_cny,
            authority_binding_reference_id=receipt.binding_reference_id,
            authority_binding_sha256=receipt.binding_sha256,
        )


class _ExposureSetVerifier:
    def __init__(self, expected, *, valid_until: datetime = VALID_UNTIL) -> None:
        self.expected = expected
        self.valid_until = valid_until

    def verify(self, receipt, *, decision_time):
        if receipt != self.expected:
            raise ValueError("exposure_set_not_authoritative")
        return thesis_risk_module.ThesisRiskExposureSetVerification.create(
            receipt=receipt,
            verifier_id="thesis-risk-test-set-verifier",
            verifier_version="1",
            verified_at=decision_time - timedelta(minutes=2),
            valid_until=self.valid_until,
            promotion_eligible=False,
        )


class _FuturePolicyVerifier(_PolicyVerifier):
    def verify(self, policy, *, decision_time):
        if policy != self.expected:
            raise ValueError("policy_not_authoritative")
        return ThesisRiskPolicyVerification.create(
            policy=policy,
            verifier_id="future-policy-verifier",
            verifier_version="1",
            verified_at=decision_time + timedelta(seconds=1),
            valid_until=VALID_UNTIL,
            promotion_eligible=False,
        )


class _FutureExposureVerifier(_ExposureVerifier):
    def verify(self, receipt, *, decision_time):
        if self.expected.get(receipt.exposure_id) != receipt:
            raise ValueError("exposure_not_authoritative")
        return ThesisRiskExposureVerification.create(
            receipt=receipt,
            verifier_id="future-exposure-verifier",
            verifier_version="1",
            verified_at=decision_time + timedelta(seconds=1),
            valid_until=VALID_UNTIL,
            promotion_eligible=False,
            authority_notional_cny=receipt.notional_cny,
            authority_binding_reference_id=receipt.binding_reference_id,
            authority_binding_sha256=receipt.binding_sha256,
        )


class _FutureExposureSetVerifier(_ExposureSetVerifier):
    def verify(self, receipt, *, decision_time):
        if receipt != self.expected:
            raise ValueError("exposure_set_not_authoritative")
        return thesis_risk_module.ThesisRiskExposureSetVerification.create(
            receipt=receipt,
            verifier_id="future-set-verifier",
            verifier_version="1",
            verified_at=decision_time + timedelta(seconds=1),
            valid_until=VALID_UNTIL,
            promotion_eligible=False,
        )


class _ReboundPolicyVerifier(_PolicyVerifier):
    def verify(self, policy, *, decision_time):
        proof = super().verify(policy, decision_time=decision_time)
        values = {
            "verifier_id": proof.verifier_id,
            "verifier_version": proof.verifier_version,
            "policy_id": "forged-policy-id",
            "policy_sha256": proof.policy_sha256,
            "reviewed_by": proof.reviewed_by,
            "review_reference": proof.review_reference,
            "verified_at": proof.verified_at,
            "valid_until": proof.valid_until,
            "promotion_eligible": proof.promotion_eligible,
        }
        payload = {
            **values,
            "verified_at": proof.verified_at.isoformat(),
            "valid_until": proof.valid_until.isoformat(),
        }
        return ThesisRiskPolicyVerification(
            **values,
            proof_sha256=thesis_risk_module._canonical_sha256(payload),
        )


class _ReboundExposureVerifier(_ExposureVerifier):
    def verify(self, receipt, *, decision_time):
        proof = super().verify(receipt, decision_time=decision_time)
        values = {
            "verifier_id": proof.verifier_id,
            "verifier_version": proof.verifier_version,
            "exposure_id": proof.exposure_id,
            "exposure_receipt_sha256": proof.exposure_receipt_sha256,
            "authority_notional_cny": proof.authority_notional_cny,
            "authority_binding_reference_id": "forged-binding-reference",
            "authority_binding_sha256": proof.authority_binding_sha256,
            "verified_at": proof.verified_at,
            "valid_until": proof.valid_until,
            "promotion_eligible": proof.promotion_eligible,
        }
        payload = {
            **values,
            "verified_at": proof.verified_at.isoformat(),
            "valid_until": proof.valid_until.isoformat(),
        }
        return ThesisRiskExposureVerification(
            **values,
            proof_sha256=thesis_risk_module._canonical_sha256(payload),
        )


class _ReboundExposureSetVerifier(_ExposureSetVerifier):
    def verify(self, receipt, *, decision_time):
        proof = super().verify(receipt, decision_time=decision_time)
        values = {
            "verifier_id": proof.verifier_id,
            "verifier_version": proof.verifier_version,
            "exposure_set_id": "forged-exposure-set-id",
            "exposure_set_receipt_sha256": proof.exposure_set_receipt_sha256,
            "source_generation": proof.source_generation,
            "source_lineage_sha256": proof.source_lineage_sha256,
            "verified_at": proof.verified_at,
            "valid_until": proof.valid_until,
            "promotion_eligible": proof.promotion_eligible,
        }
        payload = {
            **values,
            "verified_at": proof.verified_at.isoformat(),
            "valid_until": proof.valid_until.isoformat(),
        }
        return thesis_risk_module.ThesisRiskExposureSetVerification(
            **values,
            proof_sha256=thesis_risk_module._canonical_sha256(payload),
        )


ACCOUNT_VERIFIER = _AccountVerifier()


def _candidate(symbol: str, *, score: float, price: float = 20.0):
    evidence = fixture_rank_evidence(
        champion_selection_manifest_sha256="c" * 64,
        symbol=symbol,
        decision_time=DECISION_TIME,
        fixture_id=f"thesis-risk-{symbol}-{score}",
        source_fixture_sha256="d" * 64,
        rank_score=score,
    )
    return CandidateAllocationInput(
        symbol=symbol,
        score_evidence=evidence,
        decision_time=DECISION_TIME,
        price_observed_at=OBSERVED_AT,
        decision_reference_price=price,
    )


def _position(symbol: str, *, shares: int, price: float):
    return AccountPositionSnapshot(
        symbol=symbol,
        total_shares=shares,
        sellable_shares=shares,
        mark_price_cny=price,
        price_observed_at=OBSERVED_AT,
    )


def _account(*, positions=(), cash=50_000.0):
    positions = tuple(positions)
    gross = sum(row.total_shares * row.mark_price_cny for row in positions)
    snapshot = AccountAuthoritySnapshot(
        capital_authority_id="ashare-capital-v1",
        authority_generation=1,
        account_as_of=DECISION_TIME,
        available_cash_cny=cash,
        current_gross_cny=gross,
        positions=positions,
        position_snapshot_receipt_id="thesis-risk-position-snapshot-v1",
        position_snapshot_sha256=(
            optimizer_module.account_position_snapshot_sha256(positions)
        ),
        verification_receipt_sha256="b" * 64,
    )
    proof = ACCOUNT_VERIFIER.verify(snapshot, decision_time=DECISION_TIME)
    return replace(
        snapshot,
        verification_receipt_sha256=proof.verification_receipt_sha256,
    )


def _groups(**overrides: str) -> ThesisRiskGroups:
    values = {
        "industry": "industry-ai-infrastructure",
        "thesis": "thesis-capex-acceleration",
        "raw_material": "raw-copper",
        "policy_event": "event-us-capex-guidance",
        "crowding": "crowding-ai-high",
        "model_family": "model-frozen-champion-v1",
    }
    values.update(overrides)
    return ThesisRiskGroups(**values)


def _policy(*, constrained_dimension: str, cap_cny: float) -> ThesisRiskPolicy:
    caps = []
    for dimension in THESIS_RISK_DIMENSIONS:
        caps.append(
            ThesisRiskDimensionCap(
                dimension=dimension,
                max_exposure_cny=(
                    cap_cny if dimension == constrained_dimension else 50_000.0
                ),
            )
        )
    return ThesisRiskPolicy(
        policy_id=f"human-reviewed-{constrained_dimension}-{int(cap_cny)}",
        reviewed_by="nicholas-fixture-review",
        review_reference="review-ticket-20260716",
        effective_at=DECISION_TIME - timedelta(days=1),
        valid_until=DECISION_TIME + timedelta(days=30),
        dimension_caps=tuple(caps),
    )


def _candidate_receipt(candidate, groups):
    return ThesisRiskExposureReceipt.create(
        exposure_id=f"candidate-{candidate.symbol}",
        exposure_kind="candidate",
        symbol=candidate.symbol,
        groups=groups,
        notional_cny=0.0,
        as_of=OBSERVED_AT,
        available_at=OBSERVED_AT + timedelta(seconds=1),
        source_dataset_id="ta.thesis-risk.fixture.v1",
        source_receipt_id=f"source-{candidate.symbol}",
        source_lineage_sha256="1" * 64,
        source_content_sha256="2" * 64,
        binding_reference_id=candidate.score_receipt_sha256,
        binding_sha256=candidate.score_receipt_sha256,
    )


def _position_receipt(account, position, groups):
    return ThesisRiskExposureReceipt.create(
        exposure_id=f"position-{position.symbol}",
        exposure_kind="position",
        symbol=position.symbol,
        groups=groups,
        notional_cny=position.total_shares * position.mark_price_cny,
        as_of=account.account_as_of,
        available_at=account.account_as_of,
        source_dataset_id="ta.account-position-risk.v1",
        source_receipt_id=account.position_snapshot_receipt_id,
        source_lineage_sha256="3" * 64,
        source_content_sha256="4" * 64,
        binding_reference_id=account.position_snapshot_receipt_id,
        binding_sha256=account.position_snapshot_sha256,
    )


def _pending_receipt(
    *,
    symbol: str,
    notional: float,
    groups,
    pending_action: str = "open",
):
    return ThesisRiskExposureReceipt.create(
        exposure_id=f"pending-{symbol}",
        exposure_kind="pending",
        pending_action=pending_action,
        symbol=symbol,
        groups=groups,
        notional_cny=notional,
        as_of=OBSERVED_AT,
        available_at=OBSERVED_AT + timedelta(seconds=1),
        source_dataset_id="ta.pending-risk-reservation.v1",
        source_receipt_id=f"reservation-{symbol}",
        source_lineage_sha256="5" * 64,
        source_content_sha256="6" * 64,
        binding_reference_id=f"reservation-{symbol}",
        binding_sha256="7" * 64,
    )


def _exposure_set(receipts):
    receipts = tuple(receipts)
    return thesis_risk_module.ThesisRiskExposureSetReceipt.create(
        exposure_set_id="thesis-risk-current-pending-book-v1",
        receipts=receipts,
        decision_time=DECISION_TIME,
        as_of=max((row.as_of for row in receipts), default=OBSERVED_AT),
        available_at=max(
            (row.available_at for row in receipts),
            default=OBSERVED_AT + timedelta(seconds=1),
        ),
        source_id="ta.oms.pending-risk-book.v1",
        source_generation=7,
        source_lineage_sha256="8" * 64,
    )


def _optimize(*, candidates, account, receipts, policy, reductions=()):
    receipts = tuple(receipts)
    exposure_set = _exposure_set(receipts)
    authority = thesis_risk_module.build_thesis_risk_runtime_authority(
        policy=policy,
        policy_verifier=_PolicyVerifier(policy),
        exposure_receipts=receipts,
        exposure_verifier=_ExposureVerifier(receipts),
        exposure_set_receipt=exposure_set,
        exposure_set_verifier=_ExposureSetVerifier(exposure_set),
        decision_time=DECISION_TIME,
    )
    return optimize_small_account(
        candidates=tuple(candidates),
        account_snapshot=account,
        decision_time=DECISION_TIME,
        account_authority_verifier=ACCOUNT_VERIFIER,
        reduction_intents=tuple(reductions),
        thesis_risk_authority=authority,
    )


def test_thesis_risk_receipt_is_immutable_and_content_addressed() -> None:
    candidate = _candidate("600000.SH", score=1.0)
    receipt = _candidate_receipt(candidate, _groups())

    assert len(receipt.receipt_sha256) == 64
    with pytest.raises(ValueError, match="thesis_risk_receipt_hash_mismatch"):
        replace(receipt, groups=replace(receipt.groups, thesis="tampered-thesis"))


def test_exposure_set_is_content_addressed_and_binds_kind_counts() -> None:
    candidate = _candidate("600000.SH", score=1.0)
    receipts = (
        _candidate_receipt(candidate, _groups()),
        _pending_receipt(symbol="600001.SH", notional=2_000.0, groups=_groups()),
    )

    exposure_set = _exposure_set(receipts)

    assert exposure_set.candidate_count == 1
    assert exposure_set.pending_count == 1
    with pytest.raises(
        ValueError,
        match="thesis_risk_exposure_set_hash_mismatch",
    ):
        replace(exposure_set, source_generation=8)


@pytest.mark.parametrize("mutation", ["remove", "add"])
def test_exposure_set_verification_rejects_receipt_membership_changes(
    mutation: str,
) -> None:
    candidate = _candidate("600000.SH", score=1.0)
    receipts = (_candidate_receipt(candidate, _groups()),)
    exposure_set = _exposure_set(receipts)
    supplied = (
        ()
        if mutation == "remove"
        else (
            *receipts,
            _pending_receipt(symbol="600001.SH", notional=1_000.0, groups=_groups()),
        )
    )

    with pytest.raises(
        ValueError,
        match="thesis_risk_exposure_set_membership_mismatch",
    ):
        thesis_risk_module.verify_thesis_risk_exposure_set(
            receipt=exposure_set,
            receipts=supplied,
            verifier=_ExposureSetVerifier(exposure_set),
            decision_time=DECISION_TIME,
        )


def test_exposure_set_verification_rejects_expired_detached_proof() -> None:
    candidate = _candidate("600000.SH", score=1.0)
    receipts = (_candidate_receipt(candidate, _groups()),)
    exposure_set = _exposure_set(receipts)

    with pytest.raises(
        ValueError,
        match="thesis_risk_exposure_set_proof_expired",
    ):
        thesis_risk_module.verify_thesis_risk_exposure_set(
            receipt=exposure_set,
            receipts=receipts,
            verifier=_ExposureSetVerifier(
                exposure_set,
                valid_until=DECISION_TIME - timedelta(minutes=1),
            ),
            decision_time=DECISION_TIME,
        )


def test_all_detached_verifiers_are_mandatory() -> None:
    policy = _policy(constrained_dimension="industry", cap_cny=10_000.0)
    candidate = _candidate("600000.SH", score=1.0)
    receipt = _candidate_receipt(candidate, _groups())
    exposure_set = _exposure_set((receipt,))

    with pytest.raises(ValueError, match="thesis_risk_policy_verifier_required"):
        thesis_risk_module.verify_thesis_risk_policy(
            policy=policy,
            verifier=object(),
            decision_time=DECISION_TIME,
        )
    with pytest.raises(ValueError, match="thesis_risk_exposure_verifier_required"):
        thesis_risk_module.verify_thesis_risk_exposure(
            receipt=receipt,
            verifier=object(),
            decision_time=DECISION_TIME,
        )
    with pytest.raises(
        ValueError,
        match="thesis_risk_exposure_set_verifier_required",
    ):
        thesis_risk_module.verify_thesis_risk_exposure_set(
            receipt=exposure_set,
            receipts=(receipt,),
            verifier=object(),
            decision_time=DECISION_TIME,
        )


def test_all_detached_proofs_reject_future_verification_time() -> None:
    policy = _policy(constrained_dimension="industry", cap_cny=10_000.0)
    candidate = _candidate("600000.SH", score=1.0)
    receipt = _candidate_receipt(candidate, _groups())
    exposure_set = _exposure_set((receipt,))

    with pytest.raises(ValueError, match="thesis_risk_policy_proof_after_decision"):
        thesis_risk_module.verify_thesis_risk_policy(
            policy=policy,
            verifier=_FuturePolicyVerifier(policy),
            decision_time=DECISION_TIME,
        )
    with pytest.raises(
        ValueError,
        match="thesis_risk_exposure_proof_after_decision",
    ):
        thesis_risk_module.verify_thesis_risk_exposure(
            receipt=receipt,
            verifier=_FutureExposureVerifier((receipt,)),
            decision_time=DECISION_TIME,
        )
    with pytest.raises(
        ValueError,
        match="thesis_risk_exposure_set_proof_after_decision",
    ):
        thesis_risk_module.verify_thesis_risk_exposure_set(
            receipt=exposure_set,
            receipts=(receipt,),
            verifier=_FutureExposureSetVerifier(exposure_set),
            decision_time=DECISION_TIME,
        )


def test_rehashed_detached_proofs_cannot_change_authority_bindings() -> None:
    policy = _policy(constrained_dimension="industry", cap_cny=10_000.0)
    candidate = _candidate("600000.SH", score=1.0)
    receipt = _candidate_receipt(candidate, _groups())
    exposure_set = _exposure_set((receipt,))

    with pytest.raises(ValueError, match="thesis_risk_policy_proof_binding_mismatch"):
        thesis_risk_module.verify_thesis_risk_policy(
            policy=policy,
            verifier=_ReboundPolicyVerifier(policy),
            decision_time=DECISION_TIME,
        )
    with pytest.raises(
        ValueError,
        match="thesis_risk_exposure_proof_binding_mismatch",
    ):
        thesis_risk_module.verify_thesis_risk_exposure(
            receipt=receipt,
            verifier=_ReboundExposureVerifier((receipt,)),
            decision_time=DECISION_TIME,
        )
    with pytest.raises(
        ValueError,
        match="thesis_risk_exposure_set_proof_binding_mismatch",
    ):
        thesis_risk_module.verify_thesis_risk_exposure_set(
            receipt=exposure_set,
            receipts=(receipt,),
            verifier=_ReboundExposureSetVerifier(exposure_set),
            decision_time=DECISION_TIME,
        )


def test_exposure_set_cannot_claim_time_before_latest_member() -> None:
    candidate = _candidate("600000.SH", score=1.0)
    receipt = _candidate_receipt(candidate, _groups())
    stale_set = thesis_risk_module.ThesisRiskExposureSetReceipt.create(
        exposure_set_id="stale-thesis-risk-book-v1",
        receipts=(receipt,),
        decision_time=DECISION_TIME,
        as_of=receipt.as_of - timedelta(seconds=1),
        available_at=receipt.available_at - timedelta(seconds=1),
        source_id="ta.stale-risk-book.v1",
        source_generation=1,
        source_lineage_sha256="8" * 64,
    )

    with pytest.raises(ValueError, match="thesis_risk_exposure_set_time_invalid"):
        thesis_risk_module.verify_thesis_risk_exposure_set(
            receipt=stale_set,
            receipts=(receipt,),
            verifier=_ExposureSetVerifier(stale_set),
            decision_time=DECISION_TIME,
        )


@pytest.mark.parametrize("pending_action", [None, "reduce", "exit", "hold"])
def test_pending_receipt_requires_open_or_increase_reservation(
    pending_action: str | None,
) -> None:
    values = {
        "exposure_id": "pending-invalid-action",
        "exposure_kind": "pending",
        "symbol": "600000.SH",
        "groups": _groups(),
        "notional_cny": 1_000.0,
        "as_of": OBSERVED_AT,
        "available_at": OBSERVED_AT + timedelta(seconds=1),
        "source_dataset_id": "ta.pending-risk-reservation.v1",
        "source_receipt_id": "pending-invalid-action",
        "source_lineage_sha256": "5" * 64,
        "source_content_sha256": "6" * 64,
        "binding_reference_id": "pending-invalid-action",
        "binding_sha256": "7" * 64,
    }
    if pending_action is not None:
        values["pending_action"] = pending_action

    with pytest.raises(
        ValueError,
        match="pending_thesis_risk_action_(required|invalid)",
    ):
        ThesisRiskExposureReceipt.create(**values)


def test_group_effect_rejects_nonfinite_signed_delta() -> None:
    with pytest.raises(ValueError, match="requested_delta_cny_invalid"):
        thesis_risk_module.ThesisRiskGroupEffect(
            dimension="industry",
            group_id="industry-ai",
            pre_exposure_cny=0.0,
            requested_delta_cny=float("nan"),
            requested_post_exposure_cny=0.0,
            delta_cny=0.0,
            post_exposure_cny=0.0,
            cap_cny=10_000.0,
            policy_proof_sha256="9" * 64,
        )


def test_runtime_authority_binds_verified_policy_set_and_initial_exposure() -> None:
    position = _position("600000.SH", shares=100, price=20.0)
    account = _account(positions=(position,), cash=48_000.0)
    candidate = _candidate("600001.SH", score=1.0)
    groups = _groups()
    receipts = (
        _candidate_receipt(candidate, groups),
        _position_receipt(account, position, groups),
        _pending_receipt(symbol="600002.SH", notional=1_000.0, groups=groups),
    )
    policy = _policy(constrained_dimension="industry", cap_cny=5_000.0)
    exposure_set = _exposure_set(receipts)

    authority = thesis_risk_module.build_thesis_risk_runtime_authority(
        policy=policy,
        policy_verifier=_PolicyVerifier(policy),
        exposure_receipts=receipts,
        exposure_verifier=_ExposureVerifier(receipts),
        exposure_set_receipt=exposure_set,
        exposure_set_verifier=_ExposureSetVerifier(exposure_set),
        decision_time=DECISION_TIME,
    )

    assert authority.policy == policy
    assert authority.exposure_set_receipt.pending_count == 1
    assert authority.initial_group_exposures == tuple(
        sorted((dimension, group_id, 3_000.0) for dimension, group_id in groups.items())
    )
    assert len(authority.authority_sha256) == 64


def test_runtime_authority_rejects_same_symbol_group_reclassification() -> None:
    candidate = _candidate("600000.SH", score=1.0)
    receipts = (
        _candidate_receipt(candidate, _groups()),
        _pending_receipt(
            symbol=candidate.symbol,
            notional=1_000.0,
            groups=_groups(thesis="forged-reclassified-thesis"),
        ),
    )
    policy = _policy(constrained_dimension="thesis", cap_cny=10_000.0)
    exposure_set = _exposure_set(receipts)

    with pytest.raises(
        ValueError,
        match="pending_thesis_risk_groups_mismatch",
    ):
        thesis_risk_module.build_thesis_risk_runtime_authority(
            policy=policy,
            policy_verifier=_PolicyVerifier(policy),
            exposure_receipts=receipts,
            exposure_verifier=_ExposureVerifier(receipts),
            exposure_set_receipt=exposure_set,
            exposure_set_verifier=_ExposureSetVerifier(exposure_set),
            decision_time=DECISION_TIME,
        )


def test_runtime_authority_rejects_duplicate_or_expired_individual_proofs() -> None:
    candidate = _candidate("600000.SH", score=1.0)
    receipt = _candidate_receipt(candidate, _groups())
    policy = _policy(constrained_dimension="industry", cap_cny=10_000.0)
    exposure_set = _exposure_set((receipt,))
    authority = thesis_risk_module.build_thesis_risk_runtime_authority(
        policy=policy,
        policy_verifier=_PolicyVerifier(policy),
        exposure_receipts=(receipt,),
        exposure_verifier=_ExposureVerifier((receipt,)),
        exposure_set_receipt=exposure_set,
        exposure_set_verifier=_ExposureSetVerifier(exposure_set),
        decision_time=DECISION_TIME,
    )
    with pytest.raises(
        ValueError,
        match="thesis_risk_authority_exposure_proofs_invalid",
    ):
        replace(
            authority,
            exposure_proofs=(
                authority.exposure_proofs[0],
                authority.exposure_proofs[0],
            ),
        )

    expired = ThesisRiskExposureVerification.create(
        receipt=receipt,
        verifier_id="manually-resigned-expired-proof",
        verifier_version="1",
        verified_at=DECISION_TIME - timedelta(minutes=2),
        valid_until=DECISION_TIME - timedelta(seconds=1),
        promotion_eligible=False,
        authority_notional_cny=receipt.notional_cny,
        authority_binding_reference_id=receipt.binding_reference_id,
        authority_binding_sha256=receipt.binding_sha256,
    )
    with pytest.raises(
        ValueError,
        match="thesis_risk_authority_exposure_proofs_invalid",
    ):
        replace(authority, exposure_proofs=(expired,))


def test_policy_requires_every_dimension_and_explicit_human_review() -> None:
    complete = _policy(constrained_dimension="industry", cap_cny=10_000.0)

    with pytest.raises(ValueError, match="thesis_risk_policy_dimensions_invalid"):
        replace(complete, dimension_caps=complete.dimension_caps[:-1])
    with pytest.raises(ValueError, match="thesis_risk_policy_review_invalid"):
        replace(complete, reviewed_by="")


def test_optimizer_fails_closed_without_explicit_thesis_risk_authorities() -> None:
    candidate = _candidate("600000.SH", score=1.0)

    with pytest.raises(ValueError, match="thesis_risk_runtime_authority_required"):
        optimize_small_account(
            candidates=(candidate,),
            account_snapshot=_account(),
            decision_time=DECISION_TIME,
            account_authority_verifier=ACCOUNT_VERIFIER,
        )


@pytest.mark.parametrize("dimension", THESIS_RISK_DIMENSIONS)
def test_same_group_cap_rejects_second_buy_for_every_risk_dimension(
    dimension: str,
) -> None:
    first = _candidate("600000.SH", score=1.0)
    second = _candidate("600001.SH", score=0.9)
    shared_group = _groups()
    receipts = (
        _candidate_receipt(first, shared_group),
        _candidate_receipt(second, shared_group),
    )
    policy = _policy(constrained_dimension=dimension, cap_cny=3_000.0)

    plan = _optimize(
        candidates=(first, second),
        account=_account(),
        receipts=receipts,
        policy=policy,
    )

    assert plan.decisions[0].order_shares == 100
    assert plan.decisions[0].thesis_risk_evaluated_order_shares == 100
    assert plan.decisions[1].order_shares == 0
    assert plan.decisions[1].thesis_risk_evaluated_order_shares == 100
    assert plan.decisions[1].reason_codes == ("risk_group_cap",)
    constrained = next(
        row
        for row in plan.decisions[1].thesis_risk_group_effects
        if row.dimension == dimension
    )
    assert constrained.delta_cny == 0.0
    assert constrained.post_exposure_cny == 2_000.0
    assert constrained.cap_cny == 3_000.0
    assert constrained.requested_post_exposure_cny == 4_000.0
    assert constrained.policy_proof_sha256 == plan.thesis_risk_policy_proof_sha256


def test_existing_position_and_new_order_share_the_same_group_cap() -> None:
    position = _position("600000.SH", shares=100, price=20.0)
    account = _account(positions=(position,), cash=48_000.0)
    candidate = _candidate("600001.SH", score=1.0)
    groups = _groups()
    receipts = (
        _position_receipt(account, position, groups),
        _candidate_receipt(candidate, groups),
    )

    plan = _optimize(
        candidates=(candidate,),
        account=account,
        receipts=receipts,
        policy=_policy(constrained_dimension="thesis", cap_cny=3_000.0),
    )

    decision = next(row for row in plan.decisions if row.symbol == candidate.symbol)
    assert decision.order_shares == 0
    assert decision.reason_codes == ("risk_group_cap",)


def test_existing_position_increase_requires_same_groups_and_is_executable() -> None:
    position = _position("600000.SH", shares=1, price=6.0)
    account = _account(positions=(position,), cash=49_994.0)
    candidate = _candidate("600000.SH", score=1.0, price=6.0)
    groups = _groups()
    receipts = (
        _position_receipt(account, position, groups),
        _candidate_receipt(candidate, groups),
    )

    plan = _optimize(
        candidates=(candidate,),
        account=account,
        receipts=receipts,
        policy=_policy(constrained_dimension="thesis", cap_cny=10_000.0),
    )

    decision = plan.decisions[0]
    assert decision.current_shares == 1
    assert decision.order_shares == 300
    assert decision.target_shares == 301
    assert decision.thesis_risk_evaluated_order_shares == 300
    assert all(
        effect.delta_cny == 1_800.0 for effect in decision.thesis_risk_group_effects
    )


def test_existing_position_candidate_group_change_fails_closed() -> None:
    position = _position("600000.SH", shares=100, price=5.0)
    account = _account(positions=(position,), cash=49_500.0)
    candidate = _candidate("600000.SH", score=1.0, price=5.0)
    receipts = (
        _position_receipt(account, position, _groups()),
        _candidate_receipt(
            candidate,
            _groups(thesis="thesis-forged-reassignment"),
        ),
    )

    with pytest.raises(
        ValueError,
        match="candidate_position_thesis_risk_groups_mismatch",
    ):
        _optimize(
            candidates=(candidate,),
            account=account,
            receipts=receipts,
            policy=_policy(constrained_dimension="thesis", cap_cny=10_000.0),
        )


def test_pending_reservation_is_included_in_group_exposure() -> None:
    candidate = _candidate("600001.SH", score=1.0)
    groups = _groups()
    receipts = (
        _candidate_receipt(candidate, groups),
        _pending_receipt(symbol="600000.SH", notional=2_000.0, groups=groups),
    )

    plan = _optimize(
        candidates=(candidate,),
        account=_account(),
        receipts=receipts,
        policy=_policy(constrained_dimension="policy_event", cap_cny=3_000.0),
    )

    assert plan.decisions[0].order_shares == 0
    assert plan.decisions[0].reason_codes == ("risk_group_cap",)


def test_pending_increase_reservation_is_included_in_group_exposure() -> None:
    candidate = _candidate("600001.SH", score=1.0)
    groups = _groups()
    receipts = (
        _candidate_receipt(candidate, groups),
        _pending_receipt(
            symbol="600000.SH",
            notional=2_000.0,
            groups=groups,
            pending_action="increase",
        ),
    )

    plan = _optimize(
        candidates=(candidate,),
        account=_account(),
        receipts=receipts,
        policy=_policy(constrained_dimension="policy_event", cap_cny=3_000.0),
    )

    assert plan.decisions[0].order_shares == 0
    assert plan.decisions[0].reason_codes == ("risk_group_cap",)


def test_new_authority_rebuilds_group_exposure_without_prior_state() -> None:
    candidate = _candidate("600001.SH", score=1.0)
    groups = _groups()
    policy = _policy(constrained_dimension="policy_event", cap_cny=3_000.0)
    blocked = _optimize(
        candidates=(candidate,),
        account=_account(),
        receipts=(
            _candidate_receipt(candidate, groups),
            _pending_receipt(
                symbol="600000.SH",
                notional=2_000.0,
                groups=groups,
            ),
        ),
        policy=policy,
    )
    permitted = _optimize(
        candidates=(candidate,),
        account=_account(),
        receipts=(_candidate_receipt(candidate, groups),),
        policy=policy,
    )

    assert blocked.decisions[0].order_shares == 0
    assert permitted.decisions[0].order_shares == 100


def test_pending_reservation_cannot_reassign_same_symbol_to_different_groups() -> None:
    candidate = _candidate("600000.SH", score=1.0)
    receipts = (
        _candidate_receipt(candidate, _groups()),
        _pending_receipt(
            symbol=candidate.symbol,
            notional=2_000.0,
            groups=_groups(thesis="forged-pending-thesis"),
        ),
    )

    with pytest.raises(
        ValueError,
        match="pending_thesis_risk_groups_mismatch",
    ):
        _optimize(
            candidates=(candidate,),
            account=_account(),
            receipts=receipts,
            policy=_policy(constrained_dimension="thesis", cap_cny=10_000.0),
        )


def test_reduce_is_allowed_when_group_is_already_above_cap() -> None:
    position = _position("600000.SH", shares=200, price=20.0)
    account = _account(positions=(position,), cash=46_000.0)
    receipt = _position_receipt(account, position, _groups())
    reduction = PositionReductionIntent(
        intent_id="risk-reduction-1",
        symbol=position.symbol,
        action="reduce",
        target_shares=100,
        decision_time=DECISION_TIME,
    )

    plan = _optimize(
        candidates=(),
        account=account,
        receipts=(receipt,),
        policy=_policy(constrained_dimension="industry", cap_cny=1_000.0),
        reductions=(reduction,),
    )

    decision = plan.decisions[0]
    assert decision.order_shares == -100
    assert decision.thesis_risk_evaluated_order_shares == -100
    assert decision.reason_codes == ("allocated", "explicit_reduction_intent")
    industry = next(
        row for row in decision.thesis_risk_group_effects if row.dimension == "industry"
    )
    assert industry.delta_cny == -2_000.0
    assert industry.post_exposure_cny == 2_000.0


def test_missing_position_exposure_receipt_fails_closed() -> None:
    position = _position("600000.SH", shares=100, price=20.0)

    with pytest.raises(ValueError, match="position_thesis_risk_receipt_missing"):
        _optimize(
            candidates=(),
            account=_account(positions=(position,), cash=48_000.0),
            receipts=(),
            policy=_policy(constrained_dimension="industry", cap_cny=10_000.0),
        )


def test_expired_or_tampered_exposure_proof_fails_closed() -> None:
    candidate = _candidate("600000.SH", score=1.0)
    receipt = _candidate_receipt(candidate, _groups())
    policy = _policy(constrained_dimension="industry", cap_cny=10_000.0)

    with pytest.raises(ValueError, match="thesis_risk_exposure_proof_expired"):
        exposure_set = _exposure_set((receipt,))
        thesis_risk_module.build_thesis_risk_runtime_authority(
            policy=policy,
            policy_verifier=_PolicyVerifier(policy),
            exposure_receipts=(receipt,),
            exposure_verifier=_ExposureVerifier(
                (receipt,), valid_until=DECISION_TIME - timedelta(seconds=1)
            ),
            exposure_set_receipt=exposure_set,
            exposure_set_verifier=_ExposureSetVerifier(exposure_set),
            decision_time=DECISION_TIME,
        )

    with pytest.raises(ValueError, match="candidate_thesis_risk_binding_mismatch"):
        bad = replace(
            receipt,
            binding_reference_id="forged-score-receipt",
            receipt_sha256=ThesisRiskExposureReceipt.compute_receipt_sha256(
                exposure_id=receipt.exposure_id,
                exposure_kind=receipt.exposure_kind,
                symbol=receipt.symbol,
                groups=receipt.groups,
                notional_cny=receipt.notional_cny,
                as_of=receipt.as_of,
                available_at=receipt.available_at,
                source_dataset_id=receipt.source_dataset_id,
                source_receipt_id=receipt.source_receipt_id,
                source_lineage_sha256=receipt.source_lineage_sha256,
                source_content_sha256=receipt.source_content_sha256,
                binding_reference_id="forged-score-receipt",
                binding_sha256=receipt.binding_sha256,
            ),
        )
        _optimize(
            candidates=(candidate,),
            account=_account(),
            receipts=(bad,),
            policy=policy,
        )


def test_plan_hash_binds_group_effects_and_policy_proof() -> None:
    candidate = _candidate("600000.SH", score=1.0)
    first_policy = _policy(constrained_dimension="industry", cap_cny=10_000.0)
    second_policy = _policy(constrained_dimension="industry", cap_cny=11_000.0)
    receipt = _candidate_receipt(candidate, _groups())

    first = _optimize(
        candidates=(candidate,),
        account=_account(),
        receipts=(receipt,),
        policy=first_policy,
    )
    second = _optimize(
        candidates=(candidate,),
        account=_account(),
        receipts=(receipt,),
        policy=second_policy,
    )

    assert first.plan_sha256 != second.plan_sha256
    assert first.thesis_risk_policy_proof_sha256 != (
        second.thesis_risk_policy_proof_sha256
    )
