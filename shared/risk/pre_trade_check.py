#!/usr/bin/env python3
"""事前风控 — 下单前检查仓位/相关性/板块/流动性。

降权不硬拒, 仅单股 >15% 硬拒。

check(order, portfolio) → {approved, adjustments, reasons}
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]

# 风控参数默认值 (与 risk_limits.yaml 对齐)
_DEFAULT_LIMITS: dict[str, Any] = {
    "single_stock_max": 0.15,
    "sector_max": 0.40,
    "total_exposure_max": 0.80,
    "daily_loss_limit": 0.03,
    "max_positions": 5,
    "correlation_threshold": 0.70,
    "liquidity": {
        "min_turnover_wan": 5000,
        "max_pct_of_volume": 0.05,
    },
}

_LIMITS_PATH = Path(__file__).resolve().parent / "risk_limits.yaml"


def _load_limits() -> dict[str, Any]:
    """加载风控参数。yaml 不可用时回退默认值。"""
    if yaml is None:
        return dict(_DEFAULT_LIMITS)
    try:
        with open(_LIMITS_PATH, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if isinstance(data, dict):
            # 合并默认值 (补齐缺失字段)
            merged = dict(_DEFAULT_LIMITS)
            merged.update(data)
            return merged
    except (OSError, yaml.YAMLError):
        pass
    return dict(_DEFAULT_LIMITS)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        f = float(v)
        return f if f == f else default  # NaN check
    except (TypeError, ValueError):
        return default


def check(order: dict[str, Any], portfolio: dict[str, Any] | None = None, belief_score: float | None = None) -> dict[str, Any]:
    max_pos = 0.15 if belief_score and belief_score > 0.7 else 0.10 if belief_score and belief_score > 0.4 else 0.05
    MIN_WEIGHT = 0.01
    """事前风控检查。

    Args:
        order: {
            "ts_code": str,
            "weight": float,        # 目标权重 (0-1)
            "sector": str,          # 板块
            "turnover_wan": float,  # 日均成交额(万), 可选
            "order_amount_wan": float,  # 下单金额(万), 可选
        }
        portfolio: {
            "positions": [
                {"ts_code": str, "weight": float, "sector": str, "cost": float, ...}
            ],
            "total_exposure": float,
            "daily_pnl_pct": float,  # 当日盈亏比例
            "correlations": {("A", "B"): float},  # 可选
        }

    Returns:
        {
            "approved": bool,
            "adjusted_weight": float,  # 调整后权重
            "adjustments": list[str],  # 调整说明
            "reasons": list[str],      # 拒绝/降权原因
        }
    """
    if not order or not isinstance(order, dict):
        raise ValueError("order must be a non-empty dict")
    if not order.get("ts_code"):
        raise ValueError("order.ts_code is required")

    if portfolio is None:
        portfolio = {}
    positions = portfolio.get("positions", []) or []
    if not isinstance(positions, list):
        positions = []

    limits = _load_limits()
    ts_code = order["ts_code"]
    target_weight = _safe_float(order.get("weight", 0.0))
    sector = str(order.get("sector", "unknown"))
    adjustments: list[str] = []
    reasons: list[str] = []
    adjusted_weight = target_weight
    approved = True

    # --- 硬限: 单股 max 15% ---
    single_max = _safe_float(limits.get("single_stock_max", 0.15))
    # 检查该标的在组合中已有权重
    existing_weight = 0.0
    for p in positions:
        if isinstance(p, dict) and p.get("ts_code") == ts_code:
            existing_weight += _safe_float(p.get("weight", 0.0))

    new_total_single = existing_weight + target_weight
    if new_total_single > single_max + 1e-9:
        # 硬拒: 单股超限
        approved = False
        reasons.append(
            f"硬拒: 单股 {ts_code} 总权重 {new_total_single:.4f} > 单股上限 {single_max:.4f}"
        )
        return {
            "approved": False,
            "adjusted_weight": 0.0,
            "adjustments": [],
            "reasons": reasons,
        }

    # --- 软限: 板块 max 40% ---
    sector_max = _safe_float(limits.get("sector_max", 0.40))
    sector_exposure = existing_weight  # 同标的已有
    for p in positions:
        if isinstance(p, dict) and p.get("sector") == sector and p.get("ts_code") != ts_code:
            sector_exposure += _safe_float(p.get("weight", 0.0))

    new_sector_total = sector_exposure + target_weight
    if new_sector_total > sector_max + 1e-9:
        # 降权: 截断到板块上限
        allowed = max(0.0, sector_max - sector_exposure)
        if allowed < target_weight:
            adjustments.append(
                f"板块降权: {sector} 总敞口 {new_sector_total:.4f} > {sector_max:.4f}, "
                f"权重 {target_weight:.4f} → {allowed:.4f}"
            )
            adjusted_weight = allowed

    # --- 软限: 总敞口 max 80% ---
    total_max = _safe_float(limits.get("total_exposure_max", 0.80))
    current_exposure = _safe_float(portfolio.get("total_exposure", 0.0))
    new_total_exposure = current_exposure + adjusted_weight
    if new_total_exposure > total_max + 1e-9:
        allowed = max(0.0, total_max - current_exposure)
        if allowed < adjusted_weight:
            adjustments.append(
                f"总敞口降权: {new_total_exposure:.4f} > {total_max:.4f}, "
                f"权重 {adjusted_weight:.4f} → {allowed:.4f}"
            )
            adjusted_weight = allowed

    # --- 软限: 持仓数 ---
    max_positions = int(limits.get("max_positions", 5))
    # 已持有的不同标的数
    existing_codes = set()
    for p in positions:
        if isinstance(p, dict) and p.get("ts_code"):
            existing_codes.add(p["ts_code"])
    if ts_code not in existing_codes and len(existing_codes) >= max_positions:
        approved = False
        reasons.append(
            f"硬拒: 持仓数 {len(existing_codes)}已达上限 {max_positions}, 新增 {ts_code} 被拒"
        )
        return {
            "approved": False,
            "adjusted_weight": 0.0,
            "adjustments": [],
            "reasons": reasons,
        }

    # --- 软限: 日亏 3% → 暂停新增 ---
    daily_loss_limit = _safe_float(limits.get("daily_loss_limit", 0.03))
    daily_pnl = _safe_float(portfolio.get("daily_pnl_pct", 0.0))
    if daily_pnl < -daily_loss_limit:
        if ts_code not in existing_codes:
            approved = False
            reasons.append(
                f"暂停新增: 当日亏损 {daily_pnl:.4f} < -{daily_loss_limit:.4f}, 禁止开新仓"
            )
            return {
                "approved": False,
                "adjusted_weight": 0.0,
                "adjustments": [],
                "reasons": reasons,
            }
        else:
            adjustments.append(
                f"日亏警告: 当日亏损 {daily_pnl:.4f}, 仅允许已有持仓调整"
            )

    # --- 软限: 相关性 ---
    corr_threshold = _safe_float(limits.get("correlation_threshold", 0.70))
    correlations = portfolio.get("correlations", {})
    if isinstance(correlations, dict):
        for pair_key, corr_val in correlations.items():
            # pair_key 可能是 "A|B" 或 tuple
            if isinstance(pair_key, str):
                parts = pair_key.split("|")
            elif isinstance(pair_key, (list, tuple)):
                parts = list(pair_key)
            else:
                continue
            if len(parts) == 2 and ts_code in parts:
                corr = _safe_float(corr_val)
                if abs(corr) > corr_threshold:
                    # 高相关按 multiplicative 方式累计降权, 避免后一个覆盖前一个
                    reduction = 0.20
                    new_w = adjusted_weight * (1.0 - reduction)
                    adjustments.append(
                        f"相关性降权: {ts_code} 与 {parts} 相关性 {corr:.3f} > {corr_threshold:.3f}, "
                        f"权重 {adjusted_weight:.4f} → {new_w:.4f}"
                    )
                    adjusted_weight = new_w

    # --- 软限: 流动性 ---
    liq = limits.get("liquidity", {})
    if isinstance(liq, dict):
        min_turnover = _safe_float(liq.get("min_turnover_wan", 5000))
        turnover = _safe_float(order.get("turnover_wan", 0.0))
        if turnover > 0 and turnover < min_turnover:
            # 流动性不足降权 30%
            new_w = adjusted_weight * 0.7
            adjustments.append(
                f"流动性降权: {ts_code} 日均成交 {turnover:.0f}万 < {min_turnover:.0f}万, "
                f"权重 {adjusted_weight:.4f} → {new_w:.4f}"
            )
            adjusted_weight = new_w

        max_pct_vol = _safe_float(liq.get("max_pct_of_volume", 0.05))
        order_amount = _safe_float(order.get("order_amount_wan", 0.0))
        if turnover > 0 and order_amount > 0:
            pct_vol = order_amount / turnover
            if pct_vol > max_pct_vol:
                # 单笔占比过高, 降权
                scale = max_pct_vol / pct_vol
                new_w = adjusted_weight * scale
                adjustments.append(
                    f"流动性降权: 单笔占比 {pct_vol:.3f} > {max_pct_vol:.3f}, "
                    f"权重 {adjusted_weight:.4f} → {new_w:.4f}"
                )
                adjusted_weight = new_w

    # 权重不能为负
    adjusted_weight = max(0.0, adjusted_weight)

    if not adjustments and adjusted_weight == target_weight:
        adjustments.append(f"通过: 权重 {target_weight:.4f} 无需调整")

    return {
        "approved": approved,
        "adjusted_weight": round(adjusted_weight, 6),
        "adjustments": adjustments,
        "reasons": reasons,
    }


if __name__ == "__main__":
    import json
    test_order = {"ts_code": "600519.SH", "weight": 0.12, "sector": "白酒", "turnover_wan": 30000}
    test_portfolio = {
        "positions": [
            {"ts_code": "000858.SZ", "weight": 0.10, "sector": "白酒"},
            {"ts_code": "601318.SH", "weight": 0.15, "sector": "保险"},
        ],
        "total_exposure": 0.25,
        "daily_pnl_pct": -0.01,
    }
    r = check(test_order, test_portfolio)
    print(json.dumps(r, ensure_ascii=False, indent=2))
