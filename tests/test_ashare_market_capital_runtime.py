from __future__ import annotations

from unittest import mock

from shared.capital import MarketCapitalReservationDecision
from shared.orchestrator import (
    _reserve_ashare_market_order,
    _validate_ashare_market_capital_state,
)


TRADE_DATE = "20260712"
AUTHORITY_ID = "ashare-capital-v1"
EXECUTION_LINEAGE_ID = "ashare-sim-fresh-20260712-v1"
LINEAGE_SHA256 = "a" * 64


def _state(**overrides: object) -> dict[str, object]:
    state: dict[str, object] = {
        "source": "market_capital_ledger",
        "schema_version": "market-capital-snapshot.v2",
        "authority_id": AUTHORITY_ID,
        "authority_generation": 1,
        "account_name": "ashare_sim",
        "market": "ashare",
        "currency": "CNY",
        "initial_equity_cny": 50_000.0,
        "equity_cny": 50_000.0,
        "cash_balance_cny": 50_000.0,
        "positions_market_value_cny": 0.0,
        "frozen_order_cash_cny": 0.0,
        "realized_pnl_cny": 0.0,
        "unrealized_pnl_cny": 0.0,
        "reserved_capital_cny": 0.0,
        "active_reservations_cny": 0.0,
        "available_to_reserve_cny": 45_000.0,
        "stock_gross_exposure_limit_cny": 45_000.0,
        "single_name_cap_cny": 7_500.0,
        "capital_utilization_rate": 0.0,
        "reconciled": True,
        "fresh": True,
        "trade_date": TRADE_DATE,
        "event_id": "MCAP-current",
        "execution_lineage_id": EXECUTION_LINEAGE_ID,
        "daily_mtm_change": 0.0,
        "daily_realized_pnl": 0.0,
        "max_daily_loss": 1_500.0,
        "consecutive_losses": 0,
        "max_consecutive_losses": 3,
        "high_water_equity": 50_000.0,
        "max_drawdown": 3_500.0,
        "real_trading_enabled": False,
    }
    state.update(overrides)
    return state


def _order(**overrides: object) -> dict[str, object]:
    order: dict[str, object] = {
        "idempotency_key": "SIM:ashare:ashare_sim:20260712:000001.SZ:buy",
        "ts_code": "000001.SZ",
        "market": "ashare",
        "side": "buy",
        "quantity": 100,
        "price": 10.0,
        "limit_price": 10.0,
        "trade_date": TRADE_DATE,
        "capital_authority_id": AUTHORITY_ID,
        "authority_generation": 1,
        "execution_lineage_id": EXECUTION_LINEAGE_ID,
        "lineage_started_at": "2026-07-12T00:00:00+08:00",
        "point_in_time_as_of": "2026-07-12T09:31:00+08:00",
        "execution_lineage_sha256": LINEAGE_SHA256,
        "market_snapshot": {
            "last_price": 10.0,
            "upper_limit": 11.0,
        },
        "real_trading_enabled": False,
    }
    order.update(overrides)
    return order


def test_valid_state_is_independent_fresh_50k_authority() -> None:
    validated, reason = _validate_ashare_market_capital_state(_state(), TRADE_DATE)
    assert reason == "approved"
    assert validated is not None
    assert validated["authority_id"] == AUTHORITY_ID
    assert validated["authority_generation"] == 1
    assert validated["stock_gross_exposure_limit_cny"] == 45_000.0
    assert validated["single_name_cap_cny"] == 7_500.0
    assert validated["new_risk_allowed"] is True


def test_bootstrap_or_wrong_authority_never_passes_pretrade() -> None:
    for overrides, expected in (
        ({"reconciled": False}, "not_reconciled"),
        ({"fresh": False}, "not_reconciled"),
        ({"authority_generation": 2}, "generation"),
        ({"market": "cn_futures"}, "market"),
        ({"source": "opening_state_manifest"}, "source"),
        ({"execution_lineage_id": ""}, "lineage"),
    ):
        validated, reason = _validate_ashare_market_capital_state(
            _state(**overrides), TRADE_DATE
        )
        assert validated is None
        assert expected in reason


def test_five_percent_drawdown_derisks_but_seven_percent_halts() -> None:
    tightened, reason = _validate_ashare_market_capital_state(
        _state(
            equity_cny=47_500.0,
            cash_balance_cny=47_500.0,
            high_water_equity=50_000.0,
        ),
        TRADE_DATE,
    )
    assert reason == "approved_drawdown_tightened"
    assert tightened is not None
    assert tightened["risk_multiplier"] == 0.75
    assert tightened["new_risk_allowed"] is True

    halted, reason = _validate_ashare_market_capital_state(
        _state(
            equity_cny=46_500.0,
            cash_balance_cny=46_500.0,
            high_water_equity=50_000.0,
        ),
        TRADE_DATE,
    )
    assert halted is None
    assert reason == "ashare_capital_drawdown_halt"


def test_reservation_passes_full_pit_lineage_to_independent_market_api() -> None:
    captured: list[object] = []

    def reserve(market: str, request: object) -> MarketCapitalReservationDecision:
        captured.append((market, request))
        return MarketCapitalReservationDecision(
            approved=True,
            reason="reserved",
            reservation_id="RES-1",
            event_id="EVT-1",
        )

    order = _order()
    with (
        mock.patch(
            "shared.orchestrator.market_capital.reserve_market_capital",
            side_effect=reserve,
        ),
        mock.patch(
            "shared.orchestrator._capture_ashare_market_capital_head",
            return_value={"event_id": "EVT-1", "checksum": "b" * 64},
        ),
    ):
        result = _reserve_ashare_market_order(order, _state(), "approved")

    assert result["approved"] is True
    assert len(captured) == 1
    market, request = captured[0]
    assert market == "ashare"
    assert request.market == "ashare"
    assert request.authority_id == AUTHORITY_ID
    assert request.authority_generation == 1
    assert request.risk_unit_key == "000001.SZ"
    assert request.point_in_time_as_of == "2026-07-12T09:31:00+08:00"
    assert request.lineage_sha256 == LINEAGE_SHA256
    assert request.execution_lineage_id == EXECUTION_LINEAGE_ID
    assert request.worst_case_amount_cny > 1_100.0
    assert order["market_capital_required"] is True
    assert order["market_capital_reservation_id"] == "RES-1"
    assert order["market_capital_event_id"] == "EVT-1"
    assert order["market_capital_expected_head_event_id"] == "EVT-1"
    assert order["market_capital_expected_head_checksum"] == "b" * 64


def test_missing_order_lineage_fails_before_reservation_call() -> None:
    order = _order(execution_lineage_sha256="")
    with mock.patch(
        "shared.orchestrator.market_capital.reserve_market_capital"
    ) as reserve:
        result = _reserve_ashare_market_order(order, _state(), "approved")
    assert result["approved"] is False
    assert result["reason"] == "ashare_capital_lineage_missing"
    reserve.assert_not_called()
