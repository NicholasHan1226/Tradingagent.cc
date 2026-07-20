from __future__ import annotations

from datetime import date, timedelta

import pytest

from Ashare.twenty_day_fixture_loop import (
    FixtureDay,
    FixtureEvidence,
    run_fixture_twenty_day_loop,
)


def _evidence(trade_date: str = "20260701", **overrides: object) -> FixtureEvidence:
    date_text = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]}"
    payload: dict[str, object] = {
        "catalog_route": "GET /v1/catalog",
        "query_route": "POST /v1/query",
        "state": "ready",
        "degraded": False,
        "freshness": "fresh",
        "quality": "valid",
        "lineage_id": "fixture-lineage",
        "receipt_id": "fixture-receipt",
        "calendar_eligible": True,
        "calendar_lineage_id": "fixture-calendar-lineage",
        "available_at": f"{date_text}T08:00:00+08:00",
        "decision_time": f"{date_text}T09:30:00+08:00",
    }
    payload.update(overrides)
    return FixtureEvidence(**payload)  # type: ignore[arg-type]


def _dates() -> list[str]:
    current = date(2026, 7, 1)
    result: list[str] = []
    while len(result) < 20:
        if current.weekday() < 5:
            result.append(current.strftime("%Y%m%d"))
        current += timedelta(days=1)
    return result


def _days(
    *, first_rows: list[dict] | None = None, first_marks: dict[str, float] | None = None
) -> list[FixtureDay]:
    return [
        FixtureDay(
            trade_date=trade_date,
            instruments=first_rows if index == 0 and first_rows else [],
            evidence=_evidence(trade_date),
            mark_prices=first_marks if index == 0 and first_marks else {},
        )
        for index, trade_date in enumerate(_dates())
    ]


def test_fixture_loop_records_fill_with_structured_evidence_slippage_and_policy() -> (
    None
):
    result = run_fixture_twenty_day_loop(
        _days(
            first_rows=[
                {"symbol": "600000.SH", "price": 20, "volume": 1, "rank_score": 0.8}
            ],
            first_marks={"600000.SH": 20},
        )
    )
    receipt = result["days"][0]["intent_receipt"]
    assert result["day_count"] == 20
    assert receipt["quantity"] == 300
    assert receipt["reference_price"] == 20
    assert receipt["fill_price"] == 20.01
    assert receipt["slippage_bps_per_side"] == 5.0
    assert receipt["total_cost_cny"] > receipt["quantity"] * receipt["reference_price"]
    assert receipt["capital_authority_id"] == "ashare-capital-v1"


@pytest.mark.parametrize(
    ("evidence", "reason"),
    [
        (_evidence(degraded="false"), "evidence_degraded"),
        (_evidence(state="failed"), "evidence_state_invalid"),
        (_evidence(freshness="stale"), "evidence_stale"),
        (_evidence(lineage_id=""), "evidence_lineage_missing"),
        (_evidence(receipt_id=""), "evidence_receipt_missing"),
        (_evidence(calendar_eligible=False), "calendar_ineligible"),
        (
            _evidence(available_at="2026-07-01T10:00:00+08:00"),
            "evidence_available_after_decision",
        ),
        (
            _evidence(available_at="2026-07-01T08:00:00"),
            "evidence_available_at_invalid",
        ),
        (
            _evidence(decision_time="2026-07-01T09:30:00"),
            "evidence_decision_time_invalid",
        ),
        (
            _evidence(decision_time="2026-07-02T09:30:00+08:00"),
            "decision_time_trade_date_mismatch",
        ),
    ],
)
def test_fixture_evidence_gate_fails_closed_before_candidate(
    evidence: FixtureEvidence, reason: str
) -> None:
    days = _days(
        first_rows=[{"symbol": "600000.SH", "price": 20, "volume": 1, "rank_score": 1}],
        first_marks={"600000.SH": 20},
    )
    days[0] = FixtureDay(
        days[0].trade_date, days[0].instruments, evidence, days[0].mark_prices
    )
    result = run_fixture_twenty_day_loop(days)
    assert result["days"][0]["reason_code"] == reason
    assert result["days"][0]["intent_receipt"] is None


def test_reconcile_has_one_stable_result_shape() -> None:
    result = run_fixture_twenty_day_loop(
        _days(
            first_rows=[
                {"symbol": "600000.SH", "price": 20, "volume": 1, "rank_score": 1}
            ],
            first_marks={"600000.SH": 20},
        )
    )
    assert set(result["days"][0]["reconcile"]) == {
        "account_id",
        "capital_layer",
        "real_trading_enabled",
        "cash_cny",
        "market_value_cny",
        "gross_exposure_cny",
        "realized_pnl_cny",
        "unrealized_pnl_cny",
        "equity_cny",
        "position_count",
        "status",
        "reason_code",
    }


@pytest.mark.parametrize(
    ("row", "reason"),
    [
        (
            {
                "symbol": "600000.SH",
                "price": 20,
                "volume": 1,
                "rank_score": 1,
                "suspended": True,
            },
            "instrument_suspended",
        ),
        (
            {"symbol": "600000.SH", "price": 20, "volume": 0, "rank_score": 1},
            "volume_unavailable",
        ),
        (
            {"symbol": "600000.SH", "price": 0, "volume": 1, "rank_score": 1},
            "invalid_reference_price",
        ),
    ],
)
def test_untradable_fixture_rows_cannot_fill(row: dict, reason: str) -> None:
    result = run_fixture_twenty_day_loop(
        _days(first_rows=[row], first_marks={"600000.SH": 20})
    )
    assert result["days"][0]["reason_code"] == reason
    assert result["days"][0]["intent_receipt"] is None


def test_restricted_individuals_cannot_masquerade_as_context() -> None:
    rows = [
        {"symbol": "300001.SZ", "instrument_type": "index"},
        {"symbol": "301001.SZ", "instrument_type": "benchmark"},
        {"symbol": "688001.SH", "instrument_type": "sector"},
        {"symbol": "689001.SH", "instrument_type": "industry"},
        {"symbol": "830000.BJ", "instrument_type": "index"},
        {"symbol": "600000.SH", "price": 20, "volume": 1, "rank_score": 1},
    ]
    result = run_fixture_twenty_day_loop(
        _days(first_rows=rows, first_marks={"600000.SH": 20})
    )
    first = result["days"][0]
    assert first["universe"]["tradable_mainboard"] == ["600000.SH"]
    assert first["universe"]["context_only"] == []


def test_only_canonical_indices_and_namespaced_aggregates_are_context() -> None:
    rows = [
        {"symbol": "600000.SH", "price": 20, "volume": 1, "rank_score": 1},
        {"symbol": "399006.SZ", "instrument_type": "index"},
        {"symbol": "000688.SH", "instrument_type": "benchmark"},
        {"symbol": "000001.SZ", "instrument_type": "index"},
        {"symbol": "SECTOR:AI", "instrument_type": "sector_aggregate"},
        {"symbol": "INDUSTRY:ROBOT", "instrument_type": "industry_aggregate"},
    ]
    result = run_fixture_twenty_day_loop(
        _days(first_rows=rows, first_marks={"600000.SH": 20})
    )
    assert result["days"][0]["universe"]["context_only"] == [
        "399006.SZ",
        "000688.SH",
        "SECTOR:AI",
        "INDUSTRY:ROBOT",
    ]


def test_invalid_or_weekend_session_evidence_fails_closed() -> None:
    days = _days(
        first_rows=[{"symbol": "600000.SH", "price": 20, "volume": 1, "rank_score": 1}],
        first_marks={"600000.SH": 20},
    )
    days[0] = FixtureDay(
        "bad-date", days[0].instruments, _evidence(), days[0].mark_prices
    )
    days.sort(key=lambda item: item.trade_date)
    result = run_fixture_twenty_day_loop(days)
    assert (
        next(day for day in result["days"] if day["trade_date"] == "bad-date")[
            "reason_code"
        ]
        == "trade_date_invalid"
    )
    weekend = _days(
        first_rows=[{"symbol": "600000.SH", "price": 20, "volume": 1, "rank_score": 1}],
        first_marks={"600000.SH": 20},
    )
    weekend[0] = FixtureDay(
        "20260704", weekend[0].instruments, _evidence(), weekend[0].mark_prices
    )
    weekend.sort(key=lambda item: item.trade_date)
    result = run_fixture_twenty_day_loop(weekend)
    assert (
        next(day for day in result["days"] if day["trade_date"] == "20260704")[
            "reason_code"
        ]
        == "trade_date_not_weekday"
    )


def test_marks_fail_closed_and_use_current_value_for_mtm_and_limits() -> None:
    days = _days(
        first_rows=[{"symbol": "600000.SH", "price": 10, "volume": 1, "rank_score": 1}],
        first_marks={"600000.SH": 10},
    )
    days[1] = FixtureDay(
        days[1].trade_date,
        [{"symbol": "600001.SH", "price": 10, "volume": 1, "rank_score": 1}],
        _evidence(days[1].trade_date),
        {"600000.SH": 70},
    )
    days[2] = FixtureDay(
        days[2].trade_date,
        [
            {
                "symbol": "600000.SH",
                "price": 20,
                "volume": 1,
                "rank_score": 1,
                "signal": "sell",
            }
        ],
        _evidence(days[2].trade_date),
        {"600000.SH": 20},
    )
    result = run_fixture_twenty_day_loop(days)
    assert result["days"][1]["reason_code"] == "single_name_mark_limit_breached"
    assert result["days"][1]["reconcile"]["gross_exposure_cny"] == 49_000
    assert result["days"][2]["reconcile"]["realized_pnl_cny"] == 6_968.79
    missing = _days(
        first_rows=[{"symbol": "600000.SH", "price": 20, "volume": 1, "rank_score": 1}],
        first_marks={"600000.SH": 20},
    )
    missing[1] = FixtureDay(
        missing[1].trade_date,
        [{"symbol": "600001.SH", "price": 20, "volume": 1, "rank_score": 1}],
        _evidence(missing[1].trade_date),
        {},
    )
    blocked = run_fixture_twenty_day_loop(missing)["days"][1]
    assert blocked["intent_receipt"] is None
    assert blocked["reconcile"]["status"] == "blocked"


def test_real_mode_and_non_twenty_days_are_rejected() -> None:
    with pytest.raises(RuntimeError, match="REAL_TRADING_ENABLED=false"):
        run_fixture_twenty_day_loop(_days(), real_trading_enabled=True)
    with pytest.raises(ValueError, match="fixture_twenty_trading_days_required"):
        run_fixture_twenty_day_loop(_days()[:19])
