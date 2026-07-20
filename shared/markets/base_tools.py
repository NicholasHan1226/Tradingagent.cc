# DEPRECATED: shadow retired in favor of multi-style simulated trading. See style_runner.py
#!/usr/bin/env python3
"""Abstract base classes for Phase D multi-market shadow/simulated tools."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from shared.markets.config_schema import MarketToolConfig
from shared.markets.safety import assert_no_live_broker, assert_no_real_execution, assert_public_data_only


class BaseMarketData(ABC):
    def __init__(
        self, market: str, config: MarketToolConfig, *, reader: Any | None = None
    ) -> None:
        assert_public_data_only(config)
        self.market = market
        self.config = config
        self.reader = reader

    @abstractmethod
    def get_daily(self, symbol: str, start: str, end: str) -> list[dict[str, Any]]:
        """Return daily bars for one symbol."""

    @abstractmethod
    def get_latest_price(self, symbol: str, date: str) -> float | None:
        """Return the latest usable price at or before date."""

    @abstractmethod
    def get_universe(self, date: str) -> list[str]:
        """Return the market universe for date."""

    @abstractmethod
    def health_check(self) -> dict[str, Any]:
        """Return public-data source health."""


class BaseSimulator(ABC):
    def __init__(
        self,
        market: str,
        config: MarketToolConfig,
        market_data: BaseMarketData,
    ) -> None:
        assert_no_real_execution(config)
        self.market = market
        self.config = config
        self.market_data = market_data
        self.reader = getattr(market_data, "reader", None)
        self.validate_config()

    def validate_config(self) -> None:
        assert_no_live_broker(self.config)
        assert_no_real_execution(self.config)

    @abstractmethod
    def simulate(self, order: dict[str, Any], account: dict[str, Any]) -> dict[str, Any]:
        """Simulate an order against an account."""

    @abstractmethod
    def fill_price(self, symbol: str, date: str) -> float | None:
        """Return a simulated fill price."""


class BaseShadowRunner(ABC):
    def __init__(
        self,
        market: str,
        config: MarketToolConfig,
        market_data: BaseMarketData,
        simulator: BaseSimulator,
    ) -> None:
        assert_no_real_execution(config)
        self.market = market
        self.config = config
        self.market_data = market_data
        self.simulator = simulator
        self.reader = getattr(market_data, "reader", None)

    @abstractmethod
    def run_shadow(self, date: str) -> dict[str, Any]:
        """Run a shadow-market cycle for date."""

    @abstractmethod
    def get_signals(self, date: str) -> list[dict[str, Any]]:
        """Return candidate signals for date."""

    @abstractmethod
    def write_shadow_record(self, record: dict[str, Any]) -> dict[str, Any]:
        """Persist or return a shadow record."""


class BaseReport(ABC):
    def __init__(
        self, market: str, config: MarketToolConfig, *, reader: Any | None = None
    ) -> None:
        assert_no_real_execution(config)
        self.market = market
        self.config = config
        self.reader = reader

    @abstractmethod
    def render_daily(self, date: str) -> dict[str, Any]:
        """Render the daily report payload."""

    @abstractmethod
    def render_scorecard(self, date: str) -> dict[str, Any]:
        """Render strategy scorecard payload."""

    @abstractmethod
    def delivery_policy(self, result: dict[str, Any]) -> dict[str, Any]:
        """Return report delivery decision."""
