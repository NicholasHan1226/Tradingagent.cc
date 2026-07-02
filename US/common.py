#!/usr/bin/env python3
"""US Phase D P0 common config and market sessions."""

from __future__ import annotations

from dataclasses import dataclass, field

from shared.markets.config_schema import MarketToolConfig


US_SESSIONS = {
    "NYSE": {
        "timezone": "America/New_York",
        "regular": ("09:30", "16:00"),
    },
    "NASDAQ": {
        "timezone": "America/New_York",
        "regular": ("09:30", "16:00"),
    },
}


@dataclass(frozen=True)
class USConfig(MarketToolConfig):
    """US market config with real execution disabled by construction."""

    market: str = "us"
    capital: dict = field(
        default_factory=lambda: {
            "default_layer": "shadow",
            "allowed_layers": ("shadow", "simulated"),
            "initial_capital": 100_000.0,
            "currency": "USD",
        }
    )
    session: dict = field(default_factory=lambda: {"timezone": "America/New_York", "type": "regular"})
    universe: dict = field(default_factory=lambda: {"max_symbols": 100, "min_close": 1.0, "active_only": True})
    risk: dict = field(default_factory=lambda: {"max_positions": 12, "max_single_position_pct": 0.15})
    fees: dict = field(default_factory=lambda: {"taker_bps": 2.0, "maker_bps": 1.0})
    sessions: dict[str, dict[str, object]] = field(default_factory=lambda: dict(US_SESSIONS))

    @property
    def currency(self) -> str:
        return self.capital.currency
