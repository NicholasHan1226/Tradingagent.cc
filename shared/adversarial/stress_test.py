#!/usr/bin/env python3
"""情景压力测试 — worst-case 情景下的回撤与恢复时间估计。

三情景:
  1. regime_reversal     — regime 反转 (如 growth→recession)
  2. sector_invalidation — 板块逻辑证伪
  3. market_crash        — 大盘 -10%

输出 {scenario, max_drawdown, recovery_time}。
用 worst-case, 不用 expected-case。
"""
from __future__ import annotations

from typing import Any

# 情景定义: name → 描述 + 历史先验回撤区间
_SCENARIOS: dict[str, dict[str, Any]] = {
    "regime_reversal": {
        "description": "Regime 反转 (如 growth→recession 或 disinflation→stagflation)",
        # 历史上 regime 反转导致权益类资产 worst-case 回撤约 -15% ~ -35%
        "drawdown_range": (-0.35, -0.15),
        "recovery_months_range": (6, 24),
    },
    "sector_invalidation": {
        "description": "板块逻辑证伪 (如政策取消/技术路线变更/需求证伪)",
        # 板块逻辑证伪 worst-case 回撤约 -25% ~ -50%
        "drawdown_range": (-0.50, -0.25),
        "recovery_months_range": (12, 36),
    },
    "market_crash": {
        "description": "大盘 -10% (系统性回调)",
        # 大盘 -10% 时个股 beta 加成, worst-case -15% ~ -30%
        "drawdown_range": (-0.30, -0.15),
        "recovery_months_range": (3, 12),
    },
}

# 默认情景列表
DEFAULT_SCENARIOS = list(_SCENARIOS.keys())


def _estimate_drawdown(scenario_key: str, scores: dict[str, Any] | None = None) -> tuple[float, int]:
    """估计给定情景的 worst-case 回撤和恢复时间(月)。

    用历史先验区间, 如果有 scores 则按 beta/估值做调整。
    """
    sc = _SCENARIOS.get(scenario_key)
    if sc is None:
        return -0.20, 12  # 未知情景保守估计

    dd_lo, dd_hi = sc["drawdown_range"]  # dd_lo 更负 (更差)
    rec_lo, rec_hi = sc["recovery_months_range"]

    # 默认取 worst-case (更负的端)
    drawdown = dd_lo
    recovery = rec_hi

    # 如果有 scores, 做简单调整
    if scores and isinstance(scores, dict):
        # 估值偏高 → 回撤更大
        fund = scores.get("fundamental")
        if isinstance(fund, dict):
            pe_note = str(fund.get("note", "")).lower()
            if "高估" in pe_note or "高估值" in pe_note or "overvalued" in pe_note:
                drawdown = max(drawdown * 1.2, dd_lo * 1.15)  # 放大 15-20%
                recovery = int(recovery * 1.2)

        # 技术面弱 → 恢复更慢
        tech = scores.get("technical")
        if isinstance(tech, dict):
            tech_score = tech.get("score", 0.5)
            try:
                if float(tech_score) < 0.4:
                    recovery = int(recovery * 1.15)
            except (TypeError, ValueError):
                pass

        # 资金面弱 → 回撤更大
        capital = scores.get("capital")
        if isinstance(capital, dict):
            capital_score = capital.get("score", 0.5)
            try:
                if float(capital_score) < 0.4:
                    drawdown = max(drawdown * 1.1, dd_lo * 1.05)
            except (TypeError, ValueError):
                pass

    # 裁剪到合理范围
    drawdown = max(-0.60, min(-0.05, drawdown))
    recovery = max(1, min(48, int(recovery)))

    return round(drawdown, 4), recovery


def stress_test(
    ts_code: str,
    scenarios: list[str] | None = None,
    scores: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """情景压力测试主函数。

    Args:
        ts_code: 标的代码
        scenarios: 情景列表, 默认 ["regime_reversal", "sector_invalidation", "market_crash"]
        scores: 可选的六维打分, 用于调整估计

    Returns:
        list of {
            "ts_code": str,
            "scenario": str,
            "description": str,
            "max_drawdown": float,  # 负数, 如 -0.25
            "recovery_time": int,   # 月
        }
    """
    if not ts_code:
        raise ValueError("ts_code is required")

    if scenarios is None:
        scenarios = DEFAULT_SCENARIOS
    elif isinstance(scenarios, str):
        scenarios = [scenarios]

    results: list[dict[str, Any]] = []
    for sc_key in scenarios:
        sc_def = _SCENARIOS.get(sc_key)
        if sc_def is None:
            # 未知情景: 用保守默认
            drawdown, recovery = _estimate_drawdown(sc_key, scores)
            results.append({
                "ts_code": ts_code,
                "scenario": sc_key,
                "description": f"未知情景: {sc_key}",
                "max_drawdown": drawdown,
                "recovery_time": recovery,
            })
            continue

        drawdown, recovery = _estimate_drawdown(sc_key, scores)
        results.append({
            "ts_code": ts_code,
            "scenario": sc_key,
            "description": sc_def["description"],
            "max_drawdown": drawdown,
            "recovery_time": recovery,
        })

    return results


def worst_case(results: list[dict[str, Any]]) -> dict[str, Any]:
    """从压力测试结果中提取 worst-case (最大回撤)。"""
    if not results:
        return {"scenario": "none", "max_drawdown": 0.0, "recovery_time": 0}
    worst = min(results, key=lambda r: r.get("max_drawdown", 0.0))
    return {
        "scenario": worst.get("scenario", ""),
        "max_drawdown": worst.get("max_drawdown", 0.0),
        "recovery_time": worst.get("recovery_time", 0),
    }


if __name__ == "__main__":
    import json
    test_scores = {
        "fundamental": {"score": 0.6, "note": "估值偏高"},
        "technical": {"score": 0.3, "note": "趋势走弱"},
        "capital": {"score": 0.3, "note": "主力流出"},
    }
    r = stress_test("600519.SH", scores=test_scores)
    print(json.dumps(r, ensure_ascii=False, indent=2))
    print("--- worst case ---")
    print(json.dumps(worst_case(r), ensure_ascii=False, indent=2))
