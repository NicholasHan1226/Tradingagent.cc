from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest

import Crypto.forty_symbol_observation_runtime as forty_runtime
from Crypto.forty_symbol_observation_runtime import (
    FORTY_SYMBOL_INVOCATION_BUDGET_SECONDS,
    FORTY_SYMBOL_RUNTIME_CONTRACT,
)
from Crypto.ten_symbol_observation_runtime import (
    CryptoTenSymbolObservationRuntimeError,
    FORTY_SYMBOL_RUNTIME_CONFIG,
    TEN_SYMBOL_RUNTIME_CONFIG,
    _family_event_id,
    _family_reason,
    _window_for_end,
    crypto_ten_symbol_observation_window,
)


def test_forty_symbol_runtime_has_independent_bounded_budget() -> None:
    assert FORTY_SYMBOL_INVOCATION_BUDGET_SECONDS == 300.0


def test_forty_symbol_runtime_has_distinct_public_identity() -> None:
    material = {"window_end": "2026-08-22T10:25:00Z"}

    assert FORTY_SYMBOL_RUNTIME_CONTRACT == (
        "tradingagent.crypto.forty_symbol_observation_runtime.v1"
    )
    assert (
        FORTY_SYMBOL_RUNTIME_CONFIG.runtime_contract
        == FORTY_SYMBOL_RUNTIME_CONTRACT
    )
    assert _family_event_id(
        FORTY_SYMBOL_RUNTIME_CONFIG, "observation", material
    ).startswith("crypto-forty-observation-")
    assert _family_reason(
        FORTY_SYMBOL_RUNTIME_CONFIG, "observation_recorded"
    ) == "crypto_forty_symbol_observation_recorded"

    assert _family_event_id(
        TEN_SYMBOL_RUNTIME_CONFIG, "observation", material
    ).startswith("crypto-ten-observation-")
    assert _family_reason(
        TEN_SYMBOL_RUNTIME_CONFIG, "observation_recorded"
    ) == "crypto_ten_symbol_observation_recorded"


def test_forty_symbol_window_waits_for_its_fixed_receipt_settle_boundary() -> None:
    window_end = datetime(2026, 8, 22, 11, 25, tzinfo=timezone.utc)

    before_settle = crypto_ten_symbol_observation_window(
        window_end + timedelta(seconds=269),
        config=FORTY_SYMBOL_RUNTIME_CONFIG,
    )
    assert before_settle.window_end == window_end - timedelta(minutes=5)
    assert before_settle.observation_cutoff == window_end - timedelta(seconds=30)

    settled = crypto_ten_symbol_observation_window(
        window_end + timedelta(seconds=270),
        config=FORTY_SYMBOL_RUNTIME_CONFIG,
    )
    assert settled.window_end == window_end
    assert settled.observation_cutoff == window_end + timedelta(seconds=270)

    jittered = crypto_ten_symbol_observation_window(
        window_end + timedelta(seconds=288),
        config=FORTY_SYMBOL_RUNTIME_CONFIG,
    )
    assert jittered == settled
    assert _window_for_end(
        window_end, config=FORTY_SYMBOL_RUNTIME_CONFIG
    ).observation_cutoff == window_end + timedelta(seconds=270)

    ten_symbol = crypto_ten_symbol_observation_window(
        window_end + timedelta(seconds=55),
        config=TEN_SYMBOL_RUNTIME_CONFIG,
    )
    assert ten_symbol.window_end == window_end
    assert ten_symbol.observation_cutoff == window_end + timedelta(seconds=55)


def test_forty_symbol_cli_emits_only_stable_core_failure_code(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        forty_runtime,
        "run_crypto_forty_symbol_observation_once",
        lambda **_: (_ for _ in ()).throw(
            CryptoTenSymbolObservationRuntimeError("runtime_cycle_failed")
        ),
    )

    assert forty_runtime.main(
        ["--runtime-manifest", "/tmp/manifest.json", "--token-file", "/tmp/token"]
    ) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.splitlines()[0] == (
        "crypto forty symbol observation runtime failed closed"
    )
    assert json.loads(captured.err.splitlines()[1]) == {
        "contract": "tradingagent.crypto.forty_symbol_runtime_failure.v1",
        "failure_code": "runtime_cycle_failed",
        "status": "failed_closed",
    }


def test_forty_symbol_cli_hides_unexpected_exception_details(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        forty_runtime,
        "run_crypto_forty_symbol_observation_once",
        lambda **_: (_ for _ in ()).throw(ValueError("token=/private/secret")),
    )

    assert forty_runtime.main(
        ["--runtime-manifest", "/tmp/manifest.json", "--token-file", "/tmp/token"]
    ) == 2

    captured = capsys.readouterr()
    assert "token=" not in captured.err
    assert json.loads(captured.err.splitlines()[1])["failure_code"] == (
        "runtime_unexpected_error"
    )
