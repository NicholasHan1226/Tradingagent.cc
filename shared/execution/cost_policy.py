"""Versioned, conservative A-share research cost and fill policy.

This module is a simulation/research baseline.  It is deliberately separate
from any broker fee schedule and never implies that an account is connected.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from shared.execution.execution_reality import (
    ASHARE_EXECUTION_REALITY_VERSION,
    ashare_execution_reality,
)


class CostPolicyError(ValueError):
    """Raised when a cost or fill request is not auditable."""


def _finite_positive(value: object, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CostPolicyError(f"{field}_must_be_numeric")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise CostPolicyError(f"{field}_must_be_positive")
    return number


def _aware(value: object, *, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise CostPolicyError(f"{field}_timezone_required")
    if value.utcoffset() is None:
        raise CostPolicyError(f"{field}_timezone_required")
    return value


@dataclass(frozen=True)
class AShareCostPolicy:
    policy_id: str
    commission_rate: float
    minimum_commission_cny: float
    sell_stamp_duty_rate: float
    transfer_fee_rate: float
    one_way_slippage_bps: float
    uncertainty_buffer_bps: float
    execution_reality_model_version: str
    source_semantics: str = "versioned_research_baseline_not_broker_quote"

    def __post_init__(self) -> None:
        if not self.policy_id or self.policy_id != self.policy_id.strip():
            raise CostPolicyError("policy_id_invalid")
        for field in (
            "commission_rate",
            "minimum_commission_cny",
            "sell_stamp_duty_rate",
            "transfer_fee_rate",
            "one_way_slippage_bps",
            "uncertainty_buffer_bps",
        ):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise CostPolicyError(f"{field}_must_be_numeric")
            if not math.isfinite(float(value)) or float(value) < 0:
                raise CostPolicyError(f"{field}_must_be_nonnegative")
        if self.execution_reality_model_version != ASHARE_EXECUTION_REALITY_VERSION:
            raise CostPolicyError("execution_reality_model_version_mismatch")


_ASHARE_EXECUTION_REALITY = ashare_execution_reality()
ASHARE_RESEARCH_COST_POLICY_V1 = AShareCostPolicy(
    policy_id="ashare-research-cost-v1",
    commission_rate=_ASHARE_EXECUTION_REALITY.commission_bps / 10_000.0,
    minimum_commission_cny=_ASHARE_EXECUTION_REALITY.min_commission_cny,
    sell_stamp_duty_rate=(_ASHARE_EXECUTION_REALITY.stamp_duty_sell_bps / 10_000.0),
    transfer_fee_rate=_ASHARE_EXECUTION_REALITY.transfer_fee_bps / 10_000.0,
    one_way_slippage_bps=10.0,
    uncertainty_buffer_bps=25.0,
    execution_reality_model_version=ASHARE_EXECUTION_REALITY_VERSION,
)


@dataclass(frozen=True)
class RoundTripCostEstimate:
    policy_id: str
    buy_commission_cny: float
    sell_commission_cny: float
    buy_transfer_fee_cny: float
    sell_transfer_fee_cny: float
    sell_stamp_duty_cny: float
    slippage_cny: float
    total_cost_cny: float
    total_cost_bps_on_entry: float


def commission(notional_cny: float, policy: AShareCostPolicy) -> float:
    notional = _finite_positive(notional_cny, field="notional_cny")
    return round(
        max(policy.minimum_commission_cny, notional * policy.commission_rate),
        6,
    )


def transfer_fee(notional_cny: float, policy: AShareCostPolicy) -> float:
    notional = _finite_positive(notional_cny, field="notional_cny")
    return round(notional * policy.transfer_fee_rate, 6)


def conservative_fill_price(
    *,
    side: str,
    signal_bar_time: datetime,
    fill_bar_time: datetime,
    next_bar_open: float,
    policy: AShareCostPolicy,
) -> float:
    """Return an adverse simulated fill strictly after the signal bar."""

    signal_time = _aware(signal_bar_time, field="signal_bar_time")
    fill_time = _aware(fill_bar_time, field="fill_bar_time")
    if fill_time <= signal_time:
        raise CostPolicyError("fill_bar_must_follow_signal_bar")
    if side not in {"buy", "sell"}:
        raise CostPolicyError("side_must_be_buy_or_sell")
    reference = _finite_positive(next_bar_open, field="next_bar_open")
    slip = policy.one_way_slippage_bps / 10_000.0
    multiplier = 1.0 + slip if side == "buy" else 1.0 - slip
    price = reference * multiplier
    if price <= 0:
        raise CostPolicyError("conservative_fill_nonpositive")
    return round(price, 6)


def conservative_planning_price(
    *,
    side: str,
    decision_reference_price: float,
    policy: AShareCostPolicy,
) -> float:
    """Reserve capital from information available at decision time only.

    This is a planning assumption, not a simulated fill.  The actual next-bar
    open is intentionally absent from the contract so it cannot leak into the
    feasible universe or target-position calculation.  Execution may later use
    :func:`conservative_fill_price` once the fill bar is actually observable.
    """

    if side not in {"buy", "sell"}:
        raise CostPolicyError("side_must_be_buy_or_sell")
    reference = _finite_positive(
        decision_reference_price,
        field="decision_reference_price",
    )
    adverse_bps = policy.one_way_slippage_bps + policy.uncertainty_buffer_bps
    adverse = adverse_bps / 10_000.0
    multiplier = 1.0 + adverse if side == "buy" else 1.0 - adverse
    price = reference * multiplier
    if price <= 0:
        raise CostPolicyError("conservative_planning_price_nonpositive")
    return round(price, 6)


def estimate_round_trip_cost(
    *,
    quantity: int,
    entry_reference_price: float,
    exit_reference_price: float,
    policy: AShareCostPolicy,
) -> RoundTripCostEstimate:
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
        raise CostPolicyError("quantity_must_be_positive_integer")
    entry = _finite_positive(entry_reference_price, field="entry_reference_price")
    exit_price = _finite_positive(exit_reference_price, field="exit_reference_price")
    buy_notional = quantity * entry
    sell_notional = quantity * exit_price
    buy_commission = commission(buy_notional, policy)
    sell_commission = commission(sell_notional, policy)
    buy_transfer_fee = transfer_fee(buy_notional, policy)
    sell_transfer_fee = transfer_fee(sell_notional, policy)
    stamp = round(sell_notional * policy.sell_stamp_duty_rate, 6)
    slippage = round(
        (buy_notional + sell_notional) * policy.one_way_slippage_bps / 10_000.0,
        6,
    )
    total = round(
        buy_commission
        + sell_commission
        + buy_transfer_fee
        + sell_transfer_fee
        + stamp
        + slippage,
        6,
    )
    return RoundTripCostEstimate(
        policy_id=policy.policy_id,
        buy_commission_cny=buy_commission,
        sell_commission_cny=sell_commission,
        buy_transfer_fee_cny=buy_transfer_fee,
        sell_transfer_fee_cny=sell_transfer_fee,
        sell_stamp_duty_cny=stamp,
        slippage_cny=slippage,
        total_cost_cny=total,
        total_cost_bps_on_entry=round(total / buy_notional * 10_000.0, 6),
    )
