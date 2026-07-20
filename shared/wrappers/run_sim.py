#!/usr/bin/env python3
"""Fail-closed tombstone for the retired mixed-market simulator.

Current simulations are composed through the market-specific AShare,
CNFutures, and Crypto lanes.  Keeping this tiny entrypoint lets an installed
legacy scheduler fail explicitly while preventing any old data reader, market
package, or broker path from being imported.
"""

from __future__ import annotations

import json
import sys


def main() -> int:
    print(
        json.dumps(
            {
                "component": "shared.wrappers.run_sim",
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
