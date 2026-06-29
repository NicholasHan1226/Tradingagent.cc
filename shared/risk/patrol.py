#!/usr/bin/env python3
"""巡检 — 定时扫描持仓风险状态, 输出告警。

patrol(portfolio, market_data) → {alerts, summary, timestamp}
聚合 position_monitor + black_swan + risk_limits 检查。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from .position_monitor import check_positions, filter_actions
from .pre_trade_check import _load_limits, _safe_float
from .black_swan import check_black_swan


def patrol(
    portfolio: dict[str, Any],
    market_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """巡检主函数 — 聚合所有风险检查。

    Args:
        portfolio: {
            "positions": [...],
            "current_prices": {ts_code: price},
            "total_exposure": float,
            "high_water": float,         # 组合最高净值
            "current_value": float,      # 组合当前净值
            "daily_pnl_pct": float,
            "regime": str,
        }
        market_data: 大盘数据 (for black_swan)

    Returns:
        {
            "timestamp": str,
            "alerts": list[dict],     # 告警列表 (按 severity 排序)
            "summary": {
                "total_alerts": int,
                "high_severity": int,
                "medium_severity": int,
                "low_severity": int,
                "black_swan_triggered": bool,
                "position_issues": int,
            },
            "black_swan": dict,
            "position_signals": list[dict],
        }
    """
    if not isinstance(portfolio, dict):
        portfolio = {}
    if market_data is None:
        market_data = {}

    positions = portfolio.get("positions", []) or []
    current_prices = portfolio.get("current_prices", {}) or {}
    regime = portfolio.get("regime")
    high_water = portfolio.get("high_water")
    current_value = portfolio.get("current_value")

    alerts: list[dict[str, Any]] = []
    now = datetime.now().isoformat(timespec="seconds")

    # 1. 黑天鹅检查
    bs_result = check_black_swan(market_data)
    if bs_result.get("triggered"):
        alerts.append({
            "type": "black_swan",
            "severity": "high",
            "message": bs_result.get("trigger_reason", "黑天鹅触发"),
            "action": bs_result.get("action", "force_reduce"),
            "timestamp": now,
        })
    elif bs_result.get("action") == "monitor":
        alerts.append({
            "type": "black_swan_warning",
            "severity": "medium",
            "message": bs_result.get("trigger_reason", ""),
            "action": "monitor",
            "timestamp": now,
        })

    # 2. 持仓监控
    pos_signals = check_positions(
        positions, current_prices, regime=regime,
        portfolio_high_water=high_water,
        portfolio_current_value=current_value,
    )
    # 只取需要行动的信号 (非 hold)
    action_signals = filter_actions(pos_signals)
    for s in action_signals:
        alerts.append({
            "type": s.get("action", "unknown"),
            "severity": s.get("severity", "low"),
            "message": s.get("reason", ""),
            "ts_code": s.get("ts_code", ""),
            "timestamp": now,
        })

    # 3. 风控参数检查 — 总敞口
    limits = _load_limits()
    total_max = _safe_float(limits.get("total_exposure_max", 0.80))
    total_exposure = _safe_float(portfolio.get("total_exposure", 0.0))
    if total_exposure > total_max + 1e-9:
        alerts.append({
            "type": "exposure_breach",
            "severity": "high",
            "message": f"总敞口 {total_exposure:.4f} > 上限 {total_max:.4f}",
            "timestamp": now,
        })

    # 4. 日亏检查
    daily_loss_limit = _safe_float(limits.get("daily_loss_limit", 0.03))
    daily_pnl = _safe_float(portfolio.get("daily_pnl_pct", 0.0))
    if daily_pnl < -daily_loss_limit:
        alerts.append({
            "type": "daily_loss_breach",
            "severity": "high",
            "message": f"当日亏损 {daily_pnl:.4f} < -{daily_loss_limit:.4f}, 暂停新增",
            "timestamp": now,
        })

    # 5. 持仓数检查
    max_positions = int(limits.get("max_positions", 5))
    n_positions = len({p.get("ts_code") for p in positions if isinstance(p, dict) and p.get("ts_code")})
    if n_positions > max_positions:
        alerts.append({
            "type": "position_count_breach",
            "severity": "medium",
            "message": f"持仓数 {n_positions} > 上限 {max_positions}",
            "timestamp": now,
        })

    # 按 severity 排序
    severity_order = {"high": 0, "medium": 1, "low": 2}
    alerts.sort(key=lambda a: severity_order.get(a.get("severity", "low"), 2))

    # 汇总
    high_n = sum(1 for a in alerts if a.get("severity") == "high")
    medium_n = sum(1 for a in alerts if a.get("severity") == "medium")
    low_n = sum(1 for a in alerts if a.get("severity") == "low")

    return {
        "timestamp": now,
        "alerts": alerts,
        "summary": {
            "total_alerts": len(alerts),
            "high_severity": high_n,
            "medium_severity": medium_n,
            "low_severity": low_n,
            "black_swan_triggered": bs_result.get("triggered", False),
            "position_issues": len(action_signals),
        },
        "black_swan": bs_result,
        "position_signals": pos_signals,
    }


if __name__ == "__main__":
    import json
    test_portfolio = {
        "positions": [
            {
                "ts_code": "600519.SH",
                "cost": 1800.0,
                "weight": 0.12,
                "entry_date": "2026-05-01",
                "high_price": 1900.0,
                "thesis": "growth",
                "sector": "白酒",
            },
        ],
        "current_prices": {"600519.SH": 1650.0},
        "total_exposure": 0.85,
        "high_water": 1.10,
        "current_value": 0.95,
        "daily_pnl_pct": -0.035,
        "regime": "recession",
    }
    test_market = {"market_change_pct": -0.035}
    r = patrol(test_portfolio, test_market)
    print(json.dumps(r, ensure_ascii=False, indent=2))
