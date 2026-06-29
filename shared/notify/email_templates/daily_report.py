#!/usr/bin/env python3
"""Email template 5: Daily report (15:30).

Trade summary + P&L + attribution + tomorrow plan.
"""

from __future__ import annotations

from typing import Any

from . import wrap_html, _section, _table, _summary_box, _pnl_color, _format_pct, _format_amount


def render(data: dict[str, Any]) -> str:
    """Render daily report email HTML.

    Args:
        data: dict with keys:
            - date
            - total_pnl, total_pnl_pct, benchmark_pnl_pct
            - trades: list of {ts_code, side, quantity, price, pnl}
            - attribution: list of {factor, contribution}
            - holdings: list of {ts_code, name, pnl_pct}
            - tomorrow_plan: list of {action, target, reason}

    Returns:
        HTML string.
    """
    date_str = data.get("date", "")
    total_pnl = data.get("total_pnl", 0)
    total_pnl_pct = data.get("total_pnl_pct", 0)
    benchmark_pct = data.get("benchmark_pnl_pct", 0)
    trades = data.get("trades", [])
    attribution = data.get("attribution", [])
    holdings = data.get("holdings", [])
    tomorrow = data.get("tomorrow_plan", [])

    color = _pnl_color(total_pnl)
    excess = total_pnl_pct - benchmark_pct if benchmark_pct else 0

    # Summary
    summary_html = (
        _summary_box("今日盈亏", f'<span class="{color}">¥{_format_amount(total_pnl)}</span>', _format_pct(total_pnl_pct)) +
        _summary_box("基准", _format_pct(benchmark_pct), "沪深300") +
        _summary_box("超额", f'<span class="{_pnl_color(excess)}">{_format_pct(excess)}</span>')
    )

    # Trade summary
    trade_rows = []
    for t in trades:
        side_label = "买入" if t.get("side") == "buy" else "卖出"
        t_pnl = t.get("pnl", 0)
        t_color = _pnl_color(t_pnl)
        trade_rows.append([
            t.get("ts_code", ""),
            side_label,
            t.get("quantity", ""),
            _format_amount(t.get("price")),
            f'<span class="{t_color}">{_format_amount(t_pnl)}</span>',
        ])
    trades_html = _table(["代码", "方向", "数量", "价格", "盈亏"], trade_rows) if trade_rows else "<p>今日无成交</p>"

    # Attribution
    attr_rows = [[a.get("factor", ""), _format_pct(a.get("contribution"))] for a in attribution]
    attr_html = _table(["归因因子", "贡献"], attr_rows) if attr_rows else "<p>暂无归因分析</p>"

    # Holdings
    holding_rows = []
    for h in holdings:
        h_pnl = h.get("pnl_pct", 0)
        h_color = _pnl_color(h_pnl)
        holding_rows.append([h.get("ts_code", ""), h.get("name", ""), f'<span class="{h_color}">{_format_pct(h_pnl)}</span>'])
    holdings_html = _table(["代码", "名称", "浮盈亏"], holding_rows) if holding_rows else "<p>暂无持仓</p>"

    # Tomorrow plan
    plan_rows = [[p.get("action", ""), p.get("target", ""), p.get("reason", "")] for p in tomorrow]
    plan_html = _table(["动作", "标的", "原因"], plan_rows) if plan_rows else "<p>明日按计划观察</p>"

    body = (
        _section("今日总结", summary_html) +
        _section("成交明细", trades_html) +
        _section("盈亏归因", attr_html) +
        _section("持仓状态", holdings_html) +
        _section("明日计划", plan_html)
    )

    return wrap_html(f"日报 | {date_str}", "Daily Report", body)
