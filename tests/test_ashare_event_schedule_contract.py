from __future__ import annotations

from pathlib import Path


def test_event_signal_tracker_schedule_is_bounded_weekly() -> None:
    """Safety contract for scheduled provider activation in the tracker.

    Nicholas authorized a weekly Monday schedule on 2026-08-24 (replacing the
    previous dispatch-only posture).  The gate stays deliberately narrow: the
    tracker is a research-only, report-only layer; exactly one cron line,
    Monday-only at an off-minute after the evening announcement window; manual
    dispatch remains available; runs serialize without cancellation and fail
    closed with TOKEN_MISSING when the TUSHARE_MCP_TOKEN secret is absent.
    """

    workflow = Path(".github/workflows/event-signal-tracker.yml").read_text(
        encoding="utf-8"
    )

    # Ad-hoc research runs stay possible.
    assert "workflow_dispatch:" in workflow
    # Exactly one bounded weekly trigger: Monday 13:23 UTC (21:23 Beijing).
    assert workflow.count("\n  schedule:") == 1
    assert workflow.count("- cron:") == 1
    assert '\n    - cron: "23 13 * * 1"' in workflow
    # Scheduled runs serialize; no overlapping provider sessions.
    assert "cancel-in-progress: false" in workflow
    # Least privilege and a hard runtime ceiling.
    assert "permissions:\n  contents: read" in workflow
    assert "timeout-minutes: 45" in workflow
