#!/usr/bin/env python3
"""Crypto market adapter for the Tradings shadow orchestrator."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from shared.data.reader import TradingsDataReader
from shared.markets.base import MarketAdapter


MARKET = "crypto"
STRATEGY_DIR = Path(__file__).resolve().parent / "strategies"

DEFAULT_UNIVERSE_FILTER: dict[str, Any] = {
    "exchange": "BINANCE",
    "quote_asset": "USDT",
    "active_only": True,
    "max_symbols": 50,
}

_ACTIVE_STATUSES = {"1", "active", "enabled", "listed", "open", "trading", "true", "yes", "y"}


def _is_active(asset: dict[str, Any]) -> bool:
    for key in ("active", "is_active", "enabled"):
        if key not in asset or asset.get(key) in (None, ""):
            continue
        value = asset.get(key)
        if isinstance(value, str):
            return value.strip().lower() in _ACTIVE_STATUSES
        return bool(value)
    status = str(asset.get("status") or asset.get("market_status") or "").strip().lower()
    return not status or status in _ACTIVE_STATUSES


def _is_binance_usdt(asset: dict[str, Any]) -> bool:
    symbol = str(asset.get("symbol") or "").strip().upper()
    if not symbol.endswith("USDT"):
        return False

    quote_asset = str(asset.get("quote_asset") or asset.get("quote") or "").strip().upper()
    if quote_asset and quote_asset != "USDT":
        return False

    exchange = str(asset.get("exchange") or asset.get("provider") or "").strip().upper()
    return not exchange or "BINANCE" in exchange


class CryptoAdapter(MarketAdapter):
    """Market-specific adapter for Crypto shadow screening and execution."""

    def __init__(
        self,
        reader: Any | None = None,
        *,
        universe_filter: dict[str, Any] | None = None,
        strategy_dir: Path | None = None,
    ) -> None:
        self.reader = reader if reader is not None else TradingsDataReader()
        self.universe_filter = {**DEFAULT_UNIVERSE_FILTER, **dict(universe_filter or {})}
        self.strategy_dir = strategy_dir or STRATEGY_DIR

    def get_market(self) -> str:
        return MARKET

    def get_universe(self, date: str) -> list[str]:
        del date
        assets = self._get_assets()
        if not assets:
            return []

        result: list[str] = []
        max_symbols = max(1, int(self.universe_filter.get("max_symbols", 50)))
        for asset in assets:
            if not isinstance(asset, dict):
                continue
            symbol = str(asset.get("symbol") or "").strip().upper()
            if not symbol or not _is_binance_usdt(asset):
                continue
            if self.universe_filter.get("active_only", True) and not _is_active(asset):
                continue
            result.append(symbol)
            if len(result) >= max_symbols:
                break
        return result

    def map_symbol_to_reader(self, symbol: str) -> tuple[str, str]:
        return MARKET, str(symbol or "").strip().upper()

    def get_strategy_config(self) -> dict[str, Any]:
        strategies = self._load_strategies()
        return {
            "market": MARKET,
            "shadow_capital": 100_000.0,
            "portfolio_method": "volatility_targeted",
            "regime": "crypto_24_7",
            "max_candidates": 30,
            "default_price": 1.0,
            "default_volatility": 0.80,
            "volatility_baseline": 0.80,
            "strategies": strategies,
            "market_rules": {
                "settlement": "T+0",
                "can_sell_same_day": True,
                "trading_hours": "24/7",
                "price_limit": None,
                "sessions": {
                    "continuous": {"start": "00:00", "end": "24:00"},
                    "reviews_utc": ["00:00", "12:00"],
                },
                "currency": "USDT",
                "fee_bps": {"maker": 10, "taker": 10},
                "lot_size": 0.0001,
            },
            "universe_filter": dict(self.universe_filter),
        }

    def get_shadow_account(self) -> str:
        return "crypto_shadow"

    def get_sim_account(self) -> str:
        return "crypto_sim"

    def _get_assets(self) -> list[dict[str, Any]]:
        get_assets = getattr(self.reader, "get_assets", None)
        if callable(get_assets):
            rows = get_assets(market=MARKET)
            if rows:
                return list(rows)
            rows = get_assets(market="Crypto")
            if rows:
                return list(rows)

        shared = getattr(self.reader, "shared", None)
        connect = getattr(shared, "_connect", None)
        if callable(connect):
            for market in (MARKET, "Crypto"):
                try:
                    rows = connect().execute(
                        "SELECT * FROM market_assets WHERE market=? ORDER BY symbol ASC",
                        (market,),
                    ).fetchall()
                except Exception:
                    continue
                assets = [dict(row) for row in rows]
                if assets:
                    return assets
        return []

    def _load_strategies(self) -> dict[str, Any]:
        strategies: dict[str, Any] = {}
        if not self.strategy_dir.exists():
            return strategies
        for path in sorted(self.strategy_dir.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            name = str(payload.get("name") or path.stem)
            strategies[name] = payload
        return strategies


__all__ = ["CryptoAdapter"]
