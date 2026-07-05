#!/usr/bin/env python3
"""Read-only CNFutures live-chain validation.

This script checks whether the China futures 5-minute data and simulated
trading loop are ready for live observation. It does not create signals, place
orders, write reviews, or mutate cron state.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CN_FUTURES_REVIEW = ROOT / "shared/review/data/cn_futures_sim_reviews.jsonl"
CN_FUTURES_STYLE_COMPARISON = ROOT / "shared/review/cn_futures/style_comparison.json"
CN_FUTURES_STYLE_PERFORMANCE = ROOT / "shared/review/cn_futures/style_performance.jsonl"
CN_FUTURES_EVOLUTION_PLAN = ROOT / "shared/review/cn_futures/evolution_plan.json"
CN_FUTURES_STYLE_WEIGHTS = ROOT / "shared/review/cn_futures/style_weights.json"
CN_FUTURES_SIM_LOG = ROOT / "shared/logs/cron/cn_futures_sim.log"


@dataclass
class Check:
    name: str
    status: str
    summary: str
    details: dict[str, Any] = field(default_factory=dict)
    severity: str = "error"


RunCommand = Callable[..., subprocess.CompletedProcess[str]]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _file_age_minutes(path: Path) -> float | None:
    if not path.exists():
        return None
    return round(max(0.0, datetime.now(timezone.utc).timestamp() - path.stat().st_mtime) / 60.0, 2)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return rows
    for line in lines:
        text = line.strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _latest_json_from_log(path: Path) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if path.exists():
        for line in reversed(path.read_text(encoding="utf-8", errors="replace").splitlines()[-300:]):
            start = line.find("{")
            if start < 0:
                continue
            try:
                parsed = json.loads(line[start:])
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                payload = parsed
                break
    return {
        "path": str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path),
        "exists": path.exists(),
        "age_minutes": _file_age_minutes(path),
        "payload": payload,
    }


def _overall_status(checks: list[Check]) -> str:
    if any(check.status == "fail" for check in checks):
        return "fail"
    if any(check.status == "warn" for check in checks):
        return "warn"
    return "pass"


def resolve_sharedsignals_root(value: Path | None = None) -> Path:
    if value is not None:
        return value
    env_value = os.environ.get("SHAREDSIGNALS_ROOT", "").strip()
    if env_value:
        return Path(env_value)
    sibling = ROOT.parent / "SharedSignals"
    if sibling.exists():
        return sibling
    return Path("/opt/investment/SharedSignals")


def check_sharedsignals_freshness(
    sharedsignals_root: Path,
    *,
    sqlite_db: Path | None = None,
    max_age_minutes: int = 10,
    python_bin: str | None = None,
    run_command: RunCommand = subprocess.run,
) -> Check:
    tool = sharedsignals_root / "tools/check_cn_futures_5min_freshness.py"
    if not tool.exists():
        return Check(
            "sharedsignals_5min_freshness",
            "fail",
            "SharedSignals 期货5分钟新鲜度脚本不存在",
            {"sharedsignals_root": str(sharedsignals_root), "tool": str(tool)},
        )

    command = [
        python_bin or sys.executable,
        str(tool),
        "--json",
        "--max-age-minutes",
        str(max(max_age_minutes, 1)),
    ]
    if sqlite_db is not None:
        command.extend(["--sqlite-db", str(sqlite_db)])
    env = dict(os.environ)
    env["PYTHONPATH"] = f"{sharedsignals_root}{os.pathsep}{env.get('PYTHONPATH', '')}".rstrip(os.pathsep)

    try:
        result = run_command(
            command,
            cwd=str(sharedsignals_root),
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=25,
        )
    except Exception as exc:  # noqa: BLE001
        return Check(
            "sharedsignals_5min_freshness",
            "fail",
            "SharedSignals 期货5分钟新鲜度检查无法执行",
            {"error": f"{exc.__class__.__name__}: {exc}", "command": command},
        )

    stdout = (result.stdout or "").strip()
    try:
        payload = json.loads(stdout.splitlines()[-1]) if stdout else {}
    except json.JSONDecodeError:
        return Check(
            "sharedsignals_5min_freshness",
            "fail",
            "SharedSignals 期货5分钟新鲜度输出不是有效 JSON",
            {"returncode": result.returncode, "stdout_tail": stdout[-500:], "stderr_tail": (result.stderr or "")[-500:]},
        )

    freshness_status = str(payload.get("status") or "unknown")
    if result.returncode == 0 and freshness_status == "fresh":
        status = "pass"
        summary = "SharedSignals 期货5分钟数据新鲜"
    elif result.returncode in {0, 1} and freshness_status in {"stale", "no_data"}:
        status = "warn"
        summary = f"SharedSignals 期货5分钟数据当前为 {freshness_status}"
    else:
        status = "fail"
        summary = "SharedSignals 期货5分钟新鲜度检查失败"

    return Check(
        "sharedsignals_5min_freshness",
        status,
        summary,
        {
            "returncode": result.returncode,
            "report": payload,
            "stderr_tail": (result.stderr or "")[-500:],
            "tool": str(tool),
        },
        severity="warn" if status == "warn" else "error",
    )


def check_cron_entries(crontab_text: str | None = None, crontab_error: str = "") -> Check:
    if crontab_text is None:
        try:
            from shared.runtime_test.market_health import _installed_crontab_text

            crontab_text, crontab_error = _installed_crontab_text()
        except Exception as exc:  # noqa: BLE001
            crontab_text = ""
            crontab_error = f"{exc.__class__.__name__}: {exc}"

    required = {
        "sharedsignals_collector": "cn_futures_5min.sh",
        "tradingagent_sim": "job_cn_futures_sim.sh",
        "tradingagent_evolution": "job_cn_futures_evolution.sh",
        "tradingagent_observation": "job_cn_futures_observation_report.sh",
    }
    found = {name: token in (crontab_text or "") for name, token in required.items()}
    missing = [name for name, exists in found.items() if not exists]
    if not missing:
        status = "pass"
        summary = "SharedSignals 采集和 TradingAgent 模拟盘 cron 已安装"
    elif crontab_error and not crontab_text:
        status = "warn"
        summary = "无法读取当前 cron，需在服务器确认"
    else:
        status = "fail"
        summary = "CNFutures 5分钟链路 cron 缺失"
    return Check(
        "cn_futures_cron",
        status,
        summary,
        {"found": found, "missing": missing, "crontab_error": crontab_error},
        severity="warn" if status == "warn" else "error",
    )


def check_sim_log(log_path: Path | None = None) -> Check:
    log_path = log_path or CN_FUTURES_SIM_LOG
    latest = _latest_json_from_log(log_path)
    payload = latest.get("payload") if isinstance(latest.get("payload"), dict) else {}
    if not latest["exists"]:
        return Check("cn_futures_sim_log", "warn", "CNFutures 模拟盘 cron 日志还不存在", latest, severity="warn")
    cadence = str(payload.get("cadence") or "").lower()
    filled_count = int(payload.get("filled_count") or payload.get("signals") or 0) if payload else 0
    latest_bar_time = str(payload.get("latest_bar_time") or payload.get("bar_time") or "").strip()
    if payload and cadence == "5min" and filled_count > 0 and not latest_bar_time:
        latest["reason"] = "missing_5min_bar_time"
        return Check("cn_futures_sim_log", "warn", "CNFutures 5分钟模拟盘日志有成交但缺少 bar_time", latest, severity="warn")
    if payload and payload.get("status") in {"ok", "market_closed"}:
        return Check("cn_futures_sim_log", "pass", "CNFutures 模拟盘最近一次日志正常", latest, severity="info")
    if payload:
        return Check("cn_futures_sim_log", "warn", "CNFutures 模拟盘最近一次日志需要复核", latest, severity="warn")
    return Check("cn_futures_sim_log", "warn", "CNFutures 模拟盘日志存在但未找到 JSON 结果", latest, severity="warn")


def check_review(review_path: Path | None = None) -> Check:
    review_path = review_path or CN_FUTURES_REVIEW
    rows = _read_jsonl(review_path)
    latest = rows[-1] if rows else {}
    details = {
        "path": str(review_path.relative_to(ROOT)) if review_path.is_relative_to(ROOT) else str(review_path),
        "exists": review_path.exists(),
        "age_minutes": _file_age_minutes(review_path),
        "review_rows": len(rows),
        "latest_generated_at": latest.get("generated_at", ""),
        "latest_state": latest.get("state", ""),
        "latest_cadence": latest.get("cadence", ""),
        "latest_bar_time": latest.get("latest_bar_time") or latest.get("bar_time") or "",
        "latest_has_bar_time": bool(latest.get("latest_bar_time") or latest.get("bar_time")),
        "latest_real_trading_enabled": bool(latest.get("real_trading_enabled")),
        "latest_filled_count": int(latest.get("filled_count") or 0) if latest else 0,
        "latest_error_count": int(latest.get("error_count") or 0) if latest else 0,
        "latest_error_summary": latest.get("error_summary") if isinstance(latest.get("error_summary"), dict) else {},
        "latest_style_health": latest.get("style_health") if isinstance(latest.get("style_health"), dict) else {},
    }
    if not rows:
        return Check("cn_futures_review", "warn", "CNFutures 复盘样本还未产生", details, severity="warn")
    if details["latest_real_trading_enabled"]:
        return Check("cn_futures_review", "warn", "CNFutures 复盘样本错误带有实盘启用标记", details, severity="warn")
    if details["latest_filled_count"] > 0 and str(details["latest_cadence"]).lower() == "5min" and not details["latest_has_bar_time"]:
        return Check("cn_futures_review", "warn", "CNFutures 5分钟复盘样本有成交但缺少 bar_time", details, severity="warn")
    if details["latest_filled_count"] > 0:
        return Check("cn_futures_review", "pass", "CNFutures 最近复盘已有模拟成交样本", details, severity="info")
    return Check("cn_futures_review", "warn", "CNFutures 最近复盘存在但尚无模拟成交样本", details, severity="warn")


def check_style_outputs(
    comparison_path: Path | None = None,
    performance_path: Path | None = None,
) -> Check:
    comparison_path = comparison_path or CN_FUTURES_STYLE_COMPARISON
    performance_path = performance_path or CN_FUTURES_STYLE_PERFORMANCE
    comparison = _read_json(comparison_path)
    performance_rows = _read_jsonl(performance_path)
    details = {
        "style_comparison": {
            "path": str(comparison_path.relative_to(ROOT)) if comparison_path.is_relative_to(ROOT) else str(comparison_path),
            "exists": comparison_path.exists(),
            "age_minutes": _file_age_minutes(comparison_path),
            "type": type(comparison).__name__ if comparison is not None else "",
            "item_count": len(comparison) if isinstance(comparison, list) else (len(comparison) if isinstance(comparison, dict) else 0),
        },
        "style_performance": {
            "path": str(performance_path.relative_to(ROOT)) if performance_path.is_relative_to(ROOT) else str(performance_path),
            "exists": performance_path.exists(),
            "age_minutes": _file_age_minutes(performance_path),
            "row_count": len(performance_rows),
            "latest": performance_rows[-1] if performance_rows else {},
        },
    }
    if comparison_path.exists() and performance_path.exists() and performance_rows:
        return Check("cn_futures_style_outputs", "pass", "CNFutures 风格对比和表现历史已生成", details, severity="info")
    return Check("cn_futures_style_outputs", "warn", "CNFutures 风格输出还不完整", details, severity="warn")


def check_evolution_outputs(
    plan_path: Path | None = None,
    weights_path: Path | None = None,
) -> Check:
    plan_path = plan_path or CN_FUTURES_EVOLUTION_PLAN
    weights_path = weights_path or CN_FUTURES_STYLE_WEIGHTS
    plan_payload = _read_json(plan_path)
    weights_payload = _read_json(weights_path)
    plan = plan_payload if isinstance(plan_payload, dict) else {}
    weights = weights_payload if isinstance(weights_payload, dict) else {}
    style_weights = weights.get("styles") if isinstance(weights.get("styles"), dict) else {}
    details = {
        "evolution_plan": {
            "path": str(plan_path.relative_to(ROOT)) if plan_path.is_relative_to(ROOT) else str(plan_path),
            "exists": plan_path.exists(),
            "age_minutes": _file_age_minutes(plan_path),
            "state": plan.get("state", "") if isinstance(plan, dict) else "",
            "action_count": len(plan.get("actions") or []) if isinstance(plan, dict) else 0,
        },
        "style_weights": {
            "path": str(weights_path.relative_to(ROOT)) if weights_path.is_relative_to(ROOT) else str(weights_path),
            "exists": weights_path.exists(),
            "age_minutes": _file_age_minutes(weights_path),
            "style_count": len(style_weights),
            "real_trading_enabled": bool(weights.get("real_trading_enabled")) if isinstance(weights, dict) else None,
        },
    }
    if plan_path.exists() and weights_path.exists() and style_weights:
        if bool(weights.get("real_trading_enabled")):
            return Check("cn_futures_evolution_outputs", "fail", "CNFutures 自迭代输出错误启用了实盘标记", details)
        return Check("cn_futures_evolution_outputs", "pass", "CNFutures 模拟盘自迭代输出已生成", details, severity="info")
    return Check("cn_futures_evolution_outputs", "warn", "CNFutures 模拟盘自迭代输出还未生成", details, severity="warn")


def check_existing_health_surfaces() -> Check:
    details: dict[str, Any] = {}
    statuses: list[str] = []
    try:
        from shared.runtime_test.market_health import run_sim_market_health

        health = run_sim_market_health(("cn_futures",))
        details["market_health"] = health
        statuses.append(str(health.get("overall_status") or "unknown"))
    except Exception as exc:  # noqa: BLE001
        details["market_health_error"] = f"{exc.__class__.__name__}: {exc}"
        statuses.append("fail")

    try:
        from shared.runtime_test.ops_report import cn_futures_review_summary

        details["ops_review_summary"] = cn_futures_review_summary()
    except Exception as exc:  # noqa: BLE001
        details["ops_review_summary_error"] = f"{exc.__class__.__name__}: {exc}"
        statuses.append("fail")

    if "fail" in statuses:
        return Check("cn_futures_existing_health_surfaces", "fail", "已有 health/ops 入口读取 CNFutures 失败", details)
    if "warn" in statuses:
        return Check("cn_futures_existing_health_surfaces", "warn", "已有 health/ops 入口可读但需要复核", details, severity="warn")
    return Check("cn_futures_existing_health_surfaces", "pass", "已有 health/ops 入口可读取 CNFutures 状态", details, severity="info")


def recommendations(checks: list[Check]) -> list[str]:
    failed = [check.name for check in checks if check.status == "fail"]
    warned = [check.name for check in checks if check.status == "warn"]
    if failed:
        return [f"先处理硬失败检查项: {', '.join(failed)}。"]
    notes: list[str] = []
    if "sharedsignals_5min_freshness" in warned:
        notes.append("若当前是周末或非交易时段，可等下一次日盘/夜盘再复查5分钟数据新鲜度。")
    if "cn_futures_review" in warned or "cn_futures_style_outputs" in warned:
        notes.append("等待 TradingAgent 5分钟模拟盘产生样本后，再用复盘和风格表现判断策略有效性。")
    if not notes:
        notes.append("链路已具备观察条件；继续累计样本，不自动提升到实盘。")
    return notes


def observation_alerts(checks: list[Check]) -> list[dict[str, Any]]:
    """Return dashboard-friendly alerts without mutating runtime state."""

    by_name = {check.name: check for check in checks}
    alerts: list[dict[str, Any]] = []
    freshness = by_name.get("sharedsignals_5min_freshness")
    if freshness is not None:
        report = freshness.details.get("report") if isinstance(freshness.details.get("report"), dict) else {}
        session = report.get("session") if isinstance(report.get("session"), dict) else {}
        freshness_status = str(report.get("status") or "").lower()
        if freshness.status == "fail":
            alerts.append({"severity": "error", "code": "futures_data_check_failed", "message": "期货5分钟数据检查失败，先修数据检查入口。"})
        elif bool(session.get("in_session")) and freshness_status in {"stale", "no_data"}:
            alerts.append({"severity": "warn", "code": "futures_5min_missing_in_session", "message": "当前处于期货交易时段，但5分钟行情还没有新鲜数据。"})
    review = by_name.get("cn_futures_review")
    if review is not None and review.status == "warn":
        latest_filled = int(review.details.get("latest_filled_count") or 0)
        if latest_filled <= 0:
            alerts.append({"severity": "info", "code": "cn_futures_no_sim_samples", "message": "CNFutures 模拟盘还没有有效成交样本，暂不能评估胜率。"})
    styles = by_name.get("cn_futures_style_outputs")
    if styles is not None and styles.status == "warn":
        alerts.append({"severity": "info", "code": "cn_futures_style_outputs_incomplete", "message": "风格对比输出还不完整，继续等待模拟盘和复盘样本。"})
    evolution = by_name.get("cn_futures_evolution_outputs")
    if evolution is not None and evolution.status == "fail":
        alerts.append({"severity": "error", "code": "cn_futures_evolution_gate_failed", "message": "自迭代输出存在硬失败，不能推广任何风格。"})
    return alerts


def observation_phase(checks: list[Check]) -> str:
    by_name = {check.name: check for check in checks}
    if any(check.status == "fail" for check in checks):
        return "blocked"
    freshness = by_name.get("sharedsignals_5min_freshness")
    review = by_name.get("cn_futures_review")
    styles = by_name.get("cn_futures_style_outputs")
    if freshness is None or freshness.status != "pass":
        return "waiting_for_5min_data"
    if review is None or review.status != "pass":
        return "waiting_for_sim_samples"
    if styles is None or styles.status != "pass":
        return "waiting_for_style_review"
    return "ready_to_observe"


def next_validation_plan(checks: list[Check]) -> dict[str, Any]:
    by_name = {check.name: check for check in checks}
    freshness = by_name.get("sharedsignals_5min_freshness")
    report = freshness.details.get("report") if freshness is not None and isinstance(freshness.details.get("report"), dict) else {}
    session = report.get("session") if isinstance(report.get("session"), dict) else {}
    phase = observation_phase(checks)
    if phase == "ready_to_observe":
        expected_phase = "continue_observation"
        primary_check = "accumulate_win_rate_samples"
    elif bool(session.get("in_session")):
        expected_phase = "validate_current_session"
        primary_check = "fresh_5min_data_then_sim_sample"
    else:
        expected_phase = "wait_for_next_session"
        primary_check = "fresh_5min_data_at_next_session_open"
    return {
        "expected_phase": expected_phase,
        "primary_check": primary_check,
        "current_session": session.get("current", ""),
        "in_session": bool(session.get("in_session")),
        "next_session_start": session.get("next_session_start", ""),
        "acceptance": {
            "freshness_status": "fresh",
            "cron_required": ["cn_futures_5min.sh", "job_cn_futures_sim.sh", "job_cn_futures_observation_report.sh"],
            "requires_sim_review_with_filled_count": True,
            "requires_style_outputs": True,
            "requires_real_trading_enabled_false": True,
        },
        "manual_command": "python shared/runtime_test/cn_futures_live_check.py --pretty",
        "real_trading_enabled": False,
    }


def run_live_check(
    *,
    sharedsignals_root: Path | None = None,
    sqlite_db: Path | None = None,
    max_age_minutes: int = 10,
    python_bin: str | None = None,
    run_command: RunCommand = subprocess.run,
    crontab_text: str | None = None,
    crontab_error: str = "",
) -> dict[str, Any]:
    resolved_sharedsignals_root = resolve_sharedsignals_root(sharedsignals_root)
    checks = [
        check_sharedsignals_freshness(
            resolved_sharedsignals_root,
            sqlite_db=sqlite_db,
            max_age_minutes=max_age_minutes,
            python_bin=python_bin,
            run_command=run_command,
        ),
        check_cron_entries(crontab_text, crontab_error),
        check_sim_log(),
        check_review(),
        check_style_outputs(),
        check_evolution_outputs(),
        check_existing_health_surfaces(),
    ]
    overall = _overall_status(checks)
    return {
        "market": "cn_futures",
        "report_type": "live_chain_validation",
        "generated_at": _now_iso(),
        "overall_status": overall,
        "observation_phase": observation_phase(checks),
        "alerts": observation_alerts(checks),
        "next_validation": next_validation_plan(checks),
        "summary": {
            "pass": sum(1 for check in checks if check.status == "pass"),
            "warn": sum(1 for check in checks if check.status == "warn"),
            "fail": sum(1 for check in checks if check.status == "fail"),
        },
        "sharedsignals_root": str(resolved_sharedsignals_root),
        "checks": [check.__dict__ for check in checks],
        "recommendations": recommendations(checks),
        "real_trading_enabled": False,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only CNFutures live-chain validation.")
    parser.add_argument("--sharedsignals-root", type=Path, default=None)
    parser.add_argument("--sqlite-db", type=Path, default=None)
    parser.add_argument("--max-age-minutes", type=int, default=10)
    parser.add_argument("--python-bin", default=None, help="Python executable for SharedSignals freshness check.")
    parser.add_argument("--pretty", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = run_live_check(
        sharedsignals_root=args.sharedsignals_root,
        sqlite_db=args.sqlite_db,
        max_age_minutes=args.max_age_minutes,
        python_bin=args.python_bin,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2 if args.pretty else None))
    return 2 if report["overall_status"] == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
