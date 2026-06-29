#!/usr/bin/env python3
"""Slippage model for market and limit orders.

Estimates slippage as a function of order size relative to average daily
volume. Market orders pay full impact; limit orders have a fill probability
that depends on distance from mid.

Reference: Ashare/tools/a_share_shadow_sim_broker.py uses a similar approach
for sim_broker fills.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

# --- Constants (tuned for A-share liquid stocks, adjustable) ---

# Market order: base slippage 0.1% per 1.0 volume ratio
MARKET_BASE_SLIPPAGE_PCT = 0.1
# Volume impact exponent (square-root model: impact ~ sqrt(volume_ratio))
VOLUME_IMPACT_EXPONENT = 0.5
# Bid-ask spread baseline for A-shares (liquid stocks ~0.1-0.2%)
BID_ASK_SPREAD_PCT = 0.15
# Limit order fill probability parameters
LIMIT_FILL_BASE_PROB = 0.85
LIMIT_FILL_DECAY_PER_BPS = 0.003  # probability decay per basis point from mid
MIN_FILL_PROB = 0.05


@dataclass
class SlippageEstimate:
    """Slippage estimation result."""

    slippage_pct: float           # estimated slippage as % of price
    fill_probability: float       # probability of fill (1.0 for market orders)
    estimated_fill_price: float | None
    model: str                    # "market_sqrt" | "limit_distance"
    details: dict[str, Any]


def estimate_slippage(
    order_type: str,
    volume: int,
    avg_volume: int,
    *,
    mid_price: float | None = None,
    limit_distance_bps: float | None = None,
) -> SlippageEstimate:
    """Estimate slippage for an order.

    Args:
        order_type: "market" or "limit".
        volume: Order volume in shares.
        avg_volume: Average daily volume in shares for the stock.
        mid_price: Mid/current price (optional, for price estimation).
        limit_distance_bps: For limit orders, distance of limit price from
            mid in basis points (positive = away from favorable side).

    Returns:
        SlippageEstimate with slippage_pct, fill_probability, and details.
    """
    if avg_volume <= 0:
        avg_volume = 1_000_000  # fallback to avoid div-by-zero

    volume_ratio = volume / avg_volume
    # Cap volume ratio at 10% of ADV (beyond that, model breaks down)
    volume_ratio_capped = min(volume_ratio, 0.10)

    if order_type.lower() == "market":
        # Square-root market impact model:
        # slippage = base * sqrt(volume_ratio) + half_spread
        impact_pct = MARKET_BASE_SLIPPAGE_PCT * math.sqrt(volume_ratio_capped) / math.sqrt(0.01)
        # Normalize: at volume_ratio=0.01 (1% ADV), impact = base_slippage
        impact_pct = MARKET_BASE_SLIPPAGE_PCT * (volume_ratio_capped / 0.01) ** VOLUME_IMPACT_EXPONENT
        half_spread = BID_ASK_SPREAD_PCT / 2.0
        total_slippage = impact_pct + half_spread

        fill_price = None
        if mid_price is not None:
            fill_price = mid_price * (1 + total_slippage / 100.0)

        return SlippageEstimate(
            slippage_pct=round(total_slippage, 4),
            fill_probability=1.0,
            estimated_fill_price=fill_price,
            model="market_sqrt",
            details={
                "volume_ratio": round(volume_ratio, 6),
                "volume_ratio_capped": round(volume_ratio_capped, 6),
                "impact_pct": round(impact_pct, 4),
                "half_spread_pct": half_spread,
                "base_slippage_pct": MARKET_BASE_SLIPPAGE_PCT,
                "exponent": VOLUME_IMPACT_EXPONENT,
            },
        )

    elif order_type.lower() == "limit":
        # Limit order: slippage is negative if limit is favorable,
        # but fill probability decreases with distance from mid.
        if limit_distance_bps is None:
            limit_distance_bps = 0.0

        # Fill probability: starts high at mid, decays with distance
        fill_prob = max(
            MIN_FILL_PROB,
            LIMIT_FILL_BASE_PROB - LIMIT_FILL_DECAY_PER_BPS * abs(limit_distance_bps),
        )
        # Clamp to [0, 1]
        fill_prob = min(1.0, max(0.0, fill_prob))

        # For limit orders at or better than mid, "slippage" is the spread capture
        # For limits away from mid, slippage is the opportunity cost
        if limit_distance_bps >= 0:
            # Limit is on unfavorable side — slippage = distance
            slippage_pct = limit_distance_bps / 100.0  # bps to %
        else:
            # Limit is favorable — negative slippage (spread capture)
            slippage_pct = limit_distance_bps / 100.0

        fill_price = None
        if mid_price is not None:
            fill_price = mid_price * (1 + slippage_pct / 100.0)

        return SlippageEstimate(
            slippage_pct=round(slippage_pct, 4),
            fill_probability=round(fill_prob, 4),
            estimated_fill_price=fill_price,
            model="limit_distance",
            details={
                "volume_ratio": round(volume_ratio, 6),
                "limit_distance_bps": limit_distance_bps,
                "base_fill_prob": LIMIT_FILL_BASE_PROB,
                "decay_per_bps": LIMIT_FILL_DECAY_PER_BPS,
            },
        )

    else:
        return SlippageEstimate(
            slippage_pct=0.0,
            fill_probability=0.0,
            estimated_fill_price=None,
            model="unknown",
            details={"error": f"Unknown order_type: {order_type}"},
        )
