"""Idempotent private shadow evaluation after detached factor projection."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping
import uuid

import Crypto.delayed_paper_factor_research as projection
from Crypto.factor_strategy_evaluation import (
    COST_POLICY_ID,
    CryptoFactorStrategyEvaluationError,
    _sample_binding_sha256,
    build_factor_strategy_evaluation,
)
from Crypto.fixture_sim.contracts import _assert_simulation_only


POST_PROJECTION_CONTRACT = "tradingagent.crypto.factor_strategy_post_projection.v1"
POST_PROJECTION_CHECKPOINT_CONTRACT = (
    "tradingagent.crypto.factor_strategy_post_projection_checkpoint.v1"
)
_STRATEGIES = {
    "momentum": "momentum",
    "trend_pullback": "trend",
    "volume_breakout": "volatility",
}


class CryptoFactorStrategyPostProjectionError(RuntimeError):
    """Stable evaluation-debt boundary; never rolls back learning projection."""


def _safe() -> dict[str, Any]:
    return {
        "authority": "none", "read_only": False, "research_only": True,
        "simulation_only": True, "learning_authority": False,
        "execution_authority": False, "execution_eligible": False,
        "production_eligible": False, "promotion_authorized": False,
        "automatic_champion_replacement": False,
        "automatic_risk_expansion_enabled": False,
        "real_trading_enabled": False, "network_used": False,
        "model_network_used": False, "live_broker_used": False,
    }


def _atomic_checkpoint(path: Path, payload: Mapping[str, Any]) -> None:
    encoded = (projection._canonical_json(payload) + "\n").encode()
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
        raise CryptoFactorStrategyPostProjectionError(
            "factor_strategy_checkpoint_write_failed"
        ) from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _inventory(root: Path) -> list[dict[str, Any]]:
    evolution = projection._root(root)
    if not evolution.is_dir() or evolution.is_symlink():
        return []
    checkpoints = projection._read_checkpoints(evolution)
    if not checkpoints:
        return []
    _, sources = projection._sources(root)
    source_by_id = {
        str(item["observation"]["observation_id"]): item for item in sources
    }
    records: dict[str, dict[str, Any]] = {}
    proof_by_id: dict[str, dict[str, Any]] = {}
    for checkpoint in checkpoints:
        observation_id = str(checkpoint["observation_id"])
        source = source_by_id.get(observation_id)
        if source is None:
            raise CryptoFactorStrategyPostProjectionError(
                "factor_strategy_source_completion_missing"
            )
        paths = projection._paths(root, observation_id)
        record = projection._parse_canonical(
            paths["record"], reason="factor_projection_record_invalid"
        )
        receipt = projection._parse_canonical(
            paths["receipt"], reason="factor_projection_receipt_invalid"
        )
        slot = str(record.get("market_slot"))
        records[slot] = record
        proof_by_id[observation_id] = {
            "completion": source["completion"], "record": record,
            "receipt": receipt, "checkpoint": checkpoint,
        }
    result: list[dict[str, Any]] = []
    for checkpoint in checkpoints:
        observation_id = str(checkpoint["observation_id"])
        record = proof_by_id[observation_id]["record"]
        source_slot = projection._utc(
            record.get("market_slot"), reason="factor_projection_record_invalid"
        )
        future_slot = (source_slot + projection.timedelta(minutes=60)).isoformat().replace(
            "+00:00", "Z"
        )
        future_record = records.get(future_slot)
        if not isinstance(future_record, Mapping) or future_record.get("segment_id") != record.get("segment_id"):
            continue
        future_id = str(future_record.get("observation_id"))
        samples: list[dict[str, Any]] = []
        labels: dict[str, str] = {}
        for symbol in ("BTCUSDT", "ETHUSDT"):
            label_path = projection._label_path(root, observation_id, symbol, 60)
            if not label_path.is_file() or label_path.is_symlink():
                samples = []
                break
            label = projection._parse_canonical(
                label_path, reason="factor_projection_label_invalid"
            )
            snapshot = record["snapshots"][symbol]
            sample = {
                "snapshot": snapshot, "label": label,
                "segment_id": record["segment_id"],
                "future_segment_id": future_record["segment_id"],
                "source_completion_sha256": proof_by_id[observation_id]["completion"]["completion_sha256"],
                "future_completion_sha256": proof_by_id[future_id]["completion"]["completion_sha256"],
                "future_observation_id": future_id,
                "source_projection_proof": proof_by_id[observation_id],
                "future_projection_proof": proof_by_id[future_id],
                "projection_checkpoint_chain": checkpoints,
                "expected_checkpoint_head_sha256": checkpoints[-1]["checkpoint_sha256"],
                "cost_policy": {
                    "cost_policy_id": COST_POLICY_ID,
                    "fee_rate": "0.001",
                    "slippage_bps_each_side": "2",
                },
            }
            sample["sample_binding_sha256"] = _sample_binding_sha256(sample)
            samples.append(sample)
            labels[symbol] = str(label["forward_label_sha256"])
        if len(samples) != 2:
            continue
        outcome = {
            "source_completion_sha256": proof_by_id[observation_id]["completion"]["completion_sha256"],
            "future_completion_sha256": proof_by_id[future_id]["completion"]["completion_sha256"],
            "labels": labels,
        }
        result.append(
            {
                "completion_sha256": outcome["source_completion_sha256"],
                "outcome_sha256": projection._sha256(outcome),
                "evaluation_as_of": max(str(item["label"]["future_observed_at"]) for item in samples),
                "samples": samples,
            }
        )
    return result


def _run_locked(root: Path) -> dict[str, Any]:
    try:
        inventory = _inventory(root)
    except projection.CryptoFactorProjectionError as exc:
        raise CryptoFactorStrategyPostProjectionError(
            "factor_strategy_projection_inventory_invalid"
        ) from exc
    if not inventory:
        return {"contract": POST_PROJECTION_CONTRACT, "status": "no_new_outcome", **_safe()}
    evolution = projection._root(root)
    checkpoint_path = evolution / "strategy_evaluation_checkpoint.json"
    current: dict[str, Any] | None = None
    if checkpoint_path.exists() or checkpoint_path.is_symlink():
        try:
            current = projection._parse_canonical(
                checkpoint_path, reason="factor_strategy_checkpoint_invalid"
            )
        except projection.CryptoFactorProjectionError as exc:
            raise CryptoFactorStrategyPostProjectionError(
                "factor_strategy_checkpoint_invalid"
            ) from exc
        material = dict(current)
        claimed = material.pop("checkpoint_sha256", None)
        if (
            current.get("contract") != POST_PROJECTION_CHECKPOINT_CONTRACT
            or claimed != projection._sha256(material)
        ):
            raise CryptoFactorStrategyPostProjectionError(
                "factor_strategy_checkpoint_invalid"
            )
        prior_outcome = current.get("last_evaluated_outcome_sha256")
        if not isinstance(prior_outcome, str) or len(prior_outcome) != 64:
            raise CryptoFactorStrategyPostProjectionError(
                "factor_strategy_checkpoint_invalid"
            )
        artifact_path = evolution / "strategy_evaluations" / f"{prior_outcome}.json"
        try:
            prior_artifact = projection._parse_canonical(
                artifact_path, reason="factor_strategy_artifact_invalid"
            )
        except projection.CryptoFactorProjectionError as exc:
            raise CryptoFactorStrategyPostProjectionError(
                "factor_strategy_artifact_invalid"
            ) from exc
        artifact_material = dict(prior_artifact)
        artifact_sha256 = artifact_material.pop("artifact_sha256", None)
        if (
            artifact_sha256 != projection._sha256(artifact_material)
            or current.get("artifact_sha256") != artifact_sha256
        ):
            raise CryptoFactorStrategyPostProjectionError(
                "factor_strategy_artifact_invalid"
            )
    selected = inventory[-1]
    if current is not None and (
        current.get("last_evaluated_completion_sha256") == selected["completion_sha256"]
        and current.get("last_evaluated_outcome_sha256") == selected["outcome_sha256"]
    ):
        return {"contract": POST_PROJECTION_CONTRACT, "status": "no_new_outcome", **_safe()}
    try:
        evaluations = {
            name: build_factor_strategy_evaluation(
                samples=selected["samples"],
                evaluation_as_of=selected["evaluation_as_of"],
                strategy_name=strategy,
            )
            for name, strategy in _STRATEGIES.items()
        }
    except (CryptoFactorStrategyEvaluationError, KeyError, TypeError, ValueError) as exc:
        raise CryptoFactorStrategyPostProjectionError(
            "factor_strategy_evaluation_failed"
        ) from exc
    artifact = {
        "contract": POST_PROJECTION_CONTRACT,
        "status": "shadow_evaluated",
        "last_evaluated_completion_sha256": selected["completion_sha256"],
        "last_evaluated_outcome_sha256": selected["outcome_sha256"],
        "evaluations": evaluations,
        "recommendation": {
            name: value["recommendation"]["shadow_only_action"]
            for name, value in evaluations.items()
        },
        **_safe(),
    }
    artifact["artifact_sha256"] = projection._sha256(artifact)
    directory = evolution / "strategy_evaluations"
    if directory.exists() or directory.is_symlink():
        if directory.is_symlink() or not directory.is_dir():
            raise CryptoFactorStrategyPostProjectionError(
                "factor_strategy_directory_invalid"
            )
    else:
        directory.mkdir(mode=0o700)
    artifact_path = directory / f"{selected['outcome_sha256']}.json"
    projection._write_immutable(artifact_path, artifact)
    checkpoint = {
        "contract": POST_PROJECTION_CHECKPOINT_CONTRACT,
        "last_evaluated_completion_sha256": selected["completion_sha256"],
        "last_evaluated_outcome_sha256": selected["outcome_sha256"],
        "artifact_sha256": artifact["artifact_sha256"],
        **_safe(),
    }
    checkpoint["checkpoint_sha256"] = projection._sha256(checkpoint)
    _atomic_checkpoint(checkpoint_path, checkpoint)
    return artifact


def run_factor_strategy_post_projection(*, output_root: Path | str) -> dict[str, Any]:
    """Append one shadow bundle only when the newest resolved outcome changes."""

    _assert_simulation_only()
    root = Path(output_root)
    evolution = projection._root(root)
    if not evolution.exists():
        return {
            "contract": POST_PROJECTION_CONTRACT,
            "status": "no_new_outcome",
            **_safe(),
        }
    if evolution.is_symlink() or not evolution.is_dir():
        raise CryptoFactorStrategyPostProjectionError(
            "factor_strategy_directory_invalid"
        )
    try:
        with projection._lock(evolution):
            return _run_locked(root)
    except projection.CryptoFactorProjectionError as exc:
        raise CryptoFactorStrategyPostProjectionError(
            "factor_strategy_projection_inventory_invalid"
        ) from exc


__all__ = [
    "CryptoFactorStrategyPostProjectionError",
    "run_factor_strategy_post_projection",
]
