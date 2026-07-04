#!/usr/bin/env python3
"""Simulation-only executor for China futures."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from shared.execution.sim_broker import SimResult
from shared.execution.sim_executor_registry import register_sim_executor

from . import MARKET
from .margin_model import estimate_order_cost


def _extract_symbol(order: dict[str, Any]) -> str:
    symbol = str(order.get("symbol") or order.get("ts_code") or order.get("contract") or "").strip()
    if not symbol:
        raise ValueError("futures symbol is required")
    return symbol


def _extract_price(order: dict[str, Any]) -> float:
    value = order.get("price", order.get("limit_price", order.get("mid_price")))
    try:
        price = float(value or 0.0)
    except (TypeError, ValueError) as exc:
        raise ValueError("price must be positive") from exc
    if price <= 0:
        raise ValueError("price must be positive")
    return price


def cn_futures_sim_execute(
    order: dict[str, Any],
    account: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
) -> SimResult:
    """Return a local simulated fill and never touch a real CTP account."""

    del account
    config = dict(config or {})
    symbol = _extract_symbol(order)
    side = str(order.get("side", order.get("direction", "buy"))).lower().strip()
    quantity = order.get("quantity", order.get("qty", 0))
    price = _extract_price(order)
    cost = estimate_order_cost(symbol=symbol, side=side, quantity=quantity, price=price)
    fee = (
        cost.total_estimated_fee
        if config.get("fee_mode") == "round_trip_estimate"
        else cost.open_fee
    )
    order_id = str(order.get("order_id") or f"SIM-CNF-{symbol.upper()}")

    return SimResult(
        status="filled",
        filled_qty=cost.quantity,
        avg_price=cost.price,
        fee=fee,
        message="Simulated China futures fill; real CTP trading disabled",
        capital_layer="simulated",
        account_type="simulated",
        order_id=order_id,
        market=MARKET,
        raw_response={
            "order_id": order_id,
            "symbol": cost.symbol,
            "side": cost.side,
            "quantity": cost.quantity,
            "price": cost.price,
            "notional": cost.notional,
            "margin_required": cost.margin_required,
            "open_fee": cost.open_fee,
            "estimated_close_fee": cost.estimated_close_fee,
            "total_estimated_fee": cost.total_estimated_fee,
            "fee_charged": fee,
            "exchange": cost.rule.exchange,
            "contract_multiplier": cost.rule.contract_multiplier,
            "tick_size": cost.rule.tick_size,
            "margin_rate": cost.rule.margin_rate,
            "night_session": cost.rule.night_session,
            "real_trading_enabled": False,
            "source": "cn_futures_sim_executor_static_rules",
            "rule": asdict(cost.rule),
        },
    )


register_sim_executor(MARKET, cn_futures_sim_execute)


__all__ = ["cn_futures_sim_execute"]
