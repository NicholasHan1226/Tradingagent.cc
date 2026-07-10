#!/usr/bin/env python3
"""Explicit, idempotent authoritative-path cutover from epoch 1 (200k CNY legacy)
to epoch 2 (50k CNY current) for A-share simulated accounts.

Usage
-----

.. code:: bash

    # Dry-run (safe, no files modified, reports exact planned actions):
    python3 tools/migrate_sim_capital_epoch.py --dry-run --pretty

    # Dry-run with external evidence paths:
    python3 tools/migrate_sim_capital_epoch.py --dry-run --pretty \\
        --positions-snapshot signals/positions/simulated_ashare_positions.json \\
        --tiers-root shared/logs/local_sim_tiers

    # Apply migration (archives old contents under the stable ledger lock):
    python3 tools/migrate_sim_capital_epoch.py --apply --pretty

    # Apply with external evidence:
    python3 tools/migrate_sim_capital_epoch.py --apply --pretty \\
        --positions-snapshot signals/positions/simulated_ashare_positions.json \\
        --tiers-root shared/logs/local_sim_tiers

What it does
------------

1. Dry-run: reports the exact actions that would be taken without modifying
   any files.  This includes which files would be moved to the archive,
   where the archive would be created, and what metadata would be written.

2. Apply:

   a. Reads the current epoch state — if already epoch 2, returns no-op.
   b. Checks the archive target for collisions (fails closed if stale).
   c. Acquires the existing ``.local_sim.lock`` in the ledger directory.
   d. Preserves that lock inode and moves the old ledger contents into
      ``shared/logs/epoch_archive/epoch_1_legacy_200k/`` while holding it.
   e. Copies the external positions snapshot into the archive as evidence
      (if ``--positions-snapshot`` is supplied).
   f. Copies tier experiments data into the archive as evidence
      (if ``--tiers-root`` is supplied).
   g. Writes an archive manifest proving completion.
   h. Bootstraps the same ``shared/logs/local_sim/`` path as a fresh, empty
      epoch-2 account with ``.epoch_metadata.json``.
   i. Writes the epoch state file marking epoch 2 as current.
   j. Releases the lock.

Old files are NEVER rewritten. Normal ledger writers remain blocked by the
same lock throughout the cutover. Running again after a successful migration
is idempotent (returns ``already_migrated``).

There is NO ``local_sim_epoch2`` path anywhere — both epochs share the
single authoritative ``shared/logs/local_sim/`` directory.

See ``shared/execution/sim_account_epoch.py`` for epoch definitions.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.execution.sim_account_epoch import apply_cutover, dry_run_cutover


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Migrate A-share simulated capital from epoch 1 (200k) to epoch 2 (50k) via authoritative-path cutover.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Report exact planned actions without modifying any files. (Default.)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually perform the cutover (archives old ledger, recreates as epoch 2).",
    )
    parser.add_argument(
        "--positions-snapshot",
        type=Path,
        default=None,
        help="Path to external simulated_ashare_positions.json to archive as evidence.",
    )
    parser.add_argument(
        "--tiers-root",
        type=Path,
        default=None,
        help="Path to local_sim_tiers/ directory to archive as evidence.",
    )
    parser.add_argument(
        "--archive-root",
        type=Path,
        default=None,
        help="Archive destination root. Default: shared/logs/epoch_archive",
    )
    parser.add_argument(
        "--ledger-path",
        type=Path,
        default=None,
        help="Path to the authoritative local_sim directory. Default: shared/logs/local_sim",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON output.",
    )
    args = parser.parse_args(argv)

    do_apply = args.apply
    # If --apply is given, --dry-run is effectively false
    if do_apply:
        result = apply_cutover(
            ledger_path=args.ledger_path,
            positions_snapshot_path=args.positions_snapshot,
            tiers_root=args.tiers_root,
            archive_root=args.archive_root,
        )
    else:
        result = dry_run_cutover(
            ledger_path=args.ledger_path,
            positions_snapshot_path=args.positions_snapshot,
            tiers_root=args.tiers_root,
            archive_root=args.archive_root,
        )

    indent = 2 if args.pretty else None
    print(json.dumps(result, ensure_ascii=False, indent=indent, sort_keys=True))
    return 0 if result.get("status") not in {"error"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
