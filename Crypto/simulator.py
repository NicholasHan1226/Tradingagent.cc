#!/usr/bin/env python3
"""Local mock simulator for Crypto Phase D."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from Crypto.common import CryptoConfig, load_crypto_config, reject_real_execution_payload
from Crypto.market_data import CryptoMarketData
from shared.markets.base_tools import BaseSimulator


class CryptoSimulator(BaseSimulator):
    """Simulate Crypto fills from public bars without signed Binance access."""

    def __init__(
        self,
        config: CryptoConfig | None = None,
        market_data: CryptoMarketData | None = None,
    ) -> None:
        resolved_config = config or load_crypto_config()
        super().__init__("crypto", resolved_config, market_data or CryptoMarketData(resolved_config))

    def validate_config(self) -> None:
        super().validate_config()
        reject_real_execution_payload(
            {
                "capital_layer": self.config.capital.default_layer,
                "live_broker": self.config.safety.live_broker_enabled,
            },
            context="CryptoSimulator.config",
        )

    def simulate(self, order: dict[str, Any], account: dict[str, Any]) -> dict[str, Any]:
        reject_real_execution_payload(order, context="CryptoSimulator.order")
        reject_real_execution_payload(account, context="CryptoSimulator.account")

        symbol = self._symbol(order)
        side = str(order.get("side") or order.get("direction") or "buy").strip().lower()
        if side not in {"buy", "sell"}:
            raise ValueError(f"unsupported Crypto side: {side}")
        quantity = self._positive_float(order.get("quantity", order.get("qty")), "quantity")
        date = str(order.get("trade_date") or order.get("date") or "")
        price = self.fill_price(symbol, date)
        if price is None or price <= 0:
            raise ValueError(
                f"no provider-neutral Crypto market evidence for {symbol} at {date}; "
                "order-price fallback is retired"
            )

        notional = quantity * price
        fee = round(notional * (float(self.config.fees.taker_bps) / 10000.0), 8)
        return {
            "status": "filled",
            "market": "crypto",
            "symbol": symbol,
            "side": side,
            "filled_qty": quantity,
            "avg_price": price,
            "fee": fee,
            "notional": round(notional, 8),
            "currency": self.config.capital.currency,
            "capital_layer": "simulated",
            "account_type": "simulated",
            "order_id": str(order.get("order_id") or f"SIM-CRYPTO-{symbol}-{date or 'latest'}"),
            "filled_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": "local_public_bar_mock",
            "real_execution": False,
        }

    def fill_price(self, symbol: str, date: str) -> float | None:
        return self.market_data.get_latest_price(symbol, date)

    @staticmethod
    def _symbol(order: dict[str, Any]) -> str:
        symbol = str(order.get("symbol") or order.get("ts_code") or order.get("pair") or "").strip().upper()
        if not symbol:
            raise ValueError("Crypto order symbol is required")
        return symbol

    @staticmethod
    def _optional_positive_float(value: Any) -> float | None:
        try:
            result = float(value)
        except (TypeError, ValueError):
            return None
        return result if result > 0 and result == result else None

    @staticmethod
    def _positive_float(value: Any, name: str) -> float:
        try:
            result = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Crypto order {name} must be positive") from exc
        if result <= 0:
            raise ValueError(f"Crypto order {name} must be positive")
        return result


__all__ = ["CryptoSimulator"]
