from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Any, Mapping

import pytest

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
    ThesisRiskRuntimeAuthority,
    build_thesis_risk_runtime_authority,
)
from shared.runtime.day_loop import (
    ASharePaperDayLoop,
    FrozenRuntimeMismatch,
    MemoryRunBundleStore,
    StageRequest,
    StageResult,
)
from shared.runtime.run_bundle import (
    ComponentIdentity,
    RunContext,
    RunStage,
    STAGE_ORDER,
)


def _digest(character: str) -> str:
    return character * 64


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


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


_THESIS_RISK_DECISION_TIME = datetime(
    2026,
    7,
    16,
    1,
    5,
    tzinfo=timezone.utc,
)
_PRIVATE_THESIS_RISK_AUTHORITY = "__thesis_risk_runtime_authority__"


class _FrozenThesisRiskPolicyVerifier:
    def __init__(self, expected: ThesisRiskPolicy) -> None:
        self._expected = expected

    def verify(self, policy, *, decision_time):
        if policy != self._expected:
            raise ValueError("unexpected_thesis_risk_policy")
        return ThesisRiskPolicyVerification.create(
            policy=policy,
            verifier_id="day-loop-test-thesis-policy-verifier",
            verifier_version="1",
            verified_at=decision_time - timedelta(seconds=1),
            valid_until=decision_time + timedelta(hours=1),
            promotion_eligible=False,
        )


class _FrozenThesisRiskExposureVerifier:
    def __init__(self, expected: tuple[ThesisRiskExposureReceipt, ...]) -> None:
        self._expected = {receipt.exposure_id: receipt for receipt in expected}

    def verify(self, receipt, *, decision_time):
        if self._expected.get(receipt.exposure_id) != receipt:
            raise ValueError("unexpected_thesis_risk_exposure")
        return ThesisRiskExposureVerification.create(
            receipt=receipt,
            verifier_id="day-loop-test-thesis-exposure-verifier",
            verifier_version="1",
            verified_at=decision_time - timedelta(seconds=1),
            valid_until=decision_time + timedelta(hours=1),
            promotion_eligible=False,
            authority_notional_cny=receipt.notional_cny,
            authority_binding_reference_id=receipt.binding_reference_id,
            authority_binding_sha256=receipt.binding_sha256,
        )


class _FrozenThesisRiskExposureSetVerifier:
    def __init__(self, expected: ThesisRiskExposureSetReceipt) -> None:
        self._expected = expected

    def verify(self, receipt, *, decision_time):
        if receipt != self._expected:
            raise ValueError("unexpected_thesis_risk_exposure_set")
        return ThesisRiskExposureSetVerification.create(
            receipt=receipt,
            verifier_id="day-loop-test-thesis-set-verifier",
            verifier_version="1",
            verified_at=decision_time - timedelta(seconds=1),
            valid_until=decision_time + timedelta(hours=1),
            promotion_eligible=False,
        )


def _groups_for(symbol: str) -> ThesisRiskGroups:
    return ThesisRiskGroups(
        **{
            dimension: f"fixture-{dimension}-{symbol}"
            for dimension in THESIS_RISK_DIMENSIONS
        }
    )


def _bind_thesis_risk_fixture(
    decision_payload: dict[str, Any],
) -> ThesisRiskRuntimeAuthority:
    """Bind manual plan rows to one explicit, frozen, non-promotable authority."""

    plan = decision_payload["small_account_plan"]
    plan_rows = plan["plan_decisions"]
    decisions = {row["decision_id"]: row for row in decision_payload["decisions"]}
    policy = ThesisRiskPolicy(
        policy_id="tests-human-reviewed-thesis-risk-v1",
        reviewed_by="day-loop-test-explicit-reviewer",
        review_reference="day-loop-test-review-20260716",
        effective_at=_THESIS_RISK_DECISION_TIME - timedelta(days=1),
        valid_until=_THESIS_RISK_DECISION_TIME + timedelta(days=30),
        dimension_caps=tuple(
            ThesisRiskDimensionCap(
                dimension=dimension,
                max_exposure_cny=50_000.0,
            )
            for dimension in THESIS_RISK_DIMENSIONS
        ),
    )
    receipts: list[ThesisRiskExposureReceipt] = []
    groups_by_decision: dict[str, ThesisRiskGroups] = {}
    for row in plan_rows:
        decision_id = row["decision_id"]
        symbol = row["symbol"]
        groups = _groups_for(symbol)
        groups_by_decision[decision_id] = groups
        current_notional = round(
            row["current_shares"] * row["valuation_price_cny"],
            6,
        )
        exposure_kind = "position" if current_notional > 0.0 else "candidate"
        binding_reference_id = (
            plan["position_snapshot_receipt_id"]
            if exposure_kind == "position"
            else decision_id
        )
        binding_sha256 = _canonical_sha256(
            {
                "binding_reference_id": binding_reference_id,
                "decision_id": decision_id,
                "exposure_kind": exposure_kind,
                "symbol": symbol,
            }
        )
        receipts.append(
            ThesisRiskExposureReceipt.create(
                exposure_id=f"{exposure_kind}-{decision_id}",
                exposure_kind=exposure_kind,
                symbol=symbol,
                groups=groups,
                notional_cny=current_notional,
                as_of=_THESIS_RISK_DECISION_TIME,
                available_at=_THESIS_RISK_DECISION_TIME,
                source_dataset_id=f"tests.day-loop.{exposure_kind}.v1",
                source_receipt_id=f"source-{decision_id}",
                source_lineage_sha256=_digest("1"),
                source_content_sha256=_digest("2"),
                binding_reference_id=binding_reference_id,
                binding_sha256=binding_sha256,
            )
        )
    frozen_receipts = tuple(receipts)
    exposure_set = ThesisRiskExposureSetReceipt.create(
        exposure_set_id="day-loop-test-frozen-thesis-risk-book-v1",
        receipts=frozen_receipts,
        decision_time=_THESIS_RISK_DECISION_TIME,
        as_of=_THESIS_RISK_DECISION_TIME,
        available_at=_THESIS_RISK_DECISION_TIME,
        source_id="tests.day-loop.frozen-thesis-risk-book.v1",
        source_generation=1,
        source_lineage_sha256=_digest("3"),
    )
    authority = build_thesis_risk_runtime_authority(
        policy=policy,
        policy_verifier=_FrozenThesisRiskPolicyVerifier(policy),
        exposure_receipts=frozen_receipts,
        exposure_verifier=_FrozenThesisRiskExposureVerifier(frozen_receipts),
        exposure_set_receipt=exposure_set,
        exposure_set_verifier=_FrozenThesisRiskExposureSetVerifier(exposure_set),
        decision_time=_THESIS_RISK_DECISION_TIME,
    )

    running_exposures = {
        (dimension, group_id): exposure_cny
        for dimension, group_id, exposure_cny in authority.initial_group_exposures
    }
    for row in plan_rows:
        decision_id = row["decision_id"]
        action = row["action"]
        order_quantity = row["order_quantity"]
        reason_codes = list(row.get("reason_codes", []))
        if action in {"open", "increase"}:
            evaluated_order_shares = order_quantity
        elif action in {"reduce", "exit"}:
            evaluated_order_shares = -order_quantity
        elif "risk_group_cap" in reason_codes:
            evaluated_order_shares = int(
                row.get("thesis_risk_evaluated_order_shares", 100)
            )
        else:
            evaluated_order_shares = 0
        requested_delta = round(
            evaluated_order_shares * row["valuation_price_cny"],
            6,
        )
        applied_delta = 0.0 if "risk_group_cap" in reason_codes else requested_delta
        effects = []
        for dimension, group_id in groups_by_decision[decision_id].items():
            key = (dimension, group_id)
            pre = float(running_exposures.get(key, 0.0))
            requested_post = max(0.0, round(pre + requested_delta, 6))
            post = max(0.0, round(pre + applied_delta, 6))
            effects.append(
                {
                    "dimension": dimension,
                    "group_id": group_id,
                    "pre_exposure_cny": pre,
                    "requested_delta_cny": requested_delta,
                    "requested_post_exposure_cny": requested_post,
                    "delta_cny": applied_delta,
                    "post_exposure_cny": post,
                    "cap_cny": policy.cap_for(dimension),
                    "policy_proof_sha256": authority.policy_proof.proof_sha256,
                }
            )
            running_exposures[key] = post
        row["reason_codes"] = reason_codes
        row["thesis_risk_evaluated_order_shares"] = evaluated_order_shares
        row["thesis_risk_group_effects"] = effects
        decision = decisions[decision_id]
        decision["reason_codes"] = reason_codes
        decision["thesis_risk_evaluated_order_shares"] = evaluated_order_shares
        decision["thesis_risk_group_effects"] = deepcopy(effects)
    plan.update(
        thesis_risk_policy_id=authority.policy.policy_id,
        thesis_risk_policy_sha256=authority.policy.policy_sha256,
        thesis_risk_policy_proof_sha256=authority.policy_proof.proof_sha256,
        thesis_risk_exposure_receipt_sha256s=[
            receipt.receipt_sha256 for receipt in authority.exposure_receipts
        ],
        thesis_risk_exposure_proof_sha256s=sorted(
            proof.proof_sha256 for proof in authority.exposure_proofs
        ),
        thesis_risk_exposure_set_id=authority.exposure_set_receipt.exposure_set_id,
        thesis_risk_exposure_set_sha256=authority.exposure_set_receipt.receipt_sha256,
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
        thesis_risk_final_group_exposures=[
            {
                "dimension": dimension,
                "group_id": group_id,
                "exposure_cny": exposure,
            }
            for (dimension, group_id), exposure in sorted(running_exposures.items())
        ],
        plan_sha256="__PLAN_SHA__",
    )
    decision_payload[_PRIVATE_THESIS_RISK_AUTHORITY] = authority
    return authority


@dataclass(frozen=True)
class _ScopePolicy:
    identity: ComponentIdentity = ComponentIdentity(
        stage=None,
        component_id="mainboard-scope-policy",
        version="1",
        artifact_sha256=_digest("a"),
    )

    def order_identity_allowed(self, symbol: str) -> bool:
        code = symbol.split(".", 1)[0]
        return code.startswith(("000", "001", "002", "003", "600", "601", "603", "605"))


class _Port:
    def __init__(self, stage: RunStage, payload: Mapping[str, Any]) -> None:
        self.identity = ComponentIdentity(
            stage=stage,
            component_id=f"fixture-{stage.value}",
            version="1",
            artifact_sha256=_digest(str(STAGE_ORDER.index(stage) + 1)),
        )
        raw_payload = dict(payload)
        authority = raw_payload.pop(_PRIVATE_THESIS_RISK_AUTHORITY, None)
        self.payload = deepcopy(raw_payload)
        if stage is RunStage.DECISION_READY:
            if not isinstance(authority, ThesisRiskRuntimeAuthority):
                raise ValueError("decision_thesis_risk_authority_fixture_required")
            self.thesis_risk_authority = authority
        self.calls: list[StageRequest] = []

    def execute(self, request: StageRequest) -> StageResult:
        self.calls.append(request)
        payload = deepcopy(self.payload)
        if payload.get("source_run_id") == "__RUN_ID__":
            payload["source_run_id"] = request.run_id
        if payload.get("source_input_bundle_sha256") == "__INPUT_BUNDLE_SHA__":
            payload["source_input_bundle_sha256"] = request.input_bundle_sha256
        if payload.get("order_receipts_sha256") == "__ORDER_RECEIPTS_SHA__":
            receipts = request.bundle.receipt_for(RunStage.ORDERS_SIMULATED).payload[
                "order_receipts"
            ]
            payload["order_receipts_sha256"] = _canonical_sha256(receipts)
        plan = payload.get("small_account_plan")
        if isinstance(plan, dict) and plan.get("plan_sha256") == "__PLAN_SHA__":
            unsigned_plan = dict(plan)
            unsigned_plan.pop("plan_sha256")
            plan["plan_sha256"] = _canonical_sha256(unsigned_plan)
        if payload.get("small_account_plan_sha256") == "__PLAN_SHA__":
            decision_payload = request.bundle.receipt_for(
                RunStage.DECISION_READY
            ).payload
            payload["small_account_plan_sha256"] = decision_payload[
                "small_account_plan"
            ]["plan_sha256"]
            for order in payload.get("approved_orders", []):
                if order.get("small_account_plan_sha256") == "__PLAN_SHA__":
                    order["small_account_plan_sha256"] = payload[
                        "small_account_plan_sha256"
                    ]
        if request.stage is RunStage.ORDERS_SIMULATED:
            for receipt in payload.get("order_receipts", []):
                if receipt.get("fill_fingerprint") == "__CANONICAL_FILL_FINGERPRINT__":
                    fingerprint_payload = dict(receipt)
                    fingerprint_payload.pop("fill_fingerprint", None)
                    receipt["fill_fingerprint"] = _canonical_sha256(fingerprint_payload)
        return StageResult(payload=payload)


def _context() -> RunContext:
    return RunContext(
        trade_date="2026-07-16",
        market="ashare",
        authority_id="ashare-capital-v1",
        authority_generation=1,
        execution_lineage="ashare-sim-fresh-test-v1",
        account_type="simulated",
        real_trading_enabled=False,
        champion_manifest_sha256=_digest("c"),
        decision_as_of=_THESIS_RISK_DECISION_TIME,
    )


def _resign_small_account_plan(decision_payload: dict) -> None:
    plan = decision_payload["small_account_plan"]
    unsigned_plan = dict(plan)
    unsigned_plan.pop("plan_sha256", None)
    plan["plan_sha256"] = _canonical_sha256(unsigned_plan)


def _payloads() -> dict[RunStage, dict[str, Any]]:
    payloads = {
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
                    "receipt_id": "receipt-daily-1",
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
            "context_receipt_id": "context-1",
            "tradable_receipt_id": "tradable-1",
            "feasible_receipt_id": "feasible-1",
            "context_entities": [
                {
                    "entity_id": "399006.SZ",
                    "entity_type": "index",
                    "context_only": True,
                    "order_identity_allowed": False,
                },
                {
                    "entity_id": "000688.SH",
                    "entity_type": "index",
                    "context_only": True,
                    "order_identity_allowed": False,
                },
            ],
            "tradable_symbols": ["000001.SZ", "600000.SH"],
            "feasible_symbols": ["000001.SZ", "600000.SH"],
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
                "position_snapshot_receipt_id": "position-authority-1",
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
                        "decision_id": "decision-1",
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
                    "decision_id": "decision-1",
                    "decision_cluster_id": "decision-cluster-1",
                    "symbol": "000001.SZ",
                    "action": "open",
                    "target_shares": 100,
                    "requested_notional_cny": 1000.0,
                    "score_semantics": "uncalibrated_deterministic_rank_score",
                }
            ],
        },
        RunStage.RISK_CHECKED: {
            "risk_policy_version": "ashare-capital-policy-v1",
            "oms_plan_id": "oms-plan-1",
            "drift_constraint": _neutral_drift_constraint(),
            "drift_constraint_sha256": _canonical_sha256(_neutral_drift_constraint()),
            "small_account_plan_sha256": "__PLAN_SHA__",
            "approved_orders": [
                {
                    "decision_id": "decision-1",
                    "order_id": "order-1",
                    "symbol": "000001.SZ",
                    "intent": "open",
                    "quantity": 100,
                    "reservation_price_cny": 10.0,
                    "expected_fee_cny": 5.01,
                    "available_cash_before_cny": 50_000.0,
                    "sellable_quantity": 0,
                    "capital_authority_id": "ashare-capital-v1",
                    "authority_generation": 1,
                    "execution_lineage": "ashare-sim-fresh-test-v1",
                    "risk_receipt_id": "risk-order-1",
                    "position_authority_receipt_id": "position-authority-1",
                    "cash_authority_receipt_id": "cash-authority-1",
                    "small_account_plan_sha256": "__PLAN_SHA__",
                    "session_policy_verified": True,
                    "not_suspended": True,
                    "limit_fillable": True,
                }
            ],
            "rejected_decisions": [],
        },
        RunStage.ORDERS_SIMULATED: {
            "execution_lineage": "ashare-sim-fresh-test-v1",
            "account_type": "simulated",
            "real_trading_enabled": False,
            "order_receipts": [
                {
                    "order_id": "order-1",
                    "symbol": "000001.SZ",
                    "intent": "open",
                    "status": "filled",
                    "requested_quantity": 100,
                    "filled_quantity": 100,
                    "residual_quantity": 0,
                    "filled_price_cny": 10.02,
                    "fee_cny": 5.0,
                    "slippage_cny": 2.0,
                    "filled_at": "2026-07-16T01:31:00+00:00",
                    "terminal_at": "2026-07-16T01:31:00+00:00",
                    "execution_receipt_id": "execution-order-1",
                    "market_evidence_receipt_id": "market-order-1",
                    "capital_commit_receipt_id": "capital-order-1",
                    "capital_commit_status": "committed",
                    "fill_fingerprint": "__CANONICAL_FILL_FINGERPRINT__",
                    "capital_authority_id": "ashare-capital-v1",
                    "authority_generation": 1,
                    "execution_lineage": "ashare-sim-fresh-test-v1",
                }
            ],
            "unknown_order_ids": [],
        },
        RunStage.RECONCILED: {
            "status": "reconciled",
            "account_authority_valid": True,
            "position_authority_valid": True,
            "execution_lineage": "ashare-sim-fresh-test-v1",
            "capital_authority_id": "ashare-capital-v1",
            "authority_generation": 1,
            "source_run_id": "__RUN_ID__",
            "source_input_bundle_sha256": "__INPUT_BUNDLE_SHA__",
            "reconciled_at": "2026-07-16T01:35:00+00:00",
            "reconciliation_receipt_id": "reconcile-1",
            "capital_ledger_head_sha256": _digest("a"),
            "position_fingerprint": _digest("b"),
            "order_receipts_sha256": "__ORDER_RECEIPTS_SHA__",
            "account_equity_cny": 49_993.0,
            "cash_cny": 48_993.0,
            "unknown_order_ids": [],
            "unreconciled_order_ids": [],
        },
        RunStage.LEARNING_RECORDED: {
            "recorded": True,
            "record_id": "sample:sample-journal-record-1",
            "journal_authority": "SampleJournal",
            "source_run_id": "__RUN_ID__",
            "source_input_bundle_sha256": "__INPUT_BUNDLE_SHA__",
            "authority_readback_verified": True,
            "journal_event_ids": ["sample:sample-journal-record-1"],
            "journal_event_ids_sha256": _canonical_sha256(
                ["sample:sample-journal-record-1"]
            ),
            "journal_head_event_count": 1,
            "journal_head_sha256": _digest("a"),
            "journal_source_sha256": _digest("b"),
        },
        RunStage.REPORTED: {
            "reported": True,
            "report_id": "today-report-1",
            "source_run_id": "__RUN_ID__",
            "source_input_bundle_sha256": "__INPUT_BUNDLE_SHA__",
            "projection_type": "today_run_local_candidate",
            "local_candidate": True,
            "production_verified": False,
            "artifact_sha256": _digest("f"),
            "readback_sha256": _digest("f"),
        },
    }
    _bind_thesis_risk_fixture(payloads[RunStage.DECISION_READY])
    return payloads


def _ports(
    overrides: Mapping[RunStage, Mapping[str, Any]] | None = None,
) -> dict[RunStage, _Port]:
    payloads = _payloads()
    for stage, payload in (overrides or {}).items():
        payloads[stage] = deepcopy(dict(payload))
    return {stage: _Port(stage, payloads[stage]) for stage in STAGE_ORDER}


def _loop(
    ports: Mapping[RunStage, _Port],
    *,
    store: MemoryRunBundleStore | None = None,
) -> ASharePaperDayLoop:
    authority = ports[RunStage.DECISION_READY].thesis_risk_authority
    return ASharePaperDayLoop(
        ports=ports,
        scope_policy=_ScopePolicy(),
        store=store or MemoryRunBundleStore(),
        thesis_risk_authority=authority,
        environ={"REAL_TRADING_ENABLED": "false"},
    )


def test_complete_simulated_day_emits_all_immutable_stage_receipts() -> None:
    ports = _ports()
    loop = _loop(ports)

    bundle = loop.run(_context())

    assert tuple(receipt.stage for receipt in bundle.stage_receipts) == STAGE_ORDER
    assert bundle.current_stage is RunStage.REPORTED
    assert bundle.status == "completed"
    assert bundle.stop_new_risk is False
    assert bundle.position_authority_valid is True
    assert bundle.exit_evaluation_allowed is True
    assert bundle.run_id.startswith("ashare-paper-day-")
    assert len(bundle.run_id.removeprefix("ashare-paper-day-")) == 32
    assert len(bundle.component_manifest_sha256) == 64
    assert len(bundle.bundle_sha256) == 64
    assert all(receipt.status == "completed" for receipt in bundle.stage_receipts)
    assert all(len(receipt.receipt_id) == 64 for receipt in bundle.stage_receipts)
    assert all(
        len(receipt.input_bundle_sha256) == 64 for receipt in bundle.stage_receipts
    )
    assert all(len(receipt.payload_sha256) == 64 for receipt in bundle.stage_receipts)
    assert all(len(port.calls) == 1 for port in ports.values())
    assert ports[RunStage.ORDERS_SIMULATED].calls[0].permitted_order_ids == ("order-1",)
    assert ports[RunStage.LEARNING_RECORDED].calls[0].idempotency_key
    with pytest.raises((AttributeError, TypeError)):
        bundle.stage_receipts = ()  # type: ignore[misc]


def test_run_next_advances_exactly_one_persisted_stage() -> None:
    ports = _ports()
    store = MemoryRunBundleStore()
    loop = _loop(ports, store=store)

    first = loop.run_next(_context(), expected_stage=RunStage.PREOPEN)

    assert tuple(receipt.stage for receipt in first.stage_receipts) == (
        RunStage.PREOPEN,
    )
    assert first.next_stage is RunStage.EVIDENCE_READY
    assert len(ports[RunStage.PREOPEN].calls) == 1
    assert all(
        len(port.calls) == 0
        for stage, port in ports.items()
        if stage is not RunStage.PREOPEN
    )
    assert store.load(first.run_id) == first

    second = loop.run_next(_context(), expected_stage=RunStage.EVIDENCE_READY)

    assert tuple(receipt.stage for receipt in second.stage_receipts) == (
        RunStage.PREOPEN,
        RunStage.EVIDENCE_READY,
    )
    assert second.next_stage is RunStage.UNIVERSE_READY
    assert len(ports[RunStage.PREOPEN].calls) == 1
    assert len(ports[RunStage.EVIDENCE_READY].calls) == 1


def test_run_next_expected_stage_mismatch_has_no_port_or_store_side_effect() -> None:
    ports = _ports()
    store = MemoryRunBundleStore()
    loop = _loop(ports, store=store)

    with pytest.raises(FrozenRuntimeMismatch, match="next_stage_mismatch"):
        loop.run_next(_context(), expected_stage=RunStage.RISK_CHECKED)

    assert store.load(_context().run_id) is None
    assert all(len(port.calls) == 0 for port in ports.values())


def test_run_until_stops_at_requested_session_boundary_and_run_resumes() -> None:
    ports = _ports()
    store = MemoryRunBundleStore()
    loop = _loop(ports, store=store)

    risk_checked = loop.run_until(_context(), through_stage=RunStage.RISK_CHECKED)

    assert tuple(receipt.stage for receipt in risk_checked.stage_receipts) == (
        RunStage.PREOPEN,
        RunStage.EVIDENCE_READY,
        RunStage.UNIVERSE_READY,
        RunStage.DECISION_READY,
        RunStage.RISK_CHECKED,
    )
    assert risk_checked.next_stage is RunStage.ORDERS_SIMULATED
    completed_stages = {receipt.stage for receipt in risk_checked.stage_receipts}
    assert all(
        len(ports[stage].calls) == (1 if stage in completed_stages else 0)
        for stage in STAGE_ORDER
    )

    completed = loop.run(_context())

    assert completed.current_stage is RunStage.REPORTED
    assert all(len(port.calls) == 1 for port in ports.values())


def test_run_next_on_completed_bundle_is_idempotent() -> None:
    ports = _ports()
    store = MemoryRunBundleStore()
    loop = _loop(ports, store=store)
    completed = loop.run(_context())
    call_counts = {stage: len(port.calls) for stage, port in ports.items()}

    replay = loop.run_next(_context())

    assert replay == completed
    assert {stage: len(port.calls) for stage, port in ports.items()} == call_counts


@pytest.mark.parametrize(
    ("mutate", "expected_reason"),
    [
        (
            lambda risk: risk.pop("drift_constraint"),
            "drift_constraint_contract_invalid",
        ),
        (
            lambda risk: risk.__setitem__("drift_constraint_sha256", _digest("f")),
            "drift_constraint_digest_invalid",
        ),
        (
            lambda risk: risk["drift_constraint"].__setitem__("reduce_only", True),
            "drift_constraint_contract_invalid",
        ),
    ],
)
def test_risk_stage_recomputes_drift_constraint_fail_closed(
    mutate,
    expected_reason: str,
) -> None:
    payloads = _payloads()
    mutate(payloads[RunStage.RISK_CHECKED])
    ports = {stage: _Port(stage, payloads[stage]) for stage in STAGE_ORDER}

    bundle = _loop(ports).run(_context())

    assert expected_reason in bundle.block_reasons
    assert bundle.stop_new_risk is True


def test_risk_stage_must_dispose_every_non_hold_decision() -> None:
    payloads = _payloads()
    payloads[RunStage.RISK_CHECKED]["approved_orders"] = []
    payloads[RunStage.RISK_CHECKED]["rejected_decisions"] = []
    payloads[RunStage.ORDERS_SIMULATED]["order_receipts"] = []
    ports = {stage: _Port(stage, payloads[stage]) for stage in STAGE_ORDER}

    bundle = _loop(ports).run(_context())

    assert bundle.stop_new_risk is True
    assert "decision_disposition_missing" in bundle.block_reasons


@pytest.mark.parametrize(
    ("dataset_state", "action", "expected_reason"),
    [
        ("stale", "reject", "dataset_stale"),
        ("failed", "reject", "dataset_failed"),
    ],
)
def test_bad_dataset_state_stops_new_risk_but_keeps_reduce_exit_path(
    dataset_state: str,
    action: str,
    expected_reason: str,
) -> None:
    payloads = _payloads()
    payloads[RunStage.EVIDENCE_READY]["datasets"][0].update(
        state=dataset_state,
        evidence_action=action,
        effective_weight=0.0,
    )
    payloads[RunStage.EVIDENCE_READY].update(
        execution_eligible=False,
        blocking_reasons=["required_dataset_rejected:cn.equity.daily"],
    )
    payloads[RunStage.DECISION_READY]["decisions"] = [
        {
            "decision_id": "exit-1",
            "decision_cluster_id": "decision-cluster-exit-1",
            "symbol": "600000.SH",
            "action": "exit",
            "target_shares": 0,
            "requested_notional_cny": 1000.0,
            "score_semantics": "uncalibrated_deterministic_rank_score",
        }
    ]
    payloads[RunStage.DECISION_READY]["small_account_plan"] = {
        "schema_version": "tradingagent.small_account_plan_receipt.v1",
        "policy_id": "ashare-small-account-50000-v1",
        "cost_policy_id": "ashare-research-cost-v1",
        "capital_authority_id": "ashare-capital-v1",
        "authority_generation": 1,
        "account_as_of": "2026-07-16T01:05:00+00:00",
        "position_snapshot_receipt_id": "position-exit-1",
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
                "symbol": "600000.SH",
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
    _bind_thesis_risk_fixture(payloads[RunStage.DECISION_READY])
    payloads[RunStage.RISK_CHECKED]["small_account_plan_sha256"] = "__PLAN_SHA__"
    payloads[RunStage.RISK_CHECKED]["approved_orders"] = [
        {
            "decision_id": "exit-1",
            "order_id": "exit-order-1",
            "symbol": "600000.SH",
            "intent": "exit",
            "quantity": 100,
            "reservation_price_cny": 10.0,
            "expected_fee_cny": 5.51,
            "available_cash_before_cny": 49_000.0,
            "sellable_quantity": 100,
            "t_plus_one_eligible": True,
            "capital_authority_id": "ashare-capital-v1",
            "authority_generation": 1,
            "execution_lineage": "ashare-sim-fresh-test-v1",
            "risk_receipt_id": "risk-exit-1",
            "position_authority_receipt_id": "position-exit-1",
            "cash_authority_receipt_id": "cash-exit-1",
            "small_account_plan_sha256": "__PLAN_SHA__",
            "session_policy_verified": True,
            "not_suspended": True,
            "limit_fillable": True,
        }
    ]
    payloads[RunStage.ORDERS_SIMULATED]["order_receipts"] = [
        {
            "order_id": "exit-order-1",
            "symbol": "600000.SH",
            "intent": "exit",
            "status": "filled",
            "requested_quantity": 100,
            "filled_quantity": 100,
            "residual_quantity": 0,
            "filled_price_cny": 9.98,
            "fee_cny": 5.0,
            "slippage_cny": 2.0,
            "filled_at": "2026-07-16T01:31:00+00:00",
            "terminal_at": "2026-07-16T01:31:00+00:00",
            "execution_receipt_id": "execution-exit-1",
            "market_evidence_receipt_id": "market-exit-1",
            "capital_commit_receipt_id": "capital-exit-1",
            "capital_commit_status": "committed",
            "fill_fingerprint": "__CANONICAL_FILL_FINGERPRINT__",
            "capital_authority_id": "ashare-capital-v1",
            "authority_generation": 1,
            "execution_lineage": "ashare-sim-fresh-test-v1",
        }
    ]
    ports = {stage: _Port(stage, payloads[stage]) for stage in STAGE_ORDER}

    bundle = _loop(ports).run(_context())

    assert bundle.status == "completed_with_blocks"
    assert bundle.stop_new_risk is True
    assert expected_reason in bundle.block_reasons
    assert bundle.position_authority_valid is True
    assert bundle.exit_evaluation_allowed is True
    decision_request = ports[RunStage.DECISION_READY].calls[0]
    assert decision_request.allowed_actions == ("reduce", "exit", "hold")
    assert ports[RunStage.ORDERS_SIMULATED].calls[0].permitted_order_ids == (
        "exit-order-1",
    )


@pytest.mark.parametrize("invalid_weight", [None, True, 0.0, 1.0])
def test_degraded_dataset_requires_an_explicit_bounded_deweight(
    invalid_weight: object,
) -> None:
    payloads = _payloads()
    dataset = payloads[RunStage.EVIDENCE_READY]["datasets"][0]
    dataset.update(
        state="degraded",
        evidence_action="deweight",
        effective_weight=invalid_weight,
    )
    ports = {stage: _Port(stage, payloads[stage]) for stage in STAGE_ORDER}

    bundle = _loop(ports).run(_context())

    assert bundle.stop_new_risk is True
    assert "dataset_deweight_invalid" in bundle.block_reasons


def test_optional_context_can_deweight_without_unblocking_required_data() -> None:
    payloads = _payloads()
    payloads[RunStage.EVIDENCE_READY]["datasets"].append(
        {
            "dataset_id": "fixture.cn.equity.growth-board.context.v1",
            "role": "optional_context",
            "state": "degraded",
            "evidence_action": "deweight",
            "effective_weight": 0.25,
            "receipt_id": "receipt-growth-context-1",
            "row_count": 1,
            "source_proof_complete": True,
            "lineage_sha256": _digest("2"),
            "source_proof_sha256": _digest("3"),
            "observation_mode": "current_observation",
            "historical_pit_eligible": False,
            "identity_fields": ["ts_code", "trade_date"],
            "identity_sha256": _digest("4"),
            "row_observation_sha256": _digest("5"),
            "data_through": "2026-07-16T00:00:00+00:00",
            "observed_at": "2026-07-16T01:00:00+00:00",
            "max_row_observed_at": "2026-07-16T01:00:00+00:00",
            "minimum_row_count": 1,
            "max_pages": 20,
            "max_rows": 100_000,
            "page_count": 1,
            "pagination_trace_sha256": _digest("6"),
            "pagination_semantic_sha256": _digest("7"),
            "page_request_set_sha256": _digest("9"),
            "page_response_set_sha256": _digest("a"),
            "cursor_chain_sha256": _digest("8"),
        }
    )
    ports = {stage: _Port(stage, payloads[stage]) for stage in STAGE_ORDER}

    bundle = _loop(ports).run(_context())

    assert bundle.stop_new_risk is False
    assert bundle.status == "completed"


def test_optional_only_evidence_can_never_self_declare_execution_eligible() -> None:
    payloads = _payloads()
    only = deepcopy(payloads[RunStage.EVIDENCE_READY]["datasets"][0])
    only["role"] = "optional_context"
    payloads[RunStage.EVIDENCE_READY]["datasets"] = [only]
    ports = {stage: _Port(stage, payloads[stage]) for stage in STAGE_ORDER}

    bundle = _loop(ports).run(_context())

    assert bundle.stop_new_risk is True
    assert "required_dataset_evidence_missing" in bundle.block_reasons


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("row_count", 0),
        ("source_proof_complete", False),
        ("lineage_sha256", "invalid"),
        ("source_proof_sha256", None),
        ("observation_mode", "historical_point_in_time"),
        ("historical_pit_eligible", True),
        ("identity_fields", []),
        ("identity_sha256", "invalid"),
        ("row_observation_sha256", "invalid"),
        ("data_through", "2026-07-16T01:00:01+00:00"),
        ("observed_at", "2026-07-16T00:59:59+00:00"),
        ("max_row_observed_at", "2026-07-16T01:05:01+00:00"),
        ("minimum_row_count", -1),
        ("minimum_row_count", 2),
        ("max_pages", 0),
        ("max_rows", 0),
        ("page_count", 0),
        ("pagination_trace_sha256", "invalid"),
        ("pagination_semantic_sha256", "invalid"),
        ("page_request_set_sha256", "invalid"),
        ("page_response_set_sha256", "invalid"),
        ("cursor_chain_sha256", "invalid"),
    ],
)
def test_provider_native_current_observation_contract_fails_closed(
    field: str,
    value: object,
) -> None:
    payloads = _payloads()
    payloads[RunStage.EVIDENCE_READY]["datasets"][0][field] = value
    ports = {stage: _Port(stage, payloads[stage]) for stage in STAGE_ORDER}

    bundle = _loop(ports).run(_context())

    assert bundle.stop_new_risk is True
    assert "dataset_row_observation_invalid" in bundle.block_reasons


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("profile_contract_sha256", "invalid"),
        ("historical_pit_eligible", True),
        ("decision_as_of", "not-an-instant"),
    ],
)
def test_research_snapshot_v2_top_level_contract_fails_closed(
    field: str,
    value: object,
) -> None:
    payloads = _payloads()
    payloads[RunStage.EVIDENCE_READY][field] = value
    ports = {stage: _Port(stage, payloads[stage]) for stage in STAGE_ORDER}

    bundle = _loop(ports).run(_context())

    assert bundle.stop_new_risk is True
    assert "research_snapshot_contract_invalid" in bundle.block_reasons


def test_legacy_row_pit_fields_cannot_substitute_current_observation_contract() -> None:
    payloads = _payloads()
    dataset = payloads[RunStage.EVIDENCE_READY]["datasets"][0]
    for field in (
        "source_proof_complete",
        "lineage_sha256",
        "source_proof_sha256",
        "observation_mode",
        "historical_pit_eligible",
        "identity_fields",
        "identity_sha256",
        "row_observation_sha256",
        "data_through",
        "observed_at",
        "max_row_observed_at",
        "minimum_row_count",
        "max_pages",
        "max_rows",
        "page_count",
        "pagination_trace_sha256",
        "pagination_semantic_sha256",
        "page_request_set_sha256",
        "page_response_set_sha256",
        "cursor_chain_sha256",
    ):
        dataset.pop(field)
    dataset.update(
        row_pit_sha256=_digest("9"),
        max_row_available_time="2026-07-16T01:00:00+00:00",
    )
    ports = {stage: _Port(stage, payloads[stage]) for stage in STAGE_ORDER}

    bundle = _loop(ports).run(_context())

    assert bundle.stop_new_risk is True
    assert "dataset_row_observation_invalid" in bundle.block_reasons


@pytest.mark.parametrize(
    ("stage", "field", "value", "reason"),
    [
        (
            RunStage.LEARNING_RECORDED,
            "authority_readback_verified",
            False,
            "learning_authority_readback_invalid",
        ),
        (
            RunStage.LEARNING_RECORDED,
            "journal_head_sha256",
            "forged",
            "learning_authority_head_invalid",
        ),
        (
            RunStage.REPORTED,
            "readback_sha256",
            _digest("c"),
            "report_artifact_readback_mismatch",
        ),
        (
            RunStage.REPORTED,
            "production_verified",
            True,
            "report_candidate_boundary_invalid",
        ),
    ],
)
def test_learning_and_report_cannot_pass_on_unverified_self_report(
    stage: RunStage,
    field: str,
    value: object,
    reason: str,
) -> None:
    payloads = _payloads()
    payloads[stage][field] = value
    ports = {item: _Port(item, payloads[item]) for item in STAGE_ORDER}

    bundle = _loop(ports).run(_context())

    assert bundle.stop_new_risk is True
    assert reason in bundle.block_reasons


@pytest.mark.parametrize(
    ("stage", "mutator", "reason"),
    [
        (
            RunStage.RISK_CHECKED,
            lambda payload: payload["approved_orders"][0].update(quantity=50),
            "order_board_lot_invalid",
        ),
        (
            RunStage.RISK_CHECKED,
            lambda payload: payload["approved_orders"][0].update(
                intent="exit", sellable_quantity=100, t_plus_one_eligible=False
            ),
            "sell_t_plus_one_ineligible",
        ),
        (
            RunStage.ORDERS_SIMULATED,
            lambda payload: payload["order_receipts"][0].update(
                filled_quantity=50, residual_quantity=0
            ),
            "fill_quantity_conservation_invalid",
        ),
        (
            RunStage.ORDERS_SIMULATED,
            lambda payload: payload["order_receipts"][0].update(fee_cny=None),
            "filled_receipt_economics_invalid",
        ),
        (
            RunStage.ORDERS_SIMULATED,
            lambda payload: payload["order_receipts"][0].update(
                fill_fingerprint=_digest("9")
            ),
            "fill_fingerprint_content_mismatch",
        ),
        (
            RunStage.RECONCILED,
            lambda payload: payload.update(capital_ledger_head_sha256="forged"),
            "reconcile_authority_proof_invalid",
        ),
    ],
)
def test_a_share_risk_fill_and_reconcile_contracts_fail_closed(
    stage: RunStage,
    mutator: object,
    reason: str,
) -> None:
    payloads = _payloads()
    mutator(payloads[stage])  # type: ignore[operator]
    ports = {item: _Port(item, payloads[item]) for item in STAGE_ORDER}

    bundle = _loop(ports).run(_context())

    assert bundle.stop_new_risk is True
    assert reason in bundle.block_reasons


def test_day_loop_accepts_strict_zero_fill_not_committed_market_failure() -> None:
    payloads = _payloads()
    receipt = payloads[RunStage.ORDERS_SIMULATED]["order_receipts"][0]
    receipt.update(
        status="not_filled",
        filled_quantity=0,
        residual_quantity=100,
        terminal_at="2026-07-16T01:32:00+00:00",
        capital_commit_status="not_committed",
        capital_release_receipt_id="MCAP-" + "1" * 32,
        capital_release_status="released",
        execution_reason="paper_market_snapshot_stale_before_capital_commit",
        market_session="continuous_auction_am",
        market_execution_time="2026-07-16T01:31:00+00:00",
        market_available_at="2026-07-16T01:31:00+00:00",
        market_data_through="2026-07-16T01:31:00+00:00",
        sim_submit_checked_at="2026-07-16T01:31:10+00:00",
        capital_commit_checked_at="2026-07-16T01:32:00+00:00",
    )
    for field_name in (
        "filled_price_cny",
        "fee_cny",
        "slippage_cny",
        "filled_at",
        "simulated_fill_id",
        "capital_commit_receipt_id",
        "fill_fingerprint",
    ):
        receipt.pop(field_name, None)
    ports = {item: _Port(item, payloads[item]) for item in STAGE_ORDER}

    bundle = _loop(ports).run(_context())

    assert bundle.status == "completed"
    assert "unfilled_receipt_proof_invalid" not in bundle.block_reasons


def test_day_loop_rejects_illegal_odd_lot_sell_plan_and_order() -> None:
    payloads = _payloads()
    payloads[RunStage.DECISION_READY]["small_account_plan"].update(
        starting_available_cash_cny=48_500.0,
        starting_gross_cny=1_500.0,
        target_gross_cny=1_300.0,
        cash_after_orders_cny=48_694.898,
        plan_decisions=[
            {
                "decision_id": "decision-1",
                "symbol": "000001.SZ",
                "action": "reduce",
                "current_shares": 150,
                "sellable_shares": 150,
                "target_shares": 130,
                "order_quantity": 20,
                "valuation_price_cny": 10.0,
                "reservation_price_cny": 10.0,
                "estimated_order_cost_cny": 5.102,
                "target_notional_cny": 1_300.0,
            }
        ],
        plan_sha256="__PLAN_SHA__",
    )
    payloads[RunStage.DECISION_READY]["decisions"] = [
        {
            "decision_id": "decision-1",
            "decision_cluster_id": "decision-cluster-1",
            "symbol": "000001.SZ",
            "action": "reduce",
            "target_shares": 130,
            "requested_notional_cny": 200.0,
            "score_semantics": "uncalibrated_deterministic_rank_score",
        }
    ]
    _bind_thesis_risk_fixture(payloads[RunStage.DECISION_READY])
    payloads[RunStage.RISK_CHECKED]["approved_orders"][0].update(
        intent="reduce",
        quantity=20,
        expected_fee_cny=5.102,
        available_cash_before_cny=48_500.0,
        sellable_quantity=150,
        t_plus_one_eligible=True,
    )
    payloads[RunStage.ORDERS_SIMULATED]["order_receipts"] = []
    ports = {stage: _Port(stage, payloads[stage]) for stage in STAGE_ORDER}

    bundle = _loop(ports).run(_context())

    assert bundle.stop_new_risk is True
    assert "ashare_odd_lot_sell_quantity_invalid" in bundle.block_reasons
    assert ports[RunStage.ORDERS_SIMULATED].calls[0].permitted_order_ids == ()


def test_risk_order_cannot_exceed_the_hashed_small_account_plan() -> None:
    payloads = _payloads()
    payloads[RunStage.RISK_CHECKED]["approved_orders"][0]["quantity"] = 200
    payloads[RunStage.ORDERS_SIMULATED]["order_receipts"] = []
    ports = {stage: _Port(stage, payloads[stage]) for stage in STAGE_ORDER}

    bundle = _loop(ports).run(_context())

    assert bundle.stop_new_risk is True
    assert "order_small_account_plan_mismatch" in bundle.block_reasons
    assert ports[RunStage.ORDERS_SIMULATED].calls[0].permitted_order_ids == ()


def test_small_account_plan_independently_enforces_single_name_cap() -> None:
    payloads = _payloads()
    plan = payloads[RunStage.DECISION_READY]["small_account_plan"]
    plan["plan_decisions"][0].update(
        target_shares=800,
        order_quantity=800,
        target_notional_cny=8_000.0,
    )
    plan.update(target_gross_cny=8_000.0, cash_after_orders_cny=41_994.99)
    payloads[RunStage.DECISION_READY]["decisions"][0].update(
        target_shares=800,
        requested_notional_cny=8_000.0,
    )
    _bind_thesis_risk_fixture(payloads[RunStage.DECISION_READY])
    payloads[RunStage.RISK_CHECKED]["approved_orders"] = []
    payloads[RunStage.RISK_CHECKED]["rejected_decisions"] = [
        {"decision_id": "decision-1", "reason": "single_name_cap"}
    ]
    payloads[RunStage.ORDERS_SIMULATED]["order_receipts"] = []
    ports = {stage: _Port(stage, payloads[stage]) for stage in STAGE_ORDER}

    bundle = _loop(ports).run(_context())

    assert bundle.stop_new_risk is True
    assert "small_account_single_name_cap_exceeded" in bundle.block_reasons


def test_small_account_plan_binds_group_delta_to_order_risk_notional() -> None:
    payloads = _payloads()
    decision_payload = payloads[RunStage.DECISION_READY]
    plan_row = decision_payload["small_account_plan"]["plan_decisions"][0]
    for effect in plan_row["thesis_risk_group_effects"]:
        effect.update(
            requested_delta_cny=0.0,
            requested_post_exposure_cny=0.0,
            delta_cny=0.0,
            post_exposure_cny=0.0,
        )
    decision_payload["decisions"][0]["thesis_risk_group_effects"] = deepcopy(
        plan_row["thesis_risk_group_effects"]
    )
    for exposure in decision_payload["small_account_plan"][
        "thesis_risk_final_group_exposures"
    ]:
        exposure["exposure_cny"] = 0.0
    ports = {stage: _Port(stage, payloads[stage]) for stage in STAGE_ORDER}

    bundle = _loop(ports).run(_context())

    assert bundle.stop_new_risk is True
    assert "small_account_thesis_risk_notional_binding_invalid" in bundle.block_reasons


def test_resigned_plan_cannot_reassign_authoritative_thesis_risk_group() -> None:
    payloads = _payloads()
    decision_payload = payloads[RunStage.DECISION_READY]
    plan_row = decision_payload["small_account_plan"]["plan_decisions"][0]
    plan_effect = next(
        effect
        for effect in plan_row["thesis_risk_group_effects"]
        if effect["dimension"] == "thesis"
    )
    original_group = plan_effect["group_id"]
    plan_effect["group_id"] = "forged-thesis-group"
    decision_payload["decisions"][0]["thesis_risk_group_effects"] = deepcopy(
        plan_row["thesis_risk_group_effects"]
    )
    final_row = next(
        row
        for row in decision_payload["small_account_plan"][
            "thesis_risk_final_group_exposures"
        ]
        if row["dimension"] == "thesis" and row["group_id"] == original_group
    )
    final_row["group_id"] = "forged-thesis-group"
    _resign_small_account_plan(decision_payload)
    ports = {stage: _Port(stage, payloads[stage]) for stage in STAGE_ORDER}

    bundle = _loop(ports).run(_context())

    assert bundle.stop_new_risk is True
    assert "small_account_thesis_risk_group_binding_invalid" in bundle.block_reasons


def test_resigned_plan_still_binds_proof_final_map_and_decision_mirror() -> None:
    mutations = ("proof", "final_map", "decision_mirror")
    expected_reasons = (
        "small_account_thesis_risk_proof_invalid",
        "small_account_thesis_risk_exposure_invalid",
        "small_account_thesis_risk_decision_binding_invalid",
    )
    for mutation, expected_reason in zip(mutations, expected_reasons):
        payloads = _payloads()
        decision_payload = payloads[RunStage.DECISION_READY]
        plan = decision_payload["small_account_plan"]
        if mutation == "proof":
            plan["thesis_risk_policy_proof_sha256"] = "f" * 64
        elif mutation == "final_map":
            plan["thesis_risk_final_group_exposures"][0]["exposure_cny"] += 1.0
        else:
            plan["plan_decisions"][0]["reason_codes"] = ["forged-reason"]
        _resign_small_account_plan(decision_payload)
        ports = {stage: _Port(stage, payloads[stage]) for stage in STAGE_ORDER}

        bundle = _loop(ports).run(_context())

        assert bundle.stop_new_risk is True
        assert expected_reason in bundle.block_reasons


def test_small_account_plan_recomputes_cost_after_caller_resigns_zero_fee() -> None:
    payloads = _payloads()
    plan = payloads[RunStage.DECISION_READY]["small_account_plan"]
    plan["plan_decisions"][0]["estimated_order_cost_cny"] = 0.0
    plan["cash_after_orders_cny"] = 49_000.0
    payloads[RunStage.RISK_CHECKED]["approved_orders"] = []
    payloads[RunStage.RISK_CHECKED]["rejected_decisions"] = [
        {"decision_id": "decision-1", "reason": "cost_verification_failed"}
    ]
    payloads[RunStage.ORDERS_SIMULATED]["order_receipts"] = []
    ports = {stage: _Port(stage, payloads[stage]) for stage in STAGE_ORDER}

    bundle = _loop(ports).run(_context())

    assert bundle.stop_new_risk is True
    assert "small_account_plan_cost_policy_invalid" in bundle.block_reasons


def test_small_account_plan_independently_enforces_position_count_cap() -> None:
    payloads = _payloads()
    plan = payloads[RunStage.DECISION_READY]["small_account_plan"]
    plan.update(
        max_positions=1,
        starting_available_cash_cny=49_000.0,
        starting_gross_cny=1_000.0,
        target_gross_cny=2_000.0,
        cash_after_orders_cny=47_994.99,
    )
    plan["plan_decisions"].append(
        {
            "decision_id": "decision-existing-hold",
            "symbol": "600000.SH",
            "action": "hold",
            "current_shares": 100,
            "sellable_shares": 100,
            "target_shares": 100,
            "order_quantity": 0,
            "valuation_price_cny": 10.0,
            "reservation_price_cny": 10.0,
            "estimated_order_cost_cny": 0.0,
            "target_notional_cny": 1_000.0,
        }
    )
    payloads[RunStage.DECISION_READY]["decisions"].append(
        {
            "decision_id": "decision-existing-hold",
            "decision_cluster_id": "decision-cluster-existing-hold",
            "symbol": "600000.SH",
            "action": "hold",
            "target_shares": 100,
            "requested_notional_cny": 0.0,
            "score_semantics": "uncalibrated_deterministic_rank_score",
        }
    )
    _bind_thesis_risk_fixture(payloads[RunStage.DECISION_READY])
    ports = {stage: _Port(stage, payloads[stage]) for stage in STAGE_ORDER}

    bundle = _loop(ports).run(_context())

    assert bundle.stop_new_risk is True
    assert "small_account_max_positions_exceeded" in bundle.block_reasons


@pytest.mark.parametrize(
    ("stage", "field", "value", "reason"),
    [
        (
            RunStage.ORDERS_SIMULATED,
            "filled_at",
            "2026-07-16T01:04:59+00:00",
            "execution_time_precedes_decision",
        ),
        (
            RunStage.ORDERS_SIMULATED,
            "terminal_at",
            "2026-07-16T01:30:59+00:00",
            "execution_time_order_invalid",
        ),
        (
            RunStage.RECONCILED,
            "reconciled_at",
            "2026-07-16T01:30:59+00:00",
            "reconcile_precedes_execution_terminal",
        ),
    ],
)
def test_execution_and_reconciliation_times_are_monotonic(
    stage: RunStage,
    field: str,
    value: str,
    reason: str,
) -> None:
    payloads = _payloads()
    if stage is RunStage.ORDERS_SIMULATED:
        payloads[stage]["order_receipts"][0][field] = value
    else:
        payloads[stage][field] = value
    ports = {item: _Port(item, payloads[item]) for item in STAGE_ORDER}

    bundle = _loop(ports).run(_context())

    assert bundle.stop_new_risk is True
    assert reason in bundle.block_reasons


def test_required_dataset_deweight_cannot_be_laundered_as_execution_eligible() -> None:
    payloads = _payloads()
    payloads[RunStage.EVIDENCE_READY]["datasets"][0].update(
        state="degraded",
        evidence_action="deweight",
        effective_weight=0.25,
    )
    ports = {stage: _Port(stage, payloads[stage]) for stage in STAGE_ORDER}

    bundle = _loop(ports).run(_context())

    assert bundle.stop_new_risk is True
    assert "required_dataset_not_accepted" in bundle.block_reasons
    assert "research_snapshot_eligibility_mismatch" in bundle.block_reasons


def test_non_mainboard_symbol_leak_is_blocked_at_universe_and_order_boundary() -> None:
    payloads = _payloads()
    payloads[RunStage.UNIVERSE_READY]["tradable_symbols"].append("300001.SZ")
    payloads[RunStage.UNIVERSE_READY]["feasible_symbols"].append("300001.SZ")
    payloads[RunStage.DECISION_READY]["decisions"] = [
        {
            "decision_id": "leak-1",
            "symbol": "300001.SZ",
            "action": "open",
            "target_shares": 100,
            "score_semantics": "uncalibrated_deterministic_rank_score",
        }
    ]
    payloads[RunStage.RISK_CHECKED]["approved_orders"] = [
        {
            "decision_id": "leak-1",
            "order_id": "leak-order-1",
            "symbol": "300001.SZ",
            "intent": "open",
            "quantity": 100,
        }
    ]
    payloads[RunStage.ORDERS_SIMULATED]["order_receipts"] = []
    ports = {stage: _Port(stage, payloads[stage]) for stage in STAGE_ORDER}

    bundle = _loop(ports).run(_context())

    assert bundle.stop_new_risk is True
    assert "non_mainboard_universe_leak" in bundle.block_reasons
    assert "non_mainboard_decision_leak" in bundle.block_reasons
    assert "non_mainboard_order_leak" in bundle.block_reasons
    assert ports[RunStage.ORDERS_SIMULATED].calls[0].permitted_order_ids == ()


def test_live_environment_is_rejected_before_any_port_runs() -> None:
    ports = _ports()
    loop = ASharePaperDayLoop(
        ports=ports,
        scope_policy=_ScopePolicy(),
        store=MemoryRunBundleStore(),
        thesis_risk_authority=(ports[RunStage.DECISION_READY].thesis_risk_authority),
        environ={"REAL_TRADING_ENABLED": "true"},
    )

    with pytest.raises(RuntimeError, match="REAL_TRADING_ENABLED=false"):
        loop.run(_context())

    assert Counter(len(port.calls) for port in ports.values()) == Counter({0: 9})


def test_context_only_chinext_and_star_indices_never_become_order_identities() -> None:
    ports = _ports()

    bundle = _loop(ports).run(_context())

    universe = bundle.receipt_for(RunStage.UNIVERSE_READY).payload
    assert [row["entity_id"] for row in universe["context_entities"]] == [
        "399006.SZ",
        "000688.SH",
    ]
    assert all(row["context_only"] is True for row in universe["context_entities"])
    assert all(
        row["order_identity_allowed"] is False for row in universe["context_entities"]
    )
