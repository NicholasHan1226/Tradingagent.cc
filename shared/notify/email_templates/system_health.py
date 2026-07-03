#!/usr/bin/env python3
"""Email template 11: System health (on anomaly).

Collection + pipeline + integrity.
"""

from __future__ import annotations

from typing import Any

from . import (
    wrap_html,
    _decision_strip,
    _plain_list,
    _plain_system_text,
    _section,
    _summary_cards,
    _table,
    _summary_box,
)


def render(data: dict[str, Any]) -> str:
    """Render system health email HTML.

    Args:
        data: dict with keys:
            - overall_status: "healthy" | "degraded" | "critical"
            - collection: {status, sources, last_update, gaps}
            - pipeline: {status, stages, failed_stages, last_run}
            - integrity: {status, checks_passed, checks_failed, details}

    Returns:
        HTML string.
    """
    overall = data.get("overall_status", "healthy")
    collection = data.get("collection", {})
    pipeline = data.get("pipeline", {})
    integrity = data.get("integrity", {})

    status_label = {"healthy": "正常", "degraded": "降级", "critical": "严重"}.get(overall, overall)
    status_badge = "badge-hold" if overall == "healthy" else "badge-alert" if overall == "degraded" else "badge-critical"

    # Overall status
    summary_html = _summary_box("系统状态", f'<span class="badge {status_badge}">{status_label}</span>')

    # Collection health
    coll_status = collection.get("status", "--")
    coll_badge = "badge-hold" if coll_status in ("ok", "healthy") else "badge-alert" if coll_status in ("degraded", "partial") else "badge-critical"
    coll_rows = [
        ["状态", f'<span class="badge {coll_badge}">{_plain_system_text(coll_status)}</span>'],
        ["数据源", _plain_list(collection.get("sources", "--"))],
        ["最后更新", collection.get("last_update", "--")],
        ["数据缺口", _plain_system_text(collection.get("gaps", "无"))],
    ]
    coll_html = _table(["项目", "状态"], coll_rows)

    # Pipeline health
    pipe_status = pipeline.get("status", "--")
    pipe_badge = "badge-hold" if pipe_status in ("ok", "healthy") else "badge-alert" if pipe_status in ("degraded", "partial") else "badge-critical"
    pipe_rows = [
        ["状态", f'<span class="badge {pipe_badge}">{_plain_system_text(pipe_status)}</span>'],
        ["交易信号流程", _plain_list(pipeline.get("stages", "--"))],
        ["异常环节", _plain_system_text(pipeline.get("failed_stages", "无"))],
        ["最后运行", pipeline.get("last_run", "--")],
    ]
    pipe_html = _table(["项目", "状态"], pipe_rows)

    # Integrity
    integ_status = integrity.get("status", "--")
    integ_badge = "badge-hold" if integ_status in ("ok", "passed") else "badge-critical"
    integ_rows = [
        ["状态", f'<span class="badge {integ_badge}">{_plain_system_text(integ_status)}</span>'],
        ["数据校验通过", _plain_system_text(integrity.get("checks_passed", "--"))],
        ["数据校验未通过", _plain_system_text(integrity.get("checks_failed", "--"))],
        ["详情", _plain_system_text(integrity.get("details", "--"))],
    ]
    integ_html = _table(["项目", "状态"], integ_rows)

    body = (
        _summary_cards(
            {
                "title": "可以参考信号" if overall == "healthy" else "先别按信号交易",
                "detail": "数据校验通过，信号质量正常" if overall == "healthy" else "交易信号管道异常，当前信号不可信",
            },
            {
                "title": data.get("max_loss", "错误信号风险"),
                "detail": f"最坏情形 {_plain_system_text(data.get('worst_case', pipeline.get('failed_stages', '--')))}；状态 {status_label}",
            },
            {
                "title": data.get("capital_summary", "不新增风险"),
                "detail": f"最后更新 {collection.get('last_update', '--')}；最后运行 {pipeline.get('last_run', '--')}",
            },
        ) +
        _section("系统总览", summary_html) +
        _section("数据采集", coll_html) +
        _section("管线运行", pipe_html) +
        _section("数据完整性", integ_html)
    )

    priority = _decision_strip(
        data,
        default_action="IGNORE" if overall == "healthy" else "ACT" if overall == "critical" else "WAIT",
        default_reason=data.get("decision_reason", "数据校验通过，信号质量正常" if overall == "healthy" else "交易信号管道异常，当前信号不可信"),
        default_deadline=data.get("deadline", "下一次健康检查"),
        default_needs="Nicholas: 仅在严重异常或持续降级时介入",
    )
    return wrap_html("系统健康", "System Health", body, priority_content=priority)
