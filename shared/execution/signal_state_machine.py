#!/usr/bin/env python3
"""Generic file-backed state machine for simulated signal cards.

Each state is a directory under ``signals/`` and transitions are atomic file
moves where that matters for isolated workers. Real/live cards are always
rejected; future market live adapters must not reuse this file queue.
"""

from __future__ import annotations

import fcntl
import errno
import json
import os
import re
import time as time_module
import uuid
from contextlib import contextmanager
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Iterator

from shared.markets.safety import reject_real_execution_payload

TRADINGAGENT_ROOT = Path("/opt/investment/tradingagent")
SIGNALS_DIR = TRADINGAGENT_ROOT / "signals"

PENDING = "pending"
CLAIMED = "claimed"
RUNNING = "running"
FILLED = "filled"
EXPIRED = "expired"
CANCELLED = "cancelled"
FAILED = "failed"
PARTIAL = "partial"

ACTIVE_STATES = (PENDING, CLAIMED, RUNNING)
TERMINAL_STATES = (FILLED, EXPIRED, CANCELLED, FAILED, PARTIAL)
SIGNAL_STATES = ACTIVE_STATES + TERMINAL_STATES

_ORDER_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
LOCK_RETRY_ATTEMPTS = 3
LOCK_RETRY_DELAY_SECONDS = 0.1
GOVERNED_SIM_MARKETS = frozenset({"ashare", "cn_futures", "crypto"})
IMMUTABLE_CARD_FIELDS = frozenset(
    {
        "order_id",
        "idempotency_key",
        "market",
        "account",
        "account_id",
        "authority_id",
        "broker_contract",
        "capital_layer",
        "account_type",
        "real_trading_enabled",
        "symbol",
        "ts_code",
        "side",
        "direction",
        "quantity",
        "price",
        "position_effect",
        "strategy_name",
        "signal_delivery_mode",
    }
)


class SignalStateConflict(RuntimeError):
    """Raised when a requested transition loses a state race."""


class SignalStateMachine:
    """Manage signal cards stored as JSON files under status directories."""

    def __init__(self, signals_dir: Path | str = SIGNALS_DIR) -> None:
        self.signals_dir = Path(signals_dir)
        self.lock_path = self.signals_dir / ".signal_state_machine.lock"

    def ensure_dirs(self) -> None:
        self.signals_dir.mkdir(parents=True, exist_ok=True)
        for state in SIGNAL_STATES:
            self.state_dir(state).mkdir(parents=True, exist_ok=True)

    def state_dir(self, state: str) -> Path:
        if state not in SIGNAL_STATES:
            raise ValueError(f"Invalid signal state: {state!r}")
        return self.signals_dir / state

    def path_for(self, state: str, order_id: str) -> Path:
        return self.state_dir(state) / f"{normalize_order_id(order_id)}.json"

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.signals_dir.mkdir(parents=True, exist_ok=True)
        with open(self.lock_path, "a", encoding="utf-8") as fh:
            self._acquire_exclusive_lock(fh.fileno())
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def _acquire_exclusive_lock(self, fd: int) -> None:
        last_error: OSError | None = None
        retry_errnos = {errno.EACCES, errno.EAGAIN, getattr(errno, "EWOULDBLOCK", errno.EAGAIN)}
        for attempt in range(1, LOCK_RETRY_ATTEMPTS + 1):
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except OSError as exc:
                if exc.errno not in retry_errnos:
                    raise
                last_error = exc
                if attempt < LOCK_RETRY_ATTEMPTS:
                    time_module.sleep(LOCK_RETRY_DELAY_SECONDS * attempt)
        raise TimeoutError(
            f"Could not acquire signal state lock {self.lock_path} after {LOCK_RETRY_ATTEMPTS} attempts"
        ) from last_error

    def write_pending(self, card: dict[str, Any]) -> dict[str, Any]:
        """Atomically create a globally unique pending signal card."""
        self.ensure_dirs()
        order_id = normalize_order_id(str(card.get("order_id", "")))
        idempotency_key = str(card.get("idempotency_key", "")).strip()
        card = dict(card)
        card["order_id"] = order_id
        card["status"] = PENDING
        _assert_non_live_card(card)

        with self._locked():
            existing_path, existing_card = self.find_by_order_id(order_id)
            if existing_path is not None:
                raise SignalStateConflict(
                    f"Signal order_id already exists in {existing_card.get('status', existing_path.parent.name)}"
                )
            if idempotency_key:
                duplicate_path, duplicate_card = self.find_by_idempotency_key(idempotency_key)
                if duplicate_path is not None:
                    raise SignalStateConflict(
                        "Signal idempotency_key already exists "
                        f"for order_id {duplicate_card.get('order_id', duplicate_path.stem)}"
                    )

            pending_path = self.path_for(PENDING, order_id)
            tmp_path = pending_path.with_name(f".{pending_path.name}.{uuid.uuid4().hex}.tmp")
            write_json(tmp_path, card)
            try:
                os.link(tmp_path, pending_path)
            except FileExistsError as exc:
                raise SignalStateConflict("Pending signal card already exists") from exc
            finally:
                tmp_path.unlink(missing_ok=True)

        return {"order_id": order_id, "status": PENDING, "signal_path": str(pending_path), "signal_card": card}

    def claim(self, order_id: str, worker_id: str | None = None) -> dict[str, Any]:
        """Atomically move a pending signal into claimed for one cron worker."""
        self.ensure_dirs()
        order_id = normalize_order_id(order_id)
        pending_path = self.path_for(PENDING, order_id)
        claimed_path = self.path_for(CLAIMED, order_id)

        with self._locked():
            if not pending_path.exists():
                existing_path, existing_card = self.find_by_order_id(order_id)
                status = existing_card.get("status", existing_path.parent.name) if existing_path else "not_found"
                raise SignalStateConflict(f"Signal cannot be claimed from status {status}")

            card = read_json(pending_path)
            _assert_non_live_card(card)
            card["status"] = CLAIMED
            card["claimed_at"] = now_iso()
            if worker_id:
                card["claimed_by"] = worker_id
            write_json(pending_path, card)
            os.rename(pending_path, claimed_path)

        return {"order_id": order_id, "status": CLAIMED, "signal_path": str(claimed_path), "signal_card": card}

    def mark_running(self, order_id: str, worker_id: str | None = None) -> dict[str, Any]:
        return self._move_active(order_id, CLAIMED, RUNNING, {"running_at": now_iso(), "running_by": worker_id})

    def fill(self, order_id: str, fill_info: dict[str, Any] | None = None, partial: bool = False) -> dict[str, Any]:
        """Mark a signal filled. Filled wins over a pending cancel request."""
        self.ensure_dirs()
        target_state = PARTIAL if partial else FILLED
        order_id = normalize_order_id(order_id)
        fill_info = dict(fill_info or {})

        with self._locked():
            existing_path, existing_card = self.find_by_order_id(order_id)
            if existing_path is None:
                raise FileNotFoundError(f"Signal not found: {order_id}")

            current_status = str(existing_card.get("status", existing_path.parent.name))
            if current_status in (FILLED, PARTIAL):
                return {
                    "order_id": order_id,
                    "status": current_status,
                    "signal_path": str(existing_path),
                    "signal_card": existing_card,
                    "message": "Signal already filled",
                }
            if current_status in (EXPIRED, FAILED):
                raise SignalStateConflict(f"Signal cannot be filled from status {current_status}")
            if current_status not in (CLAIMED, RUNNING):
                raise SignalStateConflict(f"Signal cannot be filled from status {current_status}")
            if current_status == CANCELLED and not existing_card.get("cancel_requested"):
                raise SignalStateConflict("Signal cannot be filled after final cancellation")

            card = dict(existing_card)
            for field in IMMUTABLE_CARD_FIELDS:
                if field not in fill_info:
                    continue
                if fill_info[field] != existing_card.get(field):
                    raise SignalStateConflict(
                        f"Fill cannot overwrite immutable signal field {field}"
                    )
                fill_info.pop(field)
            card.update(fill_info)
            card["status"] = target_state
            card.setdefault("fill_time", now_iso())
            card["filled_at"] = card.get("fill_time") or now_iso()
            _assert_non_live_card(card)
            target_path = self.path_for(target_state, order_id)
            write_json(existing_path, card)
            os.rename(existing_path, target_path)

        return {"order_id": order_id, "status": target_state, "signal_path": str(target_path), "signal_card": card}

    def cancel(self, order_id: str, reason: str = "") -> dict[str, Any]:
        """Cancel pending signals; for claimed/running mark cancel_requested."""
        self.ensure_dirs()
        order_id = normalize_order_id(order_id)

        with self._locked():
            existing_path, existing_card = self.find_by_order_id(order_id)
            if existing_path is None:
                return {"order_id": order_id, "status": "not_found", "message": "Signal not found"}

            current_status = str(existing_card.get("status", existing_path.parent.name))
            if current_status in (FILLED, PARTIAL):
                return {
                    "order_id": order_id,
                    "status": "cannot_cancel_filled",
                    "signal_path": str(existing_path),
                    "message": "Fill card already exists; filled signal wins over cancel",
                }
            if current_status == CANCELLED:
                return {
                    "order_id": order_id,
                    "status": "already_cancelled",
                    "signal_path": str(existing_path),
                    "signal_card": existing_card,
                }
            if current_status == PENDING:
                card = dict(existing_card)
                card["status"] = CANCELLED
                card["cancelled_at"] = now_iso()
                if reason:
                    card["cancel_reason"] = reason
                _assert_non_live_card(card)
                target_path = self.path_for(CANCELLED, order_id)
                write_json(existing_path, card)
                os.rename(existing_path, target_path)
                return {
                    "order_id": order_id,
                    "status": CANCELLED,
                    "signal_path": str(target_path),
                    "signal_card": card,
                }
            if current_status in (CLAIMED, RUNNING):
                card = dict(existing_card)
                card["cancel_requested"] = True
                card["cancel_requested_at"] = now_iso()
                if reason:
                    card["cancel_reason"] = reason
                _assert_non_live_card(card)
                write_json(existing_path, card)
                return {
                    "order_id": order_id,
                    "status": "cancel_requested",
                    "signal_path": str(existing_path),
                    "signal_card": card,
                    "message": f"Signal is {current_status}; cancellation requested without overwriting execution state",
                }

            raise SignalStateConflict(f"Signal cannot be cancelled from status {current_status}")

    def fail(self, order_id: str, reason: str = "", details: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = {"failed_at": now_iso()}
        if reason:
            payload["failure_reason"] = reason
        if details:
            payload["failure_details"] = details
        return self._move_first_available(order_id, (PENDING, CLAIMED, RUNNING), FAILED, payload)

    def sweep_expired(self, now: datetime | None = None) -> dict[str, Any]:
        """Move pending cards whose valid_until is before now into expired."""
        self.ensure_dirs()
        now = now or datetime.now().astimezone()
        expired: list[dict[str, Any]] = []

        with self._locked():
            for pending_path in sorted(self.state_dir(PENDING).glob("*.json")):
                card = read_json(pending_path)
                _assert_non_live_card(card)
                valid_until = card.get("valid_until")
                if not is_expired(valid_until, now):
                    continue
                card["status"] = EXPIRED
                card["expired_at"] = now_iso(now)
                expired_path = self.path_for(EXPIRED, str(card.get("order_id", pending_path.stem)))
                write_json(pending_path, card)
                os.rename(pending_path, expired_path)
                expired.append({"order_id": card.get("order_id", pending_path.stem), "signal_path": str(expired_path)})

        return {"status": "ok", "expired_count": len(expired), "expired": expired}

    def find_by_order_id(self, order_id: str) -> tuple[Path | None, dict[str, Any]]:
        order_id = normalize_order_id(order_id)
        matches: list[tuple[Path, dict[str, Any]]] = []
        for state in SIGNAL_STATES:
            path = self.path_for(state, order_id)
            if path.exists():
                card = read_json(path)
                _assert_non_live_card(card)
                card.setdefault("status", state)
                matches.append((path, card))
        if len(matches) > 1:
            states = ",".join(path.parent.name for path, _ in matches)
            raise SignalStateConflict(
                f"Signal order_id exists in multiple states: {states}"
            )
        if matches:
            return matches[0]
        return None, {}

    def find_by_idempotency_key(self, idempotency_key: str) -> tuple[Path | None, dict[str, Any]]:
        if not idempotency_key:
            return None, {}
        for state in SIGNAL_STATES:
            for path in self.state_dir(state).glob("*.json"):
                card = read_json(path)
                _assert_non_live_card(card)
                if str(card.get("idempotency_key", "")) == idempotency_key:
                    card.setdefault("status", state)
                    return path, card
        return None, {}

    def _move_active(self, order_id: str, from_state: str, to_state: str, updates: dict[str, Any]) -> dict[str, Any]:
        return self._move_first_available(order_id, (from_state,), to_state, updates)

    def _move_first_available(
        self,
        order_id: str,
        from_states: tuple[str, ...],
        to_state: str,
        updates: dict[str, Any],
    ) -> dict[str, Any]:
        self.ensure_dirs()
        order_id = normalize_order_id(order_id)
        with self._locked():
            existing_path, card = self.find_by_order_id(order_id)
            if existing_path is None:
                raise FileNotFoundError(f"Signal not found: {order_id}")
            current_status = str(card.get("status", existing_path.parent.name))
            if current_status not in from_states:
                raise SignalStateConflict(f"Signal cannot move from {current_status} to {to_state}")
            next_card = dict(card)
            next_card.update({key: value for key, value in updates.items() if value is not None})
            next_card["status"] = to_state
            _assert_non_live_card(next_card)
            target_path = self.path_for(to_state, order_id)
            write_json(existing_path, next_card)
            os.rename(existing_path, target_path)
        return {"order_id": order_id, "status": to_state, "signal_path": str(target_path), "signal_card": next_card}


def normalize_order_id(order_id: str) -> str:
    if not order_id or not _ORDER_ID_RE.fullmatch(order_id):
        raise ValueError(f"Invalid order_id: {order_id!r}")
    return order_id


def read_json(path: Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"Signal card must be a JSON object: {path}")
    return data


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    tmp.replace(path)


def _assert_non_live_card(card: dict[str, Any]) -> None:
    reject_real_execution_payload(card, context="signal state card")
    capital_layer = str(card.get("capital_layer") or "").strip().lower()
    account_type = str(card.get("account_type") or "").strip().lower()
    if capital_layer not in {"shadow", "simulated"} or account_type not in {
        "none",
        "paper",
        "shadow",
        "simulated",
    }:
        raise ValueError(
            "file-backed signal state machine is simulation/shadow only; "
            "capital_layer and account_type must use an explicit safe value"
        )
    if capital_layer == "simulated":
        market = str(card.get("market") or "").strip().lower()
        if market in GOVERNED_SIM_MARKETS:
            from shared.governance.market_lanes import load_market_lanes

            lane = load_market_lanes().get_for_runtime_market(market)
            broker_contract = str(card.get("broker_contract") or "").strip()
            authority_id = str(card.get("authority_id") or "").strip()
            account = str(card.get("account") or "").strip()
            account_id = str(card.get("account_id") or "").strip()
            if account and account_id and account != account_id:
                raise ValueError("simulated signal account identity is ambiguous")
            if not (account or account_id):
                raise ValueError("governed simulated signal requires account identity")
            if broker_contract != lane.broker_boundary.simulation_contract:
                raise ValueError(
                    "governed simulated signal broker_contract does not match market lane"
                )
            if authority_id != lane.authority_id:
                raise ValueError(
                    "governed simulated signal authority_id does not match market lane"
                )


def now_iso(value: datetime | None = None) -> str:
    return (value or datetime.now().astimezone()).isoformat(timespec="seconds")


def is_expired(valid_until: Any, now: datetime) -> bool:
    if valid_until in (None, ""):
        return False
    if isinstance(valid_until, datetime):
        return valid_until < now
    if isinstance(valid_until, date):
        return valid_until < now.date()
    value = str(valid_until)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        parsed_date = date.fromisoformat(value)
        return datetime.combine(parsed_date, time.max, tzinfo=now.tzinfo) < now
    try:
        parsed_datetime = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed_datetime.tzinfo is None:
            parsed_datetime = parsed_datetime.replace(tzinfo=now.tzinfo)
        return parsed_datetime < now
    except ValueError:
        pass
    try:
        parsed_date = date.fromisoformat(value)
    except ValueError:
        return False
    return datetime.combine(parsed_date, time.max, tzinfo=now.tzinfo) < now
