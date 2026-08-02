from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib

import pytest

from Ashare.trading_copilot_forecast_evaluation import (
    ForecastEvaluationError,
    evaluate_forecasts,
)


NOW = datetime(2026, 8, 2, 4, tzinfo=timezone.utc)


def _sample(index: int, *, include_kronos: bool = True) -> dict:
    decision = NOW - timedelta(days=50 - index)
    label = decision + timedelta(hours=1)
    actual = 0.01 if index % 2 == 0 else -0.008
    predictions = {
        "naive_last_value": {"pointReturn": 0.002},
        "linear_ridge_baseline": {"pointReturn": actual * 0.7},
    }
    if include_kronos:
        predictions["kronos_challenger"] = {
            "pointReturn": actual * 0.95,
            "lowerReturn": actual - 0.003,
            "upperReturn": actual + 0.003,
        }
    return {
        "sampleId": f"sample-{index}", "symbol": "600000.SH",
        "decisionTime": decision.isoformat(), "labelTime": label.isoformat(),
        "actualReturn": actual, "sourceReceiptId": f"receipt-{index}",
        "sourceReceiptSha256": hashlib.sha256(str(index).encode()).hexdigest(),
        "predictions": predictions,
    }


def _payload(count: int, *, include_kronos: bool = True) -> dict:
    return {
        "contractId": "tradingagent.trading_copilot_forecast_evaluation_input.v1",
        "generatedAt": NOW.isoformat(), "horizon": "60m", "roundTripCostBps": 12,
        "samples": [_sample(index, include_kronos=include_kronos) for index in range(count)],
    }


def test_kronos_can_only_enter_shadow_comparison_after_oos_gate() -> None:
    result = evaluate_forecasts(_payload(45))
    assert result["challengerGate"]["status"] == "eligible_for_shadow_comparison"
    assert result["challengerGate"]["promotionAuthority"] is False
    assert result["authority"]["orders"] is False
    assert result["metrics"]["kronos_challenger"]["probabilitySemantics"] is None


def test_small_sample_and_missing_kronos_remain_blocked() -> None:
    small = evaluate_forecasts(_payload(10))
    assert small["challengerGate"]["status"] == "blocked"
    missing = evaluate_forecasts(_payload(45, include_kronos=False))
    assert missing["challengerGate"]["status"] == "blocked"
    assert "尚未提供" in missing["challengerGate"]["reasons"][0]


def test_rejects_future_label_and_model_sample_mismatch() -> None:
    future = _payload(2)
    future["samples"][0]["labelTime"] = (NOW + timedelta(minutes=1)).isoformat()
    with pytest.raises(ForecastEvaluationError, match="future_leakage"):
        evaluate_forecasts(future)
    mismatch = _payload(2)
    del mismatch["samples"][0]["predictions"]["kronos_challenger"]
    with pytest.raises(ForecastEvaluationError, match="sample_set_mismatch"):
        evaluate_forecasts(mismatch)


def test_post_cost_metric_is_not_labeled_probability() -> None:
    result = evaluate_forecasts(_payload(4))
    metrics = result["metrics"]["linear_ridge_baseline"]
    assert metrics["meanPostCostDirectionalUtility"] < 0.011
    assert metrics["directionalAccuracy"] == 1
    assert metrics["intervalCoverage"] is None
