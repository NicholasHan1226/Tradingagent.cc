#!/usr/bin/env python3
"""Validate that a long-lived market lane is in its assigned worktree and paths."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path, PurePosixPath
import re
import sys
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.governance.market_lanes import (
    LaneValidation,
    _git,
    _matches,
    collect_changed_paths,
    load_market_lanes,
    validate_market_lane,
)


_FULL_COMMIT_SHA = re.compile(r"[0-9a-f]{40}")


def _exact_allowed_paths(values: Sequence[str]) -> tuple[str, ...]:
    result = tuple(values)
    if not result:
        raise ValueError("isolated validation requires at least one allowed path")
    for value in result:
        path = PurePosixPath(value)
        if (
            not value
            or value != value.strip()
            or value.startswith(("/", "~"))
            or "\\" in value
            or any(character in value for character in "*?[")
            or path.is_absolute()
            or any(part in {".", ".."} for part in path.parts)
            or path.as_posix() != value
        ):
            raise ValueError("allowed paths must be exact repository-relative files")
    return result


def validate_controller_isolated_lane(
    lane_id: str,
    repo: Path,
    *,
    base_ref: str,
    allowed_paths: Sequence[str],
) -> LaneValidation:
    """Validate one Controller-scoped market patch in an isolated worktree."""

    if _FULL_COMMIT_SHA.fullmatch(base_ref) is None:
        raise ValueError("isolated validation requires a full frozen commit SHA")
    allowed = _exact_allowed_paths(allowed_paths)
    lane = load_market_lanes().get(lane_id)
    outside = tuple(
        path
        for path in allowed
        if _matches(path, lane.handoff_only_paths)
        or not _matches(path, lane.owned_paths)
    )
    if outside:
        raise ValueError(
            f"lane {lane_id} allowed path outside its ownership: {outside[0]}"
        )

    repo_root = Path(_git(repo, "rev-parse", "--show-toplevel")).resolve()
    branch = _git(repo_root, "rev-parse", "--abbrev-ref", "HEAD")
    if branch != "HEAD" and not branch.startswith(f"codex/{lane_id}-"):
        raise ValueError(f"lane {lane_id} rejects isolated branch {branch}")
    base_head = _git(repo_root, "rev-parse", f"{base_ref}^{{commit}}")
    if base_head != base_ref:
        raise ValueError("isolated validation base did not resolve exactly")
    lane_head = _git(repo_root, "rev-parse", "HEAD^{commit}")
    counts = _git(
        repo_root,
        "rev-list",
        "--left-right",
        "--count",
        f"{base_ref}...HEAD",
    ).split()
    behind, ahead = (int(value) for value in counts)
    if behind:
        raise ValueError(
            f"lane {lane_id} diverges from or is {behind} commit(s) "
            f"behind frozen base {base_ref}"
        )

    changed_paths = collect_changed_paths(repo_root, base_ref)
    if not changed_paths:
        raise ValueError("isolated validation requires a non-empty patch")
    forbidden = tuple(path for path in changed_paths if path not in allowed)
    if forbidden:
        raise ValueError(
            f"lane {lane_id} changed paths outside Controller allowlist: "
            + ", ".join(forbidden)
        )
    return LaneValidation(
        lane_id=lane_id,
        repo_root=str(repo_root),
        branch=branch,
        base_ref=base_ref,
        base_head=base_head,
        lane_head=lane_head,
        ahead=ahead,
        behind=behind,
        changed_paths=changed_paths,
    )


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
    parser.add_argument(
        "--isolated-candidate",
        action="store_true",
        help="validate a Controller-frozen one-shot market candidate",
    )
    parser.add_argument(
        "--allowed-path",
        action="append",
        default=[],
        help=(
            "exact repo-relative file authorized by Controller; repeat to validate "
            "an isolated candidate"
        ),
    )
    args = parser.parse_args()
    try:
        if args.isolated_candidate:
            result = validate_controller_isolated_lane(
                args.lane,
                args.repo,
                base_ref=args.base_ref,
                allowed_paths=args.allowed_path,
            )
        else:
            if args.allowed_path:
                raise ValueError("--allowed-path requires --isolated-candidate")
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
