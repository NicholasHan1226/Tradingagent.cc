#!/usr/bin/env python3
"""Read-only CNFutures 5-minute trading observation report."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from shared.runtime_test.cn_futures_live_check import run_live_check

from .evolution import DEFAULT_REVIEW_ROOT, evolution_plan_path, style_weights_path
from .review import DEFAULT_REVIEW_PATH, STYLE_REVIEW_MARKET


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


def _check(report: dict[str, Any], name: str) -> dict[str, Any]:
    for item in report.get("checks") or []:
        if isinstance(item, dict) and item.get("name") == name:
            return item
    return {}


def _latest_review(review_path: Path) -> dict[str, Any]:
    rows = _read_jsonl(review_path)
    return rows[-1] if rows else {}


def _style_rows(review_root: Path) -> list[dict[str, Any]]:
    payload = _read_json(review_root / STYLE_REVIEW_MARKET / "style_comparison.json")
    if isinstance(payload, dict) and isinstance(payload.get("style_comparison"), list):
        return [dict(row) for row in payload["style_comparison"] if isinstance(row, dict)]
    return []


def _style_weights(review_root: Path) -> dict[str, Any]:
    payload = _read_json(style_weights_path(review_root))
    return payload if isinstance(payload, dict) else {}


def _evolution_plan(review_root: Path) -> dict[str, Any]:
    payload = _read_json(evolution_plan_path(review_root))
    return payload if isinstance(payload, dict) else {}


def build_observation_report(
    *,
    sharedsignals_root: Path | None = None,
    sqlite_db: Path | None = None,
    review_root: Path = DEFAULT_REVIEW_ROOT,
    review_path: Path = DEFAULT_REVIEW_PATH,
    max_age_minutes: int = 10,
    python_bin: str | None = None,
) -> dict[str, Any]:
    live = run_live_check(
        sharedsignals_root=sharedsignals_root,
        sqlite_db=sqlite_db,
        max_age_minutes=max_age_minutes,
        python_bin=python_bin,
    )
    freshness = _check(live, "sharedsignals_5min_freshness")
    freshness_report = freshness.get("details", {}).get("report", {}) if isinstance(freshness.get("details"), dict) else {}
    latest_review = _latest_review(review_path)
    style_rows = _style_rows(review_root)
    weights = _style_weights(review_root)
    plan = _evolution_plan(review_root)
    ranked_styles = sorted(
        style_rows,
        key=lambda row: (float(row.get("win_rate") or 0.0), float(row.get("sharpe") or 0.0), float(row.get("pnl") or 0.0)),
        reverse=True,
    )
    next_validation = live.get("next_validation", {}) if isinstance(live.get("next_validation"), dict) else {}
    primary_next_step = str(next_validation.get("expected_phase") or "")
    if not primary_next_step and live.get("observation_phase") == "ready_to_observe":
        primary_next_step = "continue_observation"
    hold_summary = latest_review.get("hold_reason_summary", {}) if isinstance(latest_review.get("hold_reason_summary"), dict) else {}
    hold_by_reason = hold_summary.get("by_reason") if isinstance(hold_summary.get("by_reason"), dict) else {}
    top_hold_reason = ""
    if hold_by_reason:
        top_hold_reason = max(hold_by_reason.items(), key=lambda item: int(item[1] or 0))[0]
    dashboard = {
        "readiness": live.get("observation_phase", "unknown"),
        "status": live.get("overall_status", "unknown"),
        "primary_next_step": primary_next_step,
        "latest_bar_time": freshness_report.get("latest_bar_time") if isinstance(freshness_report, dict) else None,
        "filled_count": int(latest_review.get("filled_count") or 0) if latest_review else 0,
        "hold_count": int(latest_review.get("hold_count") or 0) if latest_review else 0,
        "top_hold_reason": top_hold_reason,
        "top_style": ranked_styles[0].get("style_name", "") if ranked_styles else "",
        "alerts": live.get("alerts", []),
        "real_trading_enabled": False,
    }
    return {
        "market": STYLE_REVIEW_MARKET,
        "report_type": "cn_futures_5min_observation",
        "schema_version": "2026-07-05.dashboard.v1",
        "generated_at": live.get("generated_at", ""),
        "overall_status": live.get("overall_status", "unknown"),
        "observation_phase": live.get("observation_phase", "unknown"),
        "alerts": live.get("alerts", []),
        "dashboard": dashboard,
        "next_validation": next_validation,
        "data": {
            "freshness_status": freshness_report.get("status", "unknown") if isinstance(freshness_report, dict) else "unknown",
            "latest_bar_time": freshness_report.get("latest_bar_time") if isinstance(freshness_report, dict) else None,
            "symbol_count": freshness_report.get("symbol_count") if isinstance(freshness_report, dict) else None,
            "total_5min_bars": freshness_report.get("total_bars") if isinstance(freshness_report, dict) else None,
            "session": freshness_report.get("session", {}) if isinstance(freshness_report, dict) else {},
        },
        "simulation": {
            "review_exists": bool(latest_review),
            "latest_date": latest_review.get("date", ""),
            "latest_state": latest_review.get("state", ""),
            "record_count": int(latest_review.get("record_count") or 0) if latest_review else 0,
            "filled_count": int(latest_review.get("filled_count") or 0) if latest_review else 0,
            "hold_count": int(latest_review.get("hold_count") or 0) if latest_review else 0,
            "hold_reason_summary": hold_summary,
            "error_count": int(latest_review.get("error_count") or 0) if latest_review else 0,
            "error_summary": latest_review.get("error_summary", {}) if isinstance(latest_review.get("error_summary"), dict) else {},
        },
        "styles": {
            "ranked": ranked_styles,
            "weights": weights.get("styles", {}) if isinstance(weights.get("styles"), dict) else {},
            "real_trading_enabled": bool(weights.get("real_trading_enabled")) if isinstance(weights, dict) else False,
        },
        "evolution": {
            "state": plan.get("state", ""),
            "selection_objective": plan.get("selection_objective", "win_rate_first_risk_adjusted"),
            "action_count": len(plan.get("actions") or []) if isinstance(plan.get("actions"), list) else 0,
            "generated_variants": plan.get("generated_variants", []) if isinstance(plan.get("generated_variants"), list) else [],
        },
        "real_trading_enabled": False,
        "source_live_check": live,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only CNFutures 5-minute trading observation report.")
    parser.add_argument("--sharedsignals-root", type=Path, default=None)
    parser.add_argument("--sqlite-db", type=Path, default=None)
    parser.add_argument("--review-root", type=Path, default=DEFAULT_REVIEW_ROOT)
    parser.add_argument("--review-path", type=Path, default=DEFAULT_REVIEW_PATH)
    parser.add_argument("--max-age-minutes", type=int, default=10)
    parser.add_argument("--python-bin", default=None)
    parser.add_argument("--write-json", type=Path, default=None)
    parser.add_argument("--pretty", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = build_observation_report(
        sharedsignals_root=args.sharedsignals_root,
        sqlite_db=args.sqlite_db,
        review_root=args.review_root,
        review_path=args.review_path,
        max_age_minutes=args.max_age_minutes,
        python_bin=args.python_bin,
    )
    if args.write_json is not None:
        args.write_json.parent.mkdir(parents=True, exist_ok=True)
        args.write_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2 if args.pretty else None))
    return 2 if report.get("overall_status") == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
