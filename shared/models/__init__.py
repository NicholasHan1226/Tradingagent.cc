"""Offline model governance contracts; no broker or production authority."""

from .shadow_baselines import (
    BaselineModelConfig,
    FrozenFeatureVector,
    FrozenShadowDataset,
    FrozenTrainingExample,
    ShadowBaselineArtifact,
    ShadowBaselineError,
    ShadowPredictionReceipt,
    ShadowScore,
    fit_shadow_baseline,
    predict_shadow_baseline,
)
from .shadow_lightgbm import (
    LightGBMShadowArtifact,
    LightGBMShadowConfig,
    fit_lightgbm_shadow,
    predict_lightgbm_shadow,
)

__all__ = [
    "BaselineModelConfig",
    "FrozenFeatureVector",
    "FrozenShadowDataset",
    "FrozenTrainingExample",
    "LightGBMShadowArtifact",
    "LightGBMShadowConfig",
    "ShadowBaselineArtifact",
    "ShadowBaselineError",
    "ShadowPredictionReceipt",
    "ShadowScore",
    "fit_shadow_baseline",
    "fit_lightgbm_shadow",
    "predict_lightgbm_shadow",
    "predict_shadow_baseline",
]
