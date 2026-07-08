#!/usr/bin/env python3
"""HK market data reader for SharedSignals market=HK/hk and hk_daily bridge data."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from shared.markets.base_tools import BaseMarketData
from shared.markets.config_schema import MarketToolConfig
from HK.adapter import HKAdapter
from HK.common import HKConfig


READER_MARKETS = ("hk", "HK")


class HKMarketData(BaseMarketData):
    """Read HK public market data from SharedSignals."""

    def __init__(
        self,
        config: MarketToolConfig | None = None,
        reader: Any | None = None,
        master_path: Path | str | None = None,
    ) -> None:
        super().__init__("hk", config or HKConfig())
        if reader is not None:
            self.reader = reader
        self.adapter = HKAdapter(config=self.config, reader=self.reader, master_path=master_path)

    def get_daily(self, symbol: str, start: str = "", end: str = "") -> list[dict[str, Any]]:
        mapped = self.adapter.to_sharedsignals_symbol(symbol)
        for market in READER_MARKETS:
            rows = self.reader.get_bars_daily(market, mapped, start, end)
            if rows:
                return [dict(row) for row in rows]
        return []

    def get_latest_price(self, symbol: str, date: str) -> float | None:
        rows = self.get_daily(symbol, "", date)
        for row in reversed(rows):
            price = _to_float(row.get("adjusted_close", row.get("close")))
            if price and price > 0:
                return price
        return None

    def get_universe(self, date: str) -> list[str]:
        return self.adapter.get_universe(date)

    def health_check(self) -> dict[str, Any]:
        universe = self.get_universe("")
        return {
            "market": "hk",
            "source": "SharedSignals",
            "tables": ["market_bars_daily", "hk_daily", "market_assets"],
            "status": "ok" if universe else "degraded",
            "universe_count": len(universe),
            "real_execution": False,
        }


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
