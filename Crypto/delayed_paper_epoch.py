"""Fail-closed epoch contract for Crypto delayed-paper outage restarts."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Iterator, Mapping
import uuid

from Crypto.capital_policy import (
    CRYPTO_CAPITAL_AUTHORITY_ID,
    CRYPTO_CAPITAL_POLICY,
)
from Crypto.fixture_sim.contracts import _assert_simulation_only


EPOCH_MANIFEST_CONTRACT = "tradingagent.crypto.delayed_paper_epoch_manifest.v1"
EPOCH_IDENTITY_CONTRACT = "tradingagent.crypto.delayed_paper_epoch_identity.v1"
CURRENT_EPOCH_CONTRACT = "tradingagent.crypto.delayed_paper_current_epoch.v1"
EPOCH_MANIFEST_PATH = Path("/etc/tradingagent/crypto-delayed-paper.epoch.json")
EPOCH_ROOT_PARENT = Path("/var/lib/tradingagent/crypto-delayed-paper-epochs")
LEGACY_ARCHIVE_ROOT = Path("/var/lib/tradingagent/crypto-delayed-paper")
EPOCH_MANIFEST_MAX_BYTES = 64 * 1024
EPOCH_IDENTITY_MAX_BYTES = 64 * 1024
CURRENT_EPOCH_MAX_BYTES = 64 * 1024
OUTAGE_EPOCH_GENERATION = 2
_IDENTITY_FILENAME = ".epoch_identity.json"
_CURRENT_EPOCH_FILENAME = ".current_epoch.json"
_CURRENT_EPOCH_LOCK_FILENAME = ".current_epoch.lock"
_CONTEXT_PROOF = object()
_REPO_ROOT = Path(__file__).resolve().parents[1]
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_EXPECTED_SAFETY = {
    "real_trading_enabled": False,
    "production_eligible": False,
    "execution_authority": False,
    "testnet_enabled": False,
    "live_broker_enabled": False,
    "model_network_enabled": False,
    "automatic_promotion_enabled": False,
    "automatic_risk_expansion_enabled": False,
}
_MANIFEST_KEYS = frozenset(
    {
        "schema",
        "epoch_id",
        "epoch_generation",
        "current_output_root",
        "archived_output_root",
        "archived_epoch_policy",
        "capital_baseline_policy_id",
        "aggregate_with_archived_epoch",
        "safety",
    }
)


class CryptoDelayedPaperEpochError(RuntimeError):
    """Stable error for an untrusted or conflicting outage epoch."""


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise CryptoDelayedPaperEpochError("epoch_payload_not_canonical") from exc


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _strict_object(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if type(key) is not str or key in result:
            raise CryptoDelayedPaperEpochError("epoch_manifest_duplicate_key")
        result[key] = value
    return result


def _secure_regular_bytes(
    path: Path,
    *,
    max_bytes: int,
    missing_reason: str,
    untrusted_reason: str,
    repository_external: bool = False,
) -> bytes:
    if not path.is_absolute():
        raise CryptoDelayedPaperEpochError(untrusted_reason)
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise CryptoDelayedPaperEpochError(missing_reason) from exc
    try:
        resolved.relative_to(_REPO_ROOT)
        inside_repository = True
    except ValueError:
        inside_repository = False
    if resolved != path or (repository_external and inside_repository):
        raise CryptoDelayedPaperEpochError(untrusted_reason)
    descriptor: int | None = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid not in {0, os.geteuid()}
            or before.st_mode & 0o022
            or before.st_size <= 0
            or before.st_size > max_bytes
        ):
            raise CryptoDelayedPaperEpochError(untrusted_reason)
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(
                descriptor,
                min(65_536, remaining),
            )
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        encoded = b"".join(chunks)
        after = os.fstat(descriptor)
        current = path.lstat()
        if (
            len(encoded) != before.st_size
            or before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or current.st_dev != before.st_dev
            or current.st_ino != before.st_ino
            or path.resolve(strict=True) != path
        ):
            raise CryptoDelayedPaperEpochError(untrusted_reason)
    except CryptoDelayedPaperEpochError:
        raise
    except OSError as exc:
        raise CryptoDelayedPaperEpochError(untrusted_reason) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if not encoded.endswith(b"\n") or b"\x00" in encoded:
        raise CryptoDelayedPaperEpochError(untrusted_reason)
    return encoded


def _read_manifest(path: Path) -> dict[str, Any]:
    encoded = _secure_regular_bytes(
        path,
        max_bytes=EPOCH_MANIFEST_MAX_BYTES,
        missing_reason="epoch_manifest_missing",
        untrusted_reason="epoch_manifest_file_untrusted",
        repository_external=True,
    )
    try:
        decoded = json.loads(
            encoded.decode("utf-8"),
            object_pairs_hook=_strict_object,
        )
    except CryptoDelayedPaperEpochError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CryptoDelayedPaperEpochError("epoch_manifest_json_invalid") from exc
    if (
        not isinstance(decoded, dict)
        or (_canonical_json(decoded) + "\n").encode("utf-8") != encoded
    ):
        raise CryptoDelayedPaperEpochError("epoch_manifest_not_canonical")
    return decoded


def _epoch_id(value: Any) -> str:
    prefix = "crypto-delayed-paper-epoch-"
    if (
        not isinstance(value, str)
        or not value.startswith(prefix)
        or len(value) > 80
        or len(value) <= len(prefix)
        or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789-"
            for character in value
        )
    ):
        raise CryptoDelayedPaperEpochError("epoch_id_invalid")
    return value


@dataclass(frozen=True)
class CryptoDelayedPaperEpochContext:
    epoch_id: str
    epoch_generation: int
    output_root: Path
    archived_output_root: Path
    manifest_path: Path
    manifest_sha256: str
    _proof: object

    @property
    def identity_path(self) -> Path:
        return self.output_root / _IDENTITY_FILENAME


@dataclass(frozen=True)
class PreparedCryptoDelayedPaperEpoch:
    context: CryptoDelayedPaperEpochContext

    @property
    def output_root(self) -> Path:
        return self.context.output_root

    @property
    def identity_path(self) -> Path:
        return self.context.identity_path

    @property
    def identity_sha256(self) -> str:
        return str(_identity_payload(self.context)["epoch_identity_sha256"])


def load_crypto_delayed_paper_epoch_manifest(
    path: Path | str,
) -> CryptoDelayedPaperEpochContext:
    """Load the repository-external current-epoch pointer."""

    _assert_simulation_only()
    manifest_path = Path(path)
    if manifest_path != EPOCH_MANIFEST_PATH:
        raise CryptoDelayedPaperEpochError("epoch_manifest_path_invalid")
    raw = _read_manifest(manifest_path)
    if set(raw) != _MANIFEST_KEYS:
        raise CryptoDelayedPaperEpochError("epoch_manifest_keys_invalid")
    if raw.get("schema") != EPOCH_MANIFEST_CONTRACT:
        raise CryptoDelayedPaperEpochError("epoch_manifest_contract_invalid")
    epoch_id = _epoch_id(raw.get("epoch_id"))
    generation = raw.get("epoch_generation")
    if (
        isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation != OUTAGE_EPOCH_GENERATION
    ):
        raise CryptoDelayedPaperEpochError("epoch_generation_invalid")
    if not epoch_id.startswith(f"crypto-delayed-paper-epoch-g{generation}-"):
        raise CryptoDelayedPaperEpochError("epoch_generation_identity_mismatch")
    output_root = Path(str(raw.get("current_output_root")))
    archived_root = Path(str(raw.get("archived_output_root")))
    expected_output_root = EPOCH_ROOT_PARENT / epoch_id
    if (
        not EPOCH_ROOT_PARENT.is_absolute()
        or not output_root.is_absolute()
        or output_root != expected_output_root
        or expected_output_root.parent != EPOCH_ROOT_PARENT
        or archived_root != LEGACY_ARCHIVE_ROOT
        or not archived_root.is_absolute()
        or output_root == archived_root
    ):
        raise CryptoDelayedPaperEpochError("epoch_output_roots_invalid")
    if raw.get("archived_epoch_policy") != "read_only_archive_no_resume":
        raise CryptoDelayedPaperEpochError("epoch_archive_policy_invalid")
    if raw.get("capital_baseline_policy_id") != CRYPTO_CAPITAL_AUTHORITY_ID:
        raise CryptoDelayedPaperEpochError("epoch_capital_policy_invalid")
    safety = raw.get("safety")
    if (
        raw.get("aggregate_with_archived_epoch") is not False
        or not isinstance(safety, dict)
        or set(safety) != set(_EXPECTED_SAFETY)
        or any(
            safety.get(field) is not expected
            for field, expected in _EXPECTED_SAFETY.items()
        )
    ):
        raise CryptoDelayedPaperEpochError("epoch_manifest_safety_invalid")
    return CryptoDelayedPaperEpochContext(
        epoch_id=epoch_id,
        epoch_generation=generation,
        output_root=output_root,
        archived_output_root=archived_root,
        manifest_path=manifest_path,
        manifest_sha256=_sha256(raw),
        _proof=_CONTEXT_PROOF,
    )


def _validate_context(
    context: Any,
    *,
    output_root: Path,
) -> None:
    if type(context) is not CryptoDelayedPaperEpochContext:
        raise CryptoDelayedPaperEpochError("epoch_runtime_context_invalid")
    try:
        epoch_id = _epoch_id(context.epoch_id)
    except CryptoDelayedPaperEpochError as exc:
        raise CryptoDelayedPaperEpochError("epoch_runtime_context_invalid") from exc
    expected_output_root = EPOCH_ROOT_PARENT / epoch_id
    if (
        context._proof is not _CONTEXT_PROOF
        or isinstance(context.epoch_generation, bool)
        or context.epoch_generation != OUTAGE_EPOCH_GENERATION
        or not epoch_id.startswith(
            f"crypto-delayed-paper-epoch-g{OUTAGE_EPOCH_GENERATION}-"
        )
        or output_root != context.output_root
        or not output_root.is_absolute()
        or context.output_root != expected_output_root
        or expected_output_root.parent != EPOCH_ROOT_PARENT
        or context.archived_output_root != LEGACY_ARCHIVE_ROOT
        or output_root == context.archived_output_root
        or context.manifest_path != EPOCH_MANIFEST_PATH
        or _SHA256_PATTERN.fullmatch(context.manifest_sha256) is None
    ):
        raise CryptoDelayedPaperEpochError("epoch_runtime_context_invalid")
    current = load_crypto_delayed_paper_epoch_manifest(EPOCH_MANIFEST_PATH)
    if current != context:
        raise CryptoDelayedPaperEpochError("epoch_runtime_context_stale")


def validate_epoch_runtime_context(
    context: Any,
    *,
    output_root: Path,
) -> None:
    _validate_context(context, output_root=output_root)
    _secure_directory(
        EPOCH_ROOT_PARENT,
        reason="epoch_root_parent_untrusted",
    )
    with _current_epoch_read_lock():
        _verify_current_epoch(context)
        _secure_directory(
            context.output_root,
            reason="epoch_output_root_untrusted",
        )
        _verify_identity(context)


def epoch_runtime_receipt_fields(
    context: CryptoDelayedPaperEpochContext,
) -> dict[str, Any]:
    validate_epoch_runtime_context(
        context,
        output_root=context.output_root,
    )
    return {
        "epoch_id": context.epoch_id,
        "epoch_generation": context.epoch_generation,
        "epoch_manifest_sha256": context.manifest_sha256,
        "current_epoch_sha256": _current_epoch_payload(context)["current_epoch_sha256"],
        "epoch_identity_sha256": _identity_payload(context)["epoch_identity_sha256"],
        "capital_baseline_policy_id": (CRYPTO_CAPITAL_POLICY.authority_id),
        "capital_baseline_usdt": str(CRYPTO_CAPITAL_POLICY.initial_cash),
        "aggregate_with_archived_epoch": False,
        "archived_epoch_consumed": False,
    }


def _secure_directory(path: Path, *, reason: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise CryptoDelayedPaperEpochError(reason) from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o077
    ):
        raise CryptoDelayedPaperEpochError(reason)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _current_epoch_path() -> Path:
    return EPOCH_ROOT_PARENT / _CURRENT_EPOCH_FILENAME


def _current_epoch_lock_path() -> Path:
    return EPOCH_ROOT_PARENT / _CURRENT_EPOCH_LOCK_FILENAME


def _current_epoch_payload(
    context: CryptoDelayedPaperEpochContext,
) -> dict[str, Any]:
    current: dict[str, Any] = {
        "contract": CURRENT_EPOCH_CONTRACT,
        "epoch_id": context.epoch_id,
        "epoch_generation": context.epoch_generation,
        "current_output_root": str(context.output_root),
        "archived_output_root": str(context.archived_output_root),
        "archived_epoch_policy": "read_only_archive_no_resume",
        "epoch_manifest_path": str(context.manifest_path),
        "epoch_manifest_sha256": context.manifest_sha256,
        "capital_baseline_policy_id": CRYPTO_CAPITAL_POLICY.authority_id,
        "capital_baseline_usdt": str(CRYPTO_CAPITAL_POLICY.initial_cash),
        "capital_generation": CRYPTO_CAPITAL_POLICY.generation,
        "capital_generation_scope": CRYPTO_CAPITAL_POLICY.generation_scope,
        "aggregate_with_archived_epoch": False,
        "archived_epoch_consumed": False,
        "real_trading_enabled": False,
        "execution_authority": False,
        "production_eligible": False,
        "testnet_enabled": False,
        "live_broker_enabled": False,
        "model_network_enabled": False,
        "automatic_promotion_enabled": False,
        "automatic_risk_expansion_enabled": False,
    }
    current["current_epoch_sha256"] = _sha256(current)
    return current


@contextmanager
def _current_epoch_lock() -> Iterator[None]:
    lock_path = _current_epoch_lock_path()
    descriptor: int | None = None
    try:
        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(lock_path, flags, 0o600)
        metadata = os.fstat(descriptor)
        current = lock_path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o077
            or current.st_dev != metadata.st_dev
            or current.st_ino != metadata.st_ino
            or lock_path.resolve(strict=True) != lock_path
        ):
            raise CryptoDelayedPaperEpochError("current_epoch_lock_untrusted")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    except CryptoDelayedPaperEpochError:
        raise
    except OSError as exc:
        raise CryptoDelayedPaperEpochError("current_epoch_lock_untrusted") from exc
    finally:
        if descriptor is not None:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


@contextmanager
def _current_epoch_read_lock() -> Iterator[None]:
    """Take the existing current-epoch lock without creating or writing it."""

    lock_path = _current_epoch_lock_path()
    descriptor: int | None = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(lock_path, flags)
        metadata = os.fstat(descriptor)
        current = lock_path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o077
            or current.st_dev != metadata.st_dev
            or current.st_ino != metadata.st_ino
            or lock_path.resolve(strict=True) != lock_path
        ):
            raise CryptoDelayedPaperEpochError("current_epoch_lock_untrusted")
        fcntl.flock(descriptor, fcntl.LOCK_SH)
        yield
    except CryptoDelayedPaperEpochError:
        raise
    except OSError as exc:
        raise CryptoDelayedPaperEpochError("current_epoch_lock_untrusted") from exc
    finally:
        if descriptor is not None:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


def _write_current_epoch(
    path: Path,
    payload: Mapping[str, Any],
) -> None:
    encoded = (_canonical_json(payload) + "\n").encode("utf-8")
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    descriptor: int | None = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary, flags, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = None
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        _fsync_directory(path.parent)
    except OSError as exc:
        raise CryptoDelayedPaperEpochError("current_epoch_persist_failed") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _verify_current_epoch(
    context: CryptoDelayedPaperEpochContext,
) -> None:
    path = _current_epoch_path()
    encoded = _secure_regular_bytes(
        path,
        max_bytes=CURRENT_EPOCH_MAX_BYTES,
        missing_reason="current_epoch_missing",
        untrusted_reason="current_epoch_untrusted",
    )
    try:
        current = json.loads(
            encoded.decode("utf-8"),
            object_pairs_hook=_strict_object,
        )
    except CryptoDelayedPaperEpochError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CryptoDelayedPaperEpochError("current_epoch_invalid") from exc
    expected = _current_epoch_payload(context)
    if (
        not isinstance(current, dict)
        or (_canonical_json(current) + "\n").encode("utf-8") != encoded
        or encoded != (_canonical_json(expected) + "\n").encode("utf-8")
    ):
        raise CryptoDelayedPaperEpochError("current_epoch_conflict")


def _claim_or_verify_current_epoch(
    context: CryptoDelayedPaperEpochContext,
) -> None:
    path = _current_epoch_path()
    if path.exists() or path.is_symlink():
        _verify_current_epoch(context)
        return
    existing_state = [
        item.name
        for item in EPOCH_ROOT_PARENT.iterdir()
        if item.name != _CURRENT_EPOCH_LOCK_FILENAME
    ]
    if existing_state:
        raise CryptoDelayedPaperEpochError("current_epoch_missing_with_existing_state")
    _write_current_epoch(
        path,
        _current_epoch_payload(context),
    )
    _verify_current_epoch(context)


def _identity_payload(
    context: CryptoDelayedPaperEpochContext,
) -> dict[str, Any]:
    identity: dict[str, Any] = {
        "contract": EPOCH_IDENTITY_CONTRACT,
        "epoch_id": context.epoch_id,
        "epoch_generation": context.epoch_generation,
        "current_output_root": str(context.output_root),
        "archived_output_root": str(context.archived_output_root),
        "archived_epoch_policy": ("read_only_archive_no_resume"),
        "epoch_manifest_sha256": context.manifest_sha256,
        "capital_baseline_policy_id": (CRYPTO_CAPITAL_POLICY.authority_id),
        "capital_baseline_usdt": str(CRYPTO_CAPITAL_POLICY.initial_cash),
        "capital_generation": (CRYPTO_CAPITAL_POLICY.generation),
        "capital_generation_scope": (CRYPTO_CAPITAL_POLICY.generation_scope),
        "aggregate_with_archived_epoch": False,
        "archived_epoch_consumed": False,
        "real_trading_enabled": False,
        "execution_authority": False,
        "production_eligible": False,
        "testnet_enabled": False,
        "live_broker_enabled": False,
        "model_network_enabled": False,
        "automatic_promotion_enabled": False,
        "automatic_risk_expansion_enabled": False,
    }
    identity["epoch_identity_sha256"] = _sha256(identity)
    return identity


def _write_identity(path: Path, payload: Mapping[str, Any]) -> None:
    encoded = (_canonical_json(payload) + "\n").encode("utf-8")
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    descriptor: int | None = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary, flags, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = None
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        _fsync_directory(path.parent)
    except OSError as exc:
        raise CryptoDelayedPaperEpochError("epoch_identity_persist_failed") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _verify_identity(
    context: CryptoDelayedPaperEpochContext,
) -> None:
    encoded = _secure_regular_bytes(
        context.identity_path,
        max_bytes=EPOCH_IDENTITY_MAX_BYTES,
        missing_reason="epoch_identity_missing",
        untrusted_reason="epoch_identity_untrusted",
    )
    try:
        identity = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CryptoDelayedPaperEpochError("epoch_identity_invalid") from exc
    expected = _identity_payload(context)
    if (
        not isinstance(identity, dict)
        or (_canonical_json(identity) + "\n").encode("utf-8") != encoded
        or encoded != (_canonical_json(expected) + "\n").encode("utf-8")
    ):
        raise CryptoDelayedPaperEpochError("epoch_identity_conflict")


def prepare_crypto_delayed_paper_epoch(
    context: CryptoDelayedPaperEpochContext,
) -> PreparedCryptoDelayedPaperEpoch:
    """Claim or verify one isolated epoch root without reading the archive."""

    _assert_simulation_only()
    _validate_context(
        context,
        output_root=context.output_root,
    )
    _secure_directory(
        EPOCH_ROOT_PARENT,
        reason="epoch_root_parent_untrusted",
    )
    with _current_epoch_lock():
        _claim_or_verify_current_epoch(context)
        if context.output_root.exists() or context.output_root.is_symlink():
            _secure_directory(
                context.output_root,
                reason="epoch_output_root_untrusted",
            )
        else:
            try:
                context.output_root.mkdir(mode=0o700)
                _fsync_directory(EPOCH_ROOT_PARENT)
            except OSError as exc:
                raise CryptoDelayedPaperEpochError(
                    "epoch_output_root_create_failed"
                ) from exc
        if context.identity_path.exists() or context.identity_path.is_symlink():
            _verify_identity(context)
        else:
            if any(context.output_root.iterdir()):
                raise CryptoDelayedPaperEpochError("epoch_output_root_unclaimed")
            _write_identity(
                context.identity_path,
                _identity_payload(context),
            )
            _verify_identity(context)
    return PreparedCryptoDelayedPaperEpoch(context=context)


__all__ = [
    "CryptoDelayedPaperEpochContext",
    "CryptoDelayedPaperEpochError",
    "CURRENT_EPOCH_CONTRACT",
    "EPOCH_IDENTITY_CONTRACT",
    "EPOCH_MANIFEST_CONTRACT",
    "EPOCH_MANIFEST_PATH",
    "EPOCH_ROOT_PARENT",
    "LEGACY_ARCHIVE_ROOT",
    "OUTAGE_EPOCH_GENERATION",
    "PreparedCryptoDelayedPaperEpoch",
    "epoch_runtime_receipt_fields",
    "load_crypto_delayed_paper_epoch_manifest",
    "prepare_crypto_delayed_paper_epoch",
    "validate_epoch_runtime_context",
]
