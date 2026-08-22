from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path

import Crypto.forty_symbol_observation_runtime as forty_runtime_module
import Crypto.ten_symbol_observation_runtime as runtime_module
from Crypto.forty_symbol_observation_runtime import (
    FORTY_SYMBOL_INVOCATION_BUDGET_SECONDS,
    FORTY_SYMBOL_RUNTIME_CONTRACT,
    run_crypto_forty_symbol_observation_once,
)
from Crypto.ten_symbol_observation_profile import build_forty_symbol_observation_profile
from Crypto.ten_symbol_observation_runtime import FORTY_SYMBOL_RUNTIME_CONFIG
from shared.data.sharedsignals_v1 import parse_catalog_envelope
from tests.test_crypto_forty_symbol_universe import (
    FORTY_CATALOG_VERSION,
    FORTY_WINDOW_END,
    FortySymbolFixtureTransport,
    _forty_catalog_payload,
)


def test_forty_symbol_runtime_has_independent_bounded_budget() -> None:
    assert FORTY_SYMBOL_INVOCATION_BUDGET_SECONDS == 300.0


def test_forty_symbol_runtime_uses_its_own_receipt_contract() -> None:
    assert FORTY_SYMBOL_RUNTIME_CONTRACT == (
        "tradingagent.crypto.forty_symbol_observation_runtime.v1"
    )
    assert FORTY_SYMBOL_RUNTIME_CONTRACT == FORTY_SYMBOL_RUNTIME_CONFIG.runtime_contract


def test_forty_symbol_runtime_emits_its_own_receipt_contract(
    monkeypatch, tmp_path: Path
) -> None:
    """Exercise the shared runtime through the forty-symbol wrapper."""

    output_root = (tmp_path / "crypto-forty-symbol-observation").resolve()
    token_file = (tmp_path / "tradingdatas-crypto-read.token").resolve()
    config = replace(FORTY_SYMBOL_RUNTIME_CONFIG, output_root=output_root)
    monkeypatch.setattr(forty_runtime_module, "FORTY_SYMBOL_RUNTIME_CONFIG", config)
    monkeypatch.setattr(forty_runtime_module, "FORTY_SYMBOL_OUTPUT_ROOT", output_root)
    monkeypatch.setattr(runtime_module, "RUNTIME_TOKEN_FILE", token_file)
    monkeypatch.setenv("REAL_TRADING_ENABLED", "false")

    profile = build_forty_symbol_observation_profile(
        parse_catalog_envelope(_forty_catalog_payload()),
        expected_catalog_version=FORTY_CATALOG_VERSION,
    )
    manifest_path = tmp_path / "crypto-forty-symbol-observation.runtime.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema": config.manifest_contract,
                "base_url": "http://127.0.0.1:18083",
                "catalog_version": FORTY_CATALOG_VERSION,
                "access_policy_id": "fixture-forty-symbol-observation",
                "output_root": str(output_root),
                "profile_sha256": profile.profile_sha256,
                "profile": profile.to_payload(),
                "safety": {
                    "real_trading_enabled": False,
                    "production_eligible": False,
                    "execution_authority": False,
                    "testnet_enabled": False,
                    "live_broker_enabled": False,
                    "model_network_enabled": False,
                    "automatic_promotion_enabled": False,
                    "automatic_risk_expansion_enabled": False,
                },
            },
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    manifest_path.chmod(0o600)

    receipt = run_crypto_forty_symbol_observation_once(
        runtime_manifest=manifest_path,
        token_file=token_file,
        output_root=output_root,
        now=FORTY_WINDOW_END + timedelta(seconds=55),
        transport_factory=lambda *_args, **_kwargs: FortySymbolFixtureTransport(),
    )

    assert receipt["contract"] == FORTY_SYMBOL_RUNTIME_CONTRACT
    assert receipt["status"] == "completed"
