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
            "available_at": "2026-07-17T20:59:00+08:00",
            "exchange_calendar": {
                "trade_date": "20260720",
                "calendar_eligible": True,
                "session": "night",
                "available_at": "2026-07-17T20:59:00+08:00",
            },
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
            "open_fee_type": "rate",
            "close_fee_type": "rate",
            "night_session": True,
            "night_session_end_minute": 60,
            "session_windows": {
                "day_morning": [[540, 690]],
                "day_afternoon": [[780, 900]],
                "night": [[1260, 1439], [0, 60]],
            },
            "available_at": "2026-07-17T20:59:00+08:00",
        },
        "bar": {
            "timestamp": "2026-07-17T21:05:00+08:00",
            "available_at": "2026-07-17T21:05:00+08:00",
            "price": 3500.2,
        },
        "mark": {
            "timestamp": "2026-07-17T21:10:00+08:00",
            "available_at": "2026-07-17T21:10:00+08:00",
            "price": 3510.0,
        },
        "close": {
            "timestamp": "2026-07-17T21:15:00+08:00",
            "available_at": "2026-07-17T21:15:00+08:00",
            "price": 3512.4,
        },
    }
    fixture.update(overrides)
    return fixture


def test_fixture_long_round_trip_binds_night_trade_date_tick_margin_and_reconcile() -> (
    None
):
    result = run_fixture_closed_loop(_fixture())

    assert result["mode"] == "fixture_mock_only"
    assert result["trade_date"] == "20260720"
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
    fixture = _fixture(
        side="short",
        mark={**_fixture()["mark"], "price": 3490.0},
        close={**_fixture()["close"], "price": 3487.2},
    )
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
    fixture = _fixture(mark={**_fixture()["mark"], "price": 100.0})
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
        _fixture(
            data_evidence={
                **_fixture()["data_evidence"],
                "exchange_calendar": {
                    "trade_date": "20260719",
                    "calendar_eligible": False,
                    "session": "closed",
                    "available_at": "2026-07-17T20:59:00+08:00",
                },
            },
            bar={
                "timestamp": "2026-07-19T10:00:00+08:00",
                "available_at": "2026-07-19T10:00:00+08:00",
                "price": 3500.0,
            },
            mark={
                "timestamp": "2026-07-19T10:05:00+08:00",
                "available_at": "2026-07-19T10:05:00+08:00",
                "price": 3510.0,
            },
            close={
                "timestamp": "2026-07-19T10:10:00+08:00",
                "available_at": "2026-07-19T10:10:00+08:00",
                "price": 3512.0,
            },
        )
    )

    assert result["session"] == "closed"
    assert result["candidate"]["reason"] == "outside_contract_session"


@pytest.mark.parametrize(
    "part, replacement",
    [
        ("mark", {"timestamp": None}),
        ("mark", {"timestamp": "2026-07-17T21:05:00+08:00"}),
        ("close", {"timestamp": "2026-07-17T21:09:00+08:00"}),
        ("mark", {"available_at": "2026-07-17T21:11:00+08:00"}),
    ],
)
def test_time_order_and_availability_fail_closed(
    part: str, replacement: dict[str, object]
) -> None:
    fixture = _fixture(**{part: {**_fixture()[part], **replacement}})

    with pytest.raises(FixtureContractError):
        run_fixture_closed_loop(fixture)


def test_saturday_early_night_uses_injected_monday_trade_date() -> None:
    result = run_fixture_closed_loop(
        _fixture(
            bar={
                "timestamp": "2026-07-18T00:30:00+08:00",
                "available_at": "2026-07-18T00:30:00+08:00",
                "price": 3500.0,
            },
            mark={
                "timestamp": "2026-07-18T00:35:00+08:00",
                "available_at": "2026-07-18T00:35:00+08:00",
                "price": 3510.0,
            },
            close={
                "timestamp": "2026-07-18T00:40:00+08:00",
                "available_at": "2026-07-18T00:40:00+08:00",
                "price": 3512.0,
            },
        )
    )

    assert result["trade_date"] == "20260720"
    assert result["session"] == "night"


def test_sunday_night_requires_calendar_and_stays_closed_when_ineligible() -> None:
    result = run_fixture_closed_loop(
        _fixture(
            data_evidence={
                **_fixture()["data_evidence"],
                "exchange_calendar": {
                    "trade_date": "20260720",
                    "calendar_eligible": False,
                    "session": "closed",
                    "available_at": "2026-07-17T20:59:00+08:00",
                },
            },
            bar={
                "timestamp": "2026-07-19T21:05:00+08:00",
                "available_at": "2026-07-19T21:05:00+08:00",
                "price": 3500.0,
            },
            mark={
                "timestamp": "2026-07-19T21:10:00+08:00",
                "available_at": "2026-07-19T21:10:00+08:00",
                "price": 3510.0,
            },
            close={
                "timestamp": "2026-07-19T21:15:00+08:00",
                "available_at": "2026-07-19T21:15:00+08:00",
                "price": 3512.0,
            },
        )
    )

    assert result["session"] == "closed"
    assert result["execution"]["orders"] == []


def test_no_night_session_contract_rejects_night_calendar() -> None:
    contract = dict(_fixture()["contract"])
    contract.update(
        {
            "night_session": False,
            "night_session_end_minute": None,
            "session_windows": {
                "day_morning": [[540, 690]],
                "day_afternoon": [[780, 900]],
            },
        }
    )

    with pytest.raises(FixtureContractError):
        run_fixture_closed_loop(_fixture(contract=contract))


def test_fixed_per_lot_fees_do_not_use_notional_rate_math() -> None:
    contract = dict(_fixture()["contract"])
    contract.update(
        {
            "open_fee_rate": 2.0,
            "close_fee_rate": 3.0,
            "open_fee_type": "fixed_per_lot",
            "close_fee_type": "fixed_per_lot",
        }
    )
    result = run_fixture_closed_loop(_fixture(contract=contract))

    assert result["execution"]["orders"][0]["fee_cny"] == 4.0
    assert result["execution"]["orders"][1]["fee_cny"] == 6.0
    assert result["execution"]["fees_cny"] == 10.0


@pytest.mark.parametrize(
    "contract_patch",
    [
        {"symbol": "rb.SHF"},
        {"symbol": "cu2610.SHF"},
        {"active_symbol": "rb.SHF"},
    ],
)
def test_contract_identity_must_be_concrete_and_match_product(
    contract_patch: dict[str, object],
) -> None:
    with pytest.raises(FixtureContractError):
        run_fixture_closed_loop(
            _fixture(contract={**_fixture()["contract"], **contract_patch})
        )
