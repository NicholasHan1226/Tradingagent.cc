#!/usr/bin/env python3
"""Tombstone for the retired pre-V1 evidence endpoint checker.

The historical implementation queried provider-shaped GET routes such as
``/macro`` and ``/capital_flow``. TradingAgent now consumes TradingDatas only
through ``GET /v1/catalog`` and ``POST /v1/query``. Keeping an executable
compatibility checker would silently restore the retired runtime, so both the
CLI and library entry fail closed.
"""

from __future__ import annotations

from pathlib import Path
import sys
from typing import Any, NoReturn

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.governance.retirement import RetiredRuntimeError, retired_cli


def run_contract_check(*args: Any, **kwargs: Any) -> NoReturn:
    """Reject every use of the provider-shaped compatibility contract."""

    del args, kwargs
    raise RetiredRuntimeError(
        "sharedsignals_evidence_contract:legacy_runtime_retired"
    )


def main(argv: list[str] | None = None) -> int:
    del argv
    return retired_cli("shared.runtime_test.sharedsignals_evidence_contract")


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main", "run_contract_check"]
