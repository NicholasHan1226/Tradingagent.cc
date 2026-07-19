"""Append-only local provenance journals for evidence-only LLM runs.

Accepted evidence, schema-rejected attempts and provider invocation
arbitration use three physically separate journals.  All are isolated
shadow/audit artifacts with no candidate, capital, risk, position or order
authority and deliberately have no default runtime path.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import re
import stat
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Tuple

from .evidence_artifact import EvidenceSourceAuthorityVerifier
from .gateway import (
    DeepSeekAdapter,
    GatewayAnalysisResult,
    LLMEvidenceGateway,
    ProviderRejectedAttemptReceipt,
    ProviderTransportReceipt,
    ProviderTransportReceiptError,
    validate_provider_rejected_attempt_receipt_descriptor,
    validate_provider_transport_receipt_descriptor,
)
from .schema import (
    AUTHORITY_DENIED,
    LLMEvidenceRequest,
    OBSERVATION_SCHEMA_VERSION,
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
_REJECTED_ATTEMPT_AUDIT_EVENT_FIELDS = {
    "attempt_id",
    "event_id",
    "event_sha256",
    "observation",
    "observation_sha256",
    "occurred_at",
    "previous_event_sha256",
    "rejected_attempt_receipt",
    "schema_version",
}
_PROVIDER_INVOCATION_EVENT_FIELDS = {
    "authority",
    "entity_id",
    "event_id",
    "event_sha256",
    "invocation_key_sha256",
    "local_integrity_only",
    "model",
    "observation",
    "observation_sha256",
    "occurred_at",
    "previous_event_sha256",
    "production_eligible",
    "provider",
    "request_id",
    "request_sha256",
    "result_receipt_sha256",
    "route",
    "schema_version",
    "state",
}
_PROVIDER_INVOCATION_FINAL_STATES = {"accepted", "rejected", "no_receipt"}
_OBSERVATION_FIELDS = {
    "authority",
    "document_cutoff",
    "entity_id",
    "evidence",
    "evidence_refs",
    "model",
    "output_sha256",
    "prompt_sha256",
    "prompt_version",
    "provider",
    "reason_code",
    "record_type",
    "request_id",
    "route",
    "schema_version",
    "status",
    "task_type",
}


class LLMEvidenceEnvelopeError(ValueError):
    """Raised when request, receipt and observation cannot be bound."""


class LLMEvidenceJournalError(RuntimeError):
    """Raised when the local append-only LLM journal is not trustworthy."""


def _absolute_journal_path(path: Path | str) -> Path:
    candidate = Path(os.path.expanduser(os.fspath(path)))
    if not candidate.is_absolute():
        raise LLMEvidenceJournalError("journal_absolute_path_required")
    return Path(os.path.abspath(candidate))


class _FrozenJSONMapping(Mapping[str, Any]):
    """Immutable JSON snapshot that returns defensive nested copies."""

    __slots__ = ("_canonical_json", "_keys")

    def __init__(self, value: Mapping[str, Any]) -> None:
        try:
            canonical = json.dumps(
                dict(value),
                allow_nan=False,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            payload = json.loads(canonical)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise LLMEvidenceEnvelopeError("payload_not_canonical_json") from exc
        if not isinstance(payload, dict):
            raise LLMEvidenceEnvelopeError("payload_not_canonical_json")
        self._canonical_json = canonical
        self._keys = tuple(payload)

    def _payload(self) -> dict[str, Any]:
        payload = json.loads(self._canonical_json)
        if not isinstance(payload, dict):  # pragma: no cover - constructor invariant
            raise LLMEvidenceEnvelopeError("payload_not_canonical_json")
        return payload

    def __getitem__(self, key: str) -> Any:
        return self._payload()[key]

    def __iter__(self):
        return iter(self._keys)

    def __len__(self) -> int:
        return len(self._keys)


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


def _receipt_from_descriptor(value: object) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise LLMEvidenceEnvelopeError("transport_receipt_invalid")
    try:
        receipt = validate_provider_transport_receipt_descriptor(value)
    except (TypeError, ProviderTransportReceiptError) as exc:
        raise LLMEvidenceEnvelopeError("transport_receipt_invalid") from exc
    return receipt


def _run_id_for_receipt(
    receipt: ProviderTransportReceipt | Mapping[str, object],
) -> str:
    receipt_sha256 = (
        receipt.receipt_sha256
        if isinstance(receipt, ProviderTransportReceipt)
        else receipt.get("receipt_sha256")
    )
    return f"llm-run-{receipt_sha256}"


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

    def __post_init__(self) -> None:
        for field_name in ("transport_receipt", "observation", "authority"):
            value = getattr(self, field_name)
            if not isinstance(value, Mapping):
                raise LLMEvidenceEnvelopeError(f"{field_name}_invalid")
            object.__setattr__(self, field_name, _FrozenJSONMapping(value))

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
            receipt["provider"] != self.provider
            or receipt["model"] != self.model
            or receipt["request_sha256"] != self.request_sha256
            or receipt["source_authority_proof_set_sha256"]
            != self.source_authority_proof_set_sha256
            or receipt["transport_material_sha256"] != self.transport_material_sha256
            or receipt["verified_at"] != self.source_verified_at
            or receipt["received_at"] != self.received_at
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
            str(receipt["normalized_evidence_sha256"]),
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


def _verify_open_file_identity(
    path: Path,
    descriptor: int,
    *,
    kind: str,
) -> os.stat_result:
    try:
        opened = os.fstat(descriptor)
        current = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise LLMEvidenceJournalError(f"{kind}_identity_invalid") from exc
    if not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(current.st_mode):
        raise LLMEvidenceJournalError(f"{kind}_not_regular_file")
    if opened.st_nlink != 1 or current.st_nlink != 1:
        raise LLMEvidenceJournalError(f"{kind}_hardlink_forbidden")
    if opened.st_uid != os.geteuid() or current.st_uid != os.geteuid():
        raise LLMEvidenceJournalError(f"{kind}_owner_invalid")
    if stat.S_IMODE(opened.st_mode) != 0o600 or stat.S_IMODE(current.st_mode) != 0o600:
        raise LLMEvidenceJournalError(f"{kind}_mode_invalid")
    if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
        raise LLMEvidenceJournalError(f"{kind}_identity_invalid")
    return opened


class LLMEvidenceJournal:
    """Explicit-path checksum-chain journal for successful shadow evidence."""

    _IMMUTABLE_ENDPOINT_ATTRIBUTES = frozenset({"_path", "_head_path"})

    def __init__(self, path: Path | str) -> None:
        self._path = _absolute_journal_path(path)
        self._head_path = Path(f"{self._path}.head")
        _reject_symlink_components(self.path)
        _reject_symlink_components(self.head_path)

    def __setattr__(self, name: str, value: object) -> None:
        if name in self._IMMUTABLE_ENDPOINT_ATTRIBUTES and hasattr(self, name):
            raise AttributeError("journal_endpoint_is_immutable")
        object.__setattr__(self, name, value)

    def __delattr__(self, name: str) -> None:
        if name in self._IMMUTABLE_ENDPOINT_ATTRIBUTES:
            raise AttributeError("journal_endpoint_is_immutable")
        object.__delattr__(self, name)

    @property
    def path(self) -> Path:
        return self._path

    @property
    def head_path(self) -> Path:
        return self._head_path

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
            _verify_open_file_identity(
                self.path,
                descriptor,
                kind="journal",
            )
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
            _verify_open_file_identity(
                self.head_path,
                descriptor,
                kind="journal_head_anchor",
            )
            raw = self._read(descriptor)
            _verify_open_file_identity(
                self.head_path,
                descriptor,
                kind="journal_head_anchor",
            )
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
            _verify_open_file_identity(
                temporary,
                descriptor,
                kind="journal_head_anchor",
            )
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
            _verify_open_file_identity(
                self.path,
                descriptor,
                kind="journal",
            )
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
            _verify_open_file_identity(
                self.path,
                descriptor,
                kind="journal",
            )
            verified = self._parse(self._read(descriptor))
            if verified.head_sha256 != event.event_sha256:
                raise LLMEvidenceJournalError("journal_readback_mismatch")
            self._write_head_anchor(verified.head_sha256)
            self._verify_head_anchor(verified)
            return True
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


@dataclass(frozen=True)
class LLMRejectedAttemptAuditEvent:
    event_id: str
    attempt_id: str
    occurred_at: str
    observation: Mapping[str, object]
    observation_sha256: str
    rejected_attempt_receipt: Mapping[str, object]
    previous_event_sha256: str
    event_sha256: str
    schema_version: str = "tradingagent.llm_rejected_attempt_audit_event.v1"

    def __post_init__(self) -> None:
        object.__setattr__(self, "observation", _FrozenJSONMapping(self.observation))
        object.__setattr__(
            self,
            "rejected_attempt_receipt",
            _FrozenJSONMapping(self.rejected_attempt_receipt),
        )

    def without_hash(self) -> dict[str, object]:
        return {
            "attempt_id": self.attempt_id,
            "event_id": self.event_id,
            "observation": dict(self.observation),
            "observation_sha256": self.observation_sha256,
            "occurred_at": self.occurred_at,
            "previous_event_sha256": self.previous_event_sha256,
            "rejected_attempt_receipt": dict(self.rejected_attempt_receipt),
            "schema_version": self.schema_version,
        }

    def canonical_payload(self) -> dict[str, object]:
        return {**self.without_hash(), "event_sha256": self.event_sha256}


@dataclass(frozen=True)
class LLMRejectedAttemptAuditReadback:
    events: Tuple[LLMRejectedAttemptAuditEvent, ...]
    latest_by_attempt: Mapping[str, LLMRejectedAttemptAuditEvent]
    head_sha256: str


def _validated_rejected_observation(
    value: object,
    *,
    receipt: Mapping[str, object],
) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != _OBSERVATION_FIELDS:
        raise LLMEvidenceJournalError("rejected_attempt_observation_fields_invalid")
    snapshot = _FrozenJSONMapping(value)
    if (
        snapshot.get("record_type") != "llm_evidence_observation"
        or snapshot.get("schema_version") != OBSERVATION_SCHEMA_VERSION
        or snapshot.get("status") != "invalid"
        or snapshot.get("reason_code") != "llm_evidence_schema_invalid"
        or snapshot.get("provider") != receipt.get("provider")
        or snapshot.get("model") != receipt.get("model")
        or snapshot.get("output_sha256") != ""
        or snapshot.get("evidence") != {}
        or snapshot.get("authority") != AUTHORITY_DENIED
    ):
        raise LLMEvidenceJournalError("rejected_attempt_observation_invalid")
    for field_name in (
        "request_id",
        "task_type",
        "entity_id",
        "route",
        "prompt_version",
    ):
        _strict_id(snapshot.get(field_name), field_name=field_name)
    _require_sha(snapshot.get("prompt_sha256"), field_name="prompt_sha256")
    _time_text(snapshot.get("document_cutoff"), field_name="document_cutoff")
    evidence_refs = snapshot.get("evidence_refs")
    if not isinstance(evidence_refs, list) or not evidence_refs:
        raise LLMEvidenceJournalError("rejected_attempt_evidence_refs_invalid")
    for evidence_ref in evidence_refs:
        _strict_id(evidence_ref, field_name="evidence_ref")
    return snapshot


class LLMRejectedAttemptAuditJournal(LLMEvidenceJournal):
    """Local-integrity-only Journal for schema-rejected provider attempts."""

    @staticmethod
    def _parse(raw: bytes) -> LLMRejectedAttemptAuditReadback:
        if not raw:
            return LLMRejectedAttemptAuditReadback(
                events=(),
                latest_by_attempt=MappingProxyType({}),
                head_sha256=EMPTY_LLM_EVIDENCE_JOURNAL_HEAD_SHA256,
            )
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise LLMEvidenceJournalError("journal_utf8_invalid") from exc
        if not text.endswith("\n"):
            raise LLMEvidenceJournalError("journal_partial_line")
        expected_previous = EMPTY_LLM_EVIDENCE_JOURNAL_HEAD_SHA256
        events: list[LLMRejectedAttemptAuditEvent] = []
        latest: dict[str, LLMRejectedAttemptAuditEvent] = {}
        for line in text.splitlines():
            if not line:
                raise LLMEvidenceJournalError("journal_empty_line_invalid")
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise LLMEvidenceJournalError("journal_json_invalid") from exc
            if _canonical_json(value) != line:
                raise LLMEvidenceJournalError("journal_json_not_canonical")
            if (
                not isinstance(value, dict)
                or set(value) != _REJECTED_ATTEMPT_AUDIT_EVENT_FIELDS
            ):
                raise LLMEvidenceJournalError("rejected_attempt_event_fields_invalid")
            try:
                receipt = validate_provider_rejected_attempt_receipt_descriptor(
                    value["rejected_attempt_receipt"]
                )
                observation = _validated_rejected_observation(
                    value["observation"],
                    receipt=receipt,
                )
                event = LLMRejectedAttemptAuditEvent(
                    event_id=_strict_id(value["event_id"], field_name="event_id"),
                    attempt_id=_strict_id(
                        value["attempt_id"],
                        field_name="attempt_id",
                    ),
                    occurred_at=_time_text(
                        value["occurred_at"],
                        field_name="occurred_at",
                    ),
                    observation=observation,
                    observation_sha256=_require_sha(
                        value["observation_sha256"],
                        field_name="observation_sha256",
                    ),
                    rejected_attempt_receipt=receipt,
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
            except (LLMEvidenceEnvelopeError, ProviderTransportReceiptError) as exc:
                raise LLMEvidenceJournalError("rejected_attempt_event_invalid") from exc
            receipt_sha256 = str(receipt["receipt_sha256"])
            expected_attempt_id = f"llm-attempt-{receipt_sha256}"
            if (
                event.schema_version
                != "tradingagent.llm_rejected_attempt_audit_event.v1"
                or event.attempt_id != expected_attempt_id
                or event.event_id != f"llm-rejected:{receipt_sha256}"
                or event.occurred_at != receipt["received_at"]
                or event.observation_sha256 != _sha(dict(event.observation))
                or event.previous_event_sha256 != expected_previous
                or event.event_sha256 != _sha(event.without_hash())
            ):
                raise LLMEvidenceJournalError("rejected_attempt_event_invalid")
            if event.attempt_id in latest:
                raise LLMEvidenceJournalError("journal_duplicate_attempt")
            latest[event.attempt_id] = event
            expected_previous = event.event_sha256
            events.append(event)
        return LLMRejectedAttemptAuditReadback(
            events=tuple(events),
            latest_by_attempt=MappingProxyType(dict(latest)),
            head_sha256=expected_previous,
        )

    def append(self, *_: object, **__: object) -> bool:
        raise LLMEvidenceJournalError("rejected_attempt_append_gateway_result_required")

    def append_gateway_result(
        self,
        result: GatewayAnalysisResult,
        *,
        expected_head_sha256: str,
    ) -> bool:
        if type(result) is not GatewayAnalysisResult:
            raise LLMEvidenceJournalError("gateway_analysis_result_required")
        receipt_object = result.rejected_attempt_receipt
        if (
            result.transport_receipt is not None
            or type(receipt_object) is not ProviderRejectedAttemptReceipt
        ):
            raise LLMEvidenceJournalError("rejected_gateway_result_required")
        try:
            receipt_object.verify_integrity()
            receipt = validate_provider_rejected_attempt_receipt_descriptor(
                receipt_object.to_descriptor()
            )
            observation = _validated_rejected_observation(
                result.observation,
                receipt=receipt,
            )
            _require_sha(expected_head_sha256, field_name="expected_head_sha256")
        except (LLMEvidenceEnvelopeError, ProviderTransportReceiptError) as exc:
            raise LLMEvidenceJournalError("rejected_gateway_result_invalid") from exc
        if not self.path.exists() and self._read_head_anchor() is not None:
            raise LLMEvidenceJournalError("journal_missing_with_head_anchor")
        descriptor = self._open_locked(create=True)
        try:
            readback = self._parse(self._read(descriptor))
            self._verify_head_anchor(readback)
            if expected_head_sha256 != readback.head_sha256:
                raise LLMEvidenceJournalError("journal_head_cas_mismatch")
            receipt_sha256 = str(receipt["receipt_sha256"])
            attempt_id = f"llm-attempt-{receipt_sha256}"
            existing = readback.latest_by_attempt.get(attempt_id)
            if existing is not None:
                if dict(existing.observation) == dict(observation) and dict(
                    existing.rejected_attempt_receipt
                ) == dict(receipt):
                    return False
                raise LLMEvidenceJournalError("journal_attempt_id_conflict")
            provisional = LLMRejectedAttemptAuditEvent(
                event_id=f"llm-rejected:{receipt_sha256}",
                attempt_id=attempt_id,
                occurred_at=str(receipt["received_at"]),
                observation=observation,
                observation_sha256=_sha(dict(observation)),
                rejected_attempt_receipt=receipt,
                previous_event_sha256=readback.head_sha256,
                event_sha256="0" * 64,
            )
            event = LLMRejectedAttemptAuditEvent(
                event_id=provisional.event_id,
                attempt_id=provisional.attempt_id,
                occurred_at=provisional.occurred_at,
                observation=provisional.observation,
                observation_sha256=provisional.observation_sha256,
                rejected_attempt_receipt=provisional.rejected_attempt_receipt,
                previous_event_sha256=provisional.previous_event_sha256,
                event_sha256=_sha(provisional.without_hash()),
            )
            line = (_canonical_json(event.canonical_payload()) + "\n").encode("utf-8")
            os.lseek(descriptor, 0, os.SEEK_END)
            if os.write(descriptor, line) != len(line):
                raise LLMEvidenceJournalError("journal_short_write")
            os.fsync(descriptor)
            _verify_open_file_identity(
                self.path,
                descriptor,
                kind="journal",
            )
            verified = self._parse(self._read(descriptor))
            if verified.head_sha256 != event.event_sha256:
                raise LLMEvidenceJournalError("journal_readback_mismatch")
            self._write_head_anchor(verified.head_sha256)
            self._verify_head_anchor(verified)
            return True
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


@dataclass(frozen=True)
class LLMProviderInvocationEvent:
    event_id: str
    invocation_key_sha256: str
    request_id: str
    request_sha256: str
    entity_id: str
    route: str
    provider: str
    model: str
    state: str
    result_receipt_sha256: str
    observation: Mapping[str, object]
    observation_sha256: str
    occurred_at: str
    previous_event_sha256: str
    authority: Mapping[str, bool]
    production_eligible: bool
    local_integrity_only: bool
    event_sha256: str
    schema_version: str = "tradingagent.llm_provider_invocation_event.v1"

    def __post_init__(self) -> None:
        object.__setattr__(self, "observation", _FrozenJSONMapping(self.observation))
        object.__setattr__(self, "authority", _FrozenJSONMapping(self.authority))

    def without_hash(self) -> dict[str, object]:
        return {
            "authority": dict(self.authority),
            "entity_id": self.entity_id,
            "event_id": self.event_id,
            "invocation_key_sha256": self.invocation_key_sha256,
            "local_integrity_only": self.local_integrity_only,
            "model": self.model,
            "observation": dict(self.observation),
            "observation_sha256": self.observation_sha256,
            "occurred_at": self.occurred_at,
            "previous_event_sha256": self.previous_event_sha256,
            "production_eligible": self.production_eligible,
            "provider": self.provider,
            "request_id": self.request_id,
            "request_sha256": self.request_sha256,
            "result_receipt_sha256": self.result_receipt_sha256,
            "route": self.route,
            "schema_version": self.schema_version,
            "state": self.state,
        }

    def canonical_payload(self) -> dict[str, object]:
        return {**self.without_hash(), "event_sha256": self.event_sha256}


@dataclass(frozen=True)
class LLMProviderInvocationReadback:
    events: Tuple[LLMProviderInvocationEvent, ...]
    latest_by_invocation: Mapping[str, LLMProviderInvocationEvent]
    head_sha256: str


def _validated_invocation_observation(
    value: object,
    *,
    request_id: str,
    entity_id: str,
    provider: str,
    model: str,
    state: str,
) -> Mapping[str, object]:
    if state == "in_flight":
        if value != {}:
            raise LLMEvidenceJournalError("provider_invocation_observation_invalid")
        return _FrozenJSONMapping({})
    if not isinstance(value, Mapping) or set(value) != _OBSERVATION_FIELDS:
        raise LLMEvidenceJournalError("provider_invocation_observation_invalid")
    snapshot = _FrozenJSONMapping(value)
    if (
        snapshot.get("record_type") != "llm_evidence_observation"
        or snapshot.get("schema_version") != OBSERVATION_SCHEMA_VERSION
        or snapshot.get("request_id") != request_id
        or snapshot.get("entity_id") != entity_id
        or snapshot.get("authority") != AUTHORITY_DENIED
    ):
        raise LLMEvidenceJournalError("provider_invocation_observation_invalid")
    if state in {"accepted", "rejected"} and (
        snapshot.get("provider") != provider or snapshot.get("model") != model
    ):
        raise LLMEvidenceJournalError("provider_invocation_observation_invalid")
    if state == "no_receipt" and (
        snapshot.get("provider") not in {"", "unavailable", provider}
        or snapshot.get("model") not in {"", "unavailable", model}
    ):
        raise LLMEvidenceJournalError("provider_invocation_observation_invalid")
    if state == "accepted" and snapshot.get("status") != "available":
        raise LLMEvidenceJournalError("provider_invocation_observation_invalid")
    if state == "rejected" and (
        snapshot.get("status") != "invalid"
        or snapshot.get("reason_code") != "llm_evidence_schema_invalid"
        or snapshot.get("evidence") != {}
    ):
        raise LLMEvidenceJournalError("provider_invocation_observation_invalid")
    if state == "no_receipt" and snapshot.get("status") == "available":
        raise LLMEvidenceJournalError("provider_invocation_observation_invalid")
    return snapshot


class LLMProviderInvocationJournal(LLMEvidenceJournal):
    """Local request-claim Journal serialized across one provider call.

    An ``in_flight`` event is durably appended before provider invocation.  The
    file lock remains held until exactly one terminal result is persisted.  A
    crash without a corresponding accepted/rejected result remains unknown and
    is never automatically re-sent.
    """

    @staticmethod
    def _parse(raw: bytes) -> LLMProviderInvocationReadback:
        if not raw:
            return LLMProviderInvocationReadback(
                events=(),
                latest_by_invocation=MappingProxyType({}),
                head_sha256=EMPTY_LLM_EVIDENCE_JOURNAL_HEAD_SHA256,
            )
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise LLMEvidenceJournalError("journal_utf8_invalid") from exc
        if not text.endswith("\n"):
            raise LLMEvidenceJournalError("journal_partial_line")
        expected_previous = EMPTY_LLM_EVIDENCE_JOURNAL_HEAD_SHA256
        events: list[LLMProviderInvocationEvent] = []
        latest: dict[str, LLMProviderInvocationEvent] = {}
        for line in text.splitlines():
            if not line:
                raise LLMEvidenceJournalError("journal_empty_line_invalid")
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise LLMEvidenceJournalError("journal_json_invalid") from exc
            if _canonical_json(value) != line:
                raise LLMEvidenceJournalError("journal_json_not_canonical")
            if (
                not isinstance(value, dict)
                or set(value) != _PROVIDER_INVOCATION_EVENT_FIELDS
            ):
                raise LLMEvidenceJournalError(
                    "provider_invocation_event_fields_invalid"
                )
            try:
                state = _strict_id(value["state"], field_name="state")
                request_id = _strict_id(value["request_id"], field_name="request_id")
                entity_id = _strict_id(value["entity_id"], field_name="entity_id")
                provider = _strict_id(value["provider"], field_name="provider")
                model = _strict_id(value["model"], field_name="model")
                observation = _validated_invocation_observation(
                    value["observation"],
                    request_id=request_id,
                    entity_id=entity_id,
                    provider=provider,
                    model=model,
                    state=state,
                )
                event = LLMProviderInvocationEvent(
                    event_id=_strict_id(value["event_id"], field_name="event_id"),
                    invocation_key_sha256=_require_sha(
                        value["invocation_key_sha256"],
                        field_name="invocation_key_sha256",
                    ),
                    request_id=request_id,
                    request_sha256=_require_sha(
                        value["request_sha256"],
                        field_name="request_sha256",
                    ),
                    entity_id=entity_id,
                    route=_strict_id(value["route"], field_name="route"),
                    provider=provider,
                    model=model,
                    state=state,
                    result_receipt_sha256=str(value["result_receipt_sha256"]),
                    observation=observation,
                    observation_sha256=str(value["observation_sha256"]),
                    occurred_at=_time_text(
                        value["occurred_at"],
                        field_name="occurred_at",
                    ),
                    previous_event_sha256=_require_sha(
                        value["previous_event_sha256"],
                        field_name="previous_event_sha256",
                    ),
                    authority=MappingProxyType(dict(value["authority"])),
                    production_eligible=value["production_eligible"],
                    local_integrity_only=value["local_integrity_only"],
                    event_sha256=_require_sha(
                        value["event_sha256"],
                        field_name="event_sha256",
                    ),
                    schema_version=value["schema_version"],
                )
            except (LLMEvidenceEnvelopeError, TypeError, ValueError) as exc:
                raise LLMEvidenceJournalError(
                    "provider_invocation_event_invalid"
                ) from exc
            if (
                event.schema_version != "tradingagent.llm_provider_invocation_event.v1"
                or event.state not in {"in_flight"} | _PROVIDER_INVOCATION_FINAL_STATES
                or dict(event.authority) != AUTHORITY_DENIED
                or event.production_eligible is not False
                or event.local_integrity_only is not True
                or event.event_id
                != f"llm-invocation:{event.invocation_key_sha256}:{event.state}"
                or event.previous_event_sha256 != expected_previous
                or event.event_sha256 != _sha(event.without_hash())
            ):
                raise LLMEvidenceJournalError("provider_invocation_event_invalid")
            if event.state == "in_flight":
                if (
                    event.invocation_key_sha256 in latest
                    or event.result_receipt_sha256 != ""
                    or event.observation_sha256 != ""
                ):
                    raise LLMEvidenceJournalError(
                        "provider_invocation_transition_invalid"
                    )
            else:
                prior = latest.get(event.invocation_key_sha256)
                if prior is None or prior.state != "in_flight":
                    raise LLMEvidenceJournalError(
                        "provider_invocation_transition_invalid"
                    )
                if any(
                    getattr(prior, field_name) != getattr(event, field_name)
                    for field_name in (
                        "request_id",
                        "request_sha256",
                        "entity_id",
                        "route",
                        "provider",
                        "model",
                    )
                ):
                    raise LLMEvidenceJournalError("provider_invocation_binding_invalid")
                if event.observation_sha256 != _sha(dict(event.observation)):
                    raise LLMEvidenceJournalError(
                        "provider_invocation_observation_invalid"
                    )
                if event.state in {"accepted", "rejected"}:
                    _require_sha(
                        event.result_receipt_sha256,
                        field_name="result_receipt_sha256",
                    )
                elif event.result_receipt_sha256 != "":
                    raise LLMEvidenceJournalError("provider_invocation_receipt_invalid")
            latest[event.invocation_key_sha256] = event
            expected_previous = event.event_sha256
            events.append(event)
        return LLMProviderInvocationReadback(
            events=tuple(events),
            latest_by_invocation=MappingProxyType(dict(latest)),
            head_sha256=expected_previous,
        )

    def append(self, *_: object, **__: object) -> bool:
        raise LLMEvidenceJournalError("provider_invocation_recorder_required")

    def _append_locked(
        self,
        descriptor: int,
        readback: LLMProviderInvocationReadback,
        *,
        invocation_key_sha256: str,
        request_id: str,
        request_sha256: str,
        entity_id: str,
        route: str,
        provider: str,
        model: str,
        state: str,
        result_receipt_sha256: str = "",
        observation: Mapping[str, object] | None = None,
    ) -> LLMProviderInvocationReadback:
        safe_observation = dict(observation or {})
        provisional = LLMProviderInvocationEvent(
            event_id=f"llm-invocation:{invocation_key_sha256}:{state}",
            invocation_key_sha256=invocation_key_sha256,
            request_id=request_id,
            request_sha256=request_sha256,
            entity_id=entity_id,
            route=route,
            provider=provider,
            model=model,
            state=state,
            result_receipt_sha256=result_receipt_sha256,
            observation=safe_observation,
            observation_sha256=_sha(safe_observation) if state != "in_flight" else "",
            occurred_at=datetime.now(timezone.utc).isoformat(),
            previous_event_sha256=readback.head_sha256,
            authority=MappingProxyType(dict(AUTHORITY_DENIED)),
            production_eligible=False,
            local_integrity_only=True,
            event_sha256="0" * 64,
        )
        event = LLMProviderInvocationEvent(
            **{
                **provisional.without_hash(),
                "event_sha256": _sha(provisional.without_hash()),
            }
        )
        line = (_canonical_json(event.canonical_payload()) + "\n").encode("utf-8")
        os.lseek(descriptor, 0, os.SEEK_END)
        if os.write(descriptor, line) != len(line):
            raise LLMEvidenceJournalError("journal_short_write")
        os.fsync(descriptor)
        _verify_open_file_identity(self.path, descriptor, kind="journal")
        verified = self._parse(self._read(descriptor))
        if verified.head_sha256 != event.event_sha256:
            raise LLMEvidenceJournalError("journal_readback_mismatch")
        self._write_head_anchor(verified.head_sha256)
        self._verify_head_anchor(verified)
        return verified


def _logical_provider_invocation_key(
    request: LLMEvidenceRequest,
    *,
    entity_id: str,
    provider: str,
    model: str,
) -> str:
    return _sha(
        {
            "artifact_set_sha256": request.artifact_set_sha256,
            "document_cutoff": request.document_cutoff,
            "entity_id": entity_id,
            "evidence_refs": list(request.evidence_refs),
            "model": model,
            "payload_sha256": request.payload_sha256,
            "prompt_sha256": request.prompt_sha256,
            "prompt_template_id": request.prompt_template_id,
            "prompt_version": request.prompt_version,
            "provider": provider,
            "request_schema_version": request.schema_version,
            "route": request.route,
            "schema_version": "tradingagent.llm_provider_invocation_identity.v1",
            "task_type": request.task_type,
        }
    )


def llm_provenance_journal_paths(
    accepted_path: Path | str,
) -> tuple[Path, Path, Path]:
    """Return the one canonical local provenance Journal family.

    The caller still supplies all paths explicitly, but companions are derived
    from the accepted Journal anchor so two recorders cannot share result
    Journals while silently choosing different invocation locks.
    """

    accepted = _absolute_journal_path(accepted_path)
    return (
        accepted,
        Path(f"{accepted}.rejected-attempts"),
        Path(f"{accepted}.provider-invocations"),
    )


def _verify_distinct_journal_endpoints(
    *journals: LLMEvidenceJournal,
) -> None:
    normalized: dict[str, Path] = {}
    physical: dict[tuple[int, int], Path] = {}
    for journal in journals:
        for path in (journal.path, journal.head_path):
            _reject_symlink_components(path)
            endpoint = unicodedata.normalize(
                "NFC",
                str(path.resolve(strict=False)),
            ).casefold()
            if endpoint in normalized:
                raise LLMEvidenceJournalError(
                    "llm_provenance_journal_endpoints_must_be_distinct"
                )
            normalized[endpoint] = path
            if not path.exists():
                continue
            try:
                identity = os.stat(path, follow_symlinks=False)
            except OSError as exc:
                raise LLMEvidenceJournalError("journal_identity_invalid") from exc
            if (
                not stat.S_ISREG(identity.st_mode)
                or identity.st_nlink != 1
                or identity.st_uid != os.geteuid()
                or stat.S_IMODE(identity.st_mode) != 0o600
            ):
                raise LLMEvidenceJournalError("journal_identity_invalid")
            physical_key = (identity.st_dev, identity.st_ino)
            if physical_key in physical:
                raise LLMEvidenceJournalError(
                    "llm_provenance_journal_endpoints_must_be_distinct"
                )
            physical[physical_key] = path


def _append_with_fresh_head(
    journal: LLMEvidenceJournal,
    payload: LLMEvidenceEnvelope | GatewayAnalysisResult,
) -> None:
    for _ in range(8):
        head = journal.read().head_sha256
        try:
            if type(journal) is LLMEvidenceJournal:
                journal.append(payload, expected_head_sha256=head)  # type: ignore[arg-type]
            elif type(journal) is LLMRejectedAttemptAuditJournal:
                journal.append_gateway_result(payload, expected_head_sha256=head)  # type: ignore[arg-type]
            else:  # pragma: no cover - exact recorder construction invariant
                raise LLMEvidenceJournalError("journal_type_invalid")
            return
        except LLMEvidenceJournalError as exc:
            if str(exc) != "journal_head_cas_mismatch":
                raise
    raise LLMEvidenceJournalError("journal_head_cas_retry_exhausted")


@dataclass(frozen=True)
class LLMEvidenceProvenanceRecorder:
    """Explicit, local-only persistence router for one Gateway result."""

    accepted_journal: LLMEvidenceJournal
    rejected_attempt_journal: LLMRejectedAttemptAuditJournal
    provider_invocation_journal: LLMProviderInvocationJournal
    source_authority_verifier: EvidenceSourceAuthorityVerifier | Any

    def __post_init__(self) -> None:
        if type(self.accepted_journal) is not LLMEvidenceJournal:
            raise LLMEvidenceJournalError("accepted_evidence_journal_required")
        if type(self.rejected_attempt_journal) is not LLMRejectedAttemptAuditJournal:
            raise LLMEvidenceJournalError("rejected_attempt_audit_journal_required")
        if type(self.provider_invocation_journal) is not LLMProviderInvocationJournal:
            raise LLMEvidenceJournalError("provider_invocation_journal_required")
        if self.source_authority_verifier is None:
            raise LLMEvidenceJournalError("source_authority_verifier_required")
        _, expected_rejected, expected_invocation = llm_provenance_journal_paths(
            self.accepted_journal.path
        )
        if (
            self.rejected_attempt_journal.path != expected_rejected
            or self.provider_invocation_journal.path != expected_invocation
        ):
            raise LLMEvidenceJournalError("llm_provenance_journal_family_invalid")
        _verify_distinct_journal_endpoints(
            self.accepted_journal,
            self.rejected_attempt_journal,
            self.provider_invocation_journal,
        )

    def analyze_and_persist(
        self,
        gateway: LLMEvidenceGateway,
        request: LLMEvidenceRequest,
        *,
        entity_id: str,
    ) -> Mapping[str, Any]:
        if type(gateway) is not LLMEvidenceGateway:
            raise LLMEvidenceJournalError("llm_evidence_gateway_required")
        if not isinstance(request, LLMEvidenceRequest):
            raise LLMEvidenceJournalError("llm_request_required")
        route = gateway.router.resolve(request.route)
        adapter = gateway.adapters.get(route.provider) if route is not None else None
        if (
            type(adapter) is not DeepSeekAdapter
            or adapter.source_authority_verifier is not self.source_authority_verifier
        ):
            raise LLMEvidenceJournalError("source_authority_verifier_binding_invalid")
        try:
            request_sha256 = request.request_sha256(route.model)
            invocation_key_sha256 = _logical_provider_invocation_key(
                request,
                entity_id=_strict_id(entity_id, field_name="entity_id"),
                provider=route.provider,
                model=route.model,
            )
        except Exception as exc:
            raise LLMEvidenceJournalError("llm_request_invalid") from exc
        invocation_journal = self.provider_invocation_journal
        if (
            not invocation_journal.path.exists()
            and invocation_journal._read_head_anchor() is not None
        ):
            raise LLMEvidenceJournalError("journal_missing_with_head_anchor")
        descriptor = invocation_journal._open_locked(create=True)
        try:
            invocation_readback = invocation_journal._parse(
                invocation_journal._read(descriptor)
            )
            invocation_journal._verify_head_anchor(invocation_readback)
            accepted_readback = self.accepted_journal.read()
            rejected_readback = self.rejected_attempt_journal.read()
            accepted_request_id_matches = [
                envelope
                for envelope in accepted_readback.latest_by_run.values()
                if envelope.request_id == request.request_id
            ]
            rejected_request_id_matches = [
                event
                for event in rejected_readback.latest_by_attempt.values()
                if event.observation.get("request_id") == request.request_id
            ]
            if any(
                envelope.request_sha256 != request_sha256
                for envelope in accepted_request_id_matches
            ) or any(
                event.rejected_attempt_receipt.get("request_sha256") != request_sha256
                for event in rejected_request_id_matches
            ):
                raise LLMEvidenceJournalError("llm_provenance_request_id_conflict")
            accepted_matches = [
                envelope
                for envelope in accepted_readback.latest_by_run.values()
                if envelope.request_sha256 == request_sha256
            ]
            rejected_matches = [
                event
                for event in rejected_readback.latest_by_attempt.values()
                if event.rejected_attempt_receipt.get("request_sha256")
                == request_sha256
            ]
            if (
                len(accepted_matches) > 1
                or len(rejected_matches) > 1
                or (accepted_matches and rejected_matches)
            ):
                raise LLMEvidenceJournalError("llm_provenance_request_outcome_conflict")
            invocation = invocation_readback.latest_by_invocation.get(
                invocation_key_sha256
            )
            if any(
                event.request_id == request.request_id
                and event.invocation_key_sha256 != invocation_key_sha256
                for event in invocation_readback.latest_by_invocation.values()
            ):
                raise LLMEvidenceJournalError("llm_provenance_request_id_conflict")
            if invocation is not None and (
                invocation.request_id != request.request_id
                or invocation.request_sha256 != request_sha256
                or invocation.entity_id != entity_id
                or invocation.route != request.route
                or invocation.provider != route.provider
                or invocation.model != route.model
            ):
                raise LLMEvidenceJournalError("llm_provenance_request_id_conflict")

            if invocation is not None:
                if invocation.state == "no_receipt":
                    if accepted_matches or rejected_matches:
                        raise LLMEvidenceJournalError(
                            "llm_provenance_request_outcome_conflict"
                        )
                    return _FrozenJSONMapping(invocation.observation)
                if invocation.state in {"accepted", "rejected"}:
                    expected_matches = (
                        accepted_matches
                        if invocation.state == "accepted"
                        else rejected_matches
                    )
                    if len(expected_matches) != 1:
                        raise LLMEvidenceJournalError(
                            "llm_provenance_persisted_outcome_missing"
                        )
                    persisted = expected_matches[0]
                    receipt = (
                        persisted.transport_receipt
                        if invocation.state == "accepted"
                        else persisted.rejected_attempt_receipt
                    )
                    observation = persisted.observation
                    if receipt.get(
                        "receipt_sha256"
                    ) != invocation.result_receipt_sha256 or dict(observation) != dict(
                        invocation.observation
                    ):
                        raise LLMEvidenceJournalError(
                            "llm_provenance_persisted_outcome_mismatch"
                        )
                    return _FrozenJSONMapping(observation)
                if not accepted_matches and not rejected_matches:
                    raise LLMEvidenceJournalError(
                        "llm_provenance_invocation_in_flight_unknown"
                    )
                persisted_state = "accepted" if accepted_matches else "rejected"
                persisted = (
                    accepted_matches[0] if accepted_matches else rejected_matches[0]
                )
                receipt = (
                    persisted.transport_receipt
                    if persisted_state == "accepted"
                    else persisted.rejected_attempt_receipt
                )
                observation = persisted.observation
                invocation_readback = invocation_journal._append_locked(
                    descriptor,
                    invocation_readback,
                    invocation_key_sha256=invocation_key_sha256,
                    request_id=request.request_id,
                    request_sha256=request_sha256,
                    entity_id=entity_id,
                    route=request.route,
                    provider=route.provider,
                    model=route.model,
                    state=persisted_state,
                    result_receipt_sha256=str(receipt["receipt_sha256"]),
                    observation=observation,
                )
                return _FrozenJSONMapping(observation)

            if accepted_matches or rejected_matches:
                raise LLMEvidenceJournalError(
                    "llm_provenance_outcome_without_invocation_claim"
                )
            invocation_readback = invocation_journal._append_locked(
                descriptor,
                invocation_readback,
                invocation_key_sha256=invocation_key_sha256,
                request_id=request.request_id,
                request_sha256=request_sha256,
                entity_id=entity_id,
                route=request.route,
                provider=route.provider,
                model=route.model,
                state="in_flight",
            )
            try:
                result = gateway.analyze_with_provenance(
                    request,
                    entity_id=entity_id,
                )
                if type(result) is not GatewayAnalysisResult:
                    raise LLMEvidenceJournalError("gateway_analysis_result_required")
                result_receipt_sha256 = ""
                if result.transport_receipt is not None:
                    state = "accepted"
                    envelope = LLMEvidenceEnvelope.create(
                        run_id=f"llm-run-{result.transport_receipt.receipt_sha256}",
                        request=request,
                        source_authority_verifier=self.source_authority_verifier,
                        transport_receipt=result.transport_receipt,
                        observation=result.observation,
                    )
                    _append_with_fresh_head(self.accepted_journal, envelope)
                    result_receipt_sha256 = result.transport_receipt.receipt_sha256
                elif result.rejected_attempt_receipt is not None:
                    state = "rejected"
                    _append_with_fresh_head(self.rejected_attempt_journal, result)
                    result_receipt_sha256 = (
                        result.rejected_attempt_receipt.receipt_sha256
                    )
                else:
                    state = "no_receipt"
                invocation_journal._append_locked(
                    descriptor,
                    invocation_readback,
                    invocation_key_sha256=invocation_key_sha256,
                    request_id=request.request_id,
                    request_sha256=request_sha256,
                    entity_id=entity_id,
                    route=request.route,
                    provider=route.provider,
                    model=route.model,
                    state=state,
                    result_receipt_sha256=result_receipt_sha256,
                    observation=result.observation,
                )
                return _FrozenJSONMapping(result.observation)
            except LLMEvidenceJournalError:
                raise
            except Exception as exc:
                raise LLMEvidenceJournalError(
                    "llm_provenance_provider_outcome_unknown"
                ) from exc
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
    "LLMEvidenceProvenanceRecorder",
    "LLMProviderInvocationEvent",
    "LLMProviderInvocationJournal",
    "LLMProviderInvocationReadback",
    "LLMRejectedAttemptAuditEvent",
    "LLMRejectedAttemptAuditJournal",
    "LLMRejectedAttemptAuditReadback",
    "llm_provenance_journal_paths",
]
