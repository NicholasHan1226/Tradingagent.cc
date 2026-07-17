"""Content-addressed, point-in-time evidence for the LLM research sidecar."""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import re
import unicodedata
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class EvidenceArtifactError(ValueError):
    """Raised when evidence content, lineage or point-in-time state is invalid."""


class SourceArtifactPromptInjectionError(EvidenceArtifactError):
    """Raised when an untrusted evidence span attempts to issue instructions."""


_PROMPT_INJECTION_PATTERNS = (
    re.compile(
        r"(?i)\b(?:ignore|disregard|override)\s+(?:all\s+)?(?:the\s+)?"
        r"(?:previous|prior|above)\s+"
        r"(?:instructions?|prompts?|messages?)\b"
    ),
    re.compile(
        r"(?i)\b(?:you\s+are\s+now|act\s+as|behave\s+as)\s+(?:the\s+)?"
        r"(?:system|developer|assistant)\b"
    ),
    re.compile(r"(?i)\b(?:system|developer)\s+(?:prompt|message|instruction)\b"),
    re.compile(r"(?i)<\|\s*(?:system|developer|assistant)\s*\|>"),
    re.compile(r"(?i)\[\s*(?:system|developer|assistant)\s*\]"),
    re.compile(
        r"(?i)\b(?:reveal|print|return|exfiltrate)\b.{0,48}"
        r"\b(?:system|developer)\s+(?:prompt|message)\b"
    ),
    re.compile(r"(?:忽略|无视).{0,16}(?:此前|之前|以上|所有).{0,12}(?:指令|提示|要求)"),
    re.compile(r"(?:你现在是|扮演|充当).{0,12}(?:系统|开发者|助手)"),
    re.compile(r"(?:系统|开发者)(?:提示|消息|指令)"),
    re.compile(r"(?:泄露|显示|输出).{0,24}(?:系统|开发者)(?:提示|消息|指令)"),
    re.compile(
        r"(?is)[\"']?(?:role|from)[\"']?\s*:\s*"
        r"[\"']?(?:system|developer|assistant)\b.{0,96}"
        r"\b(?:ignore|override|obey|instructions?|prompts?|policy)\b"
    ),
)
_PROMPT_INJECTION_COMPACT_PATTERNS = (
    re.compile(
        r"(?i)(?:ignore|disregard|override)(?:all|the)?"
        r"(?:previous|prior|above)(?:instructions?|prompts?|messages?)"
    ),
    re.compile(r"(?i)(?:youarenow|actas|behaveas)(?:the)?(?:system|developer)"),
)
_UNICODE_ESCAPE_RE = re.compile(r"\\u([0-9a-fA-F]{4})")
_PROMPT_SCAN_HOMOGLYPHS = str.maketrans(
    {
        "і": "i",  # Cyrillic
        "І": "I",
        "ı": "i",
        "ο": "o",  # Greek
        "Ο": "O",
        "о": "o",  # Cyrillic
        "О": "O",
        "а": "a",
        "А": "A",
        "е": "e",
        "Е": "E",
        "с": "c",
        "С": "C",
        "р": "p",
        "Р": "P",
        "х": "x",
        "Х": "X",
    }
)


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
        raise EvidenceArtifactError("artifact_not_canonical_json") from exc


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_document(value: str) -> str:
    """Return the content hash used by source-authority receipts."""

    return _sha256_text(_strict_text(value, field_name="document_text"))


def source_prompt_injection_signals(value: str) -> tuple[str, ...]:
    """Return stable signal codes without copying poisoned text downstream."""

    text = _normalise_prompt_scan_text(_strict_text(value, field_name="source_span"))
    pattern_signals = tuple(
        f"source_prompt_injection_pattern_{index + 1}"
        for index, pattern in enumerate(_PROMPT_INJECTION_PATTERNS)
        if pattern.search(text)
    )
    compact = "".join(character for character in text.casefold() if character.isalnum())
    compact_signals = tuple(
        f"source_prompt_injection_compact_pattern_{index + 1}"
        for index, pattern in enumerate(_PROMPT_INJECTION_COMPACT_PATTERNS)
        if pattern.search(compact)
    )
    return pattern_signals + compact_signals


def _normalise_prompt_scan_text(value: str) -> str:
    """Decode common visual/transport obfuscation only for safety scanning."""

    text = value
    for _ in range(3):
        text = html.unescape(urllib.parse.unquote_plus(text))
        text = _UNICODE_ESCAPE_RE.sub(
            lambda match: chr(int(match.group(1), 16)),
            text,
        )
    text = unicodedata.normalize("NFKD", text).translate(_PROMPT_SCAN_HOMOGLYPHS)
    return "".join(
        character
        for character in text
        if unicodedata.category(character) not in {"Cf", "Cc", "Mn", "Me"}
        or character in {"\n", "\r", "\t"}
    )


def _strict_text(value: object, *, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\x00" in value
    ):
        raise EvidenceArtifactError(f"{field_name}_invalid")
    return value


def _aware_instant(value: object, *, field_name: str) -> tuple[datetime, str]:
    if not isinstance(value, str) or not value or value != value.strip():
        raise EvidenceArtifactError(f"{field_name}_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvidenceArtifactError(f"{field_name}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EvidenceArtifactError(f"{field_name}_timezone_required")
    return parsed, parsed.isoformat()


@dataclass(frozen=True)
class EvidenceSourceAuthorityReceipt:
    """Recorded source assertion that still requires an external verifier.

    The receipt hash only protects the recorded assertion from mutation.  It is
    deliberately not sufficient to establish source authority by itself.
    """

    receipt_id: str
    source_system: str
    source_document_id: str
    document_sha256: str
    available_at: str
    issued_at: str
    receipt_sha256: str

    @classmethod
    def create(
        cls,
        *,
        receipt_id: str,
        source_system: str,
        source_document_id: str,
        document_sha256: str,
        available_at: str,
        issued_at: str,
    ) -> "EvidenceSourceAuthorityReceipt":
        receipt_identity = {
            "receipt_id": _strict_text(receipt_id, field_name="receipt_id"),
            "source_system": _strict_text(
                source_system,
                field_name="source_system",
            ),
            "source_document_id": _strict_text(
                source_document_id,
                field_name="source_document_id",
            ),
            "document_sha256": str(document_sha256),
            "available_at": _aware_instant(
                available_at,
                field_name="source_receipt_available_at",
            )[1],
            "issued_at": _aware_instant(
                issued_at,
                field_name="source_receipt_issued_at",
            )[1],
        }
        receipt = cls(
            **receipt_identity,
            receipt_sha256=_sha256_text(_canonical_json(receipt_identity)),
        )
        receipt.verify_integrity()
        return receipt

    def _identity_payload(self) -> dict[str, str]:
        return {
            "receipt_id": self.receipt_id,
            "source_system": self.source_system,
            "source_document_id": self.source_document_id,
            "document_sha256": self.document_sha256,
            "available_at": self.available_at,
            "issued_at": self.issued_at,
        }

    def verify_integrity(self) -> None:
        _strict_text(self.receipt_id, field_name="receipt_id")
        _strict_text(self.source_system, field_name="source_system")
        _strict_text(self.source_document_id, field_name="source_document_id")
        if not _SHA256_RE.fullmatch(str(self.document_sha256)):
            raise EvidenceArtifactError("source_receipt_document_sha256_invalid")
        available, available_text = _aware_instant(
            self.available_at,
            field_name="source_receipt_available_at",
        )
        issued, issued_text = _aware_instant(
            self.issued_at,
            field_name="source_receipt_issued_at",
        )
        if self.available_at != available_text or self.issued_at != issued_text:
            raise EvidenceArtifactError("source_receipt_time_not_canonical")
        if issued < available:
            raise EvidenceArtifactError("source_receipt_time_order_invalid")
        expected = _sha256_text(_canonical_json(self._identity_payload()))
        if not _SHA256_RE.fullmatch(
            str(self.receipt_sha256)
        ) or not hmac.compare_digest(self.receipt_sha256, expected):
            raise EvidenceArtifactError("source_receipt_sha256_mismatch")

    def to_descriptor(self) -> dict[str, str]:
        self.verify_integrity()
        return {**self._identity_payload(), "receipt_sha256": self.receipt_sha256}


class EvidenceSourceAuthorityVerifier(Protocol):
    def verify(
        self,
        *,
        artifact: "EvidenceArtifact",
        receipt: EvidenceSourceAuthorityReceipt,
    ) -> bool: ...


@dataclass(frozen=True)
class EvidenceSourceAuthorityProof:
    """Content-addressed proof that an injected verifier accepted a receipt."""

    artifact_id: str
    artifact_sha256: str
    receipt_id: str
    receipt_sha256: str
    verifier_id: str
    verifier_version: str
    verified_at: str
    proof_sha256: str

    @classmethod
    def create(
        cls,
        *,
        artifact_id: str,
        artifact_sha256: str,
        receipt_id: str,
        receipt_sha256: str,
        verifier_id: str,
        verifier_version: str,
        verified_at: str,
    ) -> "EvidenceSourceAuthorityProof":
        identity = {
            "artifact_id": _strict_text(artifact_id, field_name="artifact_id"),
            "artifact_sha256": str(artifact_sha256),
            "receipt_id": _strict_text(receipt_id, field_name="receipt_id"),
            "receipt_sha256": str(receipt_sha256),
            "verifier_id": _strict_text(verifier_id, field_name="verifier_id"),
            "verifier_version": _strict_text(
                verifier_version,
                field_name="verifier_version",
            ),
            "verified_at": _aware_instant(
                verified_at,
                field_name="verified_at",
            )[1],
        }
        proof = cls(
            **identity,
            proof_sha256=_sha256_text(_canonical_json(identity)),
        )
        proof.verify_integrity()
        return proof

    def _identity_payload(self) -> dict[str, str]:
        return {
            "artifact_id": self.artifact_id,
            "artifact_sha256": self.artifact_sha256,
            "receipt_id": self.receipt_id,
            "receipt_sha256": self.receipt_sha256,
            "verifier_id": self.verifier_id,
            "verifier_version": self.verifier_version,
            "verified_at": self.verified_at,
        }

    def verify_integrity(self) -> None:
        _strict_text(self.artifact_id, field_name="artifact_id")
        if not _SHA256_RE.fullmatch(str(self.artifact_sha256)):
            raise EvidenceArtifactError("proof_artifact_sha256_invalid")
        _strict_text(self.receipt_id, field_name="receipt_id")
        if not _SHA256_RE.fullmatch(str(self.receipt_sha256)):
            raise EvidenceArtifactError("proof_receipt_sha256_invalid")
        _strict_text(self.verifier_id, field_name="verifier_id")
        _strict_text(self.verifier_version, field_name="verifier_version")
        _, canonical_time = _aware_instant(
            self.verified_at,
            field_name="verified_at",
        )
        if self.verified_at != canonical_time:
            raise EvidenceArtifactError("proof_verified_at_not_canonical")
        expected = _sha256_text(_canonical_json(self._identity_payload()))
        if not _SHA256_RE.fullmatch(str(self.proof_sha256)) or not hmac.compare_digest(
            self.proof_sha256, expected
        ):
            raise EvidenceArtifactError("source_authority_proof_sha256_mismatch")

    def to_descriptor(self) -> dict[str, str]:
        self.verify_integrity()
        return {**self._identity_payload(), "proof_sha256": self.proof_sha256}


def _verifier_identity(verifier: object, callback: object) -> tuple[str, str]:
    verifier_id = getattr(verifier, "verifier_id", None)
    verifier_version = getattr(verifier, "verifier_version", None)
    if not verifier_id:
        module = getattr(callback, "__module__", "unknown")
        qualname = getattr(callback, "__qualname__", type(verifier).__qualname__)
        verifier_id = f"{module}.{qualname}"
    if not verifier_version:
        verifier_version = "unversioned-callable"
    return (
        _strict_text(verifier_id, field_name="verifier_id"),
        _strict_text(verifier_version, field_name="verifier_version"),
    )


@dataclass(frozen=True)
class EvidenceArtifact:
    """Immutable evidence with local integrity and separate source authority."""

    artifact_id: str
    document_text: str
    document_sha256: str
    published_at: str
    available_at: str
    span_start: int
    span_end: int
    source_span: str
    span_sha256: str
    entity_resolution_version: str
    verification_status: str
    source_authority_receipt: EvidenceSourceAuthorityReceipt | None
    artifact_sha256: str

    @classmethod
    def create(
        cls,
        *,
        document_text: str,
        published_at: str,
        available_at: str,
        span_start: int,
        span_end: int,
        entity_resolution_version: str,
        source_authority_receipt: EvidenceSourceAuthorityReceipt | None = None,
    ) -> "EvidenceArtifact":
        document = _strict_text(document_text, field_name="document_text")
        published, published_text = _aware_instant(
            published_at,
            field_name="published_at",
        )
        available, available_text = _aware_instant(
            available_at,
            field_name="available_at",
        )
        if published > available:
            raise EvidenceArtifactError("artifact_time_order_invalid")
        if (
            isinstance(span_start, bool)
            or not isinstance(span_start, int)
            or isinstance(span_end, bool)
            or not isinstance(span_end, int)
            or span_start < 0
            or span_end <= span_start
            or span_end > len(document)
        ):
            raise EvidenceArtifactError("source_span_bounds_invalid")
        source_span = document[span_start:span_end]
        if not source_span:
            raise EvidenceArtifactError("source_span_invalid")
        entity_version = _strict_text(
            entity_resolution_version,
            field_name="entity_resolution_version",
        )
        document_sha = _sha256_text(document)
        span_sha = _sha256_text(source_span)
        if source_authority_receipt is not None:
            if not isinstance(
                source_authority_receipt,
                EvidenceSourceAuthorityReceipt,
            ):
                raise EvidenceArtifactError("source_authority_receipt_type_invalid")
            source_authority_receipt.verify_integrity()
        identity = {
            "document_sha256": document_sha,
            "published_at": published_text,
            "available_at": available_text,
            "span_start": span_start,
            "span_end": span_end,
            "span_sha256": span_sha,
            "entity_resolution_version": entity_version,
            "verification_status": "integrity_verified",
            "source_authority_receipt": (
                source_authority_receipt.to_descriptor()
                if source_authority_receipt is not None
                else None
            ),
        }
        artifact_sha = _sha256_text(_canonical_json(identity))
        artifact = cls(
            artifact_id=f"evidence:{artifact_sha}",
            document_text=document,
            document_sha256=document_sha,
            published_at=published_text,
            available_at=available_text,
            span_start=span_start,
            span_end=span_end,
            source_span=source_span,
            span_sha256=span_sha,
            entity_resolution_version=entity_version,
            verification_status="integrity_verified",
            source_authority_receipt=source_authority_receipt,
            artifact_sha256=artifact_sha,
        )
        artifact.verify()
        return artifact

    def _identity_payload(self) -> dict[str, Any]:
        return {
            "document_sha256": self.document_sha256,
            "published_at": self.published_at,
            "available_at": self.available_at,
            "span_start": self.span_start,
            "span_end": self.span_end,
            "span_sha256": self.span_sha256,
            "entity_resolution_version": self.entity_resolution_version,
            "verification_status": self.verification_status,
            "source_authority_receipt": (
                self.source_authority_receipt.to_descriptor()
                if self.source_authority_receipt is not None
                else None
            ),
        }

    def verify(self, *, document_cutoff: str | None = None) -> None:
        document = _strict_text(self.document_text, field_name="document_text")
        if not _SHA256_RE.fullmatch(
            str(self.document_sha256)
        ) or not hmac.compare_digest(
            self.document_sha256,
            _sha256_text(document),
        ):
            raise EvidenceArtifactError("document_sha256_mismatch")
        published, published_text = _aware_instant(
            self.published_at,
            field_name="published_at",
        )
        available, available_text = _aware_instant(
            self.available_at,
            field_name="available_at",
        )
        if self.published_at != published_text or self.available_at != available_text:
            raise EvidenceArtifactError("artifact_time_not_canonical")
        if published > available:
            raise EvidenceArtifactError("artifact_time_order_invalid")
        if document_cutoff is not None:
            cutoff, cutoff_text = _aware_instant(
                document_cutoff,
                field_name="document_cutoff",
            )
            if document_cutoff != cutoff_text:
                raise EvidenceArtifactError("document_cutoff_not_canonical")
            if available > cutoff:
                raise EvidenceArtifactError("artifact_after_document_cutoff")
        if (
            isinstance(self.span_start, bool)
            or not isinstance(self.span_start, int)
            or isinstance(self.span_end, bool)
            or not isinstance(self.span_end, int)
            or self.span_start < 0
            or self.span_end <= self.span_start
            or self.span_end > len(document)
        ):
            raise EvidenceArtifactError("source_span_bounds_invalid")
        expected_span = document[self.span_start : self.span_end]
        if self.source_span != expected_span:
            raise EvidenceArtifactError("source_span_content_mismatch")
        if not _SHA256_RE.fullmatch(str(self.span_sha256)) or not hmac.compare_digest(
            self.span_sha256,
            _sha256_text(expected_span),
        ):
            raise EvidenceArtifactError("span_sha256_mismatch")
        _strict_text(
            self.entity_resolution_version,
            field_name="entity_resolution_version",
        )
        if self.verification_status != "integrity_verified":
            raise EvidenceArtifactError("artifact_integrity_not_verified")
        if self.source_authority_receipt is not None:
            if not isinstance(
                self.source_authority_receipt,
                EvidenceSourceAuthorityReceipt,
            ):
                raise EvidenceArtifactError("source_authority_receipt_type_invalid")
            self.source_authority_receipt.verify_integrity()
        expected_artifact_sha = _sha256_text(_canonical_json(self._identity_payload()))
        if not _SHA256_RE.fullmatch(
            str(self.artifact_sha256)
        ) or not hmac.compare_digest(
            self.artifact_sha256,
            expected_artifact_sha,
        ):
            raise EvidenceArtifactError("artifact_sha256_mismatch")
        if self.artifact_id != f"evidence:{expected_artifact_sha}":
            raise EvidenceArtifactError("artifact_id_mismatch")

    def verify_source_authority(
        self,
        verifier: EvidenceSourceAuthorityVerifier | Any | None,
        *,
        document_cutoff: str | None = None,
        verified_at: str | None = None,
    ) -> EvidenceSourceAuthorityProof:
        """Require a recorded receipt and an injected external verifier."""

        self.verify(document_cutoff=document_cutoff)
        receipt = self.source_authority_receipt
        if receipt is None:
            raise EvidenceArtifactError("external_source_authority_receipt_required")
        receipt.verify_integrity()
        if not hmac.compare_digest(receipt.document_sha256, self.document_sha256):
            raise EvidenceArtifactError("external_source_document_sha256_mismatch")
        if receipt.available_at != self.available_at:
            raise EvidenceArtifactError("external_source_available_at_mismatch")
        if document_cutoff is not None:
            cutoff, _ = _aware_instant(
                document_cutoff,
                field_name="document_cutoff",
            )
            issued, _ = _aware_instant(
                receipt.issued_at,
                field_name="source_receipt_issued_at",
            )
            if issued > cutoff:
                raise EvidenceArtifactError(
                    "external_source_receipt_after_document_cutoff"
                )
        if verifier is None:
            raise EvidenceArtifactError("external_source_authority_verifier_required")
        callback = getattr(verifier, "verify", None)
        if callback is None and callable(verifier):
            callback = verifier
        if callback is None:
            raise EvidenceArtifactError("external_source_authority_verifier_invalid")
        try:
            accepted = callback(artifact=self, receipt=receipt)
        except Exception as exc:
            raise EvidenceArtifactError("external_source_authority_rejected") from exc
        if accepted is not True:
            raise EvidenceArtifactError("external_source_authority_rejected")
        verifier_id, verifier_version = _verifier_identity(verifier, callback)
        proof_time = verified_at or datetime.now(timezone.utc).isoformat()
        verified_instant, proof_time = _aware_instant(
            proof_time,
            field_name="verified_at",
        )
        receipt_issued, _ = _aware_instant(
            receipt.issued_at,
            field_name="source_receipt_issued_at",
        )
        if verified_instant < receipt_issued:
            raise EvidenceArtifactError("source_authority_verified_before_receipt")
        return EvidenceSourceAuthorityProof.create(
            artifact_id=self.artifact_id,
            artifact_sha256=self.artifact_sha256,
            receipt_id=receipt.receipt_id,
            receipt_sha256=receipt.receipt_sha256,
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            verified_at=proof_time,
        )

    def assert_source_span_is_untrusted_data(self) -> None:
        """Fail closed before LLM transport when a source span issues commands."""

        self.verify()
        if source_prompt_injection_signals(self.source_span):
            raise SourceArtifactPromptInjectionError(
                "source_artifact_prompt_injection_detected"
            )

    def to_request_descriptor(self) -> dict[str, Any]:
        self.verify()
        return {
            "artifact_id": self.artifact_id,
            "document_sha256": self.document_sha256,
            "published_at": self.published_at,
            "available_at": self.available_at,
            "source_span": {
                "start": self.span_start,
                "end": self.span_end,
                "text": self.source_span,
                "span_sha256": self.span_sha256,
            },
            "entity_resolution_version": self.entity_resolution_version,
            "verification_status": self.verification_status,
            "source_authority_receipt": (
                self.source_authority_receipt.to_descriptor()
                if self.source_authority_receipt is not None
                else None
            ),
            "source_authority_status": "external_verification_required",
            "artifact_sha256": self.artifact_sha256,
        }


__all__ = [
    "EvidenceArtifact",
    "EvidenceArtifactError",
    "EvidenceSourceAuthorityProof",
    "EvidenceSourceAuthorityReceipt",
    "EvidenceSourceAuthorityVerifier",
    "SourceArtifactPromptInjectionError",
    "sha256_document",
    "source_prompt_injection_signals",
]
