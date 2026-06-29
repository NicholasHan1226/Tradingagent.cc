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
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ASHARE_TOOLS = Path("/opt/investment/Ashare/tools")
SHADOW_EXECUTION_LOG = Path("/opt/investment/Ashare/data/tradebook/simulated_execution_log.jsonl")
REAL_ORDER_SETUP_CARDS = Path("/opt/investment/Ashare/data/tradebook/real_order_setup_cards.jsonl")
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


def _ensure_ashare_tools_on_path() -> None:
    ashare_tools = str(ASHARE_TOOLS)
    if ashare_tools not in sys.path:
        sys.path.insert(0, ashare_tools)


def _write_jsonl(path: Path, entry: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


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
        try:
            _ensure_ashare_tools_on_path()
            from a_share_simulated_trade_executor import TradeRequest, execute_trade

            req = TradeRequest(
                code=order["ts_code"],
                name=order.get("name", ""),
                side=order.get("side", "buy"),
                quantity=int(order.get("quantity", 0)),
                reason=order.get("reason", order.get("strategy_name", "")),
                tradebook_id=order.get("order_id", ""),
            )
            tr = execute_trade(req, dry_run=order.get("dry_run", False))
            executed = tr.status in ("ok", "warning", "dry_run_ok")
            result = {
                "status": tr.status,
                "filled_qty": tr.filled_qty,
                "avg_price": tr.avg_price,
                "message": tr.message,
                "slippage": 0.0,
            }
            message = f"Sim executed: {tr.status} @ {tr.avg_price} (qty {tr.filled_qty})"
        except Exception as exc:
            try:
                from sim_broker import simulate_order

                result = simulate_order(order)
                result["ashare_executor_error"] = str(exc)
                executed = result.get("status") in ("filled", "partial")
                message = f"Sim fallback: {result.get('status')} @ slippage {result.get('slippage')}%"
            except Exception as fallback_exc:
                result = {
                    "status": "error",
                    "message": f"Ashare simulated executor failed: {exc}; fallback sim_broker failed: {fallback_exc}",
                    "slippage": 0.0,
                }
                executed = False
                message = result["message"]

    elif channel == "shadow_broker":
        try:
            quantity = int(order.get("quantity", 0))
            price = order.get("price", order.get("limit_price", order.get("execution_price", 0.0)))
            price_float = float(price or 0.0)
            amount = order.get("amount")
            amount_float = float(amount) if amount is not None else quantity * price_float
            entry = {
                "tradebook_id": order.get("order_id", ""),
                "code": order.get("ts_code", ""),
                "name": order.get("name", ""),
                "side": order.get("side", "buy"),
                "quantity": quantity,
                "price": price_float,
                "amount": amount_float,
                "strategy": order.get("strategy_name", ""),
                "horizon": order.get("horizon", ""),
                "capital_nature": order.get("capital_nature", order.get("capital_layer", "shadow")),
                "source_decision_id": order.get("source_decision_id", ""),
                "created_at": datetime.now().isoformat(),
                "status": "shadow_recorded",
            }
            _write_jsonl(SHADOW_EXECUTION_LOG, entry)
            result = {
                "status": "shadow_recorded",
                "recorded": True,
                "message": "Shadow trade recorded to simulated_execution_log.jsonl",
            }
            executed = True
            message = result["message"]
        except Exception as exc:
            result = {"status": "error", "recorded": False, "message": str(exc)}
            executed = False
            message = f"Shadow record failed: {exc}"

    elif channel == "hermes_bridge":
        try:
            _ensure_ashare_tools_on_path()
            from a_share_tonghuashun_execution import build_condition_order_setup, real_auto_order_forbidden

            side = str(order.get("side", "buy")).lower()
            action = {"buy": "buy", "sell": "sell", "reduce": "reduce"}.get(side, side)
            item = {
                "action": action,
                "code": order.get("ts_code", ""),
                "name": order.get("name", ""),
                "quantity": order.get("quantity", ""),
                "price": order.get("limit_price", order.get("price", "")),
                "strategy_name": order.get("strategy_name", ""),
                "condition": order.get("condition", ""),
                "reason": order.get("reason", ""),
            }
            setup = build_condition_order_setup(item, route="real")
            if not real_auto_order_forbidden(setup):
                result = {
                    "status": "aborted",
                    "setup": setup,
                    "message": "Real auto-order guard did not confirm the manual-only boundary",
                }
                executed = False
                message = result["message"]
            else:
                _write_jsonl(
                    REAL_ORDER_SETUP_CARDS,
                    {
                        "created_at": datetime.now().isoformat(),
                        "order_id": order.get("order_id", ""),
                        "setup": setup,
                    },
                )
                result = {
                    "status": "pending_manual_confirm",
                    "setup": setup,
                    "message": "Real order setup card generated - manual confirm required in Tonghuashun",
                }
                executed = False
                message = result["message"]
        except Exception as exc:
            result = {"status": "error", "message": str(exc)}
            executed = False
            message = f"Real order setup failed: {exc}"

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
