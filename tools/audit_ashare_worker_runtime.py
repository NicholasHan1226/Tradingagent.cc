#!/usr/bin/env python3
"""Fail-closed filesystem audit for the A-share observation worker candidate.

The audit reads metadata only. It never opens or returns token content, never
creates runtime directories and has no network, broker, LLM or execution path.
"""

from __future__ import annotations

import argparse
import grp
import importlib
import json
import os
from pathlib import Path
import pwd
import stat
import sys
from typing import Any, Sequence


TOKEN_FILE = Path("/run/secrets/tradingagent/tradingdatas-read.token")
EXPECTED_PYYAML_VERSION = "6.0.3"


class RuntimeAuditError(RuntimeError):
    """Raised when installed worker metadata violates the frozen contract."""


def _absolute(path: Path, *, field_name: str) -> Path:
    if not isinstance(path, Path) or not path.is_absolute():
        raise RuntimeAuditError(f"{field_name}_must_be_absolute")
    return path


def _lstat_no_follow(path: Path, *, field_name: str) -> os.stat_result:
    path = _absolute(path, field_name=field_name)
    current = Path(path.anchor)
    try:
        for part in path.parts[1:]:
            current /= part
            metadata = os.lstat(current)
            if stat.S_ISLNK(metadata.st_mode):
                raise RuntimeAuditError(f"path_symlink_forbidden:{field_name}")
    except FileNotFoundError as exc:
        raise RuntimeAuditError(f"path_missing:{field_name}") from exc
    return metadata


def _private_directory(
    path: Path,
    *,
    field_name: str,
    expected_uid: int,
    expected_gid: int,
) -> Path:
    metadata = _lstat_no_follow(path, field_name=field_name)
    if not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeAuditError(f"{field_name}_directory_required")
    if metadata.st_uid != expected_uid or metadata.st_gid != expected_gid:
        raise RuntimeAuditError(f"{field_name}_directory_owner_invalid")
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        raise RuntimeAuditError(f"{field_name}_directory_mode_invalid")
    return path.resolve(strict=True)


def _identity_can_write(
    metadata: os.stat_result,
    *,
    expected_uid: int,
    expected_gid: int,
) -> bool:
    mode = stat.S_IMODE(metadata.st_mode)
    if metadata.st_uid == expected_uid:
        return bool(mode & stat.S_IWUSR)
    if metadata.st_gid == expected_gid:
        return bool(mode & stat.S_IWGRP)
    return bool(mode & stat.S_IWOTH)


def audit_directory_layout(
    *,
    release_root: Path,
    state_root: Path,
    runtime_root: Path,
    log_root: Path,
    expected_uid: int,
    expected_gid: int,
) -> dict[str, Any]:
    """Verify release read-only access and private, disjoint writable roots."""

    for field_name, value in (
        ("expected_uid", expected_uid),
        ("expected_gid", expected_gid),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RuntimeAuditError(f"{field_name}_invalid")

    release_root = _absolute(release_root, field_name="release_root")
    try:
        release_metadata = os.stat(release_root, follow_symlinks=True)
        release_resolved = release_root.resolve(strict=True)
    except FileNotFoundError as exc:
        raise RuntimeAuditError("path_missing:release_root") from exc
    if not stat.S_ISDIR(release_metadata.st_mode):
        raise RuntimeAuditError("release_root_directory_required")
    if _identity_can_write(
        release_metadata,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
    ):
        raise RuntimeAuditError("release_root_writable_by_service")

    roots = {
        "state": _private_directory(
            state_root,
            field_name="state",
            expected_uid=expected_uid,
            expected_gid=expected_gid,
        ),
        "runtime": _private_directory(
            runtime_root,
            field_name="runtime",
            expected_uid=expected_uid,
            expected_gid=expected_gid,
        ),
        "log": _private_directory(
            log_root,
            field_name="log",
            expected_uid=expected_uid,
            expected_gid=expected_gid,
        ),
    }
    all_roots = {"release": release_resolved, **roots}
    items = tuple(all_roots.items())
    for index, (_, left) in enumerate(items):
        for _, right in items[index + 1 :]:
            if left == right or left in right.parents or right in left.parents:
                raise RuntimeAuditError("runtime_roots_not_separated")

    return {
        "release_read_only": True,
        "separated": True,
        "state_mode": "0700",
        "runtime_mode": "0700",
        "log_mode": "0700",
    }


def audit_token_file(
    token_file: Path,
    *,
    expected_uid: int,
    expected_gid: int,
) -> dict[str, Any]:
    """Validate token metadata without opening or disclosing the token."""

    metadata = _lstat_no_follow(token_file, field_name="token_file")
    if not stat.S_ISREG(metadata.st_mode):
        raise RuntimeAuditError("token_regular_file_required")
    if metadata.st_nlink != 1:
        raise RuntimeAuditError("token_link_count_invalid")
    mode = stat.S_IMODE(metadata.st_mode)
    if mode != 0o600:
        raise RuntimeAuditError("token_mode_invalid")
    if metadata.st_uid != expected_uid or metadata.st_gid != expected_gid:
        raise RuntimeAuditError("token_owner_invalid")
    return {
        "link_count": metadata.st_nlink,
        "mode": f"{mode:04o}",
        "owner_gid": metadata.st_gid,
        "owner_uid": metadata.st_uid,
        "regular": True,
    }


def audit_python_runtime(python_runtime: Path) -> dict[str, Any]:
    """Validate the immutable root-owned interpreter without executing it."""

    metadata = _lstat_no_follow(
        python_runtime,
        field_name="python_runtime",
    )
    if not stat.S_ISREG(metadata.st_mode):
        raise RuntimeAuditError("python_runtime_regular_file_required")
    if metadata.st_nlink != 1:
        raise RuntimeAuditError("python_runtime_link_count_invalid")
    mode = stat.S_IMODE(metadata.st_mode)
    if mode != 0o555:
        raise RuntimeAuditError("python_runtime_mode_invalid")
    if metadata.st_uid != 0 or metadata.st_gid != 0:
        raise RuntimeAuditError("python_runtime_owner_invalid")
    return {
        "link_count": metadata.st_nlink,
        "mode": f"{mode:04o}",
        "owner_gid": metadata.st_gid,
        "owner_uid": metadata.st_uid,
        "regular": True,
    }


def audit_python_dependencies() -> dict[str, str]:
    """Import and verify the frozen minimal third-party dependency set."""

    try:
        yaml_module = importlib.import_module("yaml")
    except (ImportError, ModuleNotFoundError) as exc:
        raise RuntimeAuditError("python_dependency_pyyaml_missing") from exc
    version = getattr(yaml_module, "__version__", None)
    if version != EXPECTED_PYYAML_VERSION:
        raise RuntimeAuditError("python_dependency_pyyaml_version_invalid")
    return {"pyyaml": version}


def _identity(user: str, group: str) -> tuple[int, int]:
    try:
        user_record = pwd.getpwnam(user)
        group_record = grp.getgrnam(group)
    except KeyError as exc:
        raise RuntimeAuditError("service_identity_missing") from exc
    if user_record.pw_gid != group_record.gr_gid:
        raise RuntimeAuditError("service_primary_group_mismatch")
    return user_record.pw_uid, group_record.gr_gid


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit the installed A-share observation worker metadata.",
    )
    parser.add_argument("--release-root", required=True, type=Path)
    parser.add_argument("--python-runtime", required=True, type=Path)
    parser.add_argument("--state-root", required=True, type=Path)
    parser.add_argument("--runtime-root", required=True, type=Path)
    parser.add_argument("--log-root", required=True, type=Path)
    parser.add_argument("--expected-user", required=True)
    parser.add_argument("--expected-group", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if os.environ.get("REAL_TRADING_ENABLED") != "false":
            raise RuntimeAuditError("real_trading_must_be_false")
        if os.environ.get("MARKETGRAPH_MODE") != "mg_off":
            raise RuntimeAuditError("marketgraph_mode_must_be_mg_off")
        expected_uid, expected_gid = _identity(
            args.expected_user,
            args.expected_group,
        )
        directories = audit_directory_layout(
            release_root=args.release_root,
            state_root=args.state_root,
            runtime_root=args.runtime_root,
            log_root=args.log_root,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
        )
        python_runtime = audit_python_runtime(args.python_runtime)
        python_dependencies = audit_python_dependencies()
        token = audit_token_file(
            TOKEN_FILE,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
        )
    except RuntimeAuditError as exc:
        print(
            json.dumps(
                {
                    "error": str(exc),
                    "ok": False,
                    "real_trading_enabled": False,
                },
                separators=(",", ":"),
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            {
                "directories": directories,
                "marketgraph_mode": "mg_off",
                "ok": True,
                "python_dependencies": python_dependencies,
                "python_runtime": python_runtime,
                "real_trading_enabled": False,
                "schema_version": 1,
                "token": token,
                "token_path": str(TOKEN_FILE),
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
