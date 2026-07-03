#!/usr/bin/env python3
"""Email template 1: Pre-market plan (8:30).

Holdings table + capital plan + market outlook + sector focus + strategy.
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
    """Render pre-market plan email HTML.

    Args:
        data: dict with keys:
            - holdings: list of {ts_code, name, quantity, cost, last_close, pnl_pct}
            - capital: {available, allocated, reserve, reverse_repo}
            - market_outlook: {regime, trend, key_levels}
            - sector_focus: list of {sector, direction, reason}
            - strategy: list of {name, action, target}

    Returns:
        HTML string.
    """
    holdings = data.get("holdings", [])
    capital = data.get("capital", {})
    outlook = data.get("market_outlook", {})
    sectors = data.get("sector_focus", [])
    strategies = data.get("strategy", [])
    primary_strategy = strategies[0] if strategies else {}

    # Holdings table
    holding_rows = []
    total_pnl = 0.0
    for h in holdings:
        pnl_pct = h.get("pnl_pct", 0)
        total_pnl += pnl_pct
        color = _pnl_color(pnl_pct)
        holding_rows.append([
            h.get("ts_code", ""),
            h.get("name", ""),
            h.get("quantity", ""),
            _format_amount(h.get("cost")),
            _format_amount(h.get("last_close")),
            f'<span class="{color}">{_format_pct(pnl_pct)}</span>',
        ])
    holdings_html = _table(["代码", "名称", "持仓", "成本", "昨收", "浮盈亏"], holding_rows) if holding_rows else "<p>暂无持仓</p>"

    # Capital summary
    cap_html = ""
    if capital:
        cap_html = (
            _summary_box("可用资金", f"¥{_format_amount(capital.get('available', 0))}") +
            _summary_box("已用资金", f"¥{_format_amount(capital.get('allocated', 0))}") +
            _summary_box("逆回购", f"¥{_format_amount(capital.get('reverse_repo', 0))}", "国债逆回购")
        )

    # Market outlook
    outlook_html = ""
    if outlook:
        outlook_html = _table(
            ["维度", "判断"],
            [
                ["经济季节", outlook.get("regime", "--")],
                ["趋势", outlook.get("trend", "--")],
                ["关键位", outlook.get("key_levels", "--")],
            ]
        )

    # Sector focus
    sector_rows = [[s.get("sector", ""), s.get("direction", ""), s.get("reason", "")] for s in sectors]
    sector_html = _table(["板块", "方向", "逻辑"], sector_rows) if sector_rows else "<p>暂无重点关注</p>"

    # Strategy
    strat_rows = [[s.get("name", ""), s.get("action", ""), s.get("target", "")] for s in strategies]
    strat_html = _table(["策略", "今日动作", "目标"], strat_rows) if strat_rows else "<p>按既定策略执行</p>"

    body = (
        _summary_cards(
            {
                "title": primary_strategy.get("action", data.get("action_summary", "按盘前计划执行")),
                "detail": primary_strategy.get("target", data.get("action_reason", "先看持仓、资金和板块信号，再决定是否出手")),
            },
            {
                "title": data.get("risk_summary", outlook.get("regime", "--")),
                "detail": data.get("worst_case", outlook.get("key_levels", "关键风险位未提供")),
            },
            {
                "title": f"可用 ¥{_format_amount(capital.get('available', data.get('available_capital', 0)))}",
                "detail": f"已用 ¥{_format_amount(capital.get('allocated', 0))}，逆回购 ¥{_format_amount(capital.get('reverse_repo', 0))}",
            },
        ) +
        _section("持仓概览", holdings_html) +
        _section("资金规划", cap_html) +
        _section("市场展望", outlook_html) +
        _section("板块聚焦", sector_html) +
        _section("今日策略", strat_html)
    )

    priority = _decision_strip(
        data,
        default_action="WAIT",
        default_reason=data.get("decision_reason", "开盘前先确认市场方向和风险预算"),
        default_deadline=data.get("deadline", "09:25"),
        default_needs="Nicholas: 开盘前确认是否调整今日计划",
    ) + _execution_boundary(data)
    return wrap_html("盘前规划", "Pre-Market Plan", body, priority_content=priority)
