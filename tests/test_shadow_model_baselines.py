from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from shared.models.shadow_baselines import (
    BaselineModelConfig,
    FrozenFeatureVector,
    FrozenShadowDataset,
    FrozenTrainingExample,
    ShadowBaselineError,
    fit_shadow_baseline,
    predict_shadow_baseline,
)


UTC = timezone.utc
BASE = datetime(2026, 7, 1, tzinfo=UTC)


def _vector(index: int, *, prediction: bool = False) -> FrozenFeatureVector:
    decision = BASE + timedelta(days=index, hours=3)
    if prediction:
        decision = BASE + timedelta(days=20 + index, hours=3)
    return FrozenFeatureVector(
        sample_id=("predict" if prediction else "train") + f"-{index:03d}",
        event_time=decision - timedelta(minutes=5),
        available_at=decision - timedelta(seconds=20),
        decision_time=decision,
        source_receipt_sha256=f"{index + 1:064x}",
        features=(
            ("momentum_5", float(index - 4)),
            ("realized_vol", float((index % 3) + 1)),
        ),
    )


def _dataset(
    *,
    task: str = "binary_direction",
    pit: bool = True,
    revisions: bool = True,
) -> FrozenShadowDataset:
    examples = []
    for index in range(12):
        label = float(index >= 6) if task == "binary_direction" else index * 0.01
        examples.append(
            FrozenTrainingExample(
                vector=_vector(index),
                label=label,
                label_available_at=BASE + timedelta(days=index, hours=6),
            )
        )
    return FrozenShadowDataset(
        market="crypto",
        task=task,
        feature_contract_version="tradingagent.test.features.v1",
        label_policy_id="test-forward-label-v1",
        training_cutoff=BASE + timedelta(days=15),
        training_examples=tuple(examples),
        prediction_vectors=(_vector(0, prediction=True), _vector(1, prediction=True)),
        historical_pit_verified=pit,
        revision_history_verified=revisions,
    )


def test_elastic_net_logistic_is_deterministic_and_authority_free() -> None:
    dataset = _dataset()
    config = BaselineModelConfig(
        model_family="elastic_net_logistic",
        model_version="elastic-net-logistic-shadow-v1",
        l1_penalty=0.01,
        l2_penalty=0.05,
        learning_rate=0.1,
        iterations=500,
    )

    first_artifact = fit_shadow_baseline(dataset, config)
    second_artifact = fit_shadow_baseline(dataset, config)
    assert first_artifact == second_artifact
    assert first_artifact.output_semantics == "uncalibrated_logit_score"

    evaluated_at = BASE + timedelta(days=30)
    first = predict_shadow_baseline(
        artifact=first_artifact,
        dataset=dataset,
        evaluated_at=evaluated_at,
    )
    second = predict_shadow_baseline(
        artifact=second_artifact,
        dataset=dataset,
        evaluated_at=evaluated_at,
    )
    assert first == second
    assert first.predictive_validation_input_eligible is True
    assert first.authority == "none"
    assert first.shadow_only is True
    assert first.execution_eligible is False
    assert first.execution_authority is False
    assert first.capital_authority is False
    assert first.risk_expansion_allowed is False
    assert first.automatic_promotion_enabled is False
    assert first.real_trading_enabled is False
    assert first.scores[1].score > first.scores[0].score


def test_ridge_regression_is_a_separate_uncalibrated_control() -> None:
    dataset = _dataset(task="return_regression")
    config = BaselineModelConfig(
        model_family="ridge_regression",
        model_version="ridge-shadow-v1",
        l2_penalty=0.25,
    )
    artifact = fit_shadow_baseline(dataset, config)
    receipt = predict_shadow_baseline(
        artifact=artifact,
        dataset=dataset,
        evaluated_at=BASE + timedelta(days=30),
    )
    assert artifact.output_semantics == "uncalibrated_return_score"
    assert receipt.output_semantics == "uncalibrated_return_score"
    assert len(receipt.scores) == 2


def test_unverified_history_can_run_engineering_shadow_but_not_validation() -> None:
    dataset = _dataset(pit=False, revisions=False)
    artifact = fit_shadow_baseline(
        dataset,
        BaselineModelConfig(
            model_family="elastic_net_logistic",
            model_version="engineering-shadow-v1",
        ),
    )
    receipt = predict_shadow_baseline(
        artifact=artifact,
        dataset=dataset,
        evaluated_at=BASE + timedelta(days=30),
    )
    assert artifact.predictive_validation_input_eligible is False
    assert receipt.predictive_validation_input_eligible is False


def test_future_feature_is_rejected_before_training() -> None:
    vector = _vector(0)
    with pytest.raises(ShadowBaselineError, match="feature_time_order_invalid"):
        replace(vector, available_at=vector.decision_time + timedelta(seconds=1))


def test_label_after_training_cutoff_is_rejected() -> None:
    dataset = _dataset()
    changed = replace(
        dataset.training_examples[-1],
        label_available_at=dataset.training_cutoff + timedelta(seconds=1),
    )
    with pytest.raises(ShadowBaselineError, match="training_label_after_cutoff"):
        replace(
            dataset,
            training_examples=dataset.training_examples[:-1] + (changed,),
        )


def test_feature_contract_drift_is_rejected() -> None:
    dataset = _dataset()
    changed = replace(
        dataset.prediction_vectors[0],
        features=(
            ("momentum_5", 1.0),
            ("unexpected_feature", 2.0),
        ),
    )
    with pytest.raises(ShadowBaselineError, match="feature_contract_mismatch"):
        replace(
            dataset,
            prediction_vectors=(changed,) + dataset.prediction_vectors[1:],
        )


def test_nonfinite_feature_and_wrong_binary_label_fail_closed() -> None:
    with pytest.raises(ShadowBaselineError, match="feature:momentum_5_invalid"):
        replace(
            _vector(0),
            features=(("momentum_5", float("nan")), ("realized_vol", 1.0)),
        )

    dataset = _dataset()
    with pytest.raises(ShadowBaselineError, match="binary_label_invalid"):
        replace(
            dataset,
            training_examples=(replace(dataset.training_examples[0], label=0.5),)
            + dataset.training_examples[1:],
        )


def test_single_class_binary_training_set_fails_closed() -> None:
    dataset = _dataset()
    with pytest.raises(
        ShadowBaselineError,
        match="binary_label_classes_incomplete",
    ):
        replace(
            dataset,
            training_examples=tuple(
                replace(item, label=0.0) for item in dataset.training_examples
            ),
        )


def test_insufficient_samples_and_artifact_rebinding_fail_closed() -> None:
    dataset = _dataset()
    with pytest.raises(ShadowBaselineError, match="training_sample_count_insufficient"):
        fit_shadow_baseline(
            replace(dataset, training_examples=dataset.training_examples[:8]),
            BaselineModelConfig(
                model_family="elastic_net_logistic",
                model_version="minimum-sample-test-v1",
                minimum_training_samples=10,
            ),
        )

    artifact = fit_shadow_baseline(
        dataset,
        BaselineModelConfig(
            model_family="elastic_net_logistic",
            model_version="binding-test-v1",
        ),
    )
    other = replace(dataset, label_policy_id="different-policy")
    with pytest.raises(ShadowBaselineError, match="artifact_dataset_binding_mismatch"):
        predict_shadow_baseline(
            artifact=artifact,
            dataset=other,
            evaluated_at=BASE + timedelta(days=30),
        )


def test_prediction_cannot_be_evaluated_before_decision_time() -> None:
    dataset = _dataset()
    artifact = fit_shadow_baseline(
        dataset,
        BaselineModelConfig(
            model_family="elastic_net_logistic",
            model_version="time-binding-test-v1",
        ),
    )
    with pytest.raises(
        ShadowBaselineError,
        match="prediction_evaluated_before_decision",
    ):
        predict_shadow_baseline(
            artifact=artifact,
            dataset=dataset,
            evaluated_at=BASE + timedelta(days=19),
        )


def test_prediction_batch_can_change_without_retraining_artifact() -> None:
    dataset = _dataset()
    artifact = fit_shadow_baseline(
        dataset,
        BaselineModelConfig(
            model_family="elastic_net_logistic",
            model_version="reusable-artifact-v1",
        ),
    )
    next_batch = replace(
        dataset,
        prediction_vectors=(_vector(3, prediction=True),),
    )
    assert next_batch.dataset_sha256 != dataset.dataset_sha256
    assert next_batch.training_sha256 == dataset.training_sha256
    receipt = predict_shadow_baseline(
        artifact=artifact,
        dataset=next_batch,
        evaluated_at=BASE + timedelta(days=30),
    )
    assert receipt.dataset_sha256 == next_batch.dataset_sha256
    assert receipt.artifact_sha256 == artifact.artifact_sha256


def test_prediction_must_be_strictly_after_training_cutoff() -> None:
    dataset = _dataset()
    in_sample = replace(
        dataset.prediction_vectors[0],
        decision_time=dataset.training_cutoff,
        available_at=dataset.training_cutoff - timedelta(seconds=20),
        event_time=dataset.training_cutoff - timedelta(minutes=5),
    )
    with pytest.raises(
        ShadowBaselineError,
        match="prediction_not_strictly_out_of_sample",
    ):
        replace(dataset, prediction_vectors=(in_sample,))
