"""Append-only provenance journal for successful evidence-only LLM runs.

This journal is an isolated shadow artifact.  It binds one immutable request,
externally verified source proofs, an offline provider transport receipt and a
safe observation.  It has no candidate, capital, risk, position or order
authority and deliberately has no default runtime path.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Tuple

from .evidence_artifact import EvidenceSourceAuthorityVerifier
from .gateway import ProviderTransportReceipt, ProviderTransportReceiptError
from .schema import (
    AUTHORITY_DENIED,
    LLMEvidenceRequest,
    normalize_observation,
)


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$")
EMPTY_LLM_EVIDENCE_JOURNAL_HEAD_SHA256 = hashlib.sha256(b"").hexdigest()
_ENVELOPE_FIELDS = {
    "artifact_set_sha256",
    "authority",
    "envelope_sha256",
    "model",
    "observation",
    "observation_sha256",
    "production_eligible",
    "provider",
    "received_at",
    "request_content_sha256",
    "request_id",
    "request_sha256",
    "run_id",
    "schema_version",
    "shadow_only",
    "source_authority_proof_set_sha256",
    "source_verified_at",
    "transport_material_sha256",
    "transport_receipt",
}
_EVENT_FIELDS = {
    "envelope",
    "event_id",
    "event_sha256",
    "occurred_at",
    "previous_event_sha256",
    "run_id",
    "schema_version",
}


class LLMEvidenceEnvelopeError(ValueError):
    """Raised when request, receipt and observation cannot be bound."""


class LLMEvidenceJournalError(RuntimeError):
    """Raised when the local append-only LLM journal is not trustworthy."""


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
        raise LLMEvidenceEnvelopeError("payload_not_canonical_json") from exc


def _sha(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _strict_id(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID_RE.fullmatch(value):
        raise LLMEvidenceEnvelopeError(f"{field_name}_invalid")
    return value


def _require_sha(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise LLMEvidenceEnvelopeError(f"{field_name}_invalid")
    return value


def _time_text(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise LLMEvidenceEnvelopeError(f"{field_name}_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LLMEvidenceEnvelopeError(f"{field_name}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise LLMEvidenceEnvelopeError(f"{field_name}_timezone_required")
    if parsed.isoformat() != value:
        raise LLMEvidenceEnvelopeError(f"{field_name}_not_canonical")
    return value


def _receipt_from_descriptor(value: object) -> ProviderTransportReceipt:
    if not isinstance(value, dict):
        raise LLMEvidenceEnvelopeError("transport_receipt_invalid")
    try:
        receipt = ProviderTransportReceipt(**value)
        receipt.verify_integrity()
    except (TypeError, ProviderTransportReceiptError) as exc:
        raise LLMEvidenceEnvelopeError("transport_receipt_invalid") from exc
    return receipt


def _run_id_for_receipt(receipt: ProviderTransportReceipt) -> str:
    return f"llm-run-{receipt.receipt_sha256}"


@dataclass(frozen=True)
class LLMEvidenceEnvelope:
    run_id: str
    request_id: str
    request_content_sha256: str
    artifact_set_sha256: str
    request_sha256: str
    source_authority_proof_set_sha256: str
    source_verified_at: str
    provider: str
    model: str
    transport_material_sha256: str
    transport_receipt: Mapping[str, object]
    observation: Mapping[str, Any]
    observation_sha256: str
    received_at: str
    authority: Mapping[str, bool]
    shadow_only: bool
    production_eligible: bool
    envelope_sha256: str
    schema_version: str = "tradingagent.llm_evidence_envelope.v1"

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        request: LLMEvidenceRequest,
        source_authority_verifier: EvidenceSourceAuthorityVerifier | Any | None,
        transport_receipt: ProviderTransportReceipt,
        observation: Mapping[str, Any],
    ) -> "LLMEvidenceEnvelope":
        if not isinstance(request, LLMEvidenceRequest):
            raise LLMEvidenceEnvelopeError("llm_request_required")
        if not isinstance(transport_receipt, ProviderTransportReceipt):
            raise LLMEvidenceEnvelopeError("transport_receipt_required")
        try:
            transport_receipt.verify_integrity()
            material = request.validate_for_transport(
                transport_receipt.model,
                source_authority_verifier=source_authority_verifier,
                verified_at=transport_receipt.verified_at,
            )
        except Exception as exc:
            raise LLMEvidenceEnvelopeError("transport_binding_invalid") from exc
        metadata = material["metadata"]
        if not hmac.compare_digest(
            transport_receipt.request_sha256,
            metadata["request_sha256"],
        ):
            raise LLMEvidenceEnvelopeError("request_sha256_mismatch")
        if not hmac.compare_digest(
            transport_receipt.source_authority_proof_set_sha256,
            metadata["source_authority_proof_set_sha256"],
        ):
            raise LLMEvidenceEnvelopeError("source_authority_proof_set_mismatch")
        material_sha = _sha(material)
        if not hmac.compare_digest(
            transport_receipt.transport_material_sha256,
            material_sha,
        ):
            raise LLMEvidenceEnvelopeError("transport_material_sha256_mismatch")

        safe_observation = normalize_observation(
            observation,
            request=request,
            source_authority_verifier=source_authority_verifier,
        )
        expected_entity = request.payload.get("symbol")
        if (
            safe_observation.get("status") != "available"
            or dict(safe_observation) != dict(observation)
            or safe_observation.get("provider") != transport_receipt.provider
            or safe_observation.get("model") != transport_receipt.model
            or (
                isinstance(expected_entity, str)
                and safe_observation.get("entity_id") != expected_entity
            )
        ):
            raise LLMEvidenceEnvelopeError("observation_binding_invalid")
        if not hmac.compare_digest(
            transport_receipt.normalized_evidence_sha256,
            str(safe_observation.get("output_sha256") or ""),
        ):
            raise LLMEvidenceEnvelopeError("observation_receipt_binding_invalid")
        expected_run_id = _run_id_for_receipt(transport_receipt)
        if run_id != expected_run_id:
            raise LLMEvidenceEnvelopeError("run_id_receipt_binding_invalid")

        identity = {
            "artifact_set_sha256": request.artifact_set_sha256,
            "authority": dict(AUTHORITY_DENIED),
            "model": transport_receipt.model,
            "observation": dict(safe_observation),
            "observation_sha256": _sha(safe_observation),
            "production_eligible": False,
            "provider": transport_receipt.provider,
            "received_at": transport_receipt.received_at,
            "request_content_sha256": request.request_content_sha256,
            "request_id": request.request_id,
            "request_sha256": transport_receipt.request_sha256,
            "run_id": _strict_id(expected_run_id, field_name="run_id"),
            "schema_version": "tradingagent.llm_evidence_envelope.v1",
            "shadow_only": True,
            "source_authority_proof_set_sha256": (
                transport_receipt.source_authority_proof_set_sha256
            ),
            "source_verified_at": transport_receipt.verified_at,
            "transport_material_sha256": material_sha,
            "transport_receipt": transport_receipt.to_descriptor(),
        }
        envelope = cls(
            **identity,
            envelope_sha256=_sha(identity),
        )
        envelope.verify_integrity()
        return envelope

    def _identity_payload(self) -> dict[str, object]:
        return {
            "artifact_set_sha256": self.artifact_set_sha256,
            "authority": dict(self.authority),
            "model": self.model,
            "observation": dict(self.observation),
            "observation_sha256": self.observation_sha256,
            "production_eligible": self.production_eligible,
            "provider": self.provider,
            "received_at": self.received_at,
            "request_content_sha256": self.request_content_sha256,
            "request_id": self.request_id,
            "request_sha256": self.request_sha256,
            "run_id": self.run_id,
            "schema_version": self.schema_version,
            "shadow_only": self.shadow_only,
            "source_authority_proof_set_sha256": (
                self.source_authority_proof_set_sha256
            ),
            "source_verified_at": self.source_verified_at,
            "transport_material_sha256": self.transport_material_sha256,
            "transport_receipt": dict(self.transport_receipt),
        }

    def canonical_payload(self) -> dict[str, object]:
        return {**self._identity_payload(), "envelope_sha256": self.envelope_sha256}

    def verify_integrity(self) -> None:
        _strict_id(self.run_id, field_name="run_id")
        _strict_id(self.request_id, field_name="request_id")
        _strict_id(self.provider, field_name="provider")
        _strict_id(self.model, field_name="model")
        for field_name in (
            "request_content_sha256",
            "artifact_set_sha256",
            "request_sha256",
            "source_authority_proof_set_sha256",
            "transport_material_sha256",
            "observation_sha256",
            "envelope_sha256",
        ):
            _require_sha(getattr(self, field_name), field_name=field_name)
        if self.schema_version != "tradingagent.llm_evidence_envelope.v1":
            raise LLMEvidenceEnvelopeError("envelope_schema_invalid")
        if self.shadow_only is not True or self.production_eligible is not False:
            raise LLMEvidenceEnvelopeError("envelope_authority_invalid")
        if dict(self.authority) != AUTHORITY_DENIED:
            raise LLMEvidenceEnvelopeError("envelope_authority_invalid")
        source_verified_at = _time_text(
            self.source_verified_at,
            field_name="source_verified_at",
        )
        received_at = _time_text(self.received_at, field_name="received_at")
        if datetime.fromisoformat(received_at) < datetime.fromisoformat(
            source_verified_at
        ):
            raise LLMEvidenceEnvelopeError("envelope_time_order_invalid")
        receipt = _receipt_from_descriptor(dict(self.transport_receipt))
        if (
            receipt.provider != self.provider
            or receipt.model != self.model
            or receipt.request_sha256 != self.request_sha256
            or receipt.source_authority_proof_set_sha256
            != self.source_authority_proof_set_sha256
            or receipt.transport_material_sha256 != self.transport_material_sha256
            or receipt.verified_at != self.source_verified_at
            or receipt.received_at != self.received_at
        ):
            raise LLMEvidenceEnvelopeError("transport_receipt_binding_invalid")
        if self.run_id != _run_id_for_receipt(receipt):
            raise LLMEvidenceEnvelopeError("run_id_receipt_binding_invalid")
        if not isinstance(self.observation, Mapping):
            raise LLMEvidenceEnvelopeError("observation_invalid")
        if (
            self.observation.get("status") != "available"
            or self.observation.get("request_id") != self.request_id
            or self.observation.get("provider") != self.provider
            or self.observation.get("model") != self.model
            or self.observation.get("authority") != AUTHORITY_DENIED
            or not hmac.compare_digest(
                self.observation_sha256,
                _sha(dict(self.observation)),
            )
        ):
            raise LLMEvidenceEnvelopeError("observation_binding_invalid")
        if not hmac.compare_digest(
            receipt.normalized_evidence_sha256,
            str(self.observation.get("output_sha256") or ""),
        ):
            raise LLMEvidenceEnvelopeError("observation_receipt_binding_invalid")
        expected = _sha(self._identity_payload())
        if not hmac.compare_digest(self.envelope_sha256, expected):
            raise LLMEvidenceEnvelopeError("envelope_sha256_mismatch")

    @classmethod
    def from_payload(cls, value: object) -> "LLMEvidenceEnvelope":
        if not isinstance(value, dict) or set(value) != _ENVELOPE_FIELDS:
            raise LLMEvidenceEnvelopeError("envelope_fields_invalid")
        envelope = cls(
            run_id=value["run_id"],
            request_id=value["request_id"],
            request_content_sha256=value["request_content_sha256"],
            artifact_set_sha256=value["artifact_set_sha256"],
            request_sha256=value["request_sha256"],
            source_authority_proof_set_sha256=value[
                "source_authority_proof_set_sha256"
            ],
            source_verified_at=value["source_verified_at"],
            provider=value["provider"],
            model=value["model"],
            transport_material_sha256=value["transport_material_sha256"],
            transport_receipt=MappingProxyType(dict(value["transport_receipt"])),
            observation=MappingProxyType(dict(value["observation"])),
            observation_sha256=value["observation_sha256"],
            received_at=value["received_at"],
            authority=MappingProxyType(dict(value["authority"])),
            shadow_only=value["shadow_only"],
            production_eligible=value["production_eligible"],
            envelope_sha256=value["envelope_sha256"],
            schema_version=value["schema_version"],
        )
        envelope.verify_integrity()
        return envelope


@dataclass(frozen=True)
class LLMEvidenceJournalEvent:
    event_id: str
    run_id: str
    occurred_at: str
    envelope: LLMEvidenceEnvelope
    previous_event_sha256: str
    event_sha256: str
    schema_version: str = "tradingagent.llm_evidence_journal_event.v1"

    def without_hash(self) -> dict[str, object]:
        return {
            "envelope": self.envelope.canonical_payload(),
            "event_id": self.event_id,
            "occurred_at": self.occurred_at,
            "previous_event_sha256": self.previous_event_sha256,
            "run_id": self.run_id,
            "schema_version": self.schema_version,
        }

    def canonical_payload(self) -> dict[str, object]:
        return {**self.without_hash(), "event_sha256": self.event_sha256}


@dataclass(frozen=True)
class LLMEvidenceJournalReadback:
    events: Tuple[LLMEvidenceJournalEvent, ...]
    latest_by_run: Mapping[str, LLMEvidenceEnvelope]
    head_sha256: str


def _reject_symlink_components(path: Path) -> None:
    candidate = path.absolute()
    for component in (candidate, *candidate.parents):
        if component.exists() or component.is_symlink():
            if component.is_symlink():
                raise LLMEvidenceJournalError("journal_symlink_forbidden")


class LLMEvidenceJournal:
    """Explicit-path checksum-chain journal for successful shadow evidence."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.head_path = Path(f"{self.path}.head")
        _reject_symlink_components(self.path)
        _reject_symlink_components(self.head_path)

    def _open_locked(self, *, create: bool) -> int:
        _reject_symlink_components(self.path)
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_RDWR | int(getattr(os, "O_NOFOLLOW", 0))
        if create:
            flags |= os.O_CREAT
        try:
            descriptor = os.open(self.path, flags, 0o600)
        except OSError as exc:
            raise LLMEvidenceJournalError("journal_open_failed") from exc
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise LLMEvidenceJournalError("journal_not_regular_file")
            return descriptor
        except Exception:
            os.close(descriptor)
            raise

    def _read_head_anchor(self) -> str | None:
        _reject_symlink_components(self.head_path)
        if not self.head_path.exists():
            return None
        flags = os.O_RDONLY | int(getattr(os, "O_NOFOLLOW", 0))
        try:
            descriptor = os.open(self.head_path, flags)
        except OSError as exc:
            raise LLMEvidenceJournalError("journal_head_anchor_open_failed") from exc
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise LLMEvidenceJournalError("journal_head_anchor_not_regular_file")
            raw = self._read(descriptor)
        finally:
            os.close(descriptor)
        try:
            text = raw.decode("ascii")
        except UnicodeDecodeError as exc:
            raise LLMEvidenceJournalError("journal_head_anchor_invalid") from exc
        if not text.endswith("\n") or text.count("\n") != 1:
            raise LLMEvidenceJournalError("journal_head_anchor_invalid")
        value = text[:-1]
        if not _SHA256_RE.fullmatch(value):
            raise LLMEvidenceJournalError("journal_head_anchor_invalid")
        return value

    def _write_head_anchor(self, head_sha256: str) -> None:
        try:
            _require_sha(head_sha256, field_name="head_sha256")
        except LLMEvidenceEnvelopeError as exc:
            raise LLMEvidenceJournalError("journal_head_anchor_invalid") from exc
        _reject_symlink_components(self.head_path)
        temporary = self.head_path.with_name(
            f".{self.head_path.name}.{os.getpid()}.tmp"
        )
        if temporary.exists() or temporary.is_symlink():
            raise LLMEvidenceJournalError("journal_head_anchor_temp_exists")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | int(getattr(os, "O_NOFOLLOW", 0))
        descriptor: int | None = None
        try:
            descriptor = os.open(temporary, flags, 0o600)
            payload = f"{head_sha256}\n".encode("ascii")
            if os.write(descriptor, payload) != len(payload):
                raise LLMEvidenceJournalError("journal_head_anchor_short_write")
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            os.replace(temporary, self.head_path)
            parent_descriptor = os.open(self.head_path.parent, os.O_RDONLY)
            try:
                os.fsync(parent_descriptor)
            finally:
                os.close(parent_descriptor)
        except LLMEvidenceJournalError:
            raise
        except OSError as exc:
            raise LLMEvidenceJournalError("journal_head_anchor_write_failed") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if temporary.exists() and not temporary.is_symlink():
                temporary.unlink()

    def _verify_head_anchor(self, readback: LLMEvidenceJournalReadback) -> None:
        anchored = self._read_head_anchor()
        if not readback.events:
            if anchored is not None:
                raise LLMEvidenceJournalError("journal_empty_with_head_anchor")
            return
        if anchored is None:
            raise LLMEvidenceJournalError("journal_head_anchor_missing")
        if not hmac.compare_digest(anchored, readback.head_sha256):
            raise LLMEvidenceJournalError("journal_head_anchor_mismatch")

    @staticmethod
    def _read(descriptor: int) -> bytes:
        os.lseek(descriptor, 0, os.SEEK_SET)
        chunks = []
        while True:
            block = os.read(descriptor, 64 * 1024)
            if not block:
                break
            chunks.append(block)
        return b"".join(chunks)

    @staticmethod
    def _parse(raw: bytes) -> LLMEvidenceJournalReadback:
        if not raw:
            return LLMEvidenceJournalReadback(
                events=(),
                latest_by_run=MappingProxyType({}),
                head_sha256=EMPTY_LLM_EVIDENCE_JOURNAL_HEAD_SHA256,
            )
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise LLMEvidenceJournalError("journal_utf8_invalid") from exc
        if not text.endswith("\n"):
            raise LLMEvidenceJournalError("journal_partial_line")
        expected_previous = EMPTY_LLM_EVIDENCE_JOURNAL_HEAD_SHA256
        events = []
        latest: dict[str, LLMEvidenceEnvelope] = {}
        for line in text.splitlines():
            if not line:
                raise LLMEvidenceJournalError("journal_empty_line_invalid")
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise LLMEvidenceJournalError("journal_json_invalid") from exc
            if _canonical_json(value) != line:
                raise LLMEvidenceJournalError("journal_json_not_canonical")
            if not isinstance(value, dict) or set(value) != _EVENT_FIELDS:
                raise LLMEvidenceJournalError("journal_event_fields_invalid")
            try:
                envelope = LLMEvidenceEnvelope.from_payload(value["envelope"])
                event = LLMEvidenceJournalEvent(
                    event_id=_strict_id(value["event_id"], field_name="event_id"),
                    run_id=_strict_id(value["run_id"], field_name="run_id"),
                    occurred_at=_time_text(
                        value["occurred_at"],
                        field_name="occurred_at",
                    ),
                    envelope=envelope,
                    previous_event_sha256=_require_sha(
                        value["previous_event_sha256"],
                        field_name="previous_event_sha256",
                    ),
                    event_sha256=_require_sha(
                        value["event_sha256"],
                        field_name="event_sha256",
                    ),
                    schema_version=value["schema_version"],
                )
            except LLMEvidenceEnvelopeError as exc:
                raise LLMEvidenceJournalError("journal_event_invalid") from exc
            if event.schema_version != "tradingagent.llm_evidence_journal_event.v1":
                raise LLMEvidenceJournalError("journal_event_schema_invalid")
            if event.run_id != envelope.run_id:
                raise LLMEvidenceJournalError("journal_run_binding_invalid")
            if event.previous_event_sha256 != expected_previous:
                raise LLMEvidenceJournalError("journal_checksum_chain_invalid")
            if event.event_sha256 != _sha(event.without_hash()):
                raise LLMEvidenceJournalError("journal_event_sha256_mismatch")
            if event.run_id in latest:
                raise LLMEvidenceJournalError("journal_duplicate_run")
            latest[event.run_id] = envelope
            expected_previous = event.event_sha256
            events.append(event)
        return LLMEvidenceJournalReadback(
            events=tuple(events),
            latest_by_run=MappingProxyType(dict(latest)),
            head_sha256=expected_previous,
        )

    def read(self) -> LLMEvidenceJournalReadback:
        if not self.path.exists():
            if self._read_head_anchor() is not None:
                raise LLMEvidenceJournalError("journal_missing_with_head_anchor")
            return self._parse(b"")
        descriptor = self._open_locked(create=False)
        try:
            readback = self._parse(self._read(descriptor))
            self._verify_head_anchor(readback)
            return readback
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def append(
        self,
        envelope: LLMEvidenceEnvelope,
        *,
        expected_head_sha256: str,
    ) -> bool:
        if not isinstance(envelope, LLMEvidenceEnvelope):
            raise LLMEvidenceJournalError("llm_evidence_envelope_required")
        try:
            envelope.verify_integrity()
            _require_sha(
                expected_head_sha256,
                field_name="expected_head_sha256",
            )
        except LLMEvidenceEnvelopeError as exc:
            raise LLMEvidenceJournalError("journal_envelope_invalid") from exc
        if not self.path.exists() and self._read_head_anchor() is not None:
            raise LLMEvidenceJournalError("journal_missing_with_head_anchor")
        descriptor = self._open_locked(create=True)
        try:
            readback = self._parse(self._read(descriptor))
            self._verify_head_anchor(readback)
            if expected_head_sha256 != readback.head_sha256:
                raise LLMEvidenceJournalError("journal_head_cas_mismatch")
            existing = readback.latest_by_run.get(envelope.run_id)
            if existing is not None:
                if existing == envelope:
                    return False
                raise LLMEvidenceJournalError("journal_run_id_conflict")
            provisional = LLMEvidenceJournalEvent(
                event_id=f"llm-evidence:{envelope.envelope_sha256}",
                run_id=envelope.run_id,
                occurred_at=envelope.received_at,
                envelope=envelope,
                previous_event_sha256=readback.head_sha256,
                event_sha256="0" * 64,
            )
            event = LLMEvidenceJournalEvent(
                event_id=provisional.event_id,
                run_id=provisional.run_id,
                occurred_at=provisional.occurred_at,
                envelope=envelope,
                previous_event_sha256=provisional.previous_event_sha256,
                event_sha256=_sha(provisional.without_hash()),
            )
            line = (_canonical_json(event.canonical_payload()) + "\n").encode("utf-8")
            os.lseek(descriptor, 0, os.SEEK_END)
            if os.write(descriptor, line) != len(line):
                raise LLMEvidenceJournalError("journal_short_write")
            os.fsync(descriptor)
            verified = self._parse(self._read(descriptor))
            if verified.head_sha256 != event.event_sha256:
                raise LLMEvidenceJournalError("journal_readback_mismatch")
            self._write_head_anchor(verified.head_sha256)
            self._verify_head_anchor(verified)
            return True
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


__all__ = [
    "EMPTY_LLM_EVIDENCE_JOURNAL_HEAD_SHA256",
    "LLMEvidenceEnvelope",
    "LLMEvidenceEnvelopeError",
    "LLMEvidenceJournal",
    "LLMEvidenceJournalError",
    "LLMEvidenceJournalEvent",
    "LLMEvidenceJournalReadback",
]
