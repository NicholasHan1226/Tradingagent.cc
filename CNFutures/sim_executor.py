#!/usr/bin/env python3
"""Simulation-only executor for China futures."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
import math
from typing import Any

from shared.execution.sim_broker import SimResult
from shared.execution.sim_executor_registry import register_sim_executor

from . import MARKET
from .margin_model import estimate_order_cost
from .session import parse_cn_datetime


DEFAULT_SLIPPAGE_BPS = 2.0
DEFAULT_VOLUME_PARTICIPATION = 0.05
VALID_SIDES = {"buy", "sell", "long", "short"}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else default
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (OverflowError, TypeError, ValueError):
        return default


def _extract_symbol(order: dict[str, Any]) -> str:
    symbol = str(
        order.get("symbol") or order.get("ts_code") or order.get("contract") or ""
    ).strip()
    if not symbol:
        raise ValueError("futures symbol is required")
    return symbol


def _extract_price(order: dict[str, Any]) -> float:
    value = order.get("price", order.get("limit_price", order.get("mid_price")))
    if isinstance(value, bool):
        raise ValueError("price must be positive")
    try:
        price = float(value or 0.0)
    except (TypeError, ValueError) as exc:
        raise ValueError("price must be positive") from exc
    if not math.isfinite(price) or price <= 0:
        raise ValueError("price must be positive")
    return price


def _extract_reference_price(order: dict[str, Any]) -> float:
    value = (
        order.get("previous_close")
        if "previous_close" in order
        else order.get("reference_price")
    )
    if isinstance(value, bool):
        raise ValueError("reference price must be positive")
    try:
        price = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("reference price must be positive") from exc
    if not math.isfinite(price) or price <= 0:
        raise ValueError("reference price must be positive")
    return price


def _evidence_timestamp(order: dict[str, Any]) -> str:
    for key in (
        "bar_time",
        "quote_time",
        "quote_timestamp",
        "timestamp",
        "trade_time",
        "time",
    ):
        value = order.get(key)
        raw = str(value or "").strip()
        parsed = parse_cn_datetime(value)
        if ":" in raw and parsed is not None:
            return parsed.isoformat(timespec="seconds")
    return ""


def _reject(
    *,
    order_id: str,
    symbol: str,
    side: str,
    requested_qty: int,
    reason: str,
    message: str,
    source: str,
    details: dict[str, Any] | None = None,
) -> SimResult:
    raw_response = {
        "order_id": order_id,
        "symbol": symbol,
        "side": side,
        "requested_quantity": requested_qty,
        "reason": reason,
        "real_trading_enabled": False,
        "source": source,
    }
    raw_response.update(details or {})
    return SimResult(
        status="rejected",
        filled_qty=0,
        avg_price=0.0,
        fee=0.0,
        message=message,
        capital_layer="simulated",
        account_type="simulated",
        order_id=order_id,
        market=MARKET,
        raw_response=raw_response,
    )


def _round_to_tick(price: float, tick_size: float, *, side: str) -> float:
    ticks = price / tick_size
    if side in {"buy", "long"}:
        rounded = math.ceil(ticks) * tick_size
    else:
        rounded = math.floor(ticks) * tick_size
    decimals = max(
        0, min(8, len(str(tick_size).split(".", 1)[1]) if "." in str(tick_size) else 0)
    )
    return round(max(tick_size, rounded), decimals)


def _limit_bounds(reference_price: float, limit_rate: float) -> tuple[float, float]:
    return (
        round(reference_price * (1.0 - limit_rate), 8),
        round(reference_price * (1.0 + limit_rate), 8),
    )


def _normalize_trade_date(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    first_part = raw[:10]
    digits = "".join(ch for ch in first_part if ch.isdigit())
    if len(digits) >= 8:
        return digits[:8]
    digits = "".join(ch for ch in raw if ch.isdigit())
    return digits[:8] if len(digits) >= 8 else ""


def _parse_trade_date(value: Any) -> datetime | None:
    normalized = _normalize_trade_date(value)
    if len(normalized) != 8:
        return None
    try:
        return datetime.strptime(normalized, "%Y%m%d")
    except ValueError:
        return None


def _expiry_date(order: dict[str, Any]) -> datetime | None:
    for key in ("last_trade_date", "expiry_date", "expiration_date", "delivery_date"):
        parsed = _parse_trade_date(order.get(key))
        if parsed is not None:
            return parsed
    return None


def _book_quote(order: dict[str, Any], side: str) -> tuple[float, int, str]:
    if side in {"buy", "long"}:
        price = _safe_float(
            order.get("ask_price") or order.get("ask1") or order.get("best_ask"), 0.0
        )
        qty = _safe_int(
            order.get("ask_size")
            or order.get("ask_volume")
            or order.get("ask1_volume"),
            0,
        )
        if price > 0 and qty > 0:
            return price, qty, "order_book_ask"
        return 0.0, 0, "signal_price"
    price = _safe_float(
        order.get("bid_price") or order.get("bid1") or order.get("best_bid"), 0.0
    )
    qty = _safe_int(
        order.get("bid_size") or order.get("bid_volume") or order.get("bid1_volume"), 0
    )
    if price > 0 and qty > 0:
        return price, qty, "order_book_bid"
    return 0.0, 0, "signal_price"


def cn_futures_sim_execute(
    order: dict[str, Any],
    account: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
) -> SimResult:
    """Return a local simulated fill and never touch a real CTP account."""

    del account
    config = dict(config or {})
    symbol = _extract_symbol(order)
    side = str(order.get("side") or order.get("direction") or "").lower().strip()
    quantity = order.get("quantity", order.get("qty", 0))
    requested_qty = _safe_int(quantity)
    order_id = str(order.get("order_id") or f"SIM-CNF-{symbol.upper()}")
    if requested_qty <= 0:
        return _reject(
            order_id=order_id,
            symbol=symbol,
            side=side,
            requested_qty=requested_qty,
            reason="non_positive_quantity",
            message="Simulated China futures reject: quantity must be positive",
            source="cn_futures_sim_executor_quantity_guard",
        )
    if side not in VALID_SIDES:
        return _reject(
            order_id=order_id,
            symbol=symbol,
            side=side,
            requested_qty=requested_qty,
            reason="missing_fill_evidence",
            message="Simulated China futures reject: side is not executable",
            source="cn_futures_sim_executor_fill_evidence_guard",
        )
    try:
        requested_price = _extract_price(order)
        reference_price = _extract_reference_price(order)
    except ValueError as exc:
        return _reject(
            order_id=order_id,
            symbol=symbol,
            side=side,
            requested_qty=requested_qty,
            reason="missing_fill_evidence",
            message=f"Simulated China futures reject: {exc}",
            source="cn_futures_sim_executor_fill_evidence_guard",
        )
    initial_cost = estimate_order_cost(
        symbol=symbol, side=side, quantity=1, price=requested_price
    )
    rule = initial_cost.rule
    trade_date = _parse_trade_date(order.get("trade_date") or order.get("date"))
    expiry_date = _expiry_date(order)
    min_days_to_expiry = _safe_int(config.get("rollover_min_days_to_expiry"), 0)
    if trade_date is not None and expiry_date is not None and min_days_to_expiry > 0:
        days_to_expiry = (expiry_date - trade_date).days
        if days_to_expiry <= min_days_to_expiry:
            return _reject(
                order_id=order_id,
                symbol=symbol,
                side=side,
                requested_qty=requested_qty,
                reason="contract_expiry_guard",
                message="Simulated China futures reject: contract inside explicit expiry guard",
                source="cn_futures_sim_executor_expiry_guard",
                details={
                    "requested_price": requested_price,
                    "trade_date": _normalize_trade_date(
                        order.get("trade_date") or order.get("date")
                    ),
                    "expiry_date": expiry_date.strftime("%Y%m%d"),
                    "days_to_expiry": days_to_expiry,
                    "rollover_min_days_to_expiry": min_days_to_expiry,
                },
            )
    limit_down, limit_up = _limit_bounds(reference_price, rule.price_limit_rate)
    if requested_price < limit_down or requested_price > limit_up:
        return _reject(
            order_id=order_id,
            symbol=symbol,
            side=side,
            requested_qty=requested_qty,
            reason="price_limit_guard",
            message="Simulated China futures reject: price outside static daily limit bounds",
            source="cn_futures_sim_executor_static_rules",
            details={
                "requested_price": requested_price,
                "reference_price": reference_price,
                "limit_down": limit_down,
                "limit_up": limit_up,
                "price_limit_rate": rule.price_limit_rate,
            },
        )

    slippage_bps = max(
        0.0, _safe_float(config.get("slippage_bps"), DEFAULT_SLIPPAGE_BPS)
    )
    book_price, book_available_qty, price_source = _book_quote(order, side)
    latest_volume = _safe_float(order.get("bar_volume") or order.get("volume"), 0.0)
    raw_participation = config.get("volume_participation", DEFAULT_VOLUME_PARTICIPATION)
    participation = min(1.0, max(0.0, _safe_float(raw_participation, 0.0)))
    evidence_timestamp = _evidence_timestamp(order)
    book_evidence = book_price > 0 and book_available_qty > 0
    bar_evidence = latest_volume > 0 and participation > 0
    if not evidence_timestamp:
        return _reject(
            order_id=order_id,
            symbol=symbol,
            side=side,
            requested_qty=requested_qty,
            reason="missing_fill_evidence",
            message="Simulated China futures reject: parseable bar or quote timestamp required",
            source="cn_futures_sim_executor_fill_evidence_guard",
        )
    if not (bar_evidence or book_evidence):
        reason = (
            "liquidity_participation_disabled"
            if latest_volume > 0 and participation <= 0
            else "missing_fill_evidence"
        )
        return _reject(
            order_id=order_id,
            symbol=symbol,
            side=side,
            requested_qty=requested_qty,
            reason=reason,
            message="Simulated China futures reject: positive volume participation or same-side book depth required",
            source="cn_futures_sim_executor_fill_evidence_guard",
            details={
                "evidence_timestamp": evidence_timestamp,
                "bar_volume": latest_volume,
                "volume_participation": participation,
                "order_book_available_qty": book_available_qty,
            },
        )
    price_base = book_price if book_price > 0 else requested_price
    slippage_multiplier = 1.0 + (
        slippage_bps / 10000.0 if side in {"buy", "long"} else -slippage_bps / 10000.0
    )
    price = _round_to_tick(price_base * slippage_multiplier, rule.tick_size, side=side)
    price = min(max(price, limit_down), limit_up)
    max_fill_qty = requested_qty
    if bar_evidence:
        max_fill_qty = max(1, int(latest_volume * participation))
    if book_evidence:
        max_fill_qty = min(max_fill_qty, book_available_qty)
    filled_qty = min(requested_qty, max_fill_qty)
    fill_status = "filled" if filled_qty >= requested_qty else "partial"
    cost = estimate_order_cost(symbol=symbol, side=side, quantity=quantity, price=price)
    if filled_qty != cost.quantity:
        cost = estimate_order_cost(
            symbol=symbol, side=side, quantity=filled_qty, price=price
        )
    fee = (
        cost.total_estimated_fee
        if config.get("fee_mode") == "round_trip_estimate"
        else cost.open_fee
    )
    fill_evidence_type = price_source if book_evidence else "bar_volume_participation"

    return SimResult(
        status=fill_status,
        filled_qty=cost.quantity,
        avg_price=cost.price,
        fee=fee,
        message="Simulated China futures fill with static slippage/liquidity rules; real CTP trading disabled",
        capital_layer="simulated",
        account_type="simulated",
        order_id=order_id,
        market=MARKET,
        raw_response={
            "order_id": order_id,
            "symbol": cost.symbol,
            "side": cost.side,
            "quantity": cost.quantity,
            "requested_quantity": requested_qty,
            "price": cost.price,
            "requested_price": requested_price,
            "slippage_bps": slippage_bps,
            "execution_price_source": price_source,
            "fill_evidence_type": fill_evidence_type,
            "evidence_timestamp": evidence_timestamp,
            "order_book_available_qty": book_available_qty,
            "bid_price": _safe_float(
                order.get("bid_price") or order.get("bid1") or order.get("best_bid"),
                0.0,
            ),
            "ask_price": _safe_float(
                order.get("ask_price") or order.get("ask1") or order.get("best_ask"),
                0.0,
            ),
            "bid_size": _safe_int(
                order.get("bid_size")
                or order.get("bid_volume")
                or order.get("bid1_volume"),
                0,
            ),
            "ask_size": _safe_int(
                order.get("ask_size")
                or order.get("ask_volume")
                or order.get("ask1_volume"),
                0,
            ),
            "last_trade_date": _normalize_trade_date(order.get("last_trade_date")),
            "expiry_date": _normalize_trade_date(
                order.get("expiry_date")
                or order.get("expiration_date")
                or order.get("delivery_date")
            ),
            "bar_volume": latest_volume,
            "volume_participation": participation,
            "fill_status": fill_status,
            "notional": cost.notional,
            "margin_required": cost.margin_required,
            "open_fee": cost.open_fee,
            "estimated_close_fee": cost.estimated_close_fee,
            "total_estimated_fee": cost.total_estimated_fee,
            "fee_charged": fee,
            "exchange": cost.rule.exchange,
            "contract_multiplier": cost.rule.contract_multiplier,
            "tick_size": cost.rule.tick_size,
            "price_limit_rate": cost.rule.price_limit_rate,
            "limit_down": limit_down,
            "limit_up": limit_up,
            "margin_rate": cost.rule.margin_rate,
            "night_session": cost.rule.night_session,
            "real_trading_enabled": False,
            "source": "cn_futures_sim_executor_static_rules",
            "rule": asdict(cost.rule),
        },
    )


register_sim_executor(MARKET, cn_futures_sim_execute)


__all__ = ["cn_futures_sim_execute"]
