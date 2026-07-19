"""Risk-stage adapter binding a persisted negative-only drift constraint.

The adapter has no authority to create or resize orders.  It may only preserve
an approved order or turn a new-risk order into an explicit rejection.  Exit
and reduction orders remain available when the position authority permits
them in the downstream day-loop validator.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any, Mapping, Protocol

from shared.models.drift_runtime import DriftRuntimeConstraint

from .day_loop import DayStagePort, StageRequest, StageResult
from .run_bundle import ComponentIdentity, RunStage


class DriftRiskStageContractError(RuntimeError):
    """Raised when a base risk result cannot be safely reduced."""


class DriftConstraintProvider(Protocol):
    """Read the latest durable negative-only drift constraint."""

    def snapshot(self) -> DriftRuntimeConstraint: ...


def _canonical_sha256(value: object) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise DriftRiskStageContractError("drift_constraint_not_canonical") from exc
    return hashlib.sha256(encoded).hexdigest()


def _snapshot_constraint(
    provider: DriftConstraintProvider,
) -> DriftRuntimeConstraint:
    snapshot = getattr(provider, "snapshot", None)
    if not callable(snapshot):
        raise DriftRiskStageContractError("drift_constraint_provider_invalid")
    try:
        constraint = snapshot()
    except Exception as exc:
        raise DriftRiskStageContractError("drift_constraint_snapshot_failed") from exc
    if not isinstance(constraint, DriftRuntimeConstraint):
        raise DriftRiskStageContractError("drift_constraint_snapshot_invalid")
    return constraint


def _assert_not_looser(
    current: DriftRuntimeConstraint,
    previous: DriftRuntimeConstraint | None,
) -> None:
    if previous is None:
        return
    if current.max_risk_multiplier > previous.max_risk_multiplier:
        raise DriftRiskStageContractError("drift_constraint_loosened_during_run")
    for field_name in (
        "stop_new_orders",
        "reduce_only",
        "quarantined",
        "review_required",
    ):
        if getattr(previous, field_name) and not getattr(current, field_name):
            raise DriftRiskStageContractError("drift_constraint_loosened_during_run")


class DriftConstrainedRiskStagePort:
    """Re-read a negative-only drift latch whenever risk is evaluated."""

    def __init__(
        self,
        *,
        base_port: DayStagePort,
        constraint_provider: DriftConstraintProvider,
    ) -> None:
        identity = getattr(base_port, "identity", None)
        if (
            not isinstance(identity, ComponentIdentity)
            or identity.stage is not RunStage.RISK_CHECKED
            or not callable(getattr(base_port, "execute", None))
        ):
            raise DriftRiskStageContractError("base_risk_port_invalid")
        artifact_sha256 = _canonical_sha256(
            {
                "base_port": identity.to_dict(),
                "constraint_provider_contract": (
                    "tradingagent.negative_only_drift_constraint_provider.v1"
                ),
                "schema_version": "tradingagent.drift_constrained_risk_stage.v2",
            }
        )
        self.identity = ComponentIdentity(
            stage=RunStage.RISK_CHECKED,
            component_id=f"drift-constrained-{identity.component_id}",
            version="2",
            artifact_sha256=artifact_sha256,
        )
        self.constraint_sha256: str | None = None
        self._base_port = base_port
        self._constraint_provider = constraint_provider
        self._strictest_constraint: DriftRuntimeConstraint | None = None
        self._base_results: dict[str, StageResult] = {}

    def execute(self, request: StageRequest) -> StageResult:
        if (
            not isinstance(request, StageRequest)
            or request.stage is not RunStage.RISK_CHECKED
        ):
            raise DriftRiskStageContractError("risk_stage_request_invalid")
        constraint = _snapshot_constraint(self._constraint_provider)
        _assert_not_looser(constraint, self._strictest_constraint)
        self._strictest_constraint = constraint
        context = constraint.to_day_loop_risk_context()
        constraint_sha256 = _canonical_sha256(context)
        self.constraint_sha256 = constraint_sha256
        base_result = self._base_results.get(request.idempotency_key)
        if base_result is None:
            base_result = self._base_port.execute(request)
            self._base_results[request.idempotency_key] = base_result
        if not isinstance(base_result, StageResult):
            raise DriftRiskStageContractError("base_risk_result_invalid")
        payload = deepcopy(dict(base_result.payload))
        approved = payload.get("approved_orders")
        rejected = payload.get("rejected_decisions")
        if not isinstance(approved, list) or not isinstance(rejected, list):
            raise DriftRiskStageContractError("risk_dispositions_invalid")

        rejected_ids: set[str] = set()
        for row in rejected:
            if not isinstance(row, Mapping):
                raise DriftRiskStageContractError("risk_rejection_invalid")
            decision_id = row.get("decision_id")
            if not isinstance(decision_id, str) or not decision_id:
                raise DriftRiskStageContractError("risk_rejection_invalid")
            if decision_id in rejected_ids:
                raise DriftRiskStageContractError("risk_rejection_duplicate")
            rejected_ids.add(decision_id)

        preserved: list[dict[str, Any]] = []
        generated_rejections: list[dict[str, str]] = []
        for row in approved:
            if not isinstance(row, Mapping):
                raise DriftRiskStageContractError("approved_order_invalid")
            order = deepcopy(dict(row))
            decision_id = order.get("decision_id")
            intent = str(order.get("intent") or "").strip().lower()
            if not isinstance(decision_id, str) or not decision_id or not intent:
                raise DriftRiskStageContractError("approved_order_invalid")
            if decision_id in rejected_ids:
                raise DriftRiskStageContractError("decision_disposition_conflict")
            if constraint.stop_new_orders and intent in {"open", "increase"}:
                receipt = constraint.active_action_receipt_sha256 or "unreceipted"
                generated_rejections.append(
                    {
                        "decision_id": decision_id,
                        "reason": f"drift_stop_new_risk:{receipt}",
                    }
                )
                rejected_ids.add(decision_id)
                continue
            preserved.append(order)

        payload["approved_orders"] = preserved
        payload["rejected_decisions"] = [
            *deepcopy(rejected),
            *generated_rejections,
        ]
        payload["drift_constraint"] = context
        payload["drift_constraint_sha256"] = constraint_sha256
        return StageResult(payload=payload)


class DriftConstrainedSimulationExecutionStagePort:
    """Re-read drift immediately before the network-closed simulator runs.

    This wrapper is deliberately simulation-only.  A future broker adapter must
    enforce the same authority before producing any external side effect.
    """

    _FILLED_ONLY_FIELDS = frozenset(
        {
            "capital_commit_receipt_id",
            "fee_cny",
            "fill_fingerprint",
            "filled_at",
            "filled_price_cny",
            "market_evidence_receipt_id",
            "slippage_cny",
        }
    )

    def __init__(
        self,
        *,
        base_port: DayStagePort,
        constraint_provider: DriftConstraintProvider,
    ) -> None:
        identity = getattr(base_port, "identity", None)
        if (
            not isinstance(identity, ComponentIdentity)
            or identity.stage is not RunStage.ORDERS_SIMULATED
            or not callable(getattr(base_port, "execute", None))
        ):
            raise DriftRiskStageContractError("base_simulation_port_invalid")
        self.identity = ComponentIdentity(
            stage=RunStage.ORDERS_SIMULATED,
            component_id=f"drift-constrained-{identity.component_id}",
            version="1",
            artifact_sha256=_canonical_sha256(
                {
                    "base_port": identity.to_dict(),
                    "constraint_provider_contract": (
                        "tradingagent.negative_only_drift_constraint_provider.v1"
                    ),
                    "schema_version": (
                        "tradingagent.drift_constrained_simulation_execution.v1"
                    ),
                }
            ),
        )
        self._base_port = base_port
        self._constraint_provider = constraint_provider
        self._strictest_constraint: DriftRuntimeConstraint | None = None

    def execute(self, request: StageRequest) -> StageResult:
        if (
            not isinstance(request, StageRequest)
            or request.stage is not RunStage.ORDERS_SIMULATED
        ):
            raise DriftRiskStageContractError("simulation_stage_request_invalid")
        constraint = _snapshot_constraint(self._constraint_provider)
        _assert_not_looser(constraint, self._strictest_constraint)
        self._strictest_constraint = constraint
        base_result = self._base_port.execute(request)
        if not isinstance(base_result, StageResult):
            raise DriftRiskStageContractError("base_simulation_result_invalid")
        payload = deepcopy(dict(base_result.payload))
        receipts = payload.get("order_receipts")
        if not isinstance(receipts, list):
            raise DriftRiskStageContractError("simulation_receipts_invalid")
        receipt_sha = constraint.active_action_receipt_sha256 or "unreceipted"
        if constraint.stop_new_orders:
            for index, row in enumerate(receipts):
                if not isinstance(row, Mapping):
                    raise DriftRiskStageContractError("simulation_receipt_invalid")
                receipt = deepcopy(dict(row))
                if str(receipt.get("intent") or "").strip().lower() not in {
                    "open",
                    "increase",
                }:
                    continue
                quantity = receipt.get("requested_quantity")
                if isinstance(quantity, bool) or not isinstance(quantity, int):
                    raise DriftRiskStageContractError(
                        "simulation_receipt_quantity_invalid"
                    )
                for field_name in self._FILLED_ONLY_FIELDS:
                    receipt.pop(field_name, None)
                receipt.update(
                    {
                        "capital_commit_status": "not_applicable",
                        "filled_quantity": 0,
                        "nonfill_reason": f"drift_stop_new_risk:{receipt_sha}",
                        "residual_quantity": quantity,
                        "status": "not_filled",
                    }
                )
                receipts[index] = receipt
        context = constraint.to_day_loop_risk_context()
        payload["drift_execution_constraint"] = context
        payload["drift_execution_constraint_sha256"] = _canonical_sha256(context)
        return StageResult(payload=payload)


__all__ = [
    "DriftConstrainedRiskStagePort",
    "DriftConstrainedSimulationExecutionStagePort",
    "DriftConstraintProvider",
    "DriftRiskStageContractError",
]
