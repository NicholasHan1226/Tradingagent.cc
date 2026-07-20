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
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# This diagnostic owns a retired /realtime_5min path. A direct invocation must
# not load it while TradingDatas has not supplied a fresh V1 handoff.
if __name__ == "__main__":
    from shared.governance.retirement import retired_cli

    raise SystemExit(retired_cli("shared.runtime_test.cn_futures_live_check"))

from CNFutures.review import latest_actionable_review  # noqa: E402
from CNFutures.sample_maturity import (  # noqa: E402
    validate_futures_maturity_projection_hash,
)
from CNFutures.session import (  # noqa: E402
    cn_futures_session_state,
    is_current_session_bar,
    parse_cn_datetime,
)

CN_FUTURES_REVIEW = ROOT / "shared/review/data/cn_futures_sim_reviews.jsonl"
CN_FUTURES_MATURITY = ROOT / "shared/review/cn_futures/market_maturity_latest.json"
CN_FUTURES_SIM_LOG = ROOT / "shared/logs/cron/job_cn_futures_sim.log"
CN_FUTURES_LEGACY_SIM_LOG = ROOT / "shared/logs/cron/cn_futures_sim.log"

CURRENT_CAPITAL_AUTHORITY_ID = "cn-futures-capital-v1"
CURRENT_AUTHORITY_GENERATION = 1
CURRENT_POOL_CNY = 50_000
CURRENT_MARGIN_LIMIT_CNY = 25_000
CURRENT_MATURITY_REPORT_TYPE = "cn_futures_market_maturity_v1"
CURRENT_EVIDENCE_SOURCE = "cn_futures_review_journal+sample_kpi"
CURRENT_PROMOTION_POLICY = "manual_review_only_no_futures_live_date"
CURRENT_MATURITY_STAGES = {
    "stage_initial_samples",
    "stage_coverage_building",
    "stage_stability_evaluating",
    "stage_eligible_pending_confirmation",
}


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
    return round(
        max(0.0, datetime.now(timezone.utc).timestamp() - path.stat().st_mtime) / 60.0,
        2,
    )


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _normalized_trade_date(value: Any) -> str:
    raw = str(value or "").strip()
    digits = "".join(character for character in raw[:10] if character.isdigit())
    return digits if len(digits) == 8 else ""


def _is_sha256(value: Any) -> bool:
    raw = str(value or "").strip().lower()
    return len(raw) == 64 and all(character in "0123456789abcdef" for character in raw)


def validate_cn_futures_maturity_projection(
    payload: Mapping[str, Any], *, expected_trade_date: str = ""
) -> list[str]:
    """Validate the only active CNFutures evolution/maturity projection."""

    issues: list[str] = []
    if not validate_futures_maturity_projection_hash(payload):
        issues.append("projection_sha256_invalid")
    if payload.get("report_type") != CURRENT_MATURITY_REPORT_TYPE:
        issues.append("report_type_mismatch")
    if payload.get("evidence_source") != CURRENT_EVIDENCE_SOURCE:
        issues.append("evidence_source_mismatch")
    if payload.get("market") != "cnfutures":
        issues.append("market_mismatch")
    if payload.get("capital_layer") != "simulated":
        issues.append("capital_layer_must_be_simulated")
    if payload.get("account_type") != "simulated":
        issues.append("account_type_must_be_simulated")

    authority = payload.get("authority_scope")
    if not isinstance(authority, Mapping):
        issues.append("authority_scope_required")
        authority = {}
    if authority.get("capital_authority_id") != CURRENT_CAPITAL_AUTHORITY_ID:
        issues.append("capital_authority_id_mismatch")
    if authority.get("authority_generation") != CURRENT_AUTHORITY_GENERATION:
        issues.append("authority_generation_mismatch")
    lineage = str(authority.get("execution_lineage_id") or "").strip()
    if not lineage:
        issues.append("execution_lineage_id_required")

    if payload.get("pool_cny") != CURRENT_POOL_CNY:
        issues.append("pool_cny_must_be_50000")
    if payload.get("margin_utilization_limit_cny") != CURRENT_MARGIN_LIMIT_CNY:
        issues.append("margin_limit_cny_must_be_25000")
    if payload.get("stage") not in CURRENT_MATURITY_STAGES:
        issues.append("maturity_stage_invalid")
    if payload.get("promotion_policy_status") != CURRENT_PROMOTION_POLICY:
        issues.append("promotion_policy_status_mismatch")
    for policy_field in (
        "automatic_promotion_enabled",
        "automatic_risk_expansion_enabled",
        "live_transition_authorized",
        "real_trading_enabled",
        "live_execution_enabled",
    ):
        if payload.get(policy_field) is not False:
            issues.append(f"{policy_field}_must_be_false")
    if not isinstance(payload.get("sample_counts"), Mapping):
        issues.append("sample_counts_required")
    if not isinstance(payload.get("performance"), Mapping):
        issues.append("performance_required")
    sample_kpi = payload.get("sample_kpi_projection")
    if not isinstance(sample_kpi, Mapping) or not isinstance(
        sample_kpi.get("styles"), Mapping
    ):
        issues.append("sample_kpi_projection_required")
    if not _is_sha256(payload.get("source_review_sha256")):
        issues.append("source_review_sha256_required")
    try:
        generated_at = datetime.fromisoformat(
            str(payload.get("generated_at") or "").replace("Z", "+00:00")
        )
    except ValueError:
        generated_at = None
    if (
        generated_at is None
        or generated_at.tzinfo is None
        or generated_at.utcoffset() is None
    ):
        issues.append("generated_at_timezone_required")

    expected = _normalized_trade_date(expected_trade_date)
    actual = _normalized_trade_date(payload.get("trade_date"))
    if not actual:
        issues.append("trade_date_required")
    elif expected and actual != expected:
        issues.append("trade_date_stale")
    return list(dict.fromkeys(issues))


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
        for line in reversed(
            path.read_text(encoding="utf-8", errors="replace").splitlines()[-300:]
        ):
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


def _latest_json_from_logs(paths: list[Path]) -> dict[str, Any]:
    checked: list[dict[str, Any]] = []
    for path in paths:
        latest = _latest_json_from_log(path)
        checked.append(
            {key: latest.get(key) for key in ("path", "exists", "age_minutes")}
        )
        if latest.get("payload"):
            latest["checked_paths"] = checked
            return latest
    fallback = _latest_json_from_log(paths[0])
    fallback["checked_paths"] = checked
    return fallback


def _overall_status(checks: list[Check]) -> str:
    if any(check.status == "fail" for check in checks):
        return "fail"
    if any(check.status == "warn" for check in checks):
        return "warn"
    return "pass"


def resolve_sharedsignals_root(value: Path | None = None) -> Path:
    return value or Path("")


def check_sharedsignals_freshness(
    sharedsignals_root: Path,
    *,
    sqlite_db: Path | None = None,
    max_age_minutes: int = 10,
    python_bin: str | None = None,
    run_command: RunCommand = subprocess.run,
) -> Check:
    if run_command is not subprocess.run:
        result = run_command(
            [], check=False, capture_output=True, text=True, timeout=25
        )
        stdout = (result.stdout or "").strip()
        try:
            payload = json.loads(stdout.splitlines()[-1]) if stdout else {}
        except json.JSONDecodeError:
            return Check(
                "sharedsignals_5min_freshness",
                "fail",
                "SharedSignals 期货5分钟新鲜度输出不是有效 JSON",
                {
                    "returncode": result.returncode,
                    "stdout_tail": stdout[-500:],
                    "stderr_tail": (result.stderr or "")[-500:],
                },
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
                "source": "test_injected_runner",
            },
            severity="warn" if status == "warn" else "error",
        )

    base_url = (
        os.environ.get("SHAREDSIGNALS_API_URL", "http://127.0.0.1:8082")
        .strip()
        .rstrip("/")
    )
    session_state = cn_futures_session_state()
    params = urllib.parse.urlencode({"market": "Futures"})
    url = f"{base_url}/realtime_5min?{params}"
    try:
        req = urllib.request.Request(
            url, headers={"Accept": "application/json"}, method="GET"
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (
        OSError,
        urllib.error.URLError,
        urllib.error.HTTPError,
        json.JSONDecodeError,
    ) as exc:
        return Check(
            "sharedsignals_5min_freshness",
            "fail",
            "SharedSignals 期货5分钟 API 无法访问",
            {"error": f"{exc.__class__.__name__}: {exc}", "url": url},
        )

    rows = payload.get("data") if isinstance(payload, dict) else []
    if not isinstance(rows, list):
        rows = []
    session_start = str(session_state.get("session_start") or "")
    now_local = parse_cn_datetime(session_state.get("local_time")) or datetime.now(
        timezone.utc
    )
    filtered_rows: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        bar_time = str(row.get("bar_time") or row.get("time") or "")
        if not is_current_session_bar(
            bar_time,
            session_start=session_start,
            now=now_local,
            max_age_minutes=max_age_minutes,
        ):
            continue
        filtered_rows.append(row)
    freshness_status = "fresh" if filtered_rows else "stale" if rows else "no_data"
    if filtered_rows:
        status = "pass"
        summary = "SharedSignals 期货5分钟数据新鲜"
    elif freshness_status in {"stale", "no_data"}:
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
            "url": url,
            "row_count": len(rows),
            "filtered_row_count": len(filtered_rows),
            "session_start": session_start,
            "latest_bar_time": max(
                (
                    str(row.get("bar_time") or row.get("time") or "")
                    for row in filtered_rows
                ),
                default="",
            ),
            "payload_status": payload.get("status")
            if isinstance(payload, dict)
            else "",
        },
        severity="warn" if status == "warn" else "error",
    )


def check_cron_entries(
    crontab_text: str | None = None, crontab_error: str = ""
) -> Check:
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
        "tradingagent_sample_ops": "job_cn_futures_sample_ops.sh",
        "tradingagent_observation": "job_cn_futures_observation_report.sh",
        "tradingagent_calibration": "job_cn_futures_calibration_report.sh",
        "tradingagent_pre_open_validation": "job_cn_futures_pre_open_validation.sh",
        "tradingagent_first_sample_alert": "job_cn_futures_first_sample_alert.sh",
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
    paths = (
        [log_path]
        if log_path is not None
        else [CN_FUTURES_SIM_LOG, CN_FUTURES_LEGACY_SIM_LOG]
    )
    latest = _latest_json_from_logs(paths)
    payload = latest.get("payload") if isinstance(latest.get("payload"), dict) else {}
    if not latest["exists"]:
        return Check(
            "cn_futures_sim_log",
            "warn",
            "CNFutures 模拟盘 cron 日志还不存在",
            latest,
            severity="warn",
        )
    cadence = str(payload.get("cadence") or "").lower()
    filled_count = (
        int(payload.get("filled_count") or payload.get("signals") or 0)
        if payload
        else 0
    )
    latest_bar_time = str(
        payload.get("latest_bar_time") or payload.get("bar_time") or ""
    ).strip()
    if payload and cadence == "5min" and filled_count > 0 and not latest_bar_time:
        latest["reason"] = "missing_5min_bar_time"
        return Check(
            "cn_futures_sim_log",
            "warn",
            "CNFutures 5分钟模拟盘日志有成交但缺少 bar_time",
            latest,
            severity="warn",
        )
    state = (
        str(payload.get("state") or payload.get("status") or "").lower().strip()
        if payload
        else ""
    )
    if payload and state in {"ok", "market_closed", "observation_only"}:
        return Check(
            "cn_futures_sim_log",
            "pass",
            "CNFutures 模拟盘最近一次日志正常",
            latest,
            severity="info",
        )
    if payload:
        return Check(
            "cn_futures_sim_log",
            "warn",
            "CNFutures 模拟盘最近一次日志需要复核",
            latest,
            severity="warn",
        )
    return Check(
        "cn_futures_sim_log",
        "warn",
        "CNFutures 模拟盘日志存在但未找到 JSON 结果",
        latest,
        severity="warn",
    )


def check_review(review_path: Path | None = None) -> Check:
    review_path = review_path or CN_FUTURES_REVIEW
    rows = _read_jsonl(review_path)
    try:
        from CNFutures.session import active_trade_date

        target_trade_date = active_trade_date()
    except Exception:
        target_trade_date = ""
    latest = latest_actionable_review(rows, trade_date=target_trade_date or None)
    hold_summary = (
        latest.get("hold_reason_summary")
        if isinstance(latest.get("hold_reason_summary"), dict)
        else {}
    )
    hold_by_reason = (
        hold_summary.get("by_reason")
        if isinstance(hold_summary.get("by_reason"), dict)
        else {}
    )
    by_product_by_reason = (
        hold_summary.get("by_product_by_reason")
        if isinstance(hold_summary.get("by_product_by_reason"), dict)
        else {}
    )
    latest_hold_count = (
        int(latest.get("hold_count") or hold_summary.get("total") or 0) if latest else 0
    )
    top_hold_reason = ""
    if hold_by_reason:
        top_hold_reason = max(
            hold_by_reason.items(), key=lambda item: int(item[1] or 0)
        )[0]
    insufficient_consecutive_bars_by_product: dict[str, int] = {}
    for product, reasons in by_product_by_reason.items():
        if isinstance(reasons, dict):
            count = int(reasons.get("insufficient_consecutive_5min_bars") or 0)
            if count:
                insufficient_consecutive_bars_by_product[str(product)] = count
    sample_phase = "missing_sim_sample"
    if int(latest.get("filled_count") or 0) > 0:
        sample_phase = "filled_sample"
    elif latest_hold_count > 0 and top_hold_reason in {
        "style_session_not_allowed",
        "night_session_not_allowed",
    }:
        sample_phase = "no_night_session"
    elif latest_hold_count > 0:
        sample_phase = "strategy_hold"
    details = {
        "path": str(review_path.relative_to(ROOT))
        if review_path.is_relative_to(ROOT)
        else str(review_path),
        "exists": review_path.exists(),
        "age_minutes": _file_age_minutes(review_path),
        "review_rows": len(rows),
        "current_trade_date": target_trade_date,
        "latest_generated_at": latest.get("generated_at", ""),
        "latest_state": latest.get("state", ""),
        "latest_cadence": latest.get("cadence", ""),
        "latest_bar_time": latest.get("latest_bar_time")
        or latest.get("bar_time")
        or "",
        "latest_has_bar_time": bool(
            latest.get("latest_bar_time") or latest.get("bar_time")
        ),
        "latest_real_trading_enabled": bool(latest.get("real_trading_enabled")),
        "latest_filled_count": int(latest.get("filled_count") or 0) if latest else 0,
        "latest_hold_count": latest_hold_count,
        "latest_top_hold_reason": top_hold_reason,
        "latest_sample_phase": sample_phase if latest else "",
        "latest_error_count": int(latest.get("error_count") or 0) if latest else 0,
        "latest_error_summary": latest.get("error_summary")
        if isinstance(latest.get("error_summary"), dict)
        else {},
        "latest_style_health": latest.get("style_health")
        if isinstance(latest.get("style_health"), dict)
        else {},
        "latest_hold_reason_summary": hold_summary,
        "insufficient_consecutive_bars_by_product": insufficient_consecutive_bars_by_product,
    }
    if not rows:
        return Check(
            "cn_futures_review",
            "warn",
            "CNFutures 复盘样本还未产生",
            details,
            severity="warn",
        )
    if details["latest_real_trading_enabled"]:
        return Check(
            "cn_futures_review",
            "warn",
            "CNFutures 复盘样本错误带有实盘启用标记",
            details,
            severity="warn",
        )
    if (
        details["latest_filled_count"] > 0
        and str(details["latest_cadence"]).lower() == "5min"
        and not details["latest_has_bar_time"]
    ):
        return Check(
            "cn_futures_review",
            "warn",
            "CNFutures 5分钟复盘样本有成交但缺少 bar_time",
            details,
            severity="warn",
        )
    if details["latest_filled_count"] > 0:
        return Check(
            "cn_futures_review",
            "pass",
            "CNFutures 最近复盘已有模拟成交样本",
            details,
            severity="info",
        )
    if sample_phase == "no_night_session":
        return Check(
            "cn_futures_review",
            "pass",
            "CNFutures 最近复盘显示夜盘未授权风格主动不交易",
            details,
            severity="info",
        )
    if sample_phase == "strategy_hold":
        return Check(
            "cn_futures_review",
            "pass",
            "CNFutures 最近复盘显示策略主动 hold，尚无模拟成交样本",
            details,
            severity="info",
        )
    return Check(
        "cn_futures_review",
        "warn",
        "CNFutures 最近复盘存在但尚无模拟成交样本",
        details,
        severity="warn",
    )


def check_maturity_projection(
    maturity_path: Path | None = None,
    *,
    expected_trade_date: str = "",
) -> Check:
    """Check the exact, sim-only CNFutures maturity/KPI projection."""

    path = maturity_path or CN_FUTURES_MATURITY
    payload = _read_json(path)
    details: dict[str, Any] = {
        "path": str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path),
        "exists": path.exists(),
        "is_symlink": path.is_symlink(),
        "age_minutes": _file_age_minutes(path),
        "expected_trade_date": _normalized_trade_date(expected_trade_date),
        "issues": [],
    }
    if not path.exists():
        details["issues"] = ["current_maturity_projection_missing"]
        return Check(
            "cn_futures_maturity_projection",
            "warn",
            "CNFutures 当前成熟度投影尚未生成",
            details,
            severity="warn",
        )
    if path.is_symlink():
        details["issues"] = ["maturity_projection_symlink_not_allowed"]
        return Check(
            "cn_futures_maturity_projection",
            "fail",
            "CNFutures 成熟度投影路径不是独立本地事实",
            details,
        )
    if not isinstance(payload, Mapping):
        details["issues"] = ["maturity_projection_not_object"]
        return Check(
            "cn_futures_maturity_projection",
            "fail",
            "CNFutures 成熟度投影无法解析",
            details,
        )
    issues = validate_cn_futures_maturity_projection(
        payload, expected_trade_date=expected_trade_date
    )
    details.update(
        {
            "issues": issues,
            "projection_sha256": payload.get("projection_sha256"),
            "trade_date": payload.get("trade_date"),
            "generated_at": payload.get("generated_at"),
            "stage": payload.get("stage"),
            "authority_scope": payload.get("authority_scope"),
            "sample_counts": payload.get("sample_counts"),
            "performance": payload.get("performance"),
            "blocking_reasons": payload.get("blocking_reasons"),
            "promotion_evidence_ready": payload.get("promotion_evidence_ready") is True,
            "promotion_policy_status": payload.get("promotion_policy_status"),
            "automatic_promotion_enabled": False,
            "automatic_risk_expansion_enabled": False,
            "real_trading_enabled": False,
        }
    )
    hard_issues = [issue for issue in issues if issue != "trade_date_stale"]
    if hard_issues:
        return Check(
            "cn_futures_maturity_projection",
            "fail",
            "CNFutures 成熟度证据不符合当前独立 50k authority",
            details,
        )
    if issues:
        return Check(
            "cn_futures_maturity_projection",
            "warn",
            "CNFutures 成熟度投影尚未刷新到当前交易日",
            details,
            severity="warn",
        )
    return Check(
        "cn_futures_maturity_projection",
        "pass",
        "CNFutures 成熟度与 KPI 证据来自当前精确 authority",
        details,
        severity="info",
    )


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
        return Check(
            "cn_futures_existing_health_surfaces",
            "fail",
            "已有 health/ops 入口读取 CNFutures 失败",
            details,
        )
    if "warn" in statuses:
        return Check(
            "cn_futures_existing_health_surfaces",
            "warn",
            "已有 health/ops 入口可读但需要复核",
            details,
            severity="warn",
        )
    return Check(
        "cn_futures_existing_health_surfaces",
        "pass",
        "已有 health/ops 入口可读取 CNFutures 状态",
        details,
        severity="info",
    )


def recommendations(checks: list[Check]) -> list[str]:
    failed = [check.name for check in checks if check.status == "fail"]
    warned = [check.name for check in checks if check.status == "warn"]
    if failed:
        return [f"先处理硬失败检查项: {', '.join(failed)}。"]
    notes: list[str] = []
    if "sharedsignals_5min_freshness" in warned:
        notes.append(
            "若当前是周末或非交易时段，可等下一次日盘/夜盘再复查5分钟数据新鲜度。"
        )
    if "cn_futures_review" in warned:
        notes.append("等待 TradingAgent 5分钟模拟盘产生会话判断，再检查后续标签。")
    if "cn_futures_maturity_projection" in warned:
        notes.append(
            "运行 CNFutures sample ops，刷新当前 authority 的成熟度与 KPI 投影。"
        )
    if not notes:
        notes.append("链路已具备观察条件；继续累计样本，不自动提升到实盘。")
    return notes


def observation_alerts(checks: list[Check]) -> list[dict[str, Any]]:
    """Return dashboard-friendly alerts without mutating runtime state."""

    by_name = {check.name: check for check in checks}
    alerts: list[dict[str, Any]] = []
    freshness = by_name.get("sharedsignals_5min_freshness")
    if freshness is not None:
        report = (
            freshness.details.get("report")
            if isinstance(freshness.details.get("report"), dict)
            else {}
        )
        session = (
            report.get("session") if isinstance(report.get("session"), dict) else {}
        )
        freshness_status = str(report.get("status") or "").lower()
        if freshness.status == "fail":
            alerts.append(
                {
                    "severity": "error",
                    "code": "futures_data_check_failed",
                    "message": "期货5分钟数据检查失败，先修数据检查入口。",
                }
            )
        elif bool(session.get("in_session")) and freshness_status in {
            "stale",
            "no_data",
        }:
            alerts.append(
                {
                    "severity": "warn",
                    "code": "futures_5min_missing_in_session",
                    "message": "当前处于期货交易时段，但5分钟行情还没有新鲜数据。",
                }
            )
    review = by_name.get("cn_futures_review")
    if review is not None and review.status == "warn":
        latest_filled = int(review.details.get("latest_filled_count") or 0)
        if latest_filled <= 0:
            alerts.append(
                {
                    "severity": "info",
                    "code": "cn_futures_no_sim_samples",
                    "message": "CNFutures 模拟盘还没有有效成交样本，暂不能评估胜率。",
                }
            )
    maturity = by_name.get("cn_futures_maturity_projection")
    if maturity is not None and maturity.status == "warn":
        alerts.append(
            {
                "severity": "info",
                "code": "cn_futures_maturity_pending",
                "message": "当前 authority 的成熟度投影尚未就绪或未刷新；观察样本继续记录，晋级保持关闭。",
            }
        )
    elif maturity is not None and maturity.status == "fail":
        alerts.append(
            {
                "severity": "error",
                "code": "cn_futures_maturity_authority_failed",
                "message": "成熟度证据未通过独立 50k authority/lineage 校验，禁止用于演化判断。",
            }
        )
    return alerts


def observation_phase(checks: list[Check]) -> str:
    by_name = {check.name: check for check in checks}
    if any(check.status == "fail" for check in checks):
        return "blocked"
    freshness = by_name.get("sharedsignals_5min_freshness")
    review = by_name.get("cn_futures_review")
    maturity = by_name.get("cn_futures_maturity_projection")
    if freshness is None or freshness.status != "pass":
        return "waiting_for_5min_data"
    if review is None or review.status != "pass":
        return "waiting_for_sim_samples"
    if maturity is None or maturity.status != "pass":
        return "waiting_for_maturity_projection"
    return "ready_to_observe"


def next_validation_plan(checks: list[Check]) -> dict[str, Any]:
    by_name = {check.name: check for check in checks}
    freshness = by_name.get("sharedsignals_5min_freshness")
    report = (
        freshness.details.get("report")
        if freshness is not None and isinstance(freshness.details.get("report"), dict)
        else {}
    )
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
            "cron_required": [
                "cn_futures_5min.sh",
                "job_cn_futures_sim.sh",
                "job_cn_futures_sample_ops.sh",
                "job_cn_futures_observation_report.sh",
                "job_cn_futures_calibration_report.sh",
            ],
            "requires_current_session_decision": True,
            "requires_exact_maturity_projection": True,
            "requires_legacy_evolution_outputs": False,
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
    review_check = check_review()
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
        review_check,
        check_maturity_projection(
            expected_trade_date=str(
                review_check.details.get("current_trade_date") or ""
            )
        ),
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
    parser = argparse.ArgumentParser(
        description="Read-only CNFutures live-chain validation."
    )
    parser.add_argument("--sharedsignals-root", type=Path, default=None)
    parser.add_argument("--sqlite-db", type=Path, default=None)
    parser.add_argument("--max-age-minutes", type=int, default=10)
    parser.add_argument(
        "--python-bin",
        default=None,
        help="Python executable for SharedSignals freshness check.",
    )
    parser.add_argument("--pretty", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    from shared.governance.retirement import retired_cli

    del argv
    return retired_cli("shared.runtime_test.cn_futures_live_check")


if __name__ == "__main__":
    raise SystemExit(main())
