"""Strict HTTPS transport for evidence-only DeepSeek calls.

The transport accepts only the canonical provider envelope already validated
by ``DeepSeekAdapter``.  It never receives a trading account, portfolio, risk
object, order, or the full evidence request.  Network access is disabled by
default and cannot be enabled by ambient environment variables.
"""

from __future__ import annotations

import errno
import hashlib
import hmac
import json
import os
import re
import socket
import ssl
import stat
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping


OFFICIAL_DEEPSEEK_CHAT_COMPLETIONS_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_HTTP_TRANSPORT_ID = "deepseek-official-https"
DEEPSEEK_HTTP_TRANSPORT_VERSION = "deepseek-http-v1"
DEEPSEEK_EGRESS_POLICY_VERSION = "deepseek-egress-policy-v1"

_KEY_RE = re.compile(r"^sk-[A-Za-z0-9_-]{16,256}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_READ_CHUNK_BYTES = 64 * 1024
_MAX_CREDENTIAL_BYTES = 512
_MAX_JSON_NODES = 20_000
_TLS_ENVIRONMENT_OVERRIDES = (
    "CURL_CA_BUNDLE",
    "REQUESTS_CA_BUNDLE",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "SSLKEYLOGFILE",
)
_APPROVED_DEEPSEEK_MODELS = frozenset(
    {
        "deepseek-v4-flash",
        "deepseek-v4-pro",
    }
)
_GATEWAY_EGRESS_AUTHORITY_SEAL = object()
_VALIDATED_EGRESS_CAPABILITY_SEAL = object()
_EGRESS_CAPABILITY_HMAC_KEY = os.urandom(32)


class DeepSeekHTTPTransportError(RuntimeError):
    """Stable, redacted provider error suitable for a reason code."""

    __slots__ = ("reason_code", "observation_status")

    def __init__(
        self,
        reason_code: str,
        *,
        observation_status: str = "unavailable",
    ) -> None:
        self.reason_code = str(reason_code)
        self.observation_status = str(observation_status)
        super().__init__(self.reason_code)

    def __repr__(self) -> str:
        return f"DeepSeekHTTPTransportError({self.reason_code!r})"


def _transport_error(
    reason_code: str,
    *,
    invalid_response: bool = False,
) -> DeepSeekHTTPTransportError:
    return DeepSeekHTTPTransportError(
        reason_code,
        observation_status="invalid" if invalid_response else "unavailable",
    )


def _open_secret_from_trusted_directory(path: Path) -> int:
    """Open ``path`` relative to verified directory descriptors only."""

    directory_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    directory_flags |= getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        directory_descriptor = os.open(path.anchor, directory_flags)
    except OSError:
        raise _transport_error("deepseek_credential_parent_untrusted") from None

    try:
        for component in path.parts[1:-1]:
            try:
                next_descriptor = os.open(
                    component,
                    directory_flags,
                    dir_fd=directory_descriptor,
                )
            except OSError:
                raise _transport_error("deepseek_credential_parent_untrusted") from None
            os.close(directory_descriptor)
            directory_descriptor = next_descriptor
            metadata = os.fstat(directory_descriptor)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid not in {0, os.geteuid()}
                or stat.S_IMODE(metadata.st_mode) & 0o022
            ):
                raise _transport_error("deepseek_credential_parent_untrusted")

        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            return os.open(path.name, flags, dir_fd=directory_descriptor)
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.EMLINK}:
                raise _transport_error(
                    "deepseek_credential_symlink_forbidden"
                ) from None
            if exc.errno == errno.ENOENT:
                raise _transport_error("deepseek_credential_missing") from None
            raise _transport_error("deepseek_credential_unavailable") from None
    finally:
        os.close(directory_descriptor)


@dataclass(frozen=True)
class DeepSeekCredentialFile:
    """Explicit raw-secret file read only at the final transport boundary."""

    path: Path | str = field(repr=False)

    def __post_init__(self) -> None:
        candidate = Path(self.path).expanduser()
        if not candidate.is_absolute() or not candidate.name:
            raise _transport_error("deepseek_credential_path_invalid")
        object.__setattr__(self, "path", candidate)

    def read_secret(self) -> str:
        descriptor = _open_secret_from_trusted_directory(self.path)

        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise _transport_error("deepseek_credential_regular_file_required")
            if metadata.st_uid != os.geteuid():
                raise _transport_error("deepseek_credential_owner_invalid")
            if stat.S_IMODE(metadata.st_mode) not in {0o400, 0o600}:
                raise _transport_error("deepseek_credential_mode_invalid")
            if metadata.st_size <= 0 or metadata.st_size > _MAX_CREDENTIAL_BYTES:
                raise _transport_error("deepseek_credential_size_invalid")
            chunks: list[bytes] = []
            remaining = _MAX_CREDENTIAL_BYTES + 1
            while remaining > 0:
                chunk = os.read(descriptor, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
        finally:
            os.close(descriptor)

        if len(raw) > _MAX_CREDENTIAL_BYTES:
            raise _transport_error("deepseek_credential_size_invalid")
        try:
            secret = raw.decode("ascii")
        except UnicodeDecodeError:
            raise _transport_error("deepseek_credential_format_invalid") from None
        if any(character in secret for character in ("\n", "\r", "\x00", "=")):
            raise _transport_error("deepseek_credential_raw_required")
        if not _KEY_RE.fullmatch(secret):
            raise _transport_error("deepseek_credential_format_invalid")
        return secret


@dataclass(frozen=True)
class DeepSeekHTTPTransportConfig:
    """Secret-redacted, explicit network policy for one transport instance."""

    network_enabled: bool = False
    credential: DeepSeekCredentialFile | None = field(default=None, repr=False)
    endpoint: str = OFFICIAL_DEEPSEEK_CHAT_COMPLETIONS_URL
    timeout_seconds: float = 120.0
    max_request_bytes: int = 256 * 1024
    max_response_bytes: int = 1024 * 1024
    max_json_depth: int = 32

    def __post_init__(self) -> None:
        if type(self.network_enabled) is not bool:
            raise _transport_error("deepseek_http_network_enabled_invalid")
        if self.endpoint != OFFICIAL_DEEPSEEK_CHAT_COMPLETIONS_URL:
            raise _transport_error("deepseek_http_endpoint_invalid")
        if (
            self.credential is not None
            and type(self.credential) is not DeepSeekCredentialFile
        ):
            raise _transport_error("deepseek_credential_policy_rejected")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not 0 < float(self.timeout_seconds) <= 300
        ):
            raise _transport_error("deepseek_http_timeout_invalid")
        for field_name, value, upper_bound in (
            ("max_request_bytes", self.max_request_bytes, 1024 * 1024),
            ("max_response_bytes", self.max_response_bytes, 2 * 1024 * 1024),
            ("max_json_depth", self.max_json_depth, 64),
        ):
            if type(value) is not int or value <= 0 or value > upper_bound:
                raise _transport_error(f"deepseek_http_{field_name}_invalid")

    def to_constructor_values(self) -> dict[str, object]:
        return {
            "network_enabled": self.network_enabled,
            "credential": self.credential,
            "endpoint": self.endpoint,
            "timeout_seconds": self.timeout_seconds,
            "max_request_bytes": self.max_request_bytes,
            "max_response_bytes": self.max_response_bytes,
            "max_json_depth": self.max_json_depth,
        }

    def to_public_descriptor(self) -> dict[str, object]:
        return {
            "network_enabled": self.network_enabled,
            "endpoint": self.endpoint,
            "timeout_seconds": float(self.timeout_seconds),
            "max_request_bytes": self.max_request_bytes,
            "max_response_bytes": self.max_response_bytes,
            "max_json_depth": self.max_json_depth,
            "max_attempts": 1,
            "proxy_policy": "disabled",
            "redirect_policy": "reject",
            "tls_policy": "system_ca_hostname_verified",
            "transport_id": DEEPSEEK_HTTP_TRANSPORT_ID,
            "transport_version": DEEPSEEK_HTTP_TRANSPORT_VERSION,
            "egress_policy_version": DEEPSEEK_EGRESS_POLICY_VERSION,
        }


@dataclass(frozen=True)
class DeepSeekHTTPResponse:
    """Safe provider result plus receipt-ready network facts."""

    payload: Mapping[str, Any] = field(repr=False)
    raw_response_sha256: str
    received_at: str
    request_bytes: int
    response_bytes: int
    content_type: str
    http_status: int = 200
    attempt_count: int = 1
    retry_disposition: str = "not_retried"
    transport_id: str = DEEPSEEK_HTTP_TRANSPORT_ID
    transport_version: str = DEEPSEEK_HTTP_TRANSPORT_VERSION
    egress_policy_version: str = DEEPSEEK_EGRESS_POLICY_VERSION

    def to_transport_metadata(self) -> dict[str, object]:
        return {
            "kind": "https",
            "endpoint": OFFICIAL_DEEPSEEK_CHAT_COMPLETIONS_URL,
            "method": "POST",
            "egress_policy_version": self.egress_policy_version,
            "http_status": self.http_status,
            "content_type": self.content_type,
            "request_bytes": self.request_bytes,
            "response_bytes": self.response_bytes,
            "attempt_count": self.attempt_count,
            "retry_disposition": self.retry_disposition,
        }


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        request: urllib.request.Request,
        file_pointer: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> None:
        return None


class _DisabledProxyHandler(urllib.request.ProxyHandler):
    """Visible no-op handler proving that ambient proxy lookup is disabled."""

    def __init__(self) -> None:
        super().__init__({})

    def http_open(self, request: urllib.request.Request) -> None:
        return None

    def https_open(self, request: urllib.request.Request) -> None:
        return None


def _build_https_opener() -> urllib.request.OpenerDirector:
    if any(os.environ.get(name) for name in _TLS_ENVIRONMENT_OVERRIDES):
        raise _transport_error("deepseek_http_tls_environment_rejected")
    context = ssl.create_default_context()
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return urllib.request.build_opener(
        _DisabledProxyHandler(),
        urllib.request.HTTPSHandler(context=context),
        _NoRedirectHandler(),
    )


def _canonical_bytes(value: Mapping[str, object]) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise _transport_error("deepseek_http_outbound_invalid") from None


def _egress_capability_hmac(
    *,
    body: bytes,
    outbound_sha256: str,
    model: str,
    request_sha256: str,
    source_authority_proof_set_sha256: str,
    transport_material_sha256: str,
) -> str:
    identity = {
        "body_sha256": hashlib.sha256(body).hexdigest(),
        "model": model,
        "outbound_sha256": outbound_sha256,
        "request_sha256": request_sha256,
        "source_authority_proof_set_sha256": source_authority_proof_set_sha256,
        "transport_material_sha256": transport_material_sha256,
    }
    return hmac.new(
        _EGRESS_CAPABILITY_HMAC_KEY,
        _canonical_bytes(identity),
        hashlib.sha256,
    ).hexdigest()


@dataclass(frozen=True)
class _ValidatedDeepSeekEgress:
    """Keyed-integrity outbound bytes minted only after Gateway validation."""

    body: bytes = field(repr=False)
    outbound_sha256: str
    model: str
    request_sha256: str
    source_authority_proof_set_sha256: str
    transport_material_sha256: str
    _capability_seal: object = field(repr=False, compare=False)
    _capability_hmac_sha256: str = field(repr=False, compare=False)

    def verify_integrity(self) -> None:
        if (
            type(self) is not _ValidatedDeepSeekEgress
            or self._capability_seal is not _VALIDATED_EGRESS_CAPABILITY_SEAL
            or type(self.body) is not bytes
            or not self.body
            or type(self.model) is not str
            or self.model not in _APPROVED_DEEPSEEK_MODELS
        ):
            raise _transport_error("deepseek_http_egress_capability_invalid")
        for value in (
            self.outbound_sha256,
            self.request_sha256,
            self.source_authority_proof_set_sha256,
            self.transport_material_sha256,
            self._capability_hmac_sha256,
        ):
            if type(value) is not str or not _SHA256_RE.fullmatch(value):
                raise _transport_error("deepseek_http_egress_capability_invalid")
        expected_capability_hmac = _egress_capability_hmac(
            body=self.body,
            outbound_sha256=self.outbound_sha256,
            model=self.model,
            request_sha256=self.request_sha256,
            source_authority_proof_set_sha256=(self.source_authority_proof_set_sha256),
            transport_material_sha256=self.transport_material_sha256,
        )
        if not hmac.compare_digest(
            expected_capability_hmac,
            self._capability_hmac_sha256,
        ):
            raise _transport_error("deepseek_http_egress_capability_invalid")
        if not hmac.compare_digest(
            hashlib.sha256(self.body).hexdigest(),
            self.outbound_sha256,
        ):
            raise _transport_error("deepseek_http_egress_capability_invalid")
        try:
            outbound = json.loads(self.body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise _transport_error("deepseek_http_egress_capability_invalid") from None
        if type(outbound) is not dict or outbound.get("model") != self.model:
            raise _transport_error("deepseek_http_egress_capability_invalid")


def _create_validated_deepseek_egress(
    outbound: Mapping[str, object],
    *,
    outbound_sha256: str,
    model: str,
    request_sha256: str,
    source_authority_proof_set_sha256: str,
    transport_material_sha256: str,
    authority_seal: object,
) -> _ValidatedDeepSeekEgress:
    """Mint an internal egress capability after the Gateway has run all gates."""

    if authority_seal is not _GATEWAY_EGRESS_AUTHORITY_SEAL:
        raise _transport_error("deepseek_http_gateway_authority_required")
    if (
        type(outbound) is not dict
        or type(model) is not str
        or model not in _APPROVED_DEEPSEEK_MODELS
    ):
        raise _transport_error("deepseek_http_outbound_invalid")
    if outbound.get("model") != model:
        raise _transport_error("deepseek_http_outbound_model_mismatch")
    body = _canonical_bytes(outbound)
    actual_sha256 = hashlib.sha256(body).hexdigest()
    if not _SHA256_RE.fullmatch(str(outbound_sha256)) or not hmac.compare_digest(
        actual_sha256,
        str(outbound_sha256),
    ):
        raise _transport_error("deepseek_http_outbound_sha256_mismatch")
    capability_hmac_sha256 = _egress_capability_hmac(
        body=body,
        outbound_sha256=str(outbound_sha256),
        model=model,
        request_sha256=str(request_sha256),
        source_authority_proof_set_sha256=str(source_authority_proof_set_sha256),
        transport_material_sha256=str(transport_material_sha256),
    )
    capability = _ValidatedDeepSeekEgress(
        body=body,
        outbound_sha256=str(outbound_sha256),
        model=model,
        request_sha256=str(request_sha256),
        source_authority_proof_set_sha256=str(source_authority_proof_set_sha256),
        transport_material_sha256=str(transport_material_sha256),
        _capability_seal=_VALIDATED_EGRESS_CAPABILITY_SEAL,
        _capability_hmac_sha256=capability_hmac_sha256,
    )
    capability.verify_integrity()
    return capability


class _DuplicateKeyError(ValueError):
    pass


class _NonFiniteNumberError(ValueError):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError
        result[key] = value
    return result


def _reject_non_finite(_: str) -> None:
    raise _NonFiniteNumberError


def _validate_json_tree(value: Any, *, max_depth: int) -> None:
    nodes = 0

    def visit(candidate: Any, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > _MAX_JSON_NODES:
            raise _transport_error(
                "deepseek_http_response_too_complex",
                invalid_response=True,
            )
        if depth > max_depth:
            raise _transport_error(
                "deepseek_http_response_too_deep",
                invalid_response=True,
            )
        if isinstance(candidate, dict):
            for key, item in candidate.items():
                if type(key) is not str:
                    raise _transport_error(
                        "deepseek_http_response_schema_invalid",
                        invalid_response=True,
                    )
                visit(item, depth + 1)
        elif isinstance(candidate, list):
            for item in candidate:
                visit(item, depth + 1)

    visit(value, 1)


def _strict_json_object(
    raw: bytes | str,
    *,
    max_depth: int,
) -> dict[str, Any]:
    if isinstance(raw, bytes):
        if raw.startswith(b"\xef\xbb\xbf"):
            raise _transport_error(
                "deepseek_http_response_utf8_invalid",
                invalid_response=True,
            )
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise _transport_error(
                "deepseek_http_response_utf8_invalid",
                invalid_response=True,
            ) from None
    else:
        text = raw
    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite,
        )
    except _DuplicateKeyError:
        raise _transport_error(
            "deepseek_http_response_duplicate_key",
            invalid_response=True,
        ) from None
    except _NonFiniteNumberError:
        raise _transport_error(
            "deepseek_http_response_non_finite_number",
            invalid_response=True,
        ) from None
    except (TypeError, json.JSONDecodeError):
        raise _transport_error(
            "deepseek_http_response_json_invalid",
            invalid_response=True,
        ) from None
    if type(value) is not dict:
        raise _transport_error(
            "deepseek_http_response_object_required",
            invalid_response=True,
        )
    _validate_json_tree(value, max_depth=max_depth)
    return value


def _validate_provider_envelope(
    payload: dict[str, Any],
    *,
    expected_model: object,
    max_depth: int,
) -> None:
    if payload.get("model") != expected_model:
        raise _transport_error(
            "deepseek_http_response_model_mismatch",
            invalid_response=True,
        )
    choices = payload.get("choices")
    if type(choices) is not list or len(choices) != 1 or type(choices[0]) is not dict:
        raise _transport_error(
            "deepseek_http_response_choices_invalid",
            invalid_response=True,
        )
    choice = choices[0]
    message = choice.get("message")
    if type(message) is not dict:
        raise _transport_error(
            "deepseek_http_response_message_invalid",
            invalid_response=True,
        )
    if "tool_calls" in message or "function_call" in message:
        raise _transport_error(
            "deepseek_http_response_tool_calls_forbidden",
            invalid_response=True,
        )
    if choice.get("finish_reason") != "stop":
        raise _transport_error(
            "deepseek_http_response_finish_reason_invalid",
            invalid_response=True,
        )
    if message.get("role") != "assistant" or type(message.get("content")) is not str:
        raise _transport_error(
            "deepseek_http_response_message_invalid",
            invalid_response=True,
        )
    _strict_json_object(message["content"], max_depth=max_depth)


def _header(response: Any, name: str) -> str:
    headers = getattr(response, "headers", None)
    if headers is None:
        return ""
    value = headers.get(name)
    return str(value or "").strip()


def _read_limited_response(response: Any, *, limit: int) -> bytes:
    content_length = _header(response, "Content-Length")
    if content_length:
        try:
            advertised = int(content_length)
        except ValueError:
            raise _transport_error(
                "deepseek_http_content_length_invalid",
                invalid_response=True,
            ) from None
        if advertised < 0 or advertised > limit:
            raise _transport_error("deepseek_http_response_too_large")
    chunks: list[bytes] = []
    total = 0
    while True:
        requested = min(_READ_CHUNK_BYTES, limit + 1 - total)
        if requested <= 0:
            raise _transport_error("deepseek_http_response_too_large")
        chunk = response.read(requested)
        if not chunk:
            break
        if type(chunk) is not bytes:
            raise _transport_error(
                "deepseek_http_response_bytes_required",
                invalid_response=True,
            )
        chunks.append(chunk)
        total += len(chunk)
        if total > limit:
            raise _transport_error("deepseek_http_response_too_large")
    return b"".join(chunks)


class DeepSeekHTTPTransport:
    """One-attempt official DeepSeek HTTPS transport with no fallback."""

    __slots__ = (
        "_credential_path",
        "_max_json_depth",
        "_max_request_bytes",
        "_max_response_bytes",
        "_timeout_seconds",
    )

    def __init__(self, config: DeepSeekHTTPTransportConfig) -> None:
        if type(config) is not DeepSeekHTTPTransportConfig:
            raise TypeError("deepseek_http_config_policy_rejected")
        if config.network_enabled is not True:
            raise _transport_error("deepseek_http_network_disabled")
        if type(config.credential) is not DeepSeekCredentialFile:
            raise _transport_error("deepseek_credential_missing")
        # Copy validated primitive values and the immutable Path so later
        # mutation of caller-owned config/credential objects cannot redirect
        # egress or relax byte/depth limits.
        self._credential_path = Path(config.credential.path)
        self._timeout_seconds = float(config.timeout_seconds)
        self._max_request_bytes = int(config.max_request_bytes)
        self._max_response_bytes = int(config.max_response_bytes)
        self._max_json_depth = int(config.max_json_depth)

    def __repr__(self) -> str:
        return (
            "DeepSeekHTTPTransport("
            f"transport_id={DEEPSEEK_HTTP_TRANSPORT_ID!r}, "
            "network_enabled=True)"
        )

    def to_public_descriptor(self) -> dict[str, object]:
        return {
            "network_enabled": True,
            "endpoint": OFFICIAL_DEEPSEEK_CHAT_COMPLETIONS_URL,
            "timeout_seconds": self._timeout_seconds,
            "max_request_bytes": self._max_request_bytes,
            "max_response_bytes": self._max_response_bytes,
            "max_json_depth": self._max_json_depth,
            "max_attempts": 1,
            "proxy_policy": "disabled",
            "redirect_policy": "reject",
            "tls_policy": "system_ca_hostname_verified",
            "transport_id": DEEPSEEK_HTTP_TRANSPORT_ID,
            "transport_version": DEEPSEEK_HTTP_TRANSPORT_VERSION,
            "egress_policy_version": DEEPSEEK_EGRESS_POLICY_VERSION,
        }

    def send(self, *_: object, **__: object) -> DeepSeekHTTPResponse:
        """Reject direct public egress; only the Gateway may invoke the wire path."""

        raise _transport_error("deepseek_http_direct_send_forbidden")

    def _send_validated(
        self,
        egress: _ValidatedDeepSeekEgress,
    ) -> DeepSeekHTTPResponse:
        if type(self) is not DeepSeekHTTPTransport:
            raise TypeError("deepseek_http_transport_policy_rejected")
        if type(egress) is not _ValidatedDeepSeekEgress:
            raise _transport_error("deepseek_http_egress_capability_required")
        egress.verify_integrity()
        body = egress.body
        if len(body) > self._max_request_bytes:
            raise _transport_error("deepseek_http_request_too_large")

        # Construct the fixed, proxy-free TLS client before loading the
        # credential so client initialisation failures never prolong the
        # in-memory lifetime of the secret.
        try:
            opener = _build_https_opener()
        except Exception:
            raise _transport_error(
                "deepseek_http_client_initialization_failed"
            ) from None
        credential = DeepSeekCredentialFile(self._credential_path)
        secret = DeepSeekCredentialFile.read_secret(credential)
        secret_bytes = secret.encode("ascii")
        if secret_bytes in body:
            raise _transport_error("deepseek_http_credential_in_body_rejected")

        request = urllib.request.Request(
            OFFICIAL_DEEPSEEK_CHAT_COMPLETIONS_URL,
            data=body,
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "Authorization": f"Bearer {secret}",
                "Content-Type": "application/json",
                "User-Agent": "TradingAgent-LLMEvidence/1",
            },
            method="POST",
        )
        try:
            response = opener.open(
                request,
                timeout=self._timeout_seconds,
            )
        except urllib.error.HTTPError as exc:
            try:
                exc.close()
            finally:
                raise _transport_error(f"deepseek_http_status_{exc.code}") from None
        except (TimeoutError, socket.timeout):
            raise _transport_error("deepseek_http_timeout") from None
        except (urllib.error.URLError, OSError):
            raise _transport_error("deepseek_http_network_error") from None
        finally:
            request.remove_header("Authorization")
            del secret
            del secret_bytes

        try:
            status = int(getattr(response, "status", response.getcode()))
            if status != 200:
                raise _transport_error(f"deepseek_http_status_{status}")
            content_type_header = _header(response, "Content-Type")
            content_type = content_type_header.split(";", 1)[0].strip().casefold()
            if content_type != "application/json":
                raise _transport_error(
                    "deepseek_http_content_type_invalid",
                    invalid_response=True,
                )
            content_encoding = _header(response, "Content-Encoding").casefold()
            if content_encoding not in {"", "identity"}:
                raise _transport_error(
                    "deepseek_http_content_encoding_invalid",
                    invalid_response=True,
                )
            raw = _read_limited_response(
                response,
                limit=self._max_response_bytes,
            )
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()

        payload = _strict_json_object(raw, max_depth=self._max_json_depth)
        _validate_provider_envelope(
            payload,
            expected_model=egress.model,
            max_depth=self._max_json_depth,
        )
        return DeepSeekHTTPResponse(
            payload=MappingProxyType(payload),
            raw_response_sha256=hashlib.sha256(raw).hexdigest(),
            received_at=datetime.now(timezone.utc).isoformat(),
            request_bytes=len(body),
            response_bytes=len(raw),
            content_type=content_type,
        )


__all__ = [
    "DEEPSEEK_EGRESS_POLICY_VERSION",
    "DEEPSEEK_HTTP_TRANSPORT_ID",
    "DEEPSEEK_HTTP_TRANSPORT_VERSION",
    "OFFICIAL_DEEPSEEK_CHAT_COMPLETIONS_URL",
    "DeepSeekCredentialFile",
    "DeepSeekHTTPResponse",
    "DeepSeekHTTPTransport",
    "DeepSeekHTTPTransportConfig",
    "DeepSeekHTTPTransportError",
]
