#!/usr/bin/env python3
"""Validate that a long-lived market lane is in its assigned worktree and paths."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.governance.market_lanes import validate_market_lane


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lane", required=True, choices=("ashare", "cnfutures", "crypto"))
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument(
        "--base-ref",
        default="main",
        help=(
            "validation base; use main at slice start, then the exact "
            "Controller-recorded assignment commit for handoff"
        ),
    )
    args = parser.parse_args()
    try:
        result = validate_market_lane(
            args.lane,
            args.repo,
            base_ref=args.base_ref,
        )
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps({"ok": True, **asdict(result)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
