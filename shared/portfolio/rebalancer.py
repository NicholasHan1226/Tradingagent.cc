#!/usr/bin/env python3
"""再平衡 — regime 变化或相关性偏移时触发。

check_rebalance(portfolio, regime) → {needs_rebalance, suggested_changes, reason}
"""
from __future__ import annotations

from typing import Any

from position_sizer import _regime_weight, _safe_float


# regime 变化阈值: 不同 regime 权重差异超过此值则触发再平衡
_REGIME_DRIFT_THRESHOLD = 0.15

# 相关性偏移阈值
_CORRELATION_DRIFT_THRESHOLD = 0.15

# 权重偏移阈值 (当前权重 vs 目标权重)
_WEIGHT_DRIFT_THRESHOLD = 0.03


def check_rebalance(
    portfolio: dict[str, Any],
    regime: str | None = None,
    prev_regime: str | None = None,
    current_correlations: dict[str, Any] | None = None,
    target_correlations: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """再平衡检查主函数。

    Args:
        portfolio: {
            "positions": [{"ts_code", "weight", "sector", "belief_score", "volatility", ...}],
            "regime": str,             # 组合构建时的 regime (可选)
            "method": str,             # 构建方法
        }
        regime: 当前 regime
        prev_regime: 之前的 regime (可选, 缺省用 portfolio.regime)
        current_correlations: 当前相关性矩阵 {("A","B"): corr}
        target_correlations: 目标相关性矩阵

    Returns:
        {
            "needs_rebalance": bool,
            "reason": str,
            "suggested_changes": list[dict],  # 建议调整
            "urgency": str,                   # "high" | "medium" | "low"
        }
    """
    if not isinstance(portfolio, dict):
        portfolio = {}

    positions = portfolio.get("positions", []) or []
    if not isinstance(positions, list):
        positions = []

    reasons: list[str] = []
    suggested_changes: list[dict[str, Any]] = []
    urgency = "low"
    needs_rebalance = False

    # 1. Regime 变化检查
    old_regime = prev_regime or portfolio.get("regime")
    if regime and old_regime and regime != old_regime:
        old_weight = _regime_weight(old_regime)
        new_weight = _regime_weight(regime)
        drift = abs(new_weight - old_weight)

        if drift >= _REGIME_DRIFT_THRESHOLD:
            needs_rebalance = True
            reasons.append(
                f"Regime 变化: {old_regime}→{regime}, "
                f"权益倾斜 {old_weight:.3f}→{new_weight:.3f} (偏移 {drift:.3f})"
            )
            # 根据偏移幅度确定 urgency
            if drift >= 0.40:
                urgency = "high"
            elif drift >= 0.25:
                urgency = "medium"

            # 建议: 按新 regime 重新计算目标权重
            scale = new_weight / old_weight if old_weight > 0 else 1.0
            for p in positions:
                if not isinstance(p, dict):
                    continue
                ts_code = p.get("ts_code", "")
                current_w = _safe_float(p.get("weight", 0.0))
                target_w = current_w * scale
                # 裁剪到单股上限
                target_w = min(target_w, 0.15)
                drift_w = abs(target_w - current_w)
                if drift_w >= _WEIGHT_DRIFT_THRESHOLD:
                    direction = "加仓" if target_w > current_w else "减仓"
                    suggested_changes.append({
                        "ts_code": ts_code,
                        "action": "adjust",
                        "direction": direction,
                        "current_weight": round(current_w, 6),
                        "target_weight": round(target_w, 6),
                        "reason": f"regime 变化 {direction}",
                    })

    # 2. 相关性偏移检查
    if current_correlations and target_correlations:
        if isinstance(current_correlations, dict) and isinstance(target_correlations, dict):
            corr_drifts: list[tuple[str, float]] = []
            for pair, target_corr in target_correlations.items():
                curr_corr = current_correlations.get(pair)
                if curr_corr is not None:
                    drift = abs(_safe_float(curr_corr) - _safe_float(target_corr))
                    if drift >= _CORRELATION_DRIFT_THRESHOLD:
                        corr_drifts.append((str(pair), drift))

            if corr_drifts:
                needs_rebalance = True
                reasons.append(
                    f"相关性偏移: {len(corr_drifts)} 对相关性偏移 ≥ {_CORRELATION_DRIFT_THRESHOLD}"
                )
                if urgency == "low":
                    urgency = "medium"
                # 高相关对建议降权
                for pair_str, drift in corr_drifts:
                    parts = pair_str.split("|") if "|" in pair_str else pair_str.split(",")
                    for p in positions:
                        if isinstance(p, dict) and p.get("ts_code") in parts:
                            current_w = _safe_float(p.get("weight", 0.0))
                            target_w = current_w * 0.8  # 降权 20%
                            if abs(target_w - current_w) >= _WEIGHT_DRIFT_THRESHOLD:
                                suggested_changes.append({
                                    "ts_code": p.get("ts_code"),
                                    "action": "adjust",
                                    "direction": "减仓",
                                    "current_weight": round(current_w, 6),
                                    "target_weight": round(target_w, 6),
                                    "reason": f"相关性偏移 {pair_str} drift={drift:.3f}",
                                })

    # 3. 权重自然漂移检查 (简化: 检查是否超总敞口上限)
    total_exposure = sum(
        _safe_float(p.get("weight", 0.0)) for p in positions if isinstance(p, dict)
    )
    if total_exposure > 0.80 + 1e-9:
        needs_rebalance = True
        reasons.append(f"总敞口漂移: {total_exposure:.4f} > 0.80 上限")
        if urgency == "low":
            urgency = "medium"
        # 建议按比例缩减
        scale = 0.80 / total_exposure if total_exposure > 0 else 1.0
        for p in positions:
            if isinstance(p, dict):
                current_w = _safe_float(p.get("weight", 0.0))
                target_w = current_w * scale
                if abs(target_w - current_w) >= _WEIGHT_DRIFT_THRESHOLD:
                    suggested_changes.append({
                        "ts_code": p.get("ts_code"),
                        "action": "adjust",
                        "direction": "减仓",
                        "current_weight": round(current_w, 6),
                        "target_weight": round(target_w, 6),
                        "reason": "总敞口超限缩减",
                    })

    reason = "; ".join(reasons) if reasons else "无需再平衡"

    return {
        "needs_rebalance": needs_rebalance,
        "reason": reason,
        "suggested_changes": suggested_changes,
        "urgency": urgency,
        "current_regime": regime,
        "prev_regime": old_regime,
        "total_exposure": round(total_exposure, 6),
    }


if __name__ == "__main__":
    import json
    test_portfolio = {
        "positions": [
            {"ts_code": "600519.SH", "weight": 0.12, "sector": "白酒", "belief_score": 0.75},
            {"ts_code": "000858.SZ", "weight": 0.10, "sector": "白酒", "belief_score": 0.60},
            {"ts_code": "601318.SH", "weight": 0.10, "sector": "保险", "belief_score": 0.55},
        ],
        "regime": "growth",
        "method": "conviction_weighted",
    }
    # 测试 regime 变化
    r = check_rebalance(test_portfolio, regime="recession", prev_regime="growth")
    print(json.dumps(r, ensure_ascii=False, indent=2))
