#!/usr/bin/env python3
"""A-share market adapter for the tradingagent shadow orchestrator."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from shared.data.reader import TradingagentDataReader
from shared.markets.base import MarketAdapter
from Ashare import sim_executor as _sim_executor  # noqa: F401


MARKET = "ashare"
STRATEGY_DIR = Path(__file__).resolve().parent / "strategies"
logger = logging.getLogger(__name__)

DEFAULT_UNIVERSE_FILTER: dict[str, Any] = {
    "exclude_st": True,
    "exclude_suspended": True,
    "exclude_delisted": True,
    "exclude_bse": True,
    "exclude_non_a_share": True,
    "min_list_days": 30,
    "min_liquidity_amount": 50_000_000.0,
}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if result == result else default
    except (TypeError, ValueError):
        return default


def _daily_amount_to_yuan(value: Any) -> float:
    raw = _safe_float(value, -1.0)
    if raw < 0.0:
        return -1.0
    # Tushare daily ``amount`` is stored in thousand CNY in the read model.
    return raw * 1000.0


def _parse_date(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    for fmt in ("%Y%m%d", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(raw[:10], fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _lookback_start(date: str, calendar_days: int = 14) -> str:
    target = _parse_date(date)
    if target is None:
        return ""
    return (target - timedelta(days=calendar_days)).strftime("%Y%m%d")


def _is_st(asset: dict[str, Any]) -> bool:
    name = str(asset.get("name") or "").upper()
    status = str(asset.get("status") or "").upper()
    return "ST" in name or "*ST" in name or "退" in str(asset.get("name") or "") or "ST" in status


def _is_delisted(asset: dict[str, Any]) -> bool:
    status = str(asset.get("status") or "").strip().lower()
    return status in {"delisted", "退市", "d", "inactive"} or "delist" in status


def _is_suspended(asset: dict[str, Any], coverage_status: str | None) -> bool:
    status = str(asset.get("status") or "").strip().lower()
    if status in {"suspended", "halted", "停牌"}:
        return True
    if coverage_status is None:
        return False
    normalized = coverage_status.strip().lower()
    return normalized not in {"normal", "ok", "active", "trading", "covered"}


def _is_bse(asset: dict[str, Any]) -> bool:
    exchange = str(asset.get("exchange") or "").strip().upper()
    symbol = str(asset.get("symbol") or "")
    return exchange in {"BSE", "BJ", "NORTH"} or symbol.startswith(("8", "4"))


def _is_regular_a_share_symbol(symbol: Any) -> bool:
    raw = str(symbol or "").strip().upper()
    if "." in raw:
        digits, exchange = raw.split(".", 1)
    else:
        digits, exchange = raw, ""
    if not re.fullmatch(r"\d{6}", digits):
        return False
    if exchange == "SZ":
        return digits.startswith(("000", "001", "002", "003", "300", "301"))
    if exchange == "SH":
        return digits.startswith(("600", "601", "603", "605", "688", "689"))
    return digits.startswith(("000", "001", "002", "003", "300", "301", "600", "601", "603", "605", "688", "689"))


class AshareAdapter(MarketAdapter):
    """Market-specific adapter for A-share shadow screening and execution."""

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
        assets = self._get_assets()
        if not assets:
            return []
        coverage = self._coverage_by_symbol(date)
        result: list[str] = []
        for asset in assets:
            if not isinstance(asset, dict):
                continue
            symbol = str(asset.get("symbol") or "").strip()
            if not symbol:
                continue
            if self._exclude_asset(asset, coverage.get(symbol), date):
                continue
            result.append(symbol)
        return result

    def map_symbol_to_reader(self, symbol: str) -> tuple[str, str]:
        raw = str(symbol or "").strip().upper()
        return MARKET, raw

    def get_strategy_config(self) -> dict[str, Any]:
        strategies = self._load_strategies()
        return {
            "market": MARKET,
            "sim_capital": 200_000.0,
            "shadow_capital": 200_000.0,
            "portfolio_method": "conviction_weighted",
            "regime": "ashare_default",
            "max_candidates": 20,
            "default_price": 0.0,
            "default_volatility": 0.28,
            "strategies": strategies,
            "market_rules": {
                "settlement": "T+1",
                "can_sell_same_day": False,
                "price_limit": {
                    "main_board": 0.10,
                    "st": 0.05,
                    "star_market": 0.20,
                    "chinext": 0.20,
                    "bse": 0.30,
                },
                "sessions": {
                    "opening_auction": {"start": "09:15", "end": "09:25", "cancel_forbidden_after": "09:20"},
                    "continuous_auction_am": {"start": "09:30", "end": "11:30"},
                    "continuous_auction_pm": {"start": "13:00", "end": "14:57"},
                    "closing_auction": {"start": "14:57", "end": "15:00"},
                },
                "lot_size": 100,
                "currency": "CNY",
                "idle_cash_reverse_repo": "204001",
            },
            "universe_filter": dict(self.universe_filter),
        }

    def get_shadow_account(self) -> str:
        return "ashare_shadow"

    def get_sim_account(self) -> str:
        return "ashare_sim"

    def _get_assets(self) -> list[dict[str, Any]]:
        get_assets = getattr(self.reader, "get_assets", None)
        if callable(get_assets):
            rows = get_assets(market=MARKET)
            if rows:
                return list(rows)
            rows = get_assets(market="Ashare")
            if rows:
                return list(rows)

        shared = getattr(self.reader, "shared", None)
        connect = getattr(shared, "_connect", None)
        if callable(connect):
            for market in (MARKET, "Ashare"):
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

    def _coverage_by_symbol(self, date: str) -> dict[str, str]:
        get_coverage = getattr(self.reader, "get_coverage", None)
        if not callable(get_coverage):
            return {}
        for market in (MARKET, "Ashare"):
            rows = get_coverage(market, date)
            if rows:
                return {
                    str(row.get("symbol") or ""): str(row.get("coverage_status") or "")
                    for row in rows
                    if row.get("symbol")
                }
        return {}

    def _latest_amount(self, symbol: str, date: str) -> float | None:
        has_close, amount = self._latest_liquidity(symbol, date)
        del has_close
        return amount

    def _latest_liquidity(self, symbol: str, date: str) -> tuple[bool, float | None]:
        get_bars = getattr(self.reader, "get_bars_daily", None)
        if not callable(get_bars):
            return False, None
        has_positive_close = False
        start_date = _lookback_start(date)
        for market in (MARKET, "Ashare"):
            rows = get_bars(market, symbol, start_date, date)
            if not rows:
                continue
            for row in reversed(rows):
                if _safe_float(row.get("close"), 0.0) <= 0.0:
                    continue
                has_positive_close = True
                amount_yuan = _daily_amount_to_yuan(row.get("amount"))
                if amount_yuan >= 0:
                    return True, amount_yuan
        return has_positive_close, None

    def _exclude_asset(self, asset: dict[str, Any], coverage_status: str | None, date: str) -> bool:
        cfg = self.universe_filter
        if cfg.get("exclude_st", True) and _is_st(asset):
            return True
        if cfg.get("exclude_delisted", True) and _is_delisted(asset):
            return True
        if cfg.get("exclude_suspended", True) and _is_suspended(asset, coverage_status):
            return True
        if cfg.get("exclude_bse", True) and _is_bse(asset):
            return True
        if cfg.get("exclude_non_a_share", True) and not _is_regular_a_share_symbol(asset.get("symbol")):
            return True

        list_date = _parse_date(asset.get("list_date"))
        target_date = _parse_date(date)
        min_days = int(cfg.get("min_list_days", 30))
        if list_date is not None and target_date is not None:
            if (target_date - list_date).days < min_days:
                return True

        min_amount = _safe_float(cfg.get("min_liquidity_amount"), 50_000_000.0)
        has_close, amount = self._latest_liquidity(str(asset.get("symbol") or ""), date)
        if not has_close:
            return True
        if amount is None:
            # DB error / missing data: keep asset to avoid universe collapse.
            # Only exclude when we have explicit evidence of low liquidity.
            logger.warning(
                "_exclude_asset: no liquidity data for %s on %s — keeping in universe",
                asset.get("symbol"), date,
            )
            return False
        if amount < min_amount:
            return True
        return False

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


__all__ = ["AshareAdapter"]
