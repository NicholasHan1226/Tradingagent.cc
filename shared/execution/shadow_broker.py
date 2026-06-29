#!/usr/bin/env python3
"""Shadow broker: record orders without executing. Multi-strategy parallel.

The shadow broker accepts orders from multiple strategies simultaneously,
records them to a per-strategy ledger, and tracks P&L without any real
or simulated execution. It is the "what would have happened" record.

Reference: Ashare/tools/a_share_shadow_sim_broker.py
"""

from __future__ import annotations

import csv
import json
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, date
from pathlib import Path
from typing import Any

SHADOW_DIR = Path(__file__).resolve().parent.parent / "logs" / "shadow"
SHADOW_TRADES = SHADOW_DIR / "shadow_trades.jsonl"
SHADOW_POSITIONS = SHADOW_DIR / "shadow_positions.json"
SHADOW_PNL = SHADOW_DIR / "shadow_pnl.json"

SHADOW_TRADE_FIELDS = [
    "trade_id",
    "strategy_name",
    "trade_date",
    "ts_code",
    "side",
    "quantity",
    "price",
    "amount",
    "commission",
    "net_amount",
    "status",
    "created_at",
    "note",
]


@dataclass
class ShadowTrade:
    """A shadow trade record (no execution)."""

    trade_id: str = field(default_factory=lambda: f"SHADOW-{uuid.uuid4().hex[:12]}")
    strategy_name: str = ""
    trade_date: str = field(default_factory=lambda: date.today().isoformat())
    ts_code: str = ""
    side: str = ""                # "buy" | "sell"
    quantity: int = 0
    price: float = 0.0
    amount: float = 0.0
    commission: float = 0.0
    net_amount: float = 0.0
    status: str = "recorded"
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    note: str = ""


def _ensure_dirs() -> None:
    SHADOW_DIR.mkdir(parents=True, exist_ok=True)


def _log_trade(trade: ShadowTrade) -> None:
    _ensure_dirs()
    with open(SHADOW_TRADES, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(asdict(trade), ensure_ascii=False) + "\n")


def _load_trades(strategy_name: str | None = None) -> list[dict[str, Any]]:
    if not SHADOW_TRADES.exists():
        return []
    trades = []
    with open(SHADOW_TRADES, "r", encoding="utf-8") as fh:
        for line in fh:
            try:
                t = json.loads(line)
                if strategy_name is None or t.get("strategy_name") == strategy_name:
                    trades.append(t)
            except json.JSONDecodeError:
                continue
    return trades


def record_shadow(order: dict[str, Any], strategy_name: str) -> dict[str, Any]:
    """Record a shadow order without executing.

    Args:
        order: dict with ts_code, side, quantity, price, commission (optional).
        strategy_name: Name of the strategy that generated this order.

    Returns:
        dict with: trade_id, status, recorded, message.
    """
    ts_code = order.get("ts_code", "")
    side = order.get("side", "")
    quantity = int(order.get("quantity", 0))
    price = float(order.get("price", 0.0))
    commission = float(order.get("commission", 0.0))

    if not ts_code:
        return {"trade_id": "", "status": "rejected", "recorded": False, "message": "Missing ts_code"}
    if side not in ("buy", "sell"):
        return {"trade_id": "", "status": "rejected", "recorded": False, "message": f"Invalid side: {side}"}
    if quantity <= 0:
        return {"trade_id": "", "status": "rejected", "recorded": False, "message": f"Invalid quantity: {quantity}"}

    amount = quantity * price
    # Commission: default 0.025% if not provided, min 5 yuan
    if commission == 0.0:
        commission = max(amount * 0.00025, 5.0)

    # Stamp duty for sells (0.05%)
    stamp_duty = amount * 0.0005 if side == "sell" else 0.0
    total_cost = commission + stamp_duty

    if side == "buy":
        net_amount = amount + total_cost
    else:
        net_amount = amount - total_cost

    trade = ShadowTrade(
        strategy_name=strategy_name,
        ts_code=ts_code,
        side=side,
        quantity=quantity,
        price=price,
        amount=round(amount, 2),
        commission=round(commission, 2),
        net_amount=round(net_amount, 2),
        note=order.get("note", ""),
    )

    _log_trade(trade)

    return {
        "trade_id": trade.trade_id,
        "status": "recorded",
        "recorded": True,
        "message": f"Shadow trade recorded for strategy {strategy_name}: {side} {quantity} {ts_code} @ {price}",
        "net_amount": trade.net_amount,
    }


def get_shadow_pnl(strategy_name: str, date: str | None = None) -> dict[str, Any]:
    """Get shadow P&L for a strategy on a given date.

    Args:
        strategy_name: Strategy to query.
        date: Date string YYYY-MM-DD. If None, uses today.

    Returns:
        dict with: strategy, date, total_trades, buys, sells,
        total_cost, total_proceeds, realized_pnl, positions.
    """
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")

    trades = _load_trades(strategy_name)
    trades = [t for t in trades if t.get("trade_date") == date]

    buys = [t for t in trades if t.get("side") == "buy"]
    sells = [t for t in trades if t.get("side") == "sell"]

    total_cost = sum(t.get("net_amount", 0) for t in buys)
    total_proceeds = sum(t.get("net_amount", 0) for t in sells)

    # Track positions per ts_code
    positions: dict[str, dict[str, Any]] = {}
    for t in trades:
        code = t.get("ts_code", "")
        if code not in positions:
            positions[code] = {"quantity": 0, "cost_basis": 0.0, "trades": 0}
        qty = t.get("quantity", 0)
        net = t.get("net_amount", 0)
        if t.get("side") == "buy":
            positions[code]["quantity"] += qty
            positions[code]["cost_basis"] += net
            positions[code]["trades"] += 1
        else:
            positions[code]["quantity"] -= qty
            positions[code]["cost_basis"] -= net
            positions[code]["trades"] += 1

    # Realized P&L: for sells, compare proceeds to average cost
    avg_costs: dict[str, float] = {}
    total_qty_bought: dict[str, int] = {}
    for t in buys:
        code = t.get("ts_code", "")
        total_qty_bought[code] = total_qty_bought.get(code, 0) + t.get("quantity", 0)
        avg_costs[code] = avg_costs.get(code, 0) + t.get("net_amount", 0)

    for code in avg_costs:
        if total_qty_bought.get(code, 0) > 0:
            avg_costs[code] /= total_qty_bought[code]

    realized_pnl = 0.0
    for t in sells:
        code = t.get("ts_code", "")
        qty = t.get("quantity", 0)
        proceeds = t.get("net_amount", 0)
        cost = avg_costs.get(code, 0) * qty
        realized_pnl += proceeds - cost

    return {
        "strategy": strategy_name,
        "date": date,
        "total_trades": len(trades),
        "buys": len(buys),
        "sells": len(sells),
        "total_cost": round(total_cost, 2),
        "total_proceeds": round(total_proceeds, 2),
        "realized_pnl": round(realized_pnl, 2),
        "positions": {k: {**v, "avg_cost": round(v["cost_basis"] / v["quantity"], 4) if v["quantity"] > 0 else 0.0} for k, v in positions.items()},
    }


def list_strategies() -> list[str]:
    """List all strategy names that have shadow trades."""
    trades = _load_trades()
    return sorted(set(t.get("strategy_name", "") for t in trades if t.get("strategy_name")))


def get_all_shadow_pnl(date: str | None = None) -> dict[str, dict[str, Any]]:
    """Get shadow P&L for all strategies on a given date."""
    strategies = list_strategies()
    return {s: get_shadow_pnl(s, date) for s in strategies}
