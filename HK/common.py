#!/usr/bin/env python3
"""HK Phase D P0 common config and HKEX sessions."""

from __future__ import annotations

from dataclasses import dataclass, field

from shared.markets.config_schema import MarketToolConfig


HK_SESSIONS = {
    "HKEX": {
        "timezone": "Asia/Hong_Kong",
        "regular": (("09:30", "12:00"), ("13:00", "16:00")),
    }
}


@dataclass(frozen=True)
class HKConfig(MarketToolConfig):
    """HK market config with broker execution disabled."""

    market: str = "hk"
    capital: dict = field(
        default_factory=lambda: {
            "default_layer": "shadow",
            "allowed_layers": ("shadow", "simulated"),
            "initial_capital": 500_000.0,
            "currency": "HKD",
        }
    )
    session: dict = field(default_factory=lambda: {"timezone": "Asia/Hong_Kong", "type": "regular"})
    universe: dict = field(default_factory=lambda: {"max_symbols": 80, "min_close": 0.01, "active_only": True})
    risk: dict = field(default_factory=lambda: {"max_positions": 10, "max_single_position_pct": 0.15})
    fees: dict = field(default_factory=lambda: {"taker_bps": 8.0, "maker_bps": 8.0})
    sessions: dict[str, dict[str, object]] = field(default_factory=lambda: dict(HK_SESSIONS))

    @property
    def currency(self) -> str:
        return self.capital.currency
