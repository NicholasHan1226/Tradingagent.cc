#!/usr/bin/env python3
"""Registry for market-specific simulated-account executors."""

from __future__ import annotations

from typing import Any, Callable

SimExecutor = Callable[[dict[str, Any], dict[str, Any], dict[str, Any]], Any]

_SIM_EXECUTORS: dict[str, SimExecutor] = {}


def _normalize_market(market: str | None) -> str:
    return str(market or "").lower().strip()


def register_sim_executor(market: str, fn: SimExecutor) -> SimExecutor:
    """Register a simulated-account executor for ``market``."""
    market_key = _normalize_market(market)
    if not market_key:
        raise ValueError("market is required")
    if not callable(fn):
        raise TypeError("sim executor must be callable")
    _SIM_EXECUTORS[market_key] = fn
    return fn


def local_sim_executor(
    order: dict[str, Any],
    account: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
) -> Any:
    """Fallback executor that wraps the legacy local slippage simulator."""
    from .sim_broker import SimResult, simulate_order

    result = simulate_order(order)
    return SimResult(
        status=result.get("status", "failed"),
        filled_qty=int(result.get("filled_quantity", 0) or 0),
        avg_price=float(result.get("filled_price", 0.0) or 0.0),
        fee=float(result.get("fee", 0.0) or 0.0),
        message=str(result.get("message", "local slippage simulation fallback")),
        order_id=str(result.get("order_id", order.get("order_id", ""))),
        market=str(order.get("market", "")),
        raw_response=result,
    )


def get_sim_executor(market: str | None) -> SimExecutor | None:
    """Return the registered market executor, or the local fallback."""
    market_key = _normalize_market(market)
    return _SIM_EXECUTORS.get(market_key, local_sim_executor)
