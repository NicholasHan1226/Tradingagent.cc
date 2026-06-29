#!/usr/bin/env python3
"""Email template 11: System health (on anomaly).

Collection + pipeline + integrity.
"""

from __future__ import annotations

from typing import Any

from . import wrap_html, _section, _table, _summary_box


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
        ["状态", f'<span class="badge {coll_badge}">{coll_status}</span>'],
        ["数据源", str(collection.get("sources", "--"))],
        ["最后更新", collection.get("last_update", "--")],
        ["数据缺口", collection.get("gaps", "无")],
    ]
    coll_html = _table(["项目", "状态"], coll_rows)

    # Pipeline health
    pipe_status = pipeline.get("status", "--")
    pipe_badge = "badge-hold" if pipe_status in ("ok", "healthy") else "badge-alert" if pipe_status in ("degraded", "partial") else "badge-critical"
    pipe_rows = [
        ["状态", f'<span class="badge {pipe_badge}">{pipe_status}</span>'],
        ["管线阶段", str(pipeline.get("stages", "--"))],
        ["失败阶段", pipeline.get("failed_stages", "无")],
        ["最后运行", pipeline.get("last_run", "--")],
    ]
    pipe_html = _table(["项目", "状态"], pipe_rows)

    # Integrity
    integ_status = integrity.get("status", "--")
    integ_badge = "badge-hold" if integ_status in ("ok", "passed") else "badge-critical"
    integ_rows = [
        ["状态", f'<span class="badge {integ_badge}">{integ_status}</span>'],
        ["通过检查", str(integrity.get("checks_passed", "--"))],
        ["失败检查", str(integrity.get("checks_failed", "--"))],
        ["详情", integrity.get("details", "--")],
    ]
    integ_html = _table(["项目", "状态"], integ_rows)

    body = (
        _section("系统总览", summary_html) +
        _section("数据采集", coll_html) +
        _section("管线运行", pipe_html) +
        _section("数据完整性", integ_html)
    )

    return wrap_html("系统健康", "System Health", body)
