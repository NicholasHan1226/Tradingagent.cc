"""Minute-to-paper bridge for the canonical A-share simulated authorities.

This file does not own cash, positions, orders, fills, reconciliation, or a
second decision ledger.  It validates five-minute execution timing and creates
the exact quote snapshot consumed by the existing capital-backed paper stage.
It also provides a strict factory for the existing DecisionExposureRecord.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from shared.capital.market_policy import MarketPolicy
from shared.execution.execution_reality import ashare_execution_reality
from shared.portfolio.small_account_optimizer import SmallAccountPolicy
from shared.review.decision_ledger import (
    DecisionExposureRecord,
    ExposureDisposition,
)
from shared.runtime.market_evidence_authority import (
    AShareExecutionQuoteEvidence,
    MarketEvidenceContext,
    MarketSourceBinding,
    freeze_non_production_market_evidence,
)

from .minute_data import FIVE_MINUTES, MinuteBarEvidence


SHANGHAI = ZoneInfo("Asia/Shanghai")


class MinutePaperContractError(ValueError):
    """Fail-closed minute paper contract failure."""


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _text(value: object, reason: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise MinutePaperContractError(reason)
    return value


def _aware(value: object, reason: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise MinutePaperContractError(reason)
    return value


def _next_supported_bar_end(value: datetime) -> datetime | None:
    local = value.astimezone(SHANGHAI)
    clock = local.time()
    if time(9, 35) <= clock < time(11, 30):
        return local + FIVE_MINUTES
    if clock == time(11, 30):
        return local.replace(hour=13, minute=5)
    if time(13, 5) <= clock < time(14, 55):
        return local + FIVE_MINUTES
    return None


@dataclass(frozen=True)
class MinuteExecutionPair:
    """A decision bar and the earliest permissible next execution bar."""

    decision_bar: MinuteBarEvidence
    execution_bar: MinuteBarEvidence

    def __post_init__(self) -> None:
        if not isinstance(self.decision_bar, MinuteBarEvidence) or not isinstance(
            self.execution_bar, MinuteBarEvidence
        ):
            raise MinutePaperContractError("minute_execution_pair_bar_invalid")
        if self.decision_bar.symbol != self.execution_bar.symbol:
            raise MinutePaperContractError("minute_execution_pair_symbol_mismatch")
        expected = _next_supported_bar_end(self.decision_bar.bar_end)
        if expected is None or self.execution_bar.bar_end != expected:
            raise MinutePaperContractError("minute_execution_must_use_next_bar")
        if self.execution_bar.bar_start < self.decision_bar.bar_end:
            raise MinutePaperContractError("minute_same_bar_execution_forbidden")
        if self.execution_bar.decision_time < self.execution_bar.available_at:
            raise MinutePaperContractError("minute_execution_evidence_future")


@dataclass(frozen=True)
class MinuteSmallAccountConstraints:
    """Operating constraints layered on the canonical 50,000 CNY authority."""

    policy: SmallAccountPolicy
    initial_monitor_count: int = 10
    expanded_monitor_count: int = 60
    operating_max_positions: int = 6
    minimum_operating_positions: int = 4

    def __post_init__(self) -> None:
        if not isinstance(self.policy, SmallAccountPolicy):
            raise MinutePaperContractError("minute_small_account_policy_required")
        if self.policy.initial_equity_cny != 50_000.0:
            raise MinutePaperContractError("minute_initial_equity_invalid")
        if (
            self.initial_monitor_count != 10
            or self.expanded_monitor_count != 60
            or not 4
            <= self.minimum_operating_positions
            <= self.operating_max_positions
            <= 6
            or self.policy.max_positions < self.operating_max_positions
        ):
            raise MinutePaperContractError("minute_operating_capacity_invalid")

    @classmethod
    def canonical(cls) -> "MinuteSmallAccountConstraints":
        return cls(
            policy=SmallAccountPolicy.from_market_policy(MarketPolicy.load("ashare"))
        )

    @property
    def single_name_cap_cny(self) -> float:
        return round(
            self.policy.initial_equity_cny * self.policy.single_name_max_pct, 6
        )

    def validate_buy_quantity(self, *, price_cny: float, quantity: int) -> None:
        if (
            isinstance(quantity, bool)
            or not isinstance(quantity, int)
            or quantity <= 0
            or quantity % self.policy.lot_size
        ):
            raise MinutePaperContractError("minute_buy_quantity_not_round_lot")
        if (
            isinstance(price_cny, bool)
            or not isinstance(price_cny, (int, float))
            or not math.isfinite(float(price_cny))
            or price_cny <= 0
        ):
            raise MinutePaperContractError("minute_buy_price_invalid")
        if float(price_cny) * quantity > self.single_name_cap_cny + 1e-9:
            raise MinutePaperContractError("minute_single_name_cap_exceeded")

    def trade_required(
        self, *, current_notional: float, target_notional: float
    ) -> bool:
        for value in (current_notional, target_notional):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or value < 0
            ):
                raise MinutePaperContractError("minute_target_notional_invalid")
        delta = abs(float(target_notional) - float(current_notional))
        return bool(
            delta >= self.policy.no_trade_band_cny
            and delta >= self.policy.minimum_economic_order_cny
        )


@dataclass(frozen=True)
class MinutePaperMarketSnapshot:
    """Validated execution-bar adapter for the existing paper stage."""

    order_id: str
    pair: MinuteExecutionPair
    market_snapshot: Mapping[str, Any]
    expected_fill_price_cny: float
    maximum_fill_quantity: int

    def __post_init__(self) -> None:
        _text(self.order_id, "minute_order_id_invalid")
        if not isinstance(self.pair, MinuteExecutionPair):
            raise MinutePaperContractError("minute_execution_pair_required")
        if not isinstance(self.market_snapshot, Mapping):
            raise MinutePaperContractError("minute_market_snapshot_invalid")
        if (
            isinstance(self.maximum_fill_quantity, bool)
            or not isinstance(self.maximum_fill_quantity, int)
            or self.maximum_fill_quantity < 100
            or self.maximum_fill_quantity % 100
        ):
            raise MinutePaperContractError("minute_fill_capacity_below_one_lot")
        if not (
            self.pair.execution_bar.low_cny
            <= self.expected_fill_price_cny
            <= self.pair.execution_bar.high_cny
        ):
            raise MinutePaperContractError("minute_expected_fill_outside_bar")

    def validate_receipt(self, receipt: Mapping[str, Any]) -> None:
        if not isinstance(receipt, Mapping):
            raise MinutePaperContractError("minute_execution_receipt_invalid")
        status = receipt.get("status")
        filled_quantity = receipt.get("filled_quantity")
        if status == "not_filled":
            if filled_quantity != 0:
                raise MinutePaperContractError("minute_nonfill_quantity_invalid")
            return
        if status not in {"filled", "partially_filled", "simulated_filled"}:
            raise MinutePaperContractError("minute_execution_status_invalid")
        price = receipt.get("fill_price", receipt.get("average_fill_price"))
        if (
            isinstance(price, bool)
            or not isinstance(price, (int, float))
            or not self.pair.execution_bar.low_cny
            <= float(price)
            <= self.pair.execution_bar.high_cny
        ):
            raise MinutePaperContractError("minute_fill_price_outside_bar")
        if (
            isinstance(filled_quantity, bool)
            or not isinstance(filled_quantity, int)
            or filled_quantity <= 0
            or filled_quantity > self.maximum_fill_quantity
        ):
            raise MinutePaperContractError("minute_filled_quantity_invalid")


def build_minute_paper_market_snapshot(
    *,
    order_id: str,
    pair: MinuteExecutionPair,
    side: str,
    session_calendar_receipt: Mapping[str, Any],
    session_calendar_receipt_sha256: str,
    capital_authority_id: str,
    authority_generation: int,
    execution_lineage_id: str,
) -> MinutePaperMarketSnapshot:
    """Build one non-production quote without giving the minute row authority."""

    normalized_side = str(side or "").strip().lower()
    if normalized_side not in {"buy", "sell"}:
        raise MinutePaperContractError("minute_side_invalid")
    bar = pair.execution_bar
    model = ashare_execution_reality()
    slippage = model.conservative_label_slippage_bps_per_side / 10_000.0
    direction = Decimal("1") + (
        Decimal(str(slippage)) if normalized_side == "buy" else -Decimal(str(slippage))
    )
    expected_fill = model._round_to_tick(float(Decimal(str(bar.open_cny)) * direction))
    if not bar.low_cny <= expected_fill <= bar.high_cny:
        raise MinutePaperContractError("minute_expected_fill_outside_bar")
    lower_limit, upper_limit = model.price_limit_bounds(
        bar.previous_close_cny,
        symbol=bar.symbol,
        board="main_board",
        risk_warning=False,
    )
    if not (
        lower_limit <= bar.open_cny <= upper_limit
        and lower_limit <= expected_fill <= upper_limit
    ):
        raise MinutePaperContractError("minute_price_limit_blocked")
    capacity = int(float(bar.volume_shares) * 0.10) // 100 * 100
    if capacity < 100:
        raise MinutePaperContractError("minute_fill_capacity_below_one_lot")
    source = MarketSourceBinding(
        dataset_id=bar.dataset_id,
        catalog_version=bar.catalog_version,
        source_receipt_id=bar.receipt_id,
        source_receipt_sha256=bar.envelope_proof_sha256,
        source_lineage_sha256=bar.source_lineage_sha256,
        data_through=bar.data_through,
        observed_at=bar.observed_at,
        available_at=bar.available_at,
    )
    context = MarketEvidenceContext(
        trade_date=bar.bar_end.astimezone(SHANGHAI).date(),
        decision_as_of=pair.decision_bar.decision_time,
        capital_authority_id=capital_authority_id,
        authority_generation=authority_generation,
        execution_lineage_id=execution_lineage_id,
        account_type="simulated",
        real_trading_enabled=False,
    )
    quote = AShareExecutionQuoteEvidence(
        symbol=bar.symbol,
        order_id=order_id,
        bid_price_cny=bar.open_cny,
        ask_price_cny=bar.open_cny,
        bid_size=capacity,
        ask_size=capacity,
        previous_close_cny=bar.previous_close_cny,
        market_session=bar.market_session,
        # A completed t+1 bar is retrospective fill evidence.  The canonical
        # quote authority therefore binds its settlement/verification instant
        # (when the envelope was available), while ``modeled_fill_time`` below
        # preserves the historical next-bar-open instant.  This is fixture-only
        # and can never claim a contemporaneous live quote.
        execution_time=bar.available_at,
        source=source,
        session_calendar_receipt_sha256=session_calendar_receipt_sha256,
        context=context,
    )
    frozen_authority = freeze_non_production_market_evidence(
        quote,
        expected_dataset_id=bar.dataset_id,
        frozen_at=bar.available_at,
    )
    snapshot = {
        "snapshot_id": f"SNAPSHOT-{order_id}",
        "symbol": bar.symbol,
        "trade_date": bar.bar_end.astimezone(SHANGHAI).date().isoformat(),
        "bid_price": bar.open_cny,
        "ask_price": bar.open_cny,
        "bid_size": capacity,
        "ask_size": capacity,
        "previous_close": bar.previous_close_cny,
        "market_session": bar.market_session,
        "execution_time": bar.available_at.isoformat(),
        "modeled_fill_time": bar.bar_start.isoformat(),
        "retrospective_bar_fill_evidence": True,
        "available_at": bar.available_at.isoformat(),
        "observed_at": bar.observed_at.isoformat(),
        "data_through": bar.data_through.isoformat(),
        "source_receipt_id": bar.receipt_id,
        "source_sha256": bar.envelope_proof_sha256,
        "source_lineage_sha256": bar.source_lineage_sha256,
        "dataset_id": bar.dataset_id,
        "catalog_version": bar.catalog_version,
        "session_calendar_receipt": dict(session_calendar_receipt),
        "capital_authority_id": capital_authority_id,
        "authority_generation": authority_generation,
        "execution_lineage": execution_lineage_id,
        "account_type": "simulated",
        "real_trading_enabled": False,
        "decision_as_of": pair.decision_bar.decision_time.isoformat(),
        "market_evidence_authority": frozen_authority,
        "bar_high": bar.high_cny,
        "bar_low": bar.low_cny,
        "bar_volume_shares": bar.volume_shares,
        "minute_evidence_sha256": bar.sha256,
    }
    return MinutePaperMarketSnapshot(
        order_id=order_id,
        pair=pair,
        market_snapshot=snapshot,
        expected_fill_price_cny=expected_fill,
        maximum_fill_quantity=capacity,
    )


class MinuteDecisionOutcome(str, Enum):
    PAPER_FILLED = "paper_filled"
    PAPER_NOT_FILLED = "paper_not_filled"
    DATA_REJECTED = "data_rejected"
    MODEL_REJECTED = "model_rejected"
    HUMAN_REJECTED = "human_rejected"
    INSUFFICIENT_CAPITAL = "insufficient_capital"
    RANKED_NOT_TRADED = "ranked_not_traded"


@dataclass(frozen=True)
class MinuteFixturePosition:
    """One non-authoritative fixture position with T+1 acquisition lots."""

    symbol: str
    quantity: int
    cost_basis_cny: float
    acquired_by_date: Mapping[str, int]

    def __post_init__(self) -> None:
        _text(self.symbol, "minute_position_symbol_invalid")
        if (
            isinstance(self.quantity, bool)
            or not isinstance(self.quantity, int)
            or self.quantity <= 0
            or self.quantity % 100
        ):
            raise MinutePaperContractError("minute_position_quantity_invalid")
        if (
            isinstance(self.cost_basis_cny, bool)
            or not isinstance(self.cost_basis_cny, (int, float))
            or not math.isfinite(float(self.cost_basis_cny))
            or self.cost_basis_cny <= 0
        ):
            raise MinutePaperContractError("minute_position_cost_basis_invalid")
        if not isinstance(self.acquired_by_date, Mapping):
            raise MinutePaperContractError("minute_position_acquisition_invalid")
        total = 0
        for raw_date, quantity in self.acquired_by_date.items():
            try:
                date.fromisoformat(str(raw_date))
            except ValueError as exc:
                raise MinutePaperContractError(
                    "minute_position_acquisition_date_invalid"
                ) from exc
            if (
                isinstance(quantity, bool)
                or not isinstance(quantity, int)
                or quantity <= 0
                or quantity % 100
            ):
                raise MinutePaperContractError(
                    "minute_position_acquisition_quantity_invalid"
                )
            total += quantity
        if total != self.quantity:
            raise MinutePaperContractError("minute_position_acquisition_mismatch")

    def sellable(self, trade_date: date) -> int:
        return sum(
            quantity
            for raw_date, quantity in self.acquired_by_date.items()
            if date.fromisoformat(raw_date) < trade_date
        )


@dataclass(frozen=True)
class MinuteFixtureOrderReceipt:
    """Formal fixture receipt for filled, partial, unfilled or rejected orders."""

    order_id: str
    symbol: str
    side: str
    requested_quantity: int
    filled_quantity: int
    residual_quantity: int
    status: str
    reason_code: str
    modeled_fill_time: datetime | None
    settled_at: datetime
    fill_price_cny: float | None
    notional_cny: float
    fee_cny: float
    cash_after_cny: float
    position_after: int
    evidence_sha256: str
    authority_tier: str = "non_production_fixture"
    durable: bool = False
    real_trading_enabled: bool = False
    broker_order_id: None = None

    def __post_init__(self) -> None:
        for field_name in ("order_id", "symbol", "side", "status", "reason_code"):
            _text(getattr(self, field_name), f"minute_receipt_{field_name}_invalid")
        if self.side not in {"buy", "sell"}:
            raise MinutePaperContractError("minute_receipt_side_invalid")
        if self.status not in {
            "filled",
            "partial",
            "not_filled",
            "rejected",
            "cancelled",
        }:
            raise MinutePaperContractError("minute_receipt_status_invalid")
        for field_name in (
            "requested_quantity",
            "filled_quantity",
            "residual_quantity",
            "position_after",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise MinutePaperContractError(f"minute_receipt_{field_name}_invalid")
        if self.requested_quantity <= 0 or (
            self.filled_quantity + self.residual_quantity != self.requested_quantity
        ):
            raise MinutePaperContractError("minute_receipt_quantity_mismatch")
        _aware(self.settled_at, "minute_receipt_settled_at_invalid")
        if self.modeled_fill_time is not None:
            _aware(self.modeled_fill_time, "minute_receipt_fill_time_invalid")
        for field_name in ("notional_cny", "fee_cny", "cash_after_cny"):
            value = getattr(self, field_name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or value < 0
            ):
                raise MinutePaperContractError(f"minute_receipt_{field_name}_invalid")
        if self.status in {"filled", "partial"}:
            if (
                self.filled_quantity <= 0
                or self.fill_price_cny is None
                or self.modeled_fill_time is None
            ):
                raise MinutePaperContractError("minute_receipt_fill_evidence_missing")
        elif any(
            (
                self.filled_quantity,
                self.notional_cny,
                self.fee_cny,
                self.fill_price_cny is not None,
                self.modeled_fill_time is not None,
            )
        ):
            raise MinutePaperContractError("minute_receipt_nonfill_has_fill_evidence")
        if (
            len(self.evidence_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in self.evidence_sha256
            )
            or self.authority_tier != "non_production_fixture"
            or self.durable is not False
            or self.real_trading_enabled is not False
            or self.broker_order_id is not None
        ):
            raise MinutePaperContractError("minute_receipt_boundary_invalid")

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "symbol": self.symbol,
            "side": self.side,
            "requested_quantity": self.requested_quantity,
            "filled_quantity": self.filled_quantity,
            "residual_quantity": self.residual_quantity,
            "status": self.status,
            "reason_code": self.reason_code,
            "modeled_fill_time": (
                None
                if self.modeled_fill_time is None
                else self.modeled_fill_time.astimezone(timezone.utc).isoformat()
            ),
            "settled_at": self.settled_at.astimezone(timezone.utc).isoformat(),
            "fill_price_cny": self.fill_price_cny,
            "notional_cny": self.notional_cny,
            "fee_cny": self.fee_cny,
            "cash_after_cny": self.cash_after_cny,
            "position_after": self.position_after,
            "evidence_sha256": self.evidence_sha256,
            "authority_tier": "non_production_fixture",
            "durable": False,
            "real_trading_enabled": False,
            "broker_order_id": None,
        }

    @property
    def sha256(self) -> str:
        return _canonical_sha256(self.canonical_payload())


class MinuteFixturePaperBook:
    """Restartable fixture-only minute book using canonical A-share economics.

    This is a mock-ready verifier, not the current capital authority.  It
    deliberately cannot write the durable MarketCapitalLedger.  The production
    minute adapter must settle through that existing authority after real
    TradingDatas minute evidence and a dedicated retrospective-settlement
    contract are frozen.
    """

    state_schema = "tradingagent.ashare.minute_fixture_book.v1"

    def __init__(
        self,
        *,
        constraints: MinuteSmallAccountConstraints | None = None,
        cash_cny: float = 50_000.0,
        positions: Mapping[str, MinuteFixturePosition] | None = None,
        realized_pnl_cny: float = 0.0,
        receipts: Mapping[str, MinuteFixtureOrderReceipt] | None = None,
        request_sha256s: Mapping[str, str] | None = None,
    ) -> None:
        self.constraints = constraints or MinuteSmallAccountConstraints.canonical()
        if (
            isinstance(cash_cny, bool)
            or not isinstance(cash_cny, (int, float))
            or not math.isfinite(float(cash_cny))
            or cash_cny < 0
        ):
            raise MinutePaperContractError("minute_book_cash_invalid")
        if (
            isinstance(realized_pnl_cny, bool)
            or not isinstance(realized_pnl_cny, (int, float))
            or not math.isfinite(float(realized_pnl_cny))
        ):
            raise MinutePaperContractError("minute_book_realized_pnl_invalid")
        self.cash_cny = round(float(cash_cny), 6)
        self.realized_pnl_cny = round(float(realized_pnl_cny), 6)
        self._positions = dict(positions or {})
        self._receipts = dict(receipts or {})
        self._request_sha256s = dict(request_sha256s or {})
        if len(self._positions) > self.constraints.operating_max_positions:
            raise MinutePaperContractError("minute_book_position_capacity_exceeded")
        if set(self._receipts) != set(self._request_sha256s):
            raise MinutePaperContractError("minute_book_idempotency_state_invalid")

    @property
    def positions(self) -> Mapping[str, MinuteFixturePosition]:
        return dict(self._positions)

    @property
    def receipts(self) -> Mapping[str, MinuteFixtureOrderReceipt]:
        return dict(self._receipts)

    def _receipt(
        self,
        *,
        order_id: str,
        pair: MinuteExecutionPair,
        side: str,
        requested_quantity: int,
        status: str,
        reason_code: str,
        filled_quantity: int = 0,
        fill_price_cny: float | None = None,
        notional_cny: float = 0.0,
        fee_cny: float = 0.0,
    ) -> MinuteFixtureOrderReceipt:
        position = self._positions.get(pair.execution_bar.symbol)
        return MinuteFixtureOrderReceipt(
            order_id=order_id,
            symbol=pair.execution_bar.symbol,
            side=side,
            requested_quantity=requested_quantity,
            filled_quantity=filled_quantity,
            residual_quantity=requested_quantity - filled_quantity,
            status=status,
            reason_code=reason_code,
            modeled_fill_time=(
                pair.execution_bar.bar_start if filled_quantity else None
            ),
            settled_at=pair.execution_bar.available_at,
            fill_price_cny=fill_price_cny,
            notional_cny=round(notional_cny, 6),
            fee_cny=round(fee_cny, 6),
            cash_after_cny=self.cash_cny,
            position_after=0 if position is None else position.quantity,
            evidence_sha256=pair.execution_bar.sha256,
        )

    def execute(
        self,
        *,
        order_id: str,
        pair: MinuteExecutionPair,
        side: str,
        requested_quantity: int,
    ) -> MinuteFixtureOrderReceipt:
        order_id = _text(order_id, "minute_order_id_invalid")
        side = str(side or "").strip().lower()
        if side not in {"buy", "sell"}:
            raise MinutePaperContractError("minute_side_invalid")
        if (
            isinstance(requested_quantity, bool)
            or not isinstance(requested_quantity, int)
            or requested_quantity <= 0
            or requested_quantity % 100
        ):
            raise MinutePaperContractError("minute_order_quantity_invalid")
        if not isinstance(pair, MinuteExecutionPair):
            raise MinutePaperContractError("minute_execution_pair_required")
        request_sha = _canonical_sha256(
            {
                "order_id": order_id,
                "side": side,
                "requested_quantity": requested_quantity,
                "decision_bar": pair.decision_bar.sha256,
                "execution_bar": pair.execution_bar.sha256,
            }
        )
        previous_request = self._request_sha256s.get(order_id)
        if previous_request is not None:
            if previous_request != request_sha:
                raise MinutePaperContractError("minute_order_idempotency_conflict")
            return self._receipts[order_id]

        bar = pair.execution_bar
        model = ashare_execution_reality()
        lower_limit, upper_limit = model.price_limit_bounds(
            bar.previous_close_cny,
            symbol=bar.symbol,
            board="main_board",
            risk_warning=False,
        )
        if (side == "buy" and bar.open_cny >= upper_limit) or (
            side == "sell" and bar.open_cny <= lower_limit
        ):
            receipt = self._receipt(
                order_id=order_id,
                pair=pair,
                side=side,
                requested_quantity=requested_quantity,
                status="not_filled",
                reason_code="minute_price_limit_not_fillable",
            )
            return self._store(order_id, request_sha, receipt)
        capacity = int(float(bar.volume_shares) * 0.10) // 100 * 100
        fill_quantity = min(requested_quantity, capacity)
        if fill_quantity < 100:
            receipt = self._receipt(
                order_id=order_id,
                pair=pair,
                side=side,
                requested_quantity=requested_quantity,
                status="not_filled",
                reason_code="minute_insufficient_bar_capacity",
            )
            return self._store(order_id, request_sha, receipt)
        slippage = Decimal(
            str(model.conservative_label_slippage_bps_per_side / 10_000.0)
        )
        multiplier = Decimal("1") + (slippage if side == "buy" else -slippage)
        fill_price = model._round_to_tick(
            float(Decimal(str(bar.open_cny)) * multiplier)
        )
        if not bar.low_cny <= fill_price <= bar.high_cny:
            receipt = self._receipt(
                order_id=order_id,
                pair=pair,
                side=side,
                requested_quantity=requested_quantity,
                status="not_filled",
                reason_code="minute_fill_outside_completed_bar",
            )
            return self._store(order_id, request_sha, receipt)
        trade_date = bar.bar_end.astimezone(SHANGHAI).date()
        current = self._positions.get(bar.symbol)
        if side == "buy":
            if (
                fill_quantity * fill_price
                < self.constraints.policy.minimum_economic_order_cny
            ):
                receipt = self._receipt(
                    order_id=order_id,
                    pair=pair,
                    side=side,
                    requested_quantity=requested_quantity,
                    status="rejected",
                    reason_code="minute_no_trade_band",
                )
                return self._store(order_id, request_sha, receipt)
            self.constraints.validate_buy_quantity(
                price_cny=fill_price,
                quantity=fill_quantity,
            )
            if (
                current is None
                and len(self._positions) >= self.constraints.operating_max_positions
            ):
                receipt = self._receipt(
                    order_id=order_id,
                    pair=pair,
                    side=side,
                    requested_quantity=requested_quantity,
                    status="rejected",
                    reason_code="minute_position_capacity_exceeded",
                )
                return self._store(order_id, request_sha, receipt)
            existing_notional = (
                0.0 if current is None else current.quantity * bar.open_cny
            )
            if (
                existing_notional + fill_quantity * fill_price
                > self.constraints.single_name_cap_cny + 1e-9
            ):
                receipt = self._receipt(
                    order_id=order_id,
                    pair=pair,
                    side=side,
                    requested_quantity=requested_quantity,
                    status="rejected",
                    reason_code="minute_single_name_cap_exceeded",
                )
                return self._store(order_id, request_sha, receipt)
            notional = round(fill_quantity * fill_price, 6)
            fee = float(model.calculate_fees("buy", notional)["total"])
            debit = round(notional + fee, 6)
            if debit > self.cash_cny + 1e-9:
                receipt = self._receipt(
                    order_id=order_id,
                    pair=pair,
                    side=side,
                    requested_quantity=requested_quantity,
                    status="rejected",
                    reason_code="minute_insufficient_cash",
                )
                return self._store(order_id, request_sha, receipt)
            self.cash_cny = round(self.cash_cny - debit, 6)
            acquisitions = {} if current is None else dict(current.acquired_by_date)
            key = trade_date.isoformat()
            acquisitions[key] = acquisitions.get(key, 0) + fill_quantity
            self._positions[bar.symbol] = MinuteFixturePosition(
                symbol=bar.symbol,
                quantity=(0 if current is None else current.quantity) + fill_quantity,
                cost_basis_cny=round(
                    (0 if current is None else current.cost_basis_cny) + debit,
                    6,
                ),
                acquired_by_date=acquisitions,
            )
        else:
            if current is None:
                receipt = self._receipt(
                    order_id=order_id,
                    pair=pair,
                    side=side,
                    requested_quantity=requested_quantity,
                    status="rejected",
                    reason_code="minute_position_missing",
                )
                return self._store(order_id, request_sha, receipt)
            sellable = current.sellable(trade_date)
            if fill_quantity > sellable:
                receipt = self._receipt(
                    order_id=order_id,
                    pair=pair,
                    side=side,
                    requested_quantity=requested_quantity,
                    status="rejected",
                    reason_code="minute_t1_sellable_quantity_insufficient",
                )
                return self._store(order_id, request_sha, receipt)
            notional = round(fill_quantity * fill_price, 6)
            fee = float(model.calculate_fees("sell", notional)["total"])
            net = round(notional - fee, 6)
            allocated_basis = round(
                current.cost_basis_cny * fill_quantity / current.quantity,
                6,
            )
            self.realized_pnl_cny = round(
                self.realized_pnl_cny + net - allocated_basis,
                6,
            )
            self.cash_cny = round(self.cash_cny + net, 6)
            remaining_to_remove = fill_quantity
            acquisitions = dict(current.acquired_by_date)
            for key in sorted(acquisitions):
                if date.fromisoformat(key) >= trade_date or remaining_to_remove <= 0:
                    continue
                take = min(acquisitions[key], remaining_to_remove)
                acquisitions[key] -= take
                remaining_to_remove -= take
                if acquisitions[key] == 0:
                    del acquisitions[key]
            remaining_quantity = current.quantity - fill_quantity
            if remaining_quantity:
                self._positions[bar.symbol] = MinuteFixturePosition(
                    symbol=bar.symbol,
                    quantity=remaining_quantity,
                    cost_basis_cny=round(
                        current.cost_basis_cny - allocated_basis,
                        6,
                    ),
                    acquired_by_date=acquisitions,
                )
            else:
                del self._positions[bar.symbol]
        status = "filled" if fill_quantity == requested_quantity else "partial"
        receipt = self._receipt(
            order_id=order_id,
            pair=pair,
            side=side,
            requested_quantity=requested_quantity,
            status=status,
            reason_code=(
                "minute_simulated_filled"
                if status == "filled"
                else "minute_simulated_partial_fill"
            ),
            filled_quantity=fill_quantity,
            fill_price_cny=fill_price,
            notional_cny=notional,
            fee_cny=fee,
        )
        return self._store(order_id, request_sha, receipt)

    def cancel(
        self,
        *,
        order_id: str,
        pair: MinuteExecutionPair,
        side: str,
        requested_quantity: int,
        reason_code: str = "minute_cancelled_before_settlement",
    ) -> MinuteFixtureOrderReceipt:
        """Record an idempotent zero-fill cancellation without capital mutation."""

        order_id = _text(order_id, "minute_order_id_invalid")
        side = str(side or "").strip().lower()
        if side not in {"buy", "sell"}:
            raise MinutePaperContractError("minute_side_invalid")
        if (
            isinstance(requested_quantity, bool)
            or not isinstance(requested_quantity, int)
            or requested_quantity <= 0
            or requested_quantity % 100
        ):
            raise MinutePaperContractError("minute_order_quantity_invalid")
        reason_code = _text(reason_code, "minute_cancel_reason_invalid")
        request_sha = _canonical_sha256(
            {
                "operation": "cancel",
                "order_id": order_id,
                "side": side,
                "requested_quantity": requested_quantity,
                "decision_bar": pair.decision_bar.sha256,
                "execution_bar": pair.execution_bar.sha256,
                "reason_code": reason_code,
            }
        )
        previous_request = self._request_sha256s.get(order_id)
        if previous_request is not None:
            if previous_request != request_sha:
                raise MinutePaperContractError("minute_order_idempotency_conflict")
            return self._receipts[order_id]
        receipt = self._receipt(
            order_id=order_id,
            pair=pair,
            side=side,
            requested_quantity=requested_quantity,
            status="cancelled",
            reason_code=reason_code,
        )
        return self._store(order_id, request_sha, receipt)

    def _store(
        self,
        order_id: str,
        request_sha: str,
        receipt: MinuteFixtureOrderReceipt,
    ) -> MinuteFixtureOrderReceipt:
        self._request_sha256s[order_id] = request_sha
        self._receipts[order_id] = receipt
        return receipt

    def reconcile(self, *, marks: Mapping[str, float]) -> dict[str, Any]:
        if set(marks) != set(self._positions):
            raise MinutePaperContractError("minute_reconcile_marks_incomplete")
        market_value = 0.0
        cost_basis = 0.0
        by_symbol: dict[str, float] = {}
        for symbol, position in self._positions.items():
            raw_mark = marks[symbol]
            if (
                isinstance(raw_mark, bool)
                or not isinstance(raw_mark, (int, float))
                or not math.isfinite(float(raw_mark))
                or raw_mark <= 0
            ):
                raise MinutePaperContractError("minute_reconcile_mark_invalid")
            value = round(position.quantity * float(raw_mark), 6)
            by_symbol[symbol] = value
            market_value += value
            cost_basis += position.cost_basis_cny
        unrealized = round(market_value - cost_basis, 6)
        equity = round(self.cash_cny + market_value, 6)
        expected = round(
            self.constraints.policy.initial_equity_cny
            + self.realized_pnl_cny
            + unrealized,
            6,
        )
        if not math.isclose(equity, expected, abs_tol=0.02):
            raise MinutePaperContractError("minute_reconcile_conservation_failed")
        return {
            "account_type": "simulated",
            "authority_tier": "non_production_fixture",
            "durable": False,
            "real_trading_enabled": False,
            "cash_cny": self.cash_cny,
            "positions_market_value": by_symbol,
            "market_value_cny": round(market_value, 6),
            "gross_exposure_cny": round(market_value, 6),
            "realized_pnl_cny": self.realized_pnl_cny,
            "unrealized_pnl_cny": unrealized,
            "equity_cny": equity,
            "position_count": len(self._positions),
            "conservation_expected_equity_cny": expected,
            "reconciled": True,
        }

    def export_state(self) -> dict[str, Any]:
        payload = {
            "schema": self.state_schema,
            "cash_cny": self.cash_cny,
            "realized_pnl_cny": self.realized_pnl_cny,
            "positions": {
                symbol: {
                    "symbol": position.symbol,
                    "quantity": position.quantity,
                    "cost_basis_cny": position.cost_basis_cny,
                    "acquired_by_date": dict(position.acquired_by_date),
                }
                for symbol, position in sorted(self._positions.items())
            },
            "receipts": {
                order_id: receipt.canonical_payload()
                for order_id, receipt in sorted(self._receipts.items())
            },
            "request_sha256s": dict(sorted(self._request_sha256s.items())),
            "real_trading_enabled": False,
        }
        return {**payload, "state_sha256": _canonical_sha256(payload)}

    @classmethod
    def restore(cls, state: Mapping[str, Any]) -> "MinuteFixturePaperBook":
        if not isinstance(state, Mapping):
            raise MinutePaperContractError("minute_book_state_invalid")
        payload = dict(state)
        state_sha = payload.pop("state_sha256", None)
        if (
            payload.get("schema") != cls.state_schema
            or payload.get("real_trading_enabled") is not False
            or state_sha != _canonical_sha256(payload)
        ):
            raise MinutePaperContractError("minute_book_state_integrity_failed")
        raw_positions = payload.get("positions")
        raw_receipts = payload.get("receipts")
        if not isinstance(raw_positions, Mapping) or not isinstance(
            raw_receipts, Mapping
        ):
            raise MinutePaperContractError("minute_book_state_invalid")
        positions: dict[str, MinuteFixturePosition] = {}
        for symbol, value in raw_positions.items():
            if not isinstance(value, Mapping):
                raise MinutePaperContractError("minute_book_state_invalid")
            normalized_symbol = str(symbol)
            position = MinuteFixturePosition(**dict(value))
            if position.symbol != normalized_symbol:
                raise MinutePaperContractError("minute_book_state_invalid")
            positions[normalized_symbol] = position
        receipts: dict[str, MinuteFixtureOrderReceipt] = {}
        for order_id, raw in raw_receipts.items():
            if not isinstance(raw, Mapping):
                raise MinutePaperContractError("minute_book_state_invalid")
            values = dict(raw)
            values.pop("authority_tier", None)
            values.pop("durable", None)
            values.pop("real_trading_enabled", None)
            values.pop("broker_order_id", None)
            values["modeled_fill_time"] = (
                None
                if values["modeled_fill_time"] is None
                else datetime.fromisoformat(
                    str(values["modeled_fill_time"]).replace("Z", "+00:00")
                )
            )
            values["settled_at"] = datetime.fromisoformat(
                str(values["settled_at"]).replace("Z", "+00:00")
            )
            receipts[str(order_id)] = MinuteFixtureOrderReceipt(**values)
        return cls(
            cash_cny=float(payload["cash_cny"]),
            positions=positions,
            realized_pnl_cny=float(payload["realized_pnl_cny"]),
            receipts=receipts,
            request_sha256s=dict(payload.get("request_sha256s") or {}),
        )


def minute_decision_record(
    *,
    decision_id: str,
    decision_cluster_id: str,
    decision_time: datetime,
    symbol: str,
    model_id: str,
    model_version: str,
    manifest_sha256: str,
    action: str,
    outcome: MinuteDecisionOutcome,
    requested_notional_cny: float,
    filled_quantity: int = 0,
    filled_notional_cny: float = 0.0,
    actual_cost_cny: float = 0.0,
    simulated_fill_id: str | None = None,
    reason_code: str | None = None,
) -> DecisionExposureRecord:
    """Translate minute outcomes into the existing canonical decision ledger."""

    if not isinstance(outcome, MinuteDecisionOutcome):
        raise MinutePaperContractError("minute_decision_outcome_invalid")
    if outcome is MinuteDecisionOutcome.PAPER_FILLED:
        disposition = ExposureDisposition.PAPER_FILLED
        rejection_reason = None
        nonfill_reason = None
    elif outcome is MinuteDecisionOutcome.PAPER_NOT_FILLED:
        disposition = ExposureDisposition.PAPER_NOT_FILLED
        rejection_reason = None
        nonfill_reason = _text(reason_code, "minute_nonfill_reason_required")
    elif outcome is MinuteDecisionOutcome.RANKED_NOT_TRADED:
        disposition = ExposureDisposition.SHADOW_ONLY
        rejection_reason = None
        nonfill_reason = None
    else:
        disposition = ExposureDisposition.REJECTED
        rejection_reason = _text(reason_code, "minute_rejection_reason_required")
        nonfill_reason = None
    return DecisionExposureRecord(
        decision_id=decision_id,
        decision_cluster_id=decision_cluster_id,
        decision_time=_aware(decision_time, "minute_decision_time_invalid"),
        symbol=symbol,
        model_id=model_id,
        model_version=model_version,
        manifest_sha256=manifest_sha256,
        action=action,
        disposition=disposition,
        requested_notional_cny=requested_notional_cny,
        filled_quantity=filled_quantity,
        filled_notional_cny=filled_notional_cny,
        actual_cost_cny=actual_cost_cny,
        simulated_fill_id=simulated_fill_id,
        rejection_reason=rejection_reason,
        nonfill_reason=nonfill_reason,
        capital_layer="simulated",
        account_type="simulated",
        real_trading_enabled=False,
        live_transition_authorized=False,
        broker_order_id=None,
    )


def minute_action_allowed_during_data_failure(action: str) -> bool:
    """Risk can contract or hold on a data failure, but cannot add exposure.

    This is a decision permission only.  A reduce/exit still requires separate,
    valid execution evidence before a simulated fill may be settled.
    """

    normalized = str(action or "").strip().lower()
    if normalized not in {"open", "increase", "hold", "reduce", "exit"}:
        raise MinutePaperContractError("minute_action_invalid")
    return normalized in {"hold", "reduce", "exit"}


__all__ = [
    "MinuteDecisionOutcome",
    "MinuteExecutionPair",
    "MinuteFixtureOrderReceipt",
    "MinuteFixturePaperBook",
    "MinuteFixturePosition",
    "MinutePaperContractError",
    "MinutePaperMarketSnapshot",
    "MinuteSmallAccountConstraints",
    "build_minute_paper_market_snapshot",
    "minute_action_allowed_during_data_failure",
    "minute_decision_record",
]
