"""Explicit append-only RunBundle store for isolated simulated-day recovery.

The caller must provide a root directory.  There is no production default and
no fallback to a legacy runtime path.  Each compare-and-swap appends one
content-addressed bundle event under a process lock; reads verify the complete
hash chain before returning the latest immutable bundle.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional

from .day_loop import ConcurrentRunUpdate
from .run_bundle import RunBundle, RunBundleError, parse_run_bundle


_RUN_ID_RE = re.compile(r"^ashare-paper-day-[0-9a-f]{32}$")
_EVENT_FILE_RE = re.compile(r"^(?P<sequence>[0-9]{20})\.json$")
_TEMP_EVENT_FILE_RE = re.compile(r"^\.[0-9]{20}\.[0-9]+\.[0-9a-f]{32}\.tmp$")
_EVENT_KEYS = frozenset(
    {
        "schema_version",
        "run_id",
        "sequence",
        "previous_bundle_sha256",
        "bundle_sha256",
        "bundle",
        "event_sha256",
    }
)


class RunBundleStoreCorruption(RuntimeError):
    """Raised when a path, event or hash chain cannot be trusted."""


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise RunBundleStoreCorruption("run_bundle_store_noncanonical_value") from exc


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


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
            raise RunBundleStoreCorruption("run_bundle_store_path_unreadable") from exc
        if stat.S_ISLNK(mode):
            raise RunBundleStoreCorruption("run_bundle_store_symlink_forbidden")
        if current != absolute and not stat.S_ISDIR(mode):
            raise RunBundleStoreCorruption("run_bundle_store_parent_not_directory")


def _no_follow_flag() -> int:
    return getattr(os, "O_NOFOLLOW", 0)


def _directory_flag() -> int:
    return getattr(os, "O_DIRECTORY", 0)


class FileRunBundleStore:
    """Filesystem-backed compare-and-swap store with full-chain verification."""

    def __init__(self, root: Path | str) -> None:
        if not isinstance(root, (str, os.PathLike)) or not os.fspath(root):
            raise ValueError("run bundle store root must be explicitly configured")
        self.root = Path(os.path.abspath(os.fspath(root)))
        _assert_safe_path(self.root)

    @staticmethod
    def _validate_run_id(run_id: object) -> str:
        if not isinstance(run_id, str) or not _RUN_ID_RE.fullmatch(run_id):
            raise RunBundleStoreCorruption("run_bundle_store_run_id_invalid")
        return run_id

    def _paths(self, run_id: str) -> tuple[Path, Path, Path]:
        validated = self._validate_run_id(run_id)
        return (
            self.root / f"{validated}.jsonl",
            self.root / f"{validated}.events",
            self.root / f".{validated}.lock",
        )

    def _prepare_root(self) -> None:
        _assert_safe_path(self.root)
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise RunBundleStoreCorruption("run_bundle_store_root_unavailable") from exc
        _assert_safe_path(self.root)
        try:
            mode = self.root.stat().st_mode
        except OSError as exc:
            raise RunBundleStoreCorruption("run_bundle_store_root_unavailable") from exc
        if not stat.S_ISDIR(mode):
            raise RunBundleStoreCorruption("run_bundle_store_root_not_directory")

    @staticmethod
    def _fsync_directory(path: Path, *, reason: str) -> None:
        flags = os.O_RDONLY | _directory_flag() | _no_follow_flag()
        try:
            fd = os.open(os.fspath(path), flags)
        except OSError as exc:
            raise RunBundleStoreCorruption(reason) from exc
        try:
            if not stat.S_ISDIR(os.fstat(fd).st_mode):
                raise RunBundleStoreCorruption(reason)
            os.fsync(fd)
        except OSError as exc:
            raise RunBundleStoreCorruption(reason) from exc
        finally:
            os.close(fd)

    def _prepare_event_dir(self, event_dir: Path) -> None:
        _assert_safe_path(event_dir)
        created = False
        try:
            os.mkdir(os.fspath(event_dir), 0o700)
            created = True
        except FileExistsError:
            pass
        except OSError as exc:
            raise RunBundleStoreCorruption(
                "run_bundle_store_event_dir_unavailable"
            ) from exc
        _assert_safe_path(event_dir)
        try:
            mode = event_dir.lstat().st_mode
        except OSError as exc:
            raise RunBundleStoreCorruption(
                "run_bundle_store_event_dir_unavailable"
            ) from exc
        if not stat.S_ISDIR(mode):
            raise RunBundleStoreCorruption("run_bundle_store_event_dir_not_directory")
        if created:
            self._fsync_directory(
                self.root,
                reason="run_bundle_store_root_fsync_failed",
            )

    @contextmanager
    def _locked(
        self,
        run_id: str,
        *,
        exclusive: bool,
    ) -> Iterator[tuple[Path, Path]]:
        self._prepare_root()
        legacy_path, event_dir, lock_path = self._paths(run_id)
        _assert_safe_path(legacy_path)
        _assert_safe_path(event_dir)
        _assert_safe_path(lock_path)
        flags = os.O_RDWR | os.O_CREAT | _no_follow_flag()
        try:
            fd = os.open(os.fspath(lock_path), flags, 0o600)
        except OSError as exc:
            raise RunBundleStoreCorruption("run_bundle_store_lock_unavailable") from exc
        try:
            mode = os.fstat(fd).st_mode
            if not stat.S_ISREG(mode):
                raise RunBundleStoreCorruption("run_bundle_store_lock_not_regular")
            fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            _assert_safe_path(legacy_path)
            _assert_safe_path(event_dir)
            _assert_safe_path(lock_path)
            yield legacy_path, event_dir
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    @staticmethod
    def _event_without_hash(event: Mapping[str, Any]) -> dict[str, Any]:
        unsigned = dict(event)
        unsigned.pop("event_sha256", None)
        return unsigned

    @staticmethod
    def _parse_event_line(raw_line: str, *, location: str) -> dict[str, Any]:
        if not raw_line:
            raise RunBundleStoreCorruption(f"run_bundle_store_empty_event:{location}")
        try:
            raw = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise RunBundleStoreCorruption(
                f"run_bundle_store_malformed_json:{location}"
            ) from exc
        if not isinstance(raw, dict) or set(raw) != _EVENT_KEYS:
            raise RunBundleStoreCorruption(
                f"run_bundle_store_event_fields_invalid:{location}"
            )
        if _canonical_json(raw) != raw_line:
            raise RunBundleStoreCorruption(
                f"run_bundle_store_event_not_canonical:{location}"
            )
        return raw

    def _read_legacy_events(self, data_path: Path) -> list[dict[str, Any]]:
        _assert_safe_path(data_path)
        if not data_path.exists():
            return []
        flags = os.O_RDONLY | _no_follow_flag()
        try:
            fd = os.open(os.fspath(data_path), flags)
        except OSError as exc:
            raise RunBundleStoreCorruption("run_bundle_store_data_unavailable") from exc
        events: list[dict[str, Any]] = []
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise RunBundleStoreCorruption("run_bundle_store_data_not_regular")
            with os.fdopen(fd, "r", encoding="utf-8", closefd=False) as handle:
                for line_number, line in enumerate(handle, start=1):
                    raw_line = line.rstrip("\n")
                    events.append(
                        self._parse_event_line(
                            raw_line,
                            location=f"legacy:{line_number}",
                        )
                    )
        finally:
            os.close(fd)
        return events

    def _read_atomic_events(self, event_dir: Path) -> list[dict[str, Any]]:
        _assert_safe_path(event_dir)
        if not event_dir.exists():
            return []
        flags = os.O_RDONLY | _directory_flag() | _no_follow_flag()
        try:
            directory_fd = os.open(os.fspath(event_dir), flags)
        except OSError as exc:
            raise RunBundleStoreCorruption(
                "run_bundle_store_event_dir_unavailable"
            ) from exc
        events: list[dict[str, Any]] = []
        try:
            if not stat.S_ISDIR(os.fstat(directory_fd).st_mode):
                raise RunBundleStoreCorruption(
                    "run_bundle_store_event_dir_not_directory"
                )
            try:
                names = sorted(os.listdir(directory_fd))
            except OSError as exc:
                raise RunBundleStoreCorruption(
                    "run_bundle_store_event_dir_unavailable"
                ) from exc
            for name in names:
                if _TEMP_EVENT_FILE_RE.fullmatch(name):
                    continue
                match = _EVENT_FILE_RE.fullmatch(name)
                if match is None:
                    raise RunBundleStoreCorruption(
                        f"run_bundle_store_event_filename_invalid:{name}"
                    )
                try:
                    fd = os.open(
                        name,
                        os.O_RDONLY | _no_follow_flag(),
                        dir_fd=directory_fd,
                    )
                except OSError as exc:
                    raise RunBundleStoreCorruption(
                        f"run_bundle_store_event_unavailable:{name}"
                    ) from exc
                try:
                    if not stat.S_ISREG(os.fstat(fd).st_mode):
                        raise RunBundleStoreCorruption(
                            f"run_bundle_store_event_not_regular:{name}"
                        )
                    with os.fdopen(
                        fd,
                        "r",
                        encoding="utf-8",
                        closefd=False,
                    ) as handle:
                        encoded = handle.read()
                finally:
                    os.close(fd)
                if encoded.count("\n") != 1 or not encoded.endswith("\n"):
                    raise RunBundleStoreCorruption(
                        f"run_bundle_store_event_framing_invalid:{name}"
                    )
                event = self._parse_event_line(
                    encoded[:-1],
                    location=f"event:{name}",
                )
                if event.get("sequence") != int(match.group("sequence")):
                    raise RunBundleStoreCorruption(
                        f"run_bundle_store_event_filename_mismatch:{name}"
                    )
                events.append(event)
        finally:
            os.close(directory_fd)
        return events

    def _read_events(
        self,
        legacy_path: Path,
        event_dir: Path,
        *,
        run_id: str,
    ) -> list[dict[str, Any]]:
        events = self._read_legacy_events(legacy_path)
        events.extend(self._read_atomic_events(event_dir))
        self._validate_chain(events, run_id=run_id)
        return events

    def _publish_event(
        self,
        event_dir: Path,
        *,
        event: Mapping[str, Any],
    ) -> None:
        self._prepare_event_dir(event_dir)
        sequence = event.get("sequence")
        if not isinstance(sequence, int) or sequence < 0:
            raise RunBundleStoreCorruption("run_bundle_store_sequence_invalid")
        final_name = f"{sequence:020d}.json"
        temp_name = f".{sequence:020d}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        encoded = (_canonical_json(event) + "\n").encode("utf-8")
        flags = os.O_RDONLY | _directory_flag() | _no_follow_flag()
        try:
            directory_fd = os.open(os.fspath(event_dir), flags)
        except OSError as exc:
            raise RunBundleStoreCorruption(
                "run_bundle_store_event_dir_unavailable"
            ) from exc
        temp_created = False
        published = False
        try:
            try:
                temp_fd = os.open(
                    temp_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | _no_follow_flag(),
                    0o600,
                    dir_fd=directory_fd,
                )
                temp_created = True
            except OSError as exc:
                raise RunBundleStoreCorruption(
                    "run_bundle_store_temp_unavailable"
                ) from exc
            try:
                if not stat.S_ISREG(os.fstat(temp_fd).st_mode):
                    raise RunBundleStoreCorruption("run_bundle_store_temp_not_regular")
                try:
                    written = os.write(temp_fd, encoded)
                except OSError as exc:
                    raise RunBundleStoreCorruption(
                        "run_bundle_store_event_write_failed"
                    ) from exc
                if written != len(encoded):
                    raise RunBundleStoreCorruption("run_bundle_store_short_write")
                os.fchmod(temp_fd, 0o400)
                try:
                    os.fsync(temp_fd)
                except OSError as exc:
                    raise RunBundleStoreCorruption(
                        "run_bundle_store_event_fsync_failed"
                    ) from exc
            finally:
                os.close(temp_fd)
            try:
                os.link(
                    temp_name,
                    final_name,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                    follow_symlinks=False,
                )
                published = True
            except FileExistsError as exc:
                raise RunBundleStoreCorruption(
                    "run_bundle_store_event_already_exists"
                ) from exc
            except OSError as exc:
                raise RunBundleStoreCorruption(
                    "run_bundle_store_event_publish_failed"
                ) from exc
            try:
                os.unlink(temp_name, dir_fd=directory_fd)
                temp_created = False
            except OSError as exc:
                raise RunBundleStoreCorruption(
                    "run_bundle_store_temp_cleanup_failed"
                ) from exc
            try:
                os.fsync(directory_fd)
            except OSError as exc:
                raise RunBundleStoreCorruption(
                    "run_bundle_store_event_dir_fsync_failed"
                ) from exc
        finally:
            if temp_created:
                try:
                    os.unlink(temp_name, dir_fd=directory_fd)
                except OSError:
                    pass
            if published:
                try:
                    os.fsync(directory_fd)
                except OSError:
                    pass
            os.close(directory_fd)

    def _validate_chain(
        self,
        events: list[dict[str, Any]],
        *,
        run_id: str,
    ) -> None:
        previous_sha: Optional[str] = None
        previous_bundle: Optional[RunBundle] = None
        for index, event in enumerate(events):
            if event.get("schema_version") != 1:
                raise RunBundleStoreCorruption("run_bundle_store_schema_invalid")
            if event.get("run_id") != run_id or event.get("sequence") != index:
                raise RunBundleStoreCorruption("run_bundle_store_sequence_invalid")
            if event.get("previous_bundle_sha256") != previous_sha:
                raise RunBundleStoreCorruption("run_bundle_store_chain_broken")
            expected_event_sha = _sha256(
                _canonical_json(self._event_without_hash(event))
            )
            if event.get("event_sha256") != expected_event_sha:
                raise RunBundleStoreCorruption("run_bundle_store_event_hash_mismatch")
            try:
                bundle = parse_run_bundle(event.get("bundle"))
            except RunBundleError as exc:
                raise RunBundleStoreCorruption(
                    "run_bundle_store_bundle_invalid"
                ) from exc
            if bundle.run_id != run_id or event.get("bundle_sha256") != (
                bundle.bundle_sha256
            ):
                raise RunBundleStoreCorruption("run_bundle_store_bundle_hash_mismatch")
            if previous_bundle is not None:
                if (
                    bundle.context != previous_bundle.context
                    or bundle.components != previous_bundle.components
                    or bundle.stage_receipts[: len(previous_bundle.stage_receipts)]
                    != previous_bundle.stage_receipts
                    or len(bundle.stage_receipts)
                    != len(previous_bundle.stage_receipts) + 1
                ):
                    raise RunBundleStoreCorruption(
                        "run_bundle_store_non_append_only_transition"
                    )
            previous_sha = bundle.bundle_sha256
            previous_bundle = bundle

    def load(self, run_id: str) -> Optional[RunBundle]:
        validated = self._validate_run_id(run_id)
        _assert_safe_path(self.root)
        if not self.root.exists():
            return None
        with self._locked(validated, exclusive=False) as paths:
            legacy_path, event_dir = paths
            events = self._read_events(
                legacy_path,
                event_dir,
                run_id=validated,
            )
        if not events:
            return None
        try:
            return parse_run_bundle(events[-1]["bundle"])
        except RunBundleError as exc:  # pragma: no cover - chain already validates
            raise RunBundleStoreCorruption("run_bundle_store_bundle_invalid") from exc

    def compare_and_swap(
        self,
        *,
        run_id: str,
        expected_bundle_sha256: Optional[str],
        bundle: RunBundle,
    ) -> None:
        validated = self._validate_run_id(run_id)
        if not isinstance(bundle, RunBundle) or bundle.run_id != validated:
            raise ConcurrentRunUpdate("run_bundle_id_mismatch")
        if expected_bundle_sha256 is not None and not re.fullmatch(
            r"[0-9a-f]{64}", expected_bundle_sha256
        ):
            raise ConcurrentRunUpdate("expected_bundle_sha256_invalid")
        with self._locked(validated, exclusive=True) as paths:
            legacy_path, event_dir = paths
            events = self._read_events(
                legacy_path,
                event_dir,
                run_id=validated,
            )
            current_sha = events[-1]["bundle_sha256"] if events else None
            if current_sha == bundle.bundle_sha256:
                if events[-1]["previous_bundle_sha256"] == expected_bundle_sha256:
                    return
                raise ConcurrentRunUpdate("run_bundle_compare_and_swap_failed")
            if current_sha != expected_bundle_sha256:
                raise ConcurrentRunUpdate("run_bundle_compare_and_swap_failed")
            event: dict[str, Any] = {
                "schema_version": 1,
                "run_id": validated,
                "sequence": len(events),
                "previous_bundle_sha256": current_sha,
                "bundle_sha256": bundle.bundle_sha256,
                "bundle": bundle.to_dict(),
            }
            event["event_sha256"] = _sha256(_canonical_json(event))
            self._publish_event(event_dir, event=event)


__all__ = ["FileRunBundleStore", "RunBundleStoreCorruption"]
