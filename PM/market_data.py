#!/usr/bin/env python3
"""PM Phase D market data — reads Polymarket markets and prices from SharedSignals.

Shadow/simulated only. Never connects to Polymarket or any live CLOB.
All prices are clamped [0, 1] probability domain.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from shared.data.reader import TradingagentDataReader
from shared.markets.base_tools import BaseMarketData
from shared.markets.config_schema import MarketToolConfig

from PM.common import clamp_probability


class PMMarketData(BaseMarketData):
    """Public-data reader for Polymarket markets and prices.

    Reads from market_pm_markets (market metadata) and market_pm_prices
    (price history) tables via TradingagentDataReader. All prices are
    in [0, 1] probability space.
    """

    def __init__(self, config: MarketToolConfig | None = None) -> None:
        from PM.common import load_pm_config

        if config is None:
            config = load_pm_config().to_market_tool_config()
        super().__init__(market="pm", config=config)

    # --- Abstract method implementations --------------------------------------

    def get_daily(self, symbol: str, start: str, end: str) -> list[dict[str, Any]]:
        """Return price history rows for a market between start and end dates.

        Reads from market_pm_prices, sorted by date ascending.
        """
        try:
            rows = self.reader.get_pm_prices(symbol, start_date=start, end_date=end)
        except TypeError:
            rows = self._query_market_pm_prices(symbol, start, end)
        except Exception:
            rows = []

        if not rows:
            latest = self._latest_market_row(symbol)
            rows = [latest] if latest else []

        return self._clamp_rows(rows or [])

    def get_latest_price(self, symbol: str, date: str) -> float | None:
        """Return the latest YES price for a market at or before date.

        Returns None if no price data is available.
        """
        rows = self.get_daily(symbol, start="", end=date)
        if not rows:
            return None
        last = rows[-1]
        return self._extract_price(last)

    def get_latest_outcome_price(self, symbol: str, date: str, outcome: str = "yes") -> float | None:
        """Return the latest YES or NO price for a market at or before date."""
        rows = self.get_daily(symbol, start="", end=date)
        if not rows:
            return None
        last = rows[-1]
        return self._extract_outcome_price(last, outcome)

    def get_universe(self, date: str) -> list[str]:
        """Return active market IDs for the given date.

        Reads from market_pm_markets, filtering by active status.
        """
        try:
            markets = self.reader.get_pm_markets(active_only=True)
        except TypeError:
            markets = self.reader.get_pm_markets()
        except Exception:
            return []

        if not markets:
            return []

        result: list[str] = []
        for row in markets:
            if not isinstance(row, dict):
                continue
            mid = self._extract_market_id(row)
            if mid:
                result.append(str(mid))
        return result[: self.config.universe.max_symbols]

    def health_check(self) -> dict[str, Any]:
        """Report public-data source health for PM markets."""
        errors: list[str] = []
        stats: dict[str, Any] = {}

        # Check markets table
        try:
            markets = self.reader.get_pm_markets(active_only=False)
        except TypeError:
            markets = self.reader.get_pm_markets()
        except Exception as exc:
            errors.append(f"get_pm_markets failed: {exc}")
            markets = None

        if markets is not None:
            stats["market_count"] = len(markets)
            active = [m for m in markets if isinstance(m, dict) and m.get("active", True)]
            stats["active_markets"] = len(active)
        else:
            stats["market_count"] = 0
            stats["active_markets"] = 0

        # Check prices table (sample one market if available)
        sample_id = None
        if markets and isinstance(markets, list):
            for m in markets:
                if isinstance(m, dict):
                    sample_id = self._extract_market_id(m)
                    if sample_id:
                        break

        if sample_id:
            try:
                prices = self.reader.get_pm_prices(str(sample_id))
                stats["sample_price_count"] = len(prices) if prices else 0
            except Exception:
                stats["sample_price_count"] = 0
        else:
            stats["sample_price_count"] = 0

        return {
            "market": "pm",
            "status": "ok" if not errors else "degraded",
            "errors": errors,
            "stats": stats,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "mode": "public_data_only",
            "live_clob": False,
        }

    # --- Price helpers --------------------------------------------------------

    @staticmethod
    def _extract_price(row: dict[str, Any]) -> float | None:
        """Extract and clamp a probability price from a row."""
        for key in ("yes_price", "last_price", "price", "implied_probability", "probability"):
            if key in row and row[key] is not None and row[key] != "":
                try:
                    return clamp_probability(float(row[key]))
                except (TypeError, ValueError):
                    continue
        return None

    @classmethod
    def _extract_outcome_price(cls, row: dict[str, Any], outcome: str = "yes") -> float | None:
        """Extract a price for the requested outcome.

        Market rows are canonicalized around YES prices; NO uses explicit
        no_price when present, otherwise the complement of the YES price.
        """
        if str(outcome).strip().lower() == "no":
            for key in ("no_price", "no_last_price", "no_latest_price", "no_market_price"):
                if key in row and row[key] is not None and row[key] != "":
                    try:
                        return clamp_probability(float(row[key]))
                    except (TypeError, ValueError):
                        continue
            yes_price = cls._extract_price(row)
            return None if yes_price is None else clamp_probability(1.0 - yes_price)
        return cls._extract_price(row)

    @staticmethod
    def _extract_market_id(row: dict[str, Any]) -> str | None:
        """Extract market identifier from a row."""
        for key in ("market_id", "id", "slug", "condition_id"):
            if key in row and row[key] not in (None, ""):
                return str(row[key])
        return None

    def _clamp_rows(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Clamp all price values in rows to [0, 1]."""
        price_keys = {"yes_price", "no_price", "last_price", "price", "implied_probability", "probability",
                      "yes_bid", "yes_ask", "no_bid", "no_ask"}
        result: list[dict[str, Any]] = []
        for row in rows:
            clamped = dict(row)
            for key in price_keys.intersection(clamped):
                try:
                    clamped[key] = clamp_probability(float(clamped[key]))
                except (TypeError, ValueError):
                    pass
            result.append(clamped)
        return result

    def _latest_market_row(self, symbol: str) -> dict[str, Any] | None:
        try:
            rows = self.reader.get_pm_markets(limit=500)
        except Exception:
            return None
        if not rows:
            return None
        for row in rows:
            if not isinstance(row, dict):
                continue
            market_id = self._extract_market_id(row)
            if market_id and str(market_id) == str(symbol):
                return row
        return None

    def _query_market_pm_prices(
        self, symbol: str, start: str, end: str
    ) -> list[dict[str, Any]]:
        """Fallback: query market_pm_prices via shared reader when
        get_pm_prices doesn't accept date range parameters."""
        try:
            rows = self.reader.get_pm_prices(symbol)
        except Exception:
            return []

        if not rows:
            return []

        filtered: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            row_date = self._row_date(row)
            if start and row_date < start:
                continue
            if end and row_date > end:
                continue
            filtered.append(row)
        filtered.sort(key=lambda r: self._row_date(r))
        return filtered

    @staticmethod
    def _row_date(row: dict[str, Any]) -> str:
        """Extract date string from a row."""
        for key in ("trade_date", "date", "timestamp", "time"):
            val = row.get(key)
            if val is not None and val != "":
                s = str(val).strip()
                return s[:10] if len(s) >= 10 else s
        return ""


__all__ = ["PMMarketData"]
