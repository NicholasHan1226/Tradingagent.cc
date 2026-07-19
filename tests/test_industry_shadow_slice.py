from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from shared.industry.shadow_slice import (
    IndustryScoreAuthorityVerification,
    IndustryShadowContractError,
    IndustryShadowInput,
    build_industry_shadow_basket,
)


DECISION_TIME = datetime(2026, 7, 16, 1, 5, tzinfo=timezone.utc)


class _FixtureScoreAuthorityVerifier:
    """Independent in-memory authority fixture; never a production verifier."""

    verifier_id = "fixture-industry-score-authority-v1"

    def __init__(self, authorized: tuple[IndustryShadowInput, ...]) -> None:
        self._authorized = {item.industry_id: item for item in authorized}

    def verify(
        self,
        item: IndustryShadowInput,
        *,
        decision_time: datetime,
    ) -> IndustryScoreAuthorityVerification:
        authority = self._authorized.get(item.industry_id)
        bound = authority or item
        accepted = authority is not None and (
            authority.score_content_sha256 == item.score_content_sha256
            and authority.score_receipt_id == item.score_receipt_id
            and authority.score_receipt_sha256 == item.score_receipt_sha256
            and authority.coverage_authority_receipt_id
            == item.coverage_authority_receipt_id
            and authority.coverage_authority_receipt_sha256
            == item.coverage_authority_receipt_sha256
        )
        proof = "|".join(
            (
                self.verifier_id,
                bound.industry_id,
                bound.score_content_sha256,
                bound.score_receipt_sha256,
                bound.coverage_authority_receipt_sha256,
                decision_time.isoformat(),
                str(accepted),
            )
        )
        return IndustryScoreAuthorityVerification(
            accepted=accepted,
            verifier_id=self.verifier_id,
            proof_sha256=hashlib.sha256(proof.encode("utf-8")).hexdigest(),
            verified_at=decision_time,
            decision_time=decision_time,
            industry_id=bound.industry_id,
            score_content_sha256=bound.score_content_sha256,
            score_receipt_id=bound.score_receipt_id,
            score_receipt_sha256=bound.score_receipt_sha256,
            coverage_authority_receipt_id=bound.coverage_authority_receipt_id,
            coverage_authority_receipt_sha256=(bound.coverage_authority_receipt_sha256),
        )


def _candidate(
    industry_id: str,
    score: float,
    *,
    expected: int = 100,
    observed: int = 100,
) -> IndustryShadowInput:
    suffix = industry_id[-1].lower()
    return IndustryShadowInput(
        industry_id=industry_id,
        taxonomy_id="cn-industry-canonical",
        taxonomy_version="2026-07-15.v1",
        membership_snapshot_sha256=suffix * 64,
        membership_effective_at=datetime(2026, 7, 15, tzinfo=timezone.utc),
        membership_available_at=datetime(2026, 7, 16, 0, 30, tzinfo=timezone.utc),
        expected_member_count=expected,
        observed_member_count=observed,
        activity_score=score,
        score_method_id="industry-activity-composite",
        score_method_version="phase1.5.v1",
        score_observed_at=datetime(2026, 7, 16, 0, 15, tzinfo=timezone.utc),
        score_available_at=datetime(2026, 7, 16, 0, 45, tzinfo=timezone.utc),
        score_valid_until=datetime(2026, 7, 17, 0, 45, tzinfo=timezone.utc),
        score_receipt_id=f"score-receipt-{industry_id}",
        score_receipt_sha256=("1" if suffix != "1" else "2") * 64,
        coverage_authority_receipt_id=f"coverage-receipt-{industry_id}",
        coverage_authority_receipt_sha256=("2" if suffix != "2" else "3") * 64,
        evidence_receipt_ids=(f"receipt-{industry_id}",),
        source_generation=2,
    )


def _build(
    candidates: tuple[IndustryShadowInput, ...],
    *,
    decision_time: datetime = DECISION_TIME,
    minimum_coverage_ratio: float = 0.95,
    verifier: _FixtureScoreAuthorityVerifier | None = None,
):
    authority = verifier or _FixtureScoreAuthorityVerifier(candidates)
    return build_industry_shadow_basket(
        candidates,
        decision_time=decision_time,
        minimum_coverage_ratio=minimum_coverage_ratio,
        score_authority_verifier=authority,
    )


def test_shadow_basket_selects_one_deep_and_two_watch_without_trading_authority() -> (
    None
):
    basket = _build(
        (
            _candidate("industry-a", 0.72),
            _candidate("industry-b", 0.91),
            _candidate("industry-c", 0.81),
            _candidate("industry-d", 0.40),
        )
    )

    assert basket.deep_research_industry_id == "industry-b"
    assert basket.watch_industry_ids == ("industry-c", "industry-a")
    assert basket.shadow_only is True
    assert basket.context_only is True
    assert basket.position_effect_allowed is False
    assert basket.promotion_eligible is False
    assert len(basket.authority_proofs) == 3
    assert all(proof.accepted is True for proof in basket.authority_proofs)
    assert len(basket.basket_sha256) == 64
    assert all(not hasattr(item, "symbols") for item in basket.selected_inputs)


def test_shadow_basket_filters_incomplete_coverage_and_fails_if_slice_is_too_thin() -> (
    None
):
    candidates = (
        _candidate("industry-a", 0.99, expected=100, observed=40),
        _candidate("industry-b", 0.91),
        _candidate("industry-c", 0.81),
    )

    with pytest.raises(
        IndustryShadowContractError,
        match="insufficient_eligible_industries",
    ):
        _build(candidates)


def test_shadow_basket_is_point_in_time_and_taxonomy_consistent() -> None:
    base = _candidate("industry-a", 0.72)
    future = replace(
        _candidate("industry-b", 0.91),
        membership_available_at=datetime(2026, 7, 16, 2, 0, tzinfo=timezone.utc),
        score_available_at=datetime(2026, 7, 16, 2, 0, tzinfo=timezone.utc),
    )

    with pytest.raises(IndustryShadowContractError, match="future_membership_evidence"):
        _build((base, future, _candidate("industry-c", 0.81)))

    mismatched = replace(
        _candidate("industry-b", 0.91),
        taxonomy_version="2026-07-16.v2",
    )
    with pytest.raises(IndustryShadowContractError, match="taxonomy_mismatch"):
        _build((base, mismatched, _candidate("industry-c", 0.81)))


def test_shadow_basket_identity_binds_evidence_and_is_deterministic() -> None:
    inputs = (
        _candidate("industry-a", 0.72),
        _candidate("industry-b", 0.91),
        _candidate("industry-c", 0.81),
    )
    first = _build(inputs)
    replay_inputs = tuple(reversed(inputs))
    replay = _build(replay_inputs)
    changed_inputs = (
        replace(inputs[0], evidence_receipt_ids=("receipt-revised",)),
        inputs[1],
        inputs[2],
    )
    changed = _build(changed_inputs)

    assert replay.basket_sha256 == first.basket_sha256
    assert changed.basket_sha256 != first.basket_sha256


def test_shadow_basket_requires_independent_score_authority_verifier() -> None:
    candidates = (
        _candidate("industry-a", 0.72),
        _candidate("industry-b", 0.91),
        _candidate("industry-c", 0.81),
    )

    with pytest.raises(
        IndustryShadowContractError,
        match="score_authority_verifier_required",
    ):
        build_industry_shadow_basket(
            candidates,
            decision_time=DECISION_TIME,
            score_authority_verifier=None,
        )


@pytest.mark.parametrize(
    ("replacement", "message"),
    (
        (
            {"score_available_at": DECISION_TIME + timedelta(seconds=1)},
            "future_activity_score",
        ),
        (
            {"score_valid_until": DECISION_TIME - timedelta(seconds=1)},
            "activity_score_expired",
        ),
    ),
)
def test_shadow_basket_rejects_future_or_expired_activity_scores(
    replacement: dict[str, object],
    message: str,
) -> None:
    changed = replace(_candidate("industry-a", 0.72), **replacement)
    candidates = (
        changed,
        _candidate("industry-b", 0.91),
        _candidate("industry-c", 0.81),
    )

    with pytest.raises(IndustryShadowContractError, match=message):
        _build(candidates)


@pytest.mark.parametrize(
    "replacement",
    (
        {"activity_score": 0.99},
        {"score_method_version": "phase1.5.v2"},
        {"score_receipt_sha256": "9" * 64},
        {"coverage_authority_receipt_sha256": "8" * 64},
    ),
)
def test_shadow_basket_rejects_score_or_coverage_authority_tampering(
    replacement: dict[str, object],
) -> None:
    original = (
        _candidate("industry-a", 0.72),
        _candidate("industry-b", 0.91),
        _candidate("industry-c", 0.81),
    )
    tampered = (replace(original[0], **replacement), original[1], original[2])
    authority = _FixtureScoreAuthorityVerifier(original)

    with pytest.raises(
        IndustryShadowContractError,
        match="score_authority_binding_mismatch",
    ):
        _build(tampered, verifier=authority)


def test_shadow_basket_rejects_missing_score_or_coverage_receipt_identity() -> None:
    base = _candidate("industry-a", 0.72)

    with pytest.raises(IndustryShadowContractError, match="score_receipt_id_invalid"):
        replace(base, score_receipt_id="")
    with pytest.raises(
        IndustryShadowContractError,
        match="coverage_authority_receipt_id_invalid",
    ):
        replace(base, coverage_authority_receipt_id="")


def test_shadow_basket_hash_binds_score_method_time_and_authority_proof() -> None:
    inputs = (
        _candidate("industry-a", 0.72),
        _candidate("industry-b", 0.91),
        _candidate("industry-c", 0.81),
    )
    original = _build(inputs)
    revised_inputs = (
        replace(
            inputs[0],
            score_valid_until=inputs[0].score_valid_until + timedelta(hours=1),
        ),
        inputs[1],
        inputs[2],
    )
    revised = _build(revised_inputs)

    assert revised_inputs[0].score_content_sha256 != inputs[0].score_content_sha256
    assert revised.basket_sha256 != original.basket_sha256
