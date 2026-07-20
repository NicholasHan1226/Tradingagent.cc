from __future__ import annotations

import ast
from datetime import date, timedelta
import inspect

import pytest

from Ashare.twenty_day_fixture_loop import (
    FixtureDay,
    FixtureEvidence,
    run_fixture_twenty_day_loop,
)
import Ashare.twenty_day_fixture_loop as fixture_loop


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
    valid_first_rows = (
        [{"suspended": False, **row} for row in first_rows] if first_rows else []
    )
    return [
        FixtureDay(
            trade_date=trade_date,
            instruments=valid_first_rows if index == 0 else [],
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
    receipt = result["days"][0]["simulated_receipt"]
    assert result["day_count"] == 20
    assert receipt["quantity"] == 300
    assert receipt["reference_price"] == 20
    assert receipt["fill_price"] == 20.01
    assert receipt["slippage_bps_per_side"] == 5.0
    assert receipt["total_cost_cny"] > receipt["quantity"] * receipt["reference_price"]
    assert receipt["capital_authority_id"] == "ashare-capital-v1"
    assert receipt["status"] == "simulated_filled"


@pytest.mark.parametrize(
    ("evidence", "reason"),
    [
        (_evidence(degraded="false"), "evidence_degraded"),
        (_evidence(state="failed"), "evidence_state_invalid"),
        (_evidence(freshness="stale"), "evidence_stale"),
        (_evidence(lineage_id=""), "evidence_lineage_missing"),
        (_evidence(lineage_id=" "), "evidence_lineage_missing"),
        (_evidence(receipt_id=""), "evidence_receipt_missing"),
        (_evidence(receipt_id=" "), "evidence_receipt_missing"),
        (_evidence(calendar_lineage_id=" "), "calendar_lineage_missing"),
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
        (
            _evidence(decision_time="2026-07-01T23:59:00+08:00"),
            "decision_time_outside_fixture_session",
        ),
        (
            _evidence(decision_time="2026-07-01T12:00:00+08:00"),
            "decision_time_outside_fixture_session",
        ),
        (_evidence(session="unknown"), "fixture_session_invalid"),
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
    first = result["days"][0]
    assert first["simulated_receipt"] is None
    assert first["universe"] == {"tradable_mainboard": [], "context_only": []}
    assert first["sample_review"] == [
        {
            "sample_type": "data_reject",
            "trade_date": "20260701",
            "symbol": None,
            "execution_eligible": False,
            "training_eligible": False,
            "fixture_simulation_eligible": False,
            "simulated_fill_observed": False,
            "reason_code": reason,
        }
    ]


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
        "non_authoritative",
        "durable",
        "capital_commit_id",
        "outbox_id",
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
    assert result["days"][0]["simulated_receipt"] is None


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
        [
            {
                "symbol": "600001.SH",
                "price": 10,
                "volume": 1,
                "rank_score": 1,
                "suspended": False,
            }
        ],
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
                "suspended": False,
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
    assert blocked["simulated_receipt"] is None
    assert blocked["reconcile"]["status"] == "fixture_blocked"


def test_real_mode_and_non_twenty_days_are_rejected() -> None:
    with pytest.raises(RuntimeError, match="REAL_TRADING_ENABLED=false"):
        run_fixture_twenty_day_loop(_days(), real_trading_enabled=True)
    with pytest.raises(ValueError, match="fixture_twenty_trading_days_required"):
        run_fixture_twenty_day_loop(_days()[:19])


def _mappings(value: object) -> list[dict]:
    if isinstance(value, dict):
        return [value, *[mapping for item in value.values() for mapping in _mappings(item)]]
    if isinstance(value, list):
        return [mapping for item in value for mapping in _mappings(item)]
    return []


def _assert_no_duplicate_literal_keys(node: ast.AST) -> None:
    for child in ast.walk(node):
        if not isinstance(child, ast.Dict):
            continue
        keys = [
            key.value
            for key in child.keys
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        ]
        assert len(keys) == len(set(keys)), f"duplicate literal keys: {keys}"


def test_fixture_result_is_non_authoritative_with_stable_result_shapes() -> None:
    _assert_no_duplicate_literal_keys(
        ast.parse(inspect.getsource(fixture_loop.run_fixture_twenty_day_loop))
    )
    result = run_fixture_twenty_day_loop(
        _days(
            first_rows=[
                {"symbol": "600000.SH", "price": 20, "volume": 1, "rank_score": 1}
            ],
            first_marks={"600000.SH": 20},
        )
    )
    assert set(result) == {
        "contract_id",
        "fixture_only",
        "non_authoritative",
        "execution_authority",
        "durable",
        "capital_commit_id",
        "outbox_id",
        "calendar_authoritative",
        "real_session_verified",
        "training_eligible",
        "promotion_eligible",
        "account_id",
        "capital_layer",
        "real_trading_enabled",
        "day_count",
        "days",
        "sample_journal",
        "automatic_promotion_enabled",
        "automatic_risk_expansion_enabled",
    }
    receipt = result["days"][0]["simulated_receipt"]
    assert receipt is not None
    assert receipt["status"] == "simulated_filled"
    assert all(
        mapping.get("status") != "filled" and mapping.get("execution_eligible") is not True
        for mapping in _mappings(result)
    )
    assert all(
        mapping[key] is None
        for mapping in _mappings(result)
        for key in ("capital_commit_id", "outbox_id")
        if key in mapping
    )
    assert result["calendar_authoritative"] is False
    assert result["real_session_verified"] is False
    assert result["training_eligible"] is False
    assert result["promotion_eligible"] is False
    first = result["days"][0]
    assert first["reconcile"]["status"] == "fixture_reconciled"
    assert set(first["evidence"]) == {
        "fixture_only",
        "catalog_route",
        "query_route",
        "state",
        "degraded",
        "freshness",
        "quality",
        "lineage_id",
        "receipt_id",
        "calendar_eligible",
        "calendar_lineage_id",
        "available_at",
        "decision_time",
        "session",
        "calendar_authoritative",
        "real_session_verified",
    }


@pytest.mark.parametrize("suspended", [None, "false"])
def test_missing_or_untyped_suspension_fails_closed(suspended: object) -> None:
    days = _days()
    days[0] = FixtureDay(
        days[0].trade_date,
        [
            {
                "symbol": "600000.SH",
                "price": 20,
                "volume": 1,
                "rank_score": 1,
                "suspended": suspended,
            }
        ],
        _evidence(days[0].trade_date),
        {"600000.SH": 20},
    )
    result = run_fixture_twenty_day_loop(days)
    assert result["days"][0]["reason_code"] == "instrument_suspended"
    assert result["days"][0]["simulated_receipt"] is None


def test_missing_suspension_field_fails_closed() -> None:
    days = _days()
    days[0] = FixtureDay(
        days[0].trade_date,
        [{"symbol": "600000.SH", "price": 20, "volume": 1, "rank_score": 1}],
        _evidence(days[0].trade_date),
        {"600000.SH": 20},
    )
    result = run_fixture_twenty_day_loop(days)
    assert result["days"][0]["reason_code"] == "instrument_suspended"


def test_buy_risk_uses_current_mark_not_only_fill_price() -> None:
    result = run_fixture_twenty_day_loop(
        _days(
            first_rows=[
                {"symbol": "600000.SH", "price": 10, "volume": 1, "rank_score": 1}
            ],
            first_marks={"600000.SH": 100},
        )
    )
    first = result["days"][0]
    assert first["reason_code"] == "lot_or_single_name_cap_not_feasible"
    assert first["simulated_receipt"] is None
    assert first["reconcile"]["gross_exposure_cny"] == 0.0


def test_zero_tick_rounded_sell_is_rejected_without_cash_mutation() -> None:
    days = _days(
        first_rows=[
            {"symbol": "600000.SH", "price": 20, "volume": 1, "rank_score": 1}
        ],
        first_marks={"600000.SH": 20},
    )
    days[1] = FixtureDay(
        days[1].trade_date,
        [
            {
                "symbol": "600000.SH",
                "price": 0.01,
                "volume": 1,
                "rank_score": 1,
                "signal": "sell",
                "suspended": False,
            }
        ],
        _evidence(days[1].trade_date),
        {"600000.SH": 20},
    )
    result = run_fixture_twenty_day_loop(days)
    first, second = result["days"][:2]
    assert second["reason_code"] == "invalid_fill_price"
    assert second["simulated_receipt"] is None
    assert second["reconcile"]["cash_cny"] == first["reconcile"]["cash_cny"]
