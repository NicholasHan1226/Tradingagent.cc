"""Deterministic, dependency-free shadow baselines for the learning plane.

These models are deliberately unable to size positions, change risk, emit
orders, or promote themselves.  They provide a small reproducible control for
future LightGBM and time-series foundation-model challengers.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Tuple


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MARKETS = frozenset({"ashare", "cnfutures", "crypto"})
_TASKS = frozenset({"binary_direction", "return_regression"})
_MODEL_FAMILIES = frozenset({"elastic_net_logistic", "ridge_regression"})


class ShadowBaselineError(ValueError):
    """Raised when shadow training or prediction cannot be trusted."""


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise ShadowBaselineError("shadow_payload_not_canonical") from exc


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _require_text(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ShadowBaselineError(f"{field_name}_invalid")
    return value


def _require_sha256(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ShadowBaselineError(f"{field_name}_invalid")
    return value


def _aware(value: object, *, field_name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ShadowBaselineError(f"{field_name}_timezone_required")
    return value.astimezone(timezone.utc)


def _finite(value: object, *, field_name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise ShadowBaselineError(f"{field_name}_invalid")
    return float(value)


def _positive(value: object, *, field_name: str, allow_zero: bool = False) -> float:
    normalized = _finite(value, field_name=field_name)
    if normalized < 0.0 or (not allow_zero and normalized == 0.0):
        raise ShadowBaselineError(f"{field_name}_invalid")
    return normalized


@dataclass(frozen=True)
class FrozenFeatureVector:
    """One PIT-bound numerical feature vector without trading authority."""

    sample_id: str
    event_time: datetime
    available_at: datetime
    decision_time: datetime
    source_receipt_sha256: str
    features: Tuple[Tuple[str, float], ...]
    vector_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        _require_text(self.sample_id, field_name="sample_id")
        event_time = _aware(self.event_time, field_name="event_time")
        available_at = _aware(self.available_at, field_name="available_at")
        decision_time = _aware(self.decision_time, field_name="decision_time")
        if not event_time <= available_at <= decision_time:
            raise ShadowBaselineError("feature_time_order_invalid")
        object.__setattr__(self, "event_time", event_time)
        object.__setattr__(self, "available_at", available_at)
        object.__setattr__(self, "decision_time", decision_time)
        _require_sha256(
            self.source_receipt_sha256,
            field_name="source_receipt_sha256",
        )
        if not isinstance(self.features, tuple) or not self.features:
            raise ShadowBaselineError("features_invalid")
        normalized = []
        previous = None
        for item in self.features:
            if not isinstance(item, tuple) or len(item) != 2:
                raise ShadowBaselineError("features_invalid")
            name = _require_text(item[0], field_name="feature_name")
            if previous is not None and name <= previous:
                raise ShadowBaselineError("feature_names_not_strictly_sorted")
            normalized.append((name, _finite(item[1], field_name=f"feature:{name}")))
            previous = name
        object.__setattr__(self, "features", tuple(normalized))
        object.__setattr__(self, "vector_sha256", _sha256(self.canonical_payload()))

    @property
    def feature_names(self) -> Tuple[str, ...]:
        return tuple(name for name, _value in self.features)

    @property
    def feature_values(self) -> Tuple[float, ...]:
        return tuple(value for _name, value in self.features)

    def canonical_payload(self) -> dict[str, object]:
        return {
            "available_at": self.available_at.isoformat(),
            "decision_time": self.decision_time.isoformat(),
            "event_time": self.event_time.isoformat(),
            "features": list(self.features),
            "sample_id": self.sample_id,
            "source_receipt_sha256": self.source_receipt_sha256,
        }


@dataclass(frozen=True)
class FrozenTrainingExample:
    vector: FrozenFeatureVector
    label: float
    label_available_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.vector, FrozenFeatureVector):
            raise ShadowBaselineError("training_vector_invalid")
        object.__setattr__(self, "label", _finite(self.label, field_name="label"))
        label_available_at = _aware(
            self.label_available_at,
            field_name="label_available_at",
        )
        if label_available_at < self.vector.decision_time:
            raise ShadowBaselineError("label_available_before_decision")
        object.__setattr__(self, "label_available_at", label_available_at)

    def canonical_payload(self) -> dict[str, object]:
        return {
            "label": self.label,
            "label_available_at": self.label_available_at.isoformat(),
            "vector_sha256": self.vector.vector_sha256,
        }


@dataclass(frozen=True)
class FrozenShadowDataset:
    """Content-addressed training and inference inputs for one shadow run."""

    market: str
    task: str
    feature_contract_version: str
    label_policy_id: str
    training_cutoff: datetime
    training_examples: Tuple[FrozenTrainingExample, ...]
    prediction_vectors: Tuple[FrozenFeatureVector, ...]
    historical_pit_verified: bool
    revision_history_verified: bool
    schema_version: str = "tradingagent.shadow_dataset.v1"
    training_sha256: str = field(init=False)
    dataset_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if self.market not in _MARKETS:
            raise ShadowBaselineError("market_invalid")
        if self.task not in _TASKS:
            raise ShadowBaselineError("task_invalid")
        _require_text(
            self.feature_contract_version,
            field_name="feature_contract_version",
        )
        _require_text(self.label_policy_id, field_name="label_policy_id")
        cutoff = _aware(self.training_cutoff, field_name="training_cutoff")
        object.__setattr__(self, "training_cutoff", cutoff)
        if not isinstance(self.training_examples, tuple) or not self.training_examples:
            raise ShadowBaselineError("training_examples_invalid")
        if (
            not isinstance(self.prediction_vectors, tuple)
            or not self.prediction_vectors
        ):
            raise ShadowBaselineError("prediction_vectors_invalid")
        if any(
            not isinstance(item, FrozenTrainingExample)
            for item in self.training_examples
        ):
            raise ShadowBaselineError("training_examples_invalid")
        if any(
            not isinstance(item, FrozenFeatureVector)
            for item in self.prediction_vectors
        ):
            raise ShadowBaselineError("prediction_vectors_invalid")
        training_ids = tuple(item.vector.sample_id for item in self.training_examples)
        prediction_ids = tuple(item.sample_id for item in self.prediction_vectors)
        if len(training_ids) != len(set(training_ids)):
            raise ShadowBaselineError("training_sample_id_duplicate")
        if len(prediction_ids) != len(set(prediction_ids)):
            raise ShadowBaselineError("prediction_sample_id_duplicate")
        expected_features = self.training_examples[0].vector.feature_names
        vectors: Iterable[FrozenFeatureVector] = (
            item.vector for item in self.training_examples
        )
        for vector in tuple(vectors) + self.prediction_vectors:
            if vector.feature_names != expected_features:
                raise ShadowBaselineError("feature_contract_mismatch")
        for item in self.training_examples:
            if item.label_available_at > cutoff:
                raise ShadowBaselineError("training_label_after_cutoff")
            if item.vector.available_at > cutoff:
                raise ShadowBaselineError("training_feature_after_cutoff")
            if self.task == "binary_direction" and item.label not in {0.0, 1.0}:
                raise ShadowBaselineError("binary_label_invalid")
        if self.task == "binary_direction" and {
            item.label for item in self.training_examples
        } != {0.0, 1.0}:
            raise ShadowBaselineError("binary_label_classes_incomplete")
        if any(vector.decision_time <= cutoff for vector in self.prediction_vectors):
            raise ShadowBaselineError("prediction_not_strictly_out_of_sample")
        if self.schema_version != "tradingagent.shadow_dataset.v1":
            raise ShadowBaselineError("shadow_dataset_schema_invalid")
        if not isinstance(self.historical_pit_verified, bool) or not isinstance(
            self.revision_history_verified,
            bool,
        ):
            raise ShadowBaselineError("dataset_verification_flags_invalid")
        object.__setattr__(
            self,
            "training_sha256",
            _sha256(self.training_canonical_payload()),
        )
        object.__setattr__(self, "dataset_sha256", _sha256(self.canonical_payload()))

    @property
    def feature_names(self) -> Tuple[str, ...]:
        return self.training_examples[0].vector.feature_names

    @property
    def predictive_validation_input_eligible(self) -> bool:
        return self.historical_pit_verified and self.revision_history_verified

    def training_canonical_payload(self) -> dict[str, object]:
        return {
            "feature_contract_version": self.feature_contract_version,
            "historical_pit_verified": self.historical_pit_verified,
            "label_policy_id": self.label_policy_id,
            "market": self.market,
            "revision_history_verified": self.revision_history_verified,
            "schema_version": self.schema_version,
            "task": self.task,
            "training_cutoff": self.training_cutoff.isoformat(),
            "training_examples": [
                item.canonical_payload() for item in self.training_examples
            ],
        }

    def canonical_payload(self) -> dict[str, object]:
        return {
            "prediction_vectors": [
                item.vector_sha256 for item in self.prediction_vectors
            ],
            "training": self.training_canonical_payload(),
        }


@dataclass(frozen=True)
class BaselineModelConfig:
    model_family: str
    model_version: str
    minimum_training_samples: int = 8
    l1_penalty: float = 0.0
    l2_penalty: float = 0.1
    learning_rate: float = 0.1
    iterations: int = 400
    seed: int = 1729

    def __post_init__(self) -> None:
        if self.model_family not in _MODEL_FAMILIES:
            raise ShadowBaselineError("model_family_invalid")
        _require_text(self.model_version, field_name="model_version")
        if (
            isinstance(self.minimum_training_samples, bool)
            or not isinstance(self.minimum_training_samples, int)
            or self.minimum_training_samples < 8
        ):
            raise ShadowBaselineError("minimum_training_samples_invalid")
        _positive(self.l1_penalty, field_name="l1_penalty", allow_zero=True)
        _positive(self.l2_penalty, field_name="l2_penalty", allow_zero=True)
        _positive(self.learning_rate, field_name="learning_rate")
        if (
            isinstance(self.iterations, bool)
            or not isinstance(self.iterations, int)
            or not 10 <= self.iterations <= 10_000
        ):
            raise ShadowBaselineError("iterations_invalid")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int):
            raise ShadowBaselineError("seed_invalid")
        if self.model_family == "ridge_regression" and self.l1_penalty != 0.0:
            raise ShadowBaselineError("ridge_l1_penalty_forbidden")

    def canonical_payload(self) -> dict[str, object]:
        return {
            "iterations": self.iterations,
            "l1_penalty": self.l1_penalty,
            "l2_penalty": self.l2_penalty,
            "learning_rate": self.learning_rate,
            "minimum_training_samples": self.minimum_training_samples,
            "model_family": self.model_family,
            "model_version": self.model_version,
            "seed": self.seed,
        }


@dataclass(frozen=True)
class ShadowBaselineArtifact:
    model_family: str
    model_version: str
    task: str
    feature_names: Tuple[str, ...]
    feature_means: Tuple[float, ...]
    feature_scales: Tuple[float, ...]
    coefficients: Tuple[float, ...]
    intercept: float
    training_dataset_sha256: str
    training_sample_count: int
    config_sha256: str
    output_semantics: str
    predictive_validation_input_eligible: bool
    schema_version: str = "tradingagent.shadow_baseline_artifact.v1"
    shadow_only: bool = True
    artifact_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if self.model_family not in _MODEL_FAMILIES:
            raise ShadowBaselineError("artifact_model_family_invalid")
        _require_text(self.model_version, field_name="model_version")
        if self.task not in _TASKS:
            raise ShadowBaselineError("artifact_task_invalid")
        length = len(self.feature_names)
        if not length or any(
            len(values) != length
            for values in (
                self.feature_means,
                self.feature_scales,
                self.coefficients,
            )
        ):
            raise ShadowBaselineError("artifact_dimension_mismatch")
        for collection_name, values in (
            ("feature_means", self.feature_means),
            ("feature_scales", self.feature_scales),
            ("coefficients", self.coefficients),
        ):
            for value in values:
                _finite(value, field_name=collection_name)
        if any(value <= 0.0 for value in self.feature_scales):
            raise ShadowBaselineError("artifact_feature_scale_invalid")
        _finite(self.intercept, field_name="intercept")
        _require_sha256(
            self.training_dataset_sha256,
            field_name="training_dataset_sha256",
        )
        _require_sha256(self.config_sha256, field_name="config_sha256")
        if (
            isinstance(self.training_sample_count, bool)
            or not isinstance(self.training_sample_count, int)
            or self.training_sample_count < 1
        ):
            raise ShadowBaselineError("training_sample_count_invalid")
        expected_semantics = {
            "binary_direction": "uncalibrated_logit_score",
            "return_regression": "uncalibrated_return_score",
        }[self.task]
        if self.output_semantics != expected_semantics:
            raise ShadowBaselineError("output_semantics_invalid")
        if (
            self.schema_version != "tradingagent.shadow_baseline_artifact.v1"
            or self.shadow_only is not True
            or not isinstance(self.predictive_validation_input_eligible, bool)
        ):
            raise ShadowBaselineError("artifact_shadow_boundary_invalid")
        object.__setattr__(self, "artifact_sha256", _sha256(self.canonical_payload()))

    def canonical_payload(self) -> dict[str, object]:
        return {
            "coefficients": list(self.coefficients),
            "config_sha256": self.config_sha256,
            "feature_means": list(self.feature_means),
            "feature_names": list(self.feature_names),
            "feature_scales": list(self.feature_scales),
            "intercept": self.intercept,
            "model_family": self.model_family,
            "model_version": self.model_version,
            "output_semantics": self.output_semantics,
            "predictive_validation_input_eligible": (
                self.predictive_validation_input_eligible
            ),
            "schema_version": self.schema_version,
            "shadow_only": True,
            "task": self.task,
            "training_dataset_sha256": self.training_dataset_sha256,
            "training_sample_count": self.training_sample_count,
        }


@dataclass(frozen=True)
class ShadowScore:
    sample_id: str
    vector_sha256: str
    score: float

    def __post_init__(self) -> None:
        _require_text(self.sample_id, field_name="score_sample_id")
        _require_sha256(self.vector_sha256, field_name="vector_sha256")
        object.__setattr__(self, "score", _finite(self.score, field_name="score"))

    def canonical_payload(self) -> dict[str, object]:
        return {
            "sample_id": self.sample_id,
            "score": self.score,
            "vector_sha256": self.vector_sha256,
        }


@dataclass(frozen=True)
class ShadowPredictionReceipt:
    market: str
    task: str
    evaluated_at: datetime
    dataset_sha256: str
    artifact_sha256: str
    output_semantics: str
    scores: Tuple[ShadowScore, ...]
    predictive_validation_input_eligible: bool
    schema_version: str = "tradingagent.shadow_prediction_receipt.v1"
    authority: str = "none"
    shadow_only: bool = True
    execution_eligible: bool = False
    execution_authority: bool = False
    capital_authority: bool = False
    risk_expansion_allowed: bool = False
    automatic_promotion_enabled: bool = False
    real_trading_enabled: bool = False
    receipt_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if self.market not in _MARKETS or self.task not in _TASKS:
            raise ShadowBaselineError("prediction_scope_invalid")
        object.__setattr__(
            self,
            "evaluated_at",
            _aware(self.evaluated_at, field_name="evaluated_at"),
        )
        _require_sha256(self.dataset_sha256, field_name="dataset_sha256")
        _require_sha256(self.artifact_sha256, field_name="artifact_sha256")
        if not isinstance(self.scores, tuple) or not self.scores:
            raise ShadowBaselineError("prediction_scores_invalid")
        if any(not isinstance(item, ShadowScore) for item in self.scores):
            raise ShadowBaselineError("prediction_scores_invalid")
        sample_ids = tuple(item.sample_id for item in self.scores)
        if len(sample_ids) != len(set(sample_ids)):
            raise ShadowBaselineError("prediction_score_sample_duplicate")
        if (
            self.schema_version != "tradingagent.shadow_prediction_receipt.v1"
            or self.authority != "none"
            or self.shadow_only is not True
            or self.execution_eligible is not False
            or self.execution_authority is not False
            or self.capital_authority is not False
            or self.risk_expansion_allowed is not False
            or self.automatic_promotion_enabled is not False
            or self.real_trading_enabled is not False
            or not isinstance(self.predictive_validation_input_eligible, bool)
        ):
            raise ShadowBaselineError("prediction_shadow_boundary_invalid")
        object.__setattr__(self, "receipt_sha256", _sha256(self.canonical_payload()))

    def canonical_payload(self) -> dict[str, object]:
        return {
            "artifact_sha256": self.artifact_sha256,
            "authority": "none",
            "automatic_promotion_enabled": False,
            "capital_authority": False,
            "dataset_sha256": self.dataset_sha256,
            "evaluated_at": self.evaluated_at.isoformat(),
            "execution_authority": False,
            "execution_eligible": False,
            "market": self.market,
            "output_semantics": self.output_semantics,
            "predictive_validation_input_eligible": (
                self.predictive_validation_input_eligible
            ),
            "real_trading_enabled": False,
            "risk_expansion_allowed": False,
            "schema_version": self.schema_version,
            "scores": [item.canonical_payload() for item in self.scores],
            "shadow_only": True,
            "task": self.task,
        }


def _feature_statistics(
    rows: Tuple[Tuple[float, ...], ...],
) -> tuple[Tuple[float, ...], Tuple[float, ...]]:
    columns = tuple(zip(*rows))
    means = tuple(sum(column) / len(column) for column in columns)
    scales = []
    for column, mean in zip(columns, means):
        variance = sum((value - mean) ** 2 for value in column) / len(column)
        scale = math.sqrt(variance)
        scales.append(scale if scale > 1e-12 else 1.0)
    return means, tuple(scales)


def _standardize(
    rows: Tuple[Tuple[float, ...], ...],
    means: Tuple[float, ...],
    scales: Tuple[float, ...],
) -> Tuple[Tuple[float, ...], ...]:
    return tuple(
        tuple((value - mean) / scale for value, mean, scale in zip(row, means, scales))
        for row in rows
    )


def _solve_linear_system(
    matrix: list[list[float]],
    target: list[float],
) -> Tuple[float, ...]:
    size = len(target)
    augmented = [list(row) + [target[index]] for index, row in enumerate(matrix)]
    for pivot in range(size):
        best = max(range(pivot, size), key=lambda row: abs(augmented[row][pivot]))
        if abs(augmented[best][pivot]) <= 1e-12:
            raise ShadowBaselineError("baseline_matrix_singular")
        augmented[pivot], augmented[best] = augmented[best], augmented[pivot]
        divisor = augmented[pivot][pivot]
        augmented[pivot] = [value / divisor for value in augmented[pivot]]
        for row in range(size):
            if row == pivot:
                continue
            factor = augmented[row][pivot]
            augmented[row] = [
                current - factor * reference
                for current, reference in zip(augmented[row], augmented[pivot])
            ]
    result = tuple(augmented[index][-1] for index in range(size))
    if any(not math.isfinite(value) for value in result):
        raise ShadowBaselineError("baseline_solution_invalid")
    return result


def _fit_ridge(
    rows: Tuple[Tuple[float, ...], ...],
    labels: Tuple[float, ...],
    *,
    l2_penalty: float,
) -> tuple[float, Tuple[float, ...]]:
    design = tuple((1.0,) + row for row in rows)
    width = len(design[0])
    gram = [[0.0 for _ in range(width)] for _ in range(width)]
    rhs = [0.0 for _ in range(width)]
    for row, label in zip(design, labels):
        for left in range(width):
            rhs[left] += row[left] * label
            for right in range(width):
                gram[left][right] += row[left] * row[right]
    for index in range(1, width):
        gram[index][index] += l2_penalty
    solution = _solve_linear_system(gram, rhs)
    return solution[0], solution[1:]


def _sigmoid(value: float) -> float:
    if value >= 0.0:
        exponential = math.exp(-value)
        return 1.0 / (1.0 + exponential)
    exponential = math.exp(value)
    return exponential / (1.0 + exponential)


def _soft_threshold(value: float, threshold: float) -> float:
    if value > threshold:
        return value - threshold
    if value < -threshold:
        return value + threshold
    return 0.0


def _fit_elastic_net_logistic(
    rows: Tuple[Tuple[float, ...], ...],
    labels: Tuple[float, ...],
    *,
    l1_penalty: float,
    l2_penalty: float,
    learning_rate: float,
    iterations: int,
) -> tuple[float, Tuple[float, ...]]:
    width = len(rows[0])
    intercept = 0.0
    coefficients = [0.0 for _ in range(width)]
    count = float(len(rows))
    for _iteration in range(iterations):
        intercept_gradient = 0.0
        gradients = [0.0 for _ in range(width)]
        for row, label in zip(rows, labels):
            raw = intercept + sum(
                coefficient * value for coefficient, value in zip(coefficients, row)
            )
            residual = _sigmoid(raw) - label
            intercept_gradient += residual
            for index, value in enumerate(row):
                gradients[index] += residual * value
        intercept -= learning_rate * intercept_gradient / count
        for index in range(width):
            gradient = gradients[index] / count + l2_penalty * coefficients[index]
            updated = coefficients[index] - learning_rate * gradient
            coefficients[index] = _soft_threshold(
                updated,
                learning_rate * l1_penalty,
            )
    if not math.isfinite(intercept) or any(
        not math.isfinite(value) for value in coefficients
    ):
        raise ShadowBaselineError("logistic_solution_invalid")
    return intercept, tuple(coefficients)


def fit_shadow_baseline(
    dataset: FrozenShadowDataset,
    config: BaselineModelConfig,
) -> ShadowBaselineArtifact:
    """Fit a deterministic baseline without touching capital or runtime state."""

    if not isinstance(dataset, FrozenShadowDataset):
        raise ShadowBaselineError("shadow_dataset_required")
    if not isinstance(config, BaselineModelConfig):
        raise ShadowBaselineError("baseline_model_config_required")
    if len(dataset.training_examples) < config.minimum_training_samples:
        raise ShadowBaselineError("training_sample_count_insufficient")
    if (
        config.model_family == "ridge_regression"
        and dataset.task != "return_regression"
    ):
        raise ShadowBaselineError("ridge_task_mismatch")
    if (
        config.model_family == "elastic_net_logistic"
        and dataset.task != "binary_direction"
    ):
        raise ShadowBaselineError("logistic_task_mismatch")
    raw_rows = tuple(item.vector.feature_values for item in dataset.training_examples)
    labels = tuple(item.label for item in dataset.training_examples)
    means, scales = _feature_statistics(raw_rows)
    rows = _standardize(raw_rows, means, scales)
    if config.model_family == "ridge_regression":
        intercept, coefficients = _fit_ridge(
            rows,
            labels,
            l2_penalty=config.l2_penalty,
        )
    else:
        intercept, coefficients = _fit_elastic_net_logistic(
            rows,
            labels,
            l1_penalty=config.l1_penalty,
            l2_penalty=config.l2_penalty,
            learning_rate=config.learning_rate,
            iterations=config.iterations,
        )
    semantics = {
        "binary_direction": "uncalibrated_logit_score",
        "return_regression": "uncalibrated_return_score",
    }[dataset.task]
    return ShadowBaselineArtifact(
        model_family=config.model_family,
        model_version=config.model_version,
        task=dataset.task,
        feature_names=dataset.feature_names,
        feature_means=means,
        feature_scales=scales,
        coefficients=coefficients,
        intercept=intercept,
        training_dataset_sha256=dataset.training_sha256,
        training_sample_count=len(dataset.training_examples),
        config_sha256=_sha256(config.canonical_payload()),
        output_semantics=semantics,
        predictive_validation_input_eligible=(
            dataset.predictive_validation_input_eligible
        ),
    )


def predict_shadow_baseline(
    *,
    artifact: ShadowBaselineArtifact,
    dataset: FrozenShadowDataset,
    evaluated_at: datetime,
) -> ShadowPredictionReceipt:
    """Score frozen vectors and return an authority-free immutable receipt."""

    if not isinstance(artifact, ShadowBaselineArtifact):
        raise ShadowBaselineError("shadow_baseline_artifact_required")
    if not isinstance(dataset, FrozenShadowDataset):
        raise ShadowBaselineError("shadow_dataset_required")
    if artifact.training_dataset_sha256 != dataset.training_sha256:
        raise ShadowBaselineError("artifact_dataset_binding_mismatch")
    if artifact.task != dataset.task or artifact.feature_names != dataset.feature_names:
        raise ShadowBaselineError("artifact_feature_contract_mismatch")
    evaluated = _aware(evaluated_at, field_name="evaluated_at")
    if any(vector.decision_time > evaluated for vector in dataset.prediction_vectors):
        raise ShadowBaselineError("prediction_evaluated_before_decision")
    rows = _standardize(
        tuple(vector.feature_values for vector in dataset.prediction_vectors),
        artifact.feature_means,
        artifact.feature_scales,
    )
    scores = tuple(
        ShadowScore(
            sample_id=vector.sample_id,
            vector_sha256=vector.vector_sha256,
            score=artifact.intercept
            + sum(
                coefficient * value
                for coefficient, value in zip(artifact.coefficients, row)
            ),
        )
        for vector, row in zip(dataset.prediction_vectors, rows)
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
    "BaselineModelConfig",
    "FrozenFeatureVector",
    "FrozenShadowDataset",
    "FrozenTrainingExample",
    "ShadowBaselineArtifact",
    "ShadowBaselineError",
    "ShadowPredictionReceipt",
    "ShadowScore",
    "fit_shadow_baseline",
    "predict_shadow_baseline",
]
