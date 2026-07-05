#!/usr/bin/env python3
"""Margin and fee estimates for China futures simulation."""

from __future__ import annotations

from dataclasses import dataclass

from .contract_rules import ContractRule, get_contract_rule


@dataclass(frozen=True)
class OrderCostEstimate:
    """Estimated notional, margin and fee for a simulated futures order."""

    symbol: str
    side: str
    quantity: int
    price: float
    notional: float
    margin_required: float
    open_fee: float
    estimated_close_fee: float
    total_estimated_fee: float
    rule: ContractRule


def _coerce_quantity(value: object) -> int:
    try:
        quantity = int(float(value or 0))
    except (TypeError, ValueError) as exc:
        raise ValueError("quantity must be a positive integer") from exc
    if quantity <= 0:
        raise ValueError("quantity must be a positive integer")
    return quantity


def _coerce_price(value: object) -> float:
    try:
        price = float(value or 0.0)
    except (TypeError, ValueError) as exc:
        raise ValueError("price must be positive") from exc
    if price <= 0:
        raise ValueError("price must be positive")
    return price


def _fee(notional: float, quantity: int, fee_rule: float, fee_type: str) -> float:
    normalized_type = str(fee_type or "").strip().lower()
    if normalized_type in {"rate", "notional_rate"}:
        return round(notional * fee_rule, 2)
    if normalized_type in {"fixed_per_lot", "per_lot", "fixed"}:
        return round(quantity * fee_rule, 2)
    raise ValueError(f"unsupported futures fee_type: {fee_type}")


def _validate_fee_rule(rule: ContractRule) -> None:
    _fee(1.0, 1, rule.open_fee_rate, rule.open_fee_type)
    _fee(1.0, 1, rule.close_fee_rate, rule.close_fee_type)


def estimate_order_cost(
    symbol: str,
    side: str,
    quantity: object,
    price: object,
) -> OrderCostEstimate:
    """Estimate margin and round-trip fee for a simulated order."""

    rule = get_contract_rule(symbol)
    _validate_fee_rule(rule)
    qty = _coerce_quantity(quantity)
    px = _coerce_price(price)
    direction = str(side or "").lower().strip()
    if direction not in {"buy", "sell", "long", "short"}:
        raise ValueError("side must be buy/sell/long/short")

    notional = round(px * rule.contract_multiplier * qty, 2)
    margin_required = round(notional * rule.margin_rate, 2)
    open_fee = _fee(notional, qty, rule.open_fee_rate, rule.open_fee_type)
    close_fee = _fee(notional, qty, rule.close_fee_rate, rule.close_fee_type)
    return OrderCostEstimate(
        symbol=str(symbol).strip().lower(),
        side=direction,
        quantity=qty,
        price=px,
        notional=notional,
        margin_required=margin_required,
        open_fee=open_fee,
        estimated_close_fee=close_fee,
        total_estimated_fee=round(open_fee + close_fee, 2),
        rule=rule,
    )


__all__ = ["OrderCostEstimate", "estimate_order_cost"]
