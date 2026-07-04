#!/usr/bin/env python3
"""Simulation-only executor for China futures."""

from __future__ import annotations

from dataclasses import asdict
import math
from typing import Any

from shared.execution.sim_broker import SimResult
from shared.execution.sim_executor_registry import register_sim_executor

from . import MARKET
from .margin_model import estimate_order_cost


DEFAULT_SLIPPAGE_BPS = 2.0
DEFAULT_VOLUME_PARTICIPATION = 0.05


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
        return parsed if parsed == parsed else default
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


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


def _round_to_tick(price: float, tick_size: float, *, side: str) -> float:
    ticks = price / tick_size
    if side in {"buy", "long"}:
        rounded = math.ceil(ticks) * tick_size
    else:
        rounded = math.floor(ticks) * tick_size
    decimals = max(0, min(8, len(str(tick_size).split(".", 1)[1]) if "." in str(tick_size) else 0))
    return round(max(tick_size, rounded), decimals)


def _limit_bounds(reference_price: float, limit_rate: float) -> tuple[float, float]:
    return (
        round(reference_price * (1.0 - limit_rate), 8),
        round(reference_price * (1.0 + limit_rate), 8),
    )


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
    requested_price = _extract_price(order)
    initial_cost = estimate_order_cost(symbol=symbol, side=side, quantity=1, price=requested_price)
    rule = initial_cost.rule
    reference_price = _safe_float(order.get("previous_close") or order.get("reference_price"), requested_price)
    limit_down, limit_up = _limit_bounds(reference_price, rule.price_limit_rate)
    if requested_price < limit_down or requested_price > limit_up:
        return SimResult(
            status="rejected",
            filled_qty=0,
            avg_price=0.0,
            fee=0.0,
            message="Simulated China futures reject: price outside static daily limit bounds",
            capital_layer="simulated",
            account_type="simulated",
            order_id=str(order.get("order_id") or f"SIM-CNF-{symbol.upper()}"),
            market=MARKET,
            raw_response={
                "symbol": symbol,
                "side": side,
                "requested_price": requested_price,
                "reference_price": reference_price,
                "limit_down": limit_down,
                "limit_up": limit_up,
                "price_limit_rate": rule.price_limit_rate,
                "real_trading_enabled": False,
                "source": "cn_futures_sim_executor_static_rules",
            },
        )

    slippage_bps = max(0.0, _safe_float(config.get("slippage_bps"), DEFAULT_SLIPPAGE_BPS))
    slippage_multiplier = 1.0 + (slippage_bps / 10000.0 if side in {"buy", "long"} else -slippage_bps / 10000.0)
    price = _round_to_tick(requested_price * slippage_multiplier, rule.tick_size, side=side)
    price = min(max(price, limit_down), limit_up)
    requested_qty = _safe_int(quantity)
    latest_volume = _safe_float(order.get("bar_volume") or order.get("volume"), 0.0)
    participation = min(1.0, max(0.0, _safe_float(config.get("volume_participation"), DEFAULT_VOLUME_PARTICIPATION)))
    max_fill_qty = requested_qty
    if latest_volume > 0 and participation > 0:
        max_fill_qty = max(1, int(latest_volume * participation))
    filled_qty = min(requested_qty, max_fill_qty)
    fill_status = "filled" if filled_qty >= requested_qty else "partial"
    cost = estimate_order_cost(symbol=symbol, side=side, quantity=quantity, price=price)
    if filled_qty != cost.quantity:
        cost = estimate_order_cost(symbol=symbol, side=side, quantity=filled_qty, price=price)
    fee = (
        cost.total_estimated_fee
        if config.get("fee_mode") == "round_trip_estimate"
        else cost.open_fee
    )
    order_id = str(order.get("order_id") or f"SIM-CNF-{symbol.upper()}")

    return SimResult(
        status=fill_status,
        filled_qty=cost.quantity,
        avg_price=cost.price,
        fee=fee,
        message="Simulated China futures fill with static slippage/liquidity rules; real CTP trading disabled",
        capital_layer="simulated",
        account_type="simulated",
        order_id=order_id,
        market=MARKET,
        raw_response={
            "order_id": order_id,
            "symbol": cost.symbol,
            "side": cost.side,
            "quantity": cost.quantity,
            "requested_quantity": requested_qty,
            "price": cost.price,
            "requested_price": requested_price,
            "slippage_bps": slippage_bps,
            "bar_volume": latest_volume,
            "volume_participation": participation,
            "fill_status": fill_status,
            "notional": cost.notional,
            "margin_required": cost.margin_required,
            "open_fee": cost.open_fee,
            "estimated_close_fee": cost.estimated_close_fee,
            "total_estimated_fee": cost.total_estimated_fee,
            "fee_charged": fee,
            "exchange": cost.rule.exchange,
            "contract_multiplier": cost.rule.contract_multiplier,
            "tick_size": cost.rule.tick_size,
            "price_limit_rate": cost.rule.price_limit_rate,
            "limit_down": limit_down,
            "limit_up": limit_up,
            "margin_rate": cost.rule.margin_rate,
            "night_session": cost.rule.night_session,
            "real_trading_enabled": False,
            "source": "cn_futures_sim_executor_static_rules",
            "rule": asdict(cost.rule),
        },
    )


register_sim_executor(MARKET, cn_futures_sim_execute)


__all__ = ["cn_futures_sim_execute"]
