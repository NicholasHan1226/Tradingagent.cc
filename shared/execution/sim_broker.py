#!/usr/bin/env python3
"""Simulated broker with API-backed dispatch and local slippage fallback.

``execute_sim_order`` dispatches to market-specific simulated-account APIs and
returns a receipt-shaped ``SimResult``. ``simulate_order`` is kept as the legacy
local slippage model for callers that still need backtest-compatible estimates.

Reference: Ashare/sim_executor.py for the A-share paper-broker adapter.
"""

from __future__ import annotations

import json
import math
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
    filled_qty: int | float = 0
    avg_price: float = 0.0
    fee: float = 0.0
    message: str = ""
    capital_layer: str = "simulated"
    account_type: str = "simulated"
    order_id: str = ""
    market: str = ""
    raw_response: dict[str, Any] = field(default_factory=dict)
    broker_contract: str = ""
    authority_id: str = ""
    authority_generation: int | None = None

    def __post_init__(self) -> None:
        self.status = _normalize_sim_status(self.status)
        self.filled_qty = _normalize_filled_quantity(self.filled_qty)
        self.avg_price = _normalize_non_negative_float(self.avg_price, name="avg_price")
        self.fee = _normalize_non_negative_float(self.fee, name="fee")
        if not isinstance(self.raw_response, dict):
            raise ValueError("sim result raw_response must be an object")
        reject_real_execution_payload(
            self.raw_response, context="sim result raw_response"
        )
        if str(self.capital_layer or "").strip().lower() != "simulated":
            raise ValueError("sim result capital_layer must be simulated")
        if str(self.account_type or "").strip().lower() != "simulated":
            raise ValueError("sim result account_type must be simulated")
        if self.authority_generation is not None and (
            isinstance(self.authority_generation, bool)
            or not isinstance(self.authority_generation, int)
            or self.authority_generation <= 0
        ):
            raise ValueError("sim result authority_generation must be positive integer")
        self.capital_layer = "simulated"
        self.account_type = "simulated"
        if self.status in LOCAL_BACKUP_STATUSES:
            if self.filled_qty <= 0 or self.avg_price <= 0:
                raise ValueError(
                    f"{self.status} sim result requires positive filled_qty and avg_price"
                )
        elif self.filled_qty != 0 or self.avg_price != 0 or self.fee != 0:
            raise ValueError(
                f"{self.status} sim result cannot carry fill quantity, price, or fee"
            )


def _normalize_filled_quantity(value: Any) -> int | float:
    """Preserve fractional Crypto quantities while keeping lot markets integral."""

    if isinstance(value, bool):
        raise ValueError("filled_qty must be a non-negative finite number")
    try:
        quantity = float(value or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("filled_qty must be a non-negative finite number") from exc
    if not math.isfinite(quantity) or quantity < 0:
        raise ValueError("filled_qty must be a non-negative finite number")
    rounded = round(quantity)
    return int(rounded) if abs(quantity - rounded) <= 1e-12 else quantity


def _normalize_non_negative_float(value: Any, *, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a non-negative finite number")
    try:
        parsed = float(value or 0.0)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a non-negative finite number") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise ValueError(f"{name} must be a non-negative finite number")
    return parsed


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
    status: str = "filled"  # filled | partial | unfilled
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
        register_sim_executor(
            "ashare",
            ashare_sim_execute,
            simulation_contract="tradingagent.ashare.paper_broker.v1",
            authority_id="ashare-capital-v1",
        )
    elif market_key == "crypto":
        try:
            from Crypto.sim_executor import crypto_sim_execute
        except Exception:
            return
        register_sim_executor(
            "crypto",
            crypto_sim_execute,
            simulation_contract="tradingagent.crypto.paper_broker.v1",
            authority_id="crypto-shadow-sim-v1",
        )
    elif market_key == "cn_futures":
        try:
            from CNFutures.sim_executor import cn_futures_sim_execute
        except Exception:
            return
        register_sim_executor(
            "cn_futures",
            cn_futures_sim_execute,
            simulation_contract="tradingagent.cnfutures.paper_broker.v1",
            authority_id="cn-futures-capital-v1",
        )


def _binding_claim_error(
    payload: dict[str, Any],
    *,
    market_key: str,
    simulation_contract: str,
    authority_id: str,
    authority_generation: int | None,
    account_id: str | None,
    source: str,
) -> str:
    claimed_market = str(payload.get("market") or "").strip().lower()
    if claimed_market and claimed_market != market_key:
        return f"{source}.market={claimed_market} does not match {market_key}"
    claimed_contract = str(payload.get("broker_contract") or "").strip()
    if claimed_contract and claimed_contract != simulation_contract:
        return f"{source}.broker_contract does not match registered binding"
    claimed_authority = str(payload.get("authority_id") or "").strip()
    if claimed_authority and claimed_authority != authority_id:
        return f"{source}.authority_id does not match registered binding"
    if "authority_generation" in payload:
        claimed_generation = payload.get("authority_generation")
        if (
            isinstance(claimed_generation, bool)
            or not isinstance(claimed_generation, int)
            or claimed_generation <= 0
            or (
                authority_generation is not None
                and claimed_generation != authority_generation
            )
        ):
            return f"{source}.authority_generation does not match account binding"
    claimed_accounts = {
        str(payload.get(field) or "").strip()
        for field in ("account_id", "account")
        if str(payload.get(field) or "").strip()
    }
    if len(claimed_accounts) > 1:
        return f"{source}.account identity fields disagree"
    if claimed_accounts and account_id is not None and claimed_accounts != {account_id}:
        return f"{source}.account does not match market lane binding"
    return ""


_GOVERNED_SIM_ACCOUNT_IDS = {
    "ashare": "ashare_sim",
    "cn_futures": "cn_futures_sim",
    "crypto": "crypto_sim",
}


def _governed_account_binding(
    account: dict[str, Any],
    *,
    market_key: str,
    simulation_contract: str,
    authority_id: str,
) -> tuple[str, int] | str | None:
    """Validate the caller-owned market account binding before enrichment.

    The dispatcher may propagate a verified binding to executor inputs, but it
    must never manufacture one for an unbound or cross-market account.
    """

    expected_account = _GOVERNED_SIM_ACCOUNT_IDS.get(market_key)
    if expected_account is None:
        return None
    identities = {
        str(account.get(field) or "").strip()
        for field in ("account_id", "account")
        if str(account.get(field) or "").strip()
    }
    if identities != {expected_account}:
        return f"account identity must be exactly {expected_account}"
    if str(account.get("market") or "").strip().lower() != market_key:
        return "account.market does not match governed market lane"
    if str(account.get("broker_contract") or "").strip() != simulation_contract:
        return "account.broker_contract does not match governed market lane"
    if str(account.get("authority_id") or "").strip() != authority_id:
        return "account.authority_id does not match governed market lane"
    generation = account.get("authority_generation")
    if (
        isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation <= 0
    ):
        return "account.authority_generation must be a positive integer"
    return expected_account, generation


def _normalize_sim_status(status: Any) -> str:
    value = str(status or "").lower().strip()
    aliases = {
        "ok": "filled",
        "dry_run_ok": "pending",
        # A warning is not evidence of a partial fill. Treat it as a non-fill
        # failure unless an adapter emits an explicit, validated partial result.
        "warning": "failed",
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
    candidate_pool_layer = (
        str(
            order.get("candidate_pool_layer")
            or metadata.get("candidate_pool_layer")
            or ""
        )
        .lower()
        .strip()
    )
    execution_source = (
        str(order.get("execution_source") or metadata.get("execution_source") or "")
        .lower()
        .strip()
    )
    sample_intent = (
        str(order.get("sample_intent") or metadata.get("sample_intent") or "")
        .lower()
        .strip()
    )
    valid_candidate = (
        candidate_pool_layer == "candidate"
        and execution_source == "ashare_candidate_layer"
        and sample_intent in {"", "exploitation"}
    )
    valid_exploration = (
        candidate_pool_layer == "exploration"
        and execution_source == "ashare_candidate_layer"
        and sample_intent == "exploration"
    )
    if side == "buy" and not (valid_candidate or valid_exploration):
        return (
            "A-share simulated buy requires candidate_pool_layer=candidate "
            "with sample_intent=exploitation, or candidate_pool_layer=exploration "
            "with sample_intent=exploration; execution_source=ashare_candidate_layer"
        )
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
            broker_contract=result.broker_contract,
            authority_id=result.authority_id,
            authority_generation=result.authority_generation,
        )

    if isinstance(result, dict):
        reject_real_execution_payload(result, context=f"{market}.sim_result")
        return SimResult(
            status=result.get("status", "failed"),
            filled_qty=result.get("filled_qty", result.get("filled_quantity", 0)),
            avg_price=float(
                result.get("avg_price", result.get("filled_price", 0.0)) or 0.0
            ),
            fee=float(result.get("fee", 0.0) or 0.0),
            message=str(result.get("message", "")),
            order_id=str(result.get("order_id", order.get("order_id", ""))),
            market=str(result.get("market", market)),
            raw_response=dict(result),
            broker_contract=str(result.get("broker_contract", "")),
            authority_id=str(result.get("authority_id", "")),
            authority_generation=result.get("authority_generation"),
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
    from .sim_executor_registry import (
        get_sim_executor,
        get_sim_executor_binding,
    )

    market_key = str(market or "").lower().strip()
    order_payload = _coerce_payload_mapping(order, scalar_key="order")
    account_payload = _coerce_payload_mapping(account, scalar_key="account")
    config_payload = _coerce_payload_mapping(config, scalar_key="config")
    try:
        reject_real_execution_payload(
            order_payload, context=f"execute_sim_order.{market_key or 'unknown'}.order"
        )
        reject_real_execution_payload(
            account_payload,
            context=f"execute_sim_order.{market_key or 'unknown'}.account",
        )
        reject_real_execution_payload(
            config_payload,
            context=f"execute_sim_order.{market_key or 'unknown'}.config",
        )
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
    if executor is None:
        _ensure_builtin_executor(market_key)
        executor = get_sim_executor(market_key)
    if executor is None:
        return SimResult(
            status="failed",
            message=f"No simulated executor available for market={market_key or 'unknown'}",
            order_id=str(order_payload.get("order_id", "")),
            market=market_key,
        )

    binding = get_sim_executor_binding(market_key)
    if binding is None:
        return SimResult(
            status="failed",
            message=f"Missing simulated executor binding for market={market_key}",
            order_id=str(order_payload.get("order_id", "")),
            market=market_key,
        )
    account_binding = _governed_account_binding(
        account_payload,
        market_key=market_key,
        simulation_contract=binding.simulation_contract,
        authority_id=binding.authority_id,
    )
    if isinstance(account_binding, str):
        return SimResult(
            status="failed",
            message=f"Simulated account binding invalid: {account_binding}",
            order_id=str(order_payload.get("order_id", "")),
            market=market_key,
            raw_response={"recorded": False, "reason": "sim_account_binding_invalid"},
        )
    bound_account_id: str | None = None
    bound_generation: int | None = None
    if isinstance(account_binding, tuple):
        bound_account_id, bound_generation = account_binding
        if "authority_generation" not in order_payload:
            return SimResult(
                status="failed",
                message="Simulated input binding mismatch: order.authority_generation is required",
                order_id=str(order_payload.get("order_id", "")),
                market=market_key,
                raw_response={
                    "recorded": False,
                    "reason": "sim_input_binding_mismatch",
                },
            )
    for source, payload in (
        ("order", order_payload),
        ("account", account_payload),
        ("config", config_payload),
    ):
        mismatch = _binding_claim_error(
            payload,
            market_key=market_key,
            simulation_contract=binding.simulation_contract,
            authority_id=binding.authority_id,
            authority_generation=bound_generation,
            account_id=bound_account_id,
            source=source,
        )
        if mismatch:
            return SimResult(
                status="failed",
                message=f"Simulated input binding mismatch: {mismatch}",
                order_id=str(order_payload.get("order_id", "")),
                market=market_key,
                raw_response={
                    "recorded": False,
                    "reason": "sim_input_binding_mismatch",
                },
            )
    for payload in (sim_order, sim_account, sim_config):
        payload["market"] = market_key
        payload["broker_contract"] = binding.simulation_contract
        payload["authority_id"] = binding.authority_id
        if bound_generation is not None:
            payload["authority_generation"] = bound_generation

    try:
        result = executor(sim_order, sim_account, sim_config)
    except Exception as exc:  # pragma: no cover - defensive receipt shaping
        return SimResult(
            status="failed",
            message=f"Sim executor failed for market={market_key or 'unknown'}: {exc}",
            order_id=str(order_payload.get("order_id", "")),
            market=market_key,
        )

    try:
        sim_result = _coerce_sim_result(result, sim_order, market_key)
    except (TypeError, ValueError, RuntimeError) as exc:
        return SimResult(
            status="failed",
            message=f"Invalid simulated receipt for market={market_key}: {exc}",
            order_id=str(order_payload.get("order_id", "")),
            market=market_key,
        )
    if (
        sim_result.market != market_key
        or sim_result.broker_contract != binding.simulation_contract
        or sim_result.authority_id != binding.authority_id
        or (
            sim_result.authority_generation is not None
            and sim_result.authority_generation != bound_generation
        )
    ):
        return SimResult(
            status="failed",
            message=(
                "Simulated receipt binding mismatch: "
                f"expected market={market_key}, contract={binding.simulation_contract}, "
                f"authority={binding.authority_id}"
            ),
            order_id=str(order_payload.get("order_id", "")),
            market=market_key,
            raw_response={"recorded": False, "reason": "sim_receipt_binding_mismatch"},
        )
    sim_result.authority_generation = bound_generation
    if market_key == "ashare" and sim_result.status in LOCAL_BACKUP_STATUSES:
        try:
            from .local_sim_ledger import record_local_sim_order

            backup = record_local_sim_order(
                sim_order, market_key, sim_account, sim_config, sim_result
            )
        except (
            Exception
        ) as exc:  # pragma: no cover - backup must not mask executor result
            backup = {
                "status": "failed",
                "recorded": False,
                "error": f"{exc.__class__.__name__}: {exc}",
            }
        sim_result.raw_response = {
            **dict(sim_result.raw_response or {}),
            "local_sim_backup": backup,
        }
        if backup.get("status") in {"rejected", "failed"} or not bool(
            backup.get("recorded")
        ):
            reason = str(
                backup.get("reason")
                or backup.get("error")
                or "local simulated ledger rejected fill"
            )
            if reason == "insufficient_cash":
                message = (
                    "A-share server-local simulated fill rejected by ledger: "
                    f"insufficient cash ({backup.get('cash_available')} available, "
                    f"{backup.get('required_cash')} required)"
                )
            else:
                message = (
                    f"A-share server-local simulated fill rejected by ledger: {reason}"
                )
            return SimResult(
                status=str(backup.get("status") or "failed"),
                filled_qty=0,
                avg_price=0.0,
                fee=0.0,
                message=message,
                order_id=sim_result.order_id or str(order_payload.get("order_id", "")),
                market=market_key,
                raw_response={
                    **dict(sim_result.raw_response or {}),
                    "pre_ledger_result": {
                        "status": sim_result.status,
                        "filled_qty": sim_result.filled_qty,
                        "avg_price": sim_result.avg_price,
                    },
                },
                broker_contract=binding.simulation_contract,
                authority_id=binding.authority_id,
                authority_generation=bound_generation,
            )
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
    filled_price = (
        est.estimated_fill_price if est.estimated_fill_price is not None else mid_price
    )

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
        return {
            "total_trades": 0,
            "filled_trades": 0,
            "avg_slippage": 0.0,
            "by_strategy": {},
        }

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
    avg_slippage = (
        sum(t.get("slippage_pct", 0) for t in filled) / len(filled) if filled else 0.0
    )

    by_strategy: dict[str, dict[str, Any]] = {}
    for t in filled:
        strat = t.get("details", {}).get("strategy_name", "unknown")
        if strat not in by_strategy:
            by_strategy[strat] = {"trades": 0, "total_slippage": 0.0}
        by_strategy[strat]["trades"] += 1
        by_strategy[strat]["total_slippage"] += t.get("slippage_pct", 0)

    for strat in by_strategy:
        n = by_strategy[strat]["trades"]
        by_strategy[strat]["avg_slippage"] = (
            round(by_strategy[strat]["total_slippage"] / n, 4) if n else 0.0
        )

    return {
        "total_trades": len(trades),
        "filled_trades": len(filled),
        "avg_slippage": round(avg_slippage, 4),
        "by_strategy": by_strategy,
    }
