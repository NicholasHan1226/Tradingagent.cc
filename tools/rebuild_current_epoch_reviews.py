#!/usr/bin/env python3
"""Dry-run or apply an A-share current-epoch derived-review rebuild."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from Ashare.epoch_review import apply_epoch_reset_plan, build_epoch_reset_plan
from shared.execution.sim_account_epoch import epoch_capital_cny, get_epoch, read_epoch_state


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
    current_epoch = int(state.get("current_epoch_id") or 1)
    if current_epoch <= 1 or not state.get("cutover_timestamp"):
        report = {
            "status": "error",
            "reason": "capital_cutover_not_active",
            "current_epoch_id": current_epoch,
        }
        print(json.dumps(report, ensure_ascii=False, indent=2 if args.pretty else None, sort_keys=True))
        return 2

    epoch_state = {
        **state,
        "current_epoch_id": current_epoch,
        "capital_cny": float(state.get("capital_cny") or epoch_capital_cny(current_epoch)),
        "cutover_timestamp": str(state["cutover_timestamp"]),
    }
    archive_dir = args.archive_dir or _default_archive_dir(epoch_state)
    plan = build_epoch_reset_plan(args.review_dir, archive_dir, epoch_state)
    if plan.get("status") != "ready":
        report = {"status": "error", "mode": "dry_run", "plan": plan}
        print(json.dumps(report, ensure_ascii=False, indent=2 if args.pretty else None, sort_keys=True))
        return 2

    if args.apply:
        result = apply_epoch_reset_plan(plan)
        report = {"status": result.get("status"), "mode": "apply", "plan": plan, "result": result}
        exit_code = 0 if result.get("status") == "applied" else 2
    else:
        report = {"status": "dry_run", "mode": "dry_run", "plan": plan}
        exit_code = 0
    print(json.dumps(report, ensure_ascii=False, indent=2 if args.pretty else None, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
