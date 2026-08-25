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
    # Least privilege and a hard runtime ceiling (90min absorbs the one-time
    # top-1000 universe backfill; steady-state runs finish in ~15min).
    assert "permissions:\n  contents: read" in workflow
    assert "timeout-minutes: 90" in workflow

    # The scheduled run tracks the expanded universe and its warm-up steps
    # are all idempotent over the persisted cache.
    assert "--expanded" in workflow
    assert "event_calendar_expand_samples.py" in workflow
    assert "refresh_share_float" in workflow
    assert "event_dailybasic_fetch.py" in workflow

    # A timeout must not discard the backfill: actions/cache's post step is
    # success-only, so an explicit cancel-safe save persists whatever reached
    # disk (all sub-steps are resumable) for the next run's restore-keys.
    assert "actions/cache/save@v4" in workflow
    assert "if: ${{ failure() || cancelled() }}" in workflow
