from __future__ import annotations

from pathlib import Path


def test_event_signal_tracker_requires_explicit_dispatch() -> None:
    workflow = Path(".github/workflows/event-signal-tracker.yml").read_text(
        encoding="utf-8"
    )

    assert "workflow_dispatch:" in workflow
    assert "\n  schedule:" not in workflow
    assert "\n    - cron:" not in workflow
