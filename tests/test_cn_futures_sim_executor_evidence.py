#!/usr/bin/env python3
"""Regression tests for CNFutures simulated fill-evidence gates."""

from __future__ import annotations

import math

import pytest

from CNFutures.sim_executor import cn_futures_sim_execute


def _bar_order(**overrides: object) -> dict[str, object]:
    order: dict[str, object] = {
        "order_id": "SIM-CNF-EVIDENCE",
        "symbol": "RB2610.SHF",
        "side": "buy",
        "position_effect": "open",
        "quantity": 1,
        "price": 3500.0,
        "previous_close": 3500.0,
        "bar_time": "2026-07-11T09:35:00+08:00",
        "decision_time": "2026-07-11T09:36:00+08:00",
        "trade_date": "20260711",
        "bar_volume": 1000,
    }
    order.update(overrides)
    return order


def _reason(
    order: dict[str, object], *, config: dict[str, object] | None = None
) -> str:
    result = cn_futures_sim_execute(order, config=config)
    assert result.status == "rejected"
    assert result.filled_qty == 0
    return str(result.raw_response["reason"])


@pytest.mark.parametrize("price", [True, 0.0, -1.0, math.nan, math.inf, -math.inf])
def test_executor_rejects_non_finite_or_non_positive_price_as_missing_evidence(
    price: object,
) -> None:
    assert _reason(_bar_order(price=price)) == "missing_fill_evidence"


def test_executor_rejects_invalid_side_as_missing_evidence() -> None:
    assert _reason(_bar_order(side="hold")) == "missing_fill_evidence"


def test_executor_requires_an_explicit_side() -> None:
    order = _bar_order()
    order.pop("side")
    assert _reason(order) == "missing_fill_evidence"


@pytest.mark.parametrize("reference", [True, None, 0.0, -1.0, math.nan, math.inf])
def test_executor_requires_finite_positive_reference_price(reference: object) -> None:
    order = _bar_order(previous_close=reference)
    assert _reason(order) == "missing_fill_evidence"


@pytest.mark.parametrize("bar_time", [None, "", "2026-07-11", "not-a-time"])
def test_executor_requires_parseable_bar_or_quote_timestamp(bar_time: object) -> None:
    assert _reason(_bar_order(bar_time=bar_time)) == "missing_fill_evidence"


@pytest.mark.parametrize(
    "decision_time",
    [None, "", "2026-07-11T09:36:00", "not-a-time"],
)
def test_executor_requires_explicit_aware_decision_time(decision_time: object) -> None:
    assert _reason(_bar_order(decision_time=decision_time)) == "decision_time_required"


@pytest.mark.parametrize("trade_date", [None, "", "202607", "20261340"])
def test_executor_requires_parseable_trade_date(trade_date: object) -> None:
    assert _reason(_bar_order(trade_date=trade_date)) == "trade_date_required"


def test_executor_rejects_evidence_after_decision_time() -> None:
    assert (
        _reason(
            _bar_order(
                bar_time="2026-07-11T09:36:00.000001+08:00",
                decision_time="2026-07-11T09:36:00+08:00",
            )
        )
        == "future_fill_evidence"
    )


def test_executor_rejects_stale_evidence_using_default_ttl() -> None:
    assert (
        _reason(
            _bar_order(
                bar_time="2026-07-11T09:25:59+08:00",
                decision_time="2026-07-11T09:36:00+08:00",
            )
        )
        == "stale_fill_evidence"
    )


def test_executor_honours_a_stricter_explicit_evidence_ttl() -> None:
    assert (
        _reason(
            _bar_order(
                bar_time="2026-07-11T09:35:00+08:00",
                decision_time="2026-07-11T09:36:00+08:00",
            ),
            config={"max_fill_evidence_age_seconds": 30},
        )
        == "stale_fill_evidence"
    )


@pytest.mark.parametrize(
    "max_age", [True, None, 0, -1, math.nan, math.inf, "invalid"]
)
def test_executor_rejects_invalid_evidence_ttl(max_age: object) -> None:
    assert (
        _reason(
            _bar_order(), config={"max_fill_evidence_age_seconds": max_age}
        )
        == "fill_evidence_max_age_invalid"
    )


def test_executor_binds_evidence_to_the_exchange_trade_date() -> None:
    assert _reason(_bar_order(trade_date="20260710")) == "trade_date_mismatch"


def test_executor_rejects_cross_session_trade_date_even_when_quote_is_recent() -> None:
    assert (
        _reason(
            _bar_order(
                bar_time="2026-07-08T20:59:59+08:00",
                decision_time="2026-07-08T21:00:01+08:00",
                trade_date="20260709",
            )
        )
        == "trade_date_mismatch"
    )


def test_executor_supports_night_session_trade_date_binding() -> None:
    result = cn_futures_sim_execute(
        _bar_order(
            bar_time="2026-07-08T21:04:00+08:00",
            decision_time="2026-07-08T21:05:00+08:00",
            trade_date="20260709",
        )
    )

    assert result.status == "filled"
    assert result.raw_response["decision_time"] == "2026-07-08T21:05:00+08:00"
    assert result.raw_response["trade_date"] == "20260709"
    assert result.raw_response["fill_evidence_age_seconds"] == 60.0


def test_executor_rejects_missing_volume_and_same_side_book_depth() -> None:
    assert _reason(_bar_order(bar_volume=0)) == "missing_fill_evidence"


def test_executor_reports_disabled_participation_when_bar_is_only_liquidity_evidence() -> (
    None
):
    assert (
        _reason(_bar_order(), config={"volume_participation": 0.0})
        == "liquidity_participation_disabled"
    )


def test_executor_accepts_positive_bar_volume_and_participation() -> None:
    result = cn_futures_sim_execute(_bar_order(), config={"volume_participation": 0.05})

    assert result.status == "filled"
    assert result.filled_qty == 1
    assert result.raw_response["fill_evidence_type"] == "bar_volume_participation"
    assert result.raw_response["evidence_timestamp"] == "2026-07-11T09:35:00+08:00"


def test_executor_accepts_same_side_book_price_and_quantity_without_bar_volume() -> (
    None
):
    result = cn_futures_sim_execute(
        _bar_order(
            bar_time=None,
            bar_volume=0,
            quote_time="2026-07-11T09:35:01+08:00",
            ask_price=3501.0,
            ask_size=2,
        ),
        config={"volume_participation": 0.0, "slippage_bps": 0.0},
    )

    assert result.status == "filled"
    assert result.avg_price == 3501.0
    assert result.raw_response["fill_evidence_type"] == "order_book_ask"
    assert result.raw_response["evidence_timestamp"] == "2026-07-11T09:35:01+08:00"


def test_executor_binds_book_fill_to_quote_time_not_unrelated_bar_time() -> None:
    assert (
        _reason(
            _bar_order(
                bar_time="2026-07-11T09:35:59+08:00",
                quote_time="2026-07-11T09:20:00+08:00",
                ask_price=3501.0,
                ask_size=2,
            )
        )
        == "stale_fill_evidence"
    )


def test_executor_rejects_opposite_side_book_as_fill_evidence() -> None:
    assert (
        _reason(
            _bar_order(
                side="sell",
                bar_time=None,
                bar_volume=0,
                quote_time="2026-07-11T09:35:01+08:00",
                ask_price=3501.0,
                ask_size=2,
            )
        )
        == "missing_fill_evidence"
    )


@pytest.mark.parametrize("intent", ["open", "reduce_only", "flatten_no_overnight"])
def test_executor_never_fills_any_intent_without_fill_evidence(intent: str) -> None:
    assert (
        _reason(_bar_order(intent=intent, bar_time=None, bar_volume=0))
        == "missing_fill_evidence"
    )


def test_executor_rejection_paths_expose_stable_reasons() -> None:
    expiry = cn_futures_sim_execute(
        _bar_order(
            symbol="RB2607.SHF",
            trade_date="20260703",
            last_trade_date="20260705",
        ),
        config={"rollover_min_days_to_expiry": 5},
    )
    price_limit = cn_futures_sim_execute(_bar_order(price=4000.0))

    assert expiry.status == "rejected"
    assert expiry.raw_response["reason"] == "contract_expiry_guard"
    assert price_limit.status == "rejected"
    assert price_limit.raw_response["reason"] == "price_limit_guard"
