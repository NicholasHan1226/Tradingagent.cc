from __future__ import annotations

import pytest

from Crypto.demotion_policy import DemotionCriteria, DemotionPolicy
from Crypto.registry import CRYPTO_C3_LIFECYCLE_BLOCKER, CryptoC3LifecycleRetired


def test_demotion_policy_construction_is_retired() -> None:
    with pytest.raises(CryptoC3LifecycleRetired, match=CRYPTO_C3_LIFECYCLE_BLOCKER):
        DemotionPolicy()


def test_demotion_criteria_construction_is_retired() -> None:
    with pytest.raises(CryptoC3LifecycleRetired, match="authority=none"):
        DemotionCriteria()


def test_demotion_evaluate_is_unreachable_without_construction() -> None:
    policy = DemotionPolicy.__new__(DemotionPolicy)
    with pytest.raises(CryptoC3LifecycleRetired, match="authority=none"):
        policy.evaluate(
            "champ_001",
            {
                "consecutive_failures": 0,
                "drawdown_pct": 2.0,
                "sharpe_ratio": 1.5,
                "win_rate": 0.6,
            },
        )
