#!/usr/bin/env python3
"""Email template 7: Trade receipt (after fill).

Stock + direction + quantity + price + time + slippage.
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
)


def render(data: dict[str, Any]) -> str:
    """Render trade receipt email HTML.

    Args:
        data: dict with keys:
            - ts_code, name, direction, quantity
            - filled_price, requested_price, slippage_pct
            - fill_time, order_id, commission
            - channel: "sim" | "shadow"

    Returns:
        HTML string.
    """
    ts_code = data.get("ts_code", "")
    name = data.get("name", "")
    direction = data.get("direction", "")
    quantity = data.get("quantity", 0)
    filled_price = data.get("filled_price", 0)
    requested_price = data.get("requested_price")
    slippage = data.get("slippage_pct", 0)
    fill_time = data.get("fill_time", "")
    order_id = data.get("order_id", "")
    commission = data.get("commission", 0)
    channel = data.get("channel", "")
    receipt_status = data.get("receipt_status", data.get("status", "filled"))

    dir_label = {"buy": "买入", "sell": "卖出"}.get(direction, direction)
    badge_class = "badge-buy" if direction == "buy" else "badge-sell"
    channel_label = {"sim": "模拟盘", "shadow": "影子盘"}.get(channel, channel)

    # Receipt summary
    summary_html = (
        _summary_box("标的", f"{ts_code} {name}") +
        _summary_box("方向", f'<span class="badge {badge_class}">{dir_label}</span>') +
        _summary_box("成交价", f"¥{filled_price}", f"数量 {quantity}股")
    )

    # Trade details
    try:
        filled_amount = float(filled_price) * float(quantity)
    except (TypeError, ValueError):
        filled_amount = 0.0
    detail_rows = [
        ["成交价", f"¥{filled_price}"],
        ["委托价", f"¥{requested_price}" if requested_price else "市价"],
        ["滑点", f"{slippage:+.4f}%" if slippage else "0.0000%"],
        ["成交数量", f"{quantity}股"],
        ["成交金额", f"¥{filled_amount:.2f}"],
        ["手续费", f"¥{commission:.2f}"],
        ["成交时间", fill_time],
        ["订单号", order_id],
        ["执行通道", channel_label],
        ["回执状态", receipt_status],
    ]
    detail_html = _table(["项目", "详情"], detail_rows)

    body = (
        _summary_cards(
            {
                "title": f"{dir_label}已回报" if receipt_status in ("filled", "signed", "confirmed") else "回执需复核",
                "detail": f"{ts_code} {name}，成交时间 {fill_time or '--'}",
            },
            {
                "title": data.get("max_loss", f"滑点 {slippage:+.4f}%" if slippage else "滑点 0.0000%"),
                "detail": f"最坏情形 {data.get('worst_case', '回执不完整时不当作成交证明')}；置信度 {data.get('confidence', receipt_status)}",
            },
            {
                "title": f"¥{filled_amount:.2f}",
                "detail": f"数量 {quantity}；手续费 ¥{commission:.2f}；通道 {channel_label}",
            },
        ) +
        _section("成交确认", summary_html) +
        _section("成交明细", detail_html)
    )

    decision = "ACT" if receipt_status in ("failed", "unconfirmed", "invalid") else "IGNORE"
    priority = _decision_strip(
        data,
        default_action=decision,
        default_reason=data.get("decision_reason", "成交回执已收到，异常状态才需要介入"),
        default_deadline=data.get("deadline", data.get("fill_time", "--")),
        default_needs="Nicholas: 只在回执异常或资金层不符时介入",
    ) + _execution_boundary(data)
    return wrap_html("交易回执", "Trade Receipt", body, priority_content=priority)
