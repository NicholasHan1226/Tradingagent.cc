"""Fail-closed authenticated HTTP transport for TradingDatas V1.

The transport is the only layer allowed to turn a restricted token-file value
into a Bearer header.  It is bound to the provider-neutral ``/v1/catalog`` and
``/v1/query`` routes and provides no database, provider or legacy fallback.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
import ipaddress
import json
from pathlib import Path
import threading
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

from shared.data.sharedsignals_v1 import (
    CATALOG_PATH,
    ContractViolation,
    HTTPResponse,
    HTTPTransport,
    QUERY_PATH,
    SharedSignalsV1Config,
    SharedSignalsV1Error,
)
from shared.data.tradingdatas_auth import (
    TradingDatasBearerToken,
    TradingDatasTokenFile,
    TradingDatasTokenFileError,
)


class RuntimeGateConfigurationError(ValueError):
    """Raised when a runtime transport authority input is malformed."""


class TradingDatasAuthenticationError(SharedSignalsV1Error):
    """Redacted, latched rejection from the frozen bearer boundary."""


class RejectRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Keep V1 requests on the explicitly configured authority and path."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        del req, fp, code, msg, headers, newurl
        return None


def _validated_transport_base_url(base_url: str) -> str:
    if (
        type(base_url) is not str
        or not base_url
        or base_url != base_url.strip()
        or any(
            ord(character) < 0x20 or ord(character) == 0x7F for character in base_url
        )
        or any(delimiter in base_url for delimiter in ("?", "#", "\\"))
    ):
        raise RuntimeGateConfigurationError(
            "TradingDatas transport base URL must be a canonical authority"
        )
    try:
        parsed_authority = urllib.parse.urlsplit(base_url)
        port = parsed_authority.port
        hostname = parsed_authority.hostname
    except ValueError as exc:
        raise RuntimeGateConfigurationError(
            "TradingDatas transport base URL must be a canonical authority"
        ) from exc
    if (
        parsed_authority.scheme not in {"http", "https"}
        or not parsed_authority.netloc
        or hostname is None
        or parsed_authority.path
        or parsed_authority.query
        or parsed_authority.fragment
        or parsed_authority.netloc.endswith(":")
        or "%" in parsed_authority.netloc
        or (port is not None and not 1 <= port <= 65_535)
    ):
        raise RuntimeGateConfigurationError(
            "TradingDatas transport base URL must be a canonical authority"
        )
    if parsed_authority.username is not None or parsed_authority.password is not None:
        raise RuntimeGateConfigurationError(
            "TradingDatas transport base URL must not contain userinfo"
        )
    canonical_authority = urllib.parse.urlunsplit(
        (parsed_authority.scheme, parsed_authority.netloc, "", "", "")
    )
    if base_url != canonical_authority:
        raise RuntimeGateConfigurationError(
            "TradingDatas transport base URL must be a canonical authority"
        )
    try:
        authority = SharedSignalsV1Config(
            base_url=canonical_authority,
            expected_catalog_version="transport-boundary",
            dataset_ids=frozenset({"transport.boundary"}),
            access_policy_id="transport-boundary",
        ).base_url
    except (TypeError, ValueError, ContractViolation) as exc:
        raise RuntimeGateConfigurationError(
            "TradingDatas transport base URL is invalid"
        ) from exc
    if parsed_authority.scheme == "http":
        try:
            host = ipaddress.ip_address(hostname)
        except ValueError as exc:
            raise RuntimeGateConfigurationError(
                "plaintext TradingDatas HTTP requires a loopback IP literal"
            ) from exc
        if not host.is_loopback:
            raise RuntimeGateConfigurationError(
                "plaintext TradingDatas HTTP requires a loopback IP literal"
            )
    return authority


class UrllibJSONV1Transport:
    """Small explicit HTTP transport for the already-frozen V1 client port."""

    def __init__(
        self,
        *,
        bearer_token: TradingDatasBearerToken,
        base_url: str,
    ) -> None:
        if type(bearer_token) is not TradingDatasBearerToken:
            raise RuntimeGateConfigurationError(
                "TradingDatas bearer token must come from the configured token file"
            )
        authority = _validated_transport_base_url(base_url)
        self._bearer_token = bearer_token
        self._authentication_failed = False
        self._request_in_progress = False
        self._state_lock = threading.Lock()
        self._allowed_requests = frozenset(
            {
                ("GET", f"{authority}{CATALOG_PATH}"),
                ("POST", f"{authority}{QUERY_PATH}"),
            }
        )
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            RejectRedirectHandler(),
        )

    def __repr__(self) -> str:
        return "UrllibJSONV1Transport(<redacted>)"

    @contextmanager
    def _request_lease(self) -> Iterator[None]:
        with self._state_lock:
            if self._authentication_failed:
                raise TradingDatasAuthenticationError(
                    "TradingDatas V1 authentication rejection is latched"
                )
            if self._request_in_progress:
                raise TradingDatasAuthenticationError(
                    "TradingDatas V1 concurrent request was rejected"
                )
            self._request_in_progress = True
        try:
            yield
        finally:
            with self._state_lock:
                self._request_in_progress = False

    def __call__(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        json_body: Mapping[str, Any] | None,
        timeout_seconds: float,
    ) -> HTTPResponse:
        with self._request_lease():
            return self._call_once(
                method=method,
                url=url,
                headers=headers,
                json_body=json_body,
                timeout_seconds=timeout_seconds,
            )

    def _call_once(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        json_body: Mapping[str, Any] | None,
        timeout_seconds: float,
    ) -> HTTPResponse:
        if type(method) is not str or type(url) is not str:
            raise ContractViolation(
                "TradingDatas bearer transport requires native request strings"
            )
        if (method, url) not in self._allowed_requests:
            raise ContractViolation(
                "TradingDatas bearer transport rejected an unbound request target"
            )
        if (method == "GET") != (json_body is None):
            raise ContractViolation(
                "TradingDatas bearer transport rejected an invalid request shape"
            )
        encoded_body = (
            None
            if json_body is None
            else json.dumps(
                json_body,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        )
        caller_headers = dict(headers)
        if any(
            type(name) is not str or type(value) is not str
            for name, value in caller_headers.items()
        ):
            raise ContractViolation(
                "TradingDatas bearer transport requires native header strings"
            )
        if any(name.lower() == "authorization" for name in caller_headers):
            raise ContractViolation(
                "TradingDatas V1 transport owns the Authorization header"
            )
        expected_headers = {"Accept": "application/json"}
        if method == "POST":
            expected_headers["Content-Type"] = "application/json"
        if caller_headers != expected_headers:
            raise ContractViolation(
                "TradingDatas bearer transport rejected caller-controlled headers"
            )
        wire_headers = caller_headers.copy()
        wire_headers["Authorization"] = self._bearer_token._authorization_header()
        request = urllib.request.Request(
            url=url,
            data=encoded_body,
            headers=wire_headers,
            method=method,
        )
        try:
            try:
                with self._opener.open(request, timeout=timeout_seconds) as response:
                    status_code = int(getattr(response, "status", 200))
                    if status_code in {401, 403}:
                        self._authentication_failed = True
                        raw_body = b""
                    else:
                        raw_body = response.read(4_194_305)
            except urllib.error.HTTPError as exc:
                status_code = int(exc.code)
                if status_code in {401, 403}:
                    self._authentication_failed = True
                raw_body = b""
                exc.close()
        finally:
            request.remove_header("Authorization")
            wire_headers.pop("Authorization", None)
        if len(raw_body) > 4_194_304:
            raise ContractViolation("TradingDatas V1 response exceeds 4 MiB")
        if status_code in {401, 403}:
            raise TradingDatasAuthenticationError(
                "TradingDatas V1 authentication was rejected"
            )
        if status_code != 200:
            return HTTPResponse(status_code=status_code, json_body={})
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ContractViolation(
                "TradingDatas V1 response must be UTF-8 JSON"
            ) from exc
        if not isinstance(payload, Mapping):
            raise ContractViolation("TradingDatas V1 response must be a JSON object")
        return HTTPResponse(status_code=status_code, json_body=payload)


def build_runtime_transport(
    transport_id: str,
    *,
    token_file: Path | str,
    base_url: str,
) -> HTTPTransport:
    if transport_id != "http-json-v1":
        raise RuntimeGateConfigurationError(
            "TRADINGDATAS_RUNTIME_TRANSPORT must equal http-json-v1 for CLI runtime"
        )
    authority = _validated_transport_base_url(base_url)
    try:
        bearer_token = TradingDatasTokenFile(token_file).read_token()
    except TradingDatasTokenFileError as exc:
        raise RuntimeGateConfigurationError(exc.reason_code) from None
    return UrllibJSONV1Transport(bearer_token=bearer_token, base_url=authority)


__all__ = [
    "RejectRedirectHandler",
    "RuntimeGateConfigurationError",
    "TradingDatasAuthenticationError",
    "UrllibJSONV1Transport",
    "build_runtime_transport",
]
