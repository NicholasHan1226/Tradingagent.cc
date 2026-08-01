from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from shared.models.shadow_lightgbm import (
    LightGBMShadowConfig,
    PINNED_LIGHTGBM_VERSION,
    PINNED_NUMPY_VERSION,
    PINNED_SCIPY_VERSION,
    fit_lightgbm_shadow,
    predict_lightgbm_shadow,
)

from test_shadow_model_baselines import BASE, _dataset


try:
    import lightgbm
    import numpy
    import scipy
except (ImportError, OSError) as exc:
    pytest.skip(
        f"pinned optional LightGBM runtime unavailable: {type(exc).__name__}",
        allow_module_level=True,
    )


def test_real_pinned_lightgbm_cpu_backend_is_replayable_and_authority_free() -> None:
    assert lightgbm.__version__ == PINNED_LIGHTGBM_VERSION
    assert numpy.__version__ == PINNED_NUMPY_VERSION
    assert scipy.__version__ == PINNED_SCIPY_VERSION
    dataset = _dataset()
    repeated = tuple(dataset.training_examples[index % 12] for index in range(48))
    unique = tuple(
        replace(item, vector=replace(item.vector, sample_id=f"expanded-{index:03d}"))
        for index, item in enumerate(repeated)
    )
    expanded = replace(dataset, training_examples=unique)
    config = LightGBMShadowConfig()

    first_artifact = fit_lightgbm_shadow(expanded, config)
    second_artifact = fit_lightgbm_shadow(expanded, config)
    assert first_artifact == second_artifact

    first_receipt = predict_lightgbm_shadow(
        artifact=first_artifact,
        dataset=expanded,
        config=config,
        evaluated_at=BASE + timedelta(days=30),
    )
    second_receipt = predict_lightgbm_shadow(
        artifact=second_artifact,
        dataset=expanded,
        config=config,
        evaluated_at=BASE + timedelta(days=30),
    )
    assert first_receipt == second_receipt
    assert first_receipt.output_semantics == "uncalibrated_logit_score"
    assert first_receipt.authority == "none"
    assert first_receipt.execution_authority is False
    assert first_receipt.capital_authority is False
    assert first_receipt.risk_expansion_allowed is False
    assert first_receipt.automatic_promotion_enabled is False
    assert first_receipt.real_trading_enabled is False
