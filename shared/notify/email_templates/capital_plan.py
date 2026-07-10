#!/usr/bin/env python3
"""Email template 9: Capital plan (in pre-market)."""

from __future__ import annotations

from typing import Any

from shared.markets.sim_capital import default_sim_capital

from . import (
    wrap_html,
    _decision_strip,
    _execution_boundary,
    _section,
    _summary_cards,
    _table,
    _summary_box,
    _format_amount,
)


def render(data: dict[str, Any]) -> str:
    """Render capital plan email HTML.

    Args:
        data: dict with keys:
            - total_capital: total available capital
            - allocation: list of {strategy, amount, pct}
            - reverse_repo: {amount, rate, term, expected_return}
            - reserved: reserved amount
            - available: available for new positions

    Returns:
        HTML string.
    """
    total = data.get("total_capital", default_sim_capital("ashare"))
    allocation = data.get("allocation", [])
    repo = data.get("reverse_repo", {})
    reserved = data.get("reserved", 0)
    available = data.get("available", 0)
    primary_allocation = allocation[0] if allocation else {}

    # Summary
    summary_html = (
        _summary_box("总资金", f"¥{_format_amount(total)}") +
        _summary_box("可用", f"¥{_format_amount(available)}") +
        _summary_box("逆回购", f"¥{_format_amount(repo.get('amount', 0))}", repo.get("term", ""))
    )

    # Allocation breakdown
    alloc_rows = []
    total_allocated = 0
    for a in allocation:
        amount = a.get("amount", 0)
        total_allocated += amount
        alloc_rows.append([
            a.get("strategy", ""),
            f"¥{_format_amount(amount)}",
            f"{a.get('pct', 0)*100:.1f}%" if isinstance(a.get("pct"), float) else a.get("pct", "--"),
        ])
    # Add reserved and available
    alloc_rows.append(["预留资金", f"¥{_format_amount(reserved)}", f"{reserved/total*100:.1f}%" if total else "--"])
    alloc_rows.append(["可用资金", f"¥{_format_amount(available)}", f"{available/total*100:.1f}%" if total else "--"])
    alloc_html = _table(["用途", "金额", "占比"], alloc_rows)

    # Reverse repo details
    repo_html = ""
    if repo:
        repo_rows = [
            ["金额", f"¥{_format_amount(repo.get('amount', 0))}"],
            ["利率", f"{repo.get('rate', 0):.3f}%"],
            ["期限", repo.get("term", "1天")],
            ["预期收益", f"¥{repo.get('expected_return', 0):.2f}"],
        ]
        repo_html = _table(["项目", "详情"], repo_rows)

    body = (
        _summary_cards(
            {
                "title": primary_allocation.get("strategy", data.get("action_summary", "按资金上限分配")),
                "detail": f"优先金额 ¥{_format_amount(primary_allocation.get('amount', 0))}；执行窗口 {data.get('deadline', '--')}",
            },
            {
                "title": data.get("max_loss", f"预留 ¥{_format_amount(reserved)}"),
                "detail": f"最坏情形 {data.get('worst_case', '--')}；置信度 {data.get('confidence', '--')}",
            },
            {
                "title": f"可用 ¥{_format_amount(available)}",
                "detail": f"总资金 ¥{_format_amount(total)}；逆回购 ¥{_format_amount(repo.get('amount', 0))}",
            },
        ) +
        _section("资金概览", summary_html) +
        _section("分配明细", alloc_html) +
        (_section("逆回购", repo_html) if repo_html else "")
    )

    priority = _decision_strip(
        data,
        default_action=data.get("decision_action", "WAIT"),
        default_reason=data.get("decision_reason", "资金计划待市场窗口确认"),
        default_deadline=data.get("deadline", "09:25"),
        default_needs="Nicholas: 确认可用资金和单笔预算上限",
    ) + _execution_boundary(data)
    return wrap_html("资金规划", "Capital Plan", body, priority_content=priority)
