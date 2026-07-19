"""Deterministic runtime port for persisted negative-only drift constraints."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Tuple

from .drift_action_store import DriftActionStore
from .drift_policy import SafeAutomaticAction


class DriftRuntimeContractError(ValueError):
    """Raised when a runtime risk request has no valid authority or shape."""


def _validated_multiplier(value: float) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not 0 <= float(value) <= 1
    ):
        raise DriftRuntimeContractError("risk_multiplier_out_of_range")
    return float(value)


@dataclass(frozen=True)
class DriftRuntimeConstraint:
    """Read-only risk cap that a day loop or risk engine can consume."""

    max_risk_multiplier: float
    stop_new_orders: bool
    reduce_only: bool
    quarantined: bool
    review_required: bool
    active_action_receipt_sha256: str | None
    reason_codes: Tuple[str, ...]
    schema_version: str = "tradingagent.drift_runtime_constraint.v1"

    def __post_init__(self) -> None:
        _validated_multiplier(self.max_risk_multiplier)
        if self.active_action_receipt_sha256 is not None and (
            len(self.active_action_receipt_sha256) != 64
            or any(
                char not in "0123456789abcdef"
                for char in self.active_action_receipt_sha256
            )
        ):
            raise DriftRuntimeContractError("active_receipt_sha256_invalid")

    def to_day_loop_risk_context(self) -> dict:
        """Return the provider-neutral mapping expected by orchestration ports."""

        return {
            "schema_version": self.schema_version,
            "active_action_receipt_sha256": self.active_action_receipt_sha256,
            "risk_multiplier_cap": self.max_risk_multiplier,
            "stop_new_orders": self.stop_new_orders,
            "reduce_only": self.reduce_only,
            "quarantined": self.quarantined,
            "review_required": self.review_required,
            "reason_codes": list(self.reason_codes),
        }


@dataclass(frozen=True)
class AppliedDriftRisk:
    constraint: DriftRuntimeConstraint
    effective_risk_multiplier: float
    order_allowed: bool


class DriftRuntimeRiskAdapter:
    """Load the explicit drift authority and cap risk without adding authority."""

    def __init__(self, action_store: DriftActionStore) -> None:
        if not isinstance(action_store, DriftActionStore):
            raise DriftRuntimeContractError("action_store_required")
        self._action_store = action_store

    def snapshot(self) -> DriftRuntimeConstraint:
        receipt = self._action_store.load_active(required=False)
        if receipt is None:
            return DriftRuntimeConstraint(
                max_risk_multiplier=1.0,
                stop_new_orders=False,
                reduce_only=False,
                quarantined=False,
                review_required=False,
                active_action_receipt_sha256=None,
                reason_codes=(),
            )

        actions = frozenset(receipt.actions)
        quarantined = SafeAutomaticAction.QUARANTINE in actions
        reduce_only = quarantined or SafeAutomaticAction.REDUCE_ONLY in actions
        stop_new_orders = (
            reduce_only
            or SafeAutomaticAction.STOP_NEW_RISK in actions
            or receipt.risk_multiplier <= 0
        )
        return DriftRuntimeConstraint(
            max_risk_multiplier=receipt.risk_multiplier,
            stop_new_orders=stop_new_orders,
            reduce_only=reduce_only,
            quarantined=quarantined,
            review_required=SafeAutomaticAction.REQUIRE_REVIEW in actions,
            active_action_receipt_sha256=receipt.receipt_sha256,
            reason_codes=receipt.reasons,
        )

    def apply(
        self,
        *,
        proposed_risk_multiplier: float,
        increases_gross_exposure: bool,
    ) -> AppliedDriftRisk:
        requested = _validated_multiplier(proposed_risk_multiplier)
        if not isinstance(increases_gross_exposure, bool):
            raise DriftRuntimeContractError("increases_gross_exposure_must_be_boolean")
        constraint = self.snapshot()
        return AppliedDriftRisk(
            constraint=constraint,
            effective_risk_multiplier=min(requested, constraint.max_risk_multiplier),
            order_allowed=not (increases_gross_exposure and constraint.stop_new_orders),
        )


__all__ = [
    "AppliedDriftRisk",
    "DriftRuntimeConstraint",
    "DriftRuntimeContractError",
    "DriftRuntimeRiskAdapter",
]
