#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Weekly evidence review — Friday close.

The report can flag manual-review and demotion candidates, but it never changes
strategy lifecycle, increases risk, or upgrades simulation to real trading.
Money remains inside one explicit account scope and native currency. Market,
capital-layer, and all-market records aggregate counts only.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shared.governance.market_lanes import canonical_runtime_market

from .attribution import attribute_pct
from .pnl_summary import sim_ledger_pnl_summary
from .sample_quality import strategy_valid_trades
from .sim_ledger_reader import DEFAULT_LOCAL_SIM_TRADES, DEFAULT_SIM_LEDGER_ROOT

REVIEW_DIR = Path(__file__).resolve().parent
WEEKLY_LOG = REVIEW_DIR / "data" / "weekly_reviews.jsonl"
WEEKLY_STATE = REVIEW_DIR / "data" / "weekly_state.json"  # tracks consecutive weeks
MARKET_CURRENCIES = {
    "ashare": "CNY",
    "cn_futures": "CNY",
    "crypto": "USDT",
}
UNSCOPED_ACCOUNT_KEY = "__unscoped__"


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


def _ledger_by_market(
    ledger_pnl_summary: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Project ledger money per exact account without market-level totals."""
    projected: dict[str, dict[str, Any]] = {}
    for raw_market, raw_ledger in sorted(ledger_pnl_summary.items()):
        market = canonical_runtime_market(raw_market)
        ledger = raw_ledger if isinstance(raw_ledger, dict) else {}
        raw_accounts = ledger.get("account_summaries")
        accounts = raw_accounts if isinstance(raw_accounts, dict) else {}
        account_summaries: dict[str, dict[str, Any]] = {}
        for raw_scope, raw_account in sorted(accounts.items()):
            scope = _normalize_account_scope(raw_scope)
            if scope is None or not isinstance(raw_account, dict):
                continue
            account_summaries[scope] = {
                "market": market,
                "currency": _market_currency(market),
                "capital_layer": "simulated",
                "account_scope": scope,
                "realized_pnl": round(_safe_float(raw_account.get("realized_pnl")), 6),
                "unrealized_pnl": round(
                    _safe_float(raw_account.get("unrealized_pnl")), 6
                ),
                "total_pnl": round(_safe_float(raw_account.get("total_pnl")), 6),
                "market_value": round(_safe_float(raw_account.get("market_value")), 6),
                "open_position_count": int(raw_account.get("open_position_count") or 0),
                "missing_mark_count": int(raw_account.get("missing_mark_count") or 0),
                "source": raw_account.get("pnl_source") or None,
                "mark_authority": raw_account.get("mark_authority") or None,
            }
        projected[market] = {
            "market": market,
            "currency": _market_currency(market),
            "capital_layer": "simulated",
            "account_scope": None,
            "account_count": len(account_summaries),
            "account_summaries": account_summaries,
            "open_position_count": sum(
                int(account["open_position_count"])
                for account in account_summaries.values()
            ),
            "missing_mark_count": sum(
                int(account["missing_mark_count"])
                for account in account_summaries.values()
            ),
            "source": None,
            "mark_authority": None,
            "monetary_aggregation": "forbidden_across_accounts",
        }
    return projected


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


def _update_consecutive(
    state: dict[str, Any], strategy: str, week_positive: bool, week_below_50: bool
) -> dict[str, Any]:
    """Track consecutive weeks for manual review/demotion evidence."""
    s = state.setdefault("strategies", {}).setdefault(strategy, {})
    # Positive streaks only nominate manual review; they never auto-promote.
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


def review_week(
    week_trades: list[dict[str, Any]], strategies: list[str] | None = None
) -> dict[str, Any]:
    """Friday review.

    Args:
        week_trades: all trades for the week (each carries strategy/dimension/condition/pnl).
        strategies: known strategy ids (to include strategies with zero trades this week).

    Returns market -> capital-layer -> account reviews plus
    ``ledger_by_market``. Market, capital-layer, and all-market records contain
    only counts; monetary fields require an explicit account scope.
    """
    grouped = _group_by_market_capital_layer_account(week_trades)
    state = _read_json(WEEKLY_STATE)
    as_of = _now_iso()
    ledger_pnl_summary = sim_ledger_pnl_summary(
        ledger_root=DEFAULT_SIM_LEDGER_ROOT,
        local_trades_path=DEFAULT_LOCAL_SIM_TRADES,
    )
    ledger_by_market = _ledger_by_market(ledger_pnl_summary)
    market_reviews: dict[str, Any] = {}
    all_layer_counts: dict[str, dict[str, Any]] = {}

    for market, capital_layers in sorted(grouped.items()):
        layer_reviews: dict[str, Any] = {}
        for layer, account_rows in sorted(capital_layers.items()):
            account_reviews: dict[str, dict[str, Any]] = {}
            layer_trade_count = 0
            explicit_account_count = 0
            unscoped_trade_count = 0

            for account_key, raw_account_trades in sorted(account_rows.items()):
                account_scope = (
                    None if account_key == UNSCOPED_ACCOUNT_KEY else account_key
                )
                account_trades = (
                    strategy_valid_trades(raw_account_trades)
                    if layer == "simulated"
                    else raw_account_trades
                )
                total_wins = sum(
                    1 for trade in account_trades if _safe_float(trade.get("pnl")) > 0
                )
                layer_trade_count += len(account_trades)

                if account_scope is None:
                    unscoped_trade_count += len(account_trades)
                    account_reviews[account_key] = {
                        "market": market,
                        "currency": _market_currency(market),
                        "capital_layer": layer,
                        "account_scope": None,
                        "week_trade_count": len(account_trades),
                        "monetary_state": "unavailable_missing_account_scope",
                        "review_state": "count_only",
                        "reason": "explicit_account_scope_required",
                        "automatic_promotion_enabled": False,
                        "automatic_risk_expansion_enabled": False,
                    }
                    continue

                explicit_account_count += 1
                stats = _strategy_stats(account_trades)
                for strategy in strategies or []:
                    stats.setdefault(
                        strategy,
                        {"trades": 0, "wins": 0, "win_rate": 0.0, "pnl": 0.0},
                    )
                dim_eff = _dimension_effectiveness(account_trades)
                attr_cond = attribute_pct(account_trades).get("by_condition", {})
                conditions_to_adjust = [
                    condition
                    for condition, pct in attr_cond.items()
                    if pct < -0.1 and condition != "unattributed"
                ]
                strategies_to_eliminate: list[str] = []
                strategies_for_manual_review: list[str] = []
                for strategy, strategy_stats in stats.items():
                    tracked = _update_consecutive(
                        state,
                        f"{market}:{layer}:{account_scope}:{strategy}",
                        strategy_stats["pnl"] > 0,
                        strategy_stats["win_rate"] < 0.50,
                    )
                    if tracked.get("consecutive_below50_weeks", 0) >= 2:
                        strategies_to_eliminate.append(strategy)
                    if tracked.get("consecutive_positive_weeks", 0) >= 2:
                        strategies_for_manual_review.append(strategy)

                total_pnl = sum(
                    _safe_float(trade.get("pnl")) for trade in account_trades
                )
                account_reviews[account_scope] = {
                    "market": market,
                    "currency": _market_currency(market),
                    "capital_layer": layer,
                    "account_scope": account_scope,
                    "week_pnl": round(total_pnl, 6),
                    "week_win_rate": (
                        round(total_wins / len(account_trades), 4)
                        if account_trades
                        else 0.0
                    ),
                    "week_trade_count": len(account_trades),
                    "strategy_win_rates": stats,
                    "dimension_effectiveness": dim_eff,
                    "conditions_to_adjust": conditions_to_adjust,
                    "strategies_to_eliminate": strategies_to_eliminate,
                    "strategies_to_promote": [],
                    "strategies_for_manual_review": strategies_for_manual_review,
                    "automatic_promotion_enabled": False,
                    "automatic_risk_expansion_enabled": False,
                    "monetary_state": "available",
                }

            layer_reviews[layer] = {
                "market": market,
                "currency": _market_currency(market),
                "capital_layer": layer,
                "account_count": explicit_account_count,
                "unscoped_trade_count": unscoped_trade_count,
                "week_trade_count": layer_trade_count,
                "monetary_aggregation": "forbidden_across_accounts",
                "account_reviews": account_reviews,
            }
            count_summary = all_layer_counts.setdefault(
                layer,
                {
                    "capital_layer": layer,
                    "market_count": 0,
                    "markets": [],
                    "week_trade_count": 0,
                    "account_count": 0,
                    "unscoped_trade_count": 0,
                    "monetary_aggregation": "forbidden",
                },
            )
            count_summary["market_count"] += 1
            count_summary["markets"].append(market)
            count_summary["week_trade_count"] += layer_trade_count
            count_summary["account_count"] += explicit_account_count
            count_summary["unscoped_trade_count"] += unscoped_trade_count

        market_reviews[market] = {
            "market": market,
            "currency": _market_currency(market),
            "capital_layer_reviews": layer_reviews,
        }

    _write_json(WEEKLY_STATE, state)
    reviewed_trade_count = sum(
        int(summary["week_trade_count"]) for summary in all_layer_counts.values()
    )
    result = {
        "session": "weekly",
        "as_of": as_of,
        "all_markets": {
            "market_count": len(market_reviews),
            "capital_layer_count": len(all_layer_counts),
            "week_trade_count": reviewed_trade_count,
            "open_position_count": sum(
                int(ledger["open_position_count"])
                for ledger in ledger_by_market.values()
            ),
            "missing_mark_count": sum(
                int(ledger["missing_mark_count"])
                for ledger in ledger_by_market.values()
            ),
            "monetary_aggregation": "forbidden",
        },
        "capital_layer_reviews": all_layer_counts,
        "market_reviews": market_reviews,
        "ledger_by_market": ledger_by_market,
    }
    for market_record in market_reviews.values():
        for layer_record in market_record["capital_layer_reviews"].values():
            for account_record in layer_record["account_reviews"].values():
                _append_log({"session": "weekly", "as_of": as_of, **account_record})
    return result


# ---- self-test --------------------------------------------------------------

if __name__ == "__main__":
    trades = [
        {
            "market": "ashare",
            "pnl": 0.05,
            "strategy": "pullback",
            "dimensions": {"macro": 0.6, "technical": 0.4},
            "condition": "low_vol",
        },
        {
            "market": "ashare",
            "pnl": -0.03,
            "strategy": "pullback",
            "dimension": "technical",
            "condition": "low_vol",
        },
        {
            "market": "ashare",
            "pnl": 0.04,
            "strategy": "trend",
            "dimensions": {"technical": 1.0},
            "condition": "mid_vol",
        },
        {
            "market": "ashare",
            "pnl": -0.06,
            "strategy": "event_driven",
            "dimension": "event",
            "condition": "high_vol",
        },
        {
            "market": "ashare",
            "pnl": -0.02,
            "strategy": "event_driven",
            "dimension": "event",
            "condition": "high_vol",
        },
    ]
    print(
        json.dumps(
            review_week(
                trades, strategies=["pullback", "trend", "event_driven", "breakout"]
            ),
            ensure_ascii=False,
            indent=2,
        )
    )
