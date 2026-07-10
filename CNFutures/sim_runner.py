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
from .contract_rules import get_contract_rule, night_session_end_minute, normalize_product
from .margin_model import estimate_order_cost
from .review import append_review
from .session import parse_cn_datetime, session_bar_age_minutes
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


def _distinct_products(symbols: list[str]) -> list[str]:
    products: set[str] = set()
    for symbol in symbols:
        try:
            products.add(normalize_product(symbol))
        except ValueError:
            continue
    return sorted(products)


def _product_or_empty(symbol: str) -> str:
    try:
        return normalize_product(symbol)
    except ValueError:
        return ""


def _style_is_active(style: dict[str, Any]) -> bool:
    status = str(style.get("status") or "").strip().lower()
    if status in {"paused", "deprecated"}:
        return False
    return bool(style.get("enabled", True))


def _inactive_style_reason(style: dict[str, Any]) -> str:
    status = str(style.get("status") or "").strip().lower()
    if status == "deprecated":
        return "style_deprecated"
    if status == "paused":
        return "style_paused"
    if not bool(style.get("enabled", True)):
        return "style_disabled"
    return ""


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


def _session_bucket(now: datetime | None) -> str:
    current = _cn_local_time(now)
    if current is None:
        return "unknown"
    if time(9, 0) <= current <= time(11, 30) or time(13, 0) <= current <= time(15, 0):
        return "day"
    if current >= time(21, 0) or current <= time(2, 30):
        return "night"
    return "closed"


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
    parsed = parse_cn_datetime(value)
    return parsed.replace(tzinfo=None) if parsed is not None else None


def _bar_age_minutes(latest_bar_time: str, now: datetime | None) -> float | None:
    if now is None:
        return None
    return session_bar_age_minutes(latest_bar_time, now)


def _is_intraday_bar_fresh(latest_bar_time: str, *, now: datetime | None, max_age_minutes: float) -> tuple[bool, float | None]:
    age = _bar_age_minutes(latest_bar_time, now)
    if age is None:
        return False, None
    return -5.0 <= age <= max_age_minutes, age


def _local_naive_dt(value: datetime) -> datetime:
    return value.astimezone(CN_TZ).replace(tzinfo=None) if value.tzinfo is not None else value


def _minute_of_day(value: datetime) -> int:
    return value.hour * 60 + value.minute


def _is_after_product_night_close(symbol: str, latest_bar_time: str, now: datetime | None) -> bool:
    """Return true when a stale-looking night bar is actually the product close."""

    if now is None:
        return False
    close_minute = night_session_end_minute(symbol)
    if close_minute is None:
        return False
    bar_dt = _parse_dt(latest_bar_time)
    if bar_dt is None:
        return False
    now_dt = _local_naive_dt(now)
    bar_minute = _minute_of_day(bar_dt)
    now_minute = _minute_of_day(now_dt)
    bar_at_close = close_minute - 5 <= bar_minute <= close_minute
    if not bar_at_close:
        return False
    if close_minute <= 3 * 60:
        return now_dt.date() == bar_dt.date() and now_minute > close_minute
    return (now_dt.date() == bar_dt.date() and now_minute > close_minute) or now_dt.date() > bar_dt.date()


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
    return -min_days <= days_to_month <= min_days, days_to_month


def _first_present(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None


def _enrich_order_from_bar(order: dict[str, Any], bar: dict[str, Any]) -> None:
    mapped = {
        "bid_price": _first_present(bar, "bid_price", "bid1", "best_bid"),
        "ask_price": _first_present(bar, "ask_price", "ask1", "best_ask"),
        "bid_size": _first_present(bar, "bid_size", "bid_volume", "bid1_volume"),
        "ask_size": _first_present(bar, "ask_size", "ask_volume", "ask1_volume"),
        "last_trade_date": _first_present(bar, "last_trade_date"),
        "expiry_date": _first_present(bar, "expiry_date", "expiration_date", "delivery_date"),
    }
    for key, value in mapped.items():
        if value not in (None, ""):
            order[key] = value


def _scenario_tags(symbol: str, signal: dict[str, Any], now: datetime | None) -> dict[str, Any]:
    tags = signal.get("scenario_tags") if isinstance(signal.get("scenario_tags"), dict) else {}
    product = "unknown"
    try:
        product = normalize_product(symbol)
    except ValueError:
        pass
    return {
        "product": product,
        "session": _session_bucket(now),
        "time_bucket": tags.get("time_bucket", "unknown"),
        "direction": signal.get("side") or signal.get("action") or "unknown",
        "volatility_bucket": tags.get("volatility_bucket", "unknown"),
        "volume_bucket": tags.get("volume_bucket", "unknown"),
        "signal_strength_bucket": tags.get("signal_strength_bucket", "unknown"),
    }


def _exit_plan_for_signal(signal: dict[str, Any], style: dict[str, Any]) -> dict[str, Any]:
    plan = signal.get("exit_plan") if isinstance(signal.get("exit_plan"), dict) else {}
    horizon = max(1, _safe_int(plan.get("prediction_horizon_bars") or signal.get("prediction_horizon_bars") or style.get("prediction_horizon_bars"), 3))
    time_stop_bars = max(1, _safe_int(plan.get("time_stop_bars") or style.get("time_stop_bars"), horizon))
    max_hold_bars = max(time_stop_bars, _safe_int(plan.get("max_hold_bars") or style.get("max_hold_bars"), max(horizon, time_stop_bars)))
    return {
        "prediction_horizon_bars": horizon,
        "time_stop_bars": time_stop_bars,
        "max_hold_bars": max_hold_bars,
        "stop_loss_pct": max(0.0, _safe_float(plan.get("stop_loss_pct") if "stop_loss_pct" in plan else style.get("stop_loss_pct"), 0.004)),
        "take_profit_pct": max(0.0, _safe_float(plan.get("take_profit_pct") if "take_profit_pct" in plan else style.get("take_profit_pct"), 0.006)),
        "flatten_before_session_close_minutes": max(0, _safe_int(plan.get("flatten_before_session_close_minutes") or style.get("flatten_before_session_close_minutes"), 10)),
        "no_overnight": bool(plan.get("no_overnight", style.get("no_overnight", True))),
    }


def _forward_outcome_label(bars: list[dict[str, Any]], signal: dict[str, Any], exit_plan: dict[str, Any]) -> dict[str, Any]:
    side = str(signal.get("side") or signal.get("action") or "").lower().strip()
    direction = 1 if side == "buy" else (-1 if side == "sell" else 0)
    entry_price = _safe_float(signal.get("price"), 0.0)
    horizon = max(1, _safe_int(exit_plan.get("prediction_horizon_bars"), 3))
    if direction == 0 or entry_price <= 0:
        return {"status": "unscored", "reason": "not_directional_signal", "prediction_horizon_bars": horizon}
    entry_index = len(bars) - 1
    future_rows = bars[entry_index + 1 : entry_index + 1 + horizon]
    if not future_rows:
        return {
            "status": "pending_future_bars",
            "prediction_horizon_bars": horizon,
            "entry_price": entry_price,
            "direction": side,
        }
    closes = [_safe_float(row.get("close"), 0.0) for row in future_rows]
    closes = [value for value in closes if value > 0]
    if not closes:
        return {"status": "unscored", "reason": "missing_future_close", "prediction_horizon_bars": horizon, "entry_price": entry_price, "direction": side}
    directional_returns = [direction * ((close / entry_price) - 1.0) for close in closes]
    horizon_return = directional_returns[min(len(directional_returns), horizon) - 1]
    max_favorable = max(directional_returns)
    max_adverse = min(directional_returns)
    stop_loss_pct = max(0.0, _safe_float(exit_plan.get("stop_loss_pct"), 0.0))
    take_profit_pct = max(0.0, _safe_float(exit_plan.get("take_profit_pct"), 0.0))
    time_stop_bars = max(1, _safe_int(exit_plan.get("time_stop_bars"), horizon))
    time_stop_index = min(len(directional_returns), time_stop_bars) - 1
    time_stop_return = directional_returns[time_stop_index]
    return {
        "status": "labeled",
        "prediction_horizon_bars": horizon,
        "entry_price": entry_price,
        "direction": side,
        "future_bar_count": len(future_rows),
        "horizon_return_pct": round(horizon_return, 8),
        "time_stop_return_pct": round(time_stop_return, 8),
        "max_favorable_excursion_pct": round(max_favorable, 8),
        "max_adverse_excursion_pct": round(max_adverse, 8),
        "direction_correct": horizon_return > 0,
        "time_stop_positive": time_stop_return > 0,
        "take_profit_hit": bool(take_profit_pct and max_favorable >= take_profit_pct),
        "stop_loss_hit": bool(stop_loss_pct and abs(max_adverse) >= stop_loss_pct),
    }


def _latest_hold_bar_time(holds: list[dict[str, Any]]) -> str:
    for hold in reversed(holds):
        if isinstance(hold, dict) and hold.get("bar_time"):
            return str(hold.get("bar_time"))
    return ""


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
        mark_price = _safe_float(order.get("price"), _safe_float(receipt.get("avg_price"), avg_price))
        contract_multiplier = _safe_int(
            order.get("contract_multiplier") or raw.get("contract_multiplier"), 1
        )
        positions[key] = {
            "style": style_name,
            "strategy_name": style_name,
            "symbol": symbol,
            "net_qty": new_qty,
            "side": _position_side(new_qty),
            "avg_price": avg_price,
            "last_price": _safe_float(receipt.get("avg_price"), _safe_float(order.get("price"), 0.0)),
            "mark_price": mark_price,
            "contract_multiplier": contract_multiplier,
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
        rows = get_bars(READER_MARKET, symbol, INTRADAY_INTERVAL, date, date)
    except TypeError:
        try:
            rows = get_bars(market=READER_MARKET, symbol=symbol, interval=INTRADAY_INTERVAL, start=date, end=date)
        except TypeError:
            rows = get_bars(READER_MARKET, symbol, INTRADAY_INTERVAL)
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
    previous_raw = previous.get("raw_response") if isinstance(previous.get("raw_response"), dict) else {}
    receipt_raw = receipt.get("raw_response") if isinstance(receipt.get("raw_response"), dict) else {}
    previous_fee = _safe_float(previous.get("fee"), 0.0)
    previous_round_trip_fee = _safe_float(previous_raw.get("total_estimated_fee"), 0.0)
    if previous_round_trip_fee > 0 and previous_fee >= previous_round_trip_fee:
        fee = previous_fee
    else:
        close_fee = _safe_float(receipt_raw.get("estimated_close_fee"), 0.0)
        fee = previous_fee + (close_fee if close_fee > 0 else _safe_float(receipt.get("fee"), 0.0))
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


def _realized_pnl_from_position_close(
    *,
    position: dict[str, Any] | None,
    side: str,
    receipt: dict[str, Any],
    rule_multiplier: int,
) -> dict[str, Any]:
    if not position:
        return {}
    net_qty = _safe_int(position.get("net_qty"), 0)
    entry_price = _safe_float(position.get("avg_price"), 0.0)
    exit_price = _safe_float(receipt.get("avg_price"), 0.0)
    closed_qty = min(abs(net_qty), _safe_int(receipt.get("filled_qty"), 0))
    if net_qty == 0 or entry_price <= 0 or exit_price <= 0 or closed_qty <= 0:
        return {}
    current_side = str(side or "").lower().strip()
    gross = 0.0
    if net_qty > 0 and current_side in {"sell", "short"}:
        gross = (exit_price - entry_price) * closed_qty * rule_multiplier
    elif net_qty < 0 and current_side in {"buy", "long"}:
        gross = (entry_price - exit_price) * closed_qty * rule_multiplier
    else:
        return {}
    raw = receipt.get("raw_response") if isinstance(receipt.get("raw_response"), dict) else {}
    fee = _safe_float(raw.get("estimated_close_fee"), 0.0)
    if fee <= 0:
        fee = _safe_float(receipt.get("fee"), 0.0)
    return {
        "realized_pnl": round(gross - fee, 6),
        "gross_pnl": round(gross, 6),
        "round_trip_fee": round(fee, 6),
        "closed_quantity": closed_qty,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "entry_side": _position_side(net_qty),
        "exit_side": current_side,
        "method": "force_flatten_position_close",
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


def _compute_position_pnl_summary(signals_dir: Path) -> dict[str, dict[str, Any]]:
    """Compute realized + mark-to-market unrealized PnL per style from the position snapshot."""
    snapshot = _read_position_snapshot(signals_dir)
    summary: dict[str, dict[str, Any]] = {}
    for position in snapshot.get("positions", []):
        if not isinstance(position, dict):
            continue
        style_name = str(position.get("style") or position.get("strategy_name") or "unknown")
        net_qty = _safe_int(position.get("net_qty"), 0)
        avg_price = _safe_float(position.get("avg_price"), 0.0)
        mark_price = _safe_float(position.get("mark_price"), avg_price)
        multiplier = _safe_int(position.get("contract_multiplier"), 1)
        realized = _safe_float(position.get("realized_pnl"), 0.0)
        unrealized = round((mark_price - avg_price) * net_qty * multiplier, 6)
        item = summary.setdefault(
            style_name,
            {"realized_pnl": 0.0, "unrealized_pnl": 0.0, "total_pnl": 0.0, "open_position_count": 0},
        )
        item["realized_pnl"] = round(item["realized_pnl"] + realized, 6)
        item["unrealized_pnl"] = round(item["unrealized_pnl"] + unrealized, 6)
        item["total_pnl"] = round(item["realized_pnl"] + item["unrealized_pnl"], 6)
        item["open_position_count"] += 1
    return summary


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
        "direct_execution": False,
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
    cn_now = now.astimezone(CN_TZ) if now.tzinfo is not None else now.replace(tzinfo=CN_TZ)
    market_data_date = cn_now.strftime("%Y%m%d")
    config = adapter.get_strategy_config()
    styles = config.get("styles") if isinstance(config.get("styles"), dict) else {}
    account = adapter.get_sim_account()
    capital = _safe_float(account.get("sim_capital") if isinstance(account, dict) else None, 200_000.0)
    errors: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    holds: list[dict[str, Any]] = []
    position_snapshot = _read_position_snapshot(signals_dir)
    session_bucket = _session_bucket(now)
    if cadence_value == "daily":
        universe = adapter.get_universe(date)
    else:
        get_intraday_universe = getattr(adapter, "get_intraday_universe", None)
        universe = get_intraday_universe(market_data_date, interval=INTRADAY_INTERVAL) if callable(get_intraday_universe) else adapter.get_universe(date)
    if not universe:
        errors.append({"stage": "universe", "market": MARKET, "error": "empty_futures_universe"})
    if not styles:
        errors.append({"stage": "strategy", "market": MARKET, "error": "empty_strategy_styles"})
    if cadence_value != "daily" and session_bucket == "closed":
        position_pnl_summary = _compute_position_pnl_summary(signals_dir)
        return {
            "market": MARKET,
            "reader_market": READER_MARKET,
            "date": date,
            "cadence": cadence_value,
            "capital_layer": "simulated",
            "account_type": "simulated",
            "state": "market_closed",
            "session": "closed",
            "universe_count": len(universe),
            "style_count": len(styles),
            "record_count": 0,
            "filled_count": 0,
            "records": [],
            "errors": [],
            "holds": [],
            "hold_count": 0,
            "hold_reason_summary": {},
            "review": {
                "state": "market_closed",
                "append_skipped": True,
                "reason": "closed_session_empty_review_not_persisted",
                "position_pnl_summary": position_pnl_summary,
            },
            "real_trading_enabled": False,
            "generated_at": _now_iso(),
            "max_intraday_bar_age_minutes": max_intraday_bar_age_minutes,
        }

    configured_min_products = max(1, _safe_int(getattr(adapter, "universe_filter", {}).get("min_distinct_products"), 3))
    max_symbols = max(1, _safe_int(getattr(adapter, "universe_filter", {}).get("max_symbols"), 30))
    required_min_products = min(configured_min_products, max_symbols)
    distinct_products = _distinct_products(universe)
    if len(distinct_products) < required_min_products:
        holds.append({
            "stage": "universe",
            "style": "",
            "symbol": "",
            "product": "",
            "cadence": cadence_value,
            "bar_time": "",
            "session": session_bucket,
            "reason": "insufficient_distinct_product_coverage",
            "distinct_products": distinct_products,
            "required_min_distinct_products": required_min_products,
        })
        position_pnl_summary = _compute_position_pnl_summary(signals_dir)
        review = append_review(
            date=date,
            market=MARKET,
            records=[],
            errors=[],
            holds=holds,
            path=review_path,
            position_pnl_summary=position_pnl_summary,
        )
        return {
            "market": MARKET,
            "reader_market": READER_MARKET,
            "date": date,
            "cadence": cadence_value,
            "capital_layer": "simulated",
            "account_type": "simulated",
            "state": "observation_only",
            "universe_count": len(universe),
            "distinct_products": distinct_products,
            "distinct_product_count": len(distinct_products),
            "required_min_distinct_products": required_min_products,
            "style_count": len(styles),
            "record_count": 0,
            "filled_count": 0,
            "records": [],
            "errors": [],
            "holds": holds,
            "hold_count": len(holds),
            "latest_hold_bar_time": "",
            "hold_reason_summary": review.get("hold_reason_summary", {}),
            "review": review,
            "real_trading_enabled": False,
            "generated_at": _now_iso(),
            "max_intraday_bar_age_minutes": max_intraday_bar_age_minutes,
        }

    for style_name, style_config in styles.items():
        style = dict(style_config or {})
        style.setdefault("name", style_name)
        if not _style_is_active(style):
            reason = _inactive_style_reason(style) or "style_inactive"
            holds.append({
                "stage": "style",
                "style": style_name,
                "symbol": "",
                "product": "",
                "cadence": cadence_value,
                "bar_time": "",
                "session": session_bucket,
                "reason": reason,
                "confidence": 0.0,
                "evolution_action": style.get("evolution_action", ""),
                "evolution_reason": style.get("evolution_reason", ""),
            })
            continue
        if not _style_allows_session(style, now):
            holds.append({
                "stage": "style",
                "style": style_name,
                "symbol": "",
                "product": "",
                "cadence": cadence_value,
                "bar_time": "",
                "session": session_bucket,
                "reason": "style_session_not_allowed",
                "confidence": 0.0,
            })
            continue
        for symbol in universe:
            if not _style_allows_symbol(style, symbol):
                continue
            bars, bar_cadence, latest_bar_time = _bars_for_cadence(reader, symbol, market_data_date, cadence_value)
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
                    if _is_after_product_night_close(symbol, latest_bar_time, now):
                        holds.append({
                            "stage": "data",
                            "symbol": symbol,
                            "product": _product_or_empty(symbol),
                            "style": style_name,
                            "cadence": cadence_value,
                            "bar_time": latest_bar_time,
                            "bar_age_minutes": age_minutes,
                            "max_age_minutes": max_intraday_bar_age_minutes,
                            "session": session_bucket,
                            "reason": "product_night_session_closed",
                        })
                        continue
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
                holds.append({
                    "stage": "risk",
                    "symbol": symbol,
                    "product": _product_or_empty(symbol),
                    "style": style_name,
                    "cadence": cadence_value,
                    "days_to_contract_month_start": days_to_contract_month,
                    "reason": "contract_rollover_guard",
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
                holds.append({
                    "stage": "signal",
                    "style": style_name,
                    "symbol": symbol,
                    "product": _product_or_empty(symbol),
                    "cadence": bar_cadence,
                    "bar_time": latest_bar_time,
                    "session": session_bucket,
                    "reason": str(signal.get("reason") or "hold"),
                    "confidence": signal.get("confidence", 0.0),
                })
                continue
            exit_plan = _exit_plan_for_signal(signal, style)
            scenario_tags = _scenario_tags(symbol, signal, now)
            forward_outcome = _forward_outcome_label(bars, signal, exit_plan)
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
                "trade_date": date,
                "scenario_tags": scenario_tags,
                "exit_plan": exit_plan,
            }
            _enrich_order_from_bar(order, bars[-1] if bars else {})
            if _has_repeated_same_side_exposure(
                signals_dir,
                date=date,
                style_name=style_name,
                symbol=symbol,
                side=str(order["side"]),
            ):
                holds.append({
                    "stage": "risk",
                    "symbol": symbol,
                    "product": _product_or_empty(symbol),
                    "style": style_name,
                    "cadence": bar_cadence,
                    "bar_time": latest_bar_time,
                    "side": order["side"],
                    "reason": "repeated_same_side_exposure",
                })
                continue
            existing_qty = _safe_int((existing_position or {}).get("net_qty"), 0)
            is_reducing = existing_qty != 0 and (existing_qty > 0) != (_side_sign(str(order["side"])) > 0)
            if not is_reducing:
                projected_cost = estimate_order_cost(symbol=symbol, side=str(order["side"]), quantity=quantity, price=price)
                current_margin = _style_margin_used(position_snapshot, style_name)
                margin_cap = capital * min(max(_safe_float(style.get("max_margin_usage"), 0.20), 0.01), 0.80)
                if current_margin + projected_cost.margin_required > margin_cap:
                    holds.append({
                        "stage": "risk",
                        "symbol": symbol,
                        "product": _product_or_empty(symbol),
                        "style": style_name,
                        "cadence": bar_cadence,
                        "bar_time": latest_bar_time,
                        "current_margin_required": round(current_margin, 6),
                        "projected_margin_required": round(projected_cost.margin_required, 6),
                        "margin_cap": round(margin_cap, 6),
                        "reason": "margin_cap_exceeded",
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
                    "rollover_min_days_to_expiry": _safe_int(style.get("rollover_min_days_to_expiry"), 0),
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
            if force_flatten and existing_position:
                performance = _realized_pnl_from_position_close(
                    position=existing_position,
                    side=str(order["side"]),
                    receipt=receipt,
                    rule_multiplier=rule.contract_multiplier,
                )
            else:
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
                    "scenario_tags": scenario_tags,
                    "exit_plan": exit_plan,
                    "forward_outcome": forward_outcome,
                    "order": order,
                    "receipt": receipt,
                    "performance": performance,
                    "signal_card": card,
                    "signal_result": signal_result,
                    "capital_layer": "simulated",
                    "account_type": "simulated",
                }
            )

    position_pnl_summary = _compute_position_pnl_summary(signals_dir)
    review = append_review(
        date=date,
        market=MARKET,
        records=records,
        errors=errors,
        holds=holds,
        path=review_path,
        position_pnl_summary=position_pnl_summary,
    )
    return {
        "market": MARKET,
        "reader_market": READER_MARKET,
        "date": date,
        "cadence": cadence_value,
        "capital_layer": "simulated",
        "account_type": "simulated",
        "state": "degraded" if errors else "ok",
        "universe_count": len(universe),
        "distinct_products": distinct_products,
        "distinct_product_count": len(distinct_products),
        "required_min_distinct_products": required_min_products,
        "style_count": len(styles),
        "record_count": len(records),
        "filled_count": sum(1 for record in records if record["receipt"].get("status") == "filled"),
        "records": records,
        "errors": errors,
        "holds": holds,
        "hold_count": len(holds),
        "latest_hold_bar_time": _latest_hold_bar_time(holds),
        "hold_reason_summary": review.get("hold_reason_summary", {}),
        "review": review,
        "real_trading_enabled": False,
        "generated_at": _now_iso(),
        "max_intraday_bar_age_minutes": max_intraday_bar_age_minutes,
    }


__all__ = ["run_multi_style_simulation"]
