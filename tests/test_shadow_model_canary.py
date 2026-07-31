from __future__ import annotations

import pytest

from scripts.run_shadow_model_canary import run_canary


@pytest.mark.parametrize("backend", ("ridge", "logistic"))
def test_dependency_free_canaries_are_bounded_and_authority_free(
    backend: str,
) -> None:
    result = run_canary(backend=backend)
    assert result["backend"] == backend
    assert result["fixture_only"] is True
    assert result["predictive_validation_input_eligible"] is False
    assert result["authority"] == "none"
    assert result["execution_authority"] is False
    assert result["capital_authority"] is False
    assert result["risk_expansion_allowed"] is False
    assert result["automatic_promotion_enabled"] is False
    assert result["real_trading_enabled"] is False
    assert result["model_network_used"] is False
    assert result["score_count"] == 4


def test_canary_refuses_real_trading_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REAL_TRADING_ENABLED", "true")
    with pytest.raises(RuntimeError, match="real_trading_must_remain_disabled"):
        run_canary(backend="ridge")
