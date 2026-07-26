"""Fail-closed bearer-token file boundary for TradingDatas consumers.

Only an absolute service-configured file may create a token object.  The token
value and configured path are deliberately absent from repr/str/errors so the
runtime gate, integration receipts and logs cannot disclose either value.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path
import re
import stat


_MAX_TOKEN_BYTES = 4_096
# RFC 6750 bearer credentials may use base64-style trailing padding, but an
# assignment-like payload must never be accepted as a raw token file.
_TOKEN_RE = re.compile(r"^[A-Za-z0-9._~+/-]+={0,2}$")
_TOKEN_SEAL = object()
_SERVICE_SECRET_ROOT = Path("/run/secrets/tradingagent")


class TradingDatasTokenFileError(RuntimeError):
    """Stable redacted failure raised before any network request."""

    __slots__ = ("reason_code",)

    def __init__(self, reason_code: str) -> None:
        self.reason_code = str(reason_code)
        super().__init__(self.reason_code)

    def __repr__(self) -> str:
        return f"TradingDatasTokenFileError({self.reason_code!r})"


def _token_error(reason_code: str) -> TradingDatasTokenFileError:
    return TradingDatasTokenFileError(reason_code)


def _trusted_owner_uids() -> frozenset[int]:
    return frozenset({0, os.geteuid()})


def _service_secret_roots() -> tuple[Path, ...]:
    """Return the frozen service-managed roots allowed to hold credentials."""

    return (_SERVICE_SECRET_ROOT,)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _require_secure_open_capabilities() -> None:
    if (
        not getattr(os, "O_NOFOLLOW", 0)
        or not getattr(os, "O_DIRECTORY", 0)
        or os.open not in os.supports_dir_fd
        or os.stat not in os.supports_dir_fd
        or os.stat not in os.supports_follow_symlinks
    ):
        raise _token_error("tradingdatas_token_secure_open_unsupported")


def _directory_open_flags() -> int:
    """Open directory components without requiring directory read permission."""

    access_flag = getattr(os, "O_PATH", 0) or os.O_RDONLY
    return access_flag | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _open_token_no_follow(path: Path) -> tuple[int, int]:
    """Return parent and leaf descriptors opened component-by-component."""

    _require_secure_open_capabilities()
    directory_flags = _directory_open_flags()
    try:
        parent_descriptor = os.open(path.anchor, directory_flags)
    except OSError:
        raise _token_error("tradingdatas_token_parent_untrusted") from None

    try:
        for component in path.parts[1:-1]:
            try:
                next_descriptor = os.open(
                    component,
                    directory_flags,
                    dir_fd=parent_descriptor,
                )
            except OSError:
                raise _token_error("tradingdatas_token_parent_untrusted") from None
            os.close(parent_descriptor)
            parent_descriptor = next_descriptor
            if not stat.S_ISDIR(os.fstat(parent_descriptor).st_mode):
                raise _token_error("tradingdatas_token_parent_untrusted")

        leaf_flags = (
            os.O_RDONLY
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            leaf_descriptor = os.open(
                path.name,
                leaf_flags,
                dir_fd=parent_descriptor,
            )
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.EMLINK}:
                raise _token_error("tradingdatas_token_symlink_forbidden") from None
            if exc.errno == errno.ENOENT:
                raise _token_error("tradingdatas_token_missing") from None
            raise _token_error("tradingdatas_token_unavailable") from None
        return parent_descriptor, leaf_descriptor
    except Exception:
        os.close(parent_descriptor)
        raise


def _security_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_uid,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _read_bounded(descriptor: int) -> bytes:
    chunks: list[bytes] = []
    remaining = _MAX_TOKEN_BYTES + 1
    while remaining > 0:
        chunk = os.read(descriptor, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


class TradingDatasBearerToken:
    """In-memory secret with a permanently redacted public representation."""

    __slots__ = ("_value",)

    def __init__(self, value: str, *, _seal: object) -> None:
        if _seal is not _TOKEN_SEAL:
            raise _token_error("tradingdatas_token_file_source_required")
        self._value = value

    def __repr__(self) -> str:
        return "TradingDatasBearerToken(<redacted>)"

    def __str__(self) -> str:
        return "<redacted>"

    def _authorization_header(self) -> str:
        return f"Bearer {self._value}"


class TradingDatasTokenFile:
    """Read one raw token from an exact-0600 trusted regular file."""

    __slots__ = ("_path",)

    def __init__(self, path: Path | str) -> None:
        candidate = Path(path)
        normalized = Path(os.path.normpath(str(candidate)))
        if (
            not candidate.is_absolute()
            or not candidate.name
            or candidate != normalized
            or any(component in {".", ".."} for component in candidate.parts[1:])
        ):
            raise _token_error("tradingdatas_token_path_invalid")
        if not any(_is_within(candidate, root) for root in _service_secret_roots()):
            raise _token_error("tradingdatas_token_service_root_required")
        self._path = candidate

    def __repr__(self) -> str:
        return "TradingDatasTokenFile(<redacted-path>)"

    def read_token(self) -> TradingDatasBearerToken:
        parent_descriptor, leaf_descriptor = _open_token_no_follow(self._path)
        try:
            before = os.fstat(leaf_descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise _token_error("tradingdatas_token_regular_file_required")
            if before.st_uid not in _trusted_owner_uids():
                raise _token_error("tradingdatas_token_owner_invalid")
            if stat.S_IMODE(before.st_mode) != 0o600:
                raise _token_error("tradingdatas_token_mode_invalid")
            if before.st_size <= 0 or before.st_size > _MAX_TOKEN_BYTES:
                raise _token_error("tradingdatas_token_size_invalid")

            raw = _read_bounded(leaf_descriptor)
            after = os.fstat(leaf_descriptor)
            try:
                named = os.stat(
                    self._path.name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            except OSError:
                raise _token_error("tradingdatas_token_changed_during_read") from None
            if (
                len(raw) > _MAX_TOKEN_BYTES
                or _security_identity(before) != _security_identity(after)
                or (named.st_dev, named.st_ino) != (after.st_dev, after.st_ino)
            ):
                raise _token_error("tradingdatas_token_changed_during_read")

            try:
                os.lseek(leaf_descriptor, 0, os.SEEK_SET)
                verified_raw = _read_bounded(leaf_descriptor)
                verified_after = os.fstat(leaf_descriptor)
                verified_named = os.stat(
                    self._path.name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            except OSError:
                raise _token_error(
                    "tradingdatas_token_changed_during_read"
                ) from None
            if (
                raw != verified_raw
                or len(verified_raw) > _MAX_TOKEN_BYTES
                or _security_identity(after)
                != _security_identity(verified_after)
                or (verified_named.st_dev, verified_named.st_ino)
                != (verified_after.st_dev, verified_after.st_ino)
            ):
                raise _token_error("tradingdatas_token_changed_during_read")
        finally:
            os.close(leaf_descriptor)
            os.close(parent_descriptor)

        try:
            token = raw.decode("ascii")
        except UnicodeDecodeError:
            raise _token_error("tradingdatas_token_format_invalid") from None
        if not token or not _TOKEN_RE.fullmatch(token):
            raise _token_error("tradingdatas_token_format_invalid")
        return TradingDatasBearerToken(token, _seal=_TOKEN_SEAL)


__all__ = [
    "TradingDatasBearerToken",
    "TradingDatasTokenFile",
    "TradingDatasTokenFileError",
]
