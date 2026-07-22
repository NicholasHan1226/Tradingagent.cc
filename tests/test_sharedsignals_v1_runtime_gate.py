from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
from typing import Any
import urllib.request

import pytest

from shared.data.sharedsignals_v1 import HTTPResponse
from shared.runtime_test.sharedsignals_v1_gate import (
    RejectRedirectHandler,
    RuntimeGateConfigurationError,
    SharedSignalsV1RuntimeGateConfig,
    TradingDatasV1RuntimeGateConfig,
    build_runtime_transport,
    check_v1_runtime_gate,
    config_from_environment,
)


ROOT = Path(__file__).resolve().parents[1]

CATALOG_VERSION = "catalog-runtime-fixture-v1"
ACCESS_POLICY_ID = "ta-runtime-read-v1"
DATASET_ID = "market-pulse-us-v1"
SCHEMA_MAJOR = 1


def _ready_query_payload(dataset_id: str = DATASET_ID) -> dict[str, Any]:
    return {
        "api_version": "v1",
        "catalog_version": CATALOG_VERSION,
        "request_id": "query-request-1",
        "dataset_id": dataset_id,
        "data": [{"symbol": "FIXTURE"}],
        "next_cursor": None,
        "metadata": {
            "state": "ready",
            "degraded": False,
            "freshness": {"state": "fresh", "fresh": True},
            "quality": {"state": "valid", "valid": True},
            "lineage": {
                "state": "complete",
                "complete": True,
                "provider_neutral": True,
            },
            "receipt_id": "receipt-runtime-1",
            "data_through": "2026-07-16T01:00:00+00:00",
            "observed_at": "2026-07-16T01:00:01+00:00",
            "reasons": [],
        },
    }


class RecordingTransport:
    def __init__(self, query_payload: dict[str, Any] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.query_payload = query_payload or _ready_query_payload()

    def __call__(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        json_body: dict[str, Any] | None,
        timeout_seconds: float,
    ) -> HTTPResponse:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "json_body": json_body,
                "timeout_seconds": timeout_seconds,
            }
        )
        if method == "GET" and url.endswith("/v1/catalog"):
            return HTTPResponse(
                status_code=200,
                json_body={
                    "api_version": "v1",
                    "catalog_version": CATALOG_VERSION,
                    "request_id": "catalog-request-1",
                    "data": [{"dataset_id": DATASET_ID}],
                },
            )
        if method == "POST" and url.endswith("/v1/query"):
            return HTTPResponse(status_code=200, json_body=self.query_payload)
        raise AssertionError(f"unexpected call: {method} {url}")


def _config() -> TradingDatasV1RuntimeGateConfig:
    return TradingDatasV1RuntimeGateConfig(
        base_url="https://tradingdatas.fixture.invalid",
        catalog_version=CATALOG_VERSION,
        dataset_ids=(DATASET_ID,),
        schema_major=SCHEMA_MAJOR,
        access_policy_id=ACCESS_POLICY_ID,
        transport_id="fixture-v1",
        timeout_seconds=3.0,
        token_file=Path("/fixture/tradingdatas/ta.token"),
    )


def test_legacy_runtime_config_symbol_is_a_compatibility_alias_only() -> None:
    assert SharedSignalsV1RuntimeGateConfig is TradingDatasV1RuntimeGateConfig


def _environment() -> dict[str, str]:
    return {
        "TRADINGDATAS_API_URL": "https://tradingdatas.fixture.invalid",
        "TRADINGDATAS_CATALOG_VERSION": CATALOG_VERSION,
        "TRADINGDATAS_ACCESS_POLICY_ID": ACCESS_POLICY_ID,
        "TRADINGDATAS_MARKET_PULSE_DATASET_IDS_JSON": (
            '{"us":"market-pulse-us-v1","crypto":["market-pulse-crypto-v1"]}'
        ),
        "TRADINGDATAS_SCHEMA_MAJOR": str(SCHEMA_MAJOR),
        "TRADINGDATAS_RUNTIME_TRANSPORT": "http-json-v1",
        "TRADINGDATAS_API_TOKEN_FILE": "/fixture/tradingdatas/ta.token",
        "TRADINGDATAS_API_TIMEOUT": "3",
    }


@pytest.mark.parametrize(
    "missing_name",
    [
        "TRADINGDATAS_API_URL",
        "TRADINGDATAS_CATALOG_VERSION",
        "TRADINGDATAS_ACCESS_POLICY_ID",
        "TRADINGDATAS_MARKET_PULSE_DATASET_IDS_JSON",
        "TRADINGDATAS_SCHEMA_MAJOR",
        "TRADINGDATAS_RUNTIME_TRANSPORT",
        "TRADINGDATAS_API_TOKEN_FILE",
    ],
)
def test_environment_config_requires_every_v1_authority_field(
    missing_name: str,
) -> None:
    environment = _environment()
    environment.pop(missing_name)

    with pytest.raises(RuntimeGateConfigurationError, match=missing_name):
        config_from_environment(environment, market="us")


@pytest.mark.parametrize(
    "plaintext_name",
    (
        "TRADINGDATAS_API_TOKEN",
        "TRADINGDATAS_BEARER_TOKEN",
        "TRADINGDATAS_TOKEN",
    ),
)
def test_plaintext_token_environment_is_rejected_even_with_token_file_config(
    plaintext_name: str,
) -> None:
    environment = _environment()
    environment[plaintext_name] = "ambient-secret-must-be-rejected"

    with pytest.raises(RuntimeGateConfigurationError) as caught:
        config_from_environment(environment, market="us")

    rendered = str(caught.value)
    assert "plaintext TradingDatas token" in rendered
    assert environment[plaintext_name] not in rendered


def test_environment_config_selects_only_the_explicit_market_dataset() -> None:
    config = config_from_environment(_environment(), market="crypto")

    assert config.dataset_ids == ("market-pulse-crypto-v1",)
    assert config.schema_major == SCHEMA_MAJOR
    assert config.transport_id == "http-json-v1"


def test_runtime_gate_uses_only_catalog_and_query_with_schema_major() -> None:
    transport = RecordingTransport()

    result = check_v1_runtime_gate(_config(), transport=transport)

    assert result["status"] == "ok"
    assert result["blocking"] is False
    assert [call["url"] for call in transport.calls] == [
        "https://tradingdatas.fixture.invalid/v1/catalog",
        "https://tradingdatas.fixture.invalid/v1/query",
    ]
    query_payload = transport.calls[1]["json_body"]
    assert query_payload is not None
    assert query_payload["dataset_id"] == DATASET_ID
    assert query_payload["schema_major"] == SCHEMA_MAJOR
    assert "order" not in query_payload


def test_runtime_smoke_rejects_nonterminal_page_without_claiming_research_readiness() -> None:
    payload = _ready_query_payload()
    payload["next_cursor"] = "opaque-next-page"

    result = check_v1_runtime_gate(
        _config(),
        transport=RecordingTransport(payload),
    )

    assert result["status"] == "critical"
    assert result["blocking"] is True
    assert result["datasets"][0]["eligible"] is False
    assert result["datasets"][0]["action"] == "reject"
    assert result["datasets"][0]["pagination_complete"] is False
    assert "runtime_smoke_requires_terminal_page" in result["datasets"][0]["reasons"]
    assert result["scope"] == "transport_metadata_smoke"
    assert result["research_contract_verified"] is False


@pytest.mark.parametrize(
    ("state", "degraded", "expected_reason"),
    [
        ("degraded", True, "dataset_evidence_incomplete"),
        ("stale", False, "dataset_evidence_incomplete"),
        ("unobserved", False, "dataset_failed"),
        ("paused", False, "dataset_failed"),
        ("failed", False, "dataset_failed"),
        ("empty", False, "dataset_failed"),
    ],
)
def test_http_200_impaired_dataset_with_null_proof_fails_closed_without_forgery(
    state: str,
    degraded: bool,
    expected_reason: str,
) -> None:
    payload = _ready_query_payload()
    payload["metadata"].update(
        {
            "state": state,
            "degraded": degraded,
            "lineage": None,
            "receipt_id": None,
            "data_through": None,
            "observed_at": None,
            "reasons": [f"fixture_{state}"],
        }
    )

    result = check_v1_runtime_gate(
        _config(),
        transport=RecordingTransport(payload),
    )

    assert result["status"] == "critical"
    assert result["blocking"] is True
    assert result["datasets"][0]["receipt_id"] is None
    assert result["datasets"][0]["action"] == "reject"
    assert expected_reason in result["datasets"][0]["reasons"]


def test_runtime_gate_without_injected_transport_fails_closed() -> None:
    result = check_v1_runtime_gate(_config(), transport=None)

    assert result["status"] == "critical"
    assert result["blocking"] is True
    assert result["reason"] == "transport_not_configured"


def test_runtime_http_transport_refuses_redirects() -> None:
    handler = RejectRedirectHandler()

    redirected = handler.redirect_request(
        urllib.request.Request("https://tradingdatas.fixture.invalid/v1/query"),
        None,
        302,
        "Found",
        {"Location": "https://unexpected.invalid/legacy"},
        "https://unexpected.invalid/legacy",
    )

    assert redirected is None


def test_runtime_http_transport_disables_environment_proxy(
    tmp_path: Path,
    monkeypatch,
) -> None:
    captured_handlers: list[object] = []

    def fake_build_opener(*handlers):
        captured_handlers.extend(handlers)
        return object()

    monkeypatch.setattr(urllib.request, "build_opener", fake_build_opener)
    monkeypatch.setattr(
        "shared.data.tradingdatas_auth._service_secret_roots",
        lambda: (tmp_path,),
    )

    token_file = tmp_path / "ta.token"
    token_file.write_text("fixture-token-value", encoding="ascii")
    token_file.chmod(0o600)
    build_runtime_transport(
        "http-json-v1",
        token_file=token_file,
        base_url="https://tradingdatas.fixture.invalid",
    )

    proxy_handlers = [
        handler
        for handler in captured_handlers
        if isinstance(handler, urllib.request.ProxyHandler)
    ]
    assert len(proxy_handlers) == 1
    assert proxy_handlers[0].proxies == {}


def test_runtime_gate_redacts_transport_error_details() -> None:
    def failing_transport(**_kwargs):
        raise OSError("https://user:sk-secret@tradingdatas.invalid/private")

    result = check_v1_runtime_gate(_config(), transport=failing_transport)

    assert result["blocking"] is True
    assert result["reason"] == "v1_contract_or_transport_failure"
    assert result["error_type"] == "OSError"
    assert "error" not in result
    assert "sk-secret" not in str(result)


def test_runtime_gate_hashes_provider_reason_text_and_derives_local_codes() -> None:
    payload = _ready_query_payload()
    sentinel = "provider-debug-secret-sk-0123456789abcdef0123456789abcdef"
    payload["metadata"]["reasons"] = ["dataset_failed", sentinel]

    result = check_v1_runtime_gate(
        _config(),
        transport=RecordingTransport(payload),
    )

    dataset = result["datasets"][0]
    assert result["blocking"] is False
    assert dataset["reasons"] == []
    assert len(dataset["reasons_sha256"]) == 64
    assert sentinel not in json.dumps(result, ensure_ascii=False, sort_keys=True)


def test_active_sim_wrappers_stop_before_unmigrated_legacy_readers() -> None:
    wrapper_paths = (
        "shared/wrappers/job_crypto_sim.sh",
        "shared/wrappers/job_cn_futures_sim.sh",
    )

    for relative_path in wrapper_paths:
        source = (ROOT / relative_path).read_text(encoding="utf-8")
        assert "tradingdatas_v1_runtime_gate" in source, relative_path
        assert "block_unmigrated_tradingdatas_consumer" in source, relative_path
        assert "sharedsignals_source_gate" not in source, relative_path
        assert "run_sim.py" not in source, relative_path
        assert "CNFutures.run_simulation" not in source, relative_path


def test_missing_runtime_v1_config_blocks_before_python_or_legacy_reader(
    tmp_path: Path,
) -> None:
    python_sentinel = tmp_path / "python-called"
    fake_python = tmp_path / "python"
    fake_python.write_text(
        f"#!/bin/bash\ntouch {python_sentinel!s}\nexit 0\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    env = os.environ.copy()
    for name in _environment():
        env.pop(name, None)
    env.update(
        {
            "TRADINGAGENT_ENV_LOADER_READY": "1",
            "TRADINGAGENT_ROOT": str(ROOT),
            "TRADINGS_CRON_LOG_ROOT": str(tmp_path / "logs"),
            "TRADINGS_STATE_ROOT": str(tmp_path / "state"),
            "TRADINGS_REPAIR_QUEUE": str(tmp_path / "repair.jsonl"),
            "PYTHON_BIN": str(fake_python),
            "REAL_TRADING_ENABLED": "false",
        }
    )

    result = subprocess.run(
        ["bash", str(ROOT / "shared/wrappers/job_crypto_sim.sh")],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 78
    assert "retired_legacy_runtime" in result.stderr
    assert "wait_for_tradingdatas_fresh_handoff" in result.stderr
    assert not python_sentinel.exists()


def test_active_runtime_files_contain_no_legacy_sharedsignals_routes() -> None:
    active_files = (
        "shared/wrappers/_common.sh",
        "shared/wrappers/job_crypto_sim.sh",
        "shared/wrappers/job_cn_futures_sim.sh",
        "shared/runtime_test/sharedsignals_v1_gate.py",
        "cron/health_check.sh",
    )
    forbidden = (
        "/source_status",
        "/cache/status",
        "/capabilities",
        "/tushare",
        "sharedsignals_source_status",
        "TradingagentDataReader",
        "DEFAULT_SHARED_SIGNALS_DB",
    )

    for relative_path in active_files:
        source = (ROOT / relative_path).read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in source, (relative_path, token)


def test_legacy_source_status_runtime_module_is_removed() -> None:
    assert not (ROOT / "shared/runtime_test/sharedsignals_source_status.py").exists()


def test_blocked_unmigrated_sim_wrappers_are_not_scheduled() -> None:
    blocked_wrappers = (
        "job_crypto_sim.sh",
        "job_cn_futures_sim.sh",
    )
    for relative_path in ("shared/crontab.txt", "crontab.txt"):
        active_lines = [
            line.strip()
            for line in (ROOT / relative_path).read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        for wrapper in blocked_wrappers:
            assert all(wrapper not in line for line in active_lines), (
                relative_path,
                wrapper,
            )


def test_runtime_v1_authority_variables_are_declared_without_live_defaults() -> None:
    env_loader = (ROOT / "shared/env_loader.sh").read_text(encoding="utf-8")
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")

    for name in (
        "TRADINGDATAS_SCHEMA_MAJOR",
        "TRADINGDATAS_RUNTIME_TRANSPORT",
        "TRADINGDATAS_API_TOKEN_FILE",
    ):
        assert f'export {name}="${{{name}:-}}"' in env_loader
        assert f"{name}=" in env_example
    assert "TRADINGDATAS_SCHEMA_MAJOR=1" not in env_example
    assert "TRADINGDATAS_RUNTIME_TRANSPORT=http-json-v1" not in env_example
    assert "TRADINGDATAS_API_TOKEN_FILE=/" not in env_example
