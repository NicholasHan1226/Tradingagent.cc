"""Fail-closed tombstone for retired C3 demotion scaffolding.

C3 is not built. This module must not evaluate or authorize demotion, and
must not claim promotion or capital authority.
"""

from __future__ import annotations

from typing import Any, NoReturn

from Crypto.registry import CRYPTO_C3_LIFECYCLE_BLOCKER, CryptoC3LifecycleRetired


def _raise_c3_not_built() -> NoReturn:
    raise CryptoC3LifecycleRetired(
        f"{CRYPTO_C3_LIFECYCLE_BLOCKER}; C2 rolling evaluation precedes "
        "any Champion/Challenger demotion; authority=none"
    )


class DemotionCriteria:
    """Preserve the former type name while refusing construction."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        _raise_c3_not_built()


class DemotionPolicy:
    """Preserve the former symbol while refusing construction and evaluation."""

    def __init__(self, criteria: Any = None) -> None:
        del criteria
        _raise_c3_not_built()

    def evaluate(self, champion_id: str, performance: Any) -> NoReturn:
        del champion_id, performance
        _raise_c3_not_built()


__all__ = ["DemotionCriteria", "DemotionPolicy"]
