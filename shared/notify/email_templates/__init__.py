#!/usr/bin/env python3
"""Shared email template utilities and HTML styling.

All 11 email templates use these helpers to produce consistent HTML emails.
Charts over text, summary language, no system jargon or agent names.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any


# --- Email channel configuration ---

CHANNELS = {
    "trading": {
        "from": "notice@agentspaces.cc",
        "to": "tradingadviser@coze.email",
        "label": "交易类",
    },
    "system": {
        "from": "notice@tradingagent.cc",
        "to": "tradingadviser@coze.email",
        "label": "系统类",
    },
}

# Template -> channel mapping
TEMPLATE_CHANNELS = {
    "pre_market_plan": "trading",
    "trading_signal": "trading",
    "midday_review": "trading",
    "closing_plan": "trading",
    "daily_report": "trading",
    "weekly_report": "trading",
    "trade_receipt": "trading",
    "capital_plan": "trading",
    "strategy_invalidation": "trading",
    "emergency_alert": "system",
    "system_health": "system",
}


def _css() -> str:
    return """
    <style>
      body { font-family: -apple-system, 'PingFang SC', 'Microsoft YaHei', sans-serif; margin: 0; padding: 20px; background: #f5f5f5; }
      .container { max-width: 680px; margin: 0 auto; background: #fff; border-radius: 8px; overflow: hidden; }
      .header { background: #1a1a2e; color: #fff; padding: 20px 24px; }
      .header h1 { margin: 0; font-size: 18px; font-weight: 600; }
      .header .meta { font-size: 12px; color: #aaa; margin-top: 4px; }
      .body { padding: 24px; }
      .section { margin-bottom: 20px; }
      .section-title { font-size: 14px; font-weight: 600; color: #333; border-left: 3px solid #4a6cf7; padding-left: 8px; margin-bottom: 10px; }
      table { width: 100%; border-collapse: collapse; font-size: 13px; }
      th { background: #f8f9fa; text-align: left; padding: 8px 10px; color: #666; font-weight: 500; border-bottom: 1px solid #e0e0e0; }
      td { padding: 8px 10px; border-bottom: 1px solid #f0f0f0; color: #333; }
      .positive { color: #e74c3c; font-weight: 600; }
      .negative { color: #27ae60; font-weight: 600; }
      .neutral { color: #666; }
      .badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 12px; font-weight: 500; }
      .badge-buy { background: #fdf0ef; color: #e74c3c; }
      .badge-sell { background: #eafaf1; color: #27ae60; }
      .badge-hold { background: #f0f0f0; color: #666; }
      .badge-alert { background: #fff3cd; color: #856404; }
      .badge-critical { background: #fde8e8; color: #c0392b; }
      .summary-box { background: #f8f9fa; border-radius: 6px; padding: 14px 16px; margin-bottom: 16px; }
      .summary-box .label { font-size: 12px; color: #999; }
      .summary-box .value { font-size: 20px; font-weight: 700; color: #333; }
      .footer { padding: 16px 24px; background: #f8f9fa; font-size: 11px; color: #999; text-align: center; }
      .chart-placeholder { background: #f8f9fa; border: 1px dashed #ccc; border-radius: 6px; padding: 20px; text-align: center; color: #999; font-size: 12px; margin: 10px 0; }
    </style>
    """


def _header(title: str, subtitle: str = "") -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    return f"""
    <div class="header">
      <h1>{title}</h1>
      <div class="meta">{subtitle} | {now}</div>
    </div>
    """


def _footer() -> str:
    return """
    <div class="footer">
      本邮件由交易系统自动生成，仅供研究参考，不构成投资建议。<br>
      真实资金操作需人工确认。
    </div>
    """


def _section(title: str, content: str) -> str:
    return f"""
    <div class="section">
      <div class="section-title">{title}</div>
      {content}
    </div>
    """


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    header_html = "".join(f"<th>{h}</th>" for h in headers)
    rows_html = ""
    for row in rows:
        cells = "".join(f"<td>{c}</td>" for c in row)
        rows_html += f"<tr>{cells}</tr>"
    return f"""
    <table>
      <thead><tr>{header_html}</tr></thead>
      <tbody>{rows_html}</tbody>
    </table>
    """


def _summary_box(label: str, value: str, extra: str = "") -> str:
    return f"""
    <div class="summary-box">
      <div class="label">{label}</div>
      <div class="value">{value}</div>
      {f'<div style="font-size:12px;color:#999;margin-top:4px;">{extra}</div>' if extra else ''}
    </div>
    """


def _pnl_color(value: float | str) -> str:
    """Return CSS class for P&L coloring (A-share: red=up, green=down)."""
    try:
        v = float(value)
        if v > 0:
            return "positive"
        elif v < 0:
            return "negative"
    except (ValueError, TypeError):
        pass
    return "neutral"


def _format_pct(value: float | None, decimals: int = 2) -> str:
    if value is None:
        return "--"
    return f"{value:+.{decimals}f}%"


def _format_amount(value: float | None) -> str:
    if value is None:
        return "--"
    if abs(value) >= 10000:
        return f"{value/10000:.2f}万"
    return f"{value:.2f}"


def wrap_html(title: str, subtitle: str, body_content: str) -> str:
    """Wrap body content in full HTML email template."""
    return f"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  {_css()}
</head>
<body>
  <div class="container">
    {_header(title, subtitle)}
    <div class="body">
      {body_content}
    </div>
    {_footer()}
  </div>
</body>
</html>
    """


def get_channel(template_name: str) -> dict[str, str]:
    """Get email channel config for a template."""
    channel_key = TEMPLATE_CHANNELS.get(template_name, "trading")
    return CHANNELS[channel_key]
