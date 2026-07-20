from __future__ import annotations

import importlib
import json
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

import pytest

from shared.data.evidence_gate import DatasetEvidencePolicy, EvidenceAction
from shared.data.research_snapshot_store import FileResearchSnapshotStore
from shared.data.sharedsignals_v1 import QueryRequest
from shared.data.sharedsignals_v1 import HTTPResponse
from shared.models.drift_action_store import DriftActionStore
from shared.models.drift_policy import (
    DriftDecision,
    SafeAutomaticAction,
)
from shared.models.drift_runtime import DriftRuntimeRiskAdapter
from shared.portfolio.small_account_optimizer import (
    AccountAuthorityVerification,
    AccountAuthoritySnapshot,
    AccountAuthorityVerifier,
    AccountPositionSnapshot,
    CandidateAllocationInput,
    PositionReductionIntent,
    account_position_snapshot_sha256,
)
from shared.portfolio.champion import fixture_rank_evidence
from shared.review.sample_journal import SampleJournal
from shared.runtime.day_loop import ConcurrentRunUpdate, FrozenRuntimeMismatch
from shared.runtime.file_store import FileRunBundleStore
from shared.runtime.publisher import LocalRunBundlePublisher
from shared.runtime.run_bundle import (
    ComponentIdentity,
    RunBundle,
    RunContext,
    RunStage,
)
from shared.runtime.small_account_stage import SmallAccountDecisionStagePort
from shared.universe.policy import CanonicalMainboardScopePolicy
from tests.test_runtime_stage_ports import (
    CATALOG,
    CONTEXT_DATASET,
    DECISION_AS_OF,
    LINEAGE,
    PRICE_DATASET,
    _digest,
    _payloads,
    _profile,
    _transport_responses,
)
from tests._thesis_risk_fixture import build_thesis_risk_fixture


_BUSINESS_STAGES = frozenset(
    {
        RunStage.PREOPEN,
        RunStage.UNIVERSE_READY,
        RunStage.DECISION_READY,
        RunStage.RISK_CHECKED,
        RunStage.ORDERS_SIMULATED,
        RunStage.RECONCILED,
    }
)


def _module():
    return importlib.import_module("shared.runtime.composition")


def _config_kwargs() -> dict[str, Any]:
    return {
        "trade_date": "2026-07-16",
        "decision_as_of": datetime.fromisoformat(DECISION_AS_OF),
        "tradingdatas_v1_base_url": "http://tradingdatas.fixture.invalid:8082",
        "tradingdatas_catalog_version": CATALOG,
        "tradingdatas_access_policy_id": "ta-paper-read-v1",
        "dataset_profile": _profile(),
        "dataset_requests": {
            PRICE_DATASET: QueryRequest(
                dataset_id=PRICE_DATASET,
                schema_major=1,
                fields=("ts_code", "close"),
                as_of=DECISION_AS_OF,
            ),
            CONTEXT_DATASET: QueryRequest(
                dataset_id=CONTEXT_DATASET,
                schema_major=1,
                fields=("sector_id", "breadth"),
                as_of=DECISION_AS_OF,
            ),
        },
        "evidence_policies": {
            PRICE_DATASET: DatasetEvidencePolicy(PRICE_DATASET),
            CONTEXT_DATASET: DatasetEvidencePolicy(
                CONTEXT_DATASET,
                degraded_action=EvidenceAction.DEWEIGHT,
                degraded_weight=0.25,
            ),
        },
        "capital_authority_id": "ashare-composition-offline-fixture-capital-v1",
        "authority_generation": 1,
        "execution_lineage": LINEAGE,
        "champion_manifest_sha256": _digest("c"),
        "real_trading_enabled": False,
        "live_execution_enabled": False,
        "network_enabled": False,
    }


def _managed_identities() -> dict[RunStage, ComponentIdentity]:
    return {
        RunStage.EVIDENCE_READY: ComponentIdentity(
            stage=RunStage.EVIDENCE_READY,
            component_id="tradingdatas-research-evidence-port",
            version="1",
            artifact_sha256=_digest("2"),
        ),
        RunStage.LEARNING_RECORDED: ComponentIdentity(
            stage=RunStage.LEARNING_RECORDED,
            component_id="sample-journal-learning-port",
            version="1",
            artifact_sha256=_digest("8"),
        ),
        RunStage.REPORTED: ComponentIdentity(
            stage=RunStage.REPORTED,
            component_id="local-today-report-port",
            version="1",
            artifact_sha256=_digest("9"),
        ),
    }


class _FixtureAccountAuthorityVerifier(AccountAuthorityVerifier):
    def __init__(self, proof: AccountAuthorityVerification) -> None:
        self._proof = proof

    def verify(
        self,
        snapshot: AccountAuthoritySnapshot,
        *,
        decision_time: datetime,
    ) -> AccountAuthorityVerification:
        return self._proof


def _verified_fixture_account(
    *,
    decision_time: datetime,
    available_cash_cny: float,
    current_gross_cny: float,
    positions: tuple[AccountPositionSnapshot, ...] = (),
) -> tuple[AccountAuthoritySnapshot, AccountAuthorityVerifier]:
    snapshot = AccountAuthoritySnapshot(
        capital_authority_id="ashare-composition-offline-fixture-capital-v1",
        authority_generation=1,
        account_as_of=decision_time,
        available_cash_cny=available_cash_cny,
        current_gross_cny=current_gross_cny,
        positions=positions,
        position_snapshot_receipt_id="position-authority-receipt-1",
        position_snapshot_sha256=account_position_snapshot_sha256(positions),
        verification_receipt_sha256=_digest("0"),
        authority_source_class="offline_fixture",
    )
    proof = AccountAuthorityVerification.create(
        snapshot=snapshot,
        verifier_id="composition-fixture-account-authority",
        verifier_version="1",
        verified_at=decision_time,
        valid_until=decision_time,
        promotion_eligible=False,
    )
    snapshot = replace(
        snapshot,
        verification_receipt_sha256=proof.verification_receipt_sha256,
    )
    return snapshot, _FixtureAccountAuthorityVerifier(proof)


def _business_ports(
    module,
    *,
    include_actionable_order: bool = True,
    include_candidate_evidence: bool = True,
    candidate_set_id: str = "candidate-set-20260716-v1",
) -> dict[RunStage, Any]:
    payloads = deepcopy(_payloads())
    payloads[RunStage.DECISION_READY]["candidate_set_receipt"]["candidate_set_id"] = (
        candidate_set_id
    )
    if not include_candidate_evidence:
        payloads[RunStage.DECISION_READY]["candidate_set_receipt"]["candidates"] = []
        payloads[RunStage.DECISION_READY]["journal_predictions"] = []
    if not include_actionable_order:
        payloads[RunStage.RISK_CHECKED]["approved_orders"] = []
        payloads[RunStage.ORDERS_SIMULATED]["order_receipts"] = []
        return {
            stage: module.FrozenFixtureStagePort(stage, payloads[stage])
            for stage in _BUSINESS_STAGES
        }
    approved = payloads[RunStage.RISK_CHECKED]["approved_orders"][0]
    approved.update(
        {
            "decision_id": "__OPTIMIZER_DECISION_ID__",
            "symbol": "__OPTIMIZER_SYMBOL__",
            "intent": "__OPTIMIZER_ACTION__",
            "side": "__OPTIMIZER_SIDE__",
            "quantity": "__OPTIMIZER_ORDER_QUANTITY__",
            "position_authority_receipt_id": ("__POSITION_SNAPSHOT_RECEIPT_ID__"),
            "reservation_price_cny": "__OPTIMIZER_RESERVATION_PRICE_CNY__",
            "expected_fee_cny": "__OPTIMIZER_ESTIMATED_ORDER_COST_CNY__",
            "available_cash_before_cny": "__STARTING_AVAILABLE_CASH_CNY__",
            "t_plus_one_eligible": "__OPTIMIZER_T1_ELIGIBLE__",
            "sellable_quantity": "__OPTIMIZER_SELLABLE_QUANTITY__",
        }
    )
    execution = payloads[RunStage.ORDERS_SIMULATED]["order_receipts"][0]
    execution.update(
        {
            "symbol": "__OPTIMIZER_SYMBOL__",
            "intent": "__OPTIMIZER_ACTION__",
            "requested_quantity": "__OPTIMIZER_ORDER_QUANTITY__",
            "residual_quantity": "__OPTIMIZER_ORDER_QUANTITY__",
        }
    )
    return {
        stage: module.FrozenFixtureStagePort(stage, payloads[stage])
        for stage in _BUSINESS_STAGES
    }


def _small_account_decision_port() -> SmallAccountDecisionStagePort:
    decision_time = datetime.fromisoformat(DECISION_AS_OF)
    account_snapshot, account_authority_verifier = _verified_fixture_account(
        decision_time=decision_time,
        available_cash_cny=50_000.0,
        current_gross_cny=0.0,
    )
    candidates = (
        CandidateAllocationInput(
            symbol="000001.SZ",
            score_evidence=fixture_rank_evidence(
                champion_selection_manifest_sha256=_digest("c"),
                symbol="000001.SZ",
                decision_time=decision_time,
                fixture_id="paper-runtime-open-candidate",
                source_fixture_sha256=_digest("e"),
                rank_score=0.28,
            ),
            decision_time=decision_time,
            price_observed_at=datetime.fromisoformat("2026-07-16T01:00:00+00:00"),
            decision_reference_price=10.0,
        ),
    )
    return SmallAccountDecisionStagePort(
        identity=ComponentIdentity(
            stage=RunStage.DECISION_READY,
            component_id="small-account-decision-stage",
            version="1",
            artifact_sha256=_digest("4"),
        ),
        account_snapshot=account_snapshot,
        candidates=candidates,
        decision_time=decision_time,
        account_authority_verifier=account_authority_verifier,
        **build_thesis_risk_fixture(
            candidates=candidates,
            account_snapshot=account_snapshot,
            decision_time=decision_time,
        ),
        runtime_environment="local_candidate",
        promotion_eligible=False,
    )


@dataclass(frozen=True)
class _Roots:
    snapshots: Path
    bundles: Path
    journal: Path
    publication: Path
    drift_actions: Path


def _roots(tmp_path: Path) -> _Roots:
    return _Roots(
        snapshots=tmp_path / "research-snapshots",
        bundles=tmp_path / "run-bundles",
        journal=tmp_path / "review" / "sample_journal.jsonl",
        publication=tmp_path / "today",
        drift_actions=tmp_path / "drift-actions",
    )


def _compose(
    module,
    *,
    roots: _Roots,
    transport: Any,
    bundle_store: FileRunBundleStore | None = None,
    publisher: LocalRunBundlePublisher | None = None,
    business_ports: Mapping[RunStage, Any] | None = None,
    small_account_decision_port: Any | None = None,
    drift_risk_adapter: Any | None = None,
    scope_policy: Any | None = None,
):
    return module.compose_paper_runtime(
        config=module.PaperRuntimeConfig(**_config_kwargs()),
        transport_fixture=transport,
        research_snapshot_store=FileResearchSnapshotStore(roots.snapshots),
        run_bundle_store=bundle_store or FileRunBundleStore(roots.bundles),
        sample_journal=SampleJournal(roots.journal),
        business_stage_ports=business_ports or _business_ports(module),
        small_account_decision_port=(
            small_account_decision_port or _small_account_decision_port()
        ),
        drift_risk_adapter=(
            drift_risk_adapter
            or DriftRuntimeRiskAdapter(DriftActionStore(roots.drift_actions))
        ),
        managed_stage_identities=_managed_identities(),
        scope_policy=scope_policy or CanonicalMainboardScopePolicy(),
        local_publisher=publisher or LocalRunBundlePublisher(roots.publication),
    )


def test_composition_rejects_duck_typed_scope_policy(tmp_path: Path) -> None:
    module = _module()
    canonical = CanonicalMainboardScopePolicy()

    class _ForgedExactLookingScopePolicy:
        identity = canonical.identity
        policy_sha256 = canonical.policy_sha256
        manifest = canonical.manifest

        def order_identity_allowed(self, symbol: str) -> bool:
            return canonical.order_identity_allowed(symbol)

    with pytest.raises(
        module.PaperRuntimeConfigurationError,
        match="scope_policy_must_be_exact_canonical_mainboard_policy",
    ):
        _compose(
            module,
            roots=_roots(tmp_path),
            transport=_fixture_transport(module),
            scope_policy=_ForgedExactLookingScopePolicy(),
        )


def test_composition_rejects_canonical_scope_policy_subclass(tmp_path: Path) -> None:
    module = _module()

    class _UnsafeScopeSubclass(CanonicalMainboardScopePolicy):
        def order_identity_allowed(self, symbol: str) -> bool:
            del symbol
            return True

    with pytest.raises(
        module.PaperRuntimeConfigurationError,
        match="scope_policy_must_be_exact_canonical_mainboard_policy",
    ):
        _compose(
            module,
            roots=_roots(tmp_path),
            transport=_fixture_transport(module),
            scope_policy=_UnsafeScopeSubclass(),
        )


def test_composition_requires_persistent_drift_risk_adapter(tmp_path: Path) -> None:
    module = _module()

    with pytest.raises(
        module.PaperRuntimeConfigurationError,
        match="drift_risk_adapter_invalid",
    ):
        _compose(
            module,
            roots=_roots(tmp_path),
            transport=_fixture_transport(module),
            drift_risk_adapter=object(),
        )


def test_persisted_drift_latch_blocks_buy_but_completes_no_order_day(
    tmp_path: Path,
) -> None:
    module = _module()
    roots = _roots(tmp_path)
    store = DriftActionStore(roots.drift_actions)
    receipt = store.record(
        DriftDecision(
            actions=(
                SafeAutomaticAction.STOP_NEW_RISK,
                SafeAutomaticAction.REQUIRE_REVIEW,
            ),
            risk_multiplier=0.0,
            reasons=("verified_drift_latch",),
            evidence_sha256=_digest("f"),
        ),
        recorded_at=datetime.fromisoformat("2026-07-16T01:04:00+00:00"),
    )
    ports = _business_ports(module)
    execution_payload = deepcopy(_payloads()[RunStage.ORDERS_SIMULATED])
    execution_payload["order_receipts"] = []
    ports[RunStage.ORDERS_SIMULATED] = module.FrozenFixtureStagePort(
        RunStage.ORDERS_SIMULATED,
        execution_payload,
    )

    result = _compose(
        module,
        roots=roots,
        transport=_fixture_transport(module),
        business_ports=ports,
        drift_risk_adapter=DriftRuntimeRiskAdapter(store),
    ).run()
    risk = result.bundle.receipt_for(RunStage.RISK_CHECKED).payload

    assert risk["approved_orders"] == []
    assert risk["rejected_decisions"] == [
        {
            "decision_id": risk["rejected_decisions"][0]["decision_id"],
            "reason": f"drift_stop_new_risk:{receipt.receipt_sha256}",
        }
    ]
    assert risk["drift_constraint"]["active_action_receipt_sha256"] == (
        receipt.receipt_sha256
    )
    assert risk["drift_constraint_sha256"]
    assert result.bundle.stop_new_risk is True
    assert result.bundle.status == "completed_with_blocks"


def test_required_evidence_block_still_completes_closed_loop_without_new_risk(
    tmp_path: Path,
) -> None:
    module = _module()
    responses = _transport_responses()
    failed_price_body = deepcopy(responses[1].json_body)
    failed_price_body["metadata"].update(
        state="failed",
        degraded=True,
        reasons=["fixture_required_dataset_failed"],
    )
    responses[1] = HTTPResponse(
        status_code=200,
        json_body=failed_price_body,
    )

    result = _compose(
        module,
        roots=_roots(tmp_path),
        transport=_fixture_transport(module, responses),
        business_ports=_business_ports(
            module,
            include_actionable_order=False,
        ),
    ).run()

    decision = result.bundle.receipt_for(RunStage.DECISION_READY).payload
    assert result.bundle.status == "completed_with_blocks"
    assert result.bundle.stop_new_risk is True
    assert [row["action"] for row in decision["decisions"]] == ["hold"]
    assert (
        result.bundle.receipt_for(RunStage.RISK_CHECKED).payload["approved_orders"]
        == []
    )
    assert result.bundle.current_stage is RunStage.REPORTED
    assert (
        result.bundle.receipt_for(RunStage.LEARNING_RECORDED).payload["recorded"]
        is True
    )
    assert SampleJournal(_roots(tmp_path).journal).read_events()


def test_composition_requires_exact_local_candidate_optimizer_stage(
    tmp_path: Path,
) -> None:
    module = _module()

    with pytest.raises(
        module.PaperRuntimeConfigurationError,
        match="small_account_decision_port_invalid",
    ):
        _compose(
            module,
            roots=_roots(tmp_path),
            transport=_fixture_transport(module),
            small_account_decision_port=object(),
        )


def test_composed_decision_identity_binds_optimizer_and_evidence(
    tmp_path: Path,
) -> None:
    module = _module()
    shared_store = FileRunBundleStore(tmp_path / "shared-run-bundles")
    first = _compose(
        module,
        roots=_roots(tmp_path / "first"),
        transport=_fixture_transport(module),
        bundle_store=shared_store,
        business_ports=_business_ports(
            module,
            candidate_set_id="candidate-set-evidence-v1",
        ),
    ).run()
    first_identity = first.bundle.component_for(RunStage.DECISION_READY)

    assert first_identity.artifact_sha256 != _digest("4")

    with pytest.raises(
        FrozenRuntimeMismatch,
        match="component_manifest_changed_during_restart",
    ):
        _compose(
            module,
            roots=_roots(tmp_path / "second"),
            transport=_fixture_transport(module),
            bundle_store=shared_store,
            business_ports=_business_ports(
                module,
                candidate_set_id="candidate-set-evidence-v2",
            ),
        ).run()


def test_composition_uses_optimizer_plan_for_decision_receipt(
    tmp_path: Path,
) -> None:
    module = _module()

    result = _compose(
        module,
        roots=_roots(tmp_path),
        transport=_fixture_transport(module),
    ).run()
    decision = result.bundle.receipt_for(RunStage.DECISION_READY).payload
    plan = decision["small_account_plan"]

    assert decision["optimizer_plan_sha256"]
    assert plan["capital_authority_id"] == (
        "ashare-composition-offline-fixture-capital-v1"
    )
    assert plan["starting_available_cash_cny"] == 50_000.0
    assert plan["plan_decisions"][0]["order_quantity"] == 200
    assert plan["plan_decisions"][0]["order_quantity"] % 100 == 0
    assert plan["plan_decisions"][0]["estimated_order_cost_cny"] == pytest.approx(
        5.02007
    )
    assert plan["cash_after_orders_cny"] == pytest.approx(47_987.97993)


def test_composition_sell_requires_explicit_intent_and_preserves_t1_limit(
    tmp_path: Path,
) -> None:
    module = _module()
    decision_time = datetime.fromisoformat(DECISION_AS_OF)
    price_time = datetime.fromisoformat("2026-07-16T01:00:00+00:00")
    position = AccountPositionSnapshot(
        symbol="000001.SZ",
        total_shares=300,
        sellable_shares=100,
        mark_price_cny=10.0,
        price_observed_at=price_time,
    )
    account, account_authority_verifier = _verified_fixture_account(
        decision_time=decision_time,
        available_cash_cny=47_000.0,
        current_gross_cny=3_000.0,
        positions=(position,),
    )
    exit_intent = PositionReductionIntent(
        intent_id="fixture-exit-1",
        symbol="000001.SZ",
        action="exit",
        target_shares=0,
        decision_time=decision_time,
    )
    candidates = (
        CandidateAllocationInput(
            symbol="000001.SZ",
            score_evidence=fixture_rank_evidence(
                champion_selection_manifest_sha256=_digest("c"),
                symbol="000001.SZ",
                decision_time=decision_time,
                fixture_id="paper-runtime-exit-candidate",
                source_fixture_sha256=_digest("e"),
                rank_score=0.0,
            ),
            decision_time=decision_time,
            price_observed_at=price_time,
            decision_reference_price=10.0,
        ),
    )
    port = SmallAccountDecisionStagePort(
        identity=ComponentIdentity(
            stage=RunStage.DECISION_READY,
            component_id="small-account-decision-stage",
            version="1",
            artifact_sha256=_digest("4"),
        ),
        account_snapshot=account,
        candidates=candidates,
        reduction_intents=(exit_intent,),
        decision_time=decision_time,
        account_authority_verifier=account_authority_verifier,
        **build_thesis_risk_fixture(
            candidates=candidates,
            account_snapshot=account,
            decision_time=decision_time,
        ),
        runtime_environment="local_candidate",
        promotion_eligible=False,
    )

    result = _compose(
        module,
        roots=_roots(tmp_path),
        transport=_fixture_transport(module),
        small_account_decision_port=port,
    ).run()
    decision = result.bundle.receipt_for(RunStage.DECISION_READY).payload
    row = decision["small_account_plan"]["plan_decisions"][0]
    risk_order = result.bundle.receipt_for(RunStage.RISK_CHECKED).payload[
        "approved_orders"
    ][0]

    assert row["action"] == "reduce"
    assert row["current_shares"] == 300
    assert row["sellable_shares"] == 100
    assert row["order_quantity"] == 100
    assert row["target_shares"] == 200
    assert decision["decisions"][0]["source_reduction_intent_id"] == ("fixture-exit-1")
    assert risk_order["side"] == "sell"
    assert risk_order["quantity"] == 100
    assert risk_order["expected_fee_cny"] == row["estimated_order_cost_cny"]


def test_composition_never_sells_holding_without_explicit_reduction_intent(
    tmp_path: Path,
) -> None:
    module = _module()
    decision_time = datetime.fromisoformat(DECISION_AS_OF)
    price_time = datetime.fromisoformat("2026-07-16T01:00:00+00:00")
    account, account_authority_verifier = _verified_fixture_account(
        decision_time=decision_time,
        available_cash_cny=47_000.0,
        current_gross_cny=3_000.0,
        positions=(
            AccountPositionSnapshot(
                symbol="000001.SZ",
                total_shares=300,
                sellable_shares=100,
                mark_price_cny=10.0,
                price_observed_at=price_time,
            ),
        ),
    )
    port = SmallAccountDecisionStagePort(
        identity=ComponentIdentity(
            stage=RunStage.DECISION_READY,
            component_id="small-account-decision-stage",
            version="1",
            artifact_sha256=_digest("4"),
        ),
        account_snapshot=account,
        candidates=(),
        decision_time=decision_time,
        account_authority_verifier=account_authority_verifier,
        **build_thesis_risk_fixture(
            candidates=(),
            account_snapshot=account,
            decision_time=decision_time,
        ),
        runtime_environment="local_candidate",
        promotion_eligible=False,
    )

    result = _compose(
        module,
        roots=_roots(tmp_path),
        transport=_fixture_transport(module),
        business_ports=_business_ports(
            module,
            include_actionable_order=False,
            include_candidate_evidence=False,
        ),
        small_account_decision_port=port,
    ).run()
    decision = result.bundle.receipt_for(RunStage.DECISION_READY).payload
    plan_row = decision["small_account_plan"]["plan_decisions"][0]

    assert plan_row["action"] == "hold"
    assert plan_row["order_quantity"] == 0
    assert "source_reduction_intent_id" not in decision["decisions"][0]
    assert (
        result.bundle.receipt_for(RunStage.RISK_CHECKED).payload["approved_orders"]
        == []
    )
    assert (
        result.bundle.receipt_for(RunStage.ORDERS_SIMULATED).payload["order_receipts"]
        == []
    )


def test_composition_rejects_optimizer_only_new_risk_without_evidence(
    tmp_path: Path,
) -> None:
    module = _module()

    with pytest.raises(
        module.PaperRuntimeConfigurationError,
        match="optimizer_decision_evidence_missing_for_actionable_symbol",
    ):
        _compose(
            module,
            roots=_roots(tmp_path),
            transport=_fixture_transport(module),
            business_ports=_business_ports(
                module,
                include_candidate_evidence=False,
            ),
        ).run()


def _fixture_transport(module, responses: object | None = None):
    return module.FrozenFixtureHTTPTransport(
        _transport_responses() if responses is None else responses
    )


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("tradingdatas_v1_base_url", ""),
        ("tradingdatas_catalog_version", ""),
        ("tradingdatas_access_policy_id", ""),
        ("dataset_profile", None),
        ("dataset_requests", {}),
        ("evidence_policies", {}),
        ("capital_authority_id", ""),
        ("authority_generation", 0),
        ("execution_lineage", ""),
        ("champion_manifest_sha256", ""),
        ("decision_as_of", datetime(2026, 7, 16, 1, 5)),
    ],
)
def test_missing_or_invalid_authority_and_v1_config_fail_closed(
    field_name: str,
    invalid_value: object,
) -> None:
    module = _module()
    values = _config_kwargs()
    values[field_name] = invalid_value

    with pytest.raises(module.PaperRuntimeConfigurationError):
        module.PaperRuntimeConfig(**values)


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("real_trading_enabled", True),
        ("real_trading_enabled", "false"),
        ("live_execution_enabled", True),
        ("live_execution_enabled", "false"),
        ("network_enabled", True),
        ("network_enabled", "false"),
    ],
)
def test_real_live_and_network_flags_fail_closed(
    field_name: str,
    invalid_value: object,
) -> None:
    module = _module()
    values = _config_kwargs()
    values[field_name] = invalid_value

    with pytest.raises(module.PaperRuntimeConfigurationError):
        module.PaperRuntimeConfig(**values)


def test_decision_as_of_is_normalized_into_context_run_id_and_bundle(
    tmp_path: Path,
) -> None:
    module = _module()
    config = module.PaperRuntimeConfig(**_config_kwargs())

    assert config._run_context.decision_as_of == "2026-07-16T09:05:00+08:00"
    assert config._run_context.to_dict()["decision_as_of"] == (
        "2026-07-16T09:05:00+08:00"
    )
    different_instant = RunContext(
        trade_date="2026-07-16",
        decision_as_of="2026-07-16T09:06:00+08:00",
        market="ashare",
        authority_id="ashare-capital-v1",
        authority_generation=1,
        execution_lineage=LINEAGE,
        account_type="simulated",
        real_trading_enabled=False,
        champion_manifest_sha256=_digest("c"),
    )
    assert different_instant.run_id != config._run_context.run_id

    roots = _roots(tmp_path)
    result = _compose(
        module,
        roots=roots,
        transport=_fixture_transport(module),
    ).run()
    assert result.bundle.to_dict()["context"]["decision_as_of"] == (
        "2026-07-16T09:05:00+08:00"
    )
    projection = json.loads(result.publication.latest_path.read_text(encoding="utf-8"))
    assert projection["context"]["decision_as_of"] == ("2026-07-16T09:05:00+08:00")


def test_decision_as_of_with_wrong_shanghai_trade_date_fails_closed() -> None:
    module = _module()
    values = _config_kwargs()
    wrong_instant = "2026-07-15T15:59:00+00:00"
    values["decision_as_of"] = datetime.fromisoformat(wrong_instant)
    values["dataset_requests"] = {
        PRICE_DATASET: QueryRequest(
            dataset_id=PRICE_DATASET,
            schema_major=1,
            fields=("ts_code", "close"),
            as_of=wrong_instant,
        ),
        CONTEXT_DATASET: QueryRequest(
            dataset_id=CONTEXT_DATASET,
            schema_major=1,
            fields=("sector_id", "breadth"),
            as_of=wrong_instant,
        ),
    }

    with pytest.raises(
        module.PaperRuntimeConfigurationError,
        match="decision_as_of_trade_date_mismatch",
    ):
        module.PaperRuntimeConfig(**values)


class _SelfReportedUnsafeBusinessPort:
    network_enabled = False
    broker_enabled = False

    def __init__(self, stage: RunStage) -> None:
        self.identity = ComponentIdentity(
            stage=stage,
            component_id=f"unsafe-{stage.value}",
            version="1",
            artifact_sha256=_digest("f"),
        )

    def execute(self, _: object) -> object:
        raise AssertionError("unsafe_business_port_must_not_execute")


def test_composition_rejects_self_reported_callable_business_port(
    tmp_path: Path,
) -> None:
    module = _module()
    roots = _roots(tmp_path)
    ports = _business_ports(module)
    ports[RunStage.PREOPEN] = _SelfReportedUnsafeBusinessPort(RunStage.PREOPEN)

    with pytest.raises(
        module.PaperRuntimeConfigurationError,
        match="business_stage_port_must_be_frozen_fixture:preopen",
    ):
        _compose(
            module,
            roots=roots,
            transport=_fixture_transport(module),
            business_ports=ports,
        )


def test_composition_rejects_frozen_fixture_subclass_with_network_behavior(
    tmp_path: Path,
) -> None:
    module = _module()

    class _NetworkCapablePort(module.FrozenFixtureStagePort):
        def network_request(self) -> None:
            raise AssertionError("network_behavior_must_not_be_reachable")

    roots = _roots(tmp_path)
    ports = _business_ports(module)
    ports[RunStage.PREOPEN] = _NetworkCapablePort(
        RunStage.PREOPEN,
        _payloads()[RunStage.PREOPEN],
    )

    with pytest.raises(
        module.PaperRuntimeConfigurationError,
        match="business_stage_port_must_be_frozen_fixture:preopen",
    ):
        _compose(
            module,
            roots=roots,
            transport=_fixture_transport(module),
            business_ports=ports,
        )


def test_composition_rejects_an_unmarked_transport_callable(tmp_path: Path) -> None:
    module = _module()
    roots = _roots(tmp_path)

    def unsafe_transport(**_: object):
        raise AssertionError("must_not_be_called")

    with pytest.raises(
        module.PaperRuntimeConfigurationError,
        match="transport_fixture_must_be_frozen_fixture",
    ):
        _compose(
            module,
            roots=roots,
            transport=unsafe_transport,
        )


def test_composition_rejects_a_self_marked_transport_callable(tmp_path: Path) -> None:
    module = _module()
    roots = _roots(tmp_path)

    class _SelfMarkedTransport:
        offline_fixture = True

        def __call__(self, **_: object):
            raise AssertionError("must_not_be_called")

    with pytest.raises(
        module.PaperRuntimeConfigurationError,
        match="transport_fixture_must_be_frozen_fixture",
    ):
        _compose(
            module,
            roots=roots,
            transport=_SelfMarkedTransport(),
        )


def test_fixture_run_publishes_only_a_validated_completed_local_candidate(
    tmp_path: Path,
) -> None:
    module = _module()
    roots = _roots(tmp_path)
    transport = _fixture_transport(module)

    result = _compose(
        module,
        roots=roots,
        transport=transport,
    ).run()

    assert result.bundle.status == "completed"
    assert result.publication.idempotent is False
    assert [
        (call["method"], call["url"].rsplit("/", 2)[-2:]) for call in transport.calls
    ] == [
        ("GET", ["v1", "catalog"]),
        ("POST", ["v1", "query"]),
        ("POST", ["v1", "query"]),
    ]

    projection = json.loads(result.publication.latest_path.read_text(encoding="utf-8"))
    assert projection["status"] == "completed"
    assert projection["run_id"] == result.bundle.run_id
    assert projection["_projection"] == {
        "authority": "non_authority",
        "bundle_sha256": result.bundle.bundle_sha256,
        "environment": "local_candidate",
        "production_verified": False,
        "record_type": "run_bundle_projection",
        "schema_version": 1,
    }
    assert result.publication.immutable_path.read_bytes() == (
        result.publication.latest_path.read_bytes()
    )
    journal_events = SampleJournal(roots.journal).read_events()
    assert [event["journal_event_type"] for event in journal_events] == [
        "prediction_snapshot",
        "sample_event",
        "sample_event",
        "sample_event",
    ]
    assert journal_events[1]["audit_event_type"] == ("decision_exposure_disposition")
    assert journal_events[1]["eligible_for_statistical_learning"] is False
    assert list(roots.snapshots.glob("snapshot-*.json"))
    assert list(roots.snapshots.glob("decision-*.json"))


class _CrashBeforeEvidenceBundlePersist(FileRunBundleStore):
    def compare_and_swap(
        self,
        *,
        run_id: str,
        expected_bundle_sha256: str | None,
        bundle: RunBundle,
    ) -> None:
        if bundle.current_stage is RunStage.EVIDENCE_READY:
            raise ConcurrentRunUpdate("fixture_crash_before_evidence_persist")
        super().compare_and_swap(
            run_id=run_id,
            expected_bundle_sha256=expected_bundle_sha256,
            bundle=bundle,
        )


def test_restart_uses_persisted_snapshot_and_bundle_without_transport_replay(
    tmp_path: Path,
) -> None:
    module = _module()
    roots = _roots(tmp_path)
    first_transport = _fixture_transport(module)

    with pytest.raises(
        ConcurrentRunUpdate,
        match="fixture_crash_before_evidence_persist",
    ):
        _compose(
            module,
            roots=roots,
            transport=first_transport,
            bundle_store=_CrashBeforeEvidenceBundlePersist(roots.bundles),
        ).run()

    assert len(first_transport.calls) == 3
    assert list(roots.snapshots.glob("snapshot-*.json"))
    assert list(roots.snapshots.glob("decision-*.json"))

    recovery_transport = _fixture_transport(module, [])
    recovered = _compose(
        module,
        roots=roots,
        transport=recovery_transport,
    ).run()
    assert recovered.bundle.status == "completed"
    assert recovery_transport.calls == []

    bundle_event_bytes = {
        path.relative_to(roots.bundles): path.read_bytes()
        for path in roots.bundles.rglob("*.json")
    }
    assert bundle_event_bytes
    journal_bytes = roots.journal.read_bytes()
    snapshot_bytes = {
        path.name: path.read_bytes() for path in roots.snapshots.iterdir()
    }
    publication_bytes = recovered.publication.latest_path.read_bytes()

    restart_transport = _fixture_transport(module, [])
    restarted = _compose(
        module,
        roots=roots,
        transport=restart_transport,
    ).run()

    assert restarted.bundle.bundle_sha256 == recovered.bundle.bundle_sha256
    assert restarted.publication.idempotent is True
    assert restart_transport.calls == []
    assert {
        path.relative_to(roots.bundles): path.read_bytes()
        for path in roots.bundles.rglob("*.json")
    } == bundle_event_bytes
    assert roots.journal.read_bytes() == journal_bytes
    assert {
        path.name: path.read_bytes() for path in roots.snapshots.iterdir()
    } == snapshot_bytes
    assert restarted.publication.latest_path.read_bytes() == publication_bytes


class _TamperingPublisher(LocalRunBundlePublisher):
    def publish(self, bundle: RunBundle):
        published = super().publish(bundle)
        if bundle.status == "completed":
            projection = json.loads(published.latest_path.read_text(encoding="utf-8"))
            projection["_projection"]["bundle_sha256"] = "0" * 64
            published.latest_path.write_text(
                json.dumps(projection, separators=(",", ":"), sort_keys=True) + "\n",
                encoding="utf-8",
            )
        return published


def test_final_projection_readback_mismatch_fails_closed(tmp_path: Path) -> None:
    module = _module()
    roots = _roots(tmp_path)

    with pytest.raises(
        module.PaperRuntimePublicationError,
        match="final_projection_readback_invalid",
    ):
        _compose(
            module,
            roots=roots,
            transport=_fixture_transport(module),
            publisher=_TamperingPublisher(roots.publication),
        ).run()


def test_composition_has_no_legacy_or_direct_storage_fallbacks() -> None:
    module = _module()
    source = Path(module.__file__).read_text(encoding="utf-8").lower()

    for forbidden in (
        "shared_signals_api",
        "shared.data.reader",
        "sqlite",
        "tushare",
        "special_endpoint",
    ):
        assert forbidden not in source
