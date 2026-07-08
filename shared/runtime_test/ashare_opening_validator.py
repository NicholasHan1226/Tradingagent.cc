#!/usr/bin/env python3
"""Read-only opening acceptance for A-share simulated trading.

Validates SharedSignals 5-minute data, server-local simulated trade samples,
receipts and review artifacts after the market opens. Alerts are produced only
when anomalies are detected; the script never creates orders or writes ledger
state.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

DEFAULT_SQLITE_DB = Path("/opt/investment/MarketGraphRuntime/read_model/marketdata.sqlite")
CN_TZ = timezone(timedelta(hours=8))
NO_TRADE_LOG = Path(__file__).resolve().parents[1] / "logs" / "ashare_no_trade_explanations.jsonl"
MAX_PRE_OPEN_DAILY_AGE_DAYS = 5


def _now_cn() -> datetime:
    return datetime.now(CN_TZ)


def _parse_now(value: str | None) -> datetime:
    if not value:
        return _now_cn()
    parsed = datetime.fromisoformat(value.replace(" ", "T", 1))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=CN_TZ)
    return parsed.astimezone(CN_TZ)


def _session_start(now: datetime) -> tuple[str, datetime | None]:
    current = now.time()
    if time(9, 30) <= current <= time(11, 30):
        return "morning", datetime.combine(now.date(), time(9, 30), tzinfo=CN_TZ)
    if time(13, 0) <= current <= time(15, 0):
        return "afternoon", datetime.combine(now.date(), time(13, 0), tzinfo=CN_TZ)
    return "closed", None


def _pre_open_session(now: datetime) -> tuple[str, datetime | None]:
    current = now.time()
    if time(8, 0) <= current < time(9, 30):
        return "morning", datetime.combine(now.date(), time(9, 30), tzinfo=CN_TZ)
    if time(11, 30) <= current < time(13, 0):
        return "afternoon", datetime.combine(now.date(), time(13, 0), tzinfo=CN_TZ)
    return "closed", None


def _query_daily_bars(db_path: Path, trade_date: str) -> dict[str, Any]:
    if not db_path.exists():
        return {"error": f"sqlite database not found: {db_path}", "symbol_count": 0}
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        row = conn.execute(
            """
            SELECT COUNT(*), COUNT(DISTINCT symbol), MIN(trade_date), MAX(trade_date)
            FROM market_bars_daily
            WHERE market='Ashare'
              AND trade_date <= ?
              AND close > 0
            """,
            (trade_date,),
        ).fetchone()
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{exc.__class__.__name__}: {exc}", "symbol_count": 0}
    finally:
        if conn is not None:
            conn.close()
    return {
        "daily_bar_count": int(row[0] or 0) if row else 0,
        "symbol_count": int(row[1] or 0) if row else 0,
        "first_trade_date": row[2] if row else None,
        "latest_trade_date": row[3] if row else None,
    }


def _compact_date(value: str) -> date | None:
    raw = str(value or "").strip().replace("-", "")[:8]
    if len(raw) != 8 or not raw.isdigit():
        return None
    try:
        return date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))
    except ValueError:
        return None


def _daily_age_days(latest_trade_date: Any, current: datetime) -> int | None:
    latest = _compact_date(str(latest_trade_date or ""))
    if latest is None:
        return None
    return max(0, (current.date() - latest).days)


def _query_session_bars(db_path: Path, start: datetime, now: datetime) -> dict[str, Any]:
    if not db_path.exists():
        return {"error": f"sqlite database not found: {db_path}", "symbol_count": 0, "bar_count": 0}
    start_text = start.strftime("%Y-%m-%d %H:%M:%S")
    now_text = now.strftime("%Y-%m-%d %H:%M:%S")
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        row = conn.execute(
            """
            SELECT COUNT(*), COUNT(DISTINCT symbol), MIN(bar_time), MAX(bar_time)
            FROM market_bars_intraday
            WHERE market='Ashare'
              AND COALESCE(interval, '') IN ('5min', '5MIN', '5')
              AND bar_time >= ?
              AND bar_time <= ?
            """,
            (start_text, now_text),
        ).fetchone()
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{exc.__class__.__name__}: {exc}", "symbol_count": 0, "bar_count": 0}
    finally:
        if conn is not None:
            conn.close()
    return {
        "bar_count": int(row[0] or 0) if row else 0,
        "symbol_count": int(row[1] or 0) if row else 0,
        "first_bar_time": row[2] if row else None,
        "latest_bar_time": row[3] if row else None,
    }


def _count_jsonl_rows(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip())


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _count_local_sim_trades(path: Path, date: str) -> int:
    if not path.exists():
        return 0
    count = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        market = str(payload.get("market") or payload.get("market_type") or "ashare").lower()
        if market not in {"ashare", "a_share", "a-share"}:
            continue
        trade_date = str(
            payload.get("trade_date")
            or payload.get("date")
            or payload.get("filled_at")
            or payload.get("executed_at")
            or payload.get("timestamp")
            or ""
        )[:10].replace("-", "")
        if trade_date == date:
            count += 1
    return count


def _count_filled_signals(signals_dir: Path, date: str) -> int:
    filled_dir = signals_dir / "filled"
    if not filled_dir.exists():
        return 0
    trade_date = date.replace("-", "")
    count = 0
    for path in filled_dir.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        if str(payload.get("market") or "").lower() != "ashare":
            continue
        card_date = str(payload.get("trade_date") or payload.get("valid_until") or "")[:10].replace("-", "")
        if card_date == trade_date:
            count += 1
    return count


def _signal_status_counts(signals_dir: Path, date: str) -> dict[str, int]:
    trade_date = date.replace("-", "")
    counts = {
        "pending": 0,
        "claimed": 0,
        "running": 0,
        "filled": 0,
        "failed": 0,
        "partial": 0,
        "expired": 0,
        "cancelled": 0,
    }
    for status in counts:
        bucket = signals_dir / status
        if not bucket.exists():
            continue
        for path in bucket.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            if str(payload.get("market") or "").lower() != "ashare":
                continue
            card_date = str(payload.get("trade_date") or payload.get("valid_until") or payload.get("created_at") or "")[:10].replace("-", "")
            if card_date == trade_date:
                counts[status] += 1
    return counts


def _count_market_receipts(receipt_path: Path, date: str) -> int:
    if not receipt_path.exists():
        return 0
    count = 0
    for line in receipt_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        if str(payload.get("market") or "").lower() != "ashare":
            continue
        receipt_date = str(payload.get("trade_date") or payload.get("receipt_at") or "")[:10].replace("-", "")
        if receipt_date == date:
            count += 1
    return count


def _latest_no_trade_explanation(path: Path, date: str) -> dict[str, Any]:
    rows = _read_jsonl(path)
    for payload in reversed(rows):
        raw_date = str(payload.get("date") or payload.get("trade_date") or payload.get("generated_at") or "")[:10].replace("-", "")
        if raw_date and raw_date != date:
            continue
        explanation = payload.get("no_trade_explanation")
        if isinstance(explanation, dict):
            score_diagnostics = explanation.get("score_diagnostics", {})
            if not isinstance(score_diagnostics, dict):
                score_diagnostics = {}
            diagnostic_summary = _score_diagnostic_summary(score_diagnostics)
            return {
                "path": str(path),
                "generated_at": payload.get("generated_at"),
                "state": payload.get("state"),
                "category": explanation.get("category"),
                "action": explanation.get("action") or explanation.get("next_action"),
                "counts": explanation.get("counts", {}),
                "sample_risk_rejections": explanation.get("sample_risk_rejections", [])[:5],
                "sample_execution_skips": explanation.get("sample_execution_skips", [])[:5],
                "sample_errors": explanation.get("sample_errors", [])[:3],
                "score_diagnostics": score_diagnostics,
                "candidate_pool_status": score_diagnostics.get("candidate_pool_status"),
                "data_quality_status": score_diagnostics.get("data_quality_status"),
                "max_combined": score_diagnostics.get("max_combined"),
                "candidate_threshold": score_diagnostics.get("candidate_threshold"),
                "candidate_above_threshold_count": score_diagnostics.get("candidate_above_threshold_count"),
                "watch_above_threshold_count": score_diagnostics.get("watch_above_threshold_count"),
                "diagnostic_summary": diagnostic_summary,
            }
    return {}


def _score_diagnostic_summary(score_diagnostics: dict[str, Any]) -> dict[str, Any]:
    candidate_pool_status = str(score_diagnostics.get("candidate_pool_status") or "")
    data_quality_status = str(score_diagnostics.get("data_quality_status") or "")
    candidate_count = int(score_diagnostics.get("candidate_above_threshold_count") or 0)
    watch_count = int(score_diagnostics.get("watch_above_threshold_count") or 0)
    max_combined = score_diagnostics.get("max_combined")
    candidate_threshold = score_diagnostics.get("candidate_threshold")

    summary: dict[str, Any] = {
        "candidate_pool_status": candidate_pool_status or None,
        "data_quality_status": data_quality_status or None,
        "max_combined": max_combined,
        "candidate_threshold": candidate_threshold,
        "candidate_above_threshold_count": candidate_count,
        "watch_above_threshold_count": watch_count,
        "evidence_reason_summary": score_diagnostics.get("evidence_reason_summary") or {},
    }
    evidence_actions = _evidence_gap_actions(score_diagnostics.get("evidence_reason_summary"))
    if candidate_pool_status == "pool_empty_despite_threshold_scores":
        summary.update(
            {
                "reason": "candidate_pool_anomaly",
                "next_action": "review_candidate_pool_layering_anomaly",
            }
        )
    elif candidate_pool_status == "candidates_ready" or candidate_count > 0:
        summary.update(
            {
                "reason": "candidate_pool_ready",
                "next_action": "continue_opening_observation",
            }
        )
    elif evidence_actions:
        summary.update(
            {
                "reason": "research_evidence_missing_default_neutral",
                "next_action": evidence_actions[0],
                "next_actions": evidence_actions,
            }
        )
    elif data_quality_status == "missing_evidence_default_like":
        summary.update(
            {
                "reason": "research_evidence_missing_default_neutral",
                "next_action": "review_sharedsignals_marketgraph_dimension_evidence",
            }
        )
    elif data_quality_status == "research_dimensions_mostly_neutral":
        summary.update(
            {
                "reason": "research_dimensions_neutral",
                "next_action": "review_research_dimension_coverage",
            }
        )
    elif candidate_pool_status == "no_scored_symbols":
        summary.update(
            {
                "reason": "score_coverage_missing",
                "next_action": "check_ashare_score_universe_and_data_reader",
            }
        )
    elif candidate_pool_status in {"strategy_threshold_not_met", "strategy_threshold_not_met_watch_only"}:
        summary.update(
            {
                "reason": "strategy_threshold_not_met",
                "next_action": "monitor_strategy_threshold_gap",
            }
        )
    else:
        summary.update({"reason": None, "next_action": None})
    return summary


def _evidence_gap_actions(evidence_reason_summary: Any) -> list[str]:
    if not isinstance(evidence_reason_summary, dict):
        return []
    reason_actions = {
        "missing_regime": "check_marketgraph_all_weather_regime",
        "missing_fundamental_rows": "check_sharedsignals_fundamentals",
        "no_supported_fundamental_factors": "check_sharedsignals_fundamentals",
        "missing_capital_flow_rows": "check_sharedsignals_capital_flow",
        "insufficient_daily_bars": "check_sharedsignals_daily_bar_history",
        "insufficient_priced_daily_bars": "check_sharedsignals_daily_bar_history",
        "invalid_momentum_base_price": "check_sharedsignals_daily_bar_history",
        "no_matched_event_evidence": "check_marketgraph_event_candidates",
        "missing_sentiment_rows": "check_marketgraph_sentiment_signals",
        "scoring_exception": "review_scorer_errors",
        "scoring_returned_none": "review_scorer_errors",
    }
    priority = [
        "check_sharedsignals_daily_bar_history",
        "check_sharedsignals_capital_flow",
        "check_sharedsignals_fundamentals",
        "check_marketgraph_all_weather_regime",
        "check_marketgraph_event_candidates",
        "check_marketgraph_sentiment_signals",
        "review_scorer_errors",
    ]
    actions: list[str] = []
    for reason_counts in evidence_reason_summary.values():
        if not isinstance(reason_counts, dict):
            continue
        for reason in reason_counts:
            action = reason_actions.get(str(reason))
            if action and action not in actions:
                actions.append(action)
    actions.sort(key=lambda action: priority.index(action) if action in priority else len(priority))
    return actions


def _classify_from_latest_no_trade(latest: dict[str, Any]) -> tuple[str, str] | None:
    category = str(latest.get("category") or "")
    action = str(latest.get("action") or "")
    if not category:
        return None
    diagnostic_summary = latest.get("diagnostic_summary")
    if isinstance(diagnostic_summary, dict) and diagnostic_summary.get("next_action"):
        action = str(diagnostic_summary["next_action"])
    mapped = {
        "no_universe": ("no_universe", "check_sharedsignals_assets_and_daily_coverage"),
        "no_candidates": ("no_candidates", "check_candidate_pool_thresholds_and_universe_filter"),
        "all_candidates_missing_price": ("candidate_price_missing", "check_sharedsignals_daily_or_realtime_prices"),
        "all_rejected_by_risk": ("all_rejected_by_risk", "review_risk_rejections"),
        "no_portfolio_orders": ("no_portfolio_orders", "check_position_sizing_and_portfolio_constructor"),
        "portfolio_empty": ("portfolio_empty_or_capital_lot_blocked", "check_capital_lot_size_and_constructor_output"),
        "duplicate_existing_signal": ("duplicate_existing_signal", "review_same_day_idempotency_state"),
        "execution_skipped": ("execution_skipped", "review_execution_skip_reasons"),
        "execution_failed": ("execution_failed", "review_failed_receipts"),
        "pending_execution": ("pending_execution", "review_pending_signal_state"),
        "degraded_errors": ("degraded_errors", "review_orchestrator_errors"),
        "no_filled_sim_orders": ("no_filled_sim_orders", "review_full_sim_run"),
    }
    if category in mapped:
        mapped_category, mapped_action = mapped[category]
        return mapped_category, action or mapped_action
    return category, action or "review_latest_no_trade_log"


def _explain_no_trade(
    *,
    bars: dict[str, Any],
    local_sim_count: int,
    receipt_count: int,
    filled_signal_count: int,
    review_count: int,
    signal_status_counts: dict[str, int] | None = None,
    latest_no_trade: dict[str, Any] | None = None,
    elapsed_minutes: int | None,
    wait_minutes: int,
    min_symbols: int,
) -> dict[str, Any]:
    """Classify why the opening probe has not produced a simulated A-share trade."""

    bar_count = int(bars.get("bar_count") or 0)
    symbol_count = int(bars.get("symbol_count") or 0)
    signal_counts = signal_status_counts or {}
    latest_no_trade = latest_no_trade or {}
    latest_classification = _classify_from_latest_no_trade(latest_no_trade)
    if bars.get("error"):
        category = "data_query_failed"
        action = "check_sharedsignals_read_model"
    elif elapsed_minutes is not None and elapsed_minutes < max(1, int(wait_minutes)):
        category = "not_due_yet"
        action = "wait_until_first_sample_window"
    elif bar_count <= 0:
        category = "no_5min_data"
        action = "check_sharedsignals_p0_5min_collection"
    elif symbol_count < max(1, int(min_symbols)):
        category = "low_5min_coverage"
        action = "check_sharedsignals_symbol_coverage"
    elif latest_classification is not None:
        category, action = latest_classification
    elif local_sim_count > 0 and receipt_count <= 0:
        category = "receipt_missing"
        action = "check_sim_execution_receipt_writer"
    elif review_count <= 0 and local_sim_count > 0:
        category = "review_pending"
        action = "wait_for_review_or_run_daily_review"
    elif sum(int(signal_counts.get(key, 0)) for key in ("pending", "filled", "failed", "partial", "expired", "cancelled")) <= 0:
        category = "no_signal_cards_created"
        action = "check_signal_generation_thresholds"
    elif local_sim_count <= 0 and filled_signal_count <= 0:
        category = "no_trade_signal_or_all_rejected"
        action = "check_signal_generation_and_risk_rejections"
    elif filled_signal_count > 0 and local_sim_count <= 0:
        category = "execution_missing"
        action = "check_server_local_sim_executor"
    else:
        category = "trade_loop_ready"
        action = "continue_monitoring"
    return {
        "category": category,
        "next_action": action,
        "diagnostic_summary": latest_no_trade.get("diagnostic_summary", {}),
        "inputs": {
            "bar_count": bar_count,
            "symbol_count": symbol_count,
            "min_symbols": max(1, int(min_symbols)),
            "local_sim_trades": local_sim_count,
            "filled_signals": filled_signal_count,
            "sim_execution_receipts": receipt_count,
            "daily_reviews": review_count,
            "signals": signal_counts,
            "elapsed_minutes": elapsed_minutes,
            "wait_minutes": max(1, int(wait_minutes)),
        },
        "latest_no_trade_log": latest_no_trade,
    }


def _expected_scientific_no_trade(explanation: dict[str, Any]) -> bool:
    category = str(explanation.get("category") or "")
    latest_log = explanation.get("latest_no_trade_log")
    if not isinstance(latest_log, dict) or not latest_log:
        return False
    if category in {
        "no_portfolio_orders",
        "all_rejected_by_risk",
        "duplicate_existing_signal",
        "no_signal_cards_created",
        "no_trade_signal_or_all_rejected",
    }:
        return True
    if category == "no_candidates":
        diagnostics = explanation.get("diagnostic_summary")
        latest = explanation.get("latest_no_trade_log")
        if isinstance(latest, dict):
            data_quality = str(latest.get("data_quality_status") or "")
        else:
            data_quality = ""
        if data_quality.startswith("missing_"):
            return False
        if isinstance(diagnostics, dict):
            reason = str(diagnostics.get("reason") or "")
            if reason in {"research_dimensions_neutral", "strategy_threshold_not_met"}:
                return True
        return True
    return False


def _has_warning_alerts(alerts: list[dict[str, Any]]) -> bool:
    return any(str(alert.get("severity") or "").lower() in {"warn", "warning", "error", "critical"} for alert in alerts)


def validate_pre_open(
    *,
    sqlite_db: Path = DEFAULT_SQLITE_DB,
    now: datetime | None = None,
    min_symbols: int = 1000,
) -> dict[str, Any]:
    current = now or _now_cn()
    if current.tzinfo is None:
        current = current.replace(tzinfo=CN_TZ)
    else:
        current = current.astimezone(CN_TZ)
    session_name, start = _pre_open_session(current)
    result: dict[str, Any] = {
        "market": "ashare",
        "report_type": "pre_open_acceptance",
        "checked_at": current.isoformat(timespec="seconds"),
        "sqlite_db": str(sqlite_db),
        "data_source": "SharedSignals read_model",
        "read_only": True,
        "session": session_name,
        "session_start": start.isoformat(timespec="seconds") if start else None,
        "min_symbols": max(1, int(min_symbols)),
        "real_trading_enabled": False,
    }
    if start is None:
        return {**result, "status": "warn", "reason": "not_in_pre_open_window"}
    bars = _query_daily_bars(sqlite_db, start.strftime("%Y%m%d"))
    result.update(bars)
    daily_age = _daily_age_days(bars.get("latest_trade_date"), current)
    result["latest_daily_age_days"] = daily_age
    result["max_daily_age_days"] = MAX_PRE_OPEN_DAILY_AGE_DAYS
    if bars.get("error"):
        result["status"] = "fail"
        result["reason"] = "pre_open_daily_query_failed"
    elif int(bars.get("symbol_count") or 0) < max(1, int(min_symbols)):
        result["status"] = "warn"
        result["reason"] = "pre_open_daily_bars_missing"
    elif daily_age is None or daily_age > MAX_PRE_OPEN_DAILY_AGE_DAYS:
        result["status"] = "warn"
        result["reason"] = "pre_open_daily_bars_stale"
    else:
        result["status"] = "pass"
        result["reason"] = "pre_open_acceptance_passed"
    return result


def validate_opening(
    *,
    sqlite_db: Path = DEFAULT_SQLITE_DB,
    now: datetime | None = None,
    min_symbols: int = 10,
) -> dict[str, Any]:
    current = now or _now_cn()
    if current.tzinfo is None:
        current = current.replace(tzinfo=CN_TZ)
    else:
        current = current.astimezone(CN_TZ)
    session_name, start = _session_start(current)
    result: dict[str, Any] = {
        "market": "ashare",
        "report_type": "opening_validation",
        "checked_at": current.isoformat(timespec="seconds"),
        "sqlite_db": str(sqlite_db),
        "data_source": "SharedSignals read_model",
        "read_only": True,
        "session": session_name,
        "session_start": start.isoformat(timespec="seconds") if start else None,
        "min_symbols": max(1, int(min_symbols)),
        "real_trading_enabled": False,
    }
    if start is None:
        return {**result, "status": "warn", "reason": "outside_ashare_session"}
    bars = _query_session_bars(sqlite_db, start, current)
    result.update(bars)
    if bars.get("error"):
        result["status"] = "fail"
        result["reason"] = "opening_validation_query_failed"
    elif int(bars.get("bar_count") or 0) <= 0:
        result["status"] = "warn"
        result["reason"] = "opening_session_has_no_5min_bars"
    elif int(bars.get("symbol_count") or 0) < max(1, int(min_symbols)):
        result["status"] = "warn"
        result["reason"] = "opening_session_symbol_coverage_low"
    else:
        result["status"] = "pass"
        result["reason"] = "opening_session_5min_data_ready"
    return result


def first_sample_alerts(
    *,
    sqlite_db: Path = DEFAULT_SQLITE_DB,
    signals_dir: Path | None = None,
    local_sim_path: Path | None = None,
    receipt_path: Path | None = None,
    review_path: Path | None = None,
    no_trade_log_path: Path | None = None,
    now: datetime | None = None,
    min_symbols: int = 10,
    wait_minutes: int = 10,
) -> dict[str, Any]:
    current = now or _now_cn()
    if current.tzinfo is None:
        current = current.replace(tzinfo=CN_TZ)
    else:
        current = current.astimezone(CN_TZ)
    session_name, start = _session_start(current)
    elapsed_minutes = int((current - start).total_seconds() // 60) if start is not None else None

    root = Path(__file__).resolve().parents[2]
    signals_dir = signals_dir or root / "signals"
    local_sim_path = local_sim_path or root / "shared" / "logs" / "local_sim" / "local_sim_trades.jsonl"
    receipt_path = receipt_path or root / "signals" / "sim_execution_receipts.jsonl"
    review_path = review_path or root / "shared" / "review" / "data" / "daily_reviews.jsonl"
    no_trade_log_path = no_trade_log_path or NO_TRADE_LOG

    result: dict[str, Any] = {
        "market": "ashare",
        "report_type": "first_sample_alert",
        "checked_at": current.isoformat(timespec="seconds"),
        "sqlite_db": str(sqlite_db),
        "data_source": "SharedSignals read_model",
        "read_only": True,
        "session": session_name,
        "session_start": start.isoformat(timespec="seconds") if start else None,
        "elapsed_minutes": elapsed_minutes,
        "min_symbols": max(1, int(min_symbols)),
        "wait_minutes": max(1, int(wait_minutes)),
        "alerts": [],
        "real_trading_enabled": False,
    }
    if start is None:
        return {**result, "status": "warn", "reason": "outside_ashare_session"}
    if elapsed_minutes is not None and elapsed_minutes < max(1, int(wait_minutes)):
        return {**result, "status": "pass", "reason": "first_sample_check_not_due"}

    bars = _query_session_bars(sqlite_db, start, current)
    result.update(bars)
    alerts: list[dict[str, Any]] = []
    if bars.get("error"):
        alerts.append({"severity": "error", "code": "ashare_5min_check_failed", "message": "A股5分钟首样本检查无法读取 SharedSignals read model。"})
    elif int(bars.get("bar_count") or 0) <= 0 or int(bars.get("symbol_count") or 0) < max(1, int(min_symbols)):
        alerts.append({"severity": "warn", "code": "ashare_5min_missing_in_session", "message": "A股交易时段开始后仍缺少足够的5分钟数据。"})

    trade_date = current.strftime("%Y%m%d")
    local_sim_count = _count_local_sim_trades(local_sim_path, trade_date)
    receipt_count = _count_market_receipts(receipt_path, trade_date)
    review_count = _count_jsonl_rows(review_path)
    filled_signal_count = _count_filled_signals(signals_dir, trade_date)
    signal_counts = _signal_status_counts(signals_dir, trade_date)
    latest_no_trade = _latest_no_trade_explanation(no_trade_log_path, trade_date)
    result["samples"] = {
        "bar_count": int(bars.get("bar_count") or 0),
        "symbol_count": int(bars.get("symbol_count") or 0),
        "signals": signal_counts,
        "local_sim_trades": local_sim_count,
        "sim_execution_receipts": receipt_count,
        "daily_reviews": review_count,
        "filled_signals": filled_signal_count,
    }
    result["no_trade_explanation"] = _explain_no_trade(
        bars=bars,
        local_sim_count=local_sim_count,
        receipt_count=receipt_count,
        filled_signal_count=filled_signal_count,
        review_count=review_count,
        signal_status_counts=signal_counts,
        latest_no_trade=latest_no_trade,
        elapsed_minutes=elapsed_minutes,
        wait_minutes=wait_minutes,
        min_symbols=min_symbols,
    )
    expected_no_trade = local_sim_count <= 0 and _expected_scientific_no_trade(result["no_trade_explanation"])
    if local_sim_count <= 0:
        if expected_no_trade:
            alerts.append({
                "severity": "info",
                "code": "ashare_first_sim_trade_not_expected",
                "message": "A股5分钟数据已就绪，但当前资金计划、候选池或风控结果不要求新增模拟成交。",
            })
        else:
            alerts.append({"severity": "warn", "code": "ashare_first_sim_trade_missing", "message": "A股5分钟数据已进入会话窗口，但服务器本地模拟盘尚无成交样本。"})
    if local_sim_count > 0 and receipt_count <= 0:
        alerts.append({"severity": "warn", "code": "ashare_first_receipt_missing", "message": "A股已有本地模拟成交，但签名回执尚未生成。"})
    if review_count <= 0:
        alerts.append({"severity": "info", "code": "ashare_review_not_yet_run", "message": "A股复盘日志尚未生成，等待日终复盘任务。"})

    result["alerts"] = alerts
    has_warning_alerts = _has_warning_alerts(alerts)
    result["status"] = "warn" if has_warning_alerts else "pass"
    if has_warning_alerts:
        result["reason"] = "first_sample_alerts_present"
    elif expected_no_trade:
        result["reason"] = "first_sample_no_trade_explained"
    else:
        result["reason"] = "first_sample_ready"
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only A-share opening acceptance.")
    parser.add_argument("--sqlite-db", type=Path, default=DEFAULT_SQLITE_DB)
    parser.add_argument("--signals-dir", type=Path, default=None)
    parser.add_argument("--local-sim-path", type=Path, default=None)
    parser.add_argument("--receipt-path", type=Path, default=None)
    parser.add_argument("--review-path", type=Path, default=None)
    parser.add_argument("--no-trade-log-path", type=Path, default=None)
    parser.add_argument("--now", default=None)
    parser.add_argument("--min-symbols", type=int, default=10)
    parser.add_argument("--wait-minutes", type=int, default=10)
    parser.add_argument("--pre-open", action="store_true")
    parser.add_argument("--first-sample", action="store_true")
    parser.add_argument("--pretty", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    now = _parse_now(args.now)
    if args.pre_open:
        report = validate_pre_open(sqlite_db=args.sqlite_db, now=now, min_symbols=args.min_symbols)
    elif args.first_sample:
        report = first_sample_alerts(
            sqlite_db=args.sqlite_db,
            signals_dir=args.signals_dir,
            local_sim_path=args.local_sim_path,
            receipt_path=args.receipt_path,
            review_path=args.review_path,
            no_trade_log_path=args.no_trade_log_path,
            now=now,
            min_symbols=args.min_symbols,
            wait_minutes=args.wait_minutes,
        )
    else:
        report = validate_opening(sqlite_db=args.sqlite_db, now=now, min_symbols=args.min_symbols)
    print(json.dumps(report, ensure_ascii=False, indent=2 if args.pretty else None))
    return 2 if report.get("status") == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
