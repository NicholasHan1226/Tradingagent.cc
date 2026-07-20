#!/usr/bin/env python3
"""Prediction Markets simulated executor backed by a local CLOB sandbox.

This module is research-only. It never calls Polymarket or any live venue and
must not be interpreted as a real-money execution path.
"""

from __future__ import annotations

import uuid
from typing import Any, Callable

from shared.execution.sim_broker import SimResult
from shared.execution.sim_executor_registry import register_sim_executor
from shared.markets.safety import reject_real_execution_payload

_VALID_SIDES = {"buy", "sell"}
_VALID_OUTCOMES = {"yes", "no"}
_MIN_PRICE = 0.0
_MAX_PRICE = 1.0
PAPER_BROKER_CONTRACT = "tradingagent.pm.research_sandbox.v1"
SIM_AUTHORITY_ID = "pm-research-sim-v1"


def _coerce_price(value: Any, field_name: str) -> float:
    try:
        price = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be numeric") from exc
    if not (_MIN_PRICE <= price <= _MAX_PRICE):
        raise ValueError(f"{field_name} must be within [0, 1]")
    return price


def _default_matcher(
    order: dict[str, Any],
    account: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    requested_price = _coerce_price(
        order.get("price", order.get("limit_price", order.get("mid_price", 0.5))),
        "price",
    )
    spread = float(config.get("sandbox_spread", 0.01) or 0.01)
    spread = max(0.0, min(spread, 0.10))
    side = str(order.get("side", "buy")).lower().strip()

    if side == "buy":
        matched_price = min(_MAX_PRICE, requested_price + spread / 2.0)
    else:
        matched_price = max(_MIN_PRICE, requested_price - spread / 2.0)
    return {
        "matched": True,
        "avg_price": round(matched_price, 4),
        "opponent_order_id": f"pm-sandbox-opp-{uuid.uuid4().hex[:8]}",
    }


def _resolve_matcher(config: dict[str, Any]) -> Callable[[dict[str, Any], dict[str, Any], dict[str, Any]], Any]:
    matcher = config.get("clob_matcher")
    if matcher is None:
        return _default_matcher
    if not callable(matcher):
        raise ValueError("config.clob_matcher must be callable")
    return matcher


def pm_sim_execute(
    order: dict[str, Any],
    account: dict[str, Any] | None,
    config: dict[str, Any] | None,
) -> SimResult:
    """Execute a PM simulated order against a local mock CLOB sandbox."""
    order = dict(order or {})
    account = dict(account or {})
    config = dict(config or {})
    reject_real_execution_payload(order, context="pm_sim_execute.order")
    reject_real_execution_payload(account, context="pm_sim_execute.account")
    reject_real_execution_payload(config, context="pm_sim_execute.config")

    side = str(order.get("side", "buy")).lower().strip()
    outcome = str(order.get("outcome", "yes")).lower().strip()

    if side not in _VALID_SIDES:
        raise ValueError("PM side must be buy or sell")
    if outcome not in _VALID_OUTCOMES:
        raise ValueError("PM outcome must be YES or NO")

    matcher = _resolve_matcher(config)
    match = matcher(order, account, config)
    if not isinstance(match, dict):
        raise ValueError("CLOB matcher must return a dict")
    if not bool(match.get("matched", True)):
        raise ValueError("PM simulated order did not match an opponent order")

    avg_price = _coerce_price(
        match.get("avg_price", order.get("price", order.get("limit_price", 0.5))),
        "avg_price",
    )
    order_id = str(order.get("order_id", f"PM-SIM-{uuid.uuid4().hex[:12]}"))
    market_id = str(order.get("market_id", order.get("symbol", "")))
    message = (
        "PM research-only CLOB sandbox fill; simulated only, not real money."
    )

    raw_response = {
        "venue": "pm_clob_sandbox",
        "mode": "research_only",
        "matched": True,
        "outcome": outcome.upper(),
        "side": side,
        "market_id": market_id,
        "account_id": account.get("account_id", "pm_sim"),
        "opponent_order_id": str(match.get("opponent_order_id", "")),
        "notes": message,
        "broker_contract": PAPER_BROKER_CONTRACT,
        "authority_id": SIM_AUTHORITY_ID,
    }
    raw_response.update(match)

    return SimResult(
        status="filled",
        filled_qty=1,
        avg_price=avg_price,
        fee=0.0,
        message=message,
        order_id=order_id,
        market="pm",
        raw_response=raw_response,
        broker_contract=PAPER_BROKER_CONTRACT,
        authority_id=SIM_AUTHORITY_ID,
    )


register_sim_executor(
    "pm",
    pm_sim_execute,
    simulation_contract=PAPER_BROKER_CONTRACT,
    authority_id=SIM_AUTHORITY_ID,
)
