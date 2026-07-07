#!/usr/bin/env python3
"""China futures adapter for SharedSignals-backed simulation."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from shared.markets.base import MarketAdapter
from shared.markets.sim_capital import default_sim_capital

from . import MARKET
from .contract_rules import normalize_product

try:  # Optional in partial local checkouts.
    from shared.data.reader import TradingagentDataReader
except Exception:  # pragma: no cover
    TradingagentDataReader = None  # type: ignore[assignment]


READER_MARKET = "Futures"
STRATEGY_DIR = Path(__file__).resolve().parent / "strategies"
DEFAULT_REVIEW_ROOT = Path(__file__).resolve().parents[1] / "shared" / "review"
DEFAULT_SHARED_SIGNALS_DB = Path("/opt/investment/MarketGraphRuntime/read_model/marketdata.sqlite")
DEFAULT_SIM_CAPITAL = default_sim_capital(MARKET)

DEFAULT_UNIVERSE_FILTER: dict[str, Any] = {
    "active_only": True,
    "max_symbols": 30,
    "products": ("rb", "cu", "i", "m", "if", "ih", "ic", "im"),
}

DEFAULT_STYLES: dict[str, dict[str, Any]] = {
    "trend": {
        "name": "trend",
        "description": "5-minute trend-following futures simulation lane",
        "signal_threshold": 0.01,
        "risk_per_trade": 0.03,
        "max_margin_usage": 0.30,
        "products": ("rb", "cu", "i", "m"),
    },
    "breakout": {
        "name": "breakout",
        "description": "Volume-confirmed breakout futures simulation lane",
        "signal_threshold": 0.015,
        "risk_per_trade": 0.02,
        "max_margin_usage": 0.20,
        "products": ("rb", "cu", "i", "m"),
    },
    "mean_reversion": {
        "name": "mean_reversion",
        "description": "Small counter-trend futures simulation lane",
        "signal_threshold": 0.012,
        "risk_per_trade": 0.01,
        "max_margin_usage": 0.10,
        "contrarian": True,
        "products": ("rb", "cu", "i", "m"),
    },
    "index_intraday_directional": {
        "name": "index_intraday_directional",
        "description": "Intraday long/short direction model for China stock index futures; flat-only overnight.",
        "style_family": "index_intraday_directional",
        "signal_threshold": 0.0025,
        "risk_per_trade": 0.01,
        "max_margin_usage": 0.08,
        "products": ("if", "ih", "ic", "im"),
        "momentum_lookback_bars": 3,
        "moving_average_bars": 6,
        "prediction_horizon_bars": 3,
        "no_overnight": True,
        "day_session_only": True,
        "trend_alignment_required": True,
        "min_volume_ratio": 1.05,
        "open_cooldown_minutes": 15,
        "gap_cooldown_minutes": 30,
        "max_open_gap_pct": 0.01,
        "min_recent_range_pct": 0.001,
        "min_directional_consistency": 0.60,
        "max_intrabar_reversal_pct": 0.002,
        "min_signal_to_range_ratio": 0.35,
        "max_bar_gap_minutes": 7,
        "min_body_to_range_ratio": 0.30,
        "min_consecutive_aligned_bars": 2,
        "max_late_chase_pct": 0.012,
        "slippage_bps": 2.0,
        "volume_participation": 0.05,
        "flatten_before_session_close_minutes": 10,
        "rollover_min_days_to_contract_month_start": 5,
    },
}

_ACTIVE_STATUSES = {"", "1", "active", "enabled", "listed", "open", "trading", "normal", "true", "yes"}


def _is_active(asset: dict[str, Any]) -> bool:
    for key in ("active", "is_active", "enabled"):
        if key in asset and asset.get(key) not in (None, ""):
            value = asset.get(key)
            if isinstance(value, str):
                return value.strip().lower() in _ACTIVE_STATUSES
            return bool(value)
    status = str(asset.get("status") or asset.get("market_status") or "").strip().lower()
    return status in _ACTIVE_STATUSES


def _is_executable_contract_symbol(symbol: str) -> bool:
    """Reject generic product symbols such as CU.SHF; simulations need contracts."""

    value = str(symbol or "").strip().lower()
    base = value.split(".", 1)[0]
    try:
        product = normalize_product(value)
    except ValueError:
        return False
    suffix = base[len(product):]
    return suffix.isdigit() and len(suffix) >= 3


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
        self._explicit_reader = reader is not None
        if reader is not None:
            self.reader = reader
        elif TradingagentDataReader is not None:
            self.reader = TradingagentDataReader()
        else:
            self.reader = None
        self.universe_filter = {**DEFAULT_UNIVERSE_FILTER, **dict(universe_filter or {})}
        self.strategy_dir = strategy_dir or STRATEGY_DIR
        self._styles_override = styles

    def get_market(self) -> str:
        return MARKET

    def get_universe(self, date: str) -> list[str]:
        assets = self._get_assets()
        asset_by_symbol = {
            str(asset.get("symbol") or asset.get("ts_code") or "").strip().lower(): asset
            for asset in assets
            if isinstance(asset, dict) and str(asset.get("symbol") or asset.get("ts_code") or "").strip()
        }
        max_symbols = max(1, int(self.universe_filter.get("max_symbols", 30)))
        allowed_products = {
            str(item).strip().lower()
            for item in self.universe_filter.get("products", ())
            if str(item).strip()
        }

        symbols_with_bars = self._get_symbols_with_bars_from_reader(date)
        if not symbols_with_bars and (self.reader is None or self._explicit_reader or self._allow_direct_sqlite_fallback()):
            symbols_with_bars = self._get_symbols_with_bars(date)
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

        symbols_with_bars = self._get_symbols_with_intraday_bars_from_reader(date, interval)
        if not symbols_with_bars and (self.reader is None or self._explicit_reader or self._allow_direct_sqlite_fallback()):
            symbols_with_bars = self._get_symbols_with_intraday_bars(date, interval)
        if not symbols_with_bars:
            return self.get_universe(date)
        assets = self._get_assets()
        asset_by_symbol = {
            str(asset.get("symbol") or asset.get("ts_code") or "").strip().lower(): asset
            for asset in assets
            if isinstance(asset, dict) and str(asset.get("symbol") or asset.get("ts_code") or "").strip()
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
            str(asset.get("symbol") or asset.get("ts_code") or "").strip().lower(): asset
            for asset in assets
            if isinstance(asset, dict) and str(asset.get("symbol") or asset.get("ts_code") or "").strip()
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
                rows = get_bars_daily(READER_MARKET, symbol, str(date or ""), str(date or ""))
            except Exception:
                rows = []
            if rows:
                selected.append(symbol)
                if len(selected) >= max_symbols:
                    break
        return selected

    def _get_symbols_with_intraday_bars_from_reader(self, date: str, interval: str) -> list[str]:
        if self.reader is None:
            return []
        direct = self._get_intraday_symbols_from_reader_read_model(date, interval)
        if direct:
            return direct
        get_bars_intraday = getattr(self.reader, "get_bars_intraday", None)
        if not callable(get_bars_intraday):
            return []
        selected: list[str] = []
        max_symbols = max(1, int(self.universe_filter.get("max_symbols", 30)))
        for symbol in self._scan_candidate_symbols():
            try:
                rows = get_bars_intraday(READER_MARKET, symbol, interval, str(date or ""), str(date or ""))
            except Exception:
                rows = []
            if rows:
                selected.append(symbol)
                if len(selected) >= max_symbols:
                    break
        return selected

    def _get_intraday_symbols_from_reader_read_model(self, date: str, interval: str) -> list[str]:
        shared = getattr(self.reader, "shared", None)
        query = getattr(shared, "_query", None)
        if not callable(query):
            return []
        trade_date = str(date or "").replace("-", "").strip()
        if not trade_date:
            return []
        interval_values = ["5m", "5min"] if interval in {"5m", "5min"} else [interval]
        placeholders = ",".join("?" for _ in interval_values)
        rows = query(
            "WITH scoped AS ("
            "SELECT symbol, MAX(COALESCE(bar_time, '')) AS latest_bar_time "
            "FROM market_bars_intraday "
            f"WHERE market=? AND interval IN ({placeholders}) "
            "AND replace(COALESCE(trade_date,''),'-','')=? "
            "GROUP BY symbol"
            "), latest AS (SELECT MAX(latest_bar_time) AS latest_bar_time FROM scoped) "
            "SELECT scoped.symbol FROM scoped, latest "
            "WHERE scoped.latest_bar_time=latest.latest_bar_time "
            "ORDER BY scoped.symbol ASC",
            (READER_MARKET, *interval_values, trade_date),
        )
        if not rows:
            return []
        max_symbols = max(1, int(self.universe_filter.get("max_symbols", 30)))
        allowed_products = {
            str(item).strip().lower()
            for item in self.universe_filter.get("products", ())
            if str(item).strip()
        }
        return self._select_symbols(
            [str(row.get("symbol") or "").strip() for row in rows if isinstance(row, dict)],
            asset_by_symbol={},
            allowed_products=allowed_products,
            max_symbols=max_symbols,
        )

    def _get_symbols_with_bars(self, date: str) -> list[str]:
        db_path = self._shared_signals_db_path()
        if not db_path.exists():
            return []
        trade_date = str(date or "").strip()
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT DISTINCT symbol
                FROM market_bars_daily
                WHERE market=? AND trade_date=?
                ORDER BY symbol ASC
                """,
                (READER_MARKET, trade_date),
            ).fetchall()
            if rows:
                return [str(row["symbol"]) for row in rows if row["symbol"]]
            rows = conn.execute(
                """
                SELECT DISTINCT symbol
                FROM market_bars_daily
                WHERE market=?
                AND trade_date=(
                    SELECT MAX(trade_date)
                    FROM market_bars_daily
                    WHERE market=? AND trade_date<=?
                )
                ORDER BY symbol ASC
                """,
                (READER_MARKET, READER_MARKET, trade_date),
            ).fetchall()
        except Exception:
            return []
        return [str(row["symbol"]) for row in rows if row["symbol"]]

    def _get_symbols_with_intraday_bars(self, date: str, interval: str) -> list[str]:
        db_path = self._shared_signals_db_path()
        if not db_path.exists():
            return []
        trade_date = str(date or "").replace("-", "").strip()
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT DISTINCT symbol
                FROM market_bars_intraday
                WHERE market=? AND interval=? AND trade_date=?
                ORDER BY symbol ASC
                """,
                (READER_MARKET, interval, trade_date),
            ).fetchall()
            if rows:
                return [str(row["symbol"]) for row in rows if row["symbol"]]
            rows = conn.execute(
                """
                SELECT DISTINCT symbol
                FROM market_bars_intraday
                WHERE market=? AND interval=?
                AND trade_date=(
                    SELECT MAX(trade_date)
                    FROM market_bars_intraday
                    WHERE market=? AND interval=? AND trade_date<=?
                )
                ORDER BY symbol ASC
                """,
                (READER_MARKET, interval, READER_MARKET, interval, trade_date),
            ).fetchall()
        except Exception:
            return []
        return [str(row["symbol"]) for row in rows if row["symbol"]]

    def map_symbol_to_reader(self, symbol: str) -> tuple[str, str]:
        return READER_MARKET, str(symbol or "").strip()

    def get_strategy_config(self) -> dict[str, Any]:
        styles = self._load_styles()
        return {
            "market": MARKET,
            "reader_market": READER_MARKET,
            "capital_layer": "simulated",
            "account_type": "simulated",
            "sim_capital": DEFAULT_SIM_CAPITAL,
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
                "data_owner": "SharedSignals",
                "reader_market": READER_MARKET,
            },
            "universe_filter": dict(self.universe_filter),
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
            "sim_capital": DEFAULT_SIM_CAPITAL,
            "capital_layer": "simulated",
            "account_type": "simulated",
            "positions": [],
        }

    def _get_assets(self) -> list[dict[str, Any]]:
        if self.reader is None:
            return self._get_assets_from_sqlite()
        get_assets = getattr(self.reader, "get_assets", None)
        if callable(get_assets):
            rows = get_assets(market=READER_MARKET)
            filtered = [dict(row) for row in rows or [] if dict(row).get("market") in (None, "", READER_MARKET)]
            if filtered:
                return filtered
        shared = getattr(self.reader, "shared", None)
        connect = getattr(shared, "_connect", None)
        if callable(connect):
            try:
                rows = connect().execute(
                    "SELECT * FROM market_assets WHERE market=? ORDER BY symbol ASC",
                    (READER_MARKET,),
                ).fetchall()
            except Exception:
                return self._get_assets_from_sqlite()
            return [dict(row) for row in rows]
        return self._get_assets_from_sqlite()

    def get_bars_daily(
        self,
        market: str,
        symbol: str,
        start: Any = None,
        end: Any = None,
    ) -> list[dict[str, Any]]:
        """Read daily bars from SharedSignals SQLite when no reader facade exists."""

        del start
        if market != READER_MARKET:
            return []
        db_path = self._shared_signals_db_path()
        if not db_path.exists():
            return []
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT *
                FROM market_bars_daily
                WHERE market=? AND symbol=?
                AND (? IS NULL OR trade_date<=?)
                ORDER BY trade_date DESC
                LIMIT 120
                """,
                (READER_MARKET, symbol, end, end),
            ).fetchall()
        except Exception:
            return []
        return [dict(row) for row in reversed(rows)]

    def get_bars_intraday(
        self,
        market: str,
        symbol: str,
        interval: str = "5min",
        start: Any = None,
        end: Any = None,
    ) -> list[dict[str, Any]]:
        """Read 5-minute futures bars from SharedSignals SQLite."""

        if market != READER_MARKET:
            return []
        db_path = self._shared_signals_db_path()
        if not db_path.exists():
            return []
        trade_date = str(end or start or "").replace("-", "").strip()
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            sql = """
                SELECT *
                FROM market_bars_intraday
                WHERE market=? AND symbol=? AND interval=?
            """
            params: list[Any] = [READER_MARKET, symbol, interval]
            if trade_date:
                sql += " AND trade_date=?"
                params.append(trade_date)
            sql += " ORDER BY bar_time DESC LIMIT 120"
            rows = conn.execute(sql, tuple(params)).fetchall()
        except Exception:
            return []
        return [dict(row) for row in reversed(rows)]

    def _load_styles(self) -> dict[str, dict[str, Any]]:
        if self._styles_override is not None:
            return {str(name): dict(config) for name, config in self._styles_override.items()}
        styles = {name: dict(config) for name, config in DEFAULT_STYLES.items()}
        if not self.strategy_dir.exists():
            return self._apply_runtime_styles(styles)
        for path in sorted(self.strategy_dir.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            name = str(payload.get("name") or path.stem)
            styles[name] = payload
        return self._apply_runtime_styles(styles)

    def _apply_runtime_styles(self, styles: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        review_root = Path(os.environ.get("CN_FUTURES_REVIEW_ROOT") or DEFAULT_REVIEW_ROOT)
        generated_dir = review_root / MARKET / "generated_styles"
        if generated_dir.exists():
            for path in sorted(generated_dir.glob("*.json")):
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if not isinstance(payload, dict):
                    continue
                name = str(payload.get("name") or path.stem)
                if name:
                    styles[name] = payload
        weights_path = review_root / MARKET / "style_weights.json"
        try:
            weight_payload = json.loads(weights_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return styles
        overlays = weight_payload.get("styles") if isinstance(weight_payload, dict) else {}
        if not isinstance(overlays, dict):
            return styles
        for name, overlay in overlays.items():
            if name not in styles or not isinstance(overlay, dict):
                continue
            styles[name].update({
                key: overlay[key]
                for key in ("status", "enabled", "weight", "evolution_action", "evolution_reason", "last_modified")
                if key in overlay
            })
        return styles

    def _shared_signals_db_path(self) -> Path:
        return Path(os.environ.get("SHARED_SIGNALS_DB") or DEFAULT_SHARED_SIGNALS_DB)

    def _allow_direct_sqlite_fallback(self) -> bool:
        value = os.environ.get("CN_FUTURES_ALLOW_DIRECT_SQLITE_FALLBACK", "").strip().lower()
        if value in {"1", "true", "yes", "on"}:
            return True
        configured = Path(os.environ.get("SHARED_SIGNALS_DB") or DEFAULT_SHARED_SIGNALS_DB)
        return configured != DEFAULT_SHARED_SIGNALS_DB

    def _get_assets_from_sqlite(self) -> list[dict[str, Any]]:
        db_path = self._shared_signals_db_path()
        if not db_path.exists():
            return []
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM market_assets WHERE market=? ORDER BY symbol ASC",
                (READER_MARKET,),
            ).fetchall()
        except Exception:
            return []
        return [dict(row) for row in rows]


__all__ = ["CNFuturesAdapter", "READER_MARKET"]
