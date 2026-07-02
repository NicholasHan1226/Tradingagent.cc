#!/usr/bin/env python3
"""PM Phase D simulator — local mock, probability settlement, NO live CLOB.

Simulates YES/NO bet execution in probability space [0, 1]. Never connects
to Polymarket, never routes real orders. Shadow/simulated only.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from shared.markets.base_tools import BaseSimulator
from shared.markets.config_schema import MarketToolConfig
from shared.markets.safety import assert_no_real_execution, reject_real_execution_payload

from PM.common import assert_probability, clamp_probability
from PM.market_data import PMMarketData


class PMSimulator(BaseSimulator):
    """Local mock simulator for Polymarket probability bets.

    Executes simulated YES/NO orders with probability-domain settlement.
    No live CLOB — all fills are computed locally from market prices
    clamped to [0, 1].

    Order format:
        {
            "market_id": str,       # Polymarket condition/market ID
            "side": "buy" | "sell",
            "outcome": "yes" | "no",
            "quantity": int,        # number of shares (default 1)
            "price": float,         # limit price [0, 1]
        }
    """

    # Allowed values
    _VALID_SIDES = {"buy", "sell"}
    _VALID_OUTCOMES = {"yes", "no"}
    _MIN_PRICE = 0.0
    _MAX_PRICE = 1.0

    def __init__(
        self,
        config: MarketToolConfig | None = None,
        market_data: PMMarketData | None = None,
    ) -> None:
        if market_data is None:
            market_data = PMMarketData(config)
        if config is None:
            from PM.common import load_pm_config
            config = load_pm_config().to_market_tool_config()

        super().__init__(market="pm", config=config, market_data=market_data)

    def validate_config(self) -> None:
        """Double-verify no real execution path exists."""
        super().validate_config()
        assert_no_real_execution(self.config)
        if getattr(self.config.safety, "real_money_enabled", False):
            raise RuntimeError("PM simulator: real_money_enabled must be False")
        if getattr(self.config.safety, "live_broker_enabled", False):
            raise RuntimeError("PM simulator: live_broker_enabled must be False")

    # --- Abstract method implementations --------------------------------------

    def simulate(
        self, order: dict[str, Any], account: dict[str, Any]
    ) -> dict[str, Any]:
        """Simulate an order against a shadow/simulated account.

        Returns a fill record with probability-clamped prices.
        """
        order = dict(order or {})
        account = dict(account or {})
        reject_real_execution_payload(order, context="PMSimulator.order")
        for key in ("account_type", "execution_mode", "mode", "broker_mode"):
            value = str(account.get(key) or "").strip().lower()
            if value in {"real", "live", "broker", "exchange"}:
                raise RuntimeError("PMSimulator.account: real/live execution is rejected in simulated market tools")

        # Validate and extract order fields
        side = str(order.get("side", "buy")).lower().strip()
        if side not in self._VALID_SIDES:
            raise ValueError(f"PM side must be buy or sell, got {side!r}")

        outcome = str(order.get("outcome", "yes")).lower().strip()
        if outcome not in self._VALID_OUTCOMES:
            raise ValueError(f"PM outcome must be yes or no, got {outcome!r}")

        market_id = str(order.get("market_id") or order.get("symbol") or "")
        if not market_id:
            raise ValueError("order must include market_id or symbol")

        qty = int(order.get("quantity", order.get("qty", 1)))
        if qty <= 0:
            raise ValueError(f"quantity must be positive, got {qty}")

        limit_price = assert_probability(
            order.get("price", order.get("limit_price", 0.5)),
            "limit_price",
        )

        # Resolve fill price
        fill_price = self._resolve_fill_price(order, limit_price, side)

        # Apply settlement logic
        settlement = self._compute_settlement(outcome, fill_price, qty)

        order_id = str(order.get("order_id", f"PM-SIM-{uuid.uuid4().hex[:12]}"))
        account_id = str(account.get("account_id", "pm_shadow"))

        return {
            "order_id": order_id,
            "market_id": market_id,
            "market": "pm",
            "side": side,
            "outcome": outcome.upper(),
            "quantity": qty,
            "limit_price": round(limit_price, 4),
            "fill_price": round(fill_price, 4),
            "status": "filled",
            "account_id": account_id,
            "capital_layer": "simulated",
            "account_type": "simulated",
            "fee": 0.0,
            "settlement": settlement,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "mode": "simulated",
            "live_clob": False,
        }

    def fill_price(self, symbol: str, date: str) -> float | None:
        """Return the latest YES price for a market as the simulated fill.

        Falls back to 0.5 (maximum entropy) when no data is available.
        """
        price = self.market_data.get_latest_price(symbol, date)
        if price is not None:
            return clamp_probability(price)
        # Maximum entropy default when no data
        return 0.5

    # --- Internal methods -----------------------------------------------------

    def _resolve_fill_price(
        self,
        order: dict[str, Any],
        limit_price: float,
        side: str,
    ) -> float:
        """Resolve the simulated fill price from market data or order price."""
        market_id = str(order.get("market_id") or order.get("symbol") or "")
        # Use current time as date for latest price lookup
        date = str(order.get("date") or order.get("as_of") or
                   datetime.now(timezone.utc).strftime("%Y-%m-%d"))

        market_price = self.fill_price(market_id, date)

        # Simple spread simulation: buy fills at market + small spread, sell at market - spread
        spread = 0.005  # 0.5% default spread
        if side == "buy":
            raw = (market_price + spread) if market_price is not None else limit_price
        else:
            raw = (market_price - spread) if market_price is not None else limit_price

        # Clamp and respect limit price
        fill = clamp_probability(raw)
        if side == "buy":
            fill = min(fill, limit_price)
        else:
            fill = max(fill, limit_price)

        return fill

    @staticmethod
    def _compute_settlement(
        outcome: str,
        fill_price: float,
        qty: int,
    ) -> dict[str, Any]:
        """Compute expected P&L for probability settlement.

        YES shares: worth 1.0 if correct, 0.0 if incorrect.
        NO shares: worth 1.0 if correct, 0.0 if incorrect.
        P&L is theoretical max if outcome resolves correctly.
        """
        fill_price = clamp_probability(fill_price)
        cost = fill_price * qty
        max_payout = 1.0 * qty
        profit_if_correct = max_payout - cost
        loss_if_wrong = -cost

        return {
            "mechanism": "probability_settlement",
            "outcome": outcome.upper(),
            "cost": round(cost, 4),
            "max_payout": round(max_payout, 4),
            "profit_if_correct": round(profit_if_correct, 4),
            "loss_if_wrong": round(loss_if_wrong, 4),
            "probability_at_fill": round(fill_price, 4),
        }


__all__ = ["PMSimulator"]
