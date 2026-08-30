"""Pure Decimal accounting for research; no account, order or network writes.

Linear USDT contracts use fixed coin quantities, not reciprocal returns.
Fees are charged on each executed notional; slippage changes execution prices.
Funding is a discrete signed cash flow, never a prorated premium proxy.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from Crypto.ten_symbol_factor_prescreen import _assert_simulation_only, _parse_utc

ZERO = Decimal(0)
ONE = Decimal(1)


def number(value: Any, *, positive: bool = False) -> Decimal:
    if not isinstance(value, (str, Decimal, int)) or isinstance(value, bool):
        raise ValueError("accounting_decimal_invalid")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("accounting_decimal_invalid") from exc
    if not result.is_finite() or (positive and result <= 0):
        raise ValueError("accounting_decimal_invalid")
    return result


def linear_leg(
    *, quantity: Decimal, entry: Decimal, exit: Decimal, side: int,
    fee_rate: Decimal, slippage: Decimal,
) -> dict[str, Decimal]:
    """Dollar P&L for a fixed-quantity long or short; no leverage multiplier."""
    quantity, entry, exit = (number(x, positive=True) for x in (quantity, entry, exit))
    fee_rate, slippage = number(fee_rate), number(slippage)
    if type(side) is not int or side not in (-1, 1):
        raise ValueError("accounting_side_invalid")
    if not ZERO <= fee_rate < ONE or not ZERO <= slippage < ONE:
        raise ValueError("accounting_cost_invalid")
    entry_fill = entry * (ONE + side * slippage)
    exit_fill = exit * (ONE - side * slippage)
    gross = side * quantity * (exit - entry)
    filled = side * quantity * (exit_fill - entry_fill)
    fees = quantity * (entry_fill + exit_fill) * fee_rate
    return {
        "gross_pnl": gross, "slippage_cost": gross - filled,
        "fees": fees, "net_pnl": filled - fees,
        "entry_fill": entry_fill, "exit_fill": exit_fill,
        "entry_notional": quantity * entry,
    }


def funded_hedge(
    prices: Sequence[Mapping[str, Any]],
    funding: Sequence[Mapping[str, Any]],
    *, expected_funding_times: Sequence[str], quantity: Decimal,
    spot_fee: Decimal, perp_fee: Decimal, slippage: Decimal,
    collateral: Decimal, maintenance_rate: Decimal,
    step: timedelta = timedelta(minutes=5),
) -> dict[str, Any]:
    """Long spot + equal-quantity short linear perp, fully collateralised.

Caller must provide observed spot/perp/mark paths and an independently known
funding schedule. Missing events/marks or price gaps reject the experiment.
Positions open AFTER entry-time settlement and close AFTER exit settlement:
funding accrues exactly on (entry_time, exit_time]. Margin checks use sampled
marks and are a conservative research screen, not an exchange liquidation model.
"""
    _assert_simulation_only()
    quantity = number(quantity, positive=True)
    collateral = number(collateral, positive=True)
    maintenance_rate = number(maintenance_rate)
    if not ZERO < maintenance_rate < ONE or step <= timedelta(0) or len(prices) < 2:
        raise ValueError("carry_parameters_invalid")
    path = []
    for row in prices:
        time = _parse_utc(row["time"])
        values = {key: number(row[key], positive=True) for key in ("spot", "perp", "mark")}
        if path and time - path[-1][0] != step:
            raise ValueError("carry_price_gap_or_duplicate")
        path.append((time, values))
    entry_time, start = path[0]
    exit_time, end = path[-1]
    spot = linear_leg(quantity=quantity, entry=start["spot"], exit=end["spot"], side=1,
                      fee_rate=spot_fee, slippage=slippage)
    perp = linear_leg(quantity=quantity, entry=start["perp"], exit=end["perp"], side=-1,
                      fee_rate=perp_fee, slippage=slippage)
    # Fully funded futures collateral: no leveraged margin-return denominator.
    if collateral < quantity * start["perp"]:
        raise ValueError("carry_unlevered_collateral_required")
    expected = [_parse_utc(t) for t in expected_funding_times]
    if expected != sorted(set(expected)) or any(not entry_time < t <= exit_time for t in expected):
        raise ValueError("carry_funding_schedule_invalid")
    settlements = {}
    for row in funding:
        time = _parse_utc(row["funding_time"])
        if time in settlements or time not in expected:
            raise ValueError("carry_funding_unexpected_or_duplicate")
        settlements[time] = quantity * number(row["mark_price"], positive=True) * number(row["funding_rate"])
    if set(settlements) != set(expected):
        raise ValueError("carry_funding_or_mark_missing")
    spot_cost = quantity * spot["entry_fill"] * (ONE + number(spot_fee))
    initial = spot_cost + collateral
    funding_total = ZERO
    max_drawdown = ZERO
    peak = initial
    margin_breach = None
    equity_path = []
    settlement_index = 0
    for time, marks in path:
        # Exchange settlement timestamps may include milliseconds and need
        # not coincide with a sampled 5m price. Never round or prorate them.
        while settlement_index < len(expected) and expected[settlement_index] <= time:
            funding_total += settlements[expected[settlement_index]]
            settlement_index += 1
        perp_equity = (collateral + quantity * (perp["entry_fill"] - marks["mark"])
                       - quantity * perp["entry_fill"] * number(perp_fee) + funding_total)
        required = quantity * marks["mark"] * (maintenance_rate + number(perp_fee))
        if perp_equity <= required and margin_breach is None:
            margin_breach = time.isoformat()
        equity = quantity * marks["spot"] + perp_equity
        equity_path.append(equity)
    net = spot["net_pnl"] + perp["net_pnl"] + funding_total
    equity_path.append(initial + net)  # include both actual exit legs and fees
    for equity in equity_path:
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, (peak - equity) / peak)
    result = {
        "status": "margin_screen_failed" if margin_breach else "evaluated_research_only",
        "gross_price_pnl": str(spot["gross_pnl"] + perp["gross_pnl"]),
        "fees": str(spot["fees"] + perp["fees"]),
        "slippage_cost": str(spot["slippage_cost"] + perp["slippage_cost"]),
        "funding_cashflow": str(funding_total), "funding_event_count": len(funding),
        "capital_committed": str(initial), "margin_breach_at": margin_breach,
        "net_pnl": None if margin_breach else str(net),
        "return_on_committed_capital": None if margin_breach else str(net / initial),
        "sampled_mark_max_drawdown": str(max_drawdown),
        "settlement_boundary": "entry_exclusive_exit_inclusive; trades_after_settlement",
        "margin_model": "sampled_marks_only; not_exchange_liquidation_precision",
        "authority": "none", "research_only": True, "not_promotion_evidence": True,
        "real_trading_enabled": False, "promotion_authorized": False,
    }
    return result
