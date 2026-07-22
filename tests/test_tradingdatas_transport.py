from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path
from typing import Any

import pytest

import shared.data.tradingdatas_auth as tradingdatas_auth
from shared.data.sharedsignals_v1 import ContractViolation
from shared.data.tradingdatas_transport import build_runtime_transport


def test_transport_is_owned_by_data_layer_with_runtime_gate_compatibility_only() -> (
    None
):
    module_name = "shared.data.tradingdatas_transport"

    assert importlib.util.find_spec(module_name) is not None
    transport = importlib.import_module(module_name)
    legacy_gate = importlib.import_module("shared.runtime_test.sharedsignals_v1_gate")

    exported_names = (
        "RejectRedirectHandler",
        "RuntimeGateConfigurationError",
        "TradingDatasAuthenticationError",
        "UrllibJSONV1Transport",
        "build_runtime_transport",
    )
    for name in exported_names:
        canonical = getattr(transport, name)
        assert getattr(legacy_gate, name) is canonical
        assert canonical.__module__ == module_name


class _OversizedResponse:
    status = 200

    def __enter__(self) -> _OversizedResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, limit: int) -> bytes:
        assert limit == 4_194_305
        return b"x" * limit


class _OversizedOpener:
    def open(self, _request: Any, *, timeout: float) -> _OversizedResponse:
        assert timeout == 3.0
        return _OversizedResponse()


def test_data_layer_transport_rejects_response_larger_than_four_mib(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        tradingdatas_auth,
        "_service_secret_roots",
        lambda: (tmp_path,),
    )
    token_file = tmp_path / "ta.token"
    token_file.write_text("fixture-token", encoding="ascii")
    token_file.chmod(0o600)
    transport = build_runtime_transport(
        "http-json-v1",
        token_file=token_file,
        base_url="https://tradingdatas.fixture.invalid",
    )
    transport._opener = _OversizedOpener()  # type: ignore[attr-defined]

    with pytest.raises(
        ContractViolation,
        match="TradingDatas V1 response exceeds 4 MiB",
    ):
        transport(
            method="GET",
            url="https://tradingdatas.fixture.invalid/v1/catalog",
            headers={"Accept": "application/json"},
            json_body=None,
            timeout_seconds=3.0,
        )
