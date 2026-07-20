#!/usr/bin/env python3
"""Fail-closed tombstone for the retired Crypto shadow workflow."""

from __future__ import annotations

from pathlib import Path
from typing import Any, NoReturn

from Crypto.capital_policy import CRYPTO_CAPITAL_AUTHORITY_ID
from Crypto.common import CryptoConfig
from Crypto.sim_executor import CryptoLegacyExecutionRetired


def _raise_retired() -> NoReturn:
    raise CryptoLegacyExecutionRetired(
        "CryptoWorkflow:legacy_runtime_retired; use the capital-backed fixture runtime for "
        f"{CRYPTO_CAPITAL_AUTHORITY_ID}"
    )


class CryptoWorkflow:
    """Preserve import compatibility while refusing the former writable path."""

    def __init__(
        self,
        config: CryptoConfig | None = None,
        *,
        reader: Any | None = None,
        signals_dir: Path | str | None = None,
    ) -> None:
        del config, reader, signals_dir
        _raise_retired()

    def run_crypto_shadow_cycle(self, as_of: str) -> NoReturn:
        del as_of
        _raise_retired()


def run_crypto_shadow_cycle(as_of: str, *, reader: Any | None = None) -> NoReturn:
    """Reject the former module-level workflow entrypoint."""

    del as_of, reader
    _raise_retired()


__all__ = ["CryptoWorkflow", "run_crypto_shadow_cycle"]
