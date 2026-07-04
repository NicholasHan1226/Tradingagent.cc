#!/usr/bin/env python3
"""Automated multi-style simulation runner for China futures."""

from __future__ import annotations

import json
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from shared.execution.signal_state_machine import SignalStateConflict, SignalStateMachine
from shared.execution.sim_broker import execute_sim_order

from . import MARKET
from .adapter import CNFuturesAdapter, READER_MARKET
from .contract_rules import get_contract_rule, normalize_product
from .margin_model import estimate_order_cost
from .review import append_review
from .signal_engine import generate_style_signal
from . import sim_executor as _sim_executor  # noqa: F401  # Ensure registry side effect.


INTRADAY_INTERVAL = "5min"
DEFAULT_MAX_INTRADAY_BAR_AGE_MINUTES = 10.0
CN_TZ = timezone(timedelta(hours=8))
POSITIONS_FILENAME = "cn_futures_sim_positions.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if result == result else default
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _style_is_active(style: dict[str, Any]) -> bool:
    status = str(style.get("status") or "").strip().lower()
    if status in {"paused", "deprecated"}:
        return False
    return bool(style.get("enabled", True))


def _style_allows_symbol(style: dict[str, Any], symbol: str) -> bool:
    raw_products = style.get("products") or style.get("target_products")
    if not raw_products:
        return True
    allowed = {
        str(item).strip().lower()
        for item in raw_products
        if str(item).strip()
    }
    if not allowed:
        return True
    try:
        return normalize_product(symbol) in allowed
    except ValueError:
        return False


def _cn_local_time(now: datetime | None) -> time | None:
    if now is None:
        return None
    current = now
    if current.tzinfo is not None:
        current = current.astimezone(CN_TZ)
    return current.time()


def _style_allows_session(style: dict[str, Any], now: datetime | None) -> bool:
    if str(style.get("style_family") or "").strip().lower() != "index_intraday_directional":
        return True
    if not bool(style.get("no_overnight", True)):
        return True
    current = _cn_local_time(now)
    if current is None:
        return True
    return (time(9, 30) <= current <= time(11, 30)) or (time(13, 0) <= current <= time(15, 0))


def _minutes_until_day_session_close(now: datetime | None) -> float | None:
    current_time = _cn_local_time(now)
    if current_time is None:
        return None
    close_time: time | None = None
    if time(9, 30) <= current_time <= time(11, 30):
        close_time = time(11, 30)
    elif time(13, 0) <= current_time <= time(15, 0):
        close_time = time(15, 0)
    if close_time is None:
        return None
    today = datetime(2000, 1, 1)
    current_dt = datetime.combine(today, current_time)
    close_dt = datetime.combine(today, close_time)
    return (close_dt - current_dt).total_seconds() / 60.0


def _should_flatten_no_overnight(style: dict[str, Any], now: datetime | None) -> bool:
    if not bool(style.get("no_overnight", False)):
        return False
    minutes_left = _minutes_until_day_session_close(now)
    if minutes_left is None:
        return False
    threshold = max(1, _safe_int(style.get("flatten_before_session_close_minutes"), 10))
    return 0 <= minutes_left <= threshold


def _parse_dt(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    normalized = raw.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _bar_age_minutes(latest_bar_time: str, now: datetime | None) -> float | None:
    if now is None:
        return None
    bar_dt = _parse_dt(latest_bar_time)
    if bar_dt is None:
        return None
    now_dt = now.astimezone(timezone.utc).replace(tzinfo=None) if now.tzinfo is not None else now
    return (now_dt - bar_dt).total_seconds() / 60.0


def _is_intraday_bar_fresh(latest_bar_time: str, *, now: datetime | None, max_age_minutes: float) -> tuple[bool, float | None]:
    age = _bar_age_minutes(latest_bar_time, now)
    if age is None:
        return False, None
    return age <= max_age_minutes, age


def _read_daily_bars(reader: Any, symbol: str, date: str) -> list[dict[str, Any]]:
    get_bars = getattr(reader, "get_bars_daily", None)
    if not callable(get_bars):
        return []
    try:
        rows = get_bars(READER_MARKET, symbol, None, date)
    except TypeError:
        rows = get_bars(market=READER_MARKET, symbol=symbol, end=date)
    except Exception:
        return []
    return [dict(row) for row in rows or [] if isinstance(row, dict)]


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


def _contract_month_start(symbol: str) -> datetime | None:
    value = str(symbol or "").strip().lower().split(".", 1)[0]
    digits = "".join(ch for ch in value if ch.isdigit())
    if len(digits) < 4:
        return None
    year = 2000 + int(digits[:2])
    month = int(digits[2:4])
    if month < 1 or month > 12:
        return None
    return datetime(year, month, 1)


def _contract_inside_rollover_guard(symbol: str, date: str, style: dict[str, Any]) -> tuple[bool, int | None]:
    min_days = _safe_int(style.get("rollover_min_days_to_contract_month_start"), 0)
    if min_days <= 0:
        return False, None
    trade_dt = _parse_trade_date(date)
    contract_start = _contract_month_start(symbol)
    if trade_dt is None or contract_start is None:
        return False, None
    days_to_month = (contract_start - trade_dt).days
    return days_to_month <= min_days, days_to_month


def _positions_path(signals_dir: Path) -> Path:
    return signals_dir / "positions" / POSITIONS_FILENAME


def _read_position_snapshot(signals_dir: Path) -> dict[str, Any]:
    path = _positions_path(signals_dir)
    if not path.exists():
        return {"market": MARKET, "positions": [], "position_count": 0, "total_margin_required": 0.0}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"market": MARKET, "positions": [], "position_count": 0, "total_margin_required": 0.0}
    if not isinstance(payload, dict):
        return {"market": MARKET, "positions": [], "position_count": 0, "total_margin_required": 0.0}
    positions = payload.get("positions")
    if not isinstance(positions, list):
        payload["positions"] = []
    return payload


def _write_position_snapshot(signals_dir: Path, snapshot: dict[str, Any]) -> None:
    positions = [
        position
        for position in snapshot.get("positions", [])
        if isinstance(position, dict) and _safe_int(position.get("net_qty"), 0) != 0
    ]
    total_margin = round(sum(_safe_float(position.get("margin_required"), 0.0) for position in positions), 6)
    payload = {
        "market": MARKET,
        "capital_layer": "simulated",
        "account_type": "simulated",
        "position_count": len(positions),
        "total_margin_required": total_margin,
        "positions": positions,
        "updated_at": _now_iso(),
    }
    path = _positions_path(signals_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def _position_key(style_name: str, symbol: str) -> str:
    return f"{style_name}|{symbol}"


def _positions_by_key(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    positions: dict[str, dict[str, Any]] = {}
    for position in snapshot.get("positions", []):
        if not isinstance(position, dict):
            continue
        style_name = str(position.get("style") or position.get("strategy_name") or "").strip()
        symbol = str(position.get("symbol") or "").strip()
        if style_name and symbol and _safe_int(position.get("net_qty"), 0) != 0:
            positions[_position_key(style_name, symbol)] = position
    return positions


def _style_margin_used(snapshot: dict[str, Any], style_name: str) -> float:
    return round(
        sum(
            _safe_float(position.get("margin_required"), 0.0)
            for position in snapshot.get("positions", [])
            if isinstance(position, dict) and str(position.get("style") or "") == style_name
        ),
        6,
    )


def _side_sign(side: str) -> int:
    return 1 if str(side or "").lower().strip() in {"buy", "long"} else -1


def _position_side(net_qty: int) -> str:
    return "long" if net_qty > 0 else "short"


def _opposite_side_for_position(position: dict[str, Any]) -> str:
    return "sell" if _safe_int(position.get("net_qty"), 0) > 0 else "buy"


def _update_position_snapshot(
    signals_dir: Path,
    *,
    date: str,
    style_name: str,
    symbol: str,
    order: dict[str, Any],
    receipt: dict[str, Any],
    performance: dict[str, Any],
) -> dict[str, Any]:
    snapshot = _read_position_snapshot(signals_dir)
    positions = _positions_by_key(snapshot)
    key = _position_key(style_name, symbol)
    previous = dict(positions.get(key, {}))
    previous_qty = _safe_int(previous.get("net_qty"), 0)
    filled_qty = _safe_int(receipt.get("filled_qty"), 0)
    if filled_qty <= 0:
        return snapshot
    fill_sign = _side_sign(str(order.get("side") or "buy"))
    new_qty = previous_qty + (fill_sign * filled_qty)
    raw = receipt.get("raw_response") if isinstance(receipt.get("raw_response"), dict) else {}
    positions.pop(key, None)
    if new_qty != 0:
        avg_price = _safe_float(receipt.get("avg_price"), _safe_float(order.get("price"), 0.0))
        if previous_qty and (previous_qty > 0) == (new_qty > 0) and (previous_qty > 0) == (fill_sign > 0):
            previous_abs = abs(previous_qty)
            previous_price = _safe_float(previous.get("avg_price"), avg_price)
            avg_price = round(((previous_price * previous_abs) + (avg_price * filled_qty)) / max(previous_abs + filled_qty, 1), 8)
        positions[key] = {
            "style": style_name,
            "strategy_name": style_name,
            "symbol": symbol,
            "net_qty": new_qty,
            "side": _position_side(new_qty),
            "avg_price": avg_price,
            "last_price": _safe_float(receipt.get("avg_price"), _safe_float(order.get("price"), 0.0)),
            "margin_required": _safe_float(raw.get("margin_required"), 0.0) if previous_qty == 0 or (previous_qty > 0) != (new_qty > 0) else _safe_float(previous.get("margin_required"), 0.0) + _safe_float(raw.get("margin_required"), 0.0),
            "notional": _safe_float(raw.get("notional"), 0.0),
            "updated_trade_date": _normalize_trade_date(date),
            "updated_at": _now_iso(),
            "last_order_id": order.get("order_id"),
            "last_bar_time": order.get("bar_time"),
            "realized_pnl": _safe_float(previous.get("realized_pnl"), 0.0) + _safe_float(performance.get("realized_pnl"), 0.0),
        }
    snapshot["positions"] = sorted(positions.values(), key=lambda item: (str(item.get("style")), str(item.get("symbol"))))
    _write_position_snapshot(signals_dir, snapshot)
    return _read_position_snapshot(signals_dir)


def _read_intraday_bars(reader: Any, symbol: str, date: str) -> list[dict[str, Any]]:
    get_bars = getattr(reader, "get_bars_intraday", None)
    if not callable(get_bars):
        return []
    rows: Any
    try:
        rows = get_bars(READER_MARKET, symbol, INTRADAY_INTERVAL)
    except TypeError:
        try:
            rows = get_bars(market=READER_MARKET, symbol=symbol, interval=INTRADAY_INTERVAL)
        except TypeError:
            rows = get_bars(market=READER_MARKET, symbol=symbol, interval=INTRADAY_INTERVAL, start="", end="")
        except Exception:
            return []
    except Exception:
        return []
    trade_date = _normalize_trade_date(date)
    normalized = [dict(row) for row in rows or [] if isinstance(row, dict)]
    filtered = [
        row
        for row in normalized
        if not trade_date
        or _normalize_trade_date(row.get("trade_date") or row.get("bar_time") or row.get("time")) == trade_date
    ]
    filtered.sort(key=lambda row: str(row.get("bar_time") or row.get("time") or row.get("trade_time") or ""))
    return filtered


def _bars_for_cadence(reader: Any, symbol: str, date: str, cadence: str) -> tuple[list[dict[str, Any]], str, str]:
    cadence_value = str(cadence or INTRADAY_INTERVAL).lower()
    if cadence_value in {"daily", "1d", "day"}:
        return _read_daily_bars(reader, symbol, date), "daily", ""
    bars = _read_intraday_bars(reader, symbol, date)
    latest_bar_time = str((bars[-1] if bars else {}).get("bar_time") or (bars[-1] if bars else {}).get("time") or "")
    return bars, INTRADAY_INTERVAL, latest_bar_time


def _order_period_key(date: str, cadence: str, latest_bar_time: str) -> str:
    if cadence == "daily":
        return str(date)
    raw = latest_bar_time or str(date)
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) >= 12:
        return digits[:12]
    if len(digits) >= 8:
        return digits[:8]
    return str(date)


def _same_day_filled_signals(signals_dir: Path, *, date: str, style_name: str, symbol: str) -> list[dict[str, Any]]:
    filled_dir = signals_dir / "filled"
    if not filled_dir.exists():
        return []
    trade_date = _normalize_trade_date(date)
    rows: list[dict[str, Any]] = []
    for path in sorted(filled_dir.glob("SIM-CNF-*.json")):
        try:
            import json

            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if str(payload.get("strategy_name") or "") != style_name:
            continue
        if str(payload.get("symbol") or payload.get("ts_code") or "") != symbol:
            continue
        payload_date = _normalize_trade_date(payload.get("trade_date") or payload.get("valid_until") or payload.get("bar_time"))
        if trade_date and payload_date != trade_date:
            continue
        rows.append(payload)
    rows.sort(key=lambda item: str(item.get("bar_time") or item.get("timestamp") or item.get("order_id") or ""))
    return rows


def _has_repeated_same_side_exposure(
    signals_dir: Path,
    *,
    date: str,
    style_name: str,
    symbol: str,
    side: str,
) -> bool:
    rows = _same_day_filled_signals(signals_dir, date=date, style_name=style_name, symbol=symbol)
    if not rows:
        return False
    latest = rows[-1]
    latest_side = str(latest.get("side") or latest.get("direction") or "").lower().strip()
    return bool(latest_side and latest_side == str(side or "").lower().strip())


def _latest_opposite_fill(
    signals_dir: Path,
    *,
    date: str,
    style_name: str,
    symbol: str,
    side: str,
) -> dict[str, Any] | None:
    rows = _same_day_filled_signals(signals_dir, date=date, style_name=style_name, symbol=symbol)
    for row in reversed(rows):
        previous_side = str(row.get("side") or row.get("direction") or "").lower().strip()
        if previous_side and previous_side != str(side or "").lower().strip():
            return row
    return None


def _realized_pnl_from_reversal(
    *,
    previous: dict[str, Any] | None,
    side: str,
    receipt: dict[str, Any],
    rule_multiplier: int,
) -> dict[str, Any]:
    if not previous:
        return {}
    previous_price = _safe_float(previous.get("filled_price") or previous.get("price"), 0.0)
    exit_price = _safe_float(receipt.get("avg_price"), 0.0)
    qty = min(_safe_int(previous.get("filled_qty") or previous.get("filled_quantity") or previous.get("quantity"), 0), _safe_int(receipt.get("filled_qty"), 0))
    if previous_price <= 0 or exit_price <= 0 or qty <= 0:
        return {}
    previous_side = str(previous.get("side") or previous.get("direction") or "").lower().strip()
    current_side = str(side or "").lower().strip()
    gross = 0.0
    if previous_side in {"buy", "long"} and current_side in {"sell", "short"}:
        gross = (exit_price - previous_price) * qty * rule_multiplier
    elif previous_side in {"sell", "short"} and current_side in {"buy", "long"}:
        gross = (previous_price - exit_price) * qty * rule_multiplier
    else:
        return {}
    fee = _safe_float(previous.get("fee"), 0.0) + _safe_float(receipt.get("fee"), 0.0)
    return {
        "realized_pnl": round(gross - fee, 6),
        "gross_pnl": round(gross, 6),
        "round_trip_fee": round(fee, 6),
        "closed_quantity": qty,
        "entry_price": previous_price,
        "exit_price": exit_price,
        "entry_side": previous_side,
        "exit_side": current_side,
        "method": "same_day_reversal_estimate",
    }


def _quantity_for_style(
    *,
    symbol: str,
    price: float,
    capital: float,
    style: dict[str, Any],
) -> int:
    cost = estimate_order_cost(symbol=symbol, side="buy", quantity=1, price=price)
    margin_per_lot = max(cost.margin_required, 1.0)
    max_margin_usage = min(max(_safe_float(style.get("max_margin_usage"), 0.20), 0.01), 0.80)
    risk_per_trade = min(max(_safe_float(style.get("risk_per_trade"), 0.02), 0.001), max_margin_usage)
    weight = min(max(_safe_float(style.get("weight"), 1.0), 0.01), 1.0)
    margin_budget = capital * min(max_margin_usage, risk_per_trade * weight)
    return max(1, int(margin_budget // margin_per_lot))


def _signal_card(
    *,
    date: str,
    cadence: str,
    latest_bar_time: str,
    style_name: str,
    symbol: str,
    order: dict[str, Any],
    receipt: dict[str, Any],
    signal: dict[str, Any],
) -> dict[str, Any]:
    order_id = str(order["order_id"])
    raw = receipt.get("raw_response") if isinstance(receipt.get("raw_response"), dict) else {}
    return {
        "order_id": order_id,
        "ts_code": symbol,
        "symbol": symbol,
        "market": MARKET,
        "reader_market": READER_MARKET,
        "direction": order["side"],
        "side": order["side"],
        "quantity": _safe_int(order.get("quantity"), 0),
        "price": _safe_float(order.get("price"), 0.0),
        "trigger_price": _safe_float(order.get("price"), 0.0),
        "status": "pending",
        "capital_layer": "simulated",
        "account_type": "simulated",
        "account": f"cn_futures_sim_{style_name}",
        "strategy_name": style_name,
        "manual_confirm_required": False,
        "direct_execution": True,
        "real_trading_enabled": False,
        "valid_until": date,
        "timestamp": _now_iso(),
        "idempotency_key": order_id,
        "source": "cn_futures_multi_style_simulation",
        "cadence": cadence,
        "bar_time": latest_bar_time,
        "signal": signal,
        "margin_required": raw.get("margin_required"),
        "fee": receipt.get("fee"),
    }


def _write_filled_signal(signals_dir: Path, card: dict[str, Any], receipt: dict[str, Any]) -> dict[str, Any]:
    machine = SignalStateMachine(signals_dir)
    try:
        machine.write_pending(card)
    except SignalStateConflict:
        return {"status": "duplicate", "order_id": card.get("order_id", "")}
    machine.claim(str(card["order_id"]), worker_id="cn_futures_sim")
    machine.mark_running(str(card["order_id"]), worker_id="cn_futures_sim")
    fill_info = {
        "filled_price": receipt.get("avg_price", card.get("price", 0.0)),
        "filled_quantity": receipt.get("filled_qty", card.get("quantity", 0)),
        "filled_qty": receipt.get("filled_qty", card.get("quantity", 0)),
        "fee": receipt.get("fee", 0.0),
        "fill_time": _now_iso(),
        "raw_response": receipt.get("raw_response", {}),
    }
    partial = str(receipt.get("status") or "").lower().strip() == "partial"
    filled = machine.fill(str(card["order_id"]), fill_info, partial=partial)
    return {"status": "partial" if partial else "filled", "order_id": card.get("order_id", ""), "filled_signal": filled}


def run_multi_style_simulation(
    adapter: CNFuturesAdapter,
    date: str,
    reader: Any,
    *,
    signals_dir: Path,
    review_path: Path | None = None,
    cadence: str = INTRADAY_INTERVAL,
    now: datetime | None = None,
    max_intraday_bar_age_minutes: float = DEFAULT_MAX_INTRADAY_BAR_AGE_MINUTES,
) -> dict[str, Any]:
    """Run all configured futures styles through simulated execution."""

    cadence_value = "daily" if str(cadence or "").lower() in {"daily", "1d", "day"} else INTRADAY_INTERVAL
    if now is None:
        now = datetime.now()
    config = adapter.get_strategy_config()
    styles = config.get("styles") if isinstance(config.get("styles"), dict) else {}
    account = adapter.get_sim_account()
    capital = _safe_float(account.get("sim_capital") if isinstance(account, dict) else None, 100_000.0)
    errors: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    position_snapshot = _read_position_snapshot(signals_dir)
    if cadence_value == "daily":
        universe = adapter.get_universe(date)
    else:
        get_intraday_universe = getattr(adapter, "get_intraday_universe", None)
        universe = get_intraday_universe(date, interval=INTRADAY_INTERVAL) if callable(get_intraday_universe) else adapter.get_universe(date)
    if not universe:
        errors.append({"stage": "universe", "market": MARKET, "error": "empty_futures_universe"})
    if not styles:
        errors.append({"stage": "strategy", "market": MARKET, "error": "empty_strategy_styles"})

    for style_name, style_config in styles.items():
        style = dict(style_config or {})
        style.setdefault("name", style_name)
        if not _style_is_active(style):
            continue
        if not _style_allows_session(style, now):
            continue
        for symbol in universe:
            if not _style_allows_symbol(style, symbol):
                continue
            bars, bar_cadence, latest_bar_time = _bars_for_cadence(reader, symbol, date, cadence_value)
            if not bars:
                errors.append({
                    "stage": "data",
                    "symbol": symbol,
                    "style": style_name,
                    "cadence": cadence_value,
                    "error": "missing_intraday_bars" if cadence_value != "daily" else "missing_daily_bars",
                })
                continue
            positions = _positions_by_key(position_snapshot)
            existing_position = positions.get(_position_key(style_name, symbol))
            force_flatten = bool(existing_position) and _should_flatten_no_overnight(style, now)
            if cadence_value != "daily":
                fresh, age_minutes = _is_intraday_bar_fresh(
                    latest_bar_time,
                    now=now,
                    max_age_minutes=max_intraday_bar_age_minutes,
                )
                if not fresh and not force_flatten:
                    errors.append({
                        "stage": "data",
                        "symbol": symbol,
                        "style": style_name,
                        "cadence": cadence_value,
                        "bar_time": latest_bar_time,
                        "bar_age_minutes": age_minutes,
                        "max_age_minutes": max_intraday_bar_age_minutes,
                        "error": "stale_intraday_bar",
                    })
                    continue
            rollover_blocked, days_to_contract_month = _contract_inside_rollover_guard(symbol, date, style)
            if rollover_blocked and not existing_position:
                errors.append({
                    "stage": "risk",
                    "symbol": symbol,
                    "style": style_name,
                    "cadence": cadence_value,
                    "days_to_contract_month_start": days_to_contract_month,
                    "error": "contract_rollover_guard",
                })
                continue
            if force_flatten and existing_position:
                flatten_side = _opposite_side_for_position(existing_position)
                signal = {
                    "action": flatten_side,
                    "side": flatten_side,
                    "price": _safe_float((bars[-1] if bars else {}).get("close"), 0.0),
                    "reason": "flatten_no_overnight",
                }
            else:
                signal = generate_style_signal(symbol, bars, style)
            if signal.get("action") == "hold":
                continue
            price = _safe_float(signal.get("price"), 0.0)
            if price <= 0:
                errors.append({"stage": "signal", "symbol": symbol, "style": style_name, "error": "invalid_price"})
                continue
            try:
                rule = get_contract_rule(symbol)
                quantity = abs(_safe_int(existing_position.get("net_qty"), 0)) if force_flatten and existing_position else _quantity_for_style(symbol=symbol, price=price, capital=capital, style=style)
            except Exception as exc:
                errors.append({"stage": "risk", "symbol": symbol, "style": style_name, "error": str(exc)})
                continue
            period_key = _order_period_key(date, bar_cadence, latest_bar_time)
            intent = "flatten_no_overnight" if force_flatten else "open_or_reverse"
            suffix = "-flatten" if force_flatten else ""
            order_id = f"SIM-CNF-{style_name}-{symbol}-{period_key}{suffix}".replace("/", "-")
            order = {
                "order_id": order_id,
                "symbol": symbol,
                "ts_code": symbol,
                "side": signal.get("side", signal.get("action", "buy")),
                "quantity": quantity,
                "price": price,
                "strategy_name": style_name,
                "market": MARKET,
                "capital_layer": "simulated",
                "account_type": "simulated",
                "contract_multiplier": rule.contract_multiplier,
                "cadence": bar_cadence,
                "bar_time": latest_bar_time,
                "intent": intent,
                "bar_volume": _safe_float((bars[-1] if bars else {}).get("volume"), 0.0),
                "previous_close": _safe_float((bars[-2] if len(bars) >= 2 else {}).get("close"), price),
            }
            if _has_repeated_same_side_exposure(
                signals_dir,
                date=date,
                style_name=style_name,
                symbol=symbol,
                side=str(order["side"]),
            ):
                errors.append({
                    "stage": "risk",
                    "symbol": symbol,
                    "style": style_name,
                    "cadence": bar_cadence,
                    "bar_time": latest_bar_time,
                    "side": order["side"],
                    "error": "repeated_same_side_exposure",
                })
                continue
            existing_qty = _safe_int((existing_position or {}).get("net_qty"), 0)
            is_reducing = existing_qty != 0 and (existing_qty > 0) != (_side_sign(str(order["side"])) > 0)
            if not is_reducing:
                projected_cost = estimate_order_cost(symbol=symbol, side=str(order["side"]), quantity=quantity, price=price)
                current_margin = _style_margin_used(position_snapshot, style_name)
                margin_cap = capital * min(max(_safe_float(style.get("max_margin_usage"), 0.20), 0.01), 0.80)
                if current_margin + projected_cost.margin_required > margin_cap:
                    errors.append({
                        "stage": "risk",
                        "symbol": symbol,
                        "style": style_name,
                        "cadence": bar_cadence,
                        "bar_time": latest_bar_time,
                        "current_margin_required": round(current_margin, 6),
                        "projected_margin_required": round(projected_cost.margin_required, 6),
                        "margin_cap": round(margin_cap, 6),
                        "error": "margin_cap_exceeded",
                    })
                    continue
            previous_opposite = _latest_opposite_fill(
                signals_dir,
                date=date,
                style_name=style_name,
                symbol=symbol,
                side=str(order["side"]),
            )
            receipt_obj = execute_sim_order(
                order=order,
                market=MARKET,
                account={"account": f"cn_futures_sim_{style_name}", "capital_layer": "simulated", "account_type": "simulated"},
                config={
                    "fee_mode": "round_trip_estimate",
                    "style": style_name,
                    "slippage_bps": _safe_float(style.get("slippage_bps"), 2.0),
                    "volume_participation": _safe_float(style.get("volume_participation"), 0.05),
                },
            )
            receipt = {
                "status": receipt_obj.status,
                "filled_qty": receipt_obj.filled_qty,
                "avg_price": receipt_obj.avg_price,
                "fee": receipt_obj.fee,
                "message": receipt_obj.message,
                "capital_layer": receipt_obj.capital_layer,
                "account_type": receipt_obj.account_type,
                "order_id": receipt_obj.order_id,
                "market": receipt_obj.market,
                "raw_response": receipt_obj.raw_response,
            }
            performance = _realized_pnl_from_reversal(
                previous=previous_opposite,
                side=str(order["side"]),
                receipt=receipt,
                rule_multiplier=rule.contract_multiplier,
            )
            card = _signal_card(
                date=date,
                cadence=bar_cadence,
                latest_bar_time=latest_bar_time,
                style_name=style_name,
                symbol=symbol,
                order=order,
                receipt=receipt,
                signal=signal,
            )
            signal_result = _write_filled_signal(signals_dir, card, receipt)
            position_snapshot = _update_position_snapshot(
                signals_dir,
                date=date,
                style_name=style_name,
                symbol=symbol,
                order=order,
                receipt=receipt,
                performance=performance,
            )
            records.append(
                {
                    "date": date,
                    "market": MARKET,
                    "cadence": bar_cadence,
                    "bar_time": latest_bar_time,
                    "style": style_name,
                    "symbol": symbol,
                    "signal": signal,
                    "order": order,
                    "receipt": receipt,
                    "performance": performance,
                    "signal_card": card,
                    "signal_result": signal_result,
                    "capital_layer": "simulated",
                    "account_type": "simulated",
                }
            )

    review = append_review(date=date, market=MARKET, records=records, errors=errors, path=review_path)
    return {
        "market": MARKET,
        "reader_market": READER_MARKET,
        "date": date,
        "cadence": cadence_value,
        "capital_layer": "simulated",
        "account_type": "simulated",
        "state": "degraded" if errors else "ok",
        "universe_count": len(universe),
        "style_count": len(styles),
        "record_count": len(records),
        "filled_count": sum(1 for record in records if record["receipt"].get("status") == "filled"),
        "records": records,
        "errors": errors,
        "review": review,
        "real_trading_enabled": False,
        "generated_at": _now_iso(),
        "max_intraday_bar_age_minutes": max_intraday_bar_age_minutes,
    }


__all__ = ["run_multi_style_simulation"]
