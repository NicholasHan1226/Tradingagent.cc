#!/usr/bin/env python3
"""Append-only review records for CN futures simulation runs."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_REVIEW_PATH = (
    Path(__file__).resolve().parents[1]
    / "shared"
    / "review"
    / "data"
    / "cn_futures_sim_reviews.jsonl"
)
STYLE_REVIEW_MARKET = "cn_futures"


def is_actionable_review(payload: dict[str, Any]) -> bool:
    """Return whether a review row contains in-session evidence worth surfacing."""

    if not payload:
        return False
    for key in ("record_count", "filled_count", "hold_count", "error_count"):
        try:
            if int(payload.get(key) or 0) > 0:
                return True
        except (TypeError, ValueError):
            continue
    if str(payload.get("latest_bar_time") or payload.get("bar_time") or "").strip():
        return True
    return False


def _compact_date(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    first_part = raw[:10]
    digits = "".join(ch for ch in first_part if ch.isdigit())
    if len(digits) >= 8:
        return digits[:8]
    digits = "".join(ch for ch in raw if ch.isdigit())
    return digits[:8] if len(digits) >= 8 else ""


def latest_actionable_review(rows: list[dict[str, Any]], *, trade_date: str | None = None) -> dict[str, Any]:
    """Prefer the newest review with hold/fill/error/bar evidence over empty close rows."""

    target_date = _compact_date(trade_date)
    if target_date:
        rows = [
            row for row in rows
            if _compact_date(row.get("date") or row.get("trade_date") or row.get("generated_at")) == target_date
        ]
    for row in reversed(rows):
        if is_actionable_review(row):
            return row
    return rows[-1] if rows else {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
        return parsed if parsed == parsed else default
    except (TypeError, ValueError):
        return default


def _record_pnl(record: dict[str, Any]) -> float | None:
    for container_key in ("performance", "pnl", "result"):
        container = record.get(container_key)
        if isinstance(container, dict):
            for key in ("realized_pnl", "net_pnl", "pnl"):
                if key in container:
                    return _safe_float(container.get(key))
    for key in ("realized_pnl", "net_pnl", "pnl"):
        if key in record:
            return _safe_float(record.get(key))
    return None


def _latest_record_value(records: list[dict[str, Any]], key: str) -> Any:
    for record in reversed(records):
        value = record.get(key)
        if value not in (None, ""):
            return value
        order = record.get("order") if isinstance(record.get("order"), dict) else {}
        value = order.get(key)
        if value not in (None, ""):
            return value
    return ""


def score_records(records: list[dict[str, Any]], *, min_sample_trades: int = 20) -> dict[str, Any]:
    """Score simulated CN futures styles from append-only review records.

    Open-only simulation fills do not prove profitability. When realized PnL is
    unavailable or sample size is small, the score is explicitly marked as
    sample_insufficient instead of implying a tradable edge.
    """

    styles: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "trade_count": 0,
            "filled_count": 0,
            "fee": 0.0,
            "margin_required": 0.0,
            "notional": 0.0,
            "realized_pnl": 0.0,
            "pnl_sample_count": 0,
            "wins": 0,
            "losses": 0,
            "max_drawdown": None,
            "score": 0.0,
            "status": "sample_insufficient",
            "sample_warning": "",
            "pnl_attribution": "sample_insufficient",
        }
    )
    equity_curves: dict[str, list[float]] = defaultdict(list)
    for record in records:
        if not isinstance(record, dict):
            continue
        style = str(record.get("style") or "unknown")
        receipt = record.get("receipt") if isinstance(record.get("receipt"), dict) else {}
        raw = receipt.get("raw_response") if isinstance(receipt.get("raw_response"), dict) else {}
        metrics = styles[style]
        metrics["trade_count"] += 1
        if str(receipt.get("status", "")).lower() == "filled":
            metrics["filled_count"] += 1
        metrics["fee"] += _safe_float(receipt.get("fee"))
        metrics["margin_required"] += _safe_float(raw.get("margin_required"))
        metrics["notional"] += _safe_float(raw.get("notional"))
        pnl = _record_pnl(record)
        if pnl is not None:
            metrics["pnl_sample_count"] += 1
            metrics["realized_pnl"] += pnl
            if pnl > 0:
                metrics["wins"] += 1
            elif pnl < 0:
                metrics["losses"] += 1
            previous = equity_curves[style][-1] if equity_curves[style] else 0.0
            equity_curves[style].append(previous + pnl)

    for style, metrics in styles.items():
        trade_count = int(metrics["trade_count"])
        pnl_sample_count = int(metrics["pnl_sample_count"])
        decisive = int(metrics["wins"]) + int(metrics["losses"])
        metrics["win_rate"] = (metrics["wins"] / decisive) if decisive else None
        curve = equity_curves.get(style, [])
        if curve:
            peak = curve[0]
            max_drawdown = 0.0
            for value in curve:
                peak = max(peak, value)
                max_drawdown = min(max_drawdown, value - peak)
            metrics["max_drawdown"] = abs(max_drawdown)
        if trade_count < min_sample_trades or pnl_sample_count < min_sample_trades:
            metrics["status"] = "sample_insufficient"
            metrics["sample_warning"] = (
                f"requires at least {min_sample_trades} realized PnL samples; "
                f"has trades={trade_count}, pnl_samples={pnl_sample_count}"
            )
            metrics["score"] = 0.0
            metrics["pnl_attribution"] = "no_closed_pnl" if pnl_sample_count <= 0 else "sample_insufficient"
            continue
        win_rate = _safe_float(metrics.get("win_rate"), 0.0)
        realized_pnl = _safe_float(metrics.get("realized_pnl"))
        fee = _safe_float(metrics.get("fee"))
        drawdown = _safe_float(metrics.get("max_drawdown"))
        risk_penalty = drawdown + fee
        metrics["score"] = round(realized_pnl + (win_rate * 100.0) - risk_penalty, 4)
        metrics["status"] = "eligible_for_candidate_pool" if metrics["score"] > 0 else "underperforming"
        metrics["sample_warning"] = ""
        metrics["pnl_attribution"] = "realized_pnl_positive" if realized_pnl > 0 else ("realized_pnl_negative" if realized_pnl < 0 else "realized_pnl_flat")
    return {
        "min_sample_trades": min_sample_trades,
        "style_scores": {style: dict(metrics) for style, metrics in styles.items()},
    }


def summarize_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    styles: dict[str, dict[str, Any]] = defaultdict(lambda: {"filled_count": 0, "fee": 0.0, "margin_required": 0.0})
    for record in records:
        style = str(record.get("style") or "unknown")
        receipt = record.get("receipt") if isinstance(record.get("receipt"), dict) else {}
        raw = receipt.get("raw_response") if isinstance(receipt, dict) else {}
        if not isinstance(raw, dict):
            raw = {}
        if str(receipt.get("status", "")).lower() == "filled":
            styles[style]["filled_count"] += 1
        styles[style]["fee"] += float(receipt.get("fee") or 0.0)
        styles[style]["margin_required"] += float(raw.get("margin_required") or 0.0)
    return {
        "filled_count": sum(item["filled_count"] for item in styles.values()),
        "styles": {style: dict(values) for style, values in styles.items()},
    }


def summarize_errors(errors: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize run errors for health reports and dashboard consumers."""

    by_error: dict[str, int] = defaultdict(int)
    by_stage: dict[str, int] = defaultdict(int)
    by_style: dict[str, dict[str, Any]] = defaultdict(lambda: {"error_count": 0, "by_error": defaultdict(int)})
    examples: list[dict[str, Any]] = []
    for error in errors:
        if not isinstance(error, dict):
            continue
        error_name = str(error.get("error") or "unknown")
        stage = str(error.get("stage") or "unknown")
        style = str(error.get("style") or "unknown")
        by_error[error_name] += 1
        by_stage[stage] += 1
        by_style[style]["error_count"] += 1
        by_style[style]["by_error"][error_name] += 1
        if len(examples) < 12:
            examples.append({
                key: error.get(key)
                for key in ("stage", "style", "symbol", "error", "bar_time", "bar_age_minutes", "side")
                if key in error
            })
    return {
        "total": sum(by_error.values()),
        "by_error": dict(by_error),
        "by_stage": dict(by_stage),
        "by_style": {
            style: {
                "error_count": int(values["error_count"]),
                "by_error": dict(values["by_error"]),
            }
            for style, values in by_style.items()
        },
        "examples": examples,
    }


def summarize_holds(holds: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize non-trade hold reasons for dashboard and opening diagnostics."""

    by_reason: dict[str, int] = defaultdict(int)
    by_style: dict[str, dict[str, Any]] = defaultdict(lambda: {"hold_count": 0, "by_reason": defaultdict(int)})
    by_symbol: dict[str, int] = defaultdict(int)
    by_session: dict[str, int] = defaultdict(int)
    examples: list[dict[str, Any]] = []
    for hold in holds:
        if not isinstance(hold, dict):
            continue
        reason = str(hold.get("reason") or "unknown")
        style = str(hold.get("style") or "unknown")
        symbol = str(hold.get("symbol") or "unknown")
        session = str(hold.get("session") or "unknown")
        by_reason[reason] += 1
        by_symbol[symbol] += 1
        by_session[session] += 1
        by_style[style]["hold_count"] += 1
        by_style[style]["by_reason"][reason] += 1
        if len(examples) < 12:
            examples.append({
                key: hold.get(key)
                for key in ("style", "symbol", "reason", "bar_time", "cadence", "session")
                if key in hold
            })
    return {
        "total": sum(by_reason.values()),
        "by_reason": dict(by_reason),
        "by_style": {
            style: {
                "hold_count": int(values["hold_count"]),
                "by_reason": dict(values["by_reason"]),
            }
            for style, values in by_style.items()
        },
        "by_symbol": dict(by_symbol),
        "by_session": dict(by_session),
        "examples": examples,
    }


def _scenario_key(tags: dict[str, Any]) -> str:
    parts = [
        str(tags.get("session") or "unknown"),
        str(tags.get("time_bucket") or "unknown"),
        str(tags.get("product") or "unknown"),
        str(tags.get("direction") or "unknown"),
        str(tags.get("volatility_bucket") or "unknown"),
        str(tags.get("volume_bucket") or "unknown"),
    ]
    return "|".join(parts)


def summarize_forward_outcomes(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize forward direction labels and scenario win rates."""

    by_style: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"total": 0, "labeled": 0, "pending": 0, "wins": 0, "losses": 0, "time_stop_wins": 0, "take_profit_hits": 0, "stop_loss_hits": 0}
    )
    by_scenario: dict[str, dict[str, Any]] = defaultdict(lambda: {"total": 0, "labeled": 0, "wins": 0, "losses": 0})
    examples: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        style = str(record.get("style") or "unknown")
        outcome = record.get("forward_outcome") if isinstance(record.get("forward_outcome"), dict) else {}
        tags = record.get("scenario_tags") if isinstance(record.get("scenario_tags"), dict) else {}
        status = str(outcome.get("status") or "missing")
        style_row = by_style[style]
        style_row["total"] += 1
        if status == "pending_future_bars":
            style_row["pending"] += 1
            continue
        if status != "labeled":
            continue
        scenario = _scenario_key(tags)
        scenario_row = by_scenario[scenario]
        scenario_row["total"] += 1
        scenario_row["labeled"] += 1
        style_row["labeled"] += 1
        if bool(outcome.get("direction_correct")):
            style_row["wins"] += 1
            scenario_row["wins"] += 1
        else:
            style_row["losses"] += 1
            scenario_row["losses"] += 1
        if bool(outcome.get("time_stop_positive")):
            style_row["time_stop_wins"] += 1
        if bool(outcome.get("take_profit_hit")):
            style_row["take_profit_hits"] += 1
        if bool(outcome.get("stop_loss_hit")):
            style_row["stop_loss_hits"] += 1
        if len(examples) < 12:
            examples.append({
                "style": style,
                "symbol": record.get("symbol"),
                "bar_time": record.get("bar_time"),
                "scenario": scenario,
                "horizon_return_pct": outcome.get("horizon_return_pct"),
                "time_stop_return_pct": outcome.get("time_stop_return_pct"),
                "direction_correct": outcome.get("direction_correct"),
            })
    styles: dict[str, dict[str, Any]] = {}
    for style, values in by_style.items():
        labeled = int(values["labeled"])
        wins = int(values["wins"])
        time_stop_wins = int(values["time_stop_wins"])
        row = dict(values)
        row["win_rate"] = (wins / labeled) if labeled else None
        row["time_stop_win_rate"] = (time_stop_wins / labeled) if labeled else None
        styles[style] = row
    scenarios: dict[str, dict[str, Any]] = {}
    for scenario, values in by_scenario.items():
        labeled = int(values["labeled"])
        wins = int(values["wins"])
        row = dict(values)
        row["win_rate"] = (wins / labeled) if labeled else None
        scenarios[scenario] = row
    return {
        "styles": styles,
        "scenarios": scenarios,
        "examples": examples,
    }


def dynamic_threshold_candidates(forward_summary: dict[str, Any], hold_summary: dict[str, Any]) -> list[dict[str, Any]]:
    """Suggest simulated-only threshold changes from labeled outcomes and hold pressure."""

    styles = forward_summary.get("styles") if isinstance(forward_summary.get("styles"), dict) else {}
    hold_by_style = hold_summary.get("by_style") if isinstance(hold_summary.get("by_style"), dict) else {}
    candidates: list[dict[str, Any]] = []
    for style, values in sorted(styles.items()):
        if not isinstance(values, dict):
            continue
        labeled = int(values.get("labeled") or 0)
        pending = int(values.get("pending") or 0)
        win_rate = values.get("win_rate")
        holds = hold_by_style.get(style) if isinstance(hold_by_style.get(style), dict) else {}
        by_reason = holds.get("by_reason") if isinstance(holds.get("by_reason"), dict) else {}
        top_hold_reason = ""
        if by_reason:
            top_hold_reason = max(by_reason.items(), key=lambda item: int(item[1] or 0))[0]
        action = "observe"
        threshold_multiplier = 1.0
        reason = "await_forward_labels" if labeled < 20 else "stable"
        if labeled >= 20 and win_rate is not None and float(win_rate) < 0.50:
            action = "raise_threshold"
            threshold_multiplier = 1.10
            reason = "forward_win_rate_below_floor"
        elif labeled >= 20 and win_rate is not None and float(win_rate) >= 0.62 and top_hold_reason in {"direction_score_below_threshold", "volume_confirmation_filter"}:
            action = "test_lower_threshold_variant"
            threshold_multiplier = 0.95
            reason = "high_win_rate_with_hold_pressure"
        candidates.append({
            "style_name": style,
            "action": action,
            "reason": reason,
            "labeled_count": labeled,
            "pending_count": pending,
            "win_rate": win_rate,
            "time_stop_win_rate": values.get("time_stop_win_rate"),
            "threshold_multiplier": threshold_multiplier,
            "top_hold_reason": top_hold_reason,
            "capital_layer": "simulated",
            "real_trading_enabled": False,
        })
    return candidates


def style_health(records: list[dict[str, Any]], errors: list[dict[str, Any]]) -> dict[str, Any]:
    """Return per-style action hints without mutating strategy configs."""

    health: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "filled_count": 0,
            "error_count": 0,
            "status": "observe",
            "suggested_action": "collect_more_samples",
        }
    )
    for record in records:
        if not isinstance(record, dict):
            continue
        style = str(record.get("style") or "unknown")
        receipt = record.get("receipt") if isinstance(record.get("receipt"), dict) else {}
        if str(receipt.get("status") or "").lower() == "filled":
            health[style]["filled_count"] += 1
    for error in errors:
        if not isinstance(error, dict):
            continue
        style = str(error.get("style") or "unknown")
        health[style]["error_count"] += 1
        health[style].setdefault("errors", defaultdict(int))
        health[style]["errors"][str(error.get("error") or "unknown")] += 1

    for values in health.values():
        filled_count = int(values["filled_count"])
        error_count = int(values["error_count"])
        if error_count and not filled_count:
            values["status"] = "blocked"
            values["suggested_action"] = "inspect_data_or_risk_gate"
        elif error_count >= filled_count and error_count:
            values["status"] = "degraded"
            values["suggested_action"] = "reduce_weight_until_errors_clear"
        elif filled_count:
            values["status"] = "active_sample"
            values["suggested_action"] = "continue_simulated_collection"
        if isinstance(values.get("errors"), defaultdict):
            values["errors"] = dict(values["errors"])
    return {style: dict(values) for style, values in health.items()}


def _review_root_for(path: Path) -> Path:
    if path.parent.name == "data":
        return path.parent.parent
    return path.parent


def _style_metric(
    *,
    date: str,
    style_name: str,
    style_score: dict[str, Any],
    style_state: dict[str, Any],
    forward_state: dict[str, Any] | None = None,
    threshold_candidate: dict[str, Any] | None = None,
    unrealized_pnl: float = 0.0,
) -> dict[str, Any]:
    filled_count = int(style_state.get("filled_count") or style_score.get("filled_count") or 0)
    trade_count = int(style_score.get("trade_count") or filled_count)
    realized_pnl = _safe_float(style_score.get("realized_pnl"))
    drawdown = _safe_float(style_score.get("max_drawdown"))
    fee = _safe_float(style_score.get("fee"))
    sharpe = realized_pnl / max(1.0, drawdown + fee) if trade_count else 0.0
    forward_state = forward_state if isinstance(forward_state, dict) else {}
    threshold_candidate = threshold_candidate if isinstance(threshold_candidate, dict) else {}
    total_pnl = round(realized_pnl + unrealized_pnl, 6)
    return {
        "style_name": style_name,
        "market": STYLE_REVIEW_MARKET,
        "date": date,
        "pnl_source": "sim_ledger_mark_to_market",
        "pnl": total_pnl,
        "realized_pnl": round(realized_pnl, 6),
        "unrealized_pnl": round(unrealized_pnl, 6),
        "total_pnl": total_pnl,
        "win_rate": _safe_float(style_score.get("win_rate")),
        "forward_win_rate": forward_state.get("win_rate"),
        "forward_labeled_count": int(forward_state.get("labeled") or 0),
        "forward_pending_count": int(forward_state.get("pending") or 0),
        "time_stop_win_rate": forward_state.get("time_stop_win_rate"),
        "threshold_candidate": threshold_candidate,
        "max_dd": round(drawdown, 6),
        "sharpe": round(sharpe, 6),
        "trades": trade_count,
        "avg_hold_hours": 0.0,
        "status": style_state.get("status") or style_score.get("status") or "observe",
        "sample_warning": style_score.get("sample_warning", ""),
        "suggested_action": style_state.get("suggested_action", ""),
        "filled_count": filled_count,
        "error_count": int(style_state.get("error_count") or 0),
        "fee": round(fee, 6),
        "margin_required": round(_safe_float(style_score.get("margin_required")), 6),
        "notional": round(_safe_float(style_score.get("notional")), 6),
        "capital_layer": "simulated",
        "account_type": "simulated",
        "real_execution": False,
    }


def write_style_outputs(
    payload: dict[str, Any],
    *,
    review_path: Path | None = None,
    position_pnl_summary: dict[str, dict[str, Any]] | None = None,
) -> dict[str, str]:
    """Write dashboard-compatible style comparison outputs for CNFutures."""

    from shared.markets.performance_tracker import compare_styles, save_run

    target = review_path or DEFAULT_REVIEW_PATH
    review_root = _review_root_for(target)
    output_dir = review_root / STYLE_REVIEW_MARKET
    output_dir.mkdir(parents=True, exist_ok=True)
    score_summary = payload.get("score_summary") if isinstance(payload.get("score_summary"), dict) else {}
    style_scores = score_summary.get("style_scores") if isinstance(score_summary.get("style_scores"), dict) else {}
    health = payload.get("style_health") if isinstance(payload.get("style_health"), dict) else {}
    styles = payload.get("styles") if isinstance(payload.get("styles"), dict) else {}
    forward_summary = payload.get("forward_label_summary") if isinstance(payload.get("forward_label_summary"), dict) else {}
    forward_styles = forward_summary.get("styles") if isinstance(forward_summary.get("styles"), dict) else {}
    threshold_candidates = payload.get("dynamic_threshold_candidates") if isinstance(payload.get("dynamic_threshold_candidates"), list) else []
    threshold_by_style = {
        str(item.get("style_name")): item
        for item in threshold_candidates
        if isinstance(item, dict) and item.get("style_name")
    }
    unrealized_by_style: dict[str, float] = {}
    if isinstance(position_pnl_summary, dict):
        for style_name, values in position_pnl_summary.items():
            if isinstance(values, dict):
                unrealized_by_style[style_name] = _safe_float(values.get("unrealized_pnl"))
    style_names = sorted(set(style_scores) | set(health) | set(styles))
    metrics = [
        _style_metric(
            date=str(payload.get("date") or ""),
            style_name=style_name,
            style_score=style_scores.get(style_name) if isinstance(style_scores.get(style_name), dict) else {},
            style_state=health.get(style_name) if isinstance(health.get(style_name), dict) else {},
            forward_state=forward_styles.get(style_name) if isinstance(forward_styles.get(style_name), dict) else {},
            threshold_candidate=threshold_by_style.get(style_name) if isinstance(threshold_by_style.get(style_name), dict) else {},
            unrealized_pnl=unrealized_by_style.get(style_name, 0.0),
        )
        for style_name in style_names
    ]
    for metric in metrics:
        save_run(str(metric["style_name"]), STYLE_REVIEW_MARKET, metric, review_root=review_root)
    comparison = compare_styles(STYLE_REVIEW_MARKET, review_root=review_root) if metrics else []
    comparison_by_style = {str(row.get("style_name")): dict(row) for row in comparison if isinstance(row, dict)}
    style_comparison = [
        {
            **metric,
            **comparison_by_style.get(str(metric["style_name"]), {}),
            "status": metric["status"],
            "sample_warning": metric["sample_warning"],
            "suggested_action": metric["suggested_action"],
        }
        for metric in metrics
    ]
    output = {
        "market": STYLE_REVIEW_MARKET,
        "date": payload.get("date", ""),
        "capital_layer": "simulated",
        "account_type": "simulated",
        "real_execution": False,
        "state": payload.get("state", ""),
        "record_count": int(payload.get("record_count") or 0),
        "filled_count": int(payload.get("filled_count") or 0),
        "error_count": int(payload.get("error_count") or 0),
        "styles_loaded": len(style_names),
        "styles_total": len(style_names),
        "style_states": [
            {
                "style_name": style_name,
                "status": (health.get(style_name) or {}).get("status", "observe") if isinstance(health.get(style_name), dict) else "observe",
                "suggested_action": (health.get(style_name) or {}).get("suggested_action", "") if isinstance(health.get(style_name), dict) else "",
            }
            for style_name in style_names
        ],
        "style_comparison": style_comparison,
        "score_summary": score_summary,
        "error_summary": payload.get("error_summary") if isinstance(payload.get("error_summary"), dict) else {},
        "hold_count": int(payload.get("hold_count") or 0),
        "hold_reason_summary": payload.get("hold_reason_summary") if isinstance(payload.get("hold_reason_summary"), dict) else {},
        "forward_label_summary": forward_summary,
        "dynamic_threshold_candidates": threshold_candidates,
        "source_review_path": str(target),
        "generated_at": payload.get("generated_at", _now_iso()),
    }
    style_path = output_dir / "style_comparison.json"
    style_path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "style_comparison": str(style_path),
        "style_performance": str(output_dir / "style_performance.jsonl"),
    }


def append_review(
    *,
    date: str,
    market: str,
    records: list[dict[str, Any]],
    errors: list[dict[str, Any]],
    holds: list[dict[str, Any]] | None = None,
    path: Path | None = None,
    position_pnl_summary: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Append one run review and return the persisted payload."""

    summary = summarize_records(records)
    score_summary = score_records(records)
    error_summary = summarize_errors(errors)
    hold_summary = summarize_holds(list(holds or []))
    forward_summary = summarize_forward_outcomes(records)
    threshold_candidates = dynamic_threshold_candidates(forward_summary, hold_summary)
    health = style_health(records, errors)
    payload: dict[str, Any] = {
        "date": date,
        "market": market,
        "cadence": _latest_record_value(records, "cadence"),
        "latest_bar_time": _latest_record_value(records, "bar_time"),
        "capital_layer": "simulated",
        "account_type": "simulated",
        "real_trading_enabled": False,
        "state": "degraded" if errors else "ok",
        "record_count": len(records),
        "hold_count": int(hold_summary.get("total") or 0),
        "error_count": len(errors),
        "errors": errors,
        "hold_reason_summary": hold_summary,
        "forward_label_summary": forward_summary,
        "dynamic_threshold_candidates": threshold_candidates,
        "generated_at": _now_iso(),
        **summary,
        "score_summary": score_summary,
        "error_summary": error_summary,
        "style_health": health,
    }
    target = path or DEFAULT_REVIEW_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    payload["style_output_paths"] = write_style_outputs(payload, review_path=target, position_pnl_summary=position_pnl_summary)
    payload["position_pnl_summary"] = position_pnl_summary
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return payload


__all__ = [
    "DEFAULT_REVIEW_PATH",
    "append_review",
    "dynamic_threshold_candidates",
    "score_records",
    "summarize_forward_outcomes",
    "summarize_errors",
    "summarize_holds",
    "summarize_records",
    "style_health",
    "write_style_outputs",
]
