"""Read-only sensitivity projection for the canonical 50k A-share policy."""

from __future__ import annotations

import math
import re
from copy import deepcopy
from typing import Any, Mapping, Sequence

from shared.review.outcome_evaluation import canonical_sha256


SMALL_ACCOUNT_SENSITIVITY_SCHEMA_VERSION = "ashare-small-account-sensitivity.v1"
SMALL_ACCOUNT_SENSITIVITY_MANIFEST_SCHEMA_VERSION = (
    "ashare-small-account-sensitivity-manifest.v1"
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_HARD_POLICY_AXES = {
    "initial_equity_cny": 50_000.0,
    "single_name_max_pct": 0.15,
    "gross_limit_pct": 0.90,
    "lot_size": 100,
}
_SENSITIVITY_GRIDS = {
    "max_positions": [3, 5, 7, 8],
    "minimum_economic_order_cny": [1_000.0, 2_000.0, 3_000.0],
    "no_trade_band_cny": [500.0, 1_000.0, 1_500.0],
    "cost_stress_multiplier": [1.0, 1.5, 2.0],
}
_EXPECTED_SENSITIVITY_COMBINATIONS = frozenset(
    (
        max_positions,
        float(minimum_order),
        float(no_trade_band),
        float(cost_stress),
    )
    for max_positions in _SENSITIVITY_GRIDS["max_positions"]
    for minimum_order in _SENSITIVITY_GRIDS["minimum_economic_order_cny"]
    for no_trade_band in _SENSITIVITY_GRIDS["no_trade_band_cny"]
    for cost_stress in _SENSITIVITY_GRIDS["cost_stress_multiplier"]
    if minimum_order >= no_trade_band
)
_CONSTRAINT_VERIFICATION = {
    "decision_notional_reconciliation": "unavailable_missing_price_or_notional",
    "minimum_economic_order": "unavailable_missing_order_notional",
    "no_trade_band": "unavailable_missing_current_and_target_notional",
    "nonzero_position_count": "verified",
    "order_lot_size": "verified",
    "plan_hash": "verified",
    "reported_target_gross_limit": "verified_from_reported_field_only",
    "single_name_max_pct": "unavailable_missing_position_notional",
}
_SCENARIO_KEYS = {
    "scenario_id",
    "initial_equity_cny",
    "single_name_max_pct",
    "gross_limit_pct",
    "lot_size",
    "max_positions",
    "minimum_economic_order_cny",
    "no_trade_band_cny",
    "cost_stress_multiplier",
}
_PLAN_KEYS = {
    "plan_sha256",
    "policy_id",
    "execution_scope",
    "max_positions",
    "target_gross_cny",
    "cash_after_orders_cny",
    "estimated_order_costs_cny",
    "estimated_adverse_fill_loss_cny",
    "undeployed_cash_cny",
    "undeployed_reason_codes",
    "decisions",
}
_REPORT_SCENARIO_KEYS = _SCENARIO_KEYS | {
    "plan_sha256",
    "policy_id",
    "target_gross_cny",
    "cash_after_orders_cny",
    "estimated_order_costs_cny",
    "estimated_adverse_fill_loss_cny",
    "undeployed_cash_cny",
    "undeployed_reason_codes",
    "plan",
    "decision_count",
    "nonzero_order_count",
    "constraint_verification",
    "canonical_policy_fully_verified",
    "plan_observation_only",
}
_AUTHORITY = {
    "research_only": True,
    "capital_effect": "none",
    "position_effect": "none",
    "order_effect": "none",
    "automatic_promotion_enabled": False,
    "automatic_risk_expansion_enabled": False,
    "live_transition_authorized": False,
    "real_trading_enabled": False,
}


class SmallAccountSensitivityError(ValueError):
    """Raised when a sensitivity observation sweeps a hard policy axis."""


def _finite(value: Any, field: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SmallAccountSensitivityError(f"{field}_invalid")
    number = float(value)
    if not math.isfinite(number) or number < minimum:
        raise SmallAccountSensitivityError(f"{field}_invalid")
    return number


def _scenario(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SmallAccountSensitivityError("scenario_invalid")
    if set(value) != _SCENARIO_KEYS:
        raise SmallAccountSensitivityError("scenario_axes_invalid")
    scenario_id = str(value.get("scenario_id") or "").strip()
    if not scenario_id:
        raise SmallAccountSensitivityError("scenario_id_required")
    initial_equity = _finite(value.get("initial_equity_cny"), "initial_equity_cny")
    if initial_equity != 50_000.0:
        raise SmallAccountSensitivityError("initial_equity_must_equal_50000")
    single_name = _finite(value.get("single_name_max_pct"), "single_name_max_pct")
    if single_name != 0.15:
        raise SmallAccountSensitivityError("single_name_limit_must_equal_15pct")
    gross_limit = _finite(value.get("gross_limit_pct"), "gross_limit_pct")
    if gross_limit != 0.90:
        raise SmallAccountSensitivityError("gross_limit_must_equal_90pct")
    lot_size = value.get("lot_size")
    if lot_size != 100 or isinstance(lot_size, bool):
        raise SmallAccountSensitivityError("lot_size_must_equal_100")
    max_positions = value.get("max_positions")
    if isinstance(max_positions, bool) or not isinstance(max_positions, int):
        raise SmallAccountSensitivityError("max_positions_invalid")
    if max_positions not in _SENSITIVITY_GRIDS["max_positions"]:
        raise SmallAccountSensitivityError("max_positions_outside_prespecified_grid")
    minimum_order = _finite(
        value.get("minimum_economic_order_cny"), "minimum_economic_order_cny"
    )
    no_trade_band = _finite(value.get("no_trade_band_cny"), "no_trade_band_cny")
    if minimum_order not in _SENSITIVITY_GRIDS["minimum_economic_order_cny"]:
        raise SmallAccountSensitivityError(
            "minimum_economic_order_outside_prespecified_grid"
        )
    if no_trade_band not in _SENSITIVITY_GRIDS["no_trade_band_cny"]:
        raise SmallAccountSensitivityError("no_trade_band_outside_prespecified_grid")
    if minimum_order < no_trade_band:
        raise SmallAccountSensitivityError("minimum_order_must_cover_no_trade_band")
    cost_stress = _finite(
        value.get("cost_stress_multiplier"),
        "cost_stress_multiplier",
        minimum=1.0,
    )
    if cost_stress not in _SENSITIVITY_GRIDS["cost_stress_multiplier"]:
        raise SmallAccountSensitivityError("cost_stress_outside_prespecified_grid")
    return {
        "scenario_id": scenario_id,
        "initial_equity_cny": initial_equity,
        "single_name_max_pct": single_name,
        "gross_limit_pct": gross_limit,
        "lot_size": lot_size,
        "max_positions": max_positions,
        "minimum_economic_order_cny": minimum_order,
        "no_trade_band_cny": no_trade_band,
        "cost_stress_multiplier": cost_stress,
    }


def _plan(value: Any, scenario: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SmallAccountSensitivityError("plan_invalid")
    if set(value) != _PLAN_KEYS:
        raise SmallAccountSensitivityError("plan_fields_invalid")
    supplied_plan_sha = str(value.get("plan_sha256") or "").strip().lower()
    if not _SHA256_RE.fullmatch(supplied_plan_sha):
        raise SmallAccountSensitivityError("plan_sha256_invalid")
    policy_id = str(value.get("policy_id") or "").strip()
    if not policy_id:
        raise SmallAccountSensitivityError("policy_id_required")
    if value.get("execution_scope") != "simulated_research_only":
        raise SmallAccountSensitivityError("execution_scope_must_be_simulated")
    if value.get("max_positions") != scenario["max_positions"]:
        raise SmallAccountSensitivityError("plan_max_positions_mismatch")
    target_gross = _finite(value.get("target_gross_cny"), "target_gross_cny")
    max_gross = scenario["initial_equity_cny"] * scenario["gross_limit_pct"]
    if target_gross > max_gross + 1e-9:
        raise SmallAccountSensitivityError("target_gross_exceeds_90pct")
    cash_after = _finite(value.get("cash_after_orders_cny"), "cash_after_orders_cny")
    costs = _finite(value.get("estimated_order_costs_cny"), "estimated_order_costs_cny")
    adverse = _finite(
        value.get("estimated_adverse_fill_loss_cny"),
        "estimated_adverse_fill_loss_cny",
    )
    undeployed = _finite(value.get("undeployed_cash_cny"), "undeployed_cash_cny")
    reason_codes = value.get("undeployed_reason_codes")
    if not isinstance(reason_codes, list) or any(
        not isinstance(reason, str) or not reason.strip() for reason in reason_codes
    ):
        raise SmallAccountSensitivityError("undeployed_reason_codes_invalid")
    decisions = value.get("decisions")
    if not isinstance(decisions, list):
        raise SmallAccountSensitivityError("decisions_invalid")
    copied_decisions: list[dict[str, Any]] = []
    seen_symbols: set[str] = set()
    for decision in decisions:
        if not isinstance(decision, Mapping):
            raise SmallAccountSensitivityError("decision_invalid")
        if set(decision) != {"symbol", "order_shares"}:
            raise SmallAccountSensitivityError("decision_fields_invalid")
        symbol = str(decision.get("symbol") or "").strip().upper()
        shares = decision.get("order_shares")
        if (
            not symbol
            or isinstance(shares, bool)
            or not isinstance(shares, int)
            or shares < 0
            or shares % scenario["lot_size"] != 0
        ):
            raise SmallAccountSensitivityError("decision_order_invalid")
        if symbol in seen_symbols:
            raise SmallAccountSensitivityError("duplicate_decision_symbol")
        seen_symbols.add(symbol)
        copied_decisions.append({"symbol": symbol, "order_shares": shares})
    nonzero_order_count = sum(
        decision["order_shares"] > 0 for decision in copied_decisions
    )
    if nonzero_order_count > scenario["max_positions"]:
        raise SmallAccountSensitivityError("nonzero_positions_exceed_scenario_max")
    normalized_plan = {
        "policy_id": policy_id,
        "execution_scope": "simulated_research_only",
        "max_positions": scenario["max_positions"],
        "target_gross_cny": target_gross,
        "cash_after_orders_cny": cash_after,
        "estimated_order_costs_cny": costs,
        "estimated_adverse_fill_loss_cny": adverse,
        "undeployed_cash_cny": undeployed,
        "undeployed_reason_codes": sorted(set(reason_codes)),
        "decisions": sorted(copied_decisions, key=lambda row: row["symbol"]),
    }
    expected_plan_sha = canonical_sha256(normalized_plan)
    if supplied_plan_sha != expected_plan_sha:
        raise SmallAccountSensitivityError("plan_sha256_mismatch")
    return {"plan_sha256": expected_plan_sha, **normalized_plan}


def _scenario_manifest(scenarios: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "manifest_schema_version": (SMALL_ACCOUNT_SENSITIVITY_MANIFEST_SCHEMA_VERSION),
        "hard_policy_axes": deepcopy(_HARD_POLICY_AXES),
        "sensitivity_grids": deepcopy(_SENSITIVITY_GRIDS),
        "scenarios": sorted(
            [deepcopy(dict(scenario)) for scenario in scenarios],
            key=lambda row: row["scenario_id"],
        ),
    }


def _sensitivity_combination(
    scenario: Mapping[str, Any],
) -> tuple[int, float, float, float]:
    return (
        int(scenario["max_positions"]),
        float(scenario["minimum_economic_order_cny"]),
        float(scenario["no_trade_band_cny"]),
        float(scenario["cost_stress_multiplier"]),
    )


def build_small_account_sensitivity_report(
    observations: Sequence[Mapping[str, Any]],
    *,
    scenario_manifest_sha256: str,
) -> dict[str, Any]:
    """Compare observed plans while forbidding policy selection or mutation."""

    manifest_sha = str(scenario_manifest_sha256 or "").strip().lower()
    if not _SHA256_RE.fullmatch(manifest_sha):
        raise SmallAccountSensitivityError("scenario_manifest_sha256_invalid")
    if isinstance(observations, (str, bytes, bytearray)):
        raise SmallAccountSensitivityError("observations_must_be_sequence")
    rows: list[dict[str, Any]] = []
    normalized_scenarios: list[dict[str, Any]] = []
    seen: set[str] = set()
    for observation in deepcopy(list(observations)):
        if not isinstance(observation, Mapping):
            raise SmallAccountSensitivityError("observation_invalid")
        if set(observation) != {"scenario", "plan"}:
            raise SmallAccountSensitivityError("observation_fields_invalid")
        scenario = _scenario(observation.get("scenario"))
        if scenario["scenario_id"] in seen:
            raise SmallAccountSensitivityError("duplicate_scenario_id")
        seen.add(scenario["scenario_id"])
        normalized_scenarios.append(scenario)
        plan = _plan(observation.get("plan"), scenario)
        nonzero_order_count = sum(
            decision["order_shares"] > 0 for decision in plan["decisions"]
        )
        rows.append(
            {
                **scenario,
                "plan_sha256": plan["plan_sha256"],
                "policy_id": plan["policy_id"],
                "target_gross_cny": plan["target_gross_cny"],
                "cash_after_orders_cny": plan["cash_after_orders_cny"],
                "estimated_order_costs_cny": plan["estimated_order_costs_cny"],
                "estimated_adverse_fill_loss_cny": plan[
                    "estimated_adverse_fill_loss_cny"
                ],
                "undeployed_cash_cny": plan["undeployed_cash_cny"],
                "undeployed_reason_codes": plan["undeployed_reason_codes"],
                "plan": deepcopy(plan),
                "decision_count": len(plan["decisions"]),
                "nonzero_order_count": nonzero_order_count,
                "constraint_verification": deepcopy(_CONSTRAINT_VERIFICATION),
                "canonical_policy_fully_verified": False,
                "plan_observation_only": True,
            }
        )
    rows.sort(key=lambda row: row["scenario_id"])
    observed_combinations = {
        _sensitivity_combination(scenario) for scenario in normalized_scenarios
    }
    if (
        len(normalized_scenarios) != len(_EXPECTED_SENSITIVITY_COMBINATIONS)
        or observed_combinations != _EXPECTED_SENSITIVITY_COMBINATIONS
    ):
        raise SmallAccountSensitivityError("incomplete_prespecified_scenario_grid")
    scenario_manifest = _scenario_manifest(normalized_scenarios)
    expected_manifest_sha = canonical_sha256(scenario_manifest)
    if manifest_sha != expected_manifest_sha:
        raise SmallAccountSensitivityError("manifest_sha256_mismatch")
    report: dict[str, Any] = {
        "record_type": "ashare_small_account_sensitivity",
        "schema_version": SMALL_ACCOUNT_SENSITIVITY_SCHEMA_VERSION,
        "scenario_manifest_sha256": manifest_sha,
        "scenario_manifest": scenario_manifest,
        "scenario_count": len(rows),
        "prespecified_scenario_count": len(_EXPECTED_SENSITIVITY_COMBINATIONS),
        "prespecified_grid_complete": True,
        "scenarios": rows,
        "winner_selection_allowed": False,
        "allowed_sensitivity_axes": [
            "cost_stress_multiplier",
            "max_positions",
            "minimum_economic_order_cny",
            "no_trade_band_cny",
        ],
        "sensitivity_grids": deepcopy(_SENSITIVITY_GRIDS),
        "hard_policy_axes": deepcopy(_HARD_POLICY_AXES),
        "scientific_interpretation": (
            "descriptive_observation_only_missing_price_and_position_notional"
        ),
        "authority": deepcopy(_AUTHORITY),
    }
    report["report_sha256"] = canonical_sha256(report)
    verify_small_account_sensitivity_report(report)
    return report


def verify_small_account_sensitivity_report(value: Any) -> bool:
    if not isinstance(value, Mapping):
        raise SmallAccountSensitivityError("sensitivity_report_invalid")
    if value.get("schema_version") != SMALL_ACCOUNT_SENSITIVITY_SCHEMA_VERSION:
        raise SmallAccountSensitivityError("sensitivity_schema_invalid")
    if value.get("authority") != _AUTHORITY:
        raise SmallAccountSensitivityError("sensitivity_authority_invalid")
    scenarios = value.get("scenarios")
    if not isinstance(scenarios, list) or value.get("scenario_count") != len(scenarios):
        raise SmallAccountSensitivityError("sensitivity_scenarios_invalid")
    if value.get("winner_selection_allowed") is not False:
        raise SmallAccountSensitivityError("sensitivity_selection_forbidden")
    if (
        value.get("prespecified_grid_complete") is not True
        or value.get("prespecified_scenario_count")
        != len(_EXPECTED_SENSITIVITY_COMBINATIONS)
        or value.get("scenario_count") != len(_EXPECTED_SENSITIVITY_COMBINATIONS)
    ):
        raise SmallAccountSensitivityError("incomplete_prespecified_scenario_grid")
    if value.get("allowed_sensitivity_axes") != [
        "cost_stress_multiplier",
        "max_positions",
        "minimum_economic_order_cny",
        "no_trade_band_cny",
    ]:
        raise SmallAccountSensitivityError("sensitivity_axes_invalid")
    if value.get("sensitivity_grids") != _SENSITIVITY_GRIDS:
        raise SmallAccountSensitivityError("sensitivity_grids_invalid")
    if value.get("hard_policy_axes") != _HARD_POLICY_AXES:
        raise SmallAccountSensitivityError("hard_policy_axes_invalid")
    if value.get("scientific_interpretation") != (
        "descriptive_observation_only_missing_price_and_position_notional"
    ):
        raise SmallAccountSensitivityError("scientific_interpretation_invalid")
    manifest = value.get("scenario_manifest")
    if not isinstance(manifest, Mapping):
        raise SmallAccountSensitivityError("scenario_manifest_invalid")
    raw_manifest_scenarios = manifest.get("scenarios")
    if not isinstance(raw_manifest_scenarios, list):
        raise SmallAccountSensitivityError("scenario_manifest_invalid")
    normalized_manifest_scenarios = [_scenario(row) for row in raw_manifest_scenarios]
    expected_manifest = _scenario_manifest(normalized_manifest_scenarios)
    if dict(manifest) != expected_manifest:
        raise SmallAccountSensitivityError("scenario_manifest_invalid")
    expected_manifest_sha = canonical_sha256(expected_manifest)
    if value.get("scenario_manifest_sha256") != expected_manifest_sha:
        raise SmallAccountSensitivityError("scenario_manifest_sha256_mismatch")
    scenario_by_id = {
        scenario["scenario_id"]: scenario for scenario in normalized_manifest_scenarios
    }
    if len(scenario_by_id) != len(normalized_manifest_scenarios):
        raise SmallAccountSensitivityError("duplicate_scenario_id")
    if set(scenario_by_id) != {
        str(row.get("scenario_id") or "")
        for row in scenarios
        if isinstance(row, Mapping)
    }:
        raise SmallAccountSensitivityError("scenario_manifest_report_mismatch")
    manifest_combinations = {
        _sensitivity_combination(scenario) for scenario in normalized_manifest_scenarios
    }
    if (
        len(normalized_manifest_scenarios) != len(_EXPECTED_SENSITIVITY_COMBINATIONS)
        or manifest_combinations != _EXPECTED_SENSITIVITY_COMBINATIONS
    ):
        raise SmallAccountSensitivityError("incomplete_prespecified_scenario_grid")
    for row in scenarios:
        if not isinstance(row, Mapping) or set(row) != _REPORT_SCENARIO_KEYS:
            raise SmallAccountSensitivityError("sensitivity_scenario_invalid")
        scenario = scenario_by_id[str(row.get("scenario_id") or "")]
        for field, expected in scenario.items():
            if row.get(field) != expected:
                raise SmallAccountSensitivityError("scenario_manifest_report_mismatch")
        plan = _plan(row.get("plan"), scenario)
        nonzero_count = sum(
            decision["order_shares"] > 0 for decision in plan["decisions"]
        )
        plan_projection = {
            "plan_sha256": plan["plan_sha256"],
            "policy_id": plan["policy_id"],
            "target_gross_cny": plan["target_gross_cny"],
            "cash_after_orders_cny": plan["cash_after_orders_cny"],
            "estimated_order_costs_cny": plan["estimated_order_costs_cny"],
            "estimated_adverse_fill_loss_cny": plan["estimated_adverse_fill_loss_cny"],
            "undeployed_cash_cny": plan["undeployed_cash_cny"],
            "undeployed_reason_codes": plan["undeployed_reason_codes"],
        }
        if any(
            row.get(field) != expected for field, expected in plan_projection.items()
        ):
            raise SmallAccountSensitivityError("scenario_plan_projection_mismatch")
        if (
            row.get("decision_count") != len(plan["decisions"])
            or row.get("nonzero_order_count") != nonzero_count
            or row.get("constraint_verification") != _CONSTRAINT_VERIFICATION
            or row.get("canonical_policy_fully_verified") is not False
            or row.get("plan_observation_only") is not True
        ):
            raise SmallAccountSensitivityError("sensitivity_plan_projection_invalid")
    forbidden_selection_keys = {
        "best_policy",
        "winner",
        "selected_scenario",
        "recommended_scenario",
    }
    if forbidden_selection_keys.intersection(value):
        raise SmallAccountSensitivityError("sensitivity_selection_forbidden")
    unsigned = deepcopy(dict(value))
    supplied = unsigned.pop("report_sha256", None)
    if supplied != canonical_sha256(unsigned):
        raise SmallAccountSensitivityError("sensitivity_sha256_mismatch")
    return True


__all__ = [
    "SMALL_ACCOUNT_SENSITIVITY_SCHEMA_VERSION",
    "SmallAccountSensitivityError",
    "build_small_account_sensitivity_report",
    "verify_small_account_sensitivity_report",
]
