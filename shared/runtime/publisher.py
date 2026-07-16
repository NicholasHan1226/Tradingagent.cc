"""Explicit local-candidate publisher for validated paper-day RunBundles."""

from __future__ import annotations

import errno
import fcntl
import json
import os
import secrets
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .run_bundle import RunBundle, RunBundleError, parse_run_bundle


class RunBundlePublishError(RuntimeError):
    """Raised when a local projection cannot be published safely."""


@dataclass(frozen=True)
class PublishedRunBundle:
    run_id: str
    bundle_sha256: str
    immutable_path: Path
    latest_path: Path
    idempotent: bool


class LocalRunBundlePublisher:
    """Publish a validated RunBundle under an explicitly supplied local root."""

    def __init__(self, root: Path | str) -> None:
        if not isinstance(root, (str, os.PathLike)) or not os.fspath(root):
            raise ValueError("run bundle publish root must be explicitly configured")
        self.root = Path(os.path.abspath(os.fspath(root)))
        _assert_safe_path(self.root)

    def publish(self, bundle: RunBundle) -> PublishedRunBundle:
        _assert_safe_path(self.root)
        if not isinstance(bundle, RunBundle):
            raise RunBundlePublishError("run_bundle_publish_requires_validated_bundle")
        try:
            validated = parse_run_bundle(bundle.to_dict())
        except RunBundleError as exc:
            raise RunBundlePublishError("run_bundle_publish_validation_failed") from exc

        encoded = _encode_projection(validated)
        _prepare_root(self.root)
        with _exclusive_publish_lock(self.root):
            return self._publish_locked(validated, encoded)

    def _publish_locked(
        self,
        validated: RunBundle,
        encoded: bytes,
    ) -> PublishedRunBundle:
        run_root = self.root / "runs" / validated.run_id
        _assert_safe_path(run_root)
        run_root.mkdir(parents=True, exist_ok=True)
        _assert_safe_path(run_root)
        _fsync_directory(self.root)
        _fsync_directory(self.root / "runs")
        _fsync_directory(run_root)
        immutable_path = run_root / f"{validated.bundle_sha256}.json"
        latest_path = self.root / "latest.json"
        immutable_bytes = _read_existing_regular(immutable_path)
        latest_bytes = _read_existing_regular(latest_path)
        if immutable_bytes is not None and immutable_bytes != encoded:
            raise RunBundlePublishError("immutable_projection_conflict")
        if latest_bytes is not None and latest_bytes != encoded:
            current = _parse_projection(latest_bytes)
            if current.run_id == validated.run_id:
                current_count = len(current.stage_receipts)
                next_count = len(validated.stage_receipts)
                if next_count < current_count:
                    raise RunBundlePublishError("latest_projection_rollback")
                if (
                    current.context != validated.context
                    or current.components != validated.components
                    or validated.stage_receipts[:current_count]
                    != current.stage_receipts
                ):
                    raise RunBundlePublishError("latest_projection_conflict")
            elif current.context.trade_date == validated.context.trade_date:
                raise RunBundlePublishError("latest_projection_competing_run")
            elif current.context.trade_date > validated.context.trade_date:
                raise RunBundlePublishError("latest_projection_date_rollback")
        if immutable_bytes == encoded and latest_bytes == encoded:
            return PublishedRunBundle(
                run_id=validated.run_id,
                bundle_sha256=validated.bundle_sha256,
                immutable_path=immutable_path,
                latest_path=latest_path,
                idempotent=True,
            )
        if immutable_bytes is None:
            _publish_immutable(run_root, immutable_path, encoded)
        temporary = _write_temporary(self.root, ".latest", encoded)
        try:
            os.replace(temporary, latest_path)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            _fsync_directory(self.root)
            raise RunBundlePublishError("latest_projection_replace_failed") from exc
        _fsync_directory(self.root)
        return PublishedRunBundle(
            run_id=validated.run_id,
            bundle_sha256=validated.bundle_sha256,
            immutable_path=immutable_path,
            latest_path=latest_path,
            idempotent=False,
        )


def _parse_projection(encoded: bytes) -> RunBundle:
    try:
        value = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunBundlePublishError("latest_projection_invalid") from exc
    if not isinstance(value, dict):
        raise RunBundlePublishError("latest_projection_invalid")
    metadata = value.pop("_projection", None)
    try:
        bundle = parse_run_bundle(value)
    except RunBundleError as exc:
        raise RunBundlePublishError("latest_projection_invalid") from exc
    if metadata != {
        "authority": "non_authority",
        "bundle_sha256": bundle.bundle_sha256,
        "environment": "local_candidate",
        "production_verified": False,
        "record_type": "run_bundle_projection",
        "schema_version": 1,
    }:
        raise RunBundlePublishError("latest_projection_invalid")
    if _encode_projection(bundle) != encoded:
        raise RunBundlePublishError("latest_projection_invalid")
    return bundle


def _encode_projection(bundle: RunBundle) -> bytes:
    projection = bundle.to_dict()
    projection["_projection"] = {
        "authority": "non_authority",
        "bundle_sha256": bundle.bundle_sha256,
        "environment": "local_candidate",
        "production_verified": False,
        "record_type": "run_bundle_projection",
        "schema_version": 1,
    }
    return (
        json.dumps(
            projection,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _assert_safe_path(path: Path) -> None:
    absolute = Path(os.path.abspath(os.fspath(path)))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise RunBundlePublishError("run_bundle_publish_path_unreadable") from exc
        if stat.S_ISLNK(mode):
            raise RunBundlePublishError("run_bundle_publish_symlink_forbidden")
        if current != absolute and not stat.S_ISDIR(mode):
            raise RunBundlePublishError("run_bundle_publish_parent_not_directory")


def _prepare_root(root: Path) -> None:
    _assert_safe_path(root)
    try:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        raise RunBundlePublishError("run_bundle_publish_root_unavailable") from exc
    _assert_safe_path(root)
    try:
        details = root.stat()
    except OSError as exc:
        raise RunBundlePublishError("run_bundle_publish_root_unavailable") from exc
    if not stat.S_ISDIR(details.st_mode):
        raise RunBundlePublishError("run_bundle_publish_root_not_directory")


@contextmanager
def _exclusive_publish_lock(root: Path) -> Iterator[None]:
    lock_path = root / ".publisher.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(os.fspath(lock_path), flags, 0o600)
    except OSError as exc:
        raise RunBundlePublishError("run_bundle_publish_lock_unavailable") from exc
    details = os.fstat(fd)
    if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
        os.close(fd)
        raise RunBundlePublishError("run_bundle_publish_lock_unavailable")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
    except OSError as exc:
        os.close(fd)
        raise RunBundlePublishError("run_bundle_publish_lock_unavailable") from exc
    try:
        _assert_safe_path(root)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _read_existing_regular(path: Path) -> bytes | None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(os.fspath(path), flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise RunBundlePublishError("run_bundle_publish_symlink_forbidden") from exc
        raise RunBundlePublishError("run_bundle_publish_path_unreadable") from exc
    try:
        details = os.fstat(fd)
        if not stat.S_ISREG(details.st_mode):
            raise RunBundlePublishError("run_bundle_publish_not_regular")
        if details.st_nlink != 1:
            raise RunBundlePublishError("run_bundle_publish_hardlink_forbidden")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    except OSError as exc:
        raise RunBundlePublishError("run_bundle_publish_path_unreadable") from exc
    finally:
        os.close(fd)


def _write_temporary(directory: Path, prefix: str, encoded: bytes) -> Path:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    for _ in range(16):
        path = directory / (f"{prefix}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
        try:
            fd = os.open(os.fspath(path), flags, 0o600)
        except FileExistsError:
            continue
        except OSError as exc:
            raise RunBundlePublishError("run_bundle_publish_temp_unavailable") from exc
        failure: RunBundlePublishError | None = None
        cause: OSError | None = None
        try:
            details = os.fstat(fd)
            if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
                failure = RunBundlePublishError("run_bundle_publish_temp_not_regular")
            else:
                view = memoryview(encoded)
                written = 0
                try:
                    while written < len(encoded):
                        count = os.write(fd, view[written:])
                        if count <= 0:
                            failure = RunBundlePublishError(
                                "run_bundle_publish_short_write"
                            )
                            break
                        written += count
                except OSError as exc:
                    cause = exc
                    failure = RunBundlePublishError(
                        "run_bundle_publish_file_write_failed"
                    )
                if failure is None:
                    try:
                        os.fsync(fd)
                    except OSError as exc:
                        cause = exc
                        failure = RunBundlePublishError(
                            "run_bundle_publish_file_fsync_failed"
                        )
        finally:
            os.close(fd)
        if failure is not None:
            path.unlink(missing_ok=True)
            if cause is not None:
                raise failure from cause
            raise failure
        return path
    raise RunBundlePublishError("run_bundle_publish_temp_conflict")


def _publish_immutable(
    run_root: Path,
    immutable_path: Path,
    encoded: bytes,
) -> None:
    temporary = _write_temporary(run_root, ".projection", encoded)
    try:
        try:
            os.link(temporary, immutable_path, follow_symlinks=False)
        except FileExistsError:
            existing = _read_existing_regular(immutable_path)
            if existing != encoded:
                raise RunBundlePublishError("immutable_projection_conflict")
        except OSError as exc:
            raise RunBundlePublishError("immutable_projection_publish_failed") from exc
    finally:
        temporary.unlink(missing_ok=True)
        _fsync_directory(run_root)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(os.fspath(path), flags)
    except OSError as exc:
        raise RunBundlePublishError("run_bundle_publish_directory_unavailable") from exc
    try:
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise RunBundlePublishError("run_bundle_publish_not_directory")
        os.fsync(fd)
    except OSError as exc:
        raise RunBundlePublishError(
            "run_bundle_publish_directory_fsync_failed"
        ) from exc
    finally:
        os.close(fd)


__all__ = [
    "LocalRunBundlePublisher",
    "PublishedRunBundle",
    "RunBundlePublishError",
]
