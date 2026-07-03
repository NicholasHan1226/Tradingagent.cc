#!/usr/bin/env python3
"""Email template 2: Trading signal (on trigger).

Stock + condition + scores + action + position size.
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
    _format_pct,
    _format_amount,
)


def render(data: dict[str, Any]) -> str:
    """Render trading signal email HTML.

    Args:
        data: dict with keys:
            - ts_code, name, current_price
            - trigger_condition
            - scores: {macro, event, fundamental, moneyflow, technical, sentiment, total}
            - action: "buy" | "sell" | "hold"
            - position_size: {shares, amount, pct_of_capital}
            - stop_loss, take_profit

    Returns:
        HTML string.
    """
    ts_code = data.get("ts_code", "")
    name = data.get("name", "")
    price = data.get("current_price", 0)
    action = data.get("action", "hold")
    condition = data.get("trigger_condition", "")
    scores = data.get("scores", {})
    position = data.get("position_size", {})
    stop_loss = data.get("stop_loss")
    take_profit = data.get("take_profit")

    action_label = {"buy": "买入", "sell": "卖出", "hold": "持有"}.get(action, action)
    badge_class = {"buy": "badge-buy", "sell": "badge-sell", "hold": "badge-hold"}.get(action, "badge-hold")

    # Signal summary
    signal_html = (
        _summary_box("标的", f"{ts_code} {name}", f"现价 ¥{price}") +
        _summary_box("操作", f'<span class="badge {badge_class}">{action_label}</span>', f"触发条件: {condition}")
    )

    # Scores table
    score_rows = [
        ["宏观", scores.get("macro", "--")],
        ["事件", scores.get("event", "--")],
        ["基本面", scores.get("fundamental", "--")],
        ["资金", scores.get("moneyflow", "--")],
        ["技术", scores.get("technical", "--")],
        ["情绪", scores.get("sentiment", "--")],
        ["综合", f'<strong>{scores.get("total", "--")}</strong>'],
    ]
    scores_html = _table(["维度", "评分"], score_rows)

    # Position sizing
    pos_html = ""
    if position:
        pos_html = _table(
            ["项目", "数值"],
            [
                ["股数", position.get("shares", "--")],
                ["金额", f"¥{_format_amount(position.get('amount', 0))}"],
                ["仓位占比", _format_pct(position.get("pct_of_capital"))],
            ]
        )

    # Risk levels
    risk_html = ""
    if stop_loss or take_profit:
        risk_rows = []
        if stop_loss:
            risk_rows.append(["止损", f"¥{stop_loss}"])
        if take_profit:
            risk_rows.append(["止盈", f"¥{take_profit}"])
        risk_html = _table(["项目", "价格"], risk_rows)

    body = (
        _summary_cards(
            {
                "title": f"{action_label} {ts_code} {name}".strip(),
                "detail": f"{condition or '触发条件未提供'}；有效期到 {data.get('expires_at', '--')}",
            },
            {
                "title": data.get("max_loss", f"止损 {stop_loss if stop_loss else '--'}"),
                "detail": f"最坏情形 {data.get('worst_case', '--')}；置信度 {data.get('confidence', scores.get('total', '--'))}",
            },
            {
                "title": f"¥{_format_amount(position.get('amount', data.get('position_amount', 0)))}",
                "detail": f"股数 {position.get('shares', '--')}；仓位 {_format_pct(position.get('pct_of_capital'))}",
            },
        ) +
        _section("信号概要", signal_html) +
        _section("六维评分", scores_html) +
        (_section("仓位建议", pos_html) if pos_html else "") +
        (_section("风控位", risk_html) if risk_html else "")
    )

    default_decision = "ACT" if action in ("buy", "sell") else "WAIT"
    priority = _decision_strip(
        data,
        default_action=default_decision,
        default_reason=data.get("decision_reason", "高优先级信号，需按风险预算复核"),
        default_deadline=data.get("expires_at", "--"),
        default_needs="Nicholas: 下单前确认市场、资金层和回执状态",
    ) + _execution_boundary(data)
    return wrap_html("交易信号", "Trading Signal", body, priority_content=priority)
