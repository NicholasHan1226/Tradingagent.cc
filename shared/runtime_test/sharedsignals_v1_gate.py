#!/usr/bin/env python3
"""Fail-closed TradingDatas V1 transport/metadata smoke for TradingAgent.

This module is a consumer only.  It delegates all contract parsing to the
provider-neutral V1 client and all dataset decisions to ``DataEvidenceGate``.
It does not prove provider-native row identity, bounded pagination or research
snapshot readiness; the integration probe owns those checks.  There is no
local database, provider adapter, or legacy endpoint fallback.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import sys
import threading
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

from shared.data.evidence_gate import (
    DataEvidenceGate,
    DatasetEvidencePolicy,
    EvidenceAction,
    EvidenceDecision,
)
from shared.data.sharedsignals_v1 import (
    CATALOG_PATH,
    ContractViolation,
    HTTPResponse,
    HTTPTransport,
    QueryEnvelope,
    QUERY_PATH,
    QueryRequest,
    SharedSignalsV1Client,
    SharedSignalsV1Config,
    SharedSignalsV1Error,
)
from shared.data.tradingdatas_auth import (
    TradingDatasBearerToken,
    TradingDatasTokenFile,
    TradingDatasTokenFileError,
)


class RuntimeGateConfigurationError(ValueError):
    """Raised when a runtime authority input is absent or malformed."""


_PLAINTEXT_TOKEN_ENVIRONMENT_KEYS = (
    "TRADINGDATAS_API_TOKEN",
    "TRADINGDATAS_BEARER_TOKEN",
    "TRADINGDATAS_TOKEN",
)


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


@dataclass(frozen=True)
class TradingDatasV1RuntimeGateConfig:
    """All runtime authority inputs needed before a V1 probe may run."""

    base_url: str
    catalog_version: str
    dataset_ids: tuple[str, ...]
    schema_major: int
    # Local cache/audit namespace only. It is not a TradingDatas wire header or
    # credential. Bearer authentication is owned exclusively by the final HTTP
    # transport and sourced from ``token_file``.
    access_policy_id: str
    transport_id: str
    timeout_seconds: float
    token_file: Path | str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.transport_id, str) or not self.transport_id.strip():
            raise RuntimeGateConfigurationError(
                "TRADINGDATAS_RUNTIME_TRANSPORT must be explicitly configured"
            )
        if self.transport_id != self.transport_id.strip():
            raise RuntimeGateConfigurationError(
                "TRADINGDATAS_RUNTIME_TRANSPORT must not contain outer whitespace"
            )
        if not isinstance(self.dataset_ids, tuple) or not self.dataset_ids:
            raise RuntimeGateConfigurationError(
                "TRADINGDATAS_MARKET_PULSE_DATASET_IDS_JSON must select at least one dataset"
            )
        if len(set(self.dataset_ids)) != len(self.dataset_ids):
            raise RuntimeGateConfigurationError(
                "dataset IDs must not contain duplicates"
            )
        try:
            token_file = Path(self.token_file)
        except TypeError as exc:
            raise RuntimeGateConfigurationError(
                "TRADINGDATAS_API_TOKEN_FILE must be an absolute path"
            ) from exc
        if not token_file.is_absolute() or not token_file.name:
            raise RuntimeGateConfigurationError(
                "TRADINGDATAS_API_TOKEN_FILE must be an absolute path"
            )
        object.__setattr__(self, "token_file", token_file)
        try:
            self.to_client_config()
        except (TypeError, ValueError, ContractViolation) as exc:
            raise RuntimeGateConfigurationError(str(exc)) from exc

    def to_client_config(self) -> SharedSignalsV1Config:
        return SharedSignalsV1Config(
            base_url=self.base_url,
            expected_catalog_version=self.catalog_version,
            dataset_ids=frozenset(self.dataset_ids),
            access_policy_id=self.access_policy_id,
            timeout_seconds=self.timeout_seconds,
            cache_ttl_seconds=0,
        )


# Compatibility-only Python symbol. The upstream product and every runtime
# configuration key are TradingDatas; keeping this alias avoids an abrupt
# import break for TA-side callers while the compatibility module is retired.
SharedSignalsV1RuntimeGateConfig = TradingDatasV1RuntimeGateConfig


def _required_environment_value(
    environment: Mapping[str, str],
    variable_name: str,
) -> str:
    value = environment.get(variable_name)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeGateConfigurationError(
            f"{variable_name} must be explicitly configured"
        )
    if value != value.strip():
        raise RuntimeGateConfigurationError(
            f"{variable_name} must not contain outer whitespace"
        )
    return value


def _dataset_ids_for_market(raw_json: str, market: str | None) -> tuple[str, ...]:
    try:
        payload = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise RuntimeGateConfigurationError(
            "TRADINGDATAS_MARKET_PULSE_DATASET_IDS_JSON must be valid JSON"
        ) from exc
    if not isinstance(payload, dict) or not payload:
        raise RuntimeGateConfigurationError(
            "TRADINGDATAS_MARKET_PULSE_DATASET_IDS_JSON must be a non-empty object"
        )

    selected: list[Any] = []
    if market is None:
        for value in payload.values():
            selected.extend(value if isinstance(value, list) else [value])
    else:
        normalized_market = str(market).strip().lower()
        if not normalized_market:
            raise RuntimeGateConfigurationError("market must be explicitly configured")
        if normalized_market not in payload:
            raise RuntimeGateConfigurationError(
                "TRADINGDATAS_MARKET_PULSE_DATASET_IDS_JSON has no dataset "
                f"for market={normalized_market}"
            )
        value = payload[normalized_market]
        selected.extend(value if isinstance(value, list) else [value])

    dataset_ids: list[str] = []
    for value in selected:
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            raise RuntimeGateConfigurationError(
                "TRADINGDATAS_MARKET_PULSE_DATASET_IDS_JSON values must be "
                "non-empty dataset IDs"
            )
        if value not in dataset_ids:
            dataset_ids.append(value)
    if not dataset_ids:
        raise RuntimeGateConfigurationError(
            "TRADINGDATAS_MARKET_PULSE_DATASET_IDS_JSON selected no datasets"
        )
    return tuple(dataset_ids)


def token_file_from_environment(environment: Mapping[str, str]) -> Path:
    """Return the explicit credential path without reading or exposing it."""

    if any(key in environment for key in _PLAINTEXT_TOKEN_ENVIRONMENT_KEYS):
        raise RuntimeGateConfigurationError(
            "plaintext TradingDatas token environment variables are forbidden"
        )
    raw = _required_environment_value(environment, "TRADINGDATAS_API_TOKEN_FILE")
    candidate = Path(raw)
    if not candidate.is_absolute() or not candidate.name:
        raise RuntimeGateConfigurationError(
            "TRADINGDATAS_API_TOKEN_FILE must be an absolute path"
        )
    return candidate


def config_from_environment(
    environment: Mapping[str, str],
    *,
    market: str | None,
) -> TradingDatasV1RuntimeGateConfig:
    """Build a gate config without inventing any authority-routing default."""

    base_url = _required_environment_value(environment, "TRADINGDATAS_API_URL")
    catalog_version = _required_environment_value(
        environment,
        "TRADINGDATAS_CATALOG_VERSION",
    )
    access_policy_id = _required_environment_value(
        environment,
        "TRADINGDATAS_ACCESS_POLICY_ID",
    )
    raw_dataset_ids = _required_environment_value(
        environment,
        "TRADINGDATAS_MARKET_PULSE_DATASET_IDS_JSON",
    )
    raw_schema_major = _required_environment_value(
        environment,
        "TRADINGDATAS_SCHEMA_MAJOR",
    )
    transport_id = _required_environment_value(
        environment,
        "TRADINGDATAS_RUNTIME_TRANSPORT",
    )
    token_file = token_file_from_environment(environment)
    try:
        schema_major = int(raw_schema_major)
    except ValueError as exc:
        raise RuntimeGateConfigurationError(
            "TRADINGDATAS_SCHEMA_MAJOR must be a positive integer"
        ) from exc
    if schema_major <= 0 or str(schema_major) != raw_schema_major:
        raise RuntimeGateConfigurationError(
            "TRADINGDATAS_SCHEMA_MAJOR must be a canonical positive integer"
        )

    raw_timeout = environment.get("TRADINGDATAS_API_TIMEOUT", "10")
    try:
        timeout_seconds = float(raw_timeout)
    except (TypeError, ValueError) as exc:
        raise RuntimeGateConfigurationError(
            "TRADINGDATAS_API_TIMEOUT must be positive"
        ) from exc
    if timeout_seconds <= 0:
        raise RuntimeGateConfigurationError("TRADINGDATAS_API_TIMEOUT must be positive")

    return TradingDatasV1RuntimeGateConfig(
        base_url=base_url,
        catalog_version=catalog_version,
        dataset_ids=_dataset_ids_for_market(raw_dataset_ids, market),
        schema_major=schema_major,
        access_policy_id=access_policy_id,
        transport_id=transport_id,
        timeout_seconds=timeout_seconds,
        token_file=token_file,
    )


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


def _critical_result(reason: str, *, error: Exception | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "critical",
        "blocking": True,
        "reason": reason,
        "scope": "transport_metadata_smoke",
        "research_contract_verified": False,
        "datasets": [],
    }
    if error is not None:
        result["error_type"] = type(error).__name__
    return result


def _source_proof_complete(envelope: QueryEnvelope) -> bool:
    metadata = envelope.metadata
    return bool(
        isinstance(metadata.lineage, Mapping)
        and metadata.lineage
        and isinstance(metadata.receipt_id, str)
        and metadata.receipt_id
        and isinstance(metadata.data_through, str)
        and metadata.data_through
        and isinstance(metadata.observed_at, str)
        and metadata.observed_at
    )


def _controlled_reason_codes(
    envelope: QueryEnvelope,
    decision: EvidenceDecision,
) -> list[str]:
    """Report local state-machine codes, never provider-supplied free text."""

    if not _source_proof_complete(envelope):
        top_state = envelope.metadata.state.strip().lower()
        hard_failed = {
            "failed",
            "error",
            "invalid",
            "unavailable",
            "unobserved",
            "paused",
            "empty",
        }
        return [
            "dataset_failed"
            if top_state in hard_failed
            else "dataset_evidence_incomplete"
        ]
    if decision.effective_state == "degraded":
        return ["dataset_degraded"]
    if decision.effective_state == "stale":
        return ["dataset_stale"]
    if decision.effective_state == "failed":
        return ["dataset_failed"]
    if decision.effective_state == "unknown":
        return ["dataset_state_unknown"]
    return []


def _reason_digest(decision: EvidenceDecision) -> str:
    encoded = json.dumps(
        list(decision.reasons),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def check_v1_runtime_gate(
    config: TradingDatasV1RuntimeGateConfig,
    *,
    transport: HTTPTransport | None,
) -> dict[str, Any]:
    """Smoke-test catalog/query transport and reject non-ready metadata."""

    if not isinstance(config, TradingDatasV1RuntimeGateConfig):
        raise TypeError("config must be a TradingDatasV1RuntimeGateConfig")
    if transport is None:
        return _critical_result("transport_not_configured")

    client = SharedSignalsV1Client(config.to_client_config(), transport=transport)
    policies = {
        dataset_id: DatasetEvidencePolicy(
            dataset_id=dataset_id,
            degraded_action=EvidenceAction.REJECT,
            stale_action=EvidenceAction.REJECT,
        )
        for dataset_id in config.dataset_ids
    }
    evidence_gate = DataEvidenceGate(policies)

    try:
        catalog = client.get_catalog()
        dataset_results: list[dict[str, Any]] = []
        for dataset_id in config.dataset_ids:
            envelope = client.query(
                QueryRequest(
                    dataset_id=dataset_id,
                    schema_major=config.schema_major,
                )
            )
            decision = evidence_gate.evaluate(envelope)
            pagination_complete = envelope.next_cursor is None
            reason_codes = _controlled_reason_codes(envelope, decision)
            if not pagination_complete:
                reason_codes.append("runtime_smoke_requires_terminal_page")
            dataset_results.append(
                {
                    "dataset_id": dataset_id,
                    "state": envelope.metadata.state,
                    "degraded": envelope.metadata.degraded,
                    "effective_state": decision.effective_state,
                    "action": (
                        decision.action.value
                        if pagination_complete
                        else EvidenceAction.REJECT.value
                    ),
                    "eligible": decision.eligible and pagination_complete,
                    "pagination_complete": pagination_complete,
                    "receipt_id": envelope.metadata.receipt_id,
                    "data_through": envelope.metadata.data_through,
                    "observed_at": envelope.metadata.observed_at,
                    "reasons": reason_codes,
                    "reasons_sha256": _reason_digest(decision),
                }
            )
    except (SharedSignalsV1Error, OSError, urllib.error.URLError) as exc:
        return _critical_result("v1_contract_or_transport_failure", error=exc)

    blocking = any(not row["eligible"] for row in dataset_results)
    return {
        "status": "critical" if blocking else "ok",
        "blocking": blocking,
        "reason": "dataset_evidence_rejected" if blocking else "",
        "api_version": catalog.api_version,
        "catalog_version": catalog.catalog_version,
        "schema_major": config.schema_major,
        "transport_id": config.transport_id,
        "scope": "transport_metadata_smoke",
        "research_contract_verified": False,
        "datasets": dataset_results,
    }


def check_v1_runtime_gate_from_environment(
    environment: Mapping[str, str],
    *,
    market: str | None,
) -> dict[str, Any]:
    try:
        config = config_from_environment(environment, market=market)
        transport = build_runtime_transport(
            config.transport_id,
            token_file=config.token_file,
            base_url=config.base_url,
        )
    except RuntimeGateConfigurationError as exc:
        return _critical_result("missing_or_invalid_v1_config", error=exc)
    return check_v1_runtime_gate(config, transport=transport)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail-closed TradingDatas V1 dataset gate for TradingAgent"
    )
    parser.add_argument("--market", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    result = check_v1_runtime_gate_from_environment(os.environ, market=args.market)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(
            f"{result['status']} blocking={result['blocking']} "
            f"reason={result.get('reason', '')}"
        )
    return 2 if result["blocking"] else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))


__all__ = [
    "RejectRedirectHandler",
    "RuntimeGateConfigurationError",
    "SharedSignalsV1RuntimeGateConfig",
    "TradingDatasAuthenticationError",
    "TradingDatasV1RuntimeGateConfig",
    "UrllibJSONV1Transport",
    "build_runtime_transport",
    "check_v1_runtime_gate",
    "check_v1_runtime_gate_from_environment",
    "config_from_environment",
    "main",
    "token_file_from_environment",
]
