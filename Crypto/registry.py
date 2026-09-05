"""Fail-closed tombstone for retired C3 registry scaffolding.

C3 is not built. EVOLUTION_PROGRAM §5.2 requires C2 rolling evidence before
any Champion/Challenger registry. This module must not write
``shared/review/crypto``, must not register champions, and must not claim
promotion or capital authority.
"""

from __future__ import annotations

from typing import Any, NoReturn

from shared.governance.retirement import RetiredRuntimeError

CRYPTO_C3_LIFECYCLE_BLOCKER = "crypto_c3_registry_not_implemented"


class CryptoC3LifecycleRetired(RetiredRuntimeError):
    """Raised when a caller reaches retired C3 registry scaffolding."""


def _raise_c3_not_built() -> NoReturn:
    raise CryptoC3LifecycleRetired(
        f"{CRYPTO_C3_LIFECYCLE_BLOCKER}; C2 rolling evaluation precedes "
        "any Champion/Challenger registry; authority=none"
    )


class ChampionRecord:
    """Preserve the former type name while refusing construction."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        _raise_c3_not_built()


class CryptoChampionRegistry:
    """Preserve import compatibility while refusing every write or lookup."""

    def __init__(self, registry_path: Any = None) -> None:
        del registry_path
        _raise_c3_not_built()

    def register_champion(self, record: Any) -> NoReturn:
        del record
        _raise_c3_not_built()

    def get_by_id(self, champion_id: str) -> NoReturn:
        del champion_id
        _raise_c3_not_built()

    def get_active_champion(self, symbol: str, strategy_type: str) -> NoReturn:
        del symbol, strategy_type
        _raise_c3_not_built()

    def get_champion_history(self, symbol: str, strategy_type: str) -> NoReturn:
        del symbol, strategy_type
        _raise_c3_not_built()

    def demote_champion(self, champion_id: str, reason: str) -> NoReturn:
        del champion_id, reason
        _raise_c3_not_built()


__all__ = [
    "CRYPTO_C3_LIFECYCLE_BLOCKER",
    "ChampionRecord",
    "CryptoC3LifecycleRetired",
    "CryptoChampionRegistry",
]
