#!/usr/bin/env python3
"""US market data adapter reading SharedSignals market=US/us."""

from __future__ import annotations

from typing import Any

from shared.markets.base_tools import BaseMarketData
from shared.markets.config_schema import MarketToolConfig
from US.common import USConfig


READER_MARKETS = ("us", "US")


def _symbol(value: str) -> str:
    return str(value or "").strip().upper()


class USMarketData(BaseMarketData):
    """Read US public market data from TradingAgent's SharedSignals reader."""

    def __init__(self, config: MarketToolConfig | None = None, reader: Any | None = None) -> None:
        super().__init__("us", config or USConfig())
        if reader is not None:
            self.reader = reader

    def get_daily(self, symbol: str, start: str = "", end: str = "") -> list[dict[str, Any]]:
        ticker = _symbol(symbol)
        for market in READER_MARKETS:
            rows = self.reader.get_bars_daily(market, ticker, start, end)
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
        del date
        assets = self._assets()
        symbols: list[str] = []
        seen: set[str] = set()
        for asset in assets:
            symbol = _symbol(str(asset.get("symbol") or asset.get("ts_code") or ""))
            if not symbol or symbol in seen:
                continue
            if self.config.universe.active_only and not _is_active(asset):
                continue
            symbols.append(symbol)
            seen.add(symbol)
            if len(symbols) >= self.config.universe.max_symbols:
                break
        return symbols

    def health_check(self) -> dict[str, Any]:
        universe = self.get_universe("")
        return {
            "market": "us",
            "source": "SharedSignals",
            "status": "ok" if universe else "degraded",
            "universe_count": len(universe),
            "real_execution": False,
        }

    def _assets(self) -> list[dict[str, Any]]:
        get_assets = getattr(self.reader, "get_assets", None)
        if callable(get_assets):
            for market in READER_MARKETS:
                rows = get_assets(market=market)
                if rows:
                    return [dict(row) for row in rows]
        shared = getattr(self.reader, "shared", None)
        get_assets = getattr(shared, "get_assets", None)
        if callable(get_assets):
            for market in READER_MARKETS:
                rows = get_assets(market)
                if rows:
                    return [dict(row) for row in rows]
        return []


def _is_active(asset: dict[str, Any]) -> bool:
    value = asset.get("active")
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str) and value.strip():
        return value.strip().lower() in {"1", "true", "yes", "active", "tradable", "listed"}
    status = str(asset.get("status") or "").strip().lower()
    return not status or status in {"active", "tradable", "listed", "normal", "ok"}


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
