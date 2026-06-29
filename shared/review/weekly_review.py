#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Weekly review — Friday close.

Decides strategy promotions / demotions based on 2 consecutive weeks of data.

  - Eliminate (demote): win rate <50% for 2 consecutive weeks → downgrade weight/kill
  - Promote (upgrade) : shadow positive for 2 consecutive weeks → upgrade to sim/real

Also reports dimension effectiveness so the screening layer can re-weight.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .attribution import attribute_pct

REVIEW_DIR = Path(__file__).resolve().parent
WEEKLY_LOG = REVIEW_DIR / "data" / "weekly_reviews.jsonl"
WEEKLY_STATE = REVIEW_DIR / "data" / "weekly_state.json"  # tracks consecutive weeks


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ensure_dirs() -> None:
    WEEKLY_LOG.parent.mkdir(parents=True, exist_ok=True)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        f = float(v)
        return default if f != f else f
    except (TypeError, ValueError):
        return default


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    _ensure_dirs()
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _append_log(record: dict[str, Any]) -> None:
    _ensure_dirs()
    with open(WEEKLY_LOG, "a", encoding="utf-8") as f:
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


def _group_by_capital_layer(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows or []:
        layer = _normalize_capital_layer(row.get("capital_layer"))
        normalized = dict(row)
        normalized["capital_layer"] = layer
        grouped[layer].append(normalized)
    return dict(grouped)


def _strategy_stats(week_trades: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per-strategy win rate + pnl for the week."""
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in week_trades or []:
        s = t.get("strategy", "unattributed")
        buckets[str(s)].append(t)
    out: dict[str, dict[str, Any]] = {}
    for s, trades in buckets.items():
        wins = sum(1 for t in trades if _safe_float(t.get("pnl")) > 0)
        pnl = sum(_safe_float(t.get("pnl")) for t in trades)
        out[s] = {
            "trades": len(trades),
            "wins": wins,
            "win_rate": round(wins / len(trades), 4) if trades else 0.0,
            "pnl": round(pnl, 6),
        }
    return out


def _dimension_effectiveness(week_trades: list[dict[str, Any]]) -> dict[str, float]:
    """Dimension → pnl contribution %. Positive = effective, negative = bleeding."""
    attr = attribute_pct(week_trades)
    return attr.get("by_dimension", {})


# ---- consecutive-week tracking ----------------------------------------------

def _update_consecutive(state: dict[str, Any], strategy: str, week_positive: bool, week_below_50: bool) -> dict[str, Any]:
    """Track consecutive weeks for promotion/demotion logic."""
    s = state.setdefault("strategies", {}).setdefault(strategy, {})
    # promotion: consecutive positive weeks
    if week_positive:
        s["consecutive_positive_weeks"] = s.get("consecutive_positive_weeks", 0) + 1
        s["consecutive_below50_weeks"] = 0
    else:
        s["consecutive_positive_weeks"] = 0
    # demotion: consecutive weeks win_rate < 50%
    if week_below_50:
        s["consecutive_below50_weeks"] = s.get("consecutive_below50_weeks", 0) + 1
    else:
        s["consecutive_below50_weeks"] = 0
    return s


# ---- main API ---------------------------------------------------------------

def review_week(week_trades: list[dict[str, Any]], strategies: list[str] | None = None) -> dict[str, Any]:
    """Friday review.

    Args:
        week_trades: all trades for the week (each carries strategy/dimension/condition/pnl).
        strategies: known strategy ids (to include strategies with zero trades this week).

    Returns:
        {
          "strategy_win_rates": {strategy: {trades, wins, win_rate, pnl}},
          "dimension_effectiveness": {dim: pnl_pct},
          "conditions_to_adjust": [str],      # conditions that bled
          "strategies_to_eliminate": [str],   # demotion candidates
          "strategies_to_promote": [str],     # promotion candidates
          "week_pnl": float,
          "week_win_rate": float,
        }
    """
    grouped = _group_by_capital_layer(week_trades)
    state = _read_json(WEEKLY_STATE)
    as_of = _now_iso()
    capital_layer_reviews: dict[str, Any] = {}

    for layer in sorted(grouped or {"shadow": []}):
        layer_trades = grouped.get(layer, [])
        stats = _strategy_stats(layer_trades)
        for s in strategies or []:
            stats.setdefault(s, {"trades": 0, "wins": 0, "win_rate": 0.0, "pnl": 0.0})

        dim_eff = _dimension_effectiveness(layer_trades)
        attr_cond = attribute_pct(layer_trades).get("by_condition", {})
        conditions_to_adjust = [c for c, pct in attr_cond.items() if pct < -0.1 and c != "unattributed"]
        strategies_to_eliminate: list[str] = []
        strategies_to_promote: list[str] = []

        for s, st in stats.items():
            wr = st["win_rate"]
            week_positive = st["pnl"] > 0
            week_below_50 = wr < 0.50
            tracked = _update_consecutive(state, f"{layer}:{s}", week_positive, week_below_50)
            if tracked.get("consecutive_below50_weeks", 0) >= 2:
                strategies_to_eliminate.append(s)
            if tracked.get("consecutive_positive_weeks", 0) >= 2:
                strategies_to_promote.append(s)

        total_pnl = sum(_safe_float(t.get("pnl")) for t in layer_trades)
        total_wins = sum(1 for t in layer_trades if _safe_float(t.get("pnl")) > 0)
        week_wr = total_wins / len(layer_trades) if layer_trades else 0.0
        capital_layer_reviews[layer] = {
            "capital_layer": layer,
            "week_pnl": round(total_pnl, 6),
            "week_win_rate": round(week_wr, 4),
            "week_trade_count": len(layer_trades),
            "strategy_win_rates": stats,
            "dimension_effectiveness": dim_eff,
            "conditions_to_adjust": conditions_to_adjust,
            "strategies_to_eliminate": strategies_to_eliminate,
            "strategies_to_promote": strategies_to_promote,
        }

    _write_json(WEEKLY_STATE, state)
    result = {
        "session": "weekly",
        "as_of": as_of,
        "capital_layer_reviews": capital_layer_reviews,
    }
    for capital_layer, layer_record in capital_layer_reviews.items():
        log_record = {"session": "weekly", "as_of": as_of, "capital_layer": capital_layer}
        log_record.update(layer_record)
        _append_log(log_record)
    return result


# ---- self-test --------------------------------------------------------------

if __name__ == "__main__":
    trades = [
        {"pnl": 0.05, "strategy": "pullback", "dimensions": {"macro": 0.6, "technical": 0.4}, "condition": "low_vol"},
        {"pnl": -0.03, "strategy": "pullback", "dimension": "technical", "condition": "low_vol"},
        {"pnl": 0.04, "strategy": "trend", "dimensions": {"technical": 1.0}, "condition": "mid_vol"},
        {"pnl": -0.06, "strategy": "event_driven", "dimension": "event", "condition": "high_vol"},
        {"pnl": -0.02, "strategy": "event_driven", "dimension": "event", "condition": "high_vol"},
    ]
    print(json.dumps(review_week(trades, strategies=["pullback", "trend", "event_driven", "breakout"]), ensure_ascii=False, indent=2))
