from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import copy

import pytest

from Crypto.factor_research import build_factor_snapshot, build_forward_label
from Crypto.factor_strategy_evaluation import (
    CryptoFactorStrategyEvaluationError,
    _sample_binding_sha256,
    build_factor_strategy_evaluation,
)
from Crypto.adapter import CryptoAdapter

START = datetime(2026, 8, 1, tzinfo=timezone.utc)
AS_OF = "2026-08-01T04:00:00Z"


def _bars(start: datetime, rising: bool) -> list[dict[str, str]]:
    rows = []
    for index in range(13):
        close = Decimal("100") + (Decimal(index) if rising else -Decimal(index))
        rows.append({
            "open_time": (start + timedelta(minutes=index * 5)).isoformat().replace("+00:00", "Z"),
            "open": str(close), "high": str(close + 1), "low": str(close - 1),
            "close": str(close), "base_volume": str(10 + index),
            "quote_volume": str(close * (10 + index)),
        })
    return rows


def _evidence(slot: datetime, marker: str) -> dict[str, str]:
    return {
        "receipt_id": f"receipt:{marker}", "lineage_sha256": marker * 64,
        "data_through": (slot + timedelta(hours=2, minutes=5)).isoformat().replace("+00:00", "Z"),
        "observed_at": (slot + timedelta(hours=2, minutes=5, seconds=1)).isoformat().replace("+00:00", "Z"),
    }


def _future_evidence(slot: datetime, marker: str) -> dict[str, str]:
    return {
        "receipt_id": f"receipt:{marker}", "lineage_sha256": marker * 64,
        "data_through": (slot + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
        "observed_at": (slot + timedelta(minutes=5, seconds=1)).isoformat().replace("+00:00", "Z"),
    }


def _sample(slot: datetime = START, rising: bool = True) -> dict[str, object]:
    snapshot = build_factor_snapshot(
        observation_id=f"obs-{slot.hour}-{rising}", symbol="BTCUSDT",
        bars=_bars(slot, rising), evidence=_evidence(slot, "a"),
    )
    future = slot + timedelta(hours=2)
    label = build_forward_label(
        snapshot=snapshot, horizon_minutes=60,
        future_market_slot=future.isoformat().replace("+00:00", "Z"),
        entry_price="100", exit_price="102" if rising else "98",
        future_evidence=_future_evidence(future, "b"),
    )
    sample = {
        "snapshot": snapshot, "label": label,
        "segment_id": "crypto-5m-segment-20260801T000000Z",
        "future_segment_id": "crypto-5m-segment-20260801T000000Z",
        "source_completion_sha256": "1" * 64, "future_completion_sha256": "2" * 64,
        "future_observation_id": "future-observation-1h",
        "source_completion_proof": {"completion_sha256": "1" * 64, "observation_id": snapshot["observation_id"], "market_slot": snapshot["market_slot"]},
        "future_completion_proof": {"completion_sha256": "2" * 64, "observation_id": "future-observation-1h", "market_slot": label["future_market_slot"]},
        "cost_policy": {"cost_policy_id": "crypto-round-trip-taker-v1", "fee_rate": "0.001", "slippage_bps_each_side": "2"},
    }
    sample["sample_binding_sha256"] = _sample_binding_sha256(sample)
    return sample


def test_deterministic_nonzero_fixture_artifact_and_pending() -> None:
    samples = [_sample(), _sample(START + timedelta(hours=1), rising=False), {"status": "pending"}]
    first = build_factor_strategy_evaluation(samples=samples, evaluation_as_of=AS_OF)
    second = build_factor_strategy_evaluation(samples=samples, evaluation_as_of=AS_OF)
    assert first == second
    assert first["strategy_name"] == "momentum"
    assert len(first["strategy_version"]) == 64
    assert first["configured_maturity"] == "training"
    assert first["evaluated_status"] == "exploratory_insufficient_edge"
    assert first["sample_count"] == 3
    assert first["resolved_count"] == 2 and first["pending_count"] == 1 and first["excluded_count"] == 0
    assert first["resolved_coverage"] == "0.6666666666666666666666666667"
    assert first["metrics"]["cost_adjusted_net_return"] is not None
    assert first["recommendation"]["shadow_only_action"] in {"retain_for_more_evidence", "downweight", "disable"}
    assert first["promotion"] is False and first["execution"] is False and first["live"] is False


@pytest.mark.parametrize("mutator,reason", [
    (lambda s: s.update(future_segment_id="crypto-5m-segment-20260801T010000Z"), "evaluation_cross_gap_label"),
    (lambda s: s["label"].update(forward_label_sha256="f" * 64), "evaluation_receipt_or_lineage_binding_invalid"),
])
def test_invalid_resolved_input_fails_closed(mutator, reason: str) -> None:
    sample = _sample()
    mutator(sample)
    with pytest.raises(CryptoFactorStrategyEvaluationError, match=reason):
        build_factor_strategy_evaluation(samples=[sample], evaluation_as_of=AS_OF)


def test_cost_mismatch_fails_closed_after_binding_recomputed() -> None:
    sample = _sample()
    sample["cost_policy"]["fee_rate"] = "0.002"  # type: ignore[index]
    sample["sample_binding_sha256"] = _sample_binding_sha256(sample)
    with pytest.raises(CryptoFactorStrategyEvaluationError, match="evaluation_cost_policy_mismatch"):
        build_factor_strategy_evaluation(samples=[sample], evaluation_as_of=AS_OF)


def test_strategy_hash_tamper_fails_closed() -> None:
    strategy = CryptoAdapter().get_strategy_config()["strategies"]["momentum"]
    import hashlib, json
    expected = hashlib.sha256(json.dumps(strategy, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode()).hexdigest()
    with pytest.raises(CryptoFactorStrategyEvaluationError, match="evaluation_strategy_hash_mismatch"):
        build_factor_strategy_evaluation(samples=[_sample()], evaluation_as_of=AS_OF, expected_strategy_version="f" * 64)


def test_same_length_completion_digest_tamper_fails_closed() -> None:
    sample = _sample()
    sample["source_completion_sha256"] = "9" * 64
    with pytest.raises(CryptoFactorStrategyEvaluationError, match="evaluation_sample_binding_invalid"):
        build_factor_strategy_evaluation(samples=[sample], evaluation_as_of=AS_OF)


def test_future_completion_observation_mismatch_fails_closed() -> None:
    sample = _sample()
    sample["future_observation_id"] = "different-future-observation"
    sample["sample_binding_sha256"] = _sample_binding_sha256(sample)
    with pytest.raises(CryptoFactorStrategyEvaluationError, match="evaluation_source_binding_invalid"):
        build_factor_strategy_evaluation(samples=[sample], evaluation_as_of=AS_OF)


def test_all_abstain_recommends_disable() -> None:
    sample = _sample(rising=False)
    result = build_factor_strategy_evaluation(samples=[sample], evaluation_as_of=AS_OF)
    assert result["metrics"]["signal_count"] == 0
    assert result["recommendation"]["shadow_only_action"] == "disable"


def test_future_after_as_of_fails_closed() -> None:
    with pytest.raises(CryptoFactorStrategyEvaluationError, match="evaluation_future_after_as_of"):
        build_factor_strategy_evaluation(samples=[_sample()], evaluation_as_of="2026-08-01T02:00:00Z")
