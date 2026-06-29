#!/usr/bin/env python3
"""Email template 3: Midday review (11:35).

Morning performance + afternoon plan.
"""

from __future__ import annotations

from typing import Any

from . import wrap_html, _section, _table, _summary_box, _pnl_color, _format_pct, _format_amount


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
        _section("上午表现", summary_html) +
        _section("上午成交", trades_html) +
        _section("当前持仓", holdings_html) +
        _section("下午计划", plan_html)
    )

    return wrap_html("午盘复盘", "Midday Review", body)
