from __future__ import annotations

import hashlib
import importlib
import io
import json
import os
import ssl
import tempfile
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import replace
from datetime import datetime
from email.message import Message
from pathlib import Path
from typing import Any, Mapping

import pytest


@pytest.fixture
def tmp_path() -> Iterator[Path]:
    """Create credentials below a portable, owner-controlled parent chain."""

    with tempfile.TemporaryDirectory(
        prefix=".ta-deepseek-test-",
        dir=Path.home(),
    ) as directory:
        secure_path = Path(directory)
        secure_path.chmod(0o700)
        yield secure_path


def _http_module() -> Any:
    """Load lazily so the intended RED phase is a behavioural failure."""

    try:
        return importlib.import_module("shared.llm.providers.deepseek_http")
    except ModuleNotFoundError as exc:  # pragma: no cover - RED phase only
        raise AssertionError("strict DeepSeek HTTPS transport is missing") from exc


def _fake_key() -> str:
    # Deliberately assembled at runtime so no credential-shaped literal is
    # committed to the repository. This value is never sent over a network.
    return "sk-" + "offline-unit-test-" + ("x" * 32)


def _canonical_bytes(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _outbound() -> dict[str, object]:
    return {
        "model": "deepseek-v4-pro",
        "messages": [
            {
                "role": "system",
                "content": "Return one JSON object containing research evidence.",
            },
            {
                "role": "user",
                "content": json.dumps(
                    {"payload": {"symbol": "600000.SH"}},
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            },
        ],
        "response_format": {"type": "json_object"},
        "stream": False,
        "max_tokens": 8192,
        "thinking": {"type": "enabled"},
        "reasoning_effort": "high",
    }


def _provider_envelope(
    *,
    choices: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    evidence = {
        "bull_case": "Published evidence supports improving delivery.",
        "bear_case": "Customer acceptance remains uncertain.",
        "key_risk": "Revenue recognition may be delayed.",
        "evidence_refs": ["artifact-fixture-001"],
    }
    return {
        "id": "chatcmpl-offline-fixture-001",
        "object": "chat.completion",
        "created": 1784246400,
        "model": "deepseek-v4-pro",
        "choices": choices
        if choices is not None
        else [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": json.dumps(
                        evidence,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                },
            }
        ],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 20,
            "total_tokens": 30,
        },
    }


class _FakeResponse:
    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        content_type: str = "application/json; charset=utf-8",
        content_encoding: str | None = None,
        content_length: int | None = None,
    ) -> None:
        self.status = status
        self._stream = io.BytesIO(body)
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        if content_encoding is not None:
            self.headers["Content-Encoding"] = content_encoding
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)
        self.read_limits: list[int | None] = []

    def read(self, amount: int | None = None) -> bytes:
        self.read_limits.append(amount)
        return self._stream.read() if amount is None else self._stream.read(amount)

    def getcode(self) -> int:
        return self.status

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_: object) -> None:
        return None


class _FakeOpener:
    def __init__(self, outcome: _FakeResponse | BaseException) -> None:
        self.outcome = outcome
        self.calls: list[tuple[urllib.request.Request, float | None]] = []
        self.header_snapshots: list[dict[str, str]] = []

    def open(
        self,
        request: urllib.request.Request,
        *,
        timeout: float | None = None,
    ) -> _FakeResponse:
        self.calls.append((request, timeout))
        self.header_snapshots.append(
            {name.casefold(): value for name, value in request.header_items()}
        )
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def _write_credential(
    tmp_path: Path,
    *,
    content: str | None = None,
    mode: int = 0o600,
    name: str = "deepseek.key",
) -> tuple[Path, str]:
    key = _fake_key() if content is None else content
    path = tmp_path / name
    path.write_text(key, encoding="utf-8")
    path.chmod(mode)
    return path, key


def _config(
    module: Any,
    credential_path: Path,
    **overrides: object,
) -> Any:
    values: dict[str, object] = {
        "network_enabled": True,
        "credential": module.DeepSeekCredentialFile(credential_path),
        "endpoint": module.OFFICIAL_DEEPSEEK_CHAT_COMPLETIONS_URL,
        "timeout_seconds": 30.0,
        "max_request_bytes": 256 * 1024,
        "max_response_bytes": 2 * 1024 * 1024,
        "max_json_depth": 32,
    }
    values.update(overrides)
    return module.DeepSeekHTTPTransportConfig(**values)


def _transport_with_fake(
    monkeypatch: pytest.MonkeyPatch,
    module: Any,
    config: Any,
    opener: _FakeOpener,
) -> Any:
    monkeypatch.setattr(module, "_build_https_opener", lambda: opener)
    return module.DeepSeekHTTPTransport(config)


def _validated_egress(
    module: Any,
    outbound: Mapping[str, object],
    *,
    outbound_sha256: str | None = None,
) -> Any:
    return module._create_validated_deepseek_egress(
        outbound,
        outbound_sha256=(
            _sha256(_canonical_bytes(outbound))
            if outbound_sha256 is None
            else outbound_sha256
        ),
        model=str(outbound.get("model") or ""),
        request_sha256="a" * 64,
        source_authority_proof_set_sha256="b" * 64,
        transport_material_sha256="c" * 64,
        authority_seal=module._GATEWAY_EGRESS_AUTHORITY_SEAL,
    )


def _send(transport: Any, outbound: Mapping[str, object]) -> Any:
    module = _http_module()
    return transport._send_validated(_validated_egress(module, outbound))


def _assert_sanitized_error(
    module: Any,
    caught: pytest.ExceptionInfo[BaseException],
    *,
    reason_code: str,
    key: str,
    path: Path,
) -> None:
    error = caught.value
    assert isinstance(error, module.DeepSeekHTTPTransportError)
    assert error.reason_code == reason_code
    rendered = f"{error!r} {error}"
    assert key not in rendered
    assert str(path) not in rendered


def test_default_configuration_is_network_disabled_and_ignores_ambient_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _http_module()
    build_called = False

    def forbidden_builder() -> object:
        nonlocal build_called
        build_called = True
        raise AssertionError("disabled configuration must not build a client")

    monkeypatch.setenv("TRADINGAGENT_LLM_NETWORK_ENABLED", "true")
    monkeypatch.setenv("DEEPSEEK_API_KEY", _fake_key())
    monkeypatch.setattr(module, "_build_https_opener", forbidden_builder)

    config = module.DeepSeekHTTPTransportConfig()

    assert config.network_enabled is False
    assert config.credential is None
    assert not hasattr(type(config), "from_environment")
    with pytest.raises(
        module.DeepSeekHTTPTransportError,
        match="deepseek_http_network_disabled",
    ):
        module.DeepSeekHTTPTransport(config)
    assert build_called is False


def test_transport_requires_exact_explicit_config_and_has_no_public_opener(
    tmp_path: Path,
) -> None:
    module = _http_module()
    path, _ = _write_credential(tmp_path)
    config = _config(module, path)

    class ConfigSubclass(module.DeepSeekHTTPTransportConfig):
        pass

    with pytest.raises(TypeError, match="deepseek_http_config_policy_rejected"):
        module.DeepSeekHTTPTransport(
            ConfigSubclass(
                network_enabled=True,
                credential=module.DeepSeekCredentialFile(path),
                endpoint=module.OFFICIAL_DEEPSEEK_CHAT_COMPLETIONS_URL,
                timeout_seconds=30.0,
                max_request_bytes=256 * 1024,
                max_response_bytes=2 * 1024 * 1024,
                max_json_depth=32,
            )
        )
    with pytest.raises(TypeError):
        module.DeepSeekHTTPTransport(config, opener=_FakeOpener(Exception()))


def test_transport_snapshots_policy_and_uses_constant_endpoint_after_config_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _http_module()
    path, original_key = _write_credential(tmp_path)
    replacement, replacement_key = _write_credential(
        tmp_path,
        content="sk-" + "replacement-offline-" + ("y" * 32),
        name="replacement.key",
    )
    config = _config(module, path)
    credential = config.credential
    opener = _FakeOpener(_FakeResponse(_canonical_bytes(_provider_envelope())))
    transport = _transport_with_fake(monkeypatch, module, config, opener)

    object.__setattr__(config, "endpoint", "https://example.com/capture")
    object.__setattr__(config, "max_request_bytes", 1)
    object.__setattr__(config, "max_response_bytes", 1)
    object.__setattr__(config, "max_json_depth", 1)
    assert credential is not None
    object.__setattr__(credential, "path", replacement)

    result = _send(transport, _outbound())

    assert result.http_status == 200
    assert len(opener.calls) == 1
    request, _ = opener.calls[0]
    headers = opener.header_snapshots[0]
    assert request.full_url == module.OFFICIAL_DEEPSEEK_CHAT_COMPLETIONS_URL
    assert headers["authorization"] == f"Bearer {original_key}"
    assert "authorization" not in {
        name.casefold() for name, _ in request.header_items()
    }
    assert replacement_key not in repr(request.header_items())
    descriptor = transport.to_public_descriptor()
    assert descriptor["endpoint"] == module.OFFICIAL_DEEPSEEK_CHAT_COMPLETIONS_URL
    assert descriptor["max_request_bytes"] == 256 * 1024
    assert descriptor["max_response_bytes"] == 2 * 1024 * 1024
    assert descriptor["max_json_depth"] == 32


@pytest.mark.parametrize(
    "endpoint",
    (
        "http://api.deepseek.com/chat/completions",
        "https://api.deepseek.com/v1/chat/completions",
        "https://api.deepseek.com/chat/completions?key=value",
        "https://api.deepseek.com/chat/completions#fragment",
        "https://user@api.deepseek.com/chat/completions",
        "https://example.com/chat/completions",
    ),
)
def test_only_exact_official_https_endpoint_is_accepted(
    tmp_path: Path,
    endpoint: str,
) -> None:
    module = _http_module()
    path, _ = _write_credential(tmp_path)

    with pytest.raises(
        module.DeepSeekHTTPTransportError,
        match="deepseek_http_endpoint_invalid",
    ):
        _config(module, path, endpoint=endpoint)


def test_default_opener_disables_proxies_rejects_redirects_and_verifies_tls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _http_module()
    for name in (
        "CURL_CA_BUNDLE",
        "REQUESTS_CA_BUNDLE",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SSLKEYLOGFILE",
    ):
        monkeypatch.delenv(name, raising=False)

    opener = module._build_https_opener()
    handlers = tuple(opener.handlers)
    proxy_handlers = [
        handler
        for handler in handlers
        if isinstance(handler, urllib.request.ProxyHandler)
    ]
    https_handlers = [
        handler
        for handler in handlers
        if isinstance(handler, urllib.request.HTTPSHandler)
    ]
    redirect_handlers = [
        handler
        for handler in handlers
        if isinstance(handler, urllib.request.HTTPRedirectHandler)
    ]

    assert len(proxy_handlers) == 1
    assert proxy_handlers[0].proxies == {}
    assert len(https_handlers) == 1
    tls_context = https_handlers[0]._context
    assert tls_context.check_hostname is True
    assert tls_context.verify_mode == ssl.CERT_REQUIRED
    assert len(redirect_handlers) == 1
    assert (
        redirect_handlers[0].redirect_request(
            urllib.request.Request(module.OFFICIAL_DEEPSEEK_CHAT_COMPLETIONS_URL),
            None,
            302,
            "Found",
            Message(),
            "https://example.com/capture",
        )
        is None
    )


@pytest.mark.parametrize(
    "variable",
    (
        "CURL_CA_BUNDLE",
        "REQUESTS_CA_BUNDLE",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SSLKEYLOGFILE",
    ),
)
def test_default_opener_rejects_ambient_tls_and_keylog_overrides(
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
) -> None:
    module = _http_module()
    for name in (
        "CURL_CA_BUNDLE",
        "REQUESTS_CA_BUNDLE",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SSLKEYLOGFILE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(variable, "/tmp/unapproved-tls-material")

    with pytest.raises(
        module.DeepSeekHTTPTransportError,
        match="deepseek_http_tls_environment_rejected",
    ):
        module._build_https_opener()


def test_raw_credential_file_must_be_regular_0600_and_is_redacted_from_repr(
    tmp_path: Path,
) -> None:
    module = _http_module()
    path, key = _write_credential(tmp_path)

    credential = module.DeepSeekCredentialFile(path)

    assert credential.read_secret() == key
    rendered = repr(credential)
    assert key not in rendered
    assert str(path) not in rendered


@pytest.mark.parametrize(
    "content,mode,reason_code",
    (
        (None, 0o644, "deepseek_credential_mode_invalid"),
        ("DEEPSEEK_API_KEY=" + ("x" * 32), 0o600, "deepseek_credential_raw_required"),
        ((_fake_key() + "\nsecond-line"), 0o600, "deepseek_credential_raw_required"),
        ("not-a-deepseek-key", 0o600, "deepseek_credential_format_invalid"),
    ),
)
def test_credential_rejects_unsafe_mode_env_style_multiline_and_bad_format(
    tmp_path: Path,
    content: str | None,
    mode: int,
    reason_code: str,
) -> None:
    module = _http_module()
    path, key = _write_credential(tmp_path, content=content, mode=mode)
    credential = module.DeepSeekCredentialFile(path)

    with pytest.raises(module.DeepSeekHTTPTransportError) as caught:
        credential.read_secret()

    _assert_sanitized_error(
        module,
        caught,
        reason_code=reason_code,
        key=key,
        path=path,
    )


def test_credential_rejects_symlink_without_following_it(tmp_path: Path) -> None:
    module = _http_module()
    target, key = _write_credential(tmp_path, name="target.key")
    link = tmp_path / "linked.key"
    link.symlink_to(target)
    credential = module.DeepSeekCredentialFile(link)

    with pytest.raises(module.DeepSeekHTTPTransportError) as caught:
        credential.read_secret()

    _assert_sanitized_error(
        module,
        caught,
        reason_code="deepseek_credential_symlink_forbidden",
        key=key,
        path=link,
    )


def test_credential_rejects_symlinked_parent_directory(tmp_path: Path) -> None:
    module = _http_module()
    trusted = tmp_path / "trusted"
    trusted.mkdir(mode=0o700)
    target, key = _write_credential(trusted)
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(trusted, target_is_directory=True)
    candidate = linked_parent / target.name

    with pytest.raises(module.DeepSeekHTTPTransportError) as caught:
        module.DeepSeekCredentialFile(candidate).read_secret()

    _assert_sanitized_error(
        module,
        caught,
        reason_code="deepseek_credential_parent_untrusted",
        key=key,
        path=candidate,
    )


@pytest.mark.parametrize("mode", (0o720, 0o702, 0o777))
def test_credential_rejects_group_or_world_writable_parent(
    tmp_path: Path,
    mode: int,
) -> None:
    module = _http_module()
    unsafe = tmp_path / f"unsafe-{mode:o}"
    unsafe.mkdir(mode=0o700)
    path, key = _write_credential(unsafe)
    unsafe.chmod(mode)
    try:
        with pytest.raises(module.DeepSeekHTTPTransportError) as caught:
            module.DeepSeekCredentialFile(path).read_secret()
    finally:
        unsafe.chmod(0o700)

    _assert_sanitized_error(
        module,
        caught,
        reason_code="deepseek_credential_parent_untrusted",
        key=key,
        path=path,
    )


def test_send_uses_canonical_post_exact_url_and_file_credential_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _http_module()
    path, key = _write_credential(tmp_path)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-" + ("ambient-trap-" * 4))
    raw = _canonical_bytes(_provider_envelope())
    opener = _FakeOpener(_FakeResponse(raw))
    config = _config(module, path)
    transport = _transport_with_fake(monkeypatch, module, config, opener)
    outbound = _outbound()

    result = _send(transport, outbound)

    assert len(opener.calls) == 1
    request, timeout = opener.calls[0]
    headers = opener.header_snapshots[0]
    assert request.full_url == module.OFFICIAL_DEEPSEEK_CHAT_COMPLETIONS_URL
    assert request.get_method() == "POST"
    assert request.data == _canonical_bytes(outbound)
    assert timeout == 30.0
    assert headers["authorization"] == f"Bearer {key}"
    assert headers["content-type"] == "application/json"
    assert headers["accept"] == "application/json"
    assert "authorization" not in {
        name.casefold() for name, _ in request.header_items()
    }
    assert key not in request.full_url
    assert key.encode("utf-8") not in (request.data or b"")
    assert result.payload == _provider_envelope()


def test_capability_rejects_wrong_outbound_hash_before_credential_or_opener(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _http_module()
    path, key = _write_credential(tmp_path)
    opener = _FakeOpener(_FakeResponse(_canonical_bytes(_provider_envelope())))
    _transport_with_fake(
        monkeypatch,
        module,
        _config(module, path),
        opener,
    )
    credential_reads = 0

    def forbidden_credential_read(_: object) -> str:
        nonlocal credential_reads
        credential_reads += 1
        raise AssertionError("sensitive payload must fail before credential read")

    monkeypatch.setattr(
        module.DeepSeekCredentialFile,
        "read_secret",
        forbidden_credential_read,
    )

    with pytest.raises(module.DeepSeekHTTPTransportError) as caught:
        _validated_egress(
            module,
            _outbound(),
            outbound_sha256="0" * 64,
        )

    _assert_sanitized_error(
        module,
        caught,
        reason_code="deepseek_http_outbound_sha256_mismatch",
        key=key,
        path=path,
    )
    assert opener.calls == []
    assert credential_reads == 0


def test_public_send_rejects_arbitrary_payload_before_secret_or_opener(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _http_module()
    path, key = _write_credential(tmp_path)
    opener = _FakeOpener(_FakeResponse(_canonical_bytes(_provider_envelope())))
    transport = _transport_with_fake(
        monkeypatch,
        module,
        _config(module, path),
        opener,
    )
    credential_reads = 0

    def forbidden_credential_read(_: object) -> str:
        nonlocal credential_reads
        credential_reads += 1
        raise AssertionError("direct send must fail before credential read")

    monkeypatch.setattr(
        module.DeepSeekCredentialFile,
        "read_secret",
        forbidden_credential_read,
    )
    arbitrary_payload = {
        "model": "deepseek-v4-pro",
        "messages": [
            {
                "role": "user",
                "content": {
                    "broker_account": "forbidden",
                    "positions": ["600000.SH"],
                },
            }
        ],
    }

    with pytest.raises(module.DeepSeekHTTPTransportError) as caught:
        transport.send(
            arbitrary_payload,
            outbound_sha256=_sha256(_canonical_bytes(arbitrary_payload)),
        )

    _assert_sanitized_error(
        module,
        caught,
        reason_code="deepseek_http_direct_send_forbidden",
        key=key,
        path=path,
    )
    assert opener.calls == []
    assert credential_reads == 0


def test_private_capability_factory_rejects_forged_authority() -> None:
    module = _http_module()
    outbound = _outbound()

    with pytest.raises(
        module.DeepSeekHTTPTransportError,
        match="deepseek_http_gateway_authority_required",
    ):
        module._create_validated_deepseek_egress(
            outbound,
            outbound_sha256=_sha256(_canonical_bytes(outbound)),
            model="deepseek-v4-pro",
            request_sha256="a" * 64,
            source_authority_proof_set_sha256="b" * 64,
            transport_material_sha256="c" * 64,
            authority_seal=True,
        )


def test_mutated_egress_capability_fails_before_secret_or_opener(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _http_module()
    path, key = _write_credential(tmp_path)
    opener = _FakeOpener(_FakeResponse(_canonical_bytes(_provider_envelope())))
    transport = _transport_with_fake(
        monkeypatch,
        module,
        _config(module, path),
        opener,
    )
    credential_reads = 0

    def forbidden_credential_read(_: object) -> str:
        nonlocal credential_reads
        credential_reads += 1
        raise AssertionError("mutated capability must fail before credential read")

    monkeypatch.setattr(
        module.DeepSeekCredentialFile,
        "read_secret",
        forbidden_credential_read,
    )
    egress = _validated_egress(module, _outbound())
    object.__setattr__(
        egress,
        "body",
        _canonical_bytes(
            {
                "model": "deepseek-v4-pro",
                "messages": [{"role": "user", "content": {"positions": [1]}}],
            }
        ),
    )
    object.__setattr__(
        egress,
        "outbound_sha256",
        _sha256(egress.body),
    )
    assert egress.outbound_sha256 == _sha256(egress.body)

    with pytest.raises(module.DeepSeekHTTPTransportError) as caught:
        transport._send_validated(egress)

    _assert_sanitized_error(
        module,
        caught,
        reason_code="deepseek_http_egress_capability_invalid",
        key=key,
        path=path,
    )
    assert opener.calls == []
    assert credential_reads == 0


def test_client_initialisation_failure_happens_before_credential_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _http_module()
    path, _ = _write_credential(tmp_path)
    credential_reads = 0

    def forbidden_credential_read(_: object) -> str:
        nonlocal credential_reads
        credential_reads += 1
        raise AssertionError("credential must remain unread")

    def broken_builder() -> object:
        raise RuntimeError("local client initialisation failed")

    monkeypatch.setattr(
        module.DeepSeekCredentialFile,
        "read_secret",
        forbidden_credential_read,
    )
    monkeypatch.setattr(module, "_build_https_opener", broken_builder)
    transport = module.DeepSeekHTTPTransport(_config(module, path))

    with pytest.raises(module.DeepSeekHTTPTransportError) as caught:
        _send(transport, _outbound())

    assert caught.value.reason_code == "deepseek_http_client_initialization_failed"
    assert credential_reads == 0


def test_request_and_response_byte_limits_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _http_module()
    path, key = _write_credential(tmp_path)
    opener = _FakeOpener(_FakeResponse(b"{" + (b"x" * 256) + b"}"))
    outbound = _outbound()
    outbound["messages"] = [{"role": "user", "content": "x" * 512}]
    request_transport = _transport_with_fake(
        monkeypatch,
        module,
        _config(module, path, max_request_bytes=64),
        opener,
    )

    with pytest.raises(module.DeepSeekHTTPTransportError) as request_error:
        _send(request_transport, outbound)
    _assert_sanitized_error(
        module,
        request_error,
        reason_code="deepseek_http_request_too_large",
        key=key,
        path=path,
    )
    assert opener.calls == []

    response_opener = _FakeOpener(
        _FakeResponse(
            b"{" + (b"x" * 256) + b"}",
            content_length=258,
        )
    )
    response_transport = _transport_with_fake(
        monkeypatch,
        module,
        _config(module, path, max_response_bytes=64),
        response_opener,
    )
    with pytest.raises(module.DeepSeekHTTPTransportError) as response_error:
        _send(response_transport, _outbound())
    _assert_sanitized_error(
        module,
        response_error,
        reason_code="deepseek_http_response_too_large",
        key=key,
        path=path,
    )
    assert len(response_opener.calls) == 1


@pytest.mark.parametrize(
    "content_type,content_encoding,reason_code",
    (
        ("text/plain", None, "deepseek_http_content_type_invalid"),
        ("application/problem+json", None, "deepseek_http_content_type_invalid"),
        ("application/json", "gzip", "deepseek_http_content_encoding_invalid"),
    ),
)
def test_response_requires_uncompressed_application_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    content_type: str,
    content_encoding: str | None,
    reason_code: str,
) -> None:
    module = _http_module()
    path, key = _write_credential(tmp_path)
    opener = _FakeOpener(
        _FakeResponse(
            _canonical_bytes(_provider_envelope()),
            content_type=content_type,
            content_encoding=content_encoding,
        )
    )
    transport = _transport_with_fake(
        monkeypatch,
        module,
        _config(module, path),
        opener,
    )

    with pytest.raises(module.DeepSeekHTTPTransportError) as caught:
        _send(transport, _outbound())

    _assert_sanitized_error(
        module,
        caught,
        reason_code=reason_code,
        key=key,
        path=path,
    )


@pytest.mark.parametrize(
    "raw,config_overrides,reason_code",
    (
        (b"\xff\xfe", {}, "deepseek_http_response_utf8_invalid"),
        (b"[]", {}, "deepseek_http_response_object_required"),
        (
            b'{"id":"one","id":"two","choices":[]}',
            {},
            "deepseek_http_response_duplicate_key",
        ),
        (
            b'{"id":"one","value":NaN,"choices":[]}',
            {},
            "deepseek_http_response_non_finite_number",
        ),
        (
            json.dumps({"a": {"b": {"c": {"d": 1}}}}).encode("utf-8"),
            {"max_json_depth": 3},
            "deepseek_http_response_too_deep",
        ),
    ),
)
def test_response_json_is_strict_utf8_object_without_duplicates_nan_or_excess_depth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raw: bytes,
    config_overrides: dict[str, object],
    reason_code: str,
) -> None:
    module = _http_module()
    path, key = _write_credential(tmp_path)
    opener = _FakeOpener(_FakeResponse(raw))
    transport = _transport_with_fake(
        monkeypatch,
        module,
        _config(module, path, **config_overrides),
        opener,
    )

    with pytest.raises(module.DeepSeekHTTPTransportError) as caught:
        _send(transport, _outbound())

    _assert_sanitized_error(
        module,
        caught,
        reason_code=reason_code,
        key=key,
        path=path,
    )


@pytest.mark.parametrize(
    "choices,reason_code",
    (
        (
            [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "{}"},
                },
                {
                    "index": 1,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "{}"},
                },
            ],
            "deepseek_http_response_choices_invalid",
        ),
        (
            [
                {
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": "{}",
                        "tool_calls": [{"id": "must-not-execute"}],
                    },
                }
            ],
            "deepseek_http_response_tool_calls_forbidden",
        ),
    ),
)
def test_response_rejects_multiple_choices_and_tool_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    choices: list[dict[str, object]],
    reason_code: str,
) -> None:
    module = _http_module()
    path, key = _write_credential(tmp_path)
    raw = _canonical_bytes(_provider_envelope(choices=choices))
    opener = _FakeOpener(_FakeResponse(raw))
    transport = _transport_with_fake(
        monkeypatch,
        module,
        _config(module, path),
        opener,
    )

    with pytest.raises(module.DeepSeekHTTPTransportError) as caught:
        _send(transport, _outbound())

    _assert_sanitized_error(
        module,
        caught,
        reason_code=reason_code,
        key=key,
        path=path,
    )


@pytest.mark.parametrize("status", (301, 302, 400, 401, 402, 422, 429, 500, 503))
def test_http_failures_are_redacted_and_never_automatically_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    module = _http_module()
    path, key = _write_credential(tmp_path)
    url = module.OFFICIAL_DEEPSEEK_CHAT_COMPLETIONS_URL
    error_body = f"provider error leaked {key} at {path}".encode("utf-8")
    failure = urllib.error.HTTPError(
        url,
        status,
        "provider failure",
        Message(),
        io.BytesIO(error_body),
    )
    opener = _FakeOpener(failure)
    transport = _transport_with_fake(
        monkeypatch,
        module,
        _config(module, path),
        opener,
    )

    with pytest.raises(module.DeepSeekHTTPTransportError) as caught:
        _send(transport, _outbound())

    _assert_sanitized_error(
        module,
        caught,
        reason_code=f"deepseek_http_status_{status}",
        key=key,
        path=path,
    )
    assert len(opener.calls) == 1


def test_network_failure_is_redacted_and_never_automatically_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _http_module()
    path, key = _write_credential(tmp_path)
    failure = urllib.error.URLError(f"network failure {key} at {path}")
    opener = _FakeOpener(failure)
    transport = _transport_with_fake(
        monkeypatch,
        module,
        _config(module, path),
        opener,
    )

    with pytest.raises(module.DeepSeekHTTPTransportError) as caught:
        _send(transport, _outbound())

    _assert_sanitized_error(
        module,
        caught,
        reason_code="deepseek_http_network_error",
        key=key,
        path=path,
    )
    assert len(opener.calls) == 1


def test_success_binds_raw_response_hash_and_safe_http_transport_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _http_module()
    path, key = _write_credential(tmp_path)
    raw = json.dumps(
        _provider_envelope(),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=False,
        separators=(",", ":"),
    ).encode("utf-8")
    opener = _FakeOpener(_FakeResponse(raw))
    config = _config(module, path)
    transport = _transport_with_fake(monkeypatch, module, config, opener)
    outbound = _outbound()

    result = _send(transport, outbound)

    assert result.raw_response_sha256 == _sha256(raw)
    assert result.request_bytes == len(_canonical_bytes(outbound))
    assert result.response_bytes == len(raw)
    assert result.content_type == "application/json"
    assert result.http_status == 200
    assert result.attempt_count == 1
    assert result.retry_disposition == "not_retried"
    assert result.transport_id == module.DEEPSEEK_HTTP_TRANSPORT_ID
    assert result.transport_version == module.DEEPSEEK_HTTP_TRANSPORT_VERSION
    assert result.egress_policy_version == module.DEEPSEEK_EGRESS_POLICY_VERSION
    received_at = datetime.fromisoformat(result.received_at)
    assert received_at.tzinfo is not None
    assert received_at.utcoffset() is not None

    descriptor = transport.to_public_descriptor()
    rendered = f"{transport!r} {config!r} {result!r} {descriptor!r}"
    assert descriptor["endpoint"] == module.OFFICIAL_DEEPSEEK_CHAT_COMPLETIONS_URL
    assert descriptor["proxy_policy"] == "disabled"
    assert descriptor["redirect_policy"] == "reject"
    assert descriptor["max_attempts"] == 1
    assert descriptor["transport_id"] == module.DEEPSEEK_HTTP_TRANSPORT_ID
    assert descriptor["transport_version"] == module.DEEPSEEK_HTTP_TRANSPORT_VERSION
    assert key not in rendered
    assert str(path) not in rendered


class _VersionedSourceVerifier:
    verifier_id = "http-fixture-source-authority-verifier"
    verifier_version = "2026-07-17.v1"

    @staticmethod
    def verify(**kwargs: Any) -> bool:
        artifact = kwargs["artifact"]
        receipt = kwargs["receipt"]
        return receipt.document_sha256 == artifact.document_sha256


def _llm_request() -> Any:
    artifact_module = importlib.import_module("shared.llm.evidence_artifact")
    schema = importlib.import_module("shared.llm.schema")
    document = "公告显示合同有可核验增量，兑现时间仍存在不确定性。"
    span = "合同有可核验增量"
    start = document.index(span)
    receipt = artifact_module.EvidenceSourceAuthorityReceipt.create(
        receipt_id="source-receipt-http-001",
        source_system="official-disclosure-fixture",
        source_document_id="doc-http-001",
        document_sha256=artifact_module.sha256_document(document),
        available_at="2026-07-15T08:05:00+08:00",
        issued_at="2026-07-15T08:06:00+08:00",
    )
    artifact = artifact_module.EvidenceArtifact.create(
        document_text=document,
        published_at="2026-07-15T08:00:00+08:00",
        available_at="2026-07-15T08:05:00+08:00",
        span_start=start,
        span_end=start + len(span),
        entity_resolution_version="ashare-entity-resolution.v1",
        source_authority_receipt=receipt,
    )
    return schema.LLMEvidenceRequest.create(
        request_id="REQ-HTTP-001",
        task_type="adversarial_review",
        route="slow_research",
        prompt_template_id="general-evidence-review",
        prompt_version="bull-bear.v1",
        document_cutoff="2026-07-16T08:30:00+08:00",
        evidence_refs=(artifact.artifact_id,),
        artifacts=(artifact,),
        payload={"symbol": "600000.SH"},
    )


def _provider_envelope_for_request(request: Any) -> dict[str, object]:
    envelope = _provider_envelope()
    envelope["choices"] = [
        {
            "index": 0,
            "finish_reason": "stop",
            "message": {
                "role": "assistant",
                "content": json.dumps(
                    {
                        "bull_case": "合同事实有已验证来源",
                        "bear_case": "客户验收仍有不确定性",
                        "key_risk": "收入确认可能延后",
                        "evidence_refs": list(request.evidence_refs),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            },
        }
    ]
    return envelope


@pytest.mark.parametrize("gateway_authority", (None, True))
def test_direct_http_adapter_invoke_requires_gateway_authority_before_side_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gateway_authority: object,
) -> None:
    module = _http_module()
    gateway_module = importlib.import_module("shared.llm.gateway")
    provider_config_module = importlib.import_module("shared.llm.deepseek_config")
    request = _llm_request()
    path, _ = _write_credential(tmp_path)
    opener = _FakeOpener(_FakeResponse(_canonical_bytes(_provider_envelope())))
    transport = _transport_with_fake(
        monkeypatch,
        module,
        _config(module, path),
        opener,
    )
    credential_reads = 0

    def forbidden_credential_read(_: object) -> str:
        nonlocal credential_reads
        credential_reads += 1
        raise AssertionError("direct adapter invoke must fail before credential read")

    monkeypatch.setattr(
        module.DeepSeekCredentialFile,
        "read_secret",
        forbidden_credential_read,
    )
    provider_config = provider_config_module.DeepSeekProviderConfig.from_environment(
        {"TRADINGAGENT_LLM_NETWORK_ENABLED": "true"},
        allow_network_transport=True,
    )
    route = provider_config.router().resolve("slow_research")
    assert route is not None
    adapter = gateway_module.DeepSeekAdapter(
        transport=transport,
        source_authority_verifier=_VersionedSourceVerifier(),
    )

    with pytest.raises(
        module.DeepSeekHTTPTransportError,
        match="deepseek_http_gateway_authority_required",
    ):
        gateway_module.DeepSeekAdapter.invoke(
            adapter,
            request,
            route,
            _gateway_authority_seal=gateway_authority,
        )

    assert credential_reads == 0
    assert opener.calls == []


def test_gateway_accepts_only_exact_http_transport_and_binds_raw_http_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _http_module()
    gateway_module = importlib.import_module("shared.llm.gateway")
    provider_config_module = importlib.import_module("shared.llm.deepseek_config")
    request = _llm_request()
    raw = json.dumps(
        _provider_envelope_for_request(request),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=False,
        separators=(",", ":"),
    ).encode("utf-8")
    opener = _FakeOpener(_FakeResponse(raw))
    path, _ = _write_credential(tmp_path)
    transport = _transport_with_fake(
        monkeypatch,
        module,
        _config(module, path),
        opener,
    )
    provider_config = provider_config_module.DeepSeekProviderConfig.from_environment(
        {
            "TRADINGAGENT_LLM_NETWORK_ENABLED": "true",
        },
        allow_network_transport=True,
    )
    sidecar = gateway_module.LLMEvidenceGateway(
        router=provider_config.router(),
        adapters={
            "deepseek": gateway_module.DeepSeekAdapter(
                transport=transport,
                source_authority_verifier=_VersionedSourceVerifier(),
            )
        },
    )

    result = sidecar.analyze_with_provenance(request, entity_id="600000.SH")

    assert result.observation["status"] == "available"
    assert result.observation["authority"]
    assert all(value is False for value in result.observation["authority"].values())
    assert len(opener.calls) == 1
    assert result.transport_receipt is not None
    receipt = result.transport_receipt.to_descriptor()
    assert receipt["transport_id"] == module.DEEPSEEK_HTTP_TRANSPORT_ID
    assert receipt["transport_version"] == module.DEEPSEEK_HTTP_TRANSPORT_VERSION
    assert receipt["response_sha256"] == _sha256(raw)
    assert receipt["transport_metadata"]["endpoint"] == (
        module.OFFICIAL_DEEPSEEK_CHAT_COMPLETIONS_URL
    )
    assert receipt["transport_metadata"]["attempt_count"] == 1
    assert result.rejected_attempt_receipt is None
    journal_module = importlib.import_module("shared.llm.evidence_journal")
    envelope = journal_module.LLMEvidenceEnvelope.create(
        run_id=f"llm-run-{result.transport_receipt.receipt_sha256}",
        request=request,
        source_authority_verifier=_VersionedSourceVerifier(),
        transport_receipt=result.transport_receipt,
        observation=result.observation,
    )
    envelope.verify_integrity()
    reloaded = journal_module.LLMEvidenceEnvelope.from_payload(
        envelope.canonical_payload()
    )
    reloaded.verify_integrity()
    assert not hasattr(
        gateway_module,
        "_provider_transport_receipt_from_descriptor",
    )
    persisted_descriptor = (
        gateway_module.validate_provider_transport_receipt_descriptor(receipt)
    )
    assert not isinstance(
        persisted_descriptor,
        gateway_module.ProviderTransportReceipt,
    )
    with pytest.raises(
        gateway_module.ProviderTransportReceiptError,
        match="accepted_receipt_transport_authority_required",
    ):
        replace(result.transport_receipt)
    with pytest.raises(
        gateway_module.ProviderTransportReceiptError,
        match="gateway_analysis_authority_required",
    ):
        replace(result)
    with pytest.raises(
        gateway_module.ProviderTransportReceiptError,
        match="gateway_analysis_authority_required",
    ):
        replace(
            result,
            observation={"status": "invalid"},
        )
    tampered_output = dict(result.observation)
    tampered_output["output_sha256"] = "0" * 64
    with pytest.raises(
        gateway_module.ProviderTransportReceiptError,
        match="gateway_analysis_authority_required",
    ):
        replace(result, observation=tampered_output)
    tampered_provider = dict(result.observation)
    tampered_provider["provider"] = "other-provider"
    with pytest.raises(
        gateway_module.ProviderTransportReceiptError,
        match="gateway_analysis_authority_required",
    ):
        replace(result, observation=tampered_provider)
    tampered_evidence = dict(result.observation)
    tampered_evidence["evidence"] = {
        **tampered_evidence["evidence"],
        "bull_case": "tampered but stale output hash",
    }
    with pytest.raises(
        gateway_module.ProviderTransportReceiptError,
        match="gateway_analysis_authority_required",
    ):
        replace(result, observation=tampered_evidence)
    nested_evidence = result.observation["evidence"]
    nested_evidence["bull_case"] = "tampered"
    nested_authority = result.observation["authority"]
    nested_authority["order_eligible"] = True
    assert result.observation["evidence"]["bull_case"] != "tampered"
    assert result.observation["authority"]["order_eligible"] is False

    def _emit_tampered_observation(
        _self: object,
        _request: object,
        *,
        entity_id: str = "",
        accepted_receipt_sink: object = None,
        rejected_attempt_receipt_sink: object = None,
    ) -> dict[str, object]:
        del entity_id, rejected_attempt_receipt_sink
        assert callable(accepted_receipt_sink)
        accepted_receipt_sink(result.transport_receipt)
        return tampered_evidence

    monkeypatch.setattr(
        gateway_module.LLMEvidenceGateway,
        "_analyze",
        _emit_tampered_observation,
    )
    with pytest.raises(
        gateway_module.ProviderTransportReceiptError,
        match="gateway_accepted_observation_output_invalid",
    ):
        sidecar.analyze_with_provenance(request, entity_id="600000.SH")

    rebound_observation = dict(result.observation)
    rebound_observation["request_id"] = "REQ-FORGED-BINDING"

    def _emit_rebound_observation(
        _self: object,
        _request: object,
        *,
        entity_id: str = "",
        accepted_receipt_sink: object = None,
        rejected_attempt_receipt_sink: object = None,
    ) -> dict[str, object]:
        del entity_id, rejected_attempt_receipt_sink
        assert callable(accepted_receipt_sink)
        accepted_receipt_sink(result.transport_receipt)
        return rebound_observation

    monkeypatch.setattr(
        gateway_module.LLMEvidenceGateway,
        "_analyze",
        _emit_rebound_observation,
    )
    with pytest.raises(
        gateway_module.ProviderTransportReceiptError,
        match="gateway_observation_request_binding_invalid",
    ):
        sidecar.analyze_with_provenance(request, entity_id="600000.SH")

    extra_field_observation = dict(result.observation)
    extra_field_observation["order_eligible"] = True

    def _emit_extra_field_observation(
        _self: object,
        _request: object,
        *,
        entity_id: str = "",
        accepted_receipt_sink: object = None,
        rejected_attempt_receipt_sink: object = None,
    ) -> dict[str, object]:
        del entity_id, rejected_attempt_receipt_sink
        assert callable(accepted_receipt_sink)
        accepted_receipt_sink(result.transport_receipt)
        return extra_field_observation

    monkeypatch.setattr(
        gateway_module.LLMEvidenceGateway,
        "_analyze",
        _emit_extra_field_observation,
    )
    with pytest.raises(
        gateway_module.ProviderTransportReceiptError,
        match="gateway_observation_request_binding_invalid",
    ):
        sidecar.analyze_with_provenance(request, entity_id="600000.SH")

    self_mint_values = dict(receipt)
    self_mint_values.pop("receipt_sha256")
    with pytest.raises(
        gateway_module.ProviderTransportReceiptError,
        match="accepted_receipt_transport_authority_required",
    ):
        gateway_module.ProviderTransportReceipt.create(**self_mint_values)
    with pytest.raises(
        gateway_module.ProviderTransportReceiptError,
        match="accepted_receipt_transport_authority_required",
    ):
        gateway_module.ProviderTransportReceipt(**receipt)


def test_gateway_preserves_rejected_http_attempt_receipt_without_response_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _http_module()
    gateway_module = importlib.import_module("shared.llm.gateway")
    provider_config_module = importlib.import_module("shared.llm.deepseek_config")
    request = _llm_request()
    envelope = _provider_envelope_for_request(request)
    invalid_evidence = {
        "bull_case": "合同事实有已验证来源",
        "bear_case": "客户验收仍有不确定性",
        "key_risk": "收入确认可能延后",
        "evidence_refs": list(request.evidence_refs),
        "belief_score": 0.99,
    }
    envelope["choices"][0]["message"]["content"] = json.dumps(
        invalid_evidence,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    raw = json.dumps(
        envelope,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=False,
        separators=(",", ":"),
    ).encode("utf-8")
    opener = _FakeOpener(_FakeResponse(raw))
    path, _ = _write_credential(tmp_path)
    transport = _transport_with_fake(
        monkeypatch,
        module,
        _config(module, path),
        opener,
    )
    provider_config = provider_config_module.DeepSeekProviderConfig.from_environment(
        {"TRADINGAGENT_LLM_NETWORK_ENABLED": "true"},
        allow_network_transport=True,
    )
    sidecar = gateway_module.LLMEvidenceGateway(
        router=provider_config.router(),
        adapters={
            "deepseek": gateway_module.DeepSeekAdapter(
                transport=transport,
                source_authority_verifier=_VersionedSourceVerifier(),
            )
        },
    )

    result = sidecar.analyze_with_provenance(request, entity_id="600000.SH")

    assert result.observation["status"] == "invalid"
    assert result.observation["reason_code"] == "llm_evidence_schema_invalid"
    assert all(value is False for value in result.observation["authority"].values())
    assert result.transport_receipt is None
    rejected = getattr(result, "rejected_attempt_receipt", None)
    assert rejected is not None
    descriptor = rejected.to_descriptor()
    assert descriptor["receipt_kind"] == "provider_rejected_attempt"
    assert descriptor["outcome"] == "rejected"
    assert descriptor["rejection_stage"] == "provider_evidence_validation"
    assert descriptor["reason_code"] == "llm_evidence_schema_invalid"
    assert descriptor["evidence_accepted"] is False
    assert descriptor["evidence_journal_eligible"] is False
    assert descriptor["production_eligible"] is False
    assert descriptor["audit_only"] is True
    assert all(value is False for value in descriptor["authority"].values())
    assert descriptor["response_sha256"] == _sha256(raw)
    assert descriptor["transport_metadata"]["http_status"] == 200
    assert descriptor["transport_metadata"]["attempt_count"] == 1
    assert descriptor["transport_metadata"]["retry_disposition"] == "not_retried"
    request_bytes = opener.calls[0][0].data
    assert isinstance(request_bytes, bytes)
    assert descriptor["outbound_sha256"] == _sha256(request_bytes)
    assert descriptor["transport_metadata"]["request_bytes"] == len(request_bytes)
    assert "normalized_evidence_sha256" not in descriptor
    assert "合同事实有已验证来源" not in repr(descriptor)
    assert "客户验收仍有不确定性" not in repr(descriptor)
    assert len(opener.calls) == 1
    persisted_descriptor = (
        gateway_module.validate_provider_rejected_attempt_receipt_descriptor(descriptor)
    )
    assert not isinstance(
        persisted_descriptor,
        gateway_module.ProviderRejectedAttemptReceipt,
    )
    persisted_authority = persisted_descriptor["authority"]
    persisted_authority["order_eligible"] = True
    assert persisted_descriptor["authority"]["order_eligible"] is False
    for field_name, replacement in (
        ("receipt_sha256", "0" * 64),
        ("audit_only", False),
        ("evidence_journal_eligible", True),
        ("reason_code", "other_failure"),
    ):
        tampered_descriptor = {**descriptor, field_name: replacement}
        with pytest.raises(gateway_module.ProviderTransportReceiptError):
            gateway_module.validate_provider_rejected_attempt_receipt_descriptor(
                tampered_descriptor
            )
    journal_module = importlib.import_module("shared.llm.evidence_journal")
    audit_journal = journal_module.LLMRejectedAttemptAuditJournal(
        tmp_path / "rejected_attempts.jsonl"
    )
    empty_head = journal_module.EMPTY_LLM_EVIDENCE_JOURNAL_HEAD_SHA256
    assert (
        audit_journal.append_gateway_result(
            result,
            expected_head_sha256=empty_head,
        )
        is True
    )
    first_head = audit_journal.read().head_sha256
    assert (
        audit_journal.append_gateway_result(
            result,
            expected_head_sha256=first_head,
        )
        is False
    )
    audit_readback = audit_journal.read()
    assert len(audit_readback.events) == 1
    audit_event = audit_readback.events[0]
    assert audit_event.attempt_id == f"llm-attempt-{rejected.receipt_sha256}"
    assert audit_event.observation["status"] == "invalid"
    assert audit_event.rejected_attempt_receipt["audit_only"] is True
    assert not isinstance(
        audit_event.rejected_attempt_receipt,
        gateway_module.ProviderRejectedAttemptReceipt,
    )
    event_authority = audit_event.rejected_attempt_receipt["authority"]
    event_authority["decision_eligible"] = True
    assert (
        audit_event.rejected_attempt_receipt["authority"]["decision_eligible"] is False
    )
    audit_alias = tmp_path / "rejected_attempts-hardlink.jsonl"
    os.link(audit_journal.path, audit_alias)
    with pytest.raises(
        journal_module.LLMEvidenceJournalError,
        match="journal_hardlink_forbidden",
    ):
        audit_journal.read()
    audit_alias.unlink()
    for changes in (
        {"response_sha256": "0" * 64},
        {"audit_only": False},
        {},
    ):
        with pytest.raises(
            gateway_module.ProviderTransportReceiptError,
            match="rejected_attempt_transport_authority_required",
        ):
            replace(rejected, **changes)
    with pytest.raises(
        journal_module.LLMEvidenceEnvelopeError,
        match="transport_receipt_required",
    ):
        journal_module.LLMEvidenceEnvelope.create(
            run_id=f"llm-run-{rejected.receipt_sha256}",
            request=request,
            source_authority_verifier=_VersionedSourceVerifier(),
            transport_receipt=rejected,
            observation=result.observation,
        )
    observation_override: dict[str, object] = {"status": "available"}

    def _emit_rejected_with_override(
        _self: object,
        _request: object,
        *,
        entity_id: str = "",
        accepted_receipt_sink: object = None,
        rejected_attempt_receipt_sink: object = None,
    ) -> dict[str, object]:
        del entity_id, accepted_receipt_sink
        assert callable(rejected_attempt_receipt_sink)
        rejected_attempt_receipt_sink(rejected)
        return dict(observation_override)

    monkeypatch.setattr(
        gateway_module.LLMEvidenceGateway,
        "_analyze",
        _emit_rejected_with_override,
    )
    with pytest.raises(
        gateway_module.ProviderTransportReceiptError,
        match="gateway_rejected_receipt_status_invalid",
    ):
        sidecar.analyze_with_provenance(request, entity_id="600000.SH")
    observation_override.clear()
    observation_override.update({"status": "invalid", "reason_code": "other_failure"})
    with pytest.raises(
        gateway_module.ProviderTransportReceiptError,
        match="gateway_rejected_receipt_reason_invalid",
    ):
        sidecar.analyze_with_provenance(request, entity_id="600000.SH")
    observation_override.clear()
    observation_override.update(dict(result.observation))
    observation_override["request_id"] = "REQ-FORGED-REJECTED-BINDING"
    with pytest.raises(
        gateway_module.ProviderTransportReceiptError,
        match="gateway_observation_request_binding_invalid",
    ):
        sidecar.analyze_with_provenance(request, entity_id="600000.SH")
    observation_override.clear()
    observation_override.update(dict(result.observation))
    observation_override["provider_raw_response"] = {"unsafe": True}
    with pytest.raises(
        gateway_module.ProviderTransportReceiptError,
        match="gateway_observation_request_binding_invalid",
    ):
        sidecar.analyze_with_provenance(request, entity_id="600000.SH")


def test_provenance_recorder_persists_rejected_attempt_once_and_replays(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _http_module()
    gateway_module = importlib.import_module("shared.llm.gateway")
    journal_module = importlib.import_module("shared.llm.evidence_journal")
    provider_config_module = importlib.import_module("shared.llm.deepseek_config")
    request = _llm_request()
    envelope = _provider_envelope_for_request(request)
    envelope["choices"][0]["message"]["content"] = json.dumps(
        {
            "bull_case": "合同事实有已验证来源",
            "bear_case": "客户验收仍有不确定性",
            "key_risk": "收入确认可能延后",
            "evidence_refs": list(request.evidence_refs),
            "belief_score": 0.99,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    raw = json.dumps(
        envelope,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=False,
        separators=(",", ":"),
    ).encode("utf-8")
    opener = _FakeOpener(_FakeResponse(raw))
    credential_path, _ = _write_credential(tmp_path)
    transport = _transport_with_fake(
        monkeypatch,
        module,
        _config(module, credential_path),
        opener,
    )
    provider_config = provider_config_module.DeepSeekProviderConfig.from_environment(
        {"TRADINGAGENT_LLM_NETWORK_ENABLED": "true"},
        allow_network_transport=True,
    )
    verifier = _VersionedSourceVerifier()
    sidecar = gateway_module.LLMEvidenceGateway(
        router=provider_config.router(),
        adapters={
            "deepseek": gateway_module.DeepSeekAdapter(
                transport=transport,
                source_authority_verifier=verifier,
            )
        },
    )
    accepted_journal = journal_module.LLMEvidenceJournal(tmp_path / "accepted.jsonl")
    _, rejected_path, invocation_path = journal_module.llm_provenance_journal_paths(
        accepted_journal.path
    )
    rejected_journal = journal_module.LLMRejectedAttemptAuditJournal(rejected_path)
    invocation_journal = journal_module.LLMProviderInvocationJournal(invocation_path)
    recorder = journal_module.LLMEvidenceProvenanceRecorder(
        accepted_journal=accepted_journal,
        rejected_attempt_journal=rejected_journal,
        provider_invocation_journal=invocation_journal,
        source_authority_verifier=verifier,
    )

    first = recorder.analyze_and_persist(
        sidecar,
        request,
        entity_id="600000.SH",
    )
    replay = recorder.analyze_and_persist(
        sidecar,
        request,
        entity_id="600000.SH",
    )

    assert first["status"] == "invalid"
    assert first["reason_code"] == "llm_evidence_schema_invalid"
    assert replay == first
    assert len(opener.calls) == 1
    assert len(accepted_journal.read().events) == 0
    rejected_readback = rejected_journal.read()
    assert len(rejected_readback.events) == 1
    assert rejected_readback.events[0].observation == first
    invocation_readback = invocation_journal.read()
    assert len(invocation_readback.events) == 2
    assert invocation_readback.events[-1].state == "rejected"


def test_provenance_recorder_rejects_rotated_request_id_without_provider_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway_module = importlib.import_module("shared.llm.gateway")
    journal_module = importlib.import_module("shared.llm.evidence_journal")
    request = _llm_request()
    rotated = replace(request, request_id="REQ-ROTATED-ID")
    content = gateway_module._canonical_json(
        {
            "schema_version": rotated.schema_version,
            "request_id": rotated.request_id,
            "task_type": rotated.task_type,
            "route": rotated.route,
            "prompt_template_id": rotated.prompt_template_id,
            "prompt_version": rotated.prompt_version,
            "prompt_sha256": rotated.prompt_sha256,
            "document_cutoff": rotated.document_cutoff,
            "evidence_refs": list(rotated.evidence_refs),
            "artifact_set_sha256": rotated.artifact_set_sha256,
            "payload_sha256": rotated.payload_sha256,
        }
    )
    rotated = replace(
        rotated,
        request_content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
    )
    verifier = _VersionedSourceVerifier()
    sidecar = gateway_module.LLMEvidenceGateway(
        router=gateway_module.LLMRouter.from_offline_fixture_mapping(
            {
                "slow_research": {
                    "provider": "deepseek",
                    "model": "deepseek-v4-pro",
                }
            }
        ),
        adapters={
            "deepseek": gateway_module.DeepSeekAdapter(
                transport=None,
                source_authority_verifier=verifier,
            )
        },
    )
    accepted_journal = journal_module.LLMEvidenceJournal(tmp_path / "accepted.jsonl")
    _, rejected_path, invocation_path = journal_module.llm_provenance_journal_paths(
        accepted_journal.path
    )
    rejected_journal = journal_module.LLMRejectedAttemptAuditJournal(rejected_path)
    invocation_journal = journal_module.LLMProviderInvocationJournal(invocation_path)
    recorder = journal_module.LLMEvidenceProvenanceRecorder(
        accepted_journal=accepted_journal,
        rejected_attempt_journal=rejected_journal,
        provider_invocation_journal=invocation_journal,
        source_authority_verifier=verifier,
    )
    calls = 0

    def unavailable(
        request_value: object,
        *,
        entity_id: str = "",
    ) -> gateway_module.GatewayAnalysisResult:
        nonlocal calls
        calls += 1
        observation = gateway_module.unavailable_observation(
            request_value,
            reason_code="deepseek_adapter_unavailable",
            entity_id=entity_id,
        )
        return gateway_module.GatewayAnalysisResult(
            observation=observation,
            transport_receipt=None,
            rejected_attempt_receipt=None,
            _gateway_authority_seal=gateway_module._GATEWAY_ANALYSIS_AUTHORITY_SEAL,
            _request=request_value,
            _entity_id=entity_id,
            _source_authority_verifier=verifier,
        )

    monkeypatch.setattr(sidecar, "analyze_with_provenance", unavailable)
    first = recorder.analyze_and_persist(sidecar, request, entity_id="600000.SH")
    assert first["status"] == "unavailable"
    with pytest.raises(
        journal_module.LLMEvidenceJournalError,
        match="llm_provenance_request_id_conflict",
    ):
        recorder.analyze_and_persist(sidecar, rotated, entity_id="600000.SH")
    assert calls == 1


def test_provenance_recorder_rejects_cross_journal_path_aliases(
    tmp_path: Path,
) -> None:
    journal_module = importlib.import_module("shared.llm.evidence_journal")
    accepted = journal_module.LLMEvidenceJournal(tmp_path / "accepted.jsonl")
    rejected = journal_module.LLMRejectedAttemptAuditJournal(accepted.head_path)

    with pytest.raises(
        journal_module.LLMEvidenceJournalError,
        match="llm_provenance_journal_endpoints_must_be_distinct",
    ):
        journal_module._verify_distinct_journal_endpoints(accepted, rejected)


def test_provenance_recorder_never_retries_unknown_in_flight_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway_module = importlib.import_module("shared.llm.gateway")
    journal_module = importlib.import_module("shared.llm.evidence_journal")
    request = _llm_request()
    verifier = _VersionedSourceVerifier()
    sidecar = gateway_module.LLMEvidenceGateway(
        router=gateway_module.LLMRouter.from_offline_fixture_mapping(
            {
                "slow_research": {
                    "provider": "deepseek",
                    "model": "deepseek-v4-pro",
                }
            }
        ),
        adapters={
            "deepseek": gateway_module.DeepSeekAdapter(
                transport=None,
                source_authority_verifier=verifier,
            )
        },
    )
    accepted = journal_module.LLMEvidenceJournal(tmp_path / "accepted.jsonl")
    _, rejected_path, invocation_path = journal_module.llm_provenance_journal_paths(
        accepted.path
    )
    rejected = journal_module.LLMRejectedAttemptAuditJournal(rejected_path)
    invocation = journal_module.LLMProviderInvocationJournal(invocation_path)
    recorder = journal_module.LLMEvidenceProvenanceRecorder(
        accepted_journal=accepted,
        rejected_attempt_journal=rejected,
        provider_invocation_journal=invocation,
        source_authority_verifier=verifier,
    )
    calls = 0

    def fail_after_claim(*_: object, **__: object) -> object:
        nonlocal calls
        calls += 1
        raise RuntimeError("simulated provider outcome loss")

    monkeypatch.setattr(sidecar, "analyze_with_provenance", fail_after_claim)
    with pytest.raises(
        journal_module.LLMEvidenceJournalError,
        match="llm_provenance_provider_outcome_unknown",
    ):
        recorder.analyze_and_persist(sidecar, request, entity_id="600000.SH")
    assert len(invocation.read().events) == 1
    assert invocation.read().events[0].state == "in_flight"

    with pytest.raises(
        journal_module.LLMEvidenceJournalError,
        match="llm_provenance_invocation_in_flight_unknown",
    ):
        recorder.analyze_and_persist(sidecar, request, entity_id="600000.SH")
    assert calls == 1
    assert len(accepted.read().events) == 0
    assert len(rejected.read().events) == 0


def test_gateway_binding_failure_converts_https_success_receipt_to_rejected_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _http_module()
    gateway_module = importlib.import_module("shared.llm.gateway")
    provider_config_module = importlib.import_module("shared.llm.deepseek_config")
    request = _llm_request()
    raw = json.dumps(
        _provider_envelope_for_request(request),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=False,
        separators=(",", ":"),
    ).encode("utf-8")
    opener = _FakeOpener(_FakeResponse(raw))
    path, _ = _write_credential(tmp_path)
    transport = _transport_with_fake(
        monkeypatch,
        module,
        _config(module, path),
        opener,
    )
    provider_config = provider_config_module.DeepSeekProviderConfig.from_environment(
        {"TRADINGAGENT_LLM_NETWORK_ENABLED": "true"},
        allow_network_transport=True,
    )
    sidecar = gateway_module.LLMEvidenceGateway(
        router=provider_config.router(),
        adapters={
            "deepseek": gateway_module.DeepSeekAdapter(
                transport=transport,
                source_authority_verifier=_VersionedSourceVerifier(),
            )
        },
    )

    def _reject_binding(*_args: object, **_kwargs: object) -> object:
        raise gateway_module.EvidenceSchemaError("forced_gateway_binding_failure")

    monkeypatch.setattr(gateway_module, "available_observation", _reject_binding)

    result = sidecar.analyze_with_provenance(request, entity_id="600000.SH")

    assert result.observation["status"] == "invalid"
    assert result.observation["reason_code"] == "llm_evidence_schema_invalid"
    assert result.transport_receipt is None
    assert result.rejected_attempt_receipt is not None
    descriptor = result.rejected_attempt_receipt.to_descriptor()
    assert descriptor["rejection_stage"] == "gateway_observation_binding"
    assert descriptor["response_sha256"] == _sha256(raw)
    assert "normalized_evidence_sha256" not in descriptor
    assert "provider_response_id" not in descriptor
    assert len(opener.calls) == 1


def test_gateway_analysis_result_rejects_accepted_and_rejected_receipts_together() -> (
    None
):
    gateway_module = importlib.import_module("shared.llm.gateway")

    with pytest.raises(
        gateway_module.ProviderTransportReceiptError,
        match="gateway_receipt_outcome_ambiguous",
    ):
        gateway_module.GatewayAnalysisResult(
            observation={"status": "available"},
            transport_receipt=object(),
            rejected_attempt_receipt=object(),
        )


def test_gateway_analysis_result_with_receipt_requires_internal_gateway_authority() -> (
    None
):
    gateway_module = importlib.import_module("shared.llm.gateway")

    with pytest.raises(
        gateway_module.ProviderTransportReceiptError,
        match="gateway_analysis_authority_required",
    ):
        gateway_module.GatewayAnalysisResult(
            observation={"status": "available"},
            transport_receipt=object(),
        )


def test_gateway_analysis_result_freezes_observation_snapshot() -> None:
    gateway_module = importlib.import_module("shared.llm.gateway")
    observation = {"status": "unavailable"}

    result = gateway_module.GatewayAnalysisResult(
        observation=observation,
        transport_receipt=None,
    )
    observation["status"] = "available"

    assert result.observation["status"] == "unavailable"
    with pytest.raises(TypeError):
        result.observation["status"] = "invalid"
    with pytest.raises(
        gateway_module.ProviderTransportReceiptError,
        match="gateway_available_receipt_required",
    ):
        gateway_module.GatewayAnalysisResult(
            observation={"status": "available"},
            transport_receipt=None,
        )


def test_rejected_attempt_receipt_cannot_be_self_minted_as_https_evidence() -> None:
    module = _http_module()
    gateway_module = importlib.import_module("shared.llm.gateway")

    with pytest.raises(
        gateway_module.ProviderTransportReceiptError,
        match="rejected_attempt_transport_authority_required",
    ):
        gateway_module.ProviderRejectedAttemptReceipt.create(
            provider="deepseek",
            model="deepseek-v4-pro",
            transport_id=module.DEEPSEEK_HTTP_TRANSPORT_ID,
            transport_version=module.DEEPSEEK_HTTP_TRANSPORT_VERSION,
            verified_at="2026-07-17T08:20:00+08:00",
            received_at="2026-07-17T08:20:01+08:00",
            request_sha256="a" * 64,
            source_authority_proof_set_sha256="b" * 64,
            transport_material_sha256="c" * 64,
            outbound_sha256="d" * 64,
            response_sha256="e" * 64,
            transport_metadata={
                "kind": "https",
                "endpoint": module.OFFICIAL_DEEPSEEK_CHAT_COMPLETIONS_URL,
                "method": "POST",
                "egress_policy_version": module.DEEPSEEK_EGRESS_POLICY_VERSION,
                "http_status": 200,
                "content_type": "application/json",
                "request_bytes": 128,
                "response_bytes": 256,
                "attempt_count": 1,
                "retry_disposition": "not_retried",
            },
        )
    with pytest.raises(
        gateway_module.ProviderTransportReceiptError,
        match="rejected_attempt_transport_authority_required",
    ):
        gateway_module.ProviderRejectedAttemptReceipt(
            schema_version=("tradingagent.llm_provider_rejected_attempt_receipt.v1"),
            receipt_kind="provider_rejected_attempt",
            outcome="rejected",
            rejection_stage="provider_evidence_validation",
            reason_code="llm_evidence_schema_invalid",
            provider="deepseek",
            model="deepseek-v4-pro",
            transport_id=module.DEEPSEEK_HTTP_TRANSPORT_ID,
            transport_version=module.DEEPSEEK_HTTP_TRANSPORT_VERSION,
            verified_at="2026-07-17T08:20:00+08:00",
            received_at="2026-07-17T08:20:01+08:00",
            request_sha256="a" * 64,
            source_authority_proof_set_sha256="b" * 64,
            transport_material_sha256="c" * 64,
            outbound_sha256="d" * 64,
            response_sha256="e" * 64,
            transport_metadata={
                "kind": "https",
                "endpoint": module.OFFICIAL_DEEPSEEK_CHAT_COMPLETIONS_URL,
                "method": "POST",
                "egress_policy_version": module.DEEPSEEK_EGRESS_POLICY_VERSION,
                "http_status": 200,
                "content_type": "application/json",
                "request_bytes": 128,
                "response_bytes": 256,
                "attempt_count": 1,
                "retry_disposition": "not_retried",
            },
            evidence_accepted=False,
            evidence_journal_eligible=False,
            production_eligible=False,
            audit_only=True,
            authority=dict(gateway_module.AUTHORITY_DENIED),
            receipt_sha256="f" * 64,
        )


def test_gateway_fails_closed_if_internal_path_emits_multiple_receipts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway_module = importlib.import_module("shared.llm.gateway")
    sidecar = gateway_module.LLMEvidenceGateway()

    def _emit_multiple(
        _self: object,
        _request: object,
        *,
        entity_id: str = "",
        accepted_receipt_sink: object = None,
        rejected_attempt_receipt_sink: object = None,
    ) -> dict[str, object]:
        del entity_id, accepted_receipt_sink
        assert callable(rejected_attempt_receipt_sink)
        rejected_attempt_receipt_sink(object())
        rejected_attempt_receipt_sink(object())
        return {"status": "invalid", "reason_code": "llm_evidence_schema_invalid"}

    monkeypatch.setattr(gateway_module.LLMEvidenceGateway, "_analyze", _emit_multiple)

    with pytest.raises(
        gateway_module.ProviderTransportReceiptError,
        match="gateway_receipt_cardinality_invalid",
    ):
        sidecar.analyze_with_provenance(_llm_request())


def test_fixture_router_rejects_http_transport_before_network_or_secret_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _http_module()
    gateway_module = importlib.import_module("shared.llm.gateway")
    router_module = importlib.import_module("shared.llm.router")
    request = _llm_request()
    path, _ = _write_credential(tmp_path)
    opener = _FakeOpener(_FakeResponse(_canonical_bytes(_provider_envelope())))
    transport = _transport_with_fake(
        monkeypatch,
        module,
        _config(module, path),
        opener,
    )
    sidecar = gateway_module.LLMEvidenceGateway(
        router=router_module.LLMRouter.from_offline_fixture_mapping(
            {
                "slow_research": {
                    "provider": "deepseek",
                    "model": "deepseek-v4-pro",
                }
            }
        ),
        adapters={
            "deepseek": gateway_module.DeepSeekAdapter(
                transport=transport,
                source_authority_verifier=_VersionedSourceVerifier(),
            )
        },
    )

    observation = sidecar.analyze(request)

    assert observation["status"] == "unavailable"
    assert observation["reason_code"] == "llm_network_router_policy_rejected"
    assert opener.calls == []


def test_network_disabled_provider_router_rejects_http_before_secret_or_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _http_module()
    gateway_module = importlib.import_module("shared.llm.gateway")
    provider_config_module = importlib.import_module("shared.llm.deepseek_config")
    request = _llm_request()
    path, _ = _write_credential(tmp_path)
    opener = _FakeOpener(_FakeResponse(_canonical_bytes(_provider_envelope())))
    transport = _transport_with_fake(
        monkeypatch,
        module,
        _config(module, path),
        opener,
    )
    credential_reads = 0

    def forbidden_credential_read(_: object) -> str:
        nonlocal credential_reads
        credential_reads += 1
        raise AssertionError("network-disabled provider config must block secret read")

    monkeypatch.setattr(
        module.DeepSeekCredentialFile,
        "read_secret",
        forbidden_credential_read,
    )
    provider_config = provider_config_module.DeepSeekProviderConfig.from_environment(
        {"TRADINGAGENT_LLM_NETWORK_ENABLED": "false"}
    )
    disabled_router = provider_config.router()
    object.__setattr__(disabled_router, "_network_authority_seal", True)
    sidecar = gateway_module.LLMEvidenceGateway(
        router=disabled_router,
        adapters={
            "deepseek": gateway_module.DeepSeekAdapter(
                transport=transport,
                source_authority_verifier=_VersionedSourceVerifier(),
            )
        },
    )

    observation = sidecar.analyze(request)

    assert provider_config.network_enabled is False
    assert disabled_router.network_authorized is False
    assert observation["status"] == "unavailable"
    assert observation["reason_code"] == "llm_network_router_policy_rejected"
    assert credential_reads == 0
    assert opener.calls == []


def test_mutated_provider_config_cannot_mint_http_router_or_read_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _http_module()
    provider_config_module = importlib.import_module("shared.llm.deepseek_config")
    path, _ = _write_credential(tmp_path)
    opener = _FakeOpener(_FakeResponse(_canonical_bytes(_provider_envelope())))
    _transport_with_fake(
        monkeypatch,
        module,
        _config(module, path),
        opener,
    )
    credential_reads = 0

    def forbidden_credential_read(_: object) -> str:
        nonlocal credential_reads
        credential_reads += 1
        raise AssertionError("mutated provider config must fail before secret read")

    monkeypatch.setattr(
        module.DeepSeekCredentialFile,
        "read_secret",
        forbidden_credential_read,
    )
    provider_config = provider_config_module.DeepSeekProviderConfig.from_environment(
        {"TRADINGAGENT_LLM_NETWORK_ENABLED": "false"}
    )
    object.__setattr__(provider_config, "network_enabled", True)

    with pytest.raises(
        provider_config_module.DeepSeekProviderConfigError,
        match="network_authority_state_invalid",
    ):
        provider_config.router()

    assert credential_reads == 0
    assert opener.calls == []


def test_gateway_rejects_mutated_sensitive_payload_before_http_side_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _http_module()
    gateway_module = importlib.import_module("shared.llm.gateway")
    provider_config_module = importlib.import_module("shared.llm.deepseek_config")
    request = _llm_request()
    object.__setattr__(
        request,
        "payload",
        {"api_key": "must-never-reach-the-http-transport"},
    )
    path, _ = _write_credential(tmp_path)
    opener = _FakeOpener(_FakeResponse(_canonical_bytes(_provider_envelope())))
    transport = _transport_with_fake(
        monkeypatch,
        module,
        _config(module, path),
        opener,
    )
    credential_reads = 0

    def forbidden_sensitive_payload_credential_read(_: object) -> str:
        nonlocal credential_reads
        credential_reads += 1
        raise AssertionError("sensitive payload must fail before credential read")

    monkeypatch.setattr(
        module.DeepSeekCredentialFile,
        "read_secret",
        forbidden_sensitive_payload_credential_read,
    )
    provider_config = provider_config_module.DeepSeekProviderConfig.from_environment(
        {"TRADINGAGENT_LLM_NETWORK_ENABLED": "true"},
        allow_network_transport=True,
    )
    sidecar = gateway_module.LLMEvidenceGateway(
        router=provider_config.router(),
        adapters={
            "deepseek": gateway_module.DeepSeekAdapter(
                transport=transport,
                source_authority_verifier=_VersionedSourceVerifier(),
            )
        },
    )

    observation = sidecar.analyze(request)

    assert observation["status"] == "invalid"
    assert observation["reason_code"] == "llm_request_egress_rejected"
    assert opener.calls == []
    assert credential_reads == 0
    assert "must-never-reach" not in repr(observation)


@pytest.mark.parametrize(
    "override",
    (
        {"provider": "other-provider"},
        {"model": "other-model"},
        {"transport_id": "offline-id-claiming-https"},
        {"transport_version": "unverified-version"},
        {"egress_policy_version": "unverified-policy"},
        {"request_bytes": 0},
        {"response_bytes": 0},
    ),
)
def test_https_receipt_rejects_cross_field_transport_fact_contradictions(
    override: dict[str, object],
) -> None:
    module = _http_module()
    gateway_module = importlib.import_module("shared.llm.gateway")
    values: dict[str, object] = {
        "provider": "deepseek",
        "model": "deepseek-v4-pro",
        "transport_id": module.DEEPSEEK_HTTP_TRANSPORT_ID,
        "transport_version": module.DEEPSEEK_HTTP_TRANSPORT_VERSION,
        "verified_at": "2026-07-17T08:20:00+08:00",
        "request_sha256": "a" * 64,
        "source_authority_proof_set_sha256": "b" * 64,
        "transport_material_sha256": "c" * 64,
        "outbound_sha256": "d" * 64,
        "response_sha256": "e" * 64,
        "normalized_evidence_sha256": "f" * 64,
        "provider_response_id": "fixture-response-001",
        "received_at": "2026-07-17T08:20:01+08:00",
    }
    metadata: dict[str, object] = {
        "kind": "https",
        "endpoint": module.OFFICIAL_DEEPSEEK_CHAT_COMPLETIONS_URL,
        "method": "POST",
        "egress_policy_version": module.DEEPSEEK_EGRESS_POLICY_VERSION,
        "http_status": 200,
        "content_type": "application/json",
        "request_bytes": 128,
        "response_bytes": 256,
        "attempt_count": 1,
        "retry_disposition": "not_retried",
    }
    if "egress_policy_version" in override:
        metadata.update(override)
    elif "request_bytes" in override or "response_bytes" in override:
        metadata.update(override)
    else:
        values.update(override)
    values["transport_metadata"] = metadata

    with pytest.raises(
        gateway_module.ProviderTransportReceiptError,
        match="https_transport_receipt_binding_invalid",
    ):
        gateway_module.ProviderTransportReceipt.create(**values)


def test_offline_receipt_cannot_claim_https_transport_identity() -> None:
    module = _http_module()
    gateway_module = importlib.import_module("shared.llm.gateway")

    with pytest.raises(
        gateway_module.ProviderTransportReceiptError,
        match="offline_transport_receipt_binding_invalid",
    ):
        gateway_module.ProviderTransportReceipt.create(
            provider="deepseek",
            model="deepseek-v4-pro",
            transport_id=module.DEEPSEEK_HTTP_TRANSPORT_ID,
            transport_version=module.DEEPSEEK_HTTP_TRANSPORT_VERSION,
            verified_at="2026-07-17T08:20:00+08:00",
            request_sha256="a" * 64,
            source_authority_proof_set_sha256="b" * 64,
            transport_material_sha256="c" * 64,
            outbound_sha256="d" * 64,
            response_sha256="e" * 64,
            normalized_evidence_sha256="f" * 64,
            provider_response_id="fixture-response-001",
            received_at="2026-07-17T08:20:01+08:00",
            transport_metadata={
                "kind": "offline_fixture",
                "endpoint": "offline://deepseek-fixture",
                "method": "FIXTURE_RESOLVE",
                "egress_policy_version": "offline-fixture-v1",
                "http_status": 0,
                "content_type": "application/json",
                "request_bytes": 128,
                "response_bytes": 256,
                "attempt_count": 1,
                "retry_disposition": "not_applicable",
            },
        )
