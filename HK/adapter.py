#!/usr/bin/env python3
"""HK Phase D P0 adapter for universe filtering and symbol mapping."""

from __future__ import annotations

import csv
import os
from pathlib import Path
from typing import Any

from shared.data.reader import TradingagentDataReader
from shared.markets.base import MarketAdapter
from shared.markets.config_schema import MarketToolConfig
from HK.common import HKConfig


READER_MARKETS = ("hk", "HK")
DEFAULT_HK_MASTER = Path(
    os.environ.get(
        "HK_STOCK_MASTER_PATH",
        str(Path(__file__).resolve().parents[2] / "SharedSignals" / "reference" / "hk_stock_master.csv"),
    )
)


class HKAdapter(MarketAdapter):
    """Map HK symbols to SharedSignals and build a basic executable universe."""

    def __init__(
        self,
        config: MarketToolConfig | None = None,
        reader: Any | None = None,
        master_path: Path | str | None = None,
    ) -> None:
        self.config = config or HKConfig()
        self.reader = reader if reader is not None else TradingagentDataReader()
        self.master_path = Path(master_path) if master_path is not None else DEFAULT_HK_MASTER

    def get_market(self) -> str:
        return "hk"

    def normalize_symbol(self, symbol: str) -> str:
        raw = str(symbol or "").strip().upper()
        if raw.endswith(".HK"):
            raw = raw[:-3]
        raw = raw.zfill(5)
        return f"{raw}.HK"

    def to_sharedsignals_symbol(self, symbol: str) -> str:
        return self.normalize_symbol(symbol)

    def map_symbol_to_reader(self, symbol: str) -> tuple[str, str]:
        return "hk", self.to_sharedsignals_symbol(symbol)

    def get_universe(self, date: str) -> list[str]:
        del date
        rows = self._reader_assets() or self._master_assets()
        symbols: list[str] = []
        seen: set[str] = set()
        for row in rows:
            if self.config.universe.active_only and not _is_active(row):
                continue
            symbol = self.normalize_symbol(str(row.get("symbol") or row.get("ts_code") or ""))
            if symbol in seen:
                continue
            seen.add(symbol)
            symbols.append(symbol)
            if len(symbols) >= self.config.universe.max_symbols:
                break
        return symbols

    def get_strategy_config(self) -> dict[str, Any]:
        return {
            "market": "hk",
            "shadow_capital": self.config.capital.initial_capital,
            "currency": self.config.capital.currency,
            "portfolio_method": "conviction_weighted",
            "max_candidates": self.config.universe.max_symbols,
            "market_rules": {
                "exchange": "HKEX",
                "sessions": self.config.sessions,
                "lot_size": 100,
                "currency": "HKD",
            },
        }

    def get_shadow_account(self) -> str:
        return "hk_shadow"

    def get_sim_account(self) -> str:
        return "hk_sim"

    def _reader_assets(self) -> list[dict[str, Any]]:
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

    def _master_assets(self) -> list[dict[str, Any]]:
        if not self.master_path.exists():
            return []
        with self.master_path.open(encoding="utf-8-sig", newline="") as fh:
            return [dict(row) for row in csv.DictReader(fh)]


def _is_active(asset: dict[str, Any]) -> bool:
    status = str(asset.get("status") or asset.get("list_status") or "").strip().lower()
    return not status or status in {"active", "listed", "normal", "ok", "l"}
