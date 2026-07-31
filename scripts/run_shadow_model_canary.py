#!/usr/bin/env python3
"""Run a deterministic, zero-authority shadow model engineering canary."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.models.shadow_baselines import (  # noqa: E402
    BaselineModelConfig,
    FrozenFeatureVector,
    FrozenShadowDataset,
    FrozenTrainingExample,
    fit_shadow_baseline,
    predict_shadow_baseline,
)
from shared.models.shadow_lightgbm import (  # noqa: E402
    LightGBMShadowConfig,
    fit_lightgbm_shadow,
    predict_lightgbm_shadow,
)


_BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _fixture_dataset(*, task: str, count: int) -> FrozenShadowDataset:
    examples = []
    for index in range(count):
        decision = _BASE + timedelta(minutes=index * 10)
        vector = FrozenFeatureVector(
            sample_id=f"fixture-train-{index:04d}",
            event_time=decision - timedelta(minutes=5),
            available_at=decision - timedelta(seconds=15),
            decision_time=decision,
            source_receipt_sha256=f"{index + 1:064x}",
            features=(
                ("momentum_5", float(index - count / 2) / count),
                ("realized_vol", float((index % 7) + 1) / 10.0),
            ),
        )
        label = (
            float(index >= count / 2)
            if task == "binary_direction"
            else float(index - count / 2) / (count * 100.0)
        )
        examples.append(
            FrozenTrainingExample(
                vector=vector,
                label=label,
                label_available_at=decision + timedelta(minutes=5),
            )
        )
    cutoff = _BASE + timedelta(minutes=count * 10 + 5)
    predictions = tuple(
        FrozenFeatureVector(
            sample_id=f"fixture-predict-{index:04d}",
            event_time=cutoff + timedelta(minutes=index * 5 + 5),
            available_at=cutoff + timedelta(minutes=index * 5 + 9, seconds=45),
            decision_time=cutoff + timedelta(minutes=index * 5 + 10),
            source_receipt_sha256=f"{count + index + 1:064x}",
            features=(
                ("momentum_5", float(index + 1) / 10.0),
                ("realized_vol", float(index + 2) / 10.0),
            ),
        )
        for index in range(4)
    )
    return FrozenShadowDataset(
        market="crypto",
        task=task,
        feature_contract_version="tradingagent.engineering_fixture_features.v1",
        label_policy_id="engineering-fixture-label-only-v1",
        training_cutoff=cutoff,
        training_examples=tuple(examples),
        prediction_vectors=predictions,
        historical_pit_verified=False,
        revision_history_verified=False,
    )


def run_canary(*, backend: str) -> dict[str, object]:
    normalized_real_trading = os.environ.get("REAL_TRADING_ENABLED", "false").lower()
    if normalized_real_trading != "false":
        raise RuntimeError("real_trading_must_remain_disabled")
    started = time.perf_counter()
    if backend == "ridge":
        dataset = _fixture_dataset(task="return_regression", count=48)
        artifact = fit_shadow_baseline(
            dataset,
            BaselineModelConfig(
                model_family="ridge_regression",
                model_version="ridge-engineering-canary-v1",
            ),
        )
        receipt = predict_shadow_baseline(
            artifact=artifact,
            dataset=dataset,
            evaluated_at=_BASE + timedelta(days=2),
        )
    elif backend == "logistic":
        dataset = _fixture_dataset(task="binary_direction", count=48)
        artifact = fit_shadow_baseline(
            dataset,
            BaselineModelConfig(
                model_family="elastic_net_logistic",
                model_version="elastic-net-logistic-engineering-canary-v1",
                l1_penalty=0.01,
                l2_penalty=0.05,
            ),
        )
        receipt = predict_shadow_baseline(
            artifact=artifact,
            dataset=dataset,
            evaluated_at=_BASE + timedelta(days=2),
        )
    elif backend == "lightgbm":
        dataset = _fixture_dataset(task="binary_direction", count=48)
        config = LightGBMShadowConfig()
        artifact = fit_lightgbm_shadow(dataset, config)
        receipt = predict_lightgbm_shadow(
            artifact=artifact,
            dataset=dataset,
            config=config,
            evaluated_at=_BASE + timedelta(days=2),
        )
    else:
        raise ValueError("unsupported_shadow_canary_backend")
    elapsed_ms = round((time.perf_counter() - started) * 1000.0, 3)
    return {
        "artifact_sha256": artifact.artifact_sha256,
        "authority": receipt.authority,
        "automatic_promotion_enabled": receipt.automatic_promotion_enabled,
        "backend": backend,
        "capital_authority": receipt.capital_authority,
        "dataset_sha256": dataset.dataset_sha256,
        "elapsed_ms": elapsed_ms,
        "execution_authority": receipt.execution_authority,
        "fixture_only": True,
        "model_network_used": False,
        "output_semantics": receipt.output_semantics,
        "predictive_validation_input_eligible": (
            receipt.predictive_validation_input_eligible
        ),
        "real_trading_enabled": receipt.real_trading_enabled,
        "receipt_sha256": receipt.receipt_sha256,
        "risk_expansion_allowed": receipt.risk_expansion_allowed,
        "score_count": len(receipt.scores),
        "schema_version": "tradingagent.shadow_model_engineering_canary.v1",
        "shadow_only": receipt.shadow_only,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--backend",
        choices=("ridge", "logistic", "lightgbm"),
        required=True,
    )
    args = parser.parse_args(argv)
    try:
        result = run_canary(backend=args.backend)
    except Exception:
        print("shadow model canary failed closed", file=sys.stderr)
        return 2
    print(
        json.dumps(
            result,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
