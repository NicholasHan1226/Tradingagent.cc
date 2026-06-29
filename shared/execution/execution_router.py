#!/usr/bin/env python3
"""Execution router: route orders by strategy stage.

Routes orders to the appropriate execution channel based on the strategy's
maturity stage:
  - "sim" -> sim_broker (slippage modeling, no capital)
  - "shadow" -> shadow_broker (record only, no execution)
  - "real" -> hermes_bridge (real execution via Mac Mini, manual confirm)

The progression is sim -> shadow -> real. A strategy must pass validation at
each stage before graduating to the next.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

# Import execution modules
from hermes_bridge import send_order as hermes_send_order
from shadow_broker import record_shadow
from sim_broker import simulate_order

ROUTER_LOG = Path(__file__).resolve().parent.parent / "logs" / "router_decisions.jsonl"

# Stage configuration
STAGE_CHANNELS = {
    "sim": "sim_broker",
    "shadow": "shadow_broker",
    "real": "hermes_bridge",
}

# Graduation thresholds (a strategy must meet these to advance)
GRADUATION_THRESHOLDS = {
    "sim_to_shadow": {
        "min_sim_trades": 50,
        "min_fill_rate": 0.90,
        "max_avg_slippage_pct": 0.15,
    },
    "shadow_to_real": {
        "min_shadow_trades": 100,
        "min_positive_days_pct": 0.60,
        "max_max_drawdown_pct": 10.0,
    },
}


def _log_route(order: dict[str, Any], channel: str, result: dict[str, Any]) -> None:
    ROUTER_LOG.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.now().isoformat(),
        "order": order,
        "channel": channel,
        "result": result,
    }
    with open(ROUTER_LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def route(order: dict[str, Any], strategy_stage: str) -> dict[str, Any]:
    """Route an order to the appropriate execution channel.

    Args:
        order: Order dict with ts_code, side, quantity, etc.
        strategy_stage: "sim" | "shadow" | "real"

    Returns:
        dict with: channel, executed, result, order_id, message.
    """
    stage = strategy_stage.lower().strip()
    if stage not in STAGE_CHANNELS:
        return {
            "channel": "none",
            "executed": False,
            "result": {},
            "order_id": order.get("order_id", ""),
            "message": f"Unknown strategy_stage: {strategy_stage}. Must be one of {list(STAGE_CHANNELS.keys())}",
        }

    channel = STAGE_CHANNELS[stage]

    if channel == "sim_broker":
        result = simulate_order(order)
        executed = result.get("status") in ("filled", "partial")
        message = f"Simulated: {result.get('status')} @ slippage {result.get('slippage')}%"

    elif channel == "shadow_broker":
        strategy_name = order.get("strategy_name", "default")
        result = record_shadow(order, strategy_name)
        executed = result.get("recorded", False)
        message = result.get("message", "Shadow recorded")

    elif channel == "hermes_bridge":
        # Real orders go through Hermes with manual confirmation
        order["strategy_stage"] = stage
        result = hermes_send_order(order)
        executed = result.get("status") in ("sent", "pending_manual_confirm")
        message = result.get("message", "Hermes bridge")

    else:
        result = {}
        executed = False
        message = f"No handler for channel: {channel}"

    _log_route(order, channel, result)

    return {
        "channel": channel,
        "executed": executed,
        "result": result,
        "order_id": result.get("order_id", order.get("order_id", "")),
        "message": message,
    }


def check_graduation(strategy_name: str, current_stage: str, stats: dict[str, Any]) -> dict[str, Any]:
    """Check if a strategy is ready to graduate to the next stage.

    Args:
        strategy_name: Strategy name.
        current_stage: Current stage ("sim" | "shadow" | "real").
        stats: Performance stats dict.

    Returns:
        dict with: ready, next_stage, thresholds, met, message.
    """
    stage = current_stage.lower().strip()

    if stage == "sim":
        thresholds = GRADUATION_THRESHOLDS["sim_to_shadow"]
        met = {
            "min_sim_trades": stats.get("total_trades", 0) >= thresholds["min_sim_trades"],
            "min_fill_rate": stats.get("fill_rate", 0) >= thresholds["min_fill_rate"],
            "max_avg_slippage_pct": stats.get("avg_slippage", 999) <= thresholds["max_avg_slippage_pct"],
        }
        ready = all(met.values())
        return {
            "ready": ready,
            "next_stage": "shadow" if ready else "sim",
            "thresholds": thresholds,
            "met": met,
            "message": f"Strategy '{strategy_name}' {'ready' if ready else 'not ready'} for shadow stage",
        }

    elif stage == "shadow":
        thresholds = GRADUATION_THRESHOLDS["shadow_to_real"]
        met = {
            "min_shadow_trades": stats.get("total_trades", 0) >= thresholds["min_shadow_trades"],
            "min_positive_days_pct": stats.get("positive_days_pct", 0) >= thresholds["min_positive_days_pct"],
            "max_max_drawdown_pct": stats.get("max_drawdown_pct", 999) <= thresholds["max_max_drawdown_pct"],
        }
        ready = all(met.values())
        return {
            "ready": ready,
            "next_stage": "real" if ready else "shadow",
            "thresholds": thresholds,
            "met": met,
            "message": f"Strategy '{strategy_name}' {'ready' if ready else 'not ready'} for real stage",
        }

    elif stage == "real":
        return {
            "ready": False,
            "next_stage": "real",
            "thresholds": {},
            "met": {},
            "message": f"Strategy '{strategy_name}' is already at real stage",
        }

    else:
        return {
            "ready": False,
            "next_stage": current_stage,
            "thresholds": {},
            "met": {},
            "message": f"Unknown stage: {current_stage}",
        }


def get_route_history(strategy_name: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    """Get routing history from the log.

    Args:
        strategy_name: Filter by strategy name (optional).
        limit: Max number of entries to return.

    Returns:
        List of routing decision entries.
    """
    if not ROUTER_LOG.exists():
        return []

    entries = []
    with open(ROUTER_LOG, "r", encoding="utf-8") as fh:
        lines = fh.readlines()

    for line in reversed(lines):
        try:
            entry = json.loads(line)
            if strategy_name is None or entry.get("order", {}).get("strategy_name") == strategy_name:
                entries.append(entry)
                if len(entries) >= limit:
                    break
        except json.JSONDecodeError:
            continue

    return entries
