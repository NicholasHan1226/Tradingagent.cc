from __future__ import annotations

from unittest.mock import Mock

import pytest

from Crypto.registry import CRYPTO_C3_LIFECYCLE_BLOCKER, CryptoC3LifecycleRetired
from Crypto.rollback import RollbackManager


def test_rollback_manager_construction_is_retired() -> None:
    with pytest.raises(CryptoC3LifecycleRetired, match=CRYPTO_C3_LIFECYCLE_BLOCKER):
        RollbackManager(Mock())


def test_rollback_methods_are_unreachable_without_construction() -> None:
    manager = RollbackManager.__new__(RollbackManager)
    with pytest.raises(CryptoC3LifecycleRetired, match="authority=none"):
        manager.rollback_to_previous("BTCUSDT", "performance_degradation")
    with pytest.raises(CryptoC3LifecycleRetired, match="authority=none"):
        manager.get_rollback_candidates("BTCUSDT")
    with pytest.raises(CryptoC3LifecycleRetired, match="authority=none"):
        manager.get_rollback_history("BTCUSDT")
    with pytest.raises(CryptoC3LifecycleRetired, match="authority=none"):
        manager.validate_rollback("BTCUSDT")
