#!/usr/bin/env python3
"""Simulated broker with API-backed dispatch and local slippage fallback.

``execute_sim_order`` dispatches to market-specific simulated-account APIs and
returns a receipt-shaped ``SimResult``. ``simulate_order`` is kept as the legacy
local slippage model for callers that still need backtest-compatible estimates.

Reference: Ashare/sim_executor.py for the A-share Mini bridge path
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from .slippage_model import estimate_slippage
from shared.markets.safety import reject_real_execution_payload

SIM_LEDGER = Path(__file__).resolve().parent.parent / "logs" / "sim_orders.jsonl"
SIM_STATUSES = {"filled", "partial", "rejected", "failed", "pending"}
LOCAL_BACKUP_STATUSES = {"filled", "partial"}


@dataclass
class SimResult:
    """Simulated-account execution receipt returned by market executors."""

    status: str
    filled_qty: int = 0
    avg_price: float = 0.0
    fee: float = 0.0
    message: str = ""
    capital_layer: str = "simulated"
    account_type: str = "simulated"
    order_id: str = ""
    market: str = ""
    raw_response: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.status = _normalize_sim_status(self.status)
        self.filled_qty = int(self.filled_qty or 0)
        self.avg_price = float(self.avg_price or 0.0)
        self.fee = float(self.fee or 0.0)
        self.capital_layer = "simulated"
        self.account_type = "simulated"


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


def _ensure_builtin_executor(market_key: str) -> None:
    """Import and register built-in executors so callers do not depend on import order."""
    try:
        from .sim_executor_registry import register_sim_executor
    except Exception:
        return
    if market_key == "ashare":
        try:
            from Ashare.sim_executor import ashare_sim_execute
        except Exception:
            return
        register_sim_executor("ashare", ashare_sim_execute)
    elif market_key == "crypto":
        try:
            from Crypto.sim_executor import crypto_sim_execute
        except Exception:
            return
        register_sim_executor("crypto", crypto_sim_execute)


def _normalize_sim_status(status: Any) -> str:
    value = str(status or "").lower().strip()
    aliases = {
        "ok": "filled",
        "dry_run_ok": "filled",
        "warning": "partial",
        "unfilled": "pending",
        "error": "failed",
    }
    value = aliases.get(value, value)
    return value if value in SIM_STATUSES else "failed"


def _with_sim_markers(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    marked = dict(value)
    marked["capital_layer"] = "simulated"
    marked["account_type"] = "simulated"
    return marked


def _ashare_provenance_error(order: dict[str, Any]) -> str:
    side = str(order.get("side") or order.get("direction") or "buy").lower().strip()
    metadata = order.get("metadata") if isinstance(order.get("metadata"), dict) else {}
    candidate_pool_layer = str(order.get("candidate_pool_layer") or metadata.get("candidate_pool_layer") or "").lower().strip()
    execution_source = str(order.get("execution_source") or metadata.get("execution_source") or "").lower().strip()
    if side == "buy" and not (candidate_pool_layer == "candidate" and execution_source == "ashare_candidate_layer"):
        return "A-share simulated buy requires candidate_pool_layer=candidate and execution_source=ashare_candidate_layer"
    if side == "sell" and execution_source != "ashare_rebalance_sell":
        return "A-share simulated sell requires execution_source=ashare_rebalance_sell"
    return ""


def _coerce_payload_mapping(value: Any, *, scalar_key: str = "value") -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if value in (None, ""):
        return {}
    return {scalar_key: value}


def _coerce_sim_result(result: Any, order: dict[str, Any], market: str) -> SimResult:
    if isinstance(result, SimResult):
        raw_response = result.raw_response
        return SimResult(
            status=result.status,
            filled_qty=result.filled_qty,
            avg_price=result.avg_price,
            fee=result.fee,
            message=result.message,
            order_id=result.order_id or str(order.get("order_id", "")),
            market=result.market or market,
            raw_response=raw_response,
        )

    if isinstance(result, dict):
        return SimResult(
            status=result.get("status", "failed"),
            filled_qty=int(result.get("filled_qty", result.get("filled_quantity", 0)) or 0),
            avg_price=float(result.get("avg_price", result.get("filled_price", 0.0)) or 0.0),
            fee=float(result.get("fee", 0.0) or 0.0),
            message=str(result.get("message", "")),
            order_id=str(result.get("order_id", order.get("order_id", ""))),
            market=str(result.get("market", market)),
            raw_response=dict(result),
        )

    return SimResult(
        status="failed",
        message=f"Invalid sim executor result type: {type(result).__name__}",
        order_id=str(order.get("order_id", "")),
        market=market,
    )


def execute_sim_order(
    order: dict[str, Any],
    market: str,
    account: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
) -> SimResult:
    """Execute a simulated-account order via the registered market executor.

    Market executors must accept ``execute(order, account, config)`` and return
    ``SimResult``. The returned receipt is always marked as
    ``capital_layer=simulated`` and ``account_type=simulated``.
    """
    from .sim_executor_registry import get_sim_executor, local_sim_executor

    market_key = str(market or "").lower().strip()
    order_payload = _coerce_payload_mapping(order, scalar_key="order")
    account_payload = _coerce_payload_mapping(account, scalar_key="account")
    config_payload = _coerce_payload_mapping(config, scalar_key="config")
    try:
        reject_real_execution_payload(order_payload, context=f"execute_sim_order.{market_key or 'unknown'}.order")
        reject_real_execution_payload(account_payload, context=f"execute_sim_order.{market_key or 'unknown'}.account")
        reject_real_execution_payload(config_payload, context=f"execute_sim_order.{market_key or 'unknown'}.config")
    except Exception as exc:
        return SimResult(
            status="failed",
            message=str(exc),
            order_id=str(order_payload.get("order_id", "")),
            market=market_key,
        )
    sim_order = _with_sim_markers(order_payload)
    sim_account = _with_sim_markers(account_payload)
    sim_config = _with_sim_markers(config_payload)
    if market_key == "ashare":
        provenance_error = _ashare_provenance_error(sim_order)
        if provenance_error:
            return SimResult(
                status="failed",
                message=provenance_error,
                order_id=str(sim_order.get("order_id", "")),
                market=market_key,
                raw_response={"recorded": False, "reason": provenance_error},
            )
    executor = get_sim_executor(market_key)
    if executor is None or (executor is local_sim_executor and market_key in {"ashare", "crypto"}):
        _ensure_builtin_executor(market_key)
        executor = get_sim_executor(market_key)
    if executor is None:
        return SimResult(
            status="failed",
            message=f"No simulated executor available for market={market_key or 'unknown'}",
            order_id=str(order_payload.get("order_id", "")),
            market=market_key,
        )

    try:
        result = executor(sim_order, sim_account, sim_config)
    except Exception as exc:  # pragma: no cover - defensive receipt shaping
        return SimResult(
            status="failed",
            message=f"Sim executor failed for market={market_key or 'unknown'}: {exc}",
            order_id=str(order_payload.get("order_id", "")),
            market=market_key,
        )

    sim_result = _coerce_sim_result(result, sim_order, market_key)
    if (
        market_key == "ashare"
        and sim_result.status in LOCAL_BACKUP_STATUSES
        and os.environ.get("TRADINGS_LOCAL_SIM_BACKUP_ENABLED", "1") != "0"
    ):
        try:
            from .local_sim_ledger import record_local_sim_order

            backup = record_local_sim_order(sim_order, market_key, sim_account, sim_config, sim_result)
        except Exception as exc:  # pragma: no cover - backup must not block Hermes dispatch
            backup = {"status": "failed", "recorded": False, "error": f"{exc.__class__.__name__}: {exc}"}
        sim_result.raw_response = {**dict(sim_result.raw_response or {}), "local_sim_backup": backup}
    return sim_result


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
