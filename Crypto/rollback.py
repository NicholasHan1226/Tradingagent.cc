"""Fail-closed tombstone for retired C3 rollback scaffolding.

C3 is not built. This module must not reactivate, retire, or rewrite any
champion record, and must not claim promotion or capital authority.
"""

from __future__ import annotations

from typing import Any, NoReturn

from Crypto.registry import CRYPTO_C3_LIFECYCLE_BLOCKER, CryptoC3LifecycleRetired


def _raise_c3_not_built() -> NoReturn:
    raise CryptoC3LifecycleRetired(
        f"{CRYPTO_C3_LIFECYCLE_BLOCKER}; C2 rolling evaluation precedes "
        "any Champion/Challenger rollback; authority=none"
    )


class RollbackManager:
    """Preserve the former symbol while refusing construction and writes."""

    def __init__(self, registry: Any = None) -> None:
        del registry
        _raise_c3_not_built()

    def rollback_to_previous(self, symbol: str, reason: str) -> NoReturn:
        del symbol, reason
        _raise_c3_not_built()

    def get_rollback_candidates(self, symbol: str) -> NoReturn:
        del symbol
        _raise_c3_not_built()

    def get_rollback_history(self, symbol: str) -> NoReturn:
        del symbol
        _raise_c3_not_built()

    def validate_rollback(self, symbol: str) -> NoReturn:
        del symbol
        _raise_c3_not_built()


__all__ = ["RollbackManager"]
