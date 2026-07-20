#!/usr/bin/env python3
"""Simulation-only executor for China futures."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
import math
from typing import Any

from shared.execution.sim_broker import SimResult
from shared.execution.sim_executor_registry import register_sim_executor
from shared.markets.safety import reject_real_execution_payload

from . import MARKET
from .margin_model import estimate_order_cost
from .session import active_trade_date, parse_cn_datetime


DEFAULT_SLIPPAGE_BPS = 2.0
DEFAULT_VOLUME_PARTICIPATION = 0.05
DEFAULT_MAX_FILL_EVIDENCE_AGE_SECONDS = 600.0
VALID_SIDES = {"buy", "sell", "long", "short"}
VALID_POSITION_EFFECTS = {"open", "close", "close_today", "close_yesterday"}
PAPER_BROKER_CONTRACT = "tradingagent.cnfutures.paper_broker.v1"
SIM_AUTHORITY_ID = "cn-futures-capital-v1"


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else default
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = float(value)
    except (OverflowError, TypeError, ValueError):
        return default
    if not math.isfinite(parsed) or not parsed.is_integer():
        return default
    return int(parsed)


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


def _evidence_datetime(
    order: dict[str, Any], *, prefer_quote: bool = False
) -> datetime | None:
    keys = (
        (
            "quote_time",
            "quote_timestamp",
            "bar_time",
            "timestamp",
            "trade_time",
            "time",
        )
        if prefer_quote
        else (
            "bar_time",
            "timestamp",
            "trade_time",
            "time",
            "quote_time",
            "quote_timestamp",
        )
    )
    for key in keys:
        value = order.get(key)
        raw = str(value or "").strip()
        parsed = parse_cn_datetime(value)
        if ":" in raw and parsed is not None:
            return parsed
    return None


def _aware_decision_time(value: Any) -> datetime | None:
    """Parse an explicit decision timestamp without inventing a timezone."""

    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parse_cn_datetime(parsed)


def _max_fill_evidence_age_seconds(config: dict[str, Any]) -> float | None:
    if "max_fill_evidence_age_seconds" not in config:
        return DEFAULT_MAX_FILL_EVIDENCE_AGE_SECONDS
    raw = config.get("max_fill_evidence_age_seconds")
    if isinstance(raw, bool) or raw is None:
        return None
    try:
        parsed = float(raw)
    except (OverflowError, TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or parsed <= 0:
        return None
    return parsed


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
        "broker_contract": PAPER_BROKER_CONTRACT,
        "authority_id": SIM_AUTHORITY_ID,
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
        broker_contract=PAPER_BROKER_CONTRACT,
        authority_id=SIM_AUTHORITY_ID,
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


def _close_position_snapshot_error(
    *,
    account: dict[str, Any] | None,
    symbol: str,
    side: str,
    position_effect: str,
    requested_qty: int,
) -> tuple[str, dict[str, Any]] | None:
    """Validate a close against an authority-bound long/short position snapshot."""

    if position_effect == "open":
        return None
    snapshot = account.get("position_snapshot") if isinstance(account, dict) else None
    if not isinstance(snapshot, dict):
        return "position_snapshot_required", {}
    snapshot_id = str(snapshot.get("snapshot_id") or "").strip()
    as_of = str(snapshot.get("as_of") or "").strip()
    if not snapshot_id or not as_of:
        return "position_snapshot_identity_missing", {}
    if str(snapshot.get("authority_id") or "").strip() != SIM_AUTHORITY_ID:
        return "position_snapshot_authority_mismatch", {"snapshot_id": snapshot_id}
    if (
        str(snapshot.get("broker_contract") or "").strip()
        != PAPER_BROKER_CONTRACT
    ):
        return "position_snapshot_contract_mismatch", {"snapshot_id": snapshot_id}
    positions = snapshot.get("positions")
    if not isinstance(positions, list):
        return "position_snapshot_positions_invalid", {"snapshot_id": snapshot_id}
    target_side = "long" if side in {"sell", "short"} else "short"
    matches = [
        row
        for row in positions
        if isinstance(row, dict)
        and str(row.get("symbol") or "").strip().upper() == symbol.upper()
        and str(row.get("position_side") or row.get("side") or "")
        .strip()
        .lower()
        == target_side
    ]
    if len(matches) != 1:
        return "position_snapshot_match_not_unique", {
            "snapshot_id": snapshot_id,
            "target_position_side": target_side,
            "match_count": len(matches),
        }
    position = matches[0]
    today_raw = position.get("today_qty")
    yesterday_raw = position.get("yesterday_qty")
    total_raw = position.get("total_qty")
    today_qty = _safe_int(today_raw, -1) if today_raw is not None else -1
    yesterday_qty = (
        _safe_int(yesterday_raw, -1) if yesterday_raw is not None else -1
    )
    total_qty = _safe_int(total_raw, -1) if total_raw is not None else -1
    if position_effect == "close_today":
        available = today_qty
        bucket = "today"
    elif position_effect == "close_yesterday":
        available = yesterday_qty
        bucket = "yesterday"
    else:
        available = (
            today_qty + yesterday_qty
            if today_qty >= 0 and yesterday_qty >= 0
            else total_qty
        )
        bucket = "total"
    if available < 0:
        return "position_snapshot_quantity_invalid", {
            "snapshot_id": snapshot_id,
            "position_bucket": bucket,
        }
    if requested_qty > available:
        return "insufficient_close_position", {
            "snapshot_id": snapshot_id,
            "position_bucket": bucket,
            "available_quantity": available,
            "requested_quantity": requested_qty,
            "target_position_side": target_side,
        }
    return None


def cn_futures_sim_execute(
    order: dict[str, Any],
    account: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
) -> SimResult:
    """Return a local simulated fill and never touch a real CTP account."""

    reject_real_execution_payload(order, context="cn_futures_sim_execute.order")
    reject_real_execution_payload(
        account or {}, context="cn_futures_sim_execute.account"
    )
    reject_real_execution_payload(config or {}, context="cn_futures_sim_execute.config")
    config = dict(config or {})
    symbol = _extract_symbol(order)
    side = str(order.get("side") or order.get("direction") or "").lower().strip()
    position_effect = str(
        order.get("position_effect") or order.get("offset") or ""
    ).lower().strip()
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
    if position_effect not in VALID_POSITION_EFFECTS:
        return _reject(
            order_id=order_id,
            symbol=symbol,
            side=side,
            requested_qty=requested_qty,
            reason="position_effect_required",
            message=(
                "Simulated China futures reject: position_effect must explicitly "
                "declare open/close/close_today/close_yesterday"
            ),
            source="cn_futures_sim_executor_position_effect_guard",
        )
    close_snapshot_error = _close_position_snapshot_error(
        account=account,
        symbol=symbol,
        side=side,
        position_effect=position_effect,
        requested_qty=requested_qty,
    )
    if close_snapshot_error is not None:
        reason, details = close_snapshot_error
        return _reject(
            order_id=order_id,
            symbol=symbol,
            side=side,
            requested_qty=requested_qty,
            reason=reason,
            message=(
                "Simulated China futures reject: an authority-bound position "
                "snapshot does not support the requested close"
            ),
            source="cn_futures_sim_executor_position_snapshot_guard",
            details={"position_effect": position_effect, **details},
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
    book_evidence = book_price > 0 and book_available_qty > 0
    bar_evidence = latest_volume > 0 and participation > 0
    evidence_dt = _evidence_datetime(order, prefer_quote=book_evidence)
    evidence_timestamp = evidence_dt.isoformat() if evidence_dt is not None else ""
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
    decision_dt = _aware_decision_time(
        order.get("decision_time")
        if "decision_time" in order
        else config.get("decision_time")
    )
    if decision_dt is None:
        return _reject(
            order_id=order_id,
            symbol=symbol,
            side=side,
            requested_qty=requested_qty,
            reason="decision_time_required",
            message=(
                "Simulated China futures reject: an explicit timezone-aware "
                "decision_time is required"
            ),
            source="cn_futures_sim_executor_evidence_time_guard",
            details={"evidence_timestamp": evidence_timestamp},
        )
    trade_date_raw = (
        order.get("trade_date")
        if "trade_date" in order
        else order.get("date", config.get("trade_date"))
    )
    parsed_trade_date = _parse_trade_date(trade_date_raw)
    if parsed_trade_date is None:
        return _reject(
            order_id=order_id,
            symbol=symbol,
            side=side,
            requested_qty=requested_qty,
            reason="trade_date_required",
            message=(
                "Simulated China futures reject: a valid YYYYMMDD trade_date "
                "is required"
            ),
            source="cn_futures_sim_executor_evidence_time_guard",
            details={
                "evidence_timestamp": evidence_timestamp,
                "decision_time": decision_dt.isoformat(timespec="seconds"),
            },
        )
    normalized_trade_date = parsed_trade_date.strftime("%Y%m%d")
    expected_trade_date = active_trade_date(decision_dt)
    evidence_trade_date = active_trade_date(evidence_dt)
    if (
        normalized_trade_date != expected_trade_date
        or normalized_trade_date != evidence_trade_date
    ):
        return _reject(
            order_id=order_id,
            symbol=symbol,
            side=side,
            requested_qty=requested_qty,
            reason="trade_date_mismatch",
            message=(
                "Simulated China futures reject: trade_date does not match the "
                "decision timestamp's exchange trade date"
            ),
            source="cn_futures_sim_executor_evidence_time_guard",
            details={
                "evidence_timestamp": evidence_timestamp,
                "decision_time": decision_dt.isoformat(timespec="seconds"),
                "trade_date": normalized_trade_date,
                "expected_trade_date": expected_trade_date,
                "evidence_trade_date": evidence_trade_date,
            },
        )
    max_evidence_age_seconds = _max_fill_evidence_age_seconds(config)
    if max_evidence_age_seconds is None:
        return _reject(
            order_id=order_id,
            symbol=symbol,
            side=side,
            requested_qty=requested_qty,
            reason="fill_evidence_max_age_invalid",
            message=(
                "Simulated China futures reject: max fill-evidence age must be "
                "a positive finite number of seconds"
            ),
            source="cn_futures_sim_executor_evidence_time_guard",
            details={
                "evidence_timestamp": evidence_timestamp,
                "decision_time": decision_dt.isoformat(timespec="seconds"),
                "trade_date": normalized_trade_date,
            },
        )
    evidence_age_seconds = (decision_dt - evidence_dt).total_seconds()
    if evidence_age_seconds < 0:
        return _reject(
            order_id=order_id,
            symbol=symbol,
            side=side,
            requested_qty=requested_qty,
            reason="future_fill_evidence",
            message=(
                "Simulated China futures reject: fill evidence is later than "
                "decision_time"
            ),
            source="cn_futures_sim_executor_evidence_time_guard",
            details={
                "evidence_timestamp": evidence_timestamp,
                "decision_time": decision_dt.isoformat(timespec="seconds"),
                "trade_date": normalized_trade_date,
                "fill_evidence_age_seconds": evidence_age_seconds,
                "max_fill_evidence_age_seconds": max_evidence_age_seconds,
            },
        )
    if evidence_age_seconds > max_evidence_age_seconds:
        return _reject(
            order_id=order_id,
            symbol=symbol,
            side=side,
            requested_qty=requested_qty,
            reason="stale_fill_evidence",
            message=(
                "Simulated China futures reject: fill evidence exceeds the "
                "maximum allowed age"
            ),
            source="cn_futures_sim_executor_evidence_time_guard",
            details={
                "evidence_timestamp": evidence_timestamp,
                "decision_time": decision_dt.isoformat(timespec="seconds"),
                "trade_date": normalized_trade_date,
                "fill_evidence_age_seconds": evidence_age_seconds,
                "max_fill_evidence_age_seconds": max_evidence_age_seconds,
            },
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
    if config.get("fee_mode") == "round_trip_estimate":
        fee = cost.total_estimated_fee
    elif position_effect == "open":
        fee = cost.open_fee
    else:
        fee = cost.estimated_close_fee
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
            "position_effect": position_effect,
            "quantity": cost.quantity,
            "requested_quantity": requested_qty,
            "price": cost.price,
            "requested_price": requested_price,
            "slippage_bps": slippage_bps,
            "execution_price_source": price_source,
            "fill_evidence_type": fill_evidence_type,
            "evidence_timestamp": evidence_timestamp,
            "decision_time": decision_dt.isoformat(timespec="seconds"),
            "trade_date": normalized_trade_date,
            "fill_evidence_age_seconds": evidence_age_seconds,
            "max_fill_evidence_age_seconds": max_evidence_age_seconds,
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
            "broker_contract": PAPER_BROKER_CONTRACT,
            "authority_id": SIM_AUTHORITY_ID,
            "rule": asdict(cost.rule),
        },
        broker_contract=PAPER_BROKER_CONTRACT,
        authority_id=SIM_AUTHORITY_ID,
    )


register_sim_executor(
    MARKET,
    cn_futures_sim_execute,
    simulation_contract=PAPER_BROKER_CONTRACT,
    authority_id=SIM_AUTHORITY_ID,
)


__all__ = ["cn_futures_sim_execute"]
