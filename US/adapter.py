#!/usr/bin/env python3
"""US market adapter for the tradingagent shadow orchestrator."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from shared.data.reader import TradingagentDataReader
from shared.markets.base import MarketAdapter
from US import sim_executor as _sim_executor  # noqa: F401


MARKET = "us"
READER_MARKETS = (MARKET, "US")
STRATEGY_DIR = Path(__file__).resolve().parent / "strategies"

DEFAULT_UNIVERSE_FILTER: dict[str, Any] = {
    "active_only": True,
    "target_indices": ("S&P 500", "Nasdaq 100"),
}

_ACTIVE_STATUSES = {"active", "tradable", "normal", "ok", "listed"}
_INDEX_FIELDS = (
    "index_memberships",
    "indices",
    "universe",
    "benchmarks",
    "tags",
    "category",
    "index",
)
_TARGET_INDEX_HINTS = (
    "s&p500",
    "s&p 500",
    "sp500",
    "s and p 500",
    "nasdaq100",
    "nasdaq 100",
    "ndx",
    "qqq",
)


def _normalize_text(value: Any) -> str:
    return (
        str(value or "")
        .strip()
        .lower()
        .replace("_", " ")
        .replace("-", " ")
    )


def _iter_membership_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, dict):
        values: list[str] = []
        for key, nested in value.items():
            values.extend(_iter_membership_values(key))
            values.extend(_iter_membership_values(nested))
        return values
    if isinstance(value, (list, tuple, set)):
        values = []
        for item in value:
            values.extend(_iter_membership_values(item))
        return values
    raw = str(value).strip()
    if not raw:
        return []
    if raw.startswith("[") or raw.startswith("{"):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
        if parsed is not None:
            return _iter_membership_values(parsed)
    parts = [part.strip() for part in raw.replace("|", ",").replace(";", ",").split(",")]
    return [part for part in parts if part]


def _is_active(asset: dict[str, Any]) -> bool:
    active_flag = asset.get("active")
    if isinstance(active_flag, bool):
        return active_flag
    if isinstance(active_flag, (int, float)):
        return bool(active_flag)
    if isinstance(active_flag, str) and active_flag.strip():
        return active_flag.strip().lower() in {"1", "true", "yes", "y", "active"}

    tradable_flag = asset.get("tradable")
    if isinstance(tradable_flag, bool):
        return tradable_flag
    if isinstance(tradable_flag, str) and tradable_flag.strip():
        return tradable_flag.strip().lower() in {"1", "true", "yes", "y", "tradable"}

    status = _normalize_text(asset.get("status"))
    return not status or status in _ACTIVE_STATUSES


def _is_target_index_member(asset: dict[str, Any]) -> bool:
    for key in ("is_sp500", "is_sp_500", "in_sp500", "is_nasdaq100", "is_nasdaq_100", "in_nasdaq100"):
        value = asset.get(key)
        if isinstance(value, bool) and value:
            return True
        if isinstance(value, (int, float)) and value == 1:
            return True
        if isinstance(value, str) and value.strip().lower() in {"1", "true", "yes", "y"}:
            return True

    for field in _INDEX_FIELDS:
        for item in _iter_membership_values(asset.get(field)):
            normalized = _normalize_text(item)
            if any(hint in normalized for hint in _TARGET_INDEX_HINTS):
                return True
    return False


class USAdapter(MarketAdapter):
    """Market-specific adapter for US shadow screening and execution."""

    def __init__(
        self,
        reader: Any | None = None,
        *,
        universe_filter: dict[str, Any] | None = None,
        strategy_dir: Path | None = None,
    ) -> None:
        self.reader = reader if reader is not None else TradingagentDataReader()
        self.universe_filter = {**DEFAULT_UNIVERSE_FILTER, **dict(universe_filter or {})}
        self.strategy_dir = strategy_dir or STRATEGY_DIR

    def get_market(self) -> str:
        return MARKET

    def get_universe(self, date: str) -> list[str]:
        del date
        assets = self._get_assets()
        if not assets:
            return []

        active_assets = [asset for asset in assets if isinstance(asset, dict) and _is_active(asset)]
        target_assets = [asset for asset in active_assets if _is_target_index_member(asset)]
        selected_assets = target_assets or active_assets

        universe: list[str] = []
        seen: set[str] = set()
        for asset in selected_assets:
            symbol = str(asset.get("symbol") or "").strip().upper()
            if not symbol or symbol in seen:
                continue
            seen.add(symbol)
            universe.append(symbol)
        return universe

    def map_symbol_to_reader(self, symbol: str) -> tuple[str, str]:
        return MARKET, str(symbol or "").strip().upper()

    def get_strategy_config(self) -> dict[str, Any]:
        strategies = self._load_strategies()
        return {
            "market": MARKET,
            "shadow_capital": 200_000.0,
            "portfolio_method": "conviction_weighted",
            "regime": "us_equity_default",
            "max_candidates": 25,
            "default_price": 100.0,
            "default_volatility": 0.24,
            "strategies": strategies,
            "market_rules": {
                "settlement": "T+2",
                "can_sell_same_day": True,
                "extended_hours": True,
                "pdt_guardrail": {
                    "window_days": 5,
                    "max_day_trades": 3,
                    "minimum_equity_usd": 25_000.0,
                    "applies_to_shadow": False,
                },
                "sessions": {
                    "premarket": {"start": "16:00", "end": "21:30", "timezone": "Asia/Shanghai"},
                    "regular": {"start": "21:30", "end": "04:00", "timezone": "Asia/Shanghai"},
                    "after_hours": {"start": "04:00", "end": "08:00", "timezone": "Asia/Shanghai"},
                },
                "lot_size": 1,
                "currency": "USD",
            },
            "universe_filter": dict(self.universe_filter),
        }

    def get_shadow_account(self) -> str:
        return "us_shadow"

    def get_sim_account(self) -> str:
        return "us_sim"

    def _get_assets(self) -> list[dict[str, Any]]:
        get_assets = getattr(self.reader, "get_assets", None)
        if callable(get_assets):
            for market in READER_MARKETS:
                rows = get_assets(market=market)
                if rows:
                    return list(rows)

        shared = getattr(self.reader, "shared", None)
        connect = getattr(shared, "_connect", None)
        if callable(connect):
            for market in READER_MARKETS:
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


__all__ = ["USAdapter"]
