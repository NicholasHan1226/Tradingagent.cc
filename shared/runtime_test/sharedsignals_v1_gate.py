#!/usr/bin/env python3
"""Fail-closed TradingAgent runtime gate for the frozen TradingDatas V1 API.

This module is a consumer only.  It delegates all contract parsing to the
provider-neutral V1 client and all dataset decisions to ``DataEvidenceGate``.
There is no local database, provider adapter, or legacy endpoint fallback.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import os
import sys
from typing import Any
import urllib.error
import urllib.request

from shared.data.evidence_gate import (
    DataEvidenceGate,
    DatasetEvidencePolicy,
    EvidenceAction,
    EvidenceDecision,
)
from shared.data.sharedsignals_v1 import (
    ContractViolation,
    HTTPResponse,
    HTTPTransport,
    QueryEnvelope,
    QueryRequest,
    SharedSignalsV1Client,
    SharedSignalsV1Config,
    SharedSignalsV1Error,
)


class RuntimeGateConfigurationError(ValueError):
    """Raised when a runtime authority input is absent or malformed."""


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
    # credential; future authentication must be injected by the transport after
    # a fresh handoff freezes that contract.
    access_policy_id: str
    transport_id: str
    timeout_seconds: float

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
        raise RuntimeGateConfigurationError(
            "TRADINGDATAS_API_TIMEOUT must be positive"
        )

    return TradingDatasV1RuntimeGateConfig(
        base_url=base_url,
        catalog_version=catalog_version,
        dataset_ids=_dataset_ids_for_market(raw_dataset_ids, market),
        schema_major=schema_major,
        access_policy_id=access_policy_id,
        transport_id=transport_id,
        timeout_seconds=timeout_seconds,
    )


class UrllibJSONV1Transport:
    """Small explicit HTTP transport for the already-frozen V1 client port."""

    def __init__(self) -> None:
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            RejectRedirectHandler(),
        )

    def __call__(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        json_body: Mapping[str, Any] | None,
        timeout_seconds: float,
    ) -> HTTPResponse:
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
        request = urllib.request.Request(
            url=url,
            data=encoded_body,
            headers=dict(headers),
            method=method,
        )
        try:
            with self._opener.open(request, timeout=timeout_seconds) as response:
                status_code = int(getattr(response, "status", 200))
                raw_body = response.read(4_194_305)
        except urllib.error.HTTPError as exc:
            status_code = int(exc.code)
            raw_body = exc.read(4_194_305)
        if len(raw_body) > 4_194_304:
            raise ContractViolation("TradingDatas V1 response exceeds 4 MiB")
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ContractViolation(
                "TradingDatas V1 response must be UTF-8 JSON"
            ) from exc
        if not isinstance(payload, Mapping):
            raise ContractViolation("TradingDatas V1 response must be a JSON object")
        return HTTPResponse(status_code=status_code, json_body=payload)


def build_runtime_transport(transport_id: str) -> HTTPTransport:
    if transport_id != "http-json-v1":
        raise RuntimeGateConfigurationError(
            "TRADINGDATAS_RUNTIME_TRANSPORT must equal http-json-v1 for CLI runtime"
        )
    return UrllibJSONV1Transport()


def _critical_result(reason: str, *, error: Exception | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "critical",
        "blocking": True,
        "reason": reason,
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
    """Probe every configured dataset and reject any non-ready evidence."""

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
            dataset_results.append(
                {
                    "dataset_id": dataset_id,
                    "state": envelope.metadata.state,
                    "degraded": envelope.metadata.degraded,
                    "effective_state": decision.effective_state,
                    "action": decision.action.value,
                    "eligible": decision.eligible,
                    "receipt_id": envelope.metadata.receipt_id,
                    "data_through": envelope.metadata.data_through,
                    "observed_at": envelope.metadata.observed_at,
                    "reasons": _controlled_reason_codes(envelope, decision),
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
        "datasets": dataset_results,
    }


def check_v1_runtime_gate_from_environment(
    environment: Mapping[str, str],
    *,
    market: str | None,
) -> dict[str, Any]:
    try:
        config = config_from_environment(environment, market=market)
        transport = build_runtime_transport(config.transport_id)
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
    "TradingDatasV1RuntimeGateConfig",
    "UrllibJSONV1Transport",
    "build_runtime_transport",
    "check_v1_runtime_gate",
    "check_v1_runtime_gate_from_environment",
    "config_from_environment",
    "main",
]
