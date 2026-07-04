#!/usr/bin/env python3
"""Unified position schema and adapters."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Mapping

try:
    from Ashare import t_plus_1 as _t_plus_1
except ImportError:  # pragma: no cover
    _t_plus_1 = None  # type: ignore[assignment]

DEFAULT_CAPITAL_LAYER = "shadow"
CAPITAL_LAYERS = {"real", "simulated", "shadow"}


@dataclass(frozen=True)
class Position:
    ts_code: str
    quantity: int = 0
    sellable_quantity: int = 0
    avg_price: float = 0.0
    cost_basis: float = 0.0
    entry_date: str = ""
    high_price: float = 0.0
    thesis: str = ""
    capital_layer: str = DEFAULT_CAPITAL_LAYER


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
        return parsed if parsed == parsed else default
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not value:
        return None

    raw_value = str(value).strip()
    if not raw_value:
        return None

    if raw_value.isdigit() and len(raw_value) == 8:
        try:
            return datetime.strptime(raw_value, "%Y%m%d").date()
        except ValueError:
            return None

    for parser in (date.fromisoformat,):
        try:
            return parser(raw_value)
        except ValueError:
            continue

    iso_candidate = raw_value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(iso_candidate).date()
    except ValueError:
        return None


def _normalize_capital_layer(value: Any, default: str = DEFAULT_CAPITAL_LAYER) -> str:
    layer = str(value or default).strip().lower()
    if layer in {"paper", "shadow"}:
        return "shadow"
    if layer == "sim":
        return "simulated"
    if layer in CAPITAL_LAYERS:
        return layer
    return default


def _fallback_is_trading_day(trading_day: date) -> bool:
    return trading_day.weekday() < 5


def _next_trading_day(open_day: date) -> date:
    if _t_plus_1 is not None:
        return _t_plus_1.next_trading_day(open_day)

    current = open_day + timedelta(days=1)
    while not _fallback_is_trading_day(current):
        current += timedelta(days=1)
    return current


def _can_sell(entry_date: Any, current_date: Any) -> bool:
    if _t_plus_1 is not None:
        return _t_plus_1.can_sell(entry_date, current_date)

    open_day = _parse_date(entry_date)
    as_of_day = _parse_date(current_date)
    if open_day is None or as_of_day is None:
        return False
    return as_of_day >= _next_trading_day(open_day)


def _compute_sellable_quantity(
    quantity: int,
    entry_date: Any,
    as_of: date | datetime | str | None = None,
) -> int:
    if quantity <= 0:
        return 0

    entry_day = _parse_date(entry_date)
    if entry_day is None:
        return 0

    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    try:
        import zoneinfo; _bj = zoneinfo.ZoneInfo("Asia/Shanghai")
    except Exception:
        _bj = _tz(_td(hours=8))
    _today = _dt.now(_bj).date()
    current_day = _parse_date(as_of) if as_of is not None else _today
    if current_day is None:
        current_day = _today

    return quantity if _can_sell(entry_day, current_day) else 0


def from_ledger(
    row: Mapping[str, Any],
    as_of: date | datetime | str | None = None,
) -> Position:
    if not isinstance(row, Mapping):
        raise ValueError("row must be a mapping")

    ts_code = str(row.get("ts_code", "") or "").strip()
    quantity = max(_safe_int(row.get("quantity")), 0)
    avg_price = _safe_float(row.get("avg_price", row.get("cost", 0.0)))
    cost_basis = _safe_float(row.get("cost_basis", avg_price * quantity))
    entry_date = str(row.get("entry_date", "") or "")
    high_price = _safe_float(row.get("high_price", avg_price))
    thesis = str(row.get("thesis", "") or "")
    capital_layer = _normalize_capital_layer(row.get("capital_layer"))
    sellable_quantity = _compute_sellable_quantity(quantity, entry_date, as_of=as_of)

    return Position(
        ts_code=ts_code,
        quantity=quantity,
        sellable_quantity=sellable_quantity,
        avg_price=avg_price,
        cost_basis=cost_basis,
        entry_date=entry_date,
        high_price=high_price if high_price > 0 else avg_price,
        thesis=thesis,
        capital_layer=capital_layer,
    )


def coerce_position(
    row: Position | Mapping[str, Any],
    as_of: date | datetime | str | None = None,
) -> Position:
    if isinstance(row, Position):
        return Position(
            ts_code=row.ts_code,
            quantity=max(_safe_int(row.quantity), 0),
            sellable_quantity=max(_safe_int(row.sellable_quantity), 0),
            avg_price=_safe_float(row.avg_price),
            cost_basis=_safe_float(row.cost_basis),
            entry_date=str(row.entry_date or ""),
            high_price=_safe_float(row.high_price, _safe_float(row.avg_price)),
            thesis=str(row.thesis or ""),
            capital_layer=_normalize_capital_layer(row.capital_layer),
        )

    if not isinstance(row, Mapping):
        raise ValueError("position must be a Position or mapping")

    if "avg_price" in row or "cost_basis" in row or "capital_layer" in row:
        quantity = max(_safe_int(row.get("quantity")), 0)
        explicit_sellable = row.get("sellable_quantity")
        sellable_quantity = (
            max(_safe_int(explicit_sellable), 0)
            if explicit_sellable is not None
            else _compute_sellable_quantity(quantity, row.get("entry_date"), as_of=as_of)
        )
        avg_price = _safe_float(row.get("avg_price", row.get("cost", 0.0)))
        return Position(
            ts_code=str(row.get("ts_code", "") or "").strip(),
            quantity=quantity,
            sellable_quantity=sellable_quantity,
            avg_price=avg_price,
            cost_basis=_safe_float(row.get("cost_basis", avg_price * quantity)),
            entry_date=str(row.get("entry_date", "") or ""),
            high_price=_safe_float(row.get("high_price", avg_price)),
            thesis=str(row.get("thesis", "") or ""),
            capital_layer=_normalize_capital_layer(row.get("capital_layer")),
        )

    return from_ledger(row, as_of=as_of)


__all__ = ["Position", "coerce_position", "from_ledger"]
