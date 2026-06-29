#!/usr/bin/env python3
"""持仓监控 — 止损/回撤/时间退出/regime 变化。

check_positions(positions, current_prices) → list of {ts_code, action, reason}
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

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
    """解析日期字符串, 支持 YYYY-MM-DD 或 YYYYMMDD。"""
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


def check_positions(
    positions: list[dict[str, Any]],
    current_prices: dict[str, float] | None = None,
    regime: str | None = None,
    portfolio_high_water: float | None = None,
    portfolio_current_value: float | None = None,
) -> list[dict[str, Any]]:
    """持仓监控主函数。

    Args:
        positions: list of {
            "ts_code": str,
            "cost": float,           # 成本价
            "weight": float,         # 当前权重
            "entry_date": str,       # 入场日期
            "high_price": float,     # 持仓期最高价 (移动止损用)
            "sector": str,
            "thesis": str,           # 买入逻辑 (逻辑证伪检查用)
        }
        current_prices: {ts_code: price}
        regime: 当前 regime (如 "growth", "recession"), 变化时触发评估
        portfolio_high_water: 组合历史最高净值
        portfolio_current_value: 组合当前净值

    Returns:
        list of {
            "ts_code": str,
            "action": str,    # "stop_loss" | "trailing_stop" | "time_exit" | "drawdown" | "regime_change" | "hold"
            "reason": str,
            "severity": str,  # "high" | "medium" | "low"
        }
    """
    if not positions:
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

    # 组合级回撤检查
    if portfolio_high_water and portfolio_current_value:
        port_dd = (portfolio_current_value - portfolio_high_water) / portfolio_high_water if portfolio_high_water else 0.0
        if port_dd < portfolio_dd_max:
            signals.append({
                "ts_code": "PORTFOLIO",
                "action": "drawdown",
                "reason": f"组合回撤 {port_dd:.4f} < {portfolio_dd_max:.4f}, 建议全面减仓评估",
                "severity": "high",
            })

    for pos in positions:
        if not isinstance(pos, dict):
            continue
        ts_code = pos.get("ts_code", "")
        if not ts_code:
            continue

        cost = _safe_float(pos.get("cost", 0.0))
        current_price = _safe_float(current_prices.get(ts_code, 0.0))
        high_price = _safe_float(pos.get("high_price", cost))
        weight = _safe_float(pos.get("weight", 0.0))

        entry_date = _parse_date(pos.get("entry_date"))
        hold_days = (now - entry_date).days if entry_date else 0

        pos_signals: list[dict[str, Any]] = []

        # 1. 止损检查 (相对成本价)
        if cost > 0 and current_price > 0:
            pnl_pct = (current_price - cost) / cost
            if pnl_pct <= stop_pct:
                pos_signals.append({
                    "action": "stop_loss",
                    "reason": f"止损: {ts_code} 亏损 {pnl_pct:.4f} ≤ {stop_pct:.4f}",
                    "severity": "high",
                })

            # 2. 移动止损 (相对最高价回撤)
            if high_price > 0:
                trailing_dd = (current_price - high_price) / high_price
                if trailing_dd <= trailing_pct:
                    pos_signals.append({
                        "action": "trailing_stop",
                        "reason": f"移动止损: {ts_code} 从高点回撤 {trailing_dd:.4f} ≤ {trailing_pct:.4f}",
                        "severity": "high",
                    })

            # 3. 个股回撤检查
            if pnl_pct < single_dd_max:
                pos_signals.append({
                    "action": "drawdown",
                    "reason": f"个股回撤: {ts_code} 亏损 {pnl_pct:.4f} < {single_dd_max:.4f}",
                    "severity": "medium",
                })

        # 4. 时间退出
        if hold_days >= time_exit_days:
            pos_signals.append({
                "action": "time_exit",
                "reason": f"时间退出: {ts_code} 持仓 {hold_days} 天 ≥ {time_exit_days} 天, 需评估逻辑是否仍成立",
                "severity": "medium",
            })

        # 5. Regime 变化 (简化: 如果 thesis 中包含 regime 关键词且与当前 regime 不符)
        thesis = str(pos.get("thesis", "")).lower()
        if regime and thesis:
            # 简单关键词匹配
            regime_keywords = {
                "growth": ["growth", "增长", "扩张"],
                "recession": ["recession", "衰退", "收缩"],
                "inflation": ["inflation", "通胀"],
                "deflation": ["deflation", "通缩"],
            }
            current_kw = regime_keywords.get(regime.lower(), [])
            opposite_regimes = {k: v for k, v in regime_keywords.items() if k != regime.lower()}
            for opp_regime, opp_kw in opposite_regimes.items():
                if any(kw in thesis for kw in opp_kw):
                    pos_signals.append({
                        "action": "regime_change",
                        "reason": f"Regime 变化: 当前={regime}, 但 thesis 含 {opp_regime} 关键词, 逻辑可能失效",
                        "severity": "medium",
                    })
                    break

        if pos_signals:
            # 取最高 severity 的信号
            severity_order = {"high": 0, "medium": 1, "low": 2}
            pos_signals.sort(key=lambda s: severity_order.get(s.get("severity", "low"), 2))
            for s in pos_signals:
                signals.append({"ts_code": ts_code, **s})
        else:
            signals.append({
                "ts_code": ts_code,
                "action": "hold",
                "reason": f"持仓正常: {ts_code} 权重 {weight:.4f}, 持仓 {hold_days} 天",
                "severity": "low",
            })

    return signals


def filter_actions(signals: list[dict[str, Any]], actions: list[str] | None = None) -> list[dict[str, Any]]:
    """筛选特定 action 的信号。"""
    if actions is None:
        actions = ["stop_loss", "trailing_stop", "drawdown", "time_exit", "regime_change"]
    return [s for s in signals if s.get("action") in actions]


if __name__ == "__main__":
    import json
    test_positions = [
        {
            "ts_code": "600519.SH",
            "cost": 1800.0,
            "weight": 0.10,
            "entry_date": "2026-05-01",
            "high_price": 1900.0,
            "thesis": "growth regime, 消费升级",
        },
        {
            "ts_code": "000858.SZ",
            "cost": 150.0,
            "weight": 0.08,
            "entry_date": "2026-06-20",
            "high_price": 155.0,
            "thesis": "growth",
        },
    ]
    test_prices = {"600519.SH": 1650.0, "000858.SZ": 148.0}
    r = check_positions(test_positions, test_prices, regime="recession")
    print(json.dumps(r, ensure_ascii=False, indent=2))
