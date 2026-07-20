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
                    "board": "mainboard",
                    "price": 20,
                    "rank_score": 0.8,
                },
                {
                    "symbol": "300001.SZ",
                    "board": "chinext",
                    "price": 10,
                    "rank_score": 0.99,
                },
                {
                    "symbol": "688001.SH",
                    "board": "star",
                    "price": 10,
                    "rank_score": 0.99,
                },
            ]
        )
    )

    assert result["day_count"] == 20
    first = result["days"][0]
    assert first["universe"]["tradable_mainboard"] == ["600000.SH"]
    assert first["universe"]["context_only"] == ["300001.SZ", "688001.SH"]
    assert first["reason_code"] == "simulated_buy_filled"
    assert first["intent_receipt"]["quantity"] == 300
    assert first["intent_receipt"]["fee_cny"] == 5.06
    assert first["reconcile"]["status"] == "reconciled"
    assert result["automatic_promotion_enabled"] is False
    assert result["automatic_risk_expansion_enabled"] is False


def test_fixture_loop_records_no_trade_reasons_and_never_promotes_context_equities() -> (
    None
):
    days = _days(
        first_rows=[
            {"symbol": "300001.SZ", "board": "chinext", "price": 10, "rank_score": 1.0}
        ]
    )
    days[1] = FixtureDay("20260702", False, "fixture_stale", [])
    days[2] = FixtureDay(
        "20260703",
        True,
        "fixture_complete",
        [{"symbol": "000001.SZ", "board": "mainboard", "price": 10, "rank_score": 0.5}],
    )

    result = run_fixture_twenty_day_loop(days)

    assert result["days"][0]["reason_code"] == "no_eligible_mainboard_candidate"
    assert result["days"][0]["universe"]["tradable_mainboard"] == []
    assert result["days"][1]["reason_code"] == "evidence_ineligible:fixture_stale"
    assert result["days"][2]["reason_code"] == "no_trade_band"
    assert all(
        day["reconcile"]["real_trading_enabled"] is False for day in result["days"]
    )


def test_fixture_loop_enforces_lot_single_name_cap_and_t_plus_one_sell() -> None:
    days = _days(
        first_rows=[
            {
                "symbol": "600000.SH",
                "board": "mainboard",
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
                "board": "mainboard",
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
                "board": "mainboard",
                "price": 100,
                "rank_score": 0.9,
            }
        ],
    )

    result = run_fixture_twenty_day_loop(days)

    assert result["days"][1]["reason_code"] == "simulated_sell_filled"
    assert result["days"][2]["reason_code"] == "lot_or_single_name_cap_not_feasible"


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
