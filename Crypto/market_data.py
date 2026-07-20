#!/usr/bin/env python3
"""Crypto data adapter for an explicit fixture or future TradingDatas V1 port."""

from __future__ import annotations

from typing import Any

from Crypto.common import TRADINGDATAS_MARKET_CONTEXT, CryptoConfig, load_crypto_config
from shared.governance.retirement import require_explicit_data_port
from shared.markets.base_tools import BaseMarketData


class CryptoMarketData(BaseMarketData):
    """Read Crypto rows only through an explicitly injected safe data port."""

    def __init__(self, config: CryptoConfig | None = None, reader: Any | None = None) -> None:
        super().__init__("crypto", config or load_crypto_config(), reader=reader)

    def get_daily(self, symbol: str, start: str = "", end: str = "") -> list[dict[str, Any]]:
        normalized = self._normalize_symbol(symbol)
        reader = require_explicit_data_port(
            self.reader, context="CryptoMarketData.get_daily"
        )
        return list(
            reader.get_bars_daily(
                TRADINGDATAS_MARKET_CONTEXT, normalized, start or "", end or ""
            )
        )

    def get_latest_price(self, symbol: str, date: str) -> float | None:
        rows = self.get_daily(symbol, "", date)
        for row in reversed(rows):
            price = self._safe_float(row.get("close"))
            if price is not None and price > 0:
                return price
        return None

    def get_universe(self, date: str) -> list[str]:
        symbols = self._asset_universe()
        if not symbols:
            symbols = self._daily_bar_universe(date)

        result: list[str] = []
        for symbol in symbols:
            latest = self.get_latest_price(symbol, date)
            if latest is None or latest < self.config.universe.min_close:
                continue
            result.append(symbol)
            if len(result) >= self.config.universe.max_symbols:
                break
        return result

    def health_check(self) -> dict[str, Any]:
        try:
            universe = self.get_universe("")
        except Exception as exc:
            return {
                "ok": False,
                "market": "crypto",
                "tradingdatas_market_context": TRADINGDATAS_MARKET_CONTEXT,
                "source": "TradingDatas fixture_or_v1_port",
                "error": str(exc),
            }
        return {
            "ok": True,
            "market": "crypto",
            "tradingdatas_market_context": TRADINGDATAS_MARKET_CONTEXT,
            "source": "TradingDatas fixture_or_v1_port",
            "universe_count": len(universe),
            "public_data_only": True,
        }

    def _asset_universe(self) -> list[str]:
        reader = require_explicit_data_port(
            self.reader, context="CryptoMarketData.get_universe"
        )
        get_assets = getattr(reader, "get_assets", None)
        rows: list[dict[str, Any]] = []
        if callable(get_assets):
            rows = list(get_assets(TRADINGDATAS_MARKET_CONTEXT) or [])

        symbols: list[str] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            symbol = self._normalize_symbol(row.get("symbol"))
            if not symbol.endswith("USDT"):
                continue
            if self.config.universe.active_only and not self._is_active(row):
                continue
            symbols.append(symbol)
        return sorted(dict.fromkeys(symbols))

    def _daily_bar_universe(self, date: str) -> list[str]:
        del date
        return []

    @staticmethod
    def _normalize_symbol(symbol: Any) -> str:
        return str(symbol or "").strip().upper()

    @staticmethod
    def _safe_float(value: Any) -> float | None:
        try:
            result = float(value)
        except (TypeError, ValueError):
            return None
        return result if result == result else None

    @staticmethod
    def _is_active(asset: dict[str, Any]) -> bool:
        for key in ("active", "is_active", "enabled"):
            if key in asset and asset.get(key) not in (None, ""):
                value = asset.get(key)
                return str(value).strip().lower() in {"1", "true", "yes", "y", "active", "enabled", "trading"}
        status = str(asset.get("status") or asset.get("market_status") or "").strip().lower()
        return not status or status in {"active", "enabled", "listed", "open", "trading"}


__all__ = ["CryptoMarketData"]
