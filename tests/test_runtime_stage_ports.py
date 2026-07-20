from __future__ import annotations

import hashlib
import importlib
import json
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

import pytest

from shared.data.evidence_gate import (
    DataEvidenceGate,
    DatasetEvidencePolicy,
    EvidenceAction,
)
from shared.data.research_snapshot import (
    DatasetRequirement,
    ResearchDataProfile,
)
from shared.data.research_snapshot_store import FileResearchSnapshotStore
from shared.data.sharedsignals_v1 import (
    HTTPResponse,
    QueryRequest,
    SharedSignalsV1Client,
    SharedSignalsV1Config,
)
from shared.review.sample_journal import SampleJournal
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
from shared.runtime.stage_ports import (
    SampleJournalLearningPort,
    SharedSignalsResearchEvidencePort,
    StagePortContractError,
)
from tests._thesis_risk_fixture import build_thesis_risk_fixture


CATALOG = "fixture-catalog-2026-07-16"
PRICE_DATASET = "fixture.cn.equity.daily.mainboard.v1"
CONTEXT_DATASET = "fixture.cn.equity.sector.full-market-context.v1"
DECISION_AS_OF = "2026-07-16T01:05:00+00:00"
LINEAGE = "ashare-sim-fresh-20260712-v1"


def _digest(character: str) -> str:
    return character * 64


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
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


@dataclass
class _Transport:
    offline_fixture: bool = field(default=True, init=False)
    responses: list[HTTPResponse]
    calls: list[dict[str, Any]] = field(default_factory=list)

    def __call__(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        json_body: Mapping[str, Any] | None,
        timeout_seconds: float,
    ) -> HTTPResponse:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "json_body": deepcopy(dict(json_body))
                if json_body is not None
                else None,
                "timeout_seconds": timeout_seconds,
            }
        )
        if not self.responses:
            raise AssertionError("unexpected transport call")
        return self.responses.pop(0)


def _catalog_response() -> HTTPResponse:
    return HTTPResponse(
        status_code=200,
        json_body={
            "api_version": "v1",
            "catalog_version": CATALOG,
            "request_id": "catalog-request-1",
            "data": [
                {"dataset_id": PRICE_DATASET, "fields": ["ts_code", "close"]},
                {"dataset_id": CONTEXT_DATASET, "fields": ["sector_id", "breadth"]},
            ],
        },
    )


def _query_response(
    dataset_id: str,
    *,
    receipt_id: str,
    rows: list[dict[str, Any]],
    state: str = "ready",
    degraded: bool = False,
    next_cursor: str | None = None,
) -> HTTPResponse:
    freshness_state = "degraded" if degraded else "fresh"
    quality_state = "degraded" if degraded else "valid"
    pit_rows = [
        {
            **row,
            "event_time": row.get("event_time", "2026-07-15T07:00:00+00:00"),
            "available_time": row.get("available_time", "2026-07-16T00:59:00+00:00"),
            "revision_id": row.get("revision_id", "r1"),
            "receipt_id": row.get("receipt_id", f"row-{receipt_id}"),
        }
        for row in rows
    ]
    return HTTPResponse(
        status_code=200,
        json_body={
            "api_version": "v1",
            "catalog_version": CATALOG,
            "request_id": f"query-{dataset_id}",
            "dataset_id": dataset_id,
            "data": pit_rows,
            "next_cursor": next_cursor,
            "metadata": {
                "state": state,
                "degraded": degraded,
                "freshness": {"state": freshness_state, "stale": False},
                "quality": {"state": quality_state, "valid": True},
                "lineage": {"complete": True, "provider_neutral": True},
                "receipt_id": receipt_id,
                "data_through": "2026-07-15T07:00:00+00:00",
                "observed_at": "2026-07-16T01:00:00+00:00",
                "reasons": ["context_partial"] if degraded else [],
            },
        },
    )


def _client(transport: _Transport) -> SharedSignalsV1Client:
    return SharedSignalsV1Client(
        SharedSignalsV1Config(
            base_url="http://tradingdatas.fixture.invalid:8082",
            expected_catalog_version=CATALOG,
            dataset_ids=frozenset({PRICE_DATASET, CONTEXT_DATASET}),
            access_policy_id="ta-paper-read-v1",
            cache_ttl_seconds=0,
        ),
        transport=transport,
    )


def _profile() -> ResearchDataProfile:
    return ResearchDataProfile(
        profile_id="mainboard-paper-mvp-input-v1",
        catalog_version=CATALOG,
        requirements=(
            DatasetRequirement(PRICE_DATASET, role="required_execution"),
            DatasetRequirement(CONTEXT_DATASET, role="optional_context"),
        ),
    )


def _evidence_port(
    transport: _Transport,
    *,
    snapshot_store: FileResearchSnapshotStore,
) -> SharedSignalsResearchEvidencePort:
    return SharedSignalsResearchEvidencePort(
        identity=ComponentIdentity(
            stage=RunStage.EVIDENCE_READY,
            component_id="tradingdatas-research-evidence-port",
            version="1",
            artifact_sha256=_digest("2"),
        ),
        client=_client(transport),
        profile=_profile(),
        requests={
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
        evidence_gate=DataEvidenceGate(
            {
                PRICE_DATASET: DatasetEvidencePolicy(PRICE_DATASET),
                CONTEXT_DATASET: DatasetEvidencePolicy(
                    CONTEXT_DATASET,
                    degraded_action=EvidenceAction.DEWEIGHT,
                    degraded_weight=0.25,
                ),
            }
        ),
        decision_as_of=datetime.fromisoformat(DECISION_AS_OF),
        snapshot_store=snapshot_store,
    )


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


class _StaticPort:
    def __init__(self, stage: RunStage, payload: Mapping[str, Any]) -> None:
        self.identity = ComponentIdentity(
            stage=stage,
            component_id=f"fixture-{stage.value}",
            version="1",
            artifact_sha256=_digest(str(STAGE_ORDER.index(stage) + 1)),
        )
        self.payload = deepcopy(dict(payload))
        self.calls: list[StageRequest] = []

    @staticmethod
    def _bind_placeholders(
        value: Any,
        *,
        replacements: Mapping[str, Any],
    ) -> Any:
        if isinstance(value, Mapping):
            return {
                key: _StaticPort._bind_placeholders(
                    item,
                    replacements=replacements,
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [
                _StaticPort._bind_placeholders(
                    item,
                    replacements=replacements,
                )
                for item in value
            ]
        if isinstance(value, str) and value in replacements:
            return deepcopy(replacements[value])
        return deepcopy(value)

    def execute(self, request: StageRequest) -> StageResult:
        self.calls.append(request)
        replacements: dict[str, Any] = {
            "__RUN_ID__": request.run_id,
            "__INPUT_BUNDLE_SHA__": request.input_bundle_sha256,
            "__AUTHORITY_ID__": request.bundle.context.authority_id,
            "__AUTHORITY_GENERATION__": request.bundle.context.authority_generation,
            "__EXECUTION_LINEAGE__": request.bundle.context.execution_lineage,
        }
        if RunStage.EVIDENCE_READY in {
            receipt.stage for receipt in request.bundle.stage_receipts
        }:
            evidence = request.bundle.receipt_for(RunStage.EVIDENCE_READY).payload
            source_payload = {
                "profile_id": evidence["profile_id"],
                "catalog_version": evidence["catalog_version"],
                "decision_as_of": evidence["decision_as_of"],
                "research_snapshot_sha256": evidence["snapshot_sha256"],
                "dataset_receipt_ids": {
                    item["dataset_id"]: item["receipt_id"]
                    for item in evidence["datasets"]
                },
            }
            replacements["__RESEARCH_SOURCE_PAYLOAD__"] = source_payload
            replacements["__RESEARCH_SOURCE_SHA256__"] = _canonical_sha256(
                source_payload
            )
        if request.stage is RunStage.RECONCILED:
            order_receipts = request.bundle.receipt_for(
                RunStage.ORDERS_SIMULATED
            ).payload["order_receipts"]
            replacements["__ORDER_RECEIPTS_SHA256__"] = _canonical_sha256(
                order_receipts
            )
        payload = self._bind_placeholders(
            self.payload,
            replacements=replacements,
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
        if request.stage is RunStage.ORDERS_SIMULATED:
            for receipt in payload.get("order_receipts", []):
                if receipt.get("fill_fingerprint") == (
                    "__CANONICAL_FILL_FINGERPRINT__"
                ):
                    fingerprint_payload = dict(receipt)
                    fingerprint_payload.pop("fill_fingerprint", None)
                    receipt["fill_fingerprint"] = _canonical_sha256(fingerprint_payload)
        return StageResult(payload=payload)


def _prediction() -> dict[str, Any]:
    return {
        "snapshot_id": "phase1-decision-1",
        "decision_id": "decision-1",
        "decision_cluster_id": "decision-cluster-1",
        "market": "ashare",
        "symbol": "000001.SZ",
        "style": "phase1_frozen_champion",
        "strategy_version": "phase1-frozen-champion-v1",
        "prediction_at": DECISION_AS_OF,
        "data_as_of": DECISION_AS_OF,
        "event_time": "2026-07-15T07:00:00+00:00",
        "available_at": "2026-07-16T01:00:00+00:00",
        "ingested_at": "2026-07-16T01:00:00+00:00",
        "retrieved_as_of": "2026-07-16T01:00:00+00:00",
        "reference_price": 10.0,
        "direction": "long",
        "raw_style_score": 0.42,
        "score_semantics": "uncalibrated_deterministic_rank_score",
        "calibrated_probability": None,
        "probability_model_state": "not_calibrated",
        "mature_threshold_passed": False,
        "execution_gate_passed": True,
        "execution_reject_reason": None,
        "costs": {
            "round_trip_fee_bps": 105.0,
            "round_trip_slippage_bps": 10.0,
            "cost_model_version": "ashare-small-account-cost-v1",
            "cost_basis_notional_cny": 1000.0,
        },
        "data_quality": {
            "reliable": True,
            "source": "sharedsignals.query_result.v1",
            "price_timestamp": "2026-07-15T07:00:00+00:00",
        },
        "point_in_time_lineage": {
            "timestamps": {
                "event_time": "2026-07-15T07:00:00+00:00",
                "available_at": "2026-07-16T01:00:00+00:00",
                "ingested_at": "2026-07-16T01:00:00+00:00",
                "retrieved_as_of": "2026-07-16T01:00:00+00:00",
            }
        },
        "source_snapshot_payload": "__RESEARCH_SOURCE_PAYLOAD__",
        "source_snapshot_sha256": "__RESEARCH_SOURCE_SHA256__",
        "capital_authority_id": "ashare-capital-v1",
        "authority_generation": 1,
        "execution_lineage_id": LINEAGE,
        "real_trading_enabled": False,
        "live_execution_enabled": False,
    }


def _payloads() -> dict[RunStage, dict[str, Any]]:
    return {
        RunStage.PREOPEN: {
            "market": "ashare",
            "account_type": "simulated",
            "real_trading_enabled": False,
            "account_authority_valid": True,
            "position_authority_valid": True,
        },
        RunStage.UNIVERSE_READY: {
            "context_receipt_id": "context-universe-1",
            "tradable_receipt_id": "tradable-universe-1",
            "feasible_receipt_id": "feasible-universe-1",
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
                "account_as_of": DECISION_AS_OF,
                "position_snapshot_receipt_id": "position-authority-receipt-1",
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
            "candidate_set_receipt": {
                "schema_version": 1,
                "candidate_set_id": "candidate-set-20260716-v1",
                "generated_at": DECISION_AS_OF,
                "selection_policy_version": "phase1-decision-policy-v1",
                "exploration_seed": "phase1-fixed-seed-1",
                "candidates": [
                    {
                        "candidate_id": "candidate-1",
                        "decision_id": "decision-1",
                        "prediction_snapshot_id": "phase1-decision-1",
                        "symbol": "000001.SZ",
                        "selected": True,
                        "selection_reason": "ranked_within_budget",
                        "selection_propensity": 1.0,
                        "strategy_version": "phase1-frozen-champion-v1",
                    }
                ],
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
            "journal_predictions": [_prediction()],
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
                    "side": "buy",
                    "quantity": 100,
                    "sample_intent": "exploitation",
                    "prediction_snapshot_id": "phase1-decision-1",
                    "prediction_source_snapshot_sha256": ("__RESEARCH_SOURCE_SHA256__"),
                    "primary_style": "phase1_frozen_champion",
                    "supporting_styles": [],
                    "style_scores": {"phase1_frozen_champion": 0.42},
                    "style_versions": {
                        "phase1_frozen_champion": "phase1-frozen-champion-v1"
                    },
                    "decision_policy_version": "phase1-decision-policy-v1",
                    "capital_authority_id": "__AUTHORITY_ID__",
                    "authority_generation": "__AUTHORITY_GENERATION__",
                    "execution_lineage": "__EXECUTION_LINEAGE__",
                    "risk_receipt_id": "risk-receipt-1",
                    "position_authority_receipt_id": "position-authority-receipt-1",
                    "cash_authority_receipt_id": "cash-authority-receipt-1",
                    "small_account_plan_sha256": "__PLAN_SHA__",
                    "reservation_price_cny": 10.0,
                    "expected_fee_cny": 5.01,
                    "available_cash_before_cny": 50_000.0,
                    "session_policy_verified": True,
                    "not_suspended": True,
                    "limit_fillable": True,
                }
            ],
            "rejected_decisions": [],
        },
        RunStage.ORDERS_SIMULATED: {
            "execution_lineage": LINEAGE,
            "account_type": "simulated",
            "real_trading_enabled": False,
            "order_receipts": [
                {
                    "order_id": "order-1",
                    "symbol": "000001.SZ",
                    "intent": "open",
                    "status": "not_filled",
                    "capital_authority_id": "__AUTHORITY_ID__",
                    "authority_generation": "__AUTHORITY_GENERATION__",
                    "execution_lineage": "__EXECUTION_LINEAGE__",
                    "requested_quantity": 100,
                    "filled_quantity": 0,
                    "residual_quantity": 100,
                    "terminal_at": "2026-07-16T01:06:00+00:00",
                    "nonfill_reason": "order_not_filled_by_simulator",
                    "execution_receipt_id": "execution-receipt-1",
                    "capital_commit_status": "not_applicable",
                }
            ],
            "unknown_order_ids": [],
        },
        RunStage.RECONCILED: {
            "status": "reconciled",
            "account_authority_valid": True,
            "position_authority_valid": True,
            "execution_lineage": LINEAGE,
            "capital_authority_id": "__AUTHORITY_ID__",
            "authority_generation": "__AUTHORITY_GENERATION__",
            "source_run_id": "__RUN_ID__",
            "source_input_bundle_sha256": "__INPUT_BUNDLE_SHA__",
            "reconciled_at": "2026-07-16T01:06:30+00:00",
            "reconciliation_receipt_id": "reconciliation-receipt-1",
            "capital_ledger_head_sha256": _digest("d"),
            "position_fingerprint": _digest("e"),
            "order_receipts_sha256": "__ORDER_RECEIPTS_SHA256__",
            "account_equity_cny": 50_000.0,
            "cash_cny": 50_000.0,
            "unknown_order_ids": [],
            "unreconciled_order_ids": [],
        },
        RunStage.REPORTED: {
            "reported": True,
            "report_id": "today-report-1",
            "source_run_id": "__RUN_ID__",
            "source_input_bundle_sha256": "__INPUT_BUNDLE_SHA__",
            "projection_type": "today_run_local_candidate",
            "local_candidate": True,
            "production_verified": False,
            "artifact_sha256": _digest("a"),
            "readback_sha256": _digest("a"),
        },
    }


def _context() -> RunContext:
    return RunContext(
        trade_date="2026-07-16",
        decision_as_of=DECISION_AS_OF,
        market="ashare",
        authority_id="ashare-capital-v1",
        authority_generation=1,
        execution_lineage=LINEAGE,
        account_type="simulated",
        real_trading_enabled=False,
        champion_manifest_sha256=_digest("c"),
    )


def _ports(
    *,
    transport: _Transport,
    journal_path: Path,
    snapshot_store: FileResearchSnapshotStore,
) -> dict[RunStage, Any]:
    payloads = _payloads()
    ports: dict[RunStage, Any] = {
        stage: _StaticPort(stage, payload) for stage, payload in payloads.items()
    }
    ports[RunStage.EVIDENCE_READY] = _evidence_port(
        transport,
        snapshot_store=snapshot_store,
    )
    ports[RunStage.LEARNING_RECORDED] = SampleJournalLearningPort(
        identity=ComponentIdentity(
            stage=RunStage.LEARNING_RECORDED,
            component_id="sample-journal-learning-port",
            version="1",
            artifact_sha256=_digest("8"),
        ),
        journal=SampleJournal(journal_path),
    )
    return ports


def _bind_thesis_risk_authority(
    ports: Mapping[RunStage, Any],
    *,
    decision_time: datetime,
) -> ThesisRiskRuntimeAuthority:
    """Bind one explicit non-promotable authority to the port and its plan."""

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
                fixture_id=f"runtime-stage-port-{symbol}",
                source_fixture_sha256=_canonical_sha256(
                    {"contract": "runtime-stage-port", "symbol": symbol}
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


def _paper_loop(
    *,
    ports: Mapping[RunStage, Any],
    store: Any,
    decision_time: datetime | None = None,
    fault_hook: Any = None,
) -> ASharePaperDayLoop:
    resolved_time = decision_time or datetime.fromisoformat(DECISION_AS_OF)
    authority = _bind_thesis_risk_authority(
        ports,
        decision_time=resolved_time,
    )
    return ASharePaperDayLoop(
        ports=ports,
        scope_policy=_ScopePolicy(),
        store=store,
        thesis_risk_authority=authority,
        environ={"REAL_TRADING_ENABLED": "false"},
        fault_hook=fault_hook,
    )


def _transport_responses(*, next_cursor: str | None = None) -> list[HTTPResponse]:
    return [
        _catalog_response(),
        _query_response(
            PRICE_DATASET,
            receipt_id="price-receipt-1",
            rows=[{"ts_code": "000001.SZ", "close": 10.0}],
            next_cursor=next_cursor,
        ),
        _query_response(
            CONTEXT_DATASET,
            receipt_id="context-receipt-1",
            rows=[{"sector_id": "full-market", "breadth": 0.55}],
            state="degraded",
            degraded=True,
        ),
    ]


def _null_proof_transport_responses() -> list[HTTPResponse]:
    return [
        _catalog_response(),
        HTTPResponse(
            status_code=200,
            json_body={
                "api_version": "v1",
                "catalog_version": CATALOG,
                "request_id": f"query-{PRICE_DATASET}-unobserved",
                "dataset_id": PRICE_DATASET,
                "data": [{"ts_code": "000001.SZ", "close": 10.0}],
                "next_cursor": None,
                "metadata": {
                    "state": "unobserved",
                    "degraded": True,
                    "freshness": {"state": "unobserved", "stale": False},
                    "quality": {"state": "unobserved", "valid": False},
                    "lineage": None,
                    "receipt_id": None,
                    "data_through": None,
                    "observed_at": None,
                    "reasons": ["provider_not_observed"],
                },
            },
        ),
        _query_response(
            CONTEXT_DATASET,
            receipt_id="context-receipt-1",
            rows=[{"sector_id": "full-market", "breadth": 0.55}],
            state="degraded",
            degraded=True,
        ),
    ]


def test_null_proof_required_dataset_is_persisted_as_blocked_evidence_stage(
    tmp_path: Path,
) -> None:
    transport = _Transport(_null_proof_transport_responses())
    snapshot_store = FileResearchSnapshotStore(tmp_path / "research-snapshots")
    loop = _paper_loop(
        ports=_ports(
            transport=transport,
            journal_path=tmp_path / "review" / "sample_journal.jsonl",
            snapshot_store=snapshot_store,
        ),
        store=FileRunBundleStore(tmp_path / "run-bundles"),
    )

    bundle = loop.run_until(_context(), through_stage=RunStage.EVIDENCE_READY)

    receipt = bundle.receipt_for(RunStage.EVIDENCE_READY)
    assert receipt.status == "completed_with_blocks"
    assert bundle.stop_new_risk is True
    assert "research_snapshot_ineligible" in receipt.reason_codes
    assert f"required_dataset_rejected:{PRICE_DATASET}" in receipt.reason_codes
    assert f"dataset_source_proof_incomplete:{PRICE_DATASET}" in receipt.reason_codes
    evidence = receipt.payload
    assert evidence["execution_eligible"] is False
    assert evidence["blocking_reasons"] == [
        f"required_dataset_rejected:{PRICE_DATASET}",
        f"dataset_source_proof_incomplete:{PRICE_DATASET}",
    ]
    price = evidence["datasets"][0]
    assert price["evidence_action"] == "reject"
    assert price["source_proof_complete"] is False
    assert price["receipt_id"] is None
    assert price["row_count"] == 0
    assert price["max_row_available_time"] is None

    recovered = FileResearchSnapshotStore(
        tmp_path / "research-snapshots"
    ).load_bound_decision(
        profile_id="mainboard-paper-mvp-input-v1",
        decision_as_of=DECISION_AS_OF,
        catalog_version=CATALOG,
    )
    assert recovered is not None
    assert recovered.execution_eligible is False
    assert recovered.datasets[0].decoded_rows() == []


def test_v1_snapshot_to_durable_day_loop_and_sample_journal_closed_loop(
    tmp_path: Path,
) -> None:
    transport = _Transport(_transport_responses())
    journal_path = tmp_path / "review" / "sample_journal.jsonl"
    loop = _paper_loop(
        ports=_ports(
            transport=transport,
            journal_path=journal_path,
            snapshot_store=FileResearchSnapshotStore(tmp_path / "research-snapshots"),
        ),
        store=FileRunBundleStore(tmp_path / "run-bundles"),
    )

    bundle = loop.run(_context())

    assert bundle.status == "completed", bundle.block_reasons
    evidence = bundle.receipt_for(RunStage.EVIDENCE_READY).payload
    assert evidence["execution_eligible"] is True
    assert evidence["datasets"] == [
        {
            "dataset_id": PRICE_DATASET,
            "role": "required_execution",
            "state": "ready",
            "evidence_action": "accept",
            "effective_weight": 1.0,
            "reasons": [],
            "source_proof_complete": True,
            "receipt_id": "price-receipt-1",
            "row_count": 1,
            "row_pit_sha256": evidence["datasets"][0]["row_pit_sha256"],
            "max_row_available_time": "2026-07-16T00:59:00+00:00",
        },
        {
            "dataset_id": CONTEXT_DATASET,
            "role": "optional_context",
            "state": "degraded",
            "evidence_action": "deweight",
            "effective_weight": 0.25,
            "reasons": ["context_partial", "dataset_degraded"],
            "source_proof_complete": True,
            "receipt_id": "context-receipt-1",
            "row_count": 1,
            "row_pit_sha256": evidence["datasets"][1]["row_pit_sha256"],
            "max_row_available_time": "2026-07-16T00:59:00+00:00",
        },
    ]
    assert [
        (call["method"], call["url"].rsplit("/", 2)[-2:]) for call in transport.calls
    ] == [
        ("GET", ["v1", "catalog"]),
        ("POST", ["v1", "query"]),
        ("POST", ["v1", "query"]),
    ]
    assert all(
        forbidden not in str(transport.calls).lower()
        for forbidden in ("sqlite", "/tushare", "shared_signals_api")
    )

    events = SampleJournal(journal_path).read_events()
    assert [event["journal_event_type"] for event in events] == [
        "prediction_snapshot",
        "sample_event",
        "sample_event",
        "sample_event",
    ]
    assert events[0]["sample_layer"] == "observation_counterfactual"
    candidate_event = next(
        event for event in events if event.get("record_type") == "candidate_set_receipt"
    )
    assert candidate_event["promotion_eligible"] is False
    decision_event = next(
        event
        for event in events
        if event.get("audit_event_type") == "decision_exposure_disposition"
    )
    assert decision_event["disposition"] == "paper_not_filled"
    assert decision_event["eligible_for_statistical_learning"] is False
    chain_event = next(
        event
        for event in events
        if event.get("record_type") == "chain_validation"
        and event.get("audit_event_type") is None
    )
    assert chain_event["sample_layer"] == "chain_validation"
    assert chain_event["source_run_id"] == bundle.run_id
    learning = bundle.receipt_for(RunStage.LEARNING_RECORDED).payload
    assert learning["journal_authority"] == "SampleJournal"
    assert learning["recorded"] is True
    assert learning["prediction_count"] == 1
    assert learning["sample_count"] == 3
    assert learning["candidate_count"] == 1
    assert learning["decision_exposure_count"] == 1
    assert learning["authority_readback_verified"] is True
    assert learning["journal_head_event_count"] == 4
    assert len(learning["journal_head_sha256"]) == 64
    assert len(learning["journal_source_sha256"]) == 64


def test_learning_port_is_idempotent_after_crash_before_bundle_persist(
    tmp_path: Path,
) -> None:
    transport = _Transport(_transport_responses())
    journal_path = tmp_path / "review" / "sample_journal.jsonl"
    store = FileRunBundleStore(tmp_path / "run-bundles")
    ports = _ports(
        transport=transport,
        journal_path=journal_path,
        snapshot_store=FileResearchSnapshotStore(tmp_path / "research-snapshots"),
    )
    crashed = False

    def fault(stage: RunStage, point: FaultPoint) -> None:
        nonlocal crashed
        if (
            not crashed
            and stage is RunStage.LEARNING_RECORDED
            and point is FaultPoint.AFTER_PORT_BEFORE_PERSIST
        ):
            crashed = True
            raise RuntimeError("injected crash")

    with pytest.raises(RuntimeError, match="injected crash"):
        _paper_loop(
            ports=ports,
            store=store,
            fault_hook=fault,
        ).run(_context())

    recovered = _paper_loop(
        ports=ports,
        store=store,
    ).run(_context())

    assert recovered.status == "completed"
    assert len(SampleJournal(journal_path).read_events()) == 4


def test_learning_port_records_rejected_and_observation_only_decisions(
    tmp_path: Path,
) -> None:
    transport = _Transport(_transport_responses())
    journal_path = tmp_path / "review" / "sample_journal.jsonl"
    ports = _ports(
        transport=transport,
        journal_path=journal_path,
        snapshot_store=FileResearchSnapshotStore(tmp_path / "research-snapshots"),
    )
    universe = ports[RunStage.UNIVERSE_READY].payload
    universe["tradable_symbols"].append("600000.SH")
    universe["feasible_symbols"].append("600000.SH")

    decision_payload = ports[RunStage.DECISION_READY].payload
    decision_payload["decisions"].extend(
        [
            {
                "decision_id": "decision-hold",
                "decision_cluster_id": "decision-cluster-hold",
                "symbol": "600519.SH",
                "action": "hold",
                "target_shares": 0,
                "requested_notional_cny": 0.0,
                "score_semantics": "uncalibrated_deterministic_rank_score",
            },
            {
                "decision_id": "decision-rejected",
                "decision_cluster_id": "decision-cluster-rejected",
                "symbol": "600000.SH",
                "action": "open",
                "target_shares": 100,
                "requested_notional_cny": 1000.0,
                "score_semantics": "uncalibrated_deterministic_rank_score",
            },
        ]
    )
    for decision_id, cluster_id, symbol, reason in (
        ("decision-hold", "decision-cluster-hold", "600519.SH", "hold_action"),
        (
            "decision-rejected",
            "decision-cluster-rejected",
            "600000.SH",
            "insufficient_net_edge_after_cost",
        ),
    ):
        prediction = deepcopy(_prediction())
        prediction.update(
            {
                "snapshot_id": f"snapshot-{decision_id}",
                "decision_id": decision_id,
                "decision_cluster_id": cluster_id,
                "symbol": symbol,
                "execution_gate_passed": False,
                "execution_reject_reason": reason,
            }
        )
        decision_payload["journal_predictions"].append(prediction)
        decision_payload["candidate_set_receipt"]["candidates"].append(
            {
                "candidate_id": f"candidate-{decision_id}",
                "decision_id": decision_id,
                "prediction_snapshot_id": f"snapshot-{decision_id}",
                "symbol": symbol,
                "selected": decision_id == "decision-rejected",
                "selection_reason": reason,
                "selection_propensity": 0.25,
                "strategy_version": "phase1-frozen-champion-v1",
            }
        )
    decision_payload["small_account_plan"]["plan_decisions"].extend(
        [
            {
                "decision_id": "decision-hold",
                "symbol": "600519.SH",
                "action": "hold",
                "current_shares": 0,
                "sellable_shares": 0,
                "target_shares": 0,
                "order_quantity": 0,
                "valuation_price_cny": 10.0,
                "reservation_price_cny": 10.0,
                "estimated_order_cost_cny": 0.0,
                "target_notional_cny": 0.0,
            },
            {
                "decision_id": "decision-rejected",
                "symbol": "600000.SH",
                "action": "open",
                "current_shares": 0,
                "sellable_shares": 0,
                "target_shares": 100,
                "order_quantity": 100,
                "valuation_price_cny": 10.0,
                "reservation_price_cny": 10.0,
                "estimated_order_cost_cny": 5.01,
                "target_notional_cny": 1_000.0,
            },
        ]
    )
    decision_payload["small_account_plan"].update(
        target_gross_cny=2_000.0,
        cash_after_orders_cny=47_989.98,
        plan_sha256="__PLAN_SHA__",
    )
    ports[RunStage.RISK_CHECKED].payload["rejected_decisions"] = [
        {
            "decision_id": "decision-rejected",
            "reason": "insufficient_net_edge_after_cost",
        }
    ]

    bundle = _paper_loop(
        ports=ports,
        store=FileRunBundleStore(tmp_path / "run-bundles"),
    ).run(_context())

    assert bundle.status == "completed", bundle.block_reasons
    dispositions = {
        event["decision_id"]: event["disposition"]
        for event in SampleJournal(journal_path).read_events()
        if event.get("audit_event_type") == "decision_exposure_disposition"
    }
    assert dispositions == {
        "decision-1": "paper_not_filled",
        "decision-hold": "observation_only",
        "decision-rejected": "rejected",
    }
    learning = bundle.receipt_for(RunStage.LEARNING_RECORDED).payload
    assert learning["decision_exposure_count"] == 3
    assert learning["candidate_count"] == 3
    candidate_receipt = next(
        event
        for event in SampleJournal(journal_path).read_events()
        if event.get("record_type") == "candidate_set_receipt"
    )
    assert candidate_receipt["candidate_count"] == 3
    assert {
        candidate["decision_id"] for candidate in candidate_receipt["candidates"]
    } == {"decision-1", "decision-hold", "decision-rejected"}


def test_learning_port_rejects_incomplete_candidate_set_before_journal_write(
    tmp_path: Path,
) -> None:
    transport = _Transport(_transport_responses())
    journal_path = tmp_path / "review" / "sample_journal.jsonl"
    ports = _ports(
        transport=transport,
        journal_path=journal_path,
        snapshot_store=FileResearchSnapshotStore(tmp_path / "research-snapshots"),
    )
    ports[RunStage.DECISION_READY].payload["candidate_set_receipt"]["candidates"] = []

    with pytest.raises(StagePortContractError, match="candidate_set_incomplete"):
        _paper_loop(
            ports=ports,
            store=FileRunBundleStore(tmp_path / "run-bundles"),
        ).run(_context())

    assert not journal_path.exists()


def test_learning_port_refuses_to_create_a_second_fill_sample_path(
    tmp_path: Path,
) -> None:
    transport = _Transport(_transport_responses())
    journal_path = tmp_path / "review" / "sample_journal.jsonl"
    ports = _ports(
        transport=transport,
        journal_path=journal_path,
        snapshot_store=FileResearchSnapshotStore(tmp_path / "research-snapshots"),
    )
    execution_port = ports[RunStage.ORDERS_SIMULATED]
    execution_port.payload["order_receipts"][0]["status"] = "filled"
    execution_port.payload["journal_samples"] = [
        {
            "event_id": "unsafe-parallel-fill",
            "record_type": "fill",
            "order_id": "order-1",
        }
    ]

    with pytest.raises(
        StagePortContractError,
        match="direct_execution_sample_append_forbidden",
    ):
        _paper_loop(
            ports=ports,
            store=FileRunBundleStore(tmp_path / "run-bundles"),
        ).run(_context())

    assert not journal_path.exists()


def test_filled_order_is_recorded_only_through_canonical_sample_pipeline(
    tmp_path: Path,
) -> None:
    transport = _Transport(_transport_responses())
    journal_path = tmp_path / "review" / "sample_journal.jsonl"
    ports = _ports(
        transport=transport,
        journal_path=journal_path,
        snapshot_store=FileResearchSnapshotStore(tmp_path / "research-snapshots"),
    )
    receipt = ports[RunStage.ORDERS_SIMULATED].payload["order_receipts"][0]
    receipt.update(
        {
            "status": "filled",
            "filled_quantity": 100,
            "residual_quantity": 0,
            "filled_price_cny": 10.0,
            "fee_cny": 5.0,
            "slippage_cny": 1.0,
            "filled_at": "2026-07-16T01:06:00+00:00",
            "available_at": "2026-07-16T01:06:00+00:00",
            "ingested_at": "2026-07-16T01:06:00+00:00",
            "retrieved_as_of": "2026-07-16T01:06:00+00:00",
            "execution_eligible": True,
            "market_evidence_receipt_id": "market-fill-receipt-1",
            "capital_commit_receipt_id": "capital-commit-receipt-1",
            "capital_commit_status": "committed",
            "fill_fingerprint": "__CANONICAL_FILL_FINGERPRINT__",
        }
    )

    bundle = _paper_loop(
        ports=ports,
        store=FileRunBundleStore(tmp_path / "run-bundles"),
    ).run(_context())

    assert bundle.status == "completed", bundle.block_reasons
    events = SampleJournal(journal_path).read_events()
    fill_events = [event for event in events if event.get("record_type") == "fill"]
    assert len(fill_events) == 1
    assert fill_events[0]["sample_layer"] == "exploitation_fill"
    assert fill_events[0]["execution_eligible"] is True
    assert fill_events[0]["filled_quantity"] == 100
    assert fill_events[0]["filled_price"] == 10.0
    assert fill_events[0]["fee_cny"] == 5.0
    assert fill_events[0]["slippage_cny"] == 1.0
    assert len(fill_events[0]["receipt_sha256"]) == 64
    assert len(fill_events[0]["local_trade_sha256"]) == 64
    assert not any(event.get("event_id") == "unsafe-parallel-fill" for event in events)


def test_increase_fill_uses_buy_side_through_learning_and_reconcile(
    tmp_path: Path,
) -> None:
    transport = _Transport(_transport_responses())
    ports = _ports(
        transport=transport,
        journal_path=tmp_path / "review" / "sample_journal.jsonl",
        snapshot_store=FileResearchSnapshotStore(tmp_path / "research-snapshots"),
    )
    decision = ports[RunStage.DECISION_READY].payload["decisions"][0]
    decision["action"] = "increase"
    ports[RunStage.DECISION_READY].payload["small_account_plan"]["plan_decisions"][0][
        "action"
    ] = "increase"
    ports[RunStage.DECISION_READY].payload["small_account_plan"]["plan_sha256"] = (
        "__PLAN_SHA__"
    )
    order = ports[RunStage.RISK_CHECKED].payload["approved_orders"][0]
    order.update(intent="increase", side="buy")
    receipt = ports[RunStage.ORDERS_SIMULATED].payload["order_receipts"][0]
    receipt.update(
        {
            "intent": "increase",
            "status": "filled",
            "filled_quantity": 100,
            "residual_quantity": 0,
            "filled_price_cny": 10.0,
            "fee_cny": 5.0,
            "slippage_cny": 1.0,
            "filled_at": "2026-07-16T01:06:00+00:00",
            "available_at": "2026-07-16T01:06:00+00:00",
            "ingested_at": "2026-07-16T01:06:00+00:00",
            "retrieved_as_of": "2026-07-16T01:06:00+00:00",
            "execution_eligible": True,
            "market_evidence_receipt_id": "market-fill-receipt-increase",
            "capital_commit_receipt_id": "capital-commit-receipt-increase",
            "capital_commit_status": "committed",
            "fill_fingerprint": "__CANONICAL_FILL_FINGERPRINT__",
        }
    )

    bundle = _paper_loop(
        ports=ports,
        store=FileRunBundleStore(tmp_path / "run-bundles"),
    ).run(_context())

    assert bundle.status == "completed", bundle.block_reasons
    assert (
        bundle.receipt_for(RunStage.LEARNING_RECORDED).payload[
            "canonical_outcome_count"
        ]
        == 1
    )


def test_learning_readback_is_bound_to_run_not_bare_order_id(
    tmp_path: Path,
) -> None:
    journal_path = tmp_path / "review" / "sample_journal.jsonl"

    def filled_ports(*, suffix: str) -> dict[RunStage, Any]:
        ports = _ports(
            transport=_Transport(_transport_responses()),
            journal_path=journal_path,
            snapshot_store=FileResearchSnapshotStore(
                tmp_path / f"research-snapshots-{suffix}"
            ),
        )
        decision = ports[RunStage.DECISION_READY].payload["decisions"][0]
        decision["decision_id"] = f"decision-{suffix}"
        decision["decision_cluster_id"] = f"decision-cluster-{suffix}"
        prediction = ports[RunStage.DECISION_READY].payload["journal_predictions"][0]
        prediction.update(
            {
                "snapshot_id": f"prediction-{suffix}",
                "decision_id": f"decision-{suffix}",
                "decision_cluster_id": f"decision-cluster-{suffix}",
            }
        )
        candidate = ports[RunStage.DECISION_READY].payload["candidate_set_receipt"][
            "candidates"
        ][0]
        candidate.update(
            {
                "candidate_id": f"candidate-{suffix}",
                "decision_id": f"decision-{suffix}",
                "prediction_snapshot_id": f"prediction-{suffix}",
            }
        )
        plan_decision = ports[RunStage.DECISION_READY].payload["small_account_plan"][
            "plan_decisions"
        ][0]
        plan_decision["decision_id"] = f"decision-{suffix}"
        ports[RunStage.DECISION_READY].payload["small_account_plan"]["plan_sha256"] = (
            "__PLAN_SHA__"
        )
        order = ports[RunStage.RISK_CHECKED].payload["approved_orders"][0]
        order.update(
            decision_id=f"decision-{suffix}",
            prediction_snapshot_id=f"prediction-{suffix}",
        )
        receipt = ports[RunStage.ORDERS_SIMULATED].payload["order_receipts"][0]
        receipt.update(
            {
                "status": "filled",
                "filled_quantity": 100,
                "residual_quantity": 0,
                "filled_price_cny": 10.0,
                "fee_cny": 5.0,
                "slippage_cny": 1.0,
                "filled_at": "2026-07-16T01:06:00+00:00",
                "available_at": "2026-07-16T01:06:00+00:00",
                "ingested_at": "2026-07-16T01:06:00+00:00",
                "retrieved_as_of": "2026-07-16T01:06:00+00:00",
                "execution_eligible": True,
                "market_evidence_receipt_id": f"market-fill-{suffix}",
                "capital_commit_receipt_id": f"capital-commit-{suffix}",
                "capital_commit_status": "committed",
                "fill_fingerprint": "__CANONICAL_FILL_FINGERPRINT__",
            }
        )
        return ports

    first = _paper_loop(
        ports=filled_ports(suffix="first"),
        store=FileRunBundleStore(tmp_path / "run-bundles-first"),
    ).run(_context())
    second_context = RunContext(
        trade_date="2026-07-17",
        market="ashare",
        authority_id="ashare-capital-v1",
        authority_generation=1,
        execution_lineage=LINEAGE,
        account_type="simulated",
        real_trading_enabled=False,
        champion_manifest_sha256=_digest("c"),
        decision_as_of="2026-07-17T01:05:00+00:00",
    )
    second = _paper_loop(
        ports=filled_ports(suffix="second"),
        store=FileRunBundleStore(tmp_path / "run-bundles-second"),
        decision_time=datetime.fromisoformat(second_context.decision_as_of),
    ).run(second_context)

    assert first.status == "completed"
    assert second.status == "completed"
    learning = second.receipt_for(RunStage.LEARNING_RECORDED).payload
    assert learning["canonical_outcome_count"] == 1
    assert learning["decision_exposure_count"] == 1
    assert learning["sample_count"] == 4
    owned_events = [
        event
        for event in SampleJournal(journal_path).read_events()
        if event.get("journal_event_id") in learning["journal_event_ids"]
    ]
    assert owned_events
    assert all(
        event.get("source_run_id") in {None, second.run_id} for event in owned_events
    )


def test_evidence_port_rejects_an_unconsumed_cursor_instead_of_using_partial_data(
    tmp_path: Path,
) -> None:
    transport = _Transport(_transport_responses(next_cursor="next-page"))
    ports = _ports(
        transport=transport,
        journal_path=tmp_path / "review" / "sample_journal.jsonl",
        snapshot_store=FileResearchSnapshotStore(tmp_path / "research-snapshots"),
    )

    with pytest.raises(StagePortContractError, match="pagination_incomplete"):
        _paper_loop(
            ports=ports,
            store=FileRunBundleStore(tmp_path / "run-bundles"),
        ).run(_context())

    assert not (tmp_path / "review" / "sample_journal.jsonl").exists()


def test_evidence_snapshot_replays_across_new_port_instance_without_transport(
    tmp_path: Path,
) -> None:
    snapshot_root = tmp_path / "research-snapshots"
    first_transport = _Transport(_transport_responses())
    first = _paper_loop(
        ports=_ports(
            transport=first_transport,
            journal_path=tmp_path / "review-first" / "sample_journal.jsonl",
            snapshot_store=FileResearchSnapshotStore(snapshot_root),
        ),
        store=FileRunBundleStore(tmp_path / "run-bundles-first"),
    ).run(_context())

    replay_transport = _Transport([])
    replay = _paper_loop(
        ports=_ports(
            transport=replay_transport,
            journal_path=tmp_path / "review-replay" / "sample_journal.jsonl",
            snapshot_store=FileResearchSnapshotStore(snapshot_root),
        ),
        store=FileRunBundleStore(tmp_path / "run-bundles-replay"),
    ).run(_context())

    assert first.status == "completed"
    assert replay.status == "completed"
    assert replay_transport.calls == []
    assert (
        replay.receipt_for(RunStage.EVIDENCE_READY).payload
        == first.receipt_for(RunStage.EVIDENCE_READY).payload
    )


def test_local_today_report_port_publishes_a_readback_verified_candidate(
    tmp_path: Path,
) -> None:
    stage_ports_module = importlib.import_module("shared.runtime.stage_ports")
    publisher_module = importlib.import_module("shared.runtime.publisher")
    transport = _Transport(_transport_responses())
    publish_root = tmp_path / "today"
    publisher = publisher_module.LocalRunBundlePublisher(publish_root)
    ports = _ports(
        transport=transport,
        journal_path=tmp_path / "review" / "sample_journal.jsonl",
        snapshot_store=FileResearchSnapshotStore(tmp_path / "research-snapshots"),
    )
    ports[RunStage.REPORTED] = stage_ports_module.LocalTodayReportPort(
        identity=ComponentIdentity(
            stage=RunStage.REPORTED,
            component_id="local-today-report-port",
            version="1",
            artifact_sha256=_digest("9"),
        ),
        publisher=publisher,
    )

    bundle = _paper_loop(
        ports=ports,
        store=FileRunBundleStore(tmp_path / "run-bundles"),
    ).run(_context())

    report = bundle.receipt_for(RunStage.REPORTED).payload
    artifact_path = publish_root / report["artifact_path"]
    assert bundle.status == "completed"
    assert report["projection_type"] == "today_run_local_candidate"
    assert report["local_candidate"] is True
    assert report["production_verified"] is False
    assert report["source_run_id"] == bundle.run_id
    assert Path(report["artifact_path"]).is_absolute() is False
    assert Path(report["latest_path"]).is_absolute() is False
    assert (
        report["artifact_sha256"]
        == hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    )
    assert report["artifact_sha256"] == report["readback_sha256"]
    pre_report_projection = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert pre_report_projection["status"] == "incomplete"
    assert pre_report_projection["stage_receipts"][-1]["stage"] == (
        RunStage.LEARNING_RECORDED.value
    )

    final_projection = publisher.publish(bundle)
    latest = json.loads(final_projection.latest_path.read_text(encoding="utf-8"))
    assert latest["status"] == "completed"
    assert latest["_projection"]["bundle_sha256"] == bundle.bundle_sha256
