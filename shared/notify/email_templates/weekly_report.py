#!/usr/bin/env python3
"""Email template 6: Weekly report (Friday).

Strategy stats + trends + next week.
"""

from __future__ import annotations

from typing import Any

from . import wrap_html, _section, _table, _summary_box, _pnl_color, _format_pct, _format_amount


def render(data: dict[str, Any]) -> str:
    """Render weekly report email HTML.

    Args:
        data: dict with keys:
            - week_range: "2026-06-23 ~ 2026-06-27"
            - weekly_pnl, weekly_pnl_pct, benchmark_pnl_pct
            - strategy_stats: list of {name, trades, win_rate, pnl, pnl_pct}
            - daily_pnl: list of {date, pnl, pnl_pct}
            - trends: {market, sectors, capital_flow}
            - next_week: list of {focus, action}

    Returns:
        HTML string.
    """
    week_range = data.get("week_range", "")
    weekly_pnl = data.get("weekly_pnl", 0)
    weekly_pnl_pct = data.get("weekly_pnl_pct", 0)
    benchmark_pct = data.get("benchmark_pnl_pct", 0)
    strategies = data.get("strategy_stats", [])
    daily = data.get("daily_pnl", [])
    trends = data.get("trends", {})
    next_week = data.get("next_week", [])

    color = _pnl_color(weekly_pnl)
    excess = weekly_pnl_pct - benchmark_pct if benchmark_pct else 0

    # Weekly summary
    summary_html = (
        _summary_box("周盈亏", f'<span class="{color}">¥{_format_amount(weekly_pnl)}</span>', _format_pct(weekly_pnl_pct)) +
        _summary_box("基准周涨跌", _format_pct(benchmark_pct), "沪深300") +
        _summary_box("超额收益", f'<span class="{_pnl_color(excess)}">{_format_pct(excess)}</span>')
    )

    # Strategy stats
    strat_rows = []
    for s in strategies:
        s_pnl = s.get("pnl", 0)
        s_color = _pnl_color(s_pnl)
        strat_rows.append([
            s.get("name", ""),
            s.get("trades", 0),
            f"{s.get('win_rate', 0)*100:.0f}%" if isinstance(s.get("win_rate"), float) else s.get("win_rate", "--"),
            f'<span class="{s_color}">{_format_amount(s_pnl)}</span>',
            f'<span class="{s_color}">{_format_pct(s.get("pnl_pct"))}</span>',
        ])
    strat_html = _table(["策略", "交易数", "胜率", "盈亏", "收益率"], strat_rows) if strat_rows else "<p>本周无策略交易</p>"

    # Daily P&L
    daily_rows = []
    for d in daily:
        d_pnl = d.get("pnl", 0)
        d_color = _pnl_color(d_pnl)
        daily_rows.append([d.get("date", ""), f'<span class="{d_color}">{_format_amount(d_pnl)}</span>', f'<span class="{d_color}">{_format_pct(d.get("pnl_pct"))}</span>'])
    daily_html = _table(["日期", "盈亏", "收益率"], daily_rows) if daily_rows else "<p>本周无交易记录</p>"

    # Trends
    trend_html = ""
    if trends:
        trend_rows = [
            ["市场趋势", trends.get("market", "--")],
            ["板块轮动", trends.get("sectors", "--")],
            ["资金流向", trends.get("capital_flow", "--")],
        ]
        trend_html = _table(["维度", "趋势"], trend_rows)

    # Next week plan
    next_rows = [[n.get("focus", ""), n.get("action", "")] for n in next_week]
    next_html = _table(["关注点", "动作"], next_rows) if next_rows else "<p>下周按计划执行</p>"

    body = (
        _section("本周总结", summary_html) +
        _section("策略统计", strat_html) +
        _section("每日盈亏", daily_html) +
        (_section("趋势观察", trend_html) if trend_html else "") +
        _section("下周计划", next_html)
    )

    return wrap_html(f"周报 | {week_range}", "Weekly Report", body)
