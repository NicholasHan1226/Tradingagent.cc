#!/usr/bin/env python3
"""Prediction Markets adapter for the Tradings shadow loop."""

from __future__ import annotations

from typing import Any

from shared.data.reader import TradingsDataReader
from shared.markets.base import MarketAdapter

from PM import sim_executor as _pm_sim_executor
from PM.strategies import STRATEGY_CONFIGS


class PMAdapter(MarketAdapter):
    """MarketAdapter implementation for probability-market shadow training."""

    def __init__(self, reader: Any | None = None) -> None:
        self.reader = reader or TradingsDataReader()

    def get_market(self) -> str:
        return "pm"

    def get_universe(self, date: str) -> list[str]:
        universe = self.reader.get_pm_universe()
        return [str(market_id) for market_id in universe if market_id]

    def map_symbol_to_reader(self, market_id: str) -> tuple[str, str]:
        return "pm", market_id

    def get_strategy_config(self) -> dict[str, Any]:
        return {
            "shadow_capital": 50000.0,
            "portfolio_method": "pm_probability_weighted",
            "regime": "24_7_probability_market",
            "max_candidates": 20,
            "default_price": 0.5,
            "default_volatility": 0.20,
            "probability_unit": True,
            "single_market_max_weight": 0.05,
            "max_positions": 20,
            "strategies": STRATEGY_CONFIGS,
        }

    def get_shadow_account(self) -> str:
        return "pm_shadow"

    def get_sim_account(self) -> str:
        return "pm_sim"
