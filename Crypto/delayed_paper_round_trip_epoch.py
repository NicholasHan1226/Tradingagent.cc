"""Non-activating epoch-g3 candidate for round-trip capital generation 2."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any, Mapping
import uuid

from Crypto.fixture_sim.contracts import _assert_simulation_only
from Crypto.round_trip_capital import ROUND_TRIP_CAPITAL_POLICY


ROUND_TRIP_EPOCH_MANIFEST_CONTRACT = "tradingagent.crypto.round_trip_epoch_manifest.v1"
ROUND_TRIP_EPOCH_IDENTITY_CONTRACT = "tradingagent.crypto.round_trip_epoch_identity.v1"
ROUND_TRIP_EPOCH_GENERATION = 3
ROUND_TRIP_EPOCH_MANIFEST_PATH = Path(
    "/etc/tradingagent/crypto-delayed-paper-round-trip.epoch.json"
)
ROUND_TRIP_EPOCH_ROOT_PARENT = Path("/var/lib/tradingagent/crypto-delayed-paper-epochs")
_IDENTITY_FILENAME = ".round_trip_epoch_identity.json"
_MANIFEST_MAX_BYTES = 64 * 1024
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
        raise CryptoRoundTripEpochError("round_trip_epoch_manifest_path_invalid")
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
    encoded_head = _secure_regular(
        context.archived_output_root / "capital" / "head.json",
        reason="round_trip_archive_capital_head_untrusted",
        max_bytes=_MANIFEST_MAX_BYTES,
    )
    try:
        head = json.loads(encoded_head.decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CryptoRoundTripEpochError(
            "round_trip_archive_capital_head_untrusted"
        ) from exc
    if (
        not isinstance(head, Mapping)
        or head.get("checksum") != context.archived_capital_head_checksum
    ):
        raise CryptoRoundTripEpochError("round_trip_archive_capital_head_mismatch")


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
        or context.epoch_generation != ROUND_TRIP_EPOCH_GENERATION
        or context.capital_generation != ROUND_TRIP_CAPITAL_POLICY.generation
        or context.aggregate_with_archived_epoch is not False
    ):
        raise CryptoRoundTripEpochError("round_trip_epoch_context_invalid")
    if load_round_trip_epoch_manifest(context.manifest_path) != context:
        raise CryptoRoundTripEpochError("round_trip_epoch_context_stale")
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


__all__ = [
    "CryptoRoundTripEpochError",
    "PreparedCryptoRoundTripEpoch",
    "ROUND_TRIP_EPOCH_GENERATION",
    "ROUND_TRIP_EPOCH_MANIFEST_CONTRACT",
    "ROUND_TRIP_EPOCH_MANIFEST_PATH",
    "ROUND_TRIP_EPOCH_ROOT_PARENT",
    "load_round_trip_epoch_manifest",
    "prepare_round_trip_epoch_candidate",
]
