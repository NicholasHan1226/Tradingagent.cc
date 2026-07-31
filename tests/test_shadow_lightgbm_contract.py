from __future__ import annotations

from dataclasses import replace

import pytest

import shared.models.shadow_lightgbm as shadow_lightgbm
from shared.models.shadow_baselines import ShadowBaselineError
from shared.models.shadow_lightgbm import (
    LightGBMShadowConfig,
    PINNED_LIGHTGBM_VERSION,
    PINNED_NUMPY_VERSION,
    fit_lightgbm_shadow,
)

from test_shadow_model_baselines import _dataset


def test_lightgbm_profile_is_small_deterministic_and_cpu_bounded() -> None:
    config = LightGBMShadowConfig()
    assert config.num_threads == 2
    assert config.max_depth == 3
    assert config.num_leaves == 7
    assert config.minimum_training_samples == 40
    assert config.expected_lightgbm_version == PINNED_LIGHTGBM_VERSION
    assert config.expected_numpy_version == PINNED_NUMPY_VERSION


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"num_threads": 3}, "num_threads_invalid"),
        ({"max_depth": 7}, "max_depth_invalid"),
        ({"num_leaves": 16}, "num_leaves_exceeds_depth_capacity"),
        ({"minimum_training_samples": 39}, "minimum_training_samples_invalid"),
        (
            {"expected_lightgbm_version": "latest"},
            "lightgbm_expected_version_invalid",
        ),
    ],
)
def test_lightgbm_profile_rejects_resource_or_version_drift(
    changes: dict[str, object],
    reason: str,
) -> None:
    with pytest.raises(ShadowBaselineError, match=reason):
        LightGBMShadowConfig(**changes)


def test_lightgbm_dependency_is_lazy_and_missing_environment_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset = _dataset()
    repeated = tuple(dataset.training_examples[index % 12] for index in range(48))
    unique = tuple(
        replace(item, vector=replace(item.vector, sample_id=f"expanded-{index:03d}"))
        for index, item in enumerate(repeated)
    )
    expanded = replace(dataset, training_examples=unique)
    monkeypatch.setattr(
        shadow_lightgbm,
        "_load_module",
        lambda name: (_ for _ in ()).throw(
            ShadowBaselineError(f"{name}_dependency_unavailable")
        ),
    )
    with pytest.raises(
        ShadowBaselineError,
        match="(numpy|lightgbm)_dependency_unavailable",
    ):
        fit_lightgbm_shadow(expanded, LightGBMShadowConfig())
