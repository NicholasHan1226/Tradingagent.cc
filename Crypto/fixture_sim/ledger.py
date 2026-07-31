"""Process-locked append-only capital ledger for the Crypto fixture simulator."""

from __future__ import annotations

import copy
import fcntl
import json
import os
import stat
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_UP
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from Crypto.capital_policy import CRYPTO_CAPITAL_POLICY, CryptoCapitalPolicy

from .contracts import (
    ALLOWED_SYMBOLS,
    CAPITAL_HEAD_CONTRACT,
    CAPITAL_LEDGER_CONTRACT,
    CYCLE_CLAIM_CONTRACT,
    FROZEN_TAKER_FEE_RATE,
    MONEY_QUANTUM,
    PAPER_BROKER_CONTRACT,
    ZERO,
    CryptoLedgerError,
    _assert_canonical_policy,
    _assert_recursive_non_authority,
    _canonical_json,
    _canonical_value,
    _non_authority_fields,
    _sha256,
)


EVENT_ENVELOPE_KEYS = frozenset(
    {
        "contract",
        "sequence",
        "event_id",
        "event_type",
        "reference_id",
        "payload",
        "previous_checksum",
        "checksum",
    }
)
OPENING_PAYLOAD_KEYS = frozenset(
    {
        "authority_id",
        "account_id",
        "generation",
        "currency",
        "initial_cash",
        "capital_layer",
        "account_type",
        "real_trading_enabled",
        *_non_authority_fields(),
    }
)
CYCLE_CLAIM_PAYLOAD_KEYS = frozenset(
    {
        "contract",
        "run_id",
        "fixture_payload",
        "symbol",
        "execution_slot",
        "evidence_receipt_id",
        "market_evidence_sha256",
        "champion_sha256",
        "capital_authority_id",
        "capital_generation",
        "capital_account_id",
        "capital_currency",
        *_non_authority_fields(),
    }
)
CYCLE_CLAIM_WITH_VALUATION_PAYLOAD_KEYS = CYCLE_CLAIM_PAYLOAD_KEYS | frozenset(
    {"valuation_context"}
)


_LEDGER_WRITE_CAPABILITY = object()
RESERVE_PAYLOAD_KEYS = frozenset(
    {
        "run_id",
        "intent_id",
        "amount",
        "symbol",
        "quantity",
        "reference_price",
        "notional",
        "maximum_fee",
        "currency",
        "execution_slot",
        "evidence_receipt_id",
        "market_evidence_sha256",
        "champion_sha256",
        "capital_authority_id",
        "capital_generation",
        "capital_account_id",
        "before_snapshot",
        *_non_authority_fields(),
    }
)
FILL_PAYLOAD_KEYS = frozenset(
    {
        "run_id",
        "intent_id",
        "receipt_id",
        "broker_contract",
        "authority_id",
        "authority_generation",
        "account_id",
        "symbol",
        "side",
        "quantity",
        "price",
        "notional",
        "fee",
        "fee_asset",
        "filled_at",
        "evidence_receipt_id",
        "market_evidence_sha256",
        "champion_sha256",
        "status",
        "real_trading_enabled",
        *_non_authority_fields(),
    }
)
SNAPSHOT_PAYLOAD_KEYS = frozenset(
    {
        "authority_id",
        "account_id",
        "generation",
        "currency",
        "cash",
        "reserved_cash",
        "positions",
        "orders",
        "fees",
        "marks",
        "mark_slots",
        "valuation_slot",
        "position_value",
        "equity",
        "balanced",
        "capital_layer",
        "account_type",
        "real_trading_enabled",
        *_non_authority_fields(),
    }
)


class CryptoCapitalLedger:
    """Process-locked append-only Crypto capital ledger with head CAS."""

    def __init__(
        self,
        root: Path | str,
        *,
        policy: CryptoCapitalPolicy = CRYPTO_CAPITAL_POLICY,
        _write_capability: object | None = None,
    ) -> None:
        _assert_canonical_policy(policy)
        self.root = Path(root)
        self.policy = policy
        self.events_path = self.root / "events.jsonl"
        self.head_path = self.root / "head.json"
        self.lock_path = self.root / ".lock"
        self.cycle_lock_path = self.root / ".cycle.lock"
        self._write_enabled = _write_capability is _LEDGER_WRITE_CAPABILITY
        self._cycle_lock_held = False
        self._cycle_rows_cache: list[dict[str, Any]] | None = None
        self._cycle_state_cache: dict[str, Any] | None = None
        self._cycle_checksum_cache: str | None = None
        self._cycle_events_fingerprint: tuple[int, int, int, int] | None = None

    def _require_writer(self) -> None:
        if not self._write_enabled:
            raise CryptoLedgerError("capital_ledger_write_capability_required")

    def _assert_safe_paths(self) -> None:
        if self.root.exists():
            root_stat = self.root.lstat()
            if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
                raise CryptoLedgerError("capital_root_symlink_not_allowed")
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
                raise CryptoLedgerError("capital_nested_symlink_not_allowed")
            if stat.S_ISREG(node.st_mode) and node.st_nlink != 1:
                raise CryptoLedgerError("capital_hardlink_not_allowed")

    @contextmanager
    def _cycle_lock(self) -> Iterator[None]:
        """Serialize account-level check, reserve, fill, reconcile, and bundle."""

        self._require_writer()
        self._assert_safe_paths()
        self.root.mkdir(parents=True, exist_ok=True)
        self._assert_safe_paths()
        with self.cycle_lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            self._cycle_lock_held = True
            self._clear_cycle_cache()
            try:
                yield
            finally:
                self._clear_cycle_cache()
                self._cycle_lock_held = False
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _clear_cycle_cache(self) -> None:
        self._cycle_rows_cache = None
        self._cycle_state_cache = None
        self._cycle_checksum_cache = None
        self._cycle_events_fingerprint = None

    def _events_fingerprint(self) -> tuple[int, int, int, int] | None:
        if not self.events_path.exists():
            return None
        node = self.events_path.stat()
        return (node.st_dev, node.st_ino, node.st_size, node.st_mtime_ns)

    def _cache_cycle_replay(
        self,
        rows: Sequence[Mapping[str, Any]],
        state: dict[str, Any],
        checksum: str,
    ) -> None:
        if not self._cycle_lock_held:
            return
        self._cycle_rows_cache = [dict(row) for row in rows]
        self._cycle_state_cache = state
        self._cycle_checksum_cache = checksum
        self._cycle_events_fingerprint = self._events_fingerprint()

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
        rows = self._read_events()
        state, checksum = self._validate_and_replay(rows)
        self._cache_cycle_replay(rows, state, checksum)
        return rows, state, checksum

    @staticmethod
    def _empty_state() -> dict[str, Any]:
        return {
            "initialized": False,
            "cash": ZERO,
            "reserved_cash": ZERO,
            "positions": {},
            "orders": {},
            "fees": ZERO,
            "marks": {},
            "mark_slots": {},
            "cycle_claims": {},
            "reconciled_runs": set(),
            "last_valuation_by_symbol": {},
            "last_account_valuation_slot": None,
        }

    @staticmethod
    def _event_checksum(event_without_checksum: Mapping[str, Any]) -> str:
        return _sha256(event_without_checksum)

    def _read_events_unlocked(self) -> list[dict[str, Any]]:
        self._assert_safe_paths()
        if not self.events_path.exists():
            return []
        raw_text = self.events_path.read_text(encoding="utf-8")
        if raw_text and not raw_text.endswith("\n"):
            raise CryptoLedgerError("capital_ledger_partial_tail")
        rows: list[dict[str, Any]] = []
        for line_number, raw in enumerate(raw_text.splitlines(), start=1):
            if not raw.strip():
                raise CryptoLedgerError(f"capital_ledger_blank_line:{line_number}")
            try:
                row = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise CryptoLedgerError(
                    f"capital_ledger_invalid_json:{line_number}"
                ) from exc
            if not isinstance(row, dict):
                raise CryptoLedgerError(f"capital_ledger_row_invalid:{line_number}")
            rows.append(row)
        return rows

    def _read_events(self) -> list[dict[str, Any]]:
        if not self.root.exists():
            return []
        self._assert_safe_paths()
        try:
            lock = self.lock_path.open("r", encoding="utf-8")
        except FileNotFoundError:
            # A writable, brand-new ledger may not have published its lock
            # yet. Archived ledgers must already provide one; their read-only
            # mounts therefore never take this creation path.
            lock = self.lock_path.open("a+", encoding="utf-8")
        with lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_SH)
            try:
                return self._read_events_unlocked()
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _validate_and_replay(
        self, rows: Sequence[Mapping[str, Any]]
    ) -> tuple[dict[str, Any], str]:
        state = self._empty_state()
        previous_checksum = ""
        for index, row in enumerate(rows, start=1):
            if row.get("contract") != CAPITAL_LEDGER_CONTRACT:
                raise CryptoLedgerError("capital_ledger_contract_invalid")
            if type(row.get("sequence")) is not int or row.get("sequence") != index:
                raise CryptoLedgerError("capital_ledger_sequence_invalid")
            if row.get("previous_checksum") != previous_checksum:
                raise CryptoLedgerError("capital_ledger_previous_checksum_invalid")
            without_checksum = dict(row)
            actual_checksum = str(without_checksum.pop("checksum", ""))
            if actual_checksum != self._event_checksum(without_checksum):
                raise CryptoLedgerError("capital_ledger_checksum_invalid")
            self._apply_event(state, row)
            previous_checksum = actual_checksum
        self._validate_head(rows, previous_checksum)
        return state, previous_checksum

    def _validate_head(
        self, rows: Sequence[Mapping[str, Any]], previous_checksum: str
    ) -> None:
        if not rows:
            if self.head_path.exists():
                raise CryptoLedgerError("capital_head_without_events")
            return
        if not self.head_path.exists():
            raise CryptoLedgerError("capital_head_missing")
        try:
            head = json.loads(self.head_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CryptoLedgerError("capital_head_invalid_json") from exc
        expected = {
            "contract": CAPITAL_HEAD_CONTRACT,
            "authority_id": self.policy.authority_id,
            "account_id": self.policy.account_id,
            "generation": self.policy.generation,
            "sequence": len(rows),
            "checksum": previous_checksum,
        }
        if _canonical_json(head) != _canonical_json(expected):
            raise CryptoLedgerError("capital_head_mismatch")

    def _repair_head_locked(
        self,
        rows: Sequence[Mapping[str, Any]],
        checksum: str,
    ) -> None:
        """Repair only a missing or valid-prefix-lagging atomic head."""

        if not rows:
            if self.head_path.exists():
                raise CryptoLedgerError("capital_head_without_events")
            return
        expected = {
            "contract": CAPITAL_HEAD_CONTRACT,
            "authority_id": self.policy.authority_id,
            "account_id": self.policy.account_id,
            "generation": self.policy.generation,
            "sequence": len(rows),
            "checksum": checksum,
        }
        if not self.head_path.exists():
            self._write_head(sequence=len(rows), checksum=checksum)
            return
        try:
            head = json.loads(self.head_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CryptoLedgerError("capital_head_invalid_json") from exc
        if _canonical_json(head) == _canonical_json(expected):
            return
        if (
            not isinstance(head, Mapping)
            or head.get("contract") != CAPITAL_HEAD_CONTRACT
            or head.get("authority_id") != self.policy.authority_id
            or head.get("account_id") != self.policy.account_id
            or not self._matches_generation(head.get("generation"))
        ):
            raise CryptoLedgerError("capital_head_mismatch")
        sequence = head.get("sequence")
        if (
            isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence < 1
            or sequence >= len(rows)
            or head.get("checksum") != rows[sequence - 1].get("checksum")
        ):
            raise CryptoLedgerError("capital_head_mismatch")
        self._write_head(sequence=len(rows), checksum=checksum)

    def _recover_head(self) -> str:
        """Recover a head-write crash after validating the full event chain."""

        self._require_writer()
        self._assert_safe_paths()
        self.root.mkdir(parents=True, exist_ok=True)
        self._assert_safe_paths()
        with self.lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            rows = self._read_events_unlocked()
            state, checksum = self._replay_event_rows_without_head(rows)
            self._repair_head_locked(rows, checksum)
            self._cache_cycle_replay(rows, state, checksum)
            return checksum

    @staticmethod
    def _require_exact_keys(
        value: Mapping[str, Any], expected: frozenset[str], *, scope: str
    ) -> None:
        if set(value) != expected:
            raise CryptoLedgerError(f"{scope}_schema_mismatch")

    @staticmethod
    def _is_sha256(value: Any) -> bool:
        normalized = str(value or "").lower()
        return len(normalized) == 64 and all(
            character in "0123456789abcdef" for character in normalized
        )

    def _matches_generation(self, value: Any) -> bool:
        return type(value) is int and value == self.policy.generation

    def _validate_event_envelope(self, row: Mapping[str, Any]) -> None:
        self._require_exact_keys(
            row, EVENT_ENVELOPE_KEYS, scope="capital_event_envelope"
        )
        event_type = str(row.get("event_type") or "")
        reference_id = str(row.get("reference_id") or "")
        payload = row.get("payload")
        if (
            row.get("contract") != CAPITAL_LEDGER_CONTRACT
            or event_type
            not in {"opening", "cycle_claim", "reserve", "fill", "reconcile"}
            or not reference_id
            or not isinstance(payload, Mapping)
        ):
            raise CryptoLedgerError("capital_event_envelope_invalid")
        expected_event_id = (
            "crypto-capital-event-"
            + _sha256(
                {
                    "event_type": event_type,
                    "reference_id": reference_id,
                    "payload": payload,
                }
            )[:24]
        )
        if row.get("event_id") != expected_event_id:
            raise CryptoLedgerError("capital_event_id_invalid")

    def _apply_event(self, state: dict[str, Any], row: Mapping[str, Any]) -> None:
        self._validate_event_envelope(row)
        event_type = str(row.get("event_type") or "")
        payload = row.get("payload")
        if not isinstance(payload, Mapping):
            raise CryptoLedgerError("capital_event_payload_invalid")
        _assert_recursive_non_authority(payload, path="capital_event_payload")
        if event_type == "opening":
            self._require_exact_keys(
                payload, OPENING_PAYLOAD_KEYS, scope="capital_opening_payload"
            )
            if state["initialized"]:
                raise CryptoLedgerError("capital_opening_duplicated")
            if (
                row.get("reference_id")
                != f"opening:{self.policy.authority_id}:g{self.policy.generation}"
                or payload.get("authority_id") != self.policy.authority_id
                or payload.get("account_id") != self.policy.account_id
                or not self._matches_generation(payload.get("generation"))
                or payload.get("currency") != self.policy.currency
                or payload.get("capital_layer") != "simulated"
                or payload.get("account_type") != "simulated"
                or payload.get("real_trading_enabled") is not False
            ):
                raise CryptoLedgerError("capital_opening_authority_mismatch")
            amount = self._ledger_decimal(payload.get("initial_cash"), "initial_cash")
            if amount != self.policy.initial_cash:
                raise CryptoLedgerError("capital_opening_amount_mismatch")
            state["initialized"] = True
            state["cash"] = amount
            return
        if not state["initialized"]:
            raise CryptoLedgerError("capital_opening_required")
        if event_type == "cycle_claim":
            expected_claim_keys = (
                CYCLE_CLAIM_WITH_VALUATION_PAYLOAD_KEYS
                if "valuation_context" in payload
                else CYCLE_CLAIM_PAYLOAD_KEYS
            )
            self._require_exact_keys(
                payload, expected_claim_keys, scope="capital_cycle_claim_payload"
            )
            run_id = str(payload.get("run_id") or "")
            symbol = str(payload.get("symbol") or "")
            execution_slot = self._ledger_timestamp(
                payload.get("execution_slot"), "cycle_execution_slot"
            )
            valuation_context = payload.get("valuation_context")
            if valuation_context is not None:
                if not isinstance(valuation_context, Mapping) or set(
                    valuation_context
                ) != {"valuation_slot", "marks"}:
                    raise CryptoLedgerError("capital_valuation_context_invalid")
                valuation_slot = self._ledger_timestamp(
                    valuation_context.get("valuation_slot"),
                    "cycle_valuation_slot",
                )
                valuation_marks = valuation_context.get("marks")
                if (
                    valuation_slot != execution_slot
                    or not isinstance(valuation_marks, Mapping)
                    or len(valuation_marks) != len(ALLOWED_SYMBOLS)
                    or symbol not in valuation_marks
                    or any(
                        mark_symbol not in ALLOWED_SYMBOLS
                        for mark_symbol in valuation_marks
                    )
                ):
                    raise CryptoLedgerError("capital_valuation_context_invalid")
                for mark_symbol, raw_mark in valuation_marks.items():
                    if (
                        not isinstance(mark_symbol, str)
                        or not isinstance(raw_mark, Mapping)
                        or set(raw_mark)
                        != {
                            "price",
                            "observed_at",
                            "evidence_receipt_id",
                            "market_evidence_sha256",
                        }
                    ):
                        raise CryptoLedgerError("capital_valuation_mark_invalid")
                    mark_price = self._ledger_decimal(
                        raw_mark.get("price"),
                        f"valuation_mark_{mark_symbol}",
                    )
                    mark_observed_at = self._ledger_timestamp(
                        raw_mark.get("observed_at"),
                        f"valuation_mark_observed_at_{mark_symbol}",
                    )
                    if (
                        mark_price <= ZERO
                        or mark_observed_at != valuation_slot
                        or not str(raw_mark.get("evidence_receipt_id") or "").strip()
                        or not self._is_sha256(raw_mark.get("market_evidence_sha256"))
                    ):
                        raise CryptoLedgerError("capital_valuation_mark_invalid")
            if (
                payload.get("contract") != CYCLE_CLAIM_CONTRACT
                or row.get("reference_id") != f"cycle:{run_id}"
                or not run_id
                or symbol not in ALLOWED_SYMBOLS
                or not self._is_sha256(payload.get("market_evidence_sha256"))
                or not self._is_sha256(payload.get("champion_sha256"))
                or payload.get("capital_authority_id") != self.policy.authority_id
                or not self._matches_generation(payload.get("capital_generation"))
                or payload.get("capital_account_id") != self.policy.account_id
                or payload.get("capital_currency") != self.policy.currency
                or not str(payload.get("evidence_receipt_id") or "")
                or run_id in state["cycle_claims"]
            ):
                raise CryptoLedgerError("capital_cycle_claim_invalid")
            state["cycle_claims"][run_id] = {
                "symbol": symbol,
                "execution_slot": execution_slot,
                "evidence_receipt_id": str(payload["evidence_receipt_id"]),
                "market_evidence_sha256": str(payload["market_evidence_sha256"]),
                "champion_sha256": str(payload["champion_sha256"]),
                "capital_authority_id": str(payload["capital_authority_id"]),
                "capital_generation": payload["capital_generation"],
                "capital_account_id": str(payload["capital_account_id"]),
                "capital_currency": str(payload["capital_currency"]),
                "valuation_context": _canonical_value(valuation_context),
            }
            return
        if event_type == "reserve":
            self._require_exact_keys(
                payload, RESERVE_PAYLOAD_KEYS, scope="capital_reserve_payload"
            )
            run_id = str(payload.get("run_id") or "")
            claim = state["cycle_claims"].get(run_id)
            order_id = str(payload.get("intent_id") or "")
            amount = self._ledger_decimal(payload.get("amount"), "reserve_amount")
            symbol = str(payload.get("symbol") or "")
            quantity = self._ledger_decimal(payload.get("quantity"), "reserve_quantity")
            price = self._ledger_decimal(
                payload.get("reference_price"), "reserve_reference_price"
            )
            notional = self._ledger_decimal(payload.get("notional"), "reserve_notional")
            maximum_fee = self._ledger_decimal(
                payload.get("maximum_fee"), "reserve_maximum_fee"
            )
            frozen_fee = (notional * FROZEN_TAKER_FEE_RATE).quantize(
                MONEY_QUANTUM, rounding=ROUND_UP
            )
            if (
                not isinstance(claim, Mapping)
                or run_id in state["reconciled_runs"]
                or not order_id
                or amount <= ZERO
                or order_id in state["orders"]
                or row.get("reference_id") != f"reserve:{order_id}"
            ):
                raise CryptoLedgerError("capital_reservation_invalid")
            if (
                symbol not in ALLOWED_SYMBOLS
                or claim.get("symbol") != symbol
                or payload.get("currency") != self.policy.currency
                or payload.get("capital_authority_id") != self.policy.authority_id
                or not self._matches_generation(payload.get("capital_generation"))
                or payload.get("capital_account_id") != self.policy.account_id
                or payload.get("evidence_receipt_id")
                != claim.get("evidence_receipt_id")
                or payload.get("market_evidence_sha256")
                != claim.get("market_evidence_sha256")
                or payload.get("champion_sha256") != claim.get("champion_sha256")
                or self._ledger_timestamp(
                    payload.get("execution_slot"), "reserve_execution_slot"
                )
                != claim.get("execution_slot")
                or quantity <= ZERO
                or price <= ZERO
                or notional != quantity * price
                or maximum_fee != frozen_fee
                or amount != notional + maximum_fee
            ):
                raise CryptoLedgerError("capital_reservation_exposure_invalid")
            before_snapshot = payload.get("before_snapshot")
            if not isinstance(before_snapshot, Mapping):
                raise CryptoLedgerError("capital_reservation_before_snapshot_invalid")
            expected_before = self._snapshot_payload(
                state,
                marks=before_snapshot.get("marks", {}),
                mark_slots=before_snapshot.get("mark_slots", {}),
                valuation_slot=before_snapshot.get("valuation_slot"),
            )
            if claim.get("execution_slot") != self._ledger_timestamp(
                before_snapshot.get("valuation_slot"),
                "reserve_valuation_slot",
            ) or _canonical_json(before_snapshot) != _canonical_json(expected_before):
                raise CryptoLedgerError("capital_reservation_before_snapshot_invalid")
            if state["cash"] - state["reserved_cash"] < amount:
                raise CryptoLedgerError("capital_reservation_insufficient_cash")
            state["reserved_cash"] += amount
            state["orders"][order_id] = {
                "status": "reserved",
                "reserved_amount": amount,
                "symbol": symbol,
                "quantity": quantity,
                "reference_price": price,
                "notional": notional,
                "maximum_fee": maximum_fee,
                "run_id": run_id,
                "execution_slot": claim["execution_slot"],
                "evidence_receipt_id": claim["evidence_receipt_id"],
                "market_evidence_sha256": claim["market_evidence_sha256"],
                "champion_sha256": claim["champion_sha256"],
                "capital_authority_id": claim["capital_authority_id"],
                "capital_generation": claim["capital_generation"],
                "capital_account_id": claim["capital_account_id"],
                "currency": claim["capital_currency"],
            }
            return
        if event_type == "fill":
            self._require_exact_keys(
                payload, FILL_PAYLOAD_KEYS, scope="capital_fill_payload"
            )
            run_id = str(payload.get("run_id") or "")
            claim = state["cycle_claims"].get(run_id)
            intent_id = str(payload.get("intent_id") or "")
            order = state["orders"].get(intent_id)
            if (
                not isinstance(claim, Mapping)
                or run_id in state["reconciled_runs"]
                or not isinstance(order, dict)
                or order.get("status") != "reserved"
            ):
                raise CryptoLedgerError("capital_fill_without_reservation")
            if str(payload.get("side") or "") != "buy":
                raise CryptoLedgerError("capital_first_slice_supports_buy_only")
            quantity = self._ledger_decimal(payload.get("quantity"), "fill_quantity")
            price = self._ledger_decimal(payload.get("price"), "fill_price")
            notional = self._ledger_decimal(payload.get("notional"), "fill_notional")
            fee = self._ledger_decimal(payload.get("fee"), "fill_fee")
            filled_at = self._ledger_timestamp(payload.get("filled_at"), "fill_time")
            receipt_id = str(payload.get("receipt_id") or "")
            frozen_fee = (notional * FROZEN_TAKER_FEE_RATE).quantize(
                MONEY_QUANTUM, rounding=ROUND_UP
            )
            if (
                quantity <= ZERO
                or price <= ZERO
                or notional != quantity * price
                or fee != frozen_fee
                or not receipt_id
                or row.get("reference_id") != f"fill:{receipt_id}"
                or payload.get("broker_contract") != PAPER_BROKER_CONTRACT
                or payload.get("authority_id") != self.policy.authority_id
                or not self._matches_generation(payload.get("authority_generation"))
                or payload.get("account_id") != self.policy.account_id
                or payload.get("fee_asset") != self.policy.currency
                or payload.get("status") != "fixture_simulated"
                or payload.get("real_trading_enabled") is not False
            ):
                raise CryptoLedgerError("capital_fill_values_invalid")
            total = notional + fee
            if order["reserved_amount"] != total or state["reserved_cash"] < total:
                raise CryptoLedgerError("capital_fill_reservation_mismatch")
            if state["cash"] < total:
                raise CryptoLedgerError("capital_fill_insufficient_cash")
            symbol = str(payload.get("symbol") or "")
            if symbol not in ALLOWED_SYMBOLS:
                raise CryptoLedgerError("capital_fill_symbol_invalid")
            if (
                claim.get("symbol") != symbol
                or order.get("symbol") != symbol
                or order.get("quantity") != quantity
                or order.get("reference_price") != price
                or order.get("notional") != notional
                or order.get("maximum_fee") != fee
                or order.get("run_id") != run_id
                or filled_at != claim.get("execution_slot")
                or filled_at != order.get("execution_slot")
                or payload.get("evidence_receipt_id")
                != claim.get("evidence_receipt_id")
                or payload.get("evidence_receipt_id")
                != order.get("evidence_receipt_id")
                or payload.get("market_evidence_sha256")
                != claim.get("market_evidence_sha256")
                or payload.get("market_evidence_sha256")
                != order.get("market_evidence_sha256")
                or payload.get("champion_sha256") != claim.get("champion_sha256")
                or payload.get("champion_sha256") != order.get("champion_sha256")
            ):
                raise CryptoLedgerError("capital_fill_order_binding_mismatch")
            state["cash"] -= total
            state["reserved_cash"] -= total
            state["positions"][symbol] = state["positions"].get(symbol, ZERO) + quantity
            state["fees"] += fee
            order.update(
                {
                    "status": "fixture_simulated",
                    "receipt_id": receipt_id,
                    "quantity": quantity,
                    "price": price,
                    "fee": fee,
                    "filled_at": filled_at,
                    "evidence_receipt_id": str(payload["evidence_receipt_id"]),
                    "market_evidence_sha256": str(payload["market_evidence_sha256"]),
                    "champion_sha256": str(payload["champion_sha256"]),
                }
            )
            return
        if event_type == "reconcile":
            self._require_exact_keys(
                payload, SNAPSHOT_PAYLOAD_KEYS, scope="capital_reconcile_payload"
            )
            reference_id = str(row.get("reference_id") or "")
            if not reference_id.startswith("reconcile:"):
                raise CryptoLedgerError("capital_reconcile_reference_invalid")
            run_id = reference_id.removeprefix("reconcile:")
            claim = state["cycle_claims"].get(run_id)
            if not isinstance(claim, Mapping) or run_id in state["reconciled_runs"]:
                raise CryptoLedgerError("capital_reconcile_cycle_claim_missing")
            marks = payload.get("marks")
            mark_slots = payload.get("mark_slots")
            if not isinstance(marks, Mapping) or not isinstance(mark_slots, Mapping):
                raise CryptoLedgerError("capital_reconcile_marks_required")
            expected = self._snapshot_payload(
                state,
                marks=marks,
                mark_slots=mark_slots,
                valuation_slot=payload.get("valuation_slot"),
            )
            if _canonical_json(payload) != _canonical_json(expected):
                raise CryptoLedgerError("capital_reconcile_mismatch")
            valuation_context = claim.get("valuation_context")
            if valuation_context is not None:
                context_marks = valuation_context.get("marks")
                if not isinstance(context_marks, Mapping) or any(
                    marked_symbol not in context_marks
                    or _canonical_json(mark_price)
                    != _canonical_json(context_marks[marked_symbol].get("price"))
                    or _canonical_json(expected["mark_slots"].get(marked_symbol))
                    != _canonical_json(context_marks[marked_symbol].get("observed_at"))
                    for marked_symbol, mark_price in expected["marks"].items()
                ):
                    raise CryptoLedgerError(
                        "capital_reconcile_valuation_context_mismatch"
                    )
            valuation_time = self._ledger_timestamp(
                expected["valuation_slot"], "valuation_slot"
            )
            symbol = str(claim["symbol"])
            if valuation_time != claim["execution_slot"]:
                raise CryptoLedgerError("capital_reconcile_claim_slot_mismatch")
            previous_valuation = state["last_valuation_by_symbol"].get(symbol)
            if previous_valuation is not None and valuation_time <= previous_valuation:
                raise CryptoLedgerError("capital_reconcile_slot_not_monotonic")
            account_valuation = state["last_account_valuation_slot"]
            if account_valuation is not None and valuation_time < account_valuation:
                raise CryptoLedgerError("capital_account_valuation_regressed")
            for marked_symbol, previous_slot in state["mark_slots"].items():
                if marked_symbol not in expected["mark_slots"]:
                    continue
                incoming_slot = self._ledger_timestamp(
                    expected["mark_slots"][marked_symbol],
                    f"mark_slot_{marked_symbol}",
                )
                if incoming_slot < previous_slot:
                    raise CryptoLedgerError("capital_reconcile_mark_slot_regressed")
            state["marks"] = {
                symbol: self._ledger_decimal(value, f"mark_{symbol}")
                for symbol, value in expected["marks"].items()
            }
            state["mark_slots"] = {
                symbol: self._ledger_timestamp(value, f"mark_slot_{symbol}")
                for symbol, value in expected["mark_slots"].items()
            }
            state["reconciled_runs"].add(run_id)
            state["last_valuation_by_symbol"][symbol] = valuation_time
            state["last_account_valuation_slot"] = valuation_time
            return
        raise CryptoLedgerError(f"capital_event_type_unknown:{event_type}")

    @staticmethod
    def _ledger_decimal(value: Any, field_name: str) -> Decimal:
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise CryptoLedgerError(f"capital_{field_name}_invalid") from exc
        if not parsed.is_finite():
            raise CryptoLedgerError(f"capital_{field_name}_invalid")
        return parsed

    @staticmethod
    def _ledger_timestamp(value: Any, field_name: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise CryptoLedgerError(f"capital_{field_name}_invalid") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise CryptoLedgerError(f"capital_{field_name}_invalid")
        if parsed.second != 0 or parsed.microsecond != 0:
            raise CryptoLedgerError(f"capital_{field_name}_must_align_to_minute")
        return parsed.astimezone(timezone.utc)

    def _snapshot_payload(
        self,
        state: Mapping[str, Any],
        *,
        marks: Mapping[str, Any],
        mark_slots: Mapping[str, Any],
        valuation_slot: Any,
    ) -> dict[str, Any]:
        valuation_time = self._ledger_timestamp(valuation_slot, "valuation_slot")
        effective_marks = dict(state["marks"])
        effective_marks.update(marks)
        effective_slots = dict(state["mark_slots"])
        effective_slots.update(mark_slots)
        normalized_marks: dict[str, Decimal] = {}
        normalized_slots: dict[str, datetime] = {}
        position_value = ZERO
        for symbol, quantity in state["positions"].items():
            mark = self._ledger_decimal(effective_marks.get(symbol), f"mark_{symbol}")
            mark_slot = self._ledger_timestamp(
                effective_slots.get(symbol), f"mark_slot_{symbol}"
            )
            if mark <= ZERO:
                raise CryptoLedgerError("capital_reconcile_mark_invalid")
            if mark_slot > valuation_time or valuation_time - mark_slot > timedelta(
                minutes=5
            ):
                raise CryptoLedgerError("capital_reconcile_mark_stale")
            normalized_marks[symbol] = mark
            normalized_slots[symbol] = mark_slot
            position_value += quantity * mark
        normalized_orders = {
            intent_id: _canonical_value(order)
            for intent_id, order in sorted(state["orders"].items())
        }
        return {
            "authority_id": self.policy.authority_id,
            "account_id": self.policy.account_id,
            "generation": self.policy.generation,
            "currency": self.policy.currency,
            "cash": _canonical_value(state["cash"]),
            "reserved_cash": _canonical_value(state["reserved_cash"]),
            "positions": _canonical_value(dict(sorted(state["positions"].items()))),
            "orders": normalized_orders,
            "fees": _canonical_value(state["fees"]),
            "marks": _canonical_value(dict(sorted(normalized_marks.items()))),
            "mark_slots": _canonical_value(dict(sorted(normalized_slots.items()))),
            "valuation_slot": _canonical_value(valuation_time),
            "position_value": _canonical_value(position_value),
            "equity": _canonical_value(state["cash"] + position_value),
            "balanced": True,
            "capital_layer": "simulated",
            "account_type": "simulated",
            "real_trading_enabled": False,
            **_non_authority_fields(),
        }

    def snapshot(
        self,
        *,
        marks: Mapping[str, Any] | None = None,
        mark_slots: Mapping[str, Any] | None = None,
        valuation_slot: Any,
    ) -> dict[str, Any]:
        rows, state, checksum = self._validated_rows_state()
        payload = self._snapshot_payload(
            state,
            marks=marks or {},
            mark_slots=mark_slots or {},
            valuation_slot=valuation_slot,
        )
        payload["head_sequence"] = len(rows)
        payload["head_checksum"] = checksum
        return payload

    def head(self) -> tuple[int, str]:
        rows, _, checksum = self._validated_rows_state()
        return len(rows), checksum

    def cycle_guard(self, *, symbol: str) -> tuple[set[str], datetime | None]:
        _, state, _ = self._validated_rows_state()
        incomplete = set(state["cycle_claims"]) - set(state["reconciled_runs"])
        return incomplete, state["last_valuation_by_symbol"].get(symbol)

    def account_cycle_guard(
        self, *, symbol: str
    ) -> tuple[set[str], datetime | None, datetime | None]:
        _, state, _ = self._validated_rows_state()
        incomplete = set(state["cycle_claims"]) - set(state["reconciled_runs"])
        return (
            incomplete,
            state["last_valuation_by_symbol"].get(symbol),
            state["last_account_valuation_slot"],
        )

    def event_by_reference(self, reference_id: str) -> dict[str, Any] | None:
        rows, _, _ = self._validated_rows_state()
        matches = [row for row in rows if row.get("reference_id") == reference_id]
        if len(matches) > 1:
            raise CryptoLedgerError("capital_reference_id_duplicated")
        return dict(matches[0]) if matches else None

    def event_by_checksum(self, checksum: str) -> dict[str, Any] | None:
        rows, _, _ = self._validated_rows_state()
        matches = [row for row in rows if row.get("checksum") == checksum]
        if len(matches) > 1:
            raise CryptoLedgerError("capital_event_checksum_duplicated")
        return dict(matches[0]) if matches else None

    def _append_event(
        self,
        *,
        event_type: str,
        reference_id: str,
        payload: Mapping[str, Any],
        expected_head_checksum: str,
    ) -> tuple[dict[str, Any], bool]:
        self._require_writer()
        if not reference_id.strip():
            raise CryptoLedgerError("capital_reference_id_required")
        self._assert_safe_paths()
        self.root.mkdir(parents=True, exist_ok=True)
        self._assert_safe_paths()
        with self.lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            if (
                self._cycle_lock_held
                and self._cycle_rows_cache is not None
                and self._cycle_state_cache is not None
                and self._cycle_checksum_cache is not None
                and self._cycle_events_fingerprint == self._events_fingerprint()
            ):
                rows = self._cycle_rows_cache
                state = self._cycle_state_cache
                current_checksum = self._cycle_checksum_cache
            else:
                rows = self._read_events_unlocked()
                state, current_checksum = self._replay_event_rows_without_head(rows)
            self._repair_head_locked(rows, current_checksum)
            canonical_payload = _canonical_value(payload)
            for row in rows:
                if row.get("reference_id") != reference_id:
                    continue
                if row.get("event_type") != event_type or _canonical_json(
                    row.get("payload")
                ) != _canonical_json(canonical_payload):
                    raise CryptoLedgerError("capital_reference_id_conflict")
                return dict(row), True
            if expected_head_checksum != current_checksum:
                raise CryptoLedgerError("capital_head_cas_mismatch")
            sequence = len(rows) + 1
            event_without_checksum = {
                "contract": CAPITAL_LEDGER_CONTRACT,
                "sequence": sequence,
                "event_id": f"crypto-capital-event-{_sha256({'event_type': event_type, 'reference_id': reference_id, 'payload': canonical_payload})[:24]}",
                "event_type": event_type,
                "reference_id": reference_id,
                "payload": canonical_payload,
                "previous_checksum": current_checksum,
            }
            event = dict(event_without_checksum)
            event["checksum"] = self._event_checksum(event_without_checksum)
            next_state = copy.deepcopy(state)
            self._apply_event(next_state, event)
            with self.events_path.open("a", encoding="utf-8") as stream:
                stream.write(_canonical_json(event) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            directory_fd = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            self._write_head(sequence=sequence, checksum=event["checksum"])
            self._cache_cycle_replay(
                [*rows, event],
                next_state,
                event["checksum"],
            )
            return event, False

    def _validate_event_rows_without_head(
        self, rows: Sequence[Mapping[str, Any]]
    ) -> str:
        _, checksum = self._replay_event_rows_without_head(rows)
        return checksum

    def _replay_event_rows_without_head(
        self, rows: Sequence[Mapping[str, Any]]
    ) -> tuple[dict[str, Any], str]:
        state = self._empty_state()
        previous_checksum = ""
        for index, row in enumerate(rows, start=1):
            if (
                type(row.get("sequence")) is not int
                or row.get("sequence") != index
                or row.get("previous_checksum") != previous_checksum
            ):
                raise CryptoLedgerError("capital_candidate_chain_invalid")
            without_checksum = dict(row)
            checksum = str(without_checksum.pop("checksum", ""))
            if checksum != self._event_checksum(without_checksum):
                raise CryptoLedgerError("capital_candidate_checksum_invalid")
            self._apply_event(state, row)
            previous_checksum = checksum
        return state, previous_checksum

    def _write_head(self, *, sequence: int, checksum: str) -> None:
        payload = {
            "contract": CAPITAL_HEAD_CONTRACT,
            "authority_id": self.policy.authority_id,
            "account_id": self.policy.account_id,
            "generation": self.policy.generation,
            "sequence": sequence,
            "checksum": checksum,
        }
        temp = self.head_path.with_name(f".{self.head_path.name}.tmp-{os.getpid()}")
        with temp.open("w", encoding="utf-8") as stream:
            stream.write(_canonical_json(payload) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, self.head_path)
        directory_fd = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def _ensure_opening(self) -> tuple[dict[str, Any], bool]:
        self._require_writer()
        checksum = self._recover_head()
        return self._append_event(
            event_type="opening",
            reference_id=f"opening:{self.policy.authority_id}:g{self.policy.generation}",
            payload={
                "authority_id": self.policy.authority_id,
                "account_id": self.policy.account_id,
                "generation": self.policy.generation,
                "currency": self.policy.currency,
                "initial_cash": self.policy.initial_cash,
                "capital_layer": "simulated",
                "account_type": "simulated",
                "real_trading_enabled": False,
                **_non_authority_fields(),
            },
            expected_head_checksum=checksum,
        )


def _open_runtime_ledger(
    root: Path | str,
    *,
    policy: CryptoCapitalPolicy = CRYPTO_CAPITAL_POLICY,
) -> CryptoCapitalLedger:
    """Construct the package-private writer used by the fixture coordinator."""

    return CryptoCapitalLedger(
        root,
        policy=policy,
        _write_capability=_LEDGER_WRITE_CAPABILITY,
    )
