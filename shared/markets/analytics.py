#!/usr/bin/env python3
"""Small analytics helpers for local shadow/simulated market tools."""

from __future__ import annotations

from math import sqrt
from typing import Any


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return float(default)
    return result if result == result else float(default)


def date_key(value: Any, *, keep_time: bool = False) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if "T" in raw or " " in raw:
        date_part, time_part = (raw.split("T", 1)[0], raw.split("T", 1)[1] if "T" in raw else raw.split(" ", 1)[1])
    else:
        date_part, time_part = raw, ""
    if "-" in date_part:
        parts = date_part.split("-")
        date_part = f"{parts[0].zfill(4)}{parts[1].zfill(2)}{parts[2].zfill(2)}" if len(parts) >= 3 else date_part
    else:
        compact = "".join(ch for ch in date_part if ch.isdigit())
        date_part = compact.zfill(8) if compact else date_part
    if keep_time and time_part:
        time_key = "".join(ch for ch in time_part.split(".")[0] if ch.isdigit())
        return f"{date_part}T{time_key}"
    return date_part


def close_series(rows: list[dict[str, Any]]) -> list[tuple[str, float]]:
    series: list[tuple[str, float]] = []
    has_intraday = any("T" in str(row.get("trade_date") or row.get("date") or row.get("timestamp") or "") for row in rows)
    for row in rows:
        close = safe_float(row.get("adjusted_close", row.get("close")))
        key = date_key(row.get("trade_date") or row.get("date") or row.get("timestamp"), keep_time=has_intraday)
        if key and close > 0:
            series.append((key, close))
    return sorted(series, key=lambda item: item[0])


def returns_from_bars(rows: list[dict[str, Any]]) -> dict[str, float]:
    series = close_series(rows)
    result: dict[str, float] = {}
    for (prev_date, prev_close), (date, close) in zip(series, series[1:]):
        del prev_date
        if prev_close > 0:
            result[date] = close / prev_close - 1.0
    return result


def volatility(returns: list[float]) -> float:
    if len(returns) < 2:
        return 0.0
    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
    return sqrt(max(variance, 0.0))


def pearson(left: list[float], right: list[float]) -> float:
    if len(left) < 2 or len(left) != len(right):
        return 0.0
    left_mean = sum(left) / len(left)
    right_mean = sum(right) / len(right)
    numerator = sum((a - left_mean) * (b - right_mean) for a, b in zip(left, right))
    left_var = sum((a - left_mean) ** 2 for a in left)
    right_var = sum((b - right_mean) ** 2 for b in right)
    denom = sqrt(left_var * right_var)
    if denom <= 0:
        return 0.0
    return max(-1.0, min(1.0, numerator / denom))


def correlation_matrix(bars_by_symbol: dict[str, list[dict[str, Any]]]) -> dict[str, dict[str, float]]:
    returns = {symbol: returns_from_bars(rows) for symbol, rows in bars_by_symbol.items()}
    symbols = sorted(returns)
    matrix: dict[str, dict[str, float]] = {}
    for left in symbols:
        matrix[left] = {}
        for right in symbols:
            common_dates = sorted(set(returns[left]).intersection(returns[right]))
            if left == right:
                corr = 1.0
            else:
                corr = pearson(
                    [returns[left][date] for date in common_dates],
                    [returns[right][date] for date in common_dates],
                )
            matrix[left][right] = round(corr, 6)
    return matrix


def max_abs_correlation(symbol: str, peers: list[str], matrix: dict[str, dict[str, float]]) -> float:
    values = [abs(matrix.get(symbol, {}).get(peer, 0.0)) for peer in peers if peer != symbol]
    return max(values) if values else 0.0

