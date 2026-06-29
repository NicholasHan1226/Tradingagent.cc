#!/usr/bin/env python3
"""Email template 8: Emergency alert (anomaly, 10min self-heal).

Type + impact + self-heal action + need human?
"""

from __future__ import annotations

from typing import Any

from . import wrap_html, _section, _table, _summary_box


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
        "pipeline_failure": "管线故障",
        "position_breach": "持仓越界",
        "system_crash": "系统崩溃",
        "connectivity": "连接异常",
    }.get(alert_type, alert_type)

    # Alert summary
    summary_html = (
        _summary_box("告警类型", f'<span class="badge {severity_badge}">{severity_label}</span>', type_label) +
        _summary_box("描述", description[:80] + "..." if len(description) > 80 else description)
    )

    # Impact assessment
    impact_rows = [
        ["影响系统", impact.get("affected_systems", "--")],
        ["潜在损失", impact.get("potential_loss", "--")],
        ["影响策略", impact.get("affected_strategies", "--")],
    ]
    impact_html = _table(["维度", "评估"], impact_rows)

    # Self-heal status
    heal_html = ""
    if self_heal:
        heal_status = self_heal.get("status", "started")
        heal_label = {"started": "自愈已启动", "in_progress": "自愈进行中", "succeeded": "自愈成功", "failed": "自愈失败"}.get(heal_status, heal_status)
        heal_badge = "badge-hold" if heal_status in ("started", "in_progress") else "badge-buy" if heal_status == "succeeded" else "badge-critical"
        heal_rows = [
            ["自愈动作", self_heal.get("action", "--")],
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
        _section("告警概要", summary_html) +
        _section("影响评估", impact_html) +
        (_section("自愈状态", heal_html) if heal_html else "") +
        _section("处理指引", human_html)
    )

    return wrap_html("紧急告警", "Emergency Alert", body)
