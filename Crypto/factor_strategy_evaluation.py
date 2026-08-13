"""Detached evaluation of one existing Crypto strategy and factor."""
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
from Crypto.delayed_paper_factor_research import (
    FACTOR_PROJECTION_CHECKPOINT_CONTRACT,
    FACTOR_PROJECTION_CONTRACT,
    FACTOR_PROJECTION_RECEIPT_CONTRACT,
)
from Crypto.delayed_paper_ledger import COMPLETION_CONTRACT
from Crypto.round_trip_capital import SLIPPAGE_BPS, TAKER_FEE_RATE

EVALUATION_CONTRACT = "tradingagent.crypto.factor_strategy_evaluation.v1"
HORIZON_MINUTES = 60
COST_POLICY_ID = "crypto-round-trip-taker-v1"
STRATEGY_HYPOTHESIS_PAIRS = {
    "momentum": "time_series_momentum_v1",
    "trend": "trend_pullback_v1",
}
SELECTED_STRATEGY = "momentum"
SELECTED_HYPOTHESIS = STRATEGY_HYPOTHESIS_PAIRS[SELECTED_STRATEGY]
_PROOF_FALSE_FIELDS = (
    "automatic_champion_replacement", "automatic_risk_expansion_enabled",
    "durable_execution_receipt", "execution_authority", "execution_eligible",
    "learning_authority", "live_broker_used", "model_network_used", "network_used",
    "production_eligible", "promotion_authorized", "real_trading_enabled", "testnet_used",
)


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
            "automatic_risk_expansion_enabled": False,
            "execution_authority": False, "production_eligible": False,
            "promotion_authorized": False, "network_used": False,
            "model_network_used": False, "live_broker_used": False}


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
        "future_observation_id": sample.get("future_observation_id"),
        "source_projection_proof_sha256": _sha(sample.get("source_projection_proof")),
        "future_projection_proof_sha256": _sha(sample.get("future_projection_proof")),
        "projection_checkpoint_chain_sha256": _sha(sample.get("projection_checkpoint_chain")),
        "expected_checkpoint_head_sha256": sample.get("expected_checkpoint_head_sha256"),
        "cost_policy": cost_policy,
    }
    return _sha(material)


def _strategy(strategy_dir: Path | str | None, strategy_name: str = SELECTED_STRATEGY) -> tuple[dict[str, Any], dict[str, Any]]:
    hypothesis = STRATEGY_HYPOTHESIS_PAIRS.get(strategy_name)
    if hypothesis is None:
        raise CryptoFactorStrategyEvaluationError("evaluation_strategy_invalid")
    loaded = CryptoAdapter(strategy_dir=Path(strategy_dir) if strategy_dir else None).get_strategy_config().get("strategies")
    content = loaded.get(strategy_name) if isinstance(loaded, Mapping) else None
    if not isinstance(content, Mapping) or content.get("name") != strategy_name or content.get("enabled") is not True:
        raise CryptoFactorStrategyEvaluationError("evaluation_strategy_invalid")
    normalized = json.loads(_json(content))
    return normalized, {"strategy_name": strategy_name, "strategy_version": _sha(normalized),
                        "factor_hypothesis_id": hypothesis, "configured_maturity": normalized.get("maturity")}


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
    if future_slot - _utc(snapshot.get("market_slot"), "market_slot") != timedelta(minutes=HORIZON_MINUTES):
        raise CryptoFactorStrategyEvaluationError("evaluation_label_horizon_invalid")
    chain = sample.get("projection_checkpoint_chain")
    expected_head = sample.get("expected_checkpoint_head_sha256")
    if not isinstance(chain, Sequence) or isinstance(chain, (str, bytes)) or not chain or not isinstance(expected_head, str):
        raise CryptoFactorStrategyEvaluationError("evaluation_checkpoint_chain_invalid")
    by_observation: dict[str, Mapping[str, Any]] = {}
    previous: str | None = None
    for sequence, item in enumerate(chain, start=1):
        if not isinstance(item, Mapping):
            raise CryptoFactorStrategyEvaluationError("evaluation_checkpoint_chain_invalid")
        material = dict(item)
        claimed = material.pop("checkpoint_sha256", None)
        observation_id = item.get("observation_id")
        if (
            item.get("contract") != FACTOR_PROJECTION_CHECKPOINT_CONTRACT
            or item.get("sequence") != sequence
            or item.get("previous_checkpoint_sha256") != previous
            or claimed != _sha(material)
            or not isinstance(observation_id, str)
            or observation_id in by_observation
            or any(item.get(field) is not False for field in _PROOF_FALSE_FIELDS if field in item)
            or item.get("authority") not in (None, "none")
        ):
            raise CryptoFactorStrategyEvaluationError("evaluation_checkpoint_chain_invalid")
        by_observation[observation_id] = item
        previous = claimed
    if previous != expected_head:
        raise CryptoFactorStrategyEvaluationError("evaluation_checkpoint_chain_invalid")
    for role, expected_id, expected_slot in (
        ("source", snapshot.get("observation_id"), _utc(snapshot.get("market_slot"), "market_slot")),
        ("future", sample.get("future_observation_id"), future_slot),
    ):
        digest_key = f"{role}_completion_sha256"
        proof = sample.get(f"{role}_projection_proof")
        if not isinstance(proof, Mapping):
            raise CryptoFactorStrategyEvaluationError("evaluation_projection_binding_invalid")
        completion = proof.get("completion")
        record = proof.get("record")
        receipt = proof.get("receipt")
        checkpoint = proof.get("checkpoint")
        if (
            not isinstance(sample.get(digest_key), str)
            or len(sample[digest_key]) != 64
            or not all(isinstance(item, Mapping) for item in (completion, record, receipt, checkpoint))
        ):
            raise CryptoFactorStrategyEvaluationError("evaluation_projection_binding_invalid")
        completion_material = dict(completion)
        completion_sha = completion_material.pop("completion_sha256", None)
        record_material = dict(record)
        record_sha = record_material.pop("factor_projection_sha256", None)
        receipt_material = dict(receipt)
        receipt_sha = receipt_material.pop("projection_receipt_sha256", None)
        checkpoint_material = dict(checkpoint)
        checkpoint_sha = checkpoint_material.pop("checkpoint_sha256", None)
        symbol = snapshot.get("symbol")
        record_snapshots = record.get("snapshots")
        record_snapshot = record_snapshots.get(symbol) if isinstance(record_snapshots, Mapping) else None
        if role == "source":
            evidence_matches = record_snapshot == snapshot
        else:
            evidence_matches = (
                isinstance(record_snapshot, Mapping)
                and record_snapshot.get("evidence_receipt_id") == label.get("future_evidence_receipt_id")
                and record_snapshot.get("evidence_lineage_sha256") == label.get("future_evidence_lineage_sha256")
                and record_snapshot.get("market_slot") == label.get("future_market_slot")
                and record_snapshot.get("observed_at") == label.get("future_observed_at")
                and record_snapshot.get("data_through") == label.get("future_data_through")
            )
            try:
                _assert_snapshot_integrity(record_snapshot)  # type: ignore[arg-type]
            except CryptoFactorResearchError:
                evidence_matches = False
        authority_safe = all(
            item.get(field) is False
            for item in (completion, record, receipt, checkpoint)
            for field in _PROOF_FALSE_FIELDS
            if field in item
        ) and all(
            item.get("authority") in (None, "none")
            for item in (completion, record, receipt, checkpoint)
        )
        if (
            completion.get("contract") != COMPLETION_CONTRACT
            or
            completion_sha != _sha(completion_material)
            or completion_sha != sample[digest_key]
            or completion.get("observation_id") != expected_id
            or completion.get("status") != "completed"
            or record.get("contract") != FACTOR_PROJECTION_CONTRACT
            or record_sha != _sha(record_material)
            or record.get("observation_id") != expected_id
            or record.get("market_slot") != expected_slot.isoformat().replace("+00:00", "Z")
            or record.get("segment_id") != sample.get(f"{role}_segment_id", sample.get("segment_id"))
            or record.get("source_completion_sha256") != completion_sha
            or record.get("source_observation_content_sha256") != completion.get("observation_content_sha256")
            or not evidence_matches
            or receipt.get("contract") != FACTOR_PROJECTION_RECEIPT_CONTRACT
            or receipt_sha != _sha(receipt_material)
            or receipt.get("observation_id") != expected_id
            or receipt.get("source_completion_sha256") != completion_sha
            or receipt.get("factor_projection_sha256") != record_sha
            or checkpoint.get("contract") != FACTOR_PROJECTION_CHECKPOINT_CONTRACT
            or checkpoint_sha != _sha(checkpoint_material)
            or isinstance(checkpoint.get("sequence"), bool)
            or not isinstance(checkpoint.get("sequence"), int)
            or checkpoint.get("sequence", 0) <= 0
            or (checkpoint.get("sequence") == 1 and checkpoint.get("previous_checkpoint_sha256") is not None)
            or (checkpoint.get("sequence", 0) > 1 and (
                not isinstance(checkpoint.get("previous_checkpoint_sha256"), str)
                or len(checkpoint.get("previous_checkpoint_sha256")) != 64
            ))
            or checkpoint.get("observation_id") != expected_id
            or checkpoint.get("source_completion_sha256") != completion_sha
            or checkpoint.get("projection_receipt_sha256") != receipt_sha
            or by_observation.get(str(expected_id)) != checkpoint
            or not authority_safe
        ):
            raise CryptoFactorStrategyEvaluationError("evaluation_projection_binding_invalid")
    if future_slot > evaluation_as_of or _utc(label.get("future_observed_at"), "future_observed_at") > evaluation_as_of or _utc(label.get("future_data_through"), "future_data_through") > evaluation_as_of:
        raise CryptoFactorStrategyEvaluationError("evaluation_future_after_as_of")
    cost = sample.get("cost_policy")
    if not isinstance(cost, Mapping) or cost.get("cost_policy_id") != COST_POLICY_ID or _decimal(cost.get("fee_rate"), "cost_fee_rate") != TAKER_FEE_RATE or _decimal(cost.get("slippage_bps_each_side"), "slippage_bps") != SLIPPAGE_BPS or _decimal(label.get("fee_rate"), "fee_rate") != TAKER_FEE_RATE:
        raise CryptoFactorStrategyEvaluationError("evaluation_cost_policy_mismatch")
    entry = _decimal(label.get("entry_price"), "entry_price")
    exit_ = _decimal(label.get("exit_price"), "exit_price")
    fee = _decimal(label.get("fee_rate"), "fee_rate")
    if entry <= 0 or exit_ <= 0:
        raise CryptoFactorStrategyEvaluationError("evaluation_label_return_invalid")
    expected_gross = exit_ / entry - Decimal("1")
    expected_net = exit_ * (Decimal("1") - fee) / (entry * (Decimal("1") + fee)) - Decimal("1")
    if (
        _decimal(label.get("gross_return"), "gross_return") != expected_gross
        or _decimal(label.get("net_return"), "net_return") != expected_net
    ):
        raise CryptoFactorStrategyEvaluationError("evaluation_label_return_invalid")
    slip = SLIPPAGE_BPS / Decimal("10000")
    return dict(sample), (Decimal("1") + expected_net) * (Decimal("1") - slip) ** 2 - Decimal("1")


def _metrics(rows: Sequence[tuple[Mapping[str, Any], Decimal]], signals: Sequence[bool], baseline: Decimal | None = None) -> dict[str, Any]:
    selected = [value for (_, value), signal in zip(rows, signals) if signal]
    count = len(rows)
    mean = sum(selected, Decimal("0")) / Decimal(len(selected)) if selected else None
    slot_returns: dict[datetime, list[Decimal]] = {}
    for (row, value), signal in zip(rows, signals):
        if signal:
            slot = _utc(row["snapshot"]["market_slot"], "market_slot")
            slot_returns.setdefault(slot, []).append(value)
    equity = Decimal("1")
    peak = equity
    maximum_drawdown = Decimal("0")
    for slot in sorted(slot_returns):
        equity *= Decimal("1") + sum(slot_returns[slot], Decimal("0")) / Decimal(len(slot_returns[slot]))
        peak = max(peak, equity)
        if peak:
            maximum_drawdown = max(maximum_drawdown, (peak - equity) / peak)
    return {"resolved_count": count, "signal_count": len(selected), "abstention_count": count - len(selected),
            "coverage": _text(Decimal(len(selected)) / Decimal(count)) if count else "0",
            "hit_rate": _text(Decimal(sum(value > 0 for value in selected)) / Decimal(len(selected))) if selected else None,
            "cost_adjusted_net_return": _text(mean), "baseline_delta": _text(mean - baseline) if mean is not None and baseline is not None else None,
            "drawdown": _text(maximum_drawdown),
            "turnover": _text(Decimal(len(selected)) / Decimal(count)) if count else "0",
            "round_trip_leg_rate": _text(Decimal(2 * len(selected)) / Decimal(count)) if count else "0",
            "metric_basis": "equal_weight_by_market_slot exploratory drawdown; legacy turnover is exposure rate; round_trip_leg_rate is legs per resolved sample"}


def _cash_baseline(resolved_count: int) -> dict[str, Any]:
    """Existing no-position/cash behavior: no costs, return, drawdown or turnover."""

    return {"resolved_count": resolved_count, "signal_count": 0,
            "abstention_count": resolved_count, "coverage": "0", "hit_rate": None,
            "cost_adjusted_net_return": "0", "baseline_delta": None,
            "drawdown": "0", "turnover": "0", "round_trip_leg_rate": "0",
            "metric_basis": "cash_no_position"}


def build_factor_strategy_evaluation(*, samples: Sequence[Mapping[str, Any]], evaluation_as_of: str, strategy_dir: Path | str | None = None, expected_strategy_version: str | None = None, strategy_name: str = SELECTED_STRATEGY) -> dict[str, Any]:
    """Evaluate one allowlisted existing strategy versus the same-cost hold baseline."""
    as_of = _utc(evaluation_as_of, "evaluation_as_of")
    content, strategy_identity = _strategy(strategy_dir, strategy_name=strategy_name)
    if expected_strategy_version is not None and expected_strategy_version != strategy_identity["strategy_version"]:
        raise CryptoFactorStrategyEvaluationError("evaluation_strategy_hash_mismatch")
    rows: list[tuple[dict[str, Any], Decimal]] = []
    pending = 0
    identities: set[tuple[Any, ...]] = set()
    pending_identities: set[str] = set()
    for sample in samples:
        if isinstance(sample, Mapping) and sample.get("status") == "pending":
            allowed = {"status", "sample_id"}
            if set(sample) - allowed or not isinstance(sample.get("sample_id"), str) or not sample["sample_id"]:
                raise CryptoFactorStrategyEvaluationError("evaluation_pending_identity_invalid")
            if sample["sample_id"] in pending_identities:
                raise CryptoFactorStrategyEvaluationError("evaluation_sample_duplicate")
            pending_identities.add(sample["sample_id"])
            pending += 1
            continue
        if not isinstance(sample, Mapping):
            raise CryptoFactorStrategyEvaluationError("evaluation_sample_invalid")
        resolved = _resolved(sample, as_of)
        sample_identity = (sample.get("segment_id"), resolved[0]["snapshot"].get("observation_id"),
                           resolved[0]["snapshot"].get("symbol"), HORIZON_MINUTES)
        if sample_identity in identities:
            raise CryptoFactorStrategyEvaluationError("evaluation_sample_duplicate")
        identities.add(sample_identity)
        rows.append(resolved)
    rows.sort(key=lambda row: _utc(row[0]["snapshot"]["market_slot"], "market_slot"))
    baseline = _metrics(rows, [True] * len(rows))
    baseline_mean = _decimal(baseline["cost_adjusted_net_return"], "baseline_mean") if baseline["cost_adjusted_net_return"] is not None else None
    signals = [_signal(strategy_identity["factor_hypothesis_id"], row[0]["snapshot"]) for row in rows]
    metrics = _metrics(rows, signals, baseline_mean)
    cash_baseline = _cash_baseline(len(rows))
    strategy_mean = (
        _decimal(metrics["cost_adjusted_net_return"], "strategy_mean")
        if metrics["cost_adjusted_net_return"] is not None else None
    )
    metrics["cash_baseline_delta"] = _text(strategy_mean) if strategy_mean is not None else None
    status = "exploratory_insufficient_edge"
    action = (
        "disable" if not any(signals)
        else "downweight" if strategy_mean is not None and strategy_mean <= 0
        else "retain_for_more_evidence"
    )
    artifact = {"contract": EVALUATION_CONTRACT, **strategy_identity,
                "evaluation_as_of": as_of.isoformat().replace("+00:00", "Z"), "horizon_minutes": HORIZON_MINUTES,
                "sample_count": len(samples), "resolved_count": len(rows), "pending_count": pending, "excluded_count": 0,
                "resolved_coverage": _text(Decimal(len(rows)) / Decimal(len(samples))) if samples else "0",
                "configured_maturity": content.get("maturity"), "evaluated_status": status,
                "baseline": baseline, "cash_baseline": cash_baseline, "metrics": metrics,
                "recommendation": {"shadow_only_action": action, "parameter_suggestion": "no_automatic_parameter_change"}, **_safe()}
    artifact["evaluation_sha256"] = _sha(artifact)
    return artifact


__all__ = ["COST_POLICY_ID", "EVALUATION_CONTRACT", "CryptoFactorStrategyEvaluationError", "build_factor_strategy_evaluation"]
