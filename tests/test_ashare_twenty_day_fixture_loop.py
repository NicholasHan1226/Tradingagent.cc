from __future__ import annotations

import pytest

from Ashare.twenty_day_fixture_loop import FixtureDay, run_fixture_twenty_day_loop


def _days(*, first_rows: list[dict] | None = None) -> list[FixtureDay]:
    return [
        FixtureDay(
            trade_date=f"202607{day:02d}",
            evidence_eligible=True,
            evidence_reason="fixture_complete",
            instruments=(first_rows if day == 1 and first_rows is not None else []),
        )
        for day in range(1, 21)
    ]


def test_fixture_loop_runs_twenty_days_with_mainboard_fill_and_reconcile() -> None:
    result = run_fixture_twenty_day_loop(
        _days(
            first_rows=[
                {
                    "symbol": "600000.SH",
                    "price": 20,
                    "rank_score": 0.8,
                },
            ]
        )
    )

    assert result["day_count"] == 20
    first = result["days"][0]
    assert first["universe"]["tradable_mainboard"] == ["600000.SH"]
    assert first["universe"]["context_only"] == []
    assert first["reason_code"] == "simulated_buy_filled"
    assert first["intent_receipt"]["quantity"] == 300
    assert first["intent_receipt"]["fee_cny"] == 5.06
    assert (
        first["intent_receipt"]["cost_model_version"]
        == "ashare-execution-reality-20260706-v1"
    )
    assert (
        first["intent_receipt"]["commission_schedule_status"]
        == "provisional_pending_broker_contract"
    )
    assert first["reconcile"]["status"] == "reconciled"
    assert result["automatic_promotion_enabled"] is False
    assert result["automatic_risk_expansion_enabled"] is False


def test_fixture_loop_excludes_mislabelled_restricted_individual_equities() -> None:
    days = _days(
        first_rows=[
            {
                "symbol": "300001.SZ",
                "board": "mainboard",
                "price": 10,
                "rank_score": 1.0,
            },
            {
                "symbol": "688001.SH",
                "board": "mainboard",
                "price": 10,
                "rank_score": 1.0,
            },
            {
                "symbol": "830000.BJ",
                "board": "mainboard",
                "price": 10,
                "rank_score": 1.0,
            },
        ]
    )

    result = run_fixture_twenty_day_loop(days)

    assert result["days"][0]["reason_code"] == "no_eligible_mainboard_candidate"
    assert result["days"][0]["universe"]["tradable_mainboard"] == []
    assert result["days"][0]["universe"]["context_only"] == []
    assert result["days"][0]["intent_receipt"] is None


def test_fixture_loop_allows_only_indices_and_aggregates_as_context() -> None:
    result = run_fixture_twenty_day_loop(
        _days(
            first_rows=[
                {"symbol": "600000.SH", "price": 20, "rank_score": 0.8},
                {"symbol": "399006.SZ", "instrument_type": "index", "rank_score": 1.0},
                {"symbol": "000688.SH", "instrument_type": "index", "rank_score": 1.0},
                {
                    "symbol": "SECTOR:AI",
                    "instrument_type": "sector_aggregate",
                    "rank_score": 1.0,
                },
            ]
        )
    )

    first = result["days"][0]
    assert first["universe"]["tradable_mainboard"] == ["600000.SH"]
    assert first["universe"]["context_only"] == ["399006.SZ", "000688.SH", "SECTOR:AI"]
    assert first["intent_receipt"]["symbol"] == "600000.SH"
    assert all(
        day["reconcile"]["real_trading_enabled"] is False for day in result["days"]
    )


def test_fixture_loop_enforces_lot_single_name_cap_and_t_plus_one_sell() -> None:
    days = _days(
        first_rows=[
            {
                "symbol": "600000.SH",
                "price": 20,
                "rank_score": 0.9,
            }
        ]
    )
    days[1] = FixtureDay(
        "20260702",
        True,
        "fixture_complete",
        [
            {
                "symbol": "600000.SH",
                "price": 21,
                "rank_score": 0.9,
                "signal": "sell",
            }
        ],
    )
    days[2] = FixtureDay(
        "20260703",
        True,
        "fixture_complete",
        [
            {
                "symbol": "600001.SH",
                "price": 100,
                "rank_score": 0.9,
            }
        ],
    )

    result = run_fixture_twenty_day_loop(days)

    assert result["days"][1]["reason_code"] == "simulated_sell_filled"
    assert result["days"][2]["reason_code"] == "lot_or_single_name_cap_not_feasible"
    assert result["days"][0]["intent_receipt"]["fee_cny"] == 5.06
    assert result["days"][1]["intent_receipt"]["fee_cny"] == 8.213


def test_fixture_loop_uses_shared_fee_authority_for_sh_and_sz() -> None:
    days = _days(first_rows=[{"symbol": "600000.SH", "price": 20, "rank_score": 0.9}])
    days[1] = FixtureDay(
        "20260702",
        True,
        "fixture_complete",
        [{"symbol": "600000.SH", "price": 20, "rank_score": 0.9, "signal": "sell"}],
    )
    days[2] = FixtureDay(
        "20260703",
        True,
        "fixture_complete",
        [{"symbol": "000001.SZ", "price": 20, "rank_score": 0.9}],
    )

    result = run_fixture_twenty_day_loop(days)

    sh_buy = result["days"][0]["intent_receipt"]
    sz_buy = result["days"][2]["intent_receipt"]
    assert sh_buy["fee_cny"] == 5.06
    assert sz_buy["fee_cny"] == 5.06
    assert (
        sh_buy["commission_schedule_version"] == sz_buy["commission_schedule_version"]
    )


def test_fixture_loop_rejects_real_mode_wrong_routes_and_non_twenty_inputs() -> None:
    with pytest.raises(RuntimeError, match="REAL_TRADING_ENABLED=false"):
        run_fixture_twenty_day_loop(_days(), real_trading_enabled=True)
    bad_route = _days()
    bad_route[0] = FixtureDay(
        "20260701", True, "fixture", [], query_route="GET /tushare"
    )
    with pytest.raises(ValueError, match="tradingdatas_wire_contract_mismatch"):
        run_fixture_twenty_day_loop(bad_route)
    with pytest.raises(ValueError, match="fixture_twenty_trading_days_required"):
        run_fixture_twenty_day_loop(_days()[:19])
