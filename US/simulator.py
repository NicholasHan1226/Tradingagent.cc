#!/usr/bin/env python3
"""US Phase D P0 local simulator.

This module intentionally does not import Alpaca or any live broker client.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from shared.markets.base_tools import BaseSimulator
from shared.markets.config_schema import MarketToolConfig
from shared.markets.safety import reject_real_execution_payload
from US.common import USConfig
from US.market_data import USMarketData


class USSimulator(BaseSimulator):
    """Local mock paper-trading simulator for US equities."""

    def __init__(self, config: MarketToolConfig | None = None, market_data: USMarketData | None = None) -> None:
        data = market_data or USMarketData(config=config or USConfig())
        super().__init__("us", config or data.config, data)

    def fill_price(self, symbol: str, date: str) -> float | None:
        return self.market_data.get_latest_price(symbol, date)

    def simulate(self, order: dict[str, Any], account: dict[str, Any]) -> dict[str, Any]:
        reject_real_execution_payload(order, context="USSimulator.simulate.order")
        reject_real_execution_payload(account, context="USSimulator.simulate.account")
        symbol = str(order.get("symbol") or order.get("ts_code") or "").strip().upper()
        trade_date = str(order.get("trade_date") or order.get("date") or "")
        quantity = max(_to_float(order.get("quantity", order.get("qty")), 0.0), 0.0)
        price = self.fill_price(symbol, trade_date) or _to_float(order.get("price"), 0.0)
        side = str(order.get("side") or "buy").strip().lower()
        if not symbol or quantity <= 0 or price <= 0:
            return {
                "status": "rejected",
                "market": "us",
                "symbol": symbol,
                "reason": "missing_symbol_quantity_or_price",
                "broker": "local_mock",
                "capital_layer": "simulated",
                "real_execution": False,
            }
        fee = round(quantity * price * (self.config.fees.taker_bps / 10_000), 6)
        return {
            "status": "filled",
            "market": "us",
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "avg_price": round(price, 6),
            "notional": round(quantity * price, 6),
            "fee": fee,
            "currency": self.config.capital.currency,
            "broker": "local_mock",
            "broker_mode": "no_live_alpaca",
            "capital_layer": "simulated",
            "account_type": "simulated",
            "order_id": str(order.get("order_id") or f"US-SIM-{uuid.uuid4().hex[:12]}"),
            "filled_at": datetime.now(timezone.utc).isoformat(),
            "real_execution": False,
        }


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)
