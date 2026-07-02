#!/usr/bin/env python3
"""Execution router: route orders by strategy stage.

Routes orders to the appropriate execution channel based on the strategy's
maturity stage:
  - "sim" -> sim_broker (slippage modeling, no capital)
  - "shadow" -> shadow_broker (record only, no execution)
  - "real" -> hermes_bridge signal cards (file communication only)

The progression is sim -> shadow -> real. A strategy must pass validation at
each stage before graduating to the next. The real channel never performs direct
execution; it only writes pending signal cards for Mac Mini-side handling.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from shared.accounting import position_ledger

TRADINGAGENT_ROOT = Path(__file__).resolve().parents[2]
TRADINGAGENT_ASHARE = TRADINGAGENT_ROOT / "Ashare"
SHADOW_EXECUTION_LOG = Path(__file__).resolve().parent.parent / "logs" / "shadow" / "shadow_trades.jsonl"
REAL_AUTO_ORDER_FORBIDDEN = True
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


def _ensure_tradings_ashare_on_path() -> None:
    ashare_dir = str(TRADINGAGENT_ASHARE)
    if ashare_dir not in sys.path:
        sys.path.insert(0, ashare_dir)


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


def _configure_shadow_broker_paths(shadow_broker_module: Any) -> None:
    shadow_dir = SHADOW_EXECUTION_LOG.parent
    shadow_broker_module.SHADOW_DIR = shadow_dir
    shadow_broker_module.SHADOW_TRADES = SHADOW_EXECUTION_LOG
    shadow_broker_module.SHADOW_POSITIONS = shadow_dir / "shadow_positions.json"
    shadow_broker_module.SHADOW_PNL = shadow_dir / "shadow_pnl.json"
    shadow_broker_module.SHADOW_LOCK = shadow_dir / ".shadow.lock"


def _resolve_trade_date(order: dict[str, Any]) -> str:
    for field in ("trade_date", "current_trade_date", "current_date", "order_date", "timestamp"):
        value = order.get(field)
        if value:
            return _date_part(value, "")

    context = order.get("context")
    if isinstance(context, dict):
        for field in ("trade_date", "current_trade_date", "current_date", "order_date", "timestamp"):
            value = context.get(field)
            if value:
                return _date_part(value, "")

    return ""


def _resolve_position_entry_date(order: dict[str, Any]) -> str | None:
    ts_code = str(order.get("ts_code", "")).strip()
    if not ts_code:
        return None

    layer_value = order.get("capital_layer")
    positions: list[dict[str, Any]]
    if layer_value:
        try:
            positions = position_ledger.get_positions(capital_layer=str(layer_value))
        except ValueError:
            positions = position_ledger.get_positions(capital_layer="all")
    else:
        positions = position_ledger.get_positions(capital_layer="all")

    matches = [position for position in positions if str(position.get("ts_code", "")).strip() == ts_code]
    if len(matches) != 1:
        return None

    entry_date = matches[0].get("entry_date")
    return str(entry_date).strip() if entry_date else None


def _check_t_plus_1(order: dict[str, Any]) -> dict[str, Any] | None:
    side = str(order.get("direction", order.get("side", ""))).lower().strip()
    if side not in {"sell", "reduce"}:
        return None

    trade_date = _resolve_trade_date(order)
    if not trade_date:
        return {
            "channel": "none",
            "executed": False,
            "result": {
                "status": "blocked_t_plus_1",
                "message": "Missing current_trade_date for sell-side T+1 check",
            },
            "order_id": order.get("order_id", ""),
            "message": "T+1 not satisfied",
        }

    entry_date = _resolve_position_entry_date(order)
    if not entry_date:
        return {
            "channel": "none",
            "executed": False,
            "result": {
                "status": "blocked_t_plus_1",
                "message": "Missing entry_date in position ledger for sell-side T+1 check",
                "trade_date": trade_date,
            },
            "order_id": order.get("order_id", ""),
            "message": "T+1 not satisfied",
        }

    _ensure_tradings_ashare_on_path()
    from t_plus_1 import can_sell

    if can_sell(entry_date, trade_date):
        return None

    return {
        "channel": "none",
        "executed": False,
        "result": {
            "status": "blocked_t_plus_1",
            "entry_date": entry_date,
            "trade_date": trade_date,
        },
        "order_id": order.get("order_id", ""),
        "message": "T+1 not satisfied",
    }


def _extract_strategy_stats(order: dict[str, Any]) -> dict[str, Any] | None:
    for field in ("stats", "strategy_stats", "performance_stats", "graduation_stats"):
        value = order.get(field)
        if isinstance(value, dict):
            return value
    return None


def _date_part(value: Any, fallback: str) -> str:
    if not value:
        return fallback
    text = str(value)
    if "T" in text:
        return text.split("T", 1)[0]
    return text[:10]


def _as_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def _build_graduation_receipt(strategy_name: str, graduation: dict[str, Any]) -> dict[str, Any]:
    return {
        "issued_by": "execution_router",
        "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "strategy_name": strategy_name,
        "current_stage": "shadow",
        "next_stage": "real",
        "ready": bool(graduation.get("ready", False)),
        "thresholds": dict(graduation.get("thresholds") or {}),
        "met": dict(graduation.get("met") or {}),
        "message": str(graduation.get("message") or ""),
    }


def _build_real_signal_order(
    order: dict[str, Any],
    direction: str,
    graduation_receipt: dict[str, Any],
) -> dict[str, Any]:
    timestamp = str(order.get("timestamp") or datetime.now().astimezone().isoformat(timespec="seconds"))
    trade_date = _date_part(order.get("trade_date") or order.get("current_date") or timestamp, datetime.now().strftime("%Y-%m-%d"))
    price = order.get("price", order.get("limit_price", order.get("execution_price")))
    source_condition_id = str(
        order.get("source_condition_id")
        or order.get("condition_id")
        or order.get("order_id")
        or ""
    )

    trigger = dict(order.get("trigger") or {})
    trigger.setdefault("condition_id", source_condition_id)
    trigger.setdefault("triggered_at", str(order.get("triggered_at") or timestamp))
    trigger.setdefault("trigger_price", order.get("trigger_price", price))

    risk_check = order.get("risk_check")
    if isinstance(risk_check, dict):
        risk_check = dict(risk_check)
        risk_check["passed"] = bool(risk_check.get("passed", False))
        risk_check["checks"] = _as_string_list(risk_check.get("checks"))
    else:
        risk_check = {
            "passed": bool(order.get("risk_passed", False)),
            "checks": _as_string_list(order.get("risk_checks", ["missing_risk_check"])),
        }

    t_plus_1 = dict(order.get("t_plus_1") or {})
    t_plus_1.setdefault("sellable_from", _date_part(order.get("sellable_from") or order.get("position_open_date"), trade_date))
    t_plus_1.setdefault("sellable_date", _date_part(order.get("sellable_date"), trade_date))

    return {
        "order_id": order.get("order_id", ""),
        "ts_code": order.get("ts_code", ""),
        "direction": direction,
        "quantity": int(order.get("quantity", 0)),
        "price": price,
        "stop_loss": order.get("stop_loss"),
        "strategy_name": order.get("strategy_name", ""),
        "timestamp": timestamp,
        "capital_layer": "real",
        "account_type": "real",
        "manual_confirm_required": True,
        "direct_execution": False,
        "trigger": trigger,
        "evidence_refs": _as_string_list(order.get("evidence_refs")),
        "valid_until": _date_part(order.get("valid_until"), trade_date),
        "risk_check": risk_check,
        "source_condition_id": source_condition_id,
        "idempotency_key": str(order.get("idempotency_key") or order.get("order_id") or source_condition_id),
        "t_plus_1": t_plus_1,
        "graduation_receipt": graduation_receipt,
    }


def _build_shadow_record_order(order: dict[str, Any]) -> dict[str, Any]:
    side = str(order.get("direction", order.get("side", "buy"))).lower().strip()
    normalized_side = {"buy": "buy", "sell": "sell", "reduce": "sell"}.get(side, side)
    price = order.get("price", order.get("limit_price", order.get("execution_price", 0.0)))
    return {
        "ts_code": order.get("ts_code", ""),
        "side": normalized_side,
        "quantity": int(order.get("quantity", 0)),
        "price": float(price or 0.0),
        "commission": float(order.get("commission", 0.0) or 0.0),
        "trade_date": _resolve_trade_date(order) or datetime.now().strftime("%Y-%m-%d"),
        "capital_layer": str(order.get("capital_layer") or "shadow"),
        "note": str(order.get("note") or order.get("source_decision_id") or order.get("reason") or ""),
    }


def _find_position_snapshot(ts_code: str, capital_layer: str) -> dict[str, Any] | None:
    positions = position_ledger.get_positions(capital_layer=capital_layer)
    for position in positions:
        if str(position.get("ts_code", "")).strip() == ts_code:
            return position
    return None


def _seed_shadow_position_from_ledger(
    shadow_broker_module: Any,
    order: dict[str, Any],
    strategy_name: str,
) -> dict[str, Any] | None:
    capital_layer = str(order.get("capital_layer") or "shadow")
    ts_code = str(order.get("ts_code", "")).strip()
    if not ts_code or capital_layer == "real":
        return None

    ledger_position = _find_position_snapshot(ts_code, capital_layer)
    if not ledger_position:
        return None

    trade_date = _resolve_trade_date(order) or datetime.now().strftime("%Y-%m-%d")
    shadow_state = shadow_broker_module.get_shadow_pnl(strategy_name, trade_date)
    existing_position = shadow_state.get("positions", {}).get(ts_code, {})
    deficit = int(ledger_position.get("quantity", 0)) - int(existing_position.get("quantity", 0))
    if deficit <= 0:
        return None

    seed_order = {
        "ts_code": ts_code,
        "side": "buy",
        "quantity": deficit,
        "price": float(ledger_position.get("avg_price") or order.get("price") or 0.0),
        "commission": 0.0,
        "trade_date": str(ledger_position.get("entry_date") or trade_date),
        "capital_layer": capital_layer,
        "note": "router_seed_from_position_ledger",
        "market": order.get("market"),
    }
    if seed_order["price"] <= 0:
        return None
    return shadow_broker_module.record_shadow(seed_order, strategy_name)


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
    graduation_receipt: dict[str, Any] | None = None
    if stage == "real":
        strategy_name = str(order.get("strategy_name") or order.get("strategy") or "").strip()
        stats = _extract_strategy_stats(order)
        if not strategy_name or stats is None:
            result = {
                "status": "rejected_graduation",
                "ready": False,
                "message": "graduation not met",
                "reason": "missing strategy_name or stats",
                "real_auto_order_forbidden": True,
                "direct_execution": False,
            }
            _log_route(order, channel, result)
            return {
                "channel": channel,
                "executed": False,
                "result": result,
                "order_id": order.get("order_id", ""),
                "message": "graduation not met",
            }

        graduation = check_graduation(strategy_name, "shadow", stats)
        if not graduation.get("ready", False):
            result = {
                "status": "rejected_graduation",
                "ready": False,
                "message": "graduation not met",
                "graduation": graduation,
                "real_auto_order_forbidden": True,
                "direct_execution": False,
            }
            _log_route(order, channel, result)
            return {
                "channel": channel,
                "executed": False,
                "result": result,
                "order_id": order.get("order_id", ""),
                "message": "graduation not met",
            }
        graduation_receipt = _build_graduation_receipt(strategy_name, graduation)

    t_plus_1_block = _check_t_plus_1(order)
    if t_plus_1_block is not None:
        _log_route(order, "none", t_plus_1_block["result"])
        return t_plus_1_block

    if channel == "sim_broker":
        try:
            from Ashare.sim_executor import ashare_sim_execute

            tr = ashare_sim_execute(order, account=order.get("account"), config={
                "dry_run": order.get("dry_run", False),
                "mock": order.get("mock", order.get("dry_run", False)),
            })
            executed = tr.status in ("filled", "pending", "ok", "warning", "dry_run_ok")
            result = {
                "status": tr.status,
                "filled_qty": tr.filled_qty,
                "avg_price": tr.avg_price,
                "message": tr.message,
                "slippage": 0.0,
                "order_id": tr.order_id,
            }
            message = f"Sim executed: {tr.status} @ {tr.avg_price} (qty {tr.filled_qty})"
        except Exception as exc:
            try:
                from .sim_broker import simulate_order

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
            from . import shadow_broker

            _configure_shadow_broker_paths(shadow_broker)
            strategy_name = str(order.get("strategy_name") or order.get("strategy") or "").strip()
            shadow_order = _build_shadow_record_order(order)
            result = shadow_broker.record_shadow(shadow_order, strategy_name)
            if (
                not result.get("recorded")
                and shadow_order["side"] == "sell"
                and result.get("status") == "rejected"
                and "exceeds existing shadow position" in str(result.get("message", ""))
            ):
                seed_result = _seed_shadow_position_from_ledger(shadow_broker, order, strategy_name)
                if seed_result and seed_result.get("recorded"):
                    result = shadow_broker.record_shadow(shadow_order, strategy_name)
            result["ledger_path"] = str(SHADOW_EXECUTION_LOG)
            if result.get("recorded"):
                result["status"] = "shadow_recorded"
                result["message"] = f"shadow_recorded to {SHADOW_EXECUTION_LOG}"
            executed = bool(result.get("recorded"))
            message = result["message"]
        except Exception as exc:
            result = {"status": "error", "recorded": False, "message": str(exc)}
            executed = False
            message = f"Shadow record failed: {exc}"

    elif channel == "hermes_bridge":
        try:
            from .hermes_bridge import real_auto_order_forbidden, send_order

            if not (REAL_AUTO_ORDER_FORBIDDEN and real_auto_order_forbidden):
                result = {
                    "status": "aborted",
                    "message": "Real auto-order safety flag is not locked to True",
                    "real_auto_order_forbidden": False,
                    "direct_execution": False,
                }
                executed = False
                message = result["message"]
            else:
                side = str(order.get("direction", order.get("side", "buy"))).lower().strip()
                direction = {"buy": "buy", "sell": "sell", "reduce": "sell"}.get(side, side)
                if graduation_receipt is None:
                    raise ValueError("missing graduation receipt for real route")
                signal_order = _build_real_signal_order(order, direction, graduation_receipt)
                result = send_order(signal_order, router_authorized=True)
                executed = False
                result["real_auto_order_forbidden"] = True
                result["direct_execution"] = False
                message = result.get("message", "Signal card queued")
        except Exception as exc:
            result = {
                "status": "error",
                "message": str(exc),
                "real_auto_order_forbidden": True,
                "direct_execution": False,
            }
            executed = False
            message = f"Signal card route failed: {exc}"

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
