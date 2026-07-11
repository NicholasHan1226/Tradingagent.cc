#!/usr/bin/env python3
"""Dry-run or apply an A-share current-epoch derived-review rebuild."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from Ashare.epoch_review import apply_epoch_reset_plan, build_epoch_reset_plan
from shared.execution.sim_account_epoch import (
    get_epoch,
    read_epoch_state,
    require_authoritative_epoch_metadata,
)


DEFAULT_REVIEW_DIR = ROOT / "shared" / "review" / "ashare"


def _default_archive_dir(epoch_state: dict[str, Any]) -> Path:
    current_epoch = int(epoch_state.get("current_epoch_id") or 1)
    previous_epoch = int(epoch_state.get("previous_epoch_id") or current_epoch - 1)
    previous = get_epoch(previous_epoch)
    return (
        ROOT
        / "shared"
        / "logs"
        / "epoch_archive"
        / f"epoch_{previous_epoch}_{previous['label']}"
        / "derived_reviews"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Plan only (default).")
    mode.add_argument("--apply", action="store_true", help="Apply the reviewed reset plan.")
    parser.add_argument("--pretty", action="store_true")
    parser.add_argument("--review-dir", type=Path, default=DEFAULT_REVIEW_DIR)
    parser.add_argument("--archive-dir", type=Path, default=None)
    args = parser.parse_args(argv)

    state = read_epoch_state()
    try:
        metadata = require_authoritative_epoch_metadata(state)
    except ValueError as exc:
        report = {
            "status": "error",
            "reason": str(exc),
        }
        print(json.dumps(report, ensure_ascii=False, indent=2 if args.pretty else None, sort_keys=True))
        return 2
    current_epoch = int(metadata["capital_epoch"])

    archive_dir = args.archive_dir or _default_archive_dir(state)
    custom_paths = args.review_dir != DEFAULT_REVIEW_DIR or args.archive_dir is not None
    allowed_root = ROOT if not custom_paths else Path(
        os.path.commonpath(
            (
                str(args.review_dir.resolve(strict=False)),
                str(archive_dir.resolve(strict=False)),
            )
        )
    )
    epoch_state = {
        **state,
        "current_epoch_id": current_epoch,
        "capital_cny": float(metadata["capital_cny"]),
        "cutover_timestamp": str(metadata["cutover_timestamp"]),
        "allowed_root": str(allowed_root),
    }
    plan = build_epoch_reset_plan(args.review_dir, archive_dir, epoch_state)
    if plan.get("status") not in {"ready", "already_applied"}:
        report = {"status": "error", "mode": "dry_run", "plan": plan}
        print(json.dumps(report, ensure_ascii=False, indent=2 if args.pretty else None, sort_keys=True))
        return 2

    if args.apply:
        result = apply_epoch_reset_plan(plan)
        report = {"status": result.get("status"), "mode": "apply", "plan": plan, "result": result}
        exit_code = 0 if result.get("status") in {"applied", "already_applied"} else 2
    else:
        report = {"status": "dry_run", "mode": "dry_run", "plan": plan}
        exit_code = 0
    print(json.dumps(report, ensure_ascii=False, indent=2 if args.pretty else None, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
