"""Closing auction strategy stub (14:57 - 15:00).

A-share closing call auction runs from 14:57 to 15:00.
* Orders may be placed but NOT cancelled during this window.
* 15:00: single closing price determined; fills execute.

This stub defines the interface for a future closing-auction strategy.
Closing auction is often used for:
* End-of-day rebalancing.
* Reverse-repo cash sweep (204001).
* Tactical entries/exits at the closing price.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


AUCTION_START = "14:57"
AUCTION_END = "15:00"


@dataclass
class ClosingAuctionSignal:
    """Signal produced by the closing auction strategy."""

    timestamp: str = ""
    action: str = "hold"  # buy / sell / hold / repo
    code: str = ""
    price: float = 0.0
    quantity: int = 0
    confidence: float = 0.0
    reason: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


def generate_signal(market_data: dict,
                    capital_plan: dict | None = None,
                    positions: list[dict] | None = None) -> ClosingAuctionSignal:
    """Generate a closing-auction trading signal.

    **STUB** — to be implemented.

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
    # TODO: implement closing-price momentum, VWAP deviation,
    # rebalancing, and reverse-repo sweep logic.

    # If capital plan has a reverse-repo suggestion, surface it.
    if capital_plan and capital_plan.get("reverse_repo"):
        repo = capital_plan["reverse_repo"]
        if repo.get("action") == "lend":
            return ClosingAuctionSignal(
                timestamp="14:50",
                action="repo",
                code=repo.get("code", "204001"),
                price=0.0,
                quantity=repo.get("lots", 0),
                confidence=0.9,
                reason=repo.get("instruction", "Reverse repo sweep at close."),
                meta={"amount": repo.get("amount", 0.0)},
            )

    return ClosingAuctionSignal(
        timestamp=AUCTION_START,
        action="hold",
        reason="Closing auction strategy not yet implemented (stub).",
    )


def validate_timing(current_time: str) -> bool:
    """Return True if *current_time* (HH:MM) is within the closing auction window."""
    return AUCTION_START <= current_time <= AUCTION_END
