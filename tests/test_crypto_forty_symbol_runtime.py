from __future__ import annotations

from Crypto.forty_symbol_observation_runtime import (
    FORTY_SYMBOL_INVOCATION_BUDGET_SECONDS,
    FORTY_SYMBOL_RUNTIME_CONTRACT,
)
from Crypto.ten_symbol_observation_runtime import (
    FORTY_SYMBOL_RUNTIME_CONFIG,
    TEN_SYMBOL_RUNTIME_CONFIG,
    _family_event_id,
    _family_reason,
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
