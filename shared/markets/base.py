#!/usr/bin/env python3
"""Market adapter contract for tradingagent orchestrator."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class MarketAdapter(ABC):
    """Abstract market boundary for the market-agnostic shadow loop."""

    @abstractmethod
    def get_universe(self, date: str) -> list[str]:
        """Return symbols eligible for the shadow loop on date."""

    @abstractmethod
    def get_market(self) -> str:
        """Return the canonical market name used by readers and logs."""

    @abstractmethod
    def map_symbol_to_reader(self, symbol: str) -> tuple[str, str]:
        """Map adapter symbol into the reader's (market, symbol) pair."""

    @abstractmethod
    def get_strategy_config(self) -> dict[str, Any]:
        """Return strategy configuration for screening/portfolio/execution."""

    @abstractmethod
    def get_shadow_account(self) -> str:
        """Return the shadow account or strategy namespace."""

    def get_sim_account(self) -> Any:
        """Return simulated account state; defaults to the shadow namespace."""

        return self.get_shadow_account()
