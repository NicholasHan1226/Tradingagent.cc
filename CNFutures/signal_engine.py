#!/usr/bin/env python3
"""Signal generation for CN futures simulation lanes."""

from __future__ import annotations

from datetime import datetime, time
from typing import Any


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if result == result else default
    except (TypeError, ValueError):
        return default


def _latest_close(rows: list[dict[str, Any]]) -> float:
    for row in reversed(rows):
        close = _safe_float(row.get("close"), 0.0)
        if close > 0:
            return close
    return 0.0


def _volume_ratio(rows: list[dict[str, Any]]) -> float:
    if len(rows) < 2:
        return 1.0
    latest = _safe_float(rows[-1].get("volume"), 0.0)
    history = [_safe_float(row.get("volume"), 0.0) for row in rows[:-1]]
    history = [value for value in history if value > 0]
    if latest <= 0 or not history:
        return 1.0
    return latest / (sum(history) / len(history))


def _moving_average(rows: list[dict[str, Any]], window: int) -> float:
    closes = [_safe_float(row.get("close"), 0.0) for row in rows[-max(1, window):]]
    closes = [close for close in closes if close > 0]
    return sum(closes) / len(closes) if closes else 0.0


def _bar_time(row: dict[str, Any]) -> datetime | None:
    raw = str(row.get("bar_time") or row.get("time") or row.get("trade_time") or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace(" ", "T", 1))
    except ValueError:
        return None


def _minutes_to_day_close(row: dict[str, Any]) -> int | None:
    parsed = _bar_time(row)
    if parsed is None:
        return None
    close_dt = datetime.combine(parsed.date(), time(15, 0))
    return int((close_dt - parsed).total_seconds() // 60)


def _is_day_session_bar(row: dict[str, Any]) -> bool:
    parsed = _bar_time(row)
    if parsed is None:
        return False
    current = parsed.time()
    return (time(9, 30) <= current <= time(11, 30)) or (time(13, 0) <= current <= time(15, 0))


def _index_intraday_directional_signal(
    symbol: str,
    bars: list[dict[str, Any]],
    style: dict[str, Any],
) -> dict[str, Any]:
    style_name = str(style.get("name") or "index_intraday_directional")
    lookback = max(2, int(_safe_float(style.get("momentum_lookback_bars"), 3)))
    ma_window = max(lookback + 1, int(_safe_float(style.get("moving_average_bars"), 6)))
    threshold = abs(_safe_float(style.get("signal_threshold"), 0.0025))
    close_guard = int(_safe_float(style.get("flatten_before_session_close_minutes"), 10))
    min_volume_ratio = max(0.0, _safe_float(style.get("min_volume_ratio"), 1.05))
    trend_alignment_required = bool(style.get("trend_alignment_required", True))
    if len(bars) <= max(lookback, ma_window):
        return {"symbol": symbol, "style": style_name, "action": "hold", "reason": "insufficient_intraday_bars", "confidence": 0.0}

    if bool(style.get("day_session_only", True)) and not _is_day_session_bar(bars[-1]):
        return {
            "symbol": symbol,
            "style": style_name,
            "action": "hold",
            "reason": "outside_day_session",
            "confidence": 0.0,
        }

    if bool(style.get("no_overnight", True)):
        minutes_left = _minutes_to_day_close(bars[-1])
        if minutes_left is not None and minutes_left <= close_guard:
            return {
                "symbol": symbol,
                "style": style_name,
                "action": "hold",
                "reason": "session_close_guard",
                "minutes_to_close": minutes_left,
                "confidence": 0.0,
            }

    latest = _latest_close(bars)
    previous = _safe_float(bars[-1 - lookback].get("close"), 0.0)
    average = _moving_average(bars, ma_window)
    if latest <= 0 or previous <= 0 or average <= 0:
        return {"symbol": symbol, "style": style_name, "action": "hold", "reason": "invalid_price", "confidence": 0.0}

    momentum = (latest / previous) - 1.0
    ma_distance = (latest / average) - 1.0
    volume_ratio = _volume_ratio(bars)
    directional_score = (momentum * 0.70) + (ma_distance * 0.30)
    if trend_alignment_required and momentum * ma_distance <= 0:
        return {
            "symbol": symbol,
            "style": style_name,
            "action": "hold",
            "price": latest,
            "momentum": momentum,
            "ma_distance": ma_distance,
            "directional_score": directional_score,
            "volume_ratio": volume_ratio,
            "confidence": 0.0,
            "reason": "trend_alignment_filter",
        }
    if volume_ratio < min_volume_ratio:
        return {
            "symbol": symbol,
            "style": style_name,
            "action": "hold",
            "price": latest,
            "momentum": momentum,
            "ma_distance": ma_distance,
            "directional_score": directional_score,
            "volume_ratio": volume_ratio,
            "min_volume_ratio": min_volume_ratio,
            "confidence": 0.0,
            "reason": "volume_confirmation_filter",
        }
    action = "hold"
    if directional_score >= threshold and momentum > 0:
        action = "buy"
    elif directional_score <= -threshold and momentum < 0:
        action = "sell"

    if action == "hold":
        return {
            "symbol": symbol,
            "style": style_name,
            "action": "hold",
            "price": latest,
            "momentum": momentum,
            "ma_distance": ma_distance,
            "directional_score": directional_score,
            "volume_ratio": volume_ratio,
            "confidence": 0.0,
            "reason": "direction_score_below_threshold",
        }

    confidence = min(0.95, max(0.10, abs(directional_score) / max(threshold, 0.0001) * 0.30))
    if volume_ratio >= 1.15:
        confidence = min(0.98, confidence + 0.08)
    return {
        "symbol": symbol,
        "style": style_name,
        "style_family": "index_intraday_directional",
        "action": action,
        "side": action,
        "price": latest,
        "momentum": momentum,
        "ma_distance": ma_distance,
        "directional_score": directional_score,
        "volume_ratio": volume_ratio,
        "confidence": confidence,
        "reason": "index_intraday_direction_confirmed",
        "prediction_horizon_bars": int(_safe_float(style.get("prediction_horizon_bars"), 3)),
        "no_overnight": bool(style.get("no_overnight", True)),
        "capital_layer": "simulated",
        "account_type": "simulated",
    }


def generate_style_signal(
    symbol: str,
    bars: list[dict[str, Any]],
    style: dict[str, Any],
) -> dict[str, Any]:
    """Generate one simulation signal for a style, or return hold."""

    if str(style.get("style_family") or "").strip().lower() == "index_intraday_directional":
        return _index_intraday_directional_signal(symbol, bars, style)

    if len(bars) < 2:
        return {"symbol": symbol, "action": "hold", "reason": "insufficient_bars", "confidence": 0.0}
    latest = _latest_close(bars)
    previous = _safe_float(bars[-2].get("close"), 0.0)
    if latest <= 0 or previous <= 0:
        return {"symbol": symbol, "action": "hold", "reason": "invalid_price", "confidence": 0.0}

    momentum = (latest / previous) - 1.0
    threshold = abs(_safe_float(style.get("signal_threshold"), 0.01))
    contrarian = bool(style.get("contrarian", False))
    volume_ratio = _volume_ratio(bars)
    style_name = str(style.get("name") or "default")

    action = "hold"
    if momentum >= threshold:
        action = "sell" if contrarian else "buy"
    elif momentum <= -threshold:
        action = "buy" if contrarian else "sell"

    if action == "hold":
        return {
            "symbol": symbol,
            "style": style_name,
            "action": "hold",
            "price": latest,
            "momentum": momentum,
            "volume_ratio": volume_ratio,
            "confidence": 0.0,
            "reason": "below_threshold",
        }

    confidence = min(0.95, max(0.10, abs(momentum) / max(threshold, 0.0001) * 0.35))
    if volume_ratio >= 1.2:
        confidence = min(0.98, confidence + 0.10)
    return {
        "symbol": symbol,
        "style": style_name,
        "action": action,
        "side": action,
        "price": latest,
        "momentum": momentum,
        "volume_ratio": volume_ratio,
        "confidence": confidence,
        "reason": "trend_confirmed" if not contrarian else "mean_reversion_triggered",
        "capital_layer": "simulated",
        "account_type": "simulated",
    }


__all__ = ["generate_style_signal"]
