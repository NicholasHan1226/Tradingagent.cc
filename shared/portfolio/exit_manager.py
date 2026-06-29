#!/usr/bin/env python3
"""退出管理 — 基于统一 Position schema 的批量退出检查。"""

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
    "time_exit_days": 30,
}

_LIMITS_PATH = Path(__file__).resolve().parent.parent / "risk" / "risk_limits.yaml"


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


def _exit_signal(
    position: Position,
    should_exit: bool,
    exit_type: str,
    reason: str,
    severity: str = "medium",
    suggested_action: str = "",
) -> dict[str, Any]:
    signal = {
        "ts_code": position.ts_code,
        "capital_layer": position.capital_layer,
        "sellable_quantity": position.sellable_quantity,
        "should_exit": should_exit,
        "exit_type": exit_type,
        "reason": reason,
        "severity": severity,
        "suggested_action": suggested_action or ("卖出" if should_exit else "持有"),
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "executable": False,
        "blocked_reason": "",
    }

    if should_exit:
        signal["executable"] = position.sellable_quantity > 0
        if signal["executable"]:
            signal["suggested_action"] = suggested_action or "立即卖出"
        else:
            signal["blocked_reason"] = "sellable_quantity=0, 当前不可执行退出"
            signal["suggested_action"] = "记录退出信号, 待可卖后执行"
    return signal


def check_stop_loss(position: Position | Mapping[str, Any], current_price: float) -> dict[str, Any]:
    """止损检查, 包括成本止损和移动止损。"""
    normalized = coerce_position(position)
    if not normalized.ts_code:
        raise ValueError("position.ts_code is required")

    cost = _safe_float(normalized.avg_price)
    high_price = _safe_float(normalized.high_price, cost)
    price = _safe_float(current_price)
    if cost <= 0 or price <= 0:
        return _exit_signal(normalized, False, "none", "无成本价或当前价数据", "low")

    limits = _load_limits()
    stop_pct = _safe_float(limits.get("stop_loss", {}).get("pct", -0.08))
    trailing_pct = _safe_float(limits.get("stop_loss", {}).get("trailing_pct", -0.12))
    pnl_pct = (price - cost) / cost

    if pnl_pct <= stop_pct:
        return _exit_signal(
            normalized,
            True,
            "stop_loss",
            f"止损触发: 亏损 {pnl_pct:.4f} ≤ {stop_pct:.4f}",
            "high",
            "立即卖出",
        )

    if high_price > 0:
        trailing_dd = (price - high_price) / high_price
        if trailing_dd <= trailing_pct:
            return _exit_signal(
                normalized,
                True,
                "trailing_stop",
                f"移动止损: 从高点 {high_price:.2f} 回撤 {trailing_dd:.4f} ≤ {trailing_pct:.4f}",
                "high",
                "立即卖出",
            )

    trailing_dd = (price - high_price) / high_price if high_price > 0 else 0.0
    return _exit_signal(
        normalized,
        False,
        "none",
        f"未触发止损: 亏损 {pnl_pct:.4f}, 回撤 {trailing_dd:.4f}",
        "low",
    )


def check_take_profit(
    position: Position | Mapping[str, Any],
    current_price: float,
    take_profit_pct: float = 0.20,
) -> dict[str, Any]:
    """止盈检查。"""
    normalized = coerce_position(position)
    if not normalized.ts_code:
        raise ValueError("position.ts_code is required")

    cost = _safe_float(normalized.avg_price)
    price = _safe_float(current_price)
    tp_pct = _safe_float(take_profit_pct, 0.20)
    if cost <= 0 or price <= 0:
        return _exit_signal(normalized, False, "none", "无成本价或当前价数据", "low")

    pnl_pct = (price - cost) / cost
    if pnl_pct >= tp_pct:
        return _exit_signal(
            normalized,
            True,
            "take_profit",
            f"止盈触发: 盈利 {pnl_pct:.4f} ≥ {tp_pct:.4f}",
            "medium",
            "考虑卖出或移动止损",
        )

    if pnl_pct >= tp_pct * 0.8:
        return _exit_signal(
            normalized,
            False,
            "none",
            f"接近止盈: 盈利 {pnl_pct:.4f} (目标 {tp_pct:.4f})",
            "low",
        )

    return _exit_signal(
        normalized,
        False,
        "none",
        f"未触发止盈: 盈利 {pnl_pct:.4f}",
        "low",
    )


def check_time_exit(
    position: Position | Mapping[str, Any],
    time_exit_days: int | None = None,
) -> dict[str, Any]:
    """时间退出检查。"""
    normalized = coerce_position(position)
    if not normalized.ts_code:
        raise ValueError("position.ts_code is required")

    entry_date = _parse_date(normalized.entry_date)
    if entry_date is None:
        return _exit_signal(normalized, False, "none", "无入场日期数据", "low")

    if time_exit_days is None:
        limits = _load_limits()
        time_exit_days = int(limits.get("time_exit_days", 30))

    hold_days = (datetime.now() - entry_date).days
    if hold_days >= time_exit_days:
        return _exit_signal(
            normalized,
            True,
            "time_exit",
            f"时间退出: 持仓 {hold_days} 天 ≥ {time_exit_days} 天",
            "medium",
            "评估逻辑是否仍成立, 不成立则卖出",
        )

    if hold_days >= time_exit_days * 0.8:
        return _exit_signal(
            normalized,
            False,
            "none",
            f"接近时间退出: 持仓 {hold_days} 天 (上限 {time_exit_days} 天)",
            "low",
        )

    return _exit_signal(
        normalized,
        False,
        "none",
        f"未触发时间退出: 持仓 {hold_days} 天",
        "low",
    )


def check_logic_invalidation(
    position: Position | Mapping[str, Any],
    current_signals: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """逻辑证伪检查。"""
    normalized = coerce_position(position)
    if not normalized.ts_code:
        raise ValueError("position.ts_code is required")

    thesis = str(normalized.thesis or "").lower()
    if not thesis:
        return _exit_signal(normalized, False, "none", "无 thesis 数据, 无法检查逻辑证伪", "low")

    if current_signals is None:
        current_signals = {}

    invalidation_reasons: list[str] = []
    regime = str(current_signals.get("regime", "")).lower()
    if regime:
        if "growth" in thesis and ("recession" in regime or "stagflation" in regime):
            invalidation_reasons.append(f"thesis 含 growth 但当前 regime={regime}")
        if "inflation" in thesis and "deflation" in regime:
            invalidation_reasons.append(f"thesis 含通胀逻辑但当前 regime={regime} (通缩)")
        if "deflation" in thesis and "inflation" in regime:
            invalidation_reasons.append(f"thesis 含通缩逻辑但当前 regime={regime} (通胀)")

    sector_health = str(current_signals.get("sector_health", "")).lower()
    if sector_health == "weak":
        invalidation_reasons.append("板块健康度=weak")
    if current_signals.get("event_negative"):
        invalidation_reasons.append("存在负面事件冲击")
    if current_signals.get("fundamental_deterioration"):
        invalidation_reasons.append("基本面恶化")

    if invalidation_reasons:
        return _exit_signal(
            normalized,
            True,
            "logic_invalidation",
            f"逻辑证伪: {'; '.join(invalidation_reasons)}",
            "high",
            "卖出 (逻辑已失效)",
        )

    return _exit_signal(normalized, False, "none", "买入逻辑未证伪", "low")


def _signals_for_position(ts_code: str, current_signals: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(current_signals, dict):
        return {}
    scoped = current_signals.get(ts_code)
    if isinstance(scoped, dict):
        return scoped
    return current_signals


def _pick_primary_signal(signals: list[dict[str, Any]]) -> dict[str, Any]:
    if not signals:
        raise ValueError("signals must not be empty")

    priority_order = {
        "stop_loss": 0,
        "trailing_stop": 0,
        "logic_invalidation": 1,
        "time_exit": 2,
        "take_profit": 3,
    }
    severity_order = {"high": 0, "medium": 1, "low": 2}
    ordered = sorted(
        signals,
        key=lambda signal: (
            priority_order.get(signal.get("exit_type", "none"), 99),
            severity_order.get(signal.get("severity", "low"), 2),
        ),
    )
    return ordered[0]


def check_all_exits(
    positions: list[Position] | list[Mapping[str, Any]],
    current_prices: dict[str, float] | None = None,
    current_signals: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """批量检查所有持仓退出条件。"""
    if current_prices is None:
        current_prices = {}

    results: list[dict[str, Any]] = []
    for raw_position in positions or []:
        position = coerce_position(raw_position)
        if not position.ts_code:
            continue

        price = _safe_float(current_prices.get(position.ts_code))
        scoped_signals = _signals_for_position(position.ts_code, current_signals)
        per_position_signals = [
            check_stop_loss(position, price),
            check_logic_invalidation(position, scoped_signals),
            check_time_exit(position),
            check_take_profit(position, price),
        ]
        exit_signals = [signal for signal in per_position_signals if signal.get("should_exit")]
        if exit_signals:
            primary_signal = dict(_pick_primary_signal(exit_signals))
            primary_signal["all_exit_signals"] = exit_signals
            results.append(primary_signal)
            continue

        hold_signal = _exit_signal(position, False, "none", "所有退出条件正常", "low")
        hold_signal["all_exit_signals"] = []
        results.append(hold_signal)

    return results


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
            thesis="growth regime, 消费升级, 通胀预期",
            capital_layer="shadow",
        ),
    ]
    test_prices = {"600519.SH": 1650.0}
    test_signals = {"regime": "recession", "sector_health": "weak", "event_negative": True}
    print(json.dumps(check_all_exits(test_positions, test_prices, test_signals), ensure_ascii=False, indent=2))
