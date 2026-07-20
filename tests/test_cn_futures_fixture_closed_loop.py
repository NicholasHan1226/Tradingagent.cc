from __future__ import annotations

import copy
from datetime import datetime
import json

import pytest

from CNFutures.fixture_closed_loop import (
    FixtureContract,
    FixtureContractError,
    _session_for_contract,
    run_fixture_closed_loop,
)
from shared.capital.market_policy import MarketPolicy


def _fixture(**overrides: object) -> dict[str, object]:
    fixture: dict[str, object] = {
        "fixture_only": True,
        "real_trading_enabled": False,
        "generation": 7,
        "side": "long",
        "quantity": 2,
        "stop_distance": 10.0,
        "maximum_loss_cny": 1_000.0,
        "raw_heuristic_score": 0.7,
        "uncalibrated_prior": 0.1,
        "data_evidence": {
            "source_kind": "fixture_mock",
            "catalog_route": "GET /v1/catalog",
            "query_route": "POST /v1/query",
            "catalog_state": "ready",
            "query_state": "ready",
            "degraded": False,
            "freshness": "fresh",
            "quality": "valid",
            "lineage_ref": "fixture-lineage-001",
            "receipt_id": "fixture-data-receipt-001",
            "available_at": "2026-07-17T20:59:00+08:00",
            "exchange_calendar": {
                "trade_date": "20260720",
                "calendar_eligible": True,
                "session": "night",
                "available_at": "2026-07-17T20:59:00+08:00",
                "calendar_lineage_ref": "fixture-calendar-lineage-001",
                "receipt_id": "fixture-calendar-receipt-001",
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
                "night": [[1260, 1440], [0, 60]],
            },
            "available_at": "2026-07-17T20:59:00+08:00",
        },
        "bar": {
            "timestamp": "2026-07-17T21:05:00+08:00",
            "available_at": "2026-07-17T21:05:01+08:00",
            "decision_time": "2026-07-17T21:05:02+08:00",
            "price": 3500.2,
        },
        "mark": {
            "timestamp": "2026-07-17T21:10:00+08:00",
            "available_at": "2026-07-17T21:10:01+08:00",
            "decision_time": "2026-07-17T21:10:02+08:00",
            "price": 3510.0,
        },
        "close": {
            "timestamp": "2026-07-17T21:15:00+08:00",
            "available_at": "2026-07-17T21:15:01+08:00",
            "decision_time": "2026-07-17T21:15:02+08:00",
            "price": 3512.4,
        },
    }
    fixture.update(overrides)
    calendar = fixture["data_evidence"]["exchange_calendar"]
    if isinstance(calendar, dict):
        calendar.setdefault("calendar_lineage_ref", "fixture-calendar-lineage-001")
        calendar.setdefault("receipt_id", "fixture-calendar-receipt-001")
    for event_name in ("bar", "mark", "close"):
        event = fixture[event_name]
        if isinstance(event, dict):
            event.setdefault(
                "exchange_calendar",
                copy.deepcopy(fixture["data_evidence"]["exchange_calendar"]),
            )
    return fixture


def test_fixture_long_round_trip_binds_night_trade_date_tick_margin_and_reconcile() -> (
    None
):
    result = run_fixture_closed_loop(_fixture())

    assert result["mode"] == "fixture_mock_only"
    assert result["trade_date"] == "20260720"
    assert result["session"] == "night"
    assert result["candidate"]["execution_eligible"] is False
    assert result["candidate"]["fixture_simulation_eligible"] is True
    assert result["candidate"]["intent_id"].startswith("cnf-intent-")
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
    assert result["daily_reconcile"]["fixture_reconciled"] is True
    assert result["daily_reconcile"]["non_authoritative"] is True
    assert (
        result["execution"]["orders"][0]["order_id"]
        != result["execution"]["orders"][1]["order_id"]
    )


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
    assert result["daily_reconcile"]["fixture_reconciled"] is False
    assert result["daily_reconcile"]["risk_state"] == "capital_deficit"
    assert result["daily_reconcile"]["capital_deficit_cny"] > 0
    assert result["sample_review"]["reason"] == "forced_liquidation_capital_deficit"


@pytest.mark.parametrize(
    "path, value",
    [
        (("fixture_only",), False),
        (("real_trading_enabled",), True),
        (("network_enabled",), True),
        (("data_evidence", "source_kind"), "http"),
        (("data_evidence", "catalog_route"), "GET /tushare"),
        (("data_evidence", "query_route"), "POST /provider/futures"),
        (("data_evidence", "degraded"), True),
        (("data_evidence", "freshness"), "stale"),
        (("data_evidence", "query_state"), "failed"),
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


@pytest.mark.parametrize(
    "path",
    [
        ("data_evidence", "lineage_ref"),
        ("data_evidence", "receipt_id"),
        ("bar", "exchange_calendar", "calendar_lineage_ref"),
        ("mark", "exchange_calendar", "receipt_id"),
        ("close", "exchange_calendar", "calendar_lineage_ref"),
    ],
)
def test_blank_data_or_calendar_receipts_fail_closed(path: tuple[str, ...]) -> None:
    fixture = copy.deepcopy(_fixture())
    target: dict[str, object] = fixture
    for key in path[:-1]:
        target = target[key]  # type: ignore[assignment,index]
    target[path[-1]] = "  "

    with pytest.raises(FixtureContractError):
        run_fixture_closed_loop(fixture)


@pytest.mark.parametrize("trade_date", ["20261399", "20260230"])
def test_invalid_calendar_trade_date_fails_closed(trade_date: str) -> None:
    bar = dict(_fixture()["bar"])
    calendar = dict(bar["exchange_calendar"])
    calendar["trade_date"] = trade_date
    bar["exchange_calendar"] = calendar

    with pytest.raises(FixtureContractError):
        run_fixture_closed_loop(_fixture(bar=bar))


def test_tick_rounding_to_zero_fails_closed_for_short_entry_and_long_close() -> None:
    with pytest.raises(FixtureContractError):
        run_fixture_closed_loop(
            _fixture(side="short", bar={**_fixture()["bar"], "price": 0.2})
        )
    with pytest.raises(FixtureContractError):
        run_fixture_closed_loop(
            _fixture(side="long", close={**_fixture()["close"], "price": 0.2})
        )


def test_identical_fixture_replay_is_deterministic_and_side_effect_free() -> None:
    fixture = _fixture()
    original = copy.deepcopy(fixture)

    first = run_fixture_closed_loop(fixture)
    second = run_fixture_closed_loop(fixture)

    assert fixture == original
    assert first == second
    assert first["lineage_sha256"] == second["lineage_sha256"]
    assert first["execution"]["orders"] == second["execution"]["orders"]
    assert first["candidate"]["intent_id"] == second["candidate"]["intent_id"]
    assert (
        first["execution"]["orders"][0]["order_id"]
        == second["execution"]["orders"][0]["order_id"]
    )


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
                "decision_time": "2026-07-19T10:00:00+08:00",
                "price": 3500.0,
            },
            mark={
                "timestamp": "2026-07-19T10:05:00+08:00",
                "available_at": "2026-07-19T10:05:00+08:00",
                "decision_time": "2026-07-19T10:05:00+08:00",
                "price": 3510.0,
            },
            close={
                "timestamp": "2026-07-19T10:10:00+08:00",
                "available_at": "2026-07-19T10:10:00+08:00",
                "decision_time": "2026-07-19T10:10:00+08:00",
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
        ("bar", {"available_at": "2026-07-17T21:04:59+08:00"}),
        ("bar", {"available_at": "2026-07-17T21:05:03+08:00"}),
        ("mark", {"available_at": "2026-07-17T21:11:00+08:00"}),
    ],
)
def test_time_order_and_availability_fail_closed(
    part: str, replacement: dict[str, object]
) -> None:
    fixture = _fixture(**{part: {**_fixture()[part], **replacement}})

    with pytest.raises(FixtureContractError):
        run_fixture_closed_loop(fixture)


def test_publish_delay_after_event_is_valid_before_entry_decision() -> None:
    result = run_fixture_closed_loop(_fixture())

    assert result["bar_timestamp"] == "2026-07-17T21:05:00+08:00"
    assert result["candidate"]["execution_eligible"] is False
    assert result["candidate"]["fixture_simulation_eligible"] is True


def test_saturday_early_night_uses_injected_monday_trade_date() -> None:
    result = run_fixture_closed_loop(
        _fixture(
            bar={
                "timestamp": "2026-07-18T00:30:00+08:00",
                "available_at": "2026-07-18T00:30:00+08:00",
                "decision_time": "2026-07-18T00:30:00+08:00",
                "price": 3500.0,
            },
            mark={
                "timestamp": "2026-07-18T00:35:00+08:00",
                "available_at": "2026-07-18T00:35:00+08:00",
                "decision_time": "2026-07-18T00:35:00+08:00",
                "price": 3510.0,
            },
            close={
                "timestamp": "2026-07-18T00:40:00+08:00",
                "available_at": "2026-07-18T00:40:00+08:00",
                "decision_time": "2026-07-18T00:40:00+08:00",
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
                "decision_time": "2026-07-19T21:05:00+08:00",
                "price": 3500.0,
            },
            mark={
                "timestamp": "2026-07-19T21:10:00+08:00",
                "available_at": "2026-07-19T21:10:00+08:00",
                "decision_time": "2026-07-19T21:10:00+08:00",
                "price": 3510.0,
            },
            close={
                "timestamp": "2026-07-19T21:15:00+08:00",
                "available_at": "2026-07-19T21:15:00+08:00",
                "decision_time": "2026-07-19T21:15:00+08:00",
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


def test_policy_is_the_only_capital_and_daily_risk_authority() -> None:
    policy = MarketPolicy.load("cn_futures")
    result = run_fixture_closed_loop(_fixture())

    assert result["account"]["account_id"] == policy.capital_authority_id
    assert result["daily_reconcile"]["initial_equity_cny"] == policy.initial_equity_cny

    blocked = run_fixture_closed_loop(_fixture(maximum_loss_cny=2_000.0))
    assert (
        blocked["candidate"]["reason"] == "maximum_loss_exceeds_canonical_daily_budget"
    )
    assert blocked["execution"]["orders"] == []


def test_fixed_per_lot_fee_cannot_create_negative_cash_or_fill() -> None:
    contract = dict(_fixture()["contract"])
    contract.update({"open_fee_rate": 60_000.0, "open_fee_type": "fixed_per_lot"})
    result = run_fixture_closed_loop(_fixture(contract=contract))

    assert result["candidate"]["reason"] == "margin_stop_or_fee_pretrade_ineligible"
    assert result["candidate"]["execution_eligible"] is False
    assert result["daily_reconcile"]["cash_cny"] >= 0


@pytest.mark.parametrize("event_name", ["mark", "close"])
def test_followup_event_ineligible_calendar_fails_closed(event_name: str) -> None:
    event = dict(_fixture()[event_name])
    event["exchange_calendar"] = {
        "trade_date": "20260720",
        "calendar_eligible": False,
        "session": "closed",
        "available_at": "2026-07-17T20:59:00+08:00",
    }

    with pytest.raises(FixtureContractError):
        run_fixture_closed_loop(_fixture(**{event_name: event}))


def test_followup_event_session_mismatch_fails_closed() -> None:
    mark = dict(_fixture()["mark"])
    mark["exchange_calendar"] = {
        "trade_date": "20260720",
        "calendar_eligible": True,
        "session": "day_morning",
        "available_at": "2026-07-17T20:59:00+08:00",
    }

    with pytest.raises(FixtureContractError):
        run_fixture_closed_loop(_fixture(mark=mark))


def test_uppercase_contract_is_normalized_and_invalid_month_exchange_fail() -> None:
    upper = run_fixture_closed_loop(
        _fixture(
            contract={
                **_fixture()["contract"],
                "symbol": "RB2610.SHF",
                "active_symbol": "RB2610.SHF",
                "product": "RB",
            }
        )
    )
    assert upper["contract"]["symbol"] == "rb2610.SHF"

    for patch in ({"symbol": "rb2613.SHF"}, {"symbol": "rb2610.DCE"}):
        with pytest.raises(FixtureContractError):
            run_fixture_closed_loop(
                _fixture(contract={**_fixture()["contract"], **patch})
            )


def test_rollover_active_symbol_changes_fixture_lineage() -> None:
    baseline = run_fixture_closed_loop(_fixture())
    rollover = run_fixture_closed_loop(
        _fixture(contract={**_fixture()["contract"], "active_symbol": "rb2701.SHF"})
    )

    assert rollover["candidate"]["fixture_simulation_eligible"] is False
    assert rollover["fixture_lineage_sha256"] != baseline["fixture_lineage_sha256"]


@pytest.mark.parametrize(
    "path, value",
    [
        (("bar", "timestamp"), "2026-07-17T21:05:00"),
        (("mark", "available_at"), "2026-07-17T21:10:01"),
        (("close", "decision_time"), "2026-07-17T21:15:02"),
        (("contract", "available_at"), "2026-07-17T20:59:00"),
        (("bar", "exchange_calendar", "available_at"), "2026-07-17T20:59:00"),
    ],
)
def test_naive_timestamps_fail_closed(path: tuple[str, ...], value: str) -> None:
    fixture = copy.deepcopy(_fixture())
    target: dict[str, object] = fixture
    for key in path[:-1]:
        target = target[key]  # type: ignore[assignment,index]
    target[path[-1]] = value

    with pytest.raises(FixtureContractError):
        run_fixture_closed_loop(fixture)


@pytest.mark.parametrize(
    "available_at",
    ["2026-07-17T20:59:00", "2026-07-17T21:05:03+08:00"],
)
def test_closed_entry_calendar_still_requires_aware_pit_availability(
    available_at: str,
) -> None:
    bar = copy.deepcopy(_fixture()["bar"])
    assert isinstance(bar, dict)
    bar["exchange_calendar"] = {
        "trade_date": "20260720",
        "calendar_eligible": False,
        "session": "closed",
        "available_at": available_at,
        "calendar_lineage_ref": "closed-calendar-lineage",
        "receipt_id": "closed-calendar-receipt",
    }

    with pytest.raises(FixtureContractError):
        run_fixture_closed_loop(_fixture(bar=bar))


def test_session_end_is_exact_to_seconds_and_microseconds() -> None:
    fixture = _fixture()
    fixture.update(
        {
            "bar": {
                **fixture["bar"],
                "timestamp": "2026-07-18T00:58:00+08:00",
                "available_at": "2026-07-18T00:58:01+08:00",
                "decision_time": "2026-07-18T00:58:02+08:00",
            },
            "mark": {
                **fixture["mark"],
                "timestamp": "2026-07-18T00:59:00+08:00",
                "available_at": "2026-07-18T00:59:01+08:00",
                "decision_time": "2026-07-18T00:59:02+08:00",
            },
            "close": {
                **fixture["close"],
                "timestamp": "2026-07-18T01:00:00+08:00",
                "available_at": "2026-07-18T01:00:01+08:00",
                "decision_time": "2026-07-18T01:00:02+08:00",
            },
        }
    )
    assert run_fixture_closed_loop(fixture)["session"] == "night"

    close = dict(fixture["close"])
    close["timestamp"] = "2026-07-18T01:00:00.000001+08:00"
    with pytest.raises(FixtureContractError):
        run_fixture_closed_loop(_fixture(**{**fixture, "close": close}))


@pytest.mark.parametrize(
    ("timestamp", "expected_session"),
    [
        ("2026-07-17T23:59:59.999999+08:00", "night"),
        ("2026-07-18T00:00:00+08:00", "night"),
        ("2026-07-18T01:00:00+08:00", "night"),
        ("2026-07-18T01:00:00.000001+08:00", "closed"),
        ("2026-07-17T11:30:00+08:00", "day_morning"),
        ("2026-07-17T11:30:00.000001+08:00", "closed"),
    ],
)
def test_session_windows_distinguish_midnight_seam_from_true_close(
    timestamp: str, expected_session: str
) -> None:
    contract = FixtureContract.from_mapping(_fixture()["contract"])

    assert (
        _session_for_contract(datetime.fromisoformat(timestamp), contract)
        == expected_session
    )


def test_finite_inputs_with_derived_overflow_fail_closed() -> None:
    close = dict(_fixture()["close"])
    close["price"] = 1e308

    with pytest.raises(FixtureContractError):
        run_fixture_closed_loop(_fixture(close=close))


def test_fixture_output_is_strict_json_serializable() -> None:
    for fixture in (_fixture(), _fixture(maximum_loss_cny=2_000.0)):
        result = run_fixture_closed_loop(fixture)
        assert json.dumps(result, ensure_ascii=False, allow_nan=False)


def test_fixture_output_never_claims_execution_or_durable_capital_authority() -> None:
    result = run_fixture_closed_loop(_fixture())

    def walk(value: object) -> list[dict[str, object]]:
        if isinstance(value, dict):
            return [value, *(item for child in value.values() for item in walk(child))]
        if isinstance(value, list):
            return [item for child in value for item in walk(child)]
        return []

    for mapping in walk(result):
        assert mapping.get("execution_eligible") is not True
        assert mapping.get("status") != "filled"
        assert mapping.get("capital_commit_id") in (None,)
        assert mapping.get("outbox_id") in (None,)
    assert result["execution"]["orders"][0]["status"] == "simulated_filled"
    assert result["execution"]["orders"][0]["execution_authority"] is False
    assert result["execution"]["orders"][0]["durable"] is False


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
