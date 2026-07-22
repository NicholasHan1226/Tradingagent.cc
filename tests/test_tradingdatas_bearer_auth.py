from __future__ import annotations

import io
import json
import os
from collections.abc import Iterator, Mapping
from email.message import Message
from pathlib import Path
import secrets
import threading
from typing import Any
import urllib.error
import urllib.request

import pytest

import shared.data.tradingdatas_auth as tradingdatas_auth
from shared.data.tradingdatas_auth import (
    TradingDatasBearerToken,
    TradingDatasTokenFile,
    TradingDatasTokenFileError,
)
from shared.data.sharedsignals_v1 import ContractViolation
from shared.runtime_test.sharedsignals_v1_gate import (
    RuntimeGateConfigurationError,
    TradingDatasAuthenticationError,
    TradingDatasV1RuntimeGateConfig,
    build_runtime_transport,
    check_v1_runtime_gate,
)


CATALOG_VERSION = "catalog-auth-fixture-v1"
DATASET_ID = "fixture.cn.equity.daily"
BASE_URL = "https://tradingdatas.fixture.invalid"


@pytest.fixture(autouse=True)
def _use_isolated_test_secret_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        tradingdatas_auth,
        "_service_secret_roots",
        lambda: (tmp_path,),
    )


def _write_token(path: Path, *, mode: int = 0o600, raw: bytes | None = None) -> bytes:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    token = raw if raw is not None else secrets.token_urlsafe(48).encode("ascii")
    path.write_bytes(token)
    path.chmod(mode)
    return token


class _FakeResponse:
    status = 200

    def __init__(self, payload: dict[str, Any]) -> None:
        self._raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, _limit: int) -> bytes:
        return self._raw


class _ForbiddenReadResponse(_FakeResponse):
    def __init__(self, status: int) -> None:
        super().__init__({"secret": "must-not-be-read"})
        self.status = status
        self.read_called = False

    def read(self, _limit: int) -> bytes:
        self.read_called = True
        raise AssertionError("HTTP auth response body must not be read")


class _RecordingOpener:
    def __init__(
        self,
        outcomes: list[object],
        *,
        expected_authorization: str | None = None,
    ) -> None:
        self._outcomes = list(outcomes)
        self._expected_authorization = expected_authorization
        self.calls: list[dict[str, object]] = []
        self.requests: list[urllib.request.Request] = []

    def open(
        self,
        request: urllib.request.Request,
        *,
        timeout: float,
    ) -> _FakeResponse:
        authorization = request.get_header("Authorization")
        self.requests.append(request)
        call: dict[str, object] = {
            "url": request.full_url,
            "method": request.get_method(),
            "authorization_present": isinstance(authorization, str)
            and authorization.startswith("Bearer "),
            "timeout": timeout,
        }
        if self._expected_authorization is not None:
            call["authorization_exact_match"] = bool(
                isinstance(authorization, str)
                and secrets.compare_digest(
                    authorization,
                    self._expected_authorization,
                )
            )
        self.calls.append(call)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        assert isinstance(outcome, _FakeResponse)
        return outcome


class _BlockingOpener(_RecordingOpener):
    def __init__(self, outcomes: list[object]) -> None:
        super().__init__(outcomes)
        self.started = threading.Event()
        self.release = threading.Event()

    def open(
        self,
        request: urllib.request.Request,
        *,
        timeout: float,
    ) -> _FakeResponse:
        self.started.set()
        if not self.release.wait(timeout=2.0):
            raise AssertionError("test did not release the blocked request")
        return super().open(request, timeout=timeout)


class _ForbiddenReadBody(io.BytesIO):
    def __init__(self, payload: bytes) -> None:
        super().__init__(payload)
        self.read_called = False

    def read(self, *_args: object, **_kwargs: object) -> bytes:
        self.read_called = True
        raise AssertionError("HTTP auth error body must not be read")


class _ChangingHeaderMapping(Mapping[str, str]):
    """Expose Host only on the third iteration to reproduce multi-read TOCTOU."""

    def __init__(self) -> None:
        self.iterations = 0

    def __iter__(self) -> Iterator[str]:
        self.iterations += 1
        keys = ("Accept",) if self.iterations <= 2 else ("Accept", "Host")
        return iter(keys)

    def __len__(self) -> int:
        return 1 if self.iterations <= 2 else 2

    def __getitem__(self, key: str) -> str:
        if key == "Accept":
            return "application/json"
        if key == "Host":
            return "evil.internal"
        raise KeyError(key)


class _MasqueradingString(str):
    def __new__(cls, actual: str, masquerades_as: str) -> _MasqueradingString:
        instance = super().__new__(cls, actual)
        instance._masquerades_as = masquerades_as
        return instance

    def __hash__(self) -> int:
        return hash(self._masquerades_as)

    def __eq__(self, other: object) -> bool:
        return other == self._masquerades_as


def _catalog_payload() -> dict[str, object]:
    return {
        "api_version": "v1",
        "catalog_version": CATALOG_VERSION,
        "request_id": "catalog-auth-request",
        "data": [{"dataset_id": DATASET_ID}],
    }


def _config(token_path: Path) -> TradingDatasV1RuntimeGateConfig:
    return TradingDatasV1RuntimeGateConfig(
        base_url=BASE_URL,
        catalog_version=CATALOG_VERSION,
        dataset_ids=(DATASET_ID,),
        schema_major=2,
        access_policy_id="ta-read-auth-fixture",
        transport_id="http-json-v1",
        timeout_seconds=3.0,
        token_file=token_path,
    )


def test_token_file_returns_only_a_redacted_token_object(tmp_path: Path) -> None:
    token_path = tmp_path / "service" / "ta.token"
    token = _write_token(token_path)

    loaded = TradingDatasTokenFile(token_path).read_token()

    assert type(loaded) is TradingDatasBearerToken
    rendered = f"{loaded!r} {loaded!s} {TradingDatasTokenFile(token_path)!r}"
    assert token.decode("ascii") not in rendered
    assert str(token_path) not in repr(TradingDatasTokenFile(token_path))


@pytest.mark.parametrize(
    ("case", "expected_reason"),
    (
        ("missing", "tradingdatas_token_missing"),
        ("empty", "tradingdatas_token_size_invalid"),
        ("bad_mode_0400", "tradingdatas_token_mode_invalid"),
        ("bad_mode_0640", "tradingdatas_token_mode_invalid"),
        ("bad_mode_0644", "tradingdatas_token_mode_invalid"),
        ("bad_mode_0660", "tradingdatas_token_mode_invalid"),
        ("directory", "tradingdatas_token_regular_file_required"),
        ("fifo", "tradingdatas_token_regular_file_required"),
        ("hardlink", "tradingdatas_token_regular_file_required"),
        ("symlink", "tradingdatas_token_symlink_forbidden"),
        ("multiline", "tradingdatas_token_format_invalid"),
        ("nul", "tradingdatas_token_format_invalid"),
        ("non_ascii", "tradingdatas_token_format_invalid"),
        ("env_style", "tradingdatas_token_format_invalid"),
        ("oversize", "tradingdatas_token_size_invalid"),
    ),
)
def test_token_file_fail_closed_security_matrix(
    tmp_path: Path,
    case: str,
    expected_reason: str,
) -> None:
    token_path = tmp_path / "service" / "ta.token"
    if case == "missing":
        token_path.parent.mkdir(mode=0o700, parents=True)
    elif case == "empty":
        _write_token(token_path, raw=b"")
    elif case.startswith("bad_mode_"):
        _write_token(token_path, mode=int(case.removeprefix("bad_mode_"), 8))
    elif case == "directory":
        token_path.mkdir(mode=0o700, parents=True)
    elif case == "fifo":
        token_path.parent.mkdir(mode=0o700, parents=True)
        os.mkfifo(token_path, mode=0o600)
    elif case == "hardlink":
        original = tmp_path / "service" / "original.token"
        _write_token(original)
        os.link(original, token_path)
    elif case == "symlink":
        original = tmp_path / "service" / "original.token"
        _write_token(original)
        token_path.symlink_to(original)
    elif case == "multiline":
        _write_token(token_path, raw=b"first-line\nsecond-line")
    elif case == "nul":
        _write_token(token_path, raw=b"token-value\x00suffix")
    elif case == "non_ascii":
        _write_token(token_path, raw="令牌".encode("utf-8"))
    elif case == "env_style":
        _write_token(token_path, raw=b"TRADINGDATAS_API_TOKEN=value")
    elif case == "oversize":
        _write_token(token_path, raw=b"x" * 4_097)

    with pytest.raises(TradingDatasTokenFileError) as caught:
        TradingDatasTokenFile(token_path).read_token()

    assert caught.value.reason_code == expected_reason
    rendered = f"{caught.value!r} {caught.value!s}"
    assert str(token_path) not in rendered


def test_token_file_rejects_symlinked_parent(tmp_path: Path) -> None:
    trusted = tmp_path / "trusted"
    token_path = trusted / "ta.token"
    _write_token(token_path)
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(trusted, target_is_directory=True)

    with pytest.raises(TradingDatasTokenFileError) as caught:
        TradingDatasTokenFile(linked_parent / "ta.token").read_token()

    assert caught.value.reason_code == "tradingdatas_token_parent_untrusted"


def test_token_file_rejects_untrusted_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_path = tmp_path / "service" / "ta.token"
    _write_token(token_path)
    metadata = token_path.stat()
    monkeypatch.setattr(
        "shared.data.tradingdatas_auth._trusted_owner_uids",
        lambda: frozenset({metadata.st_uid + 1}),
    )

    with pytest.raises(TradingDatasTokenFileError) as caught:
        TradingDatasTokenFile(token_path).read_token()

    assert caught.value.reason_code == "tradingdatas_token_owner_invalid"


def test_token_file_requires_an_absolute_canonical_path() -> None:
    for raw_path in ("relative.token", "/tmp/../tmp/ta.token"):
        with pytest.raises(TradingDatasTokenFileError) as caught:
            TradingDatasTokenFile(raw_path)
        assert caught.value.reason_code == "tradingdatas_token_path_invalid"


@pytest.mark.parametrize(
    "aliased_path",
    (
        "/RUN/secrets/tradingagent/ta.token",
        "//run/secrets/tradingagent/ta.token",
        "/run/SECRETS/tradingagent/ta.token",
    ),
)
def test_service_secret_root_rejects_lexical_and_case_aliases(
    aliased_path: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        tradingdatas_auth,
        "_service_secret_roots",
        lambda: (Path("/run/secrets/tradingagent"),),
    )

    with pytest.raises(TradingDatasTokenFileError) as caught:
        TradingDatasTokenFile(aliased_path)

    assert caught.value.reason_code == "tradingdatas_token_service_root_required"


def test_token_file_rejects_path_outside_frozen_service_secret_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service_root = tmp_path / "service-secrets"
    token_path = tmp_path / "checkout" / "private" / "ta.token"
    _write_token(token_path)
    monkeypatch.setattr(
        tradingdatas_auth,
        "_service_secret_roots",
        lambda: (service_root,),
    )

    with pytest.raises(TradingDatasTokenFileError) as caught:
        TradingDatasTokenFile(token_path)

    assert caught.value.reason_code == "tradingdatas_token_service_root_required"
    assert str(token_path) not in str(caught.value)


def test_production_service_secret_root_is_frozen_outside_checkout() -> None:
    checkout = Path(__file__).resolve().parents[1]

    assert tradingdatas_auth._SERVICE_SECRET_ROOT == Path("/run/secrets/tradingagent")
    assert not tradingdatas_auth._SERVICE_SECRET_ROOT.is_relative_to(checkout)


def test_missing_or_invalid_token_fails_before_opener_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opener_created = False

    def forbidden_opener(*_handlers: object) -> object:
        nonlocal opener_created
        opener_created = True
        raise AssertionError("network opener must not be created")

    monkeypatch.setattr(urllib.request, "build_opener", forbidden_opener)
    missing = tmp_path / "service" / "missing.token"
    missing.parent.mkdir(mode=0o700)

    with pytest.raises(RuntimeGateConfigurationError) as caught:
        build_runtime_transport(
            "http-json-v1",
            token_file=missing,
            base_url=BASE_URL,
        )

    assert "tradingdatas_token_missing" in str(caught.value)
    assert str(missing) not in str(caught.value)
    assert opener_created is False


def test_invalid_or_userinfo_base_url_fails_before_token_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_path = tmp_path / "service" / "ta.token"
    _write_token(token_path)
    token_read = False

    def forbidden_read(_self: TradingDatasTokenFile) -> TradingDatasBearerToken:
        nonlocal token_read
        token_read = True
        raise AssertionError("credential must not be read")

    monkeypatch.setattr(TradingDatasTokenFile, "read_token", forbidden_read)

    for base_url in (
        "not-an-absolute-url",
        "https://user:password@tradingdatas.fixture.invalid",
        "https://tradingdatas.fixture.invalid/internal",
        "https://tradingdatas.fixture.invalid/",
        "https://tradingdatas.fixture.invalid?",
        "https://tradingdatas.fixture.invalid#",
        "https://tradingdatas.fixture.invalid/?",
        "https://tradingdatas.fixture.invalid/#",
        "https://tradingdatas.fixture.invalid\\other",
        "https://tradingdatas.fixture.invalid\n",
        "http://tradingdatas.fixture.invalid",
        "http://10.0.0.8:18082",
        "http://localhost:18082",
    ):
        with pytest.raises(RuntimeGateConfigurationError):
            build_runtime_transport(
                "http-json-v1",
                token_file=token_path,
                base_url=base_url,
            )

    assert token_read is False


def test_base_url_requires_an_exact_native_string_before_token_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_path = tmp_path / "service" / "ta.token"
    _write_token(token_path)
    token_read = False

    def forbidden_read(_self: TradingDatasTokenFile) -> TradingDatasBearerToken:
        nonlocal token_read
        token_read = True
        raise AssertionError("credential must not be read")

    monkeypatch.setattr(TradingDatasTokenFile, "read_token", forbidden_read)
    disguised = _MasqueradingString(BASE_URL, BASE_URL)

    with pytest.raises(RuntimeGateConfigurationError, match="canonical authority"):
        build_runtime_transport(
            "http-json-v1",
            token_file=token_path,
            base_url=disguised,
        )

    assert token_read is False


@pytest.mark.parametrize(
    "base_url",
    (
        "http://127.0.0.1:18082",
        "http://127.0.0.1:18085",
        "http://[::1]:18082",
    ),
)
def test_plaintext_transport_is_limited_to_loopback_ip_literals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    base_url: str,
) -> None:
    token_path = tmp_path / "service" / "ta.token"
    _write_token(token_path)
    opener = _RecordingOpener([_FakeResponse(_catalog_payload())])
    monkeypatch.setattr(urllib.request, "build_opener", lambda *_handlers: opener)

    transport = build_runtime_transport(
        "http-json-v1",
        token_file=token_path,
        base_url=base_url,
    )
    response = transport(
        method="GET",
        url=f"{base_url}/v1/catalog",
        headers={"Accept": "application/json"},
        json_body=None,
        timeout_seconds=3.0,
    )

    assert response.status_code == 200
    assert opener.calls[0]["url"] == f"{base_url}/v1/catalog"
    assert opener.requests[0].selector == "/v1/catalog"


def test_shadow_18085_transport_never_falls_back_to_legacy_18082(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_path = tmp_path / "service" / "ta.token"
    _write_token(token_path)
    opener = _RecordingOpener([_FakeResponse(_catalog_payload())])
    monkeypatch.setattr(urllib.request, "build_opener", lambda *_handlers: opener)
    transport = build_runtime_transport(
        "http-json-v1",
        token_file=token_path,
        base_url="http://127.0.0.1:18085",
    )

    with pytest.raises(ContractViolation, match="unbound request target"):
        transport(
            method="GET",
            url="http://127.0.0.1:18082/v1/catalog",
            headers={"Accept": "application/json"},
            json_body=None,
            timeout_seconds=3.0,
        )

    assert opener.calls == []
    assert opener.requests == []


def test_secure_open_capability_absence_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_path = tmp_path / "service" / "ta.token"
    _write_token(token_path)
    monkeypatch.setattr("shared.data.tradingdatas_auth.os.O_NOFOLLOW", 0)

    with pytest.raises(TradingDatasTokenFileError) as caught:
        TradingDatasTokenFile(token_path).read_token()

    assert caught.value.reason_code == "tradingdatas_token_secure_open_unsupported"


def test_token_file_change_during_read_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_path = tmp_path / "service" / "ta.token"
    _write_token(token_path, raw=b"original-token-material")
    original_read = os.read
    changed = False

    def mutating_read(descriptor: int, count: int) -> bytes:
        nonlocal changed
        chunk = original_read(descriptor, count)
        if chunk and not changed:
            changed = True
            token_path.write_bytes(b"replacement-token-value")
            token_path.chmod(0o600)
        return chunk

    monkeypatch.setattr("shared.data.tradingdatas_auth.os.read", mutating_read)

    with pytest.raises(TradingDatasTokenFileError) as caught:
        TradingDatasTokenFile(token_path).read_token()

    assert caught.value.reason_code == "tradingdatas_token_changed_during_read"


def test_urllib_transport_adds_bearer_to_catalog_and_query_without_exposing_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_path = tmp_path / "service" / "ta.token"
    token = _write_token(token_path)
    opener = _RecordingOpener(
        [
            _FakeResponse(_catalog_payload()),
            _FakeResponse({"status": "ignored-by-wire-test"}),
        ],
        expected_authorization=f"Bearer {token.decode('ascii')}",
    )
    monkeypatch.setattr(urllib.request, "build_opener", lambda *_handlers: opener)
    transport = build_runtime_transport(
        "http-json-v1",
        token_file=token_path,
        base_url=BASE_URL,
    )

    transport(
        method="GET",
        url=f"{BASE_URL}/v1/catalog",
        headers={"Accept": "application/json"},
        json_body=None,
        timeout_seconds=3.0,
    )
    transport(
        method="POST",
        url=f"{BASE_URL}/v1/query",
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        json_body={"dataset_id": DATASET_ID, "schema_major": 2},
        timeout_seconds=3.0,
    )

    assert [call["authorization_present"] for call in opener.calls] == [True, True]
    assert [call["authorization_exact_match"] for call in opener.calls] == [
        True,
        True,
    ]
    assert [request.get_header("Authorization") for request in opener.requests] == [
        None,
        None,
    ]
    assert [request.selector for request in opener.requests] == [
        "/v1/catalog",
        "/v1/query",
    ]
    rendered = f"{transport!r} {opener.calls!r}"
    assert token.decode("ascii") not in rendered
    assert str(token_path) not in rendered


def test_transport_rejects_caller_supplied_authorization_header(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_path = tmp_path / "service" / "ta.token"
    _write_token(token_path)
    opener = _RecordingOpener([_FakeResponse(_catalog_payload())])
    monkeypatch.setattr(urllib.request, "build_opener", lambda *_handlers: opener)
    transport = build_runtime_transport(
        "http-json-v1",
        token_file=token_path,
        base_url=BASE_URL,
    )

    with pytest.raises(ContractViolation, match="owns the Authorization header"):
        transport(
            method="GET",
            url="https://tradingdatas.fixture.invalid/v1/catalog",
            headers={"authorization": "Bearer caller-controlled"},
            json_body=None,
            timeout_seconds=3.0,
        )

    assert opener.calls == []


@pytest.mark.parametrize(
    ("method", "url", "headers", "json_body"),
    (
        (
            "GET",
            f"{BASE_URL}/v1/catalog",
            {"Accept": "application/json", "Host": "evil.internal"},
            None,
        ),
        (
            "GET",
            f"{BASE_URL}/v1/catalog",
            {"Accept": "application/json", "X-Forwarded-Host": "evil.internal"},
            None,
        ),
        (
            "GET",
            f"{BASE_URL}/v1/catalog",
            {"accept": "application/json"},
            None,
        ),
        (
            "POST",
            f"{BASE_URL}/v1/query",
            {"Accept": "application/json"},
            {"dataset_id": DATASET_ID, "schema_major": 2},
        ),
        (
            "POST",
            f"{BASE_URL}/v1/query",
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Proxy-Authorization": "forbidden",
            },
            {"dataset_id": DATASET_ID, "schema_major": 2},
        ),
    ),
)
def test_transport_rejects_any_non_client_owned_header_set_before_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    url: str,
    headers: dict[str, str],
    json_body: dict[str, object] | None,
) -> None:
    token_path = tmp_path / "service" / "ta.token"
    _write_token(token_path)
    opener = _RecordingOpener([_FakeResponse(_catalog_payload())])
    monkeypatch.setattr(urllib.request, "build_opener", lambda *_handlers: opener)
    transport = build_runtime_transport(
        "http-json-v1",
        token_file=token_path,
        base_url=BASE_URL,
    )

    with pytest.raises(ContractViolation, match="caller-controlled headers"):
        transport(
            method=method,
            url=url,
            headers=headers,
            json_body=json_body,
            timeout_seconds=3.0,
        )

    assert opener.calls == []
    assert opener.requests == []


def test_transport_uses_one_header_snapshot_for_validation_and_wire(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_path = tmp_path / "service" / "ta.token"
    _write_token(token_path)
    opener = _RecordingOpener([_FakeResponse(_catalog_payload())])
    monkeypatch.setattr(urllib.request, "build_opener", lambda *_handlers: opener)
    transport = build_runtime_transport(
        "http-json-v1",
        token_file=token_path,
        base_url=BASE_URL,
    )
    changing_headers = _ChangingHeaderMapping()

    response = transport(
        method="GET",
        url=f"{BASE_URL}/v1/catalog",
        headers=changing_headers,
        json_body=None,
        timeout_seconds=3.0,
    )

    assert response.status_code == 200
    assert changing_headers.iterations == 1
    assert opener.requests[0].get_header("Host") is None


def test_transport_rejects_string_subclass_target_and_header_masquerades(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_path = tmp_path / "service" / "ta.token"
    _write_token(token_path)
    opener = _RecordingOpener(
        [_FakeResponse(_catalog_payload()), _FakeResponse(_catalog_payload())]
    )
    monkeypatch.setattr(urllib.request, "build_opener", lambda *_handlers: opener)
    transport = build_runtime_transport(
        "http-json-v1",
        token_file=token_path,
        base_url=BASE_URL,
    )
    malicious_url = _MasqueradingString(
        "https://evil.invalid/steal",
        f"{BASE_URL}/v1/catalog",
    )
    malicious_header = _MasqueradingString("Host", "Accept")

    with pytest.raises(ContractViolation, match="native request strings"):
        transport(
            method="GET",
            url=malicious_url,
            headers={"Accept": "application/json"},
            json_body=None,
            timeout_seconds=3.0,
        )
    with pytest.raises(ContractViolation, match="native header strings"):
        transport(
            method="GET",
            url=f"{BASE_URL}/v1/catalog",
            headers={malicious_header: "application/json"},
            json_body=None,
            timeout_seconds=3.0,
        )

    assert opener.calls == []
    assert opener.requests == []


@pytest.mark.parametrize(
    ("method", "url", "json_body"),
    (
        ("DELETE", "http://untrusted.invalid/collect", None),
        ("GET", "http://untrusted.invalid/v1/catalog", None),
        ("GET", f"{BASE_URL}/v1/query", None),
        ("POST", f"{BASE_URL}/v1/catalog", {}),
        ("GET", f"{BASE_URL}/v1/catalog?redirect=1", None),
    ),
)
def test_transport_never_sends_bearer_to_unbound_method_or_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    url: str,
    json_body: dict[str, object] | None,
) -> None:
    token_path = tmp_path / "service" / "ta.token"
    _write_token(token_path)
    opener = _RecordingOpener([_FakeResponse(_catalog_payload())])
    monkeypatch.setattr(urllib.request, "build_opener", lambda *_handlers: opener)
    transport = build_runtime_transport(
        "http-json-v1",
        token_file=token_path,
        base_url=BASE_URL,
    )

    with pytest.raises(ContractViolation, match="unbound request target"):
        transport(
            method=method,
            url=url,
            headers={"Accept": "application/json"},
            json_body=json_body,
            timeout_seconds=3.0,
        )

    assert opener.calls == []
    assert opener.requests == []


@pytest.mark.parametrize("status", (401, 403))
def test_auth_failure_is_redacted_single_attempt_and_has_no_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    token_path = tmp_path / "service" / "ta.token"
    token = _write_token(token_path)
    body = b"credential rejected: " + token + b" at " + str(token_path).encode()
    error_body = _ForbiddenReadBody(body)
    failure = urllib.error.HTTPError(
        "https://tradingdatas.fixture.invalid/v1/catalog",
        status,
        "auth failed",
        Message(),
        error_body,
    )
    opener = _RecordingOpener([failure])
    monkeypatch.setattr(urllib.request, "build_opener", lambda *_handlers: opener)
    transport = build_runtime_transport(
        "http-json-v1",
        token_file=token_path,
        base_url=BASE_URL,
    )

    result = check_v1_runtime_gate(_config(token_path), transport=transport)

    assert result == {
        "status": "critical",
        "blocking": True,
        "reason": "v1_contract_or_transport_failure",
        "datasets": [],
        "error_type": "TradingDatasAuthenticationError",
    }
    assert opener.calls == [
        {
            "url": "https://tradingdatas.fixture.invalid/v1/catalog",
            "method": "GET",
            "authorization_present": True,
            "timeout": 3.0,
        }
    ]
    assert opener.requests[0].get_header("Authorization") is None
    assert error_body.read_called is False
    rendered = json.dumps(result, sort_keys=True) + repr(transport) + repr(opener.calls)
    assert token.decode("ascii") not in rendered
    assert str(token_path) not in rendered
    assert "8082" not in rendered
    assert "/tushare" not in rendered
    assert "/source_status" not in rendered


@pytest.mark.parametrize("status", (401, 403))
def test_query_auth_failure_stops_after_one_query_without_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    token_path = tmp_path / "service" / "ta.token"
    token = _write_token(token_path)
    error_body = _ForbiddenReadBody(b"must-not-be-read:" + token)
    failure = urllib.error.HTTPError(
        "https://tradingdatas.fixture.invalid/v1/query",
        status,
        "auth failed",
        Message(),
        error_body,
    )
    opener = _RecordingOpener([_FakeResponse(_catalog_payload()), failure])
    monkeypatch.setattr(urllib.request, "build_opener", lambda *_handlers: opener)
    transport = build_runtime_transport(
        "http-json-v1",
        token_file=token_path,
        base_url=BASE_URL,
    )

    result = check_v1_runtime_gate(_config(token_path), transport=transport)

    assert result["status"] == "critical"
    assert result["blocking"] is True
    assert result["error_type"] == "TradingDatasAuthenticationError"
    assert [
        (call["method"], call["url"].rsplit("/", 1)[-1]) for call in opener.calls
    ] == [
        ("GET", "catalog"),
        ("POST", "query"),
    ]
    assert all(
        request.get_header("Authorization") is None for request in opener.requests
    )
    assert error_body.read_called is False
    rendered = json.dumps(result, sort_keys=True) + repr(opener.calls)
    assert token.decode("ascii") not in rendered
    assert "8082" not in rendered
    assert "/tushare" not in rendered
    assert "/source_status" not in rendered


def test_auth_rejection_latches_transport_before_any_later_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_path = tmp_path / "service" / "ta.token"
    _write_token(token_path)
    failure = urllib.error.HTTPError(
        f"{BASE_URL}/v1/catalog",
        401,
        "auth failed",
        Message(),
        io.BytesIO(b"must-not-be-read"),
    )
    opener = _RecordingOpener([failure, _FakeResponse(_catalog_payload())])
    monkeypatch.setattr(urllib.request, "build_opener", lambda *_handlers: opener)
    transport = build_runtime_transport(
        "http-json-v1",
        token_file=token_path,
        base_url=BASE_URL,
    )

    with pytest.raises(TradingDatasAuthenticationError):
        transport(
            method="GET",
            url=f"{BASE_URL}/v1/catalog",
            headers={"Accept": "application/json"},
            json_body=None,
            timeout_seconds=3.0,
        )
    with pytest.raises(TradingDatasAuthenticationError, match="latched"):
        transport(
            method="POST",
            url=f"{BASE_URL}/v1/query",
            headers={"Accept": "application/json"},
            json_body={"dataset_id": DATASET_ID, "schema_major": 2},
            timeout_seconds=3.0,
        )

    assert len(opener.calls) == 1


@pytest.mark.parametrize("status", (401, 403))
def test_direct_auth_status_never_reads_body_and_latches_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    token_path = tmp_path / "service" / "ta.token"
    _write_token(token_path)
    rejected = _ForbiddenReadResponse(status)
    opener = _RecordingOpener([rejected, _FakeResponse(_catalog_payload())])
    monkeypatch.setattr(urllib.request, "build_opener", lambda *_handlers: opener)
    transport = build_runtime_transport(
        "http-json-v1",
        token_file=token_path,
        base_url=BASE_URL,
    )

    with pytest.raises(TradingDatasAuthenticationError):
        transport(
            method="GET",
            url=f"{BASE_URL}/v1/catalog",
            headers={"Accept": "application/json"},
            json_body=None,
            timeout_seconds=3.0,
        )
    with pytest.raises(TradingDatasAuthenticationError, match="latched"):
        transport(
            method="POST",
            url=f"{BASE_URL}/v1/query",
            headers={"Accept": "application/json"},
            json_body={"dataset_id": DATASET_ID, "schema_major": 2},
            timeout_seconds=3.0,
        )

    assert rejected.read_called is False
    assert len(opener.calls) == 1


def test_transport_rejects_concurrent_second_request_before_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_path = tmp_path / "service" / "ta.token"
    _write_token(token_path)
    opener = _BlockingOpener([_FakeResponse(_catalog_payload())])
    monkeypatch.setattr(urllib.request, "build_opener", lambda *_handlers: opener)
    transport = build_runtime_transport(
        "http-json-v1",
        token_file=token_path,
        base_url=BASE_URL,
    )
    first_result: list[object] = []

    def first_request() -> None:
        first_result.append(
            transport(
                method="GET",
                url=f"{BASE_URL}/v1/catalog",
                headers={"Accept": "application/json"},
                json_body=None,
                timeout_seconds=3.0,
            )
        )

    worker = threading.Thread(target=first_request)
    worker.start()
    assert opener.started.wait(timeout=2.0)
    try:
        with pytest.raises(TradingDatasAuthenticationError, match="concurrent"):
            transport(
                method="GET",
                url=f"{BASE_URL}/v1/catalog",
                headers={"Accept": "application/json"},
                json_body=None,
                timeout_seconds=3.0,
            )
    finally:
        opener.release.set()
        worker.join(timeout=2.0)

    assert not worker.is_alive()
    assert len(first_result) == 1
    assert len(opener.calls) == 1
