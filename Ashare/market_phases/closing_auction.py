"""Closing auction strategy (14:50 - 15:00).

A-share closing call auction runs from 14:57 to 15:00.
* Orders may be placed but NOT cancelled during this window.
* 15:00: single closing price determined; fills execute.

Closing auction is often used for:
* End-of-day rebalancing.
* Reverse-repo cash sweep (204001).
* Tactical entries/exits at the closing price.

This module emits advisory signals only. It does not place orders.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


AUCTION_START = "14:50"
AUCTION_END = "15:00"
CALL_AUCTION_START = "14:57"
REVERSE_REPO_CODE = "204001"


@dataclass
class ClosingAuctionSignal:
    """Signal produced by the closing auction strategy."""

    timestamp: str = ""
    action: str = "hold"  # hold / warn / caution / reverse_repo
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


def _closing_window_bars(bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    window = [bar for bar in bars if AUCTION_START <= _bar_time(bar) <= AUCTION_END]
    return window or bars


def _vwap(bars: list[dict[str, Any]]) -> float:
    amount = 0.0
    volume = 0.0
    for bar in bars:
        vol = _safe_float(_first_present(bar, "volume", "vol"), 0.0)
        price = _safe_float(_first_present(bar, "close", "price"), 0.0)
        if vol > 0 and price > 0:
            volume += vol
            amount += vol * price
    return amount / volume if volume > 0 else 0.0


def _tail_metrics(bars: list[dict[str, Any]]) -> dict[str, float]:
    if not bars:
        return {"vwap": 0.0, "last_price": 0.0, "vwap_deviation": 0.0, "tail_momentum": 0.0}
    first_price = _safe_float(_first_present(bars[0], "open", "close", "price"), 0.0)
    last_price = _safe_float(_first_present(bars[-1], "close", "price"), 0.0)
    vwap = _vwap(bars)
    return {
        "vwap": vwap,
        "last_price": last_price,
        "vwap_deviation": (last_price - vwap) / vwap if vwap > 0 and last_price > 0 else 0.0,
        "tail_momentum": (last_price - first_price) / first_price if first_price > 0 and last_price > 0 else 0.0,
    }


def _reverse_repo_plan(capital_plan: dict[str, Any] | None) -> dict[str, Any] | None:
    if not capital_plan:
        return None
    repo = capital_plan.get("reverse_repo")
    if isinstance(repo, dict) and str(repo.get("action", "")).lower() in {"lend", "repo", "reverse_repo"}:
        amount = _safe_float(repo.get("amount"), 0.0)
        lots = int(_safe_float(repo.get("lots"), 0.0))
        return {
            "code": str(repo.get("code") or REVERSE_REPO_CODE),
            "amount": amount,
            "lots": lots,
            "instruction": repo.get("instruction") or "Idle cash reverse repo sweep.",
        }

    idle_cash = _safe_float(
        _first_present(capital_plan, "idle_cash", "available_cash", "cash_available", "cash"),
        0.0,
    )
    if idle_cash <= 0:
        return None
    return {
        "code": REVERSE_REPO_CODE,
        "amount": idle_cash,
        "lots": int(idle_cash // 1000),
        "instruction": "Idle cash reverse repo sweep.",
    }


def generate_signal(
    market_data: dict,
    capital_plan: dict | None = None,
    positions: list[dict] | None = None,
    reader: Any = None,
) -> ClosingAuctionSignal:
    """Generate a closing-auction trading signal.

    Parameters
    ----------
    market_data
        Real-time closing-auction data.
    capital_plan
        Capital plan including reverse-repo suggestion.
    positions
        Current holdings for potential EOD rebalancing.

    Returns
    -------
    ClosingAuctionSignal
    """
    current_time = _hhmm(_first_present(market_data, "current_time", "timestamp", "time", default=AUCTION_START))
    if current_time and not validate_timing(current_time):
        return ClosingAuctionSignal(
            timestamp=current_time,
            action="hold",
            reason="Outside closing auction watch window.",
            meta={"auction_window": [AUCTION_START, AUCTION_END], "call_auction_start": CALL_AUCTION_START},
        )

    bars = _closing_window_bars(_load_5m_bars(market_data, reader=reader))
    metrics = _tail_metrics(bars)
    vwap_deviation = metrics["vwap_deviation"]
    tail_momentum = metrics["tail_momentum"]
    has_vwap_deviation = abs(vwap_deviation) > 0.005
    weak_tail = tail_momentum < -0.003
    strong_tail = tail_momentum > 0.003
    repo = _reverse_repo_plan(capital_plan)

    meta = {
        "capital_layer": "shadow",
        "bar_count": len(bars),
        "vwap": round(metrics["vwap"], 6),
        "last_price": round(metrics["last_price"], 6),
        "vwap_deviation": round(vwap_deviation, 6),
        "vwap_deviation_threshold": 0.005,
        "tail_momentum": round(tail_momentum, 6),
        "tail_momentum_threshold": 0.003,
        "call_auction_start": CALL_AUCTION_START,
        "position_count": len(positions or []),
    }

    if repo is not None:
        meta["amount"] = repo["amount"]
        meta["tail_state"] = "weak" if weak_tail else "strong" if strong_tail else "neutral"
        return ClosingAuctionSignal(
            timestamp=current_time or AUCTION_START,
            action="reverse_repo",
            code=repo["code"],
            price=0.0,
            quantity=repo["lots"],
            confidence=0.85 if not weak_tail else 0.65,
            reason=repo["instruction"],
            meta=meta,
        )

    if has_vwap_deviation and weak_tail:
        action = "caution"
        confidence = 0.75
        reason = "Closing VWAP deviation and weak tail momentum detected."
    elif has_vwap_deviation or weak_tail or strong_tail:
        action = "warn"
        confidence = 0.6
        reason = "Closing auction tail signal detected."
    else:
        action = "hold"
        confidence = 0.5 if bars else 0.2
        reason = "Closing auction metrics are within thresholds." if bars else "No 5m closing bars available."

    return ClosingAuctionSignal(
        timestamp=current_time or AUCTION_START,
        action=action,
        code=str(_first_present(market_data, "ts_code", "symbol", "code", default="")),
        price=metrics["last_price"],
        confidence=confidence,
        reason=reason,
        meta=meta,
    )


def validate_timing(current_time: str) -> bool:
    """Return True if *current_time* (HH:MM) is within the closing auction window."""
    return AUCTION_START <= current_time <= AUCTION_END
