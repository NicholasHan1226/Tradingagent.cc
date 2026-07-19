from __future__ import annotations

from copy import deepcopy

from shared.models.drift_runtime import DriftRuntimeConstraint
from shared.runtime.day_loop import StageRequest, StageResult
from shared.runtime.drift_stage import (
    DriftConstrainedRiskStagePort,
    DriftConstrainedSimulationExecutionStagePort,
)
from shared.runtime.run_bundle import ComponentIdentity, RunStage


def _identity(char: str = "a") -> ComponentIdentity:
    return ComponentIdentity(
        stage=RunStage.RISK_CHECKED,
        component_id="fixture-risk-port",
        version="1",
        artifact_sha256=char * 64,
    )


class _RiskPort:
    def __init__(self, payload: dict, *, char: str = "a") -> None:
        self.identity = _identity(char)
        self.payload = deepcopy(payload)
        self.calls = 0

    def execute(self, request: StageRequest) -> StageResult:
        self.calls += 1
        return StageResult(payload=deepcopy(self.payload))


class _ConstraintProvider:
    def __init__(self, constraint: DriftRuntimeConstraint) -> None:
        self.constraint = constraint
        self.calls = 0

    def snapshot(self) -> DriftRuntimeConstraint:
        self.calls += 1
        return self.constraint


def _request() -> StageRequest:
    return StageRequest(
        run_id="run-1",
        stage=RunStage.RISK_CHECKED,
        idempotency_key="b" * 64,
        input_bundle_sha256="c" * 64,
        bundle=object(),  # the wrapper is intentionally not a business authority
        allowed_actions=("open", "increase", "reduce", "exit", "hold"),
        permitted_order_ids=(),
    )


def _payload() -> dict:
    return {
        "risk_policy_version": "v1",
        "oms_plan_id": "oms-1",
        "approved_orders": [
            {
                "decision_id": "buy-1",
                "order_id": "order-buy-1",
                "symbol": "000001.SZ",
                "intent": "open",
            },
            {
                "decision_id": "exit-1",
                "order_id": "order-exit-1",
                "symbol": "600000.SH",
                "intent": "exit",
            },
        ],
        "rejected_decisions": [],
    }


def _constraint(*, stopped: bool, receipt: str | None) -> DriftRuntimeConstraint:
    return DriftRuntimeConstraint(
        max_risk_multiplier=0.0 if stopped else 1.0,
        stop_new_orders=stopped,
        reduce_only=stopped,
        quarantined=False,
        review_required=stopped,
        active_action_receipt_sha256=receipt,
        reason_codes=("drift_latch",) if stopped else (),
    )


def test_neutral_constraint_is_bound_without_changing_orders() -> None:
    base = _RiskPort(_payload())
    provider = _ConstraintProvider(_constraint(stopped=False, receipt=None))
    port = DriftConstrainedRiskStagePort(
        base_port=base,
        constraint_provider=provider,
    )

    result = port.execute(_request()).payload

    assert result["approved_orders"] == _payload()["approved_orders"]
    assert result["rejected_decisions"] == []
    assert result["drift_constraint"]["stop_new_orders"] is False
    assert result["drift_constraint_sha256"] == port.constraint_sha256
    assert base.calls == 1
    assert provider.calls == 1


def test_persisted_drift_latch_blocks_new_risk_but_preserves_exit() -> None:
    receipt = "d" * 64
    port = DriftConstrainedRiskStagePort(
        base_port=_RiskPort(_payload()),
        constraint_provider=_ConstraintProvider(
            _constraint(stopped=True, receipt=receipt)
        ),
    )

    result = port.execute(_request()).payload

    assert [row["order_id"] for row in result["approved_orders"]] == ["order-exit-1"]
    assert result["rejected_decisions"] == [
        {
            "decision_id": "buy-1",
            "reason": f"drift_stop_new_risk:{receipt}",
        }
    ]
    assert result["drift_constraint"]["active_action_receipt_sha256"] == receipt
    assert result["drift_constraint"]["reduce_only"] is True


def test_constraint_receipt_is_bound_to_component_identity_and_idempotent_result() -> (
    None
):
    first = DriftConstrainedRiskStagePort(
        base_port=_RiskPort(_payload()),
        constraint_provider=_ConstraintProvider(
            _constraint(stopped=True, receipt="d" * 64)
        ),
    )
    replay = DriftConstrainedRiskStagePort(
        base_port=_RiskPort(_payload()),
        constraint_provider=_ConstraintProvider(
            _constraint(stopped=True, receipt="d" * 64)
        ),
    )
    changed = DriftConstrainedRiskStagePort(
        base_port=_RiskPort(_payload(), char="b"),
        constraint_provider=_ConstraintProvider(
            _constraint(stopped=True, receipt="e" * 64)
        ),
    )

    assert first.identity == replay.identity
    assert first.identity != changed.identity
    assert first.execute(_request()) == first.execute(_request())


def test_risk_stage_rereads_latch_and_never_reuses_looser_cached_result() -> None:
    provider = _ConstraintProvider(_constraint(stopped=False, receipt=None))
    base = _RiskPort(_payload())
    port = DriftConstrainedRiskStagePort(
        base_port=base,
        constraint_provider=provider,
    )

    neutral = port.execute(_request()).payload
    provider.constraint = _constraint(stopped=True, receipt="f" * 64)
    tightened = port.execute(_request()).payload

    assert len(neutral["approved_orders"]) == 2
    assert [row["order_id"] for row in tightened["approved_orders"]] == ["order-exit-1"]
    assert provider.calls == 2
    assert base.calls == 1


class _Bundle:
    def __init__(self) -> None:
        self.context = type("Context", (), {"execution_lineage": "lineage-1"})()

    def receipt_for(self, stage: RunStage):
        assert stage is RunStage.RISK_CHECKED
        return type(
            "Receipt",
            (),
            {"payload": {"approved_orders": _payload()["approved_orders"]}},
        )()


def _execution_request() -> StageRequest:
    return StageRequest(
        run_id="run-1",
        stage=RunStage.ORDERS_SIMULATED,
        idempotency_key="1" * 64,
        input_bundle_sha256="2" * 64,
        bundle=_Bundle(),  # type: ignore[arg-type]
        allowed_actions=("open", "increase", "reduce", "exit", "hold"),
        permitted_order_ids=("order-buy-1", "order-exit-1"),
    )


def _execution_payload() -> dict:
    return {
        "execution_lineage": "lineage-1",
        "account_type": "simulated",
        "real_trading_enabled": False,
        "order_receipts": [
            {
                "order_id": "order-buy-1",
                "symbol": "000001.SZ",
                "intent": "open",
                "status": "filled",
                "requested_quantity": 100,
                "filled_quantity": 100,
                "residual_quantity": 0,
                "filled_price_cny": 10.0,
                "fill_fingerprint": "a" * 64,
                "capital_commit_status": "committed",
            },
            {
                "order_id": "order-exit-1",
                "symbol": "600000.SH",
                "intent": "exit",
                "status": "filled",
                "requested_quantity": 100,
                "filled_quantity": 100,
                "residual_quantity": 0,
                "filled_price_cny": 8.0,
                "fill_fingerprint": "b" * 64,
                "capital_commit_status": "committed",
            },
        ],
        "unknown_order_ids": [],
    }


def test_simulation_execution_rereads_latch_and_never_fills_new_risk() -> None:
    provider = _ConstraintProvider(_constraint(stopped=True, receipt="f" * 64))
    base = _RiskPort(_execution_payload(), char="c")
    base.identity = ComponentIdentity(
        stage=RunStage.ORDERS_SIMULATED,
        component_id="fixture-simulation-port",
        version="1",
        artifact_sha256="c" * 64,
    )
    port = DriftConstrainedSimulationExecutionStagePort(
        base_port=base,
        constraint_provider=provider,
    )

    result = port.execute(_execution_request()).payload
    buy, sell = result["order_receipts"]

    assert buy["status"] == "not_filled"
    assert buy["filled_quantity"] == 0
    assert buy["residual_quantity"] == 100
    assert buy["capital_commit_status"] == "not_applicable"
    assert buy["nonfill_reason"].startswith("drift_stop_new_risk:")
    assert "filled_price_cny" not in buy
    assert sell["status"] == "filled"
    assert result["drift_execution_constraint"]["stop_new_orders"] is True
    assert provider.calls == 1
