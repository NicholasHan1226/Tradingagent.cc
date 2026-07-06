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


def test_render_text_includes_ashare_loop_counts():
    report = {
        "overall_status": "warn",
        "generated_at": datetime(2026, 7, 6, tzinfo=timezone.utc).isoformat(),
        "summary": {"pass": 0, "warn": 1, "fail": 0},
        "checks": [
            {
                "name": "ashare_opening_acceptance",
                "status": "warn",
                "summary": "A股开盘验收需要继续观察",
                "details": {
                    "reason": "first_sample_alerts_present",
                    "latest_bar_time": "2026-07-06 09:40:00",
                    "sample_summary": {
                        "bar_count": 20,
                        "signals": {"filled": 0, "failed": 1},
                        "local_sim_trades": 0,
                        "sim_execution_receipts": 0,
                        "daily_reviews": 1,
                    },
                    "no_trade_category": "all_rejected_by_risk",
                },
            }
        ],
        "next_actions": ["A股继续观察"],
    }

    text = opening_acceptance.render_text(report)

    assert "bar=20" in text
    assert "信号=1" in text
    assert "成交=0" in text
    assert "回执=0" in text
    assert "复盘=1" in text
    assert "无交易分类=all_rejected_by_risk" in text


def test_sharedsignals_degraded_with_core_ok_is_pass(monkeypatch):
    payload = {
        "status": "degraded",
        "checks": {
            "functions": {"status": "ok"},
            "cron": {"status": "ok"},
            "data_freshness": {"status": "degraded"},
        },
    }
    monkeypatch.setattr(opening_acceptance, "_http_json", lambda _url: (200, payload))

    result = opening_acceptance.check_sharedsignals("http://127.0.0.1:8082")

    assert result.status == "pass"
    assert result.details["payload_status"] == "degraded"
