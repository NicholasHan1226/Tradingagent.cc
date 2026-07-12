#!/usr/bin/env python3
"""Read-only health check for simulated evolution evidence.

CNFutures uses only its hash-verified review-journal/KPI maturity projection;
legacy style-evolution files remain applicable only to the other listed markets.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shared.runtime_test.cn_futures_live_check import (
    validate_cn_futures_maturity_projection,
)
from shared.review.pnl_summary import sim_ledger_pnl_summary


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REVIEW_ROOT = ROOT / "shared" / "review"
DEFAULT_MARKETS = ("ashare", "crypto", "pm", "us", "cn_futures")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError):
        return rows
    for line in lines:
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed == parsed else default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _latest(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return rows[-1] if rows else {}


def _weights_map(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    weights = payload.get("weights")
    if not isinstance(weights, dict):
        weights = payload.get("styles")
    if not isinstance(weights, dict):
        return {}
    return {
        str(key): value for key, value in weights.items() if isinstance(value, dict)
    }


def _sum_rank_metric(rows: list[dict[str, Any]], key: str) -> float:
    return round(
        sum(_safe_float(row.get(key)) for row in rows if isinstance(row, dict)), 6
    )


def _sum_rank_trades(rows: list[dict[str, Any]]) -> int:
    return sum(_safe_int(row.get("trades")) for row in rows if isinstance(row, dict))


def _performance_summary(review_dir: Path) -> dict[str, Any]:
    rows = _read_jsonl(review_dir / "style_performance.jsonl")
    return {
        "row_count": len(rows),
        "trade_sum": sum(_safe_int(row.get("trades")) for row in rows),
        "pnl_sum": round(sum(_safe_float(row.get("pnl")) for row in rows), 6),
    }


def _read_current_projection(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        return {}
    return _read_json(path)


def _authority_scope(payload: dict[str, Any]) -> dict[str, Any]:
    value = payload.get("authority_scope")
    if not isinstance(value, dict):
        return {}
    return {
        "capital_authority_id": value.get("capital_authority_id"),
        "authority_generation": value.get("authority_generation"),
        "execution_lineage_id": value.get("execution_lineage_id"),
    }


def _valid_ashare_projection(payload: dict[str, Any], *, report_type: str) -> bool:
    authority = _authority_scope(payload)
    return bool(
        payload
        and payload.get("report_type") == report_type
        and payload.get("evidence_source") == "sample_journal_kpi"
        and payload.get("real_trading_enabled") is not True
        and payload.get("live_execution_enabled") is not True
        and authority.get("capital_authority_id") == "ashare-capital-v1"
        and authority.get("authority_generation") == 1
        and str(authority.get("execution_lineage_id") or "").strip()
    )


def _ashare_kpi_pnl(styles: dict[str, Any]) -> float:
    total = 0.0
    for raw in styles.values():
        if not isinstance(raw, dict):
            continue
        value = raw.get("post_cost_pnl_cny")
        if value is not None:
            total += _safe_float(value)
            continue
        by_intent = raw.get("performance_by_sample_intent")
        if not isinstance(by_intent, dict):
            continue
        total += sum(
            _safe_float(metrics.get("post_cost_pnl_cny"))
            for metrics in by_intent.values()
            if isinstance(metrics, dict)
        )
    return round(total, 6)


def _ashare_current_projection(review_dir: Path) -> dict[str, Any]:
    kpi = _read_current_projection(review_dir / "sample_kpi_latest.json")
    decision = _read_current_projection(review_dir / "evolution_decision_latest.json")
    maturity = _read_current_projection(review_dir / "market_maturity_latest.json")
    issues: list[str] = []

    kpi_valid = _valid_ashare_projection(kpi, report_type="sample_journal_kpi")
    decision_valid = _valid_ashare_projection(
        decision, report_type="ashare_evolution_decision_v2"
    )
    maturity_valid = _valid_ashare_projection(
        maturity, report_type="ashare_market_maturity_v1"
    )
    for valid, missing, invalid in (
        (kpi_valid, "missing_current_sample_kpi", "invalid_current_sample_kpi"),
        (
            decision_valid,
            "missing_current_evolution_decision",
            "invalid_current_evolution_decision",
        ),
        (
            maturity_valid,
            "missing_current_market_maturity",
            "invalid_current_market_maturity",
        ),
    ):
        if valid:
            continue
        payload = {
            "missing_current_sample_kpi": kpi,
            "missing_current_evolution_decision": decision,
            "missing_current_market_maturity": maturity,
        }[missing]
        issues.append(invalid if payload else missing)

    valid_payloads = [
        payload
        for payload, valid in (
            (kpi, kpi_valid),
            (decision, decision_valid),
            (maturity, maturity_valid),
        )
        if valid
    ]
    scopes = [_authority_scope(payload) for payload in valid_payloads]
    if scopes and any(scope != scopes[0] for scope in scopes[1:]):
        issues.append("current_projection_authority_mismatch")
    if any(
        payload.get("automatic_promotion_enabled") is True
        or payload.get("automatic_risk_expansion_enabled") is True
        or payload.get("real_trading_enabled") is True
        for payload in (kpi, decision, maturity)
    ):
        issues.append("unsafe_current_projection_policy")

    current_kpi = kpi if kpi_valid else {}
    current_decision = decision if decision_valid else {}
    current_maturity = maturity if maturity_valid else {}
    styles = (
        current_kpi.get("styles") if isinstance(current_kpi.get("styles"), dict) else {}
    )
    layers = (
        current_kpi.get("sample_layer_totals")
        if isinstance(current_kpi.get("sample_layer_totals"), dict)
        else {}
    )
    prediction_count = sum(
        _safe_int(style.get("prediction_count"))
        for style in styles.values()
        if isinstance(style, dict)
    )
    completed_round_trips = _safe_int(layers.get("completed_round_trip"))
    if completed_round_trips <= 0:
        completed_round_trips = sum(
            _safe_int(style.get("completed_round_trip_count"))
            for style in styles.values()
            if isinstance(style, dict)
        )

    return {
        "issues": list(dict.fromkeys(issues)),
        "generated_at": str(
            current_decision.get("generated_at")
            or current_maturity.get("generated_at")
            or current_kpi.get("generated_at")
            or ""
        ),
        "state": str(current_decision.get("state") or "missing"),
        "recommended_action": str(
            current_decision.get("recommended_action") or "observe_and_label_candidates"
        ),
        "prediction_count": prediction_count,
        "completed_round_trip_count": completed_round_trips,
        "post_cost_pnl_cny": _ashare_kpi_pnl(styles),
        "style_count": len(styles),
        "maturity_stage": str(current_maturity.get("stage") or "missing"),
        "authority_scope": _authority_scope(current_kpi),
        "promotion_evidence_ready": bool(
            current_kpi.get("scientific_evidence", {}).get("promotion_evidence_ready")
        )
        if isinstance(current_kpi.get("scientific_evidence"), dict)
        else False,
        "automatic_promotion_enabled": False,
        "automatic_risk_expansion_enabled": False,
        "real_trading_enabled": False,
    }


def _cn_futures_current_projection(review_dir: Path) -> dict[str, Any]:
    path = review_dir / "market_maturity_latest.json"
    payload = _read_current_projection(path)
    if not payload:
        return {
            "issues": ["missing_current_market_maturity"],
            "state": "missing",
            "evidence_source": "cn_futures_review_journal+sample_kpi",
            "authority_scope": {},
            "sample_counts": {},
            "performance": {},
            "style_count": 0,
            "automatic_promotion_enabled": False,
            "automatic_risk_expansion_enabled": False,
            "real_trading_enabled": False,
        }
    issues = validate_cn_futures_maturity_projection(payload)
    if issues:
        return {
            "issues": [f"invalid_current_market_maturity:{issue}" for issue in issues],
            "state": "missing",
            "evidence_source": "cn_futures_review_journal+sample_kpi",
            "authority_scope": {},
            "sample_counts": {},
            "performance": {},
            "style_count": 0,
            "automatic_promotion_enabled": False,
            "automatic_risk_expansion_enabled": False,
            "real_trading_enabled": False,
        }
    sample_counts = (
        payload.get("sample_counts")
        if isinstance(payload.get("sample_counts"), dict)
        else {}
    )
    performance = (
        payload.get("performance")
        if isinstance(payload.get("performance"), dict)
        else {}
    )
    sample_kpi = (
        payload.get("sample_kpi_projection")
        if isinstance(payload.get("sample_kpi_projection"), dict)
        else {}
    )
    styles = (
        sample_kpi.get("styles") if isinstance(sample_kpi.get("styles"), dict) else {}
    )
    return {
        "issues": [],
        "generated_at": str(payload.get("generated_at") or ""),
        "projection_sha256": str(payload.get("projection_sha256") or ""),
        "state": str(payload.get("stage") or "missing"),
        "evidence_source": "cn_futures_review_journal+sample_kpi",
        "authority_scope": _authority_scope(payload),
        "sample_counts": sample_counts,
        "performance": performance,
        "style_count": len(styles),
        "promotion_evidence_ready": payload.get("promotion_evidence_ready") is True,
        "promotion_policy_status": "manual_review_only_no_futures_live_date",
        "blocking_reasons": list(payload.get("blocking_reasons") or []),
        "automatic_promotion_enabled": False,
        "automatic_risk_expansion_enabled": False,
        "live_transition_authorized": False,
        "real_trading_enabled": False,
    }


def evaluate_self_evolution_health(
    *,
    review_root: Path | str | None = None,
    markets: tuple[str, ...] | list[str] | None = None,
    pnl_summary: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    root = Path(review_root) if review_root is not None else DEFAULT_REVIEW_ROOT
    target_markets = tuple(markets or DEFAULT_MARKETS)
    pnl = (
        pnl_summary
        if pnl_summary is not None
        else sim_ledger_pnl_summary(markets=target_markets)
    )
    market_reports: list[dict[str, Any]] = []
    all_issues: list[dict[str, Any]] = []

    for market in target_markets:
        review_dir = root / market
        if market == "ashare":
            projection = _ashare_current_projection(review_dir)
            market_pnl = pnl.get(market, {}) if isinstance(pnl, dict) else {}
            sample_quality = (
                market_pnl.get("sample_quality")
                if isinstance(market_pnl.get("sample_quality"), dict)
                else {}
            )
            strategy_sample_count = _safe_int(
                sample_quality.get("strategy_sample_valid_count")
            )
            ranking_trade_sum = _safe_int(projection.get("completed_round_trip_count"))
            issues = list(projection.get("issues") or [])
            if strategy_sample_count > 0 and ranking_trade_sum <= 0:
                issues.append("strategy_samples_not_seen_by_current_projection")
            for issue in issues:
                all_issues.append({"market": market, "issue": issue})
            market_reports.append(
                {
                    "market": market,
                    "status": "warn" if issues else "pass",
                    "issues": issues,
                    "latest_evolution_at": projection.get("generated_at", ""),
                    "latest_evolution_state": projection.get("state", "missing"),
                    "evolution_source": "sample_journal_kpi",
                    "action_count": 1 if projection.get("recommended_action") else 0,
                    "non_observe_action_count": 0,
                    "generated_variant_count": 0,
                    "ranking_trade_sum": ranking_trade_sum,
                    "ranking_pnl_sum": _safe_float(projection.get("post_cost_pnl_cny")),
                    "style_weight_count": 0,
                    "current_projection": projection,
                    "performance": _performance_summary(review_dir),
                    "pnl": {
                        "total_pnl": market_pnl.get("total_pnl", 0.0),
                        "realized_pnl": market_pnl.get("realized_pnl", 0.0),
                        "unrealized_pnl": market_pnl.get("unrealized_pnl", 0.0),
                        "open_position_count": market_pnl.get("open_position_count", 0),
                        "strategy_sample_valid_count": strategy_sample_count,
                    },
                    "weight_mismatches": [],
                    "positive_evolution_proven": False,
                }
            )
            continue

        if market == "cn_futures":
            projection = _cn_futures_current_projection(review_dir)
            market_pnl = pnl.get(market, {}) if isinstance(pnl, dict) else {}
            sample_quality = (
                market_pnl.get("sample_quality")
                if isinstance(market_pnl.get("sample_quality"), dict)
                else {}
            )
            strategy_sample_count = _safe_int(
                sample_quality.get("strategy_sample_valid_count")
            )
            sample_counts = (
                projection.get("sample_counts")
                if isinstance(projection.get("sample_counts"), dict)
                else {}
            )
            performance = (
                projection.get("performance")
                if isinstance(projection.get("performance"), dict)
                else {}
            )
            valid_sample_count = _safe_int(sample_counts.get("valid_sample_count"))
            completed_round_trips = _safe_int(
                sample_counts.get("completed_round_trip_count")
                or performance.get("completed_round_trip_count")
            )
            issues = list(projection.get("issues") or [])
            if strategy_sample_count > 0 and valid_sample_count <= 0:
                issues.append("strategy_samples_not_seen_by_current_projection")
            issues = list(dict.fromkeys(issues))
            for issue in issues:
                all_issues.append({"market": market, "issue": issue})
            market_reports.append(
                {
                    "market": market,
                    "status": "warn" if issues else "pass",
                    "issues": issues,
                    "latest_evolution_at": projection.get("generated_at", ""),
                    "latest_evolution_state": projection.get("state", "missing"),
                    "evolution_source": "cn_futures_review_journal+sample_kpi",
                    "action_count": 0,
                    "non_observe_action_count": 0,
                    "generated_variant_count": 0,
                    "ranking_trade_sum": completed_round_trips,
                    "ranking_pnl_sum": _safe_float(
                        performance.get("post_cost_pnl_cny")
                    ),
                    "style_weight_count": 0,
                    "current_projection": projection,
                    "performance": performance,
                    "pnl": {
                        "total_pnl": market_pnl.get("total_pnl", 0.0),
                        "realized_pnl": market_pnl.get("realized_pnl", 0.0),
                        "unrealized_pnl": market_pnl.get("unrealized_pnl", 0.0),
                        "open_position_count": market_pnl.get("open_position_count", 0),
                        "strategy_sample_valid_count": strategy_sample_count,
                    },
                    "weight_mismatches": [],
                    "positive_evolution_proven": False,
                    "automatic_promotion_enabled": False,
                    "automatic_risk_expansion_enabled": False,
                    "live_transition_authorized": False,
                    "real_trading_enabled": False,
                }
            )
            continue

        latest_evolution = _latest(_read_jsonl(review_dir / "evolution_log.jsonl"))
        evolution_for_samples = latest_evolution
        rankings = (
            latest_evolution.get("rankings")
            if isinstance(latest_evolution.get("rankings"), list)
            else []
        )
        sample_rankings = (
            evolution_for_samples.get("rankings")
            if isinstance(evolution_for_samples.get("rankings"), list)
            else rankings
        )
        actions = (
            latest_evolution.get("actions")
            if isinstance(latest_evolution.get("actions"), list)
            else []
        )
        sample_actions = (
            evolution_for_samples.get("actions")
            if isinstance(evolution_for_samples.get("actions"), list)
            else actions
        )
        weights = _weights_map(latest_evolution) or _weights_map(
            _read_json(review_dir / "style_weights.json")
        )
        market_pnl = pnl.get(market, {}) if isinstance(pnl, dict) else {}
        sample_quality = (
            market_pnl.get("sample_quality")
            if isinstance(market_pnl.get("sample_quality"), dict)
            else {}
        )
        strategy_sample_count = _safe_int(
            sample_quality.get("strategy_sample_valid_count")
        )
        ranking_trade_sum = _sum_rank_trades(sample_rankings)
        issues: list[str] = []

        if strategy_sample_count > 0 and ranking_trade_sum <= 0:
            issues.append("strategy_samples_not_seen_by_evolution")
        mismatches: list[dict[str, Any]] = []
        if market in {"crypto", "pm"} and not latest_evolution:
            issues.append("missing_evolution_log")

        for issue in issues:
            all_issues.append({"market": market, "issue": issue})

        market_reports.append(
            {
                "market": market,
                "status": "warn" if issues else "pass",
                "issues": issues,
                "latest_evolution_at": evolution_for_samples.get("generated_at", ""),
                "latest_evolution_state": evolution_for_samples.get("state", "missing")
                if evolution_for_samples
                else "missing",
                "evolution_source": "style_evolution",
                "action_count": len(sample_actions),
                "non_observe_action_count": sum(
                    1
                    for item in sample_actions
                    if isinstance(item, dict)
                    and item.get("action") not in {"observe", None, ""}
                ),
                "generated_variant_count": len(
                    evolution_for_samples.get("generated_variants") or []
                ),
                "ranking_trade_sum": ranking_trade_sum,
                "ranking_pnl_sum": _sum_rank_metric(sample_rankings, "pnl"),
                "style_weight_count": len(weights),
                "performance": _performance_summary(review_dir),
                "pnl": {
                    "total_pnl": market_pnl.get("total_pnl", 0.0),
                    "realized_pnl": market_pnl.get("realized_pnl", 0.0),
                    "unrealized_pnl": market_pnl.get("unrealized_pnl", 0.0),
                    "open_position_count": market_pnl.get("open_position_count", 0),
                    "strategy_sample_valid_count": strategy_sample_count,
                },
                "weight_mismatches": mismatches,
                "positive_evolution_proven": bool(
                    ranking_trade_sum > 0
                    and _sum_rank_metric(sample_rankings, "pnl") > 0
                    and any(
                        isinstance(item, dict)
                        and item.get("action")
                        in {"promote", "promoted", "variant_generated", "expand_risk"}
                        for item in sample_actions
                    )
                ),
            }
        )

    return {
        "report_type": "self_evolution_health",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "overall_status": "warn" if all_issues else "pass",
        "issue_count": len(all_issues),
        "issues": all_issues,
        "markets": market_reports,
        "read_only": True,
        "real_trading_enabled": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review-root", type=Path, default=DEFAULT_REVIEW_ROOT)
    parser.add_argument(
        "--market",
        action="append",
        choices=DEFAULT_MARKETS,
        help="May be repeated; default all simulated markets.",
    )
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()
    report = evaluate_self_evolution_health(
        review_root=args.review_root,
        markets=tuple(args.market) if args.market else None,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2 if args.pretty else None))
    return 1 if report["overall_status"] == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
