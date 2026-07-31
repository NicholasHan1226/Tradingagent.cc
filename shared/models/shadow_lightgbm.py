"""Optional CPU LightGBM challenger behind the shadow-model contract.

The dependency is loaded lazily from an isolated learning environment.  Raw
margin/return scores are intentionally uncalibrated and cannot reach capital,
risk, order, or automatic-promotion paths.
"""

from __future__ import annotations

import hashlib
import importlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Tuple

from .shadow_baselines import (
    FrozenShadowDataset,
    ShadowBaselineError,
    ShadowPredictionReceipt,
    ShadowScore,
    _aware,
    _finite,
    _require_sha256,
    _require_text,
    _sha256,
)


PINNED_LIGHTGBM_VERSION = "4.6.0"
PINNED_NUMPY_VERSION = "2.0.2"
PINNED_SCIPY_VERSION = "1.18.0"


def _load_module(name: str) -> Any:
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        raise ShadowBaselineError(f"{name}_dependency_unavailable") from exc


def _dependency_version(module: Any, *, dependency: str, expected: str) -> None:
    actual = getattr(module, "__version__", None)
    if actual != expected:
        raise ShadowBaselineError(f"{dependency}_version_mismatch")


@dataclass(frozen=True)
class LightGBMShadowConfig:
    model_version: str = "lightgbm-cpu-shadow-v1"
    minimum_training_samples: int = 40
    num_boost_round: int = 64
    learning_rate: float = 0.03
    num_leaves: int = 7
    max_depth: int = 3
    min_data_in_leaf: int = 10
    lambda_l1: float = 0.0
    lambda_l2: float = 1.0
    num_threads: int = 2
    seed: int = 1729
    expected_lightgbm_version: str = PINNED_LIGHTGBM_VERSION
    expected_numpy_version: str = PINNED_NUMPY_VERSION
    expected_scipy_version: str = PINNED_SCIPY_VERSION

    def __post_init__(self) -> None:
        _require_text(self.model_version, field_name="model_version")
        if (
            isinstance(self.minimum_training_samples, bool)
            or not isinstance(self.minimum_training_samples, int)
            or self.minimum_training_samples < 40
        ):
            raise ShadowBaselineError("minimum_training_samples_invalid")
        if (
            isinstance(self.num_boost_round, bool)
            or not isinstance(self.num_boost_round, int)
            or not 8 <= self.num_boost_round <= 256
        ):
            raise ShadowBaselineError("num_boost_round_invalid")
        _finite(self.learning_rate, field_name="learning_rate")
        if not 0.0 < self.learning_rate <= 0.2:
            raise ShadowBaselineError("learning_rate_invalid")
        if (
            isinstance(self.num_leaves, bool)
            or not isinstance(self.num_leaves, int)
            or not 3 <= self.num_leaves <= 31
        ):
            raise ShadowBaselineError("num_leaves_invalid")
        if (
            isinstance(self.max_depth, bool)
            or not isinstance(self.max_depth, int)
            or not 2 <= self.max_depth <= 6
        ):
            raise ShadowBaselineError("max_depth_invalid")
        if self.num_leaves > 2**self.max_depth:
            raise ShadowBaselineError("num_leaves_exceeds_depth_capacity")
        if (
            isinstance(self.min_data_in_leaf, bool)
            or not isinstance(self.min_data_in_leaf, int)
            or self.min_data_in_leaf < 5
        ):
            raise ShadowBaselineError("min_data_in_leaf_invalid")
        for field_name in ("lambda_l1", "lambda_l2"):
            value = _finite(getattr(self, field_name), field_name=field_name)
            if value < 0.0:
                raise ShadowBaselineError(f"{field_name}_invalid")
        if self.num_threads not in {1, 2}:
            raise ShadowBaselineError("num_threads_invalid")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int):
            raise ShadowBaselineError("seed_invalid")
        if self.expected_lightgbm_version != PINNED_LIGHTGBM_VERSION:
            raise ShadowBaselineError("lightgbm_expected_version_invalid")
        if self.expected_numpy_version != PINNED_NUMPY_VERSION:
            raise ShadowBaselineError("numpy_expected_version_invalid")
        if self.expected_scipy_version != PINNED_SCIPY_VERSION:
            raise ShadowBaselineError("scipy_expected_version_invalid")

    def canonical_payload(self) -> dict[str, object]:
        return {
            "expected_lightgbm_version": self.expected_lightgbm_version,
            "expected_numpy_version": self.expected_numpy_version,
            "expected_scipy_version": self.expected_scipy_version,
            "lambda_l1": self.lambda_l1,
            "lambda_l2": self.lambda_l2,
            "learning_rate": self.learning_rate,
            "max_depth": self.max_depth,
            "min_data_in_leaf": self.min_data_in_leaf,
            "minimum_training_samples": self.minimum_training_samples,
            "model_version": self.model_version,
            "num_boost_round": self.num_boost_round,
            "num_leaves": self.num_leaves,
            "num_threads": self.num_threads,
            "seed": self.seed,
        }


@dataclass(frozen=True)
class LightGBMShadowArtifact:
    model_version: str
    task: str
    feature_names: Tuple[str, ...]
    serialized_model: str
    training_dataset_sha256: str
    training_sample_count: int
    config_sha256: str
    output_semantics: str
    predictive_validation_input_eligible: bool
    lightgbm_version: str = PINNED_LIGHTGBM_VERSION
    numpy_version: str = PINNED_NUMPY_VERSION
    scipy_version: str = PINNED_SCIPY_VERSION
    schema_version: str = "tradingagent.lightgbm_shadow_artifact.v1"
    shadow_only: bool = True
    artifact_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        _require_text(self.model_version, field_name="model_version")
        if self.task not in {"binary_direction", "return_regression"}:
            raise ShadowBaselineError("artifact_task_invalid")
        if not isinstance(self.feature_names, tuple) or not self.feature_names:
            raise ShadowBaselineError("artifact_feature_names_invalid")
        _require_text(self.serialized_model, field_name="serialized_model")
        if len(self.serialized_model.encode("utf-8")) > 16 * 1024 * 1024:
            raise ShadowBaselineError("serialized_model_too_large")
        _require_sha256(
            self.training_dataset_sha256,
            field_name="training_dataset_sha256",
        )
        _require_sha256(self.config_sha256, field_name="config_sha256")
        if (
            isinstance(self.training_sample_count, bool)
            or not isinstance(self.training_sample_count, int)
            or self.training_sample_count < 40
        ):
            raise ShadowBaselineError("training_sample_count_invalid")
        expected_semantics = {
            "binary_direction": "uncalibrated_logit_score",
            "return_regression": "uncalibrated_return_score",
        }[self.task]
        if self.output_semantics != expected_semantics:
            raise ShadowBaselineError("output_semantics_invalid")
        if (
            self.lightgbm_version != PINNED_LIGHTGBM_VERSION
            or self.numpy_version != PINNED_NUMPY_VERSION
            or self.scipy_version != PINNED_SCIPY_VERSION
        ):
            raise ShadowBaselineError("artifact_dependency_version_invalid")
        if (
            self.schema_version != "tradingagent.lightgbm_shadow_artifact.v1"
            or self.shadow_only is not True
            or not isinstance(self.predictive_validation_input_eligible, bool)
        ):
            raise ShadowBaselineError("artifact_shadow_boundary_invalid")
        object.__setattr__(self, "artifact_sha256", _sha256(self.canonical_payload()))

    def canonical_payload(self) -> dict[str, object]:
        return {
            "config_sha256": self.config_sha256,
            "feature_names": list(self.feature_names),
            "lightgbm_version": self.lightgbm_version,
            "model_version": self.model_version,
            "numpy_version": self.numpy_version,
            "output_semantics": self.output_semantics,
            "predictive_validation_input_eligible": (
                self.predictive_validation_input_eligible
            ),
            "schema_version": self.schema_version,
            "scipy_version": self.scipy_version,
            "serialized_model_sha256": hashlib.sha256(
                self.serialized_model.encode("utf-8")
            ).hexdigest(),
            "shadow_only": True,
            "task": self.task,
            "training_dataset_sha256": self.training_dataset_sha256,
            "training_sample_count": self.training_sample_count,
        }


def _modules(config: LightGBMShadowConfig) -> tuple[Any, Any, Any]:
    numpy = _load_module("numpy")
    scipy = _load_module("scipy")
    lightgbm = _load_module("lightgbm")
    _dependency_version(
        numpy,
        dependency="numpy",
        expected=config.expected_numpy_version,
    )
    _dependency_version(
        scipy,
        dependency="scipy",
        expected=config.expected_scipy_version,
    )
    _dependency_version(
        lightgbm,
        dependency="lightgbm",
        expected=config.expected_lightgbm_version,
    )
    return numpy, scipy, lightgbm


def fit_lightgbm_shadow(
    dataset: FrozenShadowDataset,
    config: LightGBMShadowConfig,
) -> LightGBMShadowArtifact:
    if not isinstance(dataset, FrozenShadowDataset):
        raise ShadowBaselineError("shadow_dataset_required")
    if not isinstance(config, LightGBMShadowConfig):
        raise ShadowBaselineError("lightgbm_shadow_config_required")
    if len(dataset.training_examples) < config.minimum_training_samples:
        raise ShadowBaselineError("training_sample_count_insufficient")
    numpy, _scipy, lightgbm = _modules(config)
    features = numpy.asarray(
        [item.vector.feature_values for item in dataset.training_examples],
        dtype=numpy.float64,
    )
    labels = numpy.asarray(
        [item.label for item in dataset.training_examples],
        dtype=numpy.float64,
    )
    training = lightgbm.Dataset(
        features,
        label=labels,
        feature_name=list(dataset.feature_names),
        free_raw_data=True,
    )
    parameters = {
        "bagging_fraction": 1.0,
        "bagging_freq": 0,
        "bagging_seed": config.seed,
        "data_random_seed": config.seed,
        "deterministic": True,
        "feature_fraction": 1.0,
        "feature_fraction_seed": config.seed,
        "force_col_wise": True,
        "lambda_l1": config.lambda_l1,
        "lambda_l2": config.lambda_l2,
        "learning_rate": config.learning_rate,
        "max_depth": config.max_depth,
        "metric": "None",
        "min_data_in_leaf": config.min_data_in_leaf,
        "num_leaves": config.num_leaves,
        "num_threads": config.num_threads,
        "objective": (
            "binary" if dataset.task == "binary_direction" else "regression_l2"
        ),
        "seed": config.seed,
        "verbosity": -1,
    }
    booster = lightgbm.train(
        parameters,
        training,
        num_boost_round=config.num_boost_round,
    )
    serialized_model = booster.model_to_string(
        num_iteration=config.num_boost_round,
    )
    semantics = {
        "binary_direction": "uncalibrated_logit_score",
        "return_regression": "uncalibrated_return_score",
    }[dataset.task]
    return LightGBMShadowArtifact(
        model_version=config.model_version,
        task=dataset.task,
        feature_names=dataset.feature_names,
        serialized_model=serialized_model,
        training_dataset_sha256=dataset.training_sha256,
        training_sample_count=len(dataset.training_examples),
        config_sha256=_sha256(config.canonical_payload()),
        output_semantics=semantics,
        predictive_validation_input_eligible=(
            dataset.predictive_validation_input_eligible
        ),
    )


def predict_lightgbm_shadow(
    *,
    artifact: LightGBMShadowArtifact,
    dataset: FrozenShadowDataset,
    config: LightGBMShadowConfig,
    evaluated_at: datetime,
) -> ShadowPredictionReceipt:
    if not isinstance(artifact, LightGBMShadowArtifact):
        raise ShadowBaselineError("lightgbm_shadow_artifact_required")
    if artifact.training_dataset_sha256 != dataset.training_sha256:
        raise ShadowBaselineError("artifact_dataset_binding_mismatch")
    if artifact.task != dataset.task or artifact.feature_names != dataset.feature_names:
        raise ShadowBaselineError("artifact_feature_contract_mismatch")
    if artifact.config_sha256 != _sha256(config.canonical_payload()):
        raise ShadowBaselineError("artifact_config_binding_mismatch")
    evaluated = _aware(evaluated_at, field_name="evaluated_at")
    if any(vector.decision_time > evaluated for vector in dataset.prediction_vectors):
        raise ShadowBaselineError("prediction_evaluated_before_decision")
    numpy, _scipy, lightgbm = _modules(config)
    booster = lightgbm.Booster(model_str=artifact.serialized_model)
    features = numpy.asarray(
        [vector.feature_values for vector in dataset.prediction_vectors],
        dtype=numpy.float64,
    )
    predicted = booster.predict(features, raw_score=True)
    scores = tuple(
        ShadowScore(
            sample_id=vector.sample_id,
            vector_sha256=vector.vector_sha256,
            score=float(score),
        )
        for vector, score in zip(dataset.prediction_vectors, predicted)
    )
    return ShadowPredictionReceipt(
        market=dataset.market,
        task=dataset.task,
        evaluated_at=evaluated,
        dataset_sha256=dataset.dataset_sha256,
        artifact_sha256=artifact.artifact_sha256,
        output_semantics=artifact.output_semantics,
        scores=scores,
        predictive_validation_input_eligible=(
            artifact.predictive_validation_input_eligible
        ),
    )


__all__ = [
    "LightGBMShadowArtifact",
    "LightGBMShadowConfig",
    "PINNED_LIGHTGBM_VERSION",
    "PINNED_NUMPY_VERSION",
    "PINNED_SCIPY_VERSION",
    "fit_lightgbm_shadow",
    "predict_lightgbm_shadow",
]
