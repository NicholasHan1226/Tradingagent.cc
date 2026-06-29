#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Monthly review — month-end.

Higher-altitude review:
  - architecture_health  : are all pipeline stages (screening→...→review) running?
  - memory_consolidation : distill the month's lessons into durable memory
  - goal_achievement     : did we hit the stage's monthly goals?
  - next_month_focus     : the one or two things to fix next month

Inputs (month_data) is a dict assembled by the monthly runner from daily/weekly logs.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .attribution import attribute_pct
from .benchmark import compare_to_benchmark

REVIEW_DIR = Path(__file__).resolve().parent
GOALS_PATH = REVIEW_DIR / "goals.yaml"
MONTHLY_LOG = REVIEW_DIR / "data" / "monthly_reviews.jsonl"


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


def _assess_architecture_health(month_data: dict[str, Any]) -> dict[str, Any]:
    """Check each pipeline stage ran and produced output during the month.

    Expected keys in month_data["pipeline"]:
        screening, adversarial, risk, portfolio, execution, review, accounting
    Each should be {"runs": int, "errors": int, "last_run": iso}.
    """
    pipeline = month_data.get("pipeline", {})
    stages = ["screening", "adversarial", "risk", "portfolio", "execution", "review", "accounting"]
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
        key=lambda x: x[1], reverse=True,
    )
    bleeding_dims = sorted(
        [(d, p) for d, p in dims.items() if p < 0 and d != "unattributed"],
        key=lambda x: x[1],
    )
    winning_strats = sorted(
        [(s, p) for s, p in strats.items() if p > 0 and s != "unattributed"],
        key=lambda x: x[1], reverse=True,
    )
    bleeding_strats = sorted(
        [(s, p) for s, p in strats.items() if p < 0 and s != "unattributed"],
        key=lambda x: x[1],
    )

    escalations = month_data.get("self_heal_escalations", [])

    return {
        "winning_dimensions": [{"dimension": d, "pnl_pct": p} for d, p in winning_dims[:3]],
        "bleeding_dimensions": [{"dimension": d, "pnl_pct": p} for d, p in bleeding_dims[:3]],
        "winning_strategies": [{"strategy": s, "pnl_pct": p} for s, p in winning_strats[:3]],
        "bleeding_strategies": [{"strategy": s, "pnl_pct": p} for s, p in bleeding_strats[:3]],
        "escalation_count": len(escalations),
        "escalation_summaries": escalations[:5],
        "consolidated_at": _now_iso(),
    }


def _assess_goals(month_data: dict[str, Any], stage: str) -> dict[str, Any]:
    """Did we hit the monthly goals for the current stage?"""
    goals = _load_goals()
    stage_goals = goals.get(stage, {})
    monthly_return = _safe_float(month_data.get("monthly_return"))
    win_rate = _safe_float(month_data.get("win_rate"))
    sharpe = _safe_float(month_data.get("sharpe"))
    max_dd = _safe_float(month_data.get("max_drawdown"))
    beat_benchmark = bool(month_data.get("beat_benchmark", False))

    checks: list[dict[str, Any]] = []
    if "win_rate" in stage_goals:
        checks.append({"metric": "win_rate", "actual": win_rate, "goal": stage_goals["win_rate"], "met": win_rate >= stage_goals["win_rate"]})
    if "sharpe" in stage_goals and sharpe:
        checks.append({"metric": "sharpe", "actual": sharpe, "goal": stage_goals["sharpe"], "met": sharpe >= stage_goals["sharpe"]})
    if "max_drawdown" in stage_goals and max_dd:
        checks.append({"metric": "max_drawdown", "actual": max_dd, "goal": stage_goals["max_drawdown"], "met": max_dd <= stage_goals["max_drawdown"]})
    if stage_goals.get("monthly_return") == "positive":
        checks.append({"metric": "monthly_return", "actual": monthly_return, "goal": "positive", "met": monthly_return > 0})
    if stage_goals.get("beat_benchmark") is True:
        checks.append({"metric": "beat_benchmark", "actual": beat_benchmark, "goal": True, "met": beat_benchmark})

    all_met = all(c["met"] for c in checks) if checks else False
    return {"stage": stage, "checks": checks, "all_goals_met": all_met}


# ---- main API ---------------------------------------------------------------

def review_month(month_data: dict[str, Any], stage: str = "stage_1_sim") -> dict[str, Any]:
    """Month-end review.

    Args:
        month_data: {
            "trades": [...],                 # all month trades
            "pipeline": {stage: {runs, errors, last_run}},
            "monthly_return": float,
            "win_rate": float,
            "sharpe": float,
            "max_drawdown": float,
            "beat_benchmark": bool,
            "benchmark_return": float,
            "portfolio_returns_series": [...],
            "benchmark_returns_series": [...],
            "self_heal_escalations": [...],
        }
        stage: current stage key in goals.yaml.

    Returns:
        {
          "session": "monthly",
          "architecture_health": {...},
          "memory_consolidation": {...},
          "goal_achievement": {...},
          "next_month_focus": [str],
        }
    """
    arch = _assess_architecture_health(month_data)
    memory = _consolidate_memory(month_data)
    goal_chk = _assess_goals(month_data, stage)

    # next month focus: prioritize broken pipeline > bleeding dimensions > goal gaps
    focus: list[str] = []
    if arch["broken"]:
        focus.append(f"修复流水线断链: {', '.join(arch['broken'])}")
    if arch["degraded"]:
        focus.append(f"降级流水线阶段: {', '.join(arch['degraded'])}")
    if memory["bleeding_dimensions"]:
        focus.append(f"止血维度: {', '.join(d['dimension'] for d in memory['bleeding_dimensions'])}")
    if memory["bleeding_strategies"]:
        focus.append(f"降权策略: {', '.join(s['strategy'] for s in memory['bleeding_strategies'])}")
    if not goal_chk["all_goals_met"]:
        gaps = [c["metric"] for c in goal_chk["checks"] if not c["met"]]
        focus.append(f"补齐目标缺口: {', '.join(gaps)}")
    if not focus:
        focus.append("维持当前轨道, 关注规模化容量.")

    result = {
        "session": "monthly",
        "as_of": _now_iso(),
        "month": month_data.get("month", datetime.now(timezone.utc).strftime("%Y-%m")),
        "architecture_health": arch,
        "memory_consolidation": memory,
        "goal_achievement": goal_chk,
        "next_month_focus": focus,
    }
    _append_log(result)
    return result


# ---- self-test --------------------------------------------------------------

if __name__ == "__main__":
    sample_month = {
        "month": "2026-06",
        "trades": [
            {"pnl": 0.05, "dimensions": {"macro": 0.6, "technical": 0.4}, "strategy": "pullback", "condition": "low_vol"},
            {"pnl": -0.06, "dimension": "event", "strategy": "event_driven", "condition": "high_vol"},
            {"pnl": 0.03, "dimensions": {"technical": 1.0}, "strategy": "trend", "condition": "mid_vol"},
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
    print(json.dumps(review_month(sample_month, stage="stage_1_sim"), ensure_ascii=False, indent=2))
