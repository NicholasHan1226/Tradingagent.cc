#!/usr/bin/env python3
"""Simulated broker with slippage modeling.

Simulates order execution using the slippage model. Used for strategy
validation before shadow/real deployment.

Reference: Ashare/tools/a_share_simulated_trade_executor.py
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from .slippage_model import estimate_slippage
except ImportError:
    from slippage_model import estimate_slippage

SIM_LEDGER = Path(__file__).resolve().parent.parent / "logs" / "sim_orders.jsonl"


@dataclass
class SimFill:
    """Simulated fill result."""

    order_id: str
    ts_code: str
    side: str
    quantity: int
    order_type: str
    requested_price: float | None
    filled_price: float
    slippage_pct: float
    fill_probability: float
    fill_time: str
    status: str = "filled"           # filled | partial | unfilled
    filled_quantity: int = 0
    model: str = ""
    details: dict[str, Any] = field(default_factory=dict)


def _log_sim_fill(fill: SimFill) -> None:
    SIM_LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with open(SIM_LEDGER, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(asdict(fill), ensure_ascii=False) + "\n")


def simulate_order(order: dict[str, Any]) -> dict[str, Any]:
    """Simulate order execution with slippage modeling.

    Args:
        order: dict with keys:
            - ts_code (str): stock code
            - side (str): "buy" | "sell"
            - quantity (int): shares
            - order_type (str): "market" | "limit"
            - mid_price (float): current mid price
            - avg_volume (int): average daily volume in shares
            - limit_price (float, optional): limit price
            - strategy_name (str, optional)

    Returns:
        dict with: filled_price, slippage, fill_time, status, filled_quantity,
        fill_probability, order_id, details.
    """
    order_id = order.get("order_id", f"SIM-{uuid.uuid4().hex[:12]}")
    ts_code = order.get("ts_code", "")
    side = order.get("side", "buy")
    quantity = int(order.get("quantity", 0))
    order_type = order.get("order_type", "market")
    mid_price = order.get("mid_price")
    avg_volume = int(order.get("avg_volume", 1_000_000))
    limit_price = order.get("limit_price")
    strategy_name = order.get("strategy_name", "")

    if mid_price is None and limit_price is not None:
        mid_price = limit_price
    if mid_price is None:
        return {
            "order_id": order_id,
            "filled_price": 0.0,
            "slippage": 0.0,
            "fill_time": datetime.now().isoformat(),
            "status": "rejected",
            "filled_quantity": 0,
            "fill_probability": 0.0,
            "message": "Missing mid_price and limit_price",
        }

    # Calculate limit distance from mid for limit orders
    limit_distance_bps = None
    if order_type.lower() == "limit" and limit_price is not None:
        limit_distance_bps = ((limit_price - mid_price) / mid_price) * 10000

    # Estimate slippage
    est = estimate_slippage(
        order_type=order_type,
        volume=quantity,
        avg_volume=avg_volume,
        mid_price=mid_price,
        limit_distance_bps=limit_distance_bps,
    )

    # Determine fill status
    filled_price = est.estimated_fill_price if est.estimated_fill_price is not None else mid_price

    # For sell orders, slippage reduces the fill price
    if side.lower() == "sell":
        filled_price = mid_price * (1 - est.slippage_pct / 100.0)

    # Fill probability check for limit orders
    import random
    if order_type.lower() == "limit":
        if random.random() > est.fill_probability:
            status = "unfilled"
            filled_quantity = 0
            filled_price = 0.0
        else:
            status = "filled"
            filled_quantity = quantity
    else:
        # Market orders always fill (at slippage-adjusted price)
        status = "filled"
        filled_quantity = quantity

    fill_time = datetime.now().isoformat()

    fill = SimFill(
        order_id=order_id,
        ts_code=ts_code,
        side=side,
        quantity=quantity,
        order_type=order_type,
        requested_price=limit_price,
        filled_price=round(filled_price, 4),
        slippage_pct=est.slippage_pct,
        fill_probability=est.fill_probability,
        fill_time=fill_time,
        status=status,
        filled_quantity=filled_quantity,
        model=est.model,
        details={
            **est.details,
            "strategy_name": strategy_name,
            "mid_price": mid_price,
            "avg_volume": avg_volume,
        },
    )

    _log_sim_fill(fill)

    return {
        "order_id": order_id,
        "filled_price": fill.filled_price,
        "slippage": fill.slippage_pct,
        "fill_time": fill_time,
        "status": status,
        "filled_quantity": filled_quantity,
        "fill_probability": est.fill_probability,
        "model": est.model,
        "details": fill.details,
    }


def get_sim_pnl(date: str | None = None) -> dict[str, Any]:
    """Get simulated P&L for a given date (or all dates if None).

    Args:
        date: Date string YYYY-MM-DD, or None for all.

    Returns:
        dict with: total_trades, filled_trades, avg_slippage, by_strategy.
    """
    if not SIM_LEDGER.exists():
        return {"total_trades": 0, "filled_trades": 0, "avg_slippage": 0.0, "by_strategy": {}}

    trades = []
    with open(SIM_LEDGER, "r", encoding="utf-8") as fh:
        for line in fh:
            try:
                trades.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    if date:
        trades = [t for t in trades if t.get("fill_time", "").startswith(date)]

    filled = [t for t in trades if t.get("status") == "filled"]
    avg_slippage = sum(t.get("slippage_pct", 0) for t in filled) / len(filled) if filled else 0.0

    by_strategy: dict[str, dict[str, Any]] = {}
    for t in filled:
        strat = t.get("details", {}).get("strategy_name", "unknown")
        if strat not in by_strategy:
            by_strategy[strat] = {"trades": 0, "total_slippage": 0.0}
        by_strategy[strat]["trades"] += 1
        by_strategy[strat]["total_slippage"] += t.get("slippage_pct", 0)

    for strat in by_strategy:
        n = by_strategy[strat]["trades"]
        by_strategy[strat]["avg_slippage"] = round(by_strategy[strat]["total_slippage"] / n, 4) if n else 0.0

    return {
        "total_trades": len(trades),
        "filled_trades": len(filled),
        "avg_slippage": round(avg_slippage, 4),
        "by_strategy": by_strategy,
    }
