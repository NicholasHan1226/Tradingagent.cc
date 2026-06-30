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

import csv
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .attribution import attribute, attribute_pct
from .benchmark import compare_to_benchmark, get_benchmark, record_last_period

REVIEW_DIR = Path(__file__).resolve().parent
GOALS_PATH = REVIEW_DIR / "goals.yaml"
DAILY_LOG = REVIEW_DIR / "data" / "daily_reviews.jsonl"

ASHARE_DATA = Path("/opt/investment/Ashare/data")
RECOMMENDATIONS_DIR = ASHARE_DATA / "recommendations"
SHADOW_SIM_DIR = ASHARE_DATA / "shadow_sim"
PAPER_PORTFOLIO_DIR = ASHARE_DATA / "paper_portfolio"
TRADEBOOK_DIR = ASHARE_DATA / "tradebook"


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


def _append_layer_logs(base_record: dict[str, Any], grouped_records: dict[str, dict[str, Any]]) -> None:
    for capital_layer, layer_record in grouped_records.items():
        log_record = dict(base_record)
        log_record.update(layer_record)
        log_record["capital_layer"] = capital_layer
        _append_log(log_record)


def _preferred_capital_layer(layers: list[str]) -> str:
    priority = {"real": 0, "simulated": 1, "shadow": 2}
    return sorted(layers, key=lambda layer: priority.get(layer, 99))[0] if layers else "shadow"


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


# ---- Ashare data loaders ----------------------------------------------------

def _compact_date(value: Any) -> str:
    raw = str(value or "").strip()
    digits = "".join(ch for ch in raw if ch.isdigit())
    return digits[:8] if len(digits) >= 8 else raw


def _date_eq(value: Any, trade_date: str) -> bool:
    return bool(value) and _compact_date(value) == _compact_date(trade_date)


def _read_csv_dicts(path: Path) -> list[dict[str, Any]]:
    try:
        if not path.exists():
            return []
        with open(path, newline="", encoding="utf-8-sig") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


def _read_jsonl_dicts(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        if not path.exists():
            return rows
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    rows.append(item)
    except Exception:
        return []
    return rows


def _first_present(row: dict[str, Any], *keys: str, default: Any = "") -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return default


def _float_value(value: Any, default: float = 0.0) -> float:
    if isinstance(value, str):
        value = value.strip().rstrip("%")
    return _safe_float(value, default)


def _created_at_matches(created_at: Any, trade_date: str) -> bool:
    raw = str(created_at or "").strip()
    if not raw:
        return False
    compact = _compact_date(trade_date)
    return raw.startswith(compact) or _compact_date(raw) == compact


def _normalize_trade(row: dict[str, Any], default_layer: str = "shadow") -> dict[str, Any]:
    return {
        "ts_code": _first_present(row, "ts_code", "code", "symbol"),
        "side": _first_present(row, "side", "action", "trade_side"),
        "quantity": _float_value(_first_present(row, "quantity", "qty", "shares", default=0.0)),
        "price": _float_value(_first_present(row, "price", "fill_price", "avg_price", default=0.0)),
        "pnl": _float_value(row.get("pnl"), 0.0),
        "strategy": _first_present(row, "strategy", "strategy_name"),
        "signal_id": _first_present(row, "tradebook_id", "source_decision_id", "signal_id"),
        "created_at": _first_present(row, "created_at", "timestamp", "time"),
        "trade_date": _first_present(row, "trade_date", "date"),
        "capital_layer": _normalize_capital_layer(
            _first_present(row, "capital_layer", "capital_nature", "account_type", default=default_layer),
            default=default_layer,
        ),
    }


def _normalize_position(row: dict[str, Any], default_layer: str = "shadow") -> dict[str, Any]:
    weight_pct = _first_present(row, "weight_pct", default=None)
    if weight_pct is not None:
        weight = _float_value(weight_pct) / 100.0
    else:
        weight = _float_value(row.get("weight"), 0.0)
    return {
        "ts_code": _first_present(row, "ts_code", "code", "symbol"),
        "weight": weight,
        "pnl_pct": _float_value(_first_present(row, "pnl_pct", "unrealized_pnl_pct", default=0.0)),
        "stop_loss_pct": _float_value(row.get("stop_loss_pct"), -0.03),
        "take_profit_pct": _float_value(row.get("take_profit_pct"), 0.05),
        "momentum": _float_value(row.get("momentum"), 0.0),
        "capital_layer": _normalize_capital_layer(
            _first_present(row, "capital_layer", "capital_nature", "account_type", default=default_layer),
            default=default_layer,
        ),
    }


def _is_morning_trade(row: dict[str, Any]) -> bool:
    raw = str(row.get("created_at") or "").strip()
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) >= 12:
        hhmm = digits[8:12]
        return hhmm < "1135"
    if "T" in raw:
        time_part = raw.split("T", 1)[1][:5]
        return time_part < "11:35"
    if " " in raw:
        time_part = raw.split(" ", 1)[1][:5]
        return time_part < "11:35"
    return False


def load_recommendations(trade_date: str) -> list[dict[str, Any]]:
    try:
        path = RECOMMENDATIONS_DIR / "recommendations.csv"
        rows = _read_csv_dicts(path)
        return [
            row for row in rows
            if _date_eq(row.get("as_of_trade_date"), trade_date)
            or _date_eq(row.get("recommendation_date"), trade_date)
        ]
    except Exception:
        return []


def load_review_outcomes(trade_date: str) -> list[dict[str, Any]]:
    try:
        path = RECOMMENDATIONS_DIR / "reviews.csv"
        rows = _read_csv_dicts(path)
        return [row for row in rows if _date_eq(row.get("recommendation_date"), trade_date)]
    except Exception:
        return []


def load_shadow_trades(trade_date: str) -> list[dict[str, Any]]:
    try:
        shadow_path = SHADOW_SIM_DIR / "shadow_sim_trades.csv"
        rows = [
            _normalize_trade(row, default_layer="shadow")
            for row in _read_csv_dicts(shadow_path)
            if _date_eq(row.get("trade_date"), trade_date)
        ]
        if rows:
            return rows

        jsonl_path = TRADEBOOK_DIR / "simulated_execution_log.jsonl"
        jsonl_rows = []
        for row in _read_jsonl_dicts(jsonl_path):
            if _date_eq(row.get("trade_date"), trade_date) or _created_at_matches(row.get("created_at"), trade_date):
                jsonl_rows.append(_normalize_trade(row, default_layer="simulated"))
        return jsonl_rows
    except Exception:
        return []


def load_positions(as_of_date: str) -> list[dict[str, Any]]:
    try:
        shadow_path = SHADOW_SIM_DIR / "latest_shadow_positions.csv"
        rows = [_normalize_position(row, default_layer="shadow") for row in _read_csv_dicts(shadow_path)]
        if rows:
            return rows

        paper_path = PAPER_PORTFOLIO_DIR / "positions.csv"
        rows = [_normalize_position(row, default_layer="shadow") for row in _read_csv_dicts(paper_path)]
        return rows
    except Exception:
        return []


def load_direction_hits(trade_date: str) -> list[dict[str, Any]]:
    try:
        path = RECOMMENDATIONS_DIR / "direction_hit_reviews.csv"
        rows = _read_csv_dicts(path)
        return [row for row in rows if _date_eq(row.get("source_trade_date"), trade_date)]
    except Exception:
        return []


def run_daily_review(
    trade_date: str,
    benchmark_return: float = 0.0,
    stage: str = "stage_1_sim",
    session: str = "close",
) -> dict[str, Any]:
    try:
        session_key = str(session or "").lower()
        positions = load_positions(trade_date)
        trades = load_shadow_trades(trade_date)

        if session_key == "lunch":
            morning_trades = [trade for trade in trades if _is_morning_trade(trade)]
            result = review_lunch(positions, morning_trades)
            result["trade_date"] = trade_date
            return result

        if session_key == "close":
            review_outcomes = load_review_outcomes(trade_date)
            result = review_close(trades, positions, benchmark_return, stage=stage)
            result["trade_date"] = trade_date
            result["review_outcome_count"] = len(review_outcomes)
            return result

        return {"session": session, "error": f"unsupported session: {session}", "trade_date": trade_date}
    except Exception as e:
        return {"session": session, "error": str(e), "trade_date": trade_date}


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
    layer_positions = _group_by_capital_layer(positions)
    layer_trades = _group_by_capital_layer(morning_trades)
    layers = sorted(set(layer_positions) | set(layer_trades) or {"shadow"})

    capital_layer_reviews: dict[str, Any] = {}
    for layer in layers:
        layer_pos = layer_positions.get(layer, [])
        layer_trade = layer_trades.get(layer, [])
        signal_count = sum(1 for t in layer_trade if t.get("signal_id"))
        hit = _hit_rate(layer_trade)
        realized_pnl = _sum_pnl(layer_trade)
        floating_pnl = sum(_safe_float(p.get("pnl_pct")) * _safe_float(p.get("weight")) for p in layer_pos)
        pnl = realized_pnl + floating_pnl

        reduce_list = [
            p.get("ts_code", "?")
            for p in layer_pos
            if _safe_float(p.get("pnl_pct")) <= _safe_float(p.get("stop_loss_pct"), -0.03)
        ]
        add_list = [
            p.get("ts_code", "?")
            for p in layer_pos
            if 0 < _safe_float(p.get("pnl_pct")) < _safe_float(p.get("take_profit_pct"), 0.05)
            and _safe_float(p.get("momentum", 0)) > 0
        ]
        watch_list = [p.get("ts_code", "?") for p in layer_pos if p.get("ts_code") not in reduce_list + add_list]
        capital_layer_reviews[layer] = {
            "capital_layer": layer,
            "signal_count": signal_count,
            "hit_rate": round(hit, 4),
            "pnl": round(pnl, 6),
            "realized_pnl": round(realized_pnl, 6),
            "floating_pnl": round(floating_pnl, 6),
            "position_count": len(layer_pos),
            "morning_trade_count": len(layer_trade),
            "afternoon_plan": {
                "reduce": reduce_list,
                "add": add_list,
                "watch": watch_list,
                "notes": "午盘复盘: 检查上午信号兑现度, 止损标的减仓, 未兑现信号加仓.",
            },
        }

    result = {
        "session": "lunch",
        "as_of": _now_iso(),
        "capital_layer_reviews": capital_layer_reviews,
    }
    _append_layer_logs({"session": "lunch", "as_of": result["as_of"]}, capital_layer_reviews)
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
    layer_positions = _group_by_capital_layer(positions)
    layer_trades = _group_by_capital_layer(all_trades)
    layers = sorted(set(layer_positions) | set(layer_trades) or {"shadow"})
    goals = _load_goals()
    stage_goals = _stage_goals(goals, stage)

    capital_layer_reviews: dict[str, Any] = {}
    for layer in layers:
        layer_pos = layer_positions.get(layer, [])
        layer_trd = layer_trades.get(layer, [])
        wins = [t for t in layer_trd if _safe_float(t.get("pnl")) > 0]
        losses = [t for t in layer_trd if _safe_float(t.get("pnl")) < 0]
        pnl = _sum_pnl(layer_trd)
        floating = sum(_safe_float(p.get("pnl_pct")) * _safe_float(p.get("weight")) for p in layer_pos)
        total_pnl = pnl + floating
        win_rate = len(wins) / len(layer_trd) if layer_trd else 0.0
        avg_win = (_sum_pnl(wins) / len(wins)) if wins else 0.0
        avg_loss = (_sum_pnl(losses) / len(losses)) if losses else 0.0
        profit_factor = (abs(_sum_pnl(wins)) / abs(_sum_pnl(losses))) if losses and _sum_pnl(losses) != 0 else float("inf") if wins else 0.0
        trades_summary = {
            "count": len(layer_trd),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(win_rate, 4),
            "avg_win": round(avg_win, 6),
            "avg_loss": round(avg_loss, 6),
            "profit_factor": round(profit_factor, 6) if profit_factor != float("inf") else None,
            "realized_pnl": round(pnl, 6),
            "floating_pnl": round(floating, 6),
        }
        attr = attribute_pct(layer_trd)
        bench_cmp = compare_to_benchmark(
            total_pnl, benchmark_return, portfolio_returns_series, benchmark_returns_series
        )
        bench_info = get_benchmark(datetime.now(timezone.utc).strftime("%Y%m%d"))
        last_period_return = _safe_float(bench_info.get("last_period_return"))
        vs_last = {
            "this_period_return": round(total_pnl, 6),
            "last_period_return": round(last_period_return, 6),
            "improved": total_pnl > last_period_return,
            "delta": round(total_pnl - last_period_return, 6),
        }
        metrics_for_goals = {
            "win_rate": win_rate,
            "sharpe": bench_cmp.get("sharpe"),
            "max_drawdown": bench_cmp.get("max_drawdown"),
            "stage": stage,
        }
        vs_goals = _compare_to_goals(metrics_for_goals, stage_goals)
        bleeding_dims = [
            d for d, pct in attr.get("by_dimension", {}).items()
            if pct < -0.1 and d != "unattributed"
        ]
        bleeding_strats = [
            s for s, pct in attr.get("by_strategy", {}).items()
            if pct < -0.1 and s != "unattributed"
        ]
        capital_layer_reviews[layer] = {
            "capital_layer": layer,
            "trades_summary": trades_summary,
            "pnl": round(total_pnl, 6),
            "attribution": attr,
            "comparisons": {
                "vs_goals": vs_goals,
                "vs_benchmark": bench_cmp,
                "vs_last_period": vs_last,
            },
            "next_day_plan": {
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
            },
        }

    result = {
        "session": "close",
        "as_of": _now_iso(),
        "capital_layer_reviews": capital_layer_reviews,
    }
    baseline_layer = _preferred_capital_layer(list(capital_layer_reviews))
    record_last_period(capital_layer_reviews[baseline_layer]["pnl"], "daily")
    _append_layer_logs({"session": "close", "as_of": result["as_of"]}, capital_layer_reviews)
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
