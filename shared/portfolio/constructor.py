#!/usr/bin/env python3
"""组合构建 — 从已批准订单构建组合。

方法: risk_parity / equal_weight / conviction_weighted / volatility_targeted。
"""

from __future__ import annotations

from typing import Any

from .position_sizer import size_position

_MAX_TOTAL_WEIGHT = 0.80
_ASHARE_MAX_TOTAL_WEIGHT = 0.90
_ASHARE_MAX_POSITIONS = 8
_MAX_SINGLE_WEIGHT = 0.15
_CRYPTO_VOLATILITY_BASELINE = 0.80


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        f = float(v)
        return f if f == f else default
    except (TypeError, ValueError):
        return default


def _first_float(order: dict[str, Any], keys: tuple[str, ...], default: float) -> float:
    for key in keys:
        if key not in order:
            continue
        value = order.get(key)
        if value is None:
            continue
        return _safe_float(value, default)
    return default


def _quantity_decimals(lot_size: float) -> int:
    text = f"{lot_size:.12f}".rstrip("0")
    if "." not in text:
        return 0
    return min(12, max(0, len(text.split(".", 1)[1])))


def _clean_quantity(value: float, lot_size: float) -> int | float:
    if abs(value - round(value)) < 1e-12 and lot_size >= 1:
        return int(round(value))
    return round(value, _quantity_decimals(lot_size))


def _position_quantity(amount: float, price: float, lot_size: float) -> int | float:
    if price <= 0 or amount <= 0:
        return 0
    lot = lot_size if lot_size > 0 else 1.0
    raw = amount / price
    steps = int(raw / lot)
    if steps <= 0:
        return 0
    return _clean_quantity(steps * lot, lot)


def _valid_order_count(orders: list[dict[str, Any]]) -> int:
    return len([o for o in orders if isinstance(o, dict) and o.get("ts_code")])


def _is_ashare_portfolio(orders: list[dict[str, Any]], regime: str | None) -> bool:
    regime_key = str(regime or "").strip().lower().replace("-", "_")
    if regime_key.startswith("ashare") or regime_key.startswith("a_share"):
        return True
    declared = {
        str(order.get("market") or "").strip().lower().replace("-", "_")
        for order in orders
        if isinstance(order, dict) and order.get("market")
    }
    return declared == {"ashare"} or declared == {"a_share"}


def _limit_weights(
    weights: list[float],
    *,
    target_total: float | None = None,
    max_total: float = _MAX_TOTAL_WEIGHT,
    max_single: float = _MAX_SINGLE_WEIGHT,
) -> list[float]:
    """Apply portfolio limits and optionally normalize toward a target total."""
    clean = [max(0.0, _safe_float(w, 0.0)) for w in weights]
    if not clean:
        return []

    if target_total is not None:
        target = min(max_total, max(0.0, target_total))
        total = sum(clean)
        if total > 0 and target > 0:
            clean = [w / total * target for w in clean]

    limited = [min(w, max_single) for w in clean]

    # Reallocate excess from capped names to uncapped names until the target is
    # filled or every position has reached the single-name hard limit.
    if target_total is not None:
        target = min(max_total, max(0.0, target_total))
        while target - sum(limited) > 1e-12:
            room_indexes = [
                i
                for i, w in enumerate(limited)
                if w < max_single - 1e-12 and clean[i] > 0
            ]
            if not room_indexes:
                break
            remaining = target - sum(limited)
            score_total = sum(clean[i] for i in room_indexes)
            if score_total <= 0:
                break
            changed = False
            for i in room_indexes:
                add = remaining * clean[i] / score_total
                new_w = min(max_single, limited[i] + add)
                if new_w > limited[i] + 1e-12:
                    changed = True
                limited[i] = new_w
            if not changed:
                break

    total_limited = sum(limited)
    if total_limited > max_total:
        scale = max_total / total_limited
        limited = [w * scale for w in limited]

    return limited


def _volatility_targeted_weights(
    orders: list[dict[str, Any]], *, max_total: float = _MAX_TOTAL_WEIGHT
) -> list[float]:
    weights: list[float] = []
    for order in orders:
        if not isinstance(order, dict) or not order.get("ts_code"):
            weights.append(0.0)
            continue
        vol = _safe_float(order.get("volatility"), _CRYPTO_VOLATILITY_BASELINE)
        baseline = _safe_float(
            order.get("volatility_baseline"), _CRYPTO_VOLATILITY_BASELINE
        )
        if baseline <= 0:
            baseline = _CRYPTO_VOLATILITY_BASELINE
        # Explicit volatility target: lower realized volatility receives more
        # capital, while high-volatility assets are scaled against the target.
        weights.append(baseline / vol if vol > 0 else 0.0)
    return _limit_weights(weights, target_total=max_total, max_total=max_total)


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
            "capital_layer": str,     # 可选, real/simulated/shadow 透传
        }
        capital: 总资金 (元)
        method: "risk_parity" | "equal_weight" | "conviction_weighted" |
            "volatility_targeted"
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
                "capital_layer": str,  # 输入存在时透传
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

    valid_methods = {
        "risk_parity",
        "equal_weight",
        "conviction_weighted",
        "volatility_targeted",
    }
    if method not in valid_methods:
        raise ValueError(f"method must be one of {valid_methods}, got {method}")

    ashare_portfolio = _is_ashare_portfolio(orders, regime)
    max_total_weight = (
        _ASHARE_MAX_TOTAL_WEIGHT if ashare_portfolio else _MAX_TOTAL_WEIGHT
    )
    active_orders = (
        list(orders[:_ASHARE_MAX_POSITIONS]) if ashare_portfolio else list(orders)
    )

    # 计算每个订单的目标权重
    weights: list[float] = []
    if method == "volatility_targeted":
        weights = _volatility_targeted_weights(
            active_orders, max_total=max_total_weight
        )
    else:
        for order in active_orders:
            if not isinstance(order, dict) or not order.get("ts_code"):
                weights.append(0.0)
                continue

            if method == "equal_weight":
                n = _valid_order_count(active_orders)
                w = 1.0 / n if n > 0 else 0.0
                weights.append(w)

            elif method == "conviction_weighted":
                belief = _safe_float(
                    order.get("belief_score") or order.get("conviction"), 0.5
                )
                vol = _safe_float(order.get("volatility"), 0.25)
                w = size_position(belief, vol, regime)
                weights.append(w)

            elif method == "risk_parity":
                # risk parity: 权重 ∝ 1/波动率, 然后归一化
                vol = _safe_float(order.get("volatility"), 0.25)
                inv_vol = 1.0 / vol if vol > 0 else 0.0
                weights.append(inv_vol)

        # 归一化方法输出到组合总仓目标, conviction_weighted 保留原有留现金逻辑。
        if method in {"risk_parity", "equal_weight"}:
            weights = _limit_weights(
                weights,
                target_total=max_total_weight,
                max_total=max_total_weight,
            )
        else:
            weights = _limit_weights(weights, max_total=max_total_weight)

    # conviction_weighted: 权重已经是各自独立计算的, 不强制归一化 (允许留现金)
    # 其他方法归一化到市场总仓上限，并统一执行单股 15% 硬限。
    total_w = sum(weights)
    if total_w > max_total_weight:
        scale = max_total_weight / total_w
        weights = [w * scale for w in weights]
        total_w = sum(weights)

    # 构建持仓
    positions: list[dict[str, Any]] = []
    for order, w in zip(active_orders, weights):
        if not isinstance(order, dict) or not order.get("ts_code"):
            continue
        if w <= 0:
            continue
        price = _safe_float(order.get("price"), 0.0)
        amount = capital * w
        lot_size = _first_float(order, ("lot_size", "quantity_step"), 1.0)
        shares = _position_quantity(amount, price, lot_size)
        actual_amount = float(shares) * price if price > 0 else amount
        actual_weight = actual_amount / capital if capital > 0 else 0.0

        position = {
            "ts_code": order["ts_code"],
            "weight": round(actual_weight, 6),
            "shares": shares,
            "amount": round(actual_amount, 2),
            "sector": str(order.get("sector", "unknown")),
            "price": price,
        }
        if "capital_layer" in order:
            position["capital_layer"] = str(order.get("capital_layer"))
        positions.append(position)

    final_total = sum(p["weight"] for p in positions)
    capital_layers = {
        str(o.get("capital_layer"))
        for o in active_orders
        if isinstance(o, dict) and o.get("capital_layer") is not None
    }

    result: dict[str, Any] = {
        "method": method,
        "capital": capital,
        "positions": positions,
        "total_weight": round(final_total, 6),
        "cash_weight": round(max(0.0, 1.0 - final_total), 6),
    }
    if len(capital_layers) == 1:
        result["capital_layer"] = next(iter(capital_layers))
    return result


def build_portfolio(
    orders: list[dict[str, Any]],
    capital: float,
    method: str = "conviction_weighted",
    regime: str = "growth",
) -> dict[str, Any]:
    """Compatibility wrapper for callers using build_portfolio(method=...)."""
    return construct(orders, capital, method=method, regime=regime)


if __name__ == "__main__":
    import json

    test_orders = [
        {
            "ts_code": "600519.SH",
            "belief_score": 0.75,
            "volatility": 0.20,
            "sector": "白酒",
            "price": 1700.0,
        },
        {
            "ts_code": "000858.SZ",
            "belief_score": 0.60,
            "volatility": 0.25,
            "sector": "白酒",
            "price": 150.0,
        },
        {
            "ts_code": "601318.SH",
            "belief_score": 0.55,
            "volatility": 0.18,
            "sector": "保险",
            "price": 50.0,
        },
    ]
    for m in (
        "conviction_weighted",
        "equal_weight",
        "risk_parity",
        "volatility_targeted",
    ):
        print(f"\n=== {m} ===")
        r = construct(test_orders, 1_000_000, method=m, regime="growth")
        print(json.dumps(r, ensure_ascii=False, indent=2))
