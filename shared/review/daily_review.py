#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Daily review — 2x per trading day.

  - lunch  review @ 11:35 (morning session close): hit-rate, pnl, afternoon plan
  - close  review @ 15:30 (market close)         : full summary, attribution, next-day plan

Implements the 3-comparison framework:
  1. actual vs expected goals   (goals.yaml current stage)
  2. actual vs benchmark        (CSI300, via benchmark.compare_to_benchmark)
  3. actual vs last period      (yesterday's portfolio return, via benchmark store)

Attribution delegates to attribution.attribute().
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from attribution import attribute, attribute_pct
from benchmark import compare_to_benchmark, get_benchmark, record_last_period

REVIEW_DIR = Path(__file__).resolve().parent
GOALS_PATH = REVIEW_DIR / "goals.yaml"
DAILY_LOG = REVIEW_DIR / "data" / "daily_reviews.jsonl"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ensure_dirs() -> None:
    DAILY_LOG.parent.mkdir(parents=True, exist_ok=True)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        f = float(v)
        return default if f != f else f
    except (TypeError, ValueError):
        return default


def _load_goals() -> dict[str, Any]:
    """Minimal YAML loader: goals.yaml is flat enough to parse without pyyaml.
    Falls back to {} if unparseable. If pyyaml is available, prefer it.
    """
    try:
        import yaml  # type: ignore
        with open(GOALS_PATH, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        # very small fallback parser: only handles top-level "key: value" + indented blocks
        goals: dict[str, Any] = {}
        if not GOALS_PATH.exists():
            return goals
        cur: str | None = None
        for line in GOALS_PATH.read_text(encoding="utf-8").splitlines():
            raw = line.rstrip()
            if not raw or raw.lstrip().startswith("#"):
                continue
            indent = len(raw) - len(raw.lstrip())
            stripped = raw.strip()
            if ":" not in stripped:
                continue
            k, _, v = stripped.partition(":")
            k, v = k.strip(), v.strip()
            if indent == 0:
                goals[k] = {}
                cur = k
            elif cur is not None:
                # store as string; caller can coerce
                try:
                    goals[cur][k] = float(v) if v and v[0].isdigit() else (True if v == "true" else False if v == "false" else v)
                except (ValueError, AttributeError):
                    goals[cur][k] = v
        return goals


def _append_log(record: dict[str, Any]) -> None:
    _ensure_dirs()
    with open(DAILY_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _hit_rate(trades: list[dict[str, Any]]) -> float:
    if not trades:
        return 0.0
    wins = sum(1 for t in trades if _safe_float(t.get("pnl")) > 0)
    return wins / len(trades)


def _sum_pnl(trades: list[dict[str, Any]]) -> float:
    return sum(_safe_float(t.get("pnl")) for t in trades)


def _stage_goals(goals: dict[str, Any], stage: str = "stage_1_sim") -> dict[str, Any]:
    return goals.get(stage, {})


def _compare_to_goals(metrics: dict[str, Any], stage_goals: dict[str, Any]) -> dict[str, Any]:
    """Comparison #1: actual vs expected goals."""
    checks: list[dict[str, Any]] = []
    wr = metrics.get("win_rate", 0.0)
    g_wr = stage_goals.get("win_rate")
    if g_wr is not None:
        checks.append({"metric": "win_rate", "actual": wr, "goal": g_wr, "met": wr >= g_wr})

    sh = metrics.get("sharpe")
    g_sh = stage_goals.get("sharpe")
    if sh is not None and g_sh is not None:
        checks.append({"metric": "sharpe", "actual": sh, "goal": g_sh, "met": sh >= g_sh})

    mdd = metrics.get("max_drawdown")
    g_mdd = stage_goals.get("max_drawdown")
    if mdd is not None and g_mdd is not None:
        # drawdown is "bad when high", so met means actual <= goal
        checks.append({"metric": "max_drawdown", "actual": mdd, "goal": g_mdd, "met": mdd <= g_mdd})

    mr = metrics.get("monthly_return")
    if stage_goals.get("monthly_return") == "positive" and mr is not None:
        checks.append({"metric": "monthly_return", "actual": mr, "goal": "positive", "met": mr > 0})

    all_met = all(c["met"] for c in checks) if checks else False
    return {"stage": metrics.get("stage", "stage_1_sim"), "checks": checks, "all_goals_met": all_met}


# ---- lunch review -----------------------------------------------------------

def review_lunch(positions: list[dict[str, Any]], morning_trades: list[dict[str, Any]]) -> dict[str, Any]:
    """Lunch review @ 11:35.

    Args:
        positions: current open positions [{ts_code, weight, pnl_pct, ...}].
        morning_trades: trades executed in the morning session.

    Returns:
        {
          "session": "lunch",
          "signal_count": int,           # 上午产生信号数
          "hit_rate": float,             # 上午交易胜率
          "pnl": float,                  # 上午已实现+浮动盈亏( decimal )
          "afternoon_plan": {            # 下午行动建议
            "reduce": [...],             # 减仓标的( 触及止损/动量衰减 )
            "add": [...],                # 加仓标的( 信号未充分兑现 )
            "watch": [...],              # 观察标的
            "notes": str,
          },
        }
    """
    signal_count = sum(1 for t in morning_trades if t.get("signal_id"))
    hit = _hit_rate(morning_trades)
    realized_pnl = _sum_pnl(morning_trades)
    floating_pnl = sum(_safe_float(p.get("pnl_pct")) * _safe_float(p.get("weight")) for p in positions)
    pnl = realized_pnl + floating_pnl

    # afternoon plan heuristics
    reduce_list = [
        p.get("ts_code", "?")
        for p in positions
        if _safe_float(p.get("pnl_pct")) <= _safe_float(p.get("stop_loss_pct"), -0.03)
    ]
    add_list = [
        p.get("ts_code", "?")
        for p in positions
        if 0 < _safe_float(p.get("pnl_pct")) < _safe_float(p.get("take_profit_pct"), 0.05)
        and _safe_float(p.get("momentum", 0)) > 0
    ]
    watch_list = [p.get("ts_code", "?") for p in positions if p.get("ts_code") not in reduce_list + add_list]

    result = {
        "session": "lunch",
        "as_of": _now_iso(),
        "signal_count": signal_count,
        "hit_rate": round(hit, 4),
        "pnl": round(pnl, 6),
        "realized_pnl": round(realized_pnl, 6),
        "floating_pnl": round(floating_pnl, 6),
        "position_count": len(positions),
        "morning_trade_count": len(morning_trades),
        "afternoon_plan": {
            "reduce": reduce_list,
            "add": add_list,
            "watch": watch_list,
            "notes": "午盘复盘: 检查上午信号兑现度, 止损标的减仓, 未兑现信号加仓.",
        },
    }
    _append_log(result)
    return result


# ---- close review -----------------------------------------------------------

def review_close(
    all_trades: list[dict[str, Any]],
    positions: list[dict[str, Any]],
    benchmark_return: float,
    *,
    portfolio_returns_series: list[float] | None = None,
    benchmark_returns_series: list[float] | None = None,
    stage: str = "stage_1_sim",
) -> dict[str, Any]:
    """Close review @ 15:30.

    Implements all 3 comparisons + attribution.

    Args:
        all_trades: all trades for the day.
        positions: end-of-day positions.
        benchmark_return: CSI300 return for the day (decimal).
        portfolio_returns_series: optional daily-return history (for sharpe/beta/mdd).
        benchmark_returns_series: optional benchmark daily-return history.
        stage: current stage key in goals.yaml.

    Returns:
        {
          "session": "close",
          "trades_summary": {...},
          "pnl": float,
          "attribution": {...},          # from attribution.attribute_pct
          "comparisons": {
            "vs_goals": {...},           # comparison #1
            "vs_benchmark": {...},       # comparison #2
            "vs_last_period": {...},     # comparison #3
          },
          "next_day_plan": {...},
        }
    """
    # --- trades summary ---
    wins = [t for t in all_trades if _safe_float(t.get("pnl")) > 0]
    losses = [t for t in all_trades if _safe_float(t.get("pnl")) < 0]
    pnl = _sum_pnl(all_trades)
    floating = sum(_safe_float(p.get("pnl_pct")) * _safe_float(p.get("weight")) for p in positions)
    total_pnl = pnl + floating
    win_rate = len(wins) / len(all_trades) if all_trades else 0.0
    avg_win = (_sum_pnl(wins) / len(wins)) if wins else 0.0
    avg_loss = (_sum_pnl(losses) / len(losses)) if losses else 0.0
    profit_factor = (abs(_sum_pnl(wins)) / abs(_sum_pnl(losses))) if losses and _sum_pnl(losses) != 0 else float("inf") if wins else 0.0

    trades_summary = {
        "count": len(all_trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(win_rate, 4),
        "avg_win": round(avg_win, 6),
        "avg_loss": round(avg_loss, 6),
        "profit_factor": round(profit_factor, 6) if profit_factor != float("inf") else None,
        "realized_pnl": round(pnl, 6),
        "floating_pnl": round(floating, 6),
    }

    # --- attribution ---
    attr = attribute_pct(all_trades)

    # --- comparison #2: vs benchmark ---
    bench_cmp = compare_to_benchmark(
        total_pnl, benchmark_return, portfolio_returns_series, benchmark_returns_series
    )

    # --- comparison #3: vs last period ---
    bench_info = get_benchmark(datetime.now(timezone.utc).strftime("%Y%m%d"))
    last_period_return = _safe_float(bench_info.get("last_period_return"))
    vs_last = {
        "this_period_return": round(total_pnl, 6),
        "last_period_return": round(last_period_return, 6),
        "improved": total_pnl > last_period_return,
        "delta": round(total_pnl - last_period_return, 6),
    }

    # --- comparison #1: vs goals ---
    goals = _load_goals()
    stage_goals = _stage_goals(goals, stage)
    metrics_for_goals = {
        "win_rate": win_rate,
        "sharpe": bench_cmp.get("sharpe"),
        "max_drawdown": bench_cmp.get("max_drawdown"),
        "stage": stage,
    }
    vs_goals = _compare_to_goals(metrics_for_goals, stage_goals)

    # --- next day plan ---
    # Heuristics: if win rate below stage goal → tighten; if beat benchmark → maintain;
    # if attribution shows a dimension bleeding → reduce that dimension's weight.
    bleeding_dims = [
        d for d, pct in attr.get("by_dimension", {}).items()
        if pct < -0.1 and d != "unattributed"
    ]
    bleeding_strats = [
        s for s, pct in attr.get("by_strategy", {}).items()
        if pct < -0.1 and s != "unattributed"
    ]
    next_day_plan = {
        "tighten_stops": win_rate < stage_goals.get("win_rate", 0.55),
        "reduce_dimensions": bleeding_dims,
        "reduce_strategies": bleeding_strats,
        "maintain_signal": bool(vs_goals.get("all_goals_met")),
        "notes": (
            f"收盘复盘: 胜率{win_rate:.1%}, "
            f"{'达标' if vs_goals.get('all_goals_met') else '未达标'}. "
            f"出血维度: {bleeding_dims or '无'}. "
            f"下日重点: {'维持信号' if vs_goals.get('all_goals_met') else '收紧止损+降权出血维度'}."
        ),
    }

    result = {
        "session": "close",
        "as_of": _now_iso(),
        "trades_summary": trades_summary,
        "pnl": round(total_pnl, 6),
        "attribution": attr,
        "comparisons": {
            "vs_goals": vs_goals,
            "vs_benchmark": bench_cmp,
            "vs_last_period": vs_last,
        },
        "next_day_plan": next_day_plan,
    }

    # record this period's return for the next review's "vs last period"
    record_last_period(total_pnl, "daily")
    _append_log(result)
    return result


# ---- self-test --------------------------------------------------------------

if __name__ == "__main__":
    pos = [
        {"ts_code": "000001.SZ", "weight": 0.3, "pnl_pct": 0.02, "stop_loss_pct": -0.03, "take_profit_pct": 0.05, "momentum": 0.5},
        {"ts_code": "600519.SH", "weight": 0.4, "pnl_pct": -0.04, "stop_loss_pct": -0.03, "take_profit_pct": 0.08, "momentum": -0.2},
    ]
    morning = [
        {"ts_code": "000001.SZ", "pnl": 0.02, "signal_id": "s1", "dimensions": {"macro": 0.6, "technical": 0.4}, "strategy": "pullback", "condition": "low_vol"},
        {"ts_code": "600519.SH", "pnl": -0.01, "signal_id": "s2", "dimension": "event", "strategy": "event_driven", "condition": "high_vol"},
    ]
    print("=== LUNCH ===")
    print(json.dumps(review_lunch(pos, morning), ensure_ascii=False, indent=2))
    print("\n=== CLOSE ===")
    all_t = morning + [{"ts_code": "300750.SZ", "pnl": 0.03, "dimensions": {"technical": 1.0}, "strategy": "trend", "condition": "mid_vol"}]
    print(json.dumps(review_close(all_t, pos, benchmark_return=0.005, stage="stage_1_sim"), ensure_ascii=False, indent=2))
