"""Phase-1 detached evaluation of one existing Crypto strategy and factor."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from Crypto.adapter import CryptoAdapter
from Crypto.factor_research import (
    FACTOR_RESEARCH_CONTRACT, FACTOR_SET_ID, FACTOR_SET_VERSION,
    CryptoFactorResearchError, _assert_label_integrity, _assert_snapshot_integrity,
    _signal,
)
from Crypto.round_trip_capital import SLIPPAGE_BPS, TAKER_FEE_RATE

EVALUATION_CONTRACT = "tradingagent.crypto.factor_strategy_evaluation.v1"
HORIZON_MINUTES = 60
COST_POLICY_ID = "crypto-round-trip-taker-v1"
SELECTED_STRATEGY = "momentum"
SELECTED_HYPOTHESIS = "time_series_momentum_v1"


class CryptoFactorStrategyEvaluationError(RuntimeError):
    """Stable fail-closed error for Phase-1 inputs."""


def _json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise CryptoFactorStrategyEvaluationError("evaluation_payload_invalid") from exc


def _sha(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _decimal(value: Any, field: str) -> Decimal:
    if isinstance(value, bool) or value in (None, ""):
        raise CryptoFactorStrategyEvaluationError(f"evaluation_{field}_invalid")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise CryptoFactorStrategyEvaluationError(f"evaluation_{field}_invalid") from exc
    if not result.is_finite():
        raise CryptoFactorStrategyEvaluationError(f"evaluation_{field}_invalid")
    return result


def _utc(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise CryptoFactorStrategyEvaluationError(f"evaluation_{field}_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CryptoFactorStrategyEvaluationError(f"evaluation_{field}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise CryptoFactorStrategyEvaluationError(f"evaluation_{field}_invalid")
    return parsed.astimezone(timezone.utc)


def _text(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _safe() -> dict[str, Any]:
    return {"authority": "none", "read_only": True, "research_only": True,
            "simulation_only": True, "promotion": False, "execution": False,
            "execution_eligible": False, "live": False,
            "real_trading_enabled": False, "model_training": "deferred",
            "automatic_risk_expansion_enabled": False}


def _sample_binding_sha256(sample: Mapping[str, Any]) -> str:
    """Hash the stable, already-present evidence that identifies one sample."""

    snapshot = sample.get("snapshot")
    label = sample.get("label")
    cost_policy = sample.get("cost_policy")
    if not isinstance(snapshot, Mapping) or not isinstance(label, Mapping) or not isinstance(cost_policy, Mapping):
        raise CryptoFactorStrategyEvaluationError("evaluation_sample_binding_invalid")
    material = {
        "snapshot_factor_snapshot_sha256": snapshot.get("factor_snapshot_sha256"),
        "label_forward_label_sha256": label.get("forward_label_sha256"),
        "segment_id": sample.get("segment_id"),
        "future_segment_id": sample.get("future_segment_id"),
        "source_completion_sha256": sample.get("source_completion_sha256"),
        "future_completion_sha256": sample.get("future_completion_sha256"),
        "cost_policy": cost_policy,
    }
    return _sha(material)


def _strategy(strategy_dir: Path | str | None) -> tuple[dict[str, Any], dict[str, Any]]:
    loaded = CryptoAdapter(strategy_dir=Path(strategy_dir) if strategy_dir else None).get_strategy_config().get("strategies")
    content = loaded.get(SELECTED_STRATEGY) if isinstance(loaded, Mapping) else None
    if not isinstance(content, Mapping) or content.get("name") != SELECTED_STRATEGY or content.get("enabled") is not True:
        raise CryptoFactorStrategyEvaluationError("evaluation_strategy_invalid")
    normalized = json.loads(_json(content))
    return normalized, {"strategy_name": SELECTED_STRATEGY, "strategy_version": _sha(normalized), "configured_maturity": normalized.get("maturity")}


def _resolved(sample: Mapping[str, Any], evaluation_as_of: datetime) -> tuple[dict[str, Any], Decimal]:
    snapshot, label = sample.get("snapshot"), sample.get("label")
    if not isinstance(snapshot, Mapping) or not isinstance(label, Mapping):
        raise CryptoFactorStrategyEvaluationError("evaluation_sample_unresolved")
    try:
        _assert_snapshot_integrity(snapshot)
        _assert_label_integrity(label, snapshot=snapshot)
    except CryptoFactorResearchError as exc:
        raise CryptoFactorStrategyEvaluationError("evaluation_receipt_or_lineage_binding_invalid") from exc
    if label.get("label_status") != "observed_future_outcome" or label.get("horizon_minutes") != HORIZON_MINUTES:
        raise CryptoFactorStrategyEvaluationError("evaluation_label_not_resolved")
    if not isinstance(sample.get("segment_id"), str) or sample.get("future_segment_id") != sample.get("segment_id"):
        raise CryptoFactorStrategyEvaluationError("evaluation_cross_gap_label")
    claimed_binding = sample.get("sample_binding_sha256")
    if not isinstance(claimed_binding, str) or claimed_binding != _sample_binding_sha256(sample):
        raise CryptoFactorStrategyEvaluationError("evaluation_sample_binding_invalid")
    future_slot = _utc(label.get("future_market_slot"), "future_market_slot")
    for role, expected_id, expected_slot in (
        ("source", snapshot.get("observation_id"), _utc(snapshot.get("market_slot"), "market_slot")),
        ("future", label.get("observation_id"), future_slot),
    ):
        digest_key = f"{role}_completion_sha256"
        proof = sample.get(f"{role}_completion_proof")
        if (
            not isinstance(sample.get(digest_key), str)
            or len(sample[digest_key]) != 64
            or not isinstance(proof, Mapping)
            or proof.get("completion_sha256") != sample[digest_key]
            or proof.get("observation_id") != expected_id
            or proof.get("market_slot") != expected_slot.isoformat().replace("+00:00", "Z")
        ):
            raise CryptoFactorStrategyEvaluationError("evaluation_source_binding_invalid")
    if future_slot > evaluation_as_of or _utc(label.get("future_observed_at"), "future_observed_at") > evaluation_as_of or _utc(label.get("future_data_through"), "future_data_through") > evaluation_as_of:
        raise CryptoFactorStrategyEvaluationError("evaluation_future_after_as_of")
    cost = sample.get("cost_policy")
    if not isinstance(cost, Mapping) or cost.get("cost_policy_id") != COST_POLICY_ID or _decimal(cost.get("fee_rate"), "cost_fee_rate") != TAKER_FEE_RATE or _decimal(cost.get("slippage_bps_each_side"), "slippage_bps") != SLIPPAGE_BPS or _decimal(label.get("fee_rate"), "fee_rate") != TAKER_FEE_RATE:
        raise CryptoFactorStrategyEvaluationError("evaluation_cost_policy_mismatch")
    net = _decimal(label.get("net_return"), "net_return")
    slip = SLIPPAGE_BPS / Decimal("10000")
    return dict(sample), (Decimal("1") + net) * (Decimal("1") - slip) ** 2 - Decimal("1")


def _metrics(rows: Sequence[tuple[Mapping[str, Any], Decimal]], signals: Sequence[bool], baseline: Decimal | None = None) -> dict[str, Any]:
    selected = [value for (_, value), signal in zip(rows, signals) if signal]
    count = len(rows)
    mean = sum(selected, Decimal("0")) / Decimal(len(selected)) if selected else None
    return {"resolved_count": count, "signal_count": len(selected), "abstention_count": count - len(selected),
            "coverage": _text(Decimal(len(selected)) / Decimal(count)) if count else "0",
            "hit_rate": _text(Decimal(sum(value > 0 for value in selected)) / Decimal(len(selected))) if selected else None,
            "cost_adjusted_net_return": _text(mean), "baseline_delta": _text(mean - baseline) if mean is not None and baseline is not None else None,
            "drawdown": _text(Decimal("0")), "turnover": _text(Decimal(len(selected)) / Decimal(count)) if count else "0"}


def build_factor_strategy_evaluation(*, samples: Sequence[Mapping[str, Any]], evaluation_as_of: str, strategy_dir: Path | str | None = None, expected_strategy_version: str | None = None) -> dict[str, Any]:
    """Evaluate momentum/time-series-momentum versus the same-cost hold baseline."""
    as_of = _utc(evaluation_as_of, "evaluation_as_of")
    content, identity = _strategy(strategy_dir)
    if expected_strategy_version is not None and expected_strategy_version != identity["strategy_version"]:
        raise CryptoFactorStrategyEvaluationError("evaluation_strategy_hash_mismatch")
    rows: list[tuple[dict[str, Any], Decimal]] = []
    pending = 0
    for sample in samples:
        if isinstance(sample, Mapping) and sample.get("status") == "pending":
            pending += 1
            continue
        if not isinstance(sample, Mapping):
            raise CryptoFactorStrategyEvaluationError("evaluation_sample_invalid")
        rows.append(_resolved(sample, as_of))
    rows.sort(key=lambda row: _utc(row[0]["snapshot"]["market_slot"], "market_slot"))
    baseline = _metrics(rows, [True] * len(rows))
    baseline_mean = _decimal(baseline["cost_adjusted_net_return"], "baseline_mean") if baseline["cost_adjusted_net_return"] is not None else None
    signals = [_signal(SELECTED_HYPOTHESIS, row[0]["snapshot"]) for row in rows]
    metrics = _metrics(rows, signals, baseline_mean)
    status = "exploratory_insufficient_edge"
    action = "disable" if not any(signals) else "retain_for_more_evidence"
    artifact = {"contract": EVALUATION_CONTRACT, **identity, "factor_hypothesis_id": SELECTED_HYPOTHESIS,
                "evaluation_as_of": as_of.isoformat().replace("+00:00", "Z"), "horizon_minutes": HORIZON_MINUTES,
                "sample_count": len(samples), "resolved_count": len(rows), "pending_count": pending, "excluded_count": 0,
                "resolved_coverage": _text(Decimal(len(rows)) / Decimal(len(samples))) if samples else "0",
                "configured_maturity": content.get("maturity"), "evaluated_status": status,
                "baseline": baseline, "metrics": metrics,
                "recommendation": {"shadow_only_action": action, "parameter_suggestion": "no_automatic_parameter_change"}, **_safe()}
    artifact["evaluation_sha256"] = _sha(artifact)
    return artifact


__all__ = ["COST_POLICY_ID", "EVALUATION_CONTRACT", "CryptoFactorStrategyEvaluationError", "build_factor_strategy_evaluation"]
