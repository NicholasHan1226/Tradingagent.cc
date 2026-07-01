#!/usr/bin/env python3
"""Alert router: route alerts by severity.

Emergency alerts get a 10-minute self-heal period before human notification.
If the system heals within 10 minutes, no human notification is sent.
If it does not heal, the alert is escalated to human notification.

Severity levels:
  - critical: immediate self-heal attempt, 10min timer, then human
  - high: self-heal attempt, 10min timer, then human
  - medium: self-heal attempt, 30min timer, then human
  - low: log only, self-heal if possible, no human notification

Channels:
  - trading: notice@tradingagent.cc -> tradingadviser@coze.email
  - system: notice@tradingagent.cc -> soc@coze.email
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import email_sender
from .email_templates import CHANNELS
from .email_templates.emergency_alert import render as render_emergency_alert

ALERT_LOG = Path(__file__).resolve().parent / "logs" / "alerts.jsonl"
ALERT_STATE = Path(__file__).resolve().parent / "logs" / "alert_state.json"

# Self-heal time windows by severity (in minutes)
SELF_HEAL_WINDOWS = {
    "critical": 10,
    "high": 10,
    "medium": 30,
    "low": 0,  # no self-heal window, log only
}

# Alert type to channel mapping
ALERT_TYPE_CHANNELS = {
    "data_gap": "system",
    "pipeline_failure": "system",
    "system_crash": "system",
    "connectivity": "system",
    "position_breach": "trading",
    "strategy_invalidation": "trading",
    "execution_failure": "system",
    "data_integrity": "system",
}

# Self-heal actions by alert type
SELF_HEAL_ACTIONS = {
    "data_gap": "切换备用数据源，重新拉取缺失数据",
    "pipeline_failure": "重启失败管线阶段，从检查点恢复",
    "system_crash": "自动重启服务，恢复最近检查点",
    "connectivity": "重试连接，切换备用网络通道",
    "position_breach": "自动触发减仓/清仓指令（影子盘记录）",
    "strategy_invalidation": "暂停失效策略，切换到防御配置",
    "execution_failure": "重试执行，切换到模拟/影子通道",
    "data_integrity": "重新校验数据，修复损坏记录",
}


def _dispatch_escalation_email(alert: Alert) -> dict[str, Any]:
    channel = CHANNELS.get(alert.channel, CHANNELS["system"])
    html_body = render_emergency_alert({
        "alert_type": alert.alert_type,
        "severity": alert.severity,
        "description": alert.description,
        "impact": alert.impact,
        "self_heal": {
            "action": alert.self_heal_action,
            "started_at": alert.created_at,
            "status": "failed",
            "estimated_time": "已超时",
        },
        "need_human": True,
    })
    plain_body = "\n".join([
        f"告警类型: {alert.alert_type}",
        f"严重级别: {alert.severity}",
        f"描述: {alert.description}",
        f"自愈动作: {alert.self_heal_action}",
        f"截止时间: {alert.self_heal_deadline}",
        "状态: 自愈超时，需人工介入。",
    ])
    subject = f"[Tradings告警升级] {alert.alert_type} | {alert.severity}"
    return email_sender.send_email(
        channel["to"],
        subject,
        plain_body,
        html_body,
        channel=alert.channel or "system",
        from_addr=channel["from"],
        rate_limit_type="emergency_alert",
    )


@dataclass
class Alert:
    """An alert instance."""

    alert_id: str = field(default_factory=lambda: f"ALERT-{uuid.uuid4().hex[:12]}")
    alert_type: str = ""
    severity: str = "medium"          # critical | high | medium | low
    description: str = ""
    impact: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    self_heal_started: bool = False
    self_heal_action: str = ""
    self_heal_deadline: str = ""
    self_heal_status: str = ""        # pending | in_progress | succeeded | failed | not_applicable
    notified_human: bool = False
    resolved: bool = False
    resolved_at: str = ""
    channel: str = ""


def _log_alert(alert: Alert) -> None:
    ALERT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(ALERT_LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(asdict(alert), ensure_ascii=False) + "\n")


def _load_active_alerts() -> list[Alert]:
    """Load active (unresolved) alerts from state file."""
    if not ALERT_STATE.exists():
        return []
    try:
        with open(ALERT_STATE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return [Alert(**a) for a in data.get("active", [])]
    except (json.JSONDecodeError, TypeError):
        return []


def _save_active_alerts(alerts: list[Alert]) -> None:
    ALERT_STATE.parent.mkdir(parents=True, exist_ok=True)
    data = {"active": [asdict(a) for a in alerts], "updated_at": datetime.now().isoformat()}
    with open(ALERT_STATE, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)


def route_alert(alert_input: dict[str, Any]) -> dict[str, Any]:
    """Route an alert based on severity.

    Emergency/critical/high alerts get a self-heal period. If the system
    does not heal within the window, human notification is triggered.

    Args:
        alert_input: dict with alert_type, severity, description, impact.

    Returns:
        dict with: alert_id, channel, priority, self_heal_action,
        human_notification, message.
    """
    alert_type = alert_input.get("alert_type", "")
    severity = alert_input.get("severity", "medium").lower()
    description = alert_input.get("description", "")

    # Determine channel
    channel_key = ALERT_TYPE_CHANNELS.get(alert_type, "system")
    channel = CHANNELS[channel_key]

    # Get self-heal action
    self_heal_action = SELF_HEAL_ACTIONS.get(alert_type, "记录告警，等待人工处理")

    # Determine self-heal window
    heal_window = SELF_HEAL_WINDOWS.get(severity, 10)

    alert = Alert(
        alert_type=alert_type,
        severity=severity,
        description=description,
        impact=alert_input.get("impact", {}),
        channel=channel_key,
        self_heal_action=self_heal_action,
    )

    if severity == "escalated" or bool(alert_input.get("escalated")):
        alert.self_heal_started = bool(alert_input.get("self_heal_started", False))
        alert.self_heal_status = "failed"
        alert.notified_human = True
        alert.self_heal_deadline = str(alert_input.get("self_heal_deadline", ""))
        dispatch = _dispatch_escalation_email(alert)
        _log_alert(alert)
        return {
            "alert_id": alert.alert_id,
            "channel": channel_key,
            "priority": severity,
            "self_heal_action": self_heal_action,
            "self_heal_deadline": alert.self_heal_deadline,
            "human_notification": True,
            "message": "Escalated alert notified immediately.",
            "from": channel["from"],
            "to": channel["to"],
            "dispatch": dispatch,
        }

    if severity in ("critical", "high") and heal_window > 0:
        # Start self-heal period
        alert.self_heal_started = True
        alert.self_heal_status = "in_progress"
        deadline = datetime.now() + timedelta(minutes=heal_window)
        alert.self_heal_deadline = deadline.isoformat()
        alert.notified_human = False

        # Save to active alerts for later check
        active = _load_active_alerts()
        active.append(alert)
        _save_active_alerts(active)
        _log_alert(alert)

        return {
            "alert_id": alert.alert_id,
            "channel": channel_key,
            "priority": severity,
            "self_heal_action": self_heal_action,
            "self_heal_deadline": alert.self_heal_deadline,
            "human_notification": False,
            "message": f"Self-heal started ({heal_window}min window). Human notification deferred until deadline if not resolved.",
            "from": channel["from"],
            "to": channel["to"],
        }

    elif severity == "medium":
        # Medium: self-heal attempt with longer window
        alert.self_heal_started = True
        alert.self_heal_status = "in_progress"
        deadline = datetime.now() + timedelta(minutes=heal_window)
        alert.self_heal_deadline = deadline.isoformat()
        alert.notified_human = False

        active = _load_active_alerts()
        active.append(alert)
        _save_active_alerts(active)
        _log_alert(alert)

        return {
            "alert_id": alert.alert_id,
            "channel": channel_key,
            "priority": severity,
            "self_heal_action": self_heal_action,
            "self_heal_deadline": alert.self_heal_deadline,
            "human_notification": False,
            "message": f"Self-heal started ({heal_window}min window). Low priority human notification if not resolved.",
            "from": channel["from"],
            "to": channel["to"],
        }

    else:
        # Low: log only, no human notification
        alert.self_heal_started = True if self_heal_action != "记录告警，等待人工处理" else False
        alert.self_heal_status = "not_applicable"
        alert.notified_human = False
        _log_alert(alert)

        return {
            "alert_id": alert.alert_id,
            "channel": channel_key,
            "priority": severity,
            "self_heal_action": self_heal_action if alert.self_heal_started else "none",
            "self_heal_deadline": "",
            "human_notification": False,
            "message": "Low severity alert logged. No human notification.",
            "from": channel["from"],
            "to": channel["to"],
        }


def check_self_heal_status() -> dict[str, Any]:
    """Check all active alerts for self-heal deadline expiry.

    For alerts whose self-heal window has expired and are not resolved,
    trigger human notification.

    Returns:
        dict with: checked, escalated, resolved, details.
    """
    active = _load_active_alerts()
    now = datetime.now()

    still_active = []
    escalated = []
    resolved = []

    for alert in active:
        if alert.resolved:
            resolved.append(alert.alert_id)
            continue

        if alert.self_heal_deadline:
            try:
                deadline = datetime.fromisoformat(alert.self_heal_deadline)
                if now > deadline and not alert.notified_human:
                    # Self-heal window expired — escalate to human
                    alert.notified_human = True
                    alert.self_heal_status = "failed"
                    dispatch = _dispatch_escalation_email(alert)
                    escalated.append({
                        "alert_id": alert.alert_id,
                        "alert_type": alert.alert_type,
                        "severity": alert.severity,
                        "channel": alert.channel,
                        "message": f"Self-heal failed for {alert.alert_type}. Escalating to human notification.",
                        "dispatch": dispatch,
                    })
                    _log_alert(alert)
                    # Don't keep in active if escalated
                    continue
                elif now <= deadline and alert.self_heal_status == "in_progress":
                    # Still within self-heal window
                    still_active.append(alert)
                else:
                    still_active.append(alert)
            except (ValueError, TypeError):
                still_active.append(alert)
        else:
            still_active.append(alert)

    _save_active_alerts(still_active)

    return {
        "checked": len(active),
        "escalated": len(escalated),
        "resolved": len(resolved),
        "details": escalated,
    }


def resolve_alert(alert_id: str, resolution: str = "") -> dict[str, Any]:
    """Mark an alert as resolved (self-heal succeeded or human fixed).

    Args:
        alert_id: The alert ID to resolve.
        resolution: Resolution description.

    Returns:
        dict with: alert_id, resolved, message.
    """
    active = _load_active_alerts()
    found = False

    for alert in active:
        if alert.alert_id == alert_id:
            alert.resolved = True
            alert.resolved_at = datetime.now().isoformat()
            alert.self_heal_status = "succeeded" if not alert.notified_human else alert.self_heal_status
            found = True
            _log_alert(alert)
            break

    if found:
        # Remove from active
        remaining = [a for a in active if a.alert_id != alert_id]
        _save_active_alerts(remaining)
        return {
            "alert_id": alert_id,
            "resolved": True,
            "message": f"Alert resolved: {resolution}",
        }
    else:
        return {
            "alert_id": alert_id,
            "resolved": False,
            "message": "Alert not found in active alerts",
        }
