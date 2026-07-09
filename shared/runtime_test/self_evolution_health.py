#!/usr/bin/env python3
"""Read-only health check for simulated self-evolution evidence."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
    return {str(key): value for key, value in weights.items() if isinstance(value, dict)}


def _sum_rank_metric(rows: list[dict[str, Any]], key: str) -> float:
    return round(sum(_safe_float(row.get(key)) for row in rows if isinstance(row, dict)), 6)


def _sum_rank_trades(rows: list[dict[str, Any]]) -> int:
    return sum(_safe_int(row.get("trades")) for row in rows if isinstance(row, dict))


def _performance_summary(review_dir: Path) -> dict[str, Any]:
    rows = _read_jsonl(review_dir / "style_performance.jsonl")
    return {
        "row_count": len(rows),
        "trade_sum": sum(_safe_int(row.get("trades")) for row in rows),
        "pnl_sum": round(sum(_safe_float(row.get("pnl")) for row in rows), 6),
    }


def _ashare_portfolio_evolution(review_dir: Path) -> dict[str, Any]:
    return _read_json(review_dir / "portfolio_evolution_latest.json")


def _cn_futures_weight_mismatches(evolution: dict[str, Any]) -> list[dict[str, Any]]:
    weights = _weights_map(evolution)
    mismatches: list[dict[str, Any]] = []
    for action in evolution.get("actions") or []:
        if not isinstance(action, dict):
            continue
        style_name = str(action.get("style_name") or "")
        after = action.get("after") if isinstance(action.get("after"), dict) else {}
        weight_row = weights.get(style_name) or {}
        if "weight" not in after or "weight" not in weight_row:
            continue
        after_weight = round(_safe_float(after.get("weight")), 6)
        final_weight = round(_safe_float(weight_row.get("weight")), 6)
        if after_weight != final_weight:
            mismatches.append({
                "style_name": style_name,
                "action": action.get("action"),
                "after_weight": after_weight,
                "final_weight": final_weight,
            })
    return mismatches


def evaluate_self_evolution_health(
    *,
    review_root: Path | str | None = None,
    markets: tuple[str, ...] | list[str] | None = None,
    pnl_summary: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    root = Path(review_root) if review_root is not None else DEFAULT_REVIEW_ROOT
    target_markets = tuple(markets or DEFAULT_MARKETS)
    pnl = pnl_summary if pnl_summary is not None else sim_ledger_pnl_summary(markets=target_markets)
    market_reports: list[dict[str, Any]] = []
    all_issues: list[dict[str, Any]] = []

    for market in target_markets:
        review_dir = root / market
        latest_evolution = _latest(_read_jsonl(review_dir / "evolution_log.jsonl"))
        portfolio_evolution = _ashare_portfolio_evolution(review_dir) if market == "ashare" else {}
        evolution_for_samples = portfolio_evolution if portfolio_evolution else latest_evolution
        rankings = latest_evolution.get("rankings") if isinstance(latest_evolution.get("rankings"), list) else []
        sample_rankings = evolution_for_samples.get("rankings") if isinstance(evolution_for_samples.get("rankings"), list) else rankings
        actions = latest_evolution.get("actions") if isinstance(latest_evolution.get("actions"), list) else []
        sample_actions = evolution_for_samples.get("actions") if isinstance(evolution_for_samples.get("actions"), list) else actions
        weights = _weights_map(latest_evolution) or _weights_map(_read_json(review_dir / "style_weights.json"))
        if market == "ashare":
            weights = _weights_map(portfolio_evolution) or weights
        market_pnl = pnl.get(market, {}) if isinstance(pnl, dict) else {}
        sample_quality = market_pnl.get("sample_quality") if isinstance(market_pnl.get("sample_quality"), dict) else {}
        strategy_sample_count = _safe_int(sample_quality.get("strategy_sample_valid_count"))
        ranking_trade_sum = _sum_rank_trades(sample_rankings)
        issues: list[str] = []

        if strategy_sample_count > 0 and ranking_trade_sum <= 0:
            issues.append("strategy_samples_not_seen_by_evolution")
        if market == "cn_futures":
            mismatches = _cn_futures_weight_mismatches(latest_evolution)
            if mismatches:
                issues.append("action_after_weight_mismatch")
        else:
            mismatches = []
        if market in {"crypto", "pm"} and not latest_evolution:
            issues.append("missing_evolution_log")

        for issue in issues:
            all_issues.append({"market": market, "issue": issue})

        market_reports.append({
            "market": market,
            "status": "warn" if issues else "pass",
            "issues": issues,
            "latest_evolution_at": evolution_for_samples.get("generated_at", ""),
            "latest_evolution_state": evolution_for_samples.get("state", "missing") if evolution_for_samples else "missing",
            "evolution_source": "ashare_portfolio_evolution" if market == "ashare" and portfolio_evolution else "style_evolution",
            "action_count": len(sample_actions),
            "non_observe_action_count": sum(1 for item in sample_actions if isinstance(item, dict) and item.get("action") not in {"observe", None, ""}),
            "generated_variant_count": len(evolution_for_samples.get("generated_variants") or []),
            "ranking_trade_sum": ranking_trade_sum,
            "ranking_pnl_sum": _sum_rank_metric(sample_rankings, "pnl"),
            "style_weight_count": len(weights),
            "portfolio_evolution": {
                "state": portfolio_evolution.get("state", "missing") if portfolio_evolution else "missing",
                "strategy_sample_count": portfolio_evolution.get("strategy_sample_count", 0) if portfolio_evolution else 0,
                "today_strategy_sample_count": portfolio_evolution.get("today_strategy_sample_count", 0) if portfolio_evolution else 0,
            } if market == "ashare" else {},
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
                and any(isinstance(item, dict) and item.get("action") in {"promote", "promoted", "variant_generated", "expand_risk"} for item in sample_actions)
            ),
        })

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
    parser.add_argument("--market", action="append", choices=DEFAULT_MARKETS, help="May be repeated; default all simulated markets.")
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
