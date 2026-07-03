#!/usr/bin/env python3
"""Shared email template utilities and HTML styling.

All 11 email templates use these helpers to produce consistent HTML emails.
Charts over text, summary language, no system jargon or agent names.
"""

from __future__ import annotations

from datetime import datetime
from html import escape
from typing import Any


# --- Email channel configuration ---

CHANNELS = {
    "trading": {
        "from": "notice@tradingagent.cc",
        "to": "tradingadviser@coze.email",
        "label": "交易类",
    },
    "system": {
        "from": "notice@tradingagent.cc",
        "to": "soc@coze.email",
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
      body { font-family: -apple-system, BlinkMacSystemFont, 'PingFang SC', 'Microsoft YaHei', sans-serif; margin: 0; padding: 12px; background: #edf0f3; color: #20242a; }
      .container { width: 100%; max-width: 680px; min-width: 0; margin: 0 auto; background: #f7f8fa; border-radius: 8px; overflow: hidden; border: 1px solid #dde2e8; }
      .header { background: #1a1a2e; color: #fff; padding: 18px 18px 16px; }
      .header h1 { margin: 0; font-size: 20px; line-height: 1.25; font-weight: 700; letter-spacing: 0; }
      .header .meta { font-size: 12px; color: #c5cad7; margin-top: 6px; }
      .body { padding: 16px; }
      .section { margin-bottom: 18px; }
      .section-title { font-size: 14px; line-height: 1.35; font-weight: 700; color: #1f2933; border-left: 3px solid #355c7d; padding-left: 9px; margin: 0 0 10px; }
      .table-scroll { width: 100%; overflow-x: auto; border-radius: 8px; background: #fff; border: 1px solid #e2e7ee; }
      table { width: 100%; min-width: 430px; border-collapse: collapse; font-size: 13px; }
      th { background: #f3f5f8; text-align: left; padding: 9px 10px; color: #59636f; font-weight: 600; border-bottom: 1px solid #e0e5ec; white-space: nowrap; }
      td { padding: 9px 10px; border-bottom: 1px solid #eef1f4; color: #2a3038; vertical-align: top; }
      p { margin: 0; color: #59636f; font-size: 13px; line-height: 1.5; }
      .positive { color: #e74c3c; font-weight: 600; }
      .negative { color: #27ae60; font-weight: 600; }
      .neutral { color: #666; }
      .badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 12px; font-weight: 600; white-space: nowrap; }
      .badge-buy { background: #fdf0ef; color: #e74c3c; }
      .badge-sell { background: #eafaf1; color: #27ae60; }
      .badge-hold { background: #f0f0f0; color: #666; }
      .badge-alert { background: #fff3cd; color: #856404; }
      .badge-critical { background: #fde8e8; color: #c0392b; }
      .decision-strip { display: block; padding: 10px 12px; color: #fff; font-size: 12px; line-height: 1.35; }
      .decision-strip.ACT { background: #e74c3c; }
      .decision-strip.WAIT { background: #f39c12; }
      .decision-strip.IGNORE { background: #95a5a6; }
      .decision-strip span { display: inline-block; margin-right: 8px; vertical-align: middle; }
      .decision-strip .action-badge { background: rgba(255,255,255,0.18); border: 1px solid rgba(255,255,255,0.35); border-radius: 4px; padding: 2px 7px; font-weight: 800; letter-spacing: 0; }
      .decision-strip .reason { font-weight: 700; }
      .decision-strip .deadline, .decision-strip .needs-nicholas { color: rgba(255,255,255,0.92); }
      .execution-boundary { background: #fff; border-bottom: 1px solid #e0e5ec; padding: 12px 16px; }
      .boundary-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
      .boundary-item { border: 1px solid #e7ebf0; border-radius: 8px; padding: 8px; background: #fbfcfd; min-width: 0; }
      .boundary-item .label { display: block; font-size: 10px; line-height: 1.2; color: #7a8490; text-transform: uppercase; }
      .boundary-item .value { display: block; margin-top: 3px; font-size: 12px; line-height: 1.35; color: #20242a; font-weight: 700; word-break: break-word; }
      .summary-cards { display: grid; grid-template-columns: 1fr; gap: 10px; margin-bottom: 16px; }
      .decision-card, .summary-box, .chart-card { background: #fff; border: 1px solid #e2e7ee; border-radius: 8px; padding: 13px 14px; box-shadow: 0 12px 24px -20px rgba(31, 41, 55, 0.45); }
      .decision-card .card-kicker, .summary-box .label { font-size: 11px; line-height: 1.2; color: #7a8490; font-weight: 700; text-transform: uppercase; }
      .decision-card h3 { margin: 5px 0 5px; color: #1f2933; font-size: 16px; line-height: 1.25; letter-spacing: 0; }
      .decision-card p { margin: 0; color: #59636f; font-size: 12px; line-height: 1.45; }
      .summary-box { margin-bottom: 10px; }
      .summary-box .label { font-size: 12px; color: #999; }
      .summary-box .value { font-size: 20px; line-height: 1.25; font-weight: 800; color: #1f2933; word-break: break-word; }
      figure { margin: 0 0 12px; }
      figcaption { font-size: 12px; color: #59636f; margin-top: 6px; }
      svg { max-width: 100%; height: auto; display: block; }
      .footer { padding: 16px 18px; background: #eef2f6; font-size: 11px; color: #7a8490; text-align: center; line-height: 1.5; }
      @media (min-width: 560px) {
        .body { padding: 20px; }
        .summary-cards { grid-template-columns: 1.15fr 1fr 1fr; }
        .boundary-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
      }
      @media (max-width: 359px) {
        body { padding: 0; }
        .container { border-radius: 0; border-left: 0; border-right: 0; }
        .header { padding: 15px 12px; }
        .body { padding: 12px; }
        .decision-strip { padding: 8px 10px; }
      }
    </style>
    """


def _header(title: str, subtitle: str = "") -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    return f"""
    <header class="header">
      <h1>{_safe(title)}</h1>
      <div class="meta">{_safe(subtitle)} | {now}</div>
    </header>
    """


def _footer() -> str:
    return """
    <footer class="footer">
      本邮件由交易系统自动生成，仅供研究参考，不构成投资建议。<br>
      真实资金操作需人工确认。
    </footer>
    """


def _section(title: str, content: str) -> str:
    return f"""
    <section class="section">
      <h2 class="section-title">{_safe(title)}</h2>
      {content}
    </section>
    """


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    header_html = "".join(f"<th>{_safe(h)}</th>" for h in headers)
    rows_html = ""
    for row in rows:
        cells = "".join(f"<td>{c}</td>" for c in row)
        rows_html += f"<tr>{cells}</tr>"
    return f"""
    <div class="table-scroll">
      <table>
        <thead><tr>{header_html}</tr></thead>
        <tbody>{rows_html}</tbody>
      </table>
    </div>
    """


def _summary_box(label: str, value: str, extra: str = "") -> str:
    return f"""
    <article class="summary-box">
      <div class="label">{_safe(label)}</div>
      <div class="value">{value}</div>
      {f'<div style="font-size:12px;color:#7a8490;margin-top:4px;">{_safe(extra)}</div>' if extra else ''}
    </article>
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


def _safe(value: Any) -> str:
    return escape("" if value is None else str(value), quote=True)


def _money(value: Any, prefix: str = "¥") -> str:
    try:
        return f"{prefix}{_format_amount(float(value))}"
    except (TypeError, ValueError):
        return _safe(value or "--")


def _plain_list(value: Any) -> str:
    if isinstance(value, (list, tuple, set)):
        return "、".join(_safe(v) for v in value) or "--"
    if isinstance(value, dict):
        return "、".join(f"{_safe(k)}:{_safe(v)}" for k, v in value.items()) or "--"
    return _safe(value or "--")


def _decision_strip(
    data: dict[str, Any],
    default_action: str = "WAIT",
    default_reason: str = "等待更清晰的确认信号",
    default_deadline: str = "--",
    default_needs: str = "Nicholas: 30秒内确认是否需要介入",
) -> str:
    decision = data.get("decision") or {}
    raw_action = decision.get("action", data.get("decision_action", default_action))
    action = str(raw_action or default_action).upper()
    if action not in {"ACT", "WAIT", "IGNORE"}:
        action = default_action if default_action in {"ACT", "WAIT", "IGNORE"} else "WAIT"
    reason = decision.get("reason", data.get("decision_reason", default_reason))
    deadline = decision.get("deadline", data.get("deadline", data.get("expires_at", default_deadline)))
    needs = decision.get("needs_nicholas", data.get("needs_nicholas", default_needs))
    return f"""
    <div class="decision-strip {action}">
      <span class="action-badge">{action}</span>
      <span class="reason">{_safe(reason)}</span>
      <span class="deadline">Valid until {_safe(deadline)}</span>
      <span class="needs-nicholas">{_safe(needs)}</span>
    </div>
    """


def _execution_boundary(data: dict[str, Any]) -> str:
    boundary = data.get("execution_boundary") or {}

    def field(name: str, fallback: str = "--") -> str:
        return _safe(boundary.get(name, data.get(name, fallback)))

    items = [
        ("market", field("market", data.get("market", "Ashare"))),
        ("capital_layer", field("capital_layer", data.get("capital_layer", "shadow"))),
        ("route", field("route", data.get("route", "manual"))),
        ("signal_time", field("signal_time")),
        ("expires_at", field("expires_at")),
        ("data_fresh_at", field("data_fresh_at")),
        ("broker_status", field("broker_status", "unverified")),
        ("receipt_status", field("receipt_status", "not_applicable")),
    ]
    cells = "".join(
        f'<article class="boundary-item"><span class="label">{label}</span><span class="value">{value}</span></article>'
        for label, value in items
    )
    return f"""
    <section class="execution-boundary" aria-label="Execution boundary">
      <div class="boundary-grid">{cells}</div>
    </section>
    """


def _summary_cards(
    action: dict[str, Any],
    risk: dict[str, Any],
    capital: dict[str, Any],
) -> str:
    cards = [
        ("Action", action.get("title", "--"), action.get("detail", "--")),
        ("Risk", risk.get("title", "--"), risk.get("detail", "--")),
        ("Capital", capital.get("title", "--"), capital.get("detail", "--")),
    ]
    return """
    <section class="summary-cards" aria-label="30-second summary">
      {cards}
    </section>
    """.format(cards="".join(
        f"""
        <article class="decision-card">
          <div class="card-kicker">{_safe(kicker)}</div>
          <h3>{_safe(title)}</h3>
          <p>{_safe(detail)}</p>
        </article>
        """
        for kicker, title, detail in cards
    ))


def _sparkline_chart(points: list[Any], title: str = "PnL trend sparkline") -> str:
    values: list[float] = []
    for point in points:
        raw = point.get("pnl", point.get("value", 0)) if isinstance(point, dict) else point
        try:
            values.append(float(raw))
        except (TypeError, ValueError):
            values.append(0.0)
    if not values:
        values = [0.0, 0.0, 0.0]
    width, height, pad = 320, 96, 14
    min_v, max_v = min(values), max(values)
    span = max(max_v - min_v, 1.0)
    step = (width - pad * 2) / max(len(values) - 1, 1)
    coords = []
    for idx, value in enumerate(values):
        x = pad + idx * step
        y = height - pad - ((value - min_v) / span) * (height - pad * 2)
        coords.append((x, y))
    points_attr = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    zero_y = height - pad - ((0 - min_v) / span) * (height - pad * 2)
    zero_y = max(pad, min(height - pad, zero_y))
    return f"""
    <figure class="chart-card">
      <svg viewBox="0 0 {width} {height}" role="img" aria-label="{_safe(title)}">
        <rect x="0" y="0" width="{width}" height="{height}" rx="8" fill="#ffffff"/>
        <line x1="{pad}" y1="{zero_y:.1f}" x2="{width - pad}" y2="{zero_y:.1f}" stroke="#d9dee6" stroke-width="1"/>
        <polyline points="{points_attr}" fill="none" stroke="#355c7d" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>
        {''.join(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.8" fill="#e74c3c"/>' for x, y in coords)}
      </svg>
      <figcaption>{_safe(title)}</figcaption>
    </figure>
    """


def _bar_chart(items: list[Any], title: str = "Strategy contribution") -> str:
    rows = []
    parsed = []
    for item in items:
        if isinstance(item, dict):
            label = item.get("strategy", item.get("factor", item.get("name", "--")))
            raw = item.get("contribution", item.get("pnl", item.get("value", 0)))
        else:
            label, raw = str(item), 0
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = 0.0
        parsed.append((label, value))
    if not parsed:
        parsed = [("暂无贡献", 0.0)]
    max_abs = max(abs(v) for _, v in parsed) or 1.0
    for idx, (label, value) in enumerate(parsed[:6]):
        width = max(2, min(130, abs(value) / max_abs * 130))
        x = 150 if value >= 0 else 150 - width
        color = "#e74c3c" if value >= 0 else "#27ae60"
        y = 22 + idx * 28
        rows.append(
            f'<text x="8" y="{y + 12}" fill="#59636f" font-size="11">{_safe(label)[:18]}</text>'
            f'<rect x="{x:.1f}" y="{y}" width="{width:.1f}" height="14" rx="3" fill="{color}"/>'
            f'<text x="288" y="{y + 12}" text-anchor="end" fill="#20242a" font-size="11">{value:+.2f}</text>'
        )
    height = 22 + len(parsed[:6]) * 28 + 8
    return f"""
    <figure class="chart-card">
      <svg viewBox="0 0 320 {height}" role="img" aria-label="{_safe(title)}">
        <rect x="0" y="0" width="320" height="{height}" rx="8" fill="#ffffff"/>
        <line x1="150" y1="12" x2="150" y2="{height - 8}" stroke="#d9dee6" stroke-width="1"/>
        {''.join(rows)}
      </svg>
      <figcaption>{_safe(title)}</figcaption>
    </figure>
    """


def _heatmap(items: list[Any], title: str = "Position heatmap") -> str:
    parsed = []
    for item in items:
        if isinstance(item, dict):
            label = item.get("ts_code", item.get("name", "--"))
            raw = item.get("pnl_pct", item.get("weight", item.get("value", 0)))
        else:
            label, raw = str(item), 0
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = 0.0
        parsed.append((label, value))
    if not parsed:
        parsed = [("空仓", 0.0)]
    cells = []
    for idx, (label, value) in enumerate(parsed[:12]):
        col, row = idx % 4, idx // 4
        x, y = 8 + col * 76, 8 + row * 48
        intensity = min(abs(value) / 8.0, 1.0)
        fill = f"rgba(231,76,60,{0.18 + intensity * 0.58:.2f})" if value >= 0 else f"rgba(39,174,96,{0.18 + intensity * 0.58:.2f})"
        cells.append(
            f'<rect x="{x}" y="{y}" width="68" height="40" rx="6" fill="{fill}" stroke="#ffffff"/>'
            f'<text x="{x + 6}" y="{y + 16}" fill="#20242a" font-size="10" font-weight="700">{_safe(label)[:10]}</text>'
            f'<text x="{x + 6}" y="{y + 31}" fill="#59636f" font-size="10">{value:+.2f}%</text>'
        )
    height = 8 + ((len(parsed[:12]) + 3) // 4) * 48 + 8
    return f"""
    <figure class="chart-card">
      <svg viewBox="0 0 320 {height}" role="img" aria-label="{_safe(title)}">
        <rect x="0" y="0" width="320" height="{height}" rx="8" fill="#ffffff"/>
        {''.join(cells)}
      </svg>
      <figcaption>{_safe(title)}</figcaption>
    </figure>
    """


def _plain_system_text(value: Any) -> str:
    text = "" if value is None else str(value)
    replacements = {
        "pipeline status=critical": "交易信号管道异常，当前信号不可信",
        "pipeline_failure": "交易信号管道异常",
        "system_crash": "核心服务中断",
        "integrity checks_passed": "数据校验通过，信号质量正常",
        "checks_passed": "数据校验通过",
        "checks_failed": "数据校验未通过",
        "critical": "严重异常",
        "degraded": "部分降级",
        "healthy": "正常",
        "ok": "正常",
        "passed": "通过",
        "failed": "失败",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return _safe(text or "--")


def wrap_html(title: str, subtitle: str, body_content: str, priority_content: str = "") -> str:
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
    {priority_content}
    {_header(title, subtitle)}
    <main class="body">
      {body_content}
    </main>
    {_footer()}
  </div>
</body>
</html>
    """


def get_channel(template_name: str) -> dict[str, str]:
    """Get email channel config for a template."""
    channel_key = TEMPLATE_CHANNELS.get(template_name, "trading")
    return CHANNELS[channel_key]
