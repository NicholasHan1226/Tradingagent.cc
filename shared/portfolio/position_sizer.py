#!/usr/bin/env python3
"""仓位分配 — size = deterministic_score × volatility_factor × regime_weight。

volatility_factor: 高波动降权, 低波动升权 (反比调整)。
regime_weight: 不同 regime 下权益类资产的倾斜系数。

``deterministic_score`` 必须来自冻结的规则/模型输出。LLM evidence 不得作为
该函数输入，也不得通过别名字段间接影响仓位。
"""

from __future__ import annotations

from typing import Any

# Regime → 权益类资产倾斜系数 (Dalio 4 象限)
# growth + disinflation → 权益最优 (1.0-1.2)
# growth + inflation → 权益尚可 (0.8-1.0)
# recession + disinflation → 权益减配 (0.4-0.6)
# recession + inflation → 权益最差 (0.2-0.4)
_REGIME_WEIGHTS: dict[str, float] = {
    "growth": 1.0,  # 默认 growth (假设 disinflation)
    "growth_disinflation": 1.15,
    "growth_inflation": 0.85,
    "recession": 0.45,
    "recession_disinflation": 0.55,
    "recession_inflation": 0.30,
    "stagflation": 0.30,
    "unknown": 0.70,
}

# 波动率参考基准 (A股大盘年化约 20%)
_VOL_BASELINE = 0.20


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        f = float(v)
        return f if f == f else default
    except (TypeError, ValueError):
        return default


def _volatility_factor(volatility: float) -> float:
    """波动率因子: 高波动降权, 低波动升权。

    factor = baseline / volatility, 裁剪到 [0.3, 2.0]。
    """
    if volatility <= 0:
        return 1.0  # 无波动数据, 中性
    factor = _VOL_BASELINE / volatility
    return max(0.3, min(2.0, factor))


def _regime_weight(regime: str | None) -> float:
    """获取 regime 倾斜系数。"""
    if not regime:
        return _REGIME_WEIGHTS["unknown"]
    r = regime.lower().strip()
    # 精确匹配
    if r in _REGIME_WEIGHTS:
        return _REGIME_WEIGHTS[r]
    # 模糊匹配
    if "growth" in r and "inflation" in r:
        return _REGIME_WEIGHTS["growth_inflation"]
    if "growth" in r and "disinflation" in r:
        return _REGIME_WEIGHTS["growth_disinflation"]
    if "recession" in r and "inflation" in r:
        return _REGIME_WEIGHTS["recession_inflation"]
    if "recession" in r and "disinflation" in r:
        return _REGIME_WEIGHTS["recession_disinflation"]
    if "growth" in r:
        return _REGIME_WEIGHTS["growth"]
    if "recession" in r:
        return _REGIME_WEIGHTS["recession"]
    if "stagflation" in r:
        return _REGIME_WEIGHTS["stagflation"]
    return _REGIME_WEIGHTS["unknown"]


def size_position(
    deterministic_score: float,
    volatility: float,
    regime: str | None = "growth",
) -> float:
    """仓位分配主函数。

    size = deterministic_score × volatility_factor × regime_weight

    Args:
        deterministic_score: 冻结规则/模型的未校准排序分 [0, 1]
        volatility: 年化波动率, 如 0.25
        regime: 当前 regime

    Returns:
        position_size_pct: 目标仓位比例 [0, 0.15] (单股上限 15%)
    """
    score = _safe_float(deterministic_score, 0.5)
    score = max(0.0, min(1.0, score))

    vol = _safe_float(volatility, 0.20)
    vol_factor = _volatility_factor(vol)

    reg_w = _regime_weight(regime)

    # 基础仓位 = deterministic score × vol_factor × regime_weight
    # 基础最大仓位假设 20% (会被单股上限 15% 裁剪)
    base_max = 0.20
    raw_size = score * vol_factor * reg_w * base_max

    # 单股上限 15% (硬限, 与 risk_limits.yaml 对齐)
    single_max = 0.15
    size = min(raw_size, single_max)

    return round(max(0.0, size), 6)


def size_positions_batch(
    candidates: list[dict[str, Any]],
    regime: str | None = "growth",
) -> list[dict[str, Any]]:
    """批量仓位分配。

    Args:
        candidates: list of {ts_code, rank_score, volatility, ...}
        regime: 当前 regime

    Returns:
        list of {ts_code, position_size_pct, rank_score, volatility_factor, regime_weight}
    """
    results: list[dict[str, Any]] = []
    for c in candidates:
        if not isinstance(c, dict) or not c.get("ts_code"):
            continue
        # ``belief_score`` is a temporary read-only compatibility fallback for
        # older callers. New code must provide ``rank_score``.
        rank_score = _safe_float(
            c.get("rank_score", c.get("deterministic_score", c.get("belief_score"))),
            0.5,
        )
        vol = _safe_float(c.get("volatility"), 0.20)
        size = size_position(rank_score, vol, regime)
        results.append(
            {
                "ts_code": c["ts_code"],
                "position_size_pct": size,
                "rank_score": rank_score,
                "score_semantics": "uncalibrated_deterministic_rank_score",
                "volatility_factor": round(_volatility_factor(vol), 4),
                "regime_weight": round(_regime_weight(regime), 4),
            }
        )
    return results


if __name__ == "__main__":
    import json

    # 不同 deterministic score × vol × regime 组合
    test_cases = [
        (0.80, 0.15, "growth"),  # 高信心 + 低波动 + growth → 大仓位
        (0.50, 0.30, "growth"),  # 中信心 + 高波动 + growth
        (0.70, 0.20, "recession"),  # 高信心 + 正常波动 + recession → 小仓位
        (0.60, 0.25, "stagflation"),  # 中信心 + 高波动 + stagflation → 很小仓位
    ]
    for score, vol, regime in test_cases:
        size = size_position(score, vol, regime)
        print(f"score={score}, vol={vol}, regime={regime} → size={size:.4f}")

    print("\n=== batch ===")
    candidates = [
        {"ts_code": "600519.SH", "rank_score": 0.75, "volatility": 0.20},
        {"ts_code": "000858.SZ", "rank_score": 0.60, "volatility": 0.25},
    ]
    print(
        json.dumps(
            size_positions_batch(candidates, "growth"), ensure_ascii=False, indent=2
        )
    )
