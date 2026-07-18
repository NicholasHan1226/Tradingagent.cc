"""Deterministic offline metrics over frozen A-share journal evidence.

This module is deliberately a read-side projection.  It neither appends to the
SampleJournal nor grants model, capital, position, order, or promotion authority.
"""

from __future__ import annotations

import math
import random
import re
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime
from statistics import mean, median
from typing import Any, Mapping, Sequence

from shared.models.lifecycle import ValidationPlan
from shared.review.forward_labels import canonical_horizon
from shared.review.outcome_evaluation import (
    OutcomeMarketTruthVerifier,
    OutcomeEvaluationError,
    ValidationPlanProvenanceVerifier,
    canonical_sha256,
    eligible_unambiguous_outcome_rows,
    outcome_rows_as_sample_records,
    verify_outcome_evaluation_against_source,
)
from shared.review.sample_kpi import build_sample_kpi


OFFLINE_METRICS_SCHEMA_VERSION = "ashare-offline-metrics.v1"
_BOOTSTRAP_METHOD = "moving_observed_trade_date_block_bootstrap.v1"
_HORIZON_BLOCK_LENGTH = {
    "m30": 1,
    "m60": 1,
    "close": 1,
    "1d": 1,
    "3d": 3,
    "5d": 5,
}
_TRADE_DATE_RE = re.compile(r"^\d{8}$")
_AUTHORITY = {
    "research_only": True,
    "capital_authority": False,
    "position_authority": False,
    "order_authority": False,
    "automatic_promotion_enabled": False,
    "automatic_risk_expansion_enabled": False,
    "live_transition_authorized": False,
    "real_trading_enabled": False,
}


class OfflineScienceError(ValueError):
    """Raised when an offline report cannot be reproduced safely."""


def _quantile(values: Sequence[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _round(value: float | None) -> float | None:
    return round(value, 12) if value is not None else None


def _independent_rows(
    outcome_report: Mapping[str, Any],
) -> tuple[list[Mapping[str, Any]], int]:
    by_cluster: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in outcome_report["outcomes"]:
        if row.get("eligible_for_statistical_learning") is not True:
            continue
        cluster = str(row.get("decision_cluster_id") or "").strip()
        if cluster:
            by_cluster[cluster].append(row)
    ambiguous = sum(1 for rows in by_cluster.values() if len(rows) != 1)
    selected = [rows[0] for rows in by_cluster.values() if len(rows) == 1]
    selected.sort(
        key=lambda row: (
            str(row.get("trade_date") or ""),
            str(row.get("decision_cluster_id") or ""),
        )
    )
    return selected, ambiguous


def _valid_trade_date(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not _TRADE_DATE_RE.fullmatch(raw):
        return None
    try:
        datetime.strptime(raw, "%Y%m%d")
    except ValueError:
        return None
    return raw


def _moving_observed_trade_date_blocks(
    rows: Sequence[Mapping[str, Any]],
    *,
    block_length: int,
) -> tuple[dict[str, list[Mapping[str, Any]]], list[tuple[str, ...]], int]:
    """Group the full same-day cross-section into moving observed-date blocks."""

    if (
        isinstance(block_length, bool)
        or not isinstance(block_length, int)
        or block_length <= 0
    ):
        raise OfflineScienceError("bootstrap_block_length_invalid")
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    excluded = 0
    for row in rows:
        trade_date = _valid_trade_date(row.get("trade_date"))
        if trade_date is None:
            excluded += 1
            continue
        grouped[trade_date].append(row)
    ordered_dates = sorted(grouped)
    blocks = [
        tuple(ordered_dates[index : index + block_length])
        for index in range(max(0, len(ordered_dates) - block_length + 1))
    ]
    return dict(grouped), blocks, excluded


def _block_bootstrap(
    rows: Sequence[Mapping[str, Any]],
    *,
    iterations: int,
    seed_material: str,
    propensity_weight_kish_n_eff: float | None,
) -> dict[str, Any]:
    inference_rows: list[Mapping[str, Any]] = []
    horizon_lengths: list[int] = []
    excluded = 0
    for row in rows:
        try:
            horizon = canonical_horizon(row.get("primary_horizon"))
        except ValueError:
            excluded += 1
            continue
        trade_date = _valid_trade_date(row.get("trade_date"))
        label = row.get("label")
        net = (
            label.get("net_return_after_costs") if isinstance(label, Mapping) else None
        )
        if (
            trade_date is None
            or isinstance(net, bool)
            or not isinstance(net, (int, float))
            or not math.isfinite(float(net))
        ):
            excluded += 1
            continue
        inference_rows.append(row)
        horizon_lengths.append(_HORIZON_BLOCK_LENGTH[horizon])

    block_length = max(horizon_lengths) if horizon_lengths else None
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    blocks: list[tuple[str, ...]] = []
    if block_length is not None:
        grouped, blocks, unexpected_exclusions = _moving_observed_trade_date_blocks(
            inference_rows,
            block_length=block_length,
        )
        excluded += unexpected_exclusions
    observed_day_count = len(grouped)
    kish = (
        float(propensity_weight_kish_n_eff)
        if isinstance(propensity_weight_kish_n_eff, (int, float))
        and not isinstance(propensity_weight_kish_n_eff, bool)
        and math.isfinite(float(propensity_weight_kish_n_eff))
        and float(propensity_weight_kish_n_eff) >= 0.0
        else 0.0
    )
    dependence_adjusted = (
        min(math.floor(kish), observed_day_count // block_length)
        if block_length is not None
        else 0
    )
    base = {
        "method": _BOOTSTRAP_METHOD,
        "iteration_count": iterations,
        "input_unambiguous_decision_cluster_count": len(rows),
        "propensity_weight_kish_n_eff": _round(kish),
        "observed_trading_day_count": observed_day_count,
        "block_length_trading_days": block_length,
        "candidate_contiguous_block_count": len(blocks),
        "dependence_adjusted_sample_count": dependence_adjusted,
        "excluded_inference_row_count": excluded,
    }
    if block_length is None:
        return {
            **base,
            "inference_status": "unavailable_no_valid_inference_rows",
            "mean_ci_90": {"lower": None, "upper": None},
            "probability_mean_positive": None,
        }
    if observed_day_count < 2 * block_length or len(blocks) < 2:
        return {
            **base,
            "inference_status": "unavailable_insufficient_contiguous_date_blocks",
            "mean_ci_90": {"lower": None, "upper": None},
            "probability_mean_positive": None,
        }

    rng = random.Random(int(seed_material[:16], 16))
    boot_means: list[float] = []
    for _ in range(iterations):
        sampled_dates: list[str] = []
        while len(sampled_dates) < observed_day_count:
            sampled_dates.extend(rng.choice(blocks))
        sampled_dates = sampled_dates[:observed_day_count]
        sampled: list[float] = []
        for trade_date in sampled_dates:
            sampled.extend(
                float(row["label"]["net_return_after_costs"])
                for row in grouped[trade_date]
            )
        boot_means.append(mean(sampled))
    return {
        **base,
        "inference_status": "available",
        "mean_ci_90": {
            "lower": _round(_quantile(boot_means, 0.05)),
            "upper": _round(_quantile(boot_means, 0.95)),
        },
        "probability_mean_positive": _round(
            sum(value > 0.0 for value in boot_means) / len(boot_means)
        ),
    }


def _performance(
    rows: Sequence[Mapping[str, Any]],
    *,
    iterations: int,
    seed_material: str,
    propensity_weight_kish_n_eff: float | None,
) -> dict[str, Any]:
    values = [float(row["label"]["net_return_after_costs"]) for row in rows]
    if not values:
        return {
            "status": "unavailable_no_ready_outcomes",
            "mean_net_return_after_costs": None,
            "median_net_return_after_costs": None,
            "positive_rate": None,
            "q10_net_return_after_costs": None,
            "expected_shortfall_q10": None,
            "bootstrap": _block_bootstrap(
                rows,
                iterations=iterations,
                seed_material=seed_material,
                propensity_weight_kish_n_eff=propensity_weight_kish_n_eff,
            ),
        }
    q10 = _quantile(values, 0.10)
    tail = [value for value in values if q10 is not None and value <= q10]
    return {
        "status": "available",
        "mean_net_return_after_costs": _round(mean(values)),
        "median_net_return_after_costs": _round(median(values)),
        "positive_rate": _round(sum(value > 0.0 for value in values) / len(values)),
        "q10_net_return_after_costs": _round(q10),
        "expected_shortfall_q10": _round(mean(tail)) if tail else None,
        "bootstrap": _block_bootstrap(
            rows,
            iterations=iterations,
            seed_material=seed_material,
            propensity_weight_kish_n_eff=propensity_weight_kish_n_eff,
        ),
    }


def recompute_offline_metrics(
    *,
    events: Sequence[Mapping[str, Any]],
    outcome_report: Mapping[str, Any],
    expected_as_of: str,
    expected_authority_scope: Mapping[str, Any],
    validation_plan: ValidationPlan | None = None,
    validation_plan_provenance: Mapping[str, Any] | None = None,
    validation_plan_provenance_verifier: ValidationPlanProvenanceVerifier | None = None,
    market_truth_verifier: OutcomeMarketTruthVerifier | None = None,
    bootstrap_iterations: int = 1000,
) -> dict[str, Any]:
    """Recompute metrics from immutable inputs without selecting a winner."""

    try:
        verify_outcome_evaluation_against_source(
            outcome_report,
            events=events,
            expected_as_of=expected_as_of,
            expected_authority_scope=expected_authority_scope,
            validation_plan=validation_plan,
            validation_plan_provenance=validation_plan_provenance,
            validation_plan_provenance_verifier=validation_plan_provenance_verifier,
            market_truth_verifier=market_truth_verifier,
        )
    except OutcomeEvaluationError as exc:
        raise OfflineScienceError(str(exc)) from exc
    if isinstance(events, (str, bytes, bytearray)):
        raise OfflineScienceError("events_must_be_sequence")
    copied_events = deepcopy(list(events))
    if canonical_sha256(copied_events) != outcome_report["source_events_sha256"]:
        raise OfflineScienceError("events_do_not_match_outcome_report")
    if (
        isinstance(bootstrap_iterations, bool)
        or not isinstance(bootstrap_iterations, int)
        or bootstrap_iterations <= 0
        or bootstrap_iterations > 100_000
    ):
        raise OfflineScienceError("bootstrap_iterations_invalid")

    independent_rows, eligible_cluster_count, ambiguous_cluster_count = (
        eligible_unambiguous_outcome_rows(outcome_report)
    )
    disposition_counts = Counter(
        str(row.get("disposition") or "unknown") for row in outcome_report["outcomes"]
    )
    label_status_counts = Counter(
        str((row.get("label") or {}).get("status") or "missing")
        for row in outcome_report["outcomes"]
    )
    exclusion_reason_counts: Counter[str] = Counter()
    for row in outcome_report["outcomes"]:
        exclusion_reason_counts.update(row.get("exclusion_reasons") or [])
    canonical_kpi = build_sample_kpi(outcome_rows_as_sample_records(independent_rows))
    sample_size = canonical_kpi.get("sample_size_evidence")
    sample_size = sample_size if isinstance(sample_size, Mapping) else {}
    kish_n_eff = sample_size.get("N_eff")
    bootstrap_seed = canonical_sha256(
        {
            "source_events_sha256": outcome_report["source_events_sha256"],
            "method": _BOOTSTRAP_METHOD,
        }
    )
    performance = _performance(
        independent_rows,
        iterations=bootstrap_iterations,
        seed_material=bootstrap_seed,
        propensity_weight_kish_n_eff=(
            float(kish_n_eff)
            if isinstance(kish_n_eff, (int, float)) and not isinstance(kish_n_eff, bool)
            else None
        ),
    )
    inference = performance["bootstrap"]
    report: dict[str, Any] = {
        "record_type": "ashare_offline_metrics",
        "schema_version": OFFLINE_METRICS_SCHEMA_VERSION,
        "source_events_sha256": outcome_report["source_events_sha256"],
        "source_outcome_report_sha256": outcome_report["report_sha256"],
        "as_of": outcome_report["as_of"],
        "eligible_unique_decision_cluster_count": eligible_cluster_count,
        "eligible_unambiguous_decision_cluster_count": len(independent_rows),
        "observed_trading_day_count": inference["observed_trading_day_count"],
        "unique_decision_cluster_count": sample_size.get(
            "unique_decision_cluster_count"
        ),
        "propensity_weight_kish_n_eff": kish_n_eff,
        "dependence_adjusted_sample_count": inference[
            "dependence_adjusted_sample_count"
        ],
        "ambiguous_cluster_count": ambiguous_cluster_count,
        "science_cohort_policy": (
            "eligible_outcome_exact_source_and_one_row_per_decision_cluster"
        ),
        "disposition_counts": dict(sorted(disposition_counts.items())),
        "label_status_counts": dict(sorted(label_status_counts.items())),
        "exclusion_reason_counts": dict(sorted(exclusion_reason_counts.items())),
        "performance": performance,
        "canonical_sample_kpi": canonical_kpi,
        "authority": deepcopy(_AUTHORITY),
    }
    report["report_sha256"] = canonical_sha256(report)
    _verify_offline_metrics_structure(report)
    return report


def _verify_offline_metrics_structure(value: Any) -> bool:
    if not isinstance(value, Mapping):
        raise OfflineScienceError("offline_metrics_report_invalid")
    if value.get("schema_version") != OFFLINE_METRICS_SCHEMA_VERSION:
        raise OfflineScienceError("offline_metrics_schema_invalid")
    if value.get("authority") != _AUTHORITY:
        raise OfflineScienceError("offline_metrics_authority_invalid")
    performance = value.get("performance")
    if not isinstance(performance, Mapping) or performance.get("status") not in {
        "available",
        "unavailable_no_ready_outcomes",
    }:
        raise OfflineScienceError("offline_metrics_performance_invalid")
    bootstrap = performance.get("bootstrap")
    if (
        not isinstance(bootstrap, Mapping)
        or bootstrap.get("method") != _BOOTSTRAP_METHOD
        or bootstrap.get("inference_status")
        not in {
            "available",
            "unavailable_no_valid_inference_rows",
            "unavailable_insufficient_contiguous_date_blocks",
        }
        or value.get("eligible_unambiguous_decision_cluster_count")
        != bootstrap.get("input_unambiguous_decision_cluster_count")
        or value.get("observed_trading_day_count")
        != bootstrap.get("observed_trading_day_count")
        or value.get("propensity_weight_kish_n_eff")
        != bootstrap.get("propensity_weight_kish_n_eff")
        or value.get("dependence_adjusted_sample_count")
        != bootstrap.get("dependence_adjusted_sample_count")
    ):
        raise OfflineScienceError("offline_metrics_inference_contract_invalid")
    if bootstrap.get("inference_status") != "available" and (
        bootstrap.get("mean_ci_90") != {"lower": None, "upper": None}
        or bootstrap.get("probability_mean_positive") is not None
    ):
        raise OfflineScienceError("offline_metrics_unavailable_inference_not_null")
    unsigned = deepcopy(dict(value))
    supplied = unsigned.pop("report_sha256", None)
    if supplied != canonical_sha256(unsigned):
        raise OfflineScienceError("offline_metrics_sha256_mismatch")
    return True


def verify_offline_metrics_report(
    value: Any,
    *,
    events: Sequence[Mapping[str, Any]],
    outcome_report: Mapping[str, Any],
    expected_as_of: str,
    expected_authority_scope: Mapping[str, Any],
    validation_plan: ValidationPlan | None = None,
    validation_plan_provenance: Mapping[str, Any] | None = None,
    validation_plan_provenance_verifier: ValidationPlanProvenanceVerifier | None = None,
    market_truth_verifier: OutcomeMarketTruthVerifier | None = None,
    bootstrap_iterations: int = 1000,
) -> bool:
    """Rebuild metrics from exact immutable sources; self-hash is insufficient."""

    _verify_offline_metrics_structure(value)
    expected = recompute_offline_metrics(
        events=events,
        outcome_report=outcome_report,
        expected_as_of=expected_as_of,
        expected_authority_scope=expected_authority_scope,
        validation_plan=validation_plan,
        validation_plan_provenance=validation_plan_provenance,
        validation_plan_provenance_verifier=validation_plan_provenance_verifier,
        market_truth_verifier=market_truth_verifier,
        bootstrap_iterations=bootstrap_iterations,
    )
    if dict(value) != expected:
        raise OfflineScienceError("offline_metrics_do_not_match_exact_sources")
    return True


__all__ = [
    "OFFLINE_METRICS_SCHEMA_VERSION",
    "OfflineScienceError",
    "recompute_offline_metrics",
    "verify_offline_metrics_report",
]
