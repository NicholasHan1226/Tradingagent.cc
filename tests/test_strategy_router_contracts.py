from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from shared.strategy_router.contracts import (
    EvidenceGroupRef,
    StyleId,
    StyleSleeveReceipt,
    StyleStance,
    StrategyRouterContractError,
)
from shared.strategy_router.shadow_router import (
    NetCandidateIntent,
    StyleRouterRunReceipt,
    route_shadow_styles,
)


UTC = timezone.utc
DECISION_TIME = datetime(2026, 7, 16, 1, 30, tzinfo=UTC)


def _sleeve(
    style_id: StyleId,
    stance: StyleStance,
    score: float,
    *,
    group_id: str,
    receipt_sha256: str,
    symbol: str = "600000.SH",
) -> StyleSleeveReceipt:
    return StyleSleeveReceipt(
        style_id=style_id,
        style_version="1",
        lifecycle="shadow",
        symbol=symbol,
        decision_time=DECISION_TIME,
        base_snapshot_sha256="a" * 64,
        champion_score_receipt_sha256="b" * 64,
        stance=stance,
        raw_style_score=score,
        calibrated_probability=None,
        evidence_groups=(
            EvidenceGroupRef(
                group_id=group_id,
                source_receipt_sha256=receipt_sha256,
            ),
        ),
        reason_codes=(f"{style_id.value}_{stance.value}",),
    )


def test_shadow_router_nets_multiple_supporting_styles_to_one_candidate_intent() -> (
    None
):
    run = route_shadow_styles(
        (
            _sleeve(
                StyleId.INDUSTRY_TREND,
                StyleStance.SUPPORT,
                0.71,
                group_id="industry_fundamental",
                receipt_sha256="1" * 64,
            ),
            _sleeve(
                StyleId.EVENT_SURPRISE,
                StyleStance.SUPPORT,
                0.64,
                group_id="event",
                receipt_sha256="2" * 64,
            ),
        )
    )

    assert run.intent is NetCandidateIntent.OPEN_CANDIDATE
    assert run.primary_style is StyleId.INDUSTRY_TREND
    assert run.supporting_styles == (StyleId.EVENT_SURPRISE,)
    assert run.unique_evidence_group_count == 2
    assert run.router_mode == "shadow_only"
    assert run.decision_eligible is False
    assert run.position_effect_allowed is False
    assert run.order_effect_allowed is False
    assert run.automatic_promotion_enabled is False
    assert run.automatic_risk_expansion_enabled is False
    assert run.live_transition_authorized is False
    assert not hasattr(run, "target_weight")
    assert not hasattr(run, "quantity")
    assert not hasattr(run, "cash_allocation")


def test_style_conflict_abstains_and_never_scales_with_vote_count() -> None:
    run = route_shadow_styles(
        (
            _sleeve(
                StyleId.INDUSTRY_TREND,
                StyleStance.SUPPORT,
                0.90,
                group_id="price_volume",
                receipt_sha256="3" * 64,
            ),
            _sleeve(
                StyleId.CROSS_MARKET_DISLOCATION,
                StyleStance.OPPOSE,
                0.80,
                group_id="external_market",
                receipt_sha256="4" * 64,
            ),
        )
    )

    assert run.intent is NetCandidateIntent.ABSTAIN
    assert run.disagreement is True
    assert run.primary_style is None
    assert run.supporting_styles == ()


def test_router_deduplicates_same_evidence_group_without_double_counting() -> None:
    shared = "5" * 64
    run = route_shadow_styles(
        (
            _sleeve(
                StyleId.INDUSTRY_TREND,
                StyleStance.SUPPORT,
                0.70,
                group_id="price_volume",
                receipt_sha256=shared,
            ),
            _sleeve(
                StyleId.CROSS_MARKET_DISLOCATION,
                StyleStance.SUPPORT,
                0.65,
                group_id="price_volume",
                receipt_sha256=shared,
            ),
        )
    )
    assert run.unique_evidence_group_count == 1

    conflicting = replace(
        _sleeve(
            StyleId.CROSS_MARKET_DISLOCATION,
            StyleStance.SUPPORT,
            0.65,
            group_id="price_volume",
            receipt_sha256=shared,
        ),
        evidence_groups=(
            EvidenceGroupRef(
                group_id="price_volume",
                source_receipt_sha256="6" * 64,
            ),
        ),
    )
    with pytest.raises(
        StrategyRouterContractError,
        match="evidence_group_receipt_conflict",
    ):
        route_shadow_styles(
            (
                _sleeve(
                    StyleId.INDUSTRY_TREND,
                    StyleStance.SUPPORT,
                    0.70,
                    group_id="price_volume",
                    receipt_sha256=shared,
                ),
                conflicting,
            )
        )


def test_router_is_order_independent_and_rejects_duplicate_style() -> None:
    sleeves = (
        _sleeve(
            StyleId.INDUSTRY_TREND,
            StyleStance.SUPPORT,
            0.71,
            group_id="industry_fundamental",
            receipt_sha256="1" * 64,
        ),
        _sleeve(
            StyleId.EVENT_SURPRISE,
            StyleStance.SUPPORT,
            0.64,
            group_id="event",
            receipt_sha256="2" * 64,
        ),
    )
    assert (
        route_shadow_styles(sleeves).run_sha256
        == route_shadow_styles(tuple(reversed(sleeves))).run_sha256
    )

    with pytest.raises(
        StrategyRouterContractError,
        match="style_receipt_duplicate",
    ):
        route_shadow_styles((sleeves[0], sleeves[0]))


def test_style_receipt_is_mainboard_only_and_probability_free() -> None:
    with pytest.raises(
        StrategyRouterContractError,
        match="style_symbol_not_mainboard",
    ):
        _sleeve(
            StyleId.EVENT_SURPRISE,
            StyleStance.SUPPORT,
            0.64,
            group_id="event",
            receipt_sha256="2" * 64,
            symbol="300001.SZ",
        )

    with pytest.raises(
        StrategyRouterContractError,
        match="calibrated_probability_forbidden",
    ):
        replace(
            _sleeve(
                StyleId.EVENT_SURPRISE,
                StyleStance.SUPPORT,
                0.64,
                group_id="event",
                receipt_sha256="2" * 64,
            ),
            calibrated_probability=0.64,
        )


def test_style_lifecycle_cannot_be_challenger_baseline_or_active() -> None:
    base = _sleeve(
        StyleId.EVENT_SURPRISE,
        StyleStance.ABSTAIN,
        0.10,
        group_id="event",
        receipt_sha256="2" * 64,
    )
    for lifecycle in ("challenger", "baseline", "paused", "active"):
        with pytest.raises(
            StrategyRouterContractError,
            match="style_lifecycle_must_be_shadow",
        ):
            replace(base, lifecycle=lifecycle)


def test_router_run_receipt_direct_construction_cannot_break_sleeve_binding() -> None:
    sleeve = _sleeve(
        StyleId.INDUSTRY_TREND,
        StyleStance.SUPPORT,
        0.71,
        group_id="industry_fundamental",
        receipt_sha256="1" * 64,
    )
    valid = route_shadow_styles((sleeve,))

    with pytest.raises(
        StrategyRouterContractError,
        match="style_router_binding_mismatch",
    ):
        replace(valid, symbol="600001.SH")
    with pytest.raises(
        StrategyRouterContractError,
        match="style_router_binding_mismatch",
    ):
        replace(valid, decision_time=datetime(2026, 7, 16, 1, 31, tzinfo=UTC))
    with pytest.raises(
        StrategyRouterContractError,
        match="style_router_binding_mismatch",
    ):
        replace(valid, unique_evidence_group_count=99)


def test_router_run_receipt_direct_construction_rejects_invalid_derived_intent() -> (
    None
):
    support = _sleeve(
        StyleId.INDUSTRY_TREND,
        StyleStance.SUPPORT,
        0.71,
        group_id="industry_fundamental",
        receipt_sha256="1" * 64,
    )
    oppose = _sleeve(
        StyleId.EVENT_SURPRISE,
        StyleStance.OPPOSE,
        0.80,
        group_id="event",
        receipt_sha256="2" * 64,
    )
    valid = route_shadow_styles((support, oppose))

    with pytest.raises(
        StrategyRouterContractError,
        match="style_router_derived_fields_invalid",
    ):
        StyleRouterRunReceipt(
            symbol=valid.symbol,
            decision_time=valid.decision_time,
            base_snapshot_sha256=valid.base_snapshot_sha256,
            champion_score_receipt_sha256=valid.champion_score_receipt_sha256,
            sleeve_receipts=valid.sleeve_receipts,
            intent=NetCandidateIntent.OPEN_CANDIDATE,
            primary_style=StyleId.INDUSTRY_TREND,
            supporting_styles=(),
            disagreement=False,
            unique_evidence_group_count=2,
        )
