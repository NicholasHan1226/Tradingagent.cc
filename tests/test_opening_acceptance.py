from __future__ import annotations

from datetime import datetime, timezone

from shared.runtime_test import opening_acceptance


def test_overall_status_warn_when_any_warning():
    checks = [
        opening_acceptance.AcceptanceCheck("a", "pass", "ok"),
        opening_acceptance.AcceptanceCheck("b", "warn", "watch"),
    ]

    assert opening_acceptance._overall(checks) == "warn"


def test_overall_status_fail_wins():
    checks = [
        opening_acceptance.AcceptanceCheck("a", "warn", "watch"),
        opening_acceptance.AcceptanceCheck("b", "fail", "bad"),
    ]

    assert opening_acceptance._overall(checks) == "fail"


def test_render_text_contains_plain_status():
    report = {
        "overall_status": "pass",
        "generated_at": datetime(2026, 7, 6, tzinfo=timezone.utc).isoformat(),
        "summary": {"pass": 1, "warn": 0, "fail": 0},
        "checks": [{"name": "sharedsignals_api", "status": "pass", "summary": "ok", "details": {}}],
        "next_actions": ["继续观察"],
    }

    text = opening_acceptance.render_text(report)

    assert "开盘验收：通过" in text
    assert "sharedsignals_api" in text
