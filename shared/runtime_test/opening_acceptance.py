#!/usr/bin/env python3
"""Read-only opening acceptance summary for A-share and CN futures."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.notify import email_sender
try:
    from shared.data.reader import DEFAULT_SHARED_SIGNALS_DB
except Exception:  # pragma: no cover
    DEFAULT_SHARED_SIGNALS_DB = Path("/nonexistent/tradingagent-sharedsignals-diagnostic.sqlite")

CN_TZ = timezone(timedelta(hours=8))
DEFAULT_SHAREDSIGNALS_API_URL = "http://127.0.0.1:8082"
DEFAULT_HEALTH_INPUT_ROOT = ROOT / "shared" / "logs" / "health"
DEFAULT_SQLITE_DB = DEFAULT_SHARED_SIGNALS_DB
LATEST = ROOT / "shared/runtime_test/opening_acceptance_latest.json"
HISTORY = ROOT / "shared/runtime_test/opening_acceptance_history.jsonl"


@dataclass
class AcceptanceCheck:
    name: str
    status: str
    summary: str
    details: dict[str, Any] = field(default_factory=dict)


def _now_cn(value: str | None = None) -> datetime:
    if not value:
        return datetime.now(CN_TZ)
    parsed = datetime.fromisoformat(value.replace(" ", "T", 1))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=CN_TZ)
    return parsed.astimezone(CN_TZ)


def _status_rank(status: str) -> int:
    return {"pass": 0, "ok": 0, "warn": 1, "degraded": 1, "fail": 2, "critical": 2}.get(status, 1)


def _overall(checks: list[AcceptanceCheck]) -> str:
    worst = max((_status_rank(check.status) for check in checks), default=1)
    return "fail" if worst >= 2 else ("warn" if worst == 1 else "pass")


def _safe_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _file_age_minutes(path: Path) -> float | None:
    if not path.exists():
        return None
    return round(max(0.0, (time.time() - path.stat().st_mtime) / 60.0), 2)


def _http_json(url: str, timeout: float = 8.0) -> tuple[int, dict[str, Any]]:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read(65536).decode("utf-8", errors="replace")
        status_code = int(getattr(resp, "status", 200))
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = {"raw": raw[:200]}
    return status_code, payload if isinstance(payload, dict) else {"data": payload}


def _count_statuses(items: list[dict[str, Any]], key: str = "status") -> dict[str, int]:
    counts = {"pass": 0, "warn": 0, "fail": 0, "ok": 0, "critical": 0, "degraded": 0}
    for item in items:
        status = str(item.get(key) or "").lower()
        if status in counts:
            counts[status] += 1
    return {key_: value for key_, value in counts.items() if value}


def check_sharedsignals(api_url: str) -> AcceptanceCheck:
    base = api_url.rstrip("/")
    cache_url = f"{base}/cache/status"
    capability_url = f"{base}/capabilities"
    health_url = f"{base}/health"
    try:
        cache_status_code, cache_payload = _http_json(cache_url, timeout=3.0)
    except (OSError, urllib.error.URLError, TimeoutError) as exc:
        return AcceptanceCheck(
            "sharedsignals_api",
            "fail",
            "SharedSignals API 不可用",
            {"url": cache_url, "error": f"{exc.__class__.__name__}: {exc}"},
        )
    capability_status_code = 0
    capability_payload: dict[str, Any] = {}
    capability_error = ""
    try:
        capability_status_code, capability_payload = _http_json(capability_url, timeout=5.0)
    except (OSError, urllib.error.URLError, TimeoutError) as exc:
        capability_error = f"{exc.__class__.__name__}: {exc}"
    health_status_code = 0
    health_payload: dict[str, Any] = {}
    health_error = ""
    try:
        health_status_code, health_payload = _http_json(health_url, timeout=2.0)
    except (OSError, urllib.error.URLError, TimeoutError) as exc:
        health_error = f"{exc.__class__.__name__}: {exc}"
    cache_ok = 200 <= cache_status_code < 300 and int(cache_payload.get("functions_registered") or 0) > 0
    capability_data = capability_payload.get("data") if isinstance(capability_payload.get("data"), dict) else {}
    capability_ok = 200 <= capability_status_code < 300 and isinstance(capability_data.get("endpoints"), list) and len(capability_data.get("endpoints") or []) > 0
    core_ok = cache_ok and capability_ok
    health_payload_status = str(health_payload.get("status") or "").lower()
    health_ok = 200 <= health_status_code < 300 and health_payload_status in {"ok", "healthy", "degraded"}
    status = "pass" if core_ok and health_ok else ("warn" if core_ok else "fail")
    return AcceptanceCheck(
        "sharedsignals_api",
        status,
        "SharedSignals 核心 API 可用" if status == "pass" else ("SharedSignals 核心 API 可用但 /health 降级" if core_ok else "SharedSignals API 返回异常"),
        {
            "cache_url": cache_url,
            "cache_status_code": cache_status_code,
            "functions_registered": cache_payload.get("functions_registered"),
            "capability_url": capability_url,
            "capability_status_code": capability_status_code,
            "capability_endpoint_count": len(capability_data.get("endpoints") or []) if isinstance(capability_data, dict) else 0,
            "capability_error": capability_error,
            "health_url": health_url,
            "health_status_code": health_status_code,
            "health_payload_status": health_payload_status,
            "health_error": health_error,
        },
    )


def check_watchdog_inputs(health_input_root: Path = DEFAULT_HEALTH_INPUT_ROOT, max_age_minutes: int = 20) -> AcceptanceCheck:
    input_dir = health_input_root
    expected = {
        "tradingagent_health": input_dir / "tradingagent_health.json",
    }
    reports: dict[str, Any] = {}
    statuses: list[str] = []
    missing: list[str] = []
    stale: list[str] = []
    for name, path in expected.items():
        payload = _safe_json(path)
        age = _file_age_minutes(path)
        if not payload:
            missing.append(name)
            statuses.append("warn")
        elif age is not None and age > max_age_minutes:
            stale.append(name)
            statuses.append("warn")
        else:
            statuses.append(str(payload.get("status") or "warn").lower())
        reports[name] = {"path": str(path), "age_minutes": age, "status": payload.get("status") if payload else ""}
    worst = max((_status_rank(status) for status in statuses), default=1)
    status = "fail" if worst >= 2 else ("warn" if worst == 1 else "pass")
    return AcceptanceCheck(
        "watchdog_inputs",
        status,
        "watchdog 外部健康报告正常" if status == "pass" else "watchdog 外部健康报告需要复核",
        {"reports": reports, "missing": missing, "stale": stale, "max_age_minutes": max_age_minutes},
    )


def check_halt_files() -> AcceptanceCheck:
    candidates = {
        "tradingagent_executor_halt": ROOT / "signals" / "executor_halt.json",
    }
    existing = {name: str(path) for name, path in candidates.items() if path.exists()}
    return AcceptanceCheck(
        "halt_files",
        "fail" if existing else "pass",
        "没有 halt 文件" if not existing else "存在 halt 文件，必须先处理",
        {"existing": existing},
    )


def check_sim_health() -> AcceptanceCheck:
    from shared.runtime_test.market_health import run_sim_market_health

    report = run_sim_market_health()
    raw_status = str(report.get("overall_status") or "warn").lower()
    status = "fail" if raw_status == "fail" else ("warn" if raw_status == "warn" else "pass")
    return AcceptanceCheck(
        "sim_market_health",
        status,
        "模拟盘总巡检正常" if status == "pass" else "模拟盘总巡检需要复核",
        {
            "overall_status": raw_status,
            "summary": report.get("summary", {}),
            "markets": [
                {
                    "name": item.get("name"),
                    "status": item.get("status"),
                    "summary": item.get("summary"),
                }
                for item in report.get("checks", [])
                if isinstance(item, dict)
            ],
        },
    )


def _api_health_review(market: str) -> dict[str, Any]:
    from shared.runtime_test.market_health import run_sim_market_health

    report = run_sim_market_health(markets=(market,))
    checks = [item for item in report.get("checks", []) if isinstance(item, dict)]
    return {
        "overall_status": str(report.get("overall_status") or "warn").lower(),
        "summary": report.get("summary", {}),
        "checks": [
            {
                "name": item.get("name"),
                "status": item.get("status"),
                "summary": item.get("summary"),
            }
            for item in checks
        ],
    }


def _ashare_preopen_runtime_evidence(now: datetime) -> dict[str, Any]:
    try:
        from shared.runtime_test.ashare_preopen_dry_run import run_preopen_dry_run

        report = run_preopen_dry_run(now=now)
    except Exception as exc:
        return {
            "status": "warn",
            "reason": "ashare_preopen_dry_run_unavailable",
            "error": f"{exc.__class__.__name__}: {exc}",
        }
    data = report.get("data") if isinstance(report.get("data"), dict) else {}
    pool = report.get("candidate_pool") if isinstance(report.get("candidate_pool"), dict) else {}
    capital = report.get("capital_plan") if isinstance(report.get("capital_plan"), dict) else {}
    gate = report.get("execution_gate") if isinstance(report.get("execution_gate"), dict) else {}
    synthetic_order = gate.get("synthetic_order") if isinstance(gate.get("synthetic_order"), dict) else {}
    return {
        "status": str(report.get("status") or "warn").lower(),
        "trade_date": report.get("trade_date"),
        "data_status": data.get("status"),
        "symbol_count": data.get("symbol_count"),
        "latest_trade_date": data.get("latest_trade_date"),
        "candidate_status": pool.get("status"),
        "candidate_count": pool.get("candidate_count"),
        "scored_count": pool.get("scored_count"),
        "capital_status": capital.get("status"),
        "risk_mode": capital.get("risk_mode"),
        "target_positions": capital.get("target_positions"),
        "max_new_positions": capital.get("max_new_positions"),
        "cash_reserve": capital.get("cash_reserve"),
        "execution_status": gate.get("status"),
        "execution_ready": bool(gate.get("ready")),
        "execution_reason": gate.get("reason"),
        "synthetic_symbol": synthetic_order.get("ts_code"),
        "synthetic_quantity": synthetic_order.get("quantity"),
        "synthetic_budget": synthetic_order.get("budget"),
        "warnings": report.get("warnings", []),
        "blockers": report.get("blockers", []),
    }


def _cn_futures_runtime_evidence() -> dict[str, Any]:
    try:
        from shared.runtime_test.cn_futures_live_check import run_live_check

        report = run_live_check()
    except Exception as exc:
        return {
            "status": "warn",
            "reason": "cn_futures_live_check_unavailable",
            "error": f"{exc.__class__.__name__}: {exc}",
        }
    checks = [item for item in report.get("checks", []) if isinstance(item, dict)]
    data = next((item for item in checks if item.get("name") == "sharedsignals_5min_freshness"), {})
    review = next((item for item in checks if item.get("name") == "cn_futures_review"), {})
    data_details = data.get("details") if isinstance(data.get("details"), dict) else {}
    review_details = review.get("details") if isinstance(review.get("details"), dict) else {}
    return {
        "status": str(report.get("overall_status") or "warn").lower(),
        "observation_phase": report.get("observation_phase"),
        "summary": report.get("summary", {}),
        "data_status": data.get("status"),
        "row_count": data_details.get("row_count"),
        "review_status": review.get("status"),
        "latest_state": review_details.get("latest_state"),
        "latest_filled_count": review_details.get("latest_filled_count"),
        "latest_hold_count": review_details.get("latest_hold_count"),
        "latest_top_hold_reason": review_details.get("latest_top_hold_reason"),
        "alerts": report.get("alerts", []),
    }


def _accept_with_api_health_if_ready(
    *,
    market: str,
    status: str,
    reason: str,
    details: dict[str, Any],
    api_only_reasons: set[str],
) -> tuple[str, dict[str, Any]]:
    if reason not in api_only_reasons:
        return status, details
    health = _api_health_review(market)
    updated = {**details, "api_health_review": health, "original_opening_status": status}
    if health.get("overall_status") == "pass":
        updated["reason"] = f"api_health_pass_after_{reason}"
        updated["raw_status"] = "pass"
        return "pass", updated
    return status, updated


def _ashare_opening_report(now: datetime, sqlite_db: Path) -> dict[str, Any]:
    from shared.runtime_test import ashare_opening_validator as validator

    minutes = now.hour * 60 + now.minute
    if (8 * 60 <= minutes < 9 * 60 + 30) or (11 * 60 + 30 < minutes < 13 * 60):
        return validator.validate_pre_open(sqlite_db=sqlite_db, now=now, min_symbols=1000)
    if (9 * 60 + 30 <= minutes <= 11 * 60 + 30) or (13 * 60 <= minutes <= 15 * 60):
        session_start = now.replace(hour=9, minute=30, second=0, microsecond=0)
        if minutes >= 13 * 60:
            session_start = now.replace(hour=13, minute=0, second=0, microsecond=0)
        elapsed = int((now - session_start).total_seconds() // 60)
        if elapsed >= 10:
            return validator.first_sample_alerts(sqlite_db=sqlite_db, now=now, min_symbols=10, wait_minutes=10)
        return validator.validate_opening(sqlite_db=sqlite_db, now=now, min_symbols=10)
    return _closed_window_report("ashare", now, "outside_ashare_opening_acceptance_window")


def check_ashare_opening(now: datetime, sqlite_db: Path) -> AcceptanceCheck:
    report = _ashare_opening_report(now, sqlite_db)
    raw_status = str(report.get("status") or "warn").lower()
    status = "fail" if raw_status == "fail" else ("warn" if raw_status == "warn" else "pass")
    reason = str(report.get("reason") or "")
    samples = report.get("samples", {}) if isinstance(report.get("samples"), dict) else {}
    no_trade = report.get("no_trade_explanation", {}) if isinstance(report.get("no_trade_explanation"), dict) else {}
    alerts = report.get("alerts", []) if isinstance(report.get("alerts"), list) else []
    alert_codes = {str(alert.get("code") or "") for alert in alerts if isinstance(alert, dict)}
    details = {
        "report_type": report.get("report_type"),
        "reason": reason,
        "session": report.get("session"),
        "bar_count": report.get("bar_count"),
        "symbol_count": report.get("symbol_count"),
        "latest_bar_time": report.get("latest_bar_time"),
        "latest_trade_date": report.get("latest_trade_date"),
        "latest_daily_age_days": report.get("latest_daily_age_days"),
        "max_daily_age_days": report.get("max_daily_age_days"),
        "sample_summary": {
            "bar_count": report.get("bar_count") or samples.get("bar_count"),
            "symbol_count": report.get("symbol_count") or samples.get("symbol_count"),
            "signals": samples.get("signals", {}),
            "local_sim_trades": samples.get("local_sim_trades"),
            "sim_execution_receipts": samples.get("sim_execution_receipts"),
            "daily_reviews": samples.get("daily_reviews"),
        },
        "no_trade_explanation": no_trade,
        "no_trade_category": no_trade.get("category"),
        "no_trade_next_action": no_trade.get("next_action"),
        "alerts": alerts,
        "raw_status": raw_status,
    }
    api_reason = reason
    if (
        status == "warn"
        and "ashare_sqlite_diagnostic_disabled" in alert_codes
        and alert_codes.issubset({"ashare_sqlite_diagnostic_disabled"})
    ):
        details["original_reason"] = reason
        api_reason = "sqlite_diagnostic_disabled"
    if api_reason == "sqlite_diagnostic_disabled" and report.get("report_type") == "pre_open_acceptance":
        evidence = _ashare_preopen_runtime_evidence(now)
        details["runtime_evidence"] = evidence
        details["original_opening_status"] = status
        if evidence.get("status") == "pass":
            details["reason"] = "ashare_preopen_dry_run_pass_after_sqlite_diagnostic_disabled"
            details["raw_status"] = "pass"
            details["sample_summary"]["symbol_count"] = evidence.get("symbol_count")
            return AcceptanceCheck(
                "ashare_opening_acceptance",
                "pass",
                "A股开盘验收通过",
                details,
            )
    status, details = _accept_with_api_health_if_ready(
        market="ashare",
        status=status,
        reason=api_reason,
        details=details,
        api_only_reasons={"sqlite_diagnostic_disabled"},
    )
    return AcceptanceCheck(
        "ashare_opening_acceptance",
        status,
        "A股开盘验收通过" if status == "pass" else "A股开盘验收需要继续观察",
        details,
    )


def _cn_futures_opening_report(now: datetime, sqlite_db: Path) -> dict[str, Any]:
    from CNFutures import opening_validator as validator
    from CNFutures.session import cn_futures_session_state

    minutes = now.hour * 60 + now.minute
    if (8 * 60 <= minutes < 9 * 60) or (12 * 60 <= minutes < 13 * 60) or (20 * 60 <= minutes < 21 * 60):
        return validator.validate_pre_open(sqlite_db=sqlite_db, now=now, min_symbols=4)
    session_state = cn_futures_session_state(now)
    if bool(session_state.get("in_session")):
        start_raw = str(session_state.get("session_start") or "")
        start = datetime.fromisoformat(start_raw) if start_raw else now
        elapsed = int((now - start).total_seconds() // 60)
        if elapsed >= 10:
            return validator.first_sample_alerts(sqlite_db=sqlite_db, now=now, min_symbols=4, wait_minutes=10)
        return validator.validate_opening(sqlite_db=sqlite_db, now=now, min_symbols=4)
    return _closed_window_report("cn_futures", now, f"outside_cn_futures_opening_acceptance_window:{session_state.get('session')}")


def _closed_window_report(market: str, now: datetime, reason: str) -> dict[str, Any]:
    return {
        "market": market,
        "report_type": "opening_acceptance_window",
        "checked_at": now.isoformat(timespec="seconds"),
        "read_only": True,
        "session": "closed",
        "status": "pass",
        "reason": reason,
        "real_trading_enabled": False,
    }


def check_cn_futures_opening(now: datetime, sqlite_db: Path) -> AcceptanceCheck:
    report = _cn_futures_opening_report(now, sqlite_db)
    raw_status = str(report.get("status") or "warn").lower()
    status = "fail" if raw_status == "fail" else ("warn" if raw_status == "warn" else "pass")
    reason = str(report.get("reason") or "")
    details = {
        "report_type": report.get("report_type"),
        "reason": reason,
        "session": report.get("session"),
        "bar_count": report.get("bar_count"),
        "symbol_count": report.get("symbol_count"),
        "latest_bar_time": report.get("latest_bar_time"),
        "opening_30m_review": report.get("opening_30m_review", {}),
        "alerts": report.get("alerts", []),
        "raw_status": raw_status,
    }
    if reason in {"pre_open_daily_query_failed", "sqlite_diagnostic_disabled"}:
        evidence = _cn_futures_runtime_evidence()
        details["runtime_evidence"] = evidence
        details["original_opening_status"] = status
        if evidence.get("status") == "pass":
            details["reason"] = f"cn_futures_live_check_pass_after_{reason}"
            details["raw_status"] = "pass"
            return AcceptanceCheck(
                "cn_futures_opening_acceptance",
                "pass",
                "中国期货开盘验收通过",
                details,
            )
    status, details = _accept_with_api_health_if_ready(
        market="cn_futures",
        status=status,
        reason=reason,
        details=details,
        api_only_reasons={"pre_open_daily_query_failed", "sqlite_diagnostic_disabled"},
    )
    return AcceptanceCheck(
        "cn_futures_opening_acceptance",
        status,
        "中国期货开盘验收通过" if status == "pass" else "中国期货开盘验收需要继续观察",
        details,
    )


def run_acceptance(
    *,
    now: datetime | None = None,
    health_input_root: Path = DEFAULT_HEALTH_INPUT_ROOT,
    sharedsignals_api_url: str = DEFAULT_SHAREDSIGNALS_API_URL,
    sqlite_db: Path = DEFAULT_SQLITE_DB,
) -> dict[str, Any]:
    current = now or _now_cn()
    checks = [
        check_sharedsignals(sharedsignals_api_url),
        check_watchdog_inputs(health_input_root),
        check_halt_files(),
        check_sim_health(),
        check_ashare_opening(current, sqlite_db),
        check_cn_futures_opening(current, sqlite_db),
    ]
    overall = _overall(checks)
    next_actions = _next_actions(checks)
    return {
        "report_type": "opening_acceptance_summary",
        "generated_at": current.isoformat(timespec="seconds"),
        "timezone": "Asia/Shanghai",
        "overall_status": overall,
        "summary": {
            "pass": sum(1 for check in checks if check.status == "pass"),
            "warn": sum(1 for check in checks if check.status == "warn"),
            "fail": sum(1 for check in checks if check.status == "fail"),
        },
        "checks": [check.__dict__ for check in checks],
        "next_actions": next_actions,
        "real_trading_enabled": False,
        "read_only": True,
    }


def _write_outputs(report: dict[str, Any], *, append_history: bool = True) -> None:
    LATEST.parent.mkdir(parents=True, exist_ok=True)
    LATEST.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if append_history:
        with HISTORY.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(report, ensure_ascii=False) + "\n")


def _send_alert(report: dict[str, Any], rendered_text: str) -> dict[str, Any]:
    status = str(report.get("overall_status") or "warn")
    subject = f"[TradingAgent][开盘验收] {status} {report.get('generated_at', '')}"
    html = (
        "<!DOCTYPE html><html><body>"
        "<h2>TradingAgent 开盘验收异常</h2>"
        f"<pre style=\"white-space:pre-wrap;font-family:-apple-system,'PingFang SC',sans-serif;\">{rendered_text}</pre>"
        "</body></html>"
    )
    return email_sender.send_email(
        email_sender.CHANNELS["system"]["to"],
        subject,
        rendered_text,
        html,
        channel="system",
        rate_limit_type=f"opening_acceptance:{status}",
    )


def _maybe_send_alert(report: dict[str, Any], rendered_text: str, send_on: str) -> dict[str, Any]:
    status = str(report.get("overall_status") or "warn")
    should_send = send_on == "warn" and status != "pass"
    should_send = should_send or (send_on == "fail" and status == "fail")
    if not should_send:
        return {"status": "skipped", "reason": "opening_acceptance_pass_or_send_disabled"}
    return _send_alert(report, rendered_text)


def _next_actions(checks: list[AcceptanceCheck]) -> list[str]:
    actions: list[str] = []
    by_name = {check.name: check for check in checks}
    if by_name.get("sharedsignals_api") and by_name["sharedsignals_api"].status == "fail":
        actions.append("先恢复 SharedSignals API，再检查交易系统。")
    if by_name.get("halt_files") and by_name["halt_files"].status == "fail":
        actions.append("先处理 halt 文件，不能继续自动交易验收。")
    ashare = by_name.get("ashare_opening_acceptance")
    if ashare and ashare.status == "warn":
        reason = ashare.details.get("reason") or "unknown"
        actions.append(f"A股继续观察：{reason}。")
    cnf = by_name.get("cn_futures_opening_acceptance")
    if cnf and cnf.status == "warn":
        reason = cnf.details.get("reason") or "unknown"
        actions.append(f"中国期货继续观察：{reason}。")
    if by_name.get("sim_market_health") and by_name["sim_market_health"].status != "pass":
        actions.append("复核模拟盘总巡检中的 warn/fail 市场。")
    return actions or ["当前可接受，继续按定时任务观察下一轮。"]


def render_text(report: dict[str, Any]) -> str:
    status_label = {"pass": "通过", "warn": "警告", "fail": "失败"}.get(str(report.get("overall_status")), str(report.get("overall_status")))
    lines = [
        f"开盘验收：{status_label}",
        f"时间：{report.get('generated_at')}",
        f"结果：通过 {report.get('summary', {}).get('pass', 0)}，警告 {report.get('summary', {}).get('warn', 0)}，失败 {report.get('summary', {}).get('fail', 0)}",
    ]
    for check in report.get("checks", []):
        label = {"pass": "通过", "warn": "警告", "fail": "失败"}.get(str(check.get("status")), str(check.get("status")))
        line = f"- {check.get('name')}: {label}；{check.get('summary')}"
        details = check.get("details") or {}
        reason = details.get("reason")
        if reason:
            line += f"；原因={reason}"
        latest_bar = details.get("latest_bar_time")
        if latest_bar:
            line += f"；最新bar={latest_bar}"
        samples = details.get("sample_summary")
        if isinstance(samples, dict):
            rendered_sample_keys = ("bar_count", "signals", "local_sim_trades", "sim_execution_receipts", "daily_reviews")
            has_sample_value = any(
                samples.get(key) is not None and samples.get(key) != "" and samples.get(key) != {}
                for key in rendered_sample_keys
            )
            if has_sample_value:
                signals = samples.get("signals") if isinstance(samples.get("signals"), dict) else {}
                signal_total = sum(int(value or 0) for value in signals.values())
                line += (
                    f"；bar={samples.get('bar_count') or 0}"
                    f"；信号={signal_total}"
                    f"；成交={samples.get('local_sim_trades') or 0}"
                    f"；回执={samples.get('sim_execution_receipts') or 0}"
                    f"；复盘={samples.get('daily_reviews') or 0}"
                )
        no_trade_category = details.get("no_trade_category")
        if no_trade_category:
            line += f"；无交易分类={no_trade_category}"
        lines.append(line)
    lines.append("下一步：")
    lines.extend(f"- {item}" for item in report.get("next_actions", []))
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only opening acceptance summary.")
    parser.add_argument("--now", default=None)
    parser.add_argument("--health-input-root", type=Path, default=DEFAULT_HEALTH_INPUT_ROOT)
    parser.add_argument("--sharedsignals-root", type=Path, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--sharedsignals-api-url", default=os.environ.get("SHAREDSIGNALS_API_URL", DEFAULT_SHAREDSIGNALS_API_URL))
    parser.add_argument("--sqlite-db", type=Path, default=DEFAULT_SQLITE_DB)
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON instead of concise text.")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON when --json is used.")
    parser.add_argument("--send-on", choices=["warn", "fail", "never"], default="never")
    parser.add_argument("--exit-zero", action="store_true", help="Return 0 after writing/reporting so cron does not retry identical alerts.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = run_acceptance(
        now=_now_cn(args.now) if args.now else None,
        health_input_root=args.health_input_root,
        sharedsignals_api_url=args.sharedsignals_api_url,
        sqlite_db=args.sqlite_db,
    )
    rendered = render_text(report)
    email_result = _maybe_send_alert(report, rendered, args.send_on)
    report["email"] = email_result
    _write_outputs(report)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2 if args.pretty else None))
    else:
        print(rendered)
        if email_result.get("status") not in {"skipped", "rate_limited"}:
            print(f"邮件: {email_result.get('status')} -> {email_result.get('to')}")
    if args.exit_zero:
        return 0
    return 2 if report["overall_status"] == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
