#!/usr/bin/env python3
"""A-share simulated executor backed by the Mac Mini file bridge.

Production simulated orders must not talk to Tonghuashun UI directly from the
server-side Tradings process. Instead, this executor writes a pending signal
card for the Mini cron bridge and returns ``pending``. Local tests may enable
mock mode to return an immediate filled receipt without any UI dependency.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from shared.execution.signal_state_machine import SignalStateMachine
from shared.execution.sim_broker import SimResult
from shared.execution.sim_executor_registry import register_sim_executor


DEFAULT_SIGNALS_DIR = Path("/opt/investment/Tradings/signals")
MARKET = "ashare"
SIM_ACCOUNT = "ashare_sim"


def _account_name(account: dict[str, Any] | str | None) -> str:
    if isinstance(account, dict):
        for key in ("account", "account_id", "account_name", "name"):
            value = str(account.get(key, "")).strip()
            if value:
                return value
    value = str(account or "").strip()
    return value or SIM_ACCOUNT


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _coerce_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _signal_card(
    order: dict[str, Any],
    account: dict[str, Any] | str | None,
    config: dict[str, Any],
) -> dict[str, Any]:
    now = datetime.now().astimezone()
    today = now.date().isoformat()
    order_id = str(order.get("order_id") or f"SIM-ASHARE-{now.strftime('%Y%m%d%H%M%S')}")
    price = _coerce_float(order.get("price", order.get("limit_price", order.get("mid_price"))), 0.0)
    quantity = _coerce_int(order.get("quantity", order.get("qty", order.get("filled_qty"))), 0)
    side = str(order.get("side", order.get("direction", "buy"))).lower().strip() or "buy"
    return {
        "order_id": order_id,
        "market": MARKET,
        "ts_code": str(order.get("ts_code") or order.get("symbol") or "").strip(),
        "direction": side,
        "side": side,
        "quantity": quantity,
        "price": price,
        "trigger_price": price,
        "status": "pending",
        "capital_layer": "simulated",
        "account_type": "simulated",
        "account": _account_name(account),
        "manual_confirm_required": False,
        "direct_execution": True,
        "dry_run": bool(config.get("dry_run", False)),
        "strategy_name": str(order.get("strategy_name") or "ashare_sim_executor"),
        "timestamp": now.isoformat(timespec="seconds"),
        "valid_until": str(config.get("valid_until") or today),
        "idempotency_key": str(order.get("idempotency_key") or order_id),
        "source": "ashare_sim_executor_file_bridge",
        "bridge": "mini_hermes_file_bridge",
        "t_plus_1": {
            "sellable_from": str(config.get("sellable_from") or today),
            "sellable_date": str(config.get("sellable_date") or today),
        },
        "notes": "Production simulated execution is delegated to the Mac Mini bridge.",
    }


def ashare_sim_execute(
    order: dict[str, Any],
    account: dict[str, Any] | str | None = None,
    config: dict[str, Any] | None = None,
) -> SimResult:
    """Queue an A-share simulated order for Mini execution, or fill via mock."""

    config = dict(config or {})
    card = _signal_card(order, account, config)
    order_id = str(card["order_id"])
    mock_mode = bool(
        config.get("mock")
        or config.get("mock_filled")
        or config.get("mock_mini_filled")
    )

    if mock_mode:
        return SimResult(
            status="filled",
            filled_qty=int(card["quantity"]),
            avg_price=float(card["price"]),
            fee=float(config.get("mock_fee", 0.0) or 0.0),
            message="Local mock fill for A-share Mini bridge tests",
            order_id=order_id,
            market=MARKET,
            raw_response={
                "mode": "mock_filled",
                "signal_card": card,
            },
        )

    signals_dir = Path(config.get("signals_dir") or DEFAULT_SIGNALS_DIR)
    machine = SignalStateMachine(signals_dir)
    queued = machine.write_pending(card)
    return SimResult(
        status="pending",
        filled_qty=0,
        avg_price=0.0,
        fee=0.0,
        message="Queued for Mac Mini Hermes file bridge execution",
        order_id=order_id,
        market=MARKET,
        raw_response={
            "mode": "file_bridge_pending",
            "signals_dir": str(signals_dir),
            "signal_path": queued.get("signal_path", ""),
            "signal_card": queued.get("signal_card", card),
        },
    )


register_sim_executor(MARKET, ashare_sim_execute)


__all__ = ["ashare_sim_execute"]
