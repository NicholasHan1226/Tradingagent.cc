#!/usr/bin/env python3
"""Email template 4: Closing plan (14:30).

Positions + closing actions + overnight risk.
"""

from __future__ import annotations

from typing import Any

from . import (
    wrap_html,
    _decision_strip,
    _execution_boundary,
    _section,
    _summary_cards,
    _table,
    _summary_box,
    _pnl_color,
    _format_pct,
    _format_amount,
)


def render(data: dict[str, Any]) -> str:
    """Render closing plan email HTML.

    Args:
        data: dict with keys:
            - holdings: list of {ts_code, name, quantity, current_price, pnl_pct, overnight_risk}
            - closing_actions: list of {action, ts_code, reason}
            - overnight_risk: {level, factors, recommendation}
            - total_pnl, total_pnl_pct

    Returns:
        HTML string.
    """
    holdings = data.get("holdings", [])
    actions = data.get("closing_actions", [])
    risk = data.get("overnight_risk", {})
    total_pnl = data.get("total_pnl", 0)
    total_pnl_pct = data.get("total_pnl_pct", 0)
    primary_action = actions[0] if actions else {}

    color = _pnl_color(total_pnl)

    # Summary
    summary_html = _summary_box("日内盈亏", f'<span class="{color}">¥{_format_amount(total_pnl)}</span>', _format_pct(total_pnl_pct))

    # Holdings with overnight risk
    holding_rows = []
    for h in holdings:
        h_pnl = h.get("pnl_pct", 0)
        h_color = _pnl_color(h_pnl)
        risk_level = h.get("overnight_risk", "低")
        risk_badge = "badge-alert" if risk_level in ("中", "medium") else "badge-critical" if risk_level in ("高", "high") else "badge-hold"
        holding_rows.append([
            h.get("ts_code", ""),
            h.get("name", ""),
            _format_amount(h.get("current_price")),
            f'<span class="{h_color}">{_format_pct(h_pnl)}</span>',
            f'<span class="badge {risk_badge}">{risk_level}</span>',
        ])
    holdings_html = _table(["代码", "名称", "现价", "浮盈亏", "隔夜风险"], holding_rows) if holding_rows else "<p>暂无持仓</p>"

    # Closing actions
    action_rows = []
    for a in actions:
        action_label = {"sell": "减仓", "buy": "加仓", "hold": "持有", "close": "清仓"}.get(a.get("action", ""), a.get("action", ""))
        action_rows.append([action_label, a.get("ts_code", ""), a.get("reason", "")])
    actions_html = _table(["动作", "标的", "原因"], action_rows) if action_rows else "<p>尾盘无操作</p>"

    # Overnight risk assessment
    risk_html = ""
    if risk:
        risk_html = _table(
            ["维度", "评估"],
            [
                ["风险等级", risk.get("level", "--")],
                ["风险因素", risk.get("factors", "--")],
                ["建议", risk.get("recommendation", "--")],
            ]
        )

    body = (
        _summary_cards(
            {
                "title": primary_action.get("action", data.get("action_summary", "尾盘无操作" if not actions else "执行尾盘计划")),
                "detail": primary_action.get("reason", data.get("action_reason", "只处理收盘前仍有效的动作")),
            },
            {
                "title": data.get("max_loss", risk.get("level", "隔夜风险未定")),
                "detail": f"最坏情形 {data.get('worst_case', risk.get('factors', '--'))}；建议 {risk.get('recommendation', '--')}",
            },
            {
                "title": data.get("capital_summary", f"持仓 {len(holdings)} 个"),
                "detail": f"日内盈亏 ¥{_format_amount(total_pnl)}；剩余预算 {data.get('remaining_budget', '--')}",
            },
        ) +
        _section("持仓概览", summary_html) +
        _section("尾盘操作", actions_html) +
        _section("隔夜持仓", holdings_html) +
        (_section("隔夜风险评估", risk_html) if risk_html else "")
    )

    priority = _decision_strip(
        data,
        default_action="ACT" if actions else "WAIT",
        default_reason=data.get("decision_reason", "尾盘窗口短，优先处理隔夜风险"),
        default_deadline=data.get("deadline", "14:57"),
        default_needs="Nicholas: 收盘前确认是否保留隔夜仓位",
    ) + _execution_boundary(data)
    return wrap_html("尾盘规划", "Closing Plan", body, priority_content=priority)
