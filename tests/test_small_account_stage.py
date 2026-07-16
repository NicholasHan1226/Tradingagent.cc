from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

import shared.portfolio.small_account_optimizer as optimizer_module
import shared.portfolio.thesis_risk as thesis_risk_module
from shared.portfolio.small_account_optimizer import (
    AccountAuthoritySnapshot,
    AccountPositionSnapshot,
    CandidateAllocationInput,
    PositionReductionIntent,
    optimize_small_account as _optimize_small_account,
)
from shared.portfolio.champion import (
    ChampionScoreReceipt,
    ChampionSelectionContext,
    FrozenChampionSpec,
    fixture_rank_evidence,
)
from shared.runtime.day_loop import StageRequest, _small_account_plan_contract
from shared.runtime.run_bundle import (
    ComponentIdentity,
    RunBundle,
    RunContext,
    RunStage,
    STAGE_ORDER,
    StageReceipt,
)
from shared.runtime.small_account_stage import (
    SmallAccountDecisionStagePort,
    SmallAccountStageContractError,
)
from tests._champion_authority_fixture import (
    FrozenChampionSelectionVerifier,
    FrozenNumericPITFeatureSnapshotVerifier,
    build_champion_authority_fixture,
)
from tests._thesis_risk_fixture import build_thesis_risk_fixture


DECISION_TIME = datetime(2026, 7, 16, 6, 55, tzinfo=timezone.utc)
PRICE_TIME = datetime(2026, 7, 16, 6, 54, tzinfo=timezone.utc)
AUTHORITY_VALID_UNTIL = datetime(2026, 7, 16, 7, 55, tzinfo=timezone.utc)
DECISION_IDENTITY = ComponentIdentity(
    stage=RunStage.DECISION_READY,
    component_id="small-account-decision-stage",
    version="1",
    artifact_sha256="4" * 64,
)


class _FixtureAccountAuthorityVerifier:
    def verify(
        self,
        snapshot: AccountAuthoritySnapshot,
        *,
        decision_time: datetime,
    ) -> optimizer_module.AccountAuthorityVerification:
        return optimizer_module.AccountAuthorityVerification.create(
            snapshot=snapshot,
            verifier_id="stage-test-fixture-account-authority",
            verifier_version="1",
            verified_at=snapshot.account_as_of,
            valid_until=AUTHORITY_VALID_UNTIL,
            promotion_eligible=False,
        )


_FIXTURE_ACCOUNT_AUTHORITY_VERIFIER = _FixtureAccountAuthorityVerifier()


def _digest(character: str) -> str:
    return character * 64


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _champion() -> FrozenChampionSpec:
    return FrozenChampionSpec(
        champion_id="small-account-stage-test",
        version="1",
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


def _selection_context() -> ChampionSelectionContext:
    champion = _champion()
    return ChampionSelectionContext(
        selection_receipt_sha256="1" * 64,
        selection_manifest_sha256="c" * 64,
        selected_artifact_sha256="2" * 64,
        selected_model_id=champion.champion_id,
        selected_model_version=champion.version,
        frozen_champion_spec_manifest_sha256=champion.manifest_sha256,
        recorded_at=DECISION_TIME - timedelta(minutes=2),
        simulation_only=True,
    )


def _candidate(
    symbol: str = "000001.SZ",
    *,
    score: float = 1.0,
    price: float = 10.0,
    selection_manifest_sha256: str = "c" * 64,
):
    champion = _champion()
    authority = build_champion_authority_fixture(
        champion=champion,
        symbol=symbol,
        decision_time=DECISION_TIME,
        feature_values={
            "quality_score": score,
            "value_score": score,
            "momentum_score": score,
            "low_volatility_score": score,
        },
        selection_manifest_sha256=selection_manifest_sha256,
        source_id=f"stage-data-{symbol}",
    )
    return CandidateAllocationInput(
        symbol=symbol,
        score_evidence=authority.score_receipt,
        decision_time=DECISION_TIME,
        price_observed_at=PRICE_TIME,
        decision_reference_price=price,
    )


def _position(
    symbol: str = "000001.SZ",
    *,
    total: int = 300,
    sellable: int = 100,
    price: float = 10.0,
) -> AccountPositionSnapshot:
    return AccountPositionSnapshot(
        symbol=symbol,
        total_shares=total,
        sellable_shares=sellable,
        mark_price_cny=price,
        price_observed_at=PRICE_TIME,
    )


def _account(
    *,
    cash: float = 50_000.0,
    positions: tuple[AccountPositionSnapshot, ...] = (),
    position_hash: str | None = None,
    authority_id: str = "ashare-capital-v1",
    authority_source_class: str = "canonical_authority",
    authority_generation: int = 1,
) -> AccountAuthoritySnapshot:
    gross = sum(
        position.total_shares * position.mark_price_cny for position in positions
    )
    snapshot = AccountAuthoritySnapshot(
        capital_authority_id=authority_id,
        authority_generation=authority_generation,
        account_as_of=DECISION_TIME,
        available_cash_cny=cash,
        current_gross_cny=gross,
        positions=positions,
        position_snapshot_receipt_id="positions-authority-1",
        position_snapshot_sha256=(
            position_hash
            or optimizer_module.account_position_snapshot_sha256(positions)
        ),
        verification_receipt_sha256="8" * 64,
        authority_source_class=authority_source_class,
    )
    proof = _FIXTURE_ACCOUNT_AUTHORITY_VERIFIER.verify(
        snapshot,
        decision_time=DECISION_TIME,
    )
    return replace(
        snapshot,
        verification_receipt_sha256=proof.verification_receipt_sha256,
    )


def optimize_small_account(**kwargs):
    if "thesis_risk_authority" not in kwargs:
        kwargs.update(
            build_thesis_risk_fixture(
                candidates=tuple(kwargs["candidates"]),
                account_snapshot=kwargs["account_snapshot"],
                decision_time=kwargs["decision_time"],
            )
        )
    return _optimize_small_account(**kwargs)


def _bundle(
    *,
    position_authority_valid: bool = False,
    authority_generation: int = 1,
) -> RunBundle:
    context = RunContext(
        trade_date="2026-07-16",
        decision_as_of=DECISION_TIME,
        market="ashare",
        authority_id="ashare-capital-v1",
        authority_generation=authority_generation,
        execution_lineage="ashare-sim-stage-test-v1",
        account_type="simulated",
        real_trading_enabled=False,
        champion_manifest_sha256="c" * 64,
    )
    components = tuple(
        DECISION_IDENTITY
        if stage is RunStage.DECISION_READY
        else ComponentIdentity(
            stage=stage,
            component_id=f"stage-{stage.value}",
            version="1",
            artifact_sha256=f"{index + 1:x}" * 64,
        )
        for index, stage in enumerate(STAGE_ORDER)
    )
    return RunBundle(
        context=context,
        components=components,
        position_authority_valid=position_authority_valid,
    )


def _decision_ready_bundle(
    *,
    position_authority_valid: bool = True,
    authority_generation: int = 1,
) -> RunBundle:
    bundle = _bundle(authority_generation=authority_generation)
    prior_payloads = {
        RunStage.PREOPEN: {
            "market": "ashare",
            "account_type": "simulated",
            "real_trading_enabled": False,
        },
        RunStage.EVIDENCE_READY: {
            "decision_as_of": DECISION_TIME.isoformat(),
        },
        RunStage.UNIVERSE_READY: {
            "tradable_symbols": ["000001.SZ"],
            "feasible_symbols": ["000001.SZ"],
        },
    }
    for index, stage in enumerate(
        (RunStage.PREOPEN, RunStage.EVIDENCE_READY, RunStage.UNIVERSE_READY)
    ):
        receipt = StageReceipt.create(
            stage=stage,
            status="completed",
            idempotency_key=f"{index + 1:x}" * 64,
            component=bundle.component_for(stage),
            input_bundle_sha256=bundle.bundle_sha256,
            payload=prior_payloads[stage],
            reason_codes=(),
        )
        bundle = bundle.append(
            receipt,
            stop_new_risk=False,
            position_authority_valid=(
                position_authority_valid if stage is RunStage.PREOPEN else None
            ),
            block_reasons=(),
            permitted_order_ids=None,
        )
    return bundle


def _request(bundle: RunBundle) -> StageRequest:
    return StageRequest(
        run_id=bundle.run_id,
        stage=RunStage.DECISION_READY,
        idempotency_key="d" * 64,
        input_bundle_sha256=bundle.bundle_sha256,
        bundle=bundle,
        allowed_actions=("open", "increase", "reduce", "exit", "hold"),
        permitted_order_ids=(),
    )


def _port(
    *,
    account: AccountAuthoritySnapshot,
    candidates: tuple[CandidateAllocationInput, ...],
    reduction_intents: tuple[PositionReductionIntent, ...] = (),
    runtime_environment: str = "canonical_simulated",
    promotion_eligible: bool = False,
    thesis_risk_authority_transform=None,
) -> SmallAccountDecisionStagePort:
    candidate_rows = candidates
    if runtime_environment == "local_candidate":
        candidate_rows = tuple(
            replace(
                candidate,
                score_evidence=fixture_rank_evidence(
                    champion_selection_manifest_sha256="c" * 64,
                    symbol=candidate.symbol,
                    decision_time=candidate.decision_time,
                    fixture_id=f"small-account-stage-{candidate.symbol}",
                    source_fixture_sha256="e" * 64,
                    rank_score=candidate.rank_score,
                ),
            )
            for candidate in candidates
        )
    authority_kwargs = {}
    if runtime_environment == "canonical_simulated":
        authority_kwargs = _champion_authority_kwargs(candidate_rows)
    thesis_risk_kwargs = build_thesis_risk_fixture(
        candidates=candidate_rows,
        account_snapshot=account,
        decision_time=DECISION_TIME,
    )
    if thesis_risk_authority_transform is not None:
        thesis_risk_kwargs["thesis_risk_authority"] = thesis_risk_authority_transform(
            thesis_risk_kwargs["thesis_risk_authority"]
        )
    return SmallAccountDecisionStagePort(
        identity=DECISION_IDENTITY,
        account_snapshot=account,
        candidates=candidate_rows,
        reduction_intents=reduction_intents,
        decision_time=DECISION_TIME,
        account_authority_verifier=_FIXTURE_ACCOUNT_AUTHORITY_VERIFIER,
        **thesis_risk_kwargs,
        runtime_environment=runtime_environment,
        promotion_eligible=promotion_eligible,
        **authority_kwargs,
    )


def _with_promotable_nested_thesis_proofs(authority):
    policy_proof = thesis_risk_module.ThesisRiskPolicyVerification.create(
        policy=authority.policy,
        verifier_id="adversarial-promotable-policy-proof",
        verifier_version="1",
        verified_at=DECISION_TIME - timedelta(seconds=1),
        valid_until=DECISION_TIME + timedelta(hours=1),
        promotion_eligible=True,
    )
    exposure_proofs = tuple(
        thesis_risk_module.ThesisRiskExposureVerification.create(
            receipt=receipt,
            verifier_id="adversarial-promotable-exposure-proof",
            verifier_version="1",
            verified_at=DECISION_TIME - timedelta(seconds=1),
            valid_until=DECISION_TIME + timedelta(hours=1),
            promotion_eligible=True,
            authority_notional_cny=receipt.notional_cny,
            authority_binding_reference_id=receipt.binding_reference_id,
            authority_binding_sha256=receipt.binding_sha256,
        )
        for receipt in authority.exposure_receipts
    )
    exposure_set_proof = thesis_risk_module.ThesisRiskExposureSetVerification.create(
        receipt=authority.exposure_set_receipt,
        verifier_id="adversarial-promotable-set-proof",
        verifier_version="1",
        verified_at=DECISION_TIME - timedelta(seconds=1),
        valid_until=DECISION_TIME + timedelta(hours=1),
        promotion_eligible=True,
    )
    payload = thesis_risk_module._runtime_authority_content_payload(
        decision_time=authority.decision_time,
        policy=authority.policy,
        policy_proof=policy_proof,
        exposure_receipts=authority.exposure_receipts,
        exposure_proofs=exposure_proofs,
        exposure_set_receipt=authority.exposure_set_receipt,
        exposure_set_proof=exposure_set_proof,
        initial_exposures=authority.initial_group_exposures,
    )
    return thesis_risk_module.ThesisRiskRuntimeAuthority(
        decision_time=authority.decision_time,
        policy=authority.policy,
        policy_proof=policy_proof,
        exposure_receipts=authority.exposure_receipts,
        exposure_proofs=exposure_proofs,
        exposure_set_receipt=authority.exposure_set_receipt,
        exposure_set_proof=exposure_set_proof,
        initial_group_exposures=authority.initial_group_exposures,
        authority_sha256=thesis_risk_module._canonical_sha256(payload),
    )


def _champion_authority_kwargs(
    candidates: tuple[CandidateAllocationInput, ...],
) -> dict[str, object]:
    selection_context = _selection_context()
    snapshots = tuple(
        candidate.score_evidence.feature_snapshot
        for candidate in candidates
        if isinstance(candidate.score_evidence, ChampionScoreReceipt)
    )
    return {
        "current_champion_selection_context": selection_context,
        "champion_selection_verifier": FrozenChampionSelectionVerifier(
            selection_context
        ),
        "numeric_feature_snapshot_verifier": (
            FrozenNumericPITFeatureSnapshotVerifier(snapshots)
        ),
    }


def test_stage_requires_explicit_independent_account_authority_verifier() -> None:
    with pytest.raises(TypeError, match="account_authority_verifier"):
        SmallAccountDecisionStagePort(
            identity=DECISION_IDENTITY,
            account_snapshot=_account(),
            candidates=(_candidate(),),
            decision_time=DECISION_TIME,
        )


def test_canonical_stage_requires_external_current_champion_authority() -> None:
    account = _account()
    candidates = (_candidate(),)
    with pytest.raises(
        SmallAccountStageContractError,
        match="current_champion_selection_authority_required",
    ):
        SmallAccountDecisionStagePort(
            identity=DECISION_IDENTITY,
            account_snapshot=account,
            candidates=candidates,
            decision_time=DECISION_TIME,
            account_authority_verifier=_FIXTURE_ACCOUNT_AUTHORITY_VERIFIER,
            **build_thesis_risk_fixture(
                candidates=candidates,
                account_snapshot=account,
                decision_time=DECISION_TIME,
            ),
        )


def test_canonical_stage_rejects_score_receipt_from_noncurrent_champion() -> None:
    port = _port(
        account=_account(),
        candidates=(_candidate(selection_manifest_sha256="f" * 64),),
    )

    with pytest.raises(
        SmallAccountStageContractError,
        match="candidate_score_receipt_invalid",
    ):
        port.execute(_request(_decision_ready_bundle()))


def test_canonical_stage_rejects_offline_fixture_rank_evidence() -> None:
    candidate = _candidate()
    fixture_candidate = replace(
        candidate,
        score_evidence=fixture_rank_evidence(
            champion_selection_manifest_sha256="c" * 64,
            symbol=candidate.symbol,
            decision_time=candidate.decision_time,
            fixture_id="cannot-enter-canonical-stage",
            source_fixture_sha256="e" * 64,
            rank_score=candidate.rank_score,
        ),
    )
    port = _port(account=_account(), candidates=(fixture_candidate,))

    with pytest.raises(
        SmallAccountStageContractError,
        match="candidate_score_receipt_invalid",
    ):
        port.execute(_request(_decision_ready_bundle()))


def test_offline_fixture_account_requires_local_nonpromotion_stage_scope() -> None:
    account = _account(
        authority_id="ashare-phase1-offline-fixture-capital-v1",
        authority_source_class="offline_fixture",
    )

    with pytest.raises(
        SmallAccountStageContractError,
        match="offline_fixture_runtime_environment_invalid",
    ):
        _port(account=account, candidates=(_candidate(),))

    port = _port(
        account=account,
        candidates=(_candidate(),),
        runtime_environment="local_candidate",
        promotion_eligible=False,
    )

    assert port.account_authority_source_class == "offline_fixture"
    assert port.runtime_environment == "local_candidate"
    assert port.promotion_eligible is False


def test_local_candidate_stage_rejects_canonical_or_promotion_authority() -> None:
    with pytest.raises(
        SmallAccountStageContractError,
        match="local_candidate_requires_offline_fixture_authority",
    ):
        _port(
            account=_account(),
            candidates=(_candidate(),),
            runtime_environment="local_candidate",
        )

    with pytest.raises(
        SmallAccountStageContractError,
        match="small_account_stage_promotion_forbidden",
    ):
        _port(
            account=_account(
                authority_id="ashare-phase1-offline-fixture-capital-v1",
                authority_source_class="offline_fixture",
            ),
            candidates=(_candidate(),),
            runtime_environment="local_candidate",
            promotion_eligible=True,
        )


def test_stage_rejects_promotable_nested_thesis_proofs() -> None:
    with pytest.raises(
        SmallAccountStageContractError,
        match="thesis_risk_proof_promotion_forbidden",
    ):
        _port(
            account=_account(),
            candidates=(_candidate(),),
            thesis_risk_authority_transform=(_with_promotable_nested_thesis_proofs),
        )


def test_stage_binds_rotated_generation_to_matching_run_context() -> None:
    port = _port(
        account=_account(authority_generation=2),
        candidates=(_candidate(),),
    )

    payload = port.execute(
        _request(_decision_ready_bundle(authority_generation=2))
    ).payload

    assert payload["small_account_plan"]["authority_generation"] == 2

    with pytest.raises(
        SmallAccountStageContractError,
        match="capital_authority_mismatch",
    ):
        _port(
            account=_account(authority_generation=2),
            candidates=(_candidate(),),
        ).execute(_request(_decision_ready_bundle(authority_generation=1)))


def test_stage_translates_optimizer_output_to_day_loop_plan_contract() -> None:
    account = _account()
    candidates = (_candidate(),)
    optimizer_plan = optimize_small_account(
        candidates=candidates,
        account_snapshot=account,
        decision_time=DECISION_TIME,
        account_authority_verifier=_FIXTURE_ACCOUNT_AUTHORITY_VERIFIER,
        **_champion_authority_kwargs(candidates),
    )
    port = _port(account=account, candidates=candidates)

    bundle = _decision_ready_bundle()
    payload = port.execute(_request(bundle)).payload

    assert set(payload) == {
        "champion_manifest_sha256",
        "optimizer_policy_version",
        "optimizer_plan_sha256",
        "small_account_plan",
        "decisions",
    }
    assert payload["optimizer_plan_sha256"] == optimizer_plan.plan_sha256
    plan = payload["small_account_plan"]
    assert set(plan) == {
        "schema_version",
        "policy_id",
        "cost_policy_id",
        "capital_authority_id",
        "authority_generation",
        "account_as_of",
        "position_snapshot_receipt_id",
        "position_snapshot_sha256",
        "verification_receipt_sha256",
        "current_equity_cny",
        "risk_budget_base_cny",
        "max_positions",
        "starting_available_cash_cny",
        "starting_gross_cny",
        "target_gross_cny",
        "cash_after_orders_cny",
        "plan_decisions",
        "thesis_risk_policy_id",
        "thesis_risk_policy_sha256",
        "thesis_risk_policy_proof_sha256",
        "thesis_risk_exposure_receipt_sha256s",
        "thesis_risk_exposure_proof_sha256s",
        "thesis_risk_exposure_set_id",
        "thesis_risk_exposure_set_sha256",
        "thesis_risk_exposure_set_proof_sha256",
        "thesis_risk_runtime_authority_sha256",
        "thesis_risk_initial_group_exposures",
        "thesis_risk_final_group_exposures",
        "plan_sha256",
    }
    unsigned = dict(plan)
    unsigned.pop("plan_sha256")
    assert plan["plan_sha256"] == _canonical_sha256(unsigned)
    assert plan["max_positions"] == optimizer_plan.max_positions == 8
    row = plan["plan_decisions"][0]
    assert set(row) == {
        "decision_id",
        "symbol",
        "action",
        "current_shares",
        "sellable_shares",
        "target_shares",
        "order_quantity",
        "valuation_price_cny",
        "reservation_price_cny",
        "estimated_order_cost_cny",
        "target_notional_cny",
        "reason_codes",
        "thesis_risk_evaluated_order_shares",
        "thesis_risk_group_effects",
    }
    assert len(row["thesis_risk_group_effects"]) == 6
    optimized = optimizer_plan.decisions[0]
    assert row["order_quantity"] == abs(optimized.order_shares)
    assert row["reservation_price_cny"] == optimized.conservative_planning_price_cny
    assert row["estimated_order_cost_cny"] == optimized.estimated_order_cost_cny
    assert row["sellable_shares"] == optimized.sellable_shares
    assert payload["decisions"][0]["action"] == "open"
    assert payload["decisions"][0]["requested_notional_cny"] == pytest.approx(
        row["order_quantity"] * row["reservation_price_cny"]
    )
    reasons, _, plan_sha, _ = _small_account_plan_contract(
        bundle,
        payload,
        thesis_risk_authority=port.thesis_risk_authority,
    )
    assert reasons == ()
    assert plan_sha == plan["plan_sha256"]


def test_stage_uses_no_trade_band_for_existing_probe_position() -> None:
    position = _position(total=100, sellable=100, price=10.0)
    account = _account(cash=35_000.0, positions=(position,))
    candidates = (_candidate(score=1.0, price=10.0),)
    optimizer_plan = optimize_small_account(
        candidates=candidates,
        account_snapshot=account,
        decision_time=DECISION_TIME,
        account_authority_verifier=_FIXTURE_ACCOUNT_AUTHORITY_VERIFIER,
        **_champion_authority_kwargs(candidates),
    )

    port = _port(account=account, candidates=candidates)
    payload = port.execute(_request(_decision_ready_bundle())).payload
    plan = payload["small_account_plan"]

    assert payload["decisions"][0]["action"] == "hold"
    assert payload["decisions"][0]["requested_notional_cny"] == 0.0
    assert plan["current_equity_cny"] == optimizer_plan.current_equity_cny == 36_000.0
    assert (
        plan["risk_budget_base_cny"]
        == optimizer_plan.risk_budget_base_cny
        == (36_000.0)
    )
    assert plan["starting_gross_cny"] == account.current_gross_cny == 1_000.0
    assert plan["target_gross_cny"] == optimizer_plan.target_gross_cny
    assert plan["cash_after_orders_cny"] == optimizer_plan.cash_after_orders_cny
    assert plan["plan_decisions"][0]["target_notional_cny"] == (
        optimizer_plan.decisions[0].target_notional_cny
    )
    assert plan["plan_decisions"][0]["valuation_price_cny"] == 10.0
    assert plan["plan_decisions"][0]["reservation_price_cny"] == 10.0
    reasons, _, _, _ = _small_account_plan_contract(
        _decision_ready_bundle(),
        payload,
        thesis_risk_authority=port.thesis_risk_authority,
    )
    assert reasons == ()


def test_stage_preserves_position_and_t1_limits_for_explicit_exit() -> None:
    position = _position(total=300, sellable=100)
    account = _account(cash=47_000.0, positions=(position,))
    exit_intent = PositionReductionIntent(
        intent_id="exit-position-1",
        symbol=position.symbol,
        action="exit",
        target_shares=0,
        decision_time=DECISION_TIME,
    )
    port = _port(
        account=account,
        candidates=(_candidate(score=0.0),),
        reduction_intents=(exit_intent,),
    )

    bundle = _decision_ready_bundle()
    payload = port.execute(_request(bundle)).payload
    row = payload["small_account_plan"]["plan_decisions"][0]
    decision = payload["decisions"][0]

    assert row["current_shares"] == 300
    assert row["sellable_shares"] == 100
    assert row["order_quantity"] == 100
    assert row["target_shares"] == 200
    assert decision["action"] == "reduce"
    assert decision["source_reduction_intent_id"] == "exit-position-1"
    assert decision["source_reduction_intent_action"] == "exit"
    reasons, _, _, _ = _small_account_plan_contract(
        bundle,
        payload,
        thesis_risk_authority=port.thesis_risk_authority,
    )
    assert reasons == ()


def test_stage_holds_position_absent_from_candidate_set() -> None:
    position = _position(total=100, sellable=100)
    account = _account(cash=49_000.0, positions=(position,))

    payload = (
        _port(account=account, candidates=())
        .execute(_request(_decision_ready_bundle()))
        .payload
    )

    row = payload["small_account_plan"]["plan_decisions"][0]
    assert row["action"] == "hold"
    assert row["order_quantity"] == 0
    assert row["target_shares"] == 100


def test_stage_suppresses_new_risk_but_preserves_explicit_reduction() -> None:
    position = _position(symbol="600000.SH", total=300, sellable=100)
    account = _account(cash=47_000.0, positions=(position,))
    exit_intent = PositionReductionIntent(
        intent_id="blocked-run-exit-1",
        symbol=position.symbol,
        action="exit",
        target_shares=0,
        decision_time=DECISION_TIME,
    )
    blocked_bundle = replace(
        _decision_ready_bundle(),
        stop_new_risk=True,
        block_reasons=("required_dataset_not_accepted",),
    )
    request = replace(
        _request(blocked_bundle),
        allowed_actions=("reduce", "exit", "hold"),
    )

    port = _port(
        account=account,
        candidates=(_candidate(),),
        reduction_intents=(exit_intent,),
    )
    payload = port.execute(request).payload

    assert {row["action"] for row in payload["decisions"]}.isdisjoint(
        {"open", "increase"}
    )
    decisions = {row["symbol"]: row for row in payload["decisions"]}
    assert decisions["000001.SZ"]["action"] == "hold"
    assert decisions["000001.SZ"]["requested_notional_cny"] == 0.0
    expected_reduction = {
        "decision_id": decisions["600000.SH"]["decision_id"],
        "decision_cluster_id": "small-account-position-600000.SH",
        "symbol": "600000.SH",
        "action": "reduce",
        "target_shares": 200,
        "requested_notional_cny": 996.5,
        "score_semantics": "uncalibrated_deterministic_rank_score",
        "rank_score": 0.0,
        "score_receipt_sha256": None,
        "score_evidence_class": "existing_position_hold",
        "sizing_method": "fixed_minimum_economic_probe_v1",
        "source_reduction_intent_id": "blocked-run-exit-1",
        "source_reduction_intent_action": "exit",
    }
    assert {
        key: decisions["600000.SH"][key] for key in expected_reduction
    } == expected_reduction
    assert len(decisions["600000.SH"]["thesis_risk_group_effects"]) == 6
    reasons, _, _, _ = _small_account_plan_contract(
        blocked_bundle,
        payload,
        thesis_risk_authority=port.thesis_risk_authority,
    )
    assert reasons == ()


def test_stage_emits_holds_when_position_authority_is_not_valid() -> None:
    position = _position(total=100, sellable=100)
    account = _account(cash=49_000.0, positions=(position,))
    blocked_bundle = replace(
        _decision_ready_bundle(position_authority_valid=False),
        stop_new_risk=True,
        block_reasons=("position_authority_invalid",),
    )
    request = replace(_request(blocked_bundle), allowed_actions=("hold",))

    payload = (
        _port(
            account=account,
            candidates=(_candidate(),),
            reduction_intents=(
                PositionReductionIntent(
                    intent_id="unsafe-exit-1",
                    symbol=position.symbol,
                    action="exit",
                    target_shares=0,
                    decision_time=DECISION_TIME,
                ),
            ),
        )
        .execute(request)
        .payload
    )

    assert [row["action"] for row in payload["decisions"]] == ["hold"]
    assert payload["small_account_plan"]["target_gross_cny"] == 1_000.0


def test_stage_rejects_unverified_position_authority() -> None:
    port = _port(account=_account(), candidates=(_candidate(),))

    with pytest.raises(
        SmallAccountStageContractError,
        match="position_authority_not_verified",
    ):
        port.execute(_request(_decision_ready_bundle(position_authority_valid=False)))


def test_stage_is_idempotent_for_the_same_request_key() -> None:
    port = _port(account=_account(), candidates=(_candidate(),))
    request = _request(_decision_ready_bundle())

    first = port.execute(request)
    second = port.execute(request)

    assert first.payload == second.payload


def test_stage_rejects_invocation_before_decision_ready_boundary() -> None:
    port = _port(account=_account(), candidates=(_candidate(),))

    with pytest.raises(
        SmallAccountStageContractError,
        match="decision_ready_is_not_next_stage",
    ):
        port.execute(_request(_bundle(position_authority_valid=True)))
