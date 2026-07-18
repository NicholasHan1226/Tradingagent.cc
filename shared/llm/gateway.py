"""Provider-neutral, explicitly transport-injected LLM evidence gateway.

The default remains network-free. A caller may inject only the exact offline
fixture type or the exact audited DeepSeek HTTPS transport type.
"""

from __future__ import annotations

import json
import hashlib
import hmac
import re
from collections.abc import Iterator
from dataclasses import InitVar, dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Callable, Mapping

from .evidence_artifact import (
    EvidenceArtifactError,
    EvidenceSourceAuthorityVerifier,
    SourceArtifactPromptInjectionError,
)
from .providers.deepseek_http import (
    DEEPSEEK_EGRESS_POLICY_VERSION,
    DEEPSEEK_HTTP_TRANSPORT_ID,
    DEEPSEEK_HTTP_TRANSPORT_VERSION,
    DeepSeekHTTPTransport,
    DeepSeekHTTPTransportError,
    _create_validated_deepseek_egress,
    _GATEWAY_EGRESS_AUTHORITY_SEAL,
)
from .router import (
    DEEPSEEK_PROVIDER,
    DEEPSEEK_V4_FLASH_MODEL,
    DEEPSEEK_V4_PRO_MODEL,
    LLMRouter,
    ModelRoute,
)
from .deepseek_config import DeepSeekProviderConfig
from .schema import (
    AUTHORITY_DENIED,
    EvidenceSchemaError,
    LLMEvidenceRequest,
    OBSERVATION_SCHEMA_VERSION,
    PromptTemplateError,
    RequestIntegrityError,
    SensitivePayloadError,
    available_observation,
    invalid_observation,
    sha256_text,
    unavailable_observation,
    validate_cloud_egress,
    validate_provider_evidence,
)


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PROVIDER_RESPONSE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_PROVIDER_OUTPUT_SECRET_PATTERNS = (
    re.compile(r"(?i)(?<![a-z0-9])sk-[a-z0-9_-]{12,}"),
    re.compile(r"(?i)(?<![a-z0-9])bearer\s+[a-z0-9._~-]{12,}"),
)
_PROVIDER_OUTPUT_SECRET_KEYS = {
    "access_key",
    "api_key",
    "authorization",
    "credential",
    "credentials",
    "password",
    "secret",
    "secret_key",
    "token",
}
_DEEPSEEK_MAX_TOKENS = {
    "bulk_extraction": 4096,
    "slow_research": 8192,
}
OFFLINE_DEEPSEEK_TRANSPORT_ID = "offline-deepseek-fixture"
OFFLINE_DEEPSEEK_TRANSPORT_VERSION = "offline-fixture-v1"
OFFLINE_DEEPSEEK_EGRESS_POLICY_VERSION = "offline-fixture-v1"
_TRANSPORT_METADATA_FIELDS = {
    "attempt_count",
    "content_type",
    "egress_policy_version",
    "endpoint",
    "http_status",
    "kind",
    "method",
    "request_bytes",
    "response_bytes",
    "retry_disposition",
}
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
_ADAPTER_GATEWAY_AUTHORITY_SEAL = object()
_ACCEPTED_RECEIPT_TRANSPORT_AUTHORITY_SEAL = object()
_REJECTED_ATTEMPT_TRANSPORT_AUTHORITY_SEAL = object()
_GATEWAY_ANALYSIS_AUTHORITY_SEAL = object()


class ProviderTransportReceiptError(ValueError):
    """Raised when provider transport provenance is malformed or mutated."""


class ProviderEvidenceBindingError(EvidenceSchemaError):
    """Raised after transport when evidence cannot be bound to the request."""


class ProviderOutputSensitiveError(EvidenceSchemaError):
    """Raised when provider evidence contains credential-shaped output."""


class _FrozenJSONMapping(Mapping[str, Any]):
    """Immutable JSON snapshot that returns defensive nested copies."""

    __slots__ = ("_canonical_json", "_keys")

    def __init__(self, value: Mapping[str, Any]) -> None:
        try:
            canonical = json.dumps(
                dict(value),
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=False,
                separators=(",", ":"),
            )
            payload = json.loads(canonical)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ProviderTransportReceiptError(
                "gateway_observation_json_invalid"
            ) from exc
        if not isinstance(payload, dict):
            raise ProviderTransportReceiptError("gateway_observation_type_invalid")
        self._canonical_json = canonical
        self._keys = tuple(payload)

    def _payload(self) -> dict[str, Any]:
        payload = json.loads(self._canonical_json)
        if not isinstance(payload, dict):  # pragma: no cover - constructor invariant
            raise ProviderTransportReceiptError("gateway_observation_type_invalid")
        return payload

    def __getitem__(self, key: str) -> Any:
        return self._payload()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._keys)

    def __len__(self) -> int:
        return len(self._keys)

    def __repr__(self) -> str:
        return repr(self._payload())


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _reject_secret_shaped_provider_output(value: object, *, path: str) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ProviderOutputSensitiveError("provider_output_key_invalid")
            normalized = re.sub(r"[^a-z0-9]+", "_", key.casefold()).strip("_")
            if normalized in _PROVIDER_OUTPUT_SECRET_KEYS:
                raise ProviderOutputSensitiveError(
                    f"provider_output_secret_key:{path}.{normalized}"
                )
            _reject_secret_shaped_provider_output(
                item,
                path=f"{path}.{normalized}",
            )
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_secret_shaped_provider_output(
                item,
                path=f"{path}[{index}]",
            )
        return
    if isinstance(value, str) and any(
        pattern.search(value) for pattern in _PROVIDER_OUTPUT_SECRET_PATTERNS
    ):
        raise ProviderOutputSensitiveError(f"provider_output_secret_value:{path}")


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ProviderTransportReceiptError("transport_payload_not_canonical") from exc


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _require_plain_fixture_json(value: Any) -> None:
    value_type = type(value)
    if value is None or value_type in {bool, int, float, str}:
        return
    if value_type is list:
        for item in value:
            _require_plain_fixture_json(item)
        return
    if value_type is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise TypeError("offline_fixture_plain_json_required")
            _require_plain_fixture_json(item)
        return
    raise TypeError("offline_fixture_plain_json_required")


def _strict_text(value: object, *, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\x00" in value
    ):
        raise ProviderTransportReceiptError(f"{field_name}_invalid")
    return value


def _provider_response_id(value: object) -> str:
    candidate = str(value or "unavailable")
    lowered = candidate.casefold()
    if not _PROVIDER_RESPONSE_ID_RE.fullmatch(candidate) or lowered.startswith(
        ("sk-", "token-", "secret-", "key-")
    ):
        raise ProviderTransportReceiptError("provider_response_id_invalid")
    return candidate


def _aware_instant(value: object, *, field_name: str) -> str:
    text = _strict_text(value, field_name=field_name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProviderTransportReceiptError(f"{field_name}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProviderTransportReceiptError(f"{field_name}_timezone_required")
    canonical = parsed.isoformat()
    if text != canonical:
        raise ProviderTransportReceiptError(f"{field_name}_not_canonical")
    return canonical


def _transport_metadata(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != _TRANSPORT_METADATA_FIELDS:
        raise ProviderTransportReceiptError("transport_metadata_invalid")
    kind = _strict_text(value.get("kind"), field_name="transport_kind")
    endpoint = _strict_text(value.get("endpoint"), field_name="transport_endpoint")
    method = _strict_text(value.get("method"), field_name="transport_method")
    policy_version = _strict_text(
        value.get("egress_policy_version"),
        field_name="egress_policy_version",
    )
    content_type = _strict_text(
        value.get("content_type"),
        field_name="transport_content_type",
    )
    retry_disposition = _strict_text(
        value.get("retry_disposition"),
        field_name="retry_disposition",
    )
    integer_fields: dict[str, int] = {}
    for field_name in (
        "http_status",
        "request_bytes",
        "response_bytes",
        "attempt_count",
    ):
        candidate = value.get(field_name)
        if type(candidate) is not int or candidate < 0:
            raise ProviderTransportReceiptError(f"{field_name}_invalid")
        integer_fields[field_name] = candidate
    if kind == "offline_fixture":
        if (
            endpoint != "offline://deepseek-fixture"
            or method != "FIXTURE_RESOLVE"
            or integer_fields["http_status"] != 0
            or retry_disposition != "not_applicable"
        ):
            raise ProviderTransportReceiptError("transport_metadata_invalid")
    elif kind == "https":
        if (
            endpoint != "https://api.deepseek.com/chat/completions"
            or method != "POST"
            or integer_fields["http_status"] != 200
            or retry_disposition != "not_retried"
        ):
            raise ProviderTransportReceiptError("transport_metadata_invalid")
    else:
        raise ProviderTransportReceiptError("transport_kind_invalid")
    if content_type != "application/json" or integer_fields["attempt_count"] != 1:
        raise ProviderTransportReceiptError("transport_metadata_invalid")
    return {
        "kind": kind,
        "endpoint": endpoint,
        "method": method,
        "egress_policy_version": policy_version,
        "http_status": integer_fields["http_status"],
        "content_type": content_type,
        "request_bytes": integer_fields["request_bytes"],
        "response_bytes": integer_fields["response_bytes"],
        "attempt_count": integer_fields["attempt_count"],
        "retry_disposition": retry_disposition,
    }


def _validate_transport_receipt_binding(
    *,
    provider: str,
    model: str,
    transport_id: str,
    transport_version: str,
    transport_metadata: Mapping[str, object],
) -> None:
    if transport_metadata["kind"] == "offline_fixture":
        if (
            provider != DEEPSEEK_PROVIDER
            or transport_id != OFFLINE_DEEPSEEK_TRANSPORT_ID
            or transport_version != OFFLINE_DEEPSEEK_TRANSPORT_VERSION
            or transport_metadata["egress_policy_version"]
            != OFFLINE_DEEPSEEK_EGRESS_POLICY_VERSION
            or int(transport_metadata["request_bytes"]) <= 0
            or int(transport_metadata["response_bytes"]) <= 0
        ):
            raise ProviderTransportReceiptError(
                "offline_transport_receipt_binding_invalid"
            )
        return
    if (
        provider != DEEPSEEK_PROVIDER
        or model not in {DEEPSEEK_V4_FLASH_MODEL, DEEPSEEK_V4_PRO_MODEL}
        or transport_id != DEEPSEEK_HTTP_TRANSPORT_ID
        or transport_version != DEEPSEEK_HTTP_TRANSPORT_VERSION
        or transport_metadata["egress_policy_version"] != DEEPSEEK_EGRESS_POLICY_VERSION
        or int(transport_metadata["request_bytes"]) <= 0
        or int(transport_metadata["response_bytes"]) <= 0
    ):
        raise ProviderTransportReceiptError("https_transport_receipt_binding_invalid")


@dataclass(frozen=True)
class ProviderTransportReceipt:
    """Content-addressed receipt emitted by the injected transport boundary."""

    provider: str
    model: str
    transport_id: str
    transport_version: str
    verified_at: str
    request_sha256: str
    source_authority_proof_set_sha256: str
    transport_material_sha256: str
    outbound_sha256: str
    response_sha256: str
    normalized_evidence_sha256: str
    provider_response_id: str
    received_at: str
    transport_metadata: Mapping[str, object]
    receipt_sha256: str
    _transport_authority_seal: InitVar[object | None] = None

    def __post_init__(self, _transport_authority_seal: object | None) -> None:
        normalized_transport_metadata = _transport_metadata(self.transport_metadata)
        if (
            normalized_transport_metadata["kind"] == "https"
            and _transport_authority_seal
            is not _ACCEPTED_RECEIPT_TRANSPORT_AUTHORITY_SEAL
        ):
            raise ProviderTransportReceiptError(
                "accepted_receipt_transport_authority_required"
            )
        object.__setattr__(
            self,
            "transport_metadata",
            MappingProxyType(normalized_transport_metadata),
        )

    @classmethod
    def create(
        cls,
        *,
        provider: str,
        model: str,
        transport_id: str,
        transport_version: str,
        verified_at: str,
        request_sha256: str,
        source_authority_proof_set_sha256: str,
        transport_material_sha256: str,
        outbound_sha256: str,
        response_sha256: str,
        normalized_evidence_sha256: str,
        provider_response_id: str,
        received_at: str,
        transport_metadata: Mapping[str, object],
        _transport_authority_seal: object | None = None,
    ) -> "ProviderTransportReceipt":
        normalized_provider = _strict_text(provider, field_name="provider")
        normalized_model = _strict_text(model, field_name="model")
        normalized_transport_id = _strict_text(
            transport_id,
            field_name="transport_id",
        )
        normalized_transport_version = _strict_text(
            transport_version,
            field_name="transport_version",
        )
        normalized_transport_metadata = _transport_metadata(transport_metadata)
        _validate_transport_receipt_binding(
            provider=normalized_provider,
            model=normalized_model,
            transport_id=normalized_transport_id,
            transport_version=normalized_transport_version,
            transport_metadata=normalized_transport_metadata,
        )
        if (
            normalized_transport_metadata["kind"] == "https"
            and _transport_authority_seal
            is not _ACCEPTED_RECEIPT_TRANSPORT_AUTHORITY_SEAL
        ):
            raise ProviderTransportReceiptError(
                "accepted_receipt_transport_authority_required"
            )
        identity = {
            "provider": normalized_provider,
            "model": normalized_model,
            "transport_id": normalized_transport_id,
            "transport_version": normalized_transport_version,
            "verified_at": _aware_instant(verified_at, field_name="verified_at"),
            "request_sha256": str(request_sha256),
            "source_authority_proof_set_sha256": str(source_authority_proof_set_sha256),
            "transport_material_sha256": str(transport_material_sha256),
            "outbound_sha256": str(outbound_sha256),
            "response_sha256": str(response_sha256),
            "normalized_evidence_sha256": str(normalized_evidence_sha256),
            "provider_response_id": _provider_response_id(provider_response_id),
            "received_at": _aware_instant(received_at, field_name="received_at"),
            "transport_metadata": normalized_transport_metadata,
        }
        receipt = cls(
            **identity,
            receipt_sha256=_sha256_json(identity),
            _transport_authority_seal=(
                _ACCEPTED_RECEIPT_TRANSPORT_AUTHORITY_SEAL
                if normalized_transport_metadata["kind"] == "https"
                else None
            ),
        )
        receipt.verify_integrity()
        return receipt

    def _identity_payload(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "model": self.model,
            "transport_id": self.transport_id,
            "transport_version": self.transport_version,
            "verified_at": self.verified_at,
            "request_sha256": self.request_sha256,
            "source_authority_proof_set_sha256": (
                self.source_authority_proof_set_sha256
            ),
            "transport_material_sha256": self.transport_material_sha256,
            "outbound_sha256": self.outbound_sha256,
            "response_sha256": self.response_sha256,
            "normalized_evidence_sha256": self.normalized_evidence_sha256,
            "provider_response_id": self.provider_response_id,
            "received_at": self.received_at,
            "transport_metadata": dict(self.transport_metadata),
        }

    def verify_integrity(self) -> None:
        _strict_text(self.provider, field_name="provider")
        _strict_text(self.model, field_name="model")
        _strict_text(self.transport_id, field_name="transport_id")
        _strict_text(self.transport_version, field_name="transport_version")
        _provider_response_id(self.provider_response_id)
        normalized_transport_metadata = _transport_metadata(self.transport_metadata)
        _validate_transport_receipt_binding(
            provider=self.provider,
            model=self.model,
            transport_id=self.transport_id,
            transport_version=self.transport_version,
            transport_metadata=normalized_transport_metadata,
        )
        verified_at = datetime.fromisoformat(
            _aware_instant(self.verified_at, field_name="verified_at")
        )
        received_at = datetime.fromisoformat(
            _aware_instant(self.received_at, field_name="received_at")
        )
        if received_at < verified_at:
            raise ProviderTransportReceiptError("transport_receipt_time_order_invalid")
        for field_name in (
            "request_sha256",
            "source_authority_proof_set_sha256",
            "transport_material_sha256",
            "outbound_sha256",
            "response_sha256",
            "normalized_evidence_sha256",
        ):
            if not _SHA256_RE.fullmatch(str(getattr(self, field_name))):
                raise ProviderTransportReceiptError(f"{field_name}_invalid")
        expected = _sha256_json(self._identity_payload())
        if not _SHA256_RE.fullmatch(
            str(self.receipt_sha256)
        ) or not hmac.compare_digest(self.receipt_sha256, expected):
            raise ProviderTransportReceiptError("transport_receipt_sha256_mismatch")

    def to_descriptor(self) -> dict[str, object]:
        self.verify_integrity()
        return {**self._identity_payload(), "receipt_sha256": self.receipt_sha256}


_PROVIDER_TRANSPORT_RECEIPT_DESCRIPTOR_FIELDS = {
    "model",
    "normalized_evidence_sha256",
    "outbound_sha256",
    "provider",
    "provider_response_id",
    "receipt_sha256",
    "received_at",
    "request_sha256",
    "response_sha256",
    "source_authority_proof_set_sha256",
    "transport_id",
    "transport_material_sha256",
    "transport_metadata",
    "transport_version",
    "verified_at",
}


def validate_provider_transport_receipt_descriptor(
    value: Mapping[str, object],
) -> Mapping[str, object]:
    """Validate persisted receipt facts without minting runtime authority."""

    if (
        not isinstance(value, Mapping)
        or set(value) != _PROVIDER_TRANSPORT_RECEIPT_DESCRIPTOR_FIELDS
    ):
        raise ProviderTransportReceiptError("transport_receipt_descriptor_invalid")
    metadata = value.get("transport_metadata")
    if not isinstance(metadata, Mapping):
        raise ProviderTransportReceiptError("transport_metadata_invalid")
    normalized_metadata = _transport_metadata(metadata)
    provider = _strict_text(value.get("provider"), field_name="provider")
    model = _strict_text(value.get("model"), field_name="model")
    transport_id = _strict_text(
        value.get("transport_id"),
        field_name="transport_id",
    )
    transport_version = _strict_text(
        value.get("transport_version"),
        field_name="transport_version",
    )
    _validate_transport_receipt_binding(
        provider=provider,
        model=model,
        transport_id=transport_id,
        transport_version=transport_version,
        transport_metadata=normalized_metadata,
    )
    verified_at = _aware_instant(value.get("verified_at"), field_name="verified_at")
    received_at = _aware_instant(value.get("received_at"), field_name="received_at")
    if datetime.fromisoformat(received_at) < datetime.fromisoformat(verified_at):
        raise ProviderTransportReceiptError("transport_receipt_time_order_invalid")
    identity = {
        "provider": provider,
        "model": model,
        "transport_id": transport_id,
        "transport_version": transport_version,
        "verified_at": verified_at,
        "request_sha256": value.get("request_sha256"),
        "source_authority_proof_set_sha256": value.get(
            "source_authority_proof_set_sha256"
        ),
        "transport_material_sha256": value.get("transport_material_sha256"),
        "outbound_sha256": value.get("outbound_sha256"),
        "response_sha256": value.get("response_sha256"),
        "normalized_evidence_sha256": value.get("normalized_evidence_sha256"),
        "provider_response_id": _provider_response_id(
            value.get("provider_response_id")
        ),
        "received_at": received_at,
        "transport_metadata": normalized_metadata,
    }
    for field_name in (
        "request_sha256",
        "source_authority_proof_set_sha256",
        "transport_material_sha256",
        "outbound_sha256",
        "response_sha256",
        "normalized_evidence_sha256",
    ):
        if not _SHA256_RE.fullmatch(str(identity[field_name])):
            raise ProviderTransportReceiptError(f"{field_name}_invalid")
    receipt_sha256 = value.get("receipt_sha256")
    if not isinstance(receipt_sha256, str) or not _SHA256_RE.fullmatch(receipt_sha256):
        raise ProviderTransportReceiptError("transport_receipt_sha256_mismatch")
    if not hmac.compare_digest(receipt_sha256, _sha256_json(identity)):
        raise ProviderTransportReceiptError("transport_receipt_sha256_mismatch")
    return MappingProxyType(
        {
            **identity,
            "transport_metadata": MappingProxyType(dict(normalized_metadata)),
            "receipt_sha256": receipt_sha256,
        }
    )


@dataclass(frozen=True)
class ProviderRejectedAttemptReceipt:
    """Audit-only receipt for an HTTP response rejected as evidence."""

    schema_version: str
    receipt_kind: str
    outcome: str
    rejection_stage: str
    reason_code: str
    provider: str
    model: str
    transport_id: str
    transport_version: str
    verified_at: str
    received_at: str
    request_sha256: str
    source_authority_proof_set_sha256: str
    transport_material_sha256: str
    outbound_sha256: str
    response_sha256: str
    transport_metadata: Mapping[str, object]
    evidence_accepted: bool
    evidence_journal_eligible: bool
    production_eligible: bool
    audit_only: bool
    authority: Mapping[str, bool]
    receipt_sha256: str
    _transport_authority_seal: InitVar[object | None] = None

    def __post_init__(self, _transport_authority_seal: object | None) -> None:
        if _transport_authority_seal is not _REJECTED_ATTEMPT_TRANSPORT_AUTHORITY_SEAL:
            raise ProviderTransportReceiptError(
                "rejected_attempt_transport_authority_required"
            )
        object.__setattr__(
            self,
            "transport_metadata",
            MappingProxyType(_transport_metadata(self.transport_metadata)),
        )
        object.__setattr__(
            self,
            "authority",
            MappingProxyType(dict(self.authority)),
        )

    @classmethod
    def create(
        cls,
        *,
        provider: str,
        model: str,
        transport_id: str,
        transport_version: str,
        rejection_stage: str = "provider_evidence_validation",
        verified_at: str,
        received_at: str,
        request_sha256: str,
        source_authority_proof_set_sha256: str,
        transport_material_sha256: str,
        outbound_sha256: str,
        response_sha256: str,
        transport_metadata: Mapping[str, object],
        _transport_authority_seal: object | None = None,
    ) -> "ProviderRejectedAttemptReceipt":
        if _transport_authority_seal is not _REJECTED_ATTEMPT_TRANSPORT_AUTHORITY_SEAL:
            raise ProviderTransportReceiptError(
                "rejected_attempt_transport_authority_required"
            )
        normalized_rejection_stage = _strict_text(
            rejection_stage,
            field_name="rejection_stage",
        )
        if normalized_rejection_stage not in {
            "provider_evidence_validation",
            "gateway_observation_binding",
        }:
            raise ProviderTransportReceiptError("rejected_attempt_stage_invalid")
        normalized_provider = _strict_text(provider, field_name="provider")
        normalized_model = _strict_text(model, field_name="model")
        normalized_transport_id = _strict_text(
            transport_id,
            field_name="transport_id",
        )
        normalized_transport_version = _strict_text(
            transport_version,
            field_name="transport_version",
        )
        normalized_transport_metadata = _transport_metadata(transport_metadata)
        if normalized_transport_metadata["kind"] != "https":
            raise ProviderTransportReceiptError(
                "rejected_attempt_transport_kind_invalid"
            )
        _validate_transport_receipt_binding(
            provider=normalized_provider,
            model=normalized_model,
            transport_id=normalized_transport_id,
            transport_version=normalized_transport_version,
            transport_metadata=normalized_transport_metadata,
        )
        identity = {
            "schema_version": ("tradingagent.llm_provider_rejected_attempt_receipt.v1"),
            "receipt_kind": "provider_rejected_attempt",
            "outcome": "rejected",
            "rejection_stage": normalized_rejection_stage,
            "reason_code": "llm_evidence_schema_invalid",
            "provider": normalized_provider,
            "model": normalized_model,
            "transport_id": normalized_transport_id,
            "transport_version": normalized_transport_version,
            "verified_at": _aware_instant(verified_at, field_name="verified_at"),
            "received_at": _aware_instant(received_at, field_name="received_at"),
            "request_sha256": str(request_sha256),
            "source_authority_proof_set_sha256": str(source_authority_proof_set_sha256),
            "transport_material_sha256": str(transport_material_sha256),
            "outbound_sha256": str(outbound_sha256),
            "response_sha256": str(response_sha256),
            "transport_metadata": normalized_transport_metadata,
            "evidence_accepted": False,
            "evidence_journal_eligible": False,
            "production_eligible": False,
            "audit_only": True,
            "authority": dict(AUTHORITY_DENIED),
        }
        receipt = cls(
            **identity,
            receipt_sha256=_sha256_json(identity),
            _transport_authority_seal=(_REJECTED_ATTEMPT_TRANSPORT_AUTHORITY_SEAL),
        )
        receipt.verify_integrity()
        return receipt

    def _identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "receipt_kind": self.receipt_kind,
            "outcome": self.outcome,
            "rejection_stage": self.rejection_stage,
            "reason_code": self.reason_code,
            "provider": self.provider,
            "model": self.model,
            "transport_id": self.transport_id,
            "transport_version": self.transport_version,
            "verified_at": self.verified_at,
            "received_at": self.received_at,
            "request_sha256": self.request_sha256,
            "source_authority_proof_set_sha256": (
                self.source_authority_proof_set_sha256
            ),
            "transport_material_sha256": self.transport_material_sha256,
            "outbound_sha256": self.outbound_sha256,
            "response_sha256": self.response_sha256,
            "transport_metadata": dict(self.transport_metadata),
            "evidence_accepted": self.evidence_accepted,
            "evidence_journal_eligible": self.evidence_journal_eligible,
            "production_eligible": self.production_eligible,
            "audit_only": self.audit_only,
            "authority": dict(self.authority),
        }

    def verify_integrity(self) -> None:
        if (
            self.schema_version
            != "tradingagent.llm_provider_rejected_attempt_receipt.v1"
            or self.receipt_kind != "provider_rejected_attempt"
            or self.outcome != "rejected"
            or self.rejection_stage
            not in {
                "provider_evidence_validation",
                "gateway_observation_binding",
            }
            or self.reason_code != "llm_evidence_schema_invalid"
            or self.evidence_accepted is not False
            or self.evidence_journal_eligible is not False
            or self.production_eligible is not False
            or self.audit_only is not True
            or dict(self.authority) != AUTHORITY_DENIED
        ):
            raise ProviderTransportReceiptError("rejected_attempt_state_invalid")
        normalized_transport_metadata = _transport_metadata(self.transport_metadata)
        if normalized_transport_metadata["kind"] != "https":
            raise ProviderTransportReceiptError(
                "rejected_attempt_transport_kind_invalid"
            )
        _validate_transport_receipt_binding(
            provider=self.provider,
            model=self.model,
            transport_id=self.transport_id,
            transport_version=self.transport_version,
            transport_metadata=normalized_transport_metadata,
        )
        verified_at = datetime.fromisoformat(
            _aware_instant(self.verified_at, field_name="verified_at")
        )
        received_at = datetime.fromisoformat(
            _aware_instant(self.received_at, field_name="received_at")
        )
        if received_at < verified_at:
            raise ProviderTransportReceiptError("rejected_attempt_time_order_invalid")
        for field_name in (
            "request_sha256",
            "source_authority_proof_set_sha256",
            "transport_material_sha256",
            "outbound_sha256",
            "response_sha256",
        ):
            if not _SHA256_RE.fullmatch(str(getattr(self, field_name))):
                raise ProviderTransportReceiptError(f"{field_name}_invalid")
        expected = _sha256_json(self._identity_payload())
        if not _SHA256_RE.fullmatch(
            str(self.receipt_sha256)
        ) or not hmac.compare_digest(self.receipt_sha256, expected):
            raise ProviderTransportReceiptError(
                "rejected_attempt_receipt_sha256_mismatch"
            )

    def to_descriptor(self) -> dict[str, object]:
        self.verify_integrity()
        return {**self._identity_payload(), "receipt_sha256": self.receipt_sha256}


@dataclass(frozen=True)
class ProviderInvocationResult:
    evidence: Mapping[str, Any]
    transport_receipt: ProviderTransportReceipt


def _verify_gateway_request_binding(
    *,
    observation: Mapping[str, Any],
    request: LLMEvidenceRequest | None,
    entity_id: str,
    receipt: ProviderTransportReceipt | ProviderRejectedAttemptReceipt,
    source_authority_verifier: EvidenceSourceAuthorityVerifier | Any | None,
) -> None:
    if not isinstance(request, LLMEvidenceRequest):
        raise ProviderTransportReceiptError(
            "gateway_observation_request_binding_invalid"
        )
    if set(observation) != _OBSERVATION_FIELDS:
        raise ProviderTransportReceiptError(
            "gateway_observation_request_binding_invalid"
        )
    expected = {
        "record_type": "llm_evidence_observation",
        "schema_version": OBSERVATION_SCHEMA_VERSION,
        "request_id": request.request_id,
        "task_type": request.task_type,
        "entity_id": str(entity_id),
        "route": request.route,
        "prompt_version": request.prompt_version,
        "prompt_sha256": request.prompt_sha256,
        "document_cutoff": request.document_cutoff,
        "evidence_refs": list(request.evidence_refs),
    }
    if any(observation.get(field) != value for field, value in expected.items()):
        raise ProviderTransportReceiptError(
            "gateway_observation_request_binding_invalid"
        )
    try:
        material = request.validate_for_transport(
            receipt.model,
            source_authority_verifier=source_authority_verifier,
            verified_at=receipt.verified_at,
        )
        metadata = material["metadata"]
        material_sha256 = _sha256_json(material)
    except Exception as exc:
        raise ProviderTransportReceiptError(
            "gateway_receipt_request_binding_invalid"
        ) from exc
    if (
        not hmac.compare_digest(
            str(metadata.get("request_sha256") or ""),
            receipt.request_sha256,
        )
        or not hmac.compare_digest(
            str(metadata.get("source_authority_proof_set_sha256") or ""),
            receipt.source_authority_proof_set_sha256,
        )
        or not hmac.compare_digest(
            material_sha256,
            receipt.transport_material_sha256,
        )
    ):
        raise ProviderTransportReceiptError("gateway_receipt_request_binding_invalid")


@dataclass(frozen=True)
class GatewayAnalysisResult:
    """Safe observation plus provenance exposed only after schema validation."""

    observation: Mapping[str, Any]
    transport_receipt: ProviderTransportReceipt | None
    rejected_attempt_receipt: ProviderRejectedAttemptReceipt | None = None
    _gateway_authority_seal: InitVar[object | None] = None
    _request: InitVar[LLMEvidenceRequest | None] = None
    _entity_id: InitVar[str] = ""
    _source_authority_verifier: InitVar[
        EvidenceSourceAuthorityVerifier | Any | None
    ] = None

    def __post_init__(
        self,
        _gateway_authority_seal: object | None,
        _request: LLMEvidenceRequest | None,
        _entity_id: str,
        _source_authority_verifier: EvidenceSourceAuthorityVerifier | Any | None,
    ) -> None:
        if not isinstance(self.observation, Mapping):
            raise ProviderTransportReceiptError("gateway_observation_type_invalid")
        if (
            self.transport_receipt is not None
            and self.rejected_attempt_receipt is not None
        ):
            raise ProviderTransportReceiptError("gateway_receipt_outcome_ambiguous")
        if (
            self.transport_receipt is not None
            or self.rejected_attempt_receipt is not None
        ) and _gateway_authority_seal is not _GATEWAY_ANALYSIS_AUTHORITY_SEAL:
            raise ProviderTransportReceiptError("gateway_analysis_authority_required")
        object.__setattr__(
            self,
            "observation",
            _FrozenJSONMapping(self.observation),
        )
        if self.transport_receipt is not None:
            if type(self.transport_receipt) is not ProviderTransportReceipt:
                raise ProviderTransportReceiptError(
                    "gateway_accepted_receipt_type_invalid"
                )
            self.transport_receipt.verify_integrity()
            if self.observation.get("status") != "available":
                raise ProviderTransportReceiptError(
                    "gateway_accepted_receipt_status_invalid"
                )
            if (
                self.observation.get("provider") != self.transport_receipt.provider
                or self.observation.get("model") != self.transport_receipt.model
            ):
                raise ProviderTransportReceiptError(
                    "gateway_accepted_receipt_route_invalid"
                )
            if (
                self.observation.get("output_sha256")
                != self.transport_receipt.normalized_evidence_sha256
            ):
                raise ProviderTransportReceiptError(
                    "gateway_accepted_receipt_output_invalid"
                )
            evidence = self.observation.get("evidence")
            if not isinstance(evidence, Mapping):
                raise ProviderTransportReceiptError(
                    "gateway_accepted_observation_evidence_invalid"
                )
            try:
                evidence_sha256 = sha256_text(
                    json.dumps(
                        dict(evidence),
                        ensure_ascii=False,
                        allow_nan=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
            except (TypeError, ValueError) as exc:
                raise ProviderTransportReceiptError(
                    "gateway_accepted_observation_evidence_invalid"
                ) from exc
            if evidence_sha256 != self.observation.get("output_sha256"):
                raise ProviderTransportReceiptError(
                    "gateway_accepted_observation_output_invalid"
                )
            if (
                self.observation.get("reason_code") != ""
                or self.observation.get("authority") != AUTHORITY_DENIED
            ):
                raise ProviderTransportReceiptError(
                    "gateway_accepted_observation_state_invalid"
                )
        if self.rejected_attempt_receipt is not None:
            if (
                type(self.rejected_attempt_receipt)
                is not ProviderRejectedAttemptReceipt
            ):
                raise ProviderTransportReceiptError(
                    "gateway_rejected_receipt_type_invalid"
                )
            self.rejected_attempt_receipt.verify_integrity()
            if self.observation.get("status") != "invalid":
                raise ProviderTransportReceiptError(
                    "gateway_rejected_receipt_status_invalid"
                )
            if (
                self.observation.get("reason_code")
                != self.rejected_attempt_receipt.reason_code
            ):
                raise ProviderTransportReceiptError(
                    "gateway_rejected_receipt_reason_invalid"
                )
            if (
                self.observation.get("provider")
                != self.rejected_attempt_receipt.provider
                or self.observation.get("model") != self.rejected_attempt_receipt.model
            ):
                raise ProviderTransportReceiptError(
                    "gateway_rejected_receipt_route_invalid"
                )
            if (
                self.observation.get("output_sha256") != ""
                or self.observation.get("evidence") != {}
                or self.observation.get("authority") != AUTHORITY_DENIED
            ):
                raise ProviderTransportReceiptError(
                    "gateway_rejected_observation_state_invalid"
                )
        receipt_for_binding = self.transport_receipt or self.rejected_attempt_receipt
        if receipt_for_binding is not None:
            _verify_gateway_request_binding(
                observation=self.observation,
                request=_request,
                entity_id=_entity_id,
                receipt=receipt_for_binding,
                source_authority_verifier=_source_authority_verifier,
            )
        if (
            self.observation.get("status") == "available"
            and self.transport_receipt is None
        ):
            raise ProviderTransportReceiptError("gateway_available_receipt_required")


@dataclass(frozen=True)
class OfflineDeepSeekFixtureTransport:
    """Content-addressed offline response fixture.

    The fixture stores canonical JSON, not executable code.  It is resolved by
    the request hash only after the canonical adapter has re-verified source
    authority and the final outbound DLP envelope. Both the canonical request
    and final provider envelope are part of the fixture identity.
    """

    request_sha256: str
    outbound_sha256: str
    response_json: str = field(repr=False)
    response_sha256: str
    fixture_sha256: str

    @classmethod
    def from_response(
        cls,
        *,
        request_sha256: str,
        outbound_sha256: str,
        response: Mapping[str, Any],
    ) -> "OfflineDeepSeekFixtureTransport":
        if not isinstance(response, Mapping):
            raise TypeError("offline_fixture_response_object_required")
        if type(response) is not dict:
            raise TypeError("offline_fixture_plain_json_required")
        _require_plain_fixture_json(response)
        response_json = _canonical_json(response)
        response_sha256 = hashlib.sha256(response_json.encode("utf-8")).hexdigest()
        identity = {
            "request_sha256": str(request_sha256),
            "outbound_sha256": str(outbound_sha256),
            "response_sha256": response_sha256,
        }
        return cls(
            request_sha256=str(request_sha256),
            outbound_sha256=str(outbound_sha256),
            response_json=response_json,
            response_sha256=response_sha256,
            fixture_sha256=_sha256_json(identity),
        )

    def __post_init__(self) -> None:
        if not _SHA256_RE.fullmatch(self.request_sha256):
            raise ValueError("offline_fixture_request_sha256_invalid")
        if not _SHA256_RE.fullmatch(self.outbound_sha256):
            raise ValueError("offline_fixture_outbound_sha256_invalid")
        if not _SHA256_RE.fullmatch(self.response_sha256):
            raise ValueError("offline_fixture_response_sha256_invalid")
        if not _SHA256_RE.fullmatch(self.fixture_sha256):
            raise ValueError("offline_fixture_sha256_invalid")
        try:
            response = json.loads(self.response_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("offline_fixture_response_json_invalid") from exc
        if not isinstance(response, Mapping):
            raise TypeError("offline_fixture_response_object_required")
        if not hmac.compare_digest(
            self.response_sha256,
            hashlib.sha256(_canonical_json(response).encode("utf-8")).hexdigest(),
        ):
            raise ValueError("offline_fixture_response_sha256_mismatch")
        expected_fixture_sha = _sha256_json(
            {
                "request_sha256": self.request_sha256,
                "outbound_sha256": self.outbound_sha256,
                "response_sha256": self.response_sha256,
            }
        )
        if not hmac.compare_digest(self.fixture_sha256, expected_fixture_sha):
            raise ValueError("offline_fixture_sha256_mismatch")

    def resolve(
        self,
        *,
        request_sha256: str,
        outbound_sha256: str,
    ) -> Mapping[str, Any]:
        OfflineDeepSeekFixtureTransport.__post_init__(self)
        if not _SHA256_RE.fullmatch(str(outbound_sha256)):
            raise ValueError("offline_fixture_outbound_sha256_invalid")
        if not hmac.compare_digest(self.request_sha256, str(request_sha256)):
            raise LookupError("offline_fixture_request_unrecognized")
        if not hmac.compare_digest(self.outbound_sha256, str(outbound_sha256)):
            raise LookupError("offline_fixture_outbound_unrecognized")
        response = json.loads(self.response_json)
        if not isinstance(response, Mapping):  # pragma: no cover - re-verified above
            raise TypeError("offline_fixture_response_object_required")
        return response


@dataclass
class DeepSeekAdapter:
    """DeepSeek adapter limited to two exact, audited transport classes."""

    transport: OfflineDeepSeekFixtureTransport | DeepSeekHTTPTransport | None = field(
        default=None,
        repr=False,
    )
    source_authority_verifier: EvidenceSourceAuthorityVerifier | Any | None = field(
        default=None,
        repr=False,
    )
    clock: Callable[[], str] | None = field(default=None, repr=False)

    def invoke(
        self,
        request: LLMEvidenceRequest,
        route: ModelRoute,
        *,
        _gateway_authority_seal: object | None = None,
        _rejected_attempt_receipt_sink: (
            Callable[[ProviderRejectedAttemptReceipt], None] | None
        ) = None,
    ) -> ProviderInvocationResult:
        if self.transport is None:
            raise RuntimeError("deepseek_transport_unavailable")
        transport_type = type(self.transport)
        if transport_type not in {
            OfflineDeepSeekFixtureTransport,
            DeepSeekHTTPTransport,
        }:
            raise RuntimeError("deepseek_transport_policy_rejected")
        if (
            transport_type is DeepSeekHTTPTransport
            and _gateway_authority_seal is not _ADAPTER_GATEWAY_AUTHORITY_SEAL
        ):
            raise DeepSeekHTTPTransportError("deepseek_http_gateway_authority_required")
        # Re-check immediately before transport. This binds the fixed prompt,
        # verified point-in-time evidence and structured payload to the model.
        # It also catches mutations inside an otherwise frozen request object.
        if transport_type is OfflineDeepSeekFixtureTransport:
            verified_at = self.clock() if self.clock is not None else _utc_now_iso()
        else:
            # A real network receipt cannot inherit a caller-controlled clock.
            verified_at = _utc_now_iso()
        verified_at = _aware_instant(verified_at, field_name="verified_at")
        material = request.validate_for_transport(
            route.model,
            source_authority_verifier=self.source_authority_verifier,
            verified_at=verified_at,
        )
        transport_material_sha = _sha256_json(material)
        outbound = {
            "model": material["model"],
            "messages": [
                {"role": "system", "content": material["prompt_text"]},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "payload": material["payload"],
                            "untrusted_artifact_data": material["artifacts"],
                        },
                        ensure_ascii=False,
                        allow_nan=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                },
            ],
            "response_format": {"type": "json_object"},
            "stream": False,
            "max_tokens": _DEEPSEEK_MAX_TOKENS.get(route.route, 4096),
        }
        if route.route == "bulk_extraction":
            outbound["thinking"] = {"type": "disabled"}
        elif route.route == "slow_research":
            outbound["thinking"] = {"type": "enabled"}
            outbound["reasoning_effort"] = "high"
        # Validate the exact provider envelope at the last possible
        # pre-transport step. Internal authority proofs remain local and are
        # content-bound through ``transport_material_sha256`` on the receipt;
        # they are not sent as undocumented provider request fields.
        validate_cloud_egress(outbound)
        outbound_sha = _sha256_json(outbound)
        if transport_type is OfflineDeepSeekFixtureTransport:
            raw = OfflineDeepSeekFixtureTransport.resolve(
                self.transport,
                request_sha256=material["metadata"]["request_sha256"],
                outbound_sha256=outbound_sha,
            )
            received_at = self.clock() if self.clock is not None else _utc_now_iso()
            response_sha = _sha256_json(raw)
            transport_id = OFFLINE_DEEPSEEK_TRANSPORT_ID
            transport_version = OFFLINE_DEEPSEEK_TRANSPORT_VERSION
            transport_metadata = {
                "kind": "offline_fixture",
                "endpoint": "offline://deepseek-fixture",
                "method": "FIXTURE_RESOLVE",
                "egress_policy_version": OFFLINE_DEEPSEEK_EGRESS_POLICY_VERSION,
                "http_status": 0,
                "content_type": "application/json",
                "request_bytes": len(_canonical_json(outbound).encode("utf-8")),
                "response_bytes": len(_canonical_json(raw).encode("utf-8")),
                "attempt_count": 1,
                "retry_disposition": "not_applicable",
            }
        else:
            egress = _create_validated_deepseek_egress(
                outbound,
                outbound_sha256=outbound_sha,
                model=route.model,
                request_sha256=material["metadata"]["request_sha256"],
                source_authority_proof_set_sha256=material["metadata"][
                    "source_authority_proof_set_sha256"
                ],
                transport_material_sha256=transport_material_sha,
                authority_seal=_GATEWAY_EGRESS_AUTHORITY_SEAL,
            )
            http_response = DeepSeekHTTPTransport._send_validated(
                self.transport,
                egress,
            )
            raw = http_response.payload
            received_at = http_response.received_at
            response_sha = http_response.raw_response_sha256
            transport_id = http_response.transport_id
            transport_version = http_response.transport_version
            transport_metadata = http_response.to_transport_metadata()
        if not isinstance(raw, Mapping):
            raise EvidenceSchemaError("provider transport returned a non-object")
        # Accept either direct evidence JSON or a DeepSeek-compatible envelope,
        # then validate the exact evidence before producing any durable receipt.
        choices = raw.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0] if isinstance(choices[0], Mapping) else {}
            message = first.get("message") if isinstance(first, Mapping) else {}
            content = message.get("content") if isinstance(message, Mapping) else ""
            if not isinstance(content, str):
                raise EvidenceSchemaError("provider message content is not JSON text")
            parsed = json.loads(content)
            if not isinstance(parsed, Mapping):
                raise EvidenceSchemaError("provider content is not a JSON object")
            parsed_evidence = parsed
        else:
            parsed_evidence = raw
        try:
            normalized_evidence = validate_provider_evidence(
                parsed_evidence,
                allowed_refs=request.evidence_refs,
                require_bound_citation=True,
                require_complete_schema=(
                    request.prompt_version == "bull-bear-evidence.v2"
                ),
            )
        except EvidenceSchemaError as exc:
            if (
                transport_type is DeepSeekHTTPTransport
                and _rejected_attempt_receipt_sink is not None
            ):
                _rejected_attempt_receipt_sink(
                    ProviderRejectedAttemptReceipt.create(
                        provider=route.provider,
                        model=route.model,
                        transport_id=transport_id,
                        transport_version=transport_version,
                        verified_at=verified_at,
                        received_at=received_at,
                        request_sha256=material["metadata"]["request_sha256"],
                        source_authority_proof_set_sha256=material["metadata"][
                            "source_authority_proof_set_sha256"
                        ],
                        transport_material_sha256=transport_material_sha,
                        outbound_sha256=outbound_sha,
                        response_sha256=response_sha,
                        transport_metadata=transport_metadata,
                        _transport_authority_seal=(
                            _REJECTED_ATTEMPT_TRANSPORT_AUTHORITY_SEAL
                        ),
                    )
                )
            raise ProviderEvidenceBindingError("llm_evidence_schema_invalid") from exc
        _reject_secret_shaped_provider_output(
            normalized_evidence,
            path="provider_evidence",
        )
        normalized_evidence_json = json.dumps(
            normalized_evidence,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        received_at = _aware_instant(received_at, field_name="received_at")
        receipt = ProviderTransportReceipt.create(
            provider=route.provider,
            model=route.model,
            transport_id=transport_id,
            transport_version=transport_version,
            verified_at=verified_at,
            request_sha256=material["metadata"]["request_sha256"],
            source_authority_proof_set_sha256=material["metadata"][
                "source_authority_proof_set_sha256"
            ],
            transport_material_sha256=transport_material_sha,
            outbound_sha256=outbound_sha,
            response_sha256=response_sha,
            normalized_evidence_sha256=sha256_text(normalized_evidence_json),
            provider_response_id=str(raw.get("id") or "unavailable"),
            received_at=received_at,
            transport_metadata=transport_metadata,
            _transport_authority_seal=(
                _ACCEPTED_RECEIPT_TRANSPORT_AUTHORITY_SEAL
                if transport_type is DeepSeekHTTPTransport
                else None
            ),
        )
        return ProviderInvocationResult(
            evidence=normalized_evidence,
            transport_receipt=receipt,
        )


class LLMEvidenceGateway:
    def __init__(
        self,
        *,
        router: LLMRouter | None = None,
        adapters: Mapping[str, DeepSeekAdapter] | None = None,
    ) -> None:
        candidate_router = router or DeepSeekProviderConfig.from_environment().router()
        if not self._candidate_router_policy_valid(candidate_router):
            raise TypeError("llm_router_policy_rejected")
        self.router = candidate_router
        canonical_adapters: dict[str, DeepSeekAdapter] = {}
        for provider, adapter in dict(adapters or {}).items():
            if provider != "deepseek" or type(adapter) is not DeepSeekAdapter:
                raise TypeError("llm_adapter_policy_rejected")
            canonical_adapters[provider] = adapter
        self.adapters = MappingProxyType(canonical_adapters)

    @staticmethod
    def _candidate_router_policy_valid(candidate: object) -> bool:
        return type(candidate) is LLMRouter and (
            (candidate.fixture_only and not candidate.network_authorized)
            or candidate.validated_deepseek_v4
        )

    def _router_policy_valid(self) -> bool:
        return self._candidate_router_policy_valid(self.router)

    def analyze(
        self,
        request: LLMEvidenceRequest,
        *,
        entity_id: str = "",
    ) -> dict[str, Any]:
        """Compatibility API returning only the evidence observation."""

        return self._analyze(request, entity_id=entity_id)

    def analyze_with_provenance(
        self,
        request: LLMEvidenceRequest,
        *,
        entity_id: str = "",
    ) -> GatewayAnalysisResult:
        """Return mutually exclusive accepted or audit-only rejected provenance."""

        receipts: list[ProviderTransportReceipt] = []
        rejected_attempt_receipts: list[ProviderRejectedAttemptReceipt] = []
        observation = self._analyze(
            request,
            entity_id=entity_id,
            accepted_receipt_sink=receipts.append,
            rejected_attempt_receipt_sink=rejected_attempt_receipts.append,
        )
        if (
            len(receipts) > 1
            or len(rejected_attempt_receipts) > 1
            or (receipts and rejected_attempt_receipts)
        ):
            raise ProviderTransportReceiptError("gateway_receipt_cardinality_invalid")
        route = self.router.resolve(getattr(request, "route", ""))
        adapter = self.adapters.get(route.provider) if route is not None else None
        source_authority_verifier = (
            adapter.source_authority_verifier
            if type(adapter) is DeepSeekAdapter
            else None
        )
        return GatewayAnalysisResult(
            observation=MappingProxyType(dict(observation)),
            transport_receipt=receipts[0] if receipts else None,
            rejected_attempt_receipt=(
                rejected_attempt_receipts[0] if rejected_attempt_receipts else None
            ),
            _gateway_authority_seal=_GATEWAY_ANALYSIS_AUTHORITY_SEAL,
            _request=request,
            _entity_id=entity_id,
            _source_authority_verifier=source_authority_verifier,
        )

    def _analyze(
        self,
        request: LLMEvidenceRequest,
        *,
        entity_id: str = "",
        accepted_receipt_sink: (
            Callable[[ProviderTransportReceipt], None] | None
        ) = None,
        rejected_attempt_receipt_sink: (
            Callable[[ProviderRejectedAttemptReceipt], None] | None
        ) = None,
    ) -> dict[str, Any]:
        if not self._router_policy_valid():
            return invalid_observation(
                None,
                reason_code="llm_router_policy_rejected",
                entity_id="",
            )
        route = self.router.resolve(getattr(request, "route", ""))
        try:
            validate_cloud_egress(
                {
                    "request_id": request.request_id,
                    "task_type": request.task_type,
                    "route": request.route,
                    "prompt_version": request.prompt_version,
                    "prompt_sha256": request.prompt_sha256,
                    "document_cutoff": request.document_cutoff,
                    "evidence_refs": list(request.evidence_refs),
                    "entity_id": entity_id,
                },
                path="observation_metadata",
            )
        except (AttributeError, SensitivePayloadError, TypeError, ValueError):
            reason_code = "llm_observation_metadata_rejected"
            if route is not None and route.provider in self.adapters:
                reason_code = "llm_request_egress_rejected"
            return invalid_observation(
                None,
                reason_code=reason_code,
                entity_id="",
            )
        if route is None:
            return unavailable_observation(
                request,
                reason_code="llm_route_unconfigured",
                entity_id=entity_id,
            )
        try:
            validate_cloud_egress(
                {
                    "provider": route.provider,
                    "model": route.model,
                },
                path="observation_route",
            )
        except SensitivePayloadError:
            reason_code = "llm_observation_metadata_rejected"
            if route.provider in self.adapters:
                reason_code = "llm_request_egress_rejected"
            return invalid_observation(
                None,
                reason_code=reason_code,
                entity_id="",
            )
        adapter = self.adapters.get(route.provider)
        if adapter is None:
            return unavailable_observation(
                request,
                reason_code="llm_provider_unavailable",
                provider=route.provider,
                model=route.model,
                entity_id=entity_id,
            )
        if type(adapter) is not DeepSeekAdapter:
            return unavailable_observation(
                request,
                reason_code="llm_provider_call_unavailable",
                provider=route.provider,
                model=route.model,
                entity_id=entity_id,
            )
        if type(adapter.transport) is DeepSeekHTTPTransport and (
            not self.router.validated_deepseek_v4 or not self.router.network_authorized
        ):
            return unavailable_observation(
                request,
                reason_code="llm_network_router_policy_rejected",
                provider=route.provider,
                model=route.model,
                entity_id=entity_id,
            )
        try:
            # Do not use instance dispatch: a per-instance ``invoke`` override
            # would otherwise recover the full request before canonical
            # source-proof and DLP checks run.
            result = DeepSeekAdapter.invoke(
                adapter,
                request,
                route,
                _gateway_authority_seal=_ADAPTER_GATEWAY_AUTHORITY_SEAL,
                _rejected_attempt_receipt_sink=rejected_attempt_receipt_sink,
            )
        except DeepSeekHTTPTransportError as exc:
            observation_factory = (
                invalid_observation
                if exc.observation_status == "invalid"
                else unavailable_observation
            )
            return observation_factory(
                request,
                reason_code=exc.reason_code,
                provider=route.provider,
                model=route.model,
                entity_id=entity_id,
            )
        except SourceArtifactPromptInjectionError:
            return invalid_observation(
                request,
                reason_code=(
                    "llm_source_artifact_prompt_injection_detected_human_review"
                ),
                provider=route.provider,
                model=route.model,
                entity_id=entity_id,
            )
        except (
            EvidenceArtifactError,
            PromptTemplateError,
            RequestIntegrityError,
            SensitivePayloadError,
        ):
            # A rejected request can itself contain poisoned metadata. Do not
            # echo any request fields into logs/observations after this gate.
            return invalid_observation(
                None,
                reason_code="llm_request_egress_rejected",
                provider=route.provider,
                model=route.model,
                entity_id=entity_id,
            )
        except ProviderEvidenceBindingError:
            return invalid_observation(
                request,
                reason_code="llm_evidence_schema_invalid",
                provider=route.provider,
                model=route.model,
                entity_id=entity_id,
            )
        except ProviderOutputSensitiveError:
            return invalid_observation(
                request,
                reason_code="llm_provider_sensitive_output",
                provider=route.provider,
                model=route.model,
                entity_id=entity_id,
            )
        except (EvidenceSchemaError, json.JSONDecodeError, TypeError, ValueError):
            return invalid_observation(
                request,
                reason_code="llm_provider_invalid_output",
                provider=route.provider,
                model=route.model,
                entity_id=entity_id,
            )
        except Exception:
            return unavailable_observation(
                request,
                reason_code="llm_provider_call_unavailable",
                provider=route.provider,
                model=route.model,
                entity_id=entity_id,
            )
        try:
            observation = available_observation(
                request,
                provider=route.provider,
                model=route.model,
                raw_evidence=result.evidence,
                entity_id=entity_id,
            )
        except EvidenceSchemaError:
            receipt = result.transport_receipt
            if (
                rejected_attempt_receipt_sink is not None
                and receipt.transport_metadata["kind"] == "https"
            ):
                rejected_attempt_receipt_sink(
                    ProviderRejectedAttemptReceipt.create(
                        provider=receipt.provider,
                        model=receipt.model,
                        transport_id=receipt.transport_id,
                        transport_version=receipt.transport_version,
                        rejection_stage="gateway_observation_binding",
                        verified_at=receipt.verified_at,
                        received_at=receipt.received_at,
                        request_sha256=receipt.request_sha256,
                        source_authority_proof_set_sha256=(
                            receipt.source_authority_proof_set_sha256
                        ),
                        transport_material_sha256=(receipt.transport_material_sha256),
                        outbound_sha256=receipt.outbound_sha256,
                        response_sha256=receipt.response_sha256,
                        transport_metadata=receipt.transport_metadata,
                        _transport_authority_seal=(
                            _REJECTED_ATTEMPT_TRANSPORT_AUTHORITY_SEAL
                        ),
                    )
                )
            return invalid_observation(
                request,
                reason_code="llm_evidence_schema_invalid",
                provider=route.provider,
                model=route.model,
                entity_id=entity_id,
            )
        if accepted_receipt_sink is not None:
            accepted_receipt_sink(result.transport_receipt)
        return observation
