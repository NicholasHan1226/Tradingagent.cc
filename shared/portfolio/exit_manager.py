#!/usr/bin/env python3
"""退出管理 — 止损/止盈/时间退出/逻辑证伪。

退出优先级: 止损 > 逻辑证伪 > 时间退出 > 止盈。

四个独立函数:
  check_stop_loss(position, current_price) → exit_signal
  check_take_profit(position, current_price) → exit_signal
  check_time_exit(position) → exit_signal
  check_logic_invalidation(position, current_signals) → exit_signal
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

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
            for k, v in data.items():
                if k in merged and isinstance(merged[k], dict) and isinstance(v, dict):
                    merged[k] = {**merged[k], **v}
                else:
                    merged[k] = v
            return merged
    except (OSError, yaml.YAMLError):
        pass
    return dict(_DEFAULT_LIMITS)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        f = float(v)
        return f if f == f else default
    except (TypeError, ValueError):
        return default


def _parse_date(s: Any) -> datetime | None:
    if isinstance(s, datetime):
        return s
    if not s:
        return None
    s = str(s).strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _exit_signal(
    ts_code: str,
    should_exit: bool,
    exit_type: str,
    reason: str,
    severity: str = "medium",
    suggested_action: str = "",
) -> dict[str, Any]:
    """构造统一的退出信号格式。"""
    return {
        "ts_code": ts_code,
        "should_exit": should_exit,
        "exit_type": exit_type,  # stop_loss | take_profit | time_exit | logic_invalidation | none
        "reason": reason,
        "severity": severity,    # high | medium | low
        "suggested_action": suggested_action or ("卖出" if should_exit else "持有"),
        "timestamp": datetime.now().isoformat(timespec="seconds"),
    }


def check_stop_loss(
    position: dict[str, Any],
    current_price: float,
) -> dict[str, Any]:
    """止损检查 (含移动止损)。

    Args:
        position: {ts_code, cost, high_price, ...}
        current_price: 当前价格

    Returns:
        exit_signal
    """
    if not isinstance(position, dict):
        raise ValueError("position must be a dict")
    ts_code = position.get("ts_code", "")
    if not ts_code:
        raise ValueError("position.ts_code is required")

    cost = _safe_float(position.get("cost", 0.0))
    high_price = _safe_float(position.get("high_price", cost))
    price = _safe_float(current_price, 0.0)

    if cost <= 0 or price <= 0:
        return _exit_signal(ts_code, False, "none", "无成本价或当前价数据", "low")

    limits = _load_limits()
    stop_pct = _safe_float(limits.get("stop_loss", {}).get("pct", -0.08))
    trailing_pct = _safe_float(limits.get("stop_loss", {}).get("trailing_pct", -0.12))

    pnl_pct = (price - cost) / cost

    # 硬止损 (相对成本)
    if pnl_pct <= stop_pct:
        return _exit_signal(
            ts_code, True, "stop_loss",
            f"止损触发: 亏损 {pnl_pct:.4f} ≤ {stop_pct:.4f}",
            "high", "立即卖出"
        )

    # 移动止损 (相对最高价)
    if high_price > 0:
        trailing_dd = (price - high_price) / high_price
        if trailing_dd <= trailing_pct:
            return _exit_signal(
                ts_code, True, "stop_loss",
                f"移动止损: 从高点 {high_price:.2f} 回撤 {trailing_dd:.4f} ≤ {trailing_pct:.4f}",
                "high", "立即卖出"
            )

    return _exit_signal(
        ts_code, False, "none",
        f"未触发止损: 亏损 {pnl_pct:.4f}, 回撤 {((price - high_price) / high_price if high_price > 0 else 0):.4f}",
        "low"
    )


def check_take_profit(
    position: dict[str, Any],
    current_price: float,
    take_profit_pct: float = 0.20,
) -> dict[str, Any]:
    """止盈检查。

    Args:
        position: {ts_code, cost, ...}
        current_price: 当前价格
        take_profit_pct: 止盈线 (默认 20%)

    Returns:
        exit_signal
    """
    if not isinstance(position, dict):
        raise ValueError("position must be a dict")
    ts_code = position.get("ts_code", "")
    if not ts_code:
        raise ValueError("position.ts_code is required")

    cost = _safe_float(position.get("cost", 0.0))
    price = _safe_float(current_price, 0.0)
    tp_pct = _safe_float(take_profit_pct, 0.20)

    if cost <= 0 or price <= 0:
        return _exit_signal(ts_code, False, "none", "无成本价或当前价数据", "low")

    pnl_pct = (price - cost) / cost

    if pnl_pct >= tp_pct:
        return _exit_signal(
            ts_code, True, "take_profit",
            f"止盈触发: 盈利 {pnl_pct:.4f} ≥ {tp_pct:.4f}",
            "medium", "考虑卖出或移动止损"
        )

    # 接近止盈 (80%)
    if pnl_pct >= tp_pct * 0.8:
        return _exit_signal(
            ts_code, False, "none",
            f"接近止盈: 盈利 {pnl_pct:.4f} (目标 {tp_pct:.4f})",
            "low", "考虑移动止损锁定利润"
        )

    return _exit_signal(
        ts_code, False, "none",
        f"未触发止盈: 盈利 {pnl_pct:.4f}",
        "low"
    )


def check_time_exit(position: dict[str, Any], time_exit_days: int | None = None) -> dict[str, Any]:
    """时间退出检查。

    Args:
        position: {ts_code, entry_date, ...}
        time_exit_days: 持仓天数上限 (默认从 risk_limits 读取)

    Returns:
        exit_signal
    """
    if not isinstance(position, dict):
        raise ValueError("position must be a dict")
    ts_code = position.get("ts_code", "")
    if not ts_code:
        raise ValueError("position.ts_code is required")

    entry_date = _parse_date(position.get("entry_date"))
    if entry_date is None:
        return _exit_signal(ts_code, False, "none", "无入场日期数据", "low")

    if time_exit_days is None:
        limits = _load_limits()
        time_exit_days = int(limits.get("time_exit_days", 30))

    now = datetime.now()
    hold_days = (now - entry_date).days

    if hold_days >= time_exit_days:
        return _exit_signal(
            ts_code, True, "time_exit",
            f"时间退出: 持仓 {hold_days} 天 ≥ {time_exit_days} 天",
            "medium", "评估逻辑是否仍成立, 不成立则卖出"
        )

    # 接近时间退出 (80%)
    if hold_days >= time_exit_days * 0.8:
        return _exit_signal(
            ts_code, False, "none",
            f"接近时间退出: 持仓 {hold_days} 天 (上限 {time_exit_days} 天)",
            "low", "准备评估"
        )

    return _exit_signal(
        ts_code, False, "none",
        f"未触发时间退出: 持仓 {hold_days} 天",
        "low"
    )


def check_logic_invalidation(
    position: dict[str, Any],
    current_signals: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """逻辑证伪检查。

    检查买入逻辑 (thesis) 是否被当前信号证伪。

    Args:
        position: {ts_code, thesis, sector, ...}
        current_signals: {
            "regime": str,
            "sector_health": str,    # "strong" | "weak" | "neutral"
            "event_negative": bool,  # 负面事件
            "fundamental_deterioration": bool,  # 基本面恶化
        }

    Returns:
        exit_signal
    """
    if not isinstance(position, dict):
        raise ValueError("position must be a dict")
    ts_code = position.get("ts_code", "")
    if not ts_code:
        raise ValueError("position.ts_code is required")

    thesis = str(position.get("thesis", "")).lower()
    if not thesis:
        return _exit_signal(ts_code, False, "none", "无 thesis 数据, 无法检查逻辑证伪", "low")

    if current_signals is None:
        current_signals = {}

    invalidation_reasons: list[str] = []

    # 1. Regime 不匹配
    regime = str(current_signals.get("regime", "")).lower()
    if regime:
        if "growth" in thesis and ("recession" in regime or "stagflation" in regime):
            invalidation_reasons.append(f"thesis 含 growth 但当前 regime={regime}")
        if "inflation" in thesis and "deflation" in regime:
            invalidation_reasons.append(f"thesis 含通胀逻辑但当前 regime={regime} (通缩)")
        if "deflation" in thesis and "inflation" in regime:
            invalidation_reasons.append(f"thesis 含通缩逻辑但当前 regime={regime} (通胀)")

    # 2. 板块健康度恶化
    sector_health = str(current_signals.get("sector_health", "")).lower()
    if sector_health == "weak":
        invalidation_reasons.append("板块健康度=weak")

    # 3. 负面事件
    if current_signals.get("event_negative"):
        invalidation_reasons.append("存在负面事件冲击")

    # 4. 基本面恶化
    if current_signals.get("fundamental_deterioration"):
        invalidation_reasons.append("基本面恶化")

    if invalidation_reasons:
        return _exit_signal(
            ts_code, True, "logic_invalidation",
            f"逻辑证伪: {'; '.join(invalidation_reasons)}",
            "high", "卖出 (逻辑已失效)"
        )

    return _exit_signal(
        ts_code, False, "none",
        "买入逻辑未证伪",
        "low"
    )


def check_all_exits(
    position: dict[str, Any],
    current_price: float,
    current_signals: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """检查所有退出条件, 返回最高优先级的退出信号。

    优先级: 止损 > 逻辑证伪 > 时间退出 > 止盈
    """
    if not isinstance(position, dict):
        raise ValueError("position must be a dict")

    ts_code = position.get("ts_code", "")

    signals = [
        check_stop_loss(position, current_price),
        check_logic_invalidation(position, current_signals),
        check_time_exit(position),
        check_take_profit(position, current_price),
    ]

    # 过滤出 should_exit=True 的信号
    exit_signals = [s for s in signals if s.get("should_exit")]
    if exit_signals:
        # 按 severity 排序 (high > medium > low)
        severity_order = {"high": 0, "medium": 1, "low": 2}
        exit_signals.sort(key=lambda s: severity_order.get(s.get("severity", "low"), 2))
        result = dict(exit_signals[0])
        result["all_exit_signals"] = exit_signals
        return result

    # 无退出信号, 返回 hold
    return _exit_signal(ts_code, False, "none", "所有退出条件正常", "low")


if __name__ == "__main__":
    import json
    test_position = {
        "ts_code": "600519.SH",
        "cost": 1800.0,
        "high_price": 1900.0,
        "entry_date": "2026-05-01",
        "thesis": "growth regime, 消费升级, 通胀预期",
    }
    print("=== stop_loss ===")
    print(json.dumps(check_stop_loss(test_position, 1650.0), ensure_ascii=False, indent=2))
    print("\n=== take_profit ===")
    print(json.dumps(check_take_profit(test_position, 2200.0), ensure_ascii=False, indent=2))
    print("\n=== time_exit ===")
    print(json.dumps(check_time_exit(test_position), ensure_ascii=False, indent=2))
    print("\n=== logic_invalidation ===")
    test_signals = {"regime": "recession", "sector_health": "weak", "event_negative": True}
    print(json.dumps(check_logic_invalidation(test_position, test_signals), ensure_ascii=False, indent=2))
    print("\n=== check_all ===")
    print(json.dumps(check_all_exits(test_position, 1650.0, test_signals), ensure_ascii=False, indent=2))
