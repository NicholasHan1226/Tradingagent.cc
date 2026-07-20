#!/usr/bin/env python3
"""Retired legacy CNFutures simulation CLI.

The market-specific simulator remains available through the explicit
``CNFutures.sim_runner`` library contract.  This historical CLI is deliberately
kept as a small tombstone so direct module execution cannot reconstruct the old
8082/SQLite data path or write review artifacts.
"""

from __future__ import annotations

from pathlib import Path

if __package__ in (None, ""):
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.governance.retirement import retired_cli


def main() -> int:
    """Fail closed before arguments, environment, data, or output are read."""

    return retired_cli("CNFutures.run_simulation")


if __name__ == "__main__":
    raise SystemExit(main())
