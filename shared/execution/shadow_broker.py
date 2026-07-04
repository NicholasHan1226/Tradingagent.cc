#!/usr/bin/env python3
"""Shadow broker ledger.

This module records shadow-only orders for review and strategy graduation.
It never talks to a live broker and rejects real/live/direct execution payloads.
"""

from __future__ import annotations

import errno
import fcntl
import json
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from shared.markets.safety import reject_real_execution_payload


TRADINGAGENT_ROOT = Path(__file__).resolve().parents[2]
SHADOW_DIR = TRADINGAGENT_ROOT / "shared" / "logs" / "shadow"
SHADOW_TRADES = SHADOW_DIR / "shadow_trades.jsonl"
SHADOW_POSITIONS = SHADOW_DIR / "shadow_positions.json"
SHADOW_PNL = SHADOW_DIR / "shadow_pnl.json"
SHADOW_LOCK = SHADOW_DIR / ".shadow.lock"
LOCK_RETRY_ATTEMPTS = 3
LOCK_RETRY_DELAY_SECONDS = 0.1


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _compact_date(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if "T" in text:
        text = text.split("T", 1)[0]
    return "".join(ch for ch in text[:10] if ch.isdigit())


def _display_date(value: Any) -> str:
    compact = _compact_date(value)
    if len(compact) == 8:
        return f"{compact[:4]}-{compact[4:6]}-{compact[6:8]}"
    return str(value or "").strip()


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return default if number != number else number
    except (TypeError, ValueError):
        return default


def _safe_quantity(value: Any) -> float:
    quantity = _safe_float(value, 0.0)
    if quantity.is_integer():
        return int(quantity)
    return quantity


def _normalize_market(value: Any) -> str:
    market = str(value or "").strip().lower()
    return market or "unknown"


def _infer_market(symbol: str) -> str:
    upper = symbol.upper()
    if upper.endswith((".SH", ".SZ")):
        return "ashare"
    if upper.endswith(("USDT", "USDC", "USD")):
        return "crypto"
    if "-" in symbol or "will-" in upper.lower():
        return "pm"
    return "unknown"


def _symbol_from_order(order: dict[str, Any]) -> str:
    return str(
        order.get("ts_code")
        or order.get("symbol")
        or order.get("market_id")
        or order.get("asset")
        or ""
    ).strip()


def _normalize_side(value: Any) -> str:
    side = str(value or "buy").strip().lower()
    return {"buy": "buy", "long": "buy", "sell": "sell", "reduce": "sell", "close": "sell"}.get(side, side)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _iter_trade_rows() -> Iterator[dict[str, Any]]:
    try:
        lines = SHADOW_TRADES.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError):
        return
    for line in lines:
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            yield payload


@contextmanager
def _file_lock() -> Iterator[None]:
    SHADOW_LOCK.parent.mkdir(parents=True, exist_ok=True)
    with SHADOW_LOCK.open("a+", encoding="utf-8") as handle:
        _acquire_exclusive_lock(handle.fileno(), SHADOW_LOCK)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _acquire_exclusive_lock(fd: int, lock_path: Path) -> None:
    last_error: OSError | None = None
    retry_errnos = {errno.EACCES, errno.EAGAIN, getattr(errno, "EWOULDBLOCK", errno.EAGAIN)}
    for attempt in range(1, LOCK_RETRY_ATTEMPTS + 1):
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as exc:
            if exc.errno not in retry_errnos:
                raise
            last_error = exc
            if attempt < LOCK_RETRY_ATTEMPTS:
                time.sleep(LOCK_RETRY_DELAY_SECONDS * attempt)
    raise TimeoutError(f"Could not acquire shadow broker lock {lock_path} after {LOCK_RETRY_ATTEMPTS} attempts") from last_error


def _validate_shadow_order(order: dict[str, Any]) -> None:
    try:
        reject_real_execution_payload(order, context="shadow_broker.order")
    except RuntimeError as exc:
        raise ValueError(str(exc)) from exc
    layer = str(order.get("capital_layer") or "shadow").strip().lower()
    if layer != "shadow":
        raise ValueError(f"shadow_broker only accepts capital_layer=shadow, got {layer!r}")


def _load_positions() -> dict[str, Any]:
    return _read_json(SHADOW_POSITIONS)


def _position_bucket(positions: dict[str, Any], strategy_name: str, market: str) -> dict[str, Any]:
    strategies = positions.setdefault("strategies", {})
    strategy = strategies.setdefault(strategy_name, {})
    markets = strategy.setdefault("markets", {})
    return markets.setdefault(market, {})


def _apply_trade_to_positions(
    positions: dict[str, Any],
    trade: dict[str, Any],
) -> tuple[bool, str, float]:
    strategy_name = str(trade["strategy_name"])
    market = _normalize_market(trade.get("market"))
    symbol = str(trade["ts_code"])
    side = str(trade["side"])
    quantity = _safe_float(trade.get("quantity"), 0.0)
    price = _safe_float(trade.get("price"), 0.0)
    commission = _safe_float(trade.get("commission"), 0.0)
    if quantity <= 0 or price <= 0:
        return False, "quantity and price must be positive", 0.0

    bucket = _position_bucket(positions, strategy_name, market)
    current = bucket.setdefault(
        symbol,
        {
            "ts_code": symbol,
            "market": market,
            "quantity": 0.0,
            "avg_price": 0.0,
            "cost_basis": 0.0,
            "realized_pnl": 0.0,
        },
    )
    realized_pnl = 0.0
    current_qty = _safe_float(current.get("quantity"), 0.0)
    avg_price = _safe_float(current.get("avg_price"), 0.0)
    cost_basis = _safe_float(current.get("cost_basis"), current_qty * avg_price)

    if side == "buy":
        new_qty = current_qty + quantity
        new_cost = cost_basis + quantity * price + commission
        current["quantity"] = _safe_quantity(new_qty)
        current["cost_basis"] = round(new_cost, 6)
        current["avg_price"] = round(new_cost / new_qty, 6) if new_qty else 0.0
    elif side == "sell":
        if quantity > current_qty:
            return False, "sell quantity exceeds existing shadow position", 0.0
        realized_pnl = (price - avg_price) * quantity - commission
        new_qty = current_qty - quantity
        new_cost = max(0.0, cost_basis - avg_price * quantity)
        current["quantity"] = _safe_quantity(new_qty)
        current["cost_basis"] = round(new_cost, 6)
        current["avg_price"] = round(new_cost / new_qty, 6) if new_qty else 0.0
        current["realized_pnl"] = round(_safe_float(current.get("realized_pnl"), 0.0) + realized_pnl, 6)
        if new_qty <= 0:
            bucket.pop(symbol, None)
    else:
        return False, f"unsupported side: {side}", 0.0

    if symbol in bucket:
        bucket[symbol]["updated_at"] = trade["created_at"]
    return True, "ok", round(realized_pnl, 6)


def _trade_date_for_row(row: dict[str, Any]) -> str:
    return _compact_date(row.get("trade_date")) or _compact_date(row.get("created_at"))


def _row_market(row: dict[str, Any]) -> str:
    return _normalize_market(row.get("market"))


def _matches_market(row: dict[str, Any], market: str | None) -> bool:
    if market is None:
        return True
    return _row_market(row) == _normalize_market(market)


def _replay_strategy(
    strategy_name: str,
    date: str,
    *,
    market: str | None = None,
) -> dict[str, Any]:
    target_date = _compact_date(date)
    positions: dict[str, dict[str, Any]] = {}
    realized_pnl = 0.0
    explicit_pnl = 0.0
    total_trades = 0
    buys = 0
    sells = 0
    gross_amount = 0.0
    skipped_rows = 0

    for row in _iter_trade_rows():
        if str(row.get("strategy_name") or "") != strategy_name:
            continue
        row_date = _trade_date_for_row(row)
        if target_date and row_date and row_date > target_date:
            continue
        if not _matches_market(row, market):
            continue

        symbol = str(row.get("ts_code") or row.get("symbol") or "").strip()
        side = _normalize_side(row.get("side"))
        quantity = _safe_float(row.get("quantity"), 0.0)
        price = _safe_float(row.get("price"), 0.0)
        commission = _safe_float(row.get("commission"), 0.0)
        amount = abs(_safe_float(row.get("amount"), quantity * price))
        is_target_day = not target_date or row_date == target_date
        if is_target_day:
            total_trades += 1
            gross_amount += amount
            buys += 1 if side == "buy" else 0
            sells += 1 if side == "sell" else 0

        position = positions.setdefault(symbol, {"quantity": 0.0, "avg_price": 0.0, "cost_basis": 0.0})
        if side == "buy":
            new_qty = _safe_float(position["quantity"]) + quantity
            new_cost = _safe_float(position["cost_basis"]) + quantity * price + commission
            position["quantity"] = new_qty
            position["cost_basis"] = new_cost
            position["avg_price"] = new_cost / new_qty if new_qty else 0.0
        elif side == "sell":
            current_qty = _safe_float(position["quantity"])
            if quantity > current_qty:
                skipped_rows += 1
                continue
            row_pnl = (price - _safe_float(position["avg_price"])) * quantity - commission
            if is_target_day:
                realized_pnl += row_pnl
            new_qty = current_qty - quantity
            new_cost = max(0.0, _safe_float(position["cost_basis"]) - _safe_float(position["avg_price"]) * quantity)
            position["quantity"] = new_qty
            position["cost_basis"] = new_cost
            position["avg_price"] = new_cost / new_qty if new_qty else 0.0

        if is_target_day and "pnl" in row:
            explicit_pnl += _safe_float(row.get("pnl"), 0.0)

    normalized_positions = {
        symbol: {
            "ts_code": symbol,
            "quantity": _safe_quantity(values.get("quantity")),
            "avg_price": round(_safe_float(values.get("avg_price")), 6),
            "cost_basis": round(_safe_float(values.get("cost_basis")), 6),
        }
        for symbol, values in sorted(positions.items())
        if _safe_float(values.get("quantity")) > 0
    }
    pnl = explicit_pnl if explicit_pnl else realized_pnl
    return {
        "strategy_name": strategy_name,
        "date": _display_date(date),
        "market": _normalize_market(market) if market is not None else "all",
        "capital_layer": "shadow",
        "account_type": "shadow",
        "real_execution": False,
        "total_trades": total_trades,
        "buys": buys,
        "sells": sells,
        "gross_amount": round(gross_amount, 6),
        "realized_pnl": round(realized_pnl, 6),
        "explicit_pnl": round(explicit_pnl, 6),
        "pnl": round(pnl, 6),
        "positions": normalized_positions,
        "skipped_rows": skipped_rows,
    }


def record_shadow(order: dict[str, Any], strategy_name: str, market: str | None = None) -> dict[str, Any]:
    """Record a shadow order and update local shadow JSON ledgers."""

    payload = dict(order or {})
    _validate_shadow_order(payload)
    symbol = _symbol_from_order(payload)
    if not symbol:
        raise ValueError("shadow_broker requires ts_code/symbol/market_id")

    market_key = _normalize_market(market or payload.get("market") or _infer_market(symbol))
    trade_date = _display_date(payload.get("trade_date") or payload.get("date") or _now_iso())
    quantity = _safe_float(payload.get("quantity"), 0.0)
    price = _safe_float(payload.get("price") or payload.get("limit_price") or payload.get("execution_price"), 0.0)
    commission = _safe_float(payload.get("commission"), 0.0)
    side = _normalize_side(payload.get("side") or payload.get("direction"))
    amount = quantity * price
    trade_id = str(payload.get("trade_id") or f"SHADOW-{uuid.uuid4().hex[:12]}")
    strategy = str(strategy_name or payload.get("strategy_name") or "shadow_strategy").strip()

    trade = {
        "trade_id": trade_id,
        "strategy_name": strategy,
        "trade_date": trade_date,
        "market": market_key,
        "ts_code": symbol,
        "side": side,
        "quantity": _safe_quantity(quantity),
        "price": round(price, 8),
        "amount": round(amount, 8),
        "commission": round(commission, 8),
        "net_amount": round(-amount - commission if side == "buy" else amount - commission, 8),
        "capital_layer": "shadow",
        "account_type": "shadow",
        "real_execution": False,
        "direct_execution": False,
        "created_at": _now_iso(),
    }
    if payload.get("note"):
        trade["note"] = str(payload.get("note"))

    with _file_lock():
        positions = _load_positions()
        ok, message, realized_pnl = _apply_trade_to_positions(positions, trade)
        if not ok:
            return {
                "recorded": False,
                "status": "rejected",
                "message": message,
                "trade_id": trade_id,
                "market": market_key,
                "capital_layer": "shadow",
                "real_execution": False,
            }
        trade["pnl"] = realized_pnl
        SHADOW_DIR.mkdir(parents=True, exist_ok=True)
        with SHADOW_TRADES.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(trade, ensure_ascii=False) + "\n")
        positions["updated_at"] = trade["created_at"]
        _write_json(SHADOW_POSITIONS, positions)
        pnl_snapshot = _read_json(SHADOW_PNL)
        pnl_snapshot.setdefault("dates", {}).setdefault(_display_date(trade_date), {})[strategy] = _replay_strategy(
            strategy,
            trade_date,
        )
        pnl_snapshot["updated_at"] = trade["created_at"]
        _write_json(SHADOW_PNL, pnl_snapshot)

    return {
        "recorded": True,
        "status": "shadow_recorded",
        "message": f"shadow_recorded to {SHADOW_TRADES}",
        "trade_id": trade_id,
        "market": market_key,
        "strategy_name": strategy,
        "symbol": symbol,
        "trade_date": trade_date,
        "pnl": realized_pnl,
        "capital_layer": "shadow",
        "account_type": "shadow",
        "real_execution": False,
        "direct_execution": False,
    }


def get_shadow_pnl(strategy_name: str, date: str, market: str | None = None) -> dict[str, Any]:
    """Return shadow PnL for one strategy/date, optionally scoped to a market."""

    return _replay_strategy(str(strategy_name), date, market=market)


def get_all_shadow_pnl(date: str, market: str | None = None) -> dict[str, dict[str, Any]]:
    """Return shadow PnL for every strategy seen on the requested date."""

    target_date = _compact_date(date)
    strategies = sorted(
        {
            str(row.get("strategy_name") or "")
            for row in _iter_trade_rows()
            if str(row.get("strategy_name") or "") and (not target_date or _trade_date_for_row(row) == target_date)
        }
    )
    return {strategy: get_shadow_pnl(strategy, date, market=market) for strategy in strategies}


__all__ = [
    "SHADOW_DIR",
    "SHADOW_LOCK",
    "SHADOW_PNL",
    "SHADOW_POSITIONS",
    "SHADOW_TRADES",
    "get_all_shadow_pnl",
    "get_shadow_pnl",
    "record_shadow",
]
