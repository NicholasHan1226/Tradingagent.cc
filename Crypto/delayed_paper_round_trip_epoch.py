"""Non-activating epoch-g3 candidate for round-trip capital generation 2."""

from __future__ import annotations

from dataclasses import dataclass
import argparse
import grp
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any, Mapping
import uuid

from Crypto.fixture_sim.contracts import _assert_simulation_only
from Crypto.fixture_sim.ledger import CryptoCapitalLedger, CryptoLedgerError
from Crypto.round_trip_capital import (
    ROUND_TRIP_CAPITAL_POLICY,
    CryptoRoundTripError,
    RoundTripCapitalLedger,
)


ROUND_TRIP_EPOCH_MANIFEST_CONTRACT = "tradingagent.crypto.round_trip_epoch_manifest.v1"
ROUND_TRIP_EPOCH_IDENTITY_CONTRACT = "tradingagent.crypto.round_trip_epoch_identity.v1"
ROUND_TRIP_EPOCH_GENERATION = 3
ROUND_TRIP_EPOCH_SUCCESSOR_GENERATION = 4
ROUND_TRIP_EPOCH_RECOVERY_GENERATION = 5
ROUND_TRIP_EPOCH_MANIFEST_PATH = Path(
    "/etc/tradingagent/crypto-delayed-paper-round-trip.epoch.json"
)
ROUND_TRIP_EPOCH_MANIFEST_DIRECTORY = Path(
    "/etc/tradingagent/crypto-delayed-paper-round-trip-epochs"
)
ROUND_TRIP_EPOCH_MANIFEST_PARENT = Path("/etc/tradingagent")
ROUND_TRIP_EPOCH_ROOT_PARENT = Path("/var/lib/tradingagent/crypto-delayed-paper-epochs")
_IDENTITY_FILENAME = ".round_trip_epoch_identity.json"
_MANIFEST_MAX_BYTES = 64 * 1024
ROUND_TRIP_EPOCH_VERSIONED_MANIFEST_CONTRACT = (
    "tradingagent.crypto.round_trip_epoch_manifest.v2"
)
ROUND_TRIP_EPOCH_SUCCESSOR_MANIFEST_CONTRACT = (
    "tradingagent.crypto.round_trip_epoch_manifest.v3"
)
ROUND_TRIP_EPOCH_RECOVERY_MANIFEST_CONTRACT = (
    "tradingagent.crypto.round_trip_epoch_manifest.v4"
)
ROUND_TRIP_EPOCH_SUPERSESSION_RECEIPT_CONTRACT = (
    "tradingagent.crypto.round_trip_epoch_supersession_receipt.v1"
)
ROUND_TRIP_EPOCH_SUCCESSOR_RECEIPT_CONTRACT = (
    "tradingagent.crypto.round_trip_epoch_supersession_receipt.v2"
)
ROUND_TRIP_EPOCH_RECOVERY_RECEIPT_CONTRACT = (
    "tradingagent.crypto.round_trip_epoch_supersession_receipt.v3"
)
_RUNTIME_READER_GROUP = "tradingagent"
_PROOF = object()
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


class CryptoRoundTripEpochError(RuntimeError):
    """Stable fail-closed error for an untrusted round-trip epoch candidate."""


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
        raise CryptoRoundTripEpochError("round_trip_epoch_payload_invalid") from exc


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256(value: Any) -> str:
    return _sha256_bytes(_canonical_json(value).encode())


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if type(key) is not str or key in result:
            raise CryptoRoundTripEpochError("round_trip_epoch_duplicate_key")
        result[key] = value
    return result


def _secure_regular(path: Path, *, reason: str, max_bytes: int) -> bytes:
    descriptor: int | None = None
    try:
        if not path.is_absolute() or path.resolve(strict=True) != path:
            raise CryptoRoundTripEpochError(reason)
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        before = os.fstat(descriptor)
        current = path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_mode & 0o022
            or before.st_size <= 0
            or before.st_size > max_bytes
            or current.st_dev != before.st_dev
            or current.st_ino != before.st_ino
        ):
            raise CryptoRoundTripEpochError(reason)
        encoded = os.read(descriptor, max_bytes + 1)
        after = os.fstat(descriptor)
        if (
            len(encoded) != before.st_size
            or after.st_size != before.st_size
            or after.st_mtime_ns != before.st_mtime_ns
        ):
            raise CryptoRoundTripEpochError(reason)
    except CryptoRoundTripEpochError:
        raise
    except OSError as exc:
        raise CryptoRoundTripEpochError(reason) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if not encoded.endswith(b"\n") or b"\x00" in encoded:
        raise CryptoRoundTripEpochError(reason)
    return encoded


def _secure_directory(path: Path, *, reason: str) -> None:
    try:
        node = path.lstat()
    except OSError as exc:
        raise CryptoRoundTripEpochError(reason) from exc
    if (
        not stat.S_ISDIR(node.st_mode)
        or stat.S_ISLNK(node.st_mode)
        or node.st_mode & 0o077
    ):
        raise CryptoRoundTripEpochError(reason)


def _epoch_id(value: Any, *, generation: int) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith(f"crypto-delayed-paper-round-trip-epoch-g{generation}-")
        or len(value) > 96
        or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789-"
            for character in value
        )
    ):
        raise CryptoRoundTripEpochError("round_trip_epoch_manifest_safety_invalid")
    return value


def _versioned_manifest_path(epoch_id: str) -> Path:
    return ROUND_TRIP_EPOCH_MANIFEST_DIRECTORY / f"{epoch_id}.json"


def _supersession_receipt_path(generation: int) -> Path:
    return (
        ROUND_TRIP_EPOCH_MANIFEST_DIRECTORY
        / f"generation-{generation}.supersession.json"
    )


def _secure_manifest_directory(*, create: bool) -> None:
    directory = ROUND_TRIP_EPOCH_MANIFEST_DIRECTORY
    if (
        not directory.is_absolute()
        or directory.parent != ROUND_TRIP_EPOCH_MANIFEST_PARENT
    ):
        raise CryptoRoundTripEpochError("round_trip_epoch_manifest_directory_invalid")
    if create and not directory.exists():
        try:
            directory.mkdir(mode=0o750)
            os.chown(directory, -1, _runtime_reader_gid())
            os.chmod(directory, 0o750)
        except OSError as exc:
            raise CryptoRoundTripEpochError(
                "round_trip_epoch_manifest_directory_create_failed"
            ) from exc
    try:
        node = directory.lstat()
    except OSError as exc:
        raise CryptoRoundTripEpochError(
            "round_trip_epoch_manifest_directory_untrusted"
        ) from exc
    if (
        not stat.S_ISDIR(node.st_mode)
        or stat.S_ISLNK(node.st_mode)
        or node.st_uid not in {0, os.geteuid()}
        or node.st_gid != _runtime_reader_gid()
        or stat.S_IMODE(node.st_mode) != 0o750
    ):
        raise CryptoRoundTripEpochError("round_trip_epoch_manifest_directory_untrusted")


def _runtime_reader_gid() -> int:
    try:
        return grp.getgrnam(_RUNTIME_READER_GROUP).gr_gid
    except KeyError:
        return os.getegid()


def _secure_versioned_manifest(path: Path, *, reason: str) -> bytes:
    encoded = _secure_regular(path, reason=reason, max_bytes=_MANIFEST_MAX_BYTES)
    node = path.lstat()
    if (
        node.st_uid not in {0, os.geteuid()}
        or node.st_gid != _runtime_reader_gid()
        or stat.S_IMODE(node.st_mode) != 0o640
    ):
        raise CryptoRoundTripEpochError(reason)
    return encoded


def _atomic_create_or_verify(
    path: Path, payload: Mapping[str, Any], *, reason: str
) -> None:
    expected = (_canonical_json(payload) + "\n").encode()
    if path.exists() or path.is_symlink():
        if _secure_versioned_manifest(path, reason=reason) != expected:
            raise CryptoRoundTripEpochError(reason)
        return
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        os.fchmod(descriptor, 0o640)
        os.fchown(descriptor, -1, _runtime_reader_gid())
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = None
            stream.write(expected)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if _secure_versioned_manifest(path, reason=reason) != expected:
                raise CryptoRoundTripEpochError(reason)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except CryptoRoundTripEpochError:
        raise
    except OSError as exc:
        raise CryptoRoundTripEpochError(reason) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _verify_existing_payload(
    path: Path, payload: Mapping[str, Any], *, reason: str
) -> None:
    if not path.exists() and not path.is_symlink():
        return
    expected = (_canonical_json(payload) + "\n").encode()
    if _secure_versioned_manifest(path, reason=reason) != expected:
        raise CryptoRoundTripEpochError(reason)


@dataclass(frozen=True)
class CryptoRoundTripEpochContext:
    epoch_id: str
    epoch_generation: int
    capital_generation: int
    output_root: Path
    archived_output_root: Path
    archived_epoch_id: str
    archived_epoch_identity_file_sha256: str
    archived_capital_head_checksum: str
    manifest_path: Path
    manifest_sha256: str
    aggregate_with_archived_epoch: bool
    _proof: object
    versioned: bool = False
    supersedes_manifest_path: Path | None = None
    supersedes_manifest_sha256: str | None = None
    migration_reason: str | None = None
    supersession_receipt_path: Path | None = None
    archived_capital_head_sequence: int | None = None
    supersedes_receipt_path: Path | None = None
    supersedes_receipt_sha256: str | None = None
    supersedes_output_root: Path | None = None
    supersedes_capital_head_sequence: int | None = None
    supersedes_capital_head_checksum: str | None = None

    @property
    def identity_path(self) -> Path:
        return self.output_root / _IDENTITY_FILENAME


@dataclass(frozen=True)
class PreparedCryptoRoundTripEpoch:
    context: CryptoRoundTripEpochContext

    @property
    def output_root(self) -> Path:
        return self.context.output_root

    @property
    def identity_path(self) -> Path:
        return self.context.identity_path


def load_round_trip_epoch_manifest(
    path: Path | str,
) -> CryptoRoundTripEpochContext:
    """Load the fixed non-activating g3 candidate manifest."""

    _assert_simulation_only()
    manifest_path = Path(path)
    if manifest_path != ROUND_TRIP_EPOCH_MANIFEST_PATH:
        if (
            manifest_path.parent != ROUND_TRIP_EPOCH_MANIFEST_DIRECTORY
            or manifest_path.name.startswith("generation-")
        ):
            raise CryptoRoundTripEpochError("round_trip_epoch_manifest_path_invalid")
        return _load_versioned_round_trip_epoch_manifest(manifest_path)
    encoded = _secure_regular(
        manifest_path,
        reason="round_trip_epoch_manifest_untrusted",
        max_bytes=_MANIFEST_MAX_BYTES,
    )
    try:
        raw = json.loads(
            encoded.decode(),
            object_pairs_hook=_strict_object,
        )
    except CryptoRoundTripEpochError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CryptoRoundTripEpochError(
            "round_trip_epoch_manifest_json_invalid"
        ) from exc
    expected_keys = {
        "schema",
        "epoch_id",
        "epoch_generation",
        "current_output_root",
        "archived_output_root",
        "archived_epoch_id",
        "archived_epoch_identity_file_sha256",
        "archived_capital_head_checksum",
        "archived_epoch_policy",
        "capital_authority_id",
        "capital_generation",
        "capital_baseline_usdt",
        "aggregate_with_archived_epoch",
        "activate_current_epoch",
        "safety",
    }
    if (
        not isinstance(raw, dict)
        or set(raw) != expected_keys
        or encoded != (_canonical_json(raw) + "\n").encode()
    ):
        raise CryptoRoundTripEpochError("round_trip_epoch_manifest_schema_invalid")
    epoch_id = raw.get("epoch_id")
    output_root = Path(str(raw.get("current_output_root")))
    archived_root = Path(str(raw.get("archived_output_root")))
    safety = raw.get("safety")
    digests = (
        raw.get("archived_epoch_identity_file_sha256"),
        raw.get("archived_capital_head_checksum"),
    )
    if (
        raw.get("schema") != ROUND_TRIP_EPOCH_MANIFEST_CONTRACT
        or not isinstance(epoch_id, str)
        or not epoch_id.startswith("crypto-delayed-paper-round-trip-epoch-g3-")
        or len(epoch_id) > 96
        or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789-"
            for character in epoch_id
        )
        or raw.get("epoch_generation") != ROUND_TRIP_EPOCH_GENERATION
        or raw.get("capital_generation") != ROUND_TRIP_CAPITAL_POLICY.generation
        or raw.get("capital_authority_id") != ROUND_TRIP_CAPITAL_POLICY.authority_id
        or raw.get("capital_baseline_usdt")
        != format(ROUND_TRIP_CAPITAL_POLICY.initial_cash, "f")
        or output_root != ROUND_TRIP_EPOCH_ROOT_PARENT / epoch_id
        or not archived_root.is_absolute()
        or archived_root.parent != ROUND_TRIP_EPOCH_ROOT_PARENT
        or archived_root == output_root
        or raw.get("archived_epoch_id") != archived_root.name
        or not archived_root.name.startswith("crypto-delayed-paper-epoch-g2-")
        or raw.get("archived_epoch_policy")
        != "read_only_archive_no_resume_no_aggregation"
        or raw.get("aggregate_with_archived_epoch") is not False
        or raw.get("activate_current_epoch") is not False
        or not isinstance(safety, dict)
        or safety != _EXPECTED_SAFETY
        or any(
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            for digest in digests
        )
    ):
        raise CryptoRoundTripEpochError("round_trip_epoch_manifest_safety_invalid")
    return CryptoRoundTripEpochContext(
        epoch_id=epoch_id,
        epoch_generation=ROUND_TRIP_EPOCH_GENERATION,
        capital_generation=ROUND_TRIP_CAPITAL_POLICY.generation,
        output_root=output_root,
        archived_output_root=archived_root,
        archived_epoch_id=str(raw["archived_epoch_id"]),
        archived_epoch_identity_file_sha256=str(digests[0]),
        archived_capital_head_checksum=str(digests[1]),
        manifest_path=manifest_path,
        manifest_sha256=_sha256(raw),
        aggregate_with_archived_epoch=False,
        _proof=_PROOF,
    )


def _load_versioned_round_trip_epoch_manifest(
    manifest_path: Path,
) -> CryptoRoundTripEpochContext:
    _secure_manifest_directory(create=False)
    encoded = _secure_versioned_manifest(
        manifest_path,
        reason="round_trip_epoch_manifest_untrusted",
    )
    try:
        raw = json.loads(encoded.decode(), object_pairs_hook=_strict_object)
    except CryptoRoundTripEpochError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CryptoRoundTripEpochError(
            "round_trip_epoch_manifest_json_invalid"
        ) from exc
    if (
        isinstance(raw, dict)
        and raw.get("schema") == ROUND_TRIP_EPOCH_SUCCESSOR_MANIFEST_CONTRACT
    ):
        return _load_successor_round_trip_epoch_manifest(manifest_path, raw, encoded)
    if (
        isinstance(raw, dict)
        and raw.get("schema") == ROUND_TRIP_EPOCH_RECOVERY_MANIFEST_CONTRACT
    ):
        return _load_recovery_round_trip_epoch_manifest(manifest_path, raw, encoded)
    expected_keys = {
        "schema",
        "epoch_id",
        "epoch_generation",
        "current_output_root",
        "archived_output_root",
        "archived_epoch_id",
        "archived_epoch_identity_file_sha256",
        "archived_capital_head_sequence",
        "archived_capital_head_checksum",
        "archived_epoch_policy",
        "capital_authority_id",
        "capital_generation",
        "capital_baseline_usdt",
        "aggregate_with_archived_epoch",
        "activate_current_epoch",
        "supersedes_manifest_path",
        "supersedes_manifest_sha256",
        "migration_reason",
        "supersession_receipt_path",
        "safety",
    }
    if (
        not isinstance(raw, dict)
        or set(raw) != expected_keys
        or encoded != (_canonical_json(raw) + "\n").encode()
    ):
        raise CryptoRoundTripEpochError("round_trip_epoch_manifest_schema_invalid")
    schema = raw.get("schema")
    epoch_id = _epoch_id(raw.get("epoch_id"), generation=ROUND_TRIP_EPOCH_GENERATION)
    output_root = Path(str(raw.get("current_output_root")))
    archived_root = Path(str(raw.get("archived_output_root")))
    supersedes_path = Path(str(raw.get("supersedes_manifest_path")))
    receipt_path = Path(str(raw.get("supersession_receipt_path")))
    digests = (
        raw.get("archived_epoch_identity_file_sha256"),
        raw.get("archived_capital_head_checksum"),
        raw.get("supersedes_manifest_sha256"),
    )
    sequence = raw.get("archived_capital_head_sequence")
    if (
        schema != ROUND_TRIP_EPOCH_VERSIONED_MANIFEST_CONTRACT
        or manifest_path != _versioned_manifest_path(epoch_id)
        or raw.get("epoch_generation") != ROUND_TRIP_EPOCH_GENERATION
        or raw.get("capital_generation") != ROUND_TRIP_CAPITAL_POLICY.generation
        or raw.get("capital_authority_id") != ROUND_TRIP_CAPITAL_POLICY.authority_id
        or raw.get("capital_baseline_usdt")
        != format(ROUND_TRIP_CAPITAL_POLICY.initial_cash, "f")
        or output_root != ROUND_TRIP_EPOCH_ROOT_PARENT / epoch_id
        or not archived_root.is_absolute()
        or archived_root.parent != ROUND_TRIP_EPOCH_ROOT_PARENT
        or archived_root == output_root
        or raw.get("archived_epoch_id") != archived_root.name
        or not archived_root.name.startswith("crypto-delayed-paper-epoch-g2-")
        or raw.get("archived_epoch_policy")
        != "read_only_archive_no_resume_no_aggregation"
        or raw.get("aggregate_with_archived_epoch") is not False
        or raw.get("activate_current_epoch") is not False
        or supersedes_path != ROUND_TRIP_EPOCH_MANIFEST_PATH
        or receipt_path != _supersession_receipt_path(ROUND_TRIP_EPOCH_GENERATION)
        or not isinstance(raw.get("migration_reason"), str)
        or not raw["migration_reason"].strip()
        or len(raw["migration_reason"]) > 512
        or isinstance(sequence, bool)
        or not isinstance(sequence, int)
        or sequence <= 0
        or not isinstance(raw.get("safety"), dict)
        or raw["safety"] != _EXPECTED_SAFETY
        or any(
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            for digest in digests
        )
    ):
        raise CryptoRoundTripEpochError("round_trip_epoch_manifest_safety_invalid")
    context = CryptoRoundTripEpochContext(
        epoch_id=epoch_id,
        epoch_generation=ROUND_TRIP_EPOCH_GENERATION,
        capital_generation=ROUND_TRIP_CAPITAL_POLICY.generation,
        output_root=output_root,
        archived_output_root=archived_root,
        archived_epoch_id=str(raw["archived_epoch_id"]),
        archived_epoch_identity_file_sha256=str(digests[0]),
        archived_capital_head_checksum=str(digests[1]),
        manifest_path=manifest_path,
        manifest_sha256=_sha256(raw),
        aggregate_with_archived_epoch=False,
        _proof=_PROOF,
        versioned=True,
        supersedes_manifest_path=supersedes_path,
        supersedes_manifest_sha256=str(digests[2]),
        migration_reason=str(raw["migration_reason"]),
        supersession_receipt_path=receipt_path,
        archived_capital_head_sequence=sequence,
    )
    _verify_supersession_receipt(context)
    return context


def _load_successor_round_trip_epoch_manifest(
    manifest_path: Path,
    raw: Mapping[str, Any],
    encoded: bytes,
) -> CryptoRoundTripEpochContext:
    """Load the one allowed g4 successor without rewriting the g3 evidence."""

    expected_keys = {
        "schema",
        "epoch_id",
        "epoch_generation",
        "current_output_root",
        "archived_output_root",
        "archived_epoch_id",
        "archived_epoch_identity_file_sha256",
        "archived_capital_head_sequence",
        "archived_capital_head_checksum",
        "archived_epoch_policy",
        "capital_authority_id",
        "capital_generation",
        "capital_baseline_usdt",
        "aggregate_with_archived_epoch",
        "activate_current_epoch",
        "supersedes_manifest_path",
        "supersedes_manifest_sha256",
        "supersedes_receipt_path",
        "supersedes_receipt_sha256",
        "migration_reason",
        "supersession_receipt_path",
        "safety",
    }
    if set(raw) != expected_keys or encoded != (_canonical_json(raw) + "\n").encode():
        raise CryptoRoundTripEpochError("round_trip_epoch_manifest_schema_invalid")
    epoch_id = _epoch_id(
        raw.get("epoch_id"), generation=ROUND_TRIP_EPOCH_SUCCESSOR_GENERATION
    )
    output_root = Path(str(raw.get("current_output_root")))
    archived_root = Path(str(raw.get("archived_output_root")))
    supersedes_path = Path(str(raw.get("supersedes_manifest_path")))
    prior_receipt_path = Path(str(raw.get("supersedes_receipt_path")))
    receipt_path = Path(str(raw.get("supersession_receipt_path")))
    digests = (
        raw.get("archived_epoch_identity_file_sha256"),
        raw.get("archived_capital_head_checksum"),
        raw.get("supersedes_manifest_sha256"),
        raw.get("supersedes_receipt_sha256"),
    )
    sequence = raw.get("archived_capital_head_sequence")
    if (
        raw.get("schema") != ROUND_TRIP_EPOCH_SUCCESSOR_MANIFEST_CONTRACT
        or manifest_path != _versioned_manifest_path(epoch_id)
        or raw.get("epoch_generation") != ROUND_TRIP_EPOCH_SUCCESSOR_GENERATION
        or raw.get("capital_generation") != ROUND_TRIP_CAPITAL_POLICY.generation
        or raw.get("capital_authority_id") != ROUND_TRIP_CAPITAL_POLICY.authority_id
        or raw.get("capital_baseline_usdt")
        != format(ROUND_TRIP_CAPITAL_POLICY.initial_cash, "f")
        or output_root != ROUND_TRIP_EPOCH_ROOT_PARENT / epoch_id
        or not archived_root.is_absolute()
        or archived_root.parent != ROUND_TRIP_EPOCH_ROOT_PARENT
        or archived_root == output_root
        or raw.get("archived_epoch_id") != archived_root.name
        or not archived_root.name.startswith("crypto-delayed-paper-epoch-g2-")
        or raw.get("archived_epoch_policy")
        != "read_only_archive_no_resume_no_aggregation"
        or raw.get("aggregate_with_archived_epoch") is not False
        or raw.get("activate_current_epoch") is not False
        or supersedes_path.parent != ROUND_TRIP_EPOCH_MANIFEST_DIRECTORY
        or prior_receipt_path != _supersession_receipt_path(ROUND_TRIP_EPOCH_GENERATION)
        or receipt_path
        != _supersession_receipt_path(ROUND_TRIP_EPOCH_SUCCESSOR_GENERATION)
        or not isinstance(raw.get("migration_reason"), str)
        or not raw["migration_reason"].strip()
        or len(raw["migration_reason"]) > 512
        or isinstance(sequence, bool)
        or not isinstance(sequence, int)
        or sequence <= 0
        or not isinstance(raw.get("safety"), dict)
        or raw["safety"] != _EXPECTED_SAFETY
        or any(
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            for digest in digests
        )
    ):
        raise CryptoRoundTripEpochError("round_trip_epoch_manifest_safety_invalid")
    context = CryptoRoundTripEpochContext(
        epoch_id=epoch_id,
        epoch_generation=ROUND_TRIP_EPOCH_SUCCESSOR_GENERATION,
        capital_generation=ROUND_TRIP_CAPITAL_POLICY.generation,
        output_root=output_root,
        archived_output_root=archived_root,
        archived_epoch_id=str(raw["archived_epoch_id"]),
        archived_epoch_identity_file_sha256=str(digests[0]),
        archived_capital_head_checksum=str(digests[1]),
        manifest_path=manifest_path,
        manifest_sha256=_sha256(raw),
        aggregate_with_archived_epoch=False,
        _proof=_PROOF,
        versioned=True,
        supersedes_manifest_path=supersedes_path,
        supersedes_manifest_sha256=str(digests[2]),
        migration_reason=str(raw["migration_reason"]),
        supersession_receipt_path=receipt_path,
        archived_capital_head_sequence=sequence,
        supersedes_receipt_path=prior_receipt_path,
        supersedes_receipt_sha256=str(digests[3]),
    )
    _verify_supersession_receipt(context)
    return context


def _load_recovery_round_trip_epoch_manifest(
    manifest_path: Path,
    raw: Mapping[str, Any],
    encoded: bytes,
) -> CryptoRoundTripEpochContext:
    """Load the one append-only G5 successor of a frozen G4 epoch."""

    expected_keys = {
        "schema",
        "epoch_id",
        "epoch_generation",
        "current_output_root",
        "archived_output_root",
        "archived_epoch_id",
        "archived_epoch_identity_file_sha256",
        "archived_capital_head_sequence",
        "archived_capital_head_checksum",
        "archived_epoch_policy",
        "capital_authority_id",
        "capital_generation",
        "capital_baseline_usdt",
        "aggregate_with_archived_epoch",
        "activate_current_epoch",
        "supersedes_manifest_path",
        "supersedes_manifest_sha256",
        "supersedes_receipt_path",
        "supersedes_receipt_sha256",
        "supersedes_output_root",
        "supersedes_capital_head_sequence",
        "supersedes_capital_head_checksum",
        "migration_reason",
        "supersession_receipt_path",
        "safety",
    }
    if set(raw) != expected_keys or encoded != (_canonical_json(raw) + "\n").encode():
        raise CryptoRoundTripEpochError("round_trip_epoch_manifest_schema_invalid")
    epoch_id = _epoch_id(
        raw.get("epoch_id"), generation=ROUND_TRIP_EPOCH_RECOVERY_GENERATION
    )
    output_root = Path(str(raw.get("current_output_root")))
    archived_root = Path(str(raw.get("archived_output_root")))
    predecessor_path = Path(str(raw.get("supersedes_manifest_path")))
    prior_receipt_path = Path(str(raw.get("supersedes_receipt_path")))
    receipt_path = Path(str(raw.get("supersession_receipt_path")))
    predecessor_root = Path(str(raw.get("supersedes_output_root")))
    sequence = raw.get("archived_capital_head_sequence")
    predecessor_sequence = raw.get("supersedes_capital_head_sequence")
    digests = tuple(
        raw.get(key)
        for key in (
            "archived_epoch_identity_file_sha256",
            "archived_capital_head_checksum",
            "supersedes_manifest_sha256",
            "supersedes_receipt_sha256",
            "supersedes_capital_head_checksum",
        )
    )
    if (
        raw.get("schema") != ROUND_TRIP_EPOCH_RECOVERY_MANIFEST_CONTRACT
        or manifest_path != _versioned_manifest_path(epoch_id)
        or raw.get("epoch_generation") != ROUND_TRIP_EPOCH_RECOVERY_GENERATION
        or raw.get("capital_generation") != ROUND_TRIP_CAPITAL_POLICY.generation
        or raw.get("capital_authority_id") != ROUND_TRIP_CAPITAL_POLICY.authority_id
        or raw.get("capital_baseline_usdt")
        != format(ROUND_TRIP_CAPITAL_POLICY.initial_cash, "f")
        or output_root != ROUND_TRIP_EPOCH_ROOT_PARENT / epoch_id
        or archived_root.parent != ROUND_TRIP_EPOCH_ROOT_PARENT
        or raw.get("archived_epoch_id") != archived_root.name
        or raw.get("archived_epoch_policy")
        != "read_only_archive_no_resume_no_aggregation"
        or raw.get("aggregate_with_archived_epoch") is not False
        or raw.get("activate_current_epoch") is not False
        or predecessor_path.parent != ROUND_TRIP_EPOCH_MANIFEST_DIRECTORY
        or prior_receipt_path
        != _supersession_receipt_path(ROUND_TRIP_EPOCH_SUCCESSOR_GENERATION)
        or receipt_path
        != _supersession_receipt_path(ROUND_TRIP_EPOCH_RECOVERY_GENERATION)
        or predecessor_root.parent != ROUND_TRIP_EPOCH_ROOT_PARENT
        or not predecessor_root.name.startswith(
            "crypto-delayed-paper-round-trip-epoch-g4-"
        )
        or isinstance(sequence, bool)
        or not isinstance(sequence, int)
        or sequence <= 0
        or isinstance(predecessor_sequence, bool)
        or not isinstance(predecessor_sequence, int)
        or predecessor_sequence <= 0
        or not isinstance(raw.get("migration_reason"), str)
        or not raw["migration_reason"].strip()
        or len(raw["migration_reason"]) > 512
        or raw.get("safety") != _EXPECTED_SAFETY
        or any(
            not isinstance(digest, str)
            or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)
            for digest in digests
        )
    ):
        raise CryptoRoundTripEpochError("round_trip_epoch_manifest_safety_invalid")
    context = CryptoRoundTripEpochContext(
        epoch_id=epoch_id,
        epoch_generation=ROUND_TRIP_EPOCH_RECOVERY_GENERATION,
        capital_generation=ROUND_TRIP_CAPITAL_POLICY.generation,
        output_root=output_root,
        archived_output_root=archived_root,
        archived_epoch_id=str(raw["archived_epoch_id"]),
        archived_epoch_identity_file_sha256=str(digests[0]),
        archived_capital_head_checksum=str(digests[1]),
        manifest_path=manifest_path,
        manifest_sha256=_sha256(raw),
        aggregate_with_archived_epoch=False,
        _proof=_PROOF,
        versioned=True,
        supersedes_manifest_path=predecessor_path,
        supersedes_manifest_sha256=str(digests[2]),
        migration_reason=str(raw["migration_reason"]),
        supersession_receipt_path=receipt_path,
        archived_capital_head_sequence=sequence,
        supersedes_receipt_path=prior_receipt_path,
        supersedes_receipt_sha256=str(digests[3]),
        supersedes_output_root=predecessor_root,
        supersedes_capital_head_sequence=predecessor_sequence,
        supersedes_capital_head_checksum=str(digests[4]),
    )
    _verify_supersession_receipt(context)
    return context


def _verify_archive(context: CryptoRoundTripEpochContext) -> None:
    _secure_directory(
        context.archived_output_root,
        reason="round_trip_archive_root_untrusted",
    )
    identity = _secure_regular(
        context.archived_output_root / ".epoch_identity.json",
        reason="round_trip_archive_identity_untrusted",
        max_bytes=_MANIFEST_MAX_BYTES,
    )
    if _sha256_bytes(identity) != context.archived_epoch_identity_file_sha256:
        raise CryptoRoundTripEpochError("round_trip_archive_identity_mismatch")
    try:
        sequence, checksum = CryptoCapitalLedger(
            context.archived_output_root / "capital"
        ).head()
    except (CryptoLedgerError, OSError, TypeError, ValueError) as exc:
        raise CryptoRoundTripEpochError(
            "round_trip_archive_capital_head_untrusted"
        ) from exc
    if (
        sequence <= 0
        or checksum != context.archived_capital_head_checksum
        or (
            context.archived_capital_head_sequence is not None
            and sequence != context.archived_capital_head_sequence
        )
    ):
        raise CryptoRoundTripEpochError("round_trip_archive_capital_head_mismatch")


def _supersession_receipt(context: CryptoRoundTripEpochContext) -> dict[str, Any]:
    if (
        not context.versioned
        or context.supersedes_manifest_path is None
        or context.supersedes_manifest_sha256 is None
        or context.migration_reason is None
        or context.supersession_receipt_path is None
        or context.archived_capital_head_sequence is None
    ):
        raise CryptoRoundTripEpochError("round_trip_epoch_supersession_invalid")
    receipt: dict[str, Any] = {
        "contract": (
            ROUND_TRIP_EPOCH_RECOVERY_RECEIPT_CONTRACT
            if context.epoch_generation == ROUND_TRIP_EPOCH_RECOVERY_GENERATION
            else (
                ROUND_TRIP_EPOCH_SUCCESSOR_RECEIPT_CONTRACT
                if context.epoch_generation == ROUND_TRIP_EPOCH_SUCCESSOR_GENERATION
                else ROUND_TRIP_EPOCH_SUPERSESSION_RECEIPT_CONTRACT
            )
        ),
        "epoch_id": context.epoch_id,
        "epoch_generation": context.epoch_generation,
        "current_output_root": str(context.output_root),
        "manifest_path": str(context.manifest_path),
        "manifest_sha256": context.manifest_sha256,
        "supersedes_manifest_path": str(context.supersedes_manifest_path),
        "supersedes_manifest_sha256": context.supersedes_manifest_sha256,
        "migration_reason": context.migration_reason,
        "archived_output_root": str(context.archived_output_root),
        "archived_epoch_id": context.archived_epoch_id,
        "archived_epoch_identity_file_sha256": (
            context.archived_epoch_identity_file_sha256
        ),
        "archived_capital_head_sequence": context.archived_capital_head_sequence,
        "archived_capital_head_checksum": context.archived_capital_head_checksum,
        "aggregate_with_archived_epoch": False,
        "real_trading_enabled": False,
        "execution_authority": False,
        "production_eligible": False,
        "automatic_promotion_enabled": False,
        "automatic_risk_expansion_enabled": False,
    }
    if context.epoch_generation in {
        ROUND_TRIP_EPOCH_SUCCESSOR_GENERATION,
        ROUND_TRIP_EPOCH_RECOVERY_GENERATION,
    }:
        if (
            context.supersedes_receipt_path is None
            or context.supersedes_receipt_sha256 is None
        ):
            raise CryptoRoundTripEpochError("round_trip_epoch_supersession_invalid")
        receipt.update(
            {
                "supersedes_receipt_path": str(context.supersedes_receipt_path),
                "supersedes_receipt_sha256": context.supersedes_receipt_sha256,
            }
        )
    if context.epoch_generation == ROUND_TRIP_EPOCH_RECOVERY_GENERATION:
        if (
            context.supersedes_output_root is None
            or context.supersedes_capital_head_sequence is None
            or context.supersedes_capital_head_checksum is None
        ):
            raise CryptoRoundTripEpochError("round_trip_epoch_supersession_invalid")
        receipt.update(
            {
                "supersedes_output_root": str(context.supersedes_output_root),
                "supersedes_capital_head_sequence": context.supersedes_capital_head_sequence,
                "supersedes_capital_head_checksum": context.supersedes_capital_head_checksum,
            }
        )
    return receipt


def _verify_supersession_receipt(context: CryptoRoundTripEpochContext) -> None:
    receipt_path = context.supersession_receipt_path
    if receipt_path is None:
        return
    encoded = _secure_versioned_manifest(
        receipt_path,
        reason="round_trip_epoch_supersession_receipt_invalid",
    )
    if encoded != (_canonical_json(_supersession_receipt(context)) + "\n").encode():
        raise CryptoRoundTripEpochError("round_trip_epoch_supersession_receipt_invalid")
    if context.epoch_generation in {
        ROUND_TRIP_EPOCH_SUCCESSOR_GENERATION,
        ROUND_TRIP_EPOCH_RECOVERY_GENERATION,
    }:
        superseded = _secure_versioned_manifest(
            context.supersedes_manifest_path,
            reason="round_trip_epoch_superseded_manifest_untrusted",
        )
    else:
        superseded = _secure_regular(
            context.supersedes_manifest_path,
            reason="round_trip_epoch_superseded_manifest_untrusted",
            max_bytes=_MANIFEST_MAX_BYTES,
        )
    if _sha256_bytes(superseded) != context.supersedes_manifest_sha256:
        raise CryptoRoundTripEpochError("round_trip_epoch_superseded_manifest_mismatch")
    if context.epoch_generation in {
        ROUND_TRIP_EPOCH_SUCCESSOR_GENERATION,
        ROUND_TRIP_EPOCH_RECOVERY_GENERATION,
    }:
        assert context.supersedes_receipt_path is not None
        assert context.supersedes_receipt_sha256 is not None
        prior_receipt = _secure_versioned_manifest(
            context.supersedes_receipt_path,
            reason="round_trip_epoch_superseded_receipt_untrusted",
        )
        if _sha256_bytes(prior_receipt) != context.supersedes_receipt_sha256:
            raise CryptoRoundTripEpochError(
                "round_trip_epoch_superseded_receipt_mismatch"
            )
        predecessor = load_round_trip_epoch_manifest(context.supersedes_manifest_path)
        if (
            predecessor.epoch_generation
            != (
                ROUND_TRIP_EPOCH_SUCCESSOR_GENERATION
                if context.epoch_generation == ROUND_TRIP_EPOCH_RECOVERY_GENERATION
                else ROUND_TRIP_EPOCH_GENERATION
            )
            or predecessor.supersession_receipt_path != context.supersedes_receipt_path
            or predecessor.output_root == context.output_root
            or predecessor.archived_output_root != context.archived_output_root
        ):
            raise CryptoRoundTripEpochError("round_trip_epoch_successor_chain_invalid")
    if context.epoch_generation == ROUND_TRIP_EPOCH_RECOVERY_GENERATION:
        assert context.supersedes_output_root is not None
        assert context.supersedes_capital_head_sequence is not None
        assert context.supersedes_capital_head_checksum is not None
        try:
            sequence, checksum = RoundTripCapitalLedger(
                context.supersedes_output_root / "round_trip_capital"
            ).head()
        except (CryptoRoundTripError, OSError, TypeError, ValueError) as exc:
            raise CryptoRoundTripEpochError(
                "round_trip_epoch_superseded_head_untrusted"
            ) from exc
        if (
            sequence != context.supersedes_capital_head_sequence
            or checksum != context.supersedes_capital_head_checksum
        ):
            raise CryptoRoundTripEpochError("round_trip_epoch_superseded_head_mismatch")


def prepare_versioned_round_trip_epoch_manifest(
    *,
    epoch_id: str,
    archived_output_root: Path | str,
    migration_reason: str,
) -> CryptoRoundTripEpochContext:
    """Freeze g2's current authority head into one immutable g3 migration."""

    _assert_simulation_only()
    epoch_id = _epoch_id(epoch_id, generation=ROUND_TRIP_EPOCH_GENERATION)
    archived_root = Path(archived_output_root)
    if (
        not archived_root.is_absolute()
        or archived_root.parent != ROUND_TRIP_EPOCH_ROOT_PARENT
        or not archived_root.name.startswith("crypto-delayed-paper-epoch-g2-")
        or not isinstance(migration_reason, str)
        or not migration_reason.strip()
        or len(migration_reason) > 512
    ):
        raise CryptoRoundTripEpochError("round_trip_epoch_migration_request_invalid")
    _secure_directory(
        ROUND_TRIP_EPOCH_ROOT_PARENT,
        reason="round_trip_epoch_parent_untrusted",
    )
    _secure_directory(archived_root, reason="round_trip_archive_root_untrusted")
    legacy_encoded = _secure_regular(
        ROUND_TRIP_EPOCH_MANIFEST_PATH,
        reason="round_trip_epoch_superseded_manifest_untrusted",
        max_bytes=_MANIFEST_MAX_BYTES,
    )
    identity = _secure_regular(
        archived_root / ".epoch_identity.json",
        reason="round_trip_archive_identity_untrusted",
        max_bytes=_MANIFEST_MAX_BYTES,
    )
    try:
        sequence, checksum = CryptoCapitalLedger(archived_root / "capital").head()
    except (CryptoLedgerError, OSError, TypeError, ValueError) as exc:
        raise CryptoRoundTripEpochError(
            "round_trip_archive_capital_head_untrusted"
        ) from exc
    if sequence <= 0 or not isinstance(checksum, str) or len(checksum) != 64:
        raise CryptoRoundTripEpochError("round_trip_archive_capital_head_untrusted")
    _secure_manifest_directory(create=True)
    manifest_path = _versioned_manifest_path(epoch_id)
    receipt_path = _supersession_receipt_path(ROUND_TRIP_EPOCH_GENERATION)
    payload = {
        "schema": ROUND_TRIP_EPOCH_VERSIONED_MANIFEST_CONTRACT,
        "epoch_id": epoch_id,
        "epoch_generation": ROUND_TRIP_EPOCH_GENERATION,
        "current_output_root": str(ROUND_TRIP_EPOCH_ROOT_PARENT / epoch_id),
        "archived_output_root": str(archived_root),
        "archived_epoch_id": archived_root.name,
        "archived_epoch_identity_file_sha256": _sha256_bytes(identity),
        "archived_capital_head_sequence": sequence,
        "archived_capital_head_checksum": checksum,
        "archived_epoch_policy": "read_only_archive_no_resume_no_aggregation",
        "capital_authority_id": ROUND_TRIP_CAPITAL_POLICY.authority_id,
        "capital_generation": ROUND_TRIP_CAPITAL_POLICY.generation,
        "capital_baseline_usdt": format(ROUND_TRIP_CAPITAL_POLICY.initial_cash, "f"),
        "aggregate_with_archived_epoch": False,
        "activate_current_epoch": False,
        "supersedes_manifest_path": str(ROUND_TRIP_EPOCH_MANIFEST_PATH),
        "supersedes_manifest_sha256": _sha256_bytes(legacy_encoded),
        "migration_reason": migration_reason,
        "supersession_receipt_path": str(receipt_path),
        "safety": dict(_EXPECTED_SAFETY),
    }
    context = CryptoRoundTripEpochContext(
        epoch_id=epoch_id,
        epoch_generation=ROUND_TRIP_EPOCH_GENERATION,
        capital_generation=ROUND_TRIP_CAPITAL_POLICY.generation,
        output_root=ROUND_TRIP_EPOCH_ROOT_PARENT / epoch_id,
        archived_output_root=archived_root,
        archived_epoch_id=archived_root.name,
        archived_epoch_identity_file_sha256=_sha256_bytes(identity),
        archived_capital_head_checksum=checksum,
        manifest_path=manifest_path,
        manifest_sha256=_sha256(payload),
        aggregate_with_archived_epoch=False,
        _proof=_PROOF,
        versioned=True,
        supersedes_manifest_path=ROUND_TRIP_EPOCH_MANIFEST_PATH,
        supersedes_manifest_sha256=_sha256_bytes(legacy_encoded),
        migration_reason=migration_reason,
        supersession_receipt_path=receipt_path,
        archived_capital_head_sequence=sequence,
    )
    receipt = _supersession_receipt(context)
    _verify_existing_payload(
        manifest_path,
        payload,
        reason="round_trip_epoch_versioned_manifest_conflict",
    )
    _verify_existing_payload(
        receipt_path,
        receipt,
        reason="round_trip_epoch_supersession_receipt_invalid",
    )
    _atomic_create_or_verify(
        manifest_path,
        payload,
        reason="round_trip_epoch_versioned_manifest_conflict",
    )
    _atomic_create_or_verify(
        receipt_path,
        receipt,
        reason="round_trip_epoch_supersession_receipt_invalid",
    )
    return load_round_trip_epoch_manifest(manifest_path)


def prepare_successor_round_trip_epoch_manifest(
    *,
    epoch_id: str,
    archived_output_root: Path | str,
    supersedes_manifest_path: Path | str,
    migration_reason: str,
) -> CryptoRoundTripEpochContext:
    """Freeze a new g2 head into the one permitted g4 successor to g3.

    This is deliberately a separate, append-only migration.  It cannot rewrite
    or reuse the failed g3 root, manifest, or receipt after g2 has progressed.
    """

    _assert_simulation_only()
    epoch_id = _epoch_id(epoch_id, generation=ROUND_TRIP_EPOCH_SUCCESSOR_GENERATION)
    archived_root = Path(archived_output_root)
    predecessor_path = Path(supersedes_manifest_path)
    if (
        not archived_root.is_absolute()
        or archived_root.parent != ROUND_TRIP_EPOCH_ROOT_PARENT
        or not archived_root.name.startswith("crypto-delayed-paper-epoch-g2-")
        or predecessor_path.parent != ROUND_TRIP_EPOCH_MANIFEST_DIRECTORY
        or not isinstance(migration_reason, str)
        or not migration_reason.strip()
        or len(migration_reason) > 512
    ):
        raise CryptoRoundTripEpochError("round_trip_epoch_migration_request_invalid")
    predecessor = load_round_trip_epoch_manifest(predecessor_path)
    if (
        predecessor.epoch_generation != ROUND_TRIP_EPOCH_GENERATION
        or predecessor.archived_output_root != archived_root
        or predecessor.supersession_receipt_path is None
    ):
        raise CryptoRoundTripEpochError("round_trip_epoch_successor_chain_invalid")
    _secure_directory(
        ROUND_TRIP_EPOCH_ROOT_PARENT,
        reason="round_trip_epoch_parent_untrusted",
    )
    _secure_directory(archived_root, reason="round_trip_archive_root_untrusted")
    identity = _secure_regular(
        archived_root / ".epoch_identity.json",
        reason="round_trip_archive_identity_untrusted",
        max_bytes=_MANIFEST_MAX_BYTES,
    )
    try:
        sequence, checksum = CryptoCapitalLedger(archived_root / "capital").head()
    except (CryptoLedgerError, OSError, TypeError, ValueError) as exc:
        raise CryptoRoundTripEpochError(
            "round_trip_archive_capital_head_untrusted"
        ) from exc
    if sequence <= 0 or not isinstance(checksum, str) or len(checksum) != 64:
        raise CryptoRoundTripEpochError("round_trip_archive_capital_head_untrusted")
    predecessor_encoded = _secure_versioned_manifest(
        predecessor.manifest_path,
        reason="round_trip_epoch_superseded_manifest_untrusted",
    )
    predecessor_receipt = _secure_versioned_manifest(
        predecessor.supersession_receipt_path,
        reason="round_trip_epoch_superseded_receipt_untrusted",
    )
    _secure_manifest_directory(create=True)
    manifest_path = _versioned_manifest_path(epoch_id)
    receipt_path = _supersession_receipt_path(ROUND_TRIP_EPOCH_SUCCESSOR_GENERATION)
    payload = {
        "schema": ROUND_TRIP_EPOCH_SUCCESSOR_MANIFEST_CONTRACT,
        "epoch_id": epoch_id,
        "epoch_generation": ROUND_TRIP_EPOCH_SUCCESSOR_GENERATION,
        "current_output_root": str(ROUND_TRIP_EPOCH_ROOT_PARENT / epoch_id),
        "archived_output_root": str(archived_root),
        "archived_epoch_id": archived_root.name,
        "archived_epoch_identity_file_sha256": _sha256_bytes(identity),
        "archived_capital_head_sequence": sequence,
        "archived_capital_head_checksum": checksum,
        "archived_epoch_policy": "read_only_archive_no_resume_no_aggregation",
        "capital_authority_id": ROUND_TRIP_CAPITAL_POLICY.authority_id,
        "capital_generation": ROUND_TRIP_CAPITAL_POLICY.generation,
        "capital_baseline_usdt": format(ROUND_TRIP_CAPITAL_POLICY.initial_cash, "f"),
        "aggregate_with_archived_epoch": False,
        "activate_current_epoch": False,
        "supersedes_manifest_path": str(predecessor.manifest_path),
        "supersedes_manifest_sha256": _sha256_bytes(predecessor_encoded),
        "supersedes_receipt_path": str(predecessor.supersession_receipt_path),
        "supersedes_receipt_sha256": _sha256_bytes(predecessor_receipt),
        "migration_reason": migration_reason,
        "supersession_receipt_path": str(receipt_path),
        "safety": dict(_EXPECTED_SAFETY),
    }
    context = CryptoRoundTripEpochContext(
        epoch_id=epoch_id,
        epoch_generation=ROUND_TRIP_EPOCH_SUCCESSOR_GENERATION,
        capital_generation=ROUND_TRIP_CAPITAL_POLICY.generation,
        output_root=ROUND_TRIP_EPOCH_ROOT_PARENT / epoch_id,
        archived_output_root=archived_root,
        archived_epoch_id=archived_root.name,
        archived_epoch_identity_file_sha256=_sha256_bytes(identity),
        archived_capital_head_checksum=checksum,
        manifest_path=manifest_path,
        manifest_sha256=_sha256(payload),
        aggregate_with_archived_epoch=False,
        _proof=_PROOF,
        versioned=True,
        supersedes_manifest_path=predecessor.manifest_path,
        supersedes_manifest_sha256=_sha256_bytes(predecessor_encoded),
        migration_reason=migration_reason,
        supersession_receipt_path=receipt_path,
        archived_capital_head_sequence=sequence,
        supersedes_receipt_path=predecessor.supersession_receipt_path,
        supersedes_receipt_sha256=_sha256_bytes(predecessor_receipt),
    )
    receipt = _supersession_receipt(context)
    _verify_existing_payload(
        manifest_path,
        payload,
        reason="round_trip_epoch_versioned_manifest_conflict",
    )
    _verify_existing_payload(
        receipt_path,
        receipt,
        reason="round_trip_epoch_supersession_receipt_invalid",
    )
    _atomic_create_or_verify(
        manifest_path,
        payload,
        reason="round_trip_epoch_versioned_manifest_conflict",
    )
    _atomic_create_or_verify(
        receipt_path,
        receipt,
        reason="round_trip_epoch_supersession_receipt_invalid",
    )
    return load_round_trip_epoch_manifest(manifest_path)


def prepare_recovery_successor_round_trip_epoch_manifest(
    *,
    epoch_id: str,
    supersedes_manifest_path: Path | str,
    migration_reason: str,
) -> CryptoRoundTripEpochContext:
    """Freeze G4 and create one isolated, non-aggregating G5 successor."""

    _assert_simulation_only()
    epoch_id = _epoch_id(epoch_id, generation=ROUND_TRIP_EPOCH_RECOVERY_GENERATION)
    predecessor_path = Path(supersedes_manifest_path)
    if (
        predecessor_path.parent != ROUND_TRIP_EPOCH_MANIFEST_DIRECTORY
        or not isinstance(migration_reason, str)
        or not migration_reason.strip()
        or len(migration_reason) > 512
    ):
        raise CryptoRoundTripEpochError("round_trip_epoch_migration_request_invalid")
    predecessor = load_round_trip_epoch_manifest(predecessor_path)
    if (
        predecessor.epoch_generation != ROUND_TRIP_EPOCH_SUCCESSOR_GENERATION
        or predecessor.supersession_receipt_path is None
        or predecessor.output_root.name.startswith(
            "crypto-delayed-paper-round-trip-epoch-g4-"
        )
        is False
    ):
        raise CryptoRoundTripEpochError("round_trip_epoch_successor_chain_invalid")
    _secure_directory(
        ROUND_TRIP_EPOCH_ROOT_PARENT, reason="round_trip_epoch_parent_untrusted"
    )
    _secure_directory(
        predecessor.output_root, reason="round_trip_epoch_superseded_root_untrusted"
    )
    try:
        predecessor_sequence, predecessor_checksum = RoundTripCapitalLedger(
            predecessor.output_root / "round_trip_capital"
        ).head()
    except (CryptoRoundTripError, OSError, TypeError, ValueError) as exc:
        raise CryptoRoundTripEpochError(
            "round_trip_epoch_superseded_head_untrusted"
        ) from exc
    if (
        predecessor_sequence <= 0
        or not isinstance(predecessor_checksum, str)
        or len(predecessor_checksum) != 64
    ):
        raise CryptoRoundTripEpochError("round_trip_epoch_superseded_head_untrusted")
    predecessor_encoded = _secure_versioned_manifest(
        predecessor.manifest_path,
        reason="round_trip_epoch_superseded_manifest_untrusted",
    )
    predecessor_receipt = _secure_versioned_manifest(
        predecessor.supersession_receipt_path,
        reason="round_trip_epoch_superseded_receipt_untrusted",
    )
    _secure_manifest_directory(create=True)
    manifest_path = _versioned_manifest_path(epoch_id)
    receipt_path = _supersession_receipt_path(ROUND_TRIP_EPOCH_RECOVERY_GENERATION)
    payload = {
        "schema": ROUND_TRIP_EPOCH_RECOVERY_MANIFEST_CONTRACT,
        "epoch_id": epoch_id,
        "epoch_generation": ROUND_TRIP_EPOCH_RECOVERY_GENERATION,
        "current_output_root": str(ROUND_TRIP_EPOCH_ROOT_PARENT / epoch_id),
        "archived_output_root": str(predecessor.archived_output_root),
        "archived_epoch_id": predecessor.archived_epoch_id,
        "archived_epoch_identity_file_sha256": predecessor.archived_epoch_identity_file_sha256,
        "archived_capital_head_sequence": predecessor.archived_capital_head_sequence,
        "archived_capital_head_checksum": predecessor.archived_capital_head_checksum,
        "archived_epoch_policy": "read_only_archive_no_resume_no_aggregation",
        "capital_authority_id": ROUND_TRIP_CAPITAL_POLICY.authority_id,
        "capital_generation": ROUND_TRIP_CAPITAL_POLICY.generation,
        "capital_baseline_usdt": format(ROUND_TRIP_CAPITAL_POLICY.initial_cash, "f"),
        "aggregate_with_archived_epoch": False,
        "activate_current_epoch": False,
        "supersedes_manifest_path": str(predecessor.manifest_path),
        "supersedes_manifest_sha256": _sha256_bytes(predecessor_encoded),
        "supersedes_receipt_path": str(predecessor.supersession_receipt_path),
        "supersedes_receipt_sha256": _sha256_bytes(predecessor_receipt),
        "supersedes_output_root": str(predecessor.output_root),
        "supersedes_capital_head_sequence": predecessor_sequence,
        "supersedes_capital_head_checksum": predecessor_checksum,
        "migration_reason": migration_reason,
        "supersession_receipt_path": str(receipt_path),
        "safety": dict(_EXPECTED_SAFETY),
    }
    context = CryptoRoundTripEpochContext(
        epoch_id=epoch_id,
        epoch_generation=ROUND_TRIP_EPOCH_RECOVERY_GENERATION,
        capital_generation=ROUND_TRIP_CAPITAL_POLICY.generation,
        output_root=ROUND_TRIP_EPOCH_ROOT_PARENT / epoch_id,
        archived_output_root=predecessor.archived_output_root,
        archived_epoch_id=predecessor.archived_epoch_id,
        archived_epoch_identity_file_sha256=predecessor.archived_epoch_identity_file_sha256,
        archived_capital_head_checksum=predecessor.archived_capital_head_checksum,
        manifest_path=manifest_path,
        manifest_sha256=_sha256(payload),
        aggregate_with_archived_epoch=False,
        _proof=_PROOF,
        versioned=True,
        supersedes_manifest_path=predecessor.manifest_path,
        supersedes_manifest_sha256=_sha256_bytes(predecessor_encoded),
        migration_reason=migration_reason,
        supersession_receipt_path=receipt_path,
        archived_capital_head_sequence=predecessor.archived_capital_head_sequence,
        supersedes_receipt_path=predecessor.supersession_receipt_path,
        supersedes_receipt_sha256=_sha256_bytes(predecessor_receipt),
        supersedes_output_root=predecessor.output_root,
        supersedes_capital_head_sequence=predecessor_sequence,
        supersedes_capital_head_checksum=predecessor_checksum,
    )
    receipt = _supersession_receipt(context)
    _verify_existing_payload(
        manifest_path, payload, reason="round_trip_epoch_versioned_manifest_conflict"
    )
    _verify_existing_payload(
        receipt_path, receipt, reason="round_trip_epoch_supersession_receipt_invalid"
    )
    _atomic_create_or_verify(
        manifest_path, payload, reason="round_trip_epoch_versioned_manifest_conflict"
    )
    _atomic_create_or_verify(
        receipt_path, receipt, reason="round_trip_epoch_supersession_receipt_invalid"
    )
    return load_round_trip_epoch_manifest(manifest_path)


def _identity(context: CryptoRoundTripEpochContext) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "contract": ROUND_TRIP_EPOCH_IDENTITY_CONTRACT,
        "epoch_id": context.epoch_id,
        "epoch_generation": context.epoch_generation,
        "capital_authority_id": ROUND_TRIP_CAPITAL_POLICY.authority_id,
        "capital_generation": context.capital_generation,
        "capital_baseline_usdt": format(
            ROUND_TRIP_CAPITAL_POLICY.initial_cash,
            "f",
        ),
        "current_output_root": str(context.output_root),
        "archived_output_root": str(context.archived_output_root),
        "archived_epoch_id": context.archived_epoch_id,
        "archived_epoch_identity_file_sha256": (
            context.archived_epoch_identity_file_sha256
        ),
        "archived_capital_head_checksum": context.archived_capital_head_checksum,
        "archived_epoch_policy": "read_only_archive_no_resume_no_aggregation",
        "aggregate_with_archived_epoch": False,
        "archived_epoch_consumed": False,
        "activate_current_epoch": False,
        "real_trading_enabled": False,
        "execution_authority": False,
        "production_eligible": False,
        "testnet_enabled": False,
        "live_broker_enabled": False,
        "model_network_enabled": False,
        "automatic_promotion_enabled": False,
        "automatic_risk_expansion_enabled": False,
        "manifest_sha256": context.manifest_sha256,
    }
    if context.versioned:
        payload.update(
            {
                "supersedes_manifest_path": str(context.supersedes_manifest_path),
                "supersedes_manifest_sha256": context.supersedes_manifest_sha256,
                "migration_reason": context.migration_reason,
                "supersession_receipt_path": str(context.supersession_receipt_path),
                "archived_capital_head_sequence": (
                    context.archived_capital_head_sequence
                ),
            }
        )
        if context.epoch_generation in {
            ROUND_TRIP_EPOCH_SUCCESSOR_GENERATION,
            ROUND_TRIP_EPOCH_RECOVERY_GENERATION,
        }:
            payload.update(
                {
                    "supersedes_receipt_path": str(context.supersedes_receipt_path),
                    "supersedes_receipt_sha256": context.supersedes_receipt_sha256,
                }
            )
        if context.epoch_generation == ROUND_TRIP_EPOCH_RECOVERY_GENERATION:
            payload.update(
                {
                    "supersedes_output_root": str(context.supersedes_output_root),
                    "supersedes_capital_head_sequence": context.supersedes_capital_head_sequence,
                    "supersedes_capital_head_checksum": context.supersedes_capital_head_checksum,
                }
            )
    payload["identity_sha256"] = _sha256(payload)
    return payload


def _write_identity(path: Path, payload: Mapping[str, Any]) -> None:
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8", closefd=True) as stream:
            descriptor = None
            stream.write(_canonical_json(payload) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as exc:
        raise CryptoRoundTripEpochError(
            "round_trip_epoch_identity_write_failed"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _verify_identity(context: CryptoRoundTripEpochContext) -> None:
    encoded = _secure_regular(
        context.identity_path,
        reason="round_trip_epoch_identity_untrusted",
        max_bytes=_MANIFEST_MAX_BYTES,
    )
    if encoded != (_canonical_json(_identity(context)) + "\n").encode():
        raise CryptoRoundTripEpochError("round_trip_epoch_identity_conflict")


def prepare_round_trip_epoch_candidate(
    context: CryptoRoundTripEpochContext,
) -> PreparedCryptoRoundTripEpoch:
    """Create/verify g3 without updating the active current-epoch pointer."""

    _assert_simulation_only()
    if (
        type(context) is not CryptoRoundTripEpochContext
        or context._proof is not _PROOF
        or context.epoch_generation
        not in {
            ROUND_TRIP_EPOCH_GENERATION,
            ROUND_TRIP_EPOCH_SUCCESSOR_GENERATION,
            ROUND_TRIP_EPOCH_RECOVERY_GENERATION,
        }
        or context.capital_generation != ROUND_TRIP_CAPITAL_POLICY.generation
        or context.aggregate_with_archived_epoch is not False
    ):
        raise CryptoRoundTripEpochError("round_trip_epoch_context_invalid")
    if load_round_trip_epoch_manifest(context.manifest_path) != context:
        raise CryptoRoundTripEpochError("round_trip_epoch_context_stale")
    if context.versioned:
        _verify_supersession_receipt(context)
    _secure_directory(
        ROUND_TRIP_EPOCH_ROOT_PARENT,
        reason="round_trip_epoch_parent_untrusted",
    )
    _verify_archive(context)
    if context.output_root.exists() or context.output_root.is_symlink():
        _secure_directory(
            context.output_root,
            reason="round_trip_epoch_output_root_untrusted",
        )
    else:
        try:
            context.output_root.mkdir(mode=0o700)
        except OSError as exc:
            raise CryptoRoundTripEpochError(
                "round_trip_epoch_output_root_create_failed"
            ) from exc
    if context.identity_path.exists() or context.identity_path.is_symlink():
        _verify_identity(context)
    else:
        if any(context.output_root.iterdir()):
            raise CryptoRoundTripEpochError("round_trip_epoch_output_root_unclaimed")
        _write_identity(context.identity_path, _identity(context))
        _verify_identity(context)
    _verify_archive(context)
    return PreparedCryptoRoundTripEpoch(context)


def main(argv: list[str] | None = None) -> int:
    """Create or verify one immutable g3 migration or its explicit g4 successor."""

    parser = argparse.ArgumentParser(
        description="Prepare the isolated Crypto g3 round-trip migration"
    )
    parser.add_argument("--epoch-id", required=True)
    parser.add_argument("--archived-output-root", type=Path, required=True)
    parser.add_argument("--migration-reason", required=True)
    parser.add_argument("--supersedes-manifest", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.supersedes_manifest is None:
            context = prepare_versioned_round_trip_epoch_manifest(
                epoch_id=args.epoch_id,
                archived_output_root=args.archived_output_root,
                migration_reason=args.migration_reason,
            )
        elif args.epoch_id.startswith("crypto-delayed-paper-round-trip-epoch-g5-"):
            context = prepare_recovery_successor_round_trip_epoch_manifest(
                epoch_id=args.epoch_id,
                supersedes_manifest_path=args.supersedes_manifest,
                migration_reason=args.migration_reason,
            )
        else:
            context = prepare_successor_round_trip_epoch_manifest(
                epoch_id=args.epoch_id,
                archived_output_root=args.archived_output_root,
                supersedes_manifest_path=args.supersedes_manifest,
                migration_reason=args.migration_reason,
            )
    except Exception:
        print("crypto round-trip epoch migration failed closed")
        return 2
    print(
        _canonical_json(
            {
                "status": "prepared",
                "epoch_id": context.epoch_id,
                "epoch_generation": context.epoch_generation,
                "manifest_path": str(context.manifest_path),
                "supersession_receipt_path": str(context.supersession_receipt_path),
                "real_trading_enabled": False,
                "execution_authority": False,
                "production_eligible": False,
            }
        )
    )
    return 0


__all__ = [
    "CryptoRoundTripEpochError",
    "PreparedCryptoRoundTripEpoch",
    "ROUND_TRIP_EPOCH_GENERATION",
    "ROUND_TRIP_EPOCH_SUCCESSOR_GENERATION",
    "ROUND_TRIP_EPOCH_RECOVERY_GENERATION",
    "ROUND_TRIP_EPOCH_MANIFEST_CONTRACT",
    "ROUND_TRIP_EPOCH_MANIFEST_DIRECTORY",
    "ROUND_TRIP_EPOCH_MANIFEST_PATH",
    "ROUND_TRIP_EPOCH_SUPERSESSION_RECEIPT_CONTRACT",
    "ROUND_TRIP_EPOCH_SUCCESSOR_RECEIPT_CONTRACT",
    "ROUND_TRIP_EPOCH_RECOVERY_RECEIPT_CONTRACT",
    "ROUND_TRIP_EPOCH_SUCCESSOR_MANIFEST_CONTRACT",
    "ROUND_TRIP_EPOCH_RECOVERY_MANIFEST_CONTRACT",
    "ROUND_TRIP_EPOCH_VERSIONED_MANIFEST_CONTRACT",
    "ROUND_TRIP_EPOCH_ROOT_PARENT",
    "load_round_trip_epoch_manifest",
    "main",
    "prepare_versioned_round_trip_epoch_manifest",
    "prepare_successor_round_trip_epoch_manifest",
    "prepare_recovery_successor_round_trip_epoch_manifest",
    "prepare_round_trip_epoch_candidate",
]


if __name__ == "__main__":  # pragma: no cover - CLI wrapper
    raise SystemExit(main())
