#!/usr/bin/env python3
"""Binance-backed simulated executor for Crypto.

This module keeps the execution path simulation-only. Market data lookup is
mockable via ``config["market_data_client"]`` or ``config["binance_client"]``
and never performs a real Binance order placement in tests.
"""

from __future__ import annotations

from typing import Any, Protocol

from shared.execution.sim_broker import SimResult
from shared.execution.sim_executor_registry import register_sim_executor


class _TickerClient(Protocol):
    def get_symbol_ticker(self, *, symbol: str) -> dict[str, Any]:
        """Return Binance-style ticker payload containing ``price``."""


def _resolve_market_data_client(config: dict[str, Any]) -> _TickerClient:
    client = config.get("market_data_client") or config.get("binance_client")
    if client is None:
        raise ValueError("missing Binance market data client")
    getter = getattr(client, "get_symbol_ticker", None)
    if not callable(getter):
        raise TypeError("market data client must expose get_symbol_ticker(symbol=...)")
    return client


def _extract_symbol(order: dict[str, Any]) -> str:
    symbol = str(
        order.get("symbol")
        or order.get("ts_code")
        or order.get("pair")
        or ""
    ).strip().upper()
    if not symbol:
        raise ValueError("order symbol is required")
    return symbol


def _extract_quantity(order: dict[str, Any]) -> float:
    raw_qty = order.get("quantity", order.get("qty", order.get("filled_qty", 0)))
    quantity = float(raw_qty or 0.0)
    if quantity <= 0:
        raise ValueError("order quantity must be positive")
    return quantity


def _extract_price(payload: dict[str, Any], symbol: str) -> float:
    try:
        price = float(payload.get("price"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid Binance price for {symbol}") from exc
    if price <= 0:
        raise ValueError(f"non-positive Binance price for {symbol}")
    return price


def _filled_quantity_value(quantity: float) -> int | float:
    rounded = int(quantity)
    return rounded if abs(quantity - rounded) < 1e-9 else quantity


def crypto_sim_execute(
    order: dict[str, Any],
    account: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
) -> SimResult:
    """Simulate a Binance fill using public market data only."""

    del account
    config = dict(config or {})
    symbol = _extract_symbol(order)
    quantity = _extract_quantity(order)
    client = _resolve_market_data_client(config)
    ticker = client.get_symbol_ticker(symbol=symbol)
    avg_price = _extract_price(dict(ticker or {}), symbol)
    fee_rate = float(config.get("fee_rate", 0.001) or 0.001)
    fee = round(quantity * avg_price * fee_rate, 8)
    order_id = str(order.get("order_id") or f"SIM-CRYPTO-{symbol}")

    return SimResult(
        status="filled",
        filled_qty=_filled_quantity_value(quantity),
        avg_price=avg_price,
        fee=fee,
        message="Simulated Binance market fill",
        capital_layer="simulated",
        account_type="simulated",
        order_id=order_id,
        market="crypto",
        raw_response={
            "symbol": symbol,
            "price": avg_price,
            "quantity": quantity,
            "fee_rate": fee_rate,
            "source": "binance_public_market_data_mock",
        },
    )


register_sim_executor("crypto", crypto_sim_execute)


__all__ = ["crypto_sim_execute"]
