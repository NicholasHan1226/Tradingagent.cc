"""Detached cost-adjusted strategy evaluation over the ten-symbol projection.

This module is the v2 port of ``factor_strategy_evaluation`` +
``factor_strategy_post_projection``: it rebuilds every resolved
(snapshot, label) sample from the ten-symbol projection root, verifies each
sample against the v2 evidence contract (record/receipt/checkpoint triple,
store event checksum, bars-sidecar sha, checkpoint chain replay, cost policy,
gross/net recomputation, causality), and produces one immutable evaluation
artifact per resolved outcome covering all three pre-registered hypotheses.
It has no core, capital, order, Champion, or network access.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence
import uuid

from Crypto.factor_research import (
    TEN_SYMBOL_FACTOR_SET_ID,
    TEN_SYMBOL_FACTOR_SET_VERSION,
    CryptoFactorResearchError,
    _assert_label_integrity,
    _assert_snapshot_integrity,
    _signal,
)
from Crypto.fixture_sim.contracts import _assert_simulation_only
from Crypto.round_trip_capital import SLIPPAGE_BPS, TAKER_FEE_RATE
import Crypto.ten_symbol_factor_research as projection
from Crypto.ten_symbol_observation_store import (
    CryptoTenSymbolObservationStoreError,
)


EVALUATION_CONTRACT = "tradingagent.crypto.ten_symbol_factor_strategy_evaluation.v1"
EVALUATION_BUNDLE_CONTRACT = (
    "tradingagent.crypto.ten_symbol_factor_strategy_evaluation_bundle.v1"
)
EVALUATION_CHECKPOINT_CONTRACT = (
    "tradingagent.crypto.ten_symbol_factor_strategy_evaluation_checkpoint.v1"
)
HORIZON_MINUTES = 60
AUXILIARY_HORIZONS = (240, 720, 1440)
EVALUATION_HORIZONS = (HORIZON_MINUTES, *AUXILIARY_HORIZONS)
COST_POLICY_ID = "crypto-round-trip-taker-v1"
STRATEGY_HYPOTHESIS_PAIRS = {
    "momentum": "time_series_momentum_v1",
    "trend": "trend_pullback_v1",
    "volatility": "volume_breakout_v1",
}
_SYMBOLS = projection._SYMBOLS
CHECKPOINT_FILENAME = "strategy_evaluation_checkpoint.json"


class CryptoTenSymbolFactorStrategyEvaluationError(RuntimeError):
    """Stable fail-closed error for ten-symbol strategy evaluation."""


def _json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise CryptoTenSymbolFactorStrategyEvaluationError(
            "evaluation_payload_invalid"
        ) from exc


def _sha(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _decimal(value: Any, field: str) -> Decimal:
    if isinstance(value, bool) or value in (None, ""):
        raise CryptoTenSymbolFactorStrategyEvaluationError(
            f"evaluation_{field}_invalid"
        )
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise CryptoTenSymbolFactorStrategyEvaluationError(
            f"evaluation_{field}_invalid"
        ) from exc
    if not result.is_finite():
        raise CryptoTenSymbolFactorStrategyEvaluationError(
            f"evaluation_{field}_invalid"
        )
    return result


def _utc(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise CryptoTenSymbolFactorStrategyEvaluationError(
            f"evaluation_{field}_invalid"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CryptoTenSymbolFactorStrategyEvaluationError(
            f"evaluation_{field}_invalid"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise CryptoTenSymbolFactorStrategyEvaluationError(
            f"evaluation_{field}_invalid"
        )
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _text(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _safe() -> dict[str, Any]:
    return projection._non_authority_fields()


def _cost_policy() -> dict[str, Any]:
    return {
        "cost_policy_id": COST_POLICY_ID,
        "fee_rate": format(TAKER_FEE_RATE, "f"),
        "slippage_bps_each_side": format(SLIPPAGE_BPS, "f"),
    }


# ---------------------------------------------------------------------------
# Inventory: rebuild resolved samples from the projection root
# ---------------------------------------------------------------------------


def _sample_binding_sha256(sample: Mapping[str, Any]) -> str:
    """Hash the stable, already-present evidence that identifies one sample."""

    snapshot = sample.get("snapshot")
    label = sample.get("label")
    cost_policy = sample.get("cost_policy")
    if (
        not isinstance(snapshot, Mapping)
        or not isinstance(label, Mapping)
        or not isinstance(cost_policy, Mapping)
    ):
        raise CryptoTenSymbolFactorStrategyEvaluationError(
            "evaluation_sample_binding_invalid"
        )
    material = {
        "snapshot_factor_snapshot_sha256": snapshot.get("factor_snapshot_sha256"),
        "label_forward_label_sha256": label.get("forward_label_sha256"),
        "horizon_minutes": sample.get("horizon_minutes"),
        "segment_id": sample.get("segment_id"),
        "future_segment_id": sample.get("future_segment_id"),
        "source_event_checksum": sample.get("source_event_checksum"),
        "future_event_checksum": sample.get("future_event_checksum"),
        "future_observation_id": sample.get("future_observation_id"),
        "source_projection_proof_sha256": _sha(sample.get("source_projection_proof")),
        "future_projection_proof_sha256": _sha(sample.get("future_projection_proof")),
        "projection_checkpoint_chain_sha256": _sha(
            sample.get("projection_checkpoint_chain")
        ),
        "expected_checkpoint_head_sha256": sample.get(
            "expected_checkpoint_head_sha256"
        ),
        "cost_policy": cost_policy,
    }
    return _sha(material)


def _verify_record_evidence_binding(
    *,
    record: Mapping[str, Any],
    receipt: Mapping[str, Any],
    checkpoint: Mapping[str, Any],
    event_checksum: str,
    sidecar_sha256: str,
) -> None:
    """Cross-bind the record triple to the store event and the bars sidecar."""

    record_material = dict(record)
    record_sha = record_material.pop("factor_projection_sha256", None)
    receipt_material = dict(receipt)
    receipt_sha = receipt_material.pop("projection_receipt_sha256", None)
    checkpoint_material = dict(checkpoint)
    checkpoint_sha = checkpoint_material.pop("checkpoint_sha256", None)
    authority_safe = all(
        item.get(key) == value
        for item in (record, receipt, checkpoint)
        for key, value in projection._non_authority_fields().items()
    )
    if (
        record.get("contract") != projection.TEN_SYMBOL_FACTOR_PROJECTION_CONTRACT
        or record_sha != _sha(record_material)
        or record.get("source_event_checksum") != event_checksum
        or record.get("source_bars_sidecar_sha256") != sidecar_sha256
        or receipt.get("contract")
        != projection.TEN_SYMBOL_FACTOR_PROJECTION_RECEIPT_CONTRACT
        or receipt_sha != _sha(receipt_material)
        or receipt.get("observation_id") != record.get("observation_id")
        or receipt.get("source_event_checksum") != event_checksum
        or receipt.get("source_bars_sidecar_sha256") != sidecar_sha256
        or receipt.get("factor_projection_sha256") != record_sha
        or checkpoint.get("contract")
        != projection.TEN_SYMBOL_FACTOR_PROJECTION_CHECKPOINT_CONTRACT
        or checkpoint_sha != _sha(checkpoint_material)
        or checkpoint.get("observation_id") != record.get("observation_id")
        or checkpoint.get("source_event_checksum") != event_checksum
        or checkpoint.get("projection_outcome") != "projected"
        or checkpoint.get("projection_receipt_sha256") != receipt_sha
        or not authority_safe
    ):
        raise CryptoTenSymbolFactorStrategyEvaluationError(
            "evaluation_projection_binding_invalid"
        )


def _load_projected_records(
    root: Path,
) -> tuple[
    list[dict[str, Any]],
    dict[str, dict[str, Any]],
]:
    """Verify the checkpoint chain and load every projected record triple.

    Each projected record is additionally cross-bound to its append-only
    store event checksum and to the sha of the immutable bars sidecar on
    disk, so the evaluation never trusts a projection artifact that drifted
    from the observation evidence.
    """

    store = projection._open_store(root)
    try:
        events = store.events_read_only()
    except (CryptoTenSymbolObservationStoreError, OSError, ValueError) as exc:
        raise CryptoTenSymbolFactorStrategyEvaluationError(
            "evaluation_core_invalid"
        ) from exc
    event_checksum_by_id: dict[str, str] = {}
    for event in events:
        if isinstance(event.get("event_id"), str) and isinstance(
            event.get("checksum"), str
        ):
            event_checksum_by_id[event["event_id"]] = event["checksum"]
    evolution = projection._root(root)
    if not evolution.exists():
        return [], {}
    checkpoints = projection._read_checkpoints(evolution)
    records: dict[str, dict[str, Any]] = {}
    for checkpoint in checkpoints:
        if checkpoint.get("projection_outcome") != "projected":
            continue
        observation_id = checkpoint.get("observation_id")
        if not isinstance(observation_id, str):
            raise CryptoTenSymbolFactorStrategyEvaluationError(
                "evaluation_checkpoint_chain_invalid"
            )
        event_checksum = event_checksum_by_id.get(observation_id)
        if event_checksum is None:
            raise CryptoTenSymbolFactorStrategyEvaluationError(
                "evaluation_projection_binding_invalid"
            )
        paths = projection._paths(root, observation_id)
        record = projection._parse_canonical(
            paths["record"], reason="evaluation_record_invalid"
        )
        receipt = projection._parse_canonical(
            paths["receipt"], reason="evaluation_receipt_invalid"
        )
        if record.get("source_event_checksum") != event_checksum:
            raise CryptoTenSymbolFactorStrategyEvaluationError(
                "evaluation_projection_binding_invalid"
            )
        window_end = record.get("window_end")
        try:
            sidecar = store.read_bars_sidecar(str(window_end))
        except CryptoTenSymbolObservationStoreError as exc:
            raise CryptoTenSymbolFactorStrategyEvaluationError(
                "evaluation_sidecar_binding_invalid"
            ) from exc
        if sidecar is None or _sha(sidecar) != record.get(
            "source_bars_sidecar_sha256"
        ):
            raise CryptoTenSymbolFactorStrategyEvaluationError(
                "evaluation_sidecar_binding_invalid"
            )
        _verify_record_evidence_binding(
            record=record,
            receipt=receipt,
            checkpoint=checkpoint,
            event_checksum=event_checksum,
            sidecar_sha256=str(record["source_bars_sidecar_sha256"]),
        )
        slot = record.get("market_slot")
        if not isinstance(slot, str):
            raise CryptoTenSymbolFactorStrategyEvaluationError(
                "evaluation_record_invalid"
            )
        records[slot] = {
            "record": record,
            "receipt": receipt,
            "checkpoint": checkpoint,
        }
    return checkpoints, records


def _inventory(
    root: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Rebuild every resolved (snapshot, label) sample with full proofs."""

    checkpoints, records = _load_projected_records(root)
    if not checkpoints or not records:
        return [], checkpoints
    head = str(checkpoints[-1]["checkpoint_sha256"])
    samples: list[dict[str, Any]] = []
    for slot, item in records.items():
        record = item["record"]
        observation_id = str(record["observation_id"])
        snapshots = record.get("snapshots")
        source_slot = _utc(slot, "market_slot")
        for horizon in EVALUATION_HORIZONS:
            future_item = records.get(
                _iso(source_slot + timedelta(minutes=horizon))
            )
            if not isinstance(future_item, Mapping):
                continue
            future_record = future_item["record"]
            if future_record.get("segment_id") != record.get("segment_id"):
                continue
            future_id = str(future_record["observation_id"])
            for symbol in _SYMBOLS:
                label_path = projection._label_path(
                    root, observation_id, symbol, horizon
                )
                if not label_path.is_file() or label_path.is_symlink():
                    continue
                label = projection._parse_canonical(
                    label_path, reason="evaluation_label_invalid"
                )
                snapshot = snapshots.get(symbol) if isinstance(snapshots, Mapping) else None
                if not isinstance(snapshot, Mapping):
                    raise CryptoTenSymbolFactorStrategyEvaluationError(
                        "evaluation_record_invalid"
                    )
                sample = {
                    "snapshot": snapshot,
                    "label": label,
                    "horizon_minutes": horizon,
                    "segment_id": record.get("segment_id"),
                    "future_segment_id": future_record.get("segment_id"),
                    "source_event_checksum": record.get("source_event_checksum"),
                    "future_event_checksum": future_record.get(
                        "source_event_checksum"
                    ),
                    "future_observation_id": future_id,
                    "source_projection_proof": item,
                    "future_projection_proof": future_item,
                    "projection_checkpoint_chain": checkpoints,
                    "expected_checkpoint_head_sha256": head,
                    "cost_policy": _cost_policy(),
                }
                sample["sample_binding_sha256"] = _sample_binding_sha256(sample)
                samples.append(sample)
    samples.sort(
        key=lambda sample: (
            str(sample["snapshot"].get("market_slot")),
            int(sample["horizon_minutes"]),
            str(sample["snapshot"].get("symbol")),
        )
    )
    return samples, checkpoints


def _outcome_sha256(samples: Sequence[Mapping[str, Any]]) -> str:
    """Identify the resolved-sample inventory; changes only when it does."""

    material = {
        "samples": sorted(
            f"{sample['snapshot'].get('observation_id')}:"
            f"{sample['snapshot'].get('symbol')}:"
            f"{sample['label'].get('forward_label_sha256')}"
            for sample in samples
        )
    }
    return _sha(material)


# ---------------------------------------------------------------------------
# Per-sample verification and cost-adjusted outcome
# ---------------------------------------------------------------------------


def _verify_chain_replay(sample: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    chain = sample.get("projection_checkpoint_chain")
    expected_head = sample.get("expected_checkpoint_head_sha256")
    if (
        not isinstance(chain, Sequence)
        or isinstance(chain, (str, bytes))
        or not chain
        or not isinstance(expected_head, str)
    ):
        raise CryptoTenSymbolFactorStrategyEvaluationError(
            "evaluation_checkpoint_chain_invalid"
        )
    by_observation: dict[str, Mapping[str, Any]] = {}
    previous: str | None = None
    for sequence, item in enumerate(chain, start=1):
        if not isinstance(item, Mapping):
            raise CryptoTenSymbolFactorStrategyEvaluationError(
                "evaluation_checkpoint_chain_invalid"
            )
        material = dict(item)
        claimed = material.pop("checkpoint_sha256", None)
        observation_id = item.get("observation_id")
        if (
            item.get("contract")
            != projection.TEN_SYMBOL_FACTOR_PROJECTION_CHECKPOINT_CONTRACT
            or item.get("sequence") != sequence
            or item.get("previous_checkpoint_sha256") != previous
            or claimed != _sha(material)
            or not isinstance(observation_id, str)
            or observation_id in by_observation
            or any(
                item.get(key) != value
                for key, value in projection._non_authority_fields().items()
            )
        ):
            raise CryptoTenSymbolFactorStrategyEvaluationError(
                "evaluation_checkpoint_chain_invalid"
            )
        by_observation[observation_id] = item
        previous = claimed
    if previous != expected_head:
        raise CryptoTenSymbolFactorStrategyEvaluationError(
            "evaluation_checkpoint_chain_invalid"
        )
    return by_observation


def _resolved(
    sample: Mapping[str, Any],
    evaluation_as_of: datetime,
    *,
    verified_chain: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[dict[str, Any], Decimal]:
    snapshot = sample.get("snapshot")
    label = sample.get("label")
    if not isinstance(snapshot, Mapping) or not isinstance(label, Mapping):
        raise CryptoTenSymbolFactorStrategyEvaluationError(
            "evaluation_sample_unresolved"
        )
    try:
        _assert_snapshot_integrity(
            snapshot,
            feature_set_id=TEN_SYMBOL_FACTOR_SET_ID,
            feature_set_version=TEN_SYMBOL_FACTOR_SET_VERSION,
        )
        _assert_label_integrity(
            label,
            snapshot=snapshot,
            feature_set_id=TEN_SYMBOL_FACTOR_SET_ID,
            feature_set_version=TEN_SYMBOL_FACTOR_SET_VERSION,
        )
    except CryptoFactorResearchError as exc:
        raise CryptoTenSymbolFactorStrategyEvaluationError(
            "evaluation_receipt_or_lineage_binding_invalid"
        ) from exc
    horizon = sample.get("horizon_minutes")
    if (
        isinstance(horizon, bool)
        or not isinstance(horizon, int)
        or horizon not in EVALUATION_HORIZONS
        or label.get("label_status") != "observed_future_outcome"
        or label.get("horizon_minutes") != horizon
    ):
        raise CryptoTenSymbolFactorStrategyEvaluationError(
            "evaluation_label_not_resolved"
        )
    if not isinstance(sample.get("segment_id"), str) or sample.get(
        "future_segment_id"
    ) != sample.get("segment_id"):
        raise CryptoTenSymbolFactorStrategyEvaluationError(
            "evaluation_cross_gap_label"
        )
    claimed_binding = sample.get("sample_binding_sha256")
    if not isinstance(claimed_binding, str) or claimed_binding != (
        _sample_binding_sha256(sample)
    ):
        raise CryptoTenSymbolFactorStrategyEvaluationError(
            "evaluation_sample_binding_invalid"
        )
    market_slot = _utc(snapshot.get("market_slot"), "market_slot")
    future_slot = _utc(label.get("future_market_slot"), "future_market_slot")
    if future_slot - market_slot != timedelta(minutes=horizon):
        raise CryptoTenSymbolFactorStrategyEvaluationError(
            "evaluation_label_horizon_invalid"
        )
    by_observation = (
        _verify_chain_replay(sample) if verified_chain is None else verified_chain
    )
    symbol = snapshot.get("symbol")
    for role, expected_id, expected_slot in (
        ("source", snapshot.get("observation_id"), market_slot),
        ("future", sample.get("future_observation_id"), future_slot),
    ):
        proof = sample.get(f"{role}_projection_proof")
        if not isinstance(proof, Mapping):
            raise CryptoTenSymbolFactorStrategyEvaluationError(
                "evaluation_projection_binding_invalid"
            )
        record = proof.get("record")
        receipt = proof.get("receipt")
        checkpoint = proof.get("checkpoint")
        if not all(
            isinstance(item, Mapping) for item in (record, receipt, checkpoint)
        ):
            raise CryptoTenSymbolFactorStrategyEvaluationError(
                "evaluation_projection_binding_invalid"
            )
        event_checksum = sample.get(f"{role}_event_checksum")
        if not isinstance(event_checksum, str) or len(event_checksum) != 64:
            raise CryptoTenSymbolFactorStrategyEvaluationError(
                "evaluation_projection_binding_invalid"
            )
        record_material = dict(record)
        record_sha = record_material.pop("factor_projection_sha256", None)
        record_snapshots = record.get("snapshots")
        record_snapshot = (
            record_snapshots.get(symbol)
            if isinstance(record_snapshots, Mapping)
            else None
        )
        if role == "source":
            evidence_matches = record_snapshot == snapshot
        else:
            evidence_matches = (
                isinstance(record_snapshot, Mapping)
                and record_snapshot.get("evidence_receipt_id")
                == label.get("future_evidence_receipt_id")
                and record_snapshot.get("evidence_lineage_sha256")
                == label.get("future_evidence_lineage_sha256")
                and record_snapshot.get("market_slot")
                == label.get("future_market_slot")
                and record_snapshot.get("observed_at")
                == label.get("future_observed_at")
                and record_snapshot.get("data_through")
                == label.get("future_data_through")
            )
            try:
                _assert_snapshot_integrity(
                    record_snapshot,  # type: ignore[arg-type]
                    feature_set_id=TEN_SYMBOL_FACTOR_SET_ID,
                    feature_set_version=TEN_SYMBOL_FACTOR_SET_VERSION,
                )
            except CryptoFactorResearchError:
                evidence_matches = False
        receipt_material = dict(receipt)
        receipt_sha = receipt_material.pop("projection_receipt_sha256", None)
        checkpoint_material = dict(checkpoint)
        checkpoint_sha = checkpoint_material.pop("checkpoint_sha256", None)
        if (
            record.get("contract") != projection.TEN_SYMBOL_FACTOR_PROJECTION_CONTRACT
            or record_sha != _sha(record_material)
            or record.get("observation_id") != expected_id
            or record.get("market_slot") != _iso(expected_slot)
            or record.get("segment_id")
            != sample.get(f"{role}_segment_id", sample.get("segment_id"))
            or record.get("source_event_checksum") != event_checksum
            or not evidence_matches
            or receipt.get("contract")
            != projection.TEN_SYMBOL_FACTOR_PROJECTION_RECEIPT_CONTRACT
            or receipt_sha != _sha(receipt_material)
            or receipt.get("observation_id") != expected_id
            or receipt.get("source_event_checksum") != event_checksum
            or receipt.get("factor_projection_sha256") != record_sha
            or checkpoint.get("contract")
            != projection.TEN_SYMBOL_FACTOR_PROJECTION_CHECKPOINT_CONTRACT
            or checkpoint_sha != _sha(checkpoint_material)
            or isinstance(checkpoint.get("sequence"), bool)
            or not isinstance(checkpoint.get("sequence"), int)
            or checkpoint.get("sequence", 0) <= 0
            or (
                checkpoint.get("sequence") == 1
                and checkpoint.get("previous_checkpoint_sha256") is not None
            )
            or (
                checkpoint.get("sequence", 0) > 1
                and (
                    not isinstance(checkpoint.get("previous_checkpoint_sha256"), str)
                    or len(checkpoint.get("previous_checkpoint_sha256")) != 64
                )
            )
            or checkpoint.get("observation_id") != expected_id
            or checkpoint.get("source_event_checksum") != event_checksum
            or checkpoint.get("projection_outcome") != "projected"
            or checkpoint.get("projection_receipt_sha256") != receipt_sha
            or by_observation.get(str(expected_id)) != checkpoint
        ):
            raise CryptoTenSymbolFactorStrategyEvaluationError(
                "evaluation_projection_binding_invalid"
            )
    if (
        future_slot > evaluation_as_of
        or _utc(label.get("future_observed_at"), "future_observed_at")
        > evaluation_as_of
        or _utc(label.get("future_data_through"), "future_data_through")
        > evaluation_as_of
    ):
        raise CryptoTenSymbolFactorStrategyEvaluationError(
            "evaluation_future_after_as_of"
        )
    cost = sample.get("cost_policy")
    if (
        not isinstance(cost, Mapping)
        or cost.get("cost_policy_id") != COST_POLICY_ID
        or _decimal(cost.get("fee_rate"), "cost_fee_rate") != TAKER_FEE_RATE
        or _decimal(cost.get("slippage_bps_each_side"), "slippage_bps")
        != SLIPPAGE_BPS
        or _decimal(label.get("fee_rate"), "fee_rate") != TAKER_FEE_RATE
    ):
        raise CryptoTenSymbolFactorStrategyEvaluationError(
            "evaluation_cost_policy_mismatch"
        )
    entry = _decimal(label.get("entry_price"), "entry_price")
    exit_ = _decimal(label.get("exit_price"), "exit_price")
    fee = _decimal(label.get("fee_rate"), "fee_rate")
    if entry <= 0 or exit_ <= 0:
        raise CryptoTenSymbolFactorStrategyEvaluationError(
            "evaluation_label_return_invalid"
        )
    expected_gross = exit_ / entry - Decimal("1")
    expected_net = exit_ * (Decimal("1") - fee) / (
        entry * (Decimal("1") + fee)
    ) - Decimal("1")
    if (
        _decimal(label.get("gross_return"), "gross_return") != expected_gross
        or _decimal(label.get("net_return"), "net_return") != expected_net
    ):
        raise CryptoTenSymbolFactorStrategyEvaluationError(
            "evaluation_label_return_invalid"
        )
    slip = SLIPPAGE_BPS / Decimal("10000")
    return dict(sample), (Decimal("1") + expected_net) * (
        Decimal("1") - slip
    ) ** 2 - Decimal("1")


# ---------------------------------------------------------------------------
# Metrics, baselines, artifacts
# ---------------------------------------------------------------------------

def _metric_basis(horizon: int) -> str:
    bars = horizon // 5
    return (
        "equal_weight_by_market_slot exploratory drawdown; turnover is exposure"
        " rate; round_trip_leg_rate is legs per resolved sample; "
        f"{horizon}min labels overlap {bars - 1}/{bars} across adjacent 5m"
        " slots, so effective independent samples are about"
        f" 1/{bars} of resolved_count; HAC and non-overlapping subsample"
        " significance are deferred"
    )


def _metrics(
    rows: Sequence[tuple[Mapping[str, Any], Decimal]],
    signals: Sequence[bool],
    baseline: Decimal | None = None,
    *,
    horizon: int,
) -> dict[str, Any]:
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
        equity *= Decimal("1") + sum(slot_returns[slot], Decimal("0")) / Decimal(
            len(slot_returns[slot])
        )
        peak = max(peak, equity)
        if peak:
            maximum_drawdown = max(maximum_drawdown, (peak - equity) / peak)
    return {
        "resolved_count": count,
        "signal_count": len(selected),
        "abstention_count": count - len(selected),
        "coverage": _text(Decimal(len(selected)) / Decimal(count)) if count else "0",
        "hit_rate": _text(
            Decimal(sum(value > 0 for value in selected)) / Decimal(len(selected))
        )
        if selected
        else None,
        "cost_adjusted_net_return": _text(mean),
        "baseline_delta": _text(mean - baseline)
        if mean is not None and baseline is not None
        else None,
        "drawdown": _text(maximum_drawdown),
        "turnover": _text(Decimal(len(selected)) / Decimal(count)) if count else "0",
        "round_trip_leg_rate": _text(Decimal(2 * len(selected)) / Decimal(count))
        if count
        else "0",
        "metric_basis": _metric_basis(horizon),
    }


def _cash_baseline(resolved_count: int) -> dict[str, Any]:
    """Existing no-position/cash behavior: no costs, return, drawdown or turnover."""

    return {
        "resolved_count": resolved_count,
        "signal_count": 0,
        "abstention_count": resolved_count,
        "coverage": "0",
        "hit_rate": None,
        "cost_adjusted_net_return": "0",
        "baseline_delta": None,
        "drawdown": "0",
        "turnover": "0",
        "round_trip_leg_rate": "0",
        "metric_basis": "cash_no_position",
    }


def _strategy_identity(strategy_name: str, hypothesis_id: str) -> dict[str, Any]:
    material = {
        "strategy_name": strategy_name,
        "factor_hypothesis_id": hypothesis_id,
        "feature_set_id": TEN_SYMBOL_FACTOR_SET_ID,
        "feature_set_version": TEN_SYMBOL_FACTOR_SET_VERSION,
        "cost_policy_id": COST_POLICY_ID,
    }
    return {
        "strategy_name": strategy_name,
        "strategy_version": _sha(material),
        "factor_hypothesis_id": hypothesis_id,
        "feature_set_id": TEN_SYMBOL_FACTOR_SET_ID,
        "feature_set_version": TEN_SYMBOL_FACTOR_SET_VERSION,
        "cost_policy_id": COST_POLICY_ID,
    }


def _evaluate_strategy(
    *,
    strategy_name: str,
    hypothesis_id: str,
    rows: Sequence[tuple[Mapping[str, Any], Decimal]],
    baseline: Mapping[str, Any],
    baseline_mean: Decimal | None,
    cash_baseline: Mapping[str, Any],
    evaluation_as_of: datetime,
    horizon: int,
) -> dict[str, Any]:
    signals = [_signal(hypothesis_id, row[0]["snapshot"]) for row in rows]
    metrics = _metrics(rows, signals, baseline_mean, horizon=horizon)
    strategy_mean = (
        _decimal(metrics["cost_adjusted_net_return"], "strategy_mean")
        if metrics["cost_adjusted_net_return"] is not None
        else None
    )
    metrics["cash_baseline_delta"] = (
        _text(strategy_mean) if strategy_mean is not None else None
    )
    action = (
        "disable"
        if not any(signals)
        else "downweight"
        if strategy_mean is not None and strategy_mean <= 0
        else "retain_for_more_evidence"
    )
    artifact = {
        "contract": EVALUATION_CONTRACT,
        **_strategy_identity(strategy_name, hypothesis_id),
        "evaluation_as_of": _iso(evaluation_as_of),
        "horizon_minutes": horizon,
        "research_attribution": horizon != HORIZON_MINUTES,
        "resolved_count": len(rows),
        "evaluated_status": "exploratory_insufficient_edge",
        "baseline": dict(baseline),
        "cash_baseline": dict(cash_baseline),
        "metrics": metrics,
        "recommendation": {
            "shadow_only_action": action,
            "parameter_suggestion": "no_automatic_parameter_change",
        },
        **_safe(),
    }
    artifact["evaluation_sha256"] = _sha(artifact)
    return artifact


def _atomic_checkpoint(path: Path, payload: Mapping[str, Any]) -> None:
    encoded = (_json(payload) + "\n").encode()
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as exc:
        raise CryptoTenSymbolFactorStrategyEvaluationError(
            "evaluation_checkpoint_write_failed"
        ) from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _validated_current(evolution: Path) -> dict[str, Any] | None:
    """Validate only the compact checkpoint and its one bound artifact."""

    checkpoint_path = evolution / CHECKPOINT_FILENAME
    if not checkpoint_path.exists() and not checkpoint_path.is_symlink():
        return None
    current = projection._parse_canonical(
        checkpoint_path, reason="evaluation_checkpoint_invalid"
    )
    material = dict(current)
    claimed = material.pop("checkpoint_sha256", None)
    if (
        current.get("contract") != EVALUATION_CHECKPOINT_CONTRACT
        or claimed != _sha(material)
        or any(
            current.get(key) != value
            for key, value in projection._non_authority_fields().items()
        )
    ):
        raise CryptoTenSymbolFactorStrategyEvaluationError(
            "evaluation_checkpoint_invalid"
        )
    prior_outcome = current.get("last_evaluated_outcome_sha256")
    if (
        not isinstance(prior_outcome, str)
        or len(prior_outcome) != 64
        or not isinstance(current.get("artifact_sha256"), str)
        or len(current["artifact_sha256"]) != 64
    ):
        raise CryptoTenSymbolFactorStrategyEvaluationError(
            "evaluation_checkpoint_invalid"
        )
    artifact_path = evolution / "strategy_evaluations" / f"{prior_outcome}.json"
    try:
        prior_artifact = projection._parse_canonical(
            artifact_path, reason="evaluation_artifact_invalid"
        )
    except projection.CryptoTenSymbolFactorProjectionError as exc:
        raise CryptoTenSymbolFactorStrategyEvaluationError(
            "evaluation_artifact_invalid"
        ) from exc
    artifact_material = dict(prior_artifact)
    artifact_sha256 = artifact_material.pop("artifact_sha256", None)
    if (
        artifact_sha256 != _sha(artifact_material)
        or current.get("artifact_sha256") != artifact_sha256
        or prior_artifact.get("last_evaluated_outcome_sha256") != prior_outcome
    ):
        raise CryptoTenSymbolFactorStrategyEvaluationError(
            "evaluation_artifact_invalid"
        )
    return current


def _no_checkpoint_result() -> dict[str, Any]:
    return {
        "contract": EVALUATION_BUNDLE_CONTRACT,
        "status": "no_evaluation_checkpoint",
        "reason": "evaluation_checkpoint_missing_pre_first_scrub",
        **_safe(),
    }


def run_ten_symbol_factor_strategy_evaluation(
    *,
    store_root: Path | str,
    evaluation_as_of: str | datetime | None = None,
) -> dict[str, Any]:
    """Evaluate all three hypotheses at every registered label horizon.

    Samples are rebuilt for the required 60min horizon and every auxiliary
    attribution horizon (240/720/1440).  Each horizon with resolved samples
    gets its own per-hypothesis evaluation inside one immutable bundle;
    horizons without enough history report an explicit
    ``insufficient_resolved_samples`` status instead of failing.  The
    bundle-level recommendation deliberately stays scoped to the required
    60min definition.
    """

    _assert_simulation_only()
    root = Path(store_root)
    evolution = projection._root(root)
    if evolution.exists() and (evolution.is_symlink() or not evolution.is_dir()):
        raise CryptoTenSymbolFactorStrategyEvaluationError(
            "evaluation_directory_invalid"
        )
    if not evolution.exists():
        return {
            "contract": EVALUATION_BUNDLE_CONTRACT,
            "status": "insufficient_resolved_samples",
            "resolved_count": 0,
            **_safe(),
        }
    try:
        with projection._lock(evolution):
            samples, _ = _inventory(root)
            if not samples:
                return {
                    "contract": EVALUATION_BUNDLE_CONTRACT,
                    "status": "insufficient_resolved_samples",
                    "resolved_count": 0,
                    **_safe(),
                }
            outcome = _outcome_sha256(samples)
            current = _validated_current(evolution)
            if current is not None and current.get(
                "last_evaluated_outcome_sha256"
            ) == outcome:
                return {
                    "contract": EVALUATION_BUNDLE_CONTRACT,
                    "status": "no_new_outcome",
                    "last_evaluated_outcome_sha256": outcome,
                    "artifact_sha256": current["artifact_sha256"],
                    **_safe(),
                }
            if evaluation_as_of is None:
                as_of = max(
                    _utc(sample["label"].get("future_observed_at"), "future_observed_at")
                    for sample in samples
                )
            elif isinstance(evaluation_as_of, datetime):
                if (
                    evaluation_as_of.tzinfo is None
                    or evaluation_as_of.utcoffset() != timedelta(0)
                ):
                    raise CryptoTenSymbolFactorStrategyEvaluationError(
                        "evaluation_evaluation_as_of_invalid"
                    )
                as_of = evaluation_as_of.astimezone(timezone.utc)
            else:
                as_of = _utc(evaluation_as_of, "evaluation_as_of")
            rows_by_horizon: dict[int, list[tuple[dict[str, Any], Decimal]]] = {
                horizon: [] for horizon in EVALUATION_HORIZONS
            }
            identities: set[tuple[Any, ...]] = set()
            # Every sample from one inventory run embeds the same checkpoint
            # chain object, so the expensive full replay is performed once;
            # any sample carrying a different chain object fails closed.
            verified_chain = _verify_chain_replay(samples[0])
            shared_chain = samples[0]["projection_checkpoint_chain"]
            for sample in samples:
                if sample["projection_checkpoint_chain"] is not shared_chain:
                    raise CryptoTenSymbolFactorStrategyEvaluationError(
                        "evaluation_checkpoint_chain_invalid"
                    )
                resolved = _resolved(sample, as_of, verified_chain=verified_chain)
                identity = (
                    sample.get("segment_id"),
                    resolved[0]["snapshot"].get("observation_id"),
                    resolved[0]["snapshot"].get("symbol"),
                    sample["horizon_minutes"],
                )
                if identity in identities:
                    raise CryptoTenSymbolFactorStrategyEvaluationError(
                        "evaluation_sample_duplicate"
                    )
                identities.add(identity)
                rows_by_horizon[int(sample["horizon_minutes"])].append(resolved)
            required_rows = rows_by_horizon[HORIZON_MINUTES]
            if not required_rows:
                return {
                    "contract": EVALUATION_BUNDLE_CONTRACT,
                    "status": "insufficient_resolved_samples",
                    "resolved_count": 0,
                    **_safe(),
                }
            evaluations_by_horizon: dict[str, dict[str, Any]] = {}
            horizon_status: dict[str, str] = {}
            resolved_count_by_horizon: dict[str, int] = {}
            for horizon in EVALUATION_HORIZONS:
                rows = rows_by_horizon[horizon]
                horizon_status[str(horizon)] = (
                    "evaluated" if rows else "insufficient_resolved_samples"
                )
                resolved_count_by_horizon[str(horizon)] = len(rows)
                if not rows:
                    continue
                rows.sort(
                    key=lambda row: _utc(
                        row[0]["snapshot"]["market_slot"], "market_slot"
                    )
                )
                baseline = _metrics(rows, [True] * len(rows), horizon=horizon)
                baseline_mean = (
                    _decimal(baseline["cost_adjusted_net_return"], "baseline_mean")
                    if baseline["cost_adjusted_net_return"] is not None
                    else None
                )
                cash_baseline = _cash_baseline(len(rows))
                evaluations_by_horizon[str(horizon)] = {
                    name: _evaluate_strategy(
                        strategy_name=name,
                        hypothesis_id=hypothesis_id,
                        rows=rows,
                        baseline=baseline,
                        baseline_mean=baseline_mean,
                        cash_baseline=cash_baseline,
                        evaluation_as_of=as_of,
                        horizon=horizon,
                    )
                    for name, hypothesis_id in STRATEGY_HYPOTHESIS_PAIRS.items()
                }
            required_evaluations = evaluations_by_horizon[str(HORIZON_MINUTES)]
            artifact = {
                "contract": EVALUATION_BUNDLE_CONTRACT,
                "status": "shadow_evaluated",
                "last_evaluated_outcome_sha256": outcome,
                "evaluation_as_of": _iso(as_of),
                "resolved_count": len(required_rows),
                "resolved_count_by_horizon": resolved_count_by_horizon,
                "horizon_status": horizon_status,
                "evaluations": evaluations_by_horizon,
                # The recommendation deliberately stays scoped to the
                # required 60min definition; auxiliary horizons are
                # directional research attribution only.
                "recommendation": {
                    name: value["recommendation"]["shadow_only_action"]
                    for name, value in required_evaluations.items()
                },
                **_safe(),
            }
            artifact["artifact_sha256"] = _sha(artifact)
            directory = evolution / "strategy_evaluations"
            if directory.exists() or directory.is_symlink():
                if directory.is_symlink() or not directory.is_dir():
                    raise CryptoTenSymbolFactorStrategyEvaluationError(
                        "evaluation_directory_invalid"
                    )
            else:
                directory.mkdir(mode=0o700)
            projection._write_immutable(directory / f"{outcome}.json", artifact)
            checkpoint = {
                "contract": EVALUATION_CHECKPOINT_CONTRACT,
                "last_evaluated_outcome_sha256": outcome,
                "artifact_sha256": artifact["artifact_sha256"],
                **_safe(),
            }
            checkpoint["checkpoint_sha256"] = _sha(checkpoint)
            _atomic_checkpoint(evolution / CHECKPOINT_FILENAME, checkpoint)
            return artifact
    except projection.CryptoTenSymbolFactorProjectionError as exc:
        raise CryptoTenSymbolFactorStrategyEvaluationError(
            "evaluation_projection_inventory_invalid"
        ) from exc


def run_ten_symbol_factor_strategy_evaluation_fast(
    *,
    store_root: Path | str,
) -> dict[str, Any]:
    """Compact-checkpoint-only path for routine incremental rounds.

    Incremental projection never settles labels, so the resolved outcome can
    only change after a full scrub.  This path therefore never rebuilds the
    sample inventory: before the first evaluated scrub it reports an explicit
    skip instead of failing, afterwards it reports ``no_new_outcome`` with
    the validated checkpoint binding.
    """

    _assert_simulation_only()
    root = Path(store_root)
    evolution = projection._root(root)
    if not evolution.exists():
        return _no_checkpoint_result()
    if evolution.is_symlink() or not evolution.is_dir():
        raise CryptoTenSymbolFactorStrategyEvaluationError(
            "evaluation_directory_invalid"
        )
    try:
        with projection._lock(evolution):
            current = _validated_current(evolution)
    except projection.CryptoTenSymbolFactorProjectionError as exc:
        raise CryptoTenSymbolFactorStrategyEvaluationError(
            "evaluation_checkpoint_invalid"
        ) from exc
    if current is None:
        return _no_checkpoint_result()
    return {
        "contract": EVALUATION_BUNDLE_CONTRACT,
        "status": "no_new_outcome",
        "last_evaluated_outcome_sha256": current["last_evaluated_outcome_sha256"],
        "artifact_sha256": current["artifact_sha256"],
        **_safe(),
    }


__all__ = [
    "COST_POLICY_ID",
    "EVALUATION_BUNDLE_CONTRACT",
    "EVALUATION_CHECKPOINT_CONTRACT",
    "EVALUATION_CONTRACT",
    "STRATEGY_HYPOTHESIS_PAIRS",
    "CryptoTenSymbolFactorStrategyEvaluationError",
    "run_ten_symbol_factor_strategy_evaluation",
    "run_ten_symbol_factor_strategy_evaluation_fast",
]
