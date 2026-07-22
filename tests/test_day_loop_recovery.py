from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Mapping

import pytest

from shared.portfolio.champion import fixture_rank_evidence
from shared.portfolio.small_account_optimizer import (
    AccountAuthoritySnapshot,
    AccountPositionSnapshot,
    CandidateAllocationInput,
)
from shared.portfolio.thesis_risk import (
    ThesisRiskRuntimeAuthority,
    apply_group_delta,
)
from shared.runtime.day_loop import (
    ASharePaperDayLoop,
    FaultPoint,
    FrozenRuntimeMismatch,
    MemoryRunBundleStore,
    StageRequest,
    StageResult,
)
from shared.runtime.file_store import FileRunBundleStore
from shared.runtime.run_bundle import (
    ComponentIdentity,
    RunContext,
    RunStage,
    STAGE_ORDER,
)
from tests._thesis_risk_fixture import build_thesis_risk_fixture


DECISION_AS_OF = "2026-07-16T01:05:00+00:00"


def _digest(character: str) -> str:
    return character * 64


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _neutral_drift_constraint() -> dict[str, object]:
    return {
        "schema_version": "tradingagent.drift_runtime_constraint.v1",
        "active_action_receipt_sha256": None,
        "risk_multiplier_cap": 1.0,
        "stop_new_orders": False,
        "reduce_only": False,
        "quarantined": False,
        "review_required": False,
        "reason_codes": [],
    }


def _approved_order(
    *,
    decision_id: str = "d1",
    order_id: str = "o1",
    symbol: str = "000001.SZ",
    intent: str = "open",
) -> dict[str, Any]:
    order: dict[str, Any] = {
        "decision_id": decision_id,
        "order_id": order_id,
        "symbol": symbol,
        "intent": intent,
        "quantity": 100,
        "capital_authority_id": "ashare-capital-v1",
        "authority_generation": 1,
        "execution_lineage": "lineage-v1",
        "risk_receipt_id": f"risk-{order_id}",
        "position_authority_receipt_id": f"position-{order_id}",
        "cash_authority_receipt_id": f"cash-{order_id}",
        "small_account_plan_sha256": "__PLAN_SHA__",
        "reservation_price_cny": 10.0,
        "expected_fee_cny": 5.01,
        "available_cash_before_cny": 50_000.0,
        "session_policy_verified": True,
        "not_suspended": True,
        "limit_fillable": True,
    }
    if intent in {"reduce", "exit"}:
        order.update(
            t_plus_one_eligible=True,
            sellable_quantity=100,
        )
    return order


def _filled_receipt(
    *,
    order_id: str = "o1",
    symbol: str = "000001.SZ",
    intent: str = "open",
) -> dict[str, Any]:
    receipt = {
        "order_id": order_id,
        "symbol": symbol,
        "intent": intent,
        "status": "filled",
        "capital_authority_id": "ashare-capital-v1",
        "authority_generation": 1,
        "execution_lineage": "lineage-v1",
        "requested_quantity": 100,
        "filled_quantity": 100,
        "residual_quantity": 0,
        "terminal_at": "2026-07-16T01:20:00+00:00",
        "filled_at": "2026-07-16T01:20:00+00:00",
        "filled_price_cny": 10.0,
        "fee_cny": 1.0,
        "slippage_cny": 0.5,
        "execution_receipt_id": f"execution-{order_id}",
        "market_evidence_receipt_id": f"market-{order_id}",
        "capital_commit_receipt_id": f"capital-{order_id}",
        "capital_commit_status": "committed",
    }
    receipt["fill_fingerprint"] = _canonical_sha256(receipt)
    return receipt


@dataclass(frozen=True)
class _ScopePolicy:
    identity: ComponentIdentity = ComponentIdentity(
        stage=None,
        component_id="scope",
        version="1",
        artifact_sha256=_digest("a"),
    )

    def order_identity_allowed(self, symbol: str) -> bool:
        return symbol.split(".", 1)[0].startswith(
            ("000", "001", "002", "003", "600", "601", "603", "605")
        )


class _IdempotentPort:
    def __init__(self, stage: RunStage, payload: Mapping[str, Any]) -> None:
        self.identity = ComponentIdentity(
            stage=stage,
            component_id=f"component-{stage.value}",
            version="1",
            artifact_sha256=_digest(str(STAGE_ORDER.index(stage) + 1)),
        )
        self.payload = deepcopy(dict(payload))
        self.calls: list[StageRequest] = []
        self.events: dict[str, StageResult] = {}

    def execute(self, request: StageRequest) -> StageResult:
        self.calls.append(request)
        payload = deepcopy(self.payload)
        if payload.get("source_run_id") == "__RUN_ID__":
            payload["source_run_id"] = request.run_id
        if payload.get("source_input_bundle_sha256") == "__INPUT_BUNDLE_SHA256__":
            payload["source_input_bundle_sha256"] = request.input_bundle_sha256
        if payload.get("order_receipts_sha256") == "__ORDER_RECEIPTS_SHA256__":
            payload["order_receipts_sha256"] = _canonical_sha256(
                request.bundle.receipt_for(RunStage.ORDERS_SIMULATED).payload.get(
                    "order_receipts"
                )
            )
        plan = payload.get("small_account_plan")
        if isinstance(plan, dict) and plan.get("plan_sha256") == "__PLAN_SHA__":
            unsigned_plan = dict(plan)
            unsigned_plan.pop("plan_sha256")
            plan["plan_sha256"] = _canonical_sha256(unsigned_plan)
        if payload.get("small_account_plan_sha256") == "__PLAN_SHA__":
            plan_sha = request.bundle.receipt_for(RunStage.DECISION_READY).payload[
                "small_account_plan"
            ]["plan_sha256"]
            payload["small_account_plan_sha256"] = plan_sha
            for order in payload.get("approved_orders", []):
                if order.get("small_account_plan_sha256") == "__PLAN_SHA__":
                    order["small_account_plan_sha256"] = plan_sha
        return self.events.setdefault(
            request.idempotency_key,
            StageResult(payload=payload),
        )


def _context() -> RunContext:
    return RunContext(
        trade_date="2026-07-16",
        decision_as_of=DECISION_AS_OF,
        market="ashare",
        authority_id="ashare-capital-v1",
        authority_generation=1,
        execution_lineage="lineage-v1",
        account_type="simulated",
        real_trading_enabled=False,
        champion_manifest_sha256=_digest("c"),
    )


def _payloads() -> dict[RunStage, dict[str, Any]]:
    return {
        RunStage.PREOPEN: {
            "market": "ashare",
            "account_type": "simulated",
            "real_trading_enabled": False,
            "account_authority_valid": True,
            "position_authority_valid": True,
        },
        RunStage.EVIDENCE_READY: {
            "profile_id": "mainboard-paper-mvp-input-v1",
            "profile_contract_sha256": _digest("f"),
            "catalog_version": "fixture-catalog-v1",
            "decision_as_of": "2026-07-16T01:05:00+00:00",
            "snapshot_sha256": _digest("e"),
            "execution_eligible": True,
            "historical_pit_eligible": False,
            "blocking_reasons": [],
            "datasets": [
                {
                    "dataset_id": "fixture.cn.equity.daily.v1",
                    "role": "required_execution",
                    "state": "ready",
                    "evidence_action": "accept",
                    "effective_weight": 1.0,
                    "receipt_id": "r1",
                    "row_count": 1,
                    "source_proof_complete": True,
                    "lineage_sha256": _digest("a"),
                    "source_proof_sha256": _digest("b"),
                    "observation_mode": "current_observation",
                    "historical_pit_eligible": False,
                    "identity_fields": ["ts_code", "trade_date"],
                    "identity_sha256": _digest("c"),
                    "row_observation_sha256": _digest("d"),
                    "data_through": "2026-07-16T00:00:00+00:00",
                    "observed_at": "2026-07-16T01:00:00+00:00",
                    "max_row_observed_at": "2026-07-16T01:00:00+00:00",
                    "minimum_row_count": 1,
                    "max_pages": 20,
                    "max_rows": 100_000,
                    "page_count": 1,
                    "pagination_trace_sha256": _digest("e"),
                    "pagination_semantic_sha256": _digest("f"),
                    "page_request_set_sha256": _digest("2"),
                    "page_response_set_sha256": _digest("3"),
                    "cursor_chain_sha256": _digest("1"),
                }
            ],
        },
        RunStage.UNIVERSE_READY: {
            "context_receipt_id": "c1",
            "tradable_receipt_id": "t1",
            "feasible_receipt_id": "f1",
            "context_entities": [],
            "tradable_symbols": ["000001.SZ"],
            "feasible_symbols": ["000001.SZ"],
        },
        RunStage.DECISION_READY: {
            "champion_manifest_sha256": _digest("c"),
            "optimizer_policy_version": "ashare-small-account-50000-v1",
            "small_account_plan": {
                "schema_version": "tradingagent.small_account_plan_receipt.v1",
                "policy_id": "ashare-small-account-50000-v1",
                "cost_policy_id": "ashare-research-cost-v1",
                "capital_authority_id": "ashare-capital-v1",
                "authority_generation": 1,
                "account_as_of": "2026-07-16T01:05:00+00:00",
                "position_snapshot_receipt_id": "position-o1",
                "position_snapshot_sha256": _digest("7"),
                "verification_receipt_sha256": _digest("8"),
                "current_equity_cny": 50_000.0,
                "risk_budget_base_cny": 50_000.0,
                "max_positions": 8,
                "starting_available_cash_cny": 50_000.0,
                "starting_gross_cny": 0.0,
                "target_gross_cny": 1_000.0,
                "cash_after_orders_cny": 48_994.99,
                "plan_decisions": [
                    {
                        "decision_id": "d1",
                        "symbol": "000001.SZ",
                        "action": "open",
                        "current_shares": 0,
                        "sellable_shares": 0,
                        "target_shares": 100,
                        "order_quantity": 100,
                        "valuation_price_cny": 10.0,
                        "reservation_price_cny": 10.0,
                        "estimated_order_cost_cny": 5.01,
                        "target_notional_cny": 1_000.0,
                    }
                ],
                "plan_sha256": "__PLAN_SHA__",
            },
            "decisions": [
                {
                    "decision_id": "d1",
                    "decision_cluster_id": "decision-cluster-d1",
                    "symbol": "000001.SZ",
                    "action": "open",
                    "target_shares": 100,
                    "requested_notional_cny": 1000.0,
                    "score_semantics": "uncalibrated_deterministic_rank_score",
                }
            ],
        },
        RunStage.RISK_CHECKED: {
            "risk_policy_version": "risk-v1",
            "oms_plan_id": "plan-v1",
            "drift_constraint": _neutral_drift_constraint(),
            "drift_constraint_sha256": _canonical_sha256(_neutral_drift_constraint()),
            "small_account_plan_sha256": "__PLAN_SHA__",
            "approved_orders": [_approved_order()],
            "rejected_decisions": [],
        },
        RunStage.ORDERS_SIMULATED: {
            "execution_lineage": "lineage-v1",
            "account_type": "simulated",
            "real_trading_enabled": False,
            "order_receipts": [_filled_receipt()],
            "unknown_order_ids": [],
        },
        RunStage.RECONCILED: {
            "status": "reconciled",
            "account_authority_valid": True,
            "position_authority_valid": True,
            "execution_lineage": "lineage-v1",
            "capital_authority_id": "ashare-capital-v1",
            "authority_generation": 1,
            "source_run_id": "__RUN_ID__",
            "source_input_bundle_sha256": "__INPUT_BUNDLE_SHA256__",
            "reconciled_at": "2026-07-16T01:30:00+00:00",
            "reconciliation_receipt_id": "reconcile-1",
            "capital_ledger_head_sha256": _digest("a"),
            "position_fingerprint": _digest("b"),
            "order_receipts_sha256": "__ORDER_RECEIPTS_SHA256__",
            "account_equity_cny": 50_000.0,
            "cash_cny": 48_994.99,
            "unknown_order_ids": [],
            "unreconciled_order_ids": [],
        },
        RunStage.LEARNING_RECORDED: {
            "recorded": True,
            "record_id": "j1",
            "journal_authority": "SampleJournal",
            "source_run_id": "__RUN_ID__",
            "source_input_bundle_sha256": "__INPUT_BUNDLE_SHA256__",
            "authority_readback_verified": True,
            "journal_event_ids": ["j1"],
            "journal_event_ids_sha256": _canonical_sha256(["j1"]),
            "journal_head_event_count": 1,
            "journal_head_sha256": _digest("c"),
            "journal_source_sha256": _digest("d"),
        },
        RunStage.REPORTED: {
            "reported": True,
            "report_id": "report-1",
            "source_run_id": "__RUN_ID__",
            "source_input_bundle_sha256": "__INPUT_BUNDLE_SHA256__",
            "projection_type": "today_run_local_candidate",
            "local_candidate": True,
            "production_verified": False,
            "artifact_sha256": _digest("e"),
            "readback_sha256": _digest("e"),
        },
    }


def _ports() -> dict[RunStage, _IdempotentPort]:
    return {
        stage: _IdempotentPort(stage, payload) for stage, payload in _payloads().items()
    }


def _bind_thesis_risk_authority(
    ports: Mapping[RunStage, _IdempotentPort],
) -> ThesisRiskRuntimeAuthority:
    """Bind a real immutable test authority to both decision port and plan."""

    decision_time = datetime.fromisoformat(DECISION_AS_OF)
    decision_port = ports[RunStage.DECISION_READY]
    payload = decision_port.payload
    plan = payload["small_account_plan"]
    account_as_of = datetime.fromisoformat(plan["account_as_of"])
    plan_rows = plan["plan_decisions"]
    positions = tuple(
        AccountPositionSnapshot(
            symbol=row["symbol"],
            total_shares=row["current_shares"],
            sellable_shares=row["sellable_shares"],
            mark_price_cny=row["valuation_price_cny"],
            price_observed_at=account_as_of,
        )
        for row in plan_rows
        if row["current_shares"] > 0
    )
    account = AccountAuthoritySnapshot(
        capital_authority_id=plan["capital_authority_id"],
        authority_generation=plan["authority_generation"],
        account_as_of=account_as_of,
        available_cash_cny=plan["starting_available_cash_cny"],
        current_gross_cny=sum(
            position.total_shares * position.mark_price_cny for position in positions
        ),
        positions=positions,
        position_snapshot_receipt_id=plan["position_snapshot_receipt_id"],
        position_snapshot_sha256=plan["position_snapshot_sha256"],
        verification_receipt_sha256=plan["verification_receipt_sha256"],
    )
    candidate_rows = {row["symbol"]: row for row in plan_rows}
    candidates = tuple(
        CandidateAllocationInput(
            symbol=symbol,
            score_evidence=fixture_rank_evidence(
                champion_selection_manifest_sha256=_digest("c"),
                symbol=symbol,
                decision_time=decision_time,
                fixture_id=f"day-loop-recovery-{symbol}",
                source_fixture_sha256=_canonical_sha256(
                    {"contract": "day-loop-recovery", "symbol": symbol}
                ),
                rank_score=0.5,
            ),
            decision_time=decision_time,
            price_observed_at=account_as_of,
            decision_reference_price=row["valuation_price_cny"],
        )
        for symbol, row in sorted(candidate_rows.items())
    )
    authority = build_thesis_risk_fixture(
        candidates=candidates,
        account_snapshot=account,
        decision_time=decision_time,
    )["thesis_risk_authority"]
    assert isinstance(authority, ThesisRiskRuntimeAuthority)
    decision_port.thesis_risk_authority = authority

    plan.update(
        thesis_risk_policy_id=authority.policy.policy_id,
        thesis_risk_policy_sha256=authority.policy.policy_sha256,
        thesis_risk_policy_proof_sha256=authority.policy_proof.proof_sha256,
        thesis_risk_exposure_receipt_sha256s=[
            row.receipt_sha256 for row in authority.exposure_receipts
        ],
        thesis_risk_exposure_proof_sha256s=sorted(
            row.proof_sha256 for row in authority.exposure_proofs
        ),
        thesis_risk_exposure_set_id=(authority.exposure_set_receipt.exposure_set_id),
        thesis_risk_exposure_set_sha256=(authority.exposure_set_receipt.receipt_sha256),
        thesis_risk_exposure_set_proof_sha256=(
            authority.exposure_set_proof.proof_sha256
        ),
        thesis_risk_runtime_authority_sha256=authority.authority_sha256,
        thesis_risk_initial_group_exposures=[
            {
                "dimension": dimension,
                "group_id": group_id,
                "exposure_cny": exposure_cny,
            }
            for dimension, group_id, exposure_cny in (authority.initial_group_exposures)
        ],
    )
    running = {
        (dimension, group_id): exposure_cny
        for dimension, group_id, exposure_cny in authority.initial_group_exposures
    }
    candidate_receipts = {
        row.symbol: row
        for row in authority.exposure_receipts
        if row.exposure_kind == "candidate"
    }
    decision_rows = {row["decision_id"]: row for row in payload["decisions"]}
    for row in plan_rows:
        action = row["action"]
        if action in {"open", "increase"}:
            evaluated_shares = row["order_quantity"]
        elif action in {"reduce", "exit"}:
            evaluated_shares = -row["order_quantity"]
        else:
            evaluated_shares = 0
        effects, cap_exceeded = apply_group_delta(
            exposures=running,
            groups=candidate_receipts[row["symbol"]].groups,
            requested_delta_cny=(evaluated_shares * row["valuation_price_cny"]),
            policy=authority.policy,
            policy_proof_sha256=authority.policy_proof.proof_sha256,
            enforce_cap=True,
        )
        assert cap_exceeded is False
        reason_codes = row.setdefault("reason_codes", [])
        effect_rows = [asdict(effect) for effect in effects]
        row["thesis_risk_evaluated_order_shares"] = evaluated_shares
        row["thesis_risk_group_effects"] = effect_rows
        decision = decision_rows[row["decision_id"]]
        decision["reason_codes"] = reason_codes
        decision["thesis_risk_evaluated_order_shares"] = evaluated_shares
        decision["thesis_risk_group_effects"] = effect_rows
    plan["thesis_risk_final_group_exposures"] = [
        {
            "dimension": dimension,
            "group_id": group_id,
            "exposure_cny": exposure_cny,
        }
        for (dimension, group_id), exposure_cny in sorted(running.items())
    ]
    plan["plan_sha256"] = "__PLAN_SHA__"
    return authority


class _CrashOnce:
    def __init__(self, stage: RunStage, point: FaultPoint) -> None:
        self.stage = stage
        self.point = point
        self.triggered = False

    def __call__(self, stage: RunStage, point: FaultPoint) -> None:
        if not self.triggered and stage is self.stage and point is self.point:
            self.triggered = True
            raise RuntimeError("injected crash")


def _loop(
    ports: Mapping[RunStage, _IdempotentPort],
    store: Any,
    *,
    fault_hook: _CrashOnce | None = None,
) -> ASharePaperDayLoop:
    authority = _bind_thesis_risk_authority(ports)
    return ASharePaperDayLoop(
        ports=ports,
        scope_policy=_ScopePolicy(),
        store=store,
        thesis_risk_authority=authority,
        environ={"REAL_TRADING_ENABLED": "false"},
        fault_hook=fault_hook,
    )


def test_crash_before_persist_replays_same_idempotency_key_and_recovers() -> None:
    store = MemoryRunBundleStore()
    ports = _ports()
    fault = _CrashOnce(
        RunStage.DECISION_READY,
        FaultPoint.AFTER_PORT_BEFORE_PERSIST,
    )

    with pytest.raises(RuntimeError, match="injected crash"):
        _loop(ports, store, fault_hook=fault).run(_context())

    assert len(ports[RunStage.DECISION_READY].calls) == 1
    first_key = ports[RunStage.DECISION_READY].calls[0].idempotency_key
    recovered = _loop(ports, store).run(_context())

    assert recovered.status == "completed"
    assert len(ports[RunStage.DECISION_READY].calls) == 2
    assert ports[RunStage.DECISION_READY].calls[1].idempotency_key == first_key
    assert len(ports[RunStage.PREOPEN].calls) == 1
    assert len(ports[RunStage.EVIDENCE_READY].calls) == 1
    assert len(ports[RunStage.UNIVERSE_READY].calls) == 1


def test_crash_after_persist_does_not_repeat_persisted_stage() -> None:
    store = MemoryRunBundleStore()
    ports = _ports()
    fault = _CrashOnce(RunStage.RISK_CHECKED, FaultPoint.AFTER_PERSIST)

    with pytest.raises(RuntimeError, match="injected crash"):
        _loop(ports, store, fault_hook=fault).run(_context())

    recovered = _loop(ports, store).run(_context())

    assert recovered.status == "completed"
    assert len(ports[RunStage.RISK_CHECKED].calls) == 1


def test_completed_run_is_idempotent_and_does_not_repeat_events() -> None:
    store = MemoryRunBundleStore()
    ports = _ports()
    loop = _loop(ports, store)

    first = loop.run(_context())
    second = loop.run(_context())

    assert first is second
    assert first.bundle_sha256 == second.bundle_sha256
    assert all(len(port.calls) == 1 for port in ports.values())


def test_file_store_recovers_after_restart_with_a_new_store_instance(
    tmp_path,
) -> None:
    root = tmp_path / "run-bundles"
    ports = _ports()
    fault = _CrashOnce(RunStage.DECISION_READY, FaultPoint.AFTER_PERSIST)

    with pytest.raises(RuntimeError, match="injected crash"):
        _loop(
            ports,
            FileRunBundleStore(root),
            fault_hook=fault,
        ).run(_context())

    recovered = _loop(
        ports,
        FileRunBundleStore(root),
    ).run(_context())

    assert recovered.status == "completed"
    assert len(ports[RunStage.DECISION_READY].calls) == 1
    assert FileRunBundleStore(root).load(recovered.run_id) == recovered


def test_restart_rejects_component_hash_drift_before_more_authorities_run() -> None:
    store = MemoryRunBundleStore()
    ports = _ports()
    fault = _CrashOnce(RunStage.UNIVERSE_READY, FaultPoint.AFTER_PERSIST)

    with pytest.raises(RuntimeError, match="injected crash"):
        _loop(ports, store, fault_hook=fault).run(_context())

    ports[RunStage.DECISION_READY].identity = ComponentIdentity(
        stage=RunStage.DECISION_READY,
        component_id="component-decision-ready",
        version="2",
        artifact_sha256=_digest("f"),
    )
    with pytest.raises(
        FrozenRuntimeMismatch,
        match="component_manifest_changed_during_restart",
    ):
        _loop(ports, store).run(_context())

    assert len(ports[RunStage.DECISION_READY].calls) == 0


def test_unknown_order_stops_new_risk_and_invalidates_position_until_reconcile() -> (
    None
):
    ports = _ports()
    ports[RunStage.ORDERS_SIMULATED].payload["order_receipts"].append(
        _filled_receipt(order_id="unknown-order", symbol="600000.SH")
    )
    ports[RunStage.ORDERS_SIMULATED].payload["unknown_order_ids"] = ["unknown-order"]
    ports[RunStage.RECONCILED].payload.update(
        status="blocked",
        account_authority_valid=False,
        position_authority_valid=False,
        unknown_order_ids=["unknown-order"],
        unreconciled_order_ids=["unknown-order"],
    )

    bundle = _loop(ports, MemoryRunBundleStore()).run(_context())

    assert bundle.status == "completed_with_blocks"
    assert bundle.stop_new_risk is True
    assert bundle.position_authority_valid is False
    assert bundle.exit_evaluation_allowed is False
    assert "unknown_simulated_order" in bundle.block_reasons
    assert "account_unreconciled" in bundle.block_reasons
    assert "position_authority_invalid" in bundle.block_reasons


def test_unreconciled_preopen_blocks_new_risk_but_valid_positions_keep_exit_path() -> (
    None
):
    ports = _ports()
    ports[RunStage.PREOPEN].payload.update(
        account_authority_valid=False,
        position_authority_valid=True,
    )
    ports[RunStage.DECISION_READY].payload["decisions"] = [
        {
            "decision_id": "exit-1",
            "decision_cluster_id": "decision-cluster-exit-1",
            "symbol": "000001.SZ",
            "action": "exit",
            "target_shares": 0,
            "requested_notional_cny": 1000.0,
            "score_semantics": "uncalibrated_deterministic_rank_score",
        }
    ]
    ports[RunStage.DECISION_READY].payload["small_account_plan"] = {
        "schema_version": "tradingagent.small_account_plan_receipt.v1",
        "policy_id": "ashare-small-account-50000-v1",
        "cost_policy_id": "ashare-research-cost-v1",
        "capital_authority_id": "ashare-capital-v1",
        "authority_generation": 1,
        "account_as_of": "2026-07-16T01:05:00+00:00",
        "position_snapshot_receipt_id": "position-exit-order-1",
        "position_snapshot_sha256": _digest("7"),
        "verification_receipt_sha256": _digest("8"),
        "current_equity_cny": 50_000.0,
        "risk_budget_base_cny": 50_000.0,
        "max_positions": 8,
        "starting_available_cash_cny": 49_000.0,
        "starting_gross_cny": 1_000.0,
        "target_gross_cny": 0.0,
        "cash_after_orders_cny": 49_994.49,
        "plan_decisions": [
            {
                "decision_id": "exit-1",
                "symbol": "000001.SZ",
                "action": "exit",
                "current_shares": 100,
                "sellable_shares": 100,
                "target_shares": 0,
                "order_quantity": 100,
                "valuation_price_cny": 10.0,
                "reservation_price_cny": 10.0,
                "estimated_order_cost_cny": 5.51,
                "target_notional_cny": 0.0,
            }
        ],
        "plan_sha256": "__PLAN_SHA__",
    }
    exit_order = _approved_order(
        decision_id="exit-1",
        order_id="exit-order-1",
        intent="exit",
    )
    exit_order["available_cash_before_cny"] = 49_000.0
    exit_order["expected_fee_cny"] = 5.51
    ports[RunStage.RISK_CHECKED].payload["approved_orders"] = [exit_order]
    ports[RunStage.ORDERS_SIMULATED].payload["order_receipts"] = [
        _filled_receipt(order_id="exit-order-1", intent="exit")
    ]

    bundle = _loop(ports, MemoryRunBundleStore()).run(_context())

    assert bundle.stop_new_risk is True
    assert "account_authority_invalid" in bundle.block_reasons
    assert ports[RunStage.DECISION_READY].calls[0].allowed_actions == (
        "reduce",
        "exit",
        "hold",
    )
    assert ports[RunStage.ORDERS_SIMULATED].calls[0].permitted_order_ids == (
        "exit-order-1",
    )
