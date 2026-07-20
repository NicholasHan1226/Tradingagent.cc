#!/usr/bin/env python3
"""CNFutures observation projection from an explicitly injected report.

The historical CLI and its implicit SharedSignals/SQLite live-check path are
retired.  Library callers may still build the read-only projection, but must
inject a fixture or TradingDatas-backed report explicitly.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Stop direct invocation before importing retired diagnostics or market code.
if __name__ == "__main__":
    from shared.governance.retirement import retired_cli

    raise SystemExit(retired_cli("CNFutures.observation_report"))

from shared.governance.retirement import require_explicit_data_port, retired_cli
from shared.runtime_test.cn_futures_live_check import (
    validate_cn_futures_maturity_projection,
)

from .review import DEFAULT_REVIEW_PATH, STYLE_REVIEW_MARKET, latest_actionable_review


DEFAULT_REVIEW_ROOT = ROOT / "shared" / "review"
MATURITY_FILENAME = "market_maturity_latest.json"
AFFORDABILITY_FILENAME = "cn_futures_affordability_latest.json"
AFFORDABILITY_AUTHORITY_WARNING = "affordability_non_authoritative_capacity_ignored"
REAL_TRADING_CONFIG_WARNING = "real_trading_enabled_forced_false"


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


def _latest_review(review_path: Path, *, trade_date: str = "") -> dict[str, Any]:
    rows = _read_jsonl(review_path)
    return latest_actionable_review(rows, trade_date=trade_date or None)


def _current_maturity(
    review_root: Path, *, expected_trade_date: str = ""
) -> tuple[dict[str, Any], dict[str, Any]]:
    path = review_root / STYLE_REVIEW_MARKET / MATURITY_FILENAME
    raw = _read_json(path)
    if not path.exists():
        return {}, {
            "status": "missing",
            "path": str(path),
            "issues": ["current_maturity_projection_missing"],
            "real_trading_enabled": False,
        }
    if path.is_symlink():
        return {}, {
            "status": "invalid",
            "path": str(path),
            "issues": ["maturity_projection_symlink_not_allowed"],
            "real_trading_enabled": False,
        }
    if not isinstance(raw, dict):
        return {}, {
            "status": "invalid",
            "path": str(path),
            "issues": ["maturity_projection_not_object"],
            "real_trading_enabled": False,
        }
    issues = validate_cn_futures_maturity_projection(
        raw, expected_trade_date=expected_trade_date
    )
    hard_issues = [issue for issue in issues if issue != "trade_date_stale"]
    if hard_issues:
        status = "invalid"
        current: dict[str, Any] = {}
    elif issues:
        status = "stale"
        current = {}
    else:
        status = "current"
        current = raw
    return current, {
        "status": status,
        "path": str(path),
        "issues": issues,
        "projection_sha256": raw.get("projection_sha256"),
        "report_type": raw.get("report_type"),
        "evidence_source": raw.get("evidence_source"),
        "generated_at": raw.get("generated_at"),
        "trade_date": raw.get("trade_date"),
        "stage": raw.get("stage"),
        "authority_scope": raw.get("authority_scope") if status == "current" else {},
        "sample_counts": raw.get("sample_counts") if status == "current" else {},
        "performance": raw.get("performance") if status == "current" else {},
        "blocking_reasons": raw.get("blocking_reasons") if status == "current" else [],
        "promotion_evidence_ready": raw.get("promotion_evidence_ready") is True
        if status == "current"
        else False,
        "promotion_policy_status": (
            raw.get("promotion_policy_status")
            if status == "current"
            else "manual_review_only_no_futures_live_date"
        ),
        "automatic_promotion_enabled": False,
        "automatic_risk_expansion_enabled": False,
        "live_transition_authorized": False,
        "real_trading_enabled": False,
    }


def _style_rows_from_maturity(maturity: dict[str, Any]) -> list[dict[str, Any]]:
    sample_kpi = maturity.get("sample_kpi_projection")
    styles = sample_kpi.get("styles") if isinstance(sample_kpi, dict) else {}
    if not isinstance(styles, dict):
        return []
    rows: list[dict[str, Any]] = []
    for style_name, raw in styles.items():
        if not isinstance(raw, dict):
            continue
        rows.append(
            {
                "style_name": str(style_name),
                **raw,
                "real_trading_enabled": False,
            }
        )
    return rows


def _normalize_trade_date(value: Any) -> str:
    raw = str(value or "").strip()
    digits = "".join(ch for ch in raw[:10] if ch.isdigit())
    return digits[:8] if len(digits) >= 8 else ""


def _affordability(
    review_path: Path,
    *,
    expected_date: str = "",
    fallback: dict[str, Any] | None = None,
) -> dict[str, Any]:
    path = review_path.with_name(AFFORDABILITY_FILENAME)
    payload = _read_json(path)
    source = "latest_file"
    if not isinstance(payload, dict) and isinstance(fallback, dict) and fallback:
        payload = fallback
        source = "latest_review"
    if not isinstance(payload, dict):
        return {
            "exists": False,
            "state": "missing",
            "path": str(path),
            "raw_distinct_products": [],
            "raw_distinct_product_count": 0,
            "affordable_distinct_products": [],
            "affordable_distinct_product_count": 0,
            "contracts": [],
            "authoritative": False,
            "counterfactual_only": True,
            "real_trading_enabled": False,
            "config_warnings": [],
        }
    config_warnings: list[str] = []
    contracts: list[dict[str, Any]] = []
    for row in payload.get("contracts", []):
        if not isinstance(row, dict):
            continue
        normalized = dict(row)
        if normalized.get("real_trading_enabled") is True:
            config_warnings.append(REAL_TRADING_CONFIG_WARNING)
        normalized["real_trading_enabled"] = False
        contracts.append(normalized)
    raw_products = sorted(
        {
            str(product).strip().lower()
            for product in payload.get("raw_distinct_products", [])
            if str(product).strip()
        }
    )
    payload_date = _normalize_trade_date(payload.get("date") or payload.get("as_of"))
    expected = _normalize_trade_date(expected_date)
    if not expected or not payload_date:
        state = "unknown"
    elif payload_date != expected:
        state = "stale"
    else:
        state = "current"
    account_state = (
        payload.get("account_state")
        if isinstance(payload.get("account_state"), dict)
        else {}
    )
    account_authoritative = account_state.get("authoritative") is True and not bool(
        account_state.get("counterfactual_only")
    )
    affordability_authoritative = (
        "authoritative" not in payload or payload.get("authoritative") is True
    )
    capacity_authoritative = (
        account_authoritative
        and affordability_authoritative
        and not bool(payload.get("counterfactual_only"))
    )
    candidate_affordable_products = sorted(
        {
            str(row.get("product") or "").strip().lower()
            for row in contracts
            if row.get("eligible") is True
            and not bool(row.get("counterfactual_only"))
            and str(row.get("execution_class") or "") != "counterfactual_only"
            and not bool(row.get("reduce_only"))
            and str(row.get("product") or "").strip()
        }
    )
    affordable_products = (
        candidate_affordable_products
        if state == "current" and capacity_authoritative
        else []
    )
    claimed_products = payload.get("affordable_distinct_products")
    claimed_count = payload.get("affordable_distinct_product_count")
    if not capacity_authoritative and (
        bool(candidate_affordable_products)
        or bool(claimed_products)
        or bool(claimed_count)
    ):
        config_warnings.append(AFFORDABILITY_AUTHORITY_WARNING)
    if payload.get("real_trading_enabled") is True:
        config_warnings.append(REAL_TRADING_CONFIG_WARNING)
    return {
        **payload,
        "exists": True,
        "state": state,
        "path": str(path),
        "source": source,
        "raw_distinct_products": raw_products,
        "raw_distinct_product_count": len(raw_products),
        "affordable_distinct_products": affordable_products,
        "affordable_distinct_product_count": len(affordable_products),
        "contracts": contracts,
        "authoritative": capacity_authoritative,
        "counterfactual_only": not capacity_authoritative,
        "real_trading_enabled": False,
        "config_warnings": sorted(set(config_warnings)),
    }


def build_observation_report(
    *,
    live_report: Mapping[str, Any] | None = None,
    review_root: Path = DEFAULT_REVIEW_ROOT,
    review_path: Path = DEFAULT_REVIEW_PATH,
) -> dict[str, Any]:
    """Project an observation report from an explicit upstream evidence port."""

    live = dict(
        require_explicit_data_port(
            live_report,
            context="CNFutures.build_observation_report",
        )
    )
    freshness = _check(live, "sharedsignals_5min_freshness")
    freshness_report = (
        freshness.get("details", {}).get("report", {})
        if isinstance(freshness.get("details"), dict)
        else {}
    )
    review_check = _check(live, "cn_futures_review")
    review_details = (
        review_check.get("details", {})
        if isinstance(review_check.get("details"), dict)
        else {}
    )
    expected_trade_date = str(
        review_details.get("current_trade_date")
        or (
            freshness_report.get("current_trade_date")
            if isinstance(freshness_report, dict)
            else ""
        )
        or (
            freshness_report.get("latest_bar_time")
            if isinstance(freshness_report, dict)
            else ""
        )
        or ""
    )
    latest_review = _latest_review(review_path, trade_date=expected_trade_date)
    maturity, maturity_view = _current_maturity(
        review_root, expected_trade_date=expected_trade_date
    )
    fallback_affordability = (
        latest_review.get("affordability")
        if isinstance(latest_review.get("affordability"), dict)
        else None
    )
    affordability = _affordability(
        review_path,
        expected_date=expected_trade_date,
        fallback=fallback_affordability,
    )
    config_warnings = list(affordability.get("config_warnings") or [])
    config_warnings.extend(
        f"maturity_projection_{issue}" for issue in maturity_view.get("issues", [])
    )
    ranked_styles = sorted(
        _style_rows_from_maturity(maturity),
        key=lambda row: (
            int(row.get("completed_round_trip_count") or 0),
            int(row.get("prediction_count") or 0),
            float(row.get("post_cost_pnl_cny") or 0.0),
        ),
        reverse=True,
    )
    config_warnings = sorted(set(config_warnings))
    next_validation = (
        live.get("next_validation", {})
        if isinstance(live.get("next_validation"), dict)
        else {}
    )
    primary_next_step = str(next_validation.get("expected_phase") or "")
    if not primary_next_step and live.get("observation_phase") == "ready_to_observe":
        primary_next_step = "continue_observation"
    hold_summary = (
        latest_review.get("hold_reason_summary", {})
        if isinstance(latest_review.get("hold_reason_summary"), dict)
        else {}
    )
    hold_by_reason = (
        hold_summary.get("by_reason")
        if isinstance(hold_summary.get("by_reason"), dict)
        else {}
    )
    top_hold_reason = ""
    if hold_by_reason:
        top_hold_reason = max(
            hold_by_reason.items(), key=lambda item: int(item[1] or 0)
        )[0]
    forward_summary = (
        latest_review.get("forward_label_summary", {})
        if isinstance(latest_review.get("forward_label_summary"), dict)
        else {}
    )
    threshold_candidates = (
        latest_review.get("dynamic_threshold_candidates", [])
        if isinstance(latest_review.get("dynamic_threshold_candidates"), list)
        else []
    )
    maturity_counts = (
        maturity.get("sample_counts")
        if isinstance(maturity.get("sample_counts"), dict)
        else {}
    )
    forward_labeled_count = int(maturity_counts.get("forward_label_count") or 0)
    forward_pending_count = int(maturity_counts.get("pending_forward_label_count") or 0)
    dashboard = {
        "readiness": live.get("observation_phase", "unknown"),
        "status": live.get("overall_status", "unknown"),
        "primary_next_step": primary_next_step,
        "latest_bar_time": freshness_report.get("latest_bar_time")
        if isinstance(freshness_report, dict)
        else None,
        "filled_count": int(latest_review.get("filled_count") or 0)
        if latest_review
        else 0,
        "hold_count": int(latest_review.get("hold_count") or 0) if latest_review else 0,
        "top_hold_reason": top_hold_reason,
        "forward_labeled_count": forward_labeled_count,
        "forward_pending_count": forward_pending_count,
        "dynamic_threshold_candidate_count": len(threshold_candidates),
        "raw_distinct_product_count": int(
            affordability.get("raw_distinct_product_count") or 0
        ),
        "affordable_distinct_product_count": int(
            affordability.get("affordable_distinct_product_count") or 0
        ),
        "top_style": ranked_styles[0].get("style_name", "") if ranked_styles else "",
        "alerts": live.get("alerts", []),
        "real_trading_enabled": False,
    }
    live_status = str(live.get("overall_status") or "unknown")
    if live_status == "fail":
        overall_status = "fail"
    elif live_status == "warn" or maturity_view.get("status") != "current":
        overall_status = "warn"
    else:
        overall_status = "pass"
    return {
        "market": STYLE_REVIEW_MARKET,
        "report_type": "cn_futures_5min_observation",
        "schema_version": "2026-07-05.dashboard.v1",
        "generated_at": live.get("generated_at", ""),
        "overall_status": overall_status,
        "observation_phase": live.get("observation_phase", "unknown"),
        "alerts": live.get("alerts", []),
        "config_warnings": config_warnings,
        "dashboard": dashboard,
        "next_validation": next_validation,
        "maturity": maturity_view,
        "data": {
            "freshness_status": freshness_report.get("status", "unknown")
            if isinstance(freshness_report, dict)
            else "unknown",
            "latest_bar_time": freshness_report.get("latest_bar_time")
            if isinstance(freshness_report, dict)
            else None,
            "symbol_count": freshness_report.get("symbol_count")
            if isinstance(freshness_report, dict)
            else None,
            "total_5min_bars": freshness_report.get("total_bars")
            if isinstance(freshness_report, dict)
            else None,
            "session": freshness_report.get("session", {})
            if isinstance(freshness_report, dict)
            else {},
        },
        "simulation": {
            "review_exists": bool(latest_review),
            "latest_date": latest_review.get("date", ""),
            "latest_state": latest_review.get("state", ""),
            "record_count": int(latest_review.get("record_count") or 0)
            if latest_review
            else 0,
            "filled_count": int(latest_review.get("filled_count") or 0)
            if latest_review
            else 0,
            "hold_count": int(latest_review.get("hold_count") or 0)
            if latest_review
            else 0,
            "hold_reason_summary": hold_summary,
            "forward_label_summary": forward_summary,
            "dynamic_threshold_candidates": threshold_candidates,
            "error_count": int(latest_review.get("error_count") or 0)
            if latest_review
            else 0,
            "error_summary": latest_review.get("error_summary", {})
            if isinstance(latest_review.get("error_summary"), dict)
            else {},
            "affordability": affordability,
        },
        "styles": {
            "ranked": ranked_styles,
            "weights": {},
            "source": "market_maturity_sample_kpi",
            "runtime_weight_mutation_enabled": False,
            "real_trading_enabled": False,
        },
        "evolution": {
            "state": "manual_review_only",
            "evidence_source": maturity_view.get("evidence_source"),
            "maturity_stage": maturity_view.get("stage"),
            "promotion_evidence_ready": maturity_view.get("promotion_evidence_ready")
            is True,
            "promotion_policy_status": "manual_review_only_no_futures_live_date",
            "blocking_reasons": maturity_view.get("blocking_reasons", []),
            "action_count": 0,
            "generated_variants": [],
            "automatic_promotion_enabled": False,
            "automatic_risk_expansion_enabled": False,
            "runtime_style_mutation_enabled": False,
            "live_transition_authorized": False,
            "real_trading_enabled": False,
        },
        "real_trading_enabled": False,
        "source_live_check": live,
    }


def main(argv: list[str] | None = None) -> int:
    del argv
    return retired_cli("CNFutures.observation_report")


if __name__ == "__main__":
    raise SystemExit(main())
