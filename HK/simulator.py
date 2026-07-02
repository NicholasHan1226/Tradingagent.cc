#!/usr/bin/env python3
"""HK Phase D P0 local simulator with no broker integration."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from shared.markets.base_tools import BaseSimulator
from shared.markets.config_schema import MarketToolConfig
from HK.common import HKConfig
from HK.market_data import HKMarketData


class HKSimulator(BaseSimulator):
    """Local mock simulator for HK equities."""

    def __init__(self, config: MarketToolConfig | None = None, market_data: HKMarketData | None = None) -> None:
        data = market_data or HKMarketData(config=config or HKConfig())
        super().__init__("hk", config or data.config, data)

    def fill_price(self, symbol: str, date: str) -> float | None:
        return self.market_data.get_latest_price(symbol, date)

    def simulate(self, order: dict[str, Any], account: dict[str, Any]) -> dict[str, Any]:
        symbol = str(order.get("symbol") or order.get("ts_code") or "")
        trade_date = str(order.get("trade_date") or order.get("date") or "")
        quantity = max(_to_float(order.get("quantity", order.get("qty")), 0.0), 0.0)
        price = self.fill_price(symbol, trade_date) or _to_float(order.get("price"), 0.0)
        if not symbol or quantity <= 0 or price <= 0:
            return {
                "status": "rejected",
                "market": "hk",
                "symbol": symbol,
                "reason": "missing_symbol_quantity_or_price",
                "broker": "local_mock",
                "capital_layer": "simulated",
                "real_execution": False,
            }
        fee = round(quantity * price * (self.config.fees.taker_bps / 10_000), 6)
        return {
            "status": "filled",
            "market": "hk",
            "symbol": self.market_data.adapter.normalize_symbol(symbol),
            "side": str(order.get("side") or "buy").strip().lower(),
            "quantity": quantity,
            "avg_price": round(price, 6),
            "notional": round(quantity * price, 6),
            "fee": fee,
            "currency": self.config.capital.currency,
            "broker": "local_mock",
            "broker_mode": "no_broker",
            "capital_layer": "simulated",
            "account": dict(account or {}),
            "order_id": str(order.get("order_id") or f"HK-SIM-{uuid.uuid4().hex[:12]}"),
            "filled_at": datetime.now(timezone.utc).isoformat(),
            "real_execution": False,
        }


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)
