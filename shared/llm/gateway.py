"""Provider-neutral, transport-injected LLM evidence gateway.

No HTTP client is created here.  A caller must explicitly inject a transport,
which keeps the deterministic/paper path network-free by default.
"""

from __future__ import annotations

import json
import hashlib
import hmac
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Callable, Mapping

from .evidence_artifact import (
    EvidenceArtifactError,
    EvidenceSourceAuthorityVerifier,
    SourceArtifactPromptInjectionError,
)
from .deepseek_config import DeepSeekProviderConfig
from .router import LLMRouter, ModelRoute
from .schema import (
    EvidenceSchemaError,
    LLMEvidenceRequest,
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


class ProviderTransportReceiptError(ValueError):
    """Raised when provider transport provenance is malformed or mutated."""


class ProviderEvidenceBindingError(EvidenceSchemaError):
    """Raised after transport when evidence cannot be bound to the request."""


class ProviderOutputSensitiveError(EvidenceSchemaError):
    """Raised when provider evidence contains credential-shaped output."""


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
    receipt_sha256: str

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
    ) -> "ProviderTransportReceipt":
        identity = {
            "provider": _strict_text(provider, field_name="provider"),
            "model": _strict_text(model, field_name="model"),
            "transport_id": _strict_text(transport_id, field_name="transport_id"),
            "transport_version": _strict_text(
                transport_version,
                field_name="transport_version",
            ),
            "verified_at": _aware_instant(verified_at, field_name="verified_at"),
            "request_sha256": str(request_sha256),
            "source_authority_proof_set_sha256": str(source_authority_proof_set_sha256),
            "transport_material_sha256": str(transport_material_sha256),
            "outbound_sha256": str(outbound_sha256),
            "response_sha256": str(response_sha256),
            "normalized_evidence_sha256": str(normalized_evidence_sha256),
            "provider_response_id": _provider_response_id(provider_response_id),
            "received_at": _aware_instant(received_at, field_name="received_at"),
        }
        receipt = cls(
            **identity,
            receipt_sha256=_sha256_json(identity),
        )
        receipt.verify_integrity()
        return receipt

    def _identity_payload(self) -> dict[str, str]:
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
        }

    def verify_integrity(self) -> None:
        _strict_text(self.provider, field_name="provider")
        _strict_text(self.model, field_name="model")
        _strict_text(self.transport_id, field_name="transport_id")
        _strict_text(self.transport_version, field_name="transport_version")
        _provider_response_id(self.provider_response_id)
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

    def to_descriptor(self) -> dict[str, str]:
        self.verify_integrity()
        return {**self._identity_payload(), "receipt_sha256": self.receipt_sha256}


@dataclass(frozen=True)
class ProviderInvocationResult:
    evidence: Mapping[str, Any]
    transport_receipt: ProviderTransportReceipt


@dataclass(frozen=True)
class GatewayAnalysisResult:
    """Safe observation plus provenance exposed only after schema validation."""

    observation: Mapping[str, Any]
    transport_receipt: ProviderTransportReceipt | None


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
    """DeepSeek-compatible adapter limited to an offline fixture transport."""

    transport: OfflineDeepSeekFixtureTransport | None = field(
        default=None,
        repr=False,
    )
    source_authority_verifier: EvidenceSourceAuthorityVerifier | Any | None = field(
        default=None,
        repr=False,
    )
    clock: Callable[[], str] | None = field(default=None, repr=False)
    transport_id: str = "offline-deepseek-fixture"
    transport_version: str = "offline-fixture-v1"
    receipt_sink: Callable[[ProviderTransportReceipt], None] | None = field(
        default=None,
        repr=False,
    )

    def invoke(
        self, request: LLMEvidenceRequest, route: ModelRoute
    ) -> ProviderInvocationResult:
        if self.transport is None:
            raise RuntimeError("deepseek_transport_unavailable")
        if type(self.transport) is not OfflineDeepSeekFixtureTransport:
            raise RuntimeError("deepseek_transport_policy_rejected")
        # Re-check immediately before transport. This binds the fixed prompt,
        # verified point-in-time evidence and structured payload to the model.
        # It also catches mutations inside an otherwise frozen request object.
        verified_at = (
            self.clock()
            if self.clock is not None
            else datetime.now(timezone.utc).isoformat()
        )
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
        raw = OfflineDeepSeekFixtureTransport.resolve(
            self.transport,
            request_sha256=material["metadata"]["request_sha256"],
            outbound_sha256=outbound_sha,
        )
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
            )
        except EvidenceSchemaError as exc:
            raise ProviderEvidenceBindingError(str(exc)) from exc
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
        received_at = (
            self.clock()
            if self.clock is not None
            else datetime.now(timezone.utc).isoformat()
        )
        received_at = _aware_instant(received_at, field_name="received_at")
        response_sha = _sha256_json(raw)
        receipt = ProviderTransportReceipt.create(
            provider=route.provider,
            model=route.model,
            transport_id=self.transport_id,
            transport_version=self.transport_version,
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
        )
        if self.receipt_sink is not None:
            self.receipt_sink(receipt)

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
            candidate.fixture_only or candidate.validated_deepseek_v4
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
        """Return a typed receipt only for a successfully validated observation."""

        receipts: list[ProviderTransportReceipt] = []
        observation = self._analyze(
            request,
            entity_id=entity_id,
            accepted_receipt_sink=receipts.append,
        )
        return GatewayAnalysisResult(
            observation=MappingProxyType(dict(observation)),
            transport_receipt=receipts[0] if receipts else None,
        )

    def _analyze(
        self,
        request: LLMEvidenceRequest,
        *,
        entity_id: str = "",
        accepted_receipt_sink: (
            Callable[[ProviderTransportReceipt], None] | None
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
        try:
            # Do not use instance dispatch: a per-instance ``invoke`` override
            # would otherwise recover the full request before canonical
            # source-proof and DLP checks run.
            result = DeepSeekAdapter.invoke(adapter, request, route)
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
