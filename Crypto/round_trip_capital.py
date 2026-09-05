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
ROUND_TRIP_RUNTIME_STATE_CONTRACT = (
    "tradingagent.crypto.round_trip_capital_runtime_state.v1"
)
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
COST_AWARE_CHALLENGER_CAPITAL_POLICY = RoundTripCapitalPolicy(
    authority_id="crypto-round-trip-cost-aware-challenger-capital-v1",
    account_id="crypto_sim_round_trip_cost_aware_challenger",
    generation=1,
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
    if policy not in {
        ROUND_TRIP_CAPITAL_POLICY,
        COST_AWARE_CHALLENGER_CAPITAL_POLICY,
    }:
        raise CryptoRoundTripError("round_trip_policy_not_canonical")
    if (
        policy.initial_cash != Decimal("10000")
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
        self.runtime_state_path = self.root / "runtime_state.json"
        self.lock_path = self.root / ".lock"
        self.cycle_lock_path = self.root / ".cycle.lock"
        self._write_enabled = _capability is _WRITE_CAPABILITY
        self._cycle_lock_held = False
        self._cycle_rows_cache: list[dict[str, Any]] | None = None
        self._cycle_state_cache: dict[str, Any] | None = None
        self._cycle_checksum_cache: str | None = None
        self._cycle_events_fingerprint: tuple[int, int, int, int] | None = None
        self._cycle_sequence_cache: int | None = None
        self._cycle_event_index: dict[str, dict[str, Any]] | None = None

    def _assert_safe_paths(self) -> None:
        if self.root.exists():
            node = self.root.lstat()
            if not stat.S_ISDIR(node.st_mode) or stat.S_ISLNK(node.st_mode):
                raise CryptoRoundTripError("round_trip_capital_root_untrusted")
        for path in (
            self.events_path,
            self.head_path,
            self.runtime_state_path,
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
            self._cycle_lock_held = True
            self._clear_cycle_cache()
            try:
                yield
            finally:
                self._clear_cycle_cache()
                self._cycle_lock_held = False
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def _clear_cycle_cache(self) -> None:
        self._cycle_rows_cache = None
        self._cycle_state_cache = None
        self._cycle_checksum_cache = None
        self._cycle_events_fingerprint = None
        self._cycle_sequence_cache = None
        self._cycle_event_index = None

    def _events_fingerprint(self) -> tuple[int, int, int, int] | None:
        if not self.events_path.exists():
            return (0, 0, 0, 0)
        node = self.events_path.stat()
        return (node.st_dev, node.st_ino, node.st_size, node.st_mtime_ns)

    def _cache_cycle_replay(
        self,
        rows: Sequence[Mapping[str, Any]],
        state: dict[str, Any],
        checksum: str,
        *,
        sequence: int | None = None,
        event_index: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        if not self._cycle_lock_held:
            return
        self._cycle_rows_cache = [dict(row) for row in rows]
        self._cycle_state_cache = state
        self._cycle_checksum_cache = checksum
        self._cycle_events_fingerprint = self._events_fingerprint()
        self._cycle_sequence_cache = len(rows) if sequence is None else sequence
        self._cycle_event_index = {
            str(reference_id): dict(item)
            for reference_id, item in (
                event_index
                or self._event_index_payload(
                    rows,
                    final_sequence=len(rows),
                    final_checksum=checksum,
                )
            ).items()
        }

    @staticmethod
    def _writer_state_payload(state: Mapping[str, Any]) -> dict[str, Any]:
        return _canonical_value(
            {
                "initialized": state["initialized"],
                "cash": state["cash"],
                "positions": state["positions"],
                "orders": state["orders"],
                "fees": state["fees"],
                "realized_pnl": state["realized_pnl"],
                "marks": state["marks"],
                "cycles": state["cycles"],
                "last_slot_by_symbol": state["last_slot_by_symbol"],
            }
        )

    @staticmethod
    def _writer_state_restore(raw: Mapping[str, Any]) -> dict[str, Any]:
        expected_keys = {
            "initialized",
            "cash",
            "positions",
            "orders",
            "fees",
            "realized_pnl",
            "marks",
            "cycles",
            "last_slot_by_symbol",
        }
        if set(raw) != expected_keys or not isinstance(raw.get("initialized"), bool):
            raise CryptoRoundTripError("round_trip_runtime_state_invalid")
        try:
            state = copy.deepcopy(dict(raw))
            for key in ("cash", "fees", "realized_pnl"):
                value = state[key]
                if not isinstance(value, str):
                    raise ValueError
                state[key] = Decimal(value)
                if not state[key].is_finite():
                    raise ValueError
            for mapping_key in (
                "positions",
                "orders",
                "marks",
                "cycles",
                "last_slot_by_symbol",
            ):
                if not isinstance(state[mapping_key], dict):
                    raise ValueError
            for symbol, position in state["positions"].items():
                if symbol not in ALLOWED_SYMBOLS or not isinstance(position, dict):
                    raise ValueError
                if set(position) != {
                    "quantity",
                    "entry_price",
                    "entry_notional",
                    "entry_fee",
                    "entry_time",
                    "entry_receipt_id",
                }:
                    raise ValueError
                for key in ("quantity", "entry_price", "entry_notional", "entry_fee"):
                    if not isinstance(position[key], str):
                        raise ValueError
                    position[key] = Decimal(position[key])
                    if not position[key].is_finite() or position[key] < ZERO:
                        raise ValueError
                position["entry_time"] = _utc(
                    position["entry_time"], "round_trip_runtime_state_invalid"
                )
                if not isinstance(position["entry_receipt_id"], str):
                    raise ValueError
            state["marks"] = {
                symbol: Decimal(value)
                for symbol, value in state["marks"].items()
                if symbol in ALLOWED_SYMBOLS and isinstance(value, str)
            }
            if len(state["marks"]) != len(raw["marks"]) or any(
                not value.is_finite() or value <= ZERO
                for value in state["marks"].values()
            ):
                raise ValueError
            state["last_slot_by_symbol"] = {
                symbol: _utc(value, "round_trip_runtime_state_invalid")
                for symbol, value in state["last_slot_by_symbol"].items()
                if symbol in ALLOWED_SYMBOLS
            }
            if len(state["last_slot_by_symbol"]) != len(raw["last_slot_by_symbol"]):
                raise ValueError
            if any(
                not isinstance(key, str)
                or not isinstance(value, str)
                or len(value) != 64
                for key, value in state["cycles"].items()
            ):
                raise ValueError
            return state
        except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
            raise CryptoRoundTripError("round_trip_runtime_state_invalid") from exc

    @staticmethod
    def _event_index_payload(
        rows: Sequence[Mapping[str, Any]],
        *,
        final_sequence: int,
        final_checksum: str,
    ) -> dict[str, dict[str, Any]]:
        index: dict[str, dict[str, Any]] = {}
        for row in rows:
            reference_id = str(row["reference_id"])
            if reference_id in index:
                raise CryptoRoundTripError("round_trip_reference_duplicated")
            index[reference_id] = {
                "reference_id": reference_id,
                "event_type": row["event_type"],
                "sequence": row["sequence"],
                "event_checksum": row["checksum"],
                "final_head_sequence": final_sequence,
                "final_head_checksum": final_checksum,
                "event": _canonical_value(row),
            }
        return index

    def _validated_rows_state(
        self,
    ) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
        if (
            self._cycle_lock_held
            and self._cycle_rows_cache is not None
            and self._cycle_state_cache is not None
            and self._cycle_checksum_cache is not None
            and self._cycle_events_fingerprint == self._events_fingerprint()
        ):
            return (
                self._cycle_rows_cache,
                self._cycle_state_cache,
                self._cycle_checksum_cache,
            )
        rows = self._read_rows()
        state, checksum = self._replay(rows)
        self._validate_head(rows, checksum)
        self._cache_cycle_replay(rows, state, checksum)
        return rows, state, checksum

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
        _order_proof: dict[str, Any] | None = None,
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
        self._apply_cycle(
            state, payload, reference_id=reference_id, _order_proof=_order_proof
        )

    def _replay(self, rows: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], str]:
        state = self._empty_state()
        previous = ""
        # This proof belongs to this replay only; no runtime/writer cache escapes.
        order_proof: dict[str, Any] = {}
        for index, row in enumerate(rows, start=1):
            self._validate_event(
                state,
                row,
                sequence=index,
                previous_checksum=previous,
                _order_proof=order_proof,
            )
            previous = str(row["checksum"])
        return state, previous

    def _apply_cycle(
        self,
        state: dict[str, Any],
        payload: Mapping[str, Any],
        *,
        reference_id: str,
        _order_proof: dict[str, Any] | None = None,
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
        before = self._capital_checkpoint(state, _order_proof=_order_proof)
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
            # Replay orders are append-only, and this is their sole mutation.
            if _order_proof is not None:
                _order_proof.clear()
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
        after = self._capital_checkpoint(state, _order_proof=_order_proof)
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

    def _snapshot(
        self, state: Mapping[str, Any], *, _include_orders: bool = True
    ) -> dict[str, Any]:
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
            **({"orders": state["orders"]} if _include_orders else {}),
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

    def _capital_checkpoint(
        self, state: Mapping[str, Any], *, _order_proof: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Preserve the wire proof without copying order history into a snapshot."""

        snapshot = self._snapshot(state, _include_orders=False)
        orders = state["orders"]
        # Existing canonical values are idempotent under _canonical_value, so
        # hashing raw state orders produces exactly the former snapshot digest.
        if _order_proof is None:
            digest = _sha256(orders)
        else:
            if not _order_proof:
                _order_proof["digest"] = _sha256(orders)
            digest = _order_proof["digest"]
        snapshot["order_count"] = len(orders)
        snapshot["orders_sha256"] = digest
        return snapshot

    def _validated_state(self) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
        return self._validated_rows_state()

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

    def _runtime_state_payload(
        self,
        *,
        sequence: int,
        checksum: str,
        state: Mapping[str, Any],
        events_size: int,
        aggregate: Mapping[str, Any] | None = None,
        legacy: bool = False,
        rows: Sequence[Mapping[str, Any]] = (),
        event_index: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        snapshot = self._snapshot(state)
        orders = snapshot["orders"]
        if aggregate is None:
            receipt_counts = {
                "buy": 0,
                "sell": 0,
                "fixture_simulated": 0,
                "fixture_partially_simulated": 0,
                "fixture_rejected": 0,
            }
            for order in orders.values():
                receipt_counts[str(order["side"])] += 1
                receipt_counts[str(order["status"])] += 1
            aggregate = {
                "position_count": len(state["positions"]),
                "order_count": len(orders),
                "receipt_counts": receipt_counts,
            }
        writer_state = self._writer_state_payload(state)
        index = (
            {
                str(reference_id): dict(item)
                for reference_id, item in event_index.items()
            }
            if event_index is not None
            else self._event_index_payload(
                rows,
                final_sequence=sequence,
                final_checksum=checksum,
            )
        )
        payload = {
            "contract": ROUND_TRIP_RUNTIME_STATE_CONTRACT,
            "sequence": sequence,
            "checksum": checksum,
            "events_size": events_size,
            "events_fingerprint": self._events_fingerprint(),
            "account_id": self.policy.account_id,
            "generation": self.policy.generation,
            "cash": snapshot["cash"],
            "fees": snapshot["fees"],
            "realized_pnl": snapshot["realized_pnl"],
            "equity": snapshot["equity"],
            "position_count": aggregate["position_count"],
            "order_count": aggregate["order_count"],
            "receipt_counts": aggregate["receipt_counts"],
            "orders_sha256": _sha256(orders),
            "cycle_count": len(state["cycles"]),
            "authority": _non_authority_fields(),
            "legacy_upgrade": legacy,
            "writer_state": writer_state,
            "writer_state_sha256": _sha256(writer_state),
            "event_index": index,
        }
        payload["state_sha256"] = _sha256(payload)
        return payload

    def _write_runtime_state(
        self,
        *,
        sequence: int,
        checksum: str,
        state: Mapping[str, Any],
        events_size: int,
        legacy: bool = False,
        rows: Sequence[Mapping[str, Any]] = (),
        event_index: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        payload = self._runtime_state_payload(
            sequence=sequence,
            checksum=checksum,
            state=state,
            events_size=events_size,
            legacy=legacy,
            rows=rows,
            event_index=event_index,
        )
        temporary = self.runtime_state_path.with_name(
            f".{self.runtime_state_path.name}.tmp-{os.getpid()}"
        )
        with temporary.open("x", encoding="utf-8") as stream:
            stream.write(_canonical_json(payload) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.runtime_state_path)
        directory = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return payload

    def _validate_runtime_state(
        self, state: Mapping[str, Any], *, expected_sequence: int, expected_checksum: str
    ) -> dict[str, Any]:
        material = dict(state)
        claimed = material.pop("state_sha256", None)
        if (
            state.get("contract") != ROUND_TRIP_RUNTIME_STATE_CONTRACT
            or claimed != _sha256(material)
            or state.get("sequence") != expected_sequence
            or state.get("checksum") != expected_checksum
            or not isinstance(state.get("events_size"), int)
            or state.get("events_size", -1) < 0
            or state.get("account_id") != self.policy.account_id
            or state.get("generation") != self.policy.generation
            or not isinstance(state.get("order_count"), int)
            or state.get("order_count", -1) < 0
            or not isinstance(state.get("cycle_count"), int)
            or state.get("cycle_count", -1) < 0
            or state.get("legacy_upgrade") is not False
            or not isinstance(state.get("writer_state"), Mapping)
            or state.get("writer_state_sha256") != _sha256(state.get("writer_state"))
            or not isinstance(state.get("event_index"), Mapping)
            or not isinstance(state.get("receipt_counts"), Mapping)
            or not isinstance(state.get("position_count"), int)
            or not isinstance(state.get("events_fingerprint"), (list, tuple))
            or len(state.get("events_fingerprint", ())) != 4
            or any(
                isinstance(item, bool) or not isinstance(item, int) or item < 0
                for item in state.get("events_fingerprint", ())
            )
        ):
            raise CryptoRoundTripError("round_trip_runtime_state_invalid")
        expected_receipt_keys = {
            "buy", "sell", "fixture_simulated", "fixture_partially_simulated", "fixture_rejected"
        }
        counts = state["receipt_counts"]
        if set(counts) != expected_receipt_keys or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in counts.values()
        ) or state["position_count"] > state["order_count"] or state["order_count"] > state["sequence"]:
            raise CryptoRoundTripError("round_trip_runtime_state_invalid")
        for key in ("cash", "fees", "realized_pnl", "equity"):
            value = state.get(key)
            if not isinstance(value, str):
                raise CryptoRoundTripError("round_trip_runtime_state_invalid")
            try:
                parsed = Decimal(value)
            except (InvalidOperation, ValueError) as exc:
                raise CryptoRoundTripError("round_trip_runtime_state_invalid") from exc
            if not parsed.is_finite() or format(parsed.quantize(MONEY_QUANTUM), "f") != value:
                raise CryptoRoundTripError("round_trip_runtime_state_invalid")
        if state["events_size"] != state["events_fingerprint"][2]:
            raise CryptoRoundTripError("round_trip_runtime_state_invalid")
        authority = state.get("authority")
        if not isinstance(authority, Mapping) or authority != _non_authority_fields():
            raise CryptoRoundTripError("round_trip_runtime_state_invalid")
        writer_state = self._writer_state_restore(state["writer_state"])
        snapshot = self._snapshot(writer_state)
        if (
            writer_state["initialized"] is not (expected_sequence > 0)
            or len(writer_state["positions"]) != state["position_count"]
            or len(writer_state["orders"]) != state["order_count"]
            or len(writer_state["cycles"]) != state["cycle_count"]
            or snapshot["cash"] != state["cash"]
            or snapshot["fees"] != state["fees"]
            or snapshot["realized_pnl"] != state["realized_pnl"]
            or snapshot["equity"] != state["equity"]
            or _sha256(snapshot["orders"]) != state["orders_sha256"]
        ):
            raise CryptoRoundTripError("round_trip_runtime_state_invalid")
        self._validate_event_index(
            state["event_index"],
            expected_sequence=expected_sequence,
            expected_checksum=expected_checksum,
        )
        return dict(state)

    def _validate_event_index(
        self,
        index: Mapping[str, Any],
        *,
        expected_sequence: int,
        expected_checksum: str,
    ) -> None:
        if len(index) != expected_sequence:
            raise CryptoRoundTripError("round_trip_runtime_state_invalid")
        ordered: list[Mapping[str, Any] | None] = [None] * expected_sequence
        expected_item_keys = {
            "reference_id",
            "event_type",
            "sequence",
            "event_checksum",
            "final_head_sequence",
            "final_head_checksum",
            "event",
        }
        expected_event_keys = {
            "contract",
            "sequence",
            "event_id",
            "event_type",
            "reference_id",
            "payload",
            "previous_checksum",
            "checksum",
        }
        for reference_id, item in index.items():
            if (
                not isinstance(reference_id, str)
                or not reference_id
                or not isinstance(item, Mapping)
                or set(item) != expected_item_keys
                or item.get("reference_id") != reference_id
                or item.get("final_head_sequence") != expected_sequence
                or item.get("final_head_checksum") != expected_checksum
            ):
                raise CryptoRoundTripError("round_trip_runtime_state_invalid")
            event = item.get("event")
            sequence = item.get("sequence")
            if (
                not isinstance(event, Mapping)
                or set(event) != expected_event_keys
                or not isinstance(sequence, int)
                or isinstance(sequence, bool)
                or sequence < 1
                or sequence > expected_sequence
                or ordered[sequence - 1] is not None
                or event.get("sequence") != sequence
                or event.get("reference_id") != reference_id
                or event.get("event_type") != item.get("event_type")
                or event.get("checksum") != item.get("event_checksum")
            ):
                raise CryptoRoundTripError("round_trip_runtime_state_invalid")
            material = dict(event)
            event_checksum = material.pop("checksum", None)
            event_type = event.get("event_type")
            payload = event.get("payload")
            if (
                event.get("contract") != ROUND_TRIP_LEDGER_CONTRACT
                or event_checksum != _sha256(material)
                or event_type not in {"opening", "cycle"}
                or not isinstance(payload, Mapping)
                or event.get("event_id")
                != f"crypto-round-trip-event-{_sha256({'event_type': event_type, 'reference_id': reference_id, 'payload': payload})[:24]}"
            ):
                raise CryptoRoundTripError("round_trip_runtime_state_invalid")
            ordered[sequence - 1] = event
        previous = ""
        for sequence, event in enumerate(ordered, start=1):
            if event is None or event.get("previous_checksum") != previous:
                raise CryptoRoundTripError("round_trip_runtime_state_fork")
            if (sequence == 1) is not (event.get("event_type") == "opening"):
                raise CryptoRoundTripError("round_trip_runtime_state_fork")
            previous = str(event["checksum"])
        if previous != expected_checksum:
            raise CryptoRoundTripError("round_trip_runtime_state_fork")

    def _repair_runtime_state_single_tail(
        self,
        *,
        runtime: Mapping[str, Any],
        head: Mapping[str, Any],
    ) -> tuple[list[dict[str, Any]], dict[str, Any], str, dict[str, dict[str, Any]]]:
        """Advance a trusted snapshot across exactly one durable ledger event.

        This is deliberately narrower than ``runtime_state_payload_for_rebuild``:
        it never scans or replays history.  It is only safe after the writer
        committed one canonical tail event and its matching head, then crashed
        before atomically replacing the adjacent runtime snapshot.
        """

        old_fingerprint = tuple(runtime["events_fingerprint"])
        current_fingerprint = self._events_fingerprint()
        if (
            current_fingerprint is None
            or current_fingerprint[:2] != old_fingerprint[:2]
            or old_fingerprint[2] != runtime["events_size"]
            or current_fingerprint[2] <= old_fingerprint[2]
        ):
            raise CryptoRoundTripError("round_trip_runtime_state_stale")
        try:
            with self.events_path.open("rb") as stream:
                stream.seek(old_fingerprint[2])
                tail_bytes = stream.read()
            if (
                tail_bytes.count(b"\n") != 1
                or not tail_bytes.endswith(b"\n")
                or not tail_bytes[:-1]
            ):
                raise ValueError
            tail = json.loads(tail_bytes[:-1].decode("utf-8"))
        except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            raise CryptoRoundTripError("round_trip_runtime_state_stale") from exc
        if not isinstance(tail, Mapping) or _canonical_json(tail).encode() + b"\n" != tail_bytes:
            raise CryptoRoundTripError("round_trip_runtime_state_stale")

        old_sequence = runtime["sequence"]
        old_checksum = runtime["checksum"]
        if (
            head["sequence"] != old_sequence + 1
            or head["checksum"] != tail.get("checksum")
        ):
            raise CryptoRoundTripError("round_trip_runtime_state_stale")
        event_index = {
            str(reference_id): dict(item)
            for reference_id, item in runtime["event_index"].items()
        }
        reference_id = tail.get("reference_id")
        if not isinstance(reference_id, str) or reference_id in event_index:
            raise CryptoRoundTripError("round_trip_runtime_state_stale")
        state = self._writer_state_restore(runtime["writer_state"])
        next_sequence = old_sequence + 1
        try:
            self._validate_event(
                state,
                tail,
                sequence=next_sequence,
                previous_checksum=old_checksum,
            )
        except CryptoRoundTripError as exc:
            raise CryptoRoundTripError("round_trip_runtime_state_stale") from exc

        rows = [
            dict(item["event"])
            for item in sorted(event_index.values(), key=lambda value: value["sequence"])
        ]
        next_rows = [*rows, dict(tail)]
        next_event_index = self._event_index_payload(
            next_rows,
            final_sequence=next_sequence,
            final_checksum=str(tail["checksum"]),
        )
        if self._events_fingerprint() != current_fingerprint:
            raise CryptoRoundTripError("round_trip_runtime_state_stale")
        self._write_runtime_state(
            sequence=next_sequence,
            checksum=str(tail["checksum"]),
            state=state,
            events_size=current_fingerprint[2],
            rows=next_rows,
            event_index=next_event_index,
        )
        return next_rows, state, str(tail["checksum"]), next_event_index

    def _writer_runtime_context(
        self,
        *,
        allow_single_tail_repair: bool = False,
    ) -> tuple[list[dict[str, Any]], dict[str, Any], str, dict[str, dict[str, Any]]]:
        if not self.runtime_state_path.exists():
            if (
                self.events_path.exists()
                or self.head_path.exists()
                or self._events_fingerprint() != (0, 0, 0, 0)
            ):
                raise CryptoRoundTripError("round_trip_runtime_state_missing")
            return [], self._empty_state(), "", {}
        if not self.head_path.exists() or not self.events_path.exists():
            raise CryptoRoundTripError("round_trip_runtime_state_stale")
        try:
            runtime = json.loads(self.runtime_state_path.read_text(encoding="utf-8"))
            head = json.loads(self.head_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CryptoRoundTripError("round_trip_runtime_state_invalid") from exc
        if (
            not isinstance(head, Mapping)
            or not isinstance(head.get("sequence"), int)
            or isinstance(head.get("sequence"), bool)
            or not isinstance(head.get("checksum"), str)
            or _canonical_json(head)
            != _canonical_json(
                self._head_payload(head.get("sequence"), head.get("checksum"))
            )
        ):
            raise CryptoRoundTripError("round_trip_head_mismatch")
        if (
            not isinstance(runtime, Mapping)
            or not isinstance(runtime.get("sequence"), int)
            or isinstance(runtime.get("sequence"), bool)
            or not isinstance(runtime.get("checksum"), str)
        ):
            raise CryptoRoundTripError("round_trip_runtime_state_invalid")
        runtime = self._validate_runtime_state(
            runtime,
            expected_sequence=runtime["sequence"],
            expected_checksum=runtime["checksum"],
        )
        if (
            runtime["sequence"] != head["sequence"]
            or runtime["checksum"] != head["checksum"]
        ):
            if not allow_single_tail_repair:
                raise CryptoRoundTripError("round_trip_runtime_state_stale")
            return self._repair_runtime_state_single_tail(runtime=runtime, head=head)
        if tuple(runtime["events_fingerprint"]) != self._events_fingerprint():
            raise CryptoRoundTripError("round_trip_runtime_state_stale")
        event_index = {
            str(reference_id): dict(item)
            for reference_id, item in runtime["event_index"].items()
        }
        rows = [
            dict(item["event"])
            for item in sorted(event_index.values(), key=lambda value: value["sequence"])
        ]
        return (
            rows,
            self._writer_state_restore(runtime["writer_state"]),
            str(runtime["checksum"]),
            event_index,
        )

    def runtime_state_payload_for_rebuild(self) -> dict[str, Any]:
        """Build a compact writer snapshot from an explicit full ledger audit.

        The caller owns installation of the returned payload after separately
        preserving the append-only ledger. Normal writer paths never call this
        recovery boundary and therefore never fall back to a history scan.
        """

        self._require_writer()
        rows = self._read_rows_unlocked()
        state, checksum = self._replay(rows)
        self._validate_head(rows, checksum)
        return self._runtime_state_payload(
            sequence=len(rows),
            checksum=checksum,
            state=state,
            events_size=(
                self.events_path.stat().st_size if self.events_path.exists() else 0
            ),
            rows=rows,
        )

    def _runtime_state_read_only(self) -> dict[str, Any]:
        if not self.runtime_state_path.exists():
            raise CryptoRoundTripError("round_trip_runtime_state_missing")
        try:
            state = json.loads(self.runtime_state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CryptoRoundTripError("round_trip_runtime_state_invalid") from exc
        if not self.head_path.exists():
            raise CryptoRoundTripError("round_trip_head_missing")
        try:
            head = json.loads(self.head_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CryptoRoundTripError("round_trip_head_json_invalid") from exc
        if (
            not isinstance(head, Mapping)
            or not isinstance(head.get("sequence"), int)
            or isinstance(head.get("sequence"), bool)
            or not isinstance(head.get("checksum"), str)
        ):
            raise CryptoRoundTripError("round_trip_head_mismatch")
        state = self._validate_runtime_state(
            state,
            expected_sequence=head["sequence"],
            expected_checksum=head["checksum"],
        )
        if tuple(state["events_fingerprint"]) != self._events_fingerprint():
            raise CryptoRoundTripError("round_trip_runtime_state_stale")
        if _canonical_json(head) != _canonical_json(
            self._head_payload(state["sequence"], state["checksum"])
        ):
            raise CryptoRoundTripError("round_trip_head_mismatch")
        result = {
            "authority_id": self.policy.authority_id,
            "account_id": self.policy.account_id,
            "generation": self.policy.generation,
            "currency": self.policy.currency,
            "initial_cash": format(self.policy.initial_cash, "f"),
            "cash": state["cash"],
            "fees": state["fees"],
            "realized_pnl": state["realized_pnl"],
            "equity": state["equity"],
            "position_count": state["position_count"],
            "order_count": state["order_count"],
            "receipt_counts": dict(state["receipt_counts"]),
            "balanced": True,
            "aggregate_with_prior_generations": False,
            **_non_authority_fields(),
        }
        result["head_sequence"] = state["sequence"]
        result["head_checksum"] = state["checksum"]
        return result

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
            if (
                self._cycle_lock_held
                and self._cycle_rows_cache is not None
                and self._cycle_state_cache is not None
                and self._cycle_checksum_cache is not None
                and self._cycle_sequence_cache is not None
                and self._cycle_event_index is not None
                and self._cycle_events_fingerprint == self._events_fingerprint()
            ):
                rows = self._cycle_rows_cache
                state = self._cycle_state_cache
                checksum = self._cycle_checksum_cache
                sequence = self._cycle_sequence_cache
                event_index = self._cycle_event_index
            else:
                rows, state, checksum, event_index = self._writer_runtime_context(
                    allow_single_tail_repair=True
                )
                sequence = len(rows)
            self._cache_cycle_replay(
                rows,
                state,
                checksum,
                sequence=sequence,
                event_index=event_index,
            )
            indexed = event_index.get(reference_id)
            if indexed is not None:
                row = indexed["event"]
                if row.get("event_type") != event_type or _canonical_json(
                    row.get("payload")
                ) != _canonical_json(canonical_payload):
                    raise CryptoRoundTripError("round_trip_reference_conflict")
                return dict(row), True
            sequence += 1
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
            next_rows = [*rows, event]
            next_event_index = self._event_index_payload(
                next_rows,
                final_sequence=sequence,
                final_checksum=str(event["checksum"]),
            )
            self._write_runtime_state(
                sequence=sequence,
                checksum=str(event["checksum"]),
                state=next_state,
                events_size=self.events_path.stat().st_size,
                rows=next_rows,
                event_index=next_event_index,
            )
            self._cache_cycle_replay(
                next_rows,
                next_state,
                str(event["checksum"]),
                sequence=sequence,
                event_index=next_event_index,
            )
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
        if (
            self._cycle_lock_held
            and self._cycle_state_cache is not None
            and self._cycle_checksum_cache is not None
            and self._cycle_sequence_cache is not None
            and self._cycle_events_fingerprint == self._events_fingerprint()
        ):
            result = self._snapshot(self._cycle_state_cache)
            result["head_sequence"] = self._cycle_sequence_cache
            result["head_checksum"] = self._cycle_checksum_cache
            return result
        rows, state, checksum = self._validated_state()
        result = self._snapshot(state)
        result["head_sequence"] = len(rows)
        result["head_checksum"] = checksum
        return result

    def state_read_only(self) -> dict[str, Any]:
        """Validate the ledger/head under a shared existing lock only."""
        try:
            stream = self.lock_path.open("r", encoding="utf-8")
        except FileNotFoundError as exc:
            raise CryptoRoundTripError("round_trip_readonly_lock_unavailable") from exc
        with stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_SH)
            try:
                return self._runtime_state_read_only()
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def events_read_only(self) -> list[dict[str, Any]]:
        """Return the fully replay-validated immutable event inventory.

        This is intentionally a read-side method for reports and labels.  It
        requires the already-created core lock and never repairs a head, opens
        a writer capability, or creates an account directory.
        """

        rows = self._read_rows(require_existing_lock=True)
        _, checksum = self._replay(rows)
        self._validate_head(rows, checksum)
        return [_canonical_value(row) for row in rows]

    def head(self) -> tuple[int, str]:
        if (
            self._cycle_lock_held
            and self._cycle_checksum_cache is not None
            and self._cycle_sequence_cache is not None
            and self._cycle_events_fingerprint == self._events_fingerprint()
        ):
            return self._cycle_sequence_cache, self._cycle_checksum_cache
        rows, _, checksum = self._validated_state()
        return len(rows), checksum

    def state_for_writer(self) -> dict[str, Any]:
        self._require_writer()
        if (
            self._cycle_lock_held
            and self._cycle_rows_cache is not None
            and self._cycle_state_cache is not None
            and self._cycle_checksum_cache is not None
            and self._cycle_sequence_cache is not None
            and self._cycle_event_index is not None
            and self._cycle_events_fingerprint == self._events_fingerprint()
        ):
            return self._cycle_state_cache
        rows, state, checksum, event_index = self._writer_runtime_context(
            allow_single_tail_repair=self._cycle_lock_held
        )
        self._cache_cycle_replay(
            rows,
            state,
            checksum,
            sequence=len(rows),
            event_index=event_index,
        )
        return state

    def event_for_writer(self, reference_id: str) -> dict[str, Any] | None:
        self._require_writer()
        if (
            self._cycle_lock_held
            and self._cycle_rows_cache is not None
            and self._cycle_state_cache is not None
            and self._cycle_checksum_cache is not None
            and self._cycle_sequence_cache is not None
            and self._cycle_event_index is not None
            and self._cycle_events_fingerprint == self._events_fingerprint()
        ):
            event_index = self._cycle_event_index
        else:
            rows, state, checksum, event_index = self._writer_runtime_context(
                allow_single_tail_repair=self._cycle_lock_held
            )
            self._cache_cycle_replay(
                rows,
                state,
                checksum,
                sequence=len(rows),
                event_index=event_index,
            )
        indexed = event_index.get(reference_id)
        return dict(indexed["event"]) if indexed is not None else None


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
    policy: RoundTripCapitalPolicy = ROUND_TRIP_CAPITAL_POLICY,
) -> dict[str, Any]:
    _validate_policy(policy)
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
        "authority_id": policy.authority_id,
        "authority_generation": policy.generation,
        "account_id": policy.account_id,
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
    policy: RoundTripCapitalPolicy = ROUND_TRIP_CAPITAL_POLICY,
) -> dict[str, Any]:
    _validate_policy(policy)
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
        "authority_id": policy.authority_id,
        "authority_generation": policy.generation,
        "account_id": policy.account_id,
        "symbol": order["symbol"],
        "side": order["side"],
        "status": status,
        "reason_code": reason_code,
        "requested_quantity": order["quantity"],
        "filled_quantity": filled_quantity,
        "average_price": price,
        "notional": notional,
        "fee": fee,
        "fee_asset": policy.currency,
        "filled_at": order["execution_slot"],
        **_non_authority_fields(),
    }


def _build_buy(
    cycle: Mapping[str, Any],
    *,
    cycle_id: str,
    cash: Decimal,
    policy: RoundTripCapitalPolicy = ROUND_TRIP_CAPITAL_POLICY,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    _validate_policy(policy)
    instrument = cycle["instrument"]
    price = _ceil_step(
        cycle["quote"]["ask"] * (Decimal("1") + SLIPPAGE_BPS / Decimal("10000")),
        instrument["price_tick"],
    )
    budget = min(
        policy.initial_cash * POSITION_FRACTION,
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
        policy=policy,
    )
    return order, _receipt(
        order=order,
        filled_quantity=quantity,
        status="fixture_simulated",
        reason_code=None,
        policy=policy,
    )


def _build_sell(
    cycle: Mapping[str, Any],
    *,
    cycle_id: str,
    position: Mapping[str, Any],
    fill_capacity: Decimal | None,
    policy: RoundTripCapitalPolicy = ROUND_TRIP_CAPITAL_POLICY,
) -> tuple[dict[str, Any], dict[str, Any]]:
    _validate_policy(policy)
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
        policy=policy,
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
            policy=policy,
        )
    status = (
        "fixture_simulated" if filled == requested else "fixture_partially_simulated"
    )
    return order, _receipt(
        order=order,
        filled_quantity=filled,
        status=status,
        reason_code=None,
        policy=policy,
    )


def run_round_trip_fixture_cycle(
    payload: Mapping[str, Any],
    *,
    output_root: Path | str,
    paper_fill_capacity: Decimal | None = None,
    policy: RoundTripCapitalPolicy = ROUND_TRIP_CAPITAL_POLICY,
) -> dict[str, Any]:
    """Apply one validated causal fixture to one frozen paper authority."""

    _assert_simulation_only()
    _validate_policy(policy)
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
        policy=policy,
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
                policy=policy,
            )
        elif position is None and cycle["decision"]["action"] == "buy":
            order, receipt = _build_buy(
                cycle,
                cycle_id=cycle_id,
                cash=state["cash"],
                policy=policy,
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
    "COST_AWARE_CHALLENGER_CAPITAL_POLICY",
    "CryptoRoundTripError",
    "FROZEN_EXIT_POLICY_ID",
    "MAX_HOLD_SECONDS",
    "ROUND_TRIP_CAPITAL_POLICY",
    "RoundTripCapitalLedger",
    "STOP_LOSS_RETURN",
    "TAKE_PROFIT_RETURN",
    "run_round_trip_fixture_cycle",
]
