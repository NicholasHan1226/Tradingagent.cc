#!/usr/bin/env python3
"""Validate the tracked TradingAgent runtime topology contract."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.governance.runtime_topology import (  # noqa: E402
    DEFAULT_RUNTIME_TOPOLOGY_PATH,
    load_runtime_topology,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--topology",
        type=Path,
        default=DEFAULT_RUNTIME_TOPOLOGY_PATH,
    )
    args = parser.parse_args()
    topology = load_runtime_topology(args.topology)
    payload = {
        "contract_id": topology.contract_id,
        "markets": [item.market for item in topology.market_runtimes],
        "profiles": [
            {
                "profile_id": item.profile_id,
                "isolation_level": item.isolation_level,
                "learning_placement_policy": item.learning_placement_policy,
                "hosts": sorted(set(item.placements.values())),
            }
            for item in topology.deployment_profiles
        ],
        "simulation_only": topology.safety.simulation_only,
        "data_routes": list(topology.data_contract.routes),
        "valid": True,
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
