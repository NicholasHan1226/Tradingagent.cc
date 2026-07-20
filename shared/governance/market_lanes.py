"""Fail-closed ownership checks for the long-lived market worktrees."""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
import subprocess
from typing import Any, Mapping, Sequence

import yaml


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MARKET_LANES_PATH = ROOT / "shared" / "governance" / "market_lanes.yaml"
ACTIVE_RUNTIME_MARKETS = ("ashare", "cn_futures", "crypto")
RUNTIME_MARKET_ALIASES = {
    "ashare": "ashare",
    "a-share": "ashare",
    "a_share": "ashare",
    "cn_futures": "cn_futures",
    "cn-futures": "cn_futures",
    "cnfutures": "cn_futures",
    "crypto": "crypto",
}
RUNTIME_MARKET_LANE_ALIASES = {
    "ashare": "ashare",
    "cn_futures": "cnfutures",
    "crypto": "crypto",
}


def canonical_runtime_market(value: Any) -> str:
    """Return one of the three owned runtime markets or fail closed.

    Spelling aliases are accepted only when they map to an owned lane.  Empty,
    retired and unknown market values never inherit A-share semantics.
    """

    raw = str(value or "").strip().lower()
    canonical = RUNTIME_MARKET_ALIASES.get(raw)
    if canonical not in ACTIVE_RUNTIME_MARKETS:
        raise ValueError(f"unknown or retired runtime market: {raw or '<missing>'}")
    return canonical


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _patterns(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{field} must be a non-empty list")
    result = tuple(_text(item, f"{field}[]") for item in value)
    if len(result) != len(set(result)):
        raise ValueError(f"{field} must not contain duplicates")
    for pattern in result:
        path = PurePosixPath(pattern.rstrip("/"))
        if (
            pattern.startswith(("/", "~"))
            or "\\" in pattern
            or path.is_absolute()
            or any(part in {".", ".."} for part in path.parts)
        ):
            raise ValueError(f"{field}[] must be a repository-relative pattern")
    return result


def _optional_texts(value: Any, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")
    result = tuple(_text(item, f"{field}[]") for item in value)
    if len(result) != len(set(result)):
        raise ValueError(f"{field} must not contain duplicates")
    return result


@dataclass(frozen=True)
class BrokerBoundary:
    simulation_contract: str
    external_test_contracts: tuple[str, ...]
    future_live_adapter_family: str
    live_enabled: bool


@dataclass(frozen=True)
class MarketLane:
    lane_id: str
    worktree_basename: str
    branch: str
    authority_id: str
    authority_state: str
    broker_boundary: BrokerBoundary
    owned_paths: tuple[str, ...]
    handoff_only_paths: tuple[str, ...]


@dataclass(frozen=True)
class MarketLaneRegistry:
    version: int
    lanes: tuple[MarketLane, ...]

    def get(self, lane_id: str) -> MarketLane:
        matches = [lane for lane in self.lanes if lane.lane_id == lane_id]
        if len(matches) != 1:
            raise ValueError(f"unknown or duplicate market lane: {lane_id}")
        return matches[0]

    def get_for_runtime_market(self, market: str) -> MarketLane:
        market_key = canonical_runtime_market(market)
        lane_id = RUNTIME_MARKET_LANE_ALIASES[market_key]
        return self.get(lane_id)


@dataclass(frozen=True)
class LaneValidation:
    lane_id: str
    repo_root: str
    branch: str
    base_ref: str
    base_head: str
    lane_head: str
    ahead: int
    behind: int
    changed_paths: tuple[str, ...]


def load_market_lanes(
    path: Path = DEFAULT_MARKET_LANES_PATH,
) -> MarketLaneRegistry:
    candidate = Path(path)
    if not candidate.is_file() or candidate.is_symlink():
        raise ValueError("market lane contract must be a regular file")
    root = yaml.safe_load(candidate.read_text(encoding="utf-8"))
    if not isinstance(root, Mapping):
        raise ValueError("market lane contract must be a mapping")
    version = root.get("version")
    if isinstance(version, bool) or version != 1:
        raise ValueError("market lane contract version must be integer 1")
    raw_lanes = root.get("lanes")
    if not isinstance(raw_lanes, list) or not raw_lanes:
        raise ValueError("market lanes must be a non-empty list")

    lanes: list[MarketLane] = []
    seen_ids: set[str] = set()
    seen_branches: set[str] = set()
    seen_worktrees: set[str] = set()
    seen_broker_contracts: set[str] = set()
    for index, raw in enumerate(raw_lanes):
        if not isinstance(raw, Mapping):
            raise ValueError(f"lanes[{index}] must be a mapping")
        broker_raw = raw.get("broker_boundary")
        if not isinstance(broker_raw, Mapping):
            raise ValueError(f"lanes[{index}].broker_boundary must be a mapping")
        live_enabled = broker_raw.get("live_enabled")
        if not isinstance(live_enabled, bool):
            raise ValueError(
                f"lanes[{index}].broker_boundary.live_enabled must be boolean"
            )
        broker_boundary = BrokerBoundary(
            simulation_contract=_text(
                broker_raw.get("simulation_contract"),
                f"lanes[{index}].broker_boundary.simulation_contract",
            ),
            external_test_contracts=_optional_texts(
                broker_raw.get("external_test_contracts"),
                f"lanes[{index}].broker_boundary.external_test_contracts",
            ),
            future_live_adapter_family=_text(
                broker_raw.get("future_live_adapter_family"),
                f"lanes[{index}].broker_boundary.future_live_adapter_family",
            ),
            live_enabled=live_enabled,
        )
        lane = MarketLane(
            lane_id=_text(raw.get("lane_id"), f"lanes[{index}].lane_id"),
            worktree_basename=_text(
                raw.get("worktree_basename"),
                f"lanes[{index}].worktree_basename",
            ),
            branch=_text(raw.get("branch"), f"lanes[{index}].branch"),
            authority_id=_text(
                raw.get("authority_id"),
                f"lanes[{index}].authority_id",
            ),
            authority_state=_text(
                raw.get("authority_state"),
                f"lanes[{index}].authority_state",
            ),
            broker_boundary=broker_boundary,
            owned_paths=_patterns(
                raw.get("owned_paths"),
                f"lanes[{index}].owned_paths",
            ),
            handoff_only_paths=_patterns(
                raw.get("handoff_only_paths"),
                f"lanes[{index}].handoff_only_paths",
            ),
        )
        if lane.lane_id in seen_ids:
            raise ValueError(f"duplicate market lane_id: {lane.lane_id}")
        if lane.branch in seen_branches:
            raise ValueError(f"duplicate market branch: {lane.branch}")
        if lane.worktree_basename in seen_worktrees:
            raise ValueError(
                f"duplicate market worktree_basename: {lane.worktree_basename}"
            )
        if lane.broker_boundary.live_enabled:
            raise ValueError(f"market lane {lane.lane_id} must keep live disabled")
        if lane.authority_state not in {
            "current_verified_simulated",
            "isolated_shadow_only",
        }:
            raise ValueError(
                f"market lane {lane.lane_id} has unsupported authority_state"
            )
        broker_contracts = {
            lane.broker_boundary.simulation_contract,
            lane.broker_boundary.future_live_adapter_family,
            *lane.broker_boundary.external_test_contracts,
        }
        overlap = seen_broker_contracts.intersection(broker_contracts)
        if overlap:
            raise ValueError(
                "broker contracts must be market-specific: "
                + ", ".join(sorted(overlap))
            )
        seen_broker_contracts.update(broker_contracts)
        seen_ids.add(lane.lane_id)
        seen_branches.add(lane.branch)
        seen_worktrees.add(lane.worktree_basename)
        lanes.append(lane)
    return MarketLaneRegistry(version=version, lanes=tuple(lanes))


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise ValueError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout.strip()


def _nul_paths(repo: Path, *args: str) -> set[str]:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(f"git {' '.join(args)} failed: {detail}")
    return {
        item.decode("utf-8", errors="strict")
        for item in result.stdout.split(b"\0")
        if item
    }


def collect_changed_paths(repo: Path, base_ref: str = "main") -> tuple[str, ...]:
    paths: set[str] = set()
    paths.update(_nul_paths(repo, "diff", "--name-only", "-z", f"{base_ref}...HEAD"))
    paths.update(_nul_paths(repo, "diff", "--name-only", "-z"))
    paths.update(_nul_paths(repo, "diff", "--cached", "--name-only", "-z"))
    paths.update(_nul_paths(repo, "ls-files", "--others", "--exclude-standard", "-z"))
    return tuple(sorted(paths))


def _matches(path: str, patterns: Sequence[str]) -> bool:
    return any(fnmatchcase(path, pattern) for pattern in patterns)


def validate_market_lane(
    lane_id: str,
    repo: Path,
    *,
    base_ref: str = "main",
    registry_path: Path = DEFAULT_MARKET_LANES_PATH,
) -> LaneValidation:
    lane = load_market_lanes(registry_path).get(lane_id)
    repo_root = Path(_git(repo, "rev-parse", "--show-toplevel")).resolve()
    if repo_root.name != lane.worktree_basename:
        raise ValueError(
            f"lane {lane_id} requires worktree {lane.worktree_basename}, "
            f"got {repo_root.name}"
        )
    branch = _git(repo_root, "rev-parse", "--abbrev-ref", "HEAD")
    if branch != lane.branch:
        raise ValueError(f"lane {lane_id} requires branch {lane.branch}, got {branch}")
    base_head = _git(repo_root, "rev-parse", f"{base_ref}^{{commit}}")
    lane_head = _git(repo_root, "rev-parse", "HEAD^{commit}")
    counts = _git(
        repo_root,
        "rev-list",
        "--left-right",
        "--count",
        f"{base_ref}...HEAD",
    ).split()
    if len(counts) != 2 or any(not value.isdigit() for value in counts):
        raise ValueError(f"unable to determine lane freshness against {base_ref}")
    behind, ahead = (int(value) for value in counts)
    if behind:
        raise ValueError(
            f"lane {lane_id} is {behind} commit(s) behind {base_ref}; "
            "synchronize the clean lane before development"
        )
    changed_paths = collect_changed_paths(repo_root, base_ref)
    forbidden = tuple(
        path
        for path in changed_paths
        if _matches(path, lane.handoff_only_paths)
        or not _matches(path, lane.owned_paths)
    )
    if forbidden:
        raise ValueError(
            f"lane {lane_id} changed paths outside its ownership: "
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
