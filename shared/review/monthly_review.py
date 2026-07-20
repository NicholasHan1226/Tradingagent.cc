#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Monthly review — month-end.

Higher-altitude review:
  - architecture_health  : are all pipeline stages (screening→...→review) running?
  - memory_consolidation : distill the month's lessons into durable memory
  - goal_achievement     : did we hit the stage's monthly goals?
  - next_month_focus     : the one or two things to fix next month

Inputs (month_data) is a dict assembled by the monthly runner from daily/weekly
logs. Reviews are grouped market -> capital_layer -> explicit account_scope;
monetary values never cross account or native-currency boundaries.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shared.governance.market_lanes import canonical_runtime_market

from .attribution import attribute_pct

REVIEW_DIR = Path(__file__).resolve().parent
GOALS_PATH = REVIEW_DIR / "goals.yaml"
MONTHLY_LOG = REVIEW_DIR / "data" / "monthly_reviews.jsonl"
MARKET_CURRENCIES = {
    "ashare": "CNY",
    "cn_futures": "CNY",
    "crypto": "USDT",
}
UNSCOPED_ACCOUNT_KEY = "__unscoped__"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ensure_dirs() -> None:
    MONTHLY_LOG.parent.mkdir(parents=True, exist_ok=True)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        f = float(v)
        return default if f != f else f
    except (TypeError, ValueError):
        return default


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed else None


def _load_goals() -> dict[str, Any]:
    try:
        import yaml  # type: ignore

        with open(GOALS_PATH, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _append_log(record: dict[str, Any]) -> None:
    _ensure_dirs()
    with open(MONTHLY_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _normalize_capital_layer(value: Any, default: str = "shadow") -> str:
    raw = str(value or default).strip().lower()
    if raw in {"real", "live"}:
        return "real"
    if raw in {"sim", "simulated", "simulation"}:
        return "simulated"
    if raw in {"shadow", "paper", "paper_portfolio", "paper_tracking"}:
        return "shadow"
    return default


def _active_market(value: Any) -> str | None:
    try:
        return canonical_runtime_market(value)
    except ValueError:
        return None


def _market_currency(market: str) -> str:
    return MARKET_CURRENCIES[canonical_runtime_market(market)]


def _normalize_account_scope(value: Any) -> str | None:
    scope = str(value or "").strip()
    return scope or None


def _group_by_market_capital_layer_account(
    rows: list[dict[str, Any]],
) -> dict[str, dict[str, dict[str, list[dict[str, Any]]]]]:
    grouped: dict[str, dict[str, dict[str, list[dict[str, Any]]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(list))
    )
    for row in rows or []:
        market = _active_market(row.get("market"))
        if market is None:
            continue
        layer = _normalize_capital_layer(row.get("capital_layer"))
        account_scope = _normalize_account_scope(row.get("account_scope"))
        normalized = dict(row)
        normalized["market"] = market
        normalized["capital_layer"] = layer
        normalized["account_scope"] = account_scope
        grouped[market][layer][account_scope or UNSCOPED_ACCOUNT_KEY].append(normalized)
    return {
        market: {layer: dict(accounts) for layer, accounts in capital_layers.items()}
        for market, capital_layers in grouped.items()
    }


def _market_layer_account_metrics(
    month_data: dict[str, Any], market: str, capital_layer: str, account_scope: str
) -> dict[str, Any]:
    """Read only explicitly market/layer/account-scoped return/risk metrics."""
    market_metrics = month_data.get("market_metrics")
    if not isinstance(market_metrics, dict):
        return {}
    market_record = market_metrics.get(market)
    if not isinstance(market_record, dict):
        return {}
    layer_record = market_record.get(capital_layer)
    if not isinstance(layer_record, dict):
        return {}
    account_record = layer_record.get(account_scope)
    return dict(account_record) if isinstance(account_record, dict) else {}


def _assess_architecture_health(month_data: dict[str, Any]) -> dict[str, Any]:
    """Check each pipeline stage ran and produced output during the month.

    Expected keys in month_data["pipeline"]:
        screening, adversarial, risk, portfolio, execution, review, accounting
    Each should be {"runs": int, "errors": int, "last_run": iso}.
    """
    pipeline = month_data.get("pipeline", {})
    stages = [
        "screening",
        "adversarial",
        "risk",
        "portfolio",
        "execution",
        "review",
        "accounting",
    ]
    healthy: list[str] = []
    degraded: list[str] = []
    broken: list[str] = []
    for st in stages:
        info = pipeline.get(st, {})
        runs = int(info.get("runs", 0))
        errors = int(info.get("errors", 0))
        if runs == 0:
            broken.append(st)
        elif errors / max(runs, 1) > 0.1:
            degraded.append(st)
        else:
            healthy.append(st)
    all_healthy = not broken and not degraded
    return {
        "healthy": healthy,
        "degraded": degraded,
        "broken": broken,
        "all_healthy": all_healthy,
        "score": round(len(healthy) / len(stages), 4) if stages else 0.0,
    }


def _consolidate_memory(month_data: dict[str, Any]) -> dict[str, Any]:
    """Distill the month into durable memory entries.

    Pulls from:
      - top bleeding dimensions / strategies (to avoid)
      - top winning dimensions / strategies (to repeat)
      - any self-heal escalations (to harden)
    """
    trades = month_data.get("trades", [])
    attr = attribute_pct(trades)
    dims = attr.get("by_dimension", {})
    strats = attr.get("by_strategy", {})

    winning_dims = sorted(
        [(d, p) for d, p in dims.items() if p > 0 and d != "unattributed"],
        key=lambda x: x[1],
        reverse=True,
    )
    bleeding_dims = sorted(
        [(d, p) for d, p in dims.items() if p < 0 and d != "unattributed"],
        key=lambda x: x[1],
    )
    winning_strats = sorted(
        [(s, p) for s, p in strats.items() if p > 0 and s != "unattributed"],
        key=lambda x: x[1],
        reverse=True,
    )
    bleeding_strats = sorted(
        [(s, p) for s, p in strats.items() if p < 0 and s != "unattributed"],
        key=lambda x: x[1],
    )

    escalations = month_data.get("self_heal_escalations", [])

    return {
        "winning_dimensions": [
            {"dimension": d, "pnl_pct": p} for d, p in winning_dims[:3]
        ],
        "bleeding_dimensions": [
            {"dimension": d, "pnl_pct": p} for d, p in bleeding_dims[:3]
        ],
        "winning_strategies": [
            {"strategy": s, "pnl_pct": p} for s, p in winning_strats[:3]
        ],
        "bleeding_strategies": [
            {"strategy": s, "pnl_pct": p} for s, p in bleeding_strats[:3]
        ],
        "escalation_count": len(escalations),
        "escalation_summaries": escalations[:5],
        "consolidated_at": _now_iso(),
    }


def _assess_goals(month_data: dict[str, Any], stage: str) -> dict[str, Any]:
    """Did we hit the monthly goals for the current stage?"""
    goals = _load_goals()
    stage_goals = goals.get(stage, {})
    raw_monthly_return = month_data.get("monthly_return")
    monthly_return = (
        _safe_float(raw_monthly_return) if raw_monthly_return is not None else None
    )
    win_rate = _safe_float(month_data.get("win_rate"))
    sharpe = _safe_float(month_data.get("sharpe"))
    max_dd = _safe_float(month_data.get("max_drawdown"))
    beat_benchmark = bool(month_data.get("beat_benchmark", False))

    checks: list[dict[str, Any]] = []
    if "win_rate" in stage_goals:
        checks.append(
            {
                "metric": "win_rate",
                "actual": win_rate,
                "goal": stage_goals["win_rate"],
                "met": win_rate >= stage_goals["win_rate"],
            }
        )
    if "sharpe" in stage_goals and sharpe:
        checks.append(
            {
                "metric": "sharpe",
                "actual": sharpe,
                "goal": stage_goals["sharpe"],
                "met": sharpe >= stage_goals["sharpe"],
            }
        )
    if "max_drawdown" in stage_goals and max_dd:
        checks.append(
            {
                "metric": "max_drawdown",
                "actual": max_dd,
                "goal": stage_goals["max_drawdown"],
                "met": max_dd <= stage_goals["max_drawdown"],
            }
        )
    if stage_goals.get("monthly_return") == "positive":
        checks.append(
            {
                "metric": "monthly_return",
                "actual": monthly_return,
                "goal": "positive",
                "met": monthly_return is not None and monthly_return > 0,
            }
        )
    if stage_goals.get("beat_benchmark") is True:
        checks.append(
            {
                "metric": "beat_benchmark",
                "actual": beat_benchmark,
                "goal": True,
                "met": beat_benchmark,
            }
        )

    all_met = all(c["met"] for c in checks) if checks else False
    return {"stage": stage, "checks": checks, "all_goals_met": all_met}


# ---- main API ---------------------------------------------------------------


def review_month(
    month_data: dict[str, Any], stage: str = "stage_1_sim"
) -> dict[str, Any]:
    """Month-end review.

    Args:
        month_data: {
            "trades": [...],                 # each row requires market
            "pipeline": {stage: {runs, errors, last_run}},
            "market_metrics": {
                market: {
                    capital_layer: {
                        account_scope: {monthly_return, sharpe, ...}
                    }
                }
            },
            "self_heal_escalations": [...],
        }
        stage: current stage key in goals.yaml.

    Returns market -> capital-layer -> account reviews. ``all_markets`` and
    market/layer containers contain only counts and health.
    """
    as_of = _now_iso()
    arch = _assess_architecture_health(month_data)
    month = month_data.get("month", datetime.now(timezone.utc).strftime("%Y-%m"))
    grouped = _group_by_market_capital_layer_account(month_data.get("trades", []))
    market_reviews: dict[str, Any] = {}
    capital_layer_counts: dict[str, dict[str, int]] = {}

    for market, capital_layers in sorted(grouped.items()):
        layer_reviews: dict[str, Any] = {}
        for layer, account_rows in sorted(capital_layers.items()):
            account_reviews: dict[str, dict[str, Any]] = {}
            layer_trade_count = 0
            explicit_account_count = 0
            unscoped_trade_count = 0
            for account_key, account_trades in sorted(account_rows.items()):
                account_scope = (
                    None if account_key == UNSCOPED_ACCOUNT_KEY else account_key
                )
                layer_trade_count += len(account_trades)
                if account_scope is None:
                    unscoped_trade_count += len(account_trades)
                    account_reviews[account_key] = {
                        "market": market,
                        "currency": _market_currency(market),
                        "capital_layer": layer,
                        "account_scope": None,
                        "architecture_health": arch,
                        "month_trade_count": len(account_trades),
                        "monetary_state": "unavailable_missing_account_scope",
                        "review_state": "count_only",
                        "reason": "explicit_account_scope_required",
                    }
                    continue

                explicit_account_count += 1
                scoped_metrics = _market_layer_account_metrics(
                    month_data, market, layer, account_scope
                )
                account_data = dict(month_data)
                account_data["trades"] = account_trades
                account_data["win_rate"] = (
                    sum(
                        1
                        for trade in account_trades
                        if _safe_float(trade.get("pnl")) > 0
                    )
                    / len(account_trades)
                    if account_trades
                    else 0.0
                )
                # Return/risk values require exact account equity authority.
                account_data["monthly_return"] = _optional_float(
                    scoped_metrics.get("monthly_return")
                )
                account_data["sharpe"] = _optional_float(scoped_metrics.get("sharpe"))
                account_data["max_drawdown"] = _optional_float(
                    scoped_metrics.get("max_drawdown")
                )
                account_data["beat_benchmark"] = scoped_metrics.get("beat_benchmark")
                memory = _consolidate_memory(account_data)
                goal_chk = _assess_goals(account_data, stage)

                focus: list[str] = []
                if arch["broken"]:
                    focus.append(f"修复流水线断链: {', '.join(arch['broken'])}")
                if arch["degraded"]:
                    focus.append(f"降级流水线阶段: {', '.join(arch['degraded'])}")
                if memory["bleeding_dimensions"]:
                    focus.append(
                        "止血维度: "
                        + ", ".join(
                            item["dimension"] for item in memory["bleeding_dimensions"]
                        )
                    )
                if memory["bleeding_strategies"]:
                    focus.append(
                        "降权策略: "
                        + ", ".join(
                            item["strategy"] for item in memory["bleeding_strategies"]
                        )
                    )
                if not goal_chk["all_goals_met"]:
                    gaps = [
                        check["metric"]
                        for check in goal_chk["checks"]
                        if not check["met"]
                    ]
                    if gaps:
                        focus.append(f"补齐目标缺口: {', '.join(gaps)}")
                if not focus:
                    focus.append("维持当前轨道, 关注规模化容量.")

                month_pnl = sum(
                    _safe_float(trade.get("pnl")) for trade in account_trades
                )
                account_reviews[account_scope] = {
                    "market": market,
                    "currency": _market_currency(market),
                    "capital_layer": layer,
                    "account_scope": account_scope,
                    "architecture_health": arch,
                    "memory_consolidation": memory,
                    "goal_achievement": goal_chk,
                    "next_month_focus": focus,
                    "month_trade_count": len(account_trades),
                    "month_pnl": round(month_pnl, 6),
                    "monthly_return": account_data["monthly_return"],
                    "monthly_return_source": (
                        "market_metrics"
                        if account_data["monthly_return"] is not None
                        else "unavailable_without_account_equity_authority"
                    ),
                    "monetary_state": "available",
                }

            layer_reviews[layer] = {
                "market": market,
                "currency": _market_currency(market),
                "capital_layer": layer,
                "architecture_health": arch,
                "account_count": explicit_account_count,
                "unscoped_trade_count": unscoped_trade_count,
                "month_trade_count": layer_trade_count,
                "monetary_aggregation": "forbidden_across_accounts",
                "account_reviews": account_reviews,
            }
            count_summary = capital_layer_counts.setdefault(
                layer, {"trades": 0, "accounts": 0, "unscoped": 0}
            )
            count_summary["trades"] += layer_trade_count
            count_summary["accounts"] += explicit_account_count
            count_summary["unscoped"] += unscoped_trade_count
        market_reviews[market] = {
            "market": market,
            "currency": _market_currency(market),
            "capital_layer_reviews": layer_reviews,
        }

    result = {
        "session": "monthly",
        "as_of": as_of,
        "month": month,
        "all_markets": {
            "market_count": len(market_reviews),
            "capital_layer_count": len(capital_layer_counts),
            "account_count": sum(
                summary["accounts"] for summary in capital_layer_counts.values()
            ),
            "unscoped_trade_count": sum(
                summary["unscoped"] for summary in capital_layer_counts.values()
            ),
            "month_trade_count": sum(
                summary["trades"] for summary in capital_layer_counts.values()
            ),
            "architecture_health": arch,
            "monetary_aggregation": "forbidden",
        },
        "market_reviews": market_reviews,
    }
    for market_record in market_reviews.values():
        for layer_record in market_record["capital_layer_reviews"].values():
            for account_record in layer_record["account_reviews"].values():
                _append_log(
                    {
                        "session": "monthly",
                        "as_of": as_of,
                        "month": month,
                        **account_record,
                    }
                )
    return result


# ---- self-test --------------------------------------------------------------

if __name__ == "__main__":
    sample_month = {
        "month": "2026-06",
        "trades": [
            {
                "market": "ashare",
                "pnl": 0.05,
                "dimensions": {"macro": 0.6, "technical": 0.4},
                "strategy": "pullback",
                "condition": "low_vol",
            },
            {
                "market": "ashare",
                "pnl": -0.06,
                "dimension": "event",
                "strategy": "event_driven",
                "condition": "high_vol",
            },
            {
                "market": "ashare",
                "pnl": 0.03,
                "dimensions": {"technical": 1.0},
                "strategy": "trend",
                "condition": "mid_vol",
            },
        ],
        "pipeline": {
            "screening": {"runs": 22, "errors": 0, "last_run": "2026-06-29"},
            "adversarial": {"runs": 22, "errors": 1, "last_run": "2026-06-29"},
            "risk": {"runs": 22, "errors": 0, "last_run": "2026-06-29"},
            "portfolio": {"runs": 22, "errors": 0, "last_run": "2026-06-29"},
            "execution": {"runs": 22, "errors": 3, "last_run": "2026-06-29"},
            "review": {"runs": 44, "errors": 0, "last_run": "2026-06-29"},
            "accounting": {"runs": 22, "errors": 0, "last_run": "2026-06-29"},
        },
        "monthly_return": 0.02,
        "win_rate": 0.57,
        "sharpe": 0.6,
        "max_drawdown": 0.07,
        "beat_benchmark": True,
        "benchmark_return": 0.005,
        "self_heal_escalations": [{"issue": "data_stale", "resolved": True}],
    }
    print(
        json.dumps(
            review_month(sample_month, stage="stage_1_sim"),
            ensure_ascii=False,
            indent=2,
        )
    )
