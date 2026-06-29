#!/usr/bin/env python3
"""组合构建 — 从已批准订单构建组合。

三种方法: risk_parity / equal_weight / conviction_weighted。
"""
from __future__ import annotations

from typing import Any

from .position_sizer import size_position


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        f = float(v)
        return f if f == f else default
    except (TypeError, ValueError):
        return default


def construct(
    orders: list[dict[str, Any]],
    capital: float,
    method: str = "conviction_weighted",
    regime: str = "growth",
) -> dict[str, Any]:
    """组合构建主函数。

    Args:
        orders: list of {
            "ts_code": str,
            "belief_score": float,    # 0-1, 来自 adversarial
            "volatility": float,      # 年化波动率, 如 0.25
            "sector": str,
            "price": float,           # 当前价格
            "conviction": float,      # 可选, 信心分 (默认用 belief_score)
        }
        capital: 总资金 (元)
        method: "risk_parity" | "equal_weight" | "conviction_weighted"
        regime: 当前 regime

    Returns:
        {
            "method": str,
            "capital": float,
            "positions": list[{
                "ts_code": str,
                "weight": float,
                "shares": int,
                "amount": float,
                "sector": str,
            }],
            "total_weight": float,
            "cash_weight": float,
        }
    """
    if not orders:
        return {
            "method": method,
            "capital": capital,
            "positions": [],
            "total_weight": 0.0,
            "cash_weight": 1.0,
        }

    if capital <= 0:
        raise ValueError("capital must be positive")

    valid_methods = {"risk_parity", "equal_weight", "conviction_weighted"}
    if method not in valid_methods:
        raise ValueError(f"method must be one of {valid_methods}, got {method}")

    # 计算每个订单的目标权重
    weights: list[float] = []
    for order in orders:
        if not isinstance(order, dict) or not order.get("ts_code"):
            weights.append(0.0)
            continue

        if method == "equal_weight":
            n = len([o for o in orders if isinstance(o, dict) and o.get("ts_code")])
            w = 1.0 / n if n > 0 else 0.0
            weights.append(w)

        elif method == "conviction_weighted":
            belief = _safe_float(order.get("belief_score") or order.get("conviction"), 0.5)
            vol = _safe_float(order.get("volatility"), 0.25)
            w = size_position(belief, vol, regime)
            weights.append(w)

        elif method == "risk_parity":
            # risk parity: 权重 ∝ 1/波动率, 然后归一化
            vol = _safe_float(order.get("volatility"), 0.25)
            inv_vol = 1.0 / vol if vol > 0 else 0.0
            weights.append(inv_vol)

    # 归一化 (risk_parity 需要归一化)
    if method == "risk_parity":
        total_inv_vol = sum(weights)
        if total_inv_vol > 0:
            weights = [w / total_inv_vol for w in weights]

    # conviction_weighted: 权重已经是各自独立计算的, 不强制归一化 (允许留现金)
    # 但要确保总权重合理 (不超过 0.80)
    total_w = sum(weights)
    max_total = 0.80
    if total_w > max_total:
        scale = max_total / total_w
        weights = [w * scale for w in weights]
        total_w = sum(weights)

    # 构建持仓
    positions: list[dict[str, Any]] = []
    for order, w in zip(orders, weights):
        if not isinstance(order, dict) or not order.get("ts_code"):
            continue
        if w <= 0:
            continue
        price = _safe_float(order.get("price"), 0.0)
        amount = capital * w
        shares = int(amount / price) if price > 0 else 0
        actual_amount = shares * price if price > 0 else amount
        actual_weight = actual_amount / capital if capital > 0 else 0.0

        positions.append({
            "ts_code": order["ts_code"],
            "weight": round(actual_weight, 6),
            "shares": shares,
            "amount": round(actual_amount, 2),
            "sector": str(order.get("sector", "unknown")),
            "price": price,
        })

    final_total = sum(p["weight"] for p in positions)

    return {
        "method": method,
        "capital": capital,
        "positions": positions,
        "total_weight": round(final_total, 6),
        "cash_weight": round(max(0.0, 1.0 - final_total), 6),
    }


if __name__ == "__main__":
    import json
    test_orders = [
        {"ts_code": "600519.SH", "belief_score": 0.75, "volatility": 0.20, "sector": "白酒", "price": 1700.0},
        {"ts_code": "000858.SZ", "belief_score": 0.60, "volatility": 0.25, "sector": "白酒", "price": 150.0},
        {"ts_code": "601318.SH", "belief_score": 0.55, "volatility": 0.18, "sector": "保险", "price": 50.0},
    ]
    for m in ("conviction_weighted", "equal_weight", "risk_parity"):
        print(f"\n=== {m} ===")
        r = construct(test_orders, 1_000_000, method=m, regime="growth")
        print(json.dumps(r, ensure_ascii=False, indent=2))
