#!/usr/bin/env python3
"""自愈 — 自动修复常见风险违规。

heal(portfolio, patrol_result) → {healed, actions, new_portfolio}
处理: 超限减仓 / 相关性过高调权 / 黑天鹅强制减仓。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from .pre_trade_check import _load_limits, _safe_float
from .black_swan import compute_force_reduce


def heal(
    portfolio: dict[str, Any],
    patrol_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """自愈主函数 — 根据 patrol 结果自动修复风险违规。

    Args:
        portfolio: {
            "positions": [{"ts_code", "weight", "sector", ...}],
            "total_exposure": float,
        }
        patrol_result: patrol() 的输出 (可选, 无则内部计算)

    Returns:
        {
            "healed": bool,           # 是否做了修复
            "actions": list[str],     # 修复操作描述
            "new_portfolio": dict,    # 修复后的组合
            "timestamp": str,
        }
    """
    if not isinstance(portfolio, dict):
        portfolio = {}
    if patrol_result is None:
        patrol_result = {}

    positions = list(portfolio.get("positions", []) or [])
    if not isinstance(positions, list):
        positions = []
    # 深拷贝 positions 避免修改原数据
    positions = [dict(p) if isinstance(p, dict) else {} for p in positions]

    limits = _load_limits()
    actions: list[str] = []
    now = datetime.now().isoformat(timespec="seconds")

    single_max = _safe_float(limits.get("single_stock_max", 0.15))
    sector_max = _safe_float(limits.get("sector_max", 0.40))
    total_max = _safe_float(limits.get("total_exposure_max", 0.80))

    # 1. 修复单股超限
    for p in positions:
        if not isinstance(p, dict):
            continue
        w = _safe_float(p.get("weight", 0.0))
        if w > single_max + 1e-9:
            old_w = w
            p["weight"] = single_max
            actions.append(
                f"单股减仓: {p.get('ts_code', '?')} 权重 {old_w:.4f} → {single_max:.4f}"
            )

    # 2. 修复板块超限
    sector_exposure: dict[str, float] = {}
    for p in positions:
        if isinstance(p, dict):
            sec = str(p.get("sector", "unknown"))
            sector_exposure[sec] = sector_exposure.get(sec, 0.0) + _safe_float(p.get("weight", 0.0))

    for sec, exp in sector_exposure.items():
        if exp > sector_max + 1e-9:
            # 按比例缩减该板块所有持仓
            scale = sector_max / exp if exp > 0 else 1.0
            for p in positions:
                if isinstance(p, dict) and str(p.get("sector", "unknown")) == sec:
                    old_w = _safe_float(p.get("weight", 0.0))
                    new_w = old_w * scale
                    p["weight"] = round(new_w, 6)
                    if abs(old_w - new_w) > 1e-9:
                        actions.append(
                            f"板块减仓: {p.get('ts_code', '?')} ({sec}) 权重 {old_w:.4f} → {new_w:.4f}"
                        )

    # 3. 修复总敞口超限
    total_exposure = sum(_safe_float(p.get("weight", 0.0)) for p in positions if isinstance(p, dict))
    if total_exposure > total_max + 1e-9:
        scale = total_max / total_exposure if total_exposure > 0 else 1.0
        for p in positions:
            if isinstance(p, dict):
                old_w = _safe_float(p.get("weight", 0.0))
                new_w = old_w * scale
                p["weight"] = round(new_w, 6)
                if abs(old_w - new_w) > 1e-9:
                    actions.append(
                        f"总敞口减仓: {p.get('ts_code', '?')} 权重 {old_w:.4f} → {new_w:.4f}"
                    )
        total_exposure = sum(_safe_float(p.get("weight", 0.0)) for p in positions if isinstance(p, dict))

    # 4. 黑天鹅强制减仓
    bs = patrol_result.get("black_swan", {})
    if bs.get("triggered"):
        force_to = _safe_float(bs.get("force_reduce_to", 0.50))
        reduce_info = compute_force_reduce(total_exposure, force_to)
        if reduce_info.get("need_reduce"):
            scale = force_to / total_exposure if total_exposure > 0 else 1.0
            for p in positions:
                if isinstance(p, dict):
                    old_w = _safe_float(p.get("weight", 0.0))
                    new_w = old_w * scale
                    p["weight"] = round(new_w, 6)
                    if abs(old_w - new_w) > 1e-9:
                        actions.append(
                            f"黑天鹅减仓: {p.get('ts_code', '?')} 权重 {old_w:.4f} → {new_w:.4f}"
                        )
            total_exposure = sum(_safe_float(p.get("weight", 0.0)) for p in positions if isinstance(p, dict))

    # 5. 清理权重为 0 的持仓 (标记为 closed)
    for p in positions:
        if isinstance(p, dict) and _safe_float(p.get("weight", 0.0)) < 1e-6:
            p["status"] = "closed"

    new_portfolio = {
        "positions": positions,
        "total_exposure": round(total_exposure, 6),
    }
    # 保留原 portfolio 的其他字段
    for k, v in portfolio.items():
        if k not in ("positions", "total_exposure"):
            new_portfolio[k] = v

    healed = len(actions) > 0
    if not healed:
        actions.append("无需修复: 组合风险状态正常")

    return {
        "healed": healed,
        "actions": actions,
        "new_portfolio": new_portfolio,
        "timestamp": now,
    }


if __name__ == "__main__":
    import json
    test_portfolio = {
        "positions": [
            {"ts_code": "600519.SH", "weight": 0.18, "sector": "白酒"},  # 超单股限
            {"ts_code": "000858.SZ", "weight": 0.15, "sector": "白酒"},  # 板块超限
            {"ts_code": "601318.SH", "weight": 0.50, "sector": "保险"},  # 总敞口超限
        ],
        "total_exposure": 0.83,
    }
    test_patrol = {"black_swan": {"triggered": True, "force_reduce_to": 0.50}}
    r = heal(test_portfolio, test_patrol)
    print(json.dumps(r, ensure_ascii=False, indent=2))
