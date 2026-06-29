#!/usr/bin/env python3
"""持仓监控 — 基于统一 Position schema 检查止损/回撤/时间退出/regime。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from shared.accounting.position_schema import Position, coerce_position

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]

_DEFAULT_LIMITS: dict[str, Any] = {
    "stop_loss": {"pct": -0.08, "trailing_pct": -0.12},
    "drawdown": {"portfolio_max": -0.10, "single_max": -0.15},
    "time_exit_days": 30,
}

_LIMITS_PATH = Path(__file__).resolve().parent / "risk_limits.yaml"


def _load_limits() -> dict[str, Any]:
    if yaml is None:
        return dict(_DEFAULT_LIMITS)
    try:
        with open(_LIMITS_PATH, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if isinstance(data, dict):
            merged = dict(_DEFAULT_LIMITS)
            for key, value in data.items():
                if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
                    merged[key] = {**merged[key], **value}
                else:
                    merged[key] = value
            return merged
    except (OSError, yaml.YAMLError):
        pass
    return dict(_DEFAULT_LIMITS)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
        return parsed if parsed == parsed else default
    except (TypeError, ValueError):
        return default


def _parse_date(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    raw_value = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(raw_value, fmt)
        except ValueError:
            continue
    return None


def _position_signal(
    position: Position,
    action: str,
    reason: str,
    severity: str,
) -> dict[str, Any]:
    return {
        "ts_code": position.ts_code,
        "action": action,
        "reason": reason,
        "severity": severity,
        "capital_layer": position.capital_layer,
        "sellable_quantity": position.sellable_quantity,
    }


def _iter_positions(
    positions: list[Position] | list[Mapping[str, Any]],
) -> list[Position]:
    normalized: list[Position] = []
    for raw_position in positions or []:
        try:
            normalized.append(coerce_position(raw_position))
        except ValueError:
            continue
    return normalized


def check_positions(
    positions: list[Position] | list[Mapping[str, Any]],
    current_prices: dict[str, float] | None = None,
    regime: str | None = None,
    portfolio_high_water: float | None = None,
    portfolio_current_value: float | None = None,
) -> list[dict[str, Any]]:
    """基于统一 Position schema 扫描持仓风险。

    缺少成本价、当前价、入场日期或 thesis 时, 对应规则保守不触发。
    """
    normalized_positions = _iter_positions(positions)
    if not normalized_positions:
        return []

    if current_prices is None:
        current_prices = {}

    limits = _load_limits()
    signals: list[dict[str, Any]] = []
    now = datetime.now()

    stop_pct = _safe_float(limits.get("stop_loss", {}).get("pct", -0.08))
    trailing_pct = _safe_float(limits.get("stop_loss", {}).get("trailing_pct", -0.12))
    single_dd_max = _safe_float(limits.get("drawdown", {}).get("single_max", -0.15))
    portfolio_dd_max = _safe_float(limits.get("drawdown", {}).get("portfolio_max", -0.10))
    time_exit_days = int(limits.get("time_exit_days", 30))

    if portfolio_high_water and portfolio_current_value:
        port_dd = (
            (portfolio_current_value - portfolio_high_water) / portfolio_high_water
            if portfolio_high_water
            else 0.0
        )
        if port_dd < portfolio_dd_max:
            signals.append({
                "ts_code": "PORTFOLIO",
                "action": "drawdown",
                "reason": f"组合回撤 {port_dd:.4f} < {portfolio_dd_max:.4f}, 建议全面减仓评估",
                "severity": "high",
                "capital_layer": "all",
                "sellable_quantity": 0,
            })

    for position in normalized_positions:
        if not position.ts_code:
            continue

        cost = _safe_float(position.avg_price)
        current_price = _safe_float(current_prices.get(position.ts_code))
        high_price = _safe_float(position.high_price, cost)
        entry_date = _parse_date(position.entry_date)
        hold_days = (now - entry_date).days if entry_date else None
        thesis = str(position.thesis or "").lower()

        pos_signals: list[dict[str, Any]] = []

        if cost > 0 and current_price > 0:
            pnl_pct = (current_price - cost) / cost
            if pnl_pct <= stop_pct:
                pos_signals.append(_position_signal(
                    position,
                    "stop_loss",
                    f"止损: {position.ts_code} 亏损 {pnl_pct:.4f} ≤ {stop_pct:.4f}",
                    "high",
                ))

            if high_price > 0:
                trailing_dd = (current_price - high_price) / high_price
                if trailing_dd <= trailing_pct:
                    pos_signals.append(_position_signal(
                        position,
                        "trailing_stop",
                        f"移动止损: {position.ts_code} 从高点回撤 {trailing_dd:.4f} ≤ {trailing_pct:.4f}",
                        "high",
                    ))

            if pnl_pct < single_dd_max:
                pos_signals.append(_position_signal(
                    position,
                    "drawdown",
                    f"个股回撤: {position.ts_code} 亏损 {pnl_pct:.4f} < {single_dd_max:.4f}",
                    "medium",
                ))

        if hold_days is not None and hold_days >= time_exit_days:
            pos_signals.append(_position_signal(
                position,
                "time_exit",
                f"时间退出: {position.ts_code} 持仓 {hold_days} 天 ≥ {time_exit_days} 天, 需评估逻辑是否仍成立",
                "medium",
            ))

        if regime and thesis:
            regime_keywords = {
                "growth": ["growth", "增长", "扩张"],
                "recession": ["recession", "衰退", "收缩"],
                "inflation": ["inflation", "通胀"],
                "deflation": ["deflation", "通缩"],
            }
            opposite_regimes = {
                key: value
                for key, value in regime_keywords.items()
                if key != regime.lower()
            }
            for opposite_regime, keywords in opposite_regimes.items():
                if any(keyword in thesis for keyword in keywords):
                    pos_signals.append(_position_signal(
                        position,
                        "regime_change",
                        f"Regime 变化: 当前={regime}, 但 thesis 含 {opposite_regime} 关键词, 逻辑可能失效",
                        "medium",
                    ))
                    break

        if pos_signals:
            severity_order = {"high": 0, "medium": 1, "low": 2}
            pos_signals.sort(key=lambda signal: severity_order.get(signal.get("severity", "low"), 2))
            signals.extend(pos_signals)
            continue

        hold_days_text = hold_days if hold_days is not None else "未知"
        signals.append(_position_signal(
            position,
            "hold",
            f"持仓正常: {position.ts_code} 可卖 {position.sellable_quantity}, 持仓 {hold_days_text} 天",
            "low",
        ))

    return signals


def filter_actions(signals: list[dict[str, Any]], actions: list[str] | None = None) -> list[dict[str, Any]]:
    """筛选需要动作的信号。"""
    if actions is None:
        actions = ["stop_loss", "trailing_stop", "drawdown", "time_exit", "regime_change"]
    return [signal for signal in signals if signal.get("action") in actions]


if __name__ == "__main__":
    import json

    test_positions = [
        Position(
            ts_code="600519.SH",
            quantity=100,
            sellable_quantity=100,
            avg_price=1800.0,
            cost_basis=180000.0,
            entry_date="2026-05-01",
            high_price=1900.0,
            thesis="growth regime, 消费升级",
            capital_layer="shadow",
        ),
        Position(
            ts_code="000858.SZ",
            quantity=100,
            sellable_quantity=100,
            avg_price=150.0,
            cost_basis=15000.0,
            entry_date="2026-06-20",
            high_price=155.0,
            thesis="growth",
            capital_layer="shadow",
        ),
    ]
    test_prices = {"600519.SH": 1650.0, "000858.SZ": 148.0}
    result = check_positions(test_positions, test_prices, regime="recession")
    print(json.dumps(result, ensure_ascii=False, indent=2))
