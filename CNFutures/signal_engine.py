#!/usr/bin/env python3
"""Signal generation for CN futures simulation lanes."""

from __future__ import annotations

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


def generate_style_signal(
    symbol: str,
    bars: list[dict[str, Any]],
    style: dict[str, Any],
) -> dict[str, Any]:
    """Generate one simulation signal for a style, or return hold."""

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
