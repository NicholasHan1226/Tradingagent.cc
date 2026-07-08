from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

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


def test_render_text_skips_empty_closed_window_sample_counts():
    report = {
        "overall_status": "pass",
        "generated_at": datetime(2026, 7, 8, tzinfo=timezone.utc).isoformat(),
        "summary": {"pass": 1, "warn": 0, "fail": 0},
        "checks": [
            {
                "name": "cn_futures_opening_acceptance",
                "status": "pass",
                "summary": "中国期货开盘验收通过",
                "details": {
                    "report_type": "opening_acceptance_window",
                    "reason": "outside_cn_futures_opening_acceptance_window",
                    "session": "closed",
                    "sample_summary": {
                        "bar_count": None,
                        "signals": {},
                        "local_sim_trades": None,
                        "sim_execution_receipts": None,
                        "daily_reviews": None,
                    },
                },
            }
        ],
        "next_actions": ["当前可接受"],
    }

    text = opening_acceptance.render_text(report)

    assert "outside_cn_futures_opening_acceptance_window" in text
    assert "bar=0" not in text
    assert "成交=0" not in text


def test_render_text_skips_pre_open_symbol_only_sample_counts():
    report = {
        "overall_status": "pass",
        "generated_at": datetime(2026, 7, 8, tzinfo=timezone.utc).isoformat(),
        "summary": {"pass": 1, "warn": 0, "fail": 0},
        "checks": [
            {
                "name": "ashare_opening_acceptance",
                "status": "pass",
                "summary": "A股开盘验收通过",
                "details": {
                    "report_type": "pre_open_acceptance",
                    "reason": "pre_open_acceptance_passed",
                    "session": "afternoon",
                    "sample_summary": {
                        "bar_count": None,
                        "symbol_count": 5200,
                        "signals": {},
                        "local_sim_trades": None,
                        "sim_execution_receipts": None,
                        "daily_reviews": None,
                    },
                },
            }
        ],
        "next_actions": ["当前可接受"],
    }

    text = opening_acceptance.render_text(report)

    assert "pre_open_acceptance_passed" in text
    assert "bar=0" not in text
    assert "成交=0" not in text


def test_sharedsignals_core_ok_health_degraded_is_warn(monkeypatch):
    def fake_http_json(url, timeout=8.0):
        if url.endswith("/cache/status"):
            return 200, {"functions_registered": 14}
        if url.endswith("/capabilities"):
            return 200, {"data": {"endpoints": [{"name": "get_market_data"}]}}
        if url.endswith("/health"):
            raise TimeoutError("health timed out")
        raise AssertionError(url)

    monkeypatch.setattr(opening_acceptance, "_http_json", fake_http_json)

    result = opening_acceptance.check_sharedsignals("http://127.0.0.1:8082")

    assert result.status == "warn"
    assert result.details["functions_registered"] == 14
    assert result.details["capability_endpoint_count"] == 1
    assert "TimeoutError" in result.details["health_error"]


def test_write_outputs_records_latest_and_history(tmp_path, monkeypatch):
    latest = tmp_path / "latest.json"
    history = tmp_path / "history.jsonl"
    monkeypatch.setattr(opening_acceptance, "LATEST", latest)
    monkeypatch.setattr(opening_acceptance, "HISTORY", history)
    report = {
        "overall_status": "warn",
        "generated_at": "2026-07-07T08:56:00+08:00",
        "summary": {"pass": 0, "warn": 1, "fail": 0},
    }

    opening_acceptance._write_outputs(report)

    assert json.loads(latest.read_text(encoding="utf-8"))["overall_status"] == "warn"
    assert len(history.read_text(encoding="utf-8").splitlines()) == 1


def test_send_alert_uses_system_channel(monkeypatch):
    sent = {}

    def fake_send_email(to, subject, body, html_body, *, channel, rate_limit_type):
        sent.update(
            {
                "to": to,
                "subject": subject,
                "body": body,
                "channel": channel,
                "rate_limit_type": rate_limit_type,
            }
        )
        return {"status": "sent", "to": to}

    monkeypatch.setattr(opening_acceptance.email_sender, "send_email", fake_send_email, raising=False)

    result = opening_acceptance._send_alert(
        {"overall_status": "fail", "generated_at": "2026-07-07T08:56:00+08:00"},
        "开盘验收：失败",
    )

    assert result["status"] == "sent"
    assert sent["to"] == opening_acceptance.email_sender.CHANNELS["system"]["to"]
    assert sent["channel"] == "system"
    assert sent["rate_limit_type"] == "opening_acceptance:fail"


def test_ashare_lunch_routes_to_afternoon_pre_open(monkeypatch):
    from shared.runtime_test import ashare_opening_validator

    called = {}

    def fake_pre_open(*, sqlite_db, now, min_symbols):
        called.update({"sqlite_db": sqlite_db, "now": now, "min_symbols": min_symbols})
        return {
            "market": "ashare",
            "report_type": "pre_open_acceptance",
            "status": "pass",
            "reason": "pre_open_acceptance_passed",
            "session": "afternoon",
            "real_trading_enabled": False,
        }

    monkeypatch.setattr(ashare_opening_validator, "validate_pre_open", fake_pre_open)

    report = opening_acceptance._ashare_opening_report(
        datetime.fromisoformat("2026-07-08T12:15:00+08:00"),
        Path("/tmp/nonexistent-marketdata.sqlite"),
    )

    assert report["status"] == "pass"
    assert report["report_type"] == "pre_open_acceptance"
    assert report["session"] == "afternoon"
    assert called["min_symbols"] == 1000


def test_ashare_closed_window_is_observation_not_warning():
    check = opening_acceptance.check_ashare_opening(
        datetime.fromisoformat("2026-07-08T16:05:00+08:00"),
        Path("/tmp/nonexistent-marketdata.sqlite"),
    )

    assert check.status == "pass"
    assert check.details["reason"] == "outside_ashare_opening_acceptance_window"
    assert check.details["raw_status"] == "pass"


def test_cn_futures_lunch_gap_is_observation_not_missing_trade():
    check = opening_acceptance.check_cn_futures_opening(
        datetime.fromisoformat("2026-07-08T11:52:00+08:00"),
        Path("/tmp/nonexistent-marketdata.sqlite"),
    )

    assert check.status == "pass"
    assert check.details["reason"] == "outside_cn_futures_opening_acceptance_window"
    assert check.details["raw_status"] == "pass"
    assert check.details["alerts"] == []


def test_cn_futures_midday_pre_open_routes_to_pre_open(monkeypatch):
    from CNFutures import opening_validator

    called = {}

    def fake_pre_open(*, sqlite_db, now, min_symbols):
        called.update({"sqlite_db": sqlite_db, "now": now, "min_symbols": min_symbols})
        return {
            "market": "cn_futures",
            "report_type": "pre_open_acceptance",
            "status": "pass",
            "reason": "pre_open_acceptance_passed",
            "session": "afternoon",
            "real_trading_enabled": False,
        }

    monkeypatch.setattr(opening_validator, "validate_pre_open", fake_pre_open)

    report = opening_acceptance._cn_futures_opening_report(
        datetime.fromisoformat("2026-07-08T12:30:00+08:00"),
        Path("/tmp/nonexistent-marketdata.sqlite"),
    )

    assert report["status"] == "pass"
    assert report["report_type"] == "pre_open_acceptance"
    assert report["session"] == "afternoon"
    assert called["min_symbols"] == 4
