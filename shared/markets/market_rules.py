#!/usr/bin/env python3
"""Per-market trading rules — one source of truth for each market."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from shared.execution.execution_reality import ashare_execution_reality

_ASHARE_REALITY = ashare_execution_reality()


@dataclass
class MarketRules:
    market: str
    currency: str
    settlement: str  # T+0, T+1, T+2, probability
    trading_hours: str
    price_limit_pct: float  # 0 = no limit
    stamp_duty_bps: float
    commission_bps: float
    min_commission: float
    lot_size: int
    short_selling: bool
    margin_trading: bool
    max_daily_loss_pct: float
    circuit_breaker_pct: float  # 0 = no breaker
    transfer_fee_bps: float = 0.0
    execution_reality_model_version: str = ""


RULES = {
    "ashare": MarketRules(
        "ashare",
        "CNY",
        "T+1",
        "continuous=09:30-11:30,13:00-14:57; after_hours_fixed_price=15:05-15:30",
        10.0,
        _ASHARE_REALITY.stamp_duty_sell_bps,
        _ASHARE_REALITY.commission_bps,
        _ASHARE_REALITY.min_commission_cny,
        _ASHARE_REALITY.buy_lot_size,
        False,
        True,
        20.0,
        0,
        _ASHARE_REALITY.transfer_fee_bps,
        _ASHARE_REALITY.model_version,
    ),
    "crypto": MarketRules(
        "crypto", "USDT", "T+0", "24x7", 0, 0, 10.0, 0, 1, True, True, 30.0, 0
    ),
    "us": MarketRules(
        "us", "USD", "T+2", "9:30-16:00 NY", 0, 0, 1.0, 1.0, 1, True, True, 25.0, 7.0
    ),
    "pm": MarketRules(
        "pm", "USDC", "probability", "24x7", 0, 0, 0, 0, 1, True, False, 15.0, 0
    ),
    "hk": MarketRules(
        "hk",
        "HKD",
        "T+2",
        "9:30-16:00 HK",
        0,
        10.0,
        10.0,
        10.0,
        100,
        False,
        True,
        20.0,
        0,
    ),
}


def can_sell(market: str, buy_date: str, sell_date: str) -> bool:
    """T+1/T+2 settlement check."""
    r = RULES.get(market)
    if not r or r.settlement == "T+0":
        return True
    if r.settlement == "probability":
        return True
    return sell_date > buy_date  # simplified: next day


def apply_price_limit(
    market: str,
    price: float,
    reference: float,
    *,
    symbol: str = "",
    board: str | None = None,
    risk_warning: bool = False,
) -> float:
    """Clamp to market price limits."""
    r = RULES.get(market)
    if not r or r.price_limit_pct == 0:
        return price
    if market == "ashare":
        lower, upper = _ASHARE_REALITY.price_limit_bounds(
            reference,
            symbol=symbol,
            board=board,
            risk_warning=risk_warning,
        )
        return max(lower, min(upper, price))
    price_limit_pct = r.price_limit_pct
    limit = reference * price_limit_pct / 100
    return max(reference - limit, min(reference + limit, price))


def commission(market: str, notional: float, side: str = "buy") -> float:
    """Calculate total fees for a trade."""
    r = RULES.get(market)
    if not r:
        return 0.0
    if market == "ashare":
        return float(_ASHARE_REALITY.calculate_fees(side, notional)["total"])
    fee = notional * r.commission_bps / 10000
    if side == "sell" and r.stamp_duty_bps > 0:
        fee += notional * r.stamp_duty_bps / 10000
    return max(r.min_commission, round(fee, 2))


def slippage_estimate(market: str, volatility_pct: float) -> float:
    """Estimate slippage in bps based on market and volatility."""
    base = {"ashare": 2.0, "crypto": 5.0, "us": 1.5, "pm": 0.5, "hk": 2.5}
    return base.get(market, 3.0) * (1 + volatility_pct / 100)


def is_trading_session(market: str) -> bool:
    """Check if market is currently open (Beijing-time aware)."""
    r = RULES.get(market)
    if not r:
        return False
    if "24x7" in r.trading_hours:
        return True
    import zoneinfo

    try:
        bj_tz = zoneinfo.ZoneInfo("Asia/Shanghai")
    except Exception:
        from datetime import timezone as _tz, timedelta as _td

        bj_tz = _tz(_td(hours=8))
    now = datetime.now(bj_tz)
    hour = now.hour
    wday = now.weekday()
    # Market-specific session windows (Beijing time)
    _MARKET_HOURS = {
        "ashare": (9, 15),  # 09:00-15:00
        "hk": (9, 16),  # 09:00-16:00
        "us": None,  # 21:30-04:00 next day (complex, approximate)
        "crypto": None,  # 24x7
        "pm": None,  # 24x7
    }
    window = _MARKET_HOURS.get(market)
    if window is None:
        # For markets with complex hours, assume always potentially active
        return wday < 5 if market == "us" else True
    start_h, end_h = window
    return start_h <= hour <= end_h and wday < 5


def max_position_pct(market: str) -> float:
    """Single position max as percentage of portfolio."""
    return {"ashare": 10.0, "crypto": 15.0, "us": 12.0, "pm": 8.0, "hk": 10.0}.get(
        market, 10.0
    )


if __name__ == "__main__":
    for m, r in RULES.items():
        print(
            f"{m}: {r.settlement} | limit={r.price_limit_pct}% | stamp={r.stamp_duty_bps}bps | comm={r.commission_bps}bps"
        )
