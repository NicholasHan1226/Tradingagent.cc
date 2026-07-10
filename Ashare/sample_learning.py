#!/usr/bin/env python3
"""A-share sample learning report.

This module turns simulated-only A-share evidence into a learning report:
sample quality tiers, hypothesis tracking, no-trade attribution, dynamic probe
budget guidance, tier-account objectives, and factor research readiness.
It does not place orders or touch broker/account state.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from shared.review.sample_quality import classify_trade_sample
from shared.runtime_test.ashare_no_trade_summary import NO_TRADE_LOG, summarize_no_trade_log
from shared.markets.sim_capital import default_sim_capital


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REVIEW_DIR = ROOT / "shared" / "review" / "ashare"
DEFAULT_TRADES_PATH = ROOT / "shared" / "logs" / "local_sim" / "local_sim_trades.jsonl"
LATEST_PATH = DEFAULT_REVIEW_DIR / "sample_learning_latest.json"
LOG_PATH = DEFAULT_REVIEW_DIR / "sample_learning_log.jsonl"
CN_TZ = timezone(timedelta(hours=8))


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


def _compact_date(value: Any) -> str:
    raw = str(value or "").strip()
    if len(raw) >= 10 and raw[4] == "-" and raw[7] == "-":
        return raw[:10].replace("-", "")
    return raw[:8] if raw else ""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except (FileNotFoundError, OSError):
        return []
    rows: list[dict[str, Any]] = []
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


def build_hypothesis_id(
    *,
    trade_date: str,
    symbol: str,
    side: str,
    execution_source: str,
    candidate_pool_layer: str,
    score: float,
) -> str:
    compact = _compact_date(trade_date) or "unknown"
    clean_symbol = str(symbol or "unknown").strip().upper()
    clean_side = str(side or "buy").strip().lower()
    layer = str(candidate_pool_layer or execution_source or "unknown").strip().lower()
    score_bucket = int(max(0.0, min(0.99, float(score))) * 100)
    return f"ashare-{compact}-{clean_side}-{clean_symbol}-{layer}-s{score_bucket:03d}"


def build_research_hypothesis(
    *,
    trade_date: str,
    symbol: str,
    side: str,
    execution_source: str,
    candidate_pool_layer: str,
    score_snapshot: dict[str, Any],
    sample_intent: str,
    capital_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    score = _safe_float(score_snapshot.get("combined", score_snapshot.get("score")), 0.0)
    hypothesis_id = build_hypothesis_id(
        trade_date=trade_date,
        symbol=symbol,
        side=side,
        execution_source=execution_source,
        candidate_pool_layer=candidate_pool_layer,
        score=score,
    )
    factors = {
        key: round(_safe_float(score_snapshot.get(key)), 6)
        for key in ("combined", "macro", "event", "fundamental", "capital", "technical", "sentiment")
        if key in score_snapshot
    }
    return {
        "hypothesis_id": hypothesis_id,
        "trade_date": _compact_date(trade_date),
        "symbol": str(symbol or "").upper(),
        "side": str(side or "buy").lower(),
        "sample_intent": sample_intent,
        "factor_snapshot": factors,
        "capital_plan_risk_mode": (capital_plan or {}).get("risk_mode"),
        "expected_validation_horizon": ["m30", "m60", "close", "next_day"],
        "failure_conditions": [
            "candidate_source_invalid",
            "fill_price_missing",
            "outside_regular_session",
            "forward_return_negative_after_validation_window",
        ],
    }


def _label_map(forward_validation: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    rows = forward_validation.get("labels") if isinstance(forward_validation.get("labels"), list) else []
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key in (row.get("trade_id"), row.get("order_id")):
            if key:
                result[str(key)] = row
    return result


def _label_for_trade(trade: dict[str, Any], labels: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return labels.get(str(trade.get("trade_id") or "")) or labels.get(str(trade.get("order_id") or "")) or {}


def _return_from_label(label: dict[str, Any]) -> float | None:
    labels = label.get("labels") if isinstance(label.get("labels"), dict) else {}
    close = labels.get("close") if isinstance(labels.get("close"), dict) else {}
    if close.get("return_pct") is not None:
        return _safe_float(close.get("return_pct"))
    next_day = labels.get("next_day") if isinstance(labels.get("next_day"), dict) else {}
    for key in ("close_return_pct", "high_return_pct", "open_return_pct"):
        if next_day.get(key) is not None:
            return _safe_float(next_day.get(key))
    return None


def _quality_tier(trade: dict[str, Any], label: dict[str, Any]) -> str:
    classification = classify_trade_sample(trade)
    if not classification.get("strategy_sample_valid"):
        return "chain_validation_sample"
    hypothesis_id = str(trade.get("hypothesis_id") or (trade.get("research_hypothesis") or {}).get("hypothesis_id") or "")
    price_class = str(trade.get("fill_price_source_class") or "").lower()
    has_labeled_forward = _return_from_label(label) is not None
    if hypothesis_id and price_class == "market_data" and has_labeled_forward:
        return "high_quality_strategy_sample"
    if hypothesis_id:
        return "medium_quality_strategy_sample"
    return "low_quality_strategy_sample"


def _factor_snapshot(trade: dict[str, Any]) -> dict[str, float]:
    hypothesis = trade.get("research_hypothesis") if isinstance(trade.get("research_hypothesis"), dict) else {}
    snapshot = hypothesis.get("factor_snapshot") if isinstance(hypothesis.get("factor_snapshot"), dict) else {}
    if not snapshot and isinstance(trade.get("factor_snapshot"), dict):
        snapshot = trade.get("factor_snapshot")
    result: dict[str, float] = {}
    for key, value in snapshot.items():
        parsed = _safe_float(value, None)
        if parsed is not None:
            result[str(key)] = round(parsed, 6)
    return result


def _factor_research(trades: list[dict[str, Any]], labels: dict[str, dict[str, Any]], *, min_samples: int) -> dict[str, Any]:
    buckets: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for trade in trades:
        if not classify_trade_sample(trade).get("strategy_sample_valid"):
            continue
        forward_return = _return_from_label(_label_for_trade(trade, labels))
        if forward_return is None:
            continue
        for factor, value in _factor_snapshot(trade).items():
            buckets[factor].append((value, forward_return))
    factors: dict[str, Any] = {}
    max_samples = 0
    for factor, rows in sorted(buckets.items()):
        returns = [ret for _, ret in rows]
        max_samples = max(max_samples, len(rows))
        positive_when_high = [ret > 0 for value, ret in rows if value >= 0.55]
        factors[factor] = {
            "sample_count": len(rows),
            "avg_return_pct": round(sum(returns) / len(returns), 8) if returns else 0.0,
            "directional_hit_rate": round(sum(1 for hit in positive_when_high if hit) / len(positive_when_high), 4)
            if positive_when_high
            else None,
            "status": "ready" if len(rows) >= min_samples else "sample_debt",
        }
    return {
        "status": "ready" if max_samples >= min_samples and factors else "sample_debt",
        "min_samples": max(1, int(min_samples)),
        "max_factor_sample_count": max_samples,
        "factors": factors,
        "method": "trade_factor_snapshot_vs_forward_return",
    }


def _sample_quality(trades: list[dict[str, Any]], labels: dict[str, dict[str, Any]]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    tiers: Counter[str] = Counter()
    for trade in trades:
        label = _label_for_trade(trade, labels)
        tier = _quality_tier(trade, label)
        tiers[tier] += 1
        classification = classify_trade_sample(trade)
        rows.append(
            {
                "trade_id": trade.get("trade_id"),
                "order_id": trade.get("order_id"),
                "symbol": trade.get("ts_code") or trade.get("symbol"),
                "side": trade.get("side"),
                "tier": tier,
                "strategy_sample_valid": bool(classification.get("strategy_sample_valid")),
                "sample_quality_reason": classification.get("sample_quality_reason"),
                "hypothesis_id": trade.get("hypothesis_id") or (trade.get("research_hypothesis") or {}).get("hypothesis_id"),
                "forward_return_pct": _return_from_label(label),
            }
        )
    return {
        "total_count": len(rows),
        "tier_counts": dict(sorted(tiers.items())),
        "samples": rows[:50],
    }


def _hypothesis_registry(trades: list[dict[str, Any]]) -> dict[str, Any]:
    registry: dict[str, dict[str, Any]] = {}
    for trade in trades:
        hypothesis = trade.get("research_hypothesis") if isinstance(trade.get("research_hypothesis"), dict) else {}
        hypothesis_id = str(trade.get("hypothesis_id") or hypothesis.get("hypothesis_id") or "")
        if not hypothesis_id:
            continue
        entry = registry.setdefault(
            hypothesis_id,
            {
                "hypothesis_id": hypothesis_id,
                "symbol": trade.get("ts_code") or trade.get("symbol"),
                "side": trade.get("side"),
                "sample_intent": hypothesis.get("sample_intent"),
                "trade_count": 0,
                "factor_snapshot": hypothesis.get("factor_snapshot") if isinstance(hypothesis.get("factor_snapshot"), dict) else {},
            },
        )
        entry["trade_count"] += 1
    return {"hypothesis_count": len(registry), "hypotheses": list(registry.values())[:50]}


def _postclose_attribution(sample_monitor: dict[str, Any], no_trade_summary: dict[str, Any]) -> dict[str, Any]:
    blockers = sample_monitor.get("blockers") if isinstance(sample_monitor.get("blockers"), list) else []
    latest = no_trade_summary.get("latest_no_trade_log") if isinstance(no_trade_summary.get("latest_no_trade_log"), dict) else {}
    category = str(latest.get("category") or "")
    primary = str(blockers[0]) if blockers else category or "none"
    next_actions = {
        "portfolio_evolution_stale": "refresh_portfolio_evolution_before_next_checkpoint",
        "capital_plan_defensive": "review_dynamic_capital_profile_and_candidate_quality",
        "no_candidates": "expand_scored_liquid_universe_without_lowering_candidate_gate",
        "risk_rejections_present": "review_risk_rejection_thresholds",
        "no_orders": "review_portfolio_capacity_and_lot_budget",
    }
    return {
        "primary_blocker": primary,
        "blockers": list(dict.fromkeys([*blockers, category] if category else blockers)),
        "no_trade_evidence_status": no_trade_summary.get("evidence_status"),
        "recommended_next_action": next_actions.get(primary, "continue_sample_collection_with_guardrails"),
    }


def _dynamic_probe_budget(trades: list[dict[str, Any]], sample_monitor: dict[str, Any], capital: float | None = None) -> dict[str, Any]:
    if capital is None:
        capital = default_sim_capital("ashare")
    scores = [_safe_float(_factor_snapshot(trade).get("combined"), 0.0) for trade in trades]
    top = max(scores) if scores else 0.0
    # Scale probe budget relative to 200k reference account
    capital_scale = max(0.25, min(1.0, float(capital) / 200_000.0))
    minimum = max(5_000.0, min(20_000.0 * capital_scale, float(capital) * 0.10))
    maximum = max(minimum, min(35_000.0 * capital_scale, float(capital) * 0.175))
    ratio = max(0.0, min(1.0, (top - 0.55) / 0.20))
    recommended = minimum + (maximum - minimum) * ratio
    if sample_monitor.get("overall_status") == "fail":
        recommended *= 0.85
    return {
        "min": round(minimum, 2),
        "max": round(maximum, 2),
        "recommended_allocation": round(max(minimum, min(maximum, recommended)) // 100 * 100, 2),
        "top_candidate_score": round(top, 4),
        "source": "factor_snapshot_and_sample_target_state",
    }


def _account_objectives(tier_manifest: dict[str, Any], portfolio: dict[str, Any]) -> dict[str, Any]:
    primary_capital = round(default_sim_capital("ashare"), 6)
    primary_account = f"ashare_{int(primary_capital)}"

    # Base objectives keyed by account name — populated dynamically
    objectives: dict[str, dict[str, Any]] = {}

    # Primary account objective
    objectives[primary_account] = {
        "primary_goal": "drawdown_controlled_growth",
        "guardrail": "max_drawdown_and_sample_quality",
        "target_metric": "portfolio_level_risk_adjusted_pnl",
    }

    # Legacy experiment-tier objectives (only created for accounts present in the manifest)
    known_tier_goals: dict[str, dict[str, Any]] = {
        "ashare_50000": {
            "primary_goal": "capital_efficiency",
            "guardrail": "lot_size_and_fee_drag",
            "target_metric": "cash_utilization_after_valid_samples",
            "note": "historical_experiment_epoch",
        },
        "ashare_100000": {
            "primary_goal": "balanced_efficiency",
            "guardrail": "drawdown_and_position_capacity",
            "target_metric": "risk_adjusted_pnl",
            "note": "historical_experiment_epoch",
        },
    }

    # Merge experiment tier manifest data
    accounts = tier_manifest.get("accounts") if isinstance(tier_manifest.get("accounts"), list) else []
    for account in accounts:
        if not isinstance(account, dict):
            continue
        name = str(account.get("account") or "")
        if name == primary_account:
            continue  # Primary account stats come from portfolio, not the experiment manifest
        if name in known_tier_goals:
            objectives.setdefault(name, {}).update(known_tier_goals[name])
            objectives[name]["trade_count"] = _safe_int(account.get("trade_count"))
            pnl = account.get("pnl") if isinstance(account.get("pnl"), dict) else {}
            objectives[name]["total_pnl"] = round(_safe_float(pnl.get("total_pnl")), 6)

    # Attach portfolio-level stats to the primary account
    pnl = portfolio.get("pnl") if isinstance(portfolio.get("pnl"), dict) else {}
    objectives[primary_account]["trade_count"] = _safe_int(portfolio.get("strategy_sample_count"))
    objectives[primary_account]["total_pnl"] = round(_safe_float(pnl.get("total_pnl")), 6)

    return objectives


def build_sample_learning_report(
    *,
    trade_date: str = "",
    review_dir: Path | str | None = None,
    local_trades_path: Path | str | None = None,
    no_trade_log_path: Path | str | None = None,
    min_factor_samples: int = 10,
) -> dict[str, Any]:
    review_path = Path(review_dir) if review_dir is not None else DEFAULT_REVIEW_DIR
    trades_path = Path(local_trades_path) if local_trades_path is not None else DEFAULT_TRADES_PATH
    no_trade_path = Path(no_trade_log_path) if no_trade_log_path is not None else NO_TRADE_LOG
    target_date = _compact_date(trade_date) or datetime.now(CN_TZ).strftime("%Y%m%d")
    trades = [row for row in _read_jsonl(trades_path) if _compact_date(row.get("trade_date") or row.get("created_at")) == target_date]
    forward_validation = _read_json(review_path / "forward_validation_latest.json")
    labels = _label_map(forward_validation)
    sample_monitor = _read_json(review_path / "sample_target_monitor_latest.json")
    no_trade_summary = summarize_no_trade_log(no_trade_path, target_date)
    tier_manifest = _read_json(review_path / "tier_experiments_latest.json")
    portfolio = _read_json(review_path / "portfolio_evolution_latest.json")
    quality = _sample_quality(trades, labels)
    factor_research = _factor_research(trades, labels, min_samples=max(1, int(min_factor_samples)))
    status = "pass"
    if sample_monitor.get("overall_status") in {"warn", "fail"} or factor_research["status"] == "sample_debt":
        status = "warn"
    if sample_monitor.get("overall_status") == "fail":
        status = "fail"
    report = {
        "report_type": "ashare_sample_learning",
        "generated_at": datetime.now(CN_TZ).isoformat(timespec="seconds"),
        "trade_date": target_date,
        "overall_status": status,
        "sample_quality": quality,
        "hypothesis_registry": _hypothesis_registry(trades),
        "postclose_attribution": _postclose_attribution(sample_monitor, no_trade_summary),
        "dynamic_probe_budget": _dynamic_probe_budget(trades, sample_monitor),
        "account_objectives": _account_objectives(tier_manifest, portfolio),
        "factor_research": factor_research,
        "read_only": True,
        "writes_orders": False,
        "real_trading_enabled": False,
    }
    return report


def write_sample_learning_report(
    *,
    trade_date: str = "",
    review_dir: Path | str | None = None,
    local_trades_path: Path | str | None = None,
    no_trade_log_path: Path | str | None = None,
    min_factor_samples: int = 10,
) -> dict[str, Any]:
    review_path = Path(review_dir) if review_dir is not None else DEFAULT_REVIEW_DIR
    review_path.mkdir(parents=True, exist_ok=True)
    report = build_sample_learning_report(
        trade_date=trade_date,
        review_dir=review_path,
        local_trades_path=local_trades_path,
        no_trade_log_path=no_trade_log_path,
        min_factor_samples=min_factor_samples,
    )
    latest = review_path / LATEST_PATH.name
    log = review_path / LOG_PATH.name
    latest.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(report, ensure_ascii=False, sort_keys=True) + "\n")
    report["latest_path"] = str(latest)
    report["log_path"] = str(log)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", default="")
    parser.add_argument("--review-dir", type=Path, default=DEFAULT_REVIEW_DIR)
    parser.add_argument("--local-trades-path", type=Path, default=DEFAULT_TRADES_PATH)
    parser.add_argument("--no-trade-log-path", type=Path, default=NO_TRADE_LOG)
    parser.add_argument("--min-factor-samples", type=int, default=10)
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)
    report = write_sample_learning_report(
        trade_date=args.trade_date,
        review_dir=args.review_dir,
        local_trades_path=args.local_trades_path,
        no_trade_log_path=args.no_trade_log_path,
        min_factor_samples=args.min_factor_samples,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2 if args.pretty else None, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
