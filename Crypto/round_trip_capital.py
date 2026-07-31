"""Independent simulation-only Crypto capital generation with buy/sell replay.

This module intentionally does not extend the generation-1 buy-only ledger.
It owns a new account, authority, event contract, and output directory so an
old delayed-paper epoch can remain immutable and non-aggregated.
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_UP
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from Crypto.fixture_sim.contracts import _assert_simulation_only


ROUND_TRIP_LEDGER_CONTRACT = "tradingagent.crypto.round_trip_capital_ledger.v1"
ROUND_TRIP_HEAD_CONTRACT = "tradingagent.crypto.round_trip_capital_head.v1"
ROUND_TRIP_ORDER_CONTRACT = "tradingagent.crypto.round_trip_order_intent.v1"
ROUND_TRIP_RECEIPT_CONTRACT = "tradingagent.crypto.round_trip_paper_receipt.v1"
ROUND_TRIP_CYCLE_CONTRACT = "tradingagent.crypto.round_trip_cycle.v1"
FROZEN_EXIT_POLICY_ID = "crypto-round-trip-exit-v1"
TAKE_PROFIT_RETURN = Decimal("0.03")
STOP_LOSS_RETURN = Decimal("-0.02")
MAX_HOLD_SECONDS = 24 * 60 * 60
SLIPPAGE_BPS = Decimal("2")
TAKER_FEE_RATE = Decimal("0.001")
POSITION_FRACTION = Decimal("0.10")
MONEY_QUANTUM = Decimal("0.00000001")
ZERO = Decimal("0")
ALLOWED_SYMBOLS = frozenset({"BTCUSDT", "ETHUSDT"})
_WRITE_CAPABILITY = object()


class CryptoRoundTripError(RuntimeError):
    """Stable fail-closed error for the independent round-trip generation."""


@dataclass(frozen=True)
class RoundTripCapitalPolicy:
    authority_id: str
    account_id: str
    generation: int
    initial_cash: Decimal
    currency: str = "USDT"
    aggregate_with_prior_generations: bool = False
    real_trading_enabled: bool = False
    execution_authority: bool = False
    production_eligible: bool = False


ROUND_TRIP_CAPITAL_POLICY = RoundTripCapitalPolicy(
    authority_id="crypto-round-trip-capital-v1",
    account_id="crypto_sim_round_trip",
    generation=2,
    initial_cash=Decimal("10000"),
)


def _canonical_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    raise CryptoRoundTripError("round_trip_payload_not_canonical")


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            _canonical_value(value),
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise CryptoRoundTripError("round_trip_payload_not_canonical") from exc


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _non_authority_fields() -> dict[str, Any]:
    return {
        "capital_layer": "simulated",
        "account_type": "simulated",
        "real_trading_enabled": False,
        "execution_eligible": False,
        "execution_authority": False,
        "durable_execution_receipt": False,
        "production_eligible": False,
        "network_used": False,
        "testnet_used": False,
        "live_broker_used": False,
        "model_network_used": False,
        "promotion_authorized": False,
        "automatic_promotion_enabled": False,
        "automatic_risk_expansion_enabled": False,
        "outbox_id": None,
        "capital_commit_id": None,
    }


def _decimal(value: Any, reason: str, *, nonnegative: bool = False) -> Decimal:
    if isinstance(value, (bool, float)) or value in (None, ""):
        raise CryptoRoundTripError(reason)
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise CryptoRoundTripError(reason) from exc
    if (
        not parsed.is_finite()
        or (nonnegative and parsed < ZERO)
        or (not nonnegative and parsed <= ZERO)
    ):
        raise CryptoRoundTripError(reason)
    return parsed


def _utc(value: Any, reason: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise CryptoRoundTripError(reason) from exc
    else:
        raise CryptoRoundTripError(reason)
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() is None
        or parsed.utcoffset() != timedelta(0)
        or parsed.second != 0
        or parsed.microsecond != 0
    ):
        raise CryptoRoundTripError(reason)
    return parsed.astimezone(timezone.utc)


def _floor_step(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def _ceil_step(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_UP) * step


def _money(value: Decimal, *, rounding: str = ROUND_DOWN) -> Decimal:
    return value.quantize(MONEY_QUANTUM, rounding=rounding)


def _validate_policy(policy: RoundTripCapitalPolicy) -> None:
    if policy != ROUND_TRIP_CAPITAL_POLICY:
        raise CryptoRoundTripError("round_trip_policy_not_canonical")
    if (
        policy.generation != 2
        or policy.initial_cash != Decimal("10000")
        or policy.aggregate_with_prior_generations is not False
        or policy.real_trading_enabled is not False
        or policy.execution_authority is not False
        or policy.production_eligible is not False
    ):
        raise CryptoRoundTripError("round_trip_policy_safety_invalid")


class RoundTripCapitalLedger:
    """Checksum-bound append-only ledger for capital generation 2."""

    def __init__(
        self,
        root: Path | str,
        *,
        policy: RoundTripCapitalPolicy = ROUND_TRIP_CAPITAL_POLICY,
        _capability: object | None = None,
    ) -> None:
        _validate_policy(policy)
        self.root = Path(root)
        self.policy = policy
        self.events_path = self.root / "events.jsonl"
        self.head_path = self.root / "head.json"
        self.lock_path = self.root / ".lock"
        self.cycle_lock_path = self.root / ".cycle.lock"
        self._write_enabled = _capability is _WRITE_CAPABILITY

    def _assert_safe_paths(self) -> None:
        if self.root.exists():
            node = self.root.lstat()
            if not stat.S_ISDIR(node.st_mode) or stat.S_ISLNK(node.st_mode):
                raise CryptoRoundTripError("round_trip_capital_root_untrusted")
        for path in (
            self.events_path,
            self.head_path,
            self.lock_path,
            self.cycle_lock_path,
        ):
            if not path.exists() and not path.is_symlink():
                continue
            node = path.lstat()
            if stat.S_ISLNK(node.st_mode):
                raise CryptoRoundTripError("round_trip_capital_symlink_not_allowed")
            if stat.S_ISREG(node.st_mode) and node.st_nlink != 1:
                raise CryptoRoundTripError("round_trip_capital_hardlink_not_allowed")

    def _require_writer(self) -> None:
        if not self._write_enabled:
            raise CryptoRoundTripError("round_trip_write_capability_required")

    @contextmanager
    def cycle(self) -> Iterator[None]:
        self._require_writer()
        self._assert_safe_paths()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._assert_safe_paths()
        with self.cycle_lock_path.open("a+", encoding="utf-8") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _empty_state() -> dict[str, Any]:
        return {
            "initialized": False,
            "cash": ZERO,
            "positions": {},
            "orders": {},
            "fees": ZERO,
            "realized_pnl": ZERO,
            "marks": {},
            "cycles": {},
            "last_slot_by_symbol": {},
        }

    def _read_rows_unlocked(self) -> list[dict[str, Any]]:
        self._assert_safe_paths()
        if not self.events_path.exists():
            return []
        raw = self.events_path.read_text(encoding="utf-8")
        if raw and not raw.endswith("\n"):
            raise CryptoRoundTripError("round_trip_ledger_partial_tail")
        rows: list[dict[str, Any]] = []
        for line_number, line in enumerate(raw.splitlines(), start=1):
            if not line:
                raise CryptoRoundTripError(
                    f"round_trip_ledger_blank_line:{line_number}"
                )
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise CryptoRoundTripError(
                    f"round_trip_ledger_json_invalid:{line_number}"
                ) from exc
            if not isinstance(row, dict):
                raise CryptoRoundTripError("round_trip_ledger_row_invalid")
            rows.append(row)
        return rows

    def _read_rows(
        self, *, require_existing_lock: bool = False
    ) -> list[dict[str, Any]]:
        if not self.root.exists():
            return []
        self._assert_safe_paths()
        try:
            stream = self.lock_path.open("r", encoding="utf-8")
        except FileNotFoundError:
            if require_existing_lock:
                raise CryptoRoundTripError("round_trip_readonly_lock_unavailable")
            stream = self.lock_path.open("a+", encoding="utf-8")
        with stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_SH)
            try:
                return self._read_rows_unlocked()
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def _validate_event(
        self,
        state: dict[str, Any],
        row: Mapping[str, Any],
        *,
        sequence: int,
        previous_checksum: str,
    ) -> None:
        expected_keys = {
            "contract",
            "sequence",
            "event_id",
            "event_type",
            "reference_id",
            "payload",
            "previous_checksum",
            "checksum",
        }
        if set(row) != expected_keys:
            raise CryptoRoundTripError("round_trip_event_schema_invalid")
        without = dict(row)
        checksum = str(without.pop("checksum", ""))
        if (
            row.get("contract") != ROUND_TRIP_LEDGER_CONTRACT
            or row.get("sequence") != sequence
            or row.get("previous_checksum") != previous_checksum
            or checksum != _sha256(without)
        ):
            raise CryptoRoundTripError("round_trip_event_chain_invalid")
        event_type = row.get("event_type")
        reference_id = row.get("reference_id")
        payload = row.get("payload")
        if (
            event_type not in {"opening", "cycle"}
            or not isinstance(reference_id, str)
            or not reference_id
            or not isinstance(payload, Mapping)
            or row.get("event_id")
            != f"crypto-round-trip-event-{_sha256({'event_type': event_type, 'reference_id': reference_id, 'payload': payload})[:24]}"
        ):
            raise CryptoRoundTripError("round_trip_event_envelope_invalid")
        if event_type == "opening":
            if state["initialized"]:
                raise CryptoRoundTripError("round_trip_opening_duplicated")
            expected = {
                "authority_id": self.policy.authority_id,
                "account_id": self.policy.account_id,
                "generation": self.policy.generation,
                "currency": self.policy.currency,
                "initial_cash": format(self.policy.initial_cash, "f"),
                "aggregate_with_prior_generations": False,
                **_non_authority_fields(),
            }
            if (
                reference_id
                != f"opening:{self.policy.authority_id}:g{self.policy.generation}"
                or _canonical_json(payload) != _canonical_json(expected)
            ):
                raise CryptoRoundTripError("round_trip_opening_invalid")
            state["initialized"] = True
            state["cash"] = self.policy.initial_cash
            return
        if not state["initialized"]:
            raise CryptoRoundTripError("round_trip_opening_required")
        self._apply_cycle(state, payload, reference_id=reference_id)

    def _replay(self, rows: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], str]:
        state = self._empty_state()
        previous = ""
        for index, row in enumerate(rows, start=1):
            self._validate_event(
                state,
                row,
                sequence=index,
                previous_checksum=previous,
            )
            previous = str(row["checksum"])
        return state, previous

    def _apply_cycle(
        self,
        state: dict[str, Any],
        payload: Mapping[str, Any],
        *,
        reference_id: str,
    ) -> None:
        required = {
            "contract",
            "cycle_id",
            "fixture_id",
            "symbol",
            "execution_slot",
            "decision",
            "quote",
            "instrument",
            "paper_fill_capacity",
            "exit_reason",
            "order",
            "receipt",
            "evidence_receipt_id",
            "market_evidence_sha256",
            "champion_id",
            "champion_sha256",
            "before",
            "after",
            *_non_authority_fields(),
        }
        if (
            set(payload) != required
            or payload.get("contract") != ROUND_TRIP_CYCLE_CONTRACT
        ):
            raise CryptoRoundTripError("round_trip_cycle_schema_invalid")
        cycle_id = str(payload.get("cycle_id") or "")
        symbol = str(payload.get("symbol") or "")
        execution_slot = _utc(
            payload.get("execution_slot"),
            "round_trip_execution_slot_invalid",
        )
        if (
            reference_id != f"cycle:{cycle_id}"
            or not cycle_id
            or symbol not in ALLOWED_SYMBOLS
            or payload.get("real_trading_enabled") is not False
            or payload.get("execution_authority") is not False
            or payload.get("production_eligible") is not False
            or cycle_id in state["cycles"]
        ):
            raise CryptoRoundTripError("round_trip_cycle_binding_invalid")
        previous_slot = state["last_slot_by_symbol"].get(symbol)
        if previous_slot is not None and execution_slot <= previous_slot:
            raise CryptoRoundTripError("round_trip_cycle_slot_not_monotonic")
        before = self._capital_checkpoint(state)
        if _canonical_json(payload.get("before")) != _canonical_json(before):
            raise CryptoRoundTripError("round_trip_cycle_before_mismatch")
        quote = payload.get("quote")
        instrument = payload.get("instrument")
        decision_raw = payload.get("decision")
        if (
            not isinstance(quote, Mapping)
            or not isinstance(instrument, Mapping)
            or not isinstance(decision_raw, Mapping)
        ):
            raise CryptoRoundTripError("round_trip_quote_invalid")
        bid = _decimal(quote.get("bid"), "round_trip_quote_invalid")
        ask = _decimal(quote.get("ask"), "round_trip_quote_invalid")
        if ask < bid:
            raise CryptoRoundTripError("round_trip_quote_invalid")
        typed_cycle = {
            "symbol": symbol,
            "execution_slot": execution_slot,
            "decision": _decision({"decision": decision_raw}),
            "quote": {"bid": bid, "ask": ask},
            "instrument": {
                "price_tick": _decimal(
                    instrument.get("price_tick"),
                    "round_trip_tick_invalid",
                ),
                "quantity_step": _decimal(
                    instrument.get("quantity_step"),
                    "round_trip_step_invalid",
                ),
                "min_quantity": _decimal(
                    instrument.get("min_quantity"),
                    "round_trip_min_quantity_invalid",
                ),
                "min_notional": _decimal(
                    instrument.get("min_notional"),
                    "round_trip_min_notional_invalid",
                ),
            },
        }
        if (
            bid % typed_cycle["instrument"]["price_tick"]
            or ask % typed_cycle["instrument"]["price_tick"]
        ):
            raise CryptoRoundTripError("round_trip_quote_invalid")
        position = state["positions"].get(symbol)
        expected_exit_reason = (
            _exit_reason(position=position, cycle=typed_cycle)
            if isinstance(position, Mapping)
            else None
        )
        if payload.get("exit_reason") != expected_exit_reason:
            raise CryptoRoundTripError("round_trip_exit_reason_mismatch")
        state["marks"][symbol] = bid
        order = payload.get("order")
        receipt = payload.get("receipt")
        if order is None or receipt is None:
            if order is not None or receipt is not None:
                raise CryptoRoundTripError("round_trip_order_receipt_pair_invalid")
        else:
            self._apply_order_receipt(
                state,
                order=order,
                receipt=receipt,
                cycle_id=cycle_id,
                symbol=symbol,
                execution_slot=execution_slot,
                quote=typed_cycle["quote"],
                instrument=typed_cycle["instrument"],
                cycle_binding=payload,
            )
        expected_side = (
            "sell"
            if expected_exit_reason is not None
            else (
                "buy"
                if position is None and typed_cycle["decision"]["action"] == "buy"
                else None
            )
        )
        actual_side = order.get("side") if isinstance(order, Mapping) else None
        if actual_side != expected_side:
            raise CryptoRoundTripError("round_trip_order_action_mismatch")
        state["cycles"][cycle_id] = _sha256(payload)
        state["last_slot_by_symbol"][symbol] = execution_slot
        after = self._capital_checkpoint(state)
        if _canonical_json(payload.get("after")) != _canonical_json(after):
            raise CryptoRoundTripError("round_trip_cycle_after_mismatch")

    def _apply_order_receipt(
        self,
        state: dict[str, Any],
        *,
        order: Any,
        receipt: Any,
        cycle_id: str,
        symbol: str,
        execution_slot: datetime,
        quote: Mapping[str, Decimal],
        instrument: Mapping[str, Decimal],
        cycle_binding: Mapping[str, Any],
    ) -> None:
        if not isinstance(order, Mapping) or not isinstance(receipt, Mapping):
            raise CryptoRoundTripError("round_trip_order_receipt_invalid")
        order_keys = {
            "contract",
            "intent_id",
            "cycle_id",
            "authority_id",
            "authority_generation",
            "account_id",
            "symbol",
            "side",
            "order_type",
            "quantity",
            "quote_bid",
            "quote_ask",
            "slippage_bps",
            "reference_price",
            "fee_rate",
            "execution_slot",
            "evidence_receipt_id",
            "market_evidence_sha256",
            "champion_id",
            "champion_sha256",
            *_non_authority_fields(),
        }
        receipt_keys = {
            "contract",
            "receipt_id",
            "cycle_id",
            "intent_id",
            "authority_id",
            "authority_generation",
            "account_id",
            "symbol",
            "side",
            "status",
            "reason_code",
            "requested_quantity",
            "filled_quantity",
            "average_price",
            "notional",
            "fee",
            "fee_asset",
            "filled_at",
            *_non_authority_fields(),
        }
        if (
            set(order) != order_keys
            or set(receipt) != receipt_keys
            or order.get("contract") != ROUND_TRIP_ORDER_CONTRACT
            or receipt.get("contract") != ROUND_TRIP_RECEIPT_CONTRACT
            or order.get("authority_id") != self.policy.authority_id
            or receipt.get("authority_id") != self.policy.authority_id
            or order.get("authority_generation") != self.policy.generation
            or receipt.get("authority_generation") != self.policy.generation
            or order.get("account_id") != self.policy.account_id
            or receipt.get("account_id") != self.policy.account_id
            or order.get("cycle_id") != cycle_id
            or receipt.get("cycle_id") != cycle_id
            or order.get("symbol") != symbol
            or receipt.get("symbol") != symbol
            or receipt.get("side") != order.get("side")
            or order.get("intent_id") != receipt.get("intent_id")
            or _utc(order.get("execution_slot"), "round_trip_order_slot_invalid")
            != execution_slot
            or _utc(receipt.get("filled_at"), "round_trip_receipt_time_invalid")
            != execution_slot
            or order.get("real_trading_enabled") is not False
            or receipt.get("real_trading_enabled") is not False
            or order.get("execution_authority") is not False
            or receipt.get("execution_authority") is not False
            or order.get("order_type") != "fixture_market_at_next_closed_bar_quote"
            or order.get("slippage_bps") != format(SLIPPAGE_BPS, "f")
            or order.get("fee_rate") != format(TAKER_FEE_RATE, "f")
            or order.get("quote_bid") != format(quote["bid"], "f")
            or order.get("quote_ask") != format(quote["ask"], "f")
            or receipt.get("fee_asset") != self.policy.currency
            or receipt.get("requested_quantity") != order.get("quantity")
            or order.get("evidence_receipt_id")
            != cycle_binding.get("evidence_receipt_id")
            or order.get("market_evidence_sha256")
            != cycle_binding.get("market_evidence_sha256")
            or order.get("champion_id") != cycle_binding.get("champion_id")
            or order.get("champion_sha256") != cycle_binding.get("champion_sha256")
        ):
            raise CryptoRoundTripError("round_trip_order_receipt_binding_invalid")
        side = str(order.get("side") or "")
        requested = _decimal(order.get("quantity"), "round_trip_order_quantity_invalid")
        filled = _decimal(
            receipt.get("filled_quantity"),
            "round_trip_receipt_quantity_invalid",
            nonnegative=True,
        )
        price = _decimal(
            receipt.get("average_price"),
            "round_trip_receipt_price_invalid",
            nonnegative=True,
        )
        notional = _decimal(
            receipt.get("notional"),
            "round_trip_receipt_notional_invalid",
            nonnegative=True,
        )
        fee = _decimal(
            receipt.get("fee"),
            "round_trip_receipt_fee_invalid",
            nonnegative=True,
        )
        status = str(receipt.get("status") or "")
        reference_price = _decimal(
            order.get("reference_price"),
            "round_trip_order_price_invalid",
        )
        expected_price = (
            _ceil_step(
                quote["ask"] * (Decimal("1") + SLIPPAGE_BPS / Decimal("10000")),
                instrument["price_tick"],
            )
            if side == "buy"
            else _floor_step(
                quote["bid"] * (Decimal("1") - SLIPPAGE_BPS / Decimal("10000")),
                instrument["price_tick"],
            )
        )
        expected_intent_id = (
            "crypto-round-trip-intent-"
            + _sha256(
                {
                    "cycle_id": cycle_id,
                    "side": side,
                    "quantity": requested,
                    "reference_price": reference_price,
                }
            )[:24]
        )
        expected_receipt_id = (
            "crypto-round-trip-receipt-"
            + _sha256(
                {
                    "intent_id": expected_intent_id,
                    "filled_quantity": filled,
                    "status": status,
                    "reason_code": receipt.get("reason_code"),
                }
            )[:24]
        )
        if status not in {
            "fixture_simulated",
            "fixture_partially_simulated",
            "fixture_rejected",
        }:
            raise CryptoRoundTripError("round_trip_receipt_status_invalid")
        if (
            filled > requested
            or requested % instrument["quantity_step"] != ZERO
            or filled % instrument["quantity_step"] != ZERO
            or requested < instrument["min_quantity"]
            or reference_price != expected_price
            or order.get("intent_id") != expected_intent_id
            or receipt.get("receipt_id") != expected_receipt_id
            or (filled > ZERO and price != reference_price)
            or notional != _money(filled * price)
            or fee != _money(notional * TAKER_FEE_RATE, rounding=ROUND_UP)
            or (status == "fixture_rejected" and any((filled, price, notional, fee)))
            or (
                status == "fixture_rejected"
                and receipt.get("reason_code")
                != "paper_liquidity_or_exchange_minimum_reject"
            )
            or (status != "fixture_rejected" and receipt.get("reason_code") is not None)
            or (
                status == "fixture_simulated"
                and (filled != requested or filled <= ZERO)
            )
            or (
                status == "fixture_partially_simulated"
                and not (ZERO < filled < requested)
            )
        ):
            raise CryptoRoundTripError("round_trip_receipt_values_invalid")
        intent_id = str(order.get("intent_id") or "")
        receipt_id = str(receipt.get("receipt_id") or "")
        if not intent_id or not receipt_id or intent_id in state["orders"]:
            raise CryptoRoundTripError("round_trip_order_identity_invalid")
        if side == "buy":
            if status != "fixture_simulated" or symbol in state["positions"]:
                raise CryptoRoundTripError("round_trip_buy_state_invalid")
            total = notional + fee
            if state["cash"] < total:
                raise CryptoRoundTripError("round_trip_buy_cash_invalid")
            state["cash"] -= total
            state["fees"] += fee
            state["positions"][symbol] = {
                "quantity": filled,
                "entry_price": price,
                "entry_notional": notional,
                "entry_fee": fee,
                "entry_time": execution_slot,
                "entry_receipt_id": receipt_id,
            }
        elif side == "sell":
            position = state["positions"].get(symbol)
            if not isinstance(position, dict):
                raise CryptoRoundTripError("round_trip_sell_position_missing")
            if filled > position["quantity"]:
                raise CryptoRoundTripError("round_trip_sell_quantity_exceeds_position")
            if status != "fixture_rejected":
                state["cash"] += notional - fee
                state["fees"] += fee
                original_quantity = position["quantity"]
                sold_fraction = filled / original_quantity
                sold_entry_notional = _money(position["entry_notional"] * sold_fraction)
                sold_entry_fee = _money(
                    position["entry_fee"] * sold_fraction,
                    rounding=ROUND_UP,
                )
                state["realized_pnl"] += (
                    notional - fee - sold_entry_notional - sold_entry_fee
                )
                remaining = original_quantity - filled
                if remaining == ZERO:
                    del state["positions"][symbol]
                else:
                    position["quantity"] = remaining
                    position["entry_notional"] -= sold_entry_notional
                    position["entry_fee"] -= sold_entry_fee
        else:
            raise CryptoRoundTripError("round_trip_order_side_invalid")
        state["orders"][intent_id] = _canonical_value(
            {
                **order,
                "status": status,
                "receipt_id": receipt_id,
                "filled_quantity": filled,
                "average_price": price,
                "notional": notional,
                "fee": fee,
            }
        )

    def _snapshot(self, state: Mapping[str, Any]) -> dict[str, Any]:
        position_value = ZERO
        for symbol, position in state["positions"].items():
            mark = state["marks"].get(symbol)
            if mark is None:
                mark = position["entry_price"]
            position_value += position["quantity"] * mark
        snapshot = {
            "authority_id": self.policy.authority_id,
            "account_id": self.policy.account_id,
            "generation": self.policy.generation,
            "currency": self.policy.currency,
            "initial_cash": self.policy.initial_cash,
            "cash": _money(state["cash"]),
            "positions": state["positions"],
            "orders": state["orders"],
            "fees": _money(state["fees"], rounding=ROUND_UP),
            "realized_pnl": _money(state["realized_pnl"]),
            "marks": state["marks"],
            "position_value": _money(position_value),
            "equity": _money(state["cash"] + position_value),
            "balanced": True,
            "aggregate_with_prior_generations": False,
            **_non_authority_fields(),
        }
        return _canonical_value(snapshot)

    def _capital_checkpoint(self, state: Mapping[str, Any]) -> dict[str, Any]:
        """Bounded conservation proof without copying order history per event."""

        snapshot = self._snapshot(state)
        orders = snapshot.pop("orders")
        snapshot["order_count"] = len(orders)
        snapshot["orders_sha256"] = _sha256(orders)
        return snapshot

    def _validated_state(self) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
        rows = self._read_rows()
        state, checksum = self._replay(rows)
        self._validate_head(rows, checksum)
        return rows, state, checksum

    def _validate_head(
        self,
        rows: Sequence[Mapping[str, Any]],
        checksum: str,
    ) -> None:
        if not rows:
            if self.head_path.exists():
                raise CryptoRoundTripError("round_trip_head_without_events")
            return
        if not self.head_path.exists():
            raise CryptoRoundTripError("round_trip_head_missing")
        try:
            head = json.loads(self.head_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CryptoRoundTripError("round_trip_head_json_invalid") from exc
        if _canonical_json(head) != _canonical_json(
            self._head_payload(len(rows), checksum)
        ):
            raise CryptoRoundTripError("round_trip_head_mismatch")

    def _head_payload(self, sequence: int, checksum: str) -> dict[str, Any]:
        return {
            "contract": ROUND_TRIP_HEAD_CONTRACT,
            "authority_id": self.policy.authority_id,
            "account_id": self.policy.account_id,
            "generation": self.policy.generation,
            "sequence": sequence,
            "checksum": checksum,
        }

    def _write_head(self, sequence: int, checksum: str) -> None:
        temporary = self.head_path.with_name(
            f".{self.head_path.name}.tmp-{os.getpid()}"
        )
        with temporary.open("x", encoding="utf-8") as stream:
            stream.write(_canonical_json(self._head_payload(sequence, checksum)) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.head_path)
        directory = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def _repair_head(
        self,
        rows: Sequence[Mapping[str, Any]],
        checksum: str,
    ) -> None:
        if not rows:
            return
        expected = self._head_payload(len(rows), checksum)
        if not self.head_path.exists():
            self._write_head(len(rows), checksum)
            return
        try:
            head = json.loads(self.head_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CryptoRoundTripError("round_trip_head_json_invalid") from exc
        if _canonical_json(head) == _canonical_json(expected):
            return
        sequence = head.get("sequence") if isinstance(head, Mapping) else None
        if (
            not isinstance(sequence, int)
            or isinstance(sequence, bool)
            or sequence < 1
            or sequence >= len(rows)
            or head.get("checksum") != rows[sequence - 1].get("checksum")
            or head.get("authority_id") != self.policy.authority_id
            or head.get("generation") != self.policy.generation
        ):
            raise CryptoRoundTripError("round_trip_head_mismatch")
        self._write_head(len(rows), checksum)

    def append(
        self,
        *,
        event_type: str,
        reference_id: str,
        payload: Mapping[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        self._require_writer()
        self._assert_safe_paths()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        canonical_payload = _canonical_value(payload)
        with self.lock_path.open("a+", encoding="utf-8") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            rows = self._read_rows_unlocked()
            state, checksum = self._replay(rows)
            self._repair_head(rows, checksum)
            for row in rows:
                if row.get("reference_id") != reference_id:
                    continue
                if row.get("event_type") != event_type or _canonical_json(
                    row.get("payload")
                ) != _canonical_json(canonical_payload):
                    raise CryptoRoundTripError("round_trip_reference_conflict")
                return dict(row), True
            sequence = len(rows) + 1
            without = {
                "contract": ROUND_TRIP_LEDGER_CONTRACT,
                "sequence": sequence,
                "event_id": f"crypto-round-trip-event-{_sha256({'event_type': event_type, 'reference_id': reference_id, 'payload': canonical_payload})[:24]}",
                "event_type": event_type,
                "reference_id": reference_id,
                "payload": canonical_payload,
                "previous_checksum": checksum,
            }
            event = {**without, "checksum": _sha256(without)}
            next_state = copy.deepcopy(state)
            self._validate_event(
                next_state,
                event,
                sequence=sequence,
                previous_checksum=checksum,
            )
            with self.events_path.open("a", encoding="utf-8") as events:
                events.write(_canonical_json(event) + "\n")
                events.flush()
                os.fsync(events.fileno())
            self._write_head(sequence, str(event["checksum"]))
            return event, False

    def ensure_opening(self) -> tuple[dict[str, Any], bool]:
        return self.append(
            event_type="opening",
            reference_id=(
                f"opening:{self.policy.authority_id}:g{self.policy.generation}"
            ),
            payload={
                "authority_id": self.policy.authority_id,
                "account_id": self.policy.account_id,
                "generation": self.policy.generation,
                "currency": self.policy.currency,
                "initial_cash": self.policy.initial_cash,
                "aggregate_with_prior_generations": False,
                **_non_authority_fields(),
            },
        )

    def state(self) -> dict[str, Any]:
        rows, state, checksum = self._validated_state()
        result = self._snapshot(state)
        result["head_sequence"] = len(rows)
        result["head_checksum"] = checksum
        return result

    def state_read_only(self) -> dict[str, Any]:
        """Validate the ledger/head under a shared existing lock only."""

        rows = self._read_rows(require_existing_lock=True)
        state, checksum = self._replay(rows)
        self._validate_head(rows, checksum)
        result = self._snapshot(state)
        result["head_sequence"] = len(rows)
        result["head_checksum"] = checksum
        return result

    def head(self) -> tuple[int, str]:
        rows, _, checksum = self._validated_state()
        return len(rows), checksum

    def state_for_writer(self) -> dict[str, Any]:
        self._require_writer()
        rows = self._read_rows_unlocked()
        state, checksum = self._replay(rows)
        self._repair_head(rows, checksum)
        return state

    def event_for_writer(self, reference_id: str) -> dict[str, Any] | None:
        self._require_writer()
        rows = self._read_rows_unlocked()
        _, checksum = self._replay(rows)
        self._repair_head(rows, checksum)
        matches = [row for row in rows if row.get("reference_id") == reference_id]
        if len(matches) > 1:
            raise CryptoRoundTripError("round_trip_reference_duplicated")
        return dict(matches[0]) if matches else None


def _decision(payload: Mapping[str, Any]) -> dict[str, Any]:
    raw = payload.get("decision")
    if not isinstance(raw, Mapping):
        raise CryptoRoundTripError("round_trip_decision_invalid")
    action = str(raw.get("action") or "")
    if action not in {"buy", "observe"}:
        raise CryptoRoundTripError("round_trip_decision_invalid")
    result = {
        "action": action,
        "regime_return": format(
            _signed_decimal(
                raw.get("regime_return"),
                "round_trip_decision_return_invalid",
            ),
            "f",
        ),
        "decision_return": format(
            _signed_decimal(
                raw.get("decision_return"),
                "round_trip_decision_return_invalid",
            ),
            "f",
        ),
        "decision_id": str(raw.get("decision_id") or ""),
    }
    if not result["decision_id"]:
        raise CryptoRoundTripError("round_trip_decision_invalid")
    return result


def _signed_decimal(value: Any, reason: str) -> Decimal:
    if isinstance(value, (bool, float)) or value in (None, ""):
        raise CryptoRoundTripError(reason)
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise CryptoRoundTripError(reason) from exc
    if not parsed.is_finite():
        raise CryptoRoundTripError(reason)
    return parsed


def _normalized_cycle_input(payload: Mapping[str, Any]) -> dict[str, Any]:
    symbol = str(payload.get("symbol") or "")
    fixture_id = str(payload.get("fixture_id") or "")
    receipt_id = str(payload.get("evidence_receipt_id") or "")
    evidence_digest = str(payload.get("market_evidence_sha256") or "")
    champion_id = str(payload.get("champion_id") or "")
    champion_digest = str(payload.get("champion_sha256") or "")
    quote = payload.get("quote")
    instrument = payload.get("instrument")
    if (
        symbol not in ALLOWED_SYMBOLS
        or not fixture_id
        or not receipt_id
        or len(evidence_digest) != 64
        or len(champion_digest) != 64
        or not champion_id
        or not isinstance(quote, Mapping)
        or not isinstance(instrument, Mapping)
    ):
        raise CryptoRoundTripError("round_trip_input_binding_invalid")
    bid = _decimal(quote.get("bid"), "round_trip_quote_invalid")
    ask = _decimal(quote.get("ask"), "round_trip_quote_invalid")
    tick = _decimal(instrument.get("price_tick"), "round_trip_tick_invalid")
    step = _decimal(instrument.get("quantity_step"), "round_trip_step_invalid")
    min_quantity = _decimal(
        instrument.get("min_quantity"),
        "round_trip_min_quantity_invalid",
    )
    min_notional = _decimal(
        instrument.get("min_notional"),
        "round_trip_min_notional_invalid",
    )
    if ask < bid or bid % tick or ask % tick:
        raise CryptoRoundTripError("round_trip_quote_invalid")
    return {
        "fixture_id": fixture_id,
        "symbol": symbol,
        "execution_slot": _utc(
            payload.get("execution_slot"),
            "round_trip_execution_slot_invalid",
        ),
        "decision": _decision(payload),
        "quote": {"bid": bid, "ask": ask},
        "instrument": {
            "price_tick": tick,
            "quantity_step": step,
            "min_quantity": min_quantity,
            "min_notional": min_notional,
        },
        "evidence_receipt_id": receipt_id,
        "market_evidence_sha256": evidence_digest,
        "champion_id": champion_id,
        "champion_sha256": champion_digest,
    }


def _exit_reason(
    *,
    position: Mapping[str, Any],
    cycle: Mapping[str, Any],
) -> str | None:
    bid = cycle["quote"]["bid"]
    entry_price = position["entry_price"]
    raw_return = bid / entry_price - Decimal("1")
    holding_seconds = int(
        (cycle["execution_slot"] - position["entry_time"]).total_seconds()
    )
    if holding_seconds < 0:
        raise CryptoRoundTripError("round_trip_holding_time_invalid")
    if raw_return <= STOP_LOSS_RETURN:
        return "stop_loss_threshold_reached"
    if raw_return >= TAKE_PROFIT_RETURN:
        return "take_profit_threshold_reached"
    if holding_seconds >= MAX_HOLD_SECONDS:
        return "max_holding_period_reached"
    decision = cycle["decision"]
    if (
        decision["action"] == "observe"
        and _signed_decimal(
            decision["regime_return"], "round_trip_regime_return_invalid"
        )
        < ZERO
        and _signed_decimal(
            decision["decision_return"], "round_trip_decision_return_invalid"
        )
        < ZERO
    ):
        return "momentum_reversal_observed"
    return None


def _order_base(
    *,
    cycle: Mapping[str, Any],
    cycle_id: str,
    side: str,
    quantity: Decimal,
    reference_price: Decimal,
) -> dict[str, Any]:
    intent_material = {
        "cycle_id": cycle_id,
        "side": side,
        "quantity": quantity,
        "reference_price": reference_price,
    }
    return {
        "contract": ROUND_TRIP_ORDER_CONTRACT,
        "intent_id": f"crypto-round-trip-intent-{_sha256(intent_material)[:24]}",
        "cycle_id": cycle_id,
        "authority_id": ROUND_TRIP_CAPITAL_POLICY.authority_id,
        "authority_generation": ROUND_TRIP_CAPITAL_POLICY.generation,
        "account_id": ROUND_TRIP_CAPITAL_POLICY.account_id,
        "symbol": cycle["symbol"],
        "side": side,
        "order_type": "fixture_market_at_next_closed_bar_quote",
        "quantity": quantity,
        "quote_bid": cycle["quote"]["bid"],
        "quote_ask": cycle["quote"]["ask"],
        "slippage_bps": SLIPPAGE_BPS,
        "reference_price": reference_price,
        "fee_rate": TAKER_FEE_RATE,
        "execution_slot": cycle["execution_slot"],
        "evidence_receipt_id": cycle["evidence_receipt_id"],
        "market_evidence_sha256": cycle["market_evidence_sha256"],
        "champion_id": cycle["champion_id"],
        "champion_sha256": cycle["champion_sha256"],
        **_non_authority_fields(),
    }


def _receipt(
    *,
    order: Mapping[str, Any],
    filled_quantity: Decimal,
    status: str,
    reason_code: str | None,
) -> dict[str, Any]:
    price = (
        _decimal(order["reference_price"], "round_trip_order_price_invalid")
        if filled_quantity > ZERO
        else ZERO
    )
    notional = _money(filled_quantity * price)
    fee = _money(notional * TAKER_FEE_RATE, rounding=ROUND_UP)
    material = {
        "intent_id": order["intent_id"],
        "filled_quantity": filled_quantity,
        "status": status,
        "reason_code": reason_code,
    }
    return {
        "contract": ROUND_TRIP_RECEIPT_CONTRACT,
        "receipt_id": f"crypto-round-trip-receipt-{_sha256(material)[:24]}",
        "cycle_id": order["cycle_id"],
        "intent_id": order["intent_id"],
        "authority_id": ROUND_TRIP_CAPITAL_POLICY.authority_id,
        "authority_generation": ROUND_TRIP_CAPITAL_POLICY.generation,
        "account_id": ROUND_TRIP_CAPITAL_POLICY.account_id,
        "symbol": order["symbol"],
        "side": order["side"],
        "status": status,
        "reason_code": reason_code,
        "requested_quantity": order["quantity"],
        "filled_quantity": filled_quantity,
        "average_price": price,
        "notional": notional,
        "fee": fee,
        "fee_asset": "USDT",
        "filled_at": order["execution_slot"],
        **_non_authority_fields(),
    }


def _build_buy(
    cycle: Mapping[str, Any],
    *,
    cycle_id: str,
    cash: Decimal,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    instrument = cycle["instrument"]
    price = _ceil_step(
        cycle["quote"]["ask"] * (Decimal("1") + SLIPPAGE_BPS / Decimal("10000")),
        instrument["price_tick"],
    )
    budget = min(
        ROUND_TRIP_CAPITAL_POLICY.initial_cash * POSITION_FRACTION,
        cash,
    )
    quantity = _floor_step(
        budget / (price * (Decimal("1") + TAKER_FEE_RATE)),
        instrument["quantity_step"],
    )
    if (
        quantity < instrument["min_quantity"]
        or quantity * price < instrument["min_notional"]
    ):
        return None, None
    order = _order_base(
        cycle=cycle,
        cycle_id=cycle_id,
        side="buy",
        quantity=quantity,
        reference_price=price,
    )
    return order, _receipt(
        order=order,
        filled_quantity=quantity,
        status="fixture_simulated",
        reason_code=None,
    )


def _build_sell(
    cycle: Mapping[str, Any],
    *,
    cycle_id: str,
    position: Mapping[str, Any],
    fill_capacity: Decimal | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    instrument = cycle["instrument"]
    requested = position["quantity"]
    price = _floor_step(
        cycle["quote"]["bid"] * (Decimal("1") - SLIPPAGE_BPS / Decimal("10000")),
        instrument["price_tick"],
    )
    order = _order_base(
        cycle=cycle,
        cycle_id=cycle_id,
        side="sell",
        quantity=requested,
        reference_price=price,
    )
    available = requested if fill_capacity is None else min(requested, fill_capacity)
    filled = _floor_step(available, instrument["quantity_step"])
    if (
        filled < instrument["min_quantity"]
        or filled * price < instrument["min_notional"]
    ):
        return order, _receipt(
            order=order,
            filled_quantity=ZERO,
            status="fixture_rejected",
            reason_code="paper_liquidity_or_exchange_minimum_reject",
        )
    status = (
        "fixture_simulated" if filled == requested else "fixture_partially_simulated"
    )
    return order, _receipt(
        order=order,
        filled_quantity=filled,
        status=status,
        reason_code=None,
    )


def run_round_trip_fixture_cycle(
    payload: Mapping[str, Any],
    *,
    output_root: Path | str,
    paper_fill_capacity: Decimal | None = None,
) -> dict[str, Any]:
    """Apply one validated causal fixture to capital generation 2."""

    _assert_simulation_only()
    _validate_policy(ROUND_TRIP_CAPITAL_POLICY)
    if paper_fill_capacity is not None:
        paper_fill_capacity = _decimal(
            paper_fill_capacity,
            "round_trip_fill_capacity_invalid",
            nonnegative=True,
        )
    cycle = _normalized_cycle_input(payload)
    cycle_id = f"crypto-round-trip-cycle-{_sha256({'fixture_id': cycle['fixture_id'], 'symbol': cycle['symbol'], 'execution_slot': cycle['execution_slot']})[:24]}"
    capital_root = Path(output_root) / "round_trip_capital"
    ledger = RoundTripCapitalLedger(
        capital_root,
        _capability=_WRITE_CAPABILITY,
    )
    with ledger.cycle():
        ledger.ensure_opening()
        state = ledger.state_for_writer()
        reference_id = f"cycle:{cycle_id}"
        existing = ledger.event_for_writer(reference_id)
        if existing is not None:
            stored_payload = existing.get("payload")
            expected_binding = {
                "fixture_id": cycle["fixture_id"],
                "symbol": cycle["symbol"],
                "execution_slot": _canonical_value(cycle["execution_slot"]),
                "decision": _canonical_value(cycle["decision"]),
                "quote": _canonical_value(cycle["quote"]),
                "instrument": _canonical_value(cycle["instrument"]),
                "paper_fill_capacity": _canonical_value(paper_fill_capacity),
                "evidence_receipt_id": cycle["evidence_receipt_id"],
                "market_evidence_sha256": cycle["market_evidence_sha256"],
                "champion_id": cycle["champion_id"],
                "champion_sha256": cycle["champion_sha256"],
            }
            if not isinstance(stored_payload, Mapping) or any(
                _canonical_json(stored_payload.get(key)) != _canonical_json(expected)
                for key, expected in expected_binding.items()
            ):
                raise CryptoRoundTripError("round_trip_reference_conflict")
            return _canonical_value(
                {
                    "contract": ROUND_TRIP_CYCLE_CONTRACT,
                    "cycle_id": cycle_id,
                    "exit_policy_id": FROZEN_EXIT_POLICY_ID,
                    "exit_reason": stored_payload.get("exit_reason"),
                    "order": stored_payload.get("order"),
                    "receipt": stored_payload.get("receipt"),
                    "capital": ledger.state(),
                    "idempotent_replay": True,
                    **_non_authority_fields(),
                }
            )
        before = ledger._capital_checkpoint(state)
        position = state["positions"].get(cycle["symbol"])
        exit_reason = (
            _exit_reason(position=position, cycle=cycle)
            if isinstance(position, Mapping)
            else None
        )
        order: dict[str, Any] | None = None
        receipt: dict[str, Any] | None = None
        if isinstance(position, Mapping) and exit_reason is not None:
            order, receipt = _build_sell(
                cycle,
                cycle_id=cycle_id,
                position=position,
                fill_capacity=paper_fill_capacity,
            )
        elif position is None and cycle["decision"]["action"] == "buy":
            order, receipt = _build_buy(
                cycle,
                cycle_id=cycle_id,
                cash=state["cash"],
            )
        projected = copy.deepcopy(state)
        projected["marks"][cycle["symbol"]] = cycle["quote"]["bid"]
        if order is not None and receipt is not None:
            ledger._apply_order_receipt(
                projected,
                order=_canonical_value(order),
                receipt=_canonical_value(receipt),
                cycle_id=cycle_id,
                symbol=cycle["symbol"],
                execution_slot=cycle["execution_slot"],
                quote=cycle["quote"],
                instrument=cycle["instrument"],
                cycle_binding={
                    "evidence_receipt_id": cycle["evidence_receipt_id"],
                    "market_evidence_sha256": cycle["market_evidence_sha256"],
                    "champion_id": cycle["champion_id"],
                    "champion_sha256": cycle["champion_sha256"],
                },
            )
        projected["cycles"][cycle_id] = ""
        projected["last_slot_by_symbol"][cycle["symbol"]] = cycle["execution_slot"]
        after = ledger._capital_checkpoint(projected)
        cycle_payload = {
            "contract": ROUND_TRIP_CYCLE_CONTRACT,
            "cycle_id": cycle_id,
            "fixture_id": cycle["fixture_id"],
            "symbol": cycle["symbol"],
            "execution_slot": cycle["execution_slot"],
            "decision": cycle["decision"],
            "quote": cycle["quote"],
            "instrument": cycle["instrument"],
            "paper_fill_capacity": paper_fill_capacity,
            "exit_reason": exit_reason,
            "order": order,
            "receipt": receipt,
            "evidence_receipt_id": cycle["evidence_receipt_id"],
            "market_evidence_sha256": cycle["market_evidence_sha256"],
            "champion_id": cycle["champion_id"],
            "champion_sha256": cycle["champion_sha256"],
            "before": before,
            "after": after,
            **_non_authority_fields(),
        }
        _, idempotent = ledger.append(
            event_type="cycle",
            reference_id=reference_id,
            payload=cycle_payload,
        )
        final = ledger.state()
    return _canonical_value(
        {
            "contract": ROUND_TRIP_CYCLE_CONTRACT,
            "cycle_id": cycle_id,
            "exit_policy_id": FROZEN_EXIT_POLICY_ID,
            "exit_reason": exit_reason,
            "order": order,
            "receipt": receipt,
            "capital": final,
            "idempotent_replay": idempotent,
            **_non_authority_fields(),
        }
    )


__all__ = [
    "CryptoRoundTripError",
    "FROZEN_EXIT_POLICY_ID",
    "MAX_HOLD_SECONDS",
    "ROUND_TRIP_CAPITAL_POLICY",
    "RoundTripCapitalLedger",
    "STOP_LOSS_RETURN",
    "TAKE_PROFIT_RETURN",
    "run_round_trip_fixture_cycle",
]
