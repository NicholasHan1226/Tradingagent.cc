from __future__ import annotations

from datetime import datetime, timedelta, timezone

from Crypto.forty_symbol_observation_runtime import (
    FORTY_SYMBOL_INVOCATION_BUDGET_SECONDS,
    FORTY_SYMBOL_RUNTIME_CONTRACT,
)
from Crypto.ten_symbol_observation_runtime import (
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
        window_end + timedelta(seconds=224),
        config=FORTY_SYMBOL_RUNTIME_CONFIG,
    )
    assert before_settle.window_end == window_end - timedelta(minutes=5)
    assert before_settle.observation_cutoff == window_end - timedelta(seconds=75)

    settled = crypto_ten_symbol_observation_window(
        window_end + timedelta(seconds=225),
        config=FORTY_SYMBOL_RUNTIME_CONFIG,
    )
    assert settled.window_end == window_end
    assert settled.observation_cutoff == window_end + timedelta(seconds=225)

    jittered = crypto_ten_symbol_observation_window(
        window_end + timedelta(seconds=228),
        config=FORTY_SYMBOL_RUNTIME_CONFIG,
    )
    assert jittered == settled
    assert _window_for_end(
        window_end, config=FORTY_SYMBOL_RUNTIME_CONFIG
    ).observation_cutoff == window_end + timedelta(seconds=225)

    ten_symbol = crypto_ten_symbol_observation_window(
        window_end + timedelta(seconds=55),
        config=TEN_SYMBOL_RUNTIME_CONFIG,
    )
    assert ten_symbol.window_end == window_end
    assert ten_symbol.observation_cutoff == window_end + timedelta(seconds=55)
