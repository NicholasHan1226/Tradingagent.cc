"""Calibration and paired MarketGraph ablation as read-only science evidence."""

from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from typing import Any, Mapping, Sequence

from shared.models.lifecycle import ValidationPlan
from shared.review.outcome_evaluation import (
    OutcomeEvaluationError,
    OutcomeMarketTruthVerifier,
    ValidationPlanProvenanceVerifier,
    canonical_sha256,
    eligible_unambiguous_outcome_rows,
    outcome_rows_as_sample_records,
    verify_outcome_evaluation_against_source,
)
from shared.review.sample_kpi import build_sample_kpi


CALIBRATION_ABLATION_SCHEMA_VERSION = "ashare-calibration-ablation.v1"
_AUTHORITY = {
    "research_only": True,
    "ranking_effect": "none",
    "capital_effect": "none",
    "position_effect": "none",
    "order_effect": "none",
    "automatic_promotion_enabled": False,
    "automatic_risk_expansion_enabled": False,
    "live_transition_authorized": False,
    "real_trading_enabled": False,
}


class CalibrationAblationError(ValueError):
    """Raised when a calibration or ablation report is not reproducible."""


def _pair_key(row: Mapping[str, Any]) -> tuple[str, ...]:
    return (
        str(row.get("pair_id") or ""),
        str(row.get("base_snapshot_sha256") or ""),
        str(row.get("decision_cluster_id") or ""),
        str(row.get("symbol") or ""),
        str(row.get("style") or ""),
        str(row.get("primary_horizon") or ""),
    )


def _paired_invariants(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "decision_at": row.get("decision_at"),
        "trade_date": row.get("trade_date"),
        "point_in_time_as_of": row.get("point_in_time_as_of"),
        "source_snapshot_sha256": row.get("source_snapshot_sha256"),
        "data_quality_sha256": row.get("data_quality_sha256"),
        "cost_contract_sha256": row.get("cost_contract_sha256"),
    }


def _actual_outcome_identity(row: Mapping[str, Any]) -> dict[str, Any]:
    label = row.get("label") if isinstance(row.get("label"), Mapping) else {}
    return deepcopy(dict(label))


def _ablation(outcome_report: Mapping[str, Any]) -> dict[str, Any]:
    groups: dict[tuple[str, ...], list[Mapping[str, Any]]] = defaultdict(list)
    exclusions: Counter[str] = Counter()
    for row in outcome_report["outcomes"]:
        pair_id = str(row.get("pair_id") or "").strip()
        base_sha = str(row.get("base_snapshot_sha256") or "").strip()
        arm = str(row.get("ablation_group") or "").strip()
        if not pair_id or len(base_sha) != 64 or arm not in {"mg_off", "mg_on"}:
            exclusions["pair_identity_incomplete"] += 1
            continue
        groups[_pair_key(row)].append(row)

    pairs: list[dict[str, Any]] = []
    for key in sorted(groups):
        rows = groups[key]
        by_arm: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for row in rows:
            by_arm[str(row.get("ablation_group"))].append(row)
        if set(by_arm) != {"mg_off", "mg_on"} or any(
            len(values) != 1 for values in by_arm.values()
        ):
            exclusions["pair_incomplete"] += 1
            continue
        off = by_arm["mg_off"][0]
        on = by_arm["mg_on"][0]
        if _paired_invariants(off) != _paired_invariants(on):
            exclusions["paired_invariant_mismatch"] += 1
            continue
        actual_outcome = _actual_outcome_identity(off)
        if actual_outcome != _actual_outcome_identity(on):
            exclusions["paired_actual_outcome_mismatch"] += 1
            continue
        if (
            off.get("eligible_for_statistical_learning") is not True
            or on.get("eligible_for_statistical_learning") is not True
        ):
            exclusions["paired_outcome_not_ready"] += 1
            continue
        off_return = off["label"].get("net_return_after_costs")
        on_return = on["label"].get("net_return_after_costs")
        if not all(
            isinstance(value, (int, float)) and not isinstance(value, bool)
            for value in (off_return, on_return)
        ):
            exclusions["paired_outcome_not_ready"] += 1
            continue
        pairs.append(
            {
                "pair_id": key[0],
                "base_snapshot_sha256": key[1],
                "decision_cluster_id": key[2],
                "symbol": key[3],
                "style": key[4],
                "primary_horizon": key[5],
                "mg_off_outcome_id": off["outcome_id"],
                "mg_on_outcome_id": on["outcome_id"],
                "shared_actual_outcome_sha256": canonical_sha256(actual_outcome),
                "shared_actual_net_return_after_costs": round(float(off_return), 12),
                "shared_invariants": _paired_invariants(off),
                "mg_off_decision": {
                    "decision_id": off.get("decision_id"),
                    "action": off.get("action"),
                    "disposition": off.get("disposition"),
                },
                "mg_on_decision": {
                    "decision_id": on.get("decision_id"),
                    "action": on.get("action"),
                    "disposition": on.get("disposition"),
                },
                "decision_changed": any(
                    off.get(field) != on.get(field)
                    for field in ("decision_id", "action", "disposition")
                ),
                "causal_increment_estimate_available": False,
                "descriptive_pair_only": True,
            }
        )
    return {
        "unit_of_analysis": "prespecified_exact_mg_on_off_pair",
        "eligible_pair_count": len(pairs),
        "incremental_effect_status": (
            "unavailable_shared_realized_outcome_is_not_a_causal_counterfactual"
        ),
        "pairs": pairs,
        "exclusion_reason_counts": dict(sorted(exclusions.items())),
        "winner_selection_permitted": False,
    }


def build_calibration_ablation_report(
    *,
    events: Sequence[Mapping[str, Any]],
    outcome_report: Mapping[str, Any],
    expected_as_of: str,
    expected_authority_scope: Mapping[str, Any],
    validation_plan: ValidationPlan | None = None,
    validation_plan_provenance: Mapping[str, Any] | None = None,
    validation_plan_provenance_verifier: ValidationPlanProvenanceVerifier | None = None,
    market_truth_verifier: OutcomeMarketTruthVerifier | None = None,
) -> dict[str, Any]:
    """Reuse canonical calibration and add leak-resistant paired ablation."""

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
        raise CalibrationAblationError(str(exc)) from exc
    if isinstance(events, (str, bytes, bytearray)):
        raise CalibrationAblationError("events_must_be_sequence")
    copied_events = deepcopy(list(events))
    if canonical_sha256(copied_events) != outcome_report["source_events_sha256"]:
        raise CalibrationAblationError("events_do_not_match_outcome_report")
    science_rows, eligible_cluster_count, ambiguous_cluster_count = (
        eligible_unambiguous_outcome_rows(outcome_report)
    )
    canonical_kpi = build_sample_kpi(outcome_rows_as_sample_records(science_rows))
    report: dict[str, Any] = {
        "record_type": "ashare_calibration_ablation",
        "schema_version": CALIBRATION_ABLATION_SCHEMA_VERSION,
        "source_events_sha256": outcome_report["source_events_sha256"],
        "source_outcome_report_sha256": outcome_report["report_sha256"],
        "as_of": outcome_report["as_of"],
        "calibration": deepcopy(canonical_kpi["calibration_evidence"]),
        "calibration_cohort": {
            "policy": (
                "eligible_outcome_exact_source_and_one_row_per_decision_cluster"
            ),
            "eligible_unique_decision_cluster_count": eligible_cluster_count,
            "eligible_unambiguous_decision_cluster_count": len(science_rows),
            "ambiguous_cluster_count": ambiguous_cluster_count,
        },
        "ablation": _ablation(outcome_report),
        "authority": deepcopy(_AUTHORITY),
    }
    report["report_sha256"] = canonical_sha256(report)
    _verify_calibration_ablation_structure(report)
    return report


def _verify_calibration_ablation_structure(value: Any) -> bool:
    if not isinstance(value, Mapping):
        raise CalibrationAblationError("calibration_ablation_report_invalid")
    if value.get("schema_version") != CALIBRATION_ABLATION_SCHEMA_VERSION:
        raise CalibrationAblationError("calibration_ablation_schema_invalid")
    if value.get("authority") != _AUTHORITY:
        raise CalibrationAblationError("calibration_ablation_authority_invalid")
    calibration = value.get("calibration")
    ablation = value.get("ablation")
    if not isinstance(calibration, Mapping) or not isinstance(ablation, Mapping):
        raise CalibrationAblationError("calibration_ablation_payload_invalid")
    if ablation.get("winner_selection_permitted") is not False:
        raise CalibrationAblationError("ablation_winner_selection_forbidden")
    if ablation.get("incremental_effect_status") != (
        "unavailable_shared_realized_outcome_is_not_a_causal_counterfactual"
    ):
        raise CalibrationAblationError("ablation_increment_claim_invalid")
    pairs = ablation.get("pairs")
    if not isinstance(pairs, list) or ablation.get("eligible_pair_count") != len(pairs):
        raise CalibrationAblationError("ablation_pairs_invalid")
    if any(
        not isinstance(pair, Mapping)
        or pair.get("causal_increment_estimate_available") is not False
        or pair.get("descriptive_pair_only") is not True
        or "net_return_delta_mg_on_minus_off" in pair
        for pair in pairs
    ):
        raise CalibrationAblationError("ablation_pair_increment_claim_invalid")
    unsigned = deepcopy(dict(value))
    supplied = unsigned.pop("report_sha256", None)
    if supplied != canonical_sha256(unsigned):
        raise CalibrationAblationError("calibration_ablation_sha256_mismatch")
    return True


def verify_calibration_ablation_report(
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
) -> bool:
    """Rebuild calibration and ablation from exact immutable sources."""

    _verify_calibration_ablation_structure(value)
    expected = build_calibration_ablation_report(
        events=events,
        outcome_report=outcome_report,
        expected_as_of=expected_as_of,
        expected_authority_scope=expected_authority_scope,
        validation_plan=validation_plan,
        validation_plan_provenance=validation_plan_provenance,
        validation_plan_provenance_verifier=validation_plan_provenance_verifier,
        market_truth_verifier=market_truth_verifier,
    )
    if dict(value) != expected:
        raise CalibrationAblationError(
            "calibration_ablation_does_not_match_exact_sources"
        )
    return True


__all__ = [
    "CALIBRATION_ABLATION_SCHEMA_VERSION",
    "CalibrationAblationError",
    "build_calibration_ablation_report",
    "verify_calibration_ablation_report",
]
