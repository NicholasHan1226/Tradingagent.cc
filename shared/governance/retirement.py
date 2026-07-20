"""Fail-closed helpers for code-level retirement of legacy runtimes.

Shell wrapper tombstones are not a security boundary: Python modules can be
invoked directly.  Retired entrypoints therefore call these helpers before
argument parsing, data-reader construction, network access, or file writes.
"""

from __future__ import annotations

import json
import sys
from typing import Any, NoReturn


RETIRED_RUNTIME_EXIT_CODE = 78
TRADINGDATAS_HANDOFF_REQUIRED = "tradingdatas_fixture_or_v1_port_required"


class RetiredRuntimeError(RuntimeError):
    """Raised when retired code is called without an explicit safe port."""


def require_explicit_data_port(reader: Any, *, context: str) -> Any:
    """Return an injected fixture/V1 port or fail before any implicit fallback."""

    if reader is None:
        raise RetiredRuntimeError(f"{context}:{TRADINGDATAS_HANDOFF_REQUIRED}")
    return reader


def retired_cli(component: str) -> int:
    """Emit a machine-readable tombstone without inspecting runtime config."""

    payload = {
        "component": str(component),
        "state": "retired",
        "reason": "legacy_runtime_retired",
        "replacement": "explicit_tradingdatas_catalog_query_port_after_fresh_handoff",
        "real_trading_enabled": False,
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=sys.stderr)
    return RETIRED_RUNTIME_EXIT_CODE


def raise_retired_runtime(component: str) -> NoReturn:
    """Fail a library-level legacy path that has no explicit safe replacement."""

    raise RetiredRuntimeError(f"{component}:legacy_runtime_retired")


__all__ = [
    "RETIRED_RUNTIME_EXIT_CODE",
    "TRADINGDATAS_HANDOFF_REQUIRED",
    "RetiredRuntimeError",
    "raise_retired_runtime",
    "require_explicit_data_port",
    "retired_cli",
]
