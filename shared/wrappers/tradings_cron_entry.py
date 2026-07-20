#!/usr/bin/env python3
"""Fail-closed tombstone for the retired mixed cron dispatcher.

The active repository architecture has one independently owned lane for each
of AShare, CNFutures, and Crypto.  A mixed dispatcher cannot select data
readers, markets, notifications, or execution paths on their behalf.  This
module intentionally exposes only a deterministic retirement exit so stale
installed wrappers fail without importing any legacy runtime.
"""

from __future__ import annotations

import json
import sys


def main() -> int:
    print(
        json.dumps(
            {
                "component": "shared.wrappers.tradings_cron_entry",
                "state": "retired",
                "reason": "legacy_runtime_retired",
                "replacement": "market_specific_composition_after_tradingdatas_handoff",
                "real_trading_enabled": False,
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        file=sys.stderr,
    )
    return 78


if __name__ == "__main__":
    raise SystemExit(main())
