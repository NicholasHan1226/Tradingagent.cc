"""Fail-closed loaders for architecture state and legacy retirement records."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml


SHARED_SIGNALS_QUERY_CONTRACT_ID = "sharedsignals.query_result.v1"
UNIVERSE_SCOPE_CONTRACT_ID = "tradingagent.universe_scope.v1"
LLM_EVIDENCE_CONTRACT_ID = "tradingagent.llm_evidence.v1"

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SYSTEM_STATE_PATH = ROOT / "shared" / "governance" / "system_state_matrix.yaml"
DEFAULT_LEGACY_INVENTORY_PATH = ROOT / "shared" / "governance" / "legacy_inventory.yaml"

ALLOWED_STATES = frozenset(
    {
        "CURRENT_VERIFIED",
        "TARGET_CONTRACT",
        "PLANNED_NOT_IMPLEMENTED",
        "COMPATIBILITY_TIMEBOXED",
        "HISTORICAL_READ_ONLY",
        "RETIREMENT_PENDING_VERIFICATION",
        "RETIRED_BLOCKED",
    }
)
ALLOWED_COMPATIBILITY_MODES = frozenset(
    {
        "timeboxed_read_only",
        "historical_read_only",
        "retirement_pending_verification",
    }
)


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be a mapping")
    return value


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _strings(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{field} must be a non-empty list")
    result = tuple(_text(item, f"{field}[]") for item in value)
    if len(result) != len(set(result)):
        raise ValueError(f"{field} must not contain duplicates")
    return result


def _optional_strings(value: Any, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")
    result = tuple(_text(item, f"{field}[]") for item in value)
    if len(result) != len(set(result)):
        raise ValueError(f"{field} must not contain duplicates")
    return result


def _load(path: Path) -> Mapping[str, Any]:
    candidate = Path(path)
    if not candidate.is_file() or candidate.is_symlink():
        raise ValueError("governance contract must be a regular file")
    payload = yaml.safe_load(candidate.read_text(encoding="utf-8"))
    return _mapping(payload, "governance contract")


@dataclass(frozen=True)
class SystemStateEntry:
    entry_id: str
    state: str
    layer: str
    owner: str
    canonical_path: str
    production_verified: bool
    last_verified_at: str
    evidence: tuple[str, ...]
    allowed_uses: tuple[str, ...]
    prohibited_uses: tuple[str, ...]
    successor: str


@dataclass(frozen=True)
class SystemStateMatrix:
    version: int
    entries: tuple[SystemStateEntry, ...]


@dataclass(frozen=True)
class LegacyEntry:
    legacy_id: str
    owner: str
    paths: tuple[str, ...]
    runtime_paths: tuple[str, ...]
    replacement: str
    compatibility_mode: str
    sunset_phase: str
    remaining_consumers: tuple[str, ...]
    deletion_preconditions: tuple[str, ...]
    rollback: str


@dataclass(frozen=True)
class LegacyInventory:
    version: int
    entries: tuple[LegacyEntry, ...]


def _version(root: Mapping[str, Any]) -> int:
    version = root.get("version")
    if isinstance(version, bool) or version != 1:
        raise ValueError("governance contract version must be integer 1")
    return version


def load_system_state_matrix(
    path: Path = DEFAULT_SYSTEM_STATE_PATH,
) -> SystemStateMatrix:
    root = _load(path)
    raw_entries = root.get("entries")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise ValueError("system state entries must be a non-empty list")
    entries: list[SystemStateEntry] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_entries):
        item = _mapping(raw, f"entries[{index}]")
        entry_id = _text(item.get("entry_id"), f"entries[{index}].entry_id")
        if entry_id in seen:
            raise ValueError(f"duplicate system state entry_id: {entry_id}")
        seen.add(entry_id)
        state = _text(item.get("state"), f"entries[{index}].state")
        if state not in ALLOWED_STATES:
            raise ValueError(f"unsupported system state: {state}")
        production_verified = item.get("production_verified")
        if not isinstance(production_verified, bool):
            raise ValueError("production_verified must be boolean")
        entries.append(
            SystemStateEntry(
                entry_id=entry_id,
                state=state,
                layer=_text(item.get("layer"), f"entries[{index}].layer"),
                owner=_text(item.get("owner"), f"entries[{index}].owner"),
                canonical_path=_text(
                    item.get("canonical_path"),
                    f"entries[{index}].canonical_path",
                ),
                production_verified=production_verified,
                last_verified_at=_text(
                    item.get("last_verified_at"),
                    f"entries[{index}].last_verified_at",
                ),
                evidence=_strings(item.get("evidence"), "evidence"),
                allowed_uses=_strings(item.get("allowed_uses"), "allowed_uses"),
                prohibited_uses=_strings(
                    item.get("prohibited_uses"), "prohibited_uses"
                ),
                successor=_text(item.get("successor"), "successor"),
            )
        )
    return SystemStateMatrix(version=_version(root), entries=tuple(entries))


def load_legacy_inventory(
    path: Path = DEFAULT_LEGACY_INVENTORY_PATH,
) -> LegacyInventory:
    root = _load(path)
    raw_entries = root.get("entries")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise ValueError("legacy entries must be a non-empty list")
    entries: list[LegacyEntry] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_entries):
        item = _mapping(raw, f"entries[{index}]")
        legacy_id = _text(item.get("legacy_id"), f"entries[{index}].legacy_id")
        if legacy_id in seen:
            raise ValueError(f"duplicate legacy_id: {legacy_id}")
        seen.add(legacy_id)
        compatibility_mode = _text(item.get("compatibility_mode"), "compatibility_mode")
        if compatibility_mode not in ALLOWED_COMPATIBILITY_MODES:
            raise ValueError(f"unsupported compatibility_mode: {compatibility_mode}")
        paths = _strings(item.get("paths"), "paths")
        runtime_paths = _optional_strings(item.get("runtime_paths"), "runtime_paths")
        overlap = set(paths).intersection(runtime_paths)
        if overlap:
            raise ValueError(
                f"legacy paths and runtime_paths must not overlap: {sorted(overlap)}"
            )
        entries.append(
            LegacyEntry(
                legacy_id=legacy_id,
                owner=_text(item.get("owner"), "owner"),
                paths=paths,
                runtime_paths=runtime_paths,
                replacement=_text(item.get("replacement"), "replacement"),
                compatibility_mode=compatibility_mode,
                sunset_phase=_text(item.get("sunset_phase"), "sunset_phase"),
                remaining_consumers=_strings(
                    item.get("remaining_consumers"), "remaining_consumers"
                ),
                deletion_preconditions=_strings(
                    item.get("deletion_preconditions"), "deletion_preconditions"
                ),
                rollback=_text(item.get("rollback"), "rollback"),
            )
        )
    return LegacyInventory(version=_version(root), entries=tuple(entries))
