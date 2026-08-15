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
import ipaddress
import json
import os
from pathlib import Path
import stat
import sys
from typing import Any, Callable, Mapping
import urllib.parse

from Crypto.market_observation import (
    CryptoMarketObservation,
    CryptoMarketObservationError,
    CryptoObservationWindow,
    _collect_market_observation_with_catalog,
)
from Crypto.ten_symbol_observation_profile import (
    CryptoTenSymbolObservationProfile,
    CryptoTenSymbolProfileError,
    load_ten_symbol_observation_profile_payload,
)
from Crypto.ten_symbol_observation_store import (
    TEN_SYMBOL_DATA_GAP_CONTRACT,
    TEN_SYMBOL_EVENT_CONTRACT,
    CryptoTenSymbolObservationStore,
    CryptoTenSymbolObservationStoreError,
)
from shared.data.sharedsignals_v1 import (
    HTTPTransport,
    SharedSignalsV1Client,
    SharedSignalsV1Config,
    SharedSignalsV1Error,
)
from shared.data.tradingdatas_transport import (
    RuntimeGateConfigurationError,
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
MAX_CYCLES_PER_INVOCATION = 2
# One catalog read plus ten bounded single-page queries per cycle.
REQUESTS_PER_CYCLE = 11
OUTAGE_GAP_CONTRACT = TEN_SYMBOL_DATA_GAP_CONTRACT
HISTORICAL_WINDOW_UNRECOVERABLE_REASON = "crypto_observation_watermark_invalid"
HISTORICAL_GAP_RECOVERY_REASONS = frozenset(
    {HISTORICAL_WINDOW_UNRECOVERABLE_REASON}
)
WARMUP_WINDOW_INCOMPLETE_REASON = "crypto_observation_query_shape_invalid"
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


class CryptoTenSymbolObservationRuntimeError(RuntimeError):
    """Stable, redacted fail-closed runtime error."""


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
) -> CryptoTenSymbolObservationRuntimeManifest:
    raw = _read_external_manifest(path)
    _exact_keys(raw, _MANIFEST_KEYS, "runtime_manifest_keys_invalid")
    if raw.get("schema") != RUNTIME_MANIFEST_CONTRACT:
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
            _mapping(raw.get("profile"), "runtime_profile_invalid")
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
    if not output_root.is_absolute() or output_root != RUNTIME_OUTPUT_ROOT:
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


def crypto_ten_symbol_observation_window(now: datetime) -> CryptoObservationWindow:
    """Derive the fixed slot: bar close floored to 5m plus a 55s cutoff."""

    observed = _utc_now(now)
    eligible = observed - timedelta(seconds=SLOT_CUTOFF_DELAY_SECONDS)
    window_end = eligible.replace(
        minute=eligible.minute - eligible.minute % 5,
        second=0,
        microsecond=0,
    )
    return CryptoObservationWindow(
        window_end=window_end,
        observation_cutoff=window_end
        + timedelta(seconds=SLOT_CUTOFF_DELAY_SECONDS),
    )


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _window_for_end(window_end: datetime) -> CryptoObservationWindow:
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
        + timedelta(seconds=SLOT_CUTOFF_DELAY_SECONDS),
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


class _LazyObservationPort:
    """Construct the authenticated transport only if fresh data is needed."""

    def __init__(
        self,
        *,
        manifest: CryptoTenSymbolObservationRuntimeManifest,
        token_file: Path,
        transport_factory: Callable[..., HTTPTransport],
        timeout_seconds: float = RUNTIME_TIMEOUT_SECONDS,
    ) -> None:
        self._manifest = manifest
        self._token_file = token_file
        self._transport_factory = transport_factory
        self._timeout_seconds = timeout_seconds
        self.collect_calls = 0
        self.transport_factory_attempts = 0
        self.transport_constructed_count = 0

    def collect(self, window: CryptoObservationWindow) -> CryptoMarketObservation:
        self.collect_calls += 1
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
                    catalog_version_policy="strict",
                    timeout_seconds=self._timeout_seconds,
                    max_limit=500,
                    cache_ttl_seconds=0,
                ),
                transport=transport,
            )
            catalog = client.get_catalog()
            self._manifest.profile.verify_catalog(catalog)
            return _collect_market_observation_with_catalog(
                client,
                catalog=catalog,
                expected_catalog_version=self._manifest.catalog_version,
                window=window,
            )
        except CryptoMarketObservationError:
            raise
        except CryptoTenSymbolProfileError as exc:
            raise CryptoMarketObservationError(str(exc)) from exc
        except RuntimeGateConfigurationError as exc:
            raise CryptoMarketObservationError(str(exc)) from exc
        except SharedSignalsV1Error as exc:
            raise CryptoMarketObservationError(str(exc)) from exc
        except (TypeError, ValueError) as exc:
            raise CryptoMarketObservationError(
                "crypto_ten_symbol_transport_configuration_invalid"
            ) from exc


def _require_exact_service_paths(
    *,
    token_file: Path | str,
    output_root: Path | str,
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
    if root != RUNTIME_OUTPUT_ROOT:
        raise CryptoTenSymbolObservationRuntimeError(
            "runtime_output_root_invalid"
        )
    return token, root


def _observation_event(
    *,
    manifest: CryptoTenSymbolObservationRuntimeManifest,
    window: CryptoObservationWindow,
    observation: CryptoMarketObservation,
) -> dict[str, Any]:
    id_material = {
        "event_type": "observation",
        "window_end": _iso_utc(window.window_end),
        "observation_sha256": observation.observation_sha256,
        "profile_sha256": manifest.profile.profile_sha256,
    }
    return {
        "contract": TEN_SYMBOL_EVENT_CONTRACT,
        "event_id": f"crypto-ten-observation-{_sha256(id_material)[:24]}",
        "event_type": "observation",
        "market": "crypto",
        "market_session": "24x7",
        "window_end": _iso_utc(window.window_end),
        "observation_cutoff": _iso_utc(window.observation_cutoff),
        "catalog_version": manifest.catalog_version,
        "profile_sha256": manifest.profile.profile_sha256,
        "observation": observation.to_payload(),
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
        "contract": TEN_SYMBOL_EVENT_CONTRACT,
        "event_id": f"crypto-ten-data-reject-{_sha256(id_material)[:24]}",
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
        "gap_contract": OUTAGE_GAP_CONTRACT,
        "prior_market_slot": _iso_utc(prior_market_slot),
        "skipped_from": _iso_utc(skipped_from),
        "skipped_to": _iso_utc(skipped_to),
        "recovery_market_slot": _iso_utc(recovery),
        "reason_code": reason_code,
        "recovery_observation_sha256": observation_sha,
    }
    return {
        "contract": TEN_SYMBOL_EVENT_CONTRACT,
        "gap_contract": OUTAGE_GAP_CONTRACT,
        "event_id": f"crypto-ten-data-gap-{_sha256(id_material)[:24]}",
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
        "catalog_version": manifest.catalog_version,
        "profile_sha256": manifest.profile.profile_sha256,
        "recovery_observation": observation.to_payload(),
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
) -> dict[str, Any]:
    stored = store.append_event(
        _data_reject_event(
            manifest=manifest,
            window=window,
            reason_code=reason_code,
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


def _fresh_cycle(
    *,
    store: CryptoTenSymbolObservationStore,
    lazy: _LazyObservationPort,
    manifest: CryptoTenSymbolObservationRuntimeManifest,
    target_window_end: datetime,
) -> dict[str, Any]:
    """Query one slot and record exactly one terminal or reject event."""

    window = _window_for_end(target_window_end)
    store.set_pending(
        {
            "window_end": _iso_utc(window.window_end),
            "observation_cutoff": _iso_utc(window.observation_cutoff),
            "profile_sha256": manifest.profile.profile_sha256,
            "catalog_version": manifest.catalog_version,
        }
    )
    try:
        observation = lazy.collect(window)
    except CryptoMarketObservationError as exc:
        result = _append_reject(
            store=store,
            manifest=manifest,
            window=window,
            reason_code=str(exc),
        )
        store.clear_pending(_iso_utc(window.window_end))
        return result
    stored = store.append_event(
        _observation_event(
            manifest=manifest,
            window=window,
            observation=observation,
        )
    )
    store.clear_pending(_iso_utc(window.window_end))
    return {
        "status": "completed",
        "reason_code": "crypto_ten_symbol_observation_recorded",
        "event_id": stored["event_id"],
        "event_checksum": stored["checksum"],
        "window_end": _iso_utc(window.window_end),
        "observation_cutoff": _iso_utc(window.observation_cutoff),
        "observation_sha256": observation.observation_sha256,
        "market_data_sha256": observation.market_data_sha256,
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
        observation = lazy.collect(current_window)
    except CryptoMarketObservationError as exc:
        return _append_reject(
            store=store,
            manifest=manifest,
            window=current_window,
            reason_code=str(exc),
        )
    stored = store.append_event(
        _data_gap_event(
            manifest=manifest,
            prior_market_slot=prior_market_slot,
            rejected_window=rejected_window,
            current_window=current_window,
            reason_code=reason_code,
            observation=observation,
        )
    )
    return {
        "status": "completed",
        "reason_code": "crypto_ten_symbol_outage_gap_recovered",
        "event_id": stored["event_id"],
        "event_checksum": stored["checksum"],
        "skipped_from": stored["skipped_from"],
        "skipped_to": stored["skipped_to"],
        "recovery_market_slot": stored["recovery_market_slot"],
        "recovery_observation_sha256": observation.observation_sha256,
        **_non_authority_receipt_fields(),
    }


def run_crypto_ten_symbol_observation_once(
    *,
    runtime_manifest: Path | str,
    token_file: Path | str,
    output_root: Path | str,
    now: datetime,
    transport_factory: Callable[..., HTTPTransport] = (build_runtime_transport),
) -> dict[str, Any]:
    """Recover pending work, then process missing windows in slot order."""

    _assert_simulation_only()
    token, root = _require_exact_service_paths(
        token_file=token_file,
        output_root=output_root,
    )
    manifest = load_crypto_ten_symbol_observation_runtime_manifest(runtime_manifest)
    if root != manifest.output_root:
        raise CryptoTenSymbolObservationRuntimeError(
            "runtime_output_root_invalid"
        )
    current_window = crypto_ten_symbol_observation_window(now)
    store = CryptoTenSymbolObservationStore(root)
    lazy = _LazyObservationPort(
        manifest=manifest,
        token_file=token,
        transport_factory=transport_factory,
    )
    cycle_results: list[dict[str, Any]] = []
    recovered_observations: list[dict[str, Any]] = []
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
                                    "crypto_ten_symbol_pending_already_recorded"
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
                    result = _fresh_cycle(
                        store=store,
                        lazy=lazy,
                        manifest=manifest,
                        target_window_end=pending_slot,
                    )
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
                                "source_profile_sha256": pending["profile_sha256"],
                                "runtime_manifest_profile_used_for_recovery": True,
                                "network_used": True,
                            }
                        )

            while (
                len(cycle_results) < MAX_CYCLES_PER_INVOCATION
                and (latest_terminal is None or latest_terminal < current_window.window_end)
            ):
                target_window_end = (
                    current_window.window_end
                    if latest_terminal is None
                    else latest_terminal + timedelta(minutes=5)
                )
                target_window = _window_for_end(target_window_end)
                result = _fresh_cycle(
                    store=store,
                    lazy=lazy,
                    manifest=manifest,
                    target_window_end=target_window_end,
                )
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
                    gap_result = _attempt_outage_gap_recovery(
                        store=store,
                        lazy=lazy,
                        manifest=manifest,
                        prior_market_slot=latest_terminal,
                        rejected_window=target_window,
                        current_window=current_window,
                        reason_code=str(result["reason_code"]),
                    )
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
        latest_terminal is not None
        and latest_terminal < current_window.window_end
        and len(cycle_results) >= MAX_CYCLES_PER_INVOCATION
        and not outage_gap_attempt_failed
    )
    if not cycle_results:
        core_result: Mapping[str, Any] = {
            "status": "noop",
            "reason_code": "crypto_ten_symbol_slot_already_recorded",
            **_non_authority_receipt_fields(),
        }
    else:
        core_result = cycle_results[-1]["result"]
    last_status = str(core_result.get("status") or "failed_closed")
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
        "contract": CRYPTO_TEN_SYMBOL_RUNTIME_CONTRACT,
        "status": status,
        "market": "crypto",
        "market_session": "24x7",
        "runtime_manifest_sha256": manifest.sha256,
        "fresh_query_catalog_version": manifest.catalog_version,
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
        "warmup_eligible": warmup_eligible,
        "market_data_transport": "loopback_tradingdatas_v1",
        "market_data_access_attempt_count": lazy.collect_calls,
        "transport_factory_attempt_count": lazy.transport_factory_attempts,
        "market_data_network_used": lazy.transport_constructed_count > 0,
        "model_network_used": False,
        "core_result": core_result,
        "cycle_results": cycle_results,
        "recovered_observations": recovered_observations,
        **_non_authority_receipt_fields(),
    }


def crypto_ten_symbol_observation_exit_code(receipt: Mapping[str, Any]) -> int:
    """Map expected warm-up to success; backlog and rejects stay non-zero.

    Unlike the delayed-paper core, a ``backlog_pending`` round is always a
    non-zero exit even when it made ordered progress: this accumulator has no
    capital at risk, so the timer should surface the lag until the chain is
    caught up.  Slots are still never skipped.
    """

    if not isinstance(receipt, Mapping):
        return 2
    if receipt.get("backlog_remaining") is True:
        return 2
    if receipt.get("status") in {"completed", "noop"}:
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
    "CRYPTO_TEN_SYMBOL_RUNTIME_CONTRACT",
    "CryptoTenSymbolObservationRuntimeError",
    "CryptoTenSymbolObservationRuntimeManifest",
    "HISTORICAL_GAP_RECOVERY_REASONS",
    "HISTORICAL_WINDOW_UNRECOVERABLE_REASON",
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
