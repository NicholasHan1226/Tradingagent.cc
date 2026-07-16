"""Versioned execution-reality contracts shared by research and simulation.

The model separates exchange/statutory rules from broker-contract inputs.  A
broker commission may be overridden only with an explicitly versioned verified
schedule; statutory stamp duty and transfer fees stay tied to this model
version.  This keeps simulations conservative without pretending that the
provisional commission is Nicholas's final Huachuang contract rate.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace
from decimal import Decimal, ROUND_HALF_UP
from typing import Any


ASHARE_EXECUTION_REALITY_VERSION = "ashare-execution-reality-20260706-v1"
ASHARE_CANCEL_POLICY_VERSION = "ashare-cancel-cas-20260706-v1"
EXECUTION_REALITY_SCHEMA_VERSION = "execution-reality-model-v1"

_VERIFIED_COMMISSION_STATUSES = {
    "broker_contract_verified",
    "broker_statement_verified",
}

_ASHARE_SELL_LOT_SIZE = 100


def _finite_non_negative(value: Any, *, name: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise ValueError(f"{name} must be finite and non-negative")
    return parsed


def _whole_share_quantity(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0 or not parsed.is_integer():
        return None
    return int(parsed)


def ashare_sell_quantity_rejection_reason(
    *,
    current_shares: Any,
    sellable_shares: Any,
    requested_shares: Any,
) -> str | None:
    """Return the canonical A-share sell-lot rejection reason, if any.

    A round-lot sale is always valid when covered by both current and T+1
    sellable holdings.  If a holding contains an odd-lot remainder, that
    remainder may be submitted only in full.  A full-position exit remains
    valid.  Callers must reject a non-``None`` result; they must not round or
    rewrite the requested sale.
    """

    current = _whole_share_quantity(current_shares)
    sellable = _whole_share_quantity(sellable_shares)
    requested = _whole_share_quantity(requested_shares)
    if current is None or sellable is None or requested is None or requested <= 0:
        return "ashare_sell_quantity_invalid"
    if sellable > current:
        return "ashare_sellable_quantity_invalid"
    if requested > sellable:
        return "insufficient_sellable_qty_t1"
    if requested > current:
        return "ashare_sell_quantity_exceeds_current"
    if requested == current or requested % _ASHARE_SELL_LOT_SIZE == 0:
        return None
    odd_lot_remainder = current % _ASHARE_SELL_LOT_SIZE
    if odd_lot_remainder and requested == odd_lot_remainder:
        return None
    return "ashare_odd_lot_sell_quantity_invalid"


def _symbol_digits(symbol: str) -> str:
    raw = str(symbol or "").strip().upper()
    match = re.match(r"^(\d{6})", raw)
    return match.group(1) if match else ""


def _normalise_board(symbol: str, board: str | None = None) -> str:
    explicit = str(board or "").strip().lower().replace("-", "_")
    aliases = {
        "main": "main_board",
        "mainboard": "main_board",
        "sse_main": "main_board",
        "szse_main": "main_board",
        "star": "star_market",
        "star_board": "star_market",
        "sci_tech": "star_market",
        "gem": "chinext",
        "growth_enterprise_market": "chinext",
        "beijing": "bse",
        "beijing_stock_exchange": "bse",
    }
    explicit = aliases.get(explicit, explicit)
    if explicit in {"main_board", "star_market", "chinext", "bse"}:
        return explicit
    digits = _symbol_digits(symbol)
    raw = str(symbol or "").strip().upper()
    if raw.endswith(".BJ") or digits.startswith(("4", "8")):
        return "bse"
    if digits.startswith(("688", "689")):
        return "star_market"
    if digits.startswith(("300", "301")):
        return "chinext"
    return "main_board"


@dataclass(frozen=True)
class ExecutionRealityModel:
    """Immutable A-share exchange, fee and simulated-microstructure reality."""

    market: str = "ashare"
    schema_version: str = EXECUTION_REALITY_SCHEMA_VERSION
    model_version: str = ASHARE_EXECUTION_REALITY_VERSION
    effective_from: str = "2026-07-06"
    commission_bps: float = 2.5
    min_commission_cny: float = 5.0
    commission_schedule_status: str = "provisional_pending_broker_contract"
    commission_schedule_version: str = "provisional-conservative-v1"
    stamp_duty_sell_bps: float = 5.0
    transfer_fee_bps: float = 0.1
    price_tick_cny: float = 0.01
    buy_lot_size: int = 100
    conservative_label_slippage_bps_per_side: float = 5.0

    def price_limit_pct(
        self,
        *,
        symbol: str,
        board: str | None = None,
        risk_warning: bool = False,
    ) -> float:
        resolved = _normalise_board(symbol, board)
        if resolved == "star_market":
            return 0.20
        if resolved == "chinext":
            return 0.20
        if resolved == "bse":
            return 0.30
        # The 2026-07-06 rules removed the old 5% main-board ST limit.
        # ``risk_warning`` remains explicit in the API for auditability.
        _ = bool(risk_warning)
        return 0.10

    def price_cage_bounds(self, reference_price: float) -> tuple[float, float]:
        reference = _finite_non_negative(reference_price, name="reference_price")
        if reference <= 0:
            raise ValueError("reference_price must be positive")
        ten_ticks = self.price_tick_cny * 10
        lower = min(reference * 0.98, reference - ten_ticks)
        upper = max(reference * 1.02, reference + ten_ticks)
        return self._round_to_tick(
            max(self.price_tick_cny, lower)
        ), self._round_to_tick(upper)

    def price_limit_bounds(
        self,
        reference_price: float,
        *,
        symbol: str,
        board: str | None = None,
        risk_warning: bool = False,
    ) -> tuple[float, float]:
        reference = _finite_non_negative(reference_price, name="reference_price")
        if reference <= 0:
            raise ValueError("reference_price must be positive")
        limit = self.price_limit_pct(
            symbol=symbol,
            board=board,
            risk_warning=risk_warning,
        )
        lower = self._round_to_tick(reference * (1.0 - limit))
        upper = self._round_to_tick(reference * (1.0 + limit))
        if reference - lower < self.price_tick_cny - 1e-12:
            lower = self._round_to_tick(reference - self.price_tick_cny)
        if upper - reference < self.price_tick_cny - 1e-12:
            upper = self._round_to_tick(reference + self.price_tick_cny)
        return max(self.price_tick_cny, lower), max(self.price_tick_cny, upper)

    def _round_to_tick(self, value: float) -> float:
        tick = Decimal(str(self.price_tick_cny))
        amount = Decimal(str(value))
        units = (amount / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        return float(units * tick)

    def calculate_fees(self, side: str, notional_cny: float) -> dict[str, Any]:
        normalised_side = str(side or "").strip().lower()
        if normalised_side not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")
        notional = _finite_non_negative(notional_cny, name="notional_cny")
        if notional <= 0:
            commission = stamp = transfer = 0.0
        else:
            commission = max(
                notional * self.commission_bps / 10_000.0,
                self.min_commission_cny,
            )
            stamp = (
                notional * self.stamp_duty_sell_bps / 10_000.0
                if normalised_side == "sell"
                else 0.0
            )
            transfer = notional * self.transfer_fee_bps / 10_000.0
        total = commission + stamp + transfer
        return {
            "commission": round(commission, 8),
            "stamp_duty": round(stamp, 8),
            "transfer_fee": round(transfer, 8),
            "total": round(total, 8),
            "commission_bps": self.commission_bps,
            "min_commission_cny": self.min_commission_cny,
            "stamp_duty_sell_bps": self.stamp_duty_sell_bps,
            "transfer_fee_bps": self.transfer_fee_bps,
            "commission_schedule_status": self.commission_schedule_status,
            "commission_schedule_version": self.commission_schedule_version,
            "execution_reality_model_version": self.model_version,
        }

    def as_engine_config(self) -> dict[str, Any]:
        return {
            "execution_reality_model_version": self.model_version,
            "execution_reality_effective_from": self.effective_from,
            "commission_bps": self.commission_bps,
            "min_commission": self.min_commission_cny,
            "commission_schedule_status": self.commission_schedule_status,
            "commission_schedule_version": self.commission_schedule_version,
            "stamp_duty_sell_bps": self.stamp_duty_sell_bps,
            "transfer_fee_bps": self.transfer_fee_bps,
            "base_slippage_bps": 2.0,
            "volatility_slippage_multiplier": 0.18,
            "volume_impact_bps": 8.0,
            "price_improvement_bps": 0.5,
            "queue_ahead_ratio": 0.35,
            "price_tick": self.price_tick_cny,
            "lot_size": self.buy_lot_size,
            "enforce_buy_lot": True,
            "price_limit_pct": 0.10,
            "bar_participation_cap": 0.10,
            "default_counterparty": "simulated_ashare_book",
            "cancel_policy_version": ASHARE_CANCEL_POLICY_VERSION,
        }

    def as_contract(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "execution_reality_model_version": self.model_version,
            "effective_from": self.effective_from,
            "market": self.market,
            "settlement": "T+1",
            "can_sell_same_day": False,
            "currency": "CNY",
            "price_limit_policy_version": "ashare-price-limit-20260706-v1",
            "session_policy_version": "ashare-sessions-20260706-v1",
            "price_limit": {
                "main_board": 0.10,
                "main_board_risk_warning": 0.10,
                "star_market": 0.20,
                "star_market_risk_warning": 0.20,
                "chinext": 0.20,
                "chinext_risk_warning": 0.20,
                "bse": 0.30,
            },
            "sessions": {
                "opening_auction": {
                    "start": "09:15",
                    "end": "09:25",
                    "order_type": "limit",
                    "execution_supported": False,
                    "unsupported_reason": "opening_auction_batch_match_not_implemented",
                    "cancel_forbidden_after": "09:20",
                },
                "continuous_auction_am": {
                    "start": "09:30",
                    "end": "11:30",
                    "order_types": ["market", "limit"],
                    "execution_supported": True,
                },
                "continuous_auction_pm": {
                    "start": "13:00",
                    "end": "14:57",
                    "order_types": ["market", "limit"],
                    "execution_supported": True,
                },
                "closing_auction": {
                    "start": "14:57",
                    "end": "15:00",
                    "order_type": "limit",
                    "execution_supported": False,
                    "unsupported_reason": "closing_auction_batch_match_not_implemented",
                    "cancel_allowed": False,
                },
                "after_hours_fixed_price": {
                    "start": "15:05",
                    "end": "15:30",
                    "order_type": "after_hours_fixed_price",
                    "execution_supported": False,
                    "unsupported_reason": "after_hours_fixed_price_match_not_implemented",
                    "eligible_universe": "all_ashares",
                    "price_reference": "official_closing_price",
                    "cancel_allowed": True,
                },
            },
            "lot_size": self.buy_lot_size,
            "lot_rules": {
                "version": "ashare-lot-rules-20260706-v1",
                "buy_lot_size": self.buy_lot_size,
                "buy_must_be_integer_lots": True,
                "sell_lot_size": self.buy_lot_size,
                "odd_lot_sell_remainder_only": True,
            },
            "price_cage": {
                "version": "ashare-continuous-price-cage-20260706-v1",
                "applies_to": "continuous_auction_limit_orders",
                "ratio": 0.02,
                "tick_count": 10,
                "price_tick_cny": self.price_tick_cny,
                "verified_reference_required": True,
            },
            "cancel_policy": {
                "version": ASHARE_CANCEL_POLICY_VERSION,
                "state_version_required": True,
                "compare_and_set": True,
                "terminal_fill_wins_race": True,
                "append_only_order_events_required_for_future_broker": True,
            },
            "fees": {
                "commission_bps": self.commission_bps,
                "min_commission_cny": self.min_commission_cny,
                "commission_schedule_status": self.commission_schedule_status,
                "commission_schedule_version": self.commission_schedule_version,
                "stamp_duty_sell_bps": self.stamp_duty_sell_bps,
                "transfer_fee_bps_each_side": self.transfer_fee_bps,
                "conservative_label_slippage_bps_per_side": self.conservative_label_slippage_bps_per_side,
            },
        }


def ashare_execution_reality(
    *,
    commission_override: dict[str, Any] | None = None,
) -> ExecutionRealityModel:
    model = ExecutionRealityModel()
    if not commission_override:
        return model
    override = dict(commission_override)
    status = str(override.get("commission_schedule_status") or "").strip()
    version = str(override.get("commission_schedule_version") or "").strip()
    if status not in _VERIFIED_COMMISSION_STATUSES or not version:
        raise ValueError(
            "verified commission schedule status and version are required for override"
        )
    commission_bps = _finite_non_negative(
        override.get("commission_bps"),
        name="commission_bps",
    )
    min_commission = _finite_non_negative(
        override.get("min_commission_cny", override.get("min_commission")),
        name="min_commission_cny",
    )
    return replace(
        model,
        commission_bps=commission_bps,
        min_commission_cny=min_commission,
        commission_schedule_status=status,
        commission_schedule_version=version,
    )


__all__ = [
    "ASHARE_CANCEL_POLICY_VERSION",
    "ASHARE_EXECUTION_REALITY_VERSION",
    "EXECUTION_REALITY_SCHEMA_VERSION",
    "ExecutionRealityModel",
    "ashare_sell_quantity_rejection_reason",
    "ashare_execution_reality",
]
