#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Return attribution for the review system.

Decomposes portfolio PnL into contributions by:
  - dimension  : 宏观/事件/基本面/资金/技术/情绪 (六维)
  - strategy   : 回调/趋势/突破/事件驱动/...
  - condition  : 单仓/行业/时段/波动率 regime

Each trade is expected to carry tags so we can bucket its PnL.
Trades without tags fall into "unattributed".

Trade schema (minimal fields used):
    {
      "pnl": float,                      # 已实现盈亏( decimal )
      "dimension": "macro|event|...",    # 主导维度 (optional)
      "dimensions": {"macro": 0.4, ...}, # 权重式多维 (optional, overrides dimension)
      "strategy": "pullback|trend|...",  # 策略 (optional)
      "condition": "low_vol|high_vol|...", # 条件 (optional)
    }
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

DEFAULT_DIMENSIONS = ["macro", "event", "fundamental", "moneyflow", "technical", "sentiment"]


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        f = float(v)
        if f != f:  # NaN
            return default
        return f
    except (TypeError, ValueError):
        return default


def _normalize_weights(weights: dict[str, float]) -> dict[str, float]:
    """Normalize a weight dict so values sum to 1.0 (guard against zero sum)."""
    total = sum(weights.values())
    if total <= 0:
        # equal weights
        n = len(weights) if weights else 1
        return {k: 1.0 / n for k in weights} if n else {}
    return {k: v / total for k, v in weights.items()}


def attribute(trades: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    """Attribute total PnL to dimensions / strategies / conditions.

    Args:
        trades: list of trade dicts (see module docstring for schema).

    Returns:
        {
          "total_pnl": float,
          "by_dimension": {"macro": 0.12, "event": -0.03, "unattributed": 0.0, ...},
          "by_strategy": {"pullback": 0.08, "trend": 0.04, "unattributed": 0.0, ...},
          "by_condition": {"low_vol": 0.05, "high_vol": -0.01, "unattributed": 0.0, ...},
          "trade_count": int,
          "attributed_count": int,
        }

    Notes:
        - by_dimension values are PnL contributions (not percentages); caller can
          divide by total_pnl to get percentages.
        - If a trade carries "dimensions" (weight dict), its pnl is split across
          those dimensions proportionally. If only "dimension" (single string),
          full pnl goes to that dimension.
    """
    total_pnl = 0.0
    by_dim: dict[str, float] = defaultdict(float)
    by_strat: dict[str, float] = defaultdict(float)
    by_cond: dict[str, float] = defaultdict(float)
    attributed = 0

    for t in trades or []:
        pnl = _safe_float(t.get("pnl"))
        total_pnl += pnl

        # --- dimension ---
        dims = t.get("dimensions")
        if isinstance(dims, dict) and dims:
            norm = _normalize_weights({k: _safe_float(v) for k, v in dims.items()})
            for k, w in norm.items():
                by_dim[k] += pnl * w
            attributed += 1
        else:
            d = t.get("dimension")
            if d:
                by_dim[str(d)] += pnl
                attributed += 1
            else:
                by_dim["unattributed"] += pnl

        # --- strategy ---
        s = t.get("strategy")
        if s:
            by_strat[str(s)] += pnl
        else:
            by_strat["unattributed"] += pnl

        # --- condition ---
        c = t.get("condition")
        if c:
            by_cond[str(c)] += pnl
        else:
            by_cond["unattributed"] += pnl

    # ensure all default dimensions appear (zero if no trades)
    for d in DEFAULT_DIMENSIONS:
        by_dim.setdefault(d, 0.0)
    by_dim.setdefault("unattributed", 0.0)
    by_strat.setdefault("unattributed", 0.0)
    by_cond.setdefault("unattributed", 0.0)

    return {
        "total_pnl": round(total_pnl, 6),
        "by_dimension": {k: round(v, 6) for k, v in by_dim.items()},
        "by_strategy": {k: round(v, 6) for k, v in by_strat.items()},
        "by_condition": {k: round(v, 6) for k, v in by_cond.items()},
        "trade_count": len(trades or []),
        "attributed_count": attributed,
    }


def attribute_pct(trades: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    """Same as attribute() but each bucket expressed as % of total_pnl.

    Useful for "which dimension contributed x%" reporting. If total_pnl == 0,
    all buckets are 0.0 (avoids division by zero).
    """
    base = attribute(trades)
    total = base["total_pnl"]
    out: dict[str, dict[str, float]] = {"total_pnl": total}
    for key in ("by_dimension", "by_strategy", "by_condition"):
        bucket = base[key]
        if abs(total) < 1e-12:
            out[key] = {k: 0.0 for k in bucket}
        else:
            out[key] = {k: round(v / total, 6) for k, v in bucket.items()}
    out["trade_count"] = base["trade_count"]
    out["attributed_count"] = base["attributed_count"]
    return out


# ---- self-test --------------------------------------------------------------

if __name__ == "__main__":
    sample = [
        {"pnl": 0.05, "dimensions": {"macro": 0.6, "technical": 0.4}, "strategy": "pullback", "condition": "low_vol"},
        {"pnl": -0.02, "dimension": "event", "strategy": "event_driven", "condition": "high_vol"},
        {"pnl": 0.03, "dimensions": {"technical": 1.0}, "strategy": "trend", "condition": "mid_vol"},
        {"pnl": 0.01, "strategy": "breakout"},  # no dimension
    ]
    import json
    print("attribute:", json.dumps(attribute(sample), ensure_ascii=False, indent=2))
    print("attribute_pct:", json.dumps(attribute_pct(sample), ensure_ascii=False, indent=2))
