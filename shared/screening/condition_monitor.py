#!/usr/bin/env python3
"""条件监控 — 盘中5min K线检查条件触发。

不实时全量扫描, 只检查已生成条件的触发状态。

check_conditions(conditions, bars_5min) → list[triggered]
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

_ASHARE_DATA = Path("/opt/investment/Ashare/data")


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        f = float(v)
        return f if f == f else default
    except (TypeError, ValueError):
        return default


def _get_5min_bars(ts_code: str, date: str) -> list[dict[str, Any]]:
    """获取5分钟K线 (placeholder: 从 tushare_cache 读)。"""
    try:
        import json
        bars_file = _ASHARE_DATA / "intraday" / f"{ts_code}_{date}_5min.json"
        if bars_file.exists():
            with open(bars_file, encoding="utf-8") as f:
                bars = json.load(f)
            if isinstance(bars, list):
                return bars
    except (OSError, ValueError, TypeError):
        pass
    return []


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
            bars = _get_5min_bars(ts_code, date)

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
