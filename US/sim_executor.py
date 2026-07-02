#!/usr/bin/env python3
"""US simulated executor backed by a local Alpaca paper-trading mock."""

from __future__ import annotations

import uuid
from typing import Any

from shared.execution.sim_broker import SimResult
from shared.execution.sim_executor_registry import register_sim_executor
from shared.markets.safety import reject_real_execution_payload


DEFAULT_SETTLEMENT = "T+2"
DEFAULT_FEE = 0.001


def _coerce_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return int(default)


def _mock_alpaca_submit_order(
    order: dict[str, Any],
    account: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    order_id = str(order.get("order_id") or f"ALPACA-PAPER-{uuid.uuid4().hex[:12]}")
    quantity = _coerce_int(order.get("quantity", order.get("qty")), 0)
    requested_price = _coerce_float(
        order.get("limit_price", order.get("price", order.get("mid_price"))),
        0.0,
    )
    fill_price = requested_price if requested_price > 0 else 100.0

    return {
        "id": order_id,
        "status": "filled",
        "symbol": str(order.get("symbol") or order.get("ts_code") or "").upper(),
        "qty": quantity,
        "filled_qty": quantity,
        "filled_avg_price": round(fill_price, 4),
        "side": str(order.get("side", "buy")).lower(),
        "type": str(order.get("order_type", order.get("type", "market"))).lower(),
        "time_in_force": str(config.get("time_in_force", order.get("time_in_force", "day"))).lower(),
        "asset_class": "us_equity",
        "account_id": str(account.get("account_id", "us_sim")),
    }


def us_sim_execute(
    order: dict[str, Any],
    account: dict[str, Any] | None,
    config: dict[str, Any] | None,
) -> SimResult:
    """Execute a US simulated order against a local Alpaca paper mock."""
    reject_real_execution_payload(order, context="us_sim_execute.order")
    reject_real_execution_payload(account or {}, context="us_sim_execute.account")
    reject_real_execution_payload(config or {}, context="us_sim_execute.config")
    order = dict(order or {})
    account_payload = dict(account or {})
    config_payload = dict(config or {})
    alpaca_order = _mock_alpaca_submit_order(order, account_payload, config_payload)
    settlement = str(config_payload.get("settlement", DEFAULT_SETTLEMENT))
    message = f"alpaca paper mock fill; settlement declared as {settlement}"

    return SimResult(
        status=alpaca_order.get("status", "filled"),
        filled_qty=_coerce_int(alpaca_order.get("filled_qty", alpaca_order.get("qty")), 0),
        avg_price=_coerce_float(alpaca_order.get("filled_avg_price"), 0.0),
        fee=DEFAULT_FEE,
        message=message,
        order_id=str(alpaca_order.get("id", order.get("order_id", ""))),
        market="us",
        raw_response={
            "broker": "alpaca_paper_mock",
            "settlement": settlement,
            "api_order": alpaca_order,
        },
    )


register_sim_executor("us", us_sim_execute)


__all__ = ["us_sim_execute"]
