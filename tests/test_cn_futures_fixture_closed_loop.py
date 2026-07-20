from __future__ import annotations

import copy

import pytest

from CNFutures.fixture_closed_loop import FixtureContractError, run_fixture_closed_loop


def _fixture(**overrides: object) -> dict[str, object]:
    fixture: dict[str, object] = {
        "fixture_only": True,
        "real_trading_enabled": False,
        "generation": 7,
        "side": "long",
        "quantity": 2,
        "stop_distance": 10.0,
        "maximum_loss_cny": 5_000.0,
        "raw_heuristic_score": 0.7,
        "uncalibrated_prior": 0.1,
        "data_evidence": {
            "source_kind": "fixture_mock",
            "catalog_state": "ready",
            "query_state": "ready",
            "freshness": "fresh",
            "quality": "valid",
            "lineage_ref": "fixture-lineage-001",
        },
        "contract": {
            "symbol": "rb2610.SHF",
            "active_symbol": "rb2610.SHF",
            "product": "rb",
            "multiplier": 10.0,
            "tick_size": 1.0,
            "initial_margin_rate": 0.13,
            "maintenance_margin_rate": 0.10,
            "open_fee_rate": 0.0001,
            "close_fee_rate": 0.0001,
            "night_session": True,
        },
        "bar": {"timestamp": "2026-07-20T21:05:00+08:00", "price": 3500.2},
        "mark": {"price": 3510.0},
        "close": {"price": 3512.4},
    }
    fixture.update(overrides)
    return fixture


def test_fixture_long_round_trip_binds_night_trade_date_tick_margin_and_reconcile() -> (
    None
):
    result = run_fixture_closed_loop(_fixture())

    assert result["mode"] == "fixture_mock_only"
    assert result["trade_date"] == "20260721"
    assert result["session"] == "night"
    assert result["candidate"]["execution_eligible"] is True
    assert result["execution"]["orders"][0]["price"] == 3501.0
    assert result["execution"]["orders"][0]["margin_cny"] == 9102.6
    assert result["execution"]["orders"][1]["price"] == 3512.0
    assert result["execution"]["realized_pnl_cny"] == 220.0
    assert result["execution"]["daily_mtm"]["unrealized_pnl_cny"] == 180.0
    assert result["data_evidence"]["lineage_ref"] == "fixture-lineage-001"
    assert result["daily_reconcile"]["generation"] == 7
    assert result["daily_reconcile"]["margin_cny"] == 0.0
    assert result["daily_reconcile"]["open_position_quantity"] == 0
    assert result["sample_review"]["sample_class"] == "completed_round_trip"


def test_short_uses_sell_down_buy_up_tick_rounding_and_realizes_profit() -> None:
    fixture = _fixture(side="short", mark={"price": 3490.0}, close={"price": 3487.2})
    result = run_fixture_closed_loop(fixture)

    assert result["execution"]["orders"][0]["price"] == 3500.0
    assert result["execution"]["orders"][1]["price"] == 3488.0
    assert result["execution"]["realized_pnl_cny"] == 240.0


def test_rollover_mismatch_is_counterfactual_without_orders() -> None:
    fixture = _fixture(
        contract={**_fixture()["contract"], "active_symbol": "rb2701.SHF"}
    )
    result = run_fixture_closed_loop(fixture)

    assert result["candidate"]["reason"] == "rollover_guard_active_contract_mismatch"
    assert result["candidate"]["counterfactual_only"] is True
    assert result["execution"]["orders"] == []
    assert result["daily_reconcile"]["cash_cny"] == 50_000.0


def test_unaffordable_one_lot_is_counterfactual_but_keeps_candidate() -> None:
    fixture = _fixture(maximum_loss_cny=50.0)
    result = run_fixture_closed_loop(fixture)

    assert result["candidate"]["reason"] == "one_lot_margin_or_stop_budget_ineligible"
    assert result["sample_review"]["counterfactual_only"] is True


def test_maintenance_margin_breach_forces_close_and_preserves_simulated_marker() -> (
    None
):
    fixture = _fixture(mark={"price": 100.0})
    result = run_fixture_closed_loop(fixture)

    assert result["execution"]["liquidation_risk_triggered"] is True
    assert result["execution"]["orders"][1]["position_effect"] == "forced_liquidation"
    assert result["execution"]["simulation_only"] is True
    assert result["real_trading_enabled"] is False


@pytest.mark.parametrize(
    "path, value",
    [
        (("fixture_only",), False),
        (("real_trading_enabled",), True),
        (("network_enabled",), True),
        (("data_evidence", "source_kind"), "http"),
        (("data_evidence", "freshness"), "stale"),
    ],
)
def test_fixture_evidence_and_live_markers_fail_closed(
    path: tuple[str, ...], value: object
) -> None:
    fixture = copy.deepcopy(_fixture())
    target: dict[str, object] = fixture
    for key in path[:-1]:
        target = target[key]  # type: ignore[assignment,index]
    target[path[-1]] = value

    with pytest.raises(FixtureContractError):
        run_fixture_closed_loop(fixture)


def test_weekend_day_bar_is_hold_not_an_order() -> None:
    result = run_fixture_closed_loop(
        _fixture(bar={"timestamp": "2026-07-19T10:00:00+08:00", "price": 3500.0})
    )

    assert result["session"] == "closed"
    assert result["candidate"]["reason"] == "outside_contract_session"
