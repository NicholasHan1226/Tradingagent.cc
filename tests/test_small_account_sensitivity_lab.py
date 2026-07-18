from __future__ import annotations

from copy import deepcopy

import pytest

from shared.review.outcome_evaluation import canonical_sha256
from shared.review.small_account_sensitivity import (
    SmallAccountSensitivityError,
    build_small_account_sensitivity_report,
    verify_small_account_sensitivity_report,
)


SENSITIVITY_GRIDS = {
    "max_positions": [3, 5, 7, 8],
    "minimum_economic_order_cny": [1_000.0, 2_000.0, 3_000.0],
    "no_trade_band_cny": [500.0, 1_000.0, 1_500.0],
    "cost_stress_multiplier": [1.0, 1.5, 2.0],
}


def manifest_sha256(observations: list[dict[str, object]]) -> str:
    return canonical_sha256(
        {
            "manifest_schema_version": ("ashare-small-account-sensitivity-manifest.v1"),
            "hard_policy_axes": {
                "initial_equity_cny": 50_000.0,
                "single_name_max_pct": 0.15,
                "gross_limit_pct": 0.90,
                "lot_size": 100,
            },
            "sensitivity_grids": SENSITIVITY_GRIDS,
            "scenarios": sorted(
                [deepcopy(row["scenario"]) for row in observations],
                key=lambda row: row["scenario_id"],
            ),
        }
    )


def rehash_plan(row: dict[str, object]) -> None:
    plan = row["plan"]
    unsigned = deepcopy(plan)
    unsigned.pop("plan_sha256", None)
    plan["plan_sha256"] = canonical_sha256(unsigned)


def observation(
    scenario_id: str,
    *,
    max_positions: int,
    minimum_order: float,
    no_trade_band: float,
    cost_stress: float,
) -> dict[str, object]:
    row = {
        "scenario": {
            "scenario_id": scenario_id,
            "initial_equity_cny": 50_000.0,
            "single_name_max_pct": 0.15,
            "gross_limit_pct": 0.90,
            "lot_size": 100,
            "max_positions": max_positions,
            "minimum_economic_order_cny": minimum_order,
            "no_trade_band_cny": no_trade_band,
            "cost_stress_multiplier": cost_stress,
        },
        "plan": {
            "plan_sha256": "",
            "policy_id": f"policy-{scenario_id}",
            "execution_scope": "simulated_research_only",
            "max_positions": max_positions,
            "target_gross_cny": 35_000.0,
            "cash_after_orders_cny": 15_000.0,
            "estimated_order_costs_cny": 25.0 * cost_stress,
            "estimated_adverse_fill_loss_cny": 30.0 * cost_stress,
            "undeployed_cash_cny": 15_000.0,
            "undeployed_reason_codes": ["cash_reserve"],
            "decisions": [
                {"symbol": "600000.SH", "order_shares": 100},
                {"symbol": "600001.SH", "order_shares": 0},
            ],
        },
    }
    rehash_plan(row)
    return row


def complete_observations() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for max_positions in SENSITIVITY_GRIDS["max_positions"]:
        for minimum_order in SENSITIVITY_GRIDS["minimum_economic_order_cny"]:
            for no_trade_band in SENSITIVITY_GRIDS["no_trade_band_cny"]:
                if minimum_order < no_trade_band:
                    continue
                for cost_stress in SENSITIVITY_GRIDS["cost_stress_multiplier"]:
                    scenario_id = (
                        f"mp{max_positions}-mo{int(minimum_order)}-"
                        f"nt{int(no_trade_band)}-cs{int(cost_stress * 10)}"
                    )
                    rows.append(
                        observation(
                            scenario_id,
                            max_positions=max_positions,
                            minimum_order=minimum_order,
                            no_trade_band=no_trade_band,
                            cost_stress=cost_stress,
                        )
                    )
    return rows


def test_sensitivity_report_preserves_canonical_hard_policy_and_never_picks_winner() -> (
    None
):
    observations = complete_observations()
    report = build_small_account_sensitivity_report(
        observations,
        scenario_manifest_sha256=manifest_sha256(observations),
    )

    assert verify_small_account_sensitivity_report(report) is True
    assert report["scenario_count"] == 96
    assert report["prespecified_scenario_count"] == 96
    assert report["prespecified_grid_complete"] is True
    assert report["winner_selection_allowed"] is False
    assert report["sensitivity_grids"] == SENSITIVITY_GRIDS
    assert report["allowed_sensitivity_axes"] == [
        "cost_stress_multiplier",
        "max_positions",
        "minimum_economic_order_cny",
        "no_trade_band_cny",
    ]
    assert "best_policy" not in repr(report)
    stress = next(
        row
        for row in report["scenarios"]
        if row["max_positions"] == 5
        and row["minimum_economic_order_cny"] == 3_000.0
        and row["no_trade_band_cny"] == 1_500.0
        and row["cost_stress_multiplier"] == 2.0
    )
    assert stress["estimated_order_costs_cny"] == 50.0
    assert stress["canonical_policy_fully_verified"] is False
    assert stress["constraint_verification"] == {
        "decision_notional_reconciliation": ("unavailable_missing_price_or_notional"),
        "minimum_economic_order": "unavailable_missing_order_notional",
        "no_trade_band": "unavailable_missing_current_and_target_notional",
        "nonzero_position_count": "verified",
        "order_lot_size": "verified",
        "plan_hash": "verified",
        "reported_target_gross_limit": "verified_from_reported_field_only",
        "single_name_max_pct": "unavailable_missing_position_notional",
    }
    assert report["authority"]["position_effect"] == "none"


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("initial_equity_cny", 60_000.0, "initial_equity_must_equal_50000"),
        ("single_name_max_pct", 0.20, "single_name_limit_must_equal_15pct"),
        ("gross_limit_pct", 1.0, "gross_limit_must_equal_90pct"),
        ("lot_size", 10, "lot_size_must_equal_100"),
    ],
)
def test_hard_policy_axes_cannot_be_swept(
    field: str, value: object, reason: str
) -> None:
    row = observation(
        "base",
        max_positions=8,
        minimum_order=2_000.0,
        no_trade_band=1_000.0,
        cost_stress=1.0,
    )
    row["scenario"][field] = value
    with pytest.raises(SmallAccountSensitivityError, match=reason):
        build_small_account_sensitivity_report(
            [row], scenario_manifest_sha256=manifest_sha256([row])
        )


def test_unregistered_sensitivity_axis_is_rejected() -> None:
    row = observation(
        "base",
        max_positions=8,
        minimum_order=2_000.0,
        no_trade_band=1_000.0,
        cost_stress=1.0,
    )
    row["scenario"]["single_name_override"] = 0.10
    with pytest.raises(SmallAccountSensitivityError, match="scenario_axes_invalid"):
        build_small_account_sensitivity_report(
            [row], scenario_manifest_sha256=manifest_sha256([row])
        )


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("max_positions", 4, "max_positions_outside_prespecified_grid"),
        (
            "minimum_economic_order_cny",
            2_500.0,
            "minimum_economic_order_outside_prespecified_grid",
        ),
        (
            "no_trade_band_cny",
            750.0,
            "no_trade_band_outside_prespecified_grid",
        ),
        (
            "cost_stress_multiplier",
            1.25,
            "cost_stress_outside_prespecified_grid",
        ),
    ],
)
def test_sensitivity_axes_are_limited_to_the_prespecified_exact_grids(
    field: str,
    value: object,
    reason: str,
) -> None:
    row = observation(
        "base",
        max_positions=8,
        minimum_order=2_000.0,
        no_trade_band=1_000.0,
        cost_stress=1.0,
    )
    row["scenario"][field] = value
    if field == "max_positions":
        row["plan"]["max_positions"] = value
        rehash_plan(row)
    with pytest.raises(SmallAccountSensitivityError, match=reason):
        build_small_account_sensitivity_report(
            [row], scenario_manifest_sha256=manifest_sha256([row])
        )


def test_manifest_and_plan_hashes_are_recomputed_not_trusted() -> None:
    observations = complete_observations()
    with pytest.raises(SmallAccountSensitivityError, match="manifest_sha256_mismatch"):
        build_small_account_sensitivity_report(
            observations,
            scenario_manifest_sha256="c" * 64,
        )

    row = observation(
        "base",
        max_positions=8,
        minimum_order=2_000.0,
        no_trade_band=1_000.0,
        cost_stress=1.0,
    )
    row["plan"]["cash_after_orders_cny"] = 14_999.0
    with pytest.raises(SmallAccountSensitivityError, match="plan_sha256_mismatch"):
        build_small_account_sensitivity_report(
            [row], scenario_manifest_sha256=manifest_sha256([row])
        )


def test_duplicate_symbols_and_too_many_nonzero_positions_are_rejected() -> None:
    duplicate = observation(
        "duplicate",
        max_positions=3,
        minimum_order=2_000.0,
        no_trade_band=1_000.0,
        cost_stress=1.0,
    )
    duplicate["plan"]["decisions"].append({"symbol": "600000.sh", "order_shares": 100})
    rehash_plan(duplicate)
    with pytest.raises(SmallAccountSensitivityError, match="duplicate_decision_symbol"):
        build_small_account_sensitivity_report(
            [duplicate], scenario_manifest_sha256=manifest_sha256([duplicate])
        )

    crowded = observation(
        "crowded",
        max_positions=3,
        minimum_order=2_000.0,
        no_trade_band=1_000.0,
        cost_stress=1.0,
    )
    crowded["plan"]["decisions"] = [
        {"symbol": f"60000{index}.SH", "order_shares": 100} for index in range(4)
    ]
    rehash_plan(crowded)
    with pytest.raises(
        SmallAccountSensitivityError,
        match="nonzero_positions_exceed_scenario_max",
    ):
        build_small_account_sensitivity_report(
            [crowded], scenario_manifest_sha256=manifest_sha256([crowded])
        )


def test_partial_prespecified_grid_is_rejected_before_report_publication() -> None:
    row = observation(
        "only-one-grid-cell",
        max_positions=8,
        minimum_order=2_000.0,
        no_trade_band=1_000.0,
        cost_stress=1.0,
    )

    with pytest.raises(
        SmallAccountSensitivityError,
        match="incomplete_prespecified_scenario_grid",
    ):
        build_small_account_sensitivity_report(
            [row],
            scenario_manifest_sha256=manifest_sha256([row]),
        )


def test_rehashed_top_level_plan_projection_tamper_is_rejected() -> None:
    observations = complete_observations()
    report = build_small_account_sensitivity_report(
        observations,
        scenario_manifest_sha256=manifest_sha256(observations),
    )
    report["scenarios"][0]["estimated_order_costs_cny"] = 999_999.0
    unsigned = deepcopy(report)
    unsigned.pop("report_sha256", None)
    report["report_sha256"] = canonical_sha256(unsigned)

    with pytest.raises(
        SmallAccountSensitivityError,
        match="scenario_plan_projection_mismatch",
    ):
        verify_small_account_sensitivity_report(report)
