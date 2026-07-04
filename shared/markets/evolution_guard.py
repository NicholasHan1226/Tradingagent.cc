#!/usr/bin/env python3
"""Guardrails for simulated style evolution.

The guard watches simulated performance evidence and blocks evolution when the
recent record says the system should stop learning from bad feedback.
"""

from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shared.markets.performance_tracker import StylePerformance, load_history


TRADINGAGENT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REVIEW_ROOT = TRADINGAGENT_ROOT / "shared" / "review"
MARKETS = ("crypto", "pm", "us")
PORTFOLIO_INITIAL_CAPITAL = float(os.environ.get("EVOLUTION_GUARD_INITIAL_CAPITAL", "100.0"))
MAX_DRAWDOWN_LIMIT = float(os.environ.get("EVOLUTION_GUARD_MAX_DRAWDOWN", "-0.20"))
RECOVERY_DRAWDOWN_LEVEL = float(os.environ.get("EVOLUTION_GUARD_RECOVERY_DRAWDOWN", "-0.10"))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _review_root(review_root: Path | str | None = None) -> Path:
    return Path(review_root) if review_root is not None else DEFAULT_REVIEW_ROOT


def guard_state_path(review_root: Path | str | None = None) -> Path:
    return _review_root(review_root) / "evolution_guard_state.json"


def sim_halt_path(review_root: Path | str | None = None) -> Path:
    return _review_root(review_root) / "SIM_HALT.json"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def load_market_rows(
    markets: tuple[str, ...] = MARKETS,
    *,
    days: int = 30,
    review_root: Path | str | None = None,
) -> list[StylePerformance]:
    rows: list[StylePerformance] = []
    for market in markets:
        rows.extend(load_history(market, days=days, review_root=review_root))
    return rows


def _daily_style_pnl(rows: list[StylePerformance]) -> dict[str, dict[str, float]]:
    by_day_style: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for row in rows:
        key = f"{row.market}:{row.style_name}"
        by_day_style[row.date][key] += row.pnl
    return {date: dict(values) for date, values in by_day_style.items()}


def _daily_market_pnl(rows: list[StylePerformance]) -> dict[str, dict[str, float]]:
    by_day_market: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for row in rows:
        by_day_market[row.date][row.market] += row.pnl
    return {date: dict(values) for date, values in by_day_market.items()}


def all_styles_losing_today(rows: list[StylePerformance]) -> dict[str, Any]:
    by_day_style = _daily_style_pnl(rows)
    if not by_day_style:
        return {"triggered": False, "reason": "no_performance_history"}
    latest_date = max(by_day_style)
    style_pnls = by_day_style[latest_date]
    if not style_pnls:
        return {"triggered": False, "date": latest_date, "reason": "no_style_rows"}
    triggered = all(pnl < 0 for pnl in style_pnls.values())
    return {
        "triggered": triggered,
        "date": latest_date,
        "style_count": len(style_pnls),
        "style_pnls": style_pnls,
    }


def portfolio_drawdown(rows: list[StylePerformance]) -> dict[str, Any]:
    by_day = defaultdict(float)
    max_row_dd = 0.0
    for row in rows:
        by_day[row.date] += row.pnl
        max_row_dd = max(max_row_dd, row.max_dd)
    if not by_day:
        return {"triggered": False, "drawdown": 0.0, "reason": "no_performance_history"}
    nav = PORTFOLIO_INITIAL_CAPITAL
    peak = nav
    worst = 0.0
    series = []
    for date in sorted(by_day):
        nav += by_day[date]
        peak = max(peak, nav)
        dd = (nav - peak) / peak if peak > 0 else 0.0
        worst = min(worst, dd)
        series.append({"date": date, "pnl": round(by_day[date], 6), "nav": round(nav, 6), "drawdown": round(dd, 6)})
    row_dd_triggered = max_row_dd >= abs(MAX_DRAWDOWN_LIMIT)
    portfolio_triggered = worst <= MAX_DRAWDOWN_LIMIT
    return {
        "triggered": portfolio_triggered or row_dd_triggered,
        "drawdown": round(min(worst, -max_row_dd if row_dd_triggered else worst), 6),
        "portfolio_drawdown": round(worst, 6),
        "max_style_drawdown": round(max_row_dd, 6),
        "limit": MAX_DRAWDOWN_LIMIT,
        "series": series,
    }


def market_recovered(rows: list[StylePerformance], previous_state: dict[str, Any]) -> dict[str, Any]:
    if not previous_state.get("weights_frozen"):
        return {"triggered": False, "reason": "not_frozen"}
    dd = portfolio_drawdown(rows)
    by_day = defaultdict(float)
    for row in rows:
        by_day[row.date] += row.pnl
    if not by_day:
        return {"triggered": False, "reason": "no_performance_history"}
    latest = max(by_day)
    latest_pnl = by_day[latest]
    triggered = latest_pnl > 0 and float(dd.get("portfolio_drawdown", 0.0)) > RECOVERY_DRAWDOWN_LEVEL
    return {
        "triggered": triggered,
        "date": latest,
        "latest_pnl": round(latest_pnl, 6),
        "portfolio_drawdown": dd.get("portfolio_drawdown"),
        "recovery_level": RECOVERY_DRAWDOWN_LEVEL,
    }


def three_consecutive_all_market_losses(
    rows: list[StylePerformance],
    markets: tuple[str, ...] = MARKETS,
) -> dict[str, Any]:
    by_day_market = _daily_market_pnl(rows)
    if len(by_day_market) < 3:
        return {"triggered": False, "reason": "less_than_three_days"}
    dates = sorted(by_day_market)[-3:]
    required = {market.lower() for market in markets}
    details = []
    for date in dates:
        values = {market.lower(): pnl for market, pnl in by_day_market[date].items()}
        covered = required.intersection(values)
        all_losing = bool(covered) and all(values[market] < 0 for market in covered)
        details.append({"date": date, "market_pnls": values, "covered_markets": sorted(covered), "all_losing": all_losing})
    triggered = all(item["all_losing"] for item in details)
    return {"triggered": triggered, "dates": dates, "details": details}


def _notify_circuit_breaker(result: dict[str, Any]) -> dict[str, Any]:
    try:
        from shared.notify.email_sender import send_template_email  # noqa: WPS433

        return send_template_email(
            "emergency_alert",
            {
                "alert_type": "strategy_invalidation",
                "severity": "critical",
                "description": "多市场 simulated 连续三天同时亏损，已写入模拟 halt 文件。",
                "impact": {
                    "affected_systems": "Crypto/PM/US/HK simulated evolution",
                    "potential_loss": "继续自演化可能放大错误反馈",
                    "affected_strategies": "all simulated styles",
                },
                "self_heal": {
                    "action": "halt simulated evolution and require review",
                    "started_at": _now_iso(),
                    "status": "failed",
                    "estimated_time": "manual review required",
                },
                "need_human": True,
                "summary": json.dumps(result, ensure_ascii=False),
            },
            channel="system",
            subject="[TradingAgent] simulated circuit breaker halted",
        )
    except Exception as exc:  # noqa: BLE001
        return {"status": "failed", "error": str(exc)}


def evaluate_guard(
    markets: tuple[str, ...] = MARKETS,
    *,
    review_root: Path | str | None = None,
    days: int = 30,
    notify: bool = False,
) -> dict[str, Any]:
    """Evaluate guardrails and persist the current guard state."""

    root = _review_root(review_root)
    previous = _read_json(guard_state_path(root))
    rows = load_market_rows(markets, days=days, review_root=root)
    all_losing = all_styles_losing_today(rows)
    drawdown = portfolio_drawdown(rows)
    recovered = market_recovered(rows, previous)
    circuit = three_consecutive_all_market_losses(rows, markets)

    evolution_paused = bool(all_losing.get("triggered"))
    weights_frozen = bool(drawdown.get("triggered"))
    if recovered.get("triggered"):
        weights_frozen = False
    sim_halted = bool(circuit.get("triggered"))

    actions: list[dict[str, Any]] = []
    if evolution_paused:
        actions.append({"action": "pause_evolution", "reason": "all_styles_losing_money", "date": all_losing.get("date")})
    if weights_frozen:
        actions.append({"action": "freeze_style_weights", "reason": "portfolio_drawdown_limit", "drawdown": drawdown.get("drawdown")})
    if recovered.get("triggered"):
        actions.append({"action": "thaw_style_weights", "reason": "market_recovered", "date": recovered.get("date")})
    if sim_halted:
        halt_payload = {
            "halted_at": _now_iso(),
            "reason": "three_consecutive_daily_losses_across_all_markets",
            "capital_layer": "simulated",
            "account_type": "simulated",
            "real_execution": False,
            "circuit_breaker": circuit,
        }
        _write_json(sim_halt_path(root), halt_payload)
        action = {"action": "halt_sim", "path": str(sim_halt_path(root)), "reason": halt_payload["reason"]}
        if notify:
            action["notification"] = _notify_circuit_breaker(halt_payload)
        actions.append(action)

    state = {
        "generated_at": _now_iso(),
        "capital_layer": "simulated",
        "account_type": "simulated",
        "real_execution": False,
        "markets": list(markets),
        "evolution_paused": evolution_paused,
        "weights_frozen": weights_frozen,
        "sim_halted": sim_halted or bool(previous.get("sim_halted")),
        "all_styles_losing": all_losing,
        "drawdown": drawdown,
        "recovery": recovered,
        "circuit_breaker": circuit,
        "actions": actions,
    }
    _write_json(guard_state_path(root), state)
    _append_jsonl(root / "evolution_guard.jsonl", state)
    return state


def guard_allows_evolution(*, review_root: Path | str | None = None) -> dict[str, Any]:
    state = _read_json(guard_state_path(review_root))
    blocked = bool(state.get("sim_halted") or state.get("evolution_paused") or state.get("weights_frozen"))
    return {
        "allowed": not blocked,
        "state": state,
        "reason": "guard_clear" if not blocked else "evolution_guard_blocked",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate simulated evolution guardrails")
    parser.add_argument("--markets", default=",".join(MARKETS))
    parser.add_argument("--review-root")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--notify", action="store_true")
    args = parser.parse_args()

    markets = tuple(item.strip().lower() for item in args.markets.split(",") if item.strip())
    result = evaluate_guard(markets, review_root=args.review_root, days=args.days, notify=args.notify)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("sim_halted"):
        raise SystemExit(2)


__all__ = [
    "all_styles_losing_today",
    "evaluate_guard",
    "guard_allows_evolution",
    "market_recovered",
    "portfolio_drawdown",
    "three_consecutive_all_market_losses",
]


if __name__ == "__main__":
    main()
