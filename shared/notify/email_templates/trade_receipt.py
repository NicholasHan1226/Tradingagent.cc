#!/usr/bin/env python3
"""Email template 7: Trade receipt (after fill).

Stock + direction + quantity + price + time + slippage.
"""

from __future__ import annotations

from typing import Any

from . import wrap_html, _section, _table, _summary_box


def render(data: dict[str, Any]) -> str:
    """Render trade receipt email HTML.

    Args:
        data: dict with keys:
            - ts_code, name, direction, quantity
            - filled_price, requested_price, slippage_pct
            - fill_time, order_id, commission
            - channel: "sim" | "shadow" | "hermes"

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

    dir_label = {"buy": "买入", "sell": "卖出"}.get(direction, direction)
    badge_class = "badge-buy" if direction == "buy" else "badge-sell"
    channel_label = {"sim": "模拟盘", "shadow": "影子盘", "hermes": "实盘"}.get(channel, channel)

    # Receipt summary
    summary_html = (
        _summary_box("标的", f"{ts_code} {name}") +
        _summary_box("方向", f'<span class="badge {badge_class}">{dir_label}</span>') +
        _summary_box("成交价", f"¥{filled_price}", f"数量 {quantity}股")
    )

    # Trade details
    detail_rows = [
        ["成交价", f"¥{filled_price}"],
        ["委托价", f"¥{requested_price}" if requested_price else "市价"],
        ["滑点", f"{slippage:+.4f}%" if slippage else "0.0000%"],
        ["成交数量", f"{quantity}股"],
        ["成交金额", f"¥{filled_price * quantity:.2f}"],
        ["手续费", f"¥{commission:.2f}"],
        ["成交时间", fill_time],
        ["订单号", order_id],
        ["执行通道", channel_label],
    ]
    detail_html = _table(["项目", "详情"], detail_rows)

    body = (
        _section("成交确认", summary_html) +
        _section("成交明细", detail_html)
    )

    return wrap_html("交易回执", "Trade Receipt", body)
