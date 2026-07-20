from __future__ import annotations

import pytest

from shared.execution import execution_reality
from shared.execution.execution_reality import (
    ASHARE_EXECUTION_REALITY_VERSION,
    ashare_execution_reality,
)


def test_ashare_execution_reality_is_versioned_from_2026_07_06() -> None:
    model = ashare_execution_reality()

    assert model.model_version == ASHARE_EXECUTION_REALITY_VERSION
    assert model.effective_from == "2026-07-06"
    assert model.price_limit_pct(
        symbol="600000.SH", risk_warning=True
    ) == pytest.approx(0.10)
    assert model.price_limit_pct(
        symbol="000001.SZ", risk_warning=True
    ) == pytest.approx(0.10)
    assert model.price_limit_pct(symbol="688001.SH") == pytest.approx(0.20)
    assert model.price_limit_pct(symbol="300001.SZ") == pytest.approx(0.20)
    assert model.price_limit_pct(symbol="830001.BJ") == pytest.approx(0.30)


def test_ashare_sessions_keep_after_hours_outside_continuous_auction() -> None:
    contract = ashare_execution_reality().as_contract()
    sessions = contract["sessions"]

    assert contract["price_limit_policy_version"] == "ashare-price-limit-20260706-v1"
    assert contract["session_policy_version"] == "ashare-sessions-20260706-v1"
    assert sessions["continuous_auction_pm"]["end"] == "14:57"
    assert sessions["closing_auction"] == {
        "start": "14:57",
        "end": "15:00",
        "order_type": "limit",
        "execution_supported": False,
        "unsupported_reason": "closing_auction_batch_match_not_implemented",
        "cancel_allowed": False,
    }
    assert sessions["after_hours_fixed_price"] == {
        "start": "15:05",
        "end": "15:30",
        "order_type": "after_hours_fixed_price",
        "execution_supported": False,
        "unsupported_reason": "after_hours_fixed_price_match_not_implemented",
        "eligible_universe": "all_ashares",
        "price_reference": "official_closing_price",
        "cancel_allowed": True,
    }


def test_ashare_fee_breakdown_uses_sell_stamp_and_bilateral_transfer_fee() -> None:
    model = ashare_execution_reality()

    buy = model.calculate_fees("buy", 1_000.0)
    sell = model.calculate_fees("sell", 1_000.0)

    assert buy["commission"] == pytest.approx(5.0)
    assert buy["stamp_duty"] == pytest.approx(0.0)
    assert buy["transfer_fee"] == pytest.approx(0.01)
    assert buy["total"] == pytest.approx(5.01)
    assert sell["commission"] == pytest.approx(5.0)
    assert sell["stamp_duty"] == pytest.approx(0.5)
    assert sell["transfer_fee"] == pytest.approx(0.01)
    assert sell["total"] == pytest.approx(5.51)
    assert sell["stamp_duty_sell_bps"] == pytest.approx(5.0)
    assert sell["transfer_fee_bps"] == pytest.approx(0.1)
    assert sell["execution_reality_model_version"] == ASHARE_EXECUTION_REALITY_VERSION
    assert sell["commission_schedule_status"] == "provisional_pending_broker_contract"


def test_verified_broker_commission_override_requires_versioned_evidence() -> None:
    with pytest.raises(ValueError, match="verified commission schedule"):
        ashare_execution_reality(
            commission_override={
                "commission_bps": 1.5,
                "min_commission_cny": 5.0,
            }
        )

    model = ashare_execution_reality(
        commission_override={
            "commission_bps": 1.5,
            "min_commission_cny": 5.0,
            "commission_schedule_status": "broker_contract_verified",
            "commission_schedule_version": "huachuang-contract-2026-07-v1",
        }
    )

    fees = model.calculate_fees("buy", 100_000.0)
    assert fees["commission"] == pytest.approx(15.0)
    assert fees["commission_schedule_status"] == "broker_contract_verified"
    assert fees["commission_schedule_version"] == "huachuang-contract-2026-07-v1"


def test_price_cage_uses_two_percent_or_ten_ticks_whichever_is_wider() -> None:
    model = ashare_execution_reality()

    liquid_price = model.price_cage_bounds(10.0)
    low_price = model.price_cage_bounds(2.0)

    assert liquid_price == pytest.approx((9.8, 10.2))
    assert low_price == pytest.approx((1.9, 2.1))


def test_daily_price_limits_round_half_up_to_the_versioned_tick() -> None:
    model = ashare_execution_reality()

    lower, upper = model.price_limit_bounds(
        10.05,
        symbol="600000.SH",
        risk_warning=True,
    )

    assert lower == pytest.approx(9.05)
    assert upper == pytest.approx(11.06)


@pytest.mark.parametrize(
    ("current_shares", "sellable_shares", "requested_shares", "expected_reason"),
    [
        (150, 150, 20, "ashare_odd_lot_sell_quantity_invalid"),
        (150, 150, 50, None),
        (150, 150, 100, None),
        (150, 150, 150, None),
    ],
)
def test_ashare_sell_quantity_validator_enforces_odd_lot_remainder_rule(
    current_shares: int,
    sellable_shares: int,
    requested_shares: int,
    expected_reason: str | None,
) -> None:
    validator = getattr(
        execution_reality,
        "ashare_sell_quantity_rejection_reason",
        None,
    )

    assert validator is not None
    assert (
        validator(
            current_shares=current_shares,
            sellable_shares=sellable_shares,
            requested_shares=requested_shares,
        )
        == expected_reason
    )
