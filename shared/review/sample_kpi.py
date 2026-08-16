#!/usr/bin/env python3
"""Pure, layered sample KPIs for A-share and CNFutures review outputs."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime
import math
from typing import Any, Iterable, Mapping, Optional, Sequence

from shared.review.forward_labels import (
    CANONICAL_HORIZONS,
    PRIMARY_HORIZON_POLICY_VERSION,
    SAMPLE_SCIENCE_CONTRACT_VERSION,
    canonical_horizon,
)


_READY_LABEL_STATUSES = {"ready", "labeled"}
_CALIBRATED_MODEL_STATES = {
    "calibrated",
    "out_of_sample_calibrated",
    "frozen_out_of_sample_calibrated",
}
_MIN_CALIBRATION_CLUSTERS = 20
_MIN_CALIBRATION_DAYS = 5
_MAX_RELIABILITY_ECE = 0.15


SAMPLE_LAYERS = (
    "observation_counterfactual",
    "exploration_fill",
    "exploitation_fill",
    "completed_round_trip",
    "exit_stop",
    "risk_reject",
    "chain_validation",
    "shadow_research",
)


def _normalized(value: Any) -> str:
    return str(value or "").strip().lower()


def _record_type(record: Mapping[str, Any]) -> str:
    return _normalized(
        record.get("record_type") or record.get("event_type") or record.get("type")
    )


def classify_sample_layers(record: Mapping[str, Any]) -> tuple[str, ...]:
    """Return every orthogonal layer a record belongs to in stable order."""

    if not isinstance(record, Mapping):
        raise TypeError("sample record must be a mapping")
    found: set[str] = set()
    raw_layers = record.get("sample_layers")
    if isinstance(raw_layers, (list, tuple, set)):
        found.update(_normalized(item) for item in raw_layers)
    raw_layer = _normalized(record.get("sample_layer"))
    if raw_layer:
        found.add(raw_layer)

    kind = _record_type(record)
    status = _normalized(record.get("status"))
    intent = _normalized(record.get("sample_intent"))
    classification = _normalized(record.get("sample_classification"))

    if kind in {"prediction", "observation", "counterfactual"}:
        found.add("observation_counterfactual")
    if kind in {"fill", "simulated_fill", "execution_fill"} or status in {
        "filled",
        "partial",
    }:
        if intent == "exploration":
            found.add("exploration_fill")
        elif intent in {"exploitation", "mature", "champion"}:
            found.add("exploitation_fill")
    if (
        kind in {"round_trip", "completed_round_trip", "trade_round_trip"}
        or record.get("round_trip_complete") is True
    ):
        found.add("completed_round_trip")
    exit_reason = _normalized(record.get("exit_reason"))
    if kind in {"exit", "stop", "exit_stop"} or exit_reason.startswith("stop"):
        found.add("exit_stop")
    if (
        kind in {"risk_reject", "risk_rejection", "safety_reject"}
        or status == "risk_rejected"
    ):
        found.add("risk_reject")
    if (
        kind in {"chain_validation", "chain-validation"}
        or classification == "chain_validation"
    ):
        found.add("chain_validation")
    if kind == "shadow_research":
        # Research-only shadow facts (e.g. event-catalyst labels): journaled
        # for audit and promotion-gate input, never a fill, round trip, or
        # order-path sample.
        found.add("shadow_research")
    return tuple(layer for layer in SAMPLE_LAYERS if layer in found)


def _style_for_record(record: Mapping[str, Any], layers: Sequence[str]) -> str:
    execution_layers = {
        "exploration_fill",
        "exploitation_fill",
        "completed_round_trip",
        "exit_stop",
    }
    if execution_layers.intersection(layers):
        value = (
            record.get("primary_style") or record.get("style") or record.get("style_id")
        )
    else:
        value = (
            record.get("style") or record.get("style_id") or record.get("primary_style")
        )
    return str(value or "unknown").strip() or "unknown"


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _finite_number(value: Any) -> Optional[float]:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) else None


def _trade_date(record: Mapping[str, Any]) -> str:
    raw = str(record.get("trade_date") or "").strip()
    digits = "".join(character for character in raw[:10] if character.isdigit())
    if len(digits) >= 8:
        return digits[:8]
    for field in ("prediction_at", "timestamp", "event_at", "closed_at", "exit_at"):
        raw_timestamp = str(record.get(field) or "").strip()
        if not raw_timestamp:
            continue
        try:
            parsed = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        return parsed.strftime("%Y%m%d")
    return ""


def _decision_cluster_key(record: Mapping[str, Any], sequence: int) -> str:
    explicit = str(record.get("decision_cluster_id") or "").strip()
    if explicit:
        return explicit
    base = str(record.get("base_snapshot_sha256") or "").strip().lower()
    if len(base) == 64 and all(character in "0123456789abcdef" for character in base):
        return "base:%s" % base
    symbol = str(record.get("symbol") or record.get("ts_code") or "").strip().upper()
    raw_timestamp = str(
        record.get("prediction_at") or record.get("timestamp") or ""
    ).strip()
    if symbol and raw_timestamp:
        try:
            parsed = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
            bucket = parsed.replace(
                minute=(parsed.minute // 5) * 5, second=0, microsecond=0
            ).isoformat()
        except (TypeError, ValueError):
            bucket = raw_timestamp
        return "fallback:%s:%s:%s" % (
            str(record.get("market") or "").strip().lower(),
            symbol,
            bucket,
        )
    return "unclustered:%d" % sequence


def _primary_ready_label(record: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    raw_horizon = record.get("primary_label_horizon")
    if not str(raw_horizon or "").strip():
        return None
    try:
        horizon = canonical_horizon(raw_horizon)
    except ValueError:
        return None
    labels = _label_mapping(record)
    for raw_name, label in labels.items():
        try:
            name = canonical_horizon(raw_name)
        except ValueError:
            continue
        if (
            name == horizon
            and isinstance(label, Mapping)
            and _normalized(label.get("status")) in _READY_LABEL_STATUSES
        ):
            return label
    return None


def _new_style() -> dict[str, Any]:
    return {
        "candidate_count": 0,
        "prediction_count": 0,
        "observation_counterfactual_count": 0,
        "exploration_fill_count": 0,
        "exploitation_fill_count": 0,
        "completed_round_trip_count": 0,
        "exit_stop_count": 0,
        "risk_reject_count": 0,
        "chain_validation_count": 0,
        "shadow_research_count": 0,
        "forward_label_counts": {name: {} for name in CANONICAL_HORIZONS},
        "win_rate": None,
        "average_pnl_cny": None,
        "average_win_cny": None,
        "average_loss_cny": None,
        "expectancy_cny": None,
        "gross_pnl_cny": 0.0,
        "fees_and_slippage_cny": 0.0,
        "post_cost_pnl_cny": 0.0,
        "max_drawdown_cny": None,
        "max_drawdown_scope": "account_daily_mtm_required",
        "trade_pnl_sequence_max_drawdown_cny": None,
        "performance_scope": "no_completed_round_trips",
        "performance_by_sample_intent": {},
        "rejection_reason_distribution": {},
        "_round_trips": {},
        "_rejections": Counter(),
        "_labels": {name: Counter() for name in CANONICAL_HORIZONS},
    }


def _label_mapping(record: Mapping[str, Any]) -> Mapping[str, Any]:
    labels = record.get("labels")
    if isinstance(labels, Mapping):
        return labels
    labels = record.get("forward_labels")
    return labels if isinstance(labels, Mapping) else {}


def _record_net_pnl(
    record: Mapping[str, Any],
) -> Optional[tuple[float, float, float]]:
    """Return explicit round-trip economics or ``None`` when incomplete.

    A missing net value must never be silently derived as zero or as
    ``gross-costs``.  Gross and net are separately required evidence, while
    fee/slippage remain explicit non-negative components.
    """

    gross = _finite_number(record.get("gross_pnl_cny"))
    net = _finite_number(
        record.get("net_pnl_cny")
        if record.get("net_pnl_cny") is not None
        else record.get("post_cost_pnl_cny")
    )
    fee = _finite_number(
        record.get("fee_cny")
        if record.get("fee_cny") is not None
        else record.get("fees_cny")
        if record.get("fees_cny") is not None
        else record.get("commission_cny")
    )
    slippage = _finite_number(record.get("slippage_cny"))
    if (
        gross is None
        or net is None
        or fee is None
        or slippage is None
        or fee < 0.0
        or slippage < 0.0
    ):
        return None
    costs = fee + slippage
    return gross, costs, net


def _performance_metrics(
    rows: Sequence[tuple[str, float, float, float, int]],
) -> dict[str, Any]:
    rows = sorted(rows, key=lambda item: (item[0], item[4]))
    pnl_values = [item[3] for item in rows]
    gross_values = [item[1] for item in rows]
    cost_values = [item[2] for item in rows]
    wins = [value for value in pnl_values if value > 0]
    losses = [value for value in pnl_values if value < 0]
    cumulative = 0.0
    high_water = 0.0
    max_drawdown = 0.0
    for value in pnl_values:
        cumulative += value
        high_water = max(high_water, cumulative)
        max_drawdown = max(max_drawdown, high_water - cumulative)
    return {
        "completed_round_trip_count": len(pnl_values),
        "win_rate": len(wins) / len(pnl_values),
        "average_pnl_cny": sum(pnl_values) / len(pnl_values),
        "average_win_cny": sum(wins) / len(wins) if wins else None,
        "average_loss_cny": sum(losses) / len(losses) if losses else None,
        "expectancy_cny": sum(pnl_values) / len(pnl_values),
        "gross_pnl_cny": sum(gross_values),
        "fees_and_slippage_cny": sum(cost_values),
        "post_cost_pnl_cny": sum(pnl_values),
        "trade_pnl_sequence_max_drawdown_cny": max_drawdown,
        "drawdown_scope": "auxiliary_trade_pnl_sequence_not_account_mtm",
    }


def _finalize_style(style: dict[str, Any]) -> dict[str, Any]:
    raw_buckets = style.pop("_round_trips")
    performance = {
        intent: _performance_metrics(rows)
        for intent, rows in sorted(raw_buckets.items())
        if rows
    }
    style["performance_by_sample_intent"] = performance
    if len(performance) == 1:
        intent, metrics = next(iter(performance.items()))
        style["performance_scope"] = intent
        for name in (
            "win_rate",
            "average_pnl_cny",
            "average_win_cny",
            "average_loss_cny",
            "expectancy_cny",
            "gross_pnl_cny",
            "fees_and_slippage_cny",
            "post_cost_pnl_cny",
            "trade_pnl_sequence_max_drawdown_cny",
        ):
            style[name] = metrics[name]
    elif len(performance) > 1:
        style["performance_scope"] = "separated_by_sample_intent"
        for name in (
            "win_rate",
            "average_pnl_cny",
            "average_win_cny",
            "average_loss_cny",
            "expectancy_cny",
            "gross_pnl_cny",
            "fees_and_slippage_cny",
            "post_cost_pnl_cny",
            "trade_pnl_sequence_max_drawdown_cny",
        ):
            style[name] = None

    style["rejection_reason_distribution"] = dict(
        sorted(style.pop("_rejections").items())
    )
    label_counters = style.pop("_labels")
    style["forward_label_counts"] = {
        name: dict(sorted(label_counters[name].items())) for name in CANONICAL_HORIZONS
    }
    return style


def _portfolio_summary(
    portfolio_snapshot: Optional[Mapping[str, Any]],
) -> dict[str, Any]:
    if not isinstance(portfolio_snapshot, Mapping):
        return {
            "status": "missing_authoritative_portfolio_snapshot",
            "source": None,
            "as_of": None,
            "account_equity_cny": None,
            "total_risk_cny": None,
            "gross_exposure_cny": None,
            "shadow_capital_included": False,
            "real_trading_enabled": False,
        }
    source = str(portfolio_snapshot.get("source") or "").strip()
    required = ("account_equity_cny", "total_risk_cny", "gross_exposure_cny")
    if not source or any(portfolio_snapshot.get(field) is None for field in required):
        return {
            "status": "missing_authoritative_portfolio_evidence",
            "source": source or None,
            "as_of": portfolio_snapshot.get("as_of"),
            "account_equity_cny": None,
            "total_risk_cny": None,
            "gross_exposure_cny": None,
            "shadow_capital_included": False,
            "real_trading_enabled": False,
        }
    return {
        "status": "available",
        "source": source,
        "as_of": portfolio_snapshot.get("as_of"),
        "account_equity_cny": _number(portfolio_snapshot.get("account_equity_cny")),
        "total_risk_cny": _number(portfolio_snapshot.get("total_risk_cny")),
        "gross_exposure_cny": _number(portfolio_snapshot.get("gross_exposure_cny")),
        "shadow_capital_included": False,
        "real_trading_enabled": False,
    }


def _sample_size_evidence(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    ready_label_cells = 0
    primary_rows: list[tuple[str, str, float]] = []
    status_counts: Counter[str] = Counter()
    for sequence, row in enumerate(rows):
        if _record_type(row) not in {"prediction", "observation", "counterfactual"}:
            continue
        labels = _label_mapping(row)
        canonical_seen: set[str] = set()
        for raw_name, label in labels.items():
            try:
                name = canonical_horizon(raw_name)
            except ValueError:
                continue
            if name in canonical_seen or not isinstance(label, Mapping):
                continue
            canonical_seen.add(name)
            if _normalized(label.get("status")) in _READY_LABEL_STATUSES:
                ready_label_cells += 1

        primary = _primary_ready_label(row)
        if primary is None:
            if str(row.get("primary_label_horizon") or "").strip():
                status_counts["primary_horizon_not_ready"] += 1
            else:
                status_counts["missing_prespecified_primary_horizon"] += 1
            continue
        cluster = _decision_cluster_key(row, sequence)
        trade_date = _trade_date(row)
        propensity = _finite_number(row.get("propensity"))
        if propensity is None:
            propensity = _finite_number(row.get("selection_probability"))
        weight = (
            1.0 / propensity
            if propensity is not None and 0.0 < propensity <= 1.0
            else 1.0
        )
        primary_rows.append((cluster, trade_date, weight))
        status_counts["ready"] += 1

    cluster_weights: dict[str, float] = {}
    cluster_dates: dict[str, str] = {}
    for cluster, trade_date, weight in primary_rows:
        cluster_weights[cluster] = max(cluster_weights.get(cluster, 0.0), weight)
        if trade_date:
            cluster_dates.setdefault(cluster, trade_date)
    weights = list(cluster_weights.values())
    weight_sum = sum(weights)
    squared_sum = sum(weight * weight for weight in weights)
    n_eff = (weight_sum * weight_sum / squared_sum) if squared_sum > 0.0 else 0.0
    return {
        "primary_horizon_policy_version": PRIMARY_HORIZON_POLICY_VERSION,
        "unit_of_independence": "decision_cluster_at_prespecified_primary_horizon",
        "ready_label_cell_count": ready_label_cells,
        "raw_N": len(primary_rows),
        "unique_decision_cluster_count": len(cluster_weights),
        "independent_trading_day_count": len(set(cluster_dates.values())),
        "N_eff": round(n_eff, 6),
        "primary_horizon_status_counts": dict(sorted(status_counts.items())),
        "label_cells_are_independent_samples": False,
    }


def _calibration_evidence(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    by_cluster: dict[str, list[tuple[float, int, str]]] = {}
    for sequence, row in enumerate(rows):
        if _record_type(row) not in {"prediction", "observation", "counterfactual"}:
            continue
        if _normalized(row.get("calibration_role")) != "primary":
            continue
        model_state = _normalized(row.get("probability_model_state"))
        probability = _finite_number(row.get("calibrated_probability"))
        label = _primary_ready_label(row)
        net_return = (
            _finite_number(label.get("net_return_after_costs")) if label else None
        )
        if (
            model_state not in _CALIBRATED_MODEL_STATES
            or probability is None
            or not 0.0 <= probability <= 1.0
            or net_return is None
        ):
            continue
        cluster = _decision_cluster_key(row, sequence)
        by_cluster.setdefault(cluster, []).append(
            (probability, 1 if net_return > 0.0 else 0, _trade_date(row))
        )

    # A cluster with multiple calibration primaries is ambiguous and therefore
    # contributes no evidence; selecting one after seeing outcomes would leak.
    samples = [values[0] for values in by_cluster.values() if len(values) == 1]
    ambiguous = sum(1 for values in by_cluster.values() if len(values) != 1)
    if not samples:
        return {
            "status": "unavailable_no_calibrated_predictions",
            "sufficient": False,
            "independent_sample_count": 0,
            "independent_trading_day_count": 0,
            "ambiguous_cluster_count": ambiguous,
            "brier_score": None,
            "log_loss": None,
            "base_rate": None,
            "base_rate_brier_score": None,
            "brier_skill_score": None,
            "reliability_ece": None,
            "reliability_bins": [],
        }

    probabilities = [sample[0] for sample in samples]
    outcomes = [sample[1] for sample in samples]
    sample_count = len(samples)
    base_rate = sum(outcomes) / sample_count
    brier = (
        sum(
            (probability - outcome) ** 2
            for probability, outcome in zip(probabilities, outcomes)
        )
        / sample_count
    )
    epsilon = 1e-15
    log_loss = (
        -sum(
            outcome * math.log(min(max(probability, epsilon), 1.0 - epsilon))
            + (1 - outcome)
            * math.log(min(max(1.0 - probability, epsilon), 1.0 - epsilon))
            for probability, outcome in zip(probabilities, outcomes)
        )
        / sample_count
    )
    baseline_brier = (
        sum((base_rate - outcome) ** 2 for outcome in outcomes) / sample_count
    )
    brier_skill = 1.0 - brier / baseline_brier if baseline_brier > 0.0 else None

    bins: list[dict[str, Any]] = []
    weighted_gap = 0.0
    for index in range(5):
        lower = index / 5.0
        upper = (index + 1) / 5.0
        selected = [
            (probability, outcome)
            for probability, outcome in zip(probabilities, outcomes)
            if lower <= probability < upper or (index == 4 and probability == 1.0)
        ]
        if not selected:
            continue
        mean_probability = sum(item[0] for item in selected) / len(selected)
        observed_rate = sum(item[1] for item in selected) / len(selected)
        gap = abs(mean_probability - observed_rate)
        weighted_gap += gap * len(selected) / sample_count
        bins.append(
            {
                "lower": lower,
                "upper": upper,
                "count": len(selected),
                "mean_probability": round(mean_probability, 6),
                "observed_rate": round(observed_rate, 6),
                "absolute_gap": round(gap, 6),
            }
        )

    independent_days = len({sample[2] for sample in samples if sample[2]})
    sufficient = (
        sample_count >= _MIN_CALIBRATION_CLUSTERS
        and independent_days >= _MIN_CALIBRATION_DAYS
        and brier_skill is not None
        and brier_skill > 0.0
        and weighted_gap <= _MAX_RELIABILITY_ECE
    )
    status = "sufficient" if sufficient else "insufficient_independent_samples"
    if (
        sample_count >= _MIN_CALIBRATION_CLUSTERS
        and independent_days >= _MIN_CALIBRATION_DAYS
    ):
        status = "sufficient" if sufficient else "calibration_quality_below_threshold"
    return {
        "status": status,
        "sufficient": sufficient,
        "independent_sample_count": sample_count,
        "independent_trading_day_count": independent_days,
        "ambiguous_cluster_count": ambiguous,
        "brier_score": round(brier, 6),
        "log_loss": round(log_loss, 6),
        "base_rate": round(base_rate, 6),
        "base_rate_brier_score": round(baseline_brier, 6),
        "brier_skill_score": round(brier_skill, 6) if brier_skill is not None else None,
        "reliability_ece": round(weighted_gap, 6),
        "reliability_bins": bins,
        "minimum_independent_samples": _MIN_CALIBRATION_CLUSTERS,
        "minimum_independent_trading_days": _MIN_CALIBRATION_DAYS,
        "maximum_reliability_ece": _MAX_RELIABILITY_ECE,
    }


def _account_drawdown_evidence(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    by_date: dict[str, tuple[float, str]] = {}
    conflict_dates: set[str] = set()
    for row in rows:
        if _normalized(row.get("evidence_type")) != "account_daily_mtm_equity":
            continue
        trade_date = _trade_date(row)
        equity = _finite_number(row.get("account_equity_cny"))
        source = str(row.get("equity_source") or "").strip()
        if not trade_date or equity is None or equity <= 0.0 or not source:
            continue
        prior = by_date.get(trade_date)
        if prior is not None and (prior[0] != equity or prior[1] != source):
            conflict_dates.add(trade_date)
        else:
            by_date[trade_date] = (equity, source)
    if conflict_dates:
        return {
            "status": "invalid_conflicting_daily_mtm_equity",
            "source": "account_daily_mtm_equity",
            "equity_source": None,
            "observation_count": len(by_date),
            "independent_trading_day_count": len(by_date),
            "conflict_dates": sorted(conflict_dates),
            "max_drawdown_cny": None,
            "max_drawdown_ratio": None,
            "peak_equity_cny": None,
            "trough_equity_cny": None,
        }
    if len(by_date) < 2:
        return {
            "status": "insufficient_daily_mtm_equity_history",
            "source": "account_daily_mtm_equity",
            "equity_source": next(iter(by_date.values()))[1] if by_date else None,
            "observation_count": len(by_date),
            "independent_trading_day_count": len(by_date),
            "max_drawdown_cny": None,
            "max_drawdown_ratio": None,
            "peak_equity_cny": None,
            "trough_equity_cny": None,
        }
    ordered = [(date, *by_date[date]) for date in sorted(by_date)]
    equity_sources = {item[2] for item in ordered}
    if len(equity_sources) != 1:
        return {
            "status": "invalid_mixed_daily_mtm_sources",
            "source": "account_daily_mtm_equity",
            "equity_source": None,
            "observation_count": len(ordered),
            "independent_trading_day_count": len(ordered),
            "max_drawdown_cny": None,
            "max_drawdown_ratio": None,
            "peak_equity_cny": None,
            "trough_equity_cny": None,
        }
    peak = ordered[0][1]
    max_drawdown = 0.0
    drawdown_peak = peak
    drawdown_trough = peak
    for _, equity, _ in ordered:
        if equity > peak:
            peak = equity
        drawdown = peak - equity
        if drawdown > max_drawdown:
            max_drawdown = drawdown
            drawdown_peak = peak
            drawdown_trough = equity
    return {
        "status": "available",
        "source": "account_daily_mtm_equity",
        "equity_source": next(iter(equity_sources)),
        "observation_count": len(ordered),
        "independent_trading_day_count": len(ordered),
        "max_drawdown_cny": max_drawdown,
        "max_drawdown_ratio": max_drawdown / drawdown_peak
        if drawdown_peak > 0.0
        else None,
        "peak_equity_cny": drawdown_peak,
        "trough_equity_cny": drawdown_trough,
    }


def build_opportunity_capture_evidence(
    *,
    full_eligible_symbols: Optional[Sequence[str]],
    scanned_symbols: Sequence[str],
    top_k_symbols: Sequence[str],
    realized_opportunity_symbols: Sequence[str],
    full_eligible_universe_complete: bool,
) -> dict[str, Any]:
    """Keep full-universe recall, scanned recall and top-K precision distinct."""

    scanned = {
        str(symbol).strip().upper() for symbol in scanned_symbols if str(symbol).strip()
    }
    top_k = {
        str(symbol).strip().upper() for symbol in top_k_symbols if str(symbol).strip()
    }
    realized = {
        str(symbol).strip().upper()
        for symbol in realized_opportunity_symbols
        if str(symbol).strip()
    }
    full = (
        {
            str(symbol).strip().upper()
            for symbol in full_eligible_symbols
            if str(symbol).strip()
        }
        if full_eligible_symbols is not None
        else set()
    )
    scanned_opportunities = realized.intersection(scanned)
    captured_scanned = scanned_opportunities.intersection(top_k)
    scanned_recall = (
        len(captured_scanned) / len(scanned_opportunities)
        if scanned_opportunities
        else None
    )
    top_k_precision = len(realized.intersection(top_k)) / len(top_k) if top_k else None
    full_available = bool(
        full_eligible_universe_complete and full_eligible_symbols is not None
    )
    full_opportunities = realized.intersection(full) if full_available else set()
    full_recall = (
        len(full_opportunities.intersection(top_k)) / len(full_opportunities)
        if full_available and full_opportunities
        else None
    )
    return {
        "claim_scope": "full_eligible_universe"
        if full_available
        else "scanned_universe_only",
        "full_eligible_universe_count": len(full) if full_available else None,
        "scanned_universe_count": len(scanned),
        "top_k_count": len(top_k),
        "realized_opportunity_count_in_scanned_universe": len(scanned_opportunities),
        "captured_opportunity_count_in_top_k": len(realized.intersection(top_k)),
        "full_eligible_universe_recall": full_recall,
        "full_eligible_universe_status": (
            "available" if full_available else "unavailable_incomplete_universe"
        ),
        "scanned_universe_recall": scanned_recall,
        "scanned_universe_status": (
            "available"
            if scanned_opportunities
            else "unavailable_no_realized_opportunities"
        ),
        "top_k_precision": top_k_precision,
        "top_k_status": "available" if top_k else "unavailable_empty_top_k",
    }


def build_sample_kpi(
    records: Iterable[Mapping[str, Any]],
    *,
    portfolio_snapshot: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Aggregate layered sample metrics without inventing executable capital."""

    source_rows = deepcopy(list(records))
    for row in source_rows:
        if not isinstance(row, Mapping):
            raise TypeError("every sample record must be a mapping")
    excluded_source_classes: Counter[str] = Counter()
    rows: list[Mapping[str, Any]] = []
    for row in source_rows:
        source_class = _normalized(row.get("source_class"))
        if source_class == "fixture":
            excluded_source_classes[source_class] += 1
            continue
        rows.append(row)
    styles: dict[str, dict[str, Any]] = {}
    evidence_statuses: Counter[str] = Counter()
    invalid_completed_round_trip_count = 0

    for sequence, row in enumerate(rows):
        layers = classify_sample_layers(row)
        round_trip_economics: Optional[tuple[float, float, float]] = None
        if "completed_round_trip" in layers:
            round_trip_economics = _record_net_pnl(row)
            if round_trip_economics is None:
                invalid_completed_round_trip_count += 1
                layers = tuple(
                    layer for layer in layers if layer != "completed_round_trip"
                )
        style_name = _style_for_record(row, layers)
        style = styles.setdefault(style_name, _new_style())
        kind = _record_type(row)

        if (
            kind in {"candidate", "candidate_prediction"}
            or row.get("is_candidate") is True
        ):
            style["candidate_count"] += 1
        if (
            kind in {"prediction", "counterfactual", "observation"}
            or row.get("is_prediction") is True
        ):
            style["prediction_count"] += 1

        for layer in layers:
            style[layer + "_count"] += 1

        labels = _label_mapping(row)
        canonical_seen: set[str] = set()
        for raw_name, label in labels.items():
            try:
                name = canonical_horizon(raw_name)
            except ValueError:
                continue
            if name in canonical_seen or not isinstance(label, Mapping):
                continue
            canonical_seen.add(name)
            status = str(label.get("status") or "missing_status")
            style["_labels"][name][status] += 1

        if "completed_round_trip" in layers and round_trip_economics is not None:
            gross, costs, net = round_trip_economics
            timestamp = str(
                row.get("timestamp") or row.get("closed_at") or row.get("exit_at") or ""
            )
            intent = _normalized(row.get("sample_intent"))
            if intent in {"mature", "champion"}:
                intent = "exploitation"
            if intent not in {"exploration", "exploitation"}:
                intent = "unclassified"
            style["_round_trips"].setdefault(intent, []).append(
                (timestamp, gross, costs, net, sequence)
            )

        if "risk_reject" in layers:
            reason = str(
                row.get("reject_reason")
                or row.get("risk_reject_reason")
                or row.get("rejection_reason")
                or "unspecified"
            )
            style["_rejections"][reason] += 1

        evidence_status = str(row.get("evidence_status") or "").strip()
        if evidence_status:
            evidence_statuses[evidence_status] += 1

    finalized_styles = {name: _finalize_style(styles[name]) for name in sorted(styles)}
    missing_evidence_count = sum(
        count
        for status, count in evidence_statuses.items()
        if status.startswith("missing")
    )
    layer_totals = {
        layer: sum(style[layer + "_count"] for style in finalized_styles.values())
        for layer in SAMPLE_LAYERS
    }
    round_trip_intent_totals: Counter[str] = Counter()
    for style in finalized_styles.values():
        for intent, performance in style["performance_by_sample_intent"].items():
            round_trip_intent_totals[intent] += int(
                performance["completed_round_trip_count"]
            )
    return {
        "sample_science_contract_version": SAMPLE_SCIENCE_CONTRACT_VERSION,
        "styles": finalized_styles,
        "sample_layer_totals": layer_totals,
        "completed_round_trip_totals_by_sample_intent": dict(
            sorted(round_trip_intent_totals.items())
        ),
        "evidence_status_counts": dict(sorted(evidence_statuses.items())),
        "excluded_source_class_counts": dict(sorted(excluded_source_classes.items())),
        "missing_evidence_count": missing_evidence_count,
        "invalid_completed_round_trip_count": invalid_completed_round_trip_count,
        "sample_size_evidence": _sample_size_evidence(rows),
        "calibration_evidence": _calibration_evidence(rows),
        "account_drawdown_evidence": _account_drawdown_evidence(rows),
        "portfolio": _portfolio_summary(deepcopy(portfolio_snapshot)),
        "shadow_capital_aggregated": False,
        "real_trading_enabled": False,
        "live_execution_enabled": False,
    }


__all__ = [
    "SAMPLE_LAYERS",
    "build_opportunity_capture_evidence",
    "build_sample_kpi",
    "classify_sample_layers",
]
