#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Daily review — 2x per trading day.

  - lunch  review @ 11:35 (morning session close): hit-rate, pnl, afternoon plan
  - close  review @ 15:30 (market close)         : full summary, attribution, next-day plan

Implements the 3-comparison framework:
  1. actual vs expected goals   (goals.yaml current stage)
  2. actual vs benchmark        (CSI300, via benchmark.compare_to_benchmark)
  3. actual vs last period      (disabled until a market-scoped store exists)

Attribution delegates to attribution.attribute(). Monetary and return fields
exist only inside one explicit ``market + capital_layer + account_scope``
review with native currency. Parent summaries contain counts/health only.
"""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shared.governance.market_lanes import canonical_runtime_market

from shared.accounting import position_ledger

try:
    from shared.data.reader import TradingagentDataReader
except Exception:  # pragma: no cover - optional upstream dependency
    TradingagentDataReader = None  # type: ignore[assignment]

from .attribution import attribute_pct
from .benchmark import compare_to_benchmark
from .pnl_summary import sim_ledger_pnl_summary
from .sample_quality import strategy_valid_trades, summarize_sample_quality
from .sim_ledger_reader import (
    DEFAULT_LOCAL_SIM_TRADES,
    DEFAULT_SIM_LEDGER_ROOT,
    load_sim_trades_for_date,
    summarize_trade_sources,
)

SharedSignalsReader = TradingagentDataReader

REVIEW_DIR = Path(__file__).resolve().parent
TRADINGAGENT_ROOT = REVIEW_DIR.parent.parent
SHARED_DIR = REVIEW_DIR.parent
GOALS_PATH = REVIEW_DIR / "goals.yaml"
DAILY_LOG = REVIEW_DIR / "data" / "daily_reviews.jsonl"
DIRECTION_HIT_LOG = REVIEW_DIR / "data" / "direction_hit_reviews.jsonl"
SHADOW_TRADES_LOG = SHARED_DIR / "logs" / "shadow" / "shadow_trades.jsonl"
FILLED_SIGNALS_DIR = TRADINGAGENT_ROOT / "signals" / "filled"
EXECUTION_EXCLUSION_ROOT = REVIEW_DIR
MARKET_CURRENCIES = {
    "ashare": "CNY",
    "cn_futures": "CNY",
    "crypto": "USDT",
}
UNSCOPED_ACCOUNT_KEY = "__unscoped__"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _today_compact() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


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
                    goals[cur][k] = (
                        float(v)
                        if v and v[0].isdigit()
                        else (True if v == "true" else False if v == "false" else v)
                    )
                except (ValueError, AttributeError):
                    goals[cur][k] = v
        return goals


def _append_log(record: dict[str, Any]) -> None:
    _ensure_dirs()
    with open(DAILY_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _append_direction_hit_log(record: dict[str, Any]) -> None:
    DIRECTION_HIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(DIRECTION_HIT_LOG, "a", encoding="utf-8") as f:
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


def _normalize_market(value: Any, default: str = "unknown") -> str:
    raw = str(value or default).strip().lower()
    return raw or default


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


def _row_account_scope(row: dict[str, Any]) -> str | None:
    """Return only an explicit account identifier; never derive from market/layer."""
    for key in ("account_scope", "account_id", "capital_authority_id", "account"):
        scope = _normalize_account_scope(row.get(key))
        if scope is not None:
            return scope
    return None


def _normalize_side(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"buy", "long", "open_long"}:
        return "buy"
    if raw in {"sell", "short", "reduce", "close_long"}:
        return "sell"
    return raw


def _group_by_capital_layer_market_account(
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
        account_scope = _row_account_scope(row)
        normalized = dict(row)
        normalized["capital_layer"] = layer
        normalized["market"] = market
        normalized["account_scope"] = account_scope
        grouped[layer][market][account_scope or UNSCOPED_ACCOUNT_KEY].append(normalized)
    return {
        layer: {market: dict(accounts) for market, accounts in markets.items()}
        for layer, markets in grouped.items()
    }


def _append_market_layer_logs(
    base_record: dict[str, Any], grouped_records: dict[str, dict[str, Any]]
) -> None:
    for capital_layer, layer_record in grouped_records.items():
        market_reviews = layer_record.get("market_reviews") or {}
        if not market_reviews:
            log_record = dict(base_record)
            log_record.update(layer_record)
            log_record["capital_layer"] = capital_layer
            log_record["market"] = None
            log_record["currency"] = None
            _append_log(log_record)
            continue
        for market, market_record in market_reviews.items():
            account_reviews = market_record.get("account_reviews") or {}
            if not account_reviews:
                log_record = dict(base_record)
                log_record.update(market_record)
                log_record["capital_layer"] = capital_layer
                log_record["market"] = market
                log_record["account_scope"] = None
                _append_log(log_record)
                continue
            for account_record in account_reviews.values():
                log_record = dict(base_record)
                log_record.update(account_record)
                log_record["capital_layer"] = capital_layer
                log_record["market"] = market
                _append_log(log_record)


def _preferred_capital_layer(layers: list[str]) -> str:
    priority = {"real": 0, "simulated": 1, "shadow": 2}
    return (
        sorted(layers, key=lambda layer: priority.get(layer, 99))[0]
        if layers
        else "shadow"
    )


def _hit_rate(trades: list[dict[str, Any]]) -> float:
    if not trades:
        return 0.0
    wins = sum(1 for t in trades if _safe_float(t.get("pnl")) > 0)
    return wins / len(trades)


def _sum_pnl(trades: list[dict[str, Any]]) -> float:
    return sum(_safe_float(t.get("pnl")) for t in trades)


def _load_execution_exclusions(
    trade_date: str,
) -> dict[str, dict[str, dict[str, dict[str, Any]]]]:
    compact = _compact_date(trade_date)
    grouped: dict[str, dict[str, dict[str, dict[str, Any]]]] = defaultdict(
        lambda: defaultdict(
            lambda: defaultdict(
                lambda: {
                    "total": 0,
                    "skipped_candidates": 0,
                    "risk_rejections": 0,
                    "execution_skips": 0,
                    "sample": [],
                }
            )
        )
    )
    for path in sorted(
        EXECUTION_EXCLUSION_ROOT.glob(f"*/execution_exclusions_{compact}.jsonl")
    ):
        for row in _read_jsonl_dicts(path):
            if not isinstance(row, dict):
                continue
            row_date = _first_present(
                row, "date", "trade_date", "generated_at", default=""
            )
            if row_date and not _date_eq(row_date, compact):
                continue
            layer = _normalize_capital_layer(
                row.get("capital_layer"), default="simulated"
            )
            market = _normalize_market(row.get("market"), default=path.parent.name)
            account_scope = _row_account_scope(row)
            kind = str(row.get("kind") or "").strip()
            bucket = grouped[layer][market][account_scope or UNSCOPED_ACCOUNT_KEY]
            bucket["total"] += 1
            if kind == "skipped_candidate":
                bucket["skipped_candidates"] += 1
            elif kind == "risk_rejection":
                bucket["risk_rejections"] += 1
            elif kind == "execution_skip":
                bucket["execution_skips"] += 1
            if len(bucket["sample"]) < 5:
                bucket["sample"].append(
                    {
                        "kind": kind,
                        "symbol": _first_present(row, "symbol", "ts_code", default=""),
                        "reason": _first_present(row, "reason", "reasons", default=""),
                    }
                )
    return {
        layer: {market: dict(accounts) for market, accounts in markets.items()}
        for layer, markets in grouped.items()
    }


def _execution_quality(
    exclusions: dict[str, dict[str, dict[str, dict[str, Any]]]],
    layer: str,
    market: str,
    account_scope: str,
) -> dict[str, Any]:
    record = exclusions.get(layer, {}).get(market, {}).get(account_scope, {})
    return {
        "exclusion_count": int(record.get("total", 0) or 0),
        "skipped_candidates": int(record.get("skipped_candidates", 0) or 0),
        "risk_rejections": int(record.get("risk_rejections", 0) or 0),
        "execution_skips": int(record.get("execution_skips", 0) or 0),
        "sample": record.get("sample", [])
        if isinstance(record.get("sample"), list)
        else [],
    }


def _stage_goals(goals: dict[str, Any], stage: str = "stage_1_sim") -> dict[str, Any]:
    return goals.get(stage, {})


def _compare_to_goals(
    metrics: dict[str, Any], stage_goals: dict[str, Any]
) -> dict[str, Any]:
    """Comparison #1: actual vs expected goals."""
    checks: list[dict[str, Any]] = []
    wr = metrics.get("win_rate", 0.0)
    g_wr = stage_goals.get("win_rate")
    if g_wr is not None:
        checks.append(
            {"metric": "win_rate", "actual": wr, "goal": g_wr, "met": wr >= g_wr}
        )

    sh = metrics.get("sharpe")
    g_sh = stage_goals.get("sharpe")
    if sh is not None and g_sh is not None:
        checks.append(
            {"metric": "sharpe", "actual": sh, "goal": g_sh, "met": sh >= g_sh}
        )

    mdd = metrics.get("max_drawdown")
    g_mdd = stage_goals.get("max_drawdown")
    if mdd is not None and g_mdd is not None:
        # drawdown is "bad when high", so met means actual <= goal
        checks.append(
            {
                "metric": "max_drawdown",
                "actual": mdd,
                "goal": g_mdd,
                "met": mdd <= g_mdd,
            }
        )

    mr = metrics.get("monthly_return")
    if stage_goals.get("monthly_return") == "positive" and mr is not None:
        checks.append(
            {
                "metric": "monthly_return",
                "actual": mr,
                "goal": "positive",
                "met": mr > 0,
            }
        )

    all_met = all(c["met"] for c in checks) if checks else False
    return {
        "stage": metrics.get("stage", "stage_1_sim"),
        "checks": checks,
        "all_goals_met": all_met,
    }


# ---- tradingagent data loaders --------------------------------------------------


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


def _normalize_trade(
    row: dict[str, Any], default_layer: str = "shadow"
) -> dict[str, Any]:
    return {
        "ts_code": _first_present(row, "ts_code", "code", "symbol"),
        "side": _first_present(row, "side", "action", "trade_side"),
        "quantity": _float_value(
            _first_present(row, "quantity", "qty", "shares", default=0.0)
        ),
        "price": _float_value(
            _first_present(row, "price", "fill_price", "avg_price", default=0.0)
        ),
        "pnl": _float_value(row.get("pnl"), 0.0),
        "strategy": _first_present(row, "strategy", "strategy_name"),
        "signal_id": _first_present(
            row, "tradebook_id", "source_decision_id", "signal_id"
        ),
        "created_at": _first_present(row, "created_at", "timestamp", "time"),
        "trade_date": _first_present(row, "trade_date", "date"),
        "market": _normalize_market(
            _first_present(row, "market", "asset_class", default="unknown")
        ),
        "capital_layer": _normalize_capital_layer(
            _first_present(
                row,
                "capital_layer",
                "capital_nature",
                "account_type",
                default=default_layer,
            ),
            default=default_layer,
        ),
        "account_scope": _row_account_scope(row),
    }


def _normalize_position(
    row: dict[str, Any], default_layer: str = "shadow"
) -> dict[str, Any]:
    weight_pct = _first_present(row, "weight_pct", default=None)
    if weight_pct is not None:
        weight = _float_value(weight_pct) / 100.0
    else:
        weight = _float_value(row.get("weight"), 0.0)
    return {
        "ts_code": _first_present(row, "ts_code", "code", "symbol"),
        "weight": weight,
        "pnl_pct": _float_value(
            _first_present(row, "pnl_pct", "unrealized_pnl_pct", default=0.0)
        ),
        "stop_loss_pct": _float_value(row.get("stop_loss_pct"), -0.03),
        "take_profit_pct": _float_value(row.get("take_profit_pct"), 0.05),
        "momentum": _float_value(row.get("momentum"), 0.0),
        "market": _normalize_market(
            _first_present(row, "market", "asset_class", default="unknown")
        ),
        "capital_layer": _normalize_capital_layer(
            _first_present(
                row,
                "capital_layer",
                "capital_nature",
                "account_type",
                default=default_layer,
            ),
            default=default_layer,
        ),
        "account_scope": _row_account_scope(row),
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


def _read_signal_fills(trade_date: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        for path in sorted(FILLED_SIGNALS_DIR.glob("*.json")):
            try:
                row = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if not isinstance(row, dict):
                continue
            if not (
                _date_eq(row.get("trade_date"), trade_date)
                or _created_at_matches(row.get("filled_at"), trade_date)
            ):
                continue
            normalized = _normalize_trade(
                {
                    "ts_code": _first_present(row, "ts_code", "symbol"),
                    "side": _first_present(row, "direction", "side", "action"),
                    "quantity": _first_present(
                        row, "filled_quantity", "filled_qty", "quantity", default=0
                    ),
                    "price": _first_present(row, "filled_price", "price", default=0),
                    "signal_id": _first_present(
                        row, "order_id", "idempotency_key", "signal_id"
                    ),
                    "created_at": _first_present(
                        row, "filled_at", "fill_time", "timestamp"
                    ),
                    "trade_date": _first_present(row, "trade_date", "valid_until"),
                    "market": _first_present(
                        row, "market", "asset_class", default="unknown"
                    ),
                    "capital_layer": _first_present(
                        row, "capital_layer", "account_type", default="shadow"
                    ),
                    "account_scope": _first_present(
                        row,
                        "account_scope",
                        "account_id",
                        "capital_authority_id",
                        "account",
                        default=None,
                    ),
                },
                default_layer="shadow",
            )
            rows.append(normalized)
    except Exception:
        return []
    return rows


def _merge_market_review(
    market_reviews: dict[str, dict[str, Any]],
    market: str,
    layer: str,
    review: dict[str, Any],
) -> None:
    entry = market_reviews.setdefault(
        market,
        {
            "market": market,
            "currency": _market_currency(market),
            "capital_layers": [],
            "capital_layer_reviews": {},
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "account_count": 0,
            "unscoped_trade_count": 0,
            "monetary_aggregation": "forbidden_across_accounts_and_capital_layers",
        },
    )
    if layer not in entry["capital_layers"]:
        entry["capital_layers"].append(layer)
    entry["capital_layer_reviews"][layer] = review
    trades = int(review.get("trades", review.get("morning_trade_count", 0)) or 0)
    wins = int(review.get("wins", 0) or 0)
    losses = int(review.get("losses", 0) or 0)
    entry["trades"] += trades
    entry["wins"] += wins
    entry["losses"] += losses
    entry["account_count"] += int(review.get("account_count") or 0)
    entry["unscoped_trade_count"] += int(review.get("unscoped_trade_count") or 0)
    entry["win_rate"] = (
        round(entry["wins"] / entry["trades"], 4) if entry["trades"] else 0.0
    )
    entry["stale"] = entry["trades"] == 0


def _ledger_account_summary(
    ledger_pnl_summary: dict[str, dict[str, Any]],
    market: str,
    account_scope: str,
) -> dict[str, Any] | None:
    market_ledger = ledger_pnl_summary.get(market)
    if not isinstance(market_ledger, dict):
        return None
    accounts = market_ledger.get("account_summaries")
    if not isinstance(accounts, dict):
        return None
    account = accounts.get(account_scope)
    return dict(account) if isinstance(account, dict) else None


def _summarize_market_accounts(
    market: str,
    layer: str,
    account_reviews: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Build a count-only market/layer container over account reviews."""
    return {
        "market": market,
        "currency": _market_currency(market),
        "capital_layer": layer,
        "account_count": sum(
            1 for review in account_reviews.values() if review.get("account_scope")
        ),
        "unscoped_trade_count": sum(
            int(review.get("trades") or 0)
            for review in account_reviews.values()
            if review.get("account_scope") is None
        ),
        "unscoped_position_count": sum(
            int(review.get("position_count") or 0)
            for review in account_reviews.values()
            if review.get("account_scope") is None
        ),
        "trades": sum(
            int(review.get("trades") or 0) for review in account_reviews.values()
        ),
        "strategy_trades": sum(
            int(review.get("strategy_trades") or 0)
            for review in account_reviews.values()
        ),
        "validation_sample_count": sum(
            int(review.get("validation_sample_count") or 0)
            for review in account_reviews.values()
        ),
        "position_count": sum(
            int(review.get("position_count") or 0)
            for review in account_reviews.values()
        ),
        "wins": sum(
            int(review.get("wins") or 0) for review in account_reviews.values()
        ),
        "losses": sum(
            int(review.get("losses") or 0) for review in account_reviews.values()
        ),
        "stale": not any(
            int(review.get("trades") or 0) for review in account_reviews.values()
        ),
        "monetary_aggregation": "forbidden_across_accounts",
        "account_reviews": account_reviews,
    }


def _unscoped_account_review(
    market: str,
    layer: str,
    positions: list[dict[str, Any]],
    trades: list[dict[str, Any]],
) -> dict[str, Any]:
    """Retain health/count evidence while suppressing all monetary analysis."""
    strategy_trades = strategy_valid_trades(trades)
    sample_quality = summarize_sample_quality(trades)
    return {
        "market": market,
        "currency": _market_currency(market),
        "capital_layer": layer,
        "account_scope": None,
        "trades": len(trades),
        "strategy_trades": len(strategy_trades),
        "validation_sample_count": int(
            sample_quality.get("validation_sample_count") or 0
        ),
        "sample_quality": sample_quality,
        "position_count": len(positions),
        "wins": sum(
            1 for trade in strategy_trades if _safe_float(trade.get("pnl")) > 0
        ),
        "losses": sum(
            1 for trade in strategy_trades if _safe_float(trade.get("pnl")) < 0
        ),
        "stale": not trades,
        "monetary_state": "unavailable_missing_account_scope",
        "review_state": "count_only",
        "reason": "explicit_account_scope_required",
    }


def _summarize_capital_layer(
    layer: str,
    market_reviews: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Aggregate only non-monetary review health across markets."""
    trades = sum(int(review.get("trades") or 0) for review in market_reviews.values())
    strategy_trades = sum(
        int(review.get("strategy_trades") or 0) for review in market_reviews.values()
    )
    wins = sum(int(review.get("wins") or 0) for review in market_reviews.values())
    losses = sum(int(review.get("losses") or 0) for review in market_reviews.values())
    validation_samples = sum(
        int(review.get("validation_sample_count") or 0)
        for review in market_reviews.values()
    )
    positions = sum(
        int(review.get("position_count") or 0) for review in market_reviews.values()
    )
    account_count = sum(
        int(review.get("account_count") or 0) for review in market_reviews.values()
    )
    unscoped_trade_count = sum(
        int(review.get("unscoped_trade_count") or 0)
        for review in market_reviews.values()
    )
    unscoped_position_count = sum(
        int(review.get("unscoped_position_count") or 0)
        for review in market_reviews.values()
    )
    return {
        "capital_layer": layer,
        "market_count": len(market_reviews),
        "markets": sorted(market_reviews),
        "trade_count": trades,
        "strategy_trade_count": strategy_trades,
        "validation_sample_count": validation_samples,
        "position_count": positions,
        "account_count": account_count,
        "unscoped_trade_count": unscoped_trade_count,
        "unscoped_position_count": unscoped_position_count,
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / strategy_trades, 4) if strategy_trades else 0.0,
        "stale": trades == 0,
        "monetary_aggregation": "forbidden",
        "market_reviews": market_reviews,
    }


def _all_markets_health(
    capital_layer_reviews: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    markets = {
        market
        for review in capital_layer_reviews.values()
        for market in review.get("markets", [])
    }
    return {
        "market_count": len(markets),
        "capital_layer_count": len(capital_layer_reviews),
        "trade_count": sum(
            int(review.get("trade_count") or 0)
            for review in capital_layer_reviews.values()
        ),
        "strategy_trade_count": sum(
            int(review.get("strategy_trade_count") or 0)
            for review in capital_layer_reviews.values()
        ),
        "validation_sample_count": sum(
            int(review.get("validation_sample_count") or 0)
            for review in capital_layer_reviews.values()
        ),
        "position_count": sum(
            int(review.get("position_count") or 0)
            for review in capital_layer_reviews.values()
        ),
        "account_count": sum(
            int(review.get("account_count") or 0)
            for review in capital_layer_reviews.values()
        ),
        "unscoped_trade_count": sum(
            int(review.get("unscoped_trade_count") or 0)
            for review in capital_layer_reviews.values()
        ),
        "unscoped_position_count": sum(
            int(review.get("unscoped_position_count") or 0)
            for review in capital_layer_reviews.values()
        ),
        "monetary_aggregation": "forbidden",
    }


def load_shadow_trades(trade_date: str) -> list[dict[str, Any]]:
    rows = [
        _normalize_trade(row, default_layer="shadow")
        for row in _read_jsonl_dicts(SHADOW_TRADES_LOG)
        if _date_eq(row.get("trade_date"), trade_date)
        or _created_at_matches(row.get("created_at"), trade_date)
    ]
    return rows or _read_signal_fills(trade_date)


def _dedupe_trade_key(row: dict[str, Any]) -> tuple[str, str, str, str]:
    layer = _normalize_capital_layer(row.get("capital_layer"))
    market = _normalize_market(row.get("market"))
    account_scope = _row_account_scope(row) or UNSCOPED_ACCOUNT_KEY
    identifiers = (
        row.get("order_id"),
        row.get("signal_id"),
        row.get("fill_id"),
        row.get("trade_id"),
        row.get("idempotency_key"),
    )
    for value in identifiers:
        key = str(value or "").strip()
        if key:
            return market, layer, account_scope, key
    fallback = "|".join(
        str(row.get(key, "") or "")
        for key in (
            "market",
            "ts_code",
            "side",
            "quantity",
            "price",
            "created_at",
            "trade_date",
        )
    )
    return market, layer, account_scope, fallback


def _dedupe_trades(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str, str]] = set()
    deduped: list[dict[str, Any]] = []
    for row in rows:
        key = _dedupe_trade_key(row)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


def _normalize_review_trade(row: dict[str, Any], default_layer: str) -> dict[str, Any]:
    normalized = dict(row)
    normalized.update(_normalize_trade(row, default_layer=default_layer))
    return normalized


def load_review_trades(trade_date: str) -> list[dict[str, Any]]:
    """Load all trade fills that should feed reviews.

    Keep load_shadow_trades() backward compatible while the review/report layer
    consumes both legacy shadow fills and simulated ledger fills.
    """
    sim_trades = [
        _normalize_review_trade(row, default_layer="simulated")
        for row in load_sim_trades_for_date(trade_date)
    ]
    shadow_trades = load_shadow_trades(trade_date)
    return _dedupe_trades(sim_trades + shadow_trades)


def review_trade_source_counts(trades: list[dict[str, Any]]) -> dict[str, Any]:
    layer_counts: dict[str, int] = {}
    for trade in trades:
        layer = _normalize_capital_layer(trade.get("capital_layer"))
        layer_counts[layer] = layer_counts.get(layer, 0) + 1
    return {
        "total": len(trades),
        "by_capital_layer": layer_counts,
        "by_source": summarize_trade_sources(trades),
        "sample_quality": summarize_sample_quality(trades),
    }


def load_positions(as_of_date: str) -> list[dict[str, Any]]:
    try:
        return [
            _normalize_position(row, default_layer="shadow")
            for row in position_ledger.get_positions(capital_layer="all")
            if not row.get("entry_date")
            or _compact_date(row.get("entry_date")) <= _compact_date(as_of_date)
        ]
    except Exception:
        return []


def _reader_symbol(symbol: Any) -> str:
    raw = str(symbol or "").strip()
    return raw.split(".", 1)[0] if "." in raw else raw


def _close_from_rows(rows: list[dict[str, Any]], trade_date: str) -> float:
    candidates = [
        row
        for row in rows
        if _date_eq(_first_present(row, "trade_date", "date", "bar_date"), trade_date)
    ]
    if not candidates:
        candidates = rows
    for row in reversed(candidates):
        close = _float_value(
            _first_present(row, "close", "adj_close", "price", default=0.0), 0.0
        )
        if close > 0:
            return close
    return 0.0


def _load_close_price(
    market: str, symbol: str, trade_date: str, reader: Any = None
) -> float:
    if not symbol:
        return 0.0
    if reader is None:
        reader_cls = SharedSignalsReader or TradingagentDataReader
        if reader_cls is None:
            return 0.0
        try:
            reader = reader_cls()
        except Exception:
            return 0.0
    get_bars = getattr(reader, "get_bars_daily", None)
    if not callable(get_bars):
        return 0.0

    symbols = [symbol]
    stripped = _reader_symbol(symbol)
    if stripped and stripped not in symbols:
        symbols.append(stripped)
    markets = (
        [market, market.capitalize(), "Ashare"]
        if market and market != "unknown"
        else ["Ashare"]
    )

    for market_key in markets:
        for symbol_key in symbols:
            try:
                rows = get_bars(market_key, symbol_key, trade_date, trade_date)
            except Exception:
                rows = []
            close = _close_from_rows(
                [row for row in rows if isinstance(row, dict)], trade_date
            )
            if close > 0:
                return close
    return 0.0


def _direction_hit_review(
    trade: dict[str, Any], close_price: float, trade_date: str
) -> dict[str, Any] | None:
    account_scope = _row_account_scope(trade)
    if account_scope is None:
        # Direction return is a return-bearing observation and therefore cannot
        # be published without an explicit account boundary.
        return None
    side = _normalize_side(trade.get("side"))
    if side not in {"buy", "sell"}:
        return None
    entry_price = _float_value(trade.get("price"), 0.0)
    if entry_price <= 0 or close_price <= 0:
        return None
    raw_return = (close_price - entry_price) / entry_price
    hit = raw_return > 0 if side == "buy" else raw_return < 0
    direction_return = raw_return if side == "buy" else -raw_return
    return {
        "trade_date": _compact_date(trade_date),
        "ts_code": trade.get("ts_code", ""),
        "side": side,
        "entry_price": round(entry_price, 6),
        "close_price": round(close_price, 6),
        "raw_return": round(raw_return, 6),
        "direction_return": round(direction_return, 6),
        "hit": bool(hit),
        "capital_layer": _normalize_capital_layer(trade.get("capital_layer")),
        "market": _normalize_market(trade.get("market")),
        "account_scope": account_scope,
        "strategy": trade.get("strategy", ""),
        "signal_id": trade.get("signal_id", ""),
    }


def load_direction_hits(trade_date: str) -> list[dict[str, Any]]:
    """Review trade direction against same-day close.

    Buy + close higher is a hit; sell/reduce + close lower is a hit. The
    output is grouped by capital_layer, market, and account_scope, then written as review
    evidence under shared/review/data/.
    """
    trades = strategy_valid_trades(load_review_trades(trade_date))
    close_cache: dict[tuple[str, str], float] = {}
    reviews: list[dict[str, Any]] = []

    for trade in trades:
        market = _normalize_market(trade.get("market"))
        symbol = str(trade.get("ts_code") or "").strip()
        cache_key = (market, symbol)
        if cache_key not in close_cache:
            close_cache[cache_key] = _load_close_price(market, symbol, trade_date)
        review = _direction_hit_review(trade, close_cache[cache_key], trade_date)
        if review is not None:
            reviews.append(review)

    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for review in reviews:
        grouped[
            (review["capital_layer"], review["market"], review["account_scope"])
        ].append(review)

    records: list[dict[str, Any]] = []
    as_of = _now_iso()
    for (capital_layer, market, account_scope), items in sorted(grouped.items()):
        hits = sum(1 for item in items if item.get("hit"))
        evaluated = len(items)
        record = {
            "review_type": "direction_hit_review",
            "trade_date": _compact_date(trade_date),
            "as_of": as_of,
            "capital_layer": capital_layer,
            "market": market,
            "account_scope": account_scope,
            "evaluated_count": evaluated,
            "hits": hits,
            "misses": evaluated - hits,
            "direction_accuracy": round(hits / evaluated, 4) if evaluated else 0.0,
            "reviews": items,
        }
        _append_direction_hit_log(record)
        records.append(record)
    return records


def run_daily_review(
    trade_date: str,
    benchmark_return: float | None = None,
    stage: str = "stage_1_sim",
    session: str = "close",
) -> dict[str, Any]:
    try:
        session_key = str(session or "").lower()
        positions = load_positions(trade_date)
        trades = load_review_trades(trade_date)
        source_counts = review_trade_source_counts(trades)

        if session_key == "lunch":
            morning_trades = [trade for trade in trades if _is_morning_trade(trade)]
            result = review_lunch(
                positions,
                morning_trades,
                benchmark_return=benchmark_return,
                stage=stage,
                trade_date=trade_date,
            )
            result["trade_date"] = trade_date
            result["capital_layer"] = _preferred_capital_layer(
                list(result.get("capital_layer_reviews") or {})
            )
            result["review_trade_count"] = int(
                (result.get("all_markets") or {}).get("trade_count") or 0
            )
            result["stale"] = result["review_trade_count"] == 0
            result["source_trade_counts"] = review_trade_source_counts(morning_trades)
            return result

        if session_key == "close":
            result = review_close(
                trades, positions, benchmark_return, stage=stage, trade_date=trade_date
            )
            direction_hit_reviews = load_direction_hits(trade_date)
            result["trade_date"] = trade_date
            result["direction_hit_reviews"] = direction_hit_reviews
            result["review_outcome_count"] = len(direction_hit_reviews)
            result["capital_layer"] = _preferred_capital_layer(
                list(result.get("capital_layer_reviews") or {})
            )
            result["review_trade_count"] = int(
                (result.get("all_markets") or {}).get("trade_count") or 0
            )
            result["stale"] = result["review_trade_count"] == 0
            result["source_trade_counts"] = source_counts
            return result

        return {
            "session": session,
            "error": f"unsupported session: {session}",
            "trade_date": trade_date,
        }
    except Exception as e:
        return {"session": session, "error": str(e), "trade_date": trade_date}


# ---- lunch review -----------------------------------------------------------


def review_lunch(
    positions: list[dict[str, Any]],
    morning_trades: list[dict[str, Any]],
    *,
    benchmark_return: float | None = None,
    stage: str = "stage_1_sim",
    trade_date: str | None = None,
) -> dict[str, Any]:
    """Lunch review @ 11:35.

    Args:
        positions: current open positions [{ts_code, weight, pnl_pct, ...}].
        morning_trades: trades executed in the morning session.

    Returns market/capital-layer reviews with native currency. ``all_markets``
    and top-level capital-layer records expose only non-monetary counts/health.
    """
    layer_market_positions = _group_by_capital_layer_market_account(positions)
    layer_market_trades = _group_by_capital_layer_market_account(morning_trades)
    layers = sorted(
        set(layer_market_positions) | set(layer_market_trades) or {"shadow"}
    )

    ledger_pnl_summary: dict[str, dict[str, Any]] = {}
    if "simulated" in layers:
        ledger_pnl_summary = sim_ledger_pnl_summary(
            ledger_root=DEFAULT_SIM_LEDGER_ROOT,
            local_trades_path=DEFAULT_LOCAL_SIM_TRADES,
        )
    execution_exclusions = _load_execution_exclusions(trade_date or _today_compact())

    goals = _load_goals()
    stage_goals = _stage_goals(goals, stage)

    def build_review(
        layer: str,
        market: str,
        account_scope: str,
        layer_pos: list[dict[str, Any]],
        layer_trade: list[dict[str, Any]],
        ledger_pnl: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        strategy_trade = strategy_valid_trades(layer_trade)
        sample_quality = summarize_sample_quality(layer_trade)
        signal_count = sum(1 for t in strategy_trade if t.get("signal_id"))
        hit = _hit_rate(strategy_trade)
        realized_pnl = _sum_pnl(strategy_trade)
        floating_pnl = sum(
            _safe_float(p.get("pnl_pct")) * _safe_float(p.get("weight"))
            for p in layer_pos
        )
        pnl = realized_pnl + floating_pnl
        ledger = ledger_pnl if isinstance(ledger_pnl, dict) else None
        currency = _market_currency(market)

        def ledger_amount(key: str) -> float | None:
            if ledger is None or ledger.get(key) is None:
                return None
            return round(_safe_float(ledger.get(key)), 6)

        reduce_list = [
            p.get("ts_code", "?")
            for p in layer_pos
            if _safe_float(p.get("pnl_pct"))
            <= _safe_float(p.get("stop_loss_pct"), -0.03)
        ]
        add_list = [
            p.get("ts_code", "?")
            for p in layer_pos
            if 0
            < _safe_float(p.get("pnl_pct"))
            < _safe_float(p.get("take_profit_pct"), 0.05)
            and _safe_float(p.get("momentum", 0)) > 0
        ]
        watch_list = [
            p.get("ts_code", "?")
            for p in layer_pos
            if p.get("ts_code") not in reduce_list + add_list
        ]
        attr = attribute_pct(strategy_trade)
        # The scalar benchmark input is the A-share CSI300 return. It cannot be
        # reused for futures or crypto, and no market-scoped last-period store
        # currently exists.
        market_benchmark_return = benchmark_return if market == "ashare" else None
        bench_cmp = compare_to_benchmark(pnl, market_benchmark_return)
        vs_goals = _compare_to_goals(
            {
                "win_rate": hit,
                "sharpe": bench_cmp.get("sharpe"),
                "max_drawdown": bench_cmp.get("max_drawdown"),
                "stage": stage,
            },
            stage_goals,
        )
        next_plan = {
            "reduce": reduce_list,
            "add": add_list,
            "watch": watch_list,
            "notes": "午盘复盘: 检查上午信号兑现度, 止损标的减仓, 未兑现信号加仓.",
        }
        wins = sum(1 for t in strategy_trade if _safe_float(t.get("pnl")) > 0)
        losses = sum(1 for t in strategy_trade if _safe_float(t.get("pnl")) < 0)
        return {
            "capital_layer": layer,
            "market": market,
            "currency": currency,
            "account_scope": account_scope,
            "trades": len(layer_trade),
            "strategy_trades": len(strategy_trade),
            "validation_sample_count": int(
                sample_quality.get("validation_sample_count", 0)
            ),
            "sample_quality": sample_quality,
            "wins": wins,
            "losses": losses,
            "win_rate": round(hit, 4),
            "signal_count": signal_count,
            "pnl": round(pnl, 6),
            "realized_pnl": round(realized_pnl, 6),
            "floating_pnl": round(floating_pnl, 6),
            "ledger_realized_pnl": ledger_amount("realized_pnl"),
            "ledger_unrealized_pnl": ledger_amount("unrealized_pnl"),
            "ledger_total_pnl": ledger_amount("total_pnl"),
            "ledger_strategy_realized_pnl": ledger_amount("strategy_realized_pnl"),
            "ledger_strategy_unrealized_pnl": ledger_amount("strategy_unrealized_pnl"),
            "ledger_strategy_total_pnl": ledger_amount("strategy_total_pnl"),
            "ledger_market_value": ledger_amount("market_value"),
            "ledger_strategy_market_value": ledger_amount("strategy_market_value"),
            "ledger_open_position_count": (
                int(ledger.get("open_position_count") or 0)
                if ledger is not None
                else None
            ),
            "ledger_strategy_open_position_count": (
                int(ledger.get("strategy_open_position_count") or 0)
                if ledger is not None
                else None
            ),
            "ledger_missing_mark_count": (
                int(ledger.get("missing_mark_count") or 0)
                if ledger is not None
                else None
            ),
            "ledger_pnl_source": ledger.get("pnl_source") if ledger else None,
            "ledger_mark_authority": (
                ledger.get("mark_authority") or None if ledger else None
            ),
            "ledger_state": (
                str(ledger.get("monetary_state") or "available")
                if ledger is not None
                else "unavailable"
            ),
            "position_count": len(layer_pos),
            "morning_trade_count": len(layer_trade),
            "stale": not layer_trade,
            "attribution": attr,
            "comparisons": {
                "vs_goals": vs_goals,
                "vs_benchmark": bench_cmp,
                "vs_last_period": {
                    "status": "unavailable",
                    "reason": "market_scoped_last_period_authority_unavailable",
                    "this_period_return": None,
                    "last_period_return": None,
                    "improved": None,
                    "delta": None,
                },
            },
            "afternoon_plan": next_plan,
            "next_plan": next_plan,
            "execution_quality": _execution_quality(
                execution_exclusions, layer, market, account_scope
            ),
        }

    capital_layer_reviews: dict[str, Any] = {}
    market_reviews: dict[str, dict[str, Any]] = {}
    for layer in layers:
        layer_markets = sorted(
            set(layer_market_positions.get(layer, {}))
            | set(layer_market_trades.get(layer, {}))
        )
        layer_market_reviews: dict[str, dict[str, Any]] = {}
        for market in layer_markets:
            position_accounts = layer_market_positions.get(layer, {}).get(market, {})
            trade_accounts = layer_market_trades.get(layer, {}).get(market, {})
            account_reviews: dict[str, dict[str, Any]] = {}
            for account_key in sorted(set(position_accounts) | set(trade_accounts)):
                account_positions = position_accounts.get(account_key, [])
                account_trades = trade_accounts.get(account_key, [])
                if account_key == UNSCOPED_ACCOUNT_KEY:
                    account_reviews[account_key] = _unscoped_account_review(
                        market, layer, account_positions, account_trades
                    )
                    continue
                account_reviews[account_key] = build_review(
                    layer,
                    market,
                    account_key,
                    account_positions,
                    account_trades,
                    ledger_pnl=(
                        _ledger_account_summary(ledger_pnl_summary, market, account_key)
                        if layer == "simulated"
                        else None
                    ),
                )
            market_review = _summarize_market_accounts(market, layer, account_reviews)
            layer_market_reviews[market] = market_review
            _merge_market_review(market_reviews, market, layer, market_review)
        capital_layer_reviews[layer] = _summarize_capital_layer(
            layer, layer_market_reviews
        )

    all_markets = _all_markets_health(capital_layer_reviews)

    result = {
        "session": "lunch",
        "as_of": _now_iso(),
        "capital_layer": _preferred_capital_layer(list(capital_layer_reviews)),
        "stale": int(all_markets["trade_count"]) == 0,
        "all_markets": all_markets,
        "capital_layer_reviews": capital_layer_reviews,
        "market_reviews": market_reviews,
    }
    _append_market_layer_logs(
        {"session": "lunch", "as_of": result["as_of"]}, capital_layer_reviews
    )
    return result


# ---- close review -----------------------------------------------------------


def review_close(
    all_trades: list[dict[str, Any]],
    positions: list[dict[str, Any]],
    benchmark_return: float | None,
    *,
    portfolio_returns_series: list[float] | None = None,
    benchmark_returns_series: list[float] | None = None,
    stage: str = "stage_1_sim",
    trade_date: str | None = None,
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

    Returns market/capital-layer reviews with native currency. ``all_markets``
    and top-level capital-layer records expose only non-monetary counts/health.
    """
    layer_market_positions = _group_by_capital_layer_market_account(positions)
    layer_market_trades = _group_by_capital_layer_market_account(all_trades)
    layers = sorted(
        set(layer_market_positions) | set(layer_market_trades) or {"shadow"}
    )
    ledger_pnl_summary: dict[str, dict[str, Any]] = {}
    if "simulated" in layers:
        ledger_pnl_summary = sim_ledger_pnl_summary(
            ledger_root=DEFAULT_SIM_LEDGER_ROOT,
            local_trades_path=DEFAULT_LOCAL_SIM_TRADES,
        )
    execution_exclusions = _load_execution_exclusions(trade_date or _today_compact())
    goals = _load_goals()
    stage_goals = _stage_goals(goals, stage)

    def build_review(
        layer: str,
        market: str,
        account_scope: str,
        layer_pos: list[dict[str, Any]],
        layer_trd: list[dict[str, Any]],
        ledger_pnl: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        strategy_trd = strategy_valid_trades(layer_trd)
        sample_quality = summarize_sample_quality(layer_trd)
        wins = [t for t in strategy_trd if _safe_float(t.get("pnl")) > 0]
        losses = [t for t in strategy_trd if _safe_float(t.get("pnl")) < 0]
        pnl = _sum_pnl(strategy_trd)
        floating = sum(
            _safe_float(p.get("pnl_pct")) * _safe_float(p.get("weight"))
            for p in layer_pos
        )
        total_pnl = pnl + floating
        ledger = ledger_pnl if isinstance(ledger_pnl, dict) else None
        currency = _market_currency(market)

        def ledger_amount(key: str) -> float | None:
            if ledger is None or ledger.get(key) is None:
                return None
            return round(_safe_float(ledger.get(key)), 6)

        win_rate = len(wins) / len(strategy_trd) if strategy_trd else 0.0
        avg_win = (_sum_pnl(wins) / len(wins)) if wins else 0.0
        avg_loss = (_sum_pnl(losses) / len(losses)) if losses else 0.0
        profit_factor = (
            (abs(_sum_pnl(wins)) / abs(_sum_pnl(losses)))
            if losses and _sum_pnl(losses) != 0
            else float("inf")
            if wins
            else 0.0
        )
        trades_summary = {
            "count": len(layer_trd),
            "strategy_count": len(strategy_trd),
            "validation_sample_count": int(
                sample_quality.get("validation_sample_count", 0)
            ),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(win_rate, 4),
            "avg_win": round(avg_win, 6),
            "avg_loss": round(avg_loss, 6),
            "profit_factor": round(profit_factor, 6)
            if profit_factor != float("inf")
            else None,
            "realized_pnl": round(pnl, 6),
            "floating_pnl": round(floating, 6),
        }
        attr = attribute_pct(strategy_trd)
        market_benchmark_return = benchmark_return if market == "ashare" else None
        market_portfolio_series = (
            portfolio_returns_series if market == "ashare" else None
        )
        market_benchmark_series = (
            benchmark_returns_series if market == "ashare" else None
        )
        bench_cmp = compare_to_benchmark(
            total_pnl,
            market_benchmark_return,
            market_portfolio_series,
            market_benchmark_series,
        )
        vs_last = {
            "status": "unavailable",
            "reason": "market_scoped_last_period_authority_unavailable",
            "this_period_return": None,
            "last_period_return": None,
            "improved": None,
            "delta": None,
        }
        metrics_for_goals = {
            "win_rate": win_rate,
            "sharpe": bench_cmp.get("sharpe"),
            "max_drawdown": bench_cmp.get("max_drawdown"),
            "stage": stage,
        }
        vs_goals = _compare_to_goals(metrics_for_goals, stage_goals)
        bleeding_dims = [
            d
            for d, pct in attr.get("by_dimension", {}).items()
            if pct < -0.1 and d != "unattributed"
        ]
        bleeding_strats = [
            s
            for s, pct in attr.get("by_strategy", {}).items()
            if pct < -0.1 and s != "unattributed"
        ]
        return {
            "capital_layer": layer,
            "market": market,
            "currency": currency,
            "account_scope": account_scope,
            "trades": len(layer_trd),
            "strategy_trades": len(strategy_trd),
            "validation_sample_count": int(
                sample_quality.get("validation_sample_count", 0)
            ),
            "sample_quality": sample_quality,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(win_rate, 4),
            "trades_summary": trades_summary,
            "pnl": round(total_pnl, 6),
            "ledger_realized_pnl": ledger_amount("realized_pnl"),
            "ledger_unrealized_pnl": ledger_amount("unrealized_pnl"),
            "ledger_total_pnl": ledger_amount("total_pnl"),
            "ledger_strategy_realized_pnl": ledger_amount("strategy_realized_pnl"),
            "ledger_strategy_unrealized_pnl": ledger_amount("strategy_unrealized_pnl"),
            "ledger_strategy_total_pnl": ledger_amount("strategy_total_pnl"),
            "ledger_market_value": ledger_amount("market_value"),
            "ledger_strategy_market_value": ledger_amount("strategy_market_value"),
            "ledger_open_position_count": (
                int(ledger.get("open_position_count") or 0)
                if ledger is not None
                else None
            ),
            "ledger_strategy_open_position_count": (
                int(ledger.get("strategy_open_position_count") or 0)
                if ledger is not None
                else None
            ),
            "ledger_missing_mark_count": (
                int(ledger.get("missing_mark_count") or 0)
                if ledger is not None
                else None
            ),
            "ledger_pnl_source": ledger.get("pnl_source") if ledger else None,
            "ledger_mark_authority": (
                ledger.get("mark_authority") or None if ledger else None
            ),
            "ledger_state": (
                str(ledger.get("monetary_state") or "available")
                if ledger is not None
                else "unavailable"
            ),
            "stale": not layer_trd,
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
            "execution_quality": _execution_quality(
                execution_exclusions, layer, market, account_scope
            ),
        }

    capital_layer_reviews: dict[str, Any] = {}
    market_reviews: dict[str, dict[str, Any]] = {}
    for layer in layers:
        layer_markets = sorted(
            set(layer_market_positions.get(layer, {}))
            | set(layer_market_trades.get(layer, {}))
        )
        layer_market_reviews: dict[str, dict[str, Any]] = {}
        for market in layer_markets:
            position_accounts = layer_market_positions.get(layer, {}).get(market, {})
            trade_accounts = layer_market_trades.get(layer, {}).get(market, {})
            account_reviews: dict[str, dict[str, Any]] = {}
            for account_key in sorted(set(position_accounts) | set(trade_accounts)):
                account_positions = position_accounts.get(account_key, [])
                account_trades = trade_accounts.get(account_key, [])
                if account_key == UNSCOPED_ACCOUNT_KEY:
                    account_reviews[account_key] = _unscoped_account_review(
                        market, layer, account_positions, account_trades
                    )
                    continue
                account_reviews[account_key] = build_review(
                    layer,
                    market,
                    account_key,
                    account_positions,
                    account_trades,
                    ledger_pnl=(
                        _ledger_account_summary(ledger_pnl_summary, market, account_key)
                        if layer == "simulated"
                        else None
                    ),
                )
            market_review = _summarize_market_accounts(market, layer, account_reviews)
            layer_market_reviews[market] = market_review
            _merge_market_review(market_reviews, market, layer, market_review)
        capital_layer_reviews[layer] = _summarize_capital_layer(
            layer, layer_market_reviews
        )

    all_markets = _all_markets_health(capital_layer_reviews)

    result = {
        "session": "close",
        "as_of": _now_iso(),
        "capital_layer": _preferred_capital_layer(list(capital_layer_reviews)),
        "stale": int(all_markets["trade_count"]) == 0,
        "all_markets": all_markets,
        "capital_layer_reviews": capital_layer_reviews,
        "market_reviews": market_reviews,
    }
    _append_market_layer_logs(
        {"session": "close", "as_of": result["as_of"]}, capital_layer_reviews
    )
    return result


# ---- self-test --------------------------------------------------------------

if __name__ == "__main__":
    pos = [
        {
            "ts_code": "000001.SZ",
            "weight": 0.3,
            "pnl_pct": 0.02,
            "stop_loss_pct": -0.03,
            "take_profit_pct": 0.05,
            "momentum": 0.5,
        },
        {
            "ts_code": "600519.SH",
            "weight": 0.4,
            "pnl_pct": -0.04,
            "stop_loss_pct": -0.03,
            "take_profit_pct": 0.08,
            "momentum": -0.2,
        },
    ]
    morning = [
        {
            "ts_code": "000001.SZ",
            "pnl": 0.02,
            "signal_id": "s1",
            "dimensions": {"macro": 0.6, "technical": 0.4},
            "strategy": "pullback",
            "condition": "low_vol",
        },
        {
            "ts_code": "600519.SH",
            "pnl": -0.01,
            "signal_id": "s2",
            "dimension": "event",
            "strategy": "event_driven",
            "condition": "high_vol",
        },
    ]
    print("=== LUNCH ===")
    print(json.dumps(review_lunch(pos, morning), ensure_ascii=False, indent=2))
    print("\n=== CLOSE ===")
    all_t = morning + [
        {
            "ts_code": "600036.SH",
            "pnl": 0.03,
            "dimensions": {"technical": 1.0},
            "strategy": "trend",
            "condition": "mid_vol",
        }
    ]
    print(
        json.dumps(
            review_close(all_t, pos, benchmark_return=0.005, stage="stage_1_sim"),
            ensure_ascii=False,
            indent=2,
        )
    )
