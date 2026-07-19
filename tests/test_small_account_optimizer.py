from __future__ import annotations

from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone

import pytest

import shared.portfolio.small_account_optimizer as optimizer_module

from shared.execution.cost_policy import (
    ASHARE_RESEARCH_COST_POLICY_V1,
    CostPolicyError,
    conservative_planning_price,
    estimate_round_trip_cost,
    conservative_fill_price,
)
from shared.portfolio.small_account_optimizer import (
    AccountAuthoritySnapshot,
    AccountPositionSnapshot,
    CandidateAllocationInput,
    PositionReductionIntent,
    SmallAccountPolicy,
    optimize_small_account as _optimize_small_account,
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
from shared.portfolio.champion import (
    ChampionSelectionContext,
    ChampionSelectionVerification,
    FrozenChampionSpec,
    NumericPITFeatureSnapshotVerification,
    NumericPITFeatureSource,
    create_numeric_pit_feature_snapshot,
    fixture_rank_evidence,
    score_with_champion,
)
from shared.capital.market_policy import MarketPolicy


DECISION_TIME = datetime(2026, 7, 16, 6, 55, tzinfo=timezone.utc)
PRICE_OBSERVED_AT = datetime(2026, 7, 16, 6, 54, tzinfo=timezone.utc)
FILL_TIME = datetime(2026, 7, 17, 1, 31, tzinfo=timezone.utc)


class _FixtureAccountAuthorityVerifier:
    def verify(
        self,
        snapshot: AccountAuthoritySnapshot,
        *,
        decision_time: datetime,
    ) -> optimizer_module.AccountAuthorityVerification:
        return optimizer_module.AccountAuthorityVerification.create(
            snapshot=snapshot,
            verifier_id="test-fixture-account-authority",
            verifier_version="1",
            verified_at=snapshot.account_as_of,
            valid_until=FILL_TIME,
            promotion_eligible=False,
        )


_FIXTURE_ACCOUNT_AUTHORITY_VERIFIER = _FixtureAccountAuthorityVerifier()


_FIXTURE_THESIS_RISK_GROUPS = ThesisRiskGroups(
    industry="optimizer-fixture-industry",
    thesis="optimizer-fixture-thesis",
    raw_material="optimizer-fixture-raw-material",
    policy_event="optimizer-fixture-policy-event",
    crowding="optimizer-fixture-crowding",
    model_family="optimizer-fixture-model-family",
)
_FIXTURE_THESIS_RISK_POLICY = ThesisRiskPolicy(
    policy_id="optimizer-human-reviewed-thesis-risk-v1",
    reviewed_by="optimizer-test-reviewer",
    review_reference="optimizer-test-review-20260716",
    effective_at=DECISION_TIME - timedelta(days=1),
    valid_until=FILL_TIME + timedelta(days=30),
    dimension_caps=tuple(
        ThesisRiskDimensionCap(dimension=dimension, max_exposure_cny=50_000.0)
        for dimension in THESIS_RISK_DIMENSIONS
    ),
)


class _FixtureThesisRiskPolicyVerifier:
    def verify(self, policy, *, decision_time):
        if policy != _FIXTURE_THESIS_RISK_POLICY:
            raise ValueError("unexpected_thesis_risk_policy")
        return ThesisRiskPolicyVerification.create(
            policy=policy,
            verifier_id="optimizer-test-thesis-policy-verifier",
            verifier_version="1",
            verified_at=decision_time - timedelta(seconds=1),
            valid_until=FILL_TIME,
            promotion_eligible=False,
        )


class _FixtureThesisRiskExposureVerifier:
    def __init__(self, receipts) -> None:
        self._receipts = {receipt.exposure_id: receipt for receipt in receipts}

    def verify(self, receipt, *, decision_time):
        if self._receipts.get(receipt.exposure_id) != receipt:
            raise ValueError("unexpected_thesis_risk_exposure")
        return ThesisRiskExposureVerification.create(
            receipt=receipt,
            verifier_id="optimizer-test-thesis-exposure-verifier",
            verifier_version="1",
            verified_at=decision_time - timedelta(seconds=1),
            valid_until=FILL_TIME,
            promotion_eligible=False,
            authority_notional_cny=receipt.notional_cny,
            authority_binding_reference_id=receipt.binding_reference_id,
            authority_binding_sha256=receipt.binding_sha256,
        )


class _FixtureThesisRiskExposureSetVerifier:
    def __init__(self, expected) -> None:
        self._expected = expected

    def verify(self, receipt, *, decision_time):
        if receipt != self._expected:
            raise ValueError("unexpected_thesis_risk_exposure_set")
        return ThesisRiskExposureSetVerification.create(
            receipt=receipt,
            verifier_id="optimizer-test-thesis-set-verifier",
            verifier_version="1",
            verified_at=decision_time - timedelta(seconds=1),
            valid_until=FILL_TIME,
            promotion_eligible=False,
        )


class _SelectionVerifier:
    def __init__(self, expected: ChampionSelectionContext) -> None:
        self.expected = expected

    def verify(self, context, *, champion, decision_time):
        if context != self.expected:
            raise ValueError("not_current_selection")
        return ChampionSelectionVerification.create(
            context=context,
            verifier_id="optimizer-test-selection-verifier",
            verifier_version="1",
            verified_at=decision_time - timedelta(seconds=1),
            valid_until=decision_time + timedelta(minutes=1),
            current=True,
            promotion_eligible=False,
        )


class _NumericFeatureVerifier:
    def __init__(self, expected) -> None:
        self.expected = expected

    def verify(self, snapshot, *, decision_time):
        if snapshot != self.expected:
            raise ValueError("not_authoritative_feature_snapshot")
        return NumericPITFeatureSnapshotVerification.create(
            snapshot=snapshot,
            verifier_id="optimizer-test-feature-verifier",
            verifier_version="1",
            verified_at=decision_time - timedelta(seconds=1),
            valid_until=decision_time + timedelta(minutes=1),
            promotion_eligible=False,
        )


def _candidate(
    symbol: str,
    *,
    score: float = 1.0,
    price: float = 20.0,
) -> CandidateAllocationInput:
    return CandidateAllocationInput(
        symbol=symbol,
        score_evidence=fixture_rank_evidence(
            champion_selection_manifest_sha256="c" * 64,
            symbol=symbol,
            decision_time=DECISION_TIME,
            fixture_id=f"optimizer-test-{symbol}-{score}",
            source_fixture_sha256="d" * 64,
            rank_score=score,
        ),
        decision_time=DECISION_TIME,
        price_observed_at=PRICE_OBSERVED_AT,
        decision_reference_price=price,
    )


def _position(
    symbol: str,
    *,
    shares: int,
    price: float,
    sellable_shares: int | None = None,
) -> AccountPositionSnapshot:
    return AccountPositionSnapshot(
        symbol=symbol,
        total_shares=shares,
        sellable_shares=shares if sellable_shares is None else sellable_shares,
        mark_price_cny=price,
        price_observed_at=PRICE_OBSERVED_AT,
    )


def _account(
    *,
    cash: float,
    gross: float,
    positions: tuple[AccountPositionSnapshot, ...] = (),
) -> AccountAuthoritySnapshot:
    snapshot = AccountAuthoritySnapshot(
        capital_authority_id="ashare-capital-v1",
        authority_generation=1,
        account_as_of=DECISION_TIME,
        available_cash_cny=cash,
        current_gross_cny=gross,
        positions=positions,
        position_snapshot_receipt_id="positions-20260716-v1",
        position_snapshot_sha256=(
            optimizer_module.account_position_snapshot_sha256(positions)
        ),
        verification_receipt_sha256="b" * 64,
    )
    proof = _FIXTURE_ACCOUNT_AUTHORITY_VERIFIER.verify(
        snapshot,
        decision_time=DECISION_TIME,
    )
    return replace(
        snapshot,
        verification_receipt_sha256=proof.verification_receipt_sha256,
    )


def _fixture_thesis_risk_kwargs(*, candidates, account_snapshot):
    candidate_rows = tuple(candidates)
    receipts = []
    for candidate in candidate_rows:
        receipts.append(
            ThesisRiskExposureReceipt.create(
                exposure_id=f"candidate-{candidate.symbol}",
                exposure_kind="candidate",
                symbol=candidate.symbol,
                groups=_FIXTURE_THESIS_RISK_GROUPS,
                notional_cny=0.0,
                as_of=candidate.price_observed_at,
                available_at=candidate.price_observed_at,
                source_dataset_id="optimizer.fixture.thesis-risk.v1",
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
                groups=_FIXTURE_THESIS_RISK_GROUPS,
                notional_cny=position.total_shares * position.mark_price_cny,
                as_of=account_snapshot.account_as_of,
                available_at=account_snapshot.account_as_of,
                source_dataset_id="optimizer.fixture.position-risk.v1",
                source_receipt_id=account_snapshot.position_snapshot_receipt_id,
                source_lineage_sha256="3" * 64,
                source_content_sha256="4" * 64,
                binding_reference_id=account_snapshot.position_snapshot_receipt_id,
                binding_sha256=account_snapshot.position_snapshot_sha256,
            )
        )
    receipt_tuple = tuple(receipts)
    exposure_set = ThesisRiskExposureSetReceipt.create(
        exposure_set_id="optimizer-fixture-thesis-risk-book-v1",
        receipts=receipt_tuple,
        decision_time=DECISION_TIME,
        as_of=max(
            (receipt.as_of for receipt in receipt_tuple),
            default=DECISION_TIME,
        ),
        available_at=max(
            (receipt.available_at for receipt in receipt_tuple),
            default=DECISION_TIME,
        ),
        source_id="optimizer.fixture.thesis-risk-book.v1",
        source_generation=1,
        source_lineage_sha256="5" * 64,
    )
    return {
        "thesis_risk_authority": build_thesis_risk_runtime_authority(
            policy=_FIXTURE_THESIS_RISK_POLICY,
            policy_verifier=_FixtureThesisRiskPolicyVerifier(),
            exposure_receipts=receipt_tuple,
            exposure_verifier=_FixtureThesisRiskExposureVerifier(receipt_tuple),
            exposure_set_receipt=exposure_set,
            exposure_set_verifier=(_FixtureThesisRiskExposureSetVerifier(exposure_set)),
            decision_time=DECISION_TIME,
        ),
    }


def optimize_small_account(**kwargs):
    """Test-only adapter that always supplies explicit reviewed risk evidence."""

    if "thesis_risk_authority" not in kwargs:
        kwargs.update(
            _fixture_thesis_risk_kwargs(
                candidates=kwargs["candidates"],
                account_snapshot=kwargs["account_snapshot"],
            )
        )
    return _optimize_small_account(**kwargs)


def _optimize(
    *,
    candidates: list[CandidateAllocationInput],
    cash: float,
    gross: float,
    positions: tuple[AccountPositionSnapshot, ...] = (),
    reduction_intents: tuple[PositionReductionIntent, ...] = (),
):
    return optimize_small_account(
        candidates=candidates,
        account_snapshot=_account(cash=cash, gross=gross, positions=positions),
        decision_time=DECISION_TIME,
        account_authority_verifier=_FIXTURE_ACCOUNT_AUTHORITY_VERIFIER,
        reduction_intents=reduction_intents,
    )


def _reduce(
    symbol: str,
    *,
    target_shares: int,
    action: str = "reduce",
    intent_id: str = "reduce-1",
) -> PositionReductionIntent:
    return PositionReductionIntent(
        intent_id=intent_id,
        symbol=symbol,
        action=action,
        target_shares=target_shares,
        decision_time=DECISION_TIME,
    )


def test_policy_is_bound_to_current_50k_capital_authority() -> None:
    with pytest.raises(ValueError, match="initial_equity_must_equal_50000"):
        SmallAccountPolicy(initial_equity_cny=60_000)
    with pytest.raises(ValueError, match="single_name_limit_must_equal_15pct"):
        SmallAccountPolicy(single_name_max_pct=0.20)
    with pytest.raises(ValueError, match="gross_limit_must_equal_90pct"):
        SmallAccountPolicy(stock_gross_limit_pct=0.95)
    with pytest.raises(ValueError, match="max_positions_must_be_between_1_and_8"):
        SmallAccountPolicy(max_positions=9)


def test_policy_is_loaded_from_the_canonical_ashare_capital_policy() -> None:
    policy = SmallAccountPolicy.from_market_policy(MarketPolicy.load("ashare"))

    assert policy == SmallAccountPolicy()
    assert policy.max_positions == 8
    assert policy.lot_size == 100
    assert policy.minimum_economic_order_cny == 2_000.0
    assert policy.no_trade_band_cny == 1_000.0


def test_policy_rejects_non_ashare_capital_authority() -> None:
    with pytest.raises(ValueError, match="ashare_market_policy_required"):
        SmallAccountPolicy.from_market_policy(MarketPolicy.load("cn_futures"))


def test_optimizer_caps_positions_at_eight_and_binds_limit_into_plan_digest() -> None:
    candidates = [
        _candidate(f"00000{index}.SZ", score=0.5, price=10.0) for index in range(1, 10)
    ]

    default_plan = optimize_small_account(
        candidates=candidates,
        account_snapshot=_account(cash=50_000.0, gross=0.0),
        decision_time=DECISION_TIME,
        account_authority_verifier=_FIXTURE_ACCOUNT_AUTHORITY_VERIFIER,
    )
    seven_position_plan = optimize_small_account(
        candidates=candidates,
        account_snapshot=_account(cash=50_000.0, gross=0.0),
        decision_time=DECISION_TIME,
        account_authority_verifier=_FIXTURE_ACCOUNT_AUTHORITY_VERIFIER,
        policy=SmallAccountPolicy(max_positions=7),
    )

    assert default_plan.max_positions == 8
    assert sum(decision.target_shares > 0 for decision in default_plan.decisions) == 8
    assert "max_positions_limit" in default_plan.decisions[-1].reason_codes
    assert seven_position_plan.max_positions == 7
    assert (
        sum(decision.target_shares > 0 for decision in seven_position_plan.decisions)
        == 7
    )
    assert default_plan.plan_sha256 != seven_position_plan.plan_sha256


def test_integer_allocation_respects_lot_single_name_gross_and_cash() -> None:
    plan = _optimize(
        candidates=[
            _candidate("600000.SH", score=1.0, price=20.0),
            _candidate("000001.SZ", score=0.9, price=25.0),
            _candidate("600001.SH", score=0.8, price=10.0),
            _candidate("000002.SZ", score=0.7, price=12.0),
            _candidate("600002.SH", score=0.6, price=8.0),
            _candidate("000003.SZ", score=0.5, price=6.0),
            _candidate("600003.SH", score=0.4, price=5.0),
        ],
        cash=50_000,
        gross=0,
    )

    assert plan.policy_id == "ashare-small-account-50000-v1"
    assert plan.execution_scope == "simulated_research_only"
    assert plan.target_gross_cny <= 45_000
    assert plan.cash_after_orders_cny >= 0
    assert plan.undeployed_cash_cny >= 5_000
    assert plan.undeployed_reason_codes
    for decision in plan.decisions:
        assert decision.target_shares % 100 == 0
        assert decision.order_shares % 100 == 0
        assert decision.target_notional_cny <= 7_500
        assert decision.score_semantics == "uncalibrated_deterministic_rank_score"
        assert decision.edge_estimate_bps is None
        assert decision.edge_evidence_status == "not_available_uncalibrated_rank_only"
        assert decision.statistical_promotion_eligible is False
    assert (
        plan.target_gross_cny
        + plan.cash_after_orders_cny
        + plan.estimated_order_costs_cny
        + plan.estimated_adverse_fill_loss_cny
    ) == pytest.approx(50_000)


def test_high_price_lot_is_rejected_without_expanding_authority() -> None:
    plan = _optimize(
        candidates=[_candidate("600519.SH", price=80.0)],
        cash=50_000,
        gross=0,
    )

    decision = plan.decisions[0]
    assert decision.order_shares == 0
    assert decision.target_shares == 0
    assert decision.reason_codes == ("lot_not_affordable",)


def test_fixed_probe_reaches_minimum_economic_order_independent_of_rank() -> None:
    probe = _optimize(
        candidates=[_candidate("600000.SH", score=0.25, price=5.0)],
        cash=50_000,
        gross=0,
    ).decisions[0]
    assert probe.order_shares == 400
    assert probe.target_notional_cny == 2_000.0
    assert probe.reason_codes == ("allocated",)


def test_no_trade_band_prevents_probe_churn() -> None:
    unchanged = _optimize(
        candidates=[
            _candidate(
                "600000.SH",
                score=0.8,
                price=10.0,
            )
        ],
        cash=49_000,
        gross=1_000,
        positions=(_position("600000.SH", shares=100, price=10.0),),
    ).decisions[0]
    assert unchanged.order_shares == 0
    assert unchanged.target_shares == 100
    assert "inside_no_trade_band" in unchanged.reason_codes


def test_phase1_rank_only_plan_makes_no_expected_edge_claim() -> None:
    decision = _optimize(
        candidates=[
            _candidate(
                "600000.SH",
                score=1.0,
                price=20.0,
            )
        ],
        cash=50_000,
        gross=0,
    ).decisions[0]

    assert decision.order_shares > 0
    assert decision.edge_estimate_bps is None
    assert decision.statistical_promotion_eligible is False


def test_phase1_candidate_contract_cannot_self_certify_expected_edge() -> None:
    field_names = {field.name for field in fields(CandidateAllocationInput)}

    assert "expected_gross_edge_bps" not in field_names


def test_phase1_candidate_contract_does_not_accept_raw_rank_authority() -> None:
    field_names = {field.name for field in fields(CandidateAllocationInput)}

    assert "rank_score" not in field_names
    assert "score_evidence" in field_names


def test_standalone_optimizer_revalidates_external_current_champion_selection() -> None:
    champion = FrozenChampionSpec(
        champion_id="ashare-mainboard-rank-v1",
        version="1.0.0",
        feature_names=(
            "quality_score",
            "value_score",
            "momentum_score",
            "low_volatility_score",
        ),
        feature_weights=(0.25, 0.25, 0.25, 0.25),
        decision_horizon="5d",
        trained_through="2026-06-30",
    )
    caller_selection = ChampionSelectionContext(
        selection_receipt_sha256="1" * 64,
        selection_manifest_sha256="a" * 64,
        selected_artifact_sha256="b" * 64,
        selected_model_id=champion.champion_id,
        selected_model_version=champion.version,
        frozen_champion_spec_manifest_sha256=champion.manifest_sha256,
        recorded_at=DECISION_TIME - timedelta(minutes=2),
        simulation_only=True,
    )
    current_selection = replace(
        caller_selection,
        selection_receipt_sha256="2" * 64,
    )
    snapshot = create_numeric_pit_feature_snapshot(
        symbol="600000.SH",
        decision_time=DECISION_TIME,
        feature_namespace=champion.feature_namespace,
        feature_values={name: 0.5 for name in champion.feature_names},
        sources=(
            NumericPITFeatureSource(
                dataset_id="ashare.daily.v1",
                authority_receipt_id="daily-receipt-1",
                authority_receipt_sha256="c" * 64,
                data_through=DECISION_TIME - timedelta(minutes=2),
                available_at=DECISION_TIME - timedelta(minutes=1),
                source_type="canonical_dataset",
            ),
        ),
        feature_implementation_sha256="d" * 64,
        normalization_version="phase1-cross-sectional-v1",
        data_vintage_id="pit-vintage-1",
        data_lineage_sha256="e" * 64,
    )
    candidate = CandidateAllocationInput(
        symbol="600000.SH",
        score_evidence=score_with_champion(
            champion,
            feature_snapshot=snapshot,
            selection_context=caller_selection,
            selection_verifier=_SelectionVerifier(caller_selection),
            feature_snapshot_verifier=_NumericFeatureVerifier(snapshot),
            symbol="600000.SH",
            decision_time=DECISION_TIME,
        ),
        decision_time=DECISION_TIME,
        price_observed_at=PRICE_OBSERVED_AT,
        decision_reference_price=20.0,
    )

    with pytest.raises(ValueError, match="candidate_score_evidence_invalid"):
        optimize_small_account(
            candidates=(candidate,),
            account_snapshot=_account(cash=50_000.0, gross=0.0),
            decision_time=DECISION_TIME,
            account_authority_verifier=_FIXTURE_ACCOUNT_AUTHORITY_VERIFIER,
            current_champion_selection_context=current_selection,
            champion_selection_verifier=_SelectionVerifier(current_selection),
            numeric_feature_snapshot_verifier=_NumericFeatureVerifier(snapshot),
        )


def test_uncalibrated_rank_orders_candidates_but_never_scales_probe_notional() -> None:
    low_rank = _optimize(
        candidates=[_candidate("600000.SH", score=0.20, price=10.0)],
        cash=50_000,
        gross=0,
    ).decisions[0]
    high_rank = _optimize(
        candidates=[_candidate("600000.SH", score=1.00, price=10.0)],
        cash=50_000,
        gross=0,
    ).decisions[0]

    assert low_rank.target_shares == high_rank.target_shares
    assert low_rank.target_notional_cny == high_rank.target_notional_cny
    assert low_rank.target_shares > 0


def test_rank_still_determines_candidate_order_only() -> None:
    plan = _optimize(
        candidates=[
            _candidate("600000.SH", score=0.20, price=10.0),
            _candidate("000001.SZ", score=0.80, price=10.0),
        ],
        cash=50_000,
        gross=0,
    )

    assert [row.symbol for row in plan.decisions] == ["000001.SZ", "600000.SH"]
    assert {row.target_notional_cny for row in plan.decisions} == {2_000.0}


def test_candidate_ordering_and_plan_hash_are_deterministic() -> None:
    first = _optimize(
        candidates=[
            _candidate("600001.SH", score=0.8),
            _candidate("000001.SZ", score=0.8),
        ],
        cash=50_000,
        gross=0,
    )
    second = _optimize(
        candidates=list(
            reversed(
                [
                    _candidate("600001.SH", score=0.8),
                    _candidate("000001.SZ", score=0.8),
                ]
            )
        ),
        cash=50_000,
        gross=0,
    )

    assert [row.symbol for row in first.decisions] == ["000001.SZ", "600001.SH"]
    assert first.plan_sha256 == second.plan_sha256


def test_signal_and_fill_bar_must_be_separate_and_fill_is_conservative() -> None:
    fill = conservative_fill_price(
        side="buy",
        signal_bar_time=DECISION_TIME,
        fill_bar_time=FILL_TIME,
        next_bar_open=20.0,
        policy=ASHARE_RESEARCH_COST_POLICY_V1,
    )
    assert fill > 20.0

    with pytest.raises(CostPolicyError, match="fill_bar_must_follow_signal_bar"):
        conservative_fill_price(
            side="buy",
            signal_bar_time=DECISION_TIME,
            fill_bar_time=DECISION_TIME,
            next_bar_open=20.0,
            policy=ASHARE_RESEARCH_COST_POLICY_V1,
        )


def test_planning_price_uses_only_decision_time_information() -> None:
    buy = conservative_planning_price(
        side="buy",
        decision_reference_price=20.0,
        policy=ASHARE_RESEARCH_COST_POLICY_V1,
    )
    sell = conservative_planning_price(
        side="sell",
        decision_reference_price=20.0,
        policy=ASHARE_RESEARCH_COST_POLICY_V1,
    )

    assert buy > 20.0
    assert sell < 20.0
    assert "next_bar" not in conservative_planning_price.__annotations__


def test_price_observation_after_decision_is_rejected() -> None:
    with pytest.raises(ValueError, match="price_observed_after_decision"):
        CandidateAllocationInput(
            symbol="600000.SH",
            score_evidence=fixture_rank_evidence(
                champion_selection_manifest_sha256="c" * 64,
                symbol="600000.SH",
                decision_time=DECISION_TIME,
                fixture_id="optimizer-future-price-test",
                source_fixture_sha256="d" * 64,
                rank_score=1.0,
            ),
            decision_time=DECISION_TIME,
            price_observed_at=FILL_TIME,
            decision_reference_price=20.0,
        )


def test_round_trip_cost_includes_commission_floor_stamp_and_slippage() -> None:
    estimate = estimate_round_trip_cost(
        quantity=300,
        entry_reference_price=20.0,
        exit_reference_price=21.0,
        policy=ASHARE_RESEARCH_COST_POLICY_V1,
    )

    assert estimate.buy_commission_cny >= 5.0
    assert estimate.sell_commission_cny >= 5.0
    assert estimate.buy_transfer_fee_cny > 0
    assert estimate.sell_transfer_fee_cny > 0
    assert estimate.sell_stamp_duty_cny > 0
    assert estimate.slippage_cny > 0
    assert estimate.total_cost_cny == pytest.approx(
        estimate.buy_commission_cny
        + estimate.sell_commission_cny
        + estimate.buy_transfer_fee_cny
        + estimate.sell_transfer_fee_cny
        + estimate.sell_stamp_duty_cny
        + estimate.slippage_cny
    )


def test_research_cost_policy_is_bound_to_execution_reality_version() -> None:
    assert ASHARE_RESEARCH_COST_POLICY_V1.transfer_fee_rate == pytest.approx(0.00001)
    assert (
        ASHARE_RESEARCH_COST_POLICY_V1.execution_reality_model_version
        == "ashare-execution-reality-20260706-v1"
    )


def test_reduction_uses_adverse_sell_fill_and_preserves_capital_explanation() -> None:
    plan = _optimize(
        candidates=[
            _candidate(
                "600000.SH",
                score=0.4,
                price=20.0,
            )
        ],
        cash=44_000,
        gross=6_000,
        positions=(_position("600000.SH", shares=300, price=20.0),),
        reduction_intents=(_reduce("600000.SH", target_shares=100),),
    )
    decision = plan.decisions[0]
    assert decision.order_shares < 0
    assert decision.conservative_planning_price_cny < 20.0
    assert plan.estimated_order_costs_cny > 0
    assert (
        plan.target_gross_cny
        + plan.cash_after_orders_cny
        + plan.estimated_order_costs_cny
        + plan.estimated_adverse_fill_loss_cny
    ) == pytest.approx(50_000)


def test_missing_edge_never_blocks_a_risk_reducing_sell() -> None:
    decision = _optimize(
        candidates=[
            _candidate(
                "600000.SH",
                score=0.4,
                price=20.0,
            )
        ],
        cash=44_000,
        gross=6_000,
        positions=(_position("600000.SH", shares=300, price=20.0),),
        reduction_intents=(_reduce("600000.SH", target_shares=100),),
    ).decisions[0]

    assert decision.order_shares < 0
    assert decision.edge_estimate_bps is None


def test_unrealized_gain_does_not_break_the_authorized_risk_budget() -> None:
    plan = _optimize(
        candidates=[_candidate("600000.SH", score=1.0, price=50.0)],
        cash=50_000,
        gross=5_000,
        positions=(_position("600000.SH", shares=100, price=50.0),),
    )

    assert plan.current_equity_cny == 55_000
    assert plan.risk_budget_base_cny == 50_000
    assert plan.target_gross_cny <= 45_000


def test_optimizer_requires_verified_capital_and_position_identity() -> None:
    with pytest.raises(ValueError, match="capital_authority_id_mismatch"):
        AccountAuthoritySnapshot(
            capital_authority_id="retired-shared-capital",
            authority_generation=1,
            account_as_of=DECISION_TIME,
            available_cash_cny=50_000,
            current_gross_cny=0,
            positions=(),
            position_snapshot_receipt_id="positions-20260716-v1",
            position_snapshot_sha256="a" * 64,
            verification_receipt_sha256="b" * 64,
        )


def test_optimizer_requires_explicit_independent_account_authority_verifier() -> None:
    account = _account(cash=50_000.0, gross=0.0)

    with pytest.raises(TypeError, match="account_authority_verifier"):
        optimize_small_account(
            candidates=[_candidate("600000.SH")],
            account_snapshot=account,
            decision_time=DECISION_TIME,
        )


def test_optimizer_calls_independent_account_authority_verifier() -> None:
    class RejectingVerifier:
        def verify(
            self, snapshot: AccountAuthoritySnapshot, *, decision_time: datetime
        ):
            raise ValueError("detached_authority_rejected")

    with pytest.raises(ValueError, match="account_authority_verification_failed"):
        optimize_small_account(
            candidates=[_candidate("600000.SH")],
            account_snapshot=_account(cash=50_000.0, gross=0.0),
            decision_time=DECISION_TIME,
            account_authority_verifier=RejectingVerifier(),
        )


def test_position_snapshot_content_hash_binds_sellable_quantity() -> None:
    position = _position(
        "600000.SH",
        shares=300,
        sellable_shares=100,
        price=20.0,
    )

    original = optimizer_module.account_position_snapshot_sha256((position,))
    changed = optimizer_module.account_position_snapshot_sha256(
        (replace(position, sellable_shares=200),)
    )

    assert original != changed


def test_account_content_hash_binds_cash_generation_as_of_source_and_positions() -> (
    None
):
    position = _position("600000.SH", shares=100, price=20.0)
    account = _account(cash=48_000.0, gross=2_000.0, positions=(position,))
    original = optimizer_module.account_authority_content_sha256(account)

    mutations = (
        replace(account, available_cash_cny=47_999.0),
        replace(account, authority_generation=2),
        replace(account, account_as_of=FILL_TIME),
        replace(
            account,
            authority_source_class="offline_fixture",
            capital_authority_id="ashare-phase1-offline-fixture-capital-v1",
        ),
        replace(
            account,
            positions=(replace(position, sellable_shares=0),),
        ),
    )

    assert all(
        optimizer_module.account_authority_content_sha256(mutation) != original
        for mutation in mutations
    )


def test_optimizer_accepts_exact_detached_authority_proof() -> None:
    account = _account(cash=50_000.0, gross=0.0)
    account = replace(
        account,
        position_snapshot_sha256=(
            optimizer_module.account_position_snapshot_sha256(account.positions)
        ),
    )
    proof = optimizer_module.AccountAuthorityVerification.create(
        snapshot=account,
        verifier_id="test-detached-account-authority",
        verifier_version="1",
        verified_at=DECISION_TIME,
        valid_until=FILL_TIME,
        promotion_eligible=False,
    )
    account = replace(
        account,
        verification_receipt_sha256=proof.verification_receipt_sha256,
    )

    class StaticVerifier:
        def verify(
            self, snapshot: AccountAuthoritySnapshot, *, decision_time: datetime
        ):
            return proof

    plan = optimize_small_account(
        candidates=[_candidate("600000.SH")],
        account_snapshot=account,
        decision_time=DECISION_TIME,
        account_authority_verifier=StaticVerifier(),
    )

    assert plan.verification_receipt_sha256 == proof.verification_receipt_sha256


def test_detached_proof_rejects_tampered_cash_positions_sellable_generation_and_source() -> (
    None
):
    position = _position(
        "600000.SH",
        shares=100,
        sellable_shares=100,
        price=20.0,
    )
    account = _account(cash=48_000.0, gross=2_000.0, positions=(position,))
    proof = _FIXTURE_ACCOUNT_AUTHORITY_VERIFIER.verify(
        account,
        decision_time=DECISION_TIME,
    )

    class StaticVerifier:
        def verify(
            self, snapshot: AccountAuthoritySnapshot, *, decision_time: datetime
        ):
            return proof

    changed_total = replace(position, total_shares=200, sellable_shares=200)
    changed_sellable = replace(position, sellable_shares=0)
    mutations = (
        replace(account, available_cash_cny=47_999.0),
        replace(account, authority_generation=2),
        replace(
            account,
            capital_authority_id="ashare-phase1-offline-fixture-capital-v1",
            authority_source_class="offline_fixture",
        ),
        replace(account, verification_receipt_sha256="f" * 64),
        replace(
            account,
            current_gross_cny=4_000.0,
            positions=(changed_total,),
            position_snapshot_sha256=(
                optimizer_module.account_position_snapshot_sha256((changed_total,))
            ),
        ),
        replace(
            account,
            positions=(changed_sellable,),
            position_snapshot_sha256=(
                optimizer_module.account_position_snapshot_sha256((changed_sellable,))
            ),
        ),
    )

    for mutation in mutations:
        with pytest.raises(
            ValueError, match="account_authority_proof_binding_mismatch"
        ):
            optimize_small_account(
                candidates=[_candidate("600000.SH")],
                account_snapshot=mutation,
                decision_time=DECISION_TIME,
                account_authority_verifier=StaticVerifier(),
            )


def test_declared_position_hash_tampering_fails_before_verifier_can_approve() -> None:
    account = _account(cash=50_000.0, gross=0.0)

    class ForgivingVerifier:
        def verify(
            self, snapshot: AccountAuthoritySnapshot, *, decision_time: datetime
        ):
            return _FIXTURE_ACCOUNT_AUTHORITY_VERIFIER.verify(
                snapshot,
                decision_time=decision_time,
            )

    with pytest.raises(ValueError, match="position_snapshot_content_hash_mismatch"):
        optimize_small_account(
            candidates=[_candidate("600000.SH")],
            account_snapshot=replace(account, position_snapshot_sha256="f" * 64),
            decision_time=DECISION_TIME,
            account_authority_verifier=ForgivingVerifier(),
        )


def test_future_account_snapshot_fails_closed_before_authority_verification() -> None:
    account = replace(_account(cash=50_000.0, gross=0.0), account_as_of=FILL_TIME)

    with pytest.raises(ValueError, match="account_snapshot_after_decision"):
        optimize_small_account(
            candidates=[_candidate("600000.SH")],
            account_snapshot=account,
            decision_time=DECISION_TIME,
            account_authority_verifier=_FIXTURE_ACCOUNT_AUTHORITY_VERIFIER,
        )


@pytest.mark.parametrize(
    "symbol",
    ["300001.SZ", "688001.SH", "430047.BJ", "510300.SH", "UNKNOWN"],
)
def test_optimizer_rejects_out_of_scope_candidates(symbol: str) -> None:
    with pytest.raises(ValueError, match="optimizer_symbol_out_of_scope"):
        _optimize(
            candidates=[_candidate(symbol, price=10.0)],
            cash=50_000.0,
            gross=0.0,
        )


def test_optimizer_rejects_out_of_scope_existing_holding_instead_of_holding_it() -> (
    None
):
    illegal_position = _position("688001.SH", shares=100, price=10.0)

    with pytest.raises(ValueError, match="optimizer_symbol_out_of_scope"):
        _optimize(
            candidates=[],
            cash=49_000.0,
            gross=1_000.0,
            positions=(illegal_position,),
        )


def test_offline_fixture_authority_requires_explicit_source_class() -> None:
    with pytest.raises(ValueError, match="capital_authority_id_mismatch"):
        AccountAuthoritySnapshot(
            capital_authority_id="ashare-phase1-offline-fixture-capital-v1",
            authority_generation=1,
            account_as_of=DECISION_TIME,
            available_cash_cny=50_000.0,
            current_gross_cny=0.0,
            positions=(),
            position_snapshot_receipt_id="fixture-position-receipt-1",
            position_snapshot_sha256="7" * 64,
            verification_receipt_sha256="8" * 64,
        )

    snapshot = AccountAuthoritySnapshot(
        capital_authority_id="ashare-phase1-offline-fixture-capital-v1",
        authority_generation=1,
        account_as_of=DECISION_TIME,
        available_cash_cny=50_000.0,
        current_gross_cny=0.0,
        positions=(),
        position_snapshot_receipt_id="fixture-position-receipt-1",
        position_snapshot_sha256="7" * 64,
        verification_receipt_sha256="8" * 64,
        authority_source_class="offline_fixture",
    )

    assert snapshot.authority_source_class == "offline_fixture"


def test_authority_snapshot_accepts_rotated_positive_generation() -> None:
    snapshot = AccountAuthoritySnapshot(
        capital_authority_id="ashare-capital-v1",
        authority_generation=2,
        account_as_of=DECISION_TIME,
        available_cash_cny=50_000.0,
        current_gross_cny=0.0,
        positions=(),
        position_snapshot_receipt_id="positions-generation-2",
        position_snapshot_sha256="a" * 64,
        verification_receipt_sha256="b" * 64,
    )

    assert snapshot.authority_generation == 2


@pytest.mark.parametrize("generation", [0, -1, True, False, 1.0])
def test_authority_snapshot_rejects_non_positive_native_generation(
    generation: object,
) -> None:
    with pytest.raises(ValueError, match="authority_generation_invalid"):
        AccountAuthoritySnapshot(
            capital_authority_id="ashare-capital-v1",
            authority_generation=generation,  # type: ignore[arg-type]
            account_as_of=DECISION_TIME,
            available_cash_cny=50_000.0,
            current_gross_cny=0.0,
            positions=(),
            position_snapshot_receipt_id="positions-invalid-generation",
            position_snapshot_sha256="a" * 64,
            verification_receipt_sha256="b" * 64,
        )


@pytest.mark.parametrize(
    ("authority_id", "source_class"),
    [
        ("ashare-capital-v1", "offline_fixture"),
        ("ashare-phase1-offline-fixture-capital-v1", "canonical_authority"),
        ("ashare-fixture-v1", "offline_fixture"),
        ("ashare-phase1-offline-fixture-capital-v1", "unknown"),
    ],
)
def test_authority_source_class_and_id_cannot_be_mixed(
    authority_id: str,
    source_class: str,
) -> None:
    with pytest.raises(ValueError):
        AccountAuthoritySnapshot(
            capital_authority_id=authority_id,
            authority_generation=1,
            account_as_of=DECISION_TIME,
            available_cash_cny=50_000.0,
            current_gross_cny=0.0,
            positions=(),
            position_snapshot_receipt_id="fixture-position-receipt-1",
            position_snapshot_sha256="7" * 64,
            verification_receipt_sha256="8" * 64,
            authority_source_class=source_class,
        )


def test_optimizer_rejects_cash_gross_and_position_mismatch() -> None:
    with pytest.raises(ValueError, match="declared_gross_position_mismatch"):
        AccountAuthoritySnapshot(
            capital_authority_id="ashare-capital-v1",
            authority_generation=1,
            account_as_of=DECISION_TIME,
            available_cash_cny=44_000,
            current_gross_cny=5_000,
            positions=(_position("600000.SH", shares=300, price=20.0),),
            position_snapshot_receipt_id="positions-20260716-v1",
            position_snapshot_sha256="a" * 64,
            verification_receipt_sha256="b" * 64,
        )


def test_t_plus_one_unsellable_quantity_cannot_be_sold() -> None:
    account = _account(
        cash=44_000,
        gross=6_000,
        positions=(
            _position(
                "600000.SH",
                shares=300,
                sellable_shares=100,
                price=20.0,
            ),
        ),
    )

    plan = optimize_small_account(
        candidates=[_candidate("600000.SH", score=0.0, price=20.0)],
        account_snapshot=account,
        decision_time=DECISION_TIME,
        account_authority_verifier=_FIXTURE_ACCOUNT_AUTHORITY_VERIFIER,
        reduction_intents=(_reduce("600000.SH", target_shares=0, action="exit"),),
    )

    decision = plan.decisions[0]
    assert decision.current_shares == 300
    assert decision.sellable_shares == 100
    assert decision.order_shares == -100
    assert decision.target_shares == 200
    assert "t1_sellable_limit" in decision.reason_codes
    assert decision.reduction_intent_id == "reduce-1"
    assert decision.reduction_intent_action == "exit"


def test_low_rank_candidate_cannot_create_sell_without_explicit_reduction() -> None:
    decision = _optimize(
        candidates=[_candidate("600000.SH", score=0.0, price=20.0)],
        cash=44_000,
        gross=6_000,
        positions=(_position("600000.SH", shares=300, price=20.0),),
    ).decisions[0]

    assert decision.order_shares == 0
    assert decision.target_shares == 300
    assert decision.reduction_intent_id is None
    assert "reduction_requires_explicit_intent" in decision.reason_codes


def test_explicit_reduction_intent_is_the_only_sell_authority() -> None:
    decision = _optimize(
        candidates=[_candidate("600000.SH", score=1.0, price=20.0)],
        cash=44_000,
        gross=6_000,
        positions=(_position("600000.SH", shares=300, price=20.0),),
        reduction_intents=(
            _reduce(
                "600000.SH",
                target_shares=100,
                intent_id="explicit-reduce-600000",
            ),
        ),
    ).decisions[0]

    assert decision.order_shares == -200
    assert decision.target_shares == 100
    assert decision.reduction_intent_id == "explicit-reduce-600000"
    assert decision.reduction_intent_action == "reduce"
    assert "explicit_reduction_intent" in decision.reason_codes


@pytest.mark.parametrize(
    ("target_shares", "expected_order_shares", "expected_target_shares"),
    [
        (130, 0, 150),
        (100, -50, 100),
        (50, -100, 50),
        (0, -150, 0),
    ],
)
def test_optimizer_fails_closed_on_illegal_odd_lot_sell_without_rewriting_order(
    target_shares: int,
    expected_order_shares: int,
    expected_target_shares: int,
) -> None:
    action = "exit" if target_shares == 0 else "reduce"
    decision = _optimize(
        candidates=[_candidate("600000.SH", score=1.0, price=10.0)],
        cash=48_500,
        gross=1_500,
        positions=(_position("600000.SH", shares=150, price=10.0),),
        reduction_intents=(
            _reduce("600000.SH", target_shares=target_shares, action=action),
        ),
    ).decisions[0]

    assert decision.order_shares == expected_order_shares
    assert decision.target_shares == expected_target_shares
    if target_shares == 130:
        assert decision.reason_codes == (
            "explicit_reduction_intent",
            "ashare_odd_lot_sell_quantity_invalid",
        )
    else:
        assert "allocated" in decision.reason_codes


def test_existing_holding_absent_from_candidates_is_held_without_exit_intent() -> None:
    account = _account(
        cash=48_000,
        gross=2_000,
        positions=(
            _position(
                "600000.SH",
                shares=100,
                sellable_shares=100,
                price=20.0,
            ),
        ),
    )

    plan = optimize_small_account(
        candidates=[],
        account_snapshot=account,
        decision_time=DECISION_TIME,
        account_authority_verifier=_FIXTURE_ACCOUNT_AUTHORITY_VERIFIER,
    )

    assert [decision.symbol for decision in plan.decisions] == ["600000.SH"]
    assert plan.decisions[0].order_shares == 0
    assert plan.decisions[0].target_shares == 100
    assert "not_in_candidate_set_hold" in plan.decisions[0].reason_codes
    assert plan.capital_authority_id == "ashare-capital-v1"
    assert plan.authority_generation == 1
    assert plan.position_snapshot_sha256 == (
        optimizer_module.account_position_snapshot_sha256(account.positions)
    )
