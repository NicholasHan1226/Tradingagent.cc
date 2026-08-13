from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import copy
import hashlib
import json

import pytest

from Crypto.factor_research import _signal, build_factor_snapshot, build_forward_label
from Crypto.factor_strategy_evaluation import (
    CryptoFactorStrategyEvaluationError,
    _sample_binding_sha256,
    build_factor_strategy_evaluation,
)
from Crypto.adapter import CryptoAdapter

START = datetime(2026, 8, 1, tzinfo=timezone.utc)
AS_OF = "2026-08-01T04:00:00Z"


def _bars(start: datetime, rising: bool, pullback: bool = False) -> list[dict[str, str]]:
    rows = []
    for index in range(13):
        if pullback:
            close = (
                Decimal("100") + Decimal(index) * 2
                if index < 9
                else Decimal("116") - Decimal(index - 8)
            )
        else:
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


def _sample(slot: datetime = START, rising: bool = True, pullback: bool = False) -> dict[str, object]:
    snapshot = build_factor_snapshot(
        observation_id=f"obs-{slot.hour}-{rising}", symbol="BTCUSDT",
        bars=_bars(slot, rising, pullback), evidence=_evidence(slot, "a"),
    )
    future = slot + timedelta(hours=2)
    label = build_forward_label(
        snapshot=snapshot, horizon_minutes=60,
        future_market_slot=future.isoformat().replace("+00:00", "Z"),
        entry_price="100", exit_price="102" if rising else "98",
        future_evidence=_future_evidence(future, "b"),
    )
    source_id = snapshot["observation_id"]
    future_id = "future-observation-1h"
    segment_id = "crypto-5m-segment-20260801T000000Z"

    def projection_proof(observation_id: str, market_slot: str, completion_marker: str) -> dict[str, object]:
        source_snapshot = snapshot if completion_marker == "1" else {
            **snapshot,
            "observation_id": observation_id,
            "market_slot": market_slot,
            "evidence_receipt_id": label["future_evidence_receipt_id"],
            "evidence_lineage_sha256": label["future_evidence_lineage_sha256"],
            "observed_at": label["future_observed_at"],
            "data_through": label["future_data_through"],
        }
        source_snapshot["factor_snapshot_sha256"] = hashlib.sha256(json.dumps(
            {key: value for key, value in source_snapshot.items() if key != "factor_snapshot_sha256"},
            ensure_ascii=True, separators=(",", ":"), sort_keys=True,
        ).encode()).hexdigest()
        observation_content_sha256 = completion_marker * 64
        completion = {"contract": "tradingagent.crypto.delayed_paper_completion.v1",
                      "observation_id": observation_id, "observation_content_sha256": observation_content_sha256,
                      "status": "completed"}
        completion["completion_sha256"] = hashlib.sha256(json.dumps(completion, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode()).hexdigest()
        record = {"contract": "tradingagent.crypto.factor_projection.v1", "observation_id": observation_id,
                  "market_slot": market_slot, "segment_id": segment_id, "snapshots": {"BTCUSDT": source_snapshot},
                  "source_observation_content_sha256": observation_content_sha256,
                  "source_completion_sha256": completion["completion_sha256"]}
        record["factor_projection_sha256"] = hashlib.sha256(json.dumps(record, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode()).hexdigest()
        receipt = {"contract": "tradingagent.crypto.factor_projection_receipt.v1", "observation_id": observation_id,
                   "source_completion_sha256": completion["completion_sha256"],
                   "factor_projection_sha256": record["factor_projection_sha256"]}
        receipt["projection_receipt_sha256"] = hashlib.sha256(json.dumps(receipt, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode()).hexdigest()
        checkpoint = {"contract": "tradingagent.crypto.factor_projection_checkpoint.v1",
                      "sequence": 1 if completion_marker == "1" else 2, "observation_id": observation_id,
                      "previous_checkpoint_sha256": None,
                      "source_completion_sha256": completion["completion_sha256"],
                      "projection_receipt_sha256": receipt["projection_receipt_sha256"]}
        checkpoint["checkpoint_sha256"] = hashlib.sha256(json.dumps(checkpoint, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode()).hexdigest()
        return {"completion": completion, "record": record, "receipt": receipt, "checkpoint": checkpoint}

    source_proof = projection_proof(source_id, snapshot["market_slot"], "1")
    future_proof = projection_proof(future_id, label["future_market_slot"], "2")
    future_proof["checkpoint"]["previous_checkpoint_sha256"] = source_proof["checkpoint"]["checkpoint_sha256"]  # type: ignore[index]
    future_checkpoint = future_proof["checkpoint"]  # type: ignore[assignment]
    future_checkpoint["checkpoint_sha256"] = hashlib.sha256(json.dumps(
        {key: value for key, value in future_checkpoint.items() if key != "checkpoint_sha256"},
        ensure_ascii=True, separators=(",", ":"), sort_keys=True,
    ).encode()).hexdigest()
    checkpoint_chain = [source_proof["checkpoint"], future_proof["checkpoint"]]
    sample = {
        "snapshot": snapshot, "label": label,
        "segment_id": segment_id, "future_segment_id": segment_id,
        "source_completion_sha256": source_proof["completion"]["completion_sha256"],  # type: ignore[index]
        "future_completion_sha256": future_proof["completion"]["completion_sha256"],  # type: ignore[index]
        "future_observation_id": future_id,
        "source_projection_proof": source_proof, "future_projection_proof": future_proof,
        "projection_checkpoint_chain": checkpoint_chain,
        "expected_checkpoint_head_sha256": future_checkpoint["checkpoint_sha256"],
        "cost_policy": {"cost_policy_id": "crypto-round-trip-taker-v1", "fee_rate": "0.001", "slippage_bps_each_side": "2"},
    }
    sample["sample_binding_sha256"] = _sample_binding_sha256(sample)
    return sample


def test_deterministic_nonzero_fixture_artifact() -> None:
    samples = [_sample(), _sample(START + timedelta(hours=1), rising=False), {"status": "pending", "sample_id": "next-1h"}]
    first = build_factor_strategy_evaluation(samples=samples, evaluation_as_of=AS_OF)
    second = build_factor_strategy_evaluation(samples=samples, evaluation_as_of=AS_OF)
    assert first == second
    assert first["strategy_name"] == "momentum"
    assert first["factor_hypothesis_id"] == "time_series_momentum_v1"
    assert len(first["strategy_version"]) == 64
    assert first["configured_maturity"] == "training"
    assert first["evaluated_status"] == "exploratory_insufficient_edge"
    assert first["sample_count"] == 3
    assert first["resolved_count"] == 2 and first["pending_count"] == 1 and first["excluded_count"] == 0
    assert first["resolved_coverage"] == "0.6666666666666666666666666667"
    assert first["metrics"]["cost_adjusted_net_return"] is not None
    assert first["metrics"]["signal_count"] == 1
    assert first["metrics"]["abstention_count"] == 1
    assert first["cash_baseline"] == {
        "resolved_count": 2, "signal_count": 0, "abstention_count": 2,
        "coverage": "0", "hit_rate": None, "cost_adjusted_net_return": "0",
        "baseline_delta": None, "drawdown": "0", "turnover": "0", "round_trip_leg_rate": "0", "metric_basis": "cash_no_position",
    }
    assert first["metrics"]["cash_baseline_delta"] == first["metrics"]["cost_adjusted_net_return"]
    assert Decimal(first["baseline"]["drawdown"]) > 0
    assert first["recommendation"]["shadow_only_action"] in {"retain_for_more_evidence", "downweight", "disable"}
    assert first["promotion"] is False and first["execution"] is False and first["live"] is False


def test_trend_pullback_is_distinct_allowlisted_pair_on_same_samples() -> None:
    samples = [_sample(pullback=True), _sample(START + timedelta(hours=1), rising=False)]
    snapshots = [sample["snapshot"] for sample in samples]
    momentum_signals = [_signal("time_series_momentum_v1", snapshot) for snapshot in snapshots]
    trend_signals = [_signal("trend_pullback_v1", snapshot) for snapshot in snapshots]
    momentum = build_factor_strategy_evaluation(samples=samples, evaluation_as_of=AS_OF, strategy_name="momentum")
    trend = build_factor_strategy_evaluation(samples=samples, evaluation_as_of=AS_OF, strategy_name="trend")
    assert momentum["strategy_name"] == "momentum"
    assert momentum["factor_hypothesis_id"] == "time_series_momentum_v1"
    assert trend["strategy_name"] == "trend"
    assert trend["factor_hypothesis_id"] == "trend_pullback_v1"
    assert momentum["strategy_version"] != trend["strategy_version"]
    assert momentum["metrics"]["signal_count"] == 0
    assert trend["metrics"]["signal_count"] == 1
    assert momentum_signals == [False, False]
    assert trend_signals == [True, False]
    assert momentum_signals != trend_signals
    assert momentum["metrics"]["signal_count"] != trend["metrics"]["signal_count"]
    assert trend == build_factor_strategy_evaluation(
        samples=samples, evaluation_as_of=AS_OF, strategy_name="trend"
    )


def test_unknown_strategy_selection_fails_closed() -> None:
    with pytest.raises(CryptoFactorStrategyEvaluationError, match="evaluation_strategy_invalid"):
        build_factor_strategy_evaluation(samples=[_sample()], evaluation_as_of=AS_OF, strategy_name="unknown")


def test_volume_breakout_is_distinct_allowlisted_pair_on_same_samples() -> None:
    samples = [_sample(pullback=True), _sample(START + timedelta(hours=1))]
    snapshots = [sample["snapshot"] for sample in samples]
    trend_signals = [_signal("trend_pullback_v1", snapshot) for snapshot in snapshots]
    volume_signals = [_signal("volume_breakout_v1", snapshot) for snapshot in snapshots]
    trend = build_factor_strategy_evaluation(samples=samples, evaluation_as_of=AS_OF, strategy_name="trend")
    volatility = build_factor_strategy_evaluation(
        samples=samples, evaluation_as_of=AS_OF, strategy_name="volatility"
    )
    assert volatility["strategy_name"] == "volatility"
    assert volatility["factor_hypothesis_id"] == "volume_breakout_v1"
    assert volatility["strategy_version"] != trend["strategy_version"]
    assert trend_signals == [True, False]
    assert volume_signals == [False, True]
    assert trend_signals != volume_signals
    assert trend["metrics"]["signal_count"] == 1
    assert volatility["metrics"]["signal_count"] == 1
    assert volatility == build_factor_strategy_evaluation(
        samples=samples, evaluation_as_of=AS_OF, strategy_name="volatility"
    )


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


def test_wrong_existing_strategy_content_fails_closed(tmp_path) -> None:
    strategy = CryptoAdapter().get_strategy_config()["strategies"]["momentum"]
    wrong = dict(strategy)
    wrong["name"] = "not-momentum"
    path = tmp_path / "momentum.json"
    path.write_text(json.dumps(wrong), encoding="utf-8")
    with pytest.raises(CryptoFactorStrategyEvaluationError, match="evaluation_strategy_invalid"):
        build_factor_strategy_evaluation(
            samples=[_sample()], evaluation_as_of=AS_OF, strategy_dir=tmp_path
        )


def test_same_length_completion_digest_tamper_fails_closed() -> None:
    sample = _sample()
    sample["source_completion_sha256"] = "9" * 64
    with pytest.raises(CryptoFactorStrategyEvaluationError, match="evaluation_sample_binding_invalid"):
        build_factor_strategy_evaluation(samples=[sample], evaluation_as_of=AS_OF)


def test_projection_completion_observation_mismatch_fails_closed() -> None:
    sample = _sample()
    sample["future_projection_proof"]["completion"]["observation_id"] = "different-future-observation"  # type: ignore[index]
    sample["sample_binding_sha256"] = _sample_binding_sha256(sample)
    with pytest.raises(CryptoFactorStrategyEvaluationError, match="evaluation_projection_binding_invalid"):
        build_factor_strategy_evaluation(samples=[sample], evaluation_as_of=AS_OF)


def test_projection_receipt_or_checkpoint_tamper_fails_closed() -> None:
    for role, member in (("source", "receipt"), ("future", "checkpoint")):
        sample = _sample()
        sample[f"{role}_projection_proof"][member]["observation_id"] = "tampered"  # type: ignore[index]
        sample["sample_binding_sha256"] = _sample_binding_sha256(sample)
        with pytest.raises(CryptoFactorStrategyEvaluationError, match="evaluation_(projection_binding|checkpoint_chain)_invalid"):
            build_factor_strategy_evaluation(samples=[sample], evaluation_as_of=AS_OF)


def test_all_abstain_recommends_disable() -> None:
    sample = _sample(rising=False)
    result = build_factor_strategy_evaluation(samples=[sample], evaluation_as_of=AS_OF)
    assert result["metrics"]["signal_count"] == 0
    assert result["recommendation"]["shadow_only_action"] == "disable"


def test_negative_signalled_return_recommends_downweight() -> None:
    sample = _sample()
    label = sample["label"]  # type: ignore[assignment]
    entry = Decimal(label["entry_price"])
    exit_ = Decimal("98")
    fee = Decimal(label["fee_rate"])
    label["exit_price"] = "98"
    label["gross_return"] = format(exit_ / entry - Decimal("1"), "f")
    label["net_return"] = format(exit_ * (Decimal("1") - fee) / (entry * (Decimal("1") + fee)) - Decimal("1"), "f")
    label["forward_label_sha256"] = hashlib.sha256(  # type: ignore[index]
        json.dumps(
            {key: value for key, value in label.items() if key != "forward_label_sha256"},
            ensure_ascii=True, allow_nan=False, separators=(",", ":"), sort_keys=True,
        ).encode()
    ).hexdigest()
    sample["sample_binding_sha256"] = _sample_binding_sha256(sample)
    result = build_factor_strategy_evaluation(samples=[sample], evaluation_as_of=AS_OF)
    assert Decimal(result["metrics"]["cost_adjusted_net_return"]) < 0
    assert result["metrics"]["cash_baseline_delta"] == result["metrics"]["cost_adjusted_net_return"]
    assert result["recommendation"]["shadow_only_action"] == "downweight"


def test_return_self_hash_cannot_override_price_and_fee_semantics() -> None:
    sample = _sample()
    sample["label"]["net_return"] = "-0.99"  # type: ignore[index]
    label = sample["label"]  # type: ignore[assignment]
    label["forward_label_sha256"] = hashlib.sha256(json.dumps(
        {key: value for key, value in label.items() if key != "forward_label_sha256"},
        ensure_ascii=True, allow_nan=False, separators=(",", ":"), sort_keys=True,
    ).encode()).hexdigest()
    sample["sample_binding_sha256"] = _sample_binding_sha256(sample)
    with pytest.raises(CryptoFactorStrategyEvaluationError, match="evaluation_label_return_invalid"):
        build_factor_strategy_evaluation(samples=[sample], evaluation_as_of=AS_OF)


def test_duplicate_and_malformed_pending_fail_closed() -> None:
    sample = _sample()
    with pytest.raises(CryptoFactorStrategyEvaluationError, match="evaluation_sample_duplicate"):
        build_factor_strategy_evaluation(samples=[sample, copy.deepcopy(sample)], evaluation_as_of=AS_OF)
    with pytest.raises(CryptoFactorStrategyEvaluationError, match="evaluation_pending_identity_invalid"):
        build_factor_strategy_evaluation(samples=[{"status": "pending"}], evaluation_as_of=AS_OF)
    with pytest.raises(CryptoFactorStrategyEvaluationError, match="evaluation_sample_duplicate"):
        build_factor_strategy_evaluation(
            samples=[{"status": "pending", "sample_id": "same"}, {"status": "pending", "sample_id": "same"}],
            evaluation_as_of=AS_OF,
        )


@pytest.mark.parametrize("mutator", [
    lambda sample: sample["source_projection_proof"]["completion"].update(status="pending"),
    lambda sample: sample["source_projection_proof"]["record"].update(execution_authority=True),
    lambda sample: sample["future_projection_proof"]["record"]["snapshots"]["BTCUSDT"].update(observed_at="2027-01-01T00:00:00Z"),
])
def test_projection_authority_or_future_pit_tamper_fails_closed(mutator) -> None:
    sample = _sample()
    mutator(sample)
    for role in ("source", "future"):
        proof = sample[f"{role}_projection_proof"]
        for member, hash_field in (("completion", "completion_sha256"), ("record", "factor_projection_sha256"),
                                   ("receipt", "projection_receipt_sha256"), ("checkpoint", "checkpoint_sha256")):
            item = proof[member]
            item[hash_field] = hashlib.sha256(json.dumps(
                {key: value for key, value in item.items() if key != hash_field},
                ensure_ascii=True, separators=(",", ":"), sort_keys=True,
            ).encode()).hexdigest()
    sample["sample_binding_sha256"] = _sample_binding_sha256(sample)
    with pytest.raises(CryptoFactorStrategyEvaluationError, match="evaluation_projection_binding_invalid"):
        build_factor_strategy_evaluation(samples=[sample], evaluation_as_of=AS_OF)


def test_future_after_as_of_fails_closed() -> None:
    with pytest.raises(CryptoFactorStrategyEvaluationError, match="evaluation_future_after_as_of"):
        build_factor_strategy_evaluation(samples=[_sample()], evaluation_as_of="2026-08-01T02:00:00Z")
