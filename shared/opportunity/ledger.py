"""Append-only shadow opportunity ledger with checksum-chain readback.

The ledger is deliberately separate from SampleJournal and DecisionLedger.  It
tracks high-recall research opportunities, including those never eligible for
capital.  It cannot create a TradingAgent candidate or order.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Tuple

from .contracts import (
    OpportunityContractError,
    OpportunitySnapshot,
    validate_opportunity_transition,
)
from .radar import OpportunityBatch


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
EMPTY_OPPORTUNITY_LEDGER_HEAD_SHA256 = hashlib.sha256(b"").hexdigest()
_EVENT_FIELDS = {
    "event_id",
    "event_sha256",
    "event_type",
    "occurred_at",
    "payload",
    "previous_event_sha256",
    "schema_version",
}


class OpportunityLedgerError(RuntimeError):
    """Raised when the shadow opportunity journal cannot be trusted."""


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise OpportunityLedgerError("ledger_payload_not_canonical") from exc


def _sha(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _sha_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise OpportunityLedgerError(f"{field_name}_invalid")
    return value


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise OpportunityLedgerError(f"{field_name}_invalid")
    return value


def _parse_time(value: object) -> datetime:
    if not isinstance(value, str) or not value or value != value.strip():
        raise OpportunityLedgerError("ledger_event_time_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OpportunityLedgerError("ledger_event_time_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise OpportunityLedgerError("ledger_event_time_invalid")
    if parsed.isoformat() != value:
        raise OpportunityLedgerError("ledger_event_time_not_canonical")
    return parsed


@dataclass(frozen=True)
class OpportunityLedgerEvent:
    event_id: str
    event_type: str
    occurred_at: datetime
    payload: Mapping[str, object]
    previous_event_sha256: str
    event_sha256: str
    schema_version: str = "tradingagent.opportunity_ledger_event.v1"

    def canonical_without_hash(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "occurred_at": self.occurred_at.isoformat(),
            "payload": dict(self.payload),
            "previous_event_sha256": self.previous_event_sha256,
            "schema_version": self.schema_version,
        }

    def canonical_payload(self) -> dict[str, object]:
        return {**self.canonical_without_hash(), "event_sha256": self.event_sha256}


@dataclass(frozen=True)
class OpportunityLedgerReadback:
    events: Tuple[OpportunityLedgerEvent, ...]
    latest_by_opportunity: Mapping[str, OpportunitySnapshot]
    head_sha256: str


def _event_from_payload(value: object) -> OpportunityLedgerEvent:
    if not isinstance(value, dict) or set(value) != _EVENT_FIELDS:
        raise OpportunityLedgerError("ledger_event_fields_invalid")
    event_id = _text(value["event_id"], "event_id")
    event_type = _text(value["event_type"], "event_type")
    if event_type not in {"scan_batch", "state_transition"}:
        raise OpportunityLedgerError("ledger_event_type_invalid")
    occurred_at = _parse_time(value["occurred_at"])
    previous_sha = _sha_text(
        value["previous_event_sha256"],
        "previous_event_sha256",
    )
    event_sha = _sha_text(value["event_sha256"], "event_sha256")
    payload = value["payload"]
    if not isinstance(payload, dict):
        raise OpportunityLedgerError("ledger_event_payload_invalid")
    event = OpportunityLedgerEvent(
        event_id=event_id,
        event_type=event_type,
        occurred_at=occurred_at,
        payload=MappingProxyType(dict(payload)),
        previous_event_sha256=previous_sha,
        event_sha256=event_sha,
        schema_version=value["schema_version"],
    )
    if event.schema_version != "tradingagent.opportunity_ledger_event.v1":
        raise OpportunityLedgerError("ledger_event_schema_invalid")
    if _sha(event.canonical_without_hash()) != event.event_sha256:
        raise OpportunityLedgerError("ledger_event_sha256_mismatch")
    return event


class OpportunityLedger:
    """File-backed opportunity chain with CAS, idempotency and readback."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if self.path.is_symlink() or self.path.parent.is_symlink():
            raise OpportunityLedgerError("ledger_symlink_forbidden")

    def _open_locked(self) -> int:
        if self.path.is_symlink() or self.path.parent.is_symlink():
            raise OpportunityLedgerError("ledger_symlink_forbidden")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(self.path, flags, 0o600)
        except OSError as exc:
            raise OpportunityLedgerError("ledger_open_failed") from exc
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise OpportunityLedgerError("ledger_not_regular_file")
            return descriptor
        except Exception:
            os.close(descriptor)
            raise

    @staticmethod
    def _read_descriptor(descriptor: int) -> bytes:
        os.lseek(descriptor, 0, os.SEEK_SET)
        chunks = []
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)

    @staticmethod
    def _parse(raw: bytes) -> OpportunityLedgerReadback:
        if not raw:
            return OpportunityLedgerReadback(
                events=(),
                latest_by_opportunity=MappingProxyType({}),
                head_sha256=EMPTY_OPPORTUNITY_LEDGER_HEAD_SHA256,
            )
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise OpportunityLedgerError("ledger_utf8_invalid") from exc
        lines = text.splitlines()
        if not text.endswith("\n"):
            raise OpportunityLedgerError("ledger_partial_line")
        events = []
        latest: dict[str, OpportunitySnapshot] = {}
        event_ids: dict[str, str] = {}
        expected_previous = EMPTY_OPPORTUNITY_LEDGER_HEAD_SHA256
        for line in lines:
            if not line:
                raise OpportunityLedgerError("ledger_empty_line_invalid")
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise OpportunityLedgerError("ledger_json_invalid") from exc
            if _canonical_json(payload) != line:
                raise OpportunityLedgerError("ledger_json_not_canonical")
            event = _event_from_payload(payload)
            if event.previous_event_sha256 != expected_previous:
                raise OpportunityLedgerError("ledger_checksum_chain_invalid")
            prior_event_sha = event_ids.get(event.event_id)
            if prior_event_sha is not None and prior_event_sha != event.event_sha256:
                raise OpportunityLedgerError("ledger_event_id_conflict")
            if prior_event_sha is not None:
                raise OpportunityLedgerError("ledger_duplicate_event")
            event_ids[event.event_id] = event.event_sha256
            expected_previous = event.event_sha256
            OpportunityLedger._apply_event(event, latest)
            events.append(event)
        return OpportunityLedgerReadback(
            events=tuple(events),
            latest_by_opportunity=MappingProxyType(dict(latest)),
            head_sha256=expected_previous,
        )

    @staticmethod
    def _apply_event(
        event: OpportunityLedgerEvent,
        latest: dict[str, OpportunitySnapshot],
    ) -> None:
        if event.event_type == "scan_batch":
            expected_fields = {
                "batch_payload",
                "batch_sha256",
                "opportunity_snapshots",
            }
            if set(event.payload) != expected_fields:
                raise OpportunityLedgerError("scan_batch_payload_invalid")
            batch_payload = event.payload["batch_payload"]
            batch_sha = _sha_text(event.payload["batch_sha256"], "batch_sha256")
            if not isinstance(batch_payload, dict) or _sha(batch_payload) != batch_sha:
                raise OpportunityLedgerError("scan_batch_sha256_mismatch")
            try:
                batch = OpportunityBatch.from_payload(batch_payload)
            except OpportunityContractError as exc:
                raise OpportunityLedgerError("opportunity_batch_invalid") from exc
            if (
                batch.batch_sha256 != batch_sha
                or batch.decision_time != event.occurred_at
            ):
                raise OpportunityLedgerError("opportunity_batch_binding_invalid")
            snapshots = event.payload["opportunity_snapshots"]
            if not isinstance(snapshots, list):
                raise OpportunityLedgerError("scan_batch_snapshot_set_invalid")
            if snapshots != [item.canonical_payload() for item in batch.opportunities]:
                raise OpportunityLedgerError("scan_batch_snapshot_binding_invalid")
            for raw_snapshot in snapshots:
                try:
                    snapshot = OpportunitySnapshot.from_payload(raw_snapshot)
                except OpportunityContractError as exc:
                    raise OpportunityLedgerError(
                        "opportunity_snapshot_invalid"
                    ) from exc
                if snapshot.opportunity_id in latest:
                    raise OpportunityLedgerError(
                        "opportunity_initial_snapshot_duplicate"
                    )
                latest[snapshot.opportunity_id] = snapshot
            return
        if set(event.payload) != {"opportunity_snapshot", "snapshot_sha256"}:
            raise OpportunityLedgerError("transition_payload_invalid")
        try:
            snapshot = OpportunitySnapshot.from_payload(
                event.payload["opportunity_snapshot"]
            )
        except OpportunityContractError as exc:
            raise OpportunityLedgerError("opportunity_snapshot_invalid") from exc
        supplied_sha = _sha_text(event.payload["snapshot_sha256"], "snapshot_sha256")
        if supplied_sha != snapshot.snapshot_sha256:
            raise OpportunityLedgerError("opportunity_snapshot_sha256_mismatch")
        previous = latest.get(snapshot.opportunity_id)
        if previous is None:
            raise OpportunityLedgerError("opportunity_transition_without_initial")
        if snapshot.previous_snapshot_sha256 != previous.snapshot_sha256:
            raise OpportunityLedgerError("opportunity_transition_branch_mismatch")
        if snapshot.decision_time != event.occurred_at:
            raise OpportunityLedgerError("opportunity_transition_time_binding_invalid")
        try:
            validate_opportunity_transition(previous, snapshot)
        except OpportunityContractError as exc:
            raise OpportunityLedgerError(str(exc)) from exc
        latest[snapshot.opportunity_id] = snapshot

    def read(self) -> OpportunityLedgerReadback:
        descriptor = self._open_locked()
        try:
            return self._parse(self._read_descriptor(descriptor))
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    @staticmethod
    def _new_event(
        *,
        event_id: str,
        event_type: str,
        occurred_at: datetime,
        payload: Mapping[str, object],
        previous_event_sha256: str,
    ) -> OpportunityLedgerEvent:
        provisional = OpportunityLedgerEvent(
            event_id=event_id,
            event_type=event_type,
            occurred_at=occurred_at,
            payload=MappingProxyType(dict(payload)),
            previous_event_sha256=previous_event_sha256,
            event_sha256="0" * 64,
        )
        return OpportunityLedgerEvent(
            event_id=event_id,
            event_type=event_type,
            occurred_at=occurred_at,
            payload=provisional.payload,
            previous_event_sha256=previous_event_sha256,
            event_sha256=_sha(provisional.canonical_without_hash()),
        )

    def _append(
        self,
        *,
        event_id: str,
        event_type: str,
        occurred_at: datetime,
        payload: Mapping[str, object],
        expected_head_sha256: str | None,
    ) -> bool:
        if expected_head_sha256 is not None:
            _sha_text(expected_head_sha256, "expected_head_sha256")
        descriptor = self._open_locked()
        try:
            readback = self._parse(self._read_descriptor(descriptor))
            existing = next(
                (event for event in readback.events if event.event_id == event_id),
                None,
            )
            candidate = self._new_event(
                event_id=event_id,
                event_type=event_type,
                occurred_at=occurred_at,
                payload=payload,
                previous_event_sha256=readback.head_sha256,
            )
            if existing is not None:
                # Idempotency compares business payload and event time; the
                # chain predecessor is necessarily the earlier predecessor.
                if (
                    existing.event_type == candidate.event_type
                    and existing.occurred_at == candidate.occurred_at
                    and dict(existing.payload) == dict(candidate.payload)
                ):
                    return False
                raise OpportunityLedgerError("ledger_event_id_conflict")
            if (
                expected_head_sha256 is not None
                and expected_head_sha256 != readback.head_sha256
            ):
                raise OpportunityLedgerError("ledger_head_cas_mismatch")
            simulated_latest = dict(readback.latest_by_opportunity)
            self._apply_event(candidate, simulated_latest)
            line = (_canonical_json(candidate.canonical_payload()) + "\n").encode(
                "utf-8"
            )
            os.lseek(descriptor, 0, os.SEEK_END)
            written = os.write(descriptor, line)
            if written != len(line):
                raise OpportunityLedgerError("ledger_short_write")
            os.fsync(descriptor)
            verify = self._parse(self._read_descriptor(descriptor))
            if verify.head_sha256 != candidate.event_sha256:
                raise OpportunityLedgerError("ledger_readback_mismatch")
            return True
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def append_batch(
        self,
        batch: OpportunityBatch,
        *,
        expected_head_sha256: str | None = None,
    ) -> bool:
        if not isinstance(batch, OpportunityBatch):
            raise OpportunityLedgerError("opportunity_batch_required")
        return self._append(
            event_id=f"scan-batch:{batch.batch_sha256}",
            event_type="scan_batch",
            occurred_at=batch.decision_time,
            payload={
                "batch_payload": batch.canonical_payload(),
                "batch_sha256": batch.batch_sha256,
                "opportunity_snapshots": [
                    item.canonical_payload() for item in batch.opportunities
                ],
            },
            expected_head_sha256=expected_head_sha256,
        )

    def append_transition(
        self,
        snapshot: OpportunitySnapshot,
        *,
        expected_head_sha256: str | None = None,
    ) -> bool:
        if not isinstance(snapshot, OpportunitySnapshot):
            raise OpportunityLedgerError("opportunity_snapshot_required")
        if snapshot.previous_snapshot_sha256 is None:
            raise OpportunityLedgerError("opportunity_transition_previous_missing")
        return self._append(
            event_id=f"state-transition:{snapshot.snapshot_sha256}",
            event_type="state_transition",
            occurred_at=snapshot.decision_time,
            payload={
                "opportunity_snapshot": snapshot.canonical_payload(),
                "snapshot_sha256": snapshot.snapshot_sha256,
            },
            expected_head_sha256=expected_head_sha256,
        )


__all__ = [
    "EMPTY_OPPORTUNITY_LEDGER_HEAD_SHA256",
    "OpportunityLedger",
    "OpportunityLedgerError",
    "OpportunityLedgerEvent",
    "OpportunityLedgerReadback",
]
