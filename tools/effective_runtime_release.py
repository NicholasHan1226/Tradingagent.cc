"""Read-only resolver for the release a systemd unit actually runs.

Repository HEAD, an immutable release directory, the ``current`` symlink and a
running process are separate facts.  This tool reports them separately and
fails closed when an active service cannot be bound to one release.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Mapping, Sequence


CONTRACT = "tradingagent.effective_runtime_release.v1"
UNIT_PATTERN = re.compile(r"[A-Za-z0-9_.@-]+\.service\Z")
PRODUCT_PATTERN = re.compile(r"[a-z][a-z0-9-]*\Z")
RELEASE_PATTERN = re.compile(r"[0-9a-f]{7,64}\Z")
SYSTEMD_PROPERTIES = (
    "LoadState",
    "ActiveState",
    "SubState",
    "MainPID",
    "FragmentPath",
    "DropInPaths",
    "ExecStart",
    "ExecStartPre",
    "WorkingDirectory",
)
# systemd omits properties whose value is empty unless the caller explicitly
# requests all properties.  These fields are optional source material for
# release-path extraction; represent an omitted empty value exactly as an
# empty string rather than treating a valid systemctl response as incomplete.
OPTIONAL_EMPTY_SYSTEMD_PROPERTIES = frozenset(
    {"DropInPaths", "ExecStart", "ExecStartPre", "WorkingDirectory"}
)


class EffectiveRuntimeReleaseError(RuntimeError):
    """Stable fail-closed resolver error."""


@dataclass(frozen=True)
class EffectiveRuntimeRelease:
    contract: str
    product: str
    unit: str
    load_state: str
    active_state: str
    sub_state: str
    main_pid: int
    current_release: str | None
    unit_release_refs: tuple[str, ...]
    process_release_refs: tuple[str, ...]
    effective_release: str | None
    effective_source: str | None
    current_matches_effective: bool | None
    runtime_verified: bool
    blockers: tuple[str, ...]
    read_only: bool = True
    real_trading_enabled: bool = False


def _text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise EffectiveRuntimeReleaseError(f"{field}_invalid")
    return value.strip()


def _main_pid(value: object) -> int:
    raw = _text(value, "main_pid")
    try:
        pid = int(raw)
    except ValueError as exc:
        raise EffectiveRuntimeReleaseError("main_pid_invalid") from exc
    if pid < 0:
        raise EffectiveRuntimeReleaseError("main_pid_invalid")
    return pid


def _release_refs(text: str, *, release_root: Path) -> tuple[str, ...]:
    prefix = re.escape(str(release_root))
    pattern = re.compile(
        rf"(?<![A-Za-z0-9_.-]){prefix}/(current|[0-9a-f]{{7,64}})(?=/|\s|;|\}}|$)"
    )
    return tuple(sorted(set(pattern.findall(text))))


def _resolve_current(release_root: Path) -> str | None:
    current = release_root / "current"
    if not current.is_symlink():
        return None
    try:
        target = current.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    try:
        relative = target.relative_to(release_root.resolve(strict=True))
    except (OSError, ValueError):
        return None
    if len(relative.parts) != 1 or not RELEASE_PATTERN.fullmatch(relative.name):
        return None
    if not target.is_dir() or target.is_symlink():
        return None
    return relative.name


def _normalize_refs(
    refs: tuple[str, ...], *, current_release: str | None
) -> tuple[str, ...]:
    resolved: set[str] = set()
    for ref in refs:
        if ref == "current":
            if current_release is not None:
                resolved.add(current_release)
        else:
            resolved.add(ref)
    return tuple(sorted(resolved))


def _systemd_show(unit: str) -> Mapping[str, str]:
    if not UNIT_PATTERN.fullmatch(unit):
        raise EffectiveRuntimeReleaseError("unit_name_invalid")
    command = [
        "systemctl",
        "show",
        "--no-pager",
        *(f"--property={field}" for field in SYSTEMD_PROPERTIES),
        unit,
    ]
    try:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise EffectiveRuntimeReleaseError("systemd_show_failed") from exc
    values: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator and key in SYSTEMD_PROPERTIES:
            values[key] = value
    for field in OPTIONAL_EMPTY_SYSTEMD_PROPERTIES:
        values.setdefault(field, "")
    if set(values) != set(SYSTEMD_PROPERTIES):
        raise EffectiveRuntimeReleaseError("systemd_show_incomplete")
    return values


def _process_text(pid: int) -> str:
    if pid < 1:
        return ""
    proc = Path("/proc") / str(pid)
    values: list[str] = []
    for name in ("cwd", "exe"):
        try:
            values.append(os.readlink(proc / name))
        except OSError:
            continue
    try:
        raw = (proc / "cmdline").read_bytes()
    except OSError:
        raw = b""
    if raw:
        values.append(raw.replace(b"\x00", b" ").decode("utf-8", errors="replace"))
    return "\n".join(values)


def resolve_effective_runtime_release(
    *,
    product: str,
    unit: str,
    systemd_properties: Mapping[str, str],
    release_root: Path,
    process_text: str = "",
    current_release: str | None = None,
) -> EffectiveRuntimeRelease:
    """Resolve one effective release from read-only systemd/process evidence."""

    if not PRODUCT_PATTERN.fullmatch(product):
        raise EffectiveRuntimeReleaseError("product_invalid")
    if not UNIT_PATTERN.fullmatch(unit):
        raise EffectiveRuntimeReleaseError("unit_name_invalid")
    if not release_root.is_absolute():
        raise EffectiveRuntimeReleaseError("release_root_not_absolute")
    missing = [field for field in SYSTEMD_PROPERTIES if field not in systemd_properties]
    if missing:
        raise EffectiveRuntimeReleaseError("systemd_properties_incomplete")

    load_state = _text(systemd_properties["LoadState"], "load_state")
    active_state = _text(systemd_properties["ActiveState"], "active_state")
    sub_state = _text(systemd_properties["SubState"], "sub_state")
    pid = _main_pid(systemd_properties["MainPID"])
    if current_release is not None and not RELEASE_PATTERN.fullmatch(current_release):
        raise EffectiveRuntimeReleaseError("current_release_invalid")

    unit_material = "\n".join(
        _text(systemd_properties[field], field.lower())
        for field in (
            "FragmentPath",
            "DropInPaths",
            "ExecStart",
            "ExecStartPre",
            "WorkingDirectory",
        )
    )
    unit_refs = _normalize_refs(
        _release_refs(unit_material, release_root=release_root),
        current_release=current_release,
    )
    process_refs = _normalize_refs(
        _release_refs(process_text, release_root=release_root),
        current_release=current_release,
    )

    blockers: list[str] = []
    if load_state != "loaded":
        blockers.append("unit_not_loaded")
    if (
        "current" in _release_refs(unit_material, release_root=release_root)
        and current_release is None
    ):
        blockers.append("current_release_unresolved")
    if len(unit_refs) > 1:
        blockers.append("unit_references_multiple_releases")
    if len(process_refs) > 1:
        blockers.append("process_references_multiple_releases")

    active = active_state == "active"
    if active and pid < 1:
        blockers.append("active_unit_main_pid_missing")
    if active and not process_refs:
        blockers.append("active_process_release_unproven")

    effective_release: str | None = None
    effective_source: str | None = None
    if len(process_refs) == 1:
        effective_release = process_refs[0]
        effective_source = "running_process"
    elif len(unit_refs) == 1:
        effective_release = unit_refs[0]
        effective_source = "systemd_effective_configuration"

    if active and len(process_refs) == 1 and len(unit_refs) == 1:
        if process_refs[0] != unit_refs[0]:
            blockers.append("process_unit_release_mismatch")
    if effective_release is None:
        blockers.append("effective_release_unresolved")

    current_matches = (
        None
        if current_release is None or effective_release is None
        else current_release == effective_release
    )
    runtime_verified = active and effective_release is not None and not blockers
    return EffectiveRuntimeRelease(
        contract=CONTRACT,
        product=product,
        unit=unit,
        load_state=load_state,
        active_state=active_state,
        sub_state=sub_state,
        main_pid=pid,
        current_release=current_release,
        unit_release_refs=unit_refs,
        process_release_refs=process_refs,
        effective_release=effective_release,
        effective_source=effective_source,
        current_matches_effective=current_matches,
        runtime_verified=runtime_verified,
        blockers=tuple(sorted(set(blockers))),
    )


def inspect_effective_runtime_release(
    *, product: str, unit: str, release_root: Path
) -> EffectiveRuntimeRelease:
    properties = _systemd_show(unit)
    pid = _main_pid(properties["MainPID"])
    return resolve_effective_runtime_release(
        product=product,
        unit=unit,
        systemd_properties=properties,
        release_root=release_root,
        process_text=_process_text(pid),
        current_release=_resolve_current(release_root),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Resolve the immutable release used by one systemd unit"
    )
    parser.add_argument("--product", default="tradingagent")
    parser.add_argument("--unit", required=True)
    parser.add_argument(
        "--release-root",
        type=Path,
        default=Path("/opt/investment/releases/tradingagent"),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = inspect_effective_runtime_release(
            product=args.product,
            unit=args.unit,
            release_root=args.release_root,
        )
    except EffectiveRuntimeReleaseError as exc:
        print(
            json.dumps({"contract": CONTRACT, "status": "blocked", "reason": str(exc)})
        )
        return 2
    print(json.dumps(asdict(result), sort_keys=True, separators=(",", ":")))
    return 0 if result.runtime_verified else 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CONTRACT",
    "EffectiveRuntimeRelease",
    "EffectiveRuntimeReleaseError",
    "inspect_effective_runtime_release",
    "resolve_effective_runtime_release",
]
