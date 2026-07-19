"""Explicit, non-promotable thesis-risk authorities for TA tests only."""

from __future__ import annotations

from datetime import datetime, timedelta

from shared.portfolio.small_account_optimizer import (
    AccountAuthoritySnapshot,
    CandidateAllocationInput,
)
from shared.portfolio.thesis_risk import (
    THESIS_RISK_DIMENSIONS,
    ThesisRiskDimensionCap,
    ThesisRiskExposureReceipt,
    ThesisRiskExposureSetReceipt,
    ThesisRiskExposureSetVerification,
    ThesisRiskExposureVerification,
    ThesisRiskGroups,
    ThesisRiskPolicy,
    ThesisRiskPolicyVerification,
    build_thesis_risk_runtime_authority,
)


FIXTURE_GROUPS = ThesisRiskGroups(
    industry="fixture-industry",
    thesis="fixture-thesis",
    raw_material="fixture-raw-material",
    policy_event="fixture-policy-event",
    crowding="fixture-crowding",
    model_family="fixture-frozen-champion",
)


class FrozenThesisRiskPolicyVerifier:
    def __init__(self, expected: ThesisRiskPolicy) -> None:
        self.expected = expected

    def verify(self, policy, *, decision_time):
        if policy != self.expected:
            raise ValueError("thesis_risk_policy_not_current")
        return ThesisRiskPolicyVerification.create(
            policy=policy,
            verifier_id="tests-frozen-thesis-risk-policy",
            verifier_version="1",
            verified_at=decision_time - timedelta(seconds=1),
            valid_until=decision_time + timedelta(hours=1),
            promotion_eligible=False,
        )


class FrozenThesisRiskExposureVerifier:
    def __init__(self, expected) -> None:
        self.expected = {receipt.exposure_id: receipt for receipt in expected}

    def verify(self, receipt, *, decision_time):
        if self.expected.get(receipt.exposure_id) != receipt:
            raise ValueError("thesis_risk_exposure_not_current")
        return ThesisRiskExposureVerification.create(
            receipt=receipt,
            verifier_id="tests-frozen-thesis-risk-exposure",
            verifier_version="1",
            verified_at=decision_time - timedelta(seconds=1),
            valid_until=decision_time + timedelta(hours=1),
            promotion_eligible=False,
            authority_notional_cny=receipt.notional_cny,
            authority_binding_reference_id=receipt.binding_reference_id,
            authority_binding_sha256=receipt.binding_sha256,
        )


class FrozenThesisRiskExposureSetVerifier:
    def __init__(self, expected: ThesisRiskExposureSetReceipt) -> None:
        self.expected = expected

    def verify(self, receipt, *, decision_time):
        if receipt != self.expected:
            raise ValueError("thesis_risk_exposure_set_not_current")
        return ThesisRiskExposureSetVerification.create(
            receipt=receipt,
            verifier_id="tests-frozen-thesis-risk-exposure-set",
            verifier_version="1",
            verified_at=decision_time - timedelta(seconds=1),
            valid_until=decision_time + timedelta(hours=1),
            promotion_eligible=False,
        )


def build_thesis_risk_fixture(
    *,
    candidates: tuple[CandidateAllocationInput, ...],
    account_snapshot: AccountAuthoritySnapshot,
    decision_time: datetime,
    groups: ThesisRiskGroups = FIXTURE_GROUPS,
) -> dict[str, object]:
    policy = ThesisRiskPolicy(
        policy_id="tests-human-reviewed-thesis-risk-v1",
        reviewed_by="tests-explicit-reviewer",
        review_reference="tests-review-reference-20260716",
        effective_at=decision_time - timedelta(days=1),
        valid_until=decision_time + timedelta(days=30),
        dimension_caps=tuple(
            ThesisRiskDimensionCap(
                dimension=dimension,
                max_exposure_cny=50_000.0,
            )
            for dimension in THESIS_RISK_DIMENSIONS
        ),
    )
    receipts = []
    for candidate in candidates:
        receipts.append(
            ThesisRiskExposureReceipt.create(
                exposure_id=f"candidate-{candidate.symbol}",
                exposure_kind="candidate",
                symbol=candidate.symbol,
                groups=groups,
                notional_cny=0.0,
                as_of=candidate.price_observed_at,
                available_at=candidate.price_observed_at,
                source_dataset_id="tests.thesis-risk.candidate.v1",
                source_receipt_id=f"candidate-source-{candidate.symbol}",
                source_lineage_sha256="1" * 64,
                source_content_sha256="2" * 64,
                binding_reference_id=candidate.score_receipt_sha256,
                binding_sha256=candidate.score_receipt_sha256,
            )
        )
    for position in account_snapshot.positions:
        if position.total_shares <= 0:
            continue
        receipts.append(
            ThesisRiskExposureReceipt.create(
                exposure_id=f"position-{position.symbol}",
                exposure_kind="position",
                symbol=position.symbol,
                groups=groups,
                notional_cny=position.total_shares * position.mark_price_cny,
                as_of=account_snapshot.account_as_of,
                available_at=account_snapshot.account_as_of,
                source_dataset_id="tests.thesis-risk.position.v1",
                source_receipt_id=account_snapshot.position_snapshot_receipt_id,
                source_lineage_sha256="3" * 64,
                source_content_sha256="4" * 64,
                binding_reference_id=account_snapshot.position_snapshot_receipt_id,
                binding_sha256=account_snapshot.position_snapshot_sha256,
            )
        )
    frozen_receipts = tuple(receipts)
    exposure_set = ThesisRiskExposureSetReceipt.create(
        exposure_set_id="tests-frozen-thesis-risk-book-v1",
        receipts=frozen_receipts,
        decision_time=decision_time,
        as_of=max(
            (receipt.as_of for receipt in frozen_receipts),
            default=decision_time,
        ),
        available_at=max(
            (receipt.available_at for receipt in frozen_receipts),
            default=decision_time,
        ),
        source_id="tests.frozen-thesis-risk-book.v1",
        source_generation=1,
        source_lineage_sha256="5" * 64,
    )
    authority = build_thesis_risk_runtime_authority(
        policy=policy,
        policy_verifier=FrozenThesisRiskPolicyVerifier(policy),
        exposure_receipts=frozen_receipts,
        exposure_verifier=FrozenThesisRiskExposureVerifier(frozen_receipts),
        exposure_set_receipt=exposure_set,
        exposure_set_verifier=FrozenThesisRiskExposureSetVerifier(exposure_set),
        decision_time=decision_time,
    )
    return {
        "thesis_risk_authority": authority,
    }


__all__ = [
    "FIXTURE_GROUPS",
    "FrozenThesisRiskExposureSetVerifier",
    "FrozenThesisRiskExposureVerifier",
    "FrozenThesisRiskPolicyVerifier",
    "build_thesis_risk_fixture",
]
