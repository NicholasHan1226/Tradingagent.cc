#!/usr/bin/env python3
"""Content-addressed, generation-atomic A-share sample projections."""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import errno
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Any, Callable, Iterator, Mapping, Optional


CURRENT_MANIFEST = "projection_current.json"
GENERATIONS_DIR = "projection_generations"
GENERATION_MANIFEST = "generation_manifest.json"
AUDIT_LOG = "projection_generation_audit.jsonl"
PUBLISH_LOCK = ".projection_publish.lock"

PROJECTION_FILENAMES = (
    "sample_kpi_latest.json",
    "evolution_decision_latest.json",
    "market_maturity_latest.json",
)
PROJECTION_LOG_FILENAMES = {
    "sample_kpi_latest.json": "sample_kpi_log.jsonl",
    "evolution_decision_latest.json": "evolution_decision_log.jsonl",
    "market_maturity_latest.json": "market_maturity_log.jsonl",
}


class ProjectionGenerationError(RuntimeError):
    """A projection generation was incomplete, unsafe, or inconsistent."""


def _canonical_bytes(value: Mapping[str, Any], *, pretty: bool = False) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                indent=2 if pretty else None,
                sort_keys=True,
                separators=None if pretty else (",", ":"),
                allow_nan=False,
                default=str,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProjectionGenerationError("projection_not_canonical_json") from exc


def _is_sha256(value: Any) -> bool:
    raw = str(value or "").strip().lower()
    return len(raw) == 64 and all(character in "0123456789abcdef" for character in raw)


def compute_generation_id(
    projection_input_sha256: str,
    projection_sha256: Mapping[str, Any],
) -> str:
    """Return the canonical content-addressed A-share projection generation ID.

    Cross-language contract: SHA-256 the UTF-8 encoding of compact, recursively
    key-sorted JSON containing exactly ``projection_input_sha256`` and the
    three-file ``projection_sha256`` map, followed by one LF byte.  Publishers
    and every active reader must recompute this identity rather than trusting a
    directory, manifest, or pointer supplied generation ID.
    """

    raw_input = str(projection_input_sha256 or "").strip()
    normalized_input = raw_input.lower()
    if raw_input != normalized_input or not _is_sha256(normalized_input):
        raise ProjectionGenerationError("projection_input_sha256_invalid")
    if not isinstance(projection_sha256, Mapping) or set(projection_sha256) != set(
        PROJECTION_FILENAMES
    ):
        raise ProjectionGenerationError("projection_sha_map_missing")
    normalized_shas: dict[str, str] = {}
    for filename in PROJECTION_FILENAMES:
        raw_digest = str(projection_sha256.get(filename) or "").strip()
        digest = raw_digest.lower()
        if raw_digest != digest or not _is_sha256(digest):
            raise ProjectionGenerationError("projection_sha_map_invalid:%s" % filename)
        normalized_shas[filename] = digest
    generation_identity = {
        "projection_input_sha256": normalized_input,
        "projection_sha256": normalized_shas,
    }
    return (
        "ashare-sample-projection-"
        + sha256(_canonical_bytes(generation_identity)).hexdigest()
    )


def _check_no_symlink(path: Path) -> None:
    current = path.absolute()
    while True:
        if os.path.lexists(str(current)):
            try:
                mode = os.lstat(str(current)).st_mode
            except OSError as exc:
                raise ProjectionGenerationError(
                    "projection_path_inspection_failed"
                ) from exc
            if stat.S_ISLNK(mode):
                raise ProjectionGenerationError(
                    "projection_symlink_not_allowed:%s" % current
                )
        if current == current.parent:
            break
        current = current.parent


def _fsync_directory(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write(
    path: Path,
    payload: Mapping[str, Any],
    *,
    _pre_replace: Optional[Callable[[], None]] = None,
) -> None:
    _check_no_symlink(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=".%s." % path.name,
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(_canonical_bytes(payload, pretty=True))
            handle.flush()
            os.fsync(handle.fileno())
        if _pre_replace is not None:
            _pre_replace()
        os.replace(temporary_name, str(path))
        _fsync_directory(path.parent)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _append_jsonl(path: Path, payload: Mapping[str, Any]) -> None:
    _check_no_symlink(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(str(path), flags, 0o600)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise ProjectionGenerationError("projection_log_not_regular")
        if opened.st_nlink != 1:
            raise ProjectionGenerationError("projection_log_hardlink_not_allowed")
        data = _canonical_bytes(payload)
        written = 0
        while written < len(data):
            count = os.write(fd, data[written:])
            if count <= 0:
                raise ProjectionGenerationError("projection_log_short_write")
            written += count
        os.fsync(fd)
    finally:
        os.close(fd)


def _read_json_with_bytes(path: Path) -> tuple[dict[str, Any], bytes]:
    _check_no_symlink(path)
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (
        FileNotFoundError,
        json.JSONDecodeError,
        OSError,
        UnicodeDecodeError,
    ) as exc:
        raise ProjectionGenerationError("projection_json_unreadable:%s" % path) from exc
    if not isinstance(value, dict):
        raise ProjectionGenerationError("projection_json_not_object:%s" % path)
    return value, raw


def _read_json(path: Path) -> dict[str, Any]:
    return _read_json_with_bytes(path)[0]


def _read_regular_file_snapshot(
    path: Path, *, require_immutable: bool
) -> tuple[bytes, os.stat_result]:
    """Read one regular, single-link file and prove its inode stayed stable."""

    _check_no_symlink(path)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(str(path), flags)
    except OSError as exc:
        raise ProjectionGenerationError(
            "projection_generation_file_unreadable:%s" % path.name
        ) from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise ProjectionGenerationError(
                "projection_generation_file_not_regular:%s" % path.name
            )
        if before.st_nlink != 1:
            raise ProjectionGenerationError(
                "projection_generation_file_hardlink_not_allowed:%s" % path.name
            )
        if require_immutable and before.st_mode & 0o222:
            raise ProjectionGenerationError(
                "projection_generation_file_not_immutable:%s" % path.name
            )
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(fd)
        identity_fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if any(
            getattr(before, field) != getattr(after, field) for field in identity_fields
        ):
            raise ProjectionGenerationError(
                "projection_generation_file_changed_during_validation:%s" % path.name
            )
        try:
            path_state = os.lstat(str(path))
        except OSError as exc:
            raise ProjectionGenerationError(
                "projection_generation_file_unreadable:%s" % path.name
            ) from exc
        if (
            path_state.st_dev != after.st_dev
            or path_state.st_ino != after.st_ino
            or path_state.st_nlink != 1
        ):
            raise ProjectionGenerationError(
                "projection_generation_file_replaced_during_validation:%s" % path.name
            )
        return b"".join(chunks), after
    finally:
        os.close(fd)


def _file_identity(path: Path, *, raw: bytes, state: os.stat_result) -> dict[str, Any]:
    """Return the complete path/inode/content identity for one validated file."""

    return {
        "path": str(path.absolute()),
        "st_dev": state.st_dev,
        "st_ino": state.st_ino,
        "st_mode": state.st_mode,
        "st_nlink": state.st_nlink,
        "st_size": state.st_size,
        "st_mtime_ns": state.st_mtime_ns,
        "st_ctime_ns": state.st_ctime_ns,
        "content_sha256": sha256(raw).hexdigest(),
    }


def _capture_compatibility_file_identity(path: Path) -> dict[str, Any]:
    """Seal the post-write identity of one compatibility mirror or log."""

    raw, state = _read_regular_file_snapshot(path, require_immutable=False)
    return _file_identity(path, raw=raw, state=state)


def _validate_compatibility_file_identity(
    path: Path, expected: Mapping[str, Any]
) -> None:
    """Re-read a compatibility file and reject any post-write identity drift."""

    actual = _capture_compatibility_file_identity(path)
    if actual != dict(expected):
        raise ProjectionGenerationError(
            "projection_compatibility_file_identity_changed:%s" % path.name
        )


def _seal_generation_directory(path: Path) -> None:
    """Make a validated generation read-only before it can be published."""

    expected = set(PROJECTION_FILENAMES) | {GENERATION_MANIFEST}
    entries = {entry.name: entry for entry in path.iterdir()}
    if set(entries) != expected:
        raise ProjectionGenerationError("projection_generation_file_set_mismatch")
    for name, entry in entries.items():
        try:
            state = os.lstat(str(entry))
        except OSError as exc:
            raise ProjectionGenerationError(
                "projection_generation_file_unreadable:%s" % name
            ) from exc
        if not stat.S_ISREG(state.st_mode) or state.st_nlink != 1:
            raise ProjectionGenerationError(
                "projection_generation_file_not_sealable:%s" % name
            )
        os.chmod(str(entry), 0o444)
    os.chmod(str(path), 0o555)
    _fsync_directory(path)


@contextmanager
def _projection_publish_lock(review_dir: Path) -> Iterator[None]:
    """Serialize every generation/mirror/current publication in one root."""

    lock_path = review_dir / PUBLISH_LOCK
    _check_no_symlink(lock_path)
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(str(lock_path), flags, 0o600)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.EMLINK}:
            raise ProjectionGenerationError("projection_publish_lock_symlink") from exc
        raise
    try:
        state = os.fstat(fd)
        if not stat.S_ISREG(state.st_mode) or state.st_nlink != 1:
            raise ProjectionGenerationError("projection_publish_lock_not_regular")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _validate_projection_set(
    projections: Mapping[str, Mapping[str, Any]], projection_input_sha256: str
) -> None:
    if set(projections) != set(PROJECTION_FILENAMES):
        raise ProjectionGenerationError("projection_file_set_mismatch")
    if not _is_sha256(projection_input_sha256):
        raise ProjectionGenerationError("projection_input_sha256_invalid")
    for filename, payload in projections.items():
        if not isinstance(payload, Mapping):
            raise ProjectionGenerationError("projection_not_mapping:%s" % filename)
        if payload.get("projection_input_sha256") != projection_input_sha256:
            raise ProjectionGenerationError(
                "projection_input_sha256_mismatch:%s" % filename
            )
        if payload.get("real_trading_enabled") is not False:
            raise ProjectionGenerationError("projection_not_sim_only:%s" % filename)
        for field in (
            "live_execution_enabled",
            "automatic_promotion_enabled",
            "automatic_risk_expansion_enabled",
        ):
            if payload.get(field) is not False:
                raise ProjectionGenerationError(
                    "projection_safety_field_not_false:%s:%s" % (filename, field)
                )
        if (
            filename != "sample_kpi_latest.json"
            and payload.get("live_transition_authorized") is not False
        ):
            raise ProjectionGenerationError(
                "projection_safety_field_not_false:%s:live_transition_authorized"
                % filename
            )


def _validate_generation_directory(
    generation_path: Path,
    *,
    expected_generation_id: str,
    expected_projection_input_sha256: str,
    expected_projection_sha256: Mapping[str, Any],
    expected_manifest_sha256: Optional[str] = None,
    expected_manifest: Optional[Mapping[str, Any]] = None,
    require_immutable: bool = True,
) -> dict[str, Any]:
    """Validate one immutable generation before it can become current.

    Publisher reuse and active readers share this exact validator.  The file
    set must be exact, every entry must be a regular non-symlink file, and all
    content hashes, JSON payloads, generation identity, input lineage, and
    sim-only safety fields are revalidated from bytes.
    """

    _check_no_symlink(generation_path)
    try:
        generation_state = os.lstat(str(generation_path))
    except OSError as exc:
        raise ProjectionGenerationError(
            "projection_generation_directory_unreadable"
        ) from exc
    if not stat.S_ISDIR(generation_state.st_mode):
        raise ProjectionGenerationError("projection_generation_not_directory")
    if require_immutable and generation_state.st_mode & 0o222:
        raise ProjectionGenerationError("projection_generation_directory_not_immutable")

    expected_files = set(PROJECTION_FILENAMES) | {GENERATION_MANIFEST}
    try:
        entries = {entry.name: entry for entry in generation_path.iterdir()}
    except OSError as exc:
        raise ProjectionGenerationError(
            "projection_generation_directory_unreadable"
        ) from exc
    if set(entries) != expected_files:
        raise ProjectionGenerationError("projection_generation_file_set_mismatch")
    generation_file_identities: dict[str, dict[str, Any]] = {}
    manifest_bytes, manifest_state = _read_regular_file_snapshot(
        entries[GENERATION_MANIFEST], require_immutable=require_immutable
    )
    generation_file_identities[GENERATION_MANIFEST] = _file_identity(
        entries[GENERATION_MANIFEST], raw=manifest_bytes, state=manifest_state
    )
    try:
        manifest = json.loads(manifest_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ProjectionGenerationError(
            "projection_generation_file_invalid:%s" % GENERATION_MANIFEST
        ) from exc
    if not isinstance(manifest, dict):
        raise ProjectionGenerationError("projection_generation_manifest_not_object")
    manifest_sha256 = sha256(manifest_bytes).hexdigest()
    if (
        expected_manifest_sha256 is not None
        and manifest_sha256 != expected_manifest_sha256
    ):
        raise ProjectionGenerationError("projection_generation_manifest_hash_mismatch")
    if expected_manifest is not None and manifest != dict(expected_manifest):
        raise ProjectionGenerationError("projection_generation_hash_collision")
    if (
        manifest.get("schema_version") != 1
        or manifest.get("generation_id") != expected_generation_id
        or manifest.get("projection_input_sha256") != expected_projection_input_sha256
        or manifest.get("projection_sha256") != dict(expected_projection_sha256)
        or manifest.get("real_trading_enabled") is not False
    ):
        raise ProjectionGenerationError("projection_generation_manifest_mismatch")
    if (
        compute_generation_id(
            expected_projection_input_sha256, expected_projection_sha256
        )
        != expected_generation_id
    ):
        raise ProjectionGenerationError("projection_generation_id_mismatch")

    projections: dict[str, dict[str, Any]] = {}
    for filename in PROJECTION_FILENAMES:
        path = entries[filename]
        raw, file_state = _read_regular_file_snapshot(
            path, require_immutable=require_immutable
        )
        generation_file_identities[filename] = _file_identity(
            path, raw=raw, state=file_state
        )
        if sha256(raw).hexdigest() != expected_projection_sha256.get(filename):
            raise ProjectionGenerationError(
                "projection_generation_file_hash_mismatch:%s" % filename
            )
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ProjectionGenerationError(
                "projection_generation_file_invalid:%s" % filename
            ) from exc
        if not isinstance(payload, dict):
            raise ProjectionGenerationError(
                "projection_generation_projection_mismatch:%s" % filename
            )
        projections[filename] = payload
    _validate_projection_set(projections, expected_projection_input_sha256)
    try:
        final_entries = {entry.name for entry in generation_path.iterdir()}
        final_generation_state = os.lstat(str(generation_path))
    except OSError as exc:
        raise ProjectionGenerationError(
            "projection_generation_directory_changed_during_validation"
        ) from exc
    if (
        final_entries != expected_files
        or final_generation_state.st_dev != generation_state.st_dev
        or final_generation_state.st_ino != generation_state.st_ino
        or final_generation_state.st_mode != generation_state.st_mode
    ):
        raise ProjectionGenerationError(
            "projection_generation_directory_changed_during_validation"
        )
    return {
        "generation_manifest": manifest,
        "generation_manifest_bytes": manifest_bytes,
        "generation_manifest_sha256": manifest_sha256,
        "projections": projections,
        "generation_directory_identity": {
            "path": str(generation_path.absolute()),
            "st_dev": generation_state.st_dev,
            "st_ino": generation_state.st_ino,
            "st_mode": generation_state.st_mode,
            "st_nlink": generation_state.st_nlink,
            "st_size": generation_state.st_size,
            "st_mtime_ns": generation_state.st_mtime_ns,
            "st_ctime_ns": generation_state.st_ctime_ns,
        },
        "generation_file_identities": generation_file_identities,
    }


def _remove_staging(path: Path) -> None:
    if not path.exists():
        return
    _check_no_symlink(path)
    os.chmod(str(path), 0o700)
    for child in path.iterdir():
        if child.is_dir() or child.is_symlink():
            raise ProjectionGenerationError("unexpected_projection_staging_entry")
        os.chmod(str(child), 0o600)
        child.unlink()
    path.rmdir()


def publish_projection_generation(
    *,
    review_dir: Path | str,
    projections: Mapping[str, Mapping[str, Any]],
    projection_input_sha256: str,
    run_id: str,
    generated_at: str,
    _fail_after_file_count: Optional[int] = None,
    _before_pointer_swap_hook: Optional[Callable[[Path], None]] = None,
) -> dict[str, Any]:
    """Publish three projections with one final atomic current-pointer swap.

    Generation files and compatibility mirrors are complete before the pointer
    changes.  Consumers of ``projection_current.json`` therefore observe either
    the previous complete generation or the new complete generation.
    ``_fail_after_file_count`` is a deterministic test-only crash seam.
    """

    _validate_projection_set(projections, projection_input_sha256)
    root = Path(review_dir).absolute()
    _check_no_symlink(root)
    root.mkdir(parents=True, exist_ok=True)
    with _projection_publish_lock(root):
        return _publish_projection_generation_locked(
            review_dir=root,
            projections=projections,
            projection_input_sha256=projection_input_sha256,
            run_id=run_id,
            generated_at=generated_at,
            _fail_after_file_count=_fail_after_file_count,
            _before_pointer_swap_hook=_before_pointer_swap_hook,
        )


def _publish_projection_generation_locked(
    *,
    review_dir: Path,
    projections: Mapping[str, Mapping[str, Any]],
    projection_input_sha256: str,
    run_id: str,
    generated_at: str,
    _fail_after_file_count: Optional[int],
    _before_pointer_swap_hook: Optional[Callable[[Path], None]],
) -> dict[str, Any]:
    """Publish while the root-wide exclusive projection lock is held."""

    _validate_projection_set(projections, projection_input_sha256)
    root = Path(review_dir).absolute()
    _check_no_symlink(root)
    generations_root = root / GENERATIONS_DIR
    _check_no_symlink(generations_root)
    generations_root.mkdir(parents=True, exist_ok=True)

    projection_bytes = {
        filename: _canonical_bytes(payload, pretty=True)
        for filename, payload in projections.items()
    }
    projection_sha256 = {
        filename: sha256(payload).hexdigest()
        for filename, payload in projection_bytes.items()
    }
    generation_id = compute_generation_id(projection_input_sha256, projection_sha256)
    generation_path = generations_root / generation_id
    generation_manifest = {
        "schema_version": 1,
        "generation_id": generation_id,
        "projection_input_sha256": projection_input_sha256,
        "projection_sha256": projection_sha256,
        "run_id": str(run_id),
        "generated_at": str(generated_at),
        "real_trading_enabled": False,
    }
    generation_manifest_bytes = _canonical_bytes(generation_manifest, pretty=True)
    generation_manifest_sha256 = sha256(generation_manifest_bytes).hexdigest()

    if not generation_path.exists():
        staging = generations_root / (".%s.%d.tmp" % (generation_id, os.getpid()))
        _check_no_symlink(staging)
        if staging.exists():
            raise ProjectionGenerationError("projection_staging_path_exists")
        staging.mkdir(mode=0o700)
        written_count = 0
        try:
            for filename in PROJECTION_FILENAMES:
                path = staging / filename
                with path.open("xb") as handle:
                    handle.write(projection_bytes[filename])
                    handle.flush()
                    os.fsync(handle.fileno())
                written_count += 1
                if _fail_after_file_count == written_count:
                    raise ProjectionGenerationError(
                        "injected_projection_publish_failure"
                    )
            manifest_path = staging / GENERATION_MANIFEST
            with manifest_path.open("xb") as handle:
                handle.write(generation_manifest_bytes)
                handle.flush()
                os.fsync(handle.fileno())
            _fsync_directory(staging)
            _validate_generation_directory(
                staging,
                expected_generation_id=generation_id,
                expected_projection_input_sha256=projection_input_sha256,
                expected_projection_sha256=projection_sha256,
                expected_manifest_sha256=generation_manifest_sha256,
                expected_manifest=generation_manifest,
                require_immutable=False,
            )
            _seal_generation_directory(staging)
            _validate_generation_directory(
                staging,
                expected_generation_id=generation_id,
                expected_projection_input_sha256=projection_input_sha256,
                expected_projection_sha256=projection_sha256,
                expected_manifest_sha256=generation_manifest_sha256,
                expected_manifest=generation_manifest,
            )
            os.replace(str(staging), str(generation_path))
            _fsync_directory(generations_root)
        except Exception:
            _remove_staging(staging)
            raise
    else:
        _validate_generation_directory(
            generation_path,
            expected_generation_id=generation_id,
            expected_projection_input_sha256=projection_input_sha256,
            expected_projection_sha256=projection_sha256,
            expected_manifest_sha256=generation_manifest_sha256,
            expected_manifest=generation_manifest,
            require_immutable=False,
        )
        _seal_generation_directory(generation_path)

    # Validate newly created and idempotently reused generations before any
    # compatibility mirror or current-pointer write.  A manifest-only,
    # incomplete, extra-file, symlinked, or hash-mismatched directory is a
    # collision/corruption error and cannot change the previous current bytes.
    _validate_generation_directory(
        generation_path,
        expected_generation_id=generation_id,
        expected_projection_input_sha256=projection_input_sha256,
        expected_projection_sha256=projection_sha256,
        expected_manifest_sha256=generation_manifest_sha256,
        expected_manifest=generation_manifest,
    )

    # Backward-compatible standalone files/logs are prepared before the single
    # canonical pointer swap.  Updated readers use the pointer as the transaction
    # boundary and validate each content hash before returning any projection.
    compatibility_paths: dict[str, Path] = {}
    for filename in PROJECTION_FILENAMES:
        mirror_path = root / filename
        log_path = root / PROJECTION_LOG_FILENAMES[filename]
        _atomic_write(mirror_path, projections[filename])
        _append_jsonl(log_path, projections[filename])
        compatibility_paths[filename] = mirror_path
        compatibility_paths[PROJECTION_LOG_FILENAMES[filename]] = log_path
    compatibility_identities = {
        name: _capture_compatibility_file_identity(path)
        for name, path in compatibility_paths.items()
    }

    current_manifest = {
        "schema_version": 1,
        "generation_id": generation_id,
        "generation_path": "%s/%s" % (GENERATIONS_DIR, generation_id),
        "generation_manifest": GENERATION_MANIFEST,
        "generation_manifest_sha256": generation_manifest_sha256,
        "projection_input_sha256": projection_input_sha256,
        "projection_sha256": projection_sha256,
        "run_id": str(run_id),
        "generated_at": str(generated_at),
        "published_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        "real_trading_enabled": False,
    }
    if _fail_after_file_count == len(PROJECTION_FILENAMES) + 1:
        raise ProjectionGenerationError("injected_projection_pointer_failure")
    # Revalidate after mirror writes and immediately before the atomic pointer
    # swap to close the publisher's validation/use window for the generation.
    final_generation_validation = _validate_generation_directory(
        generation_path,
        expected_generation_id=generation_id,
        expected_projection_input_sha256=projection_input_sha256,
        expected_projection_sha256=projection_sha256,
        expected_manifest_sha256=generation_manifest_sha256,
        expected_manifest=generation_manifest,
    )
    if _before_pointer_swap_hook is not None:
        _before_pointer_swap_hook(generation_path)

    # The pointer's temporary bytes are fsynced first.  The generation is then
    # re-read from regular single-link immutable inodes inside the same publish
    # lock, immediately before the one os.replace that changes canonical current.
    def validate_pointer_target() -> None:
        pointer_generation_validation = _validate_generation_directory(
            generation_path,
            expected_generation_id=generation_id,
            expected_projection_input_sha256=projection_input_sha256,
            expected_projection_sha256=projection_sha256,
            expected_manifest_sha256=generation_manifest_sha256,
            expected_manifest=generation_manifest,
        )
        if (
            pointer_generation_validation["generation_directory_identity"]
            != final_generation_validation["generation_directory_identity"]
            or pointer_generation_validation["generation_file_identities"]
            != final_generation_validation["generation_file_identities"]
        ):
            raise ProjectionGenerationError(
                "projection_generation_identity_changed_before_pointer_swap"
            )
        for name, path in compatibility_paths.items():
            _validate_compatibility_file_identity(path, compatibility_identities[name])

    _atomic_write(
        root / CURRENT_MANIFEST,
        current_manifest,
        _pre_replace=validate_pointer_target,
    )
    return deepcopy(current_manifest)


def load_current_projection_set(review_dir: Path | str) -> dict[str, Any]:
    """Load and hash-verify the complete generation named by the current pointer."""

    root = Path(review_dir).absolute()
    _check_no_symlink(root)
    current = _read_json(root / CURRENT_MANIFEST)
    generation_id = str(current.get("generation_id") or "").strip()
    generation_path_raw = str(current.get("generation_path") or "").strip()
    generation_manifest_sha256 = str(
        current.get("generation_manifest_sha256") or ""
    ).strip()
    generation_digest = generation_id.removeprefix("ashare-sample-projection-")
    if (
        current.get("schema_version") != 1
        or not generation_id.startswith("ashare-sample-projection-")
        or not _is_sha256(generation_digest)
        or not _is_sha256(generation_manifest_sha256)
        or current.get("generation_manifest") != GENERATION_MANIFEST
        or current.get("real_trading_enabled") is not False
        or generation_path_raw
        != "%s/%s"
        % (
            GENERATIONS_DIR,
            generation_id,
        )
    ):
        raise ProjectionGenerationError("projection_current_manifest_invalid")
    generation_path = root / generation_path_raw
    expected_input = str(current.get("projection_input_sha256") or "")
    if not _is_sha256(expected_input):
        raise ProjectionGenerationError("projection_generation_input_mismatch")
    expected_shas = current.get("projection_sha256")
    if (
        not isinstance(expected_shas, Mapping)
        or set(expected_shas) != set(PROJECTION_FILENAMES)
        or any(not _is_sha256(expected_shas.get(name)) for name in PROJECTION_FILENAMES)
    ):
        raise ProjectionGenerationError("projection_sha_map_missing")
    validated = _validate_generation_directory(
        generation_path,
        expected_generation_id=generation_id,
        expected_projection_input_sha256=expected_input,
        expected_projection_sha256=expected_shas,
        expected_manifest_sha256=generation_manifest_sha256,
    )
    manifest = validated["generation_manifest"]
    if manifest.get("run_id") != current.get("run_id") or manifest.get(
        "generated_at"
    ) != current.get("generated_at"):
        raise ProjectionGenerationError("projection_generation_manifest_mismatch")
    return {
        "current_manifest": current,
        "generation_manifest": manifest,
        "projections": validated["projections"],
    }


@contextmanager
def _audit_lock(review_dir: Path) -> Iterator[None]:
    lock_path = review_dir / (".%s.lock" % AUDIT_LOG)
    _check_no_symlink(lock_path)
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(str(lock_path), flags, 0o600)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.EMLINK}:
            raise ProjectionGenerationError("projection_audit_lock_symlink") from exc
        raise
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def record_projection_audit(
    *,
    review_dir: Path | str,
    generation_id: str,
    status: str,
    reason: str,
    superseded_by_generation_id: Optional[str] = None,
    recorded_at: Optional[str] = None,
) -> dict[str, Any]:
    """Append an idempotent invalid/superseded audit fact without rewriting history."""

    normalized_status = str(status or "").strip().lower()
    if normalized_status not in {"invalid", "superseded"}:
        raise ValueError("projection audit status must be invalid or superseded")
    if not str(generation_id or "").strip() or not str(reason or "").strip():
        raise ValueError("generation_id and reason are required")
    payload = {
        "event_type": "projection_generation_audit",
        "generation_id": str(generation_id),
        "status": normalized_status,
        "reason": str(reason),
        "superseded_by_generation_id": (
            str(superseded_by_generation_id)
            if superseded_by_generation_id is not None
            else None
        ),
        "recorded_at": recorded_at
        or datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        "real_trading_enabled": False,
    }
    identity = dict(payload)
    identity.pop("recorded_at", None)
    payload["audit_event_id"] = (
        "projection-audit:" + sha256(_canonical_bytes(identity)).hexdigest()[:32]
    )

    root = Path(review_dir).absolute()
    _check_no_symlink(root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / AUDIT_LOG
    with _audit_lock(root):
        existing: dict[str, Any] | None = None
        if path.exists():
            for line_number, raw_line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if not raw_line.strip():
                    continue
                try:
                    row = json.loads(raw_line)
                except json.JSONDecodeError as exc:
                    raise ProjectionGenerationError(
                        "projection_audit_log_malformed:%d" % line_number
                    ) from exc
                if (
                    isinstance(row, dict)
                    and row.get("audit_event_id") == payload["audit_event_id"]
                ):
                    existing = row
                    break
        if existing is not None:
            comparable_existing = dict(existing)
            comparable_payload = dict(payload)
            comparable_existing.pop("recorded_at", None)
            comparable_payload.pop("recorded_at", None)
            if comparable_existing != comparable_payload:
                raise ProjectionGenerationError("projection_audit_identity_conflict")
            return {"status": "idempotent", "record": deepcopy(existing)}
        _append_jsonl(path, payload)
    return {"status": "appended", "record": deepcopy(payload)}


__all__ = [
    "AUDIT_LOG",
    "CURRENT_MANIFEST",
    "GENERATIONS_DIR",
    "ProjectionGenerationError",
    "load_current_projection_set",
    "publish_projection_generation",
    "record_projection_audit",
]
