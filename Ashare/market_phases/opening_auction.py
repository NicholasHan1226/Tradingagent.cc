"""Opening auction strategy stub (9:15 - 9:25).

A-share opening call auction runs from 09:15 to 09:25.
* 09:15 - 09:20: orders may be placed and cancelled.
* 09:20 - 09:25: orders may be placed but NOT cancelled.
* 09:25: single opening price determined; fills execute.

This stub defines the interface for a future opening-auction strategy.
Concrete logic (signal generation, order sizing) is to be implemented.
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
    action: str = "hold"  # buy / sell / hold
    code: str = ""
    price: float = 0.0
    quantity: int = 0
    confidence: float = 0.0
    reason: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


def generate_signal(market_data: dict, capital_plan: dict | None = None) -> OpeningAuctionSignal:
    """Generate an opening-auction trading signal.

    **STUB** — to be implemented.

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
    # TODO: implement auction-price deviation, volume surge detection,
    # gap analysis, and order-book imbalance signals.
    return OpeningAuctionSignal(
        timestamp=AUCTION_START,
        action="hold",
        reason="Opening auction strategy not yet implemented (stub).",
    )


def validate_timing(current_time: str) -> bool:
    """Return True if *current_time* (HH:MM) is within the auction window."""
    return AUCTION_START <= current_time <= AUCTION_END
