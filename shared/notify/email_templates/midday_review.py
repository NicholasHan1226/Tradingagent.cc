#!/usr/bin/env python3
"""Email template 3: Midday review (11:35).

Morning performance + afternoon plan.
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
    """Render midday review email HTML.

    Args:
        data: dict with keys:
            - morning_pnl: total P&L in yuan
            - morning_pnl_pct: P&L percentage
            - morning_trades: list of {ts_code, side, quantity, price, pnl}
            - holdings: list of {ts_code, name, quantity, current_price, pnl_pct}
            - afternoon_plan: list of {action, target, condition}

    Returns:
        HTML string.
    """
    pnl = data.get("morning_pnl", 0)
    pnl_pct = data.get("morning_pnl_pct", 0)
    trades = data.get("morning_trades", [])
    holdings = data.get("holdings", [])
    afternoon = data.get("afternoon_plan", [])
    primary_plan = afternoon[0] if afternoon else {}

    color = _pnl_color(pnl)

    # Morning summary
    summary_html = (
        _summary_box("上午盈亏", f'<span class="{color}">¥{_format_amount(pnl)}</span>', _format_pct(pnl_pct)) +
        _summary_box("成交笔数", str(len(trades)))
    )

    # Morning trades
    trade_rows = []
    for t in trades:
        side_label = "买入" if t.get("side") == "buy" else "卖出"
        trade_rows.append([
            t.get("ts_code", ""),
            side_label,
            t.get("quantity", ""),
            _format_amount(t.get("price")),
            _format_amount(t.get("pnl")),
        ])
    trades_html = _table(["代码", "方向", "数量", "价格", "盈亏"], trade_rows) if trade_rows else "<p>上午无成交</p>"

    # Current holdings
    holding_rows = []
    for h in holdings:
        h_pnl = h.get("pnl_pct", 0)
        h_color = _pnl_color(h_pnl)
        holding_rows.append([
            h.get("ts_code", ""),
            h.get("name", ""),
            _format_amount(h.get("current_price")),
            f'<span class="{h_color}">{_format_pct(h_pnl)}</span>',
        ])
    holdings_html = _table(["代码", "名称", "现价", "浮盈亏"], holding_rows) if holding_rows else "<p>暂无持仓</p>"

    # Afternoon plan
    plan_rows = [[p.get("action", ""), p.get("target", ""), p.get("condition", "")] for p in afternoon]
    plan_html = _table(["动作", "标的", "条件"], plan_rows) if plan_rows else "<p>下午按计划观察</p>"

    body = (
        _summary_cards(
            {
                "title": primary_plan.get("action", data.get("action_summary", "下午按计划观察")),
                "detail": primary_plan.get("condition", data.get("action_reason", "午盘后只处理明确触发的计划")),
            },
            {
                "title": data.get("max_loss", f"上午盈亏 {_format_pct(pnl_pct)}"),
                "detail": f"最坏情形 {data.get('worst_case', '--')}；置信度 {data.get('confidence', '--')}",
            },
            {
                "title": data.get("capital_summary", f"持仓 {len(holdings)} 个"),
                "detail": f"上午成交 {len(trades)} 笔；剩余预算 {data.get('remaining_budget', '--')}",
            },
        ) +
        _section("上午表现", summary_html) +
        _section("上午成交", trades_html) +
        _section("当前持仓", holdings_html) +
        _section("下午计划", plan_html)
    )

    priority = _decision_strip(
        data,
        default_action="WAIT",
        default_reason=data.get("decision_reason", "午盘复核后等待下午确认条件"),
        default_deadline=data.get("deadline", "14:25"),
        default_needs="Nicholas: 下午开盘后确认是否执行计划",
    ) + _execution_boundary(data)
    return wrap_html("午盘复盘", "Midday Review", body, priority_content=priority)
