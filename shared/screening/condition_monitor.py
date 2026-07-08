#!/usr/bin/env python3
"""条件监控 — 盘中5min K线检查条件触发。

不实时全量扫描, 只检查已生成条件的触发状态。

check_conditions(conditions, bars_5min) → list[triggered]
"""
from __future__ import annotations

from datetime import datetime
from typing import Any


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        f = float(v)
        return f if f == f else default
    except (TypeError, ValueError):
        return default


def _normalize_symbol(ts_code: Any) -> str:
    raw = str(ts_code or "").strip()
    return raw.split(".", 1)[0] if "." in raw else raw


def _condition_market(condition: dict[str, Any]) -> str:
    market = str(condition.get("market") or "").strip()
    if market:
        return market
    ts_code = str(condition.get("ts_code") or "").strip().upper()
    if ts_code.endswith((".SH", ".SZ", ".BJ")):
        return "Ashare"
    if ts_code.endswith(("USDT", "USD", "PERP")):
        return "Crypto"
    return "Ashare"


def _normalize_intraday_bar(row: dict[str, Any], ts_code: str) -> dict[str, Any]:
    bar_time = str(row.get("bar_time") or row.get("trade_time") or row.get("time") or "")
    open_price = _safe_float(row.get("open"))
    high = _safe_float(row.get("high"))
    low = _safe_float(row.get("low"))
    close = _safe_float(row.get("close"))
    prev_close = _safe_float(
        row.get("pre_close", row.get("prev_close", row.get("previous_close"))),
        0.0,
    )
    pct_chg = _safe_float(row.get("pct_chg"), 0.0)
    if abs(pct_chg) < 1e-9 and prev_close > 0 and close > 0:
        pct_chg = ((close - prev_close) / prev_close) * 100.0
    return {
        "ts_code": row.get("ts_code") or row.get("symbol") or ts_code,
        "time": bar_time,
        "bar_time": bar_time,
        "trade_time": bar_time,
        "trade_date": row.get("trade_date", ""),
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "vol": _safe_float(row.get("vol", row.get("volume"))),
        "volume": _safe_float(row.get("volume", row.get("vol"))),
        "amount": _safe_float(row.get("amount")),
        "pct_chg": pct_chg,
        "pre_close": prev_close,
    }


def _get_5min_bars(ts_code: str, date: str) -> list[dict[str, Any]]:
    """Load 5-minute bars through the TradingAgent data facade."""
    try:
        from shared.data.reader import TradingagentDataReader

        reader = TradingagentDataReader()
        symbol = _normalize_symbol(ts_code)
        if not symbol:
            return []
        rows = reader.get_bars_intraday("Ashare", symbol, "5min", date, date)
        if not rows:
            rows = reader.get_bars_intraday("Ashare", symbol, "5m", date, date)
        bars = [
            _normalize_intraday_bar(row, ts_code)
            for row in rows
            if isinstance(row, dict)
        ]
        return [bar for bar in bars if bar.get("trade_time")]
    except Exception:
        return []


def _get_condition_bars(condition: dict[str, Any], date: str) -> list[dict[str, Any]]:
    try:
        from shared.data.reader import TradingagentDataReader

        reader = TradingagentDataReader()
        market = _condition_market(condition)
        ts_code = str(condition.get("ts_code") or "").strip()
        symbol = _normalize_symbol(ts_code)
        if not symbol:
            return []
        rows = reader.get_bars_intraday(market, symbol, "5min", date, date)
        if not rows:
            rows = reader.get_bars_intraday(market, symbol, "5m", date, date)
        bars = [
            _normalize_intraday_bar(row, ts_code)
            for row in rows
            if isinstance(row, dict)
        ]
        return [bar for bar in bars if bar.get("trade_time")]
    except Exception:
        return _get_5min_bars(str(condition.get("ts_code") or ""), date)


def _check_breakout(condition: dict[str, Any], bars: list[dict[str, Any]]) -> dict[str, Any] | None:
    """检查突破条件 — 当日最高价 > 触发价。"""
    trigger_price = _safe_float(condition.get("trigger_price"))
    if trigger_price < 1e-9:
        return None

    for bar in bars:
        if not isinstance(bar, dict):
            continue
        high = _safe_float(bar.get("high", 0.0))
        if high >= trigger_price:
            return {
                "condition_id": condition.get("ts_code", "") + "_breakout",
                "ts_code": condition.get("ts_code"),
                "type": "breakout",
                "triggered_at": bar.get("trade_time", ""),
                "trigger_price": trigger_price,
                "bar_price": high,
                "direction": "long",
                "scores": condition.get("scores", {}),
                "description": condition.get("description", ""),
            }
    return None


def _check_pullback(condition: dict[str, Any], bars: list[dict[str, Any]]) -> dict[str, Any] | None:
    """检查回踩条件 — 价格回到 MA20 附近 (±2%)。"""
    trigger_price = _safe_float(condition.get("trigger_price"))
    if trigger_price < 1e-9:
        return None

    params = condition.get("params", {})
    band_pct = _safe_float(params.get("band_pct", 0.02), 0.02)
    lower = trigger_price * (1 - band_pct)
    upper = trigger_price * (1 + band_pct)

    for bar in bars:
        if not isinstance(bar, dict):
            continue
        low = _safe_float(bar.get("low", 0.0))
        close = _safe_float(bar.get("close", 0.0))
        # 价格触及回踩区间
        if low <= upper and close >= lower:
            return {
                "condition_id": condition.get("ts_code", "") + "_pullback",
                "ts_code": condition.get("ts_code"),
                "type": "pullback",
                "triggered_at": bar.get("trade_time", ""),
                "trigger_price": trigger_price,
                "bar_price": close,
                "direction": "long",
                "scores": condition.get("scores", {}),
                "description": condition.get("description", ""),
            }
    return None


def _check_event(condition: dict[str, Any], bars: list[dict[str, Any]]) -> dict[str, Any] | None:
    """检查事件条件 — 事件触发即激活 (不限价)。"""
    # 事件条件生成时即已触发, 只要当日有行情就算触发
    if bars:
        first_bar = bars[-1] if isinstance(bars[-1], dict) else {}
        return {
            "condition_id": condition.get("ts_code", "") + "_event",
            "ts_code": condition.get("ts_code"),
            "type": "event",
            "triggered_at": first_bar.get("trade_time", ""),
            "trigger_price": _safe_float(first_bar.get("open", 0.0)),
            "bar_price": _safe_float(first_bar.get("close", 0.0)),
            "direction": "long",
            "scores": condition.get("scores", {}),
            "description": condition.get("description", ""),
        }
    return None


def _check_value(condition: dict[str, Any], bars: list[dict[str, Any]]) -> dict[str, Any] | None:
    """检查价值条件 — 价值条件为长期条件, 任意时刻可触发。"""
    if bars:
        last_bar = bars[0] if isinstance(bars[0], dict) else {}
        return {
            "condition_id": condition.get("ts_code", "") + "_value",
            "ts_code": condition.get("ts_code"),
            "type": "value",
            "triggered_at": last_bar.get("trade_time", ""),
            "trigger_price": _safe_float(last_bar.get("close", 0.0)),
            "bar_price": _safe_float(last_bar.get("close", 0.0)),
            "direction": "long",
            "scores": condition.get("scores", {}),
            "description": condition.get("description", ""),
        }
    return None


def _check_rotation(condition: dict[str, Any], bars: list[dict[str, Any]]) -> dict[str, Any] | None:
    """检查轮动条件 — 板块轮动信号 + 当日资金确认。"""
    if not bars:
        return None
    # 任意 bar 涨幅 > 1% 确认轮动
    for bar in bars:
        if not isinstance(bar, dict):
            continue
        pct_chg = _safe_float(bar.get("pct_chg", 0.0))
        if pct_chg > 1.0:
            return {
                "condition_id": condition.get("ts_code", "") + "_rotation",
                "ts_code": condition.get("ts_code"),
                "type": "rotation",
                "triggered_at": bar.get("trade_time", ""),
                "trigger_price": _safe_float(bar.get("close", 0.0)),
                "bar_price": _safe_float(bar.get("close", 0.0)),
                "direction": "long",
                "scores": condition.get("scores", {}),
                "description": condition.get("description", ""),
            }
    return None


_CHECK_FUNCS = {
    "breakout": _check_breakout,
    "pullback": _check_pullback,
    "event": _check_event,
    "value": _check_value,
    "rotation": _check_rotation,
}


def _is_expired(condition: dict[str, Any], date: str) -> bool:
    """检查条件是否过期。"""
    valid_until = condition.get("valid_until", "")
    if not valid_until:
        return False
    try:
        return date > valid_until
    except TypeError:
        return False


def _bar_trade_time(bar: dict[str, Any]) -> str:
    return str(bar.get("trade_time") or bar.get("time") or "")


def _fill_price_from_bar(condition: dict[str, Any], trigger: dict[str, Any], bar: dict[str, Any]) -> tuple[bool, float, str]:
    trigger_price = _safe_float(trigger.get("trigger_price", condition.get("trigger_price")), 0.0)
    low = _safe_float(bar.get("low"), 0.0)
    high = _safe_float(bar.get("high"), 0.0)
    close = _safe_float(bar.get("close"), 0.0)
    open_price = _safe_float(bar.get("open"), 0.0)
    cond_type = str(condition.get("type") or "")

    if cond_type in {"breakout", "pullback"} and trigger_price > 0:
        if low <= trigger_price <= high:
            return True, trigger_price, "trigger_price_inside_bar_range"
        if close > 0:
            return False, close, "triggered_but_trigger_price_not_reached_inside_bar"
        return False, 0.0, "triggered_but_missing_positive_bar_close"

    for candidate, reason in (
        (trigger_price, "trigger_price_available"),
        (open_price, "open_price_fallback"),
        (close, "close_price_fallback"),
    ):
        if candidate > 0:
            return True, candidate, reason
    return False, 0.0, "missing_fill_price"


def _replay_trigger(
    condition: dict[str, Any],
    trigger: dict[str, Any],
    bars: list[dict[str, Any]],
) -> dict[str, Any]:
    triggered_at = str(trigger.get("triggered_at") or "")
    matched_bar: dict[str, Any] | None = None
    for bar in bars:
        if not isinstance(bar, dict):
            continue
        if triggered_at and _bar_trade_time(bar) != triggered_at:
            continue
        matched_bar = bar
        break
    if matched_bar is None and bars:
        matched_bar = bars[-1]

    fillable = False
    fill_price = 0.0
    reason = "missing_replay_bar"
    if matched_bar is not None:
        fillable, fill_price, reason = _fill_price_from_bar(condition, trigger, matched_bar)

    replay = dict(trigger)
    replay.update(
        {
            "replay_status": "filled" if fillable else "missed",
            "replay_fillable": fillable,
            "replay_fill_price": round(fill_price, 4) if fill_price > 0 else 0.0,
            "replay_reason": reason,
            "replay_bar_time": _bar_trade_time(matched_bar or {}),
            "replay_bar_open": _safe_float((matched_bar or {}).get("open"), 0.0),
            "replay_bar_high": _safe_float((matched_bar or {}).get("high"), 0.0),
            "replay_bar_low": _safe_float((matched_bar or {}).get("low"), 0.0),
            "replay_bar_close": _safe_float((matched_bar or {}).get("close"), 0.0),
            "replay_valid_until": condition.get("valid_until", ""),
        }
    )
    return replay


def check_conditions(
    conditions: list[dict[str, Any]],
    date: str | None = None,
    bars_map: dict[str, list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """检查条件是否触发 (基于5min K线)。

    Args:
        conditions: 条件列表 (from generate_conditions)
        date: 日期 (YYYYMMDD), 默认今天
        bars_map: 预加载的5min K线 {ts_code: bars}, 默认自动加载

    Returns:
        触发的条件列表 [{condition_id, ts_code, type, triggered_at, ...}]
    """
    if date is None:
        date = datetime.now().strftime("%Y%m%d")

    if not conditions:
        return []

    triggered: list[dict[str, Any]] = []

    for cond in conditions:
        if not isinstance(cond, dict):
            continue

        # 过期检查
        if _is_expired(cond, date):
            continue

        ts_code = cond.get("ts_code", "")
        cond_type = cond.get("type", "")

        # 获取5min K线
        if bars_map is not None:
            bars = bars_map.get(ts_code, [])
        else:
            bars = _get_condition_bars(cond, date)

        if not bars:
            continue

        check_func = _CHECK_FUNCS.get(cond_type)
        if check_func is None:
            continue

        try:
            result = check_func(cond, bars)
            if result is not None:
                triggered.append(result)
        except Exception:
            continue

    return triggered


def trigger_replay(
    conditions: list[dict[str, Any]],
    date: str | None = None,
    bars_map: dict[str, list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """回放历史触发条件, 验证触发时是否存在可成交价格。"""
    if date is None:
        date = datetime.now().strftime("%Y%m%d")

    if not conditions:
        return []

    condition_map: dict[tuple[str, str], dict[str, Any]] = {}
    for cond in conditions:
        if not isinstance(cond, dict):
            continue
        key = (str(cond.get("ts_code") or ""), str(cond.get("type") or ""))
        if key[0] and key[1]:
            condition_map[key] = cond

    triggered = check_conditions(conditions, date=date, bars_map=bars_map)
    replayed: list[dict[str, Any]] = []
    for trigger in triggered:
        key = (str(trigger.get("ts_code") or ""), str(trigger.get("type") or ""))
        condition = condition_map.get(key)
        if condition is None:
            continue
        if bars_map is not None:
            bars = bars_map.get(key[0], [])
        else:
            bars = _get_condition_bars(condition, date)
        replayed.append(_replay_trigger(condition, trigger, bars))
    return replayed


if __name__ == "__main__":
    import json

    test_conditions = [
        {
            "type": "breakout",
            "ts_code": "600519.SH",
            "date": "20260629",
            "trigger_price": 1800.0,
            "direction": "long",
            "description": "突破20日高点 1800.00",
            "scores": {"combined": 0.65},
            "valid_until": "20260702",
        },
    ]
    triggered = check_conditions(test_conditions, "20260629")
    print(json.dumps(triggered, ensure_ascii=False, indent=2))
