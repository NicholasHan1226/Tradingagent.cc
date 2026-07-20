#!/usr/bin/env python3
"""Fail-closed tombstone for the retired Crypto direct simulator.

The immutable paper-broker wire contract remains available for historical
receipt readers.  New execution must enter through the capital-backed Crypto
fixture runtime; this module intentionally performs no registration and can
never manufacture a fill.
"""

from __future__ import annotations

from typing import Any, NoReturn

from Crypto.capital_policy import CRYPTO_CAPITAL_AUTHORITY_ID
from shared.governance.retirement import RetiredRuntimeError
from shared.markets.safety import reject_real_execution_payload


PAPER_BROKER_CONTRACT = "tradingagent.crypto.paper_broker.v1"


class CryptoLegacyExecutionRetired(RetiredRuntimeError):
    """Raised when a caller reaches a permanently retired Crypto path."""


def _raise_retired() -> NoReturn:
    raise CryptoLegacyExecutionRetired(
        "CryptoDirectSimulation:legacy_runtime_retired; execution requires the "
        f"{CRYPTO_CAPITAL_AUTHORITY_ID} append-only ledger runtime"
    )


def crypto_sim_execute(
    order: dict[str, Any],
    account: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
) -> NoReturn:
    """Reject the former direct fill API without registering an executor."""

    reject_real_execution_payload(order, context="crypto_sim_execute.order")
    reject_real_execution_payload(account or {}, context="crypto_sim_execute.account")
    reject_real_execution_payload(config or {}, context="crypto_sim_execute.config")
    _raise_retired()


__all__ = [
    "PAPER_BROKER_CONTRACT",
    "CryptoLegacyExecutionRetired",
    "crypto_sim_execute",
]
