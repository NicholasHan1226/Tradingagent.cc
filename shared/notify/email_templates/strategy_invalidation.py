#!/usr/bin/env python3
"""Email template 10: Strategy invalidation (regime change).

Change + impact + adjustment.
"""

from __future__ import annotations

from typing import Any

from . import wrap_html, _section, _table, _summary_box


def render(data: dict[str, Any]) -> str:
    """Render strategy invalidation email HTML.

    Args:
        data: dict with keys:
            - regime_change: {from: str, to: str, trigger: str}
            - invalidated_strategies: list of {name, reason, action}
            - impact: {affected_positions, estimated_loss, risk_level}
            - adjustment: {new_allocation, new_focus, action_plan}

    Returns:
        HTML string.
    """
    regime = data.get("regime_change", {})
    invalidated = data.get("invalidated_strategies", [])
    impact = data.get("impact", {})
    adjustment = data.get("adjustment", {})

    # Regime change summary
    from_regime = regime.get("from", "--")
    to_regime = regime.get("to", "--")
    trigger = regime.get("trigger", "--")

    summary_html = (
        _summary_box("环境变化", f"{from_regime} → {to_regime}", trigger) +
        _summary_box("风险等级", impact.get("risk_level", "中"))
    )

    # Invalidated strategies
    inv_rows = []
    for s in invalidated:
        action_label = {"pause": "暂停", "close": "清仓", "reduce": "减仓", "adjust": "调整"}.get(s.get("action", ""), s.get("action", ""))
        inv_rows.append([s.get("name", ""), s.get("reason", ""), action_label])
    inv_html = _table(["策略", "失效原因", "动作"], inv_rows) if inv_rows else "<p>暂无策略失效</p>"

    # Impact assessment
    impact_rows = [
        ["影响持仓", impact.get("affected_positions", "--")],
        ["预估损失", impact.get("estimated_loss", "--")],
        ["风险等级", impact.get("risk_level", "--")],
    ]
    impact_html = _table(["维度", "评估"], impact_rows)

    # Adjustment plan
    adjust_html = ""
    if adjustment:
        adjust_rows = [
            ["新配置", adjustment.get("new_allocation", "--")],
            ["新重点", adjustment.get("new_focus", "--")],
            ["行动计划", adjustment.get("action_plan", "--")],
        ]
        adjust_html = _table(["项目", "调整"], adjust_rows)

    body = (
        _section("环境变化", summary_html) +
        _section("失效策略", inv_html) +
        _section("影响评估", impact_html) +
        (_section("调整方案", adjust_html) if adjust_html else "")
    )

    return wrap_html("策略失效通知", "Strategy Invalidation", body)
