"""Opening auction strategy (9:15 - 9:25).

A-share opening call auction runs from 09:15 to 09:25.
* 09:15 - 09:20: orders may be placed and cancelled.
* 09:20 - 09:25: orders may be placed but NOT cancelled.
* 09:25: single opening price determined; fills execute.

This module intentionally produces risk signals only. It does not place orders.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


AUCTION_START = "09:15"
AUCTION_END = "09:25"
NO_CANCEL_AFTER = "09:20"


@dataclass
class OpeningAuctionSignal:
    """Signal produced by the opening auction strategy."""

    timestamp: str = ""
    action: str = "hold"  # hold / warn / caution
    code: str = ""
    price: float = 0.0
    quantity: int = 0
    confidence: float = 0.0
    reason: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if result == result else default
    except (TypeError, ValueError):
        return default


def _first_present(row: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return default


def _hhmm(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if "T" in raw:
        raw = raw.split("T", 1)[1]
    if " " in raw:
        raw = raw.split(" ", 1)[1]
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) >= 4:
        return f"{digits[:2]}:{digits[2:4]}"
    return raw[:5]


def _bar_time(bar: dict[str, Any]) -> str:
    return _hhmm(_first_present(bar, "bar_time", "trade_time", "time", "timestamp"))


def _normalize_symbol(symbol: Any) -> str:
    raw = str(symbol or "").strip()
    return raw.split(".", 1)[0] if "." in raw else raw


def _load_5m_bars(market_data: dict[str, Any], reader: Any = None) -> list[dict[str, Any]]:
    bars = market_data.get("bars_5m") or market_data.get("bars") or market_data.get("intraday_bars")
    if isinstance(bars, list):
        return [bar for bar in bars if isinstance(bar, dict)]

    symbol = _normalize_symbol(_first_present(market_data, "symbol", "ts_code", "code"))
    trade_date = _first_present(market_data, "trade_date", "date")
    if not symbol or not trade_date:
        return []

    if reader is None:
        try:
            from shared.data.reader import SharedSignalsReader

            reader = SharedSignalsReader()
        except Exception:
            return []

    get_bars = getattr(reader, "get_bars_intraday", None)
    if not callable(get_bars):
        return []
    try:
        rows = get_bars("Ashare", symbol, "5m", trade_date, trade_date)
        return [row for row in rows if isinstance(row, dict)]
    except Exception:
        return []


def _opening_price(market_data: dict[str, Any], bars: list[dict[str, Any]]) -> float:
    direct = _safe_float(
        _first_present(market_data, "opening_price", "indicative_price", "open", "price"),
        0.0,
    )
    if direct > 0:
        return direct
    first_bar = next((bar for bar in bars if _bar_time(bar) >= AUCTION_START), bars[0] if bars else {})
    return _safe_float(_first_present(first_bar, "open", "close", "price"), 0.0)


def _previous_close(market_data: dict[str, Any], bars: list[dict[str, Any]]) -> float:
    direct = _safe_float(
        _first_present(market_data, "pre_close", "prev_close", "previous_close", "last_close"),
        0.0,
    )
    if direct > 0:
        return direct
    for bar in bars:
        value = _safe_float(_first_present(bar, "pre_close", "prev_close", "previous_close"), 0.0)
        if value > 0:
            return value
    return 0.0


def _volume_ratio_after_open(market_data: dict[str, Any], bars: list[dict[str, Any]]) -> float:
    post_open = [bar for bar in bars if _bar_time(bar) >= AUCTION_START]
    if not post_open:
        return 0.0
    opening_volume = _safe_float(_first_present(post_open[0], "volume", "vol"), 0.0)
    baseline = _safe_float(
        _first_present(
            market_data,
            "avg_5m_volume",
            "avg_volume_5m",
            "volume_baseline",
            "baseline_volume",
        ),
        0.0,
    )
    if baseline <= 0:
        prior_volumes = [
            _safe_float(_first_present(bar, "volume", "vol"), 0.0)
            for bar in bars
            if _bar_time(bar) and _bar_time(bar) < AUCTION_START
        ]
        if not prior_volumes and len(post_open) > 1:
            prior_volumes = [
                _safe_float(_first_present(bar, "volume", "vol"), 0.0)
                for bar in post_open[1:]
            ]
        baseline = sum(prior_volumes) / len(prior_volumes) if prior_volumes else 0.0
    return opening_volume / baseline if baseline > 0 else 0.0


def generate_signal(
    market_data: dict,
    capital_plan: dict | None = None,
    reader: Any = None,
) -> OpeningAuctionSignal:
    """Generate an opening-auction trading signal.

    Parameters
    ----------
    market_data
        Real-time auction data (mock orders, indicative price, volume).
    capital_plan
        Optional capital allocation plan from :mod:`Ashare.capital_plan`.

    Returns
    -------
    OpeningAuctionSignal
    """
    current_time = _hhmm(_first_present(market_data, "current_time", "timestamp", "time", default=AUCTION_START))
    if current_time and not validate_timing(current_time):
        return OpeningAuctionSignal(
            timestamp=current_time,
            action="hold",
            reason="Outside opening auction window.",
            meta={"auction_window": [AUCTION_START, AUCTION_END]},
        )

    bars = _load_5m_bars(market_data, reader=reader)
    open_price = _opening_price(market_data, bars)
    prev_close = _previous_close(market_data, bars)
    gap_pct = (open_price - prev_close) / prev_close if prev_close > 0 and open_price > 0 else 0.0
    volume_ratio = _volume_ratio_after_open(market_data, bars)
    has_gap = abs(gap_pct) > 0.02
    has_surge = volume_ratio > 3.0

    if has_gap and has_surge:
        action = "caution"
        confidence = 0.8
        reason = "Opening auction gap and volume surge detected."
    elif has_gap or has_surge:
        action = "warn"
        confidence = 0.65
        reason = "Opening auction anomaly detected."
    else:
        action = "hold"
        confidence = 0.5 if bars else 0.2
        reason = "Opening auction gap and volume are within thresholds." if bars else "No 5m opening auction bars available."

    return OpeningAuctionSignal(
        timestamp=current_time or AUCTION_START,
        action=action,
        code=str(_first_present(market_data, "ts_code", "symbol", "code", default="")),
        price=open_price,
        confidence=confidence,
        reason=reason,
        meta={
            "capital_layer": "shadow",
            "bar_count": len(bars),
            "gap_pct": round(gap_pct, 6),
            "gap_threshold": 0.02,
            "volume_ratio": round(volume_ratio, 6),
            "volume_ratio_threshold": 3.0,
            "no_cancel_after": NO_CANCEL_AFTER,
        },
    )


def validate_timing(current_time: str) -> bool:
    """Return True if *current_time* (HH:MM) is within the auction window."""
    return AUCTION_START <= current_time <= AUCTION_END
