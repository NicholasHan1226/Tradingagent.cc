#!/usr/bin/env python3
"""Production-grade simulated execution engine.

This module is intentionally shaped like a real execution adapter while staying
paper-only. It models order state, fills, commissions, slippage and positions so
the same order objects can later be mapped to a real broker under the explicit
real-trading gate.
"""

from __future__ import annotations

import json
import math
import random
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shared.execution.real_trading_gate import validate_real_trading_enabled
from shared.markets.safety import reject_real_execution_payload


ORDER_STATES = {"pending", "open", "partial", "filled", "cancelled", "rejected"}
ORDER_TYPES = {"market", "limit"}
SIDES = {"buy", "sell"}


DEFAULT_MARKET_MODELS: dict[str, dict[str, Any]] = {
    "ashare": {
        "commission_bps": 1.0,
        "stamp_duty_sell_bps": 2.5,
        "min_commission": 0.0,
        "base_slippage_bps": 2.0,
        "volatility_slippage_multiplier": 0.18,
        "volume_impact_bps": 8.0,
        "price_improvement_bps": 0.5,
        "queue_ahead_ratio": 0.35,
        "price_tick": 0.01,
        "lot_size": 100,
        "enforce_buy_lot": True,
        "price_limit_pct": 0.10,
        "bar_participation_cap": 0.10,
        "default_counterparty": "simulated_ashare_book",
    },
    "crypto": {
        "commission_bps": 4.0,
        "stamp_duty_sell_bps": 0.0,
        "min_commission": 0.0,
        "base_slippage_bps": 1.5,
        "volatility_slippage_multiplier": 0.12,
        "volume_impact_bps": 6.0,
        "price_improvement_bps": 0.8,
        "queue_ahead_ratio": 0.2,
        "bar_participation_cap": 0.05,
        "default_counterparty": "simulated_crypto_book",
    },
    "us": {
        "commission_bps": 0.5,
        "stamp_duty_sell_bps": 0.0,
        "min_commission": 0.0,
        "base_slippage_bps": 1.0,
        "volatility_slippage_multiplier": 0.1,
        "volume_impact_bps": 4.0,
        "price_improvement_bps": 0.4,
        "queue_ahead_ratio": 0.25,
        "price_tick": 0.01,
        "bar_participation_cap": 0.05,
        "default_counterparty": "simulated_us_book",
    },
    "pm": {
        "commission_bps": 2.0,
        "stamp_duty_sell_bps": 0.0,
        "min_commission": 0.0,
        "base_slippage_bps": 4.0,
        "volatility_slippage_multiplier": 0.08,
        "volume_impact_bps": 10.0,
        "price_improvement_bps": 0.0,
        "queue_ahead_ratio": 0.5,
        "price_tick": 0.001,
        "min_price": 0.001,
        "max_price": 0.999,
        "default_counterparty": "simulated_pm_book",
    },
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _market_key(value: str | None) -> str:
    key = str(value or "").strip().lower()
    return "ashare" if key in {"a", "a-share", "a_share"} else key


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
        return parsed if parsed == parsed else default
    except (TypeError, ValueError):
        return default


def _safe_qty(value: Any) -> float:
    qty = _safe_float(value, 0.0)
    return qty if qty > 0 else 0.0


def _first_positive(mapping: dict[str, Any], *keys: str, default: float = 0.0) -> float:
    for key in keys:
        value = _safe_float(mapping.get(key), 0.0)
        if value > 0:
            return value
    return default


def _round_price(value: float, tick: float, *, side: str) -> float:
    if tick <= 0:
        return round(value, 8)
    units = value / tick
    rounded = math.ceil(units - 1e-12) * tick if side == "buy" else math.floor(units + 1e-12) * tick
    return round(rounded, 8)


def _load_profile(profile: dict[str, Any] | str | Path | None) -> dict[str, Any]:
    if profile is None:
        return {}
    if isinstance(profile, (str, Path)):
        return json.loads(Path(profile).read_text(encoding="utf-8"))
    return dict(profile)


@dataclass
class SimOrder:
    symbol: str
    side: str
    quantity: float
    limit_price: float
    order_type: str = "market"
    time_in_force: str = "day"
    market: str = "ashare"
    order_id: str = field(default_factory=lambda: f"SIM-{uuid.uuid4().hex[:12]}")
    submitted_at: str = field(default_factory=_now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.symbol = str(self.symbol or "").strip()
        self.side = str(self.side or "").strip().lower()
        self.order_type = str(self.order_type or "market").strip().lower()
        self.market = _market_key(self.market)
        self.quantity = _safe_qty(self.quantity)
        self.limit_price = _safe_float(self.limit_price, 0.0)
        payload = asdict(self)
        reject_real_execution_payload(payload, context=f"SimOrder.{self.market or 'unknown'}")
        if not self.symbol:
            raise ValueError("symbol is required")
        if self.side not in SIDES:
            raise ValueError(f"side must be one of {sorted(SIDES)}, got {self.side}")
        if self.order_type not in ORDER_TYPES:
            raise ValueError(f"order_type must be one of {sorted(ORDER_TYPES)}, got {self.order_type}")
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        if self.limit_price <= 0:
            raise ValueError("limit_price must be positive")

    @classmethod
    def from_signal(cls, signal: dict[str, Any], *, market: str) -> "SimOrder":
        symbol = str(signal.get("symbol") or signal.get("ts_code") or signal.get("market_id") or "").strip()
        quantity = signal.get("quantity", signal.get("shares", signal.get("target_quantity", 0.0)))
        if _safe_qty(quantity) <= 0:
            amount = _safe_float(signal.get("amount", signal.get("notional")), 0.0)
            price = _safe_float(signal.get("price", signal.get("limit_price")), 0.0)
            quantity = amount / price if amount > 0 and price > 0 else 0.0
        return cls(
            symbol=symbol,
            side=str(signal.get("side") or "buy"),
            quantity=quantity,
            limit_price=_safe_float(signal.get("limit_price", signal.get("price")), 0.0),
            order_type=str(signal.get("order_type") or "market"),
            time_in_force=str(signal.get("time_in_force") or "day"),
            market=market,
            order_id=str(signal.get("order_id") or f"SIM-{market}-{symbol}-{uuid.uuid4().hex[:8]}"),
            metadata=dict(signal),
        )

    def to_real(self) -> dict[str, Any]:
        validate_real_trading_enabled()
        return {
            "adapter": "real_order_placeholder",
            "order_id": self.order_id,
            "symbol": self.symbol,
            "side": self.side,
            "quantity": self.quantity,
            "limit_price": self.limit_price,
            "order_type": self.order_type,
            "time_in_force": self.time_in_force,
            "market": self.market,
            "requires_manual_broker_mapping": True,
        }


@dataclass
class SimFill:
    order_id: str
    fill_price: float
    fill_qty: float
    fill_time: str
    slippage_bps: float
    counterparty: str
    fill_id: str = field(default_factory=lambda: f"FILL-{uuid.uuid4().hex[:12]}")

    def to_real(self) -> dict[str, Any]:
        validate_real_trading_enabled()
        return {
            "adapter": "real_fill_placeholder",
            "fill_id": self.fill_id,
            "order_id": self.order_id,
            "fill_price": self.fill_price,
            "fill_qty": self.fill_qty,
            "fill_time": self.fill_time,
            "requires_broker_receipt_checksum": True,
        }


@dataclass
class SimPosition:
    symbol: str
    current_holdings: float = 0.0
    avg_cost: float = 0.0
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    mark_price: float = 0.0

    def to_real(self) -> dict[str, Any]:
        validate_real_trading_enabled()
        return {
            "adapter": "real_position_placeholder",
            "symbol": self.symbol,
            "quantity": self.current_holdings,
            "avg_cost": self.avg_cost,
            "mark_price": self.mark_price,
            "read_only_reconcile_required": True,
        }


@dataclass
class SimOrderRecord:
    order: SimOrder
    state: str = "pending"
    fills: list[SimFill] = field(default_factory=list)
    filled_qty: float = 0.0
    avg_fill_price: float = 0.0
    fees: dict[str, float] = field(default_factory=dict)
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "order": asdict(self.order),
            "state": self.state,
            "fills": [asdict(fill) for fill in self.fills],
            "filled_qty": round(self.filled_qty, 8),
            "avg_fill_price": round(self.avg_fill_price, 8),
            "fees": self.fees,
            "reason": self.reason,
            "capital_layer": "simulated",
            "account_type": "simulated",
            "real_execution": False,
        }


class CommissionModel:
    def __init__(self, market: str, profile: dict[str, Any] | str | Path | None = None) -> None:
        self.market = _market_key(market)
        config = dict(DEFAULT_MARKET_MODELS.get(self.market, DEFAULT_MARKET_MODELS["ashare"]))
        config.update(_load_profile(profile).get("commission", _load_profile(profile)))
        self.config = config

    def calculate(self, side: str, notional: float) -> dict[str, float]:
        commission = max(
            notional * _safe_float(self.config.get("commission_bps"), 0.0) / 10_000.0,
            _safe_float(self.config.get("min_commission"), 0.0),
        )
        stamp = 0.0
        if str(side).lower() == "sell":
            stamp = notional * _safe_float(self.config.get("stamp_duty_sell_bps"), 0.0) / 10_000.0
        total = round(commission + stamp, 8)
        return {
            "commission": round(commission, 8),
            "stamp_duty": round(stamp, 8),
            "total": total,
        }

    def to_real(self) -> dict[str, Any]:
        validate_real_trading_enabled()
        return {
            "adapter": "real_commission_model_placeholder",
            "market": self.market,
            "config": dict(self.config),
            "requires_broker_fee_schedule": True,
        }


class VolatilitySlippageModel:
    def __init__(self, market: str, profile: dict[str, Any] | str | Path | None = None) -> None:
        self.market = _market_key(market)
        config = dict(DEFAULT_MARKET_MODELS.get(self.market, DEFAULT_MARKET_MODELS["ashare"]))
        config.update(_load_profile(profile).get("slippage", _load_profile(profile)))
        self.config = config

    def estimate_bps(self, order: SimOrder, market_snapshot: dict[str, Any]) -> float:
        volatility_bps = _safe_float(market_snapshot.get("volatility_bps"), 0.0)
        if volatility_bps <= 0:
            volatility = _safe_float(market_snapshot.get("volatility"), 0.0)
            volatility_bps = volatility * 10_000.0 if volatility <= 1.0 else volatility
        if order.side == "buy":
            available_qty = _first_positive(market_snapshot, "ask_size", "ask_qty", "best_ask_size", "available_qty", default=0.0)
        else:
            available_qty = _first_positive(market_snapshot, "bid_size", "bid_qty", "best_bid_size", "available_qty", default=0.0)
        if available_qty <= 0:
            bar_volume = _first_positive(market_snapshot, "bar_volume", "volume", "vol", default=0.0)
            if bar_volume > 0:
                available_qty = bar_volume * _safe_float(self.config.get("bar_participation_cap"), 1.0)
        available_qty = max(available_qty or order.quantity, 1.0)
        participation = min(1.0, order.quantity / available_qty)
        bps = (
            _safe_float(self.config.get("base_slippage_bps"), 0.0)
            + volatility_bps * _safe_float(self.config.get("volatility_slippage_multiplier"), 0.0)
            + participation * _safe_float(self.config.get("volume_impact_bps"), 0.0)
        )
        return round(max(0.0, bps), 6)

    def price(self, order: SimOrder, market_snapshot: dict[str, Any]) -> tuple[float, float]:
        if order.side == "buy":
            mid = _first_positive(
                market_snapshot,
                "ask_price",
                "best_ask",
                "ask",
                "mid_price",
                "last_price",
                "close",
                default=order.limit_price,
            )
        else:
            mid = _first_positive(
                market_snapshot,
                "bid_price",
                "best_bid",
                "bid",
                "mid_price",
                "last_price",
                "close",
                default=order.limit_price,
            )
        slippage_bps = self.estimate_bps(order, market_snapshot)
        improvement_bps = _safe_float(self.config.get("price_improvement_bps"), 0.0)
        effective_bps = max(0.0, slippage_bps - improvement_bps)
        direction = 1.0 if order.side == "buy" else -1.0
        fill_price = mid * (1.0 + direction * effective_bps / 10_000.0)
        if order.order_type == "limit":
            if order.side == "buy":
                fill_price = min(fill_price, order.limit_price)
            else:
                fill_price = max(fill_price, order.limit_price)
        min_price = _safe_float(self.config.get("min_price"), 0.0)
        max_price = _safe_float(self.config.get("max_price"), 0.0)
        if min_price > 0:
            fill_price = max(min_price, fill_price)
        if max_price > 0:
            fill_price = min(max_price, fill_price)
        fill_price = _round_price(fill_price, _safe_float(self.config.get("price_tick"), 0.0), side=order.side)
        if order.order_type == "limit":
            if order.side == "buy":
                fill_price = min(fill_price, order.limit_price)
            else:
                fill_price = max(fill_price, order.limit_price)
        if min_price > 0:
            fill_price = max(min_price, fill_price)
        if max_price > 0:
            fill_price = min(max_price, fill_price)
        return round(fill_price, 8), round(slippage_bps - improvement_bps, 6)

    def to_real(self) -> dict[str, Any]:
        validate_real_trading_enabled()
        return {
            "adapter": "real_market_impact_model_placeholder",
            "market": self.market,
            "config": dict(self.config),
            "requires_live_order_book_calibration": True,
        }


class SimExecutionEngine:
    """Stateful paper execution engine with real-grade order semantics."""

    def __init__(
        self,
        market: str,
        *,
        profile: dict[str, Any] | str | Path | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.market = _market_key(market)
        self.profile = _load_profile(profile)
        self.model_config = dict(DEFAULT_MARKET_MODELS.get(self.market, DEFAULT_MARKET_MODELS["ashare"]))
        self.model_config.update(self.profile.get("execution", {}))
        self.commission_model = CommissionModel(self.market, self.profile.get("commission", self.profile))
        self.slippage_model = VolatilitySlippageModel(self.market, self.profile.get("slippage", self.profile))
        self.rng = rng or random.Random()
        self.orders: dict[str, SimOrderRecord] = {}
        self.positions: dict[str, SimPosition] = {}

    def submit_order(self, order: SimOrder, market_snapshot: dict[str, Any] | None = None) -> SimOrderRecord:
        snapshot = dict(market_snapshot or {})
        record = SimOrderRecord(order=order, state="pending")
        self.orders[order.order_id] = record
        self._transition(record, "open")

        rule_rejection = self._rule_rejection(order, snapshot)
        if rule_rejection:
            record.reason = rule_rejection
            self._transition(record, "rejected")
            return record

        if not self._is_marketable(order, snapshot):
            record.reason = "limit_not_marketable"
            return record

        fill_qty = self._fill_quantity(order, snapshot)
        if fill_qty <= 0:
            record.reason = "no_queue_fill"
            return record

        fill_price, slippage_bps = self.slippage_model.price(order, snapshot)
        fill = SimFill(
            order_id=order.order_id,
            fill_price=fill_price,
            fill_qty=fill_qty,
            fill_time=_now_iso(),
            slippage_bps=slippage_bps,
            counterparty=str(snapshot.get("counterparty") or self.model_config.get("default_counterparty")),
        )
        record.fills.append(fill)
        record.filled_qty = round(fill_qty, 8)
        record.avg_fill_price = fill_price
        notional = fill_qty * fill_price
        record.fees = self.commission_model.calculate(order.side, notional)
        self._apply_position(order, fill, record.fees)

        if fill_qty + 1e-12 >= order.quantity:
            self._transition(record, "filled")
        else:
            self._transition(record, "partial")
            if order.time_in_force.lower() in {"ioc", "fok"}:
                self._transition(record, "cancelled")
                record.reason = f"{order.time_in_force.lower()}_residual_cancelled"
        return record

    def cancel_order(self, order_id: str, reason: str = "cancelled_by_request") -> SimOrderRecord:
        record = self.orders[order_id]
        if record.state in {"filled", "cancelled", "rejected"}:
            return record
        self._transition(record, "cancelled")
        record.reason = reason
        return record

    def reject_order(self, order: SimOrder, reason: str) -> SimOrderRecord:
        record = SimOrderRecord(order=order, state="pending", reason=reason)
        self.orders[order.order_id] = record
        self._transition(record, "rejected")
        return record

    def position(self, symbol: str, mark_price: float | None = None) -> SimPosition:
        pos = self.positions.setdefault(symbol, SimPosition(symbol=symbol))
        if mark_price is not None:
            pos.mark_price = _safe_float(mark_price, pos.mark_price)
            pos.unrealized_pnl = round((pos.mark_price - pos.avg_cost) * pos.current_holdings, 8)
        return pos

    def to_real(self) -> dict[str, Any]:
        validate_real_trading_enabled()
        return {
            "adapter": "real_execution_engine_placeholder",
            "market": self.market,
            "order_count": len(self.orders),
            "requires_broker_adapter": True,
        }

    def _transition(self, record: SimOrderRecord, target: str) -> None:
        if target not in ORDER_STATES:
            raise ValueError(f"unknown order state: {target}")
        allowed = {
            "pending": {"open", "rejected", "cancelled"},
            "open": {"partial", "filled", "cancelled", "rejected"},
            "partial": {"filled", "cancelled"},
            "filled": set(),
            "cancelled": set(),
            "rejected": set(),
        }
        if target != record.state and target not in allowed.get(record.state, set()):
            raise ValueError(f"invalid state transition {record.state}->{target}")
        record.state = target

    def _is_marketable(self, order: SimOrder, snapshot: dict[str, Any]) -> bool:
        if order.order_type == "market":
            return True
        if order.side == "buy":
            required = _first_positive(snapshot, "ask_price", "best_ask", "ask", "mid_price", "last_price", "close", default=order.limit_price)
            return order.limit_price >= required
        required = _first_positive(snapshot, "bid_price", "best_bid", "bid", "mid_price", "last_price", "close", default=order.limit_price)
        return order.limit_price <= required

    def _rule_rejection(self, order: SimOrder, snapshot: dict[str, Any]) -> str:
        if order.market != self.market:
            return "market_mismatch"
        lot_size = _safe_float(self.model_config.get("lot_size"), 0.0)
        if lot_size > 0 and order.side == "buy" and self.model_config.get("enforce_buy_lot", False):
            if abs(order.quantity / lot_size - round(order.quantity / lot_size)) > 1e-9:
                return "buy_quantity_not_lot_aligned"

        if order.side == "sell":
            sellable_qty = _safe_float(snapshot.get("sellable_qty"), -1.0)
            if sellable_qty >= 0 and order.quantity > sellable_qty + 1e-12:
                return "insufficient_sellable_qty_t1"

        lower_limit = _safe_float(snapshot.get("lower_limit"), 0.0)
        upper_limit = _safe_float(snapshot.get("upper_limit"), 0.0)
        if not lower_limit or not upper_limit:
            reference_price = _first_positive(snapshot, "previous_close", "pre_close", "reference_price", default=0.0)
            price_limit_pct = _safe_float(snapshot.get("price_limit_pct"), _safe_float(self.model_config.get("price_limit_pct"), 0.0))
            if reference_price > 0 and price_limit_pct > 0:
                lower_limit = reference_price * (1.0 - price_limit_pct)
                upper_limit = reference_price * (1.0 + price_limit_pct)
        if lower_limit > 0 and order.limit_price < lower_limit - 1e-9:
            return "price_below_lower_limit"
        if upper_limit > 0 and order.limit_price > upper_limit + 1e-9:
            return "price_above_upper_limit"

        if self.market == "pm":
            min_price = _safe_float(self.model_config.get("min_price"), 0.0)
            max_price = _safe_float(self.model_config.get("max_price"), 0.0)
            if min_price > 0 and order.limit_price < min_price:
                return "price_below_min_probability"
            if max_price > 0 and order.limit_price > max_price:
                return "price_above_max_probability"

        if order.side == "buy":
            cash_available = _safe_float(snapshot.get("cash_available"), -1.0)
            if cash_available >= 0:
                reference = self._reference_execution_price(order, snapshot)
                estimated_notional = reference * order.quantity
                estimated_fee = self.commission_model.calculate(order.side, estimated_notional).get("total", 0.0)
                if cash_available + 1e-9 < estimated_notional + estimated_fee:
                    return "insufficient_cash"
        return ""

    def _fill_quantity(self, order: SimOrder, snapshot: dict[str, Any]) -> float:
        if order.side == "buy":
            available = _first_positive(snapshot, "ask_size", "ask_qty", "best_ask_size", "available_qty", default=0.0)
        else:
            available = _first_positive(snapshot, "bid_size", "bid_qty", "best_bid_size", "available_qty", default=0.0)
        if available <= 0:
            bar_volume = _first_positive(snapshot, "bar_volume", "volume", "vol", default=0.0)
            if bar_volume > 0:
                available = bar_volume * _safe_float(self.model_config.get("bar_participation_cap"), 1.0)
        if available <= 0:
            available = order.quantity
        queue_position = max(0.0, _safe_float(snapshot.get("queue_position"), 0.0))
        queue_ahead = _safe_float(self.model_config.get("queue_ahead_ratio"), 0.0) * queue_position
        fillable = max(0.0, available - queue_ahead)
        if order.order_type == "limit":
            fillable *= max(0.05, 1.0 - min(0.95, queue_position))
        participation_cap = _safe_float(snapshot.get("participation_cap"), 1.0)
        fill_qty = min(order.quantity, fillable, max(0.0, order.quantity * participation_cap))
        lot_size = _safe_float(self.model_config.get("lot_size"), 0.0)
        if lot_size > 0 and order.side == "buy" and fill_qty < order.quantity:
            fill_qty = math.floor(fill_qty / lot_size) * lot_size
        if order.time_in_force.lower() == "fok" and fill_qty + 1e-12 < order.quantity:
            return 0.0
        return round(fill_qty, 8)

    def _reference_execution_price(self, order: SimOrder, snapshot: dict[str, Any]) -> float:
        if order.side == "buy":
            return _first_positive(snapshot, "ask_price", "best_ask", "ask", "mid_price", "last_price", "close", default=order.limit_price)
        return _first_positive(snapshot, "bid_price", "best_bid", "bid", "mid_price", "last_price", "close", default=order.limit_price)

    def _apply_position(self, order: SimOrder, fill: SimFill, fees: dict[str, float]) -> None:
        pos = self.positions.setdefault(order.symbol, SimPosition(symbol=order.symbol))
        qty = fill.fill_qty
        price = fill.fill_price
        total_fee = _safe_float(fees.get("total"), 0.0)
        if order.side == "buy":
            new_qty = pos.current_holdings + qty
            new_cost = (pos.avg_cost * pos.current_holdings) + (price * qty) + total_fee
            pos.current_holdings = round(new_qty, 8)
            pos.avg_cost = round(new_cost / new_qty, 8) if new_qty > 0 else 0.0
        else:
            close_qty = min(qty, pos.current_holdings)
            pos.realized_pnl = round(pos.realized_pnl + (price - pos.avg_cost) * close_qty - total_fee, 8)
            pos.current_holdings = round(pos.current_holdings - close_qty, 8)
            if pos.current_holdings <= 0:
                pos.current_holdings = 0.0
                pos.avg_cost = 0.0
        pos.mark_price = price
        pos.unrealized_pnl = round((price - pos.avg_cost) * pos.current_holdings, 8)


__all__ = [
    "CommissionModel",
    "SimExecutionEngine",
    "SimFill",
    "SimOrder",
    "SimOrderRecord",
    "SimPosition",
    "VolatilitySlippageModel",
]
