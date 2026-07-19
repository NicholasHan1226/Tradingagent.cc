from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from shared.models.champion_registry import ChampionSelectionRegistry
from shared.models.drift_action_store import DriftActionStore
from shared.models.drift_runtime import DriftRuntimeConstraint
from shared.models.lifecycle import LifecycleActor
from shared.execution.execution_lineage import ASHARE_EXECUTION_LINEAGE_ID
from shared.portfolio.small_account_optimizer import (
    AccountAuthoritySnapshot,
    CandidateAllocationInput,
    account_position_snapshot_sha256,
)
from shared.portfolio.champion import FrozenChampionSpec
from shared.review.sample_journal import SampleJournal
from shared.runtime.canonical_small_account_stage import (
    CanonicalSmallAccountDecisionStagePort,
)
from shared.runtime.capital_stages import (
    CapitalBackedPreopenStagePort,
    CapitalBackedRiskStagePort,
    CapitalBackedSimulationExecutionStagePort,
    PaperCapitalAccount,
)
from shared.runtime.drift_stage import DriftConstrainedRiskStagePort
from shared.runtime.file_store import FileRunBundleStore
from shared.runtime.publisher import LocalRunBundlePublisher
from shared.runtime.run_bundle import RunStage
from shared.universe.policy import CanonicalMainboardScopePolicy
from tests.test_capital_backed_paper_stages import (
    DECISION_AS_OF as CAPITAL_DECISION_AS_OF,
    LINEAGE as CAPITAL_LINEAGE,
    TRADE_DATE,
    _bundle as capital_bundle,
    _clock as _execution_clock,
    _execute_buy,
    _init_ledger,
    _mark,
    _market_snapshot,
    _request as capital_request,
    _StaticPort,
)
from tests.test_champion_registry import (
    NOW,
    _current_lifecycle,
    _manifest,
    _validation_plan,
)
from tests.test_paper_runtime_composition import (
    _business_ports,
    _config_kwargs,
    _fixture_transport,
    _managed_identities,
)
from shared.data.research_snapshot_store import FileResearchSnapshotStore
from tests._champion_authority_fixture import (
    FrozenChampionSelectionVerifier,
    FrozenNumericPITFeatureSnapshotVerifier,
    build_champion_authority_fixture,
)
from tests._thesis_risk_fixture import build_thesis_risk_fixture


def _module():
    from shared.runtime import composition

    return composition


def _account(tmp_path: Path) -> PaperCapitalAccount:
    return PaperCapitalAccount(
        ledger=_init_ledger(tmp_path / "ledger"),
        artifact_root=tmp_path / "capital-artifacts",
        mark_prices={},
    )


def _canonical_account(
    tmp_path: Path,
    *,
    mark_prices: dict[str, dict[str, Any]] | None = None,
    authority_generation: int = 1,
    execution_lineage: str = ASHARE_EXECUTION_LINEAGE_ID,
) -> PaperCapitalAccount:
    ledger = _init_ledger(
        tmp_path / "ledger",
        authority_generation=authority_generation,
        execution_lineage=execution_lineage,
    )
    return PaperCapitalAccount(
        ledger=ledger,
        artifact_root=tmp_path / "capital-artifacts",
        mark_prices=mark_prices or {},
    )


def _manual_registry(tmp_path: Path) -> tuple[ChampionSelectionRegistry, str]:
    plan = _validation_plan()
    manifest = _manifest(plan)
    approval = "capital-runtime-manual-approval"
    lifecycle = _current_lifecycle(
        manifest,
        approval_reference=approval,
        recorded_at=NOW + timedelta(minutes=1),
    )
    registry = ChampionSelectionRegistry(tmp_path / "champion-registry")
    receipt = registry.record_selection(
        selection_id="capital-runtime-selection",
        action="activate",
        manifest=manifest,
        validation_plan=plan,
        lifecycle=lifecycle,
        actor=LifecycleActor.HUMAN_REVIEWER,
        human_approval_reference=approval,
        recorded_at=NOW + timedelta(minutes=2),
        expected_current_manifest_sha256=None,
    )
    return registry, receipt.selected_manifest_sha256


def _canonical_port(
    *,
    account: PaperCapitalAccount,
    decision_time: datetime,
    candidates: tuple[CandidateAllocationInput, ...] = (),
) -> CanonicalSmallAccountDecisionStagePort:
    capital = account.ledger.snapshot()
    risk_account_snapshot = AccountAuthoritySnapshot(
        capital_authority_id=capital.authority_id,
        authority_generation=capital.authority_generation,
        account_as_of=decision_time,
        available_cash_cny=capital.cash_balance_cny,
        current_gross_cny=capital.positions_market_value_cny,
        positions=(),
        position_snapshot_receipt_id=(
            f"capital-composition-preopen:{capital.event_id}:{capital.event_checksum}"
        ),
        position_snapshot_sha256=account_position_snapshot_sha256(()),
        verification_receipt_sha256="0" * 64,
        authority_source_class="canonical_authority",
    )
    authority_kwargs: dict[str, object] = {}
    if candidates:
        context = candidates[0].score_evidence.selection_context
        authority_kwargs = {
            "current_champion_selection_context": context,
            "champion_selection_verifier": FrozenChampionSelectionVerifier(context),
            "numeric_feature_snapshot_verifier": (
                FrozenNumericPITFeatureSnapshotVerifier(
                    tuple(
                        candidate.score_evidence.feature_snapshot
                        for candidate in candidates
                    )
                )
            ),
        }
    return CanonicalSmallAccountDecisionStagePort(
        account=account,
        candidates=candidates,
        decision_time=decision_time,
        trade_date=TRADE_DATE,
        mark_observed_at={},
        **build_thesis_risk_fixture(
            candidates=candidates,
            account_snapshot=risk_account_snapshot,
            decision_time=decision_time,
        ),
        **authority_kwargs,
    )


def _canonical_candidate(
    *,
    decision_time: datetime,
    champion_selection_manifest_sha256: str,
    score: float,
) -> CandidateAllocationInput:
    champion = FrozenChampionSpec(
        champion_id="capital-runtime-champion",
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
    authority = build_champion_authority_fixture(
        champion=champion,
        symbol="000001.SZ",
        decision_time=decision_time,
        feature_values={
            "quality_score": score,
            "value_score": score,
            "momentum_score": score,
            "low_volatility_score": score,
        },
        selection_manifest_sha256=champion_selection_manifest_sha256,
        source_id="capital-runtime-data-1",
    )
    return CandidateAllocationInput(
        symbol="000001.SZ",
        score_evidence=authority.score_receipt,
        decision_time=decision_time,
        price_observed_at=datetime.fromisoformat("2026-07-16T01:00:00+00:00"),
        decision_reference_price=10.0,
    )


def _canonical_market_snapshot(
    order_id: str,
    *,
    trade_date: str,
    execution_time: str,
    execution_lineage: str = CAPITAL_LINEAGE,
    decision_as_of: str = CAPITAL_DECISION_AS_OF,
    **updates: Any,
) -> dict[str, Any]:
    """Add explicit local receipt clocks to one immutable execution snapshot."""

    snapshot = _market_snapshot(
        order_id,
        trade_date=trade_date,
        execution_time=execution_time,
        decision_as_of=decision_as_of,
        execution_lineage=execution_lineage,
        **updates,
    )
    snapshot.update(
        {
            "ingested_at": execution_time,
            "retrieved_as_of": execution_time,
        }
    )
    return snapshot


def test_execution_evidence_projection_preserves_subsecond_causality() -> None:
    module = _module()
    snapshot = _canonical_market_snapshot(
        "ORDER-SUBSECOND",
        trade_date=TRADE_DATE,
        execution_time="2026-07-16T09:31:00.900000+08:00",
    )

    normalized = module._canonical_execution_evidence_snapshot(
        order_id="ORDER-SUBSECOND",
        snapshot=snapshot,
    )

    assert normalized["execution_time"] == "2026-07-16T09:31:00.900000+08:00"
    assert normalized["available_at"] == "2026-07-16T09:31:00.900000+08:00"
    assert normalized["ingested_at"] == "2026-07-16T09:31:00.900000+08:00"
    assert normalized["retrieved_as_of"] == "2026-07-16T09:31:00.900000+08:00"


def _capital_config(
    module,
    *,
    champion_manifest_sha256: str,
    authority_generation: int = 1,
    execution_lineage: str = ASHARE_EXECUTION_LINEAGE_ID,
):
    values = _config_kwargs()
    values.update(
        capital_authority_id="ashare-capital-v1",
        authority_generation=authority_generation,
        execution_lineage=execution_lineage,
        champion_manifest_sha256=champion_manifest_sha256,
    )
    return module.PaperRuntimeConfig(**values)


def test_capital_composition_binds_canonical_account_and_current_manual_champion(
    tmp_path: Path,
) -> None:
    module = _module()
    registry, manifest_sha256 = _manual_registry(tmp_path)
    account = _canonical_account(tmp_path)
    config = _capital_config(
        module,
        champion_manifest_sha256=manifest_sha256,
    )

    runtime = module.compose_capital_backed_paper_runtime(
        config=config,
        transport_fixture=_fixture_transport(module),
        research_snapshot_store=FileResearchSnapshotStore(tmp_path / "snapshots"),
        run_bundle_store=FileRunBundleStore(tmp_path / "bundles"),
        sample_journal=SampleJournal(tmp_path / "sample-journal.jsonl"),
        business_stage_ports=_business_ports(
            module,
            include_actionable_order=False,
            include_candidate_evidence=False,
        ),
        canonical_small_account_decision_port=_canonical_port(
            account=account,
            decision_time=config.decision_as_of,
        ),
        capital_account=account,
        market_snapshots={},
        execution_clock=_execution_clock("2026-07-16T09:31:00+08:00"),
        reconciled_at="2026-07-16T15:01:00+08:00",
        drift_risk_adapter=module.DriftRuntimeRiskAdapter(
            DriftActionStore(tmp_path / "drift-actions")
        ),
        champion_selection_registry=registry,
        managed_stage_identities=_managed_identities(),
        scope_policy=CanonicalMainboardScopePolicy(),
        local_publisher=LocalRunBundlePublisher(tmp_path / "publication"),
    )

    assert isinstance(
        runtime._loop._ports[RunStage.PREOPEN],
        CapitalBackedPreopenStagePort,
    )
    assert isinstance(
        runtime._loop._ports[RunStage.RISK_CHECKED],
        CapitalBackedRiskStagePort,
    )
    assert runtime._context.authority_id == "ashare-capital-v1"
    assert runtime._context.champion_manifest_sha256 == manifest_sha256

    result = runtime.run()
    assert result.bundle.status == "completed"
    assert result.bundle.context.authority_id == "ashare-capital-v1"


def test_capital_composition_consumes_current_ledger_identity_without_constants(
    tmp_path: Path,
) -> None:
    module = _module()
    rotated_generation = 2
    rotated_lineage = "ashare-sim-rotated-local-candidate-v2"
    registry, manifest_sha256 = _manual_registry(tmp_path)
    account = _canonical_account(
        tmp_path,
        authority_generation=rotated_generation,
        execution_lineage=rotated_lineage,
    )
    config = _capital_config(
        module,
        champion_manifest_sha256=manifest_sha256,
        authority_generation=rotated_generation,
        execution_lineage=rotated_lineage,
    )

    runtime = module.compose_capital_backed_paper_runtime(
        config=config,
        transport_fixture=_fixture_transport(module),
        research_snapshot_store=FileResearchSnapshotStore(tmp_path / "snapshots"),
        run_bundle_store=FileRunBundleStore(tmp_path / "bundles"),
        sample_journal=SampleJournal(tmp_path / "sample-journal.jsonl"),
        business_stage_ports=_business_ports(
            module,
            include_actionable_order=False,
            include_candidate_evidence=False,
        ),
        canonical_small_account_decision_port=_canonical_port(
            account=account,
            decision_time=config.decision_as_of,
        ),
        capital_account=account,
        market_snapshots={},
        execution_clock=_execution_clock("2026-07-16T09:31:00+08:00"),
        reconciled_at="2026-07-16T15:01:00+08:00",
        drift_risk_adapter=module.DriftRuntimeRiskAdapter(
            DriftActionStore(tmp_path / "drift-actions")
        ),
        champion_selection_registry=registry,
        managed_stage_identities=_managed_identities(),
        scope_policy=CanonicalMainboardScopePolicy(),
        local_publisher=LocalRunBundlePublisher(tmp_path / "publication"),
    )

    context = runtime.run().bundle.context
    assert context.authority_generation == rotated_generation
    assert context.execution_lineage == rotated_lineage


def test_capital_composition_runs_actionable_order_through_one_ledger(
    tmp_path: Path,
) -> None:
    module = _module()
    registry, manifest_sha256 = _manual_registry(tmp_path)
    account = _canonical_account(
        tmp_path,
        mark_prices={
            "000001.SZ": _mark(
                10.0,
                execution_lineage=ASHARE_EXECUTION_LINEAGE_ID,
            )
        },
    )
    config = _capital_config(
        module,
        champion_manifest_sha256=manifest_sha256,
    )
    candidate = _canonical_candidate(
        decision_time=config.decision_as_of,
        champion_selection_manifest_sha256=manifest_sha256,
        score=0.28,
    )

    runtime = module.compose_capital_backed_paper_runtime(
        config=config,
        transport_fixture=_fixture_transport(module),
        research_snapshot_store=FileResearchSnapshotStore(tmp_path / "snapshots"),
        run_bundle_store=FileRunBundleStore(tmp_path / "bundles"),
        sample_journal=SampleJournal(tmp_path / "sample-journal.jsonl"),
        business_stage_ports=_business_ports(module),
        canonical_small_account_decision_port=_canonical_port(
            account=account,
            decision_time=config.decision_as_of,
            candidates=(candidate,),
        ),
        capital_account=account,
        market_snapshots={
            "order-1": _canonical_market_snapshot(
                "order-1",
                trade_date=TRADE_DATE,
                execution_time="2026-07-16T09:31:00+08:00",
                execution_lineage=ASHARE_EXECUTION_LINEAGE_ID,
                decision_as_of=config._run_context.decision_as_of,
            )
        },
        execution_clock=_execution_clock("2026-07-16T09:31:00+08:00"),
        reconciled_at="2026-07-16T15:01:00+08:00",
        drift_risk_adapter=module.DriftRuntimeRiskAdapter(
            DriftActionStore(tmp_path / "drift-actions")
        ),
        champion_selection_registry=registry,
        managed_stage_identities=_managed_identities(),
        scope_policy=CanonicalMainboardScopePolicy(),
        local_publisher=LocalRunBundlePublisher(tmp_path / "publication"),
    )

    result = runtime.run()
    risk = result.bundle.receipt_for(RunStage.RISK_CHECKED).payload
    execution = result.bundle.receipt_for(RunStage.ORDERS_SIMULATED).payload

    assert result.bundle.status == "completed"
    assert len(risk["approved_orders"]) == 1
    assert risk["approved_orders"][0]["market_capital_reservation_id"]
    assert execution["order_receipts"][0]["status"] == "filled"
    assert execution["order_receipts"][0]["capital_commit_status"] == "committed"
    assert execution["order_receipts"][0]["execution_eligible"] is True
    assert execution["order_receipts"][0]["available_at"] == (
        "2026-07-16T09:31:00+08:00"
    )
    assert execution["order_receipts"][0]["ingested_at"] == (
        "2026-07-16T09:31:00+08:00"
    )
    assert execution["order_receipts"][0]["retrieved_as_of"] == (
        "2026-07-16T09:31:00+08:00"
    )
    assert account.ledger.snapshot().positions_quantity_by_risk_unit == {
        "000001.SZ": 200
    }
    assert account.ledger.snapshot().reserved_cash_cny == 0.0


def test_capital_composition_rejects_manifest_not_current_in_registry(
    tmp_path: Path,
) -> None:
    module = _module()
    registry, _ = _manual_registry(tmp_path)
    account = _canonical_account(tmp_path)
    config = _capital_config(module, champion_manifest_sha256="f" * 64)

    with pytest.raises(
        module.PaperRuntimeConfigurationError,
        match="champion_current_selection_mismatch",
    ):
        module.compose_capital_backed_paper_runtime(
            config=config,
            transport_fixture=_fixture_transport(module),
            research_snapshot_store=FileResearchSnapshotStore(tmp_path / "snapshots"),
            run_bundle_store=FileRunBundleStore(tmp_path / "bundles"),
            sample_journal=SampleJournal(tmp_path / "sample-journal.jsonl"),
            business_stage_ports=_business_ports(
                module,
                include_actionable_order=False,
                include_candidate_evidence=False,
            ),
            canonical_small_account_decision_port=_canonical_port(
                account=account,
                decision_time=config.decision_as_of,
            ),
            capital_account=account,
            market_snapshots={},
            execution_clock=_execution_clock("2026-07-16T09:31:00+08:00"),
            reconciled_at="2026-07-16T15:01:00+08:00",
            drift_risk_adapter=module.DriftRuntimeRiskAdapter(
                DriftActionStore(tmp_path / "drift-actions")
            ),
            champion_selection_registry=registry,
            managed_stage_identities=_managed_identities(),
            scope_policy=CanonicalMainboardScopePolicy(),
            local_publisher=LocalRunBundlePublisher(tmp_path / "publication"),
        )


class _StopBeforeExecution:
    def snapshot(self) -> DriftRuntimeConstraint:
        return DriftRuntimeConstraint(
            max_risk_multiplier=0.0,
            stop_new_orders=True,
            reduce_only=True,
            quarantined=False,
            review_required=True,
            active_action_receipt_sha256="d" * 64,
            reason_codes=("tightened_after_risk",),
        )


class _NeutralAtRisk:
    def snapshot(self) -> DriftRuntimeConstraint:
        return DriftRuntimeConstraint(
            max_risk_multiplier=1.0,
            stop_new_orders=False,
            reduce_only=False,
            quarantined=False,
            review_required=False,
            active_action_receipt_sha256=None,
            reason_codes=(),
        )


class _SequencedConstraints:
    def __init__(self, *constraints: DriftRuntimeConstraint) -> None:
        self._constraints = constraints
        self.calls = 0

    def snapshot(self) -> DriftRuntimeConstraint:
        index = min(self.calls, len(self._constraints) - 1)
        self.calls += 1
        return self._constraints[index]


def _two_open_orders() -> list[dict[str, Any]]:
    return [
        {
            "order_id": "ORDER-BATCH-1",
            "decision_id": "DECISION-BATCH-1",
            "symbol": "000001.SZ",
            "intent": "open",
            "side": "buy",
            "quantity": 100,
            "reservation_price_cny": 10.5,
            "expected_fee_cny": 6.0,
            "capital_authority_id": "ashare-capital-v1",
            "authority_generation": 1,
            "execution_lineage": CAPITAL_LINEAGE,
        },
        {
            "order_id": "ORDER-BATCH-2",
            "decision_id": "DECISION-BATCH-2",
            "symbol": "000002.SZ",
            "intent": "open",
            "side": "buy",
            "quantity": 100,
            "reservation_price_cny": 10.5,
            "expected_fee_cny": 6.0,
            "capital_authority_id": "ashare-capital-v1",
            "authority_generation": 1,
            "execution_lineage": CAPITAL_LINEAGE,
        },
    ]


def _reserve_batch(
    *,
    account: PaperCapitalAccount,
    bundle,
    orders: list[dict[str, Any]],
    effect_guard: object | None = None,
) -> dict[str, Any]:
    return dict(
        CapitalBackedRiskStagePort(
            base_port=DriftConstrainedRiskStagePort(
                base_port=_StaticPort(
                    RunStage.RISK_CHECKED,
                    {
                        "risk_policy_version": "test-risk-v1",
                        "oms_plan_id": "plan-batch-race",
                        "approved_orders": orders,
                        "rejected_decisions": [],
                    },
                ),
                constraint_provider=_NeutralAtRisk(),
            ),
            account=account,
            effect_guard=effect_guard,
        )
        .execute(capital_request(stage=RunStage.RISK_CHECKED, bundle=bundle))
        .payload
    )


def _batch_snapshots() -> dict[str, dict[str, Any]]:
    return {
        order_id: _canonical_market_snapshot(
            order_id,
            trade_date=TRADE_DATE,
            execution_time="2026-07-16T09:31:00+08:00",
        )
        for order_id in ("ORDER-BATCH-1", "ORDER-BATCH-2")
    }


def test_each_reserve_rereads_drift_and_blocks_second_open_after_tightening(
    tmp_path: Path,
) -> None:
    module = _module()
    account = _account(tmp_path)
    bundle = capital_bundle()
    CapitalBackedPreopenStagePort(
        base_port=_StaticPort(
            RunStage.PREOPEN,
            {
                "market": "ashare",
                "account_type": "simulated",
                "real_trading_enabled": False,
                "account_authority_valid": True,
                "position_authority_valid": True,
            },
        ),
        account=account,
    ).execute(capital_request(stage=RunStage.PREOPEN, bundle=bundle))
    provider = _SequencedConstraints(
        _NeutralAtRisk().snapshot(),
        _StopBeforeExecution().snapshot(),
    )
    guard = module._PerEffectAuthorityGuard(
        constraint_provider=provider,
    )

    payload = _reserve_batch(
        account=account,
        bundle=bundle,
        orders=_two_open_orders(),
        effect_guard=guard,
    )

    assert [row["order_id"] for row in payload["approved_orders"]] == ["ORDER-BATCH-1"]
    assert payload["rejected_decisions"] == [
        {
            "decision_id": "DECISION-BATCH-2",
            "reason": "market_capital_reservation_rejected:drift_stop_new_risk:"
            + "d" * 64,
        }
    ]
    assert provider.calls == 2
    assert len(account.ledger.active_reservation_manifest()) == 1


def test_each_execution_effect_rereads_drift_and_cleans_second_reservation(
    tmp_path: Path,
) -> None:
    module = _module()
    account = _account(tmp_path)
    bundle = capital_bundle()
    CapitalBackedPreopenStagePort(
        base_port=_StaticPort(
            RunStage.PREOPEN,
            {
                "market": "ashare",
                "account_type": "simulated",
                "real_trading_enabled": False,
                "account_authority_valid": True,
                "position_authority_valid": True,
            },
        ),
        account=account,
    ).execute(capital_request(stage=RunStage.PREOPEN, bundle=bundle))
    orders = _two_open_orders()
    risk = _reserve_batch(account=account, bundle=bundle, orders=orders)
    execution_bundle = capital_bundle(
        permitted_order_ids=("ORDER-BATCH-1", "ORDER-BATCH-2"),
        stage_payloads={RunStage.RISK_CHECKED: risk},
    )
    request = capital_request(
        stage=RunStage.ORDERS_SIMULATED,
        bundle=execution_bundle,
        permitted_order_ids=("ORDER-BATCH-1", "ORDER-BATCH-2"),
    )
    snapshots = _batch_snapshots()
    provider = _SequencedConstraints(
        _NeutralAtRisk().snapshot(),
        _NeutralAtRisk().snapshot(),
        _NeutralAtRisk().snapshot(),
        _StopBeforeExecution().snapshot(),
        _StopBeforeExecution().snapshot(),
    )

    payload = (
        module._PreSideEffectDriftCapitalExecutionStagePort(
            base_port=CapitalBackedSimulationExecutionStagePort(
                account=account,
                market_snapshots=snapshots,
                execution_clock=_execution_clock("2026-07-16T09:31:00+08:00"),
            ),
            account=account,
            market_snapshots=snapshots,
            constraint_provider=provider,
        )
        .execute(request)
        .payload
    )

    receipts = {row["order_id"]: row for row in payload["order_receipts"]}
    assert receipts["ORDER-BATCH-1"]["capital_commit_status"] == "committed"
    assert receipts["ORDER-BATCH-2"]["status"] == "not_filled"
    assert receipts["ORDER-BATCH-2"]["capital_release_status"] == "released"
    assert receipts["ORDER-BATCH-2"]["execution_reason"] == (
        "drift_stop_new_risk:" + "d" * 64
    )
    assert account.ledger.snapshot().positions_quantity_by_risk_unit == {
        "000001.SZ": 100
    }
    assert account.ledger.snapshot().reserved_cash_cny == 0.0
    assert provider.calls >= 4


def test_each_execution_effect_rechecks_champion_and_cleans_on_rotation(
    tmp_path: Path,
) -> None:
    module = _module()
    registry, manifest_sha256 = _manual_registry(tmp_path)
    binding = module._ChampionCurrentBinding.load(
        registry=registry,
        expected_manifest_sha256=manifest_sha256,
    )
    account = _account(tmp_path / "capital")
    bundle = capital_bundle()
    CapitalBackedPreopenStagePort(
        base_port=_StaticPort(
            RunStage.PREOPEN,
            {
                "market": "ashare",
                "account_type": "simulated",
                "real_trading_enabled": False,
                "account_authority_valid": True,
                "position_authority_valid": True,
            },
        ),
        account=account,
    ).execute(capital_request(stage=RunStage.PREOPEN, bundle=bundle))
    orders = _two_open_orders()
    risk = _reserve_batch(account=account, bundle=bundle, orders=orders)
    execution_bundle = capital_bundle(
        permitted_order_ids=("ORDER-BATCH-1", "ORDER-BATCH-2"),
        stage_payloads={RunStage.RISK_CHECKED: risk},
    )
    request = capital_request(
        stage=RunStage.ORDERS_SIMULATED,
        bundle=execution_bundle,
        permitted_order_ids=("ORDER-BATCH-1", "ORDER-BATCH-2"),
    )
    snapshots = _batch_snapshots()
    original_commit = account.ledger.commit_fill
    rotated = False

    def _commit_then_rotate(request_value):
        nonlocal rotated
        result = original_commit(request_value)
        if not rotated:
            rotated = True
            current = registry.load_current()
            plan = _validation_plan(suffix="b")
            manifest = _manifest(plan, suffix="b")
            approval = "capital-runtime-rotated-approval"
            lifecycle = _current_lifecycle(
                manifest,
                approval_reference=approval,
                recorded_at=NOW + timedelta(minutes=3),
            )
            registry.record_selection(
                selection_id="capital-runtime-rotated-selection",
                action="activate",
                manifest=manifest,
                validation_plan=plan,
                lifecycle=lifecycle,
                actor=LifecycleActor.HUMAN_REVIEWER,
                human_approval_reference=approval,
                recorded_at=NOW + timedelta(minutes=4),
                expected_current_manifest_sha256=(current.selected_manifest_sha256),
            )
        return result

    with patch.object(account.ledger, "commit_fill", side_effect=_commit_then_rotate):
        payload = (
            module._PreSideEffectDriftCapitalExecutionStagePort(
                base_port=CapitalBackedSimulationExecutionStagePort(
                    account=account,
                    market_snapshots=snapshots,
                    execution_clock=_execution_clock("2026-07-16T09:31:00+08:00"),
                ),
                account=account,
                market_snapshots=snapshots,
                constraint_provider=_NeutralAtRisk(),
                champion_binding=binding,
            )
            .execute(request)
            .payload
        )

    receipts = {row["order_id"]: row for row in payload["order_receipts"]}
    assert receipts["ORDER-BATCH-1"]["capital_commit_status"] == "committed"
    assert receipts["ORDER-BATCH-2"]["status"] == "not_filled"
    assert receipts["ORDER-BATCH-2"]["capital_release_status"] == "released"
    assert receipts["ORDER-BATCH-2"]["execution_reason"] == (
        "champion_current_selection_mismatch"
    )
    assert account.ledger.snapshot().positions_quantity_by_risk_unit == {
        "000001.SZ": 100
    }
    assert account.ledger.snapshot().reserved_cash_cny == 0.0


def test_execution_drift_gate_requires_explicit_receipt_evidence_clocks(
    tmp_path: Path,
) -> None:
    module = _module()
    account = _account(tmp_path)
    snapshot = _market_snapshot(
        "ORDER-MISSING-RECEIPT-CLOCKS",
        trade_date=TRADE_DATE,
        execution_time="2026-07-16T09:31:00+08:00",
    )
    base = CapitalBackedSimulationExecutionStagePort(
        account=account,
        market_snapshots={"ORDER-MISSING-RECEIPT-CLOCKS": snapshot},
        execution_clock=_execution_clock("2026-07-16T09:31:00+08:00"),
    )

    with pytest.raises(
        module.PaperRuntimeConfigurationError,
        match="capital_execution_ingested_at_missing",
    ):
        module._PreSideEffectDriftCapitalExecutionStagePort(
            base_port=base,
            account=account,
            market_snapshots={"ORDER-MISSING-RECEIPT-CLOCKS": snapshot},
            constraint_provider=_NeutralAtRisk(),
        )


def test_execution_drift_gate_releases_reservation_before_any_fill_side_effect(
    tmp_path: Path,
) -> None:
    module = _module()
    account = _account(tmp_path)
    bundle = capital_bundle()
    CapitalBackedPreopenStagePort(
        base_port=_StaticPort(
            RunStage.PREOPEN,
            {
                "market": "ashare",
                "account_type": "simulated",
                "real_trading_enabled": False,
                "account_authority_valid": True,
                "position_authority_valid": True,
            },
        ),
        account=account,
    ).execute(capital_request(stage=RunStage.PREOPEN, bundle=bundle))
    order_id = "ORDER-DRIFT-BLOCK"
    risk = (
        CapitalBackedRiskStagePort(
            base_port=DriftConstrainedRiskStagePort(
                base_port=_StaticPort(
                    RunStage.RISK_CHECKED,
                    {
                        "risk_policy_version": "test-risk-v1",
                        "oms_plan_id": "plan-drift-block",
                        "approved_orders": [
                            {
                                "order_id": order_id,
                                "decision_id": "DECISION-DRIFT-BLOCK",
                                "symbol": "000001.SZ",
                                "intent": "open",
                                "side": "buy",
                                "quantity": 100,
                                "reservation_price_cny": 10.5,
                                "expected_fee_cny": 6.0,
                                "capital_authority_id": "ashare-capital-v1",
                                "authority_generation": 1,
                                "execution_lineage": CAPITAL_LINEAGE,
                            }
                        ],
                        "rejected_decisions": [],
                    },
                ),
                constraint_provider=_NeutralAtRisk(),
            ),
            account=account,
        )
        .execute(capital_request(stage=RunStage.RISK_CHECKED, bundle=bundle))
        .payload
    )
    snapshot = _canonical_market_snapshot(
        order_id,
        trade_date=TRADE_DATE,
        execution_time="2026-07-16T09:31:00+08:00",
    )
    execution_bundle = capital_bundle(
        permitted_order_ids=(order_id,),
        stage_payloads={RunStage.RISK_CHECKED: risk},
    )
    request = capital_request(
        stage=RunStage.ORDERS_SIMULATED,
        bundle=execution_bundle,
        permitted_order_ids=(order_id,),
    )
    base = CapitalBackedSimulationExecutionStagePort(
        account=account,
        market_snapshots={order_id: snapshot},
        execution_clock=_execution_clock("2026-07-16T09:31:00+08:00"),
    )
    event_count_before = account.ledger.validate_checksum_chain()["event_count"]

    payload = (
        module._PreSideEffectDriftCapitalExecutionStagePort(
            base_port=base,
            account=account,
            market_snapshots={order_id: snapshot},
            constraint_provider=_StopBeforeExecution(),
        )
        .execute(request)
        .payload
    )

    receipt = payload["order_receipts"][0]
    assert receipt["status"] == "not_filled"
    assert receipt["filled_quantity"] == 0
    assert receipt["capital_commit_status"] == "not_applicable"
    assert receipt["capital_release_status"] == "released"
    assert receipt["execution_reason"] == "drift_stop_new_risk:" + "d" * 64
    ledger = account.ledger.snapshot()
    assert ledger.positions_quantity_by_risk_unit == {}
    assert ledger.reserved_cash_cny == 0.0
    assert account.ledger.validate_checksum_chain()["event_count"] == (
        event_count_before + 1
    )


def test_execution_drift_gate_blocks_open_but_preserves_authoritative_exit(
    tmp_path: Path,
) -> None:
    module = _module()
    ledger = _init_ledger(tmp_path / "ledger", bootstrap_date="2026-07-15")
    account = PaperCapitalAccount(
        ledger=ledger,
        artifact_root=tmp_path / "capital-artifacts",
        mark_prices={"000001.SZ": _mark(10.2)},
    )
    _execute_buy(
        account=account,
        order_id="ORDER-PRIOR-DAY-BUY",
        run_id="prior-day-buy-run",
        trade_date="2026-07-15",
        decision_as_of="2026-07-15T09:30:00+08:00",
        execution_time="2026-07-15T09:31:00+08:00",
    )
    open_id = "ORDER-BLOCKED-OPEN"
    exit_id = "ORDER-AUTHORITATIVE-EXIT"
    orders: list[dict[str, Any]] = [
        {
            "order_id": open_id,
            "decision_id": "DECISION-BLOCKED-OPEN",
            "symbol": "000002.SZ",
            "intent": "open",
            "side": "buy",
            "quantity": 100,
            "reservation_price_cny": 10.5,
            "expected_fee_cny": 6.0,
            "capital_authority_id": "ashare-capital-v1",
            "authority_generation": 1,
            "execution_lineage": CAPITAL_LINEAGE,
        },
        {
            "order_id": exit_id,
            "decision_id": "DECISION-AUTHORITATIVE-EXIT",
            "symbol": "000001.SZ",
            "intent": "exit",
            "side": "sell",
            "quantity": 100,
            "reservation_price_cny": 10.2,
            "expected_fee_cny": 6.0,
            "sellable_quantity": 100,
            "capital_authority_id": "ashare-capital-v1",
            "authority_generation": 1,
            "execution_lineage": CAPITAL_LINEAGE,
        },
    ]
    next_day_bundle = capital_bundle(
        run_id="next-day-mixed-run",
        trade_date=TRADE_DATE,
        decision_as_of="2026-07-16T09:30:00+08:00",
    )
    CapitalBackedPreopenStagePort(
        base_port=_StaticPort(
            RunStage.PREOPEN,
            {
                "market": "ashare",
                "account_type": "simulated",
                "real_trading_enabled": False,
                "account_authority_valid": True,
                "position_authority_valid": True,
            },
        ),
        account=account,
    ).execute(capital_request(stage=RunStage.PREOPEN, bundle=next_day_bundle))
    risk = (
        CapitalBackedRiskStagePort(
            base_port=DriftConstrainedRiskStagePort(
                base_port=_StaticPort(
                    RunStage.RISK_CHECKED,
                    {
                        "risk_policy_version": "test-risk-v1",
                        "oms_plan_id": "plan-mixed-drift",
                        "approved_orders": orders,
                        "rejected_decisions": [],
                    },
                ),
                constraint_provider=_NeutralAtRisk(),
            ),
            account=account,
        )
        .execute(capital_request(stage=RunStage.RISK_CHECKED, bundle=next_day_bundle))
        .payload
    )
    snapshots = {
        open_id: _canonical_market_snapshot(
            open_id,
            trade_date=TRADE_DATE,
            execution_time="2026-07-16T09:31:00+08:00",
        ),
        exit_id: _canonical_market_snapshot(
            exit_id,
            trade_date=TRADE_DATE,
            execution_time="2026-07-16T09:31:00+08:00",
            bid_price=10.2,
            bid_size=1_000,
            sellable_qty=100,
        ),
    }
    execution_bundle = capital_bundle(
        run_id="next-day-mixed-run",
        trade_date=TRADE_DATE,
        decision_as_of="2026-07-16T09:30:00+08:00",
        permitted_order_ids=(open_id, exit_id),
        stage_payloads={RunStage.RISK_CHECKED: risk},
    )
    request = capital_request(
        stage=RunStage.ORDERS_SIMULATED,
        bundle=execution_bundle,
        permitted_order_ids=(open_id, exit_id),
    )
    base = CapitalBackedSimulationExecutionStagePort(
        account=account,
        market_snapshots=snapshots,
        execution_clock=_execution_clock("2026-07-16T09:31:00+08:00"),
    )

    payload = (
        module._PreSideEffectDriftCapitalExecutionStagePort(
            base_port=base,
            account=account,
            market_snapshots=snapshots,
            constraint_provider=_StopBeforeExecution(),
        )
        .execute(request)
        .payload
    )

    receipts = {row["order_id"]: row for row in payload["order_receipts"]}
    assert receipts[open_id]["status"] == "not_filled"
    assert receipts[open_id]["capital_release_status"] == "released"
    assert receipts[exit_id]["status"] == "filled"
    assert receipts[exit_id]["capital_commit_status"] == "committed"
    assert account.ledger.snapshot().positions_quantity_by_risk_unit == {}
    assert account.ledger.snapshot().reserved_cash_cny == 0.0
