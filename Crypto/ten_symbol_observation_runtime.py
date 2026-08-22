"""Minimal server CLI for the Crypto ten-symbol observation accumulator.

The runtime is a loopback-only, simulation-only, zero-authority evidence
accumulator for the ten-symbol 5-minute cohort.  It accepts only a frozen,
secret-free, repository-external manifest; the single credential path is the
dedicated TradingDatas Crypto read-token leaf and the HTTP transport is
constructed lazily so pending/same-slot recovery never needs the network.
The output root comes exclusively from the manifest-pinned runtime root; the
CLI deliberately has no ``--output-root`` flag.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import sys
import time
from typing import Any, Callable, Mapping
import urllib.error
import urllib.parse

from Crypto.market_observation import (
    FORTY_SYMBOL_BARS_SIDECAR_CONTRACT,
    FORTY_SYMBOL_SPREAD_CONTRACT,
    FORTY_SYMBOL_SPREADS_SIDECAR_CONTRACT,
    TEN_SYMBOL_BARS_SIDECAR_CONTRACT,
    TEN_SYMBOL_SPREAD_CONTRACT,
    TEN_SYMBOL_SPREADS_SIDECAR_CONTRACT,
    CryptoMarketObservation,
    CryptoMarketObservationError,
    CryptoObservationWindow,
    OBSERVATION_SYMBOLS,
    OBSERVATION_SYMBOLS_V40,
    _book_ticker_dataset_id,
    _collect_market_observation_rows_with_catalog,
    build_spread_event_block,
    build_ten_symbol_bars_sidecar,
    build_ten_symbol_spreads_sidecar,
    collect_book_ticker_spread_entries,
    observation_from_ten_symbol_bars_sidecar,
    unavailable_spread_event_block,
    validate_ten_symbol_spreads_sidecar,
)
from Crypto.ten_symbol_observation_profile import (
    FORTY_SYMBOL_PROFILE_CONTRACT,
    TEN_SYMBOL_PROFILE_CONTRACT,
    CryptoTenSymbolObservationProfile,
    CryptoTenSymbolProfileError,
    load_ten_symbol_observation_profile_payload,
)
from Crypto.ten_symbol_observation_store import (
    FORTY_SYMBOL_CONTRACTS,
    TEN_SYMBOL_CONTRACTS,
    TEN_SYMBOL_DATA_GAP_CONTRACT,
    TEN_SYMBOL_EVENT_CONTRACT,
    CryptoTenSymbolObservationContracts,
    CryptoTenSymbolObservationStore,
    CryptoTenSymbolObservationStoreError,
)
from shared.data.sharedsignals_v1 import (
    ContractViolation,
    HTTPStatusError,
    HTTPTransport,
    SharedSignalsV1Client,
    SharedSignalsV1Config,
    SharedSignalsV1Error,
    TransportNotConfigured,
)
from shared.data.tradingdatas_transport import (
    RuntimeGateConfigurationError,
    TradingDatasAuthenticationError,
    build_runtime_transport,
)


CRYPTO_TEN_SYMBOL_RUNTIME_CONTRACT = (
    "tradingagent.crypto.ten_symbol_observation_runtime.v1"
)
RUNTIME_MANIFEST_CONTRACT = (
    "tradingagent.crypto.ten_symbol_observation_runtime_manifest.v1"
)
RUNTIME_TOKEN_FILE = Path("/run/secrets/tradingagent/tradingdatas-crypto-read.token")
RUNTIME_OUTPUT_ROOT = Path("/var/lib/tradingagent/crypto-ten-symbol-observation")
RUNTIME_MANIFEST_MAX_BYTES = 256 * 1024
SLOT_CUTOFF_DELAY_SECONDS = 55
RUNTIME_TIMEOUT_SECONDS = 60.0
INVOCATION_BUDGET_SECONDS = 120.0
MAX_CYCLES_PER_INVOCATION = 2
# One catalog read plus ten bounded single-page bar queries per cycle, and
# the auxiliary spread leg's own catalog read plus ten book-ticker queries.
REQUESTS_PER_CYCLE = 22
# Bounded same-slot retry for transport-layer faults only: one initial
# attempt plus up to two retries with a fixed delay, still clamped by the
# absolute invocation budget and the unit stop line.
MAX_COLLECT_ATTEMPTS = 3
COLLECT_RETRY_DELAY_SECONDS = 20.0
OUTAGE_GAP_CONTRACT = TEN_SYMBOL_DATA_GAP_CONTRACT
HISTORICAL_WINDOW_UNRECOVERABLE_REASON = "crypto_observation_watermark_invalid"
WARMUP_WINDOW_INCOMPLETE_REASON = "crypto_observation_query_shape_invalid"
DATA_INCOMPLETE_REASONS = frozenset(
    {
        WARMUP_WINDOW_INCOMPLETE_REASON,
        "crypto_observation_data_source_unavailable",
        "crypto_observation_watermark_invalid",
        "crypto_observation_data_through_early",
    }
)
HISTORICAL_GAP_RECOVERY_REASONS = frozenset(
    {
        HISTORICAL_WINDOW_UNRECOVERABLE_REASON,
        # A historical slot whose source rows are permanently missing (e.g. an
        # upstream collection gap) fails the shape gate on every retry and can
        # never complete; only ever gap-recovered when the slot is strictly
        # historical (guarded at the call site), so contract drift on the
        # current slot still fails closed.  The gap contract records the
        # skipped range explicitly.
        WARMUP_WINDOW_INCOMPLETE_REASON,
    }
)
_REPO_ROOT = Path(__file__).resolve().parents[1]
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
        "base_url",
        "catalog_version",
        "access_policy_id",
        "output_root",
        "profile_sha256",
        "profile",
        "safety",
    }
)


@dataclass(frozen=True)
class CryptoTenSymbolObservationRuntimeConfig:
    """Versioned runtime family: identity, query budget, and settle clock."""

    runtime_contract: str
    manifest_contract: str
    output_root: Path
    symbols: tuple[str, ...]
    profile_contract: str
    store_contracts: CryptoTenSymbolObservationContracts
    bars_sidecar_contract: str
    spread_contract: str
    spreads_sidecar_contract: str
    requests_per_cycle: int
    event_id_prefix: str
    reason_code_prefix: str
    slot_settle_delay_seconds: int


TEN_SYMBOL_RUNTIME_CONFIG = CryptoTenSymbolObservationRuntimeConfig(
    runtime_contract=CRYPTO_TEN_SYMBOL_RUNTIME_CONTRACT,
    manifest_contract=RUNTIME_MANIFEST_CONTRACT,
    output_root=RUNTIME_OUTPUT_ROOT,
    symbols=OBSERVATION_SYMBOLS,
    profile_contract=TEN_SYMBOL_PROFILE_CONTRACT,
    store_contracts=TEN_SYMBOL_CONTRACTS,
    bars_sidecar_contract=TEN_SYMBOL_BARS_SIDECAR_CONTRACT,
    spread_contract=TEN_SYMBOL_SPREAD_CONTRACT,
    spreads_sidecar_contract=TEN_SYMBOL_SPREADS_SIDECAR_CONTRACT,
    requests_per_cycle=REQUESTS_PER_CYCLE,
    event_id_prefix="crypto-ten",
    reason_code_prefix="crypto_ten_symbol",
    slot_settle_delay_seconds=SLOT_CUTOFF_DELAY_SECONDS,
)
FORTY_SYMBOL_RUNTIME_CONFIG = CryptoTenSymbolObservationRuntimeConfig(
    runtime_contract="tradingagent.crypto.forty_symbol_observation_runtime.v1",
    manifest_contract="tradingagent.crypto.forty_symbol_observation_runtime_manifest.v1",
    output_root=Path("/var/lib/tradingagent/crypto-40-symbol-observation"),
    symbols=OBSERVATION_SYMBOLS_V40,
    profile_contract=FORTY_SYMBOL_PROFILE_CONTRACT,
    store_contracts=FORTY_SYMBOL_CONTRACTS,
    bars_sidecar_contract=FORTY_SYMBOL_BARS_SIDECAR_CONTRACT,
    spread_contract=FORTY_SYMBOL_SPREAD_CONTRACT,
    spreads_sidecar_contract=FORTY_SYMBOL_SPREADS_SIDECAR_CONTRACT,
    requests_per_cycle=2 + 2 * len(OBSERVATION_SYMBOLS_V40),
    event_id_prefix="crypto-forty",
    reason_code_prefix="crypto_forty_symbol",
    # The isolated forty-symbol collector can finish after the core +55s
    # receipt boundary.  Its reader runs at close +3m45s, so bind the PIT
    # cutoff to that fixed, replayable availability boundary instead of
    # rejecting an honest receipt merely because collection took over 55s.
    slot_settle_delay_seconds=225,
)


def _family_event_id(
    config: CryptoTenSymbolObservationRuntimeConfig,
    event_type: str,
    material: Mapping[str, Any],
) -> str:
    return f"{config.event_id_prefix}-{event_type}-{_sha256(material)[:24]}"


def _family_reason(
    config: CryptoTenSymbolObservationRuntimeConfig,
    suffix: str,
) -> str:
    return f"{config.reason_code_prefix}_{suffix}"


def _resolved_output_root(
    config: CryptoTenSymbolObservationRuntimeConfig,
) -> Path:
    """Resolve the pinned runtime root for one family.

    The ten-symbol root stays a module-level constant so the existing
    isolated test harness can monkeypatch ``RUNTIME_OUTPUT_ROOT`` per test;
    the forty-symbol root is deliberately frozen in its own config and never
    aliases the ten-symbol production root.
    """

    if config is TEN_SYMBOL_RUNTIME_CONFIG:
        return RUNTIME_OUTPUT_ROOT
    return config.output_root


class CryptoTenSymbolObservationRuntimeError(RuntimeError):
    """Stable, redacted fail-closed runtime error."""


class _InvocationBudgetExhausted(RuntimeError):
    """Internal signal that the bounded systemd invocation must defer."""


def _remaining_invocation_seconds(started_at: float, budget_seconds: float) -> float:
    remaining = budget_seconds - (time.monotonic() - started_at)
    if remaining <= 0:
        raise _InvocationBudgetExhausted(
            "ten_symbol_observation_invocation_budget_exhausted"
        )
    return remaining


def _deadline_bound_transport_factory(
    transport_factory: Callable[..., HTTPTransport],
    *,
    started_at: float,
    budget_seconds: float,
) -> Callable[..., HTTPTransport]:
    """Clamp every TradingDatas wire call to one absolute invocation budget."""

    def build(*args: Any, **kwargs: Any) -> HTTPTransport:
        transport = transport_factory(*args, **kwargs)

        def send(**request: Any) -> Any:
            requested_timeout = float(request["timeout_seconds"])
            remaining = _remaining_invocation_seconds(started_at, budget_seconds)
            request["timeout_seconds"] = min(requested_timeout, remaining)
            try:
                response = transport(**request)
            except Exception:
                if time.monotonic() - started_at >= budget_seconds:
                    raise _InvocationBudgetExhausted(
                        "ten_symbol_observation_invocation_budget_exhausted"
                    ) from None
                raise
            if time.monotonic() - started_at >= budget_seconds:
                raise _InvocationBudgetExhausted(
                    "ten_symbol_observation_invocation_budget_exhausted"
                )
            return response

        return send

    return build


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
        raise CryptoTenSymbolObservationRuntimeError(
            "runtime_manifest_not_canonical"
        ) from exc


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _mapping(value: Any, reason: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CryptoTenSymbolObservationRuntimeError(reason)
    return value


def _exact_keys(
    value: Mapping[str, Any],
    expected: frozenset[str],
    reason: str,
) -> None:
    if set(value) != set(expected):
        raise CryptoTenSymbolObservationRuntimeError(reason)


def _native_text(value: Any, reason: str, *, max_chars: int = 512) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > max_chars
        or any(ord(character) < 0x20 for character in value)
    ):
        raise CryptoTenSymbolObservationRuntimeError(reason)
    return value


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if type(key) is not str or key in result:
            raise CryptoTenSymbolObservationRuntimeError(
                "runtime_manifest_duplicate_key"
            )
        result[key] = value
    return result


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _read_external_manifest(path: Path | str) -> dict[str, Any]:
    manifest_path = Path(path)
    if not manifest_path.is_absolute():
        raise CryptoTenSymbolObservationRuntimeError(
            "runtime_manifest_path_must_be_absolute"
        )
    try:
        resolved = manifest_path.resolve(strict=True)
    except OSError as exc:
        raise CryptoTenSymbolObservationRuntimeError(
            "runtime_manifest_missing"
        ) from exc
    if resolved != manifest_path or _is_within(resolved, _REPO_ROOT):
        raise CryptoTenSymbolObservationRuntimeError("runtime_manifest_file_untrusted")
    descriptor: int | None = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(manifest_path, flags)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid not in {0, os.geteuid()}
            or metadata.st_mode & 0o022
            or metadata.st_size <= 0
            or metadata.st_size > RUNTIME_MANIFEST_MAX_BYTES
        ):
            raise CryptoTenSymbolObservationRuntimeError(
                "runtime_manifest_file_untrusted"
            )
        chunks: list[bytes] = []
        remaining = RUNTIME_MANIFEST_MAX_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw_bytes = b"".join(chunks)
        after = os.fstat(descriptor)
        current = manifest_path.lstat()
        if (
            after.st_dev != metadata.st_dev
            or after.st_ino != metadata.st_ino
            or after.st_size != metadata.st_size
            or after.st_mtime_ns != metadata.st_mtime_ns
            or current.st_dev != metadata.st_dev
            or current.st_ino != metadata.st_ino
            or manifest_path.resolve(strict=True) != manifest_path
            or len(raw_bytes) != metadata.st_size
        ):
            raise CryptoTenSymbolObservationRuntimeError(
                "runtime_manifest_changed_during_read"
            )
        if not raw_bytes.endswith(b"\n") or b"\x00" in raw_bytes:
            raise CryptoTenSymbolObservationRuntimeError(
                "runtime_manifest_encoding_invalid"
            )
        decoded = json.loads(
            raw_bytes.decode("utf-8"),
            object_pairs_hook=_strict_object,
        )
    except CryptoTenSymbolObservationRuntimeError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CryptoTenSymbolObservationRuntimeError(
            "runtime_manifest_json_invalid"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if not isinstance(decoded, dict):
        raise CryptoTenSymbolObservationRuntimeError(
            "runtime_manifest_object_required"
        )
    return decoded


def _loopback_base_url(value: Any) -> str:
    base_url = _native_text(value, "runtime_base_url_invalid")
    try:
        parsed = urllib.parse.urlsplit(base_url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise CryptoTenSymbolObservationRuntimeError(
            "runtime_base_url_invalid"
        ) from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or hostname is None
        or port is None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
        or parsed.netloc.endswith(":")
        or "%" in parsed.netloc
    ):
        raise CryptoTenSymbolObservationRuntimeError("runtime_base_url_invalid")
    try:
        host = ipaddress.ip_address(hostname)
    except ValueError as exc:
        raise CryptoTenSymbolObservationRuntimeError(
            "runtime_base_url_must_be_loopback"
        ) from exc
    if not host.is_loopback:
        raise CryptoTenSymbolObservationRuntimeError(
            "runtime_base_url_must_be_loopback"
        )
    canonical = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    if canonical != base_url:
        raise CryptoTenSymbolObservationRuntimeError("runtime_base_url_invalid")
    return base_url


@dataclass(frozen=True)
class CryptoTenSymbolObservationRuntimeManifest:
    base_url: str
    catalog_version: str
    access_policy_id: str
    output_root: Path
    profile: CryptoTenSymbolObservationProfile
    profile_sha256: str
    sha256: str

    @property
    def dataset_ids(self) -> frozenset[str]:
        return frozenset(dataset.dataset_id for dataset in self.profile.datasets)


def load_crypto_ten_symbol_observation_runtime_manifest(
    path: Path | str,
    *,
    config: CryptoTenSymbolObservationRuntimeConfig = TEN_SYMBOL_RUNTIME_CONFIG,
) -> CryptoTenSymbolObservationRuntimeManifest:
    raw = _read_external_manifest(path)
    _exact_keys(raw, _MANIFEST_KEYS, "runtime_manifest_keys_invalid")
    if raw.get("schema") != config.manifest_contract:
        raise CryptoTenSymbolObservationRuntimeError("runtime_manifest_schema_invalid")
    safety = _mapping(
        raw.get("safety"),
        "runtime_safety_contract_invalid",
    )
    if dict(safety) != _EXPECTED_SAFETY:
        raise CryptoTenSymbolObservationRuntimeError(
            "runtime_safety_contract_invalid"
        )
    try:
        profile = load_ten_symbol_observation_profile_payload(
            _mapping(raw.get("profile"), "runtime_profile_invalid"),
            symbols=config.symbols,
            profile_contract=config.profile_contract,
        )
    except CryptoTenSymbolProfileError as exc:
        raise CryptoTenSymbolObservationRuntimeError(
            "runtime_profile_invalid"
        ) from exc
    profile_sha256 = _native_text(
        raw.get("profile_sha256"),
        "runtime_profile_sha256_invalid",
    )
    if (
        len(profile_sha256) != 64
        or any(character not in "0123456789abcdef" for character in profile_sha256)
        or profile.profile_sha256 != profile_sha256
    ):
        raise CryptoTenSymbolObservationRuntimeError(
            "runtime_profile_sha256_mismatch"
        )
    catalog_version = _native_text(
        raw.get("catalog_version"),
        "runtime_catalog_version_invalid",
    )
    if profile.catalog_version != catalog_version:
        raise CryptoTenSymbolObservationRuntimeError(
            "runtime_catalog_profile_mismatch"
        )
    access_policy_id = _native_text(
        raw.get("access_policy_id"),
        "runtime_access_policy_invalid",
        max_chars=128,
    )
    if any(
        token in access_policy_id.lower()
        for token in ("token", "secret", "authorization", "bearer")
    ):
        raise CryptoTenSymbolObservationRuntimeError("runtime_access_policy_invalid")
    output_root_text = raw.get("output_root")
    if not isinstance(output_root_text, str) or not output_root_text:
        raise CryptoTenSymbolObservationRuntimeError("runtime_output_root_invalid")
    output_root = Path(output_root_text)
    if not output_root.is_absolute() or output_root != _resolved_output_root(config):
        raise CryptoTenSymbolObservationRuntimeError("runtime_output_root_invalid")
    return CryptoTenSymbolObservationRuntimeManifest(
        base_url=_loopback_base_url(raw.get("base_url")),
        catalog_version=catalog_version,
        access_policy_id=access_policy_id,
        output_root=output_root,
        profile=profile,
        profile_sha256=profile_sha256,
        sha256=_sha256(raw),
    )


def _assert_simulation_only() -> None:
    if os.environ.get("REAL_TRADING_ENABLED") != "false":
        raise CryptoTenSymbolObservationRuntimeError(
            "runtime_real_trading_must_be_disabled"
        )


def _utc_now(value: datetime) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
        or value.utcoffset() != timedelta(0)
    ):
        raise CryptoTenSymbolObservationRuntimeError("runtime_now_must_be_utc")
    return value.astimezone(timezone.utc)


def crypto_ten_symbol_observation_window(
    now: datetime,
    *,
    config: CryptoTenSymbolObservationRuntimeConfig = TEN_SYMBOL_RUNTIME_CONFIG,
) -> CryptoObservationWindow:
    """Derive the latest family-specific, receipt-safe deterministic slot."""

    observed = _utc_now(now)
    settle_delay = config.slot_settle_delay_seconds
    if settle_delay < 0 or settle_delay >= 5 * 60:
        raise CryptoTenSymbolObservationRuntimeError(
            "runtime_slot_settle_delay_invalid"
        )
    eligible = observed - timedelta(seconds=settle_delay)
    window_end = eligible.replace(
        minute=eligible.minute - eligible.minute % 5,
        second=0,
        microsecond=0,
    )
    return CryptoObservationWindow(
        window_end=window_end,
        observation_cutoff=window_end + timedelta(seconds=settle_delay),
    )


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _window_for_end(
    window_end: datetime,
    *,
    config: CryptoTenSymbolObservationRuntimeConfig = TEN_SYMBOL_RUNTIME_CONFIG,
) -> CryptoObservationWindow:
    normalized = _utc_now(window_end)
    if (
        normalized.second != 0
        or normalized.microsecond != 0
        or normalized.minute % 5 != 0
    ):
        raise CryptoTenSymbolObservationRuntimeError("runtime_window_end_invalid")
    return CryptoObservationWindow(
        window_end=normalized,
        observation_cutoff=normalized
        + timedelta(seconds=config.slot_settle_delay_seconds),
    )


def _non_authority_receipt_fields() -> dict[str, Any]:
    return {
        "authority": "none",
        "execution_eligible": False,
        "capital_write_eligible": False,
        "model_authority": False,
        "real_trading_enabled": False,
        "production_eligible": False,
    }


def _is_retryable_transport_error(exc: BaseException) -> bool:
    """Classify transport-layer faults that merit one bounded same-slot retry.

    Only timeout/connection-class failures are retryable.  HTTP status
    errors (including 401/403, which must never be retried), catalog/profile
    or data-contract failures and the invocation budget signal are semantic
    and always fail immediately.
    """

    pending: list[BaseException] = [exc]
    seen: set[int] = set()
    saw_transport = False
    while pending:
        current = pending.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(
            current,
            (
                urllib.error.HTTPError,
                HTTPStatusError,
                TradingDatasAuthenticationError,
                CryptoMarketObservationError,
                CryptoTenSymbolProfileError,
                _InvocationBudgetExhausted,
            ),
        ):
            return False
        if isinstance(
            current,
            (
                TimeoutError,
                ConnectionError,
                urllib.error.URLError,
                http.client.HTTPException,
            ),
        ):
            saw_transport = True
        related: list[BaseException] = []
        if current.__cause__ is not None:
            related.append(current.__cause__)
        if current.__context__ is not None:
            related.append(current.__context__)
        reason = getattr(current, "reason", None)
        if isinstance(reason, BaseException):
            related.append(reason)
        pending.extend(candidate for candidate in related if id(candidate) not in seen)
    return saw_transport


def _spread_dataset_ids(
    config: CryptoTenSymbolObservationRuntimeConfig = TEN_SYMBOL_RUNTIME_CONFIG,
) -> frozenset[str]:
    return frozenset(_book_ticker_dataset_id(symbol) for symbol in config.symbols)


def _spread_failure_reason(exc: BaseException) -> str:
    """Map one spread-leg failure to a stable, redacted degradation reason.

    The spread leg is auxiliary evidence: its failures are recorded as a
    leg-wide ``unavailable`` status and never fail the bar observation.
    Reason codes never interpolate payloads, so they are safe to persist.
    """

    if isinstance(exc, CryptoMarketObservationError):
        return str(exc)
    if isinstance(exc, TradingDatasAuthenticationError):
        return "crypto_spread_auth_unavailable"
    if isinstance(exc, (urllib.error.HTTPError, HTTPStatusError)):
        return "crypto_spread_http_unavailable"
    if isinstance(exc, (CryptoTenSymbolProfileError, SharedSignalsV1Error)):
        return "crypto_spread_contract_unavailable"
    if isinstance(
        exc,
        (
            TimeoutError,
            ConnectionError,
            urllib.error.URLError,
            http.client.HTTPException,
        ),
    ):
        return "crypto_spread_transport_unavailable"
    return "crypto_spread_unexpected_error"


class _LazyObservationPort:
    """Construct the authenticated transport only if fresh data is needed."""

    def __init__(
        self,
        *,
        manifest: CryptoTenSymbolObservationRuntimeManifest,
        token_file: Path,
        transport_factory: Callable[..., HTTPTransport],
        config: CryptoTenSymbolObservationRuntimeConfig = TEN_SYMBOL_RUNTIME_CONFIG,
        timeout_seconds: float = RUNTIME_TIMEOUT_SECONDS,
        retry_sleep: Callable[[float], None] = time.sleep,
        budget_check: Callable[[], float] | None = None,
    ) -> None:
        self._manifest = manifest
        self._token_file = token_file
        self._config = config
        self._transport_factory = transport_factory
        self._timeout_seconds = timeout_seconds
        self._retry_sleep = retry_sleep
        self._budget_check = budget_check or (lambda: float("inf"))
        self.collect_calls = 0
        self.collect_attempts = 0
        self.transport_factory_attempts = 0
        self.transport_constructed_count = 0
        self.observed_catalog_version: str | None = None

    def collect(
        self, window: CryptoObservationWindow
    ) -> tuple[
        CryptoMarketObservation,
        dict[str, list[dict[str, Any]]],
        dict[str, Any],
    ]:
        """Collect one slot, retrying only transport-layer faults in-place.

        Every attempt constructs a fresh transport and client from the same
        frozen manifest configuration; the slot cutoff never moves.  After
        the final attempt the exact original failure propagates, preserving
        the established fail-closed paths (data_reject for wrapped semantic
        errors, runtime failure for raw transport errors).  The auxiliary
        spread leg rides on the successful bar attempt and is error-isolated:
        its failures degrade to a recorded status instead of triggering the
        bar retry path or failing the slot.
        """

        self.collect_calls += 1
        for attempt in range(1, MAX_COLLECT_ATTEMPTS + 1):
            # This check is deliberately before incrementing the receipt
            # counter: an attempt whose transport/client was never built is
            # not an attempt, even when a previous retry sleep consumed the
            # remaining absolute invocation budget.
            self._budget_check()
            self.collect_attempts += 1
            try:
                return self._collect_once(window)
            except Exception as exc:
                if attempt < MAX_COLLECT_ATTEMPTS and (
                    _is_retryable_transport_error(exc)
                ):
                    self._retry_sleep(COLLECT_RETRY_DELAY_SECONDS)
                    # Retry sleeps are part of the same invocation budget;
                    # never construct a fresh transport after the deadline.
                    self._budget_check()
                    continue
                raise
        raise CryptoTenSymbolObservationRuntimeError("runtime_collect_unreachable")

    def _collect_spread(
        self,
        *,
        transport: HTTPTransport,
        window: CryptoObservationWindow,
    ) -> dict[str, Any]:
        """Best-effort book-ticker sampling on the successful bar attempt.

        The leg uses its own client configured with exactly the ten
        book-ticker dataset IDs, so a missing or drifted spread dataset can
        never fail the bar leg's catalog gate.  Per-symbol faults are
        captured as rejected entries; a leg-wide fault (its own catalog
        read, for example) degrades to one recorded ``unavailable`` reason.
        The invocation-budget signal always propagates untouched.
        """

        try:
            client = SharedSignalsV1Client(
                SharedSignalsV1Config(
                    base_url=self._manifest.base_url,
                    expected_catalog_version=(self._manifest.catalog_version),
                    dataset_ids=_spread_dataset_ids(self._config),
                    access_policy_id=self._manifest.access_policy_id,
                    catalog_version_policy="evidence_only",
                    timeout_seconds=self._timeout_seconds,
                    max_limit=500,
                    cache_ttl_seconds=0,
                ),
                transport=transport,
            )
            catalog = client.get_catalog()
            entries = collect_book_ticker_spread_entries(
                client,
                catalog=catalog,
                expected_catalog_version=catalog.catalog_version,
                window=window,
                symbols=self._config.symbols,
            )
            sidecar = build_ten_symbol_spreads_sidecar(
                window=window,
                profile_sha256=self._manifest.profile.profile_sha256,
                catalog_version=catalog.catalog_version,
                entries=entries,
                symbols=self._config.symbols,
                spreads_sidecar_contract=self._config.spreads_sidecar_contract,
            )
            return {"sidecar": sidecar, "unavailable_reason": None}
        except _InvocationBudgetExhausted:
            raise
        except Exception as exc:
            return {"sidecar": None, "unavailable_reason": _spread_failure_reason(exc)}

    def _collect_once(
        self, window: CryptoObservationWindow
    ) -> tuple[
        CryptoMarketObservation,
        dict[str, list[dict[str, Any]]],
        dict[str, Any],
    ]:
        try:
            self.transport_factory_attempts += 1
            transport = self._transport_factory(
                "http-json-v1",
                token_file=self._token_file,
                base_url=self._manifest.base_url,
            )
            self.transport_constructed_count += 1
            client = SharedSignalsV1Client(
                SharedSignalsV1Config(
                    base_url=self._manifest.base_url,
                    expected_catalog_version=(self._manifest.catalog_version),
                    dataset_ids=self._manifest.dataset_ids,
                    access_policy_id=self._manifest.access_policy_id,
                    catalog_version_policy="evidence_only",
                    timeout_seconds=self._timeout_seconds,
                    max_limit=500,
                    cache_ttl_seconds=0,
                ),
                transport=transport,
            )
            catalog = client.get_catalog()
            self.observed_catalog_version = catalog.catalog_version
            self._manifest.profile.verify_catalog(catalog)
            observation, rows_by_symbol = (
                _collect_market_observation_rows_with_catalog(
                    client,
                    catalog=catalog,
                    expected_catalog_version=catalog.catalog_version,
                    window=window,
                    symbols=self._config.symbols,
                )
            )
            spread = self._collect_spread(transport=transport, window=window)
            return observation, rows_by_symbol, spread
        except CryptoMarketObservationError:
            raise
        except CryptoTenSymbolProfileError:
            raise
        except (
            ContractViolation,
            RuntimeGateConfigurationError,
            TradingDatasAuthenticationError,
            TransportNotConfigured,
        ):
            # Configuration, credentials, and response-contract failures are
            # integrity failures.  They must remain fail-closed even though
            # ordinary source unavailability is observable simulation data
            # loss.
            raise
        except HTTPStatusError as exc:
            if re.search(r"HTTP (401|403)$", str(exc)):
                raise
            raise CryptoMarketObservationError(
                "crypto_observation_data_source_unavailable"
            ) from exc
        except SharedSignalsV1Error as exc:
            raise CryptoMarketObservationError(
                "crypto_observation_data_source_unavailable"
            ) from exc
        except (TypeError, ValueError) as exc:
            raise CryptoMarketObservationError(
                _family_reason(config, "transport_configuration_invalid")
            ) from exc


def _require_exact_service_paths(
    *,
    token_file: Path | str,
    output_root: Path | str,
    config: CryptoTenSymbolObservationRuntimeConfig = TEN_SYMBOL_RUNTIME_CONFIG,
) -> tuple[Path, Path]:
    token = Path(token_file)
    root = Path(output_root)
    if token != RUNTIME_TOKEN_FILE:
        raise CryptoTenSymbolObservationRuntimeError(
            "runtime_token_file_must_equal_dedicated_leaf"
        )
    if not token.is_absolute() or not root.is_absolute():
        raise CryptoTenSymbolObservationRuntimeError(
            "runtime_service_paths_must_be_absolute"
        )
    if root != _resolved_output_root(config):
        raise CryptoTenSymbolObservationRuntimeError(
            "runtime_output_root_invalid"
        )
    return token, root


def _observation_event(
    *,
    manifest: CryptoTenSymbolObservationRuntimeManifest,
    window: CryptoObservationWindow,
    observation: CryptoMarketObservation,
    spread_block: Mapping[str, Any],
    config: CryptoTenSymbolObservationRuntimeConfig = TEN_SYMBOL_RUNTIME_CONFIG,
) -> dict[str, Any]:
    id_material = {
        "event_type": "observation",
        "window_end": _iso_utc(window.window_end),
        "observation_sha256": observation.observation_sha256,
        "profile_sha256": manifest.profile.profile_sha256,
    }
    return {
        "contract": config.store_contracts.event,
        "event_id": _family_event_id(config, "observation", id_material),
        "event_type": "observation",
        "market": "crypto",
        "market_session": "24x7",
        "window_end": _iso_utc(window.window_end),
        "observation_cutoff": _iso_utc(window.observation_cutoff),
        "catalog_version": observation.catalog_version,
        "profile_sha256": manifest.profile.profile_sha256,
        "observation": observation.to_payload(),
        "spread": dict(spread_block),
        "authority": "none",
        "execution_eligible": False,
        "capital_write_eligible": False,
        "model_authority": False,
    }


def _data_reject_event(
    *,
    manifest: CryptoTenSymbolObservationRuntimeManifest,
    window: CryptoObservationWindow,
    reason_code: str,
    config: CryptoTenSymbolObservationRuntimeConfig = TEN_SYMBOL_RUNTIME_CONFIG,
) -> dict[str, Any]:
    id_material = {
        "event_type": "data_reject",
        "window_end": _iso_utc(window.window_end),
        "observation_cutoff": _iso_utc(window.observation_cutoff),
        "reason_code": reason_code,
        "profile_sha256": manifest.profile.profile_sha256,
        "catalog_version": manifest.catalog_version,
    }
    return {
        "contract": config.store_contracts.event,
        "event_id": _family_event_id(config, "data-reject", id_material),
        "event_type": "data_reject",
        "market": "crypto",
        "market_session": "24x7",
        "window_end": _iso_utc(window.window_end),
        "observation_cutoff": _iso_utc(window.observation_cutoff),
        "catalog_version": manifest.catalog_version,
        "profile_sha256": manifest.profile.profile_sha256,
        "reason_code": reason_code,
        "authority": "none",
        "execution_eligible": False,
        "capital_write_eligible": False,
        "model_authority": False,
    }


def _data_gap_event(
    *,
    manifest: CryptoTenSymbolObservationRuntimeManifest,
    prior_market_slot: datetime,
    rejected_window: CryptoObservationWindow,
    current_window: CryptoObservationWindow,
    reason_code: str,
    observation: CryptoMarketObservation,
    spread_block: Mapping[str, Any],
    config: CryptoTenSymbolObservationRuntimeConfig = TEN_SYMBOL_RUNTIME_CONFIG,
) -> dict[str, Any]:
    recovery = current_window.window_end
    skipped_from = prior_market_slot + timedelta(minutes=5)
    skipped_to = recovery - timedelta(minutes=5)
    if skipped_from > skipped_to:
        raise CryptoTenSymbolObservationRuntimeError(
            "runtime_outage_gap_not_recoverable"
        )
    observation_sha = observation.observation_sha256
    id_material = {
        "gap_contract": config.store_contracts.data_gap,
        "prior_market_slot": _iso_utc(prior_market_slot),
        "skipped_from": _iso_utc(skipped_from),
        "skipped_to": _iso_utc(skipped_to),
        "recovery_market_slot": _iso_utc(recovery),
        "reason_code": reason_code,
        "recovery_observation_sha256": observation_sha,
    }
    return {
        "contract": config.store_contracts.event,
        "gap_contract": config.store_contracts.data_gap,
        "event_id": _family_event_id(config, "data-gap", id_material),
        "event_type": "data_gap",
        "market": "crypto",
        "market_session": "24x7",
        "window_end": _iso_utc(recovery),
        "observation_cutoff": _iso_utc(current_window.observation_cutoff),
        "prior_market_slot": _iso_utc(prior_market_slot),
        "skipped_from": _iso_utc(skipped_from),
        "skipped_to": _iso_utc(skipped_to),
        "recovery_market_slot": _iso_utc(recovery),
        "reason_code": reason_code,
        "rejected_target_window_end": _iso_utc(rejected_window.window_end),
        "rejected_target_observation_cutoff": _iso_utc(
            rejected_window.observation_cutoff
        ),
        "catalog_version": observation.catalog_version,
        "profile_sha256": manifest.profile.profile_sha256,
        "recovery_observation": observation.to_payload(),
        "spread": dict(spread_block),
        "authority": "none",
        "execution_eligible": False,
        "capital_write_eligible": False,
        "model_authority": False,
    }


def _append_reject(
    *,
    store: CryptoTenSymbolObservationStore,
    manifest: CryptoTenSymbolObservationRuntimeManifest,
    window: CryptoObservationWindow,
    reason_code: str,
    config: CryptoTenSymbolObservationRuntimeConfig = TEN_SYMBOL_RUNTIME_CONFIG,
) -> dict[str, Any]:
    stored = store.append_event(
        _data_reject_event(
            manifest=manifest,
            window=window,
            reason_code=reason_code,
            config=config,
        )
    )
    return {
        "status": "data_reject",
        "reason_code": reason_code,
        "event_id": stored["event_id"],
        "event_checksum": stored["checksum"],
        "window_end": _iso_utc(window.window_end),
        "observation_cutoff": _iso_utc(window.observation_cutoff),
        **_non_authority_receipt_fields(),
    }


def _persisted_spread_block(
    *,
    store: CryptoTenSymbolObservationStore,
    manifest: CryptoTenSymbolObservationRuntimeManifest,
    window: CryptoObservationWindow,
    config: CryptoTenSymbolObservationRuntimeConfig = TEN_SYMBOL_RUNTIME_CONFIG,
) -> dict[str, Any]:
    """Rebuild one slot's spread block from its persisted spreads sidecar.

    The zero-network recovery path never re-samples book tickers: a slot
    whose bars sidecar survived a crash but whose spreads sidecar was never
    written records an honest leg-wide ``unavailable`` status instead of
    inventing evidence or requiring the network/token on recovery.  A
    corrupt or drifting sidecar is local evidence corruption and fails
    closed, exactly like the bars sidecar; it is never a data rejection.
    """

    sidecar = store.read_spreads_sidecar(_iso_utc(window.window_end))
    if sidecar is None:
        return unavailable_spread_event_block(
            "crypto_spread_sidecar_missing",
            spread_contract=config.spread_contract,
        )
    try:
        entries = validate_ten_symbol_spreads_sidecar(
            sidecar,
            symbols=config.symbols,
            spreads_sidecar_contract=config.spreads_sidecar_contract,
        )
    except CryptoMarketObservationError as exc:
        raise CryptoTenSymbolObservationRuntimeError(
            "runtime_spreads_sidecar_invalid"
        ) from exc
    if (
        sidecar.get("profile_sha256") != manifest.profile.profile_sha256
        or sidecar.get("window_end") != _iso_utc(window.window_end)
        or sidecar.get("observation_cutoff") != _iso_utc(window.observation_cutoff)
    ):
        raise CryptoTenSymbolObservationRuntimeError(
            "runtime_spreads_sidecar_slot_mismatch"
        )
    return build_spread_event_block(
        entries=entries,
        catalog_version=str(sidecar["catalog_version"]),
        spread_sha256=str(sidecar["spread_sha256"]),
        spread_contract=config.spread_contract,
    )


def _fresh_spread_block(
    *,
    store: CryptoTenSymbolObservationStore,
    spread: Mapping[str, Any],
    config: CryptoTenSymbolObservationRuntimeConfig = TEN_SYMBOL_RUNTIME_CONFIG,
) -> dict[str, Any]:
    """Persist the fresh spread leg's sidecar and derive its event block."""

    sidecar = spread.get("sidecar")
    if sidecar is None:
        return unavailable_spread_event_block(
            str(spread["unavailable_reason"]),
            spread_contract=config.spread_contract,
        )
    stored = store.write_spreads_sidecar(sidecar)
    return build_spread_event_block(
        entries=stored["entries"],
        catalog_version=str(stored["catalog_version"]),
        spread_sha256=str(stored["spread_sha256"]),
        spread_contract=config.spread_contract,
    )


def _observation_for_slot(
    *,
    store: CryptoTenSymbolObservationStore,
    lazy: _LazyObservationPort,
    manifest: CryptoTenSymbolObservationRuntimeManifest,
    window: CryptoObservationWindow,
    config: CryptoTenSymbolObservationRuntimeConfig = TEN_SYMBOL_RUNTIME_CONFIG,
) -> tuple[CryptoMarketObservation, dict[str, Any]]:
    """Return one slot's verified observation plus its spread status block.

    The bars sidecar is written before the store event, so a crash between
    the two leaves a complete, immutable orphan.  Reusing it makes the retry
    zero-network and keeps the slot pinned to the originally collected rows;
    a fresh collect happens only when no sidecar exists yet.  The spreads
    sidecar follows the same write-before-event ordering right after the
    bars sidecar; on the zero-network reuse path its block is rebuilt from
    the persisted payload, and a missing one degrades to a recorded
    ``crypto_spread_sidecar_missing`` status without touching the network.
    """

    sidecar = store.read_bars_sidecar(_iso_utc(window.window_end))
    if sidecar is not None:
        # A persisted sidecar is local immutable evidence: corruption or
        # drift fails closed loudly and is never misrecorded as an upstream
        # data rejection.
        try:
            observation, _ = observation_from_ten_symbol_bars_sidecar(
                sidecar,
                symbols=config.symbols,
                bars_sidecar_contract=config.bars_sidecar_contract,
            )
        except CryptoMarketObservationError as exc:
            raise CryptoTenSymbolObservationRuntimeError(
                "runtime_bars_sidecar_invalid"
            ) from exc
        if (
            sidecar.get("profile_sha256") != manifest.profile.profile_sha256
            or observation.window.window_end != window.window_end
            or observation.window.observation_cutoff != window.observation_cutoff
        ):
            raise CryptoTenSymbolObservationRuntimeError(
                "runtime_bars_sidecar_slot_mismatch"
            )
        return observation, _persisted_spread_block(
            store=store,
            manifest=manifest,
            window=window,
            config=config,
        )
    observation, rows_by_symbol, spread = lazy.collect(window)
    store.write_bars_sidecar(
        build_ten_symbol_bars_sidecar(
            window=window,
            profile_sha256=manifest.profile.profile_sha256,
            observation=observation,
            rows_by_symbol=rows_by_symbol,
            bars_sidecar_contract=config.bars_sidecar_contract,
        )
    )
    return observation, _fresh_spread_block(store=store, spread=spread, config=config)


def _fresh_cycle(
    *,
    store: CryptoTenSymbolObservationStore,
    lazy: _LazyObservationPort,
    manifest: CryptoTenSymbolObservationRuntimeManifest,
    target_window_end: datetime,
    config: CryptoTenSymbolObservationRuntimeConfig = TEN_SYMBOL_RUNTIME_CONFIG,
) -> dict[str, Any]:
    """Query one slot and record exactly one terminal or reject event."""

    window = _window_for_end(target_window_end, config=config)
    store.set_pending(
        {
            "window_end": _iso_utc(window.window_end),
            "observation_cutoff": _iso_utc(window.observation_cutoff),
            "profile_sha256": manifest.profile.profile_sha256,
            "catalog_version": manifest.catalog_version,
        }
    )
    try:
        observation, spread_block = _observation_for_slot(
            store=store,
            lazy=lazy,
            manifest=manifest,
            window=window,
            config=config,
        )
    except CryptoMarketObservationError as exc:
        result = _append_reject(
            store=store,
            manifest=manifest,
            window=window,
            reason_code=str(exc),
            config=config,
        )
        store.clear_pending(_iso_utc(window.window_end))
        return result
    stored = store.append_event(
        _observation_event(
            manifest=manifest,
            window=window,
            observation=observation,
            spread_block=spread_block,
            config=config,
        )
    )
    store.clear_pending(_iso_utc(window.window_end))
    return {
        "status": "completed",
        "reason_code": _family_reason(config, "observation_recorded"),
        "event_id": stored["event_id"],
        "event_checksum": stored["checksum"],
        "window_end": _iso_utc(window.window_end),
        "observation_cutoff": _iso_utc(window.observation_cutoff),
        "observation_sha256": observation.observation_sha256,
        "market_data_sha256": observation.market_data_sha256,
        "catalog_version": observation.catalog_version,
        "spread_status": spread_block["status"],
        **_non_authority_receipt_fields(),
    }


def _attempt_outage_gap_recovery(
    *,
    store: CryptoTenSymbolObservationStore,
    lazy: _LazyObservationPort,
    manifest: CryptoTenSymbolObservationRuntimeManifest,
    prior_market_slot: datetime,
    rejected_window: CryptoObservationWindow,
    current_window: CryptoObservationWindow,
    reason_code: str,
    config: CryptoTenSymbolObservationRuntimeConfig = TEN_SYMBOL_RUNTIME_CONFIG,
) -> dict[str, Any]:
    """Append one checksum-bound data_gap after the current window passes."""

    if store.pending_record() is not None:
        raise CryptoTenSymbolObservationRuntimeError(
            "runtime_outage_gap_pending_forbidden"
        )
    checkpoint = store.checkpoint()
    if checkpoint.get("latest_terminal_slot") != _iso_utc(prior_market_slot):
        raise CryptoTenSymbolObservationRuntimeError("runtime_outage_gap_state_changed")
    try:
        observation, spread_block = _observation_for_slot(
            store=store,
            lazy=lazy,
            manifest=manifest,
            window=current_window,
            config=config,
        )
    except CryptoMarketObservationError as exc:
        return _append_reject(
            store=store,
            manifest=manifest,
            window=current_window,
            reason_code=str(exc),
            config=config,
        )
    stored = store.append_event(
        _data_gap_event(
            manifest=manifest,
            prior_market_slot=prior_market_slot,
            rejected_window=rejected_window,
            current_window=current_window,
            reason_code=reason_code,
            observation=observation,
            spread_block=spread_block,
            config=config,
        )
    )
    return {
        "status": "completed",
        "reason_code": _family_reason(config, "outage_gap_recovered"),
        "event_id": stored["event_id"],
        "event_checksum": stored["checksum"],
        "skipped_from": stored["skipped_from"],
        "skipped_to": stored["skipped_to"],
        "recovery_market_slot": stored["recovery_market_slot"],
        "recovery_observation_sha256": observation.observation_sha256,
        "catalog_version": observation.catalog_version,
        "spread_status": spread_block["status"],
        **_non_authority_receipt_fields(),
    }


def run_crypto_ten_symbol_observation_once(
    *,
    runtime_manifest: Path | str,
    token_file: Path | str,
    output_root: Path | str,
    now: datetime,
    transport_factory: Callable[..., HTTPTransport] = (build_runtime_transport),
    invocation_budget_seconds: float | None = None,
    retry_sleep: Callable[[float], None] = time.sleep,
    config: CryptoTenSymbolObservationRuntimeConfig = TEN_SYMBOL_RUNTIME_CONFIG,
) -> dict[str, Any]:
    """Recover pending work, then process missing windows in slot order."""

    budget_seconds = (
        INVOCATION_BUDGET_SECONDS
        if invocation_budget_seconds is None
        else invocation_budget_seconds
    )
    if (
        isinstance(budget_seconds, bool)
        or not isinstance(budget_seconds, (int, float))
        or budget_seconds <= 0
    ):
        raise ValueError("ten_symbol_observation_invocation_budget_invalid")
    invocation_started_at = time.monotonic()
    bounded_transport_factory = _deadline_bound_transport_factory(
        transport_factory,
        started_at=invocation_started_at,
        budget_seconds=float(budget_seconds),
    )
    _assert_simulation_only()
    token, root = _require_exact_service_paths(
        token_file=token_file,
        output_root=output_root,
        config=config,
    )
    manifest = load_crypto_ten_symbol_observation_runtime_manifest(
        runtime_manifest,
        config=config,
    )
    if root != manifest.output_root:
        raise CryptoTenSymbolObservationRuntimeError(
            "runtime_output_root_invalid"
        )
    current_window = crypto_ten_symbol_observation_window(now, config=config)
    store = CryptoTenSymbolObservationStore(root, contracts=config.store_contracts)
    lazy = _LazyObservationPort(
        manifest=manifest,
        token_file=token,
        transport_factory=bounded_transport_factory,
        config=config,
        retry_sleep=retry_sleep,
        budget_check=lambda: _remaining_invocation_seconds(
            invocation_started_at, float(budget_seconds)
        ),
    )
    cycle_results: list[dict[str, Any]] = []
    recovered_observations: list[dict[str, Any]] = []
    budget_deferred = False
    try:
        with store.cycle():
            state_before = store.checkpoint()
            had_prior_evidence = bool(int(state_before["event_count"]) > 0)
            latest_terminal = (
                datetime.fromisoformat(
                    str(state_before["latest_terminal_slot"]).replace("Z", "+00:00")
                )
                if state_before["latest_terminal_slot"] is not None
                else None
            )
            pending = store.pending_record()

            if pending is not None:
                pending_slot = datetime.fromisoformat(
                    str(pending["window_end"]).replace("Z", "+00:00")
                )
                if pending_slot > current_window.window_end:
                    raise CryptoTenSymbolObservationRuntimeError(
                        "runtime_clock_before_pending_slot"
                    )
                existing = store.event_for_slot(
                    "observation", str(pending["window_end"])
                ) or store.event_for_slot("data_gap", str(pending["window_end"]))
                if existing is not None:
                    store.clear_pending(str(pending["window_end"]))
                    cycle_results.append(
                        {
                            "cycle_kind": "pending_recovery",
                            "target_window_end": str(pending["window_end"]),
                            "result": {
                                "status": "recovered_pending",
                                "reason_code": (
                                    _family_reason(
                                        config, "pending_already_recorded"
                                    )
                                ),
                                "event_id": existing["event_id"],
                                "event_checksum": existing["checksum"],
                                **_non_authority_receipt_fields(),
                            },
                        }
                    )
                    recovered_observations.append(
                        {
                            "window_end": str(pending["window_end"]),
                            "source_profile_sha256": pending["profile_sha256"],
                            "runtime_manifest_profile_used_for_recovery": False,
                            "network_used": False,
                        }
                    )
                elif pending_slot < current_window.window_end:
                    # A historical pending intent can never pass the current-read
                    # watermark gate; drop the marker and let the data_gap
                    # contract record the skipped range explicitly.
                    store.clear_pending(str(pending["window_end"]))
                    cycle_results.append(
                        {
                            "cycle_kind": "pending_recovery",
                            "target_window_end": str(pending["window_end"]),
                            "result": {
                                "status": "cleared_unrecoverable_pending",
                                "reason_code": (
                                    HISTORICAL_WINDOW_UNRECOVERABLE_REASON
                                ),
                                **_non_authority_receipt_fields(),
                            },
                        }
                    )
                else:
                    if pending["profile_sha256"] != manifest.profile.profile_sha256:
                        raise CryptoTenSymbolObservationRuntimeError(
                            "runtime_pending_profile_drift"
                        )
                    try:
                        result = _fresh_cycle(
                            store=store,
                            lazy=lazy,
                            manifest=manifest,
                            target_window_end=pending_slot,
                            config=config,
                        )
                    except _InvocationBudgetExhausted:
                        budget_deferred = True
                    else:
                        cycle_results.append(
                            {
                                "cycle_kind": "pending_recovery",
                                "target_window_end": _iso_utc(pending_slot),
                                "result": result,
                            }
                        )
                        if result["status"] == "completed":
                            latest_terminal = pending_slot
                            recovered_observations.append(
                                {
                                    "window_end": str(pending["window_end"]),
                                    "source_profile_sha256": pending[
                                        "profile_sha256"
                                    ],
                                    "runtime_manifest_profile_used_for_recovery": True,
                                    "network_used": True,
                                }
                            )

            while (
                not budget_deferred
                and len(cycle_results) < MAX_CYCLES_PER_INVOCATION
                and (latest_terminal is None or latest_terminal < current_window.window_end)
            ):
                target_window_end = (
                    current_window.window_end
                    if latest_terminal is None
                    else latest_terminal + timedelta(minutes=5)
                )
                target_window = _window_for_end(target_window_end, config=config)
                try:
                    result = _fresh_cycle(
                        store=store,
                        lazy=lazy,
                        manifest=manifest,
                        target_window_end=target_window_end,
                        config=config,
                    )
                except _InvocationBudgetExhausted:
                    budget_deferred = True
                    break
                cycle_results.append(
                    {
                        "cycle_kind": "fresh_query",
                        "target_window_end": _iso_utc(target_window_end),
                        "result": result,
                    }
                )
                if result["status"] == "completed":
                    latest_terminal = target_window_end
                    continue
                if (
                    result["status"] == "data_reject"
                    and result["reason_code"] in HISTORICAL_GAP_RECOVERY_REASONS
                    and latest_terminal is not None
                    and target_window_end < current_window.window_end
                    and len(cycle_results) < MAX_CYCLES_PER_INVOCATION
                ):
                    try:
                        gap_result = _attempt_outage_gap_recovery(
                            store=store,
                            lazy=lazy,
                            manifest=manifest,
                            prior_market_slot=latest_terminal,
                            rejected_window=target_window,
                            current_window=current_window,
                            reason_code=str(result["reason_code"]),
                            config=config,
                        )
                    except _InvocationBudgetExhausted:
                        budget_deferred = True
                        break
                    cycle_results.append(
                        {
                            "cycle_kind": "outage_gap_recovery",
                            "target_window_end": _iso_utc(current_window.window_end),
                            "result": gap_result,
                        }
                    )
                    if gap_result["status"] == "completed":
                        latest_terminal = current_window.window_end
                break
    except CryptoTenSymbolObservationRuntimeError:
        raise
    except (
        CryptoMarketObservationError,
        CryptoTenSymbolObservationStoreError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        raise CryptoTenSymbolObservationRuntimeError(
            "runtime_cycle_failed"
        ) from exc

    if latest_terminal is not None and latest_terminal > current_window.window_end:
        raise CryptoTenSymbolObservationRuntimeError(
            "runtime_clock_before_latest_observation"
        )
    outage_gap_attempt_failed = bool(
        cycle_results
        and cycle_results[-1]["cycle_kind"] == "outage_gap_recovery"
        and cycle_results[-1]["result"].get("status") != "completed"
    )
    backlog_remaining = bool(
        budget_deferred
        or (
            latest_terminal is not None
            and latest_terminal < current_window.window_end
            and len(cycle_results) >= MAX_CYCLES_PER_INVOCATION
            and not outage_gap_attempt_failed
        )
    )
    if not cycle_results:
        core_result: Mapping[str, Any] = (
            {
                "status": "budget_deferred",
                "reason_code": _family_reason(
                    config, "invocation_budget_exhausted"
                ),
                **_non_authority_receipt_fields(),
            }
            if budget_deferred
            else {
                "status": "noop",
                "reason_code": _family_reason(config, "slot_already_recorded"),
                **_non_authority_receipt_fields(),
            }
        )
    else:
        core_result = cycle_results[-1]["result"]
    last_status = str(core_result.get("status") or "failed_closed")
    data_incomplete = bool(
        last_status == "data_reject"
        and core_result.get("reason_code") in DATA_INCOMPLETE_REASONS
    )
    warmup_eligible = bool(
        not had_prior_evidence
        and len(cycle_results) == 1
        and last_status == "data_reject"
        and core_result.get("reason_code") == WARMUP_WINDOW_INCOMPLETE_REASON
    )
    if backlog_remaining:
        status = "backlog_pending"
    elif last_status in {"recovered_pending", "cleared_unrecoverable_pending"}:
        status = "completed"
    else:
        status = last_status
    return {
        "contract": config.runtime_contract,
        "status": status,
        "market": "crypto",
        "market_session": "24x7",
        "runtime_manifest_sha256": manifest.sha256,
        "fresh_query_catalog_version": (
            lazy.observed_catalog_version or manifest.catalog_version
        ),
        "fresh_query_profile_sha256": manifest.profile.profile_sha256,
        "requested_window_end": _iso_utc(current_window.window_end),
        "requested_observation_cutoff": _iso_utc(current_window.observation_cutoff),
        "requested_window_consumed": bool(
            latest_terminal is not None
            and latest_terminal >= current_window.window_end
        ),
        "processed_cycle_count": len(cycle_results),
        "recovered_cycle_count": sum(
            item["cycle_kind"] == "pending_recovery" for item in cycle_results
        ),
        "fresh_cycle_count": sum(
            item["cycle_kind"] == "fresh_query" for item in cycle_results
        ),
        "outage_gap_recovered": any(
            item["cycle_kind"] == "outage_gap_recovery"
            and item["result"].get("status") == "completed"
            for item in cycle_results
        ),
        "backlog_remaining": backlog_remaining,
        "budget_deferred": budget_deferred,
        "invocation_budget_seconds": float(budget_seconds),
        "warmup_eligible": warmup_eligible,
        "data_incomplete": data_incomplete,
        "data_incomplete_reason": (
            core_result.get("reason_code") if data_incomplete else None
        ),
        "market_data_transport": "loopback_tradingdatas_v1",
        "market_data_access_attempt_count": lazy.collect_calls,
        "collect_attempts": lazy.collect_attempts,
        "transport_factory_attempt_count": lazy.transport_factory_attempts,
        "market_data_network_used": lazy.transport_constructed_count > 0,
        "model_network_used": False,
        "core_result": core_result,
        "cycle_results": cycle_results,
        "recovered_observations": recovered_observations,
        **_non_authority_receipt_fields(),
    }


def crypto_ten_symbol_observation_exit_code(receipt: Mapping[str, Any]) -> int:
    """Map data-only incompleteness to an informational success exit.

    A ``backlog_pending`` receipt with ordered progress remains visible in JSON
    and the next timer invocation continues from the earliest missing slot. It
    is not a process failure: this accumulator has no capital or state-authority
    side effect, and returning 2 would turn an observable data lag into an
    unnecessary launch/runtime blocker. A backlog with zero processed cycles is
    different: it indicates the invocation made no progress (for example an
    exhausted wall-clock budget) and remains non-zero. Contract, credential,
    integrity, and unrecoverable data failures still return non-zero, and slots
    are never skipped.
    """

    if not isinstance(receipt, Mapping):
        return 2
    if receipt.get("backlog_remaining") is True:
        return (
            0
            if isinstance(receipt.get("processed_cycle_count"), int)
            and receipt["processed_cycle_count"] > 0
            else 2
        )
    if receipt.get("status") in {"completed", "noop"}:
        return 0
    if receipt.get("data_incomplete") is True:
        return 0
    if (
        receipt.get("status") == "data_reject"
        and receipt.get("warmup_eligible") is True
    ):
        return 0
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run one loopback-only Crypto ten-symbol observation accumulation"
        )
    )
    parser.add_argument(
        "--runtime-manifest",
        type=Path,
        required=True,
    )
    parser.add_argument("--token-file", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        receipt = run_crypto_ten_symbol_observation_once(
            runtime_manifest=args.runtime_manifest,
            token_file=args.token_file,
            output_root=RUNTIME_OUTPUT_ROOT,
            now=datetime.now(tz=timezone.utc),
        )
        exit_code = crypto_ten_symbol_observation_exit_code(receipt)
    except Exception:
        print(
            "crypto ten symbol observation runtime failed closed",
            file=sys.stderr,
        )
        return 2
    if exit_code != 0:
        print(
            "crypto ten symbol observation runtime failed closed",
            file=sys.stderr,
        )
        return exit_code
    print(
        json.dumps(
            receipt,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "COLLECT_RETRY_DELAY_SECONDS",
    "CRYPTO_TEN_SYMBOL_RUNTIME_CONTRACT",
    "CryptoTenSymbolObservationRuntimeConfig",
    "CryptoTenSymbolObservationRuntimeError",
    "CryptoTenSymbolObservationRuntimeManifest",
    "FORTY_SYMBOL_RUNTIME_CONFIG",
    "TEN_SYMBOL_RUNTIME_CONFIG",
    "HISTORICAL_GAP_RECOVERY_REASONS",
    "HISTORICAL_WINDOW_UNRECOVERABLE_REASON",
    "INVOCATION_BUDGET_SECONDS",
    "MAX_COLLECT_ATTEMPTS",
    "MAX_CYCLES_PER_INVOCATION",
    "OUTAGE_GAP_CONTRACT",
    "REQUESTS_PER_CYCLE",
    "RUNTIME_MANIFEST_CONTRACT",
    "RUNTIME_OUTPUT_ROOT",
    "RUNTIME_TIMEOUT_SECONDS",
    "RUNTIME_TOKEN_FILE",
    "SLOT_CUTOFF_DELAY_SECONDS",
    "crypto_ten_symbol_observation_exit_code",
    "crypto_ten_symbol_observation_window",
    "load_crypto_ten_symbol_observation_runtime_manifest",
    "main",
    "run_crypto_ten_symbol_observation_once",
]
