#!/usr/bin/env python3
"""Append-only review records for CN futures simulation runs."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from shared.review.forward_labels import (
    EVIDENCE_ENVELOPE_GROUPS,
    canonicalize_evidence_record,
    evidence_envelope_from_record,
)

from .contract_rules import normalize_product
from .execution_evidence import validate_execution_evidence


DEFAULT_REVIEW_PATH = (
    Path(__file__).resolve().parents[1]
    / "shared"
    / "review"
    / "data"
    / "cn_futures_sim_reviews.jsonl"
)
REVIEW_MARKET = "cn_futures"
REVIEW_ECONOMICS_SCHEMA = "cn-futures-review-economics.v2"


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


def latest_actionable_review(
    rows: list[dict[str, Any]], *, trade_date: str | None = None
) -> dict[str, Any]:
    """Prefer the newest review with hold/fill/error/bar evidence over empty close rows."""

    target_date = _compact_date(trade_date)
    if target_date:
        dated_rows = [
            row
            for row in rows
            if _compact_date(
                row.get("date") or row.get("trade_date") or row.get("generated_at")
            )
        ]
        if dated_rows:
            rows = [
                row
                for row in dated_rows
                if _compact_date(
                    row.get("date") or row.get("trade_date") or row.get("generated_at")
                )
                == target_date
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


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (OverflowError, TypeError, ValueError):
        return default


_NON_ECONOMIC_EXECUTION_CLASSES = {
    "counterfactual",
    "counterfactual_only",
    "observation",
    "observation_only",
}
_CLOSE_INTENTS = {"close", "reduce_only", "flatten_no_overnight"}


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _is_counterfactual_record(record: dict[str, Any]) -> bool:
    """Reject explicitly non-economic samples from all execution statistics."""

    order = _mapping(record.get("order"))
    size_decision = _mapping(record.get("size_decision"))
    order_size_decision = _mapping(order.get("size_decision"))
    containers = (record, order, size_decision, order_size_decision)
    if any(container.get("counterfactual_only") is True for container in containers):
        return True
    for container in containers:
        execution_class = str(container.get("execution_class") or "").strip().lower()
        if execution_class in _NON_ECONOMIC_EXECUTION_CLASSES:
            return True
        sample_intent = str(container.get("sample_intent") or "").strip().lower()
        if sample_intent in {"counterfactual", "observation", "observe"}:
            return True
    return False


def _is_execution_economic_record(record: dict[str, Any]) -> bool:
    if _is_counterfactual_record(record):
        return False
    for key in ("capital_layer", "account_type"):
        value = str(record.get(key) or "").strip().lower()
        if value and value not in {"sim", "simulation", "simulated"}:
            return False
    receipt = _mapping(record.get("receipt"))
    return str(receipt.get("status") or "").strip().lower() in {"filled", "partial"}


def _record_intent(record: dict[str, Any]) -> str:
    order = _mapping(record.get("order"))
    return str(order.get("intent") or record.get("intent") or "").strip().lower()


def _record_gross_pnl(record: dict[str, Any]) -> float | None:
    for container in (_mapping(record.get("performance")), record):
        if "gross_pnl" in container:
            return _safe_float(container.get("gross_pnl"))
    return None


def _charged_fee(record: dict[str, Any]) -> float:
    """Return the charged leg fee, never a duplicated round-trip estimate."""

    receipt = _mapping(record.get("receipt"))
    raw = _mapping(receipt.get("raw_response"))
    performance = _mapping(record.get("performance"))
    for container in (record, performance, receipt, raw):
        for key in ("account_fee_cny", "charged_fee", "actual_fee", "leg_fee"):
            if key in container:
                return max(0.0, _safe_float(container.get(key)))

    intent = _record_intent(record)
    is_close = (
        intent in _CLOSE_INTENTS
        or "gross_pnl" in performance
        or _safe_float(performance.get("closed_quantity"), 0.0) > 0
    )
    if is_close and "estimated_close_fee" in raw:
        return max(0.0, _safe_float(raw.get("estimated_close_fee")))
    if not is_close and "open_fee" in raw:
        return max(0.0, _safe_float(raw.get("open_fee")))
    return max(0.0, _safe_float(receipt.get("fee")))


def _position_identity(record: dict[str, Any]) -> tuple[str, str]:
    order = _mapping(record.get("order"))
    return (
        str(record.get("style") or "unknown"),
        str(record.get("symbol") or order.get("symbol") or order.get("ts_code") or ""),
    )


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


def score_records(
    records: list[dict[str, Any]], *, min_sample_trades: int = 20
) -> dict[str, Any]:
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
            "gross_realized_pnl": 0.0,
            "pnl_sample_count": 0,
            "completed_round_trip_count": 0,
            "observation_count": 0,
            "counterfactual_count": 0,
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
    pending_entry_fees: dict[tuple[str, str], float] = defaultdict(float)
    for record in records:
        if not isinstance(record, dict):
            continue
        style = str(record.get("style") or "unknown")
        receipt = (
            record.get("receipt") if isinstance(record.get("receipt"), dict) else {}
        )
        raw = (
            receipt.get("raw_response")
            if isinstance(receipt.get("raw_response"), dict)
            else {}
        )
        metrics = styles[style]
        metrics["observation_count"] += 1
        if _is_counterfactual_record(record):
            metrics["counterfactual_count"] += 1
        if not _is_execution_economic_record(record):
            continue
        metrics["trade_count"] += 1
        if str(receipt.get("status", "")).lower() == "filled":
            metrics["filled_count"] += 1
        charged_fee = _charged_fee(record)
        metrics["fee"] += charged_fee
        metrics["margin_required"] += _safe_float(raw.get("margin_required"))
        metrics["notional"] += _safe_float(raw.get("notional"))
        position_identity = _position_identity(record)
        gross_pnl = _record_gross_pnl(record)
        pnl = _record_pnl(record)
        performance = _mapping(record.get("performance"))
        intent = _record_intent(record)
        is_close = (
            intent in _CLOSE_INTENTS
            or gross_pnl is not None
            or _safe_float(performance.get("closed_quantity"), 0.0) > 0
        )
        if gross_pnl is not None:
            entry_fee = pending_entry_fees.pop(position_identity, 0.0)
            pnl = gross_pnl - charged_fee - entry_fee
            metrics["gross_realized_pnl"] += gross_pnl
        elif pnl is not None and is_close:
            entry_fee = pending_entry_fees.pop(position_identity, 0.0)
            included_fee = _safe_float(performance.get("round_trip_fee"), 0.0)
            if entry_fee and included_fee + 1e-9 < entry_fee + charged_fee:
                pnl -= entry_fee
        elif pnl is None:
            pending_entry_fees[position_identity] += charged_fee
        if pnl is not None:
            metrics["pnl_sample_count"] += 1
            metrics["completed_round_trip_count"] += 1
            metrics["realized_pnl"] += pnl
            if pnl > 0:
                metrics["wins"] += 1
            elif pnl < 0:
                metrics["losses"] += 1
            previous = equity_curves[style][-1] if equity_curves[style] else 0.0
            equity_curves[style].append(previous + pnl)

    for style, metrics in styles.items():
        unmatched_costs = sum(
            amount
            for (pending_style, _), amount in pending_entry_fees.items()
            if pending_style == style
        )
        metrics["realized_pnl"] -= unmatched_costs
        for key in ("fee", "gross_realized_pnl", "realized_pnl"):
            metrics[key] = round(_safe_float(metrics.get(key)), 6)
        trade_count = int(metrics["trade_count"])
        pnl_sample_count = int(metrics["pnl_sample_count"])
        decisive = int(metrics["wins"]) + int(metrics["losses"])
        metrics["win_rate"] = (metrics["wins"] / decisive) if decisive else None
        curve = list(equity_curves.get(style, []))
        ending_equity = _safe_float(metrics.get("realized_pnl"))
        if not curve or abs(curve[-1] - ending_equity) > 1e-9:
            curve.append(ending_equity)
        peak = 0.0
        minimum = 0.0
        max_drawdown = 0.0
        for value in curve:
            peak = max(peak, value)
            minimum = min(minimum, value)
            max_drawdown = max(max_drawdown, peak - value)
        metrics["ending_equity"] = round(ending_equity, 6)
        metrics["high_water_equity"] = round(peak, 6)
        metrics["minimum_equity"] = round(minimum, 6)
        metrics["current_drawdown"] = round(max(0.0, peak - ending_equity), 6)
        metrics["max_drawdown"] = (
            round(max_drawdown, 6)
            if trade_count > 0 or pnl_sample_count > 0 or unmatched_costs > 0
            else None
        )
        if trade_count < min_sample_trades or pnl_sample_count < min_sample_trades:
            metrics["status"] = "sample_insufficient"
            metrics["sample_warning"] = (
                f"requires at least {min_sample_trades} realized PnL samples; "
                f"has trades={trade_count}, pnl_samples={pnl_sample_count}"
            )
            metrics["score"] = 0.0
            metrics["pnl_attribution"] = (
                "no_closed_pnl" if pnl_sample_count <= 0 else "sample_insufficient"
            )
            continue
        win_rate = _safe_float(metrics.get("win_rate"), 0.0)
        realized_pnl = _safe_float(metrics.get("realized_pnl"))
        drawdown = _safe_float(metrics.get("max_drawdown"))
        # realized_pnl is already after charged leg fees; only drawdown remains
        # as a separate risk penalty.
        risk_penalty = drawdown
        metrics["score"] = round(realized_pnl + (win_rate * 100.0) - risk_penalty, 4)
        metrics["status"] = (
            "eligible_for_candidate_pool" if metrics["score"] > 0 else "underperforming"
        )
        metrics["sample_warning"] = ""
        metrics["pnl_attribution"] = (
            "realized_pnl_positive"
            if realized_pnl > 0
            else ("realized_pnl_negative" if realized_pnl < 0 else "realized_pnl_flat")
        )
    return {
        "min_sample_trades": min_sample_trades,
        "style_scores": {style: dict(metrics) for style, metrics in styles.items()},
    }


def _latest_cumulative_score_summary(
    path: Path,
    *,
    date: str,
    market: str,
) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(row, dict):
            continue
        if row.get("score_contract_version") != REVIEW_ECONOMICS_SCHEMA:
            continue
        if _compact_date(row.get("date")) != _compact_date(date):
            continue
        if str(row.get("market") or "") != str(market or ""):
            continue
        summary = row.get("score_summary")
        return dict(summary) if isinstance(summary, dict) else {}
    return {}


def _finalize_cumulative_style_score(
    metrics: dict[str, Any],
    *,
    min_sample_trades: int,
) -> dict[str, Any]:
    for key in (
        "fee",
        "margin_required",
        "notional",
        "realized_pnl",
        "gross_realized_pnl",
    ):
        metrics[key] = round(_safe_float(metrics.get(key)), 6)
    trade_count = int(metrics.get("trade_count") or 0)
    pnl_sample_count = int(metrics.get("pnl_sample_count") or 0)
    wins = int(metrics.get("wins") or 0)
    losses = int(metrics.get("losses") or 0)
    decisive = wins + losses
    metrics["win_rate"] = wins / decisive if decisive else None
    if trade_count < min_sample_trades or pnl_sample_count < min_sample_trades:
        metrics["status"] = "sample_insufficient"
        metrics["sample_warning"] = (
            f"requires at least {min_sample_trades} realized PnL samples; "
            f"has trades={trade_count}, pnl_samples={pnl_sample_count}"
        )
        metrics["score"] = 0.0
        metrics["pnl_attribution"] = (
            "no_closed_pnl" if pnl_sample_count <= 0 else "sample_insufficient"
        )
        return metrics
    realized_pnl = _safe_float(metrics.get("realized_pnl"))
    drawdown = _safe_float(metrics.get("max_drawdown"))
    metrics["score"] = round(
        realized_pnl + (_safe_float(metrics.get("win_rate")) * 100.0) - drawdown,
        4,
    )
    metrics["status"] = (
        "eligible_for_candidate_pool" if metrics["score"] > 0 else "underperforming"
    )
    metrics["sample_warning"] = ""
    metrics["pnl_attribution"] = (
        "realized_pnl_positive"
        if realized_pnl > 0
        else "realized_pnl_negative"
        if realized_pnl < 0
        else "realized_pnl_flat"
    )
    return metrics


def _merge_score_summaries(
    previous: dict[str, Any],
    current: dict[str, Any],
) -> dict[str, Any]:
    min_sample_trades = int(current.get("min_sample_trades") or 20)
    previous_styles = _mapping(previous.get("style_scores"))
    current_styles = _mapping(current.get("style_scores"))
    additive_fields = (
        "trade_count",
        "filled_count",
        "fee",
        "margin_required",
        "notional",
        "realized_pnl",
        "gross_realized_pnl",
        "pnl_sample_count",
        "completed_round_trip_count",
        "observation_count",
        "counterfactual_count",
        "wins",
        "losses",
    )
    merged_styles: dict[str, dict[str, Any]] = {}
    for style in sorted(set(previous_styles) | set(current_styles)):
        previous_metrics = _mapping(previous_styles.get(style))
        current_metrics = _mapping(current_styles.get(style))
        merged = {**previous_metrics, **current_metrics}
        for field in additive_fields:
            merged[field] = _safe_float(previous_metrics.get(field)) + _safe_float(
                current_metrics.get(field)
            )
            if field.endswith("count") or field in {
                "trade_count",
                "filled_count",
                "pnl_sample_count",
                "wins",
                "losses",
            }:
                merged[field] = int(merged[field])

        previous_ending = _safe_float(
            previous_metrics.get("ending_equity"),
            _safe_float(previous_metrics.get("realized_pnl")),
        )
        current_ending = _safe_float(
            current_metrics.get("ending_equity"),
            _safe_float(current_metrics.get("realized_pnl")),
        )
        previous_high = _safe_float(
            previous_metrics.get("high_water_equity"),
            max(0.0, previous_ending),
        )
        current_high = _safe_float(
            current_metrics.get("high_water_equity"),
            max(0.0, current_ending),
        )
        previous_minimum = _safe_float(
            previous_metrics.get("minimum_equity"),
            min(0.0, previous_ending),
        )
        current_minimum = _safe_float(
            current_metrics.get("minimum_equity"),
            min(0.0, current_ending),
        )
        ending_equity = previous_ending + current_ending
        high_water_equity = max(
            previous_high,
            previous_ending + current_high,
        )
        minimum_equity = min(
            previous_minimum,
            previous_ending + current_minimum,
        )
        cross_batch_drawdown = max(
            0.0,
            previous_high - (previous_ending + current_minimum),
        )
        has_drawdown_evidence = any(
            metrics.get("max_drawdown") is not None
            for metrics in (previous_metrics, current_metrics)
        )
        max_drawdown = max(
            _safe_float(previous_metrics.get("max_drawdown")),
            _safe_float(current_metrics.get("max_drawdown")),
            cross_batch_drawdown,
        )
        merged["ending_equity"] = round(ending_equity, 6)
        merged["high_water_equity"] = round(high_water_equity, 6)
        merged["minimum_equity"] = round(minimum_equity, 6)
        merged["current_drawdown"] = round(
            max(0.0, high_water_equity - ending_equity),
            6,
        )
        merged["max_drawdown"] = (
            round(max_drawdown, 6) if has_drawdown_evidence else None
        )
        merged_styles[style] = _finalize_cumulative_style_score(
            merged,
            min_sample_trades=min_sample_trades,
        )
    return {
        "min_sample_trades": min_sample_trades,
        "style_scores": merged_styles,
    }


def summarize_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    styles: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "filled_count": 0,
            "execution_count": 0,
            "observation_count": 0,
            "counterfactual_count": 0,
            "fee": 0.0,
            "margin_required": 0.0,
        }
    )
    for record in records:
        style = str(record.get("style") or "unknown")
        metrics = styles[style]
        metrics["observation_count"] += 1
        if _is_counterfactual_record(record):
            metrics["counterfactual_count"] += 1
        if not _is_execution_economic_record(record):
            continue
        receipt = (
            record.get("receipt") if isinstance(record.get("receipt"), dict) else {}
        )
        raw = receipt.get("raw_response") if isinstance(receipt, dict) else {}
        if not isinstance(raw, dict):
            raw = {}
        if str(receipt.get("status", "")).lower() == "filled":
            metrics["filled_count"] += 1
        metrics["execution_count"] += 1
        metrics["fee"] = round(metrics["fee"] + _charged_fee(record), 6)
        metrics["margin_required"] += float(raw.get("margin_required") or 0.0)
    return {
        "filled_count": sum(item["filled_count"] for item in styles.values()),
        "styles": {style: dict(values) for style, values in styles.items()},
    }


def summarize_errors(errors: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize run errors for health reports and dashboard consumers."""

    by_error: dict[str, int] = defaultdict(int)
    by_stage: dict[str, int] = defaultdict(int)
    by_style: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"error_count": 0, "by_error": defaultdict(int)}
    )
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
            examples.append(
                {
                    key: error.get(key)
                    for key in (
                        "stage",
                        "style",
                        "symbol",
                        "error",
                        "bar_time",
                        "bar_age_minutes",
                        "side",
                    )
                    if key in error
                }
            )
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


def _product_from_hold(hold: dict[str, Any]) -> str:
    product = str(hold.get("product") or "").strip().lower()
    if product:
        return product
    symbol = str(hold.get("symbol") or "").strip()
    if not symbol or symbol in {"unknown", ""}:
        return "unknown"
    try:
        return normalize_product(symbol)
    except ValueError:
        return "unknown"


def _label_status(value: Any, *, eligible: bool) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"labeled", "complete", "completed"}:
        return "labeled"
    if raw in {"rejected", "ineligible", "data_unreliable"} or not eligible:
        return "rejected"
    return "pending"


def build_observation_samples(
    holds: list[dict[str, Any]],
    *,
    date: str,
    market: str,
) -> list[dict[str, Any]]:
    """Normalize every hold/rejection into a durable, non-economic sample."""

    observations: list[dict[str, Any]] = []
    for hold in holds:
        if not isinstance(hold, dict):
            continue
        prediction = dict(
            _mapping(hold.get("prediction")) or _mapping(hold.get("signal"))
        )
        size_decision = _mapping(hold.get("size_decision"))
        forward_outcome = dict(_mapping(hold.get("forward_outcome")))
        stage = str(hold.get("stage") or "signal")
        if not prediction and stage == "signal":
            for key in ("action", "side", "direction", "confidence"):
                if hold.get(key) not in (None, ""):
                    prediction[key] = hold.get(key)
        action = str(
            hold.get("action")
            or prediction.get("action")
            or ("hold" if stage == "signal" else "")
        )
        direction = str(
            hold.get("direction")
            or hold.get("side")
            or prediction.get("direction")
            or prediction.get("side")
            or action
            or "unknown"
        )
        execution_class = str(
            hold.get("execution_class")
            or (
                "counterfactual_only"
                if bool(hold.get("counterfactual_only"))
                or bool(size_decision.get("counterfactual_only"))
                else "observation_only"
            )
        )
        counterfactual_only = bool(
            hold.get("counterfactual_only")
            or size_decision.get("counterfactual_only")
            or execution_class.lower() in {"counterfactual", "counterfactual_only"}
        )
        prediction_evidence_complete = bool(prediction) and direction not in {
            "",
            "unknown",
        }
        label_eligible = bool(
            hold.get("label_eligible") if "label_eligible" in hold else stage != "data"
        )
        if not prediction_evidence_complete:
            label_eligible = False
        source_label_status = hold.get("label_status") or forward_outcome.get("status")
        if not prediction_evidence_complete:
            label_status = "prediction_evidence_incomplete"
            forward_outcome = {"status": "prediction_evidence_incomplete"}
        else:
            label_status = _label_status(source_label_status, eligible=label_eligible)
        if not forward_outcome and label_status == "pending":
            forward_outcome = {"status": "pending_future_bars"}
        elif str(forward_outcome.get("status") or "").lower() == "pending":
            forward_outcome["status"] = "pending_future_bars"
        product = _product_from_hold(hold)
        identity_parts = (
            str(date),
            str(market),
            str(hold.get("style") or "unknown"),
            str(hold.get("symbol") or "unknown"),
            str(hold.get("bar_time") or ""),
            stage,
            str(hold.get("reason") or "unknown"),
        )
        observation_id = str(hold.get("observation_id") or "").strip()
        if not observation_id:
            digest = hashlib.sha256(
                "|".join(identity_parts).encode("utf-8")
            ).hexdigest()[:24]
            observation_id = f"CNFOBS-{digest}"
        observations.append(
            {
                "observation_id": observation_id,
                "date": date,
                "market": market,
                "cadence": hold.get("cadence", ""),
                "bar_time": hold.get("bar_time", ""),
                "session": hold.get("session", ""),
                "style": str(hold.get("style") or "unknown"),
                "style_version": str(
                    hold.get("style_version") or hold.get("strategy_version") or ""
                ),
                "symbol": str(hold.get("symbol") or "unknown"),
                "product": product,
                "stage": stage,
                "reason": str(hold.get("reason") or "unknown"),
                "action": action,
                "side": str(hold.get("side") or prediction.get("side") or ""),
                "direction": direction,
                "sample_intent": str(
                    hold.get("sample_intent")
                    or ("counterfactual" if counterfactual_only else "observe")
                ),
                "execution_class": execution_class,
                "execution_eligible": False,
                "counterfactual_only": counterfactual_only,
                "label_eligible": label_eligible,
                "label_status": label_status,
                "prediction_evidence_status": (
                    "complete" if prediction_evidence_complete else "incomplete"
                ),
                "prediction": prediction,
                "size_decision": size_decision,
                "scenario_tags": _mapping(hold.get("scenario_tags")),
                "forward_outcome": forward_outcome,
                "decision_snapshot": dict(hold),
                "capital_layer": "simulated",
                "account_type": "simulated",
                "real_trading_enabled": False,
                # PIT lineage
                "point_in_time_as_of": str(
                    hold.get("point_in_time_as_of") or hold.get("bar_time") or ""
                ),
                "source_event_time": str(
                    hold.get("source_event_time") or hold.get("bar_time") or ""
                ),
                "source_snapshot_id": str(hold.get("source_snapshot_id") or ""),
                "source_snapshot_sha256": str(hold.get("source_snapshot_sha256") or ""),
                "authority": str(hold.get("authority") or ""),
                "lineage_status": str(hold.get("lineage_status") or "incomplete"),
                "capital_authority_id": str(hold.get("capital_authority_id") or ""),
                "authority_generation": hold.get("authority_generation"),
                "execution_lineage_id": str(hold.get("execution_lineage_id") or ""),
                # Cluster dedup
                "cluster_id": str(hold.get("cluster_id") or ""),
                "cluster_role": str(hold.get("cluster_role") or ""),
                "occurrence_index": _safe_int(hold.get("occurrence_index"), -1)
                if hold.get("occurrence_index") is not None
                else -1,
                "weight_multiplier": _safe_float(hold.get("weight_multiplier"), 0.0),
            }
        )
    for observation in observations:
        observation["journal_payload_sha256"] = _compact_payload_sha256(observation)
    return observations


def summarize_holds(holds: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize non-trade hold reasons for dashboard and opening diagnostics."""

    by_reason: dict[str, int] = defaultdict(int)
    by_style: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"hold_count": 0, "by_reason": defaultdict(int)}
    )
    by_symbol: dict[str, int] = defaultdict(int)
    by_session: dict[str, int] = defaultdict(int)
    by_stage: dict[str, int] = defaultdict(int)
    by_product: dict[str, int] = defaultdict(int)
    by_product_by_reason: dict[str, dict[str, int]] = defaultdict(
        lambda: defaultdict(int)
    )
    examples: list[dict[str, Any]] = []
    for hold in holds:
        if not isinstance(hold, dict):
            continue
        reason = str(hold.get("reason") or "unknown")
        style = str(hold.get("style") or "unknown")
        symbol = str(hold.get("symbol") or "unknown")
        session = str(hold.get("session") or "unknown")
        stage = str(hold.get("stage") or "signal")
        product = _product_from_hold(hold)
        by_reason[reason] += 1
        by_symbol[symbol] += 1
        by_session[session] += 1
        by_stage[stage] += 1
        by_product[product] += 1
        by_product_by_reason[product][reason] += 1
        by_style[style]["hold_count"] += 1
        by_style[style]["by_reason"][reason] += 1
        if len(examples) < 12:
            examples.append(
                {
                    key: hold.get(key)
                    for key in (
                        "style",
                        "symbol",
                        "product",
                        "reason",
                        "bar_time",
                        "cadence",
                        "session",
                    )
                    if key in hold
                }
            )
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
        "by_stage": dict(by_stage),
        "by_product": dict(by_product),
        "by_product_by_reason": {
            product: dict(reasons) for product, reasons in by_product_by_reason.items()
        },
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
        lambda: {
            "total": 0,
            "labeled": 0,
            "pending": 0,
            "wins": 0,
            "losses": 0,
            "time_stop_wins": 0,
            "take_profit_hits": 0,
            "stop_loss_hits": 0,
        }
    )
    by_scenario: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"total": 0, "labeled": 0, "wins": 0, "losses": 0}
    )
    examples: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        style = str(record.get("style") or "unknown")
        outcome = (
            record.get("forward_outcome")
            if isinstance(record.get("forward_outcome"), dict)
            else {}
        )
        tags = (
            record.get("scenario_tags")
            if isinstance(record.get("scenario_tags"), dict)
            else {}
        )
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
            examples.append(
                {
                    "style": style,
                    "symbol": record.get("symbol"),
                    "bar_time": record.get("bar_time"),
                    "scenario": scenario,
                    "horizon_return_pct": outcome.get("horizon_return_pct"),
                    "time_stop_return_pct": outcome.get("time_stop_return_pct"),
                    "direction_correct": outcome.get("direction_correct"),
                }
            )
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


def dynamic_threshold_candidates(
    forward_summary: dict[str, Any], hold_summary: dict[str, Any]
) -> list[dict[str, Any]]:
    """Suggest simulated-only threshold changes from labeled outcomes and hold pressure."""

    styles = (
        forward_summary.get("styles")
        if isinstance(forward_summary.get("styles"), dict)
        else {}
    )
    hold_by_style = (
        hold_summary.get("by_style")
        if isinstance(hold_summary.get("by_style"), dict)
        else {}
    )
    candidates: list[dict[str, Any]] = []
    for style, values in sorted(styles.items()):
        if not isinstance(values, dict):
            continue
        labeled = int(values.get("labeled") or 0)
        pending = int(values.get("pending") or 0)
        win_rate = values.get("win_rate")
        holds = (
            hold_by_style.get(style)
            if isinstance(hold_by_style.get(style), dict)
            else {}
        )
        by_reason = (
            holds.get("by_reason") if isinstance(holds.get("by_reason"), dict) else {}
        )
        top_hold_reason = ""
        if by_reason:
            top_hold_reason = max(
                by_reason.items(), key=lambda item: int(item[1] or 0)
            )[0]
        action = "observe"
        threshold_multiplier = 1.0
        reason = "await_forward_labels" if labeled < 20 else "stable"
        if labeled >= 20 and win_rate is not None and float(win_rate) < 0.50:
            action = "raise_threshold"
            threshold_multiplier = 1.10
            reason = "forward_win_rate_below_floor"
        elif (
            labeled >= 20
            and win_rate is not None
            and float(win_rate) >= 0.62
            and top_hold_reason
            in {"direction_score_below_threshold", "volume_confirmation_filter"}
        ):
            action = "test_lower_threshold_variant"
            threshold_multiplier = 0.95
            reason = "high_win_rate_with_hold_pressure"
        candidates.append(
            {
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
            }
        )
    return candidates


def style_health(
    records: list[dict[str, Any]],
    errors: list[dict[str, Any]],
    observations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return per-style action hints without mutating strategy configs."""

    health: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "filled_count": 0,
            "error_count": 0,
            "observation_count": 0,
            "hold_count": 0,
            "risk_rejection_count": 0,
            "status": "observe",
            "suggested_action": "collect_more_samples",
        }
    )
    for record in records:
        if not isinstance(record, dict):
            continue
        style = str(record.get("style") or "unknown")
        receipt = (
            record.get("receipt") if isinstance(record.get("receipt"), dict) else {}
        )
        if (
            _is_execution_economic_record(record)
            and str(receipt.get("status") or "").lower() == "filled"
        ):
            health[style]["filled_count"] += 1
    for observation in observations or []:
        if not isinstance(observation, dict):
            continue
        style = str(observation.get("style") or "unknown")
        health[style]["observation_count"] += 1
        health[style]["hold_count"] += 1
        if str(observation.get("stage") or "") in {"risk", "capital", "execution"}:
            health[style]["risk_rejection_count"] += 1
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


# ---------------------------------------------------------------------------
# Per-session decision row helpers
# ---------------------------------------------------------------------------

_SESSION_ROW_TYPE = "cn_futures_session_decision"
_VALID_SESSIONS = {"day_morning", "day_afternoon", "night"}
_LEGACY_SESSION_MAP = {"day": None}  # mapped dynamically via bar_time


def _compute_checksum(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _compact_payload_sha256(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            dict(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _normalize_session(session: str, bar_time: str = "") -> str | None:
    """Normalize a session token, mapping legacy 'day' when bar_time allows.

    Returns None when session is empty/missing (caller should skip the row).
    Raises ValueError for explicitly provided but unknown/unmappable sessions.
    """
    s = (session or "").strip().lower()
    if not s:
        return None
    if s in _VALID_SESSIONS:
        return s
    if s == "day":
        if not bar_time or not str(bar_time).strip():
            raise ValueError(
                "Cannot map legacy 'day' session without bar_time; "
                "refusing to fabricate a sub-session"
            )
        hour = _extract_hour(bar_time)
        if hour is None:
            raise ValueError(
                f"Cannot determine session from bar_time={bar_time!r}; "
                "refusing to fabricate a sub-session for legacy 'day'"
            )
        if hour < 12:
            return "day_morning"
        else:
            return "day_afternoon"
    raise ValueError(
        f"Unknown session {session!r}; accepted: {sorted(_VALID_SESSIONS)}"
    )


def _extract_hour(bar_time: str) -> int | None:
    """Extract the hour from a datetime string like '2026-07-12 09:35:00'."""
    bt = str(bar_time).strip()
    # Try ISO / space-separated formats
    for pattern in (r"[\sT](\d{1,2}):\d{2}",):
        m = re.search(pattern, bt)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                return None
    return None


def _session_from_record(record: dict[str, Any]) -> str:
    """Extract session from a record, checking nested containers."""
    for key in ("session", "session_name", "trading_session"):
        val = str(record.get(key) or "").strip().lower()
        if val:
            return val
    for container_key in ("order", "receipt", "size_decision"):
        container = record.get(container_key)
        if isinstance(container, dict):
            for key in ("session", "session_name", "trading_session"):
                val = str(container.get(key) or "").strip().lower()
                if val:
                    return val
    return ""


def _bar_time_from_record(record: dict[str, Any]) -> str:
    """Extract bar_time from record or nested containers."""
    for key in ("bar_time", "trade_time", "timestamp"):
        val = str(record.get(key) or "").strip()
        if val:
            return val
    for container_key in ("order", "receipt", "size_decision"):
        container = record.get(container_key)
        if isinstance(container, dict):
            for key in ("bar_time", "trade_time", "timestamp"):
                val = str(container.get(key) or "").strip()
                if val:
                    return val
    return ""


def _classify_record_type(record: dict[str, Any]) -> str:
    """Determine record_type for a fill record, matching acceptance module semantics."""
    # Honor explicit record_type if already set
    explicit = str(record.get("record_type") or "").strip().lower()
    if explicit in {"prediction", "candidate", "hold", "risk_reject", "simulated_fill"}:
        return explicit
    receipt = record.get("receipt")
    if isinstance(receipt, dict):
        status = str(receipt.get("status") or "").strip().lower()
        try:
            filled_qty = float(receipt.get("filled_qty") or 0)
        except (TypeError, ValueError):
            filled_qty = 0.0
        if status in {"filled", "partial"} and filled_qty > 0:
            return "simulated_fill"
    # Check for prediction/candidate
    for key in ("prediction", "signal"):
        if isinstance(record.get(key), dict) and record[key]:
            return "prediction"
    return "candidate"


def _classify_hold_type(hold: dict[str, Any]) -> str:
    """Determine record_type for a hold record."""
    stage = str(hold.get("stage") or "").strip().lower()
    if stage in {"risk", "capital", "execution"}:
        return "risk_reject"
    return "hold"


def _row_identity(row: dict[str, Any]) -> str:
    """Stable identity key for idempotent appends."""
    evidence = row.get("execution_evidence")
    execution_fill_id = (
        str(evidence.get("execution_fill_id") or "")
        if isinstance(evidence, dict)
        else ""
    )
    parts = [
        str(row.get("trade_date") or ""),
        str(row.get("session") or ""),
        str(row.get("record_type") or ""),
        str(row.get("symbol") or ""),
        str(row.get("style") or ""),
        str(row.get("bar_time") or ""),
        str(row.get("reason") or ""),
        str(row.get("cluster_id") or ""),
        str(
            row.get("occurrence_index")
            if row.get("occurrence_index") is not None
            else ""
        ),
        execution_fill_id,
    ]
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]
    return f"CNFUT-SESS-{digest}"


def _value_from_nested(source: dict[str, Any], key: str, default: Any = None) -> Any:
    """Look up a key in the source, its direct nested containers, and one
    level deeper (e.g. order.size_decision)."""
    if key in source:
        return source[key]
    for container_key in ("order", "receipt", "size_decision"):
        container = source.get(container_key)
        if isinstance(container, dict):
            if key in container:
                return container[key]
            # One level deeper for order.size_decision etc.
            for sub_key in ("size_decision", "raw_response"):
                sub = container.get(sub_key)
                if isinstance(sub, dict) and key in sub:
                    return sub[key]
    return default


def _session_evidence_record(source: Mapping[str, Any]) -> dict[str, Any]:
    """Merge raw evidence from every prediction container without collapsing aliases."""

    containers: list[tuple[str, Mapping[str, Any]]] = [("source", source)]
    for name in ("prediction_snapshot", "prediction", "decision_snapshot"):
        value = source.get(name)
        if isinstance(value, Mapping):
            containers.append((name, value))
    merged: dict[str, dict[str, Any]] = {
        group: {} for group in EVIDENCE_ENVELOPE_GROUPS
    }
    structure_errors: list[str] = []
    for name, container in containers:
        envelope = evidence_envelope_from_record(container)
        for group in EVIDENCE_ENVELOPE_GROUPS:
            values = envelope.get(group)
            if not isinstance(values, Mapping):
                structure_errors.append(f"{name}.{group}")
                continue
            for path, value in values.items():
                merged[group][f"{name}.{path}"] = value
        errors = envelope.get("structure_errors")
        if isinstance(errors, list):
            structure_errors.extend(f"{name}.{error}" for error in errors)
    merged["structure_errors"] = structure_errors  # type: ignore[assignment]

    raw_boundary = _value_from_nested(dict(source), "point_in_time_as_of", "")
    try:
        boundary = datetime.fromisoformat(str(raw_boundary).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        boundary = None
    if boundary is not None and (
        boundary.tzinfo is None or boundary.utcoffset() is None
    ):
        boundary = None
    return canonicalize_evidence_record(
        {"evidence_envelope": merged},
        boundary=boundary,
    )


def _build_session_row(
    *,
    trade_date: str,
    session: str,
    record_type: str,
    source: dict[str, Any],
    bar_time: str = "",
) -> dict[str, Any]:
    """Build a single per-session decision row."""
    execution_eligible = bool(_value_from_nested(source, "execution_eligible", False))
    counterfactual_only = bool(_value_from_nested(source, "counterfactual_only", False))
    execution_class = str(
        _value_from_nested(source, "execution_class", "") or ""
    ).strip()
    raw_execution_evidence = _value_from_nested(source, "execution_evidence", {})
    execution_evidence = (
        dict(raw_execution_evidence) if isinstance(raw_execution_evidence, dict) else {}
    )
    raw_round_trip_evidence = _value_from_nested(source, "round_trip_evidence", {})
    round_trip_evidence = (
        dict(raw_round_trip_evidence)
        if isinstance(raw_round_trip_evidence, dict)
        else {}
    )

    evidence = _session_evidence_record(source)
    row: dict[str, Any] = {
        "_row_type": _SESSION_ROW_TYPE,
        "trade_date": trade_date,
        "session": session,
        "record_type": record_type,
        "style": str(source.get("style") or "unknown"),
        "symbol": str(source.get("symbol") or source.get("ts_code") or "unknown"),
        "execution_eligible": execution_eligible,
        "execution_class": execution_class,
        "counterfactual_only": counterfactual_only,
        "real_trading_enabled": False,
        "capital_layer": "simulated",
        "account_type": "simulated",
        "bar_time": bar_time or str(source.get("bar_time") or ""),
        "entry_price": _safe_float(_value_from_nested(source, "entry_price", 0.0), 0.0),
        "decision": {
            k: source.get(k)
            for k in (
                "direction",
                "side",
                "action",
                "raw_heuristic_score",
                "uncalibrated_confidence_prior",
                "calibrated_probability",
                "probability_model_state",
                "entry_price",
                "reason",
                "reasons",
                "stage",
                "intent",
                "prediction",
                "size_decision",
                "scenario_tags",
            )
            if k in source
        },
        # PIT lineage
        "point_in_time_as_of": str(
            _value_from_nested(source, "point_in_time_as_of", "")
        ),
        "source_event_time": str(_value_from_nested(source, "source_event_time", "")),
        "evidence_envelope": deepcopy(evidence.get("evidence_envelope") or {}),
        "evidence_envelope_validation": deepcopy(
            evidence.get("evidence_envelope_validation") or {}
        ),
        "point_in_time_lineage": deepcopy(evidence.get("point_in_time_lineage") or {}),
        "source_snapshot_id": str(_value_from_nested(source, "source_snapshot_id", "")),
        "source_snapshot_sha256": str(
            _value_from_nested(source, "source_snapshot_sha256", "")
        ),
        "authority": str(_value_from_nested(source, "authority", "")),
        "lineage_status": str(_value_from_nested(source, "lineage_status", "")),
        "capital_authority_id": str(
            _value_from_nested(source, "capital_authority_id", "")
        ),
        "authority_generation": _value_from_nested(source, "authority_generation"),
        "execution_lineage_id": str(
            _value_from_nested(source, "execution_lineage_id", "")
        ),
        "execution_evidence": execution_evidence,
        "round_trip_evidence": round_trip_evidence,
        # Cluster dedup
        "cluster_id": str(_value_from_nested(source, "cluster_id", "")),
        "cluster_role": str(_value_from_nested(source, "cluster_role", "")),
        "occurrence_index": _safe_int(
            _value_from_nested(source, "occurrence_index"), -1
        )
        if _value_from_nested(source, "occurrence_index") is not None
        else -1,
        "weight_multiplier": _safe_float(
            _value_from_nested(source, "weight_multiplier", 0.0), 0.0
        ),
    }
    evidence_validation = row["evidence_envelope_validation"]
    if (
        isinstance(evidence_validation, Mapping)
        and evidence_validation.get("complete") is True
        and evidence_validation.get("status") == "valid"
    ):
        canonical = evidence_validation.get("canonical_timestamps")
        if isinstance(canonical, Mapping):
            row["source_event_time"] = str(
                canonical.get("event_time") or row["source_event_time"]
            )
    # Eligible execution facts are never backward compatible: both complete
    # current PIT lineage and hash-bound execution evidence are mandatory.
    lineage_complete = bool(
        row["lineage_status"] == "complete"
        and row["authority"] == "market_capital_ledger"
        and row["point_in_time_as_of"]
        and row["source_event_time"]
        and row["source_snapshot_id"]
        and row["source_snapshot_sha256"]
    )
    evidence_complete, _ = validate_execution_evidence(
        execution_evidence,
        source_snapshot_sha256=row["source_snapshot_sha256"],
    )
    if row["execution_eligible"] and not lineage_complete:
        row["execution_eligible"] = False
        row["execution_class"] = "pit_lineage_incomplete"
    elif row["execution_eligible"] and not evidence_complete:
        row["execution_eligible"] = False
        row["execution_class"] = "execution_evidence_invalid"
    if record_type in {"hold", "risk_reject"}:
        reason = str(source.get("reason") or source.get("reasons") or "")
        row["reason"] = reason
    row["_identity"] = _row_identity(row)
    return row


def _build_per_session_rows(
    *,
    date: str,
    records: list[dict[str, Any]],
    holds: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Convert fill records and holds into per-session decision rows.

    Returns (session_rows, contract_rejections).  Rows without a session
    are NOT silently dropped – they are recorded as contract rejections.
    """
    rows: list[dict[str, Any]] = []
    rejections: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _add_rejection(source: dict[str, Any], source_type: str) -> None:
        rejections.append(
            {
                "reason": "missing_session",
                "source_type": source_type,
                "style": str(source.get("style") or "unknown"),
                "symbol": str(
                    source.get("symbol") or source.get("ts_code") or "unknown"
                ),
                "stage": str(source.get("stage") or ""),
                "record_type_hint": str(source.get("record_type") or ""),
            }
        )

    for record in records:
        if not isinstance(record, dict):
            continue
        raw_session = _session_from_record(record)
        bar_time = _bar_time_from_record(record)
        session = _normalize_session(raw_session, bar_time)
        if session is None:
            _add_rejection(record, "record")
            continue
        record_type = _classify_record_type(record)
        row = _build_session_row(
            trade_date=date,
            session=session,
            record_type=record_type,
            source=record,
            bar_time=bar_time,
        )
        identity = row["_identity"]
        if row.get("cluster_id") or identity not in seen:
            seen.add(identity)
            rows.append(row)

    for hold in holds:
        if not isinstance(hold, dict):
            continue
        raw_session = _session_from_record(hold)
        bar_time = _bar_time_from_record(hold)
        session = _normalize_session(raw_session, bar_time)
        if session is None:
            _add_rejection(hold, "hold")
            continue
        record_type = _classify_hold_type(hold)
        row = _build_session_row(
            trade_date=date,
            session=session,
            record_type=record_type,
            source=hold,
            bar_time=bar_time,
        )
        identity = row["_identity"]
        if row.get("cluster_id") or identity not in seen:
            seen.add(identity)
            rows.append(row)

    return rows, rejections


def _existing_identities(path: Path) -> set[str]:
    """Read existing session identity keys from the review file.

    Session rows are embedded inside summary payloads under the
    'session_decisions' key, not as top-level lines.
    """
    if not path.exists():
        return set()
    identities: set[str] = set()
    for line in path.read_text("utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        # Check embedded session_decisions
        session_rows = row.get("session_decisions")
        if isinstance(session_rows, list):
            for srow in session_rows:
                if (
                    isinstance(srow, dict)
                    and srow.get("_row_type") == _SESSION_ROW_TYPE
                ):
                    ident = srow.get("_identity")
                    if ident:
                        identities.add(ident)
    return identities


def _existing_cluster_state(
    path: Path,
) -> tuple[dict[str, int], set[str]]:
    counts: dict[str, int] = {}
    execution_fill_ids: set[str] = set()
    if not path.exists():
        return counts, execution_fill_ids
    for line in path.read_text("utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        rows = payload.get("session_decisions") if isinstance(payload, dict) else []
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            cluster_id = str(row.get("cluster_id") or "").strip()
            if cluster_id:
                index = _safe_int(row.get("occurrence_index"), -1)
                counts[cluster_id] = max(counts.get(cluster_id, 0), index + 1)
            evidence = row.get("execution_evidence")
            if isinstance(evidence, dict):
                fill_id = str(evidence.get("execution_fill_id") or "").strip()
                if fill_id:
                    execution_fill_ids.add(fill_id)
    return counts, execution_fill_ids


def _write_payload_locked(path: Path, payload: dict[str, Any]) -> None:
    """Write a single summary line with lock, checksum, fsync, and cross‑append
    idempotency for embedded session_decisions.

    Session rows whose _identity already exists anywhere in the file are
    dropped from this payload before writing.  The identity check and the
    write happen inside the same fcntl exclusive-lock critical section to
    prevent TOCTOU races between concurrent append_review callers.

    The lock file is intentionally kept after unlock – it is a stable inode
    for fcntl locking.  Deleting it would create a new inode on the next
    open, breaking lock identity and opening a race window.
    """
    session_rows = payload.get("session_decisions")
    lock_path = path.with_suffix(path.suffix + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)

    with open(lock_path, "w") as lock_handle:
        fcntl.flock(lock_handle, fcntl.LOCK_EX)
        try:
            # Read existing identities under the lock to prevent TOCTOU
            existing_ids = _existing_identities(path)
            cluster_counts, existing_fill_ids = _existing_cluster_state(path)

            if isinstance(session_rows, list):
                filtered: list[dict[str, Any]] = []
                for row in session_rows:
                    if not isinstance(row, dict):
                        continue
                    cluster_id = str(row.get("cluster_id") or "").strip()
                    evidence = row.get("execution_evidence")
                    execution_fill_id = (
                        str(evidence.get("execution_fill_id") or "").strip()
                        if isinstance(evidence, dict)
                        else ""
                    )
                    if execution_fill_id and execution_fill_id in existing_fill_ids:
                        continue
                    if cluster_id:
                        occurrence_index = cluster_counts.get(cluster_id, 0)
                        row["occurrence_index"] = occurrence_index
                        row["cluster_role"] = (
                            "origin" if occurrence_index == 0 else "duplicate"
                        )
                        row["weight_multiplier"] = 1.0 if occurrence_index == 0 else 0.0
                        cluster_counts[cluster_id] = occurrence_index + 1
                        if execution_fill_id:
                            existing_fill_ids.add(execution_fill_id)
                        row["_identity"] = _row_identity(row)
                    ident = row.get("_identity", "")
                    if ident and ident in existing_ids:
                        continue  # already persisted – skip
                    if ident:
                        existing_ids.add(ident)
                    if row.get("_row_type") == _SESSION_ROW_TYPE:
                        content = {k: v for k, v in row.items() if k != "_checksum"}
                        row["_checksum"] = _compute_checksum(
                            json.dumps(content, ensure_ascii=False, sort_keys=True)
                        )
                    filtered.append(row)
                payload["session_decisions"] = filtered

            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        finally:
            fcntl.flock(lock_handle, fcntl.LOCK_UN)
    # Lock file intentionally left on disk – see docstring.


def load_review_rows(
    path: str | Path,
    *,
    verify_checksums: bool = True,
    include_summaries: bool = True,
) -> list[dict[str, Any]]:
    """Load rows from a review JSONL file, verifying checksums.

    Extracts embedded session_decisions rows (always returned) and appends
    the summary payload lines when *include_summaries* is True (default for
    backward compatibility).

    Raises ValueError if any session row has a corrupt checksum (fail-closed).
    """
    source = Path(path)
    if not source.exists():
        return []

    rows: list[dict[str, Any]] = []
    for line_num, line in enumerate(source.read_text("utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Corrupt JSON at {source}:{line_num}: {exc}") from exc
        if not isinstance(row, dict):
            continue

        # Extract and verify embedded session decision rows (never mutate)
        session_rows = row.get("session_decisions")
        if isinstance(session_rows, list):
            for i, srow in enumerate(session_rows):
                if not isinstance(srow, dict):
                    continue
                if srow.get("_row_type") != _SESSION_ROW_TYPE:
                    continue
                # Copy before modifying to avoid mutating the original
                copy = dict(srow)
                checksum = copy.pop("_checksum", None)
                if verify_checksums and checksum is not None:
                    expected = _compute_checksum(
                        json.dumps(copy, ensure_ascii=False, sort_keys=True)
                    )
                    if checksum != expected:
                        raise ValueError(
                            f"Checksum mismatch at {source}:{line_num} "
                            f"session_decisions[{i}]: "
                            f"expected={expected[:16]}..., got={checksum[:16]}..."
                        )
                copy["_checksum"] = checksum
                rows.append(copy)

        if include_summaries:
            rows.append(dict(row))
    return rows


def load_review_cluster_state(path: str | Path) -> dict[str, dict[str, Any]]:
    """Rebuild persistent 5-minute cluster counts from append-only facts."""

    state: dict[str, dict[str, Any]] = {}
    for row in load_review_rows(
        path,
        verify_checksums=True,
        include_summaries=False,
    ):
        cluster_id = str(row.get("cluster_id") or "").strip()
        if not cluster_id:
            continue
        item = state.setdefault(
            cluster_id,
            {
                "occurrence_count": 0,
                "execution_eligible_count": 0,
                "first_bar_time": str(row.get("bar_time") or ""),
            },
        )
        occurrence_index = _safe_int(row.get("occurrence_index"), -1)
        item["occurrence_count"] = max(
            int(item.get("occurrence_count") or 0), occurrence_index + 1
        )
        if row.get("execution_eligible") is True:
            item["execution_eligible_count"] = (
                int(item.get("execution_eligible_count") or 0) + 1
            )
    return state


def append_review(
    *,
    date: str,
    market: str,
    records: list[dict[str, Any]],
    errors: list[dict[str, Any]],
    holds: list[dict[str, Any]] | None = None,
    path: Path | None = None,
    position_pnl_summary: dict[str, dict[str, Any]] | None = None,
    affordability: dict[str, Any] | None = None,
    authority_scope: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Append one run review and return the persisted payload."""

    target = path or DEFAULT_REVIEW_PATH
    hold_rows = [dict(row) for row in (holds or []) if isinstance(row, dict)]

    observation_samples = build_observation_samples(
        hold_rows,
        date=date,
        market=market,
    )
    summary = summarize_records(records)
    run_score_summary = score_records(records)
    previous_score_summary = _latest_cumulative_score_summary(
        target,
        date=date,
        market=market,
    )
    score_summary = _merge_score_summaries(
        previous_score_summary,
        run_score_summary,
    )
    error_summary = summarize_errors(errors)
    hold_summary = summarize_holds(hold_rows)
    forward_summary = summarize_forward_outcomes([*records, *observation_samples])
    threshold_candidates = dynamic_threshold_candidates(forward_summary, hold_summary)
    health = style_health(records, errors, observation_samples)
    timing_rows = records or observation_samples

    # Build per-session decision rows for acceptance consumption
    session_rows, session_rejections = _build_per_session_rows(
        date=date,
        records=records,
        holds=hold_rows,
    )

    payload: dict[str, Any] = {
        "date": date,
        "market": market,
        "cadence": _latest_record_value(timing_rows, "cadence"),
        "latest_bar_time": _latest_record_value(timing_rows, "bar_time"),
        "capital_layer": "simulated",
        "account_type": "simulated",
        "real_trading_enabled": False,
        "authority_scope": (
            {
                "capital_authority_id": str(
                    (authority_scope or {}).get("capital_authority_id") or ""
                ),
                "authority_generation": (authority_scope or {}).get(
                    "authority_generation"
                ),
                "execution_lineage_id": str(
                    (authority_scope or {}).get("execution_lineage_id") or ""
                ),
            }
            if authority_scope is not None
            else None
        ),
        "state": "degraded" if errors else "ok",
        "record_count": len(records),
        "hold_count": int(hold_summary.get("total") or 0),
        "observation_sample_count": len(observation_samples),
        "error_count": len(errors),
        "errors": errors,
        "hold_reason_summary": hold_summary,
        "observation_samples": observation_samples,
        "session_decisions": session_rows,
        "session_contract_rejection_count": len(session_rejections),
        "session_contract_rejections": session_rejections,
        "forward_label_summary": forward_summary,
        "dynamic_threshold_candidates": threshold_candidates,
        "generated_at": _now_iso(),
        "score_contract_version": REVIEW_ECONOMICS_SCHEMA,
        "score_summary_scope": "trade_date_cumulative",
        **summary,
        "run_score_summary": run_score_summary,
        "score_summary": score_summary,
        "error_summary": error_summary,
        "style_health": health,
        "affordability": dict(affordability or {}),
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    payload["position_pnl_summary"] = position_pnl_summary

    # Write with lock + checksum + fsync (append-only, idempotent via session_decisions)
    _write_payload_locked(target, payload)
    return payload


__all__ = [
    "DEFAULT_REVIEW_PATH",
    "append_review",
    "build_observation_samples",
    "dynamic_threshold_candidates",
    "load_review_rows",
    "score_records",
    "summarize_forward_outcomes",
    "summarize_errors",
    "summarize_holds",
    "summarize_records",
    "style_health",
]
