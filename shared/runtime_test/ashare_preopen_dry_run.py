#!/usr/bin/env python3
# ruff: noqa: E402
"""Read-only A-share pre-open dry run for the simulated trading chain."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from Ashare.adapter import AshareAdapter
from Ashare.capital_plan import TOTAL_CAPITAL, plan_capital
from Ashare.evolution_controller import decision_market_context, load_latest_decision
from Ashare.sim_executor import _is_supported_ashare_code, _market_session_rejection
from shared.capital import load_market_capital_provider_state
from shared.data.reader import TradingagentDataReader
from shared.execution.execution_lineage import (
    ASHARE_AUTHORITY_GENERATION,
    ASHARE_CAPITAL_AUTHORITY_ID,
    ASHARE_EXECUTION_LINEAGE_ID,
)
from shared.notify import email_sender
from shared.orchestrator import (
    _account_available_cash,
    _account_capital,
    _account_positions,
    _ashare_authoritative_account_view,
    _estimate_ashare_market_reservation,
    _latest_price,
    _score_diagnostics,
)
from shared.screening.candidate_pool import build_pool
from shared.screening.six_dimension_scorer import score_universe


CN_TZ = timezone(timedelta(hours=8))
LATEST = ROOT / "shared/runtime_test/ashare_preopen_dry_run_latest.json"
HISTORY = ROOT / "shared/runtime_test/ashare_preopen_dry_run_history.jsonl"
CANDIDATE_THRESHOLD = 0.55
MIN_SYMBOLS = 1000
DEFAULT_DAILY_COVERAGE_RATIO = 0.90
DEFAULT_SCORE_LIMIT = 50


def _now_cn(value: str | None = None) -> datetime:
    if not value:
        return datetime.now(CN_TZ)
    parsed = datetime.fromisoformat(value.replace(" ", "T", 1))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=CN_TZ)
    return parsed.astimezone(CN_TZ)


def _trade_date(now: datetime) -> str:
    return now.strftime("%Y%m%d")


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if result == result else default
    except (TypeError, ValueError):
        return default


def _status_rank(status: str) -> int:
    return {"pass": 0, "ok": 0, "warn": 1, "degraded": 1, "fail": 2, "critical": 2}.get(
        str(status).lower(), 1
    )


def _overall_status(sections: list[dict[str, Any]]) -> str:
    worst = max(
        (_status_rank(str(section.get("status") or "warn")) for section in sections),
        default=1,
    )
    return "fail" if worst >= 2 else ("warn" if worst == 1 else "pass")


def _compact_scores(
    scored: list[tuple[str, dict[str, float]]], *, limit: int = 10
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for symbol, scores in scored[: max(1, limit)]:
        rows.append(
            {
                "symbol": symbol,
                "combined": round(_safe_float(scores.get("combined")), 4),
                "macro": round(_safe_float(scores.get("macro")), 4),
                "event": round(_safe_float(scores.get("event")), 4),
                "fundamental": round(_safe_float(scores.get("fundamental")), 4),
                "capital": round(_safe_float(scores.get("capital")), 4),
                "technical": round(_safe_float(scores.get("technical")), 4),
                "sentiment": round(_safe_float(scores.get("sentiment")), 4),
            }
        )
    return rows


def _latest_liquid_universe_from_reader(reader: Any, *, limit: int) -> list[str]:
    get_assets = getattr(reader, "get_assets", None)
    if not callable(get_assets):
        return []
    try:
        rows = get_assets(market="Ashare")
    except TypeError:
        try:
            rows = get_assets("Ashare")
        except Exception:
            return []
    except Exception:
        return []
    asset_symbols: list[str] = []
    seen: set[str] = set()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        symbol = (
            str(row.get("symbol") or row.get("ts_code") or row.get("code") or "")
            .strip()
            .upper()
        )
        if not symbol or symbol in seen or not _is_supported_ashare_code(symbol):
            continue
        name = str(row.get("name") or "").upper()
        status = str(row.get("status") or "").lower()
        if (
            "ST" in name
            or "退" in name
            or status in {"suspended", "halted", "delisted", "inactive"}
        ):
            continue
        seen.add(symbol)
        asset_symbols.append(symbol)

    batch_amounts = _latest_daily_amounts_from_reader(reader)
    if not batch_amounts:
        return []
    candidates = [
        (symbol, amount)
        for symbol in asset_symbols
        if (amount := batch_amounts.get(symbol, 0.0)) > 0
        and amount * 1000.0 >= 50_000_000.0
    ]
    candidates.sort(key=lambda item: (-item[1], item[0]))
    return [symbol for symbol, _ in candidates[: max(1, int(limit))]]


def _latest_daily_rows_from_reader(reader: Any) -> list[dict[str, Any]]:
    get_latest_daily_batch = getattr(reader, "get_latest_daily_batch", None)
    if not callable(get_latest_daily_batch):
        return []
    try:
        rows = list(get_latest_daily_batch("Ashare", limit=5000) or [])
    except Exception as exc:
        raise RuntimeError(f"{exc.__class__.__name__}: {exc}") from exc
    return [row for row in rows if isinstance(row, dict)]


def _latest_daily_amounts_from_reader(reader: Any) -> dict[str, float]:
    try:
        rows = _latest_daily_rows_from_reader(reader)
    except RuntimeError:
        return {}
    latest_date = ""
    clean_rows: list[dict[str, Any]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        symbol = (
            str(row.get("symbol") or row.get("ts_code") or row.get("code") or "")
            .strip()
            .upper()
        )
        if not symbol or not _is_supported_ashare_code(symbol):
            continue
        if _safe_float(row.get("close"), 0.0) <= 0:
            continue
        trade_date = str(row.get("trade_date") or row.get("date") or "").replace(
            "-", ""
        )
        if not trade_date:
            continue
        if trade_date > latest_date:
            latest_date = trade_date
        clean_rows.append(row)
    if not latest_date:
        return {}

    amounts: dict[str, float] = {}
    for row in clean_rows:
        trade_date = str(row.get("trade_date") or row.get("date") or "").replace(
            "-", ""
        )
        if trade_date != latest_date:
            continue
        symbol = (
            str(row.get("symbol") or row.get("ts_code") or row.get("code") or "")
            .strip()
            .upper()
        )
        amount = _safe_float(row.get("amount"), 0.0)
        if amount > amounts.get(symbol, 0.0):
            amounts[symbol] = amount
    return amounts


def _latest_daily_amount_from_reader(reader: Any, symbol: str) -> float:
    get_bars_daily = getattr(reader, "get_bars_daily", None)
    if not callable(get_bars_daily):
        return 0.0
    try:
        rows = get_bars_daily("Ashare", symbol, "", "")
    except TypeError:
        try:
            rows = get_bars_daily("ashare", symbol, "", "")
        except Exception:
            return 0.0
    except Exception:
        return 0.0
    best: dict[str, Any] | None = None
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        if _safe_float(row.get("close"), 0.0) <= 0:
            continue
        if best is None or str(row.get("trade_date") or "") > str(
            best.get("trade_date") or ""
        ):
            best = row
    if best is None:
        return 0.0
    return _safe_float(best.get("amount"), 0.0)


def _build_candidate_pool(
    *,
    reader: Any,
    date: str,
    score_limit: int,
) -> dict[str, Any]:
    universe = _latest_liquid_universe_from_reader(reader, limit=score_limit)
    universe_source = "sharedsignals_api_assets"
    if not universe:
        universe_source = "none"
    limited = universe[: max(1, int(score_limit))]
    scored = score_universe(
        date=date, universe=limited, data_reader=reader, market="ashare"
    )
    scores_by_symbol = {symbol: scores for symbol, scores in scored}
    pool = build_pool(
        date=date,
        universe=limited,
        holdings=[],
        market="ashare",
        reader=reader,
        scores_by_symbol=scores_by_symbol,
        include_fundamental_pool=False,
    )
    candidate_symbols = set(pool.get("candidate") or [])
    candidates = [
        {
            "ts_code": symbol,
            "code": symbol,
            "combined": _safe_float(scores.get("combined")),
            "score": _safe_float(scores.get("combined")),
            "scores": scores,
        }
        for symbol, scores in scored
        if symbol in candidate_symbols
    ]
    watch = list(pool.get("watch") or [])[:20]
    return {
        "status": "pass" if candidates else "warn",
        "reason": "candidate_layer_ready"
        if candidates
        else "no_candidate_layer_after_scoring",
        "universe_source": universe_source,
        "universe_count": len(universe),
        "scored_count": len(scored),
        "score_universe_limit": max(1, int(score_limit)),
        "candidate_threshold": CANDIDATE_THRESHOLD,
        "candidate_count": len(candidates),
        "watch_count": len(watch),
        "top_candidates": _compact_scores(
            [(row["ts_code"], row["scores"]) for row in candidates], limit=10
        ),
        "top_scored": _compact_scores(scored, limit=10),
        "score_diagnostics": _score_diagnostics(
            scores_by_symbol, actual_candidate_count=len(candidates)
        ),
        "candidates_for_plan": candidates,
    }


def _intraday_evidence_date(reader: Any) -> str | None:
    """Extract latest trade_date from intraday 5-minute batch for Ashare."""
    get_batch = getattr(reader, "get_realtime_5min_batch", None)
    if not callable(get_batch):
        return None
    try:
        rows = list(get_batch("Ashare") or [])
    except Exception:
        return None
    if not rows:
        return None
    latest = ""
    for row in rows:
        if not isinstance(row, dict):
            continue
        trade_date = str(row.get("trade_date") or row.get("bar_time") or "").replace(
            "-", ""
        )[:8]
        if not trade_date:
            continue
        if trade_date > latest:
            latest = trade_date
    return latest or None


def _is_daily_behind_intraday(
    *,
    daily_date: str,
    intraday_date: str,
    now: datetime,
) -> bool:
    """Return True when daily bars are stale relative to intraday evidence.

    Current-session intraday bars require the previous business-day daily bar.
    Older intraday evidence represents the latest completed session and daily
    bars must reach that date. Exchange holidays may make this conservative,
    which is intentional for a fail-closed trading gate.
    """
    if not daily_date or not intraday_date:
        return False
    if intraday_date <= daily_date:
        return False
    try:
        intraday_day = datetime.strptime(intraday_date, "%Y%m%d").date()
        daily_day = datetime.strptime(daily_date, "%Y%m%d").date()
    except ValueError:
        return False
    if intraday_day >= now.date():
        expected_day = intraday_day - timedelta(days=1)
        while expected_day.weekday() >= 5:
            expected_day -= timedelta(days=1)
    else:
        expected_day = intraday_day
    return daily_day < expected_day


def _api_daily_coverage_from_reader(
    reader: Any,
    *,
    now: datetime,
    min_symbols: int,
    min_coverage_ratio: float = DEFAULT_DAILY_COVERAGE_RATIO,
) -> dict[str, Any]:
    rows = _latest_daily_rows_from_reader(reader)
    latest_trade_date = ""
    symbols: set[str] = set()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        symbol = (
            str(row.get("symbol") or row.get("ts_code") or row.get("code") or "")
            .strip()
            .upper()
        )
        if not symbol or not _is_supported_ashare_code(symbol):
            continue
        if _safe_float(row.get("close"), 0.0) <= 0:
            continue
        trade_date = str(row.get("trade_date") or row.get("date") or "").replace(
            "-", ""
        )
        if not trade_date:
            continue
        if trade_date > latest_trade_date:
            latest_trade_date = trade_date
            symbols = {symbol}
        elif trade_date == latest_trade_date:
            symbols.add(symbol)
    if not latest_trade_date:
        return {
            "status": "fail",
            "reason": "api_daily_bars_missing",
            "symbol_count": 0,
            "data_source": (
                "legacy compatibility bulk reader (hard-blocked from current-v1)"
            ),
        }

    # ---- asset count & coverage ratio ----
    asset_symbols: set[str] = set()
    get_assets = getattr(reader, "get_assets", None)
    if callable(get_assets):
        try:
            asset_rows = get_assets("Ashare")
        except TypeError:
            try:
                asset_rows = get_assets()
            except Exception:
                asset_rows = None
        except Exception:
            asset_rows = None
        if asset_rows:
            asset_symbols = {
                symbol
                for row in asset_rows
                if isinstance(row, dict)
                and (
                    symbol := str(
                        row.get("symbol") or row.get("ts_code") or row.get("code") or ""
                    )
                    .strip()
                    .upper()
                )
                and _is_supported_ashare_code(symbol)
            }

    asset_count = len(asset_symbols)
    covered_symbols = symbols & asset_symbols if asset_symbols else set()
    symbol_count = len(covered_symbols)
    outside_asset_count = (
        len(symbols - asset_symbols) if asset_symbols else len(symbols)
    )
    daily_coverage_ratio: float | None = None
    if asset_count > 0:
        daily_coverage_ratio = symbol_count / asset_count

    # ---- intraday evidence date ----
    intraday_date = _intraday_evidence_date(reader)
    expected_evidence_date = intraday_date or latest_trade_date

    # ---- age ----
    age_days: int | None = None
    if latest_trade_date != "unknown":
        try:
            age_days = (
                now.replace(tzinfo=None).date()
                - datetime.strptime(latest_trade_date, "%Y%m%d").date()
            ).days
        except ValueError:
            age_days = None

    # ---- gate evaluation ----
    status = "pass"
    reason = "api_daily_bars_ready"
    if asset_count <= 0:
        status = "fail"
        reason = "api_asset_universe_unavailable"
    elif symbol_count < min_symbols:
        status = "fail"
        reason = "api_daily_bars_missing"
    elif age_days is not None and age_days > 5:
        status = "fail"
        reason = "api_daily_bars_stale"
    elif daily_coverage_ratio is not None and daily_coverage_ratio < min_coverage_ratio:
        status = "fail"
        reason = "api_daily_coverage_incomplete"
    elif (
        intraday_date
        and latest_trade_date != "unknown"
        and _is_daily_behind_intraday(
            daily_date=latest_trade_date,
            intraday_date=intraday_date,
            now=now,
        )
    ):
        status = "fail"
        reason = "api_daily_bars_behind_intraday"

    return {
        "status": status,
        "reason": reason,
        "symbol_count": symbol_count,
        "daily_symbol_count_raw": len(symbols),
        "daily_symbol_outside_asset_count": outside_asset_count,
        "asset_count": asset_count,
        "daily_coverage_ratio": round(daily_coverage_ratio, 4)
        if daily_coverage_ratio is not None
        else None,
        "expected_evidence_date": expected_evidence_date,
        "latest_trade_date": latest_trade_date,
        "latest_daily_age_days": age_days,
        "max_daily_age_days": 5,
        "min_coverage_ratio": min_coverage_ratio,
        "data_source": (
            "legacy compatibility bulk reader (hard-blocked from current-v1)"
        ),
    }


def _compact_trade_date(value: Any) -> str:
    return str(value or "").strip().replace("-", "")[:8]


def _unique_position_count(rows: Any) -> int:
    if not isinstance(rows, list):
        return 0
    return len(
        {
            str(row.get("ts_code") or row.get("symbol") or row.get("code") or "")
            .strip()
            .upper()
            for row in rows
            if isinstance(row, dict)
            and str(
                row.get("ts_code") or row.get("symbol") or row.get("code") or ""
            ).strip()
        }
    )


def _adapter_diagnostics(account: Any, config: Any, error: str = "") -> dict[str, Any]:
    safe_config = config if isinstance(config, dict) else {}
    positions = _account_positions(account, safe_config)
    capital = _account_capital(account, safe_config)
    cash = _account_available_cash(account, safe_config, capital, positions)
    raw_account = account if isinstance(account, dict) else {}
    strategy_positions = raw_account.get("strategy_positions")
    return {
        "status": "warn" if error else "diagnostic_only",
        "error": error,
        "source": str(raw_account.get("source") or ""),
        "reported_capital_cny": round(capital, 2),
        "reported_cash_available_cny": round(cash, 2),
        "reported_position_count": _unique_position_count(positions),
        "reported_strategy_cash_available_cny": (
            round(_safe_float(raw_account.get("strategy_cash_available")), 2)
            if raw_account.get("strategy_cash_available") is not None
            else None
        ),
        "reported_strategy_position_count": _unique_position_count(strategy_positions),
        "reported_sample_adjustment": (
            dict(raw_account.get("capital_plan_sample_adjustment"))
            if isinstance(raw_account.get("capital_plan_sample_adjustment"), dict)
            else {}
        ),
        "used_for_planning": False,
        "reason": "adapter_balances_positions_and_validation_samples_are_diagnostics_only",
    }


def _load_authoritative_account_view(
    adapter_account: Any,
    trade_date: str,
) -> dict[str, Any]:
    """Read the fresh server-local 50k strategy account without bootstrapping."""

    # Adapter balances and positions are diagnostics only.  Even account
    # identity is pinned here so a stale adapter cannot select a second pool.
    _ = adapter_account
    view = _ashare_authoritative_account_view({"account": "ashare_sim"}, trade_date)
    if not isinstance(view, dict):
        raise RuntimeError("ashare_local_account_view_invalid")
    if str(view.get("source") or "") != "server_local_sim_ledger":
        raise RuntimeError("ashare_local_account_source_invalid")
    if str(view.get("account") or "") != "ashare_sim":
        raise RuntimeError("ashare_local_account_identity_invalid")
    expected_authority = {
        "capital_authority_id": ASHARE_CAPITAL_AUTHORITY_ID,
        "authority_generation": ASHARE_AUTHORITY_GENERATION,
        "execution_lineage_id": ASHARE_EXECUTION_LINEAGE_ID,
    }
    if any(view.get(key) != value for key, value in expected_authority.items()):
        raise RuntimeError("ashare_local_account_fresh_lineage_mismatch")
    capital = _safe_float(view.get("capital_cny"), -1.0)
    cash = _safe_float(view.get("cash_available"), -1.0)
    if not math.isclose(capital, float(TOTAL_CAPITAL), abs_tol=0.01):
        raise RuntimeError("ashare_local_account_capital_mismatch")
    if cash < 0:
        raise RuntimeError("ashare_local_account_cash_unavailable")
    if not isinstance(view.get("positions"), list):
        raise RuntimeError("ashare_local_account_positions_invalid")
    if view.get("real_trading_enabled") is not False:
        raise RuntimeError("ashare_local_account_real_trading_flag_invalid")
    if _compact_trade_date(view.get("trade_date")) != _compact_trade_date(trade_date):
        raise RuntimeError("ashare_local_account_trade_date_mismatch")
    return {
        **view,
        **expected_authority,
        "real_trading_enabled": False,
    }


def _ashare_capital_section(trade_date: str) -> dict[str, Any]:
    """Load and validate the standalone ashare MarketCapitalLedger provider state."""
    try:
        raw_state = load_market_capital_provider_state("ashare", trade_date)
    except Exception as exc:
        return {
            "status": "fail",
            "reason": "ashare_capital_provider_error",
            "error": f"{exc.__class__.__name__}: {exc}",
            "trade_date": _compact_trade_date(trade_date),
            "real_trading_enabled": None,
            "available_ashare_capacity_cny": 0.0,
        }

    if raw_state is None:
        return {
            "status": "fail",
            "reason": "ashare_capital_unavailable",
            "trade_date": _compact_trade_date(trade_date),
            "real_trading_enabled": None,
            "available_ashare_capacity_cny": 0.0,
        }

    # --- Authority field validation ---
    if str(raw_state.get("source") or "") != "market_capital_ledger":
        return {
            **raw_state,
            "status": "fail",
            "reason": "ashare_capital_source_invalid",
            "available_ashare_capacity_cny": 0.0,
        }
    if str(raw_state.get("authority_id") or "") != "ashare-capital-v1":
        return {
            **raw_state,
            "status": "fail",
            "reason": "ashare_capital_authority_id_invalid",
            "available_ashare_capacity_cny": 0.0,
        }
    if raw_state.get("authority_generation") != 1:
        return {
            **raw_state,
            "status": "fail",
            "reason": "ashare_capital_authority_generation_invalid",
            "available_ashare_capacity_cny": 0.0,
        }
    if str(raw_state.get("market") or "") != "ashare":
        return {
            **raw_state,
            "status": "fail",
            "reason": "ashare_capital_market_invalid",
            "available_ashare_capacity_cny": 0.0,
        }
    if not math.isclose(
        _safe_float(raw_state.get("initial_equity_cny")), 50_000.0, abs_tol=0.01
    ):
        return {
            **raw_state,
            "status": "fail",
            "reason": "ashare_capital_initial_equity_invalid",
            "available_ashare_capacity_cny": 0.0,
        }
    if not math.isclose(
        _safe_float(raw_state.get("stock_gross_exposure_limit_cny")),
        45_000.0,
        abs_tol=0.01,
    ):
        return {
            **raw_state,
            "status": "fail",
            "reason": "ashare_capital_gross_exposure_invalid",
            "available_ashare_capacity_cny": 0.0,
        }
    if not math.isclose(
        _safe_float(raw_state.get("single_name_cap_cny")), 7_500.0, abs_tol=0.01
    ):
        return {
            **raw_state,
            "status": "fail",
            "reason": "ashare_capital_single_name_cap_invalid",
            "available_ashare_capacity_cny": 0.0,
        }
    if raw_state.get("real_trading_enabled") is not False:
        return {
            **raw_state,
            "status": "fail",
            "reason": "ashare_capital_real_trading_flag_invalid",
            "available_ashare_capacity_cny": 0.0,
        }

    # --- Freshness and reconciliation ---
    if raw_state.get("fresh") is not True:
        return {
            **raw_state,
            "status": "fail",
            "reason": "ashare_capital_not_reconciled_for_trade_date",
            "available_ashare_capacity_cny": 0.0,
        }
    if raw_state.get("reconciled") is not True:
        return {
            **raw_state,
            "status": "fail",
            "reason": "ashare_capital_not_reconciled",
            "available_ashare_capacity_cny": 0.0,
        }

    # --- Trade date must match ---
    if _compact_trade_date(raw_state.get("trade_date")) != _compact_trade_date(
        trade_date
    ):
        return {
            **raw_state,
            "status": "fail",
            "reason": "ashare_capital_trade_date_mismatch",
            "available_ashare_capacity_cny": 0.0,
        }

    # --- Execution lineage must be present ---
    if not str(raw_state.get("execution_lineage_id") or "").strip():
        return {
            **raw_state,
            "status": "fail",
            "reason": "ashare_capital_execution_lineage_missing",
            "available_ashare_capacity_cny": 0.0,
        }

    # --- Risk gates: 5% derisk, 7% halt ---
    initial_equity = _safe_float(raw_state.get("initial_equity_cny"), 50_000.0)
    max_daily_loss = _safe_float(raw_state.get("max_daily_loss"), initial_equity * 0.03)
    daily_realized_pnl = _safe_float(raw_state.get("daily_realized_pnl"), 0.0)
    if daily_realized_pnl <= -abs(max_daily_loss):
        return {
            **raw_state,
            "status": "fail",
            "reason": "ashare_daily_loss_pause",
            "available_ashare_capacity_cny": 0.0,
        }

    consecutive_losses = raw_state.get("consecutive_losses")
    max_consecutive_losses = raw_state.get("max_consecutive_losses")
    if (
        isinstance(consecutive_losses, int)
        and isinstance(max_consecutive_losses, int)
        and consecutive_losses >= max_consecutive_losses
    ):
        return {
            **raw_state,
            "status": "fail",
            "reason": "ashare_consecutive_loss_pause",
            "available_ashare_capacity_cny": 0.0,
        }

    equity_cny = _safe_float(raw_state.get("equity_cny"), initial_equity)
    high_water_equity = _safe_float(raw_state.get("high_water_equity"), initial_equity)
    drawdown = max(0.0, high_water_equity - equity_cny)
    max_drawdown = _safe_float(raw_state.get("max_drawdown"), initial_equity * 0.07)
    drawdown_tighten = initial_equity * 0.05  # 5% derisk

    if drawdown >= max_drawdown - 1e-9:
        return {
            **raw_state,
            "status": "fail",
            "reason": "ashare_drawdown_halt",
            "drawdown_cny": round(drawdown, 2),
            "available_ashare_capacity_cny": 0.0,
        }

    drawdown_tightened = drawdown >= drawdown_tighten
    risk_multiplier = 0.75 if drawdown_tightened else 1.0

    available_capacity = _safe_float(raw_state.get("available_to_reserve_cny"), 0.0)

    return {
        **raw_state,
        "status": "pass",
        "reason": "ashare_capital_ready",
        "drawdown_cny": round(drawdown, 2),
        "drawdown_tightened": drawdown_tightened,
        "risk_multiplier": risk_multiplier,
        "available_ashare_capacity_cny": round(max(0.0, available_capacity), 2),
        "new_risk_allowed": not drawdown_tightened or drawdown < max_drawdown,
    }


def _unavailable_capital_plan(
    *,
    adapter_diagnostics: dict[str, Any],
    account_error: str,
) -> dict[str, Any]:
    return {
        "status": "fail",
        "reason": "server_local_strategy_account_unavailable",
        "account_error": account_error,
        "account": "ashare_sim",
        "total_capital": float(TOTAL_CAPITAL),
        "cash_available": 0.0,
        "account_cash_available": 0.0,
        "existing_position_count": 0,
        "account_position_count": 0,
        "max_new_positions": 0,
        "target_positions": 0,
        "position_budget_by_symbol": {},
        "suggested_buys": [],
        "source": "server_local_sim_ledger_unavailable",
        "real_trading_enabled": False,
        "adapter_diagnostics": adapter_diagnostics,
    }


def _build_capital_plan(
    adapter: AshareAdapter,
    candidates: list[dict[str, Any]],
    *,
    date: str,
    ashare_capital_state: dict[str, Any],
) -> dict[str, Any]:
    adapter_error = ""
    try:
        account = adapter.get_sim_account()
    except Exception as exc:
        account = {}
        adapter_error = f"{exc.__class__.__name__}: {exc}"
    try:
        config = adapter.get_strategy_config()
    except Exception as exc:
        config = {}
        adapter_error = adapter_error or f"{exc.__class__.__name__}: {exc}"
    diagnostics = _adapter_diagnostics(account, config, adapter_error)

    try:
        authority = _load_authoritative_account_view(account, date)
    except Exception as exc:
        return _unavailable_capital_plan(
            adapter_diagnostics=diagnostics,
            account_error=f"{exc.__class__.__name__}: {exc}",
        )

    positions: list[dict[str, Any]] = []
    for raw in authority.get("positions") or []:
        if not isinstance(raw, dict):
            continue
        row = dict(raw)
        market_value = _safe_float(row.get("market_value", row.get("value")), 0.0)
        row["market_value"] = market_value
        row["value"] = market_value
        positions.append(row)
    cash = _safe_float(authority.get("cash_available"), -1.0)
    if cash < 0:
        return _unavailable_capital_plan(
            adapter_diagnostics=diagnostics,
            account_error="RuntimeError: ashare_local_account_cash_unavailable",
        )

    evolution_decision = load_latest_decision()
    execution_lineage = str(
        ashare_capital_state.get("execution_lineage_id") or "ashare-sim-legacy"
    ).strip()
    authority_scope = {
        "capital_authority_id": "ashare-capital-v1",
        "authority_generation": 1,
        "execution_lineage_id": execution_lineage,
    }
    evolution_context = decision_market_context(
        evolution_decision,
        target_trade_date=date,
        authority_scope=authority_scope,
    )
    market_context: dict[str, Any] = {
        "risk_rejection_rate": 0.0,
        "data_issue_rate": 0.0,
        "exploration_daily_realized_pnl_cny": _safe_float(
            ashare_capital_state.get("daily_realized_pnl"), 0.0
        ),
    }
    market_context.update(evolution_context)
    plan = plan_capital(
        positions,
        cash,
        candidates=candidates,
        dynamic=True,
        market_context=market_context,
        total_capital=float(TOTAL_CAPITAL),
    ).to_dict()

    single_name_limit = round(float(TOTAL_CAPITAL) * 0.15, 2)
    exposure_by_symbol = {
        str(row.get("ts_code") or row.get("symbol") or row.get("code") or "")
        .strip()
        .upper(): _safe_float(row.get("market_value", row.get("value")), 0.0)
        for row in positions
        if isinstance(row, dict)
    }
    raw_budgets = (
        plan.get("position_budget_by_symbol")
        if isinstance(plan.get("position_budget_by_symbol"), dict)
        else {}
    )
    safe_budgets: dict[str, float] = {}
    for symbol, raw_budget in raw_budgets.items():
        key = str(symbol).strip().upper()
        remaining_single_name = max(
            0.0, single_name_limit - exposure_by_symbol.get(key, 0.0)
        )
        safe_budgets[key] = round(
            min(max(0.0, _safe_float(raw_budget)), remaining_single_name, cash),
            2,
        )
    plan["position_budget_by_symbol"] = safe_budgets
    if isinstance(plan.get("suggested_buys"), list):
        for row in plan["suggested_buys"]:
            if not isinstance(row, dict):
                continue
            key = str(row.get("code") or row.get("ts_code") or "").strip().upper()
            safe_budget = safe_budgets.get(key, 0.0)
            row["allocation"] = safe_budget
            row["executable_budget"] = safe_budget
            row["risk_limit_budget"] = single_name_limit

    has_safe_budget = any(value > 0 for value in safe_budgets.values())
    plan.update(
        {
            "status": "pass" if has_safe_budget else "warn",
            "account": str(authority.get("account") or "ashare_sim"),
            "total_capital": float(TOTAL_CAPITAL),
            "cash_available": round(cash, 2),
            "account_cash_available": round(cash, 2),
            "existing_position_count": _unique_position_count(positions),
            "account_position_count": _unique_position_count(positions),
            "source": "server_local_sim_ledger",
            "trade_date": _compact_trade_date(authority.get("trade_date") or date),
            "real_trading_enabled": False,
            "policy_single_name_limit_cny": single_name_limit,
            "adapter_diagnostics": diagnostics,
            "account_authority": {
                "status": "pass",
                "reason": "fresh_server_local_strategy_account_ready",
                "source": "server_local_sim_ledger",
                "trade_date": _compact_trade_date(authority.get("trade_date") or date),
                "capital_authority_id": str(
                    authority.get("capital_authority_id") or ""
                ),
                "authority_generation": authority.get("authority_generation"),
                "execution_lineage_id": str(
                    authority.get("execution_lineage_id") or ""
                ),
                "real_trading_enabled": False,
            },
        }
    )
    if evolution_decision:
        plan["evolution_decision"] = {
            "state": evolution_decision.get("state"),
            "recommended_action": evolution_decision.get("recommended_action"),
            "reasons": evolution_decision.get("reasons", []),
            "policy": evolution_decision.get("policy", {}),
            "used_as_risk_context": True,
            "authoritative_for_cash_or_positions": False,
        }
    plan["reason"] = (
        "capital_plan_ready" if has_safe_budget else "capital_plan_no_new_buy_budget"
    )
    return plan


def _execution_gate(
    *,
    reader: Any,
    date: str,
    candidate: dict[str, Any] | None,
    capital_plan: dict[str, Any],
    ashare_capital_state: dict[str, Any],
    now: datetime,
) -> dict[str, Any]:
    blockers: list[str] = []
    warnings: list[str] = []
    if str(capital_plan.get("status") or "") == "fail":
        blockers.append(
            str(
                capital_plan.get("reason")
                or "server_local_strategy_account_unavailable"
            )
        )
    if str(ashare_capital_state.get("status") or "") != "pass":
        blockers.append(
            str(ashare_capital_state.get("reason") or "ashare_capital_unavailable")
        )
    if not candidate:
        return {
            "status": "fail" if blockers else "warn",
            "ready": False,
            "reason": blockers[0] if blockers else "no_candidate_for_synthetic_order",
            "blockers": blockers or ["no_candidate"],
            "warnings": warnings,
            "synthetic_order": {},
            "market_reservation_performed": False,
            "execution_performed": False,
            "market_session_check": {
                "would_execute_now": False,
                "message": _market_session_rejection(
                    {"now": now.isoformat(timespec="seconds")}
                )
                or "regular_session",
            },
        }

    symbol = str(candidate.get("ts_code") or candidate.get("code") or "")
    if not _is_supported_ashare_code(symbol):
        blockers.append("unsupported_ashare_code")
    price = _latest_price(reader, "ashare", symbol, date, 0.0)
    if price <= 0:
        blockers.append("missing_or_non_positive_price")
    budgets = (
        capital_plan.get("position_budget_by_symbol")
        if isinstance(capital_plan.get("position_budget_by_symbol"), dict)
        else {}
    )
    requested_budget = _safe_float(budgets.get(symbol), 0.0)
    if requested_budget <= 0:
        suggested = (
            capital_plan.get("suggested_buys")
            if isinstance(capital_plan.get("suggested_buys"), list)
            else []
        )
        for row in suggested:
            if not isinstance(row, dict):
                continue
            if str(row.get("code") or row.get("ts_code") or "") == symbol:
                requested_budget = _safe_float(
                    row.get("executable_budget", row.get("allocation")), 0.0
                )
                break
    policy_single_name_limit = _safe_float(
        capital_plan.get("policy_single_name_limit_cny"),
        float(TOTAL_CAPITAL) * 0.15,
    )
    ashare_available = _safe_float(
        ashare_capital_state.get("available_ashare_capacity_cny"), 0.0
    )
    budget = round(
        min(
            max(0.0, requested_budget),
            max(0.0, policy_single_name_limit),
            max(0.0, ashare_available),
        ),
        2,
    )
    if requested_budget > 0 and ashare_available <= 0 and not blockers:
        blockers.append("ashare_capacity_exhausted")
    if requested_budget <= 0:
        if int(capital_plan.get("max_new_positions") or 0) <= 0:
            warnings.append("capital_plan_no_new_buy_budget")
        else:
            blockers.append("no_position_budget")
    quantity = int(budget // max(price, 1e-9)) if budget > 0 and price > 0 else 0
    quantity = (quantity // 100) * 100 if quantity > 0 else 0
    estimated_reservation = 0.0
    while quantity >= 100:
        estimated_reservation = _estimate_ashare_market_reservation(
            {
                "market": "ashare",
                "ts_code": symbol,
                "side": "buy",
                "quantity": quantity,
                "price": price,
            }
        )
        if estimated_reservation <= budget + 0.01:
            break
        quantity -= 100
    if quantity < 100:
        quantity = 0
        estimated_reservation = 0.0
    if budget > 0 and quantity <= 0:
        blockers.append("quantity_below_100_lot")
    session_message = _market_session_rejection(
        {"now": now.isoformat(timespec="seconds")}
    )
    if session_message:
        warnings.append("outside_regular_session_now_expected_for_preopen")
    risk_unit_key = symbol.strip().upper()
    execution_lineage = str(
        ashare_capital_state.get("execution_lineage_id") or ""
    ).strip()
    authority_generation = ashare_capital_state.get("authority_generation", 1)
    ashare_event_id = str(ashare_capital_state.get("event_id") or "")
    order = {
        "ts_code": symbol,
        "side": "buy",
        "quantity": quantity,
        "price": round(price, 4),
        "budget": round(budget, 2),
        "requested_budget": round(max(0.0, requested_budget), 2),
        "policy_single_name_limit_cny": round(max(0.0, policy_single_name_limit), 2),
        "ashare_available_capacity_cny": round(max(0.0, ashare_available), 2),
        "estimated_reservation_cny": round(estimated_reservation, 2),
        "trade_date": date,
        "market": "ashare",
        "capital_layer": "simulated",
        "account_type": "simulated",
        "capital_scope": "strategy",
        "risk_unit_key": risk_unit_key,
        "authority_generation": authority_generation,
        "execution_lineage_id": execution_lineage,
        "ashare_capital_event_id": ashare_event_id,
        "account_source": "server_local_sim_ledger",
        "candidate_pool_layer": "candidate",
        "execution_source": "ashare_candidate_layer",
        "dry_run": True,
    }
    no_budget_by_plan = "capital_plan_no_new_buy_budget" in warnings and not [
        item for item in blockers if item != "missing_or_non_positive_price"
    ]
    if no_budget_by_plan and "missing_or_non_positive_price" in blockers:
        blockers = [
            item for item in blockers if item != "missing_or_non_positive_price"
        ]
    return {
        "status": "pass" if not blockers else "fail",
        "ready": not blockers and not no_budget_by_plan,
        "reason": (
            blockers[0]
            if blockers
            else "capital_plan_no_new_buy_budget"
            if no_budget_by_plan
            else "synthetic_order_gate_ready"
        ),
        "blockers": blockers,
        "warnings": warnings,
        "synthetic_order": order,
        "market_reservation_performed": False,
        "execution_performed": False,
        "market_session_check": {
            "would_execute_now": not bool(session_message),
            "message": session_message or "regular_session",
        },
    }


def run_preopen_dry_run(
    *,
    now: datetime | None = None,
    reader: Any | None = None,
    score_limit: int | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    timings: dict[str, float] = {}

    def _mark(name: str, section_start: float) -> None:
        timings[name] = round(time.perf_counter() - section_start, 3)

    current = now or _now_cn()
    date = _trade_date(current)
    data_reader = reader or TradingagentDataReader()
    adapter = AshareAdapter(reader=data_reader)
    resolved_score_limit = int(
        score_limit
        or os.environ.get("ASHARE_PREOPEN_DRY_RUN_SCORE_LIMIT", "")
        or DEFAULT_SCORE_LIMIT
    )

    section_started = time.perf_counter()
    try:
        data = _api_daily_coverage_from_reader(
            data_reader, now=current, min_symbols=MIN_SYMBOLS
        )
    except Exception as exc:
        data = {
            "status": "fail",
            "reason": "sharedsignals_api_daily_unavailable",
            "symbol_count": 0,
            "data_source": "SharedSignals API",
            "error": f"{exc.__class__.__name__}: {exc}",
        }
    _mark("data_seconds", section_started)
    data_status = str(data.get("status") or "warn").lower()
    data_section = {
        "status": data_status,
        "reason": data.get("reason"),
        "symbol_count": data.get("symbol_count"),
        "asset_count": data.get("asset_count"),
        "daily_coverage_ratio": data.get("daily_coverage_ratio"),
        "expected_evidence_date": data.get("expected_evidence_date"),
        "latest_trade_date": data.get("latest_trade_date"),
        "latest_daily_age_days": data.get("latest_daily_age_days"),
        "max_daily_age_days": data.get("max_daily_age_days"),
        "min_coverage_ratio": data.get("min_coverage_ratio"),
        "data_source": data.get("data_source") or "SharedSignals API",
    }

    section_started = time.perf_counter()
    candidate_pool = _build_candidate_pool(
        reader=data_reader,
        date=date,
        score_limit=resolved_score_limit,
    )
    _mark("candidate_pool_seconds", section_started)
    section_started = time.perf_counter()
    ashare_capital_state = _ashare_capital_section(date)
    _mark("ashare_capital_seconds", section_started)
    section_started = time.perf_counter()
    capital_plan = _build_capital_plan(
        adapter,
        candidate_pool["candidates_for_plan"],
        date=date,
        ashare_capital_state=ashare_capital_state,
    )
    _mark("capital_plan_seconds", section_started)
    top_candidate = (
        candidate_pool["candidates_for_plan"][0]
        if candidate_pool["candidates_for_plan"]
        else None
    )
    section_started = time.perf_counter()
    execution_gate = _execution_gate(
        reader=data_reader,
        date=date,
        candidate=top_candidate,
        capital_plan=capital_plan,
        ashare_capital_state=ashare_capital_state,
        now=current,
    )
    _mark("execution_gate_seconds", section_started)

    sections = [
        data_section,
        candidate_pool,
        ashare_capital_state,
        capital_plan,
        execution_gate,
    ]
    blockers: list[str] = []
    warnings: list[str] = []
    for section_name, section in (
        ("data", data_section),
        ("candidate_pool", candidate_pool),
        ("ashare_capital", ashare_capital_state),
        ("capital_plan", capital_plan),
        ("execution_gate", execution_gate),
    ):
        status = str(section.get("status") or "warn").lower()
        if status == "fail":
            blockers.append(f"{section_name}:{section.get('reason') or 'failed'}")
        elif status == "warn":
            warnings.append(f"{section_name}:{section.get('reason') or 'warning'}")
    warnings.extend(str(item) for item in execution_gate.get("warnings", []) if item)

    # Propagate upstream data failure into the execution gate itself. A gate
    # cannot be reported as passing when its price/universe evidence is invalid,
    # even when the capital plan independently has no budget for a new order.
    if data_section["status"] == "fail":
        execution_gate["upstream_reason"] = execution_gate.get("reason")
        execution_gate["status"] = "fail"
        execution_gate["ready"] = False
        execution_gate["reason"] = "api_data_failure"
        gate_blockers = execution_gate.get("blockers")
        if not isinstance(gate_blockers, list):
            gate_blockers = []
            execution_gate["blockers"] = gate_blockers
        if "api_data_failure" not in gate_blockers:
            gate_blockers.append("api_data_failure")
        if "execution_gate:api_data_failure" not in blockers:
            blockers.append("execution_gate:api_data_failure")

    report = {
        "report_type": "ashare_preopen_dry_run",
        "market": "ashare",
        "generated_at": current.isoformat(timespec="seconds"),
        "timezone": "Asia/Shanghai",
        "trade_date": date,
        "status": _overall_status(sections),
        "read_only": True,
        "dry_run": True,
        "real_trading_enabled": False,
        "writes_excluded": ["signals", "ledger", "pending", "review"],
        "run_config": {
            "score_limit": resolved_score_limit,
            "default_score_limit": DEFAULT_SCORE_LIMIT,
            "candidate_threshold": CANDIDATE_THRESHOLD,
        },
        "data": data_section,
        "candidate_pool": {
            key: value
            for key, value in candidate_pool.items()
            if key != "candidates_for_plan"
        },
        "ashare_capital": ashare_capital_state,
        "capital_plan": capital_plan,
        "execution_gate": execution_gate,
        "blockers": blockers,
        "warnings": warnings,
        "next_actions": _next_actions(blockers, warnings),
    }
    timings["total_seconds"] = round(time.perf_counter() - started, 3)
    report["timings_seconds"] = timings
    return report


def _next_actions(blockers: list[str], warnings: list[str]) -> list[str]:
    if blockers:
        actions: list[str] = []
        if any("data:" in item for item in blockers):
            actions.append("先修复 SharedSignals A股日线/覆盖数据，再进入开盘模拟。")
        if any("candidate_pool:" in item for item in blockers) or any(
            "candidate_pool:" in item for item in warnings
        ):
            actions.append("复核 A股候选池阈值、流动性过滤和六维打分输入。")
        if any("ashare_capital:" in item for item in blockers):
            actions.append(
                "先完成当日 ashare 市场资金对账，确认 generation 1、sim-only 与风险预算后再开盘模拟。"
            )
        if any("capital_plan:" in item for item in blockers):
            actions.append(
                "复核唯一 50,000 CNY server-local strategy 账本、fresh lineage、现金和持仓。"
            )
        if any("execution_gate:" in item for item in blockers):
            actions.append("复核候选价格、100股整手、来源字段和执行门禁。")
        return actions or ["复核失败项后重跑盘前 dry-run。"]
    if warnings:
        return ["允许开盘继续观察；若候选为空，系统会安全空跑并记录原因。"]
    return ["盘前 dry-run 通过，等待开盘后的 5 分钟数据与模拟成交验收。"]


def write_outputs(report: dict[str, Any], *, append_history: bool = True) -> None:
    LATEST.parent.mkdir(parents=True, exist_ok=True)
    LATEST.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if append_history:
        with HISTORY.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(report, ensure_ascii=False) + "\n")


def render_text(report: dict[str, Any]) -> str:
    lines = [
        f"A股盘前 dry-run：{report.get('status')}",
        f"时间：{report.get('generated_at')}",
        f"交易日：{report.get('trade_date')}",
        f"数据：{report.get('data', {}).get('status')}；最新日线={report.get('data', {}).get('latest_trade_date')}；覆盖={report.get('data', {}).get('symbol_count')}",
        f"候选池：{report.get('candidate_pool', {}).get('status')}；候选={report.get('candidate_pool', {}).get('candidate_count')}；已打分={report.get('candidate_pool', {}).get('scored_count')}",
        f"A股资金：{report.get('ashare_capital', {}).get('status')}；generation={report.get('ashare_capital', {}).get('authority_generation')}；fresh={report.get('ashare_capital', {}).get('fresh')}；可用={report.get('ashare_capital', {}).get('available_ashare_capacity_cny')}",
        f"资金计划：{report.get('capital_plan', {}).get('status')}；现金={report.get('capital_plan', {}).get('cash_available')}；目标持仓={report.get('capital_plan', {}).get('target_positions')}；风险模式={report.get('capital_plan', {}).get('risk_mode')}",
        f"执行门禁：{report.get('execution_gate', {}).get('status')}；ready={report.get('execution_gate', {}).get('ready')}",
    ]
    blockers = (
        report.get("blockers") if isinstance(report.get("blockers"), list) else []
    )
    warnings = (
        report.get("warnings") if isinstance(report.get("warnings"), list) else []
    )
    if blockers:
        lines.append("阻断：" + "；".join(str(item) for item in blockers))
    if warnings:
        lines.append("提醒：" + "；".join(str(item) for item in warnings))
    lines.append("下一步：")
    lines.extend(f"- {item}" for item in report.get("next_actions", []))
    return "\n".join(lines)


def _send_alert(report: dict[str, Any], rendered_text: str) -> dict[str, Any]:
    status = str(report.get("status") or "warn")
    subject = f"[TradingAgent][A股盘前dry-run] {status} {report.get('trade_date', '')}"
    html = (
        "<!DOCTYPE html><html><body>"
        "<h2>TradingAgent A股盘前 dry-run 异常</h2>"
        f"<pre style=\"white-space:pre-wrap;font-family:-apple-system,'PingFang SC',sans-serif;\">{rendered_text}</pre>"
        "</body></html>"
    )
    return email_sender.send_email(
        email_sender.CHANNELS["system"]["to"],
        subject,
        rendered_text,
        html,
        channel="system",
        rate_limit_type=f"ashare_preopen_dry_run:{status}",
    )


def maybe_send_alert(
    report: dict[str, Any], rendered_text: str, send_on: str
) -> dict[str, Any]:
    status = str(report.get("status") or "warn")
    should_send = send_on == "warn" and status != "pass"
    should_send = should_send or (send_on == "fail" and status == "fail")
    if not should_send:
        return {
            "status": "skipped",
            "reason": "ashare_preopen_dry_run_pass_or_send_disabled",
        }
    return _send_alert(report, rendered_text)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only A-share pre-open dry run.")
    parser.add_argument("--now", default=None)
    parser.add_argument("--score-limit", type=int, default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--pretty", action="store_true")
    parser.add_argument("--send-on", choices=["warn", "fail", "never"], default="never")
    parser.add_argument(
        "--no-write",
        action="store_true",
        help="Do not write latest/history report files.",
    )
    parser.add_argument(
        "--exit-zero",
        action="store_true",
        help="Return 0 after reporting so cron does not retry identical alerts.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = run_preopen_dry_run(
        now=_now_cn(args.now) if args.now else None,
        score_limit=args.score_limit,
    )
    rendered = render_text(report)
    email_result = maybe_send_alert(report, rendered, args.send_on)
    report["email"] = email_result
    if not args.no_write:
        write_outputs(report)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2 if args.pretty else None))
    else:
        print(rendered)
        if email_result.get("status") not in {"skipped", "rate_limited"}:
            print(f"邮件: {email_result.get('status')} -> {email_result.get('to')}")
    if args.exit_zero:
        return 0
    return 2 if report["status"] == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
