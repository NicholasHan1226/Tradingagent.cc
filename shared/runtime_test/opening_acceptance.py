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

CN_TZ = timezone(timedelta(hours=8))
DEFAULT_SHAREDSIGNALS_API_URL = "http://127.0.0.1:8082"
DEFAULT_SHAREDSIGNALS_ROOT = Path(os.environ.get("SHAREDSIGNALS_ROOT", "/opt/investment/SharedSignals"))
DEFAULT_SQLITE_DB = Path("/opt/investment/MarketGraphRuntime/read_model/marketdata.sqlite")


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
    health_url = f"{api_url.rstrip('/')}/health"
    try:
        status_code, payload = _http_json(health_url)
    except (OSError, urllib.error.URLError, TimeoutError) as exc:
        return AcceptanceCheck(
            "sharedsignals_api",
            "fail",
            "SharedSignals API 不可用",
            {"url": health_url, "error": f"{exc.__class__.__name__}: {exc}"},
        )
    payload_status = str(payload.get("status") or "").lower()
    ok = 200 <= status_code < 300 and payload_status in {"ok", "healthy", "degraded"}
    checks = payload.get("checks", {}) if isinstance(payload.get("checks"), dict) else {}
    functions_status = str((checks.get("functions") or {}).get("status") or "").lower() if isinstance(checks.get("functions"), dict) else ""
    cron_status = str((checks.get("cron") or {}).get("status") or "").lower() if isinstance(checks.get("cron"), dict) else ""
    core_ok = ok and functions_status in {"ok", ""} and cron_status in {"ok", ""}
    status = "pass" if core_ok else ("warn" if ok else "fail")
    return AcceptanceCheck(
        "sharedsignals_api",
        status,
        "SharedSignals 核心 API 可用" if core_ok else ("SharedSignals API 可用但有降级项" if ok else "SharedSignals API 返回异常"),
        {
            "url": health_url,
            "status_code": status_code,
            "payload_status": payload_status,
            "functions_status": functions_status,
            "cron_status": cron_status,
        },
    )


def check_watchdog_inputs(sharedsignals_root: Path, max_age_minutes: int = 20) -> AcceptanceCheck:
    input_dir = sharedsignals_root / "logs" / "watchdog_inputs"
    expected = {
        "proxy_relay": input_dir / "proxy_relay.json",
        "tradingagent_health": input_dir / "tradingagent_health.json",
        "health_sla": input_dir / "health_sla.json",
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


def check_halt_files(sharedsignals_root: Path) -> AcceptanceCheck:
    candidates = {
        "sharedsignals_watchdog_halt": sharedsignals_root / "logs" / "WATCHDOG_HALT.json",
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


def _ashare_opening_report(now: datetime, sqlite_db: Path) -> dict[str, Any]:
    from shared.runtime_test import ashare_opening_validator as validator

    minutes = now.hour * 60 + now.minute
    if 8 * 60 <= minutes < 9 * 60 + 30:
        return validator.validate_pre_open(sqlite_db=sqlite_db, now=now, min_symbols=1000)
    if (9 * 60 + 30 <= minutes <= 11 * 60 + 30) or (13 * 60 <= minutes <= 15 * 60):
        session_start = now.replace(hour=9, minute=30, second=0, microsecond=0)
        if minutes >= 13 * 60:
            session_start = now.replace(hour=13, minute=0, second=0, microsecond=0)
        elapsed = int((now - session_start).total_seconds() // 60)
        if elapsed >= 10:
            return validator.first_sample_alerts(sqlite_db=sqlite_db, now=now, min_symbols=10, wait_minutes=10)
        return validator.validate_opening(sqlite_db=sqlite_db, now=now, min_symbols=10)
    return validator.validate_opening(sqlite_db=sqlite_db, now=now, min_symbols=10)


def check_ashare_opening(now: datetime, sqlite_db: Path) -> AcceptanceCheck:
    report = _ashare_opening_report(now, sqlite_db)
    raw_status = str(report.get("status") or "warn").lower()
    status = "fail" if raw_status == "fail" else ("warn" if raw_status == "warn" else "pass")
    reason = str(report.get("reason") or "")
    return AcceptanceCheck(
        "ashare_opening_acceptance",
        status,
        "A股开盘验收通过" if status == "pass" else "A股开盘验收需要继续观察",
        {
            "report_type": report.get("report_type"),
            "reason": reason,
            "session": report.get("session"),
            "bar_count": report.get("bar_count"),
            "symbol_count": report.get("symbol_count"),
            "latest_bar_time": report.get("latest_bar_time"),
            "no_trade_explanation": report.get("no_trade_explanation", {}),
            "alerts": report.get("alerts", []),
            "raw_status": raw_status,
        },
    )


def _cn_futures_opening_report(now: datetime, sqlite_db: Path) -> dict[str, Any]:
    from CNFutures import opening_validator as validator

    minutes = now.hour * 60 + now.minute
    if (8 * 60 <= minutes < 9 * 60) or (12 * 60 <= minutes < 13 * 60) or (20 * 60 <= minutes < 21 * 60):
        return validator.validate_pre_open(sqlite_db=sqlite_db, now=now, min_symbols=4)
    in_session = (9 * 60 <= minutes <= 15 * 60) or (21 * 60 <= minutes <= 23 * 60 + 59) or (0 <= minutes <= 2 * 60 + 30)
    if in_session:
        if 9 * 60 <= minutes <= 15 * 60:
            start = now.replace(hour=9, minute=0, second=0, microsecond=0)
        elif minutes >= 21 * 60:
            start = now.replace(hour=21, minute=0, second=0, microsecond=0)
        else:
            start = (now - timedelta(days=1)).replace(hour=21, minute=0, second=0, microsecond=0)
        elapsed = int((now - start).total_seconds() // 60)
        if elapsed >= 10:
            return validator.first_sample_alerts(sqlite_db=sqlite_db, now=now, min_symbols=4, wait_minutes=10)
        return validator.validate_opening(sqlite_db=sqlite_db, now=now, min_symbols=4)
    return validator.validate_opening(sqlite_db=sqlite_db, now=now, min_symbols=4)


def check_cn_futures_opening(now: datetime, sqlite_db: Path) -> AcceptanceCheck:
    report = _cn_futures_opening_report(now, sqlite_db)
    raw_status = str(report.get("status") or "warn").lower()
    status = "fail" if raw_status == "fail" else ("warn" if raw_status == "warn" else "pass")
    return AcceptanceCheck(
        "cn_futures_opening_acceptance",
        status,
        "中国期货开盘验收通过" if status == "pass" else "中国期货开盘验收需要继续观察",
        {
            "report_type": report.get("report_type"),
            "reason": report.get("reason"),
            "session": report.get("session"),
            "bar_count": report.get("bar_count"),
            "symbol_count": report.get("symbol_count"),
            "latest_bar_time": report.get("latest_bar_time"),
            "opening_30m_review": report.get("opening_30m_review", {}),
            "alerts": report.get("alerts", []),
            "raw_status": raw_status,
        },
    )


def run_acceptance(
    *,
    now: datetime | None = None,
    sharedsignals_root: Path = DEFAULT_SHAREDSIGNALS_ROOT,
    sharedsignals_api_url: str = DEFAULT_SHAREDSIGNALS_API_URL,
    sqlite_db: Path = DEFAULT_SQLITE_DB,
) -> dict[str, Any]:
    current = now or _now_cn()
    checks = [
        check_sharedsignals(sharedsignals_api_url),
        check_watchdog_inputs(sharedsignals_root),
        check_halt_files(sharedsignals_root),
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
        reason = (check.get("details") or {}).get("reason")
        if reason:
            line += f"；原因={reason}"
        latest_bar = (check.get("details") or {}).get("latest_bar_time")
        if latest_bar:
            line += f"；最新bar={latest_bar}"
        lines.append(line)
    lines.append("下一步：")
    lines.extend(f"- {item}" for item in report.get("next_actions", []))
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only opening acceptance summary.")
    parser.add_argument("--now", default=None)
    parser.add_argument("--sharedsignals-root", type=Path, default=DEFAULT_SHAREDSIGNALS_ROOT)
    parser.add_argument("--sharedsignals-api-url", default=os.environ.get("SHAREDSIGNALS_API_URL", DEFAULT_SHAREDSIGNALS_API_URL))
    parser.add_argument("--sqlite-db", type=Path, default=DEFAULT_SQLITE_DB)
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON instead of concise text.")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON when --json is used.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = run_acceptance(
        now=_now_cn(args.now) if args.now else None,
        sharedsignals_root=args.sharedsignals_root,
        sharedsignals_api_url=args.sharedsignals_api_url,
        sqlite_db=args.sqlite_db,
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2 if args.pretty else None))
    else:
        print(render_text(report))
    return 2 if report["overall_status"] == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
