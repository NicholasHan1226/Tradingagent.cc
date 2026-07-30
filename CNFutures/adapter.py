#!/usr/bin/env python3
"""China futures adapter for an explicit fixture or TradingDatas V1 port."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from shared.markets.base import MarketAdapter
from shared.markets.sim_capital import default_sim_capital

from . import MARKET
from .contract_rules import is_executable_contract_symbol, normalize_product

READER_MARKET = "Futures"
STRATEGY_DIR = Path(__file__).resolve().parent / "strategies"
DEFAULT_REVIEW_ROOT = Path(__file__).resolve().parents[1] / "shared" / "review"

DEFAULT_UNIVERSE_FILTER: dict[str, Any] = {
    "active_only": True,
    "max_symbols": 1,
    "min_distinct_products": 1,
    # The runnable research lane is intentionally single-product.  RB is a
    # read-only shadow comparator and never belongs to this execution universe.
    "products": ("m",),
}

DEFAULT_STYLES: dict[str, dict[str, Any]] = {
    "commodity_intraday_trend": {
        "name": "commodity_intraday_trend",
        "description": "Day-session-only one-lot commodity trend candidate; simulation only.",
        "style_family": "commodity_intraday_trend",
        "signal_threshold": 0.0015,
        "risk_per_trade": 0.0025,
        "max_margin_usage": 0.10,
        "products": ("m",),
        "momentum_lookback_bars": 3,
        "moving_average_bars": 6,
        "prediction_horizon_bars": 3,
        "time_stop_bars": 3,
        "max_hold_bars": 6,
        "stop_loss_pct": 0.004,
        "take_profit_pct": 0.006,
        "no_overnight": True,
        "day_session_only": True,
        "trend_alignment_required": True,
        "min_volume_ratio": 1.20,
        "open_cooldown_minutes": 20,
        "gap_cooldown_minutes": 30,
        "max_open_gap_pct": 0.01,
        "min_recent_range_pct": 0.0015,
        "min_directional_consistency": 0.60,
        "max_intrabar_reversal_pct": 0.0025,
        "min_signal_to_range_ratio": 0.35,
        "max_bar_gap_minutes": 7,
        "min_body_to_range_ratio": 0.30,
        "min_consecutive_aligned_bars": 2,
        "max_late_chase_pct": 0.008,
        "slippage_bps": 2.0,
        "volume_participation": 0.05,
        "flatten_before_session_close_minutes": 10,
        "rollover_min_days_to_contract_month_start": 5,
    },
}

_ACTIVE_STATUSES = {
    "",
    "1",
    "active",
    "enabled",
    "listed",
    "open",
    "trading",
    "normal",
    "true",
    "yes",
}


def _is_active(asset: dict[str, Any]) -> bool:
    for key in ("active", "is_active", "enabled"):
        if key in asset and asset.get(key) not in (None, ""):
            value = asset.get(key)
            if isinstance(value, str):
                return value.strip().lower() in _ACTIVE_STATUSES
            return bool(value)
    status = (
        str(asset.get("status") or asset.get("market_status") or "").strip().lower()
    )
    return status in _ACTIVE_STATUSES


def _is_executable_contract_symbol(symbol: str) -> bool:
    """Reject generic product symbols such as CU.SHF; simulations need contracts."""

    return is_executable_contract_symbol(symbol)


class CNFuturesAdapter(MarketAdapter):
    """Market boundary for CN futures automated simulation."""

    def __init__(
        self,
        reader: Any | None = None,
        *,
        universe_filter: dict[str, Any] | None = None,
        strategy_dir: Path | None = None,
        styles: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.reader = reader
        self.universe_filter = {
            **DEFAULT_UNIVERSE_FILTER,
            **dict(universe_filter or {}),
        }
        self.strategy_dir = strategy_dir or STRATEGY_DIR
        self._styles_override = styles

    def get_market(self) -> str:
        return MARKET

    def get_universe(self, date: str) -> list[str]:
        assets = self._get_assets()
        asset_by_symbol = {
            str(asset.get("symbol") or asset.get("ts_code") or "")
            .strip()
            .lower(): asset
            for asset in assets
            if isinstance(asset, dict)
            and str(asset.get("symbol") or asset.get("ts_code") or "").strip()
        }
        max_symbols = max(1, int(self.universe_filter.get("max_symbols", 30)))
        allowed_products = {
            str(item).strip().lower()
            for item in self.universe_filter.get("products", ())
            if str(item).strip()
        }

        symbols_with_bars = self._get_symbols_with_bars_from_reader(date)
        if symbols_with_bars:
            selected = self._select_symbols(
                symbols_with_bars,
                asset_by_symbol=asset_by_symbol,
                allowed_products=allowed_products,
                max_symbols=max_symbols,
            )
            if selected:
                return selected

        asset_symbols = [
            str(asset.get("symbol") or asset.get("ts_code") or "").strip()
            for asset in assets
            if isinstance(asset, dict)
        ]
        return self._select_symbols(
            asset_symbols,
            asset_by_symbol=asset_by_symbol,
            allowed_products=allowed_products,
            max_symbols=max_symbols,
        )

    def get_intraday_universe(self, date: str, *, interval: str = "5min") -> list[str]:
        """Prefer contracts with fresh 5-minute bars for intraday simulation."""

        symbols_with_bars = self._get_symbols_with_intraday_bars_from_reader(
            date, interval
        )
        if not symbols_with_bars:
            return self.get_universe(date)
        assets = self._get_assets()
        asset_by_symbol = {
            str(asset.get("symbol") or asset.get("ts_code") or "")
            .strip()
            .lower(): asset
            for asset in assets
            if isinstance(asset, dict)
            and str(asset.get("symbol") or asset.get("ts_code") or "").strip()
        }
        max_symbols = max(1, int(self.universe_filter.get("max_symbols", 30)))
        allowed_products = {
            str(item).strip().lower()
            for item in self.universe_filter.get("products", ())
            if str(item).strip()
        }
        selected = self._select_symbols(
            symbols_with_bars,
            asset_by_symbol=asset_by_symbol,
            allowed_products=allowed_products,
            max_symbols=max_symbols,
        )
        return selected or self.get_universe(date)

    def _select_symbols(
        self,
        candidate_symbols: list[str],
        *,
        asset_by_symbol: dict[str, dict[str, Any]],
        allowed_products: set[str],
        max_symbols: int,
    ) -> list[str]:
        symbols: list[str] = []
        seen: set[str] = set()
        for candidate in candidate_symbols:
            symbol = str(candidate or "").strip()
            symbol_key = symbol.lower()
            if not symbol or symbol_key in seen:
                continue
            if not _is_executable_contract_symbol(symbol):
                continue
            try:
                product = normalize_product(symbol)
            except ValueError:
                continue
            if allowed_products and product not in allowed_products:
                continue
            asset = asset_by_symbol.get(symbol_key, {})
            if self.universe_filter.get("active_only", True) and not _is_active(asset):
                continue
            seen.add(symbol_key)
            symbols.append(symbol)
            if len(symbols) >= max_symbols:
                break
        return symbols

    def _scan_candidate_symbols(self) -> list[str]:
        assets = self._get_assets()
        asset_by_symbol = {
            str(asset.get("symbol") or asset.get("ts_code") or "")
            .strip()
            .lower(): asset
            for asset in assets
            if isinstance(asset, dict)
            and str(asset.get("symbol") or asset.get("ts_code") or "").strip()
        }
        allowed_products = {
            str(item).strip().lower()
            for item in self.universe_filter.get("products", ())
            if str(item).strip()
        }
        scan_limit = max(
            int(self.universe_filter.get("max_symbols", 30)),
            int(os.environ.get("CN_FUTURES_UNIVERSE_SCAN_LIMIT", "200")),
        )
        return self._select_symbols(
            [
                str(asset.get("symbol") or asset.get("ts_code") or "").strip()
                for asset in assets
                if isinstance(asset, dict)
            ],
            asset_by_symbol=asset_by_symbol,
            allowed_products=allowed_products,
            max_symbols=scan_limit,
        )

    def _get_symbols_with_bars_from_reader(self, date: str) -> list[str]:
        if self.reader is None:
            return []
        get_bars_daily = getattr(self.reader, "get_bars_daily", None)
        if not callable(get_bars_daily):
            return []
        selected: list[str] = []
        max_symbols = max(1, int(self.universe_filter.get("max_symbols", 30)))
        for symbol in self._scan_candidate_symbols():
            try:
                rows = get_bars_daily(
                    READER_MARKET, symbol, str(date or ""), str(date or "")
                )
            except Exception:
                rows = []
            if rows:
                selected.append(symbol)
                if len(selected) >= max_symbols:
                    break
        return selected

    def _get_symbols_with_intraday_bars_from_reader(
        self, date: str, interval: str
    ) -> list[str]:
        if self.reader is None:
            return []
        selected = self._get_symbols_from_realtime_batch(date, interval)
        if selected:
            return selected
        get_bars_intraday = getattr(self.reader, "get_bars_intraday", None)
        if callable(get_bars_intraday):
            selected: list[str] = []
            max_symbols = max(1, int(self.universe_filter.get("max_symbols", 30)))
            for symbol in self._scan_candidate_symbols():
                try:
                    rows = get_bars_intraday(
                        READER_MARKET,
                        symbol,
                        interval,
                        str(date or ""),
                        str(date or ""),
                    )
                except Exception:
                    rows = []
                if rows:
                    selected.append(symbol)
                    if len(selected) >= max_symbols:
                        break
            if selected:
                return selected
        return self._get_intraday_symbols_from_reader_read_model(date, interval)

    def _get_symbols_from_realtime_batch(self, date: str, interval: str) -> list[str]:
        get_realtime_5min_batch = getattr(self.reader, "get_realtime_5min_batch", None)
        if not callable(get_realtime_5min_batch):
            return []
        try:
            rows = get_realtime_5min_batch(
                READER_MARKET,
                None,  # Don't filter by exchange active_trade_date; API stores by calendar date
                limit=max(1, int(self.universe_filter.get("max_symbols", 30))) * 4,
            )
        except Exception:
            rows = []
        if not rows:
            return []
        interval_values = (
            {"5m", "5min"} if interval in {"5m", "5min"} else {str(interval)}
        )
        asset_by_symbol = {
            str(asset.get("symbol") or asset.get("ts_code") or "")
            .strip()
            .lower(): asset
            for asset in self._get_assets()
            if isinstance(asset, dict)
            and str(asset.get("symbol") or asset.get("ts_code") or "").strip()
        }
        allowed_products = {
            str(item).strip().lower()
            for item in self.universe_filter.get("products", ())
            if str(item).strip()
        }
        return self._select_symbols(
            [
                str(row.get("symbol") or row.get("ts_code") or "").strip()
                for row in rows
                if isinstance(row, dict)
                and str(row.get("interval") or "5min").strip().lower()
                in interval_values
            ],
            asset_by_symbol=asset_by_symbol,
            allowed_products=allowed_products,
            max_symbols=max(1, int(self.universe_filter.get("max_symbols", 30))),
        )

    def _get_intraday_symbols_from_reader_read_model(
        self, date: str, interval: str
    ) -> list[str]:
        del date, interval
        return []

    def map_symbol_to_reader(self, symbol: str) -> tuple[str, str]:
        return READER_MARKET, str(symbol or "").strip()

    def get_strategy_config(self) -> dict[str, Any]:
        styles = self._load_styles()
        return {
            "market": MARKET,
            "reader_market": READER_MARKET,
            "capital_layer": "simulated",
            "account_type": "simulated",
            "sim_capital": default_sim_capital(MARKET),
            "portfolio_method": "cn_futures_margin_weighted",
            "regime": "cn_futures_daily_simulation",
            "max_candidates": int(self.universe_filter.get("max_symbols", 30)),
            "default_price": 3500.0,
            "default_volatility": 0.35,
            "styles": styles,
            "market_rules": {
                "settlement": "T+0",
                "can_short": True,
                "uses_margin": True,
                "night_session": "by_contract",
                "real_trading_enabled": False,
                "data_owner": "TradingDatas_explicit_fixture_or_v1_port",
                "reader_market": READER_MARKET,
            },
            "universe_filter": dict(self.universe_filter),
            "shadow_research": {
                "products": ("rb",),
                "mode": "read_only_evaluation",
                "execution_eligible": False,
                "simulated_fill_allowed": False,
            },
        }

    def get_shadow_account(self) -> str:
        """Compatibility namespace for the shared MarketAdapter contract.

        CNFutures does not use the shared shadow broker; execution lanes are
        simulated through get_sim_account().
        """

        return "cn_futures_sim"

    def get_sim_account(self) -> dict[str, Any]:
        return {
            "account": "cn_futures_sim",
            "sim_capital": default_sim_capital(MARKET),
            "capital_layer": "simulated",
            "account_type": "simulated",
            "positions": [],
        }

    def _get_assets(self) -> list[dict[str, Any]]:
        if self.reader is None:
            return []
        get_assets = getattr(self.reader, "get_assets", None)
        if callable(get_assets):
            rows = get_assets(market=READER_MARKET)
            filtered = [
                dict(row)
                for row in rows or []
                if dict(row).get("market") in (None, "", READER_MARKET)
            ]
            if filtered:
                return filtered
        return []

    def get_bars_daily(
        self,
        market: str,
        symbol: str,
        start: Any = None,
        end: Any = None,
    ) -> list[dict[str, Any]]:
        """Retired compatibility method; data must come from an injected port."""

        del market, symbol, start, end
        return []

    def get_bars_intraday(
        self,
        market: str,
        symbol: str,
        interval: str = "5min",
        start: Any = None,
        end: Any = None,
    ) -> list[dict[str, Any]]:
        """Retired compatibility method; data must come from an injected port."""

        del market, symbol, interval, start, end
        return []

    def _load_styles(self) -> dict[str, dict[str, Any]]:
        if self._styles_override is not None:
            return {
                str(name): dict(config)
                for name, config in self._styles_override.items()
            }
        styles = {name: dict(config) for name, config in DEFAULT_STYLES.items()}
        # Do not glob-load strategy files: an old JSON file must never silently
        # rejoin the runnable set.  The one checked-in candidate is deliberately
        # named here and fails closed to the immutable default when malformed.
        canonical_path = self.strategy_dir / "commodity_intraday_trend.json"
        try:
            payload = json.loads(canonical_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return self._apply_runtime_styles(styles)
        if str(payload.get("name") or "").strip() == "commodity_intraday_trend":
            styles["commodity_intraday_trend"] = payload
        return self._apply_runtime_styles(styles)

    def _apply_runtime_styles(
        self, styles: dict[str, dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        # Runtime-generated variants and weight overlays are retired.  The
        # checked-in style set remains immutable until a manually reviewed
        # promotion is implemented against SampleJournal/KPI evidence.
        return {str(name): dict(config) for name, config in styles.items()}

__all__ = ["CNFuturesAdapter", "READER_MARKET"]
