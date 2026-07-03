#!/usr/bin/env python3
"""Email template 5: Daily report (15:30).

Trade summary + P&L + attribution + tomorrow plan.
"""

from __future__ import annotations

from typing import Any

from . import (
    wrap_html,
    _bar_chart,
    _decision_strip,
    _execution_boundary,
    _heatmap,
    _section,
    _sparkline_chart,
    _summary_cards,
    _table,
    _summary_box,
    _pnl_color,
    _format_pct,
    _format_amount,
)


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
    primary_plan = tomorrow[0] if tomorrow else {}

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


    ops = data.get("ops_queue_summary") or {}
    ops_receipts = data.get("ops_receipt_integrity") or {}
    ops_shadow = data.get("ops_shadow_queue_summary") or {}
    ops_failures = data.get("ops_failure_summary") or {}
    ops_html = ""
    if data.get("ops_status") and data.get("ops_status") != "missing":
        ops_rows = [
            ["状态", data.get("ops_status", "--")],
            ["执行队列", f"pending={ops.get('pending', 0)}, running={ops.get('running', 0)}, failed={ops.get('failed', 0)}, expired={ops.get('expired', 0)}"],
            ["影子队列", f"pending={ops_shadow.get('pending', 0)}, running={ops_shadow.get('running', 0)}, failed={ops_shadow.get('failed', 0)}, expired={ops_shadow.get('expired', 0)}"],
            ["回执", f"total={ops_receipts.get('total', 0)}, unsigned={ops_receipts.get('unsigned', 0)}, invalid={ops_receipts.get('invalid', 0)}"],
            ["失败分类", ", ".join(f"{k}:{v}" for k, v in ops_failures.items()) or "无"],
        ]
        ops_html = _table(["项目", "结果"], ops_rows)

    body = (
        _summary_cards(
            {
                "title": primary_plan.get("action", data.get("action_summary", "收盘后复盘，不立即交易")),
                "detail": primary_plan.get("reason", data.get("action_reason", "明日计划只在新交易日确认后执行")),
            },
            {
                "title": data.get("max_loss", f"超额 {_format_pct(excess)}"),
                "detail": f"最坏情形 {data.get('worst_case', '--')}；置信度 {data.get('confidence', '--')}",
            },
            {
                "title": data.get("capital_summary", f"今日盈亏 ¥{_format_amount(total_pnl)}"),
                "detail": f"持仓 {len(holdings)} 个；成交 {len(trades)} 笔；剩余预算 {data.get('remaining_budget', '--')}",
            },
        ) +
        _section(
            "图表快读",
            _sparkline_chart(data.get("pnl_trend") or data.get("daily_pnl") or [total_pnl], "PnL trend sparkline") +
            _bar_chart(data.get("strategy_contribution") or attribution, "Strategy contribution") +
            _heatmap(data.get("position_heatmap") or holdings, "Position heatmap"),
        ) +
        _section("今日总结", summary_html) +
        _section("成交明细", trades_html) +
        _section("盈亏归因", attr_html) +
        _section("持仓状态", holdings_html) +
        _section("明日计划", plan_html) +
        (_section("运行状态", ops_html) if ops_html else "")
    )

    priority = _decision_strip(
        data,
        default_action=data.get("decision_action", "WAIT"),
        default_reason=data.get("decision_reason", "收盘复盘完成，等待下一交易窗口"),
        default_deadline=data.get("deadline", "次日 09:25"),
        default_needs="Nicholas: 复核明日计划和隔夜风险",
    ) + _execution_boundary(data)
    return wrap_html(f"日报 | {date_str}", "Daily Report", body, priority_content=priority)
