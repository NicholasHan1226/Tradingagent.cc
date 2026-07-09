#!/usr/bin/env python3
"""A-share daily strategy sample target monitor.

The monitor checks whether the simulated-only A-share loop has collected the
daily strategy-valid sample target. It writes review evidence and may refresh
the evolution decision, but it never writes orders or enables real trading.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from Ashare.evolution_controller import write_evolution_decision
from shared.runtime_test.ashare_no_trade_summary import NO_TRADE_LOG, summarize_no_trade_log


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REVIEW_DIR = ROOT / "shared" / "review" / "ashare"
LATEST_PATH = DEFAULT_REVIEW_DIR / "sample_target_monitor_latest.json"
LOG_PATH = DEFAULT_REVIEW_DIR / "sample_target_monitor_log.jsonl"
CN_TZ = timezone(timedelta(hours=8))


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _compact_date(value: Any) -> str:
    raw = str(value or "").strip()
    if len(raw) >= 10 and raw[4] == "-" and raw[7] == "-":
        return raw[:10].replace("-", "")
    return raw[:8] if raw else ""


def _now_cn(now: datetime | None = None) -> datetime:
    current = now or datetime.now(CN_TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=CN_TZ)
    return current.astimezone(CN_TZ)


def _checkpoint(now: datetime) -> dict[str, Any]:
    minutes = now.hour * 60 + now.minute
    if minutes < 9 * 60 + 45:
        name = "before_first"
        next_check = "09:45"
    elif minutes < 11 * 60 + 45:
        name = "morning"
        next_check = "11:45"
    elif minutes < 14 * 60 + 30:
        name = "midday"
        next_check = "14:30"
    elif minutes < 15 * 60 + 30:
        name = "afternoon"
        next_check = "15:30"
    else:
        name = "final"
        next_check = None
    return {
        "name": name,
        "time": now.strftime("%H:%M"),
        "next_check": next_check,
        "schedule": ["09:45", "11:45", "14:30", "15:30"],
    }


def _policy_target(decision: dict[str, Any], default: int) -> int:
    policy = decision.get("policy") if isinstance(decision.get("policy"), dict) else {}
    target = _safe_int(policy.get("daily_strategy_sample_target"), default)
    return max(1, target)


def _no_trade_blockers(summary: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    if _safe_int(summary.get("row_count")) <= 0:
        return blockers
    latest = summary.get("latest_no_trade_log") if isinstance(summary.get("latest_no_trade_log"), dict) else {}
    category = str(latest.get("category") or "").strip()
    if category and category != "unknown":
        blockers.append(category)
    decision = str(latest.get("capital_plan_decision") or "").strip()
    if "defensive" in decision and "capital_plan_defensive" not in blockers:
        blockers.append("capital_plan_defensive")
    ranges = summary.get("count_ranges") if isinstance(summary.get("count_ranges"), dict) else {}
    candidates = ranges.get("candidates") if isinstance(ranges.get("candidates"), dict) else {}
    orders = ranges.get("orders") if isinstance(ranges.get("orders"), dict) else {}
    risk_rejections = ranges.get("risk_rejections") if isinstance(ranges.get("risk_rejections"), dict) else {}
    if candidates.get("latest") == 0:
        blockers.append("no_candidates")
    if orders.get("latest") == 0:
        blockers.append("no_orders")
    if _safe_int(risk_rejections.get("latest")) > 0:
        blockers.append("risk_rejections_present")
    if summary.get("evidence_status") == "incomplete":
        blockers.append("no_trade_evidence_incomplete")
    return list(dict.fromkeys(blockers))


def build_sample_target_monitor(
    *,
    review_dir: Path | str | None = None,
    no_trade_log_path: Path | str | None = None,
    now: datetime | None = None,
    daily_strategy_sample_target: int = 1,
    min_strategy_samples: int = 5,
) -> dict[str, Any]:
    """Build a read-only monitor report for today's A-share sample target."""

    current = _now_cn(now)
    trade_date = current.strftime("%Y%m%d")
    review_path = Path(review_dir) if review_dir is not None else DEFAULT_REVIEW_DIR
    portfolio = _read_json(review_path / "portfolio_evolution_latest.json")
    decision = _read_json(review_path / "evolution_decision_latest.json")
    target = _policy_target(decision, daily_strategy_sample_target)
    portfolio_trade_date = _compact_date(portfolio.get("trade_date"))
    today_count = _safe_int(portfolio.get("today_strategy_sample_count"))
    strategy_count = _safe_int(portfolio.get("strategy_sample_count"))
    reasons: list[str] = []
    blockers: list[str] = []
    if not portfolio:
        reasons.append("portfolio_evolution_missing")
        blockers.append("portfolio_evolution_missing")
    elif portfolio_trade_date != trade_date:
        today_count = 0
        reasons.append("portfolio_evolution_trade_date_stale")
        blockers.append("portfolio_evolution_stale")

    if today_count < target:
        reasons.append("daily_strategy_sample_target_not_met")

    no_trade_path = Path(no_trade_log_path) if no_trade_log_path is not None else NO_TRADE_LOG
    no_trade_summary = summarize_no_trade_log(no_trade_path, trade_date)
    blockers.extend(_no_trade_blockers(no_trade_summary))
    blockers = list(dict.fromkeys(blockers))

    checkpoint = _checkpoint(current)
    target_met = today_count >= target
    if target_met:
        status = "pass"
        state = "target_met"
        action = "observe"
    elif checkpoint["name"] == "final":
        status = "fail"
        state = "daily_target_missed"
        action = "force_sample_collection"
    else:
        status = "warn"
        state = "sample_debt"
        action = "force_sample_collection"

    return {
        "report_type": "ashare_sample_target_monitor",
        "generated_at": current.isoformat(timespec="seconds"),
        "timezone": "Asia/Shanghai",
        "market": "ashare",
        "trade_date": trade_date,
        "overall_status": status,
        "state": state,
        "recommended_action": action,
        "reasons": list(dict.fromkeys(reasons)),
        "blockers": blockers,
        "checkpoint": checkpoint,
        "daily_target": {
            "daily_sample_hard_gate": True,
            "target": target,
            "today_strategy_sample_count": today_count,
            "strategy_sample_count": strategy_count,
            "min_strategy_samples": max(1, int(min_strategy_samples)),
            "target_met": target_met,
        },
        "portfolio_evolution": {
            "trade_date": portfolio_trade_date,
            "state": portfolio.get("state"),
            "actions": portfolio.get("actions") if isinstance(portfolio.get("actions"), list) else [],
        },
        "evolution_decision": {
            "trade_date": _compact_date(decision.get("trade_date")),
            "state": decision.get("state"),
            "recommended_action": decision.get("recommended_action"),
            "reasons": decision.get("reasons") if isinstance(decision.get("reasons"), list) else [],
        },
        "no_trade_summary": no_trade_summary,
        "guardrails": [
            "simulated_only",
            "candidate_layer_required",
            "positive_fill_price_required",
            "risk_check_required",
            "cash_and_lot_size_required",
            "t_plus_1_required",
            "market_session_required",
        ],
        "read_only": True,
        "writes_orders": False,
        "real_trading_enabled": False,
    }


def write_sample_target_monitor(
    *,
    review_dir: Path | str | None = None,
    no_trade_log_path: Path | str | None = None,
    now: datetime | None = None,
    daily_strategy_sample_target: int = 1,
    min_strategy_samples: int = 5,
    refresh_decision: bool = True,
) -> dict[str, Any]:
    """Write the monitor report and optionally refresh evolution decision."""

    review_path = Path(review_dir) if review_dir is not None else DEFAULT_REVIEW_DIR
    review_path.mkdir(parents=True, exist_ok=True)
    report = build_sample_target_monitor(
        review_dir=review_path,
        no_trade_log_path=no_trade_log_path,
        now=now,
        daily_strategy_sample_target=daily_strategy_sample_target,
        min_strategy_samples=min_strategy_samples,
    )
    if refresh_decision and report["recommended_action"] == "force_sample_collection":
        portfolio = _read_json(review_path / "portfolio_evolution_latest.json")
        refreshed = write_evolution_decision(
            portfolio,
            review_dir=review_path,
            target_trade_date=report["trade_date"],
            daily_strategy_sample_target=report["daily_target"]["target"],
            min_strategy_samples=report["daily_target"]["min_strategy_samples"],
        )
        report["evolution_decision_refresh"] = {
            "status": "written",
            "state": refreshed.get("state"),
            "recommended_action": refreshed.get("recommended_action"),
            "reasons": refreshed.get("reasons") if isinstance(refreshed.get("reasons"), list) else [],
        }
    else:
        report["evolution_decision_refresh"] = {"status": "skipped", "reason": "target_met_or_disabled"}

    latest = review_path / LATEST_PATH.name
    log = review_path / LOG_PATH.name
    latest.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(report, ensure_ascii=False, sort_keys=True) + "\n")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review-dir", type=Path, default=DEFAULT_REVIEW_DIR)
    parser.add_argument("--no-trade-log-path", type=Path, default=NO_TRADE_LOG)
    parser.add_argument("--daily-strategy-sample-target", type=int, default=1)
    parser.add_argument("--min-strategy-samples", type=int, default=5)
    parser.add_argument("--no-refresh-decision", action="store_true")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)
    report = write_sample_target_monitor(
        review_dir=args.review_dir,
        no_trade_log_path=args.no_trade_log_path,
        daily_strategy_sample_target=args.daily_strategy_sample_target,
        min_strategy_samples=args.min_strategy_samples,
        refresh_decision=not args.no_refresh_decision,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
