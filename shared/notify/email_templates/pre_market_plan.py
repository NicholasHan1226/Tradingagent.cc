#!/usr/bin/env python3
"""Email template 1: Pre-market plan (8:30).

Holdings table + capital plan + market outlook + sector focus + strategy.
"""

from __future__ import annotations

from typing import Any

from . import wrap_html, _section, _table, _summary_box, _pnl_color, _format_pct, _format_amount


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
        _section("持仓概览", holdings_html) +
        _section("资金规划", cap_html) +
        _section("市场展望", outlook_html) +
        _section("板块聚焦", sector_html) +
        _section("今日策略", strat_html)
    )

    return wrap_html("盘前规划", "Pre-Market Plan", body)
