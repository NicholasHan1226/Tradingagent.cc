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


def _minutes_since_session_open(row: dict[str, Any]) -> int | None:
    parsed = _bar_time(row)
    if parsed is None:
        return None
    current = parsed.time()
    if time(9, 30) <= current <= time(11, 30):
        open_time = time(9, 30)
    elif time(13, 0) <= current <= time(15, 0):
        open_time = time(13, 0)
    else:
        return None
    open_dt = datetime.combine(parsed.date(), open_time)
    return int((parsed - open_dt).total_seconds() // 60)


def _is_day_session_bar(row: dict[str, Any]) -> bool:
    parsed = _bar_time(row)
    if parsed is None:
        return False
    current = parsed.time()
    return (time(9, 30) <= current <= time(11, 30)) or (time(13, 0) <= current <= time(15, 0))


def _latest_previous_close(rows: list[dict[str, Any]]) -> float:
    for row in reversed(rows):
        previous_close = _safe_float(row.get("previous_close") or row.get("pre_close") or row.get("reference_price"), 0.0)
        if previous_close > 0:
            return previous_close
    return 0.0


def _opening_gap_pct(rows: list[dict[str, Any]], previous_close: float) -> float:
    if previous_close <= 0:
        return 0.0
    for row in rows:
        if not _is_day_session_bar(row):
            continue
        opening_price = _safe_float(row.get("open") or row.get("close"), 0.0)
        if opening_price > 0:
            return (opening_price / previous_close) - 1.0
    return 0.0


def _recent_range_pct(rows: list[dict[str, Any]], window: int) -> float:
    closes = [_safe_float(row.get("close"), 0.0) for row in rows[-max(2, window):]]
    closes = [close for close in closes if close > 0]
    if len(closes) < 2:
        return 0.0
    latest = closes[-1]
    return ((max(closes) - min(closes)) / latest) if latest > 0 else 0.0


def _directional_consistency(rows: list[dict[str, Any]], window: int, action: str) -> float:
    closes = [_safe_float(row.get("close"), 0.0) for row in rows[-max(2, window + 1):]]
    closes = [close for close in closes if close > 0]
    if len(closes) < 2:
        return 0.0
    diffs = [current - previous for previous, current in zip(closes, closes[1:]) if current != previous]
    if not diffs:
        return 0.0
    if action == "buy":
        aligned = sum(1 for diff in diffs if diff > 0)
    elif action == "sell":
        aligned = sum(1 for diff in diffs if diff < 0)
    else:
        return 0.0
    return aligned / len(diffs)


def _intrabar_reversal_pct(row: dict[str, Any], action: str) -> float:
    close = _safe_float(row.get("close"), 0.0)
    if close <= 0:
        return 0.0
    if action == "buy":
        high = _safe_float(row.get("high"), 0.0)
        return max(0.0, (high - close) / close) if high > 0 else 0.0
    if action == "sell":
        low = _safe_float(row.get("low"), 0.0)
        return max(0.0, (close - low) / close) if low > 0 else 0.0
    return 0.0


def _max_bar_gap_minutes(rows: list[dict[str, Any]], window: int) -> int:
    parsed = [_bar_time(row) for row in rows[-max(2, window + 1):]]
    parsed = [value for value in parsed if value is not None]
    if len(parsed) < 2:
        return 0
    gaps = [
        int((current - previous).total_seconds() // 60)
        for previous, current in zip(parsed, parsed[1:])
        if current >= previous
    ]
    return max(gaps) if gaps else 0


def _body_to_range_ratio(row: dict[str, Any]) -> float:
    open_price = _safe_float(row.get("open"), 0.0)
    close = _safe_float(row.get("close"), 0.0)
    high = _safe_float(row.get("high"), 0.0)
    low = _safe_float(row.get("low"), 0.0)
    if open_price <= 0 or close <= 0 or high <= 0 or low <= 0 or high <= low:
        return 1.0
    return abs(close - open_price) / (high - low)


def _consecutive_aligned_bars(rows: list[dict[str, Any]], action: str) -> int:
    closes = [_safe_float(row.get("close"), 0.0) for row in rows]
    closes = [close for close in closes if close > 0]
    count = 0
    for previous, current in reversed(list(zip(closes, closes[1:]))):
        if action == "buy" and current > previous:
            count += 1
        elif action == "sell" and current < previous:
            count += 1
        else:
            break
    return count


def _late_chase_pct(rows: list[dict[str, Any]], window: int, action: str) -> float:
    recent = rows[-max(2, window + 1):]
    closes = [_safe_float(row.get("close"), 0.0) for row in recent]
    closes = [close for close in closes if close > 0]
    if len(closes) < 2:
        return 0.0
    latest = closes[-1]
    baseline = sum(closes[:-1]) / len(closes[:-1])
    if baseline <= 0:
        return 0.0
    if action == "buy":
        return max(0.0, (latest / baseline) - 1.0)
    if action == "sell":
        return max(0.0, (baseline / latest) - 1.0) if latest > 0 else 0.0
    return 0.0


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
    open_cooldown_minutes = max(0, int(_safe_float(style.get("open_cooldown_minutes"), 15)))
    gap_cooldown_minutes = max(0, int(_safe_float(style.get("gap_cooldown_minutes"), 30)))
    max_open_gap_pct = max(0.0, _safe_float(style.get("max_open_gap_pct"), 0.01))
    min_recent_range_pct = max(0.0, _safe_float(style.get("min_recent_range_pct"), 0.001))
    min_directional_consistency = max(0.0, min(1.0, _safe_float(style.get("min_directional_consistency"), 0.60)))
    max_intrabar_reversal_pct = max(0.0, _safe_float(style.get("max_intrabar_reversal_pct"), 0.002))
    min_signal_to_range_ratio = max(0.0, _safe_float(style.get("min_signal_to_range_ratio"), 0.35))
    max_bar_gap_minutes = max(0, int(_safe_float(style.get("max_bar_gap_minutes"), 7)))
    min_body_to_range_ratio = max(0.0, min(1.0, _safe_float(style.get("min_body_to_range_ratio"), 0.30)))
    min_consecutive_aligned_bars = max(0, int(_safe_float(style.get("min_consecutive_aligned_bars"), 2)))
    max_late_chase_pct = max(0.0, _safe_float(style.get("max_late_chase_pct"), 0.012))
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

    minutes_since_open = _minutes_since_session_open(bars[-1])
    if open_cooldown_minutes > 0 and minutes_since_open is not None and minutes_since_open < open_cooldown_minutes:
        return {
            "symbol": symbol,
            "style": style_name,
            "action": "hold",
            "reason": "opening_cooldown",
            "minutes_since_open": minutes_since_open,
            "open_cooldown_minutes": open_cooldown_minutes,
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

    previous_close = _latest_previous_close(bars)
    gap_pct = _opening_gap_pct(bars, previous_close)
    if (
        gap_cooldown_minutes > 0
        and max_open_gap_pct > 0
        and minutes_since_open is not None
        and minutes_since_open <= gap_cooldown_minutes
        and abs(gap_pct) >= max_open_gap_pct
    ):
        return {
            "symbol": symbol,
            "style": style_name,
            "action": "hold",
            "price": latest,
            "reason": "opening_gap_cooldown",
            "gap_pct": gap_pct,
            "max_open_gap_pct": max_open_gap_pct,
            "minutes_since_open": minutes_since_open,
            "gap_cooldown_minutes": gap_cooldown_minutes,
            "confidence": 0.0,
        }

    recent_range_pct = _recent_range_pct(bars, ma_window)
    if min_recent_range_pct > 0 and recent_range_pct < min_recent_range_pct:
        return {
            "symbol": symbol,
            "style": style_name,
            "action": "hold",
            "price": latest,
            "reason": "low_volatility_filter",
            "recent_range_pct": recent_range_pct,
            "min_recent_range_pct": min_recent_range_pct,
            "confidence": 0.0,
        }

    observed_bar_gap_minutes = _max_bar_gap_minutes(bars, lookback)
    if max_bar_gap_minutes > 0 and observed_bar_gap_minutes > max_bar_gap_minutes:
        return {
            "symbol": symbol,
            "style": style_name,
            "action": "hold",
            "price": latest,
            "reason": "bar_gap_filter",
            "observed_bar_gap_minutes": observed_bar_gap_minutes,
            "max_bar_gap_minutes": max_bar_gap_minutes,
            "confidence": 0.0,
        }

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

    directional_consistency = _directional_consistency(bars, lookback, action)
    if min_directional_consistency > 0 and directional_consistency < min_directional_consistency:
        return {
            "symbol": symbol,
            "style": style_name,
            "action": "hold",
            "price": latest,
            "momentum": momentum,
            "ma_distance": ma_distance,
            "directional_score": directional_score,
            "directional_consistency": directional_consistency,
            "min_directional_consistency": min_directional_consistency,
            "confidence": 0.0,
            "reason": "directional_consistency_filter",
        }

    intrabar_reversal_pct = _intrabar_reversal_pct(bars[-1], action)
    if max_intrabar_reversal_pct > 0 and intrabar_reversal_pct > max_intrabar_reversal_pct:
        return {
            "symbol": symbol,
            "style": style_name,
            "action": "hold",
            "price": latest,
            "momentum": momentum,
            "ma_distance": ma_distance,
            "directional_score": directional_score,
            "intrabar_reversal_pct": intrabar_reversal_pct,
            "max_intrabar_reversal_pct": max_intrabar_reversal_pct,
            "confidence": 0.0,
            "reason": "intrabar_reversal_filter",
        }

    signal_to_range_ratio = abs(directional_score) / recent_range_pct if recent_range_pct > 0 else 0.0
    if min_signal_to_range_ratio > 0 and signal_to_range_ratio < min_signal_to_range_ratio:
        return {
            "symbol": symbol,
            "style": style_name,
            "action": "hold",
            "price": latest,
            "momentum": momentum,
            "ma_distance": ma_distance,
            "directional_score": directional_score,
            "recent_range_pct": recent_range_pct,
            "signal_to_range_ratio": signal_to_range_ratio,
            "min_signal_to_range_ratio": min_signal_to_range_ratio,
            "confidence": 0.0,
            "reason": "signal_noise_filter",
        }

    body_to_range_ratio = _body_to_range_ratio(bars[-1])
    if min_body_to_range_ratio > 0 and body_to_range_ratio < min_body_to_range_ratio:
        return {
            "symbol": symbol,
            "style": style_name,
            "action": "hold",
            "price": latest,
            "body_to_range_ratio": body_to_range_ratio,
            "min_body_to_range_ratio": min_body_to_range_ratio,
            "confidence": 0.0,
            "reason": "body_to_range_filter",
        }

    consecutive_aligned_bars = _consecutive_aligned_bars(bars, action)
    if min_consecutive_aligned_bars > 0 and consecutive_aligned_bars < min_consecutive_aligned_bars:
        return {
            "symbol": symbol,
            "style": style_name,
            "action": "hold",
            "price": latest,
            "consecutive_aligned_bars": consecutive_aligned_bars,
            "min_consecutive_aligned_bars": min_consecutive_aligned_bars,
            "confidence": 0.0,
            "reason": "consecutive_alignment_filter",
        }

    late_chase_pct = _late_chase_pct(bars, lookback, action)
    if max_late_chase_pct > 0 and late_chase_pct > max_late_chase_pct:
        return {
            "symbol": symbol,
            "style": style_name,
            "action": "hold",
            "price": latest,
            "late_chase_pct": late_chase_pct,
            "max_late_chase_pct": max_late_chase_pct,
            "confidence": 0.0,
            "reason": "late_chase_filter",
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
        "directional_consistency": directional_consistency,
        "intrabar_reversal_pct": intrabar_reversal_pct,
        "signal_to_range_ratio": signal_to_range_ratio,
        "body_to_range_ratio": body_to_range_ratio,
        "consecutive_aligned_bars": consecutive_aligned_bars,
        "late_chase_pct": late_chase_pct,
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
