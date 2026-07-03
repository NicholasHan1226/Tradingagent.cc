#!/usr/bin/env python3
"""Email template 8: Emergency alert (anomaly, 10min self-heal).

Type + impact + self-heal action + need human?
"""

from __future__ import annotations

from typing import Any

from . import (
    wrap_html,
    _decision_strip,
    _plain_system_text,
    _section,
    _summary_cards,
    _table,
    _summary_box,
)


def render(data: dict[str, Any]) -> str:
    """Render emergency alert email HTML.

    Args:
        data: dict with keys:
            - alert_type: "data_gap" | "pipeline_failure" | "position_breach" | "system_crash" | "connectivity"
            - severity: "critical" | "high" | "medium"
            - description
            - impact: {affected_systems, potential_loss, affected_strategies}
            - self_heal: {action, started_at, status, estimated_time}
            - need_human: bool

    Returns:
        HTML string.
    """
    alert_type = data.get("alert_type", "")
    severity = data.get("severity", "high")
    description = data.get("description", "")
    impact = data.get("impact", {})
    self_heal = data.get("self_heal", {})
    need_human = data.get("need_human", False)

    severity_label = {"critical": "严重", "high": "高", "medium": "中"}.get(severity, severity)
    severity_badge = "badge-critical" if severity == "critical" else "badge-alert"

    type_label = {
        "data_gap": "数据缺失",
        "pipeline_failure": "交易信号管道异常，当前信号不可信",
        "position_breach": "持仓越界",
        "system_crash": "核心服务中断",
        "connectivity": "连接异常",
    }.get(alert_type, alert_type)
    plain_description = _plain_system_text(description)

    # Alert summary
    summary_html = (
        _summary_box("告警类型", f'<span class="badge {severity_badge}">{severity_label}</span>', type_label) +
        _summary_box("描述", plain_description[:80] + "..." if len(plain_description) > 80 else plain_description)
    )

    # Impact assessment
    impact_rows = [
        ["影响范围", _plain_system_text(impact.get("affected_systems", "--"))],
        ["可能损失", _plain_system_text(impact.get("potential_loss", "--"))],
        ["受影响策略", _plain_system_text(impact.get("affected_strategies", "--"))],
    ]
    impact_html = _table(["维度", "评估"], impact_rows)

    # Self-heal status
    heal_html = ""
    if self_heal:
        heal_status = self_heal.get("status", "started")
        heal_label = {"started": "自愈已启动", "in_progress": "自愈进行中", "succeeded": "自愈成功", "failed": "自愈失败"}.get(heal_status, heal_status)
        heal_badge = "badge-hold" if heal_status in ("started", "in_progress") else "badge-buy" if heal_status == "succeeded" else "badge-critical"
        heal_rows = [
            ["自动处理", _plain_system_text(self_heal.get("action", "--"))],
            ["启动时间", self_heal.get("started_at", "--")],
            ["状态", f'<span class="badge {heal_badge}">{heal_label}</span>'],
            ["预计耗时", self_heal.get("estimated_time", "10分钟内")],
        ]
        heal_html = _table(["项目", "状态"], heal_rows)

    # Human action needed?
    human_html = ""
    if need_human:
        human_html = """
        <div style="background: #fde8e8; border-radius: 6px; padding: 14px 16px; margin-top: 10px;">
          <strong style="color: #c0392b;">需要人工介入</strong><br>
          <span style="font-size: 13px; color: #333;">自愈期内未恢复或问题超出自动处理范围，请尽快确认并处理。</span>
        </div>
        """
    else:
        human_html = """
        <div style="background: #eafaf1; border-radius: 6px; padding: 14px 16px; margin-top: 10px;">
          <strong style="color: #27ae60;">暂不需要人工介入</strong><br>
          <span style="font-size: 13px; color: #333;">系统正在自动处理中，10分钟自愈期内如恢复将不再通知。</span>
        </div>
        """

    body = (
        _summary_cards(
            {
                "title": "立即检查" if need_human or severity == "critical" else "等待自愈",
                "detail": f"{type_label}；{plain_description}",
            },
            {
                "title": data.get("max_loss", impact.get("potential_loss", "--")),
                "detail": f"最坏情形 {data.get('worst_case', impact.get('affected_strategies', '--'))}；级别 {severity_label}",
            },
            {
                "title": data.get("capital_summary", "暂停新增风险"),
                "detail": f"影响范围 {impact.get('affected_systems', '--')}；当前不把异常信号当交易依据",
            },
        ) +
        _section("告警概要", summary_html) +
        _section("影响评估", impact_html) +
        (_section("自愈状态", heal_html) if heal_html else "") +
        _section("处理指引", human_html)
    )

    priority = _decision_strip(
        data,
        default_action="ACT" if need_human or severity == "critical" else "WAIT",
        default_reason=data.get("decision_reason", type_label),
        default_deadline=data.get("deadline", "10分钟自愈窗口"),
        default_needs="Nicholas: 自愈失败或严重异常时人工介入",
    )
    return wrap_html("紧急告警", "Emergency Alert", body, priority_content=priority)
