"""Minimal server CLI for the Crypto closed-5m delayed-paper candidate.

The runtime manifest is secret-free and repository-external.  It freezes the
exact catalog/profile contract, while the only credential path is the dedicated
TradingDatas Crypto read-token leaf.  Network construction is lazy so recovery
of an already-persisted pending observation does not depend on TradingDatas.
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

from Crypto.delayed_paper_ledger import (
    DECISION_LEDGER_CONTRACT,
    CryptoDelayedPaperLedgerError,
    CryptoDelayedPaperObservationStore,
    _non_authority_fields,
)
from Crypto.delayed_paper_runner import (
    FROZEN_SYMBOLS,
    _data_reject,
    _prepare_observation,
    _snapshot_to_observation,
    run_crypto_delayed_paper_once,
)
from Crypto.fixture_sim.contracts import (
    CryptoEvidenceError,
    CryptoFixtureAutoSimError,
    CryptoLedgerError,
    CryptoSafetyError,
    _assert_simulation_only,
    _validate_json_tree,
)
from Crypto.fixture_sim.ledger import CryptoCapitalLedger
from Crypto.five_minute_data import (
    CryptoBarFieldMap,
    CryptoDatasetQueryProfile,
    CryptoFiveMinuteDataError,
    CryptoFiveMinuteDataProfile,
    CryptoFiveMinuteSnapshot,
    CryptoFiveMinuteWindowRequest,
    CryptoInstrumentRuleFieldMap,
    CryptoQueryFilterBinding,
    CryptoSymbolDatasetBinding,
    TradingDatasCryptoFiveMinuteDataPort,
)
from shared.data.sharedsignals_v1 import (
    HTTPTransport,
    SharedSignalsV1Client,
    SharedSignalsV1Config,
)
from shared.data.tradingdatas_transport import (
    RuntimeGateConfigurationError,
    build_runtime_transport,
)


CRYPTO_RUNTIME_CONTRACT = "tradingagent.crypto.delayed_paper_server_runtime.v1"
RUNTIME_MANIFEST_CONTRACT = "tradingagent.crypto.delayed_paper_runtime_manifest.v1"
RUNTIME_TOKEN_FILE = Path("/run/secrets/tradingagent/tradingdatas-crypto-read.token")
RUNTIME_OUTPUT_ROOT = Path("/var/lib/tradingagent/crypto-delayed-paper")
RUNTIME_MANIFEST_MAX_BYTES = 512 * 1024
SLOT_CUTOFF_DELAY_SECONDS = 55
RUNTIME_ACCESS_POLICY_MAX_CHARS = 128
# Each snapshot may consume one catalog request plus the full ten-page query
# budget. Two bounded cycles at 6s per request need at most 132s, below the
# systemd 180s stop line without relying on the normal one-page response.
RUNTIME_TIMEOUT_SECONDS = 6.0
MAX_CYCLES_PER_INVOCATION = 2
MAX_PROFILE_PAGE_BUDGET = 10
OUTAGE_GAP_CONTRACT = "tradingagent.crypto.delayed_paper_data_gap.v1"
HISTORICAL_EXACT_AS_OF_UNRECOVERABLE_REASON = (
    "metadata.data_through must not be after the requested as_of"
)
HISTORICAL_WINDOW_INCOMPLETE_UNRECOVERABLE_REASON = "crypto_5m_window_incomplete"
HISTORICAL_GAP_RECOVERY_REASONS = frozenset(
    {
        HISTORICAL_EXACT_AS_OF_UNRECOVERABLE_REASON,
        HISTORICAL_WINDOW_INCOMPLETE_UNRECOVERABLE_REASON,
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
        "profile_sha256",
        "profile",
        "safety",
    }
)
_PROFILE_KEYS = frozenset(
    {
        "mode",
        "catalog_version",
        "symbols",
        "bar_fields",
        "rule_fields",
        "bar_close_time_semantics",
        "bar_closed_semantics",
        "active_rule_status",
        "max_bar_observation_lag_seconds",
        "max_rule_observation_lag_seconds",
    }
)
_DATASET_PROFILE_KEYS = frozenset(
    {
        "catalog_version",
        "dataset_id",
        "schema_major",
        "selected_fields",
        "query_order",
        "identity_fields",
        "filter_bindings",
        "catalog_contract_sha256",
        "page_limit",
        "max_pages",
        "max_rows",
    }
)
_FILTER_BINDING_KEYS = frozenset({"role", "field", "operator"})
_SYMBOL_BINDING_KEYS = frozenset({"symbol", "bars", "instrument_rules"})
_BAR_FIELD_KEYS = frozenset(
    {
        "symbol",
        "open_time",
        "close_time",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "quote_volume",
        "trade_count",
    }
)
_RULE_FIELD_KEYS = frozenset(
    {
        "symbol",
        "status",
        "base_asset",
        "quote_asset",
        "price_tick",
        "quantity_step",
        "min_quantity",
        "min_notional",
    }
)


class CryptoDelayedPaperRuntimeError(RuntimeError):
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
        raise CryptoDelayedPaperRuntimeError("runtime_manifest_not_canonical") from exc


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _mapping(value: Any, reason: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CryptoDelayedPaperRuntimeError(reason)
    return value


def _exact_keys(
    value: Mapping[str, Any],
    expected: frozenset[str],
    reason: str,
) -> None:
    if set(value) != set(expected):
        raise CryptoDelayedPaperRuntimeError(reason)


def _native_text(value: Any, reason: str, *, max_chars: int = 512) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > max_chars
        or any(ord(character) < 0x20 for character in value)
    ):
        raise CryptoDelayedPaperRuntimeError(reason)
    return value


def _string_tuple(value: Any, reason: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise CryptoDelayedPaperRuntimeError(reason)
    result = tuple(_native_text(item, reason) for item in value)
    if len(result) != len(set(result)):
        raise CryptoDelayedPaperRuntimeError(reason)
    return result


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if type(key) is not str or key in result:
            raise CryptoDelayedPaperRuntimeError("runtime_manifest_duplicate_key")
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
        raise CryptoDelayedPaperRuntimeError("runtime_manifest_path_must_be_absolute")
    try:
        resolved = manifest_path.resolve(strict=True)
    except OSError as exc:
        raise CryptoDelayedPaperRuntimeError("runtime_manifest_missing") from exc
    if resolved != manifest_path or _is_within(resolved, _REPO_ROOT):
        raise CryptoDelayedPaperRuntimeError("runtime_manifest_file_untrusted")
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
            raise CryptoDelayedPaperRuntimeError("runtime_manifest_file_untrusted")
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
            raise CryptoDelayedPaperRuntimeError("runtime_manifest_changed_during_read")
        if not raw_bytes.endswith(b"\n") or b"\x00" in raw_bytes:
            raise CryptoDelayedPaperRuntimeError("runtime_manifest_encoding_invalid")
        decoded = json.loads(
            raw_bytes.decode("utf-8"),
            object_pairs_hook=_strict_object,
        )
    except CryptoDelayedPaperRuntimeError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CryptoDelayedPaperRuntimeError("runtime_manifest_json_invalid") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if not isinstance(decoded, dict):
        raise CryptoDelayedPaperRuntimeError("runtime_manifest_object_required")
    try:
        _validate_json_tree(
            decoded,
            path="crypto_runtime_manifest",
            max_values=2048,
            external=True,
        )
    except (CryptoEvidenceError, CryptoSafetyError) as exc:
        raise CryptoDelayedPaperRuntimeError("runtime_manifest_tree_invalid") from exc
    return decoded


def _loopback_base_url(value: Any) -> str:
    base_url = _native_text(value, "runtime_base_url_invalid")
    try:
        parsed = urllib.parse.urlsplit(base_url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise CryptoDelayedPaperRuntimeError("runtime_base_url_invalid") from exc
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
        raise CryptoDelayedPaperRuntimeError("runtime_base_url_invalid")
    try:
        host = ipaddress.ip_address(hostname)
    except ValueError as exc:
        raise CryptoDelayedPaperRuntimeError(
            "runtime_base_url_must_be_loopback"
        ) from exc
    if not host.is_loopback:
        raise CryptoDelayedPaperRuntimeError("runtime_base_url_must_be_loopback")
    canonical = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    if canonical != base_url:
        raise CryptoDelayedPaperRuntimeError("runtime_base_url_invalid")
    return base_url


def _filter_binding(value: Any) -> CryptoQueryFilterBinding:
    raw = _mapping(value, "runtime_filter_binding_invalid")
    _exact_keys(
        raw,
        _FILTER_BINDING_KEYS,
        "runtime_filter_binding_invalid",
    )
    try:
        return CryptoQueryFilterBinding(
            role=_native_text(
                raw["role"],
                "runtime_filter_binding_invalid",
            ),
            field=_native_text(
                raw["field"],
                "runtime_filter_binding_invalid",
            ),
            operator=_native_text(
                raw["operator"],
                "runtime_filter_binding_invalid",
            ),
        )
    except (TypeError, ValueError, CryptoFiveMinuteDataError) as exc:
        raise CryptoDelayedPaperRuntimeError("runtime_filter_binding_invalid") from exc


def _dataset_profile(value: Any) -> CryptoDatasetQueryProfile:
    raw = _mapping(value, "runtime_dataset_profile_invalid")
    _exact_keys(
        raw,
        _DATASET_PROFILE_KEYS,
        "runtime_dataset_profile_invalid",
    )
    bindings = raw.get("filter_bindings")
    if not isinstance(bindings, list) or not bindings:
        raise CryptoDelayedPaperRuntimeError("runtime_dataset_profile_invalid")
    try:
        return CryptoDatasetQueryProfile(
            catalog_version=_native_text(
                raw["catalog_version"],
                "runtime_dataset_profile_invalid",
            ),
            dataset_id=_native_text(
                raw["dataset_id"],
                "runtime_dataset_profile_invalid",
            ),
            schema_major=raw["schema_major"],
            selected_fields=_string_tuple(
                raw["selected_fields"],
                "runtime_dataset_profile_invalid",
            ),
            query_order=_string_tuple(
                raw["query_order"],
                "runtime_dataset_profile_invalid",
            ),
            identity_fields=_string_tuple(
                raw["identity_fields"],
                "runtime_dataset_profile_invalid",
            ),
            filter_bindings=tuple(_filter_binding(item) for item in bindings),
            catalog_contract_sha256=_native_text(
                raw["catalog_contract_sha256"],
                "runtime_dataset_profile_invalid",
            ),
            page_limit=raw["page_limit"],
            max_pages=raw["max_pages"],
            max_rows=raw["max_rows"],
        )
    except (TypeError, ValueError, CryptoFiveMinuteDataError) as exc:
        raise CryptoDelayedPaperRuntimeError("runtime_dataset_profile_invalid") from exc


def _symbol_binding(value: Any) -> CryptoSymbolDatasetBinding:
    raw = _mapping(value, "runtime_symbol_binding_invalid")
    _exact_keys(
        raw,
        _SYMBOL_BINDING_KEYS,
        "runtime_symbol_binding_invalid",
    )
    try:
        return CryptoSymbolDatasetBinding(
            symbol=_native_text(
                raw["symbol"],
                "runtime_symbol_binding_invalid",
            ),
            bars=_dataset_profile(raw["bars"]),
            instrument_rules=_dataset_profile(raw["instrument_rules"]),
        )
    except (TypeError, ValueError, CryptoFiveMinuteDataError) as exc:
        raise CryptoDelayedPaperRuntimeError("runtime_symbol_binding_invalid") from exc


def _data_profile(value: Any) -> CryptoFiveMinuteDataProfile:
    raw = _mapping(value, "runtime_profile_invalid")
    _exact_keys(raw, _PROFILE_KEYS, "runtime_profile_keys_invalid")
    if raw.get("mode") != "tradingdatas_handoff":
        raise CryptoDelayedPaperRuntimeError("runtime_profile_mode_invalid")
    raw_symbols = raw.get("symbols")
    if not isinstance(raw_symbols, list) or len(raw_symbols) != 2:
        raise CryptoDelayedPaperRuntimeError("runtime_profile_symbols_invalid")
    bar_fields = _mapping(
        raw.get("bar_fields"),
        "runtime_bar_fields_invalid",
    )
    rule_fields = _mapping(
        raw.get("rule_fields"),
        "runtime_rule_fields_invalid",
    )
    _exact_keys(
        bar_fields,
        _BAR_FIELD_KEYS,
        "runtime_bar_fields_invalid",
    )
    _exact_keys(
        rule_fields,
        _RULE_FIELD_KEYS,
        "runtime_rule_fields_invalid",
    )
    try:
        return CryptoFiveMinuteDataProfile(
            mode="tradingdatas_handoff",
            catalog_version=_native_text(
                raw["catalog_version"],
                "runtime_profile_invalid",
            ),
            symbols=tuple(_symbol_binding(item) for item in raw_symbols),
            bar_fields=CryptoBarFieldMap(
                **{
                    key: _native_text(
                        bar_fields[key],
                        "runtime_bar_fields_invalid",
                    )
                    for key in _BAR_FIELD_KEYS
                }
            ),
            rule_fields=CryptoInstrumentRuleFieldMap(
                **{
                    key: _native_text(
                        rule_fields[key],
                        "runtime_rule_fields_invalid",
                    )
                    for key in _RULE_FIELD_KEYS
                }
            ),
            bar_close_time_semantics=_native_text(
                raw["bar_close_time_semantics"],
                "runtime_profile_invalid",
            ),
            bar_closed_semantics=_native_text(
                raw["bar_closed_semantics"],
                "runtime_profile_invalid",
            ),
            active_rule_status=_native_text(
                raw["active_rule_status"],
                "runtime_profile_invalid",
            ),
            max_bar_observation_lag_seconds=raw["max_bar_observation_lag_seconds"],
            max_rule_observation_lag_seconds=raw["max_rule_observation_lag_seconds"],
        )
    except (TypeError, ValueError, CryptoFiveMinuteDataError) as exc:
        raise CryptoDelayedPaperRuntimeError("runtime_profile_invalid") from exc


@dataclass(frozen=True)
class CryptoDelayedPaperRuntimeManifest:
    base_url: str
    catalog_version: str
    access_policy_id: str
    profile: CryptoFiveMinuteDataProfile
    profile_sha256: str
    sha256: str

    @property
    def dataset_ids(self) -> frozenset[str]:
        return frozenset(
            dataset_id
            for binding in self.profile.symbols
            for dataset_id in (
                binding.bars.dataset_id,
                binding.instrument_rules.dataset_id,
            )
        )


def load_crypto_delayed_paper_runtime_manifest(
    path: Path | str,
) -> CryptoDelayedPaperRuntimeManifest:
    raw = _read_external_manifest(path)
    _exact_keys(raw, _MANIFEST_KEYS, "runtime_manifest_keys_invalid")
    if raw.get("schema") != RUNTIME_MANIFEST_CONTRACT:
        raise CryptoDelayedPaperRuntimeError("runtime_manifest_schema_invalid")
    safety = _mapping(
        raw.get("safety"),
        "runtime_safety_contract_invalid",
    )
    if dict(safety) != _EXPECTED_SAFETY:
        raise CryptoDelayedPaperRuntimeError("runtime_safety_contract_invalid")
    profile = _data_profile(raw.get("profile"))
    profile_sha256 = _native_text(
        raw.get("profile_sha256"),
        "runtime_profile_sha256_invalid",
    )
    if (
        len(profile_sha256) != 64
        or any(character not in "0123456789abcdef" for character in profile_sha256)
        or profile.sha256 != profile_sha256
    ):
        raise CryptoDelayedPaperRuntimeError("runtime_profile_sha256_mismatch")
    catalog_version = _native_text(
        raw.get("catalog_version"),
        "runtime_catalog_version_invalid",
    )
    if profile.catalog_version != catalog_version:
        raise CryptoDelayedPaperRuntimeError("runtime_catalog_profile_mismatch")
    access_policy_id = _native_text(
        raw.get("access_policy_id"),
        "runtime_access_policy_invalid",
        max_chars=RUNTIME_ACCESS_POLICY_MAX_CHARS,
    )
    if any(
        token in access_policy_id.lower()
        for token in ("token", "secret", "authorization", "bearer")
    ):
        raise CryptoDelayedPaperRuntimeError("runtime_access_policy_invalid")
    manifest = CryptoDelayedPaperRuntimeManifest(
        base_url=_loopback_base_url(raw.get("base_url")),
        catalog_version=catalog_version,
        access_policy_id=access_policy_id,
        profile=profile,
        profile_sha256=profile_sha256,
        sha256=_sha256(raw),
    )
    if len(manifest.dataset_ids) != 4:
        raise CryptoDelayedPaperRuntimeError("runtime_dataset_binding_invalid")
    if (
        sum(
            dataset.max_pages
            for binding in manifest.profile.symbols
            for dataset in (binding.bars, binding.instrument_rules)
        )
        > MAX_PROFILE_PAGE_BUDGET
    ):
        raise CryptoDelayedPaperRuntimeError("runtime_profile_page_budget_exceeded")
    return manifest


def _utc_now(value: datetime) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
        or value.utcoffset() != timedelta(0)
    ):
        raise CryptoDelayedPaperRuntimeError("runtime_now_must_be_utc")
    return value.astimezone(timezone.utc)


def crypto_runtime_window_request(
    now: datetime,
) -> CryptoFiveMinuteWindowRequest:
    observed = _utc_now(now)
    eligible = observed - timedelta(seconds=SLOT_CUTOFF_DELAY_SECONDS)
    window_end = eligible.replace(
        minute=eligible.minute - eligible.minute % 5,
        second=0,
        microsecond=0,
    )
    observation_cutoff = window_end + timedelta(seconds=SLOT_CUTOFF_DELAY_SECONDS)
    return CryptoFiveMinuteWindowRequest(
        window_end=window_end,
        observation_cutoff=observation_cutoff,
    )


class _LazyCryptoFiveMinutePort:
    """Construct the authenticated transport only if fresh data is needed."""

    def __init__(
        self,
        *,
        manifest: CryptoDelayedPaperRuntimeManifest,
        token_file: Path,
        transport_factory: Callable[..., HTTPTransport],
    ) -> None:
        self._manifest = manifest
        self._token_file = token_file
        self._transport_factory = transport_factory
        self.load_snapshot_calls = 0
        self.transport_factory_attempts = 0
        self.transport_constructed_count = 0

    def load_snapshot(
        self,
        *,
        profile: CryptoFiveMinuteDataProfile,
        request: CryptoFiveMinuteWindowRequest,
    ) -> Any:
        if profile != self._manifest.profile:
            raise CryptoFiveMinuteDataError("crypto_runtime_profile_binding_invalid")
        self.load_snapshot_calls += 1
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
                    timeout_seconds=RUNTIME_TIMEOUT_SECONDS,
                    max_limit=max(
                        dataset.page_limit
                        for binding in profile.symbols
                        for dataset in (
                            binding.bars,
                            binding.instrument_rules,
                        )
                    ),
                    cache_ttl_seconds=0,
                ),
                transport=transport,
            )
            return TradingDatasCryptoFiveMinuteDataPort(client).load_snapshot(
                profile=profile, request=request
            )
        except CryptoFiveMinuteDataError:
            raise
        except RuntimeGateConfigurationError as exc:
            raise CryptoFiveMinuteDataError(str(exc)) from exc
        except (TypeError, ValueError) as exc:
            raise CryptoFiveMinuteDataError(
                "crypto_runtime_transport_configuration_invalid"
            ) from exc


def _require_exact_service_paths(
    *,
    token_file: Path | str,
    output_root: Path | str,
    epoch_context: Any | None = None,
) -> tuple[Path, Path]:
    token = Path(token_file)
    root = Path(output_root)
    if token != RUNTIME_TOKEN_FILE:
        raise CryptoDelayedPaperRuntimeError(
            "runtime_token_file_must_equal_dedicated_leaf"
        )
    if epoch_context is None:
        raise CryptoDelayedPaperRuntimeError("runtime_epoch_context_required")
    else:
        try:
            from Crypto.delayed_paper_epoch import (
                validate_epoch_runtime_context,
            )

            validate_epoch_runtime_context(
                epoch_context,
                output_root=root,
            )
        except Exception as exc:
            raise CryptoDelayedPaperRuntimeError(
                "runtime_epoch_context_invalid"
            ) from exc
    if not token.is_absolute() or not root.is_absolute():
        raise CryptoDelayedPaperRuntimeError("runtime_service_paths_must_be_absolute")
    return token, root


@dataclass(frozen=True)
class _RuntimeObservationState:
    pending: Mapping[str, Any] | None
    latest_market_slot: datetime | None
    latest_observation_slot: datetime | None
    latest_data_gap: Mapping[str, Any] | None
    had_prior_evidence: bool


def _observation_slot(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise CryptoDelayedPaperRuntimeError("runtime_observation_slot_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CryptoDelayedPaperRuntimeError(
            "runtime_observation_slot_invalid"
        ) from exc
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() is None
        or parsed.utcoffset() != timedelta(0)
        or parsed.second != 0
        or parsed.microsecond != 0
        or parsed.minute % 5 != 0
    ):
        raise CryptoDelayedPaperRuntimeError("runtime_observation_slot_invalid")
    return parsed.astimezone(timezone.utc)


def _verified_capital_head(root: Path) -> tuple[int, str]:
    capital_root = root / "capital"
    if not capital_root.is_dir():
        raise CryptoDelayedPaperRuntimeError("runtime_gap_capital_missing")
    try:
        ledger = CryptoCapitalLedger(capital_root)
        sequence, checksum = ledger.head()
        if sequence <= 0 or len(checksum) != 64:
            raise CryptoLedgerError("capital_gap_anchor_invalid")
        for symbol in FROZEN_SYMBOLS:
            incomplete, _, _ = ledger.account_cycle_guard(symbol=symbol)
            if incomplete:
                raise CryptoLedgerError("capital_prior_cycle_incomplete")
    except (CryptoLedgerError, OSError, TypeError, ValueError) as exc:
        raise CryptoDelayedPaperRuntimeError("runtime_gap_capital_invalid") from exc
    return sequence, checksum


def _validate_gap_capital_anchor(
    root: Path,
    gap: Mapping[str, Any],
    *,
    require_current: bool,
) -> None:
    sequence, checksum = _verified_capital_head(root)
    gap_sequence = gap.get("capital_head_sequence")
    gap_checksum = gap.get("capital_head_checksum")
    if (
        isinstance(gap_sequence, bool)
        or not isinstance(gap_sequence, int)
        or gap_sequence <= 0
        or gap_sequence > sequence
        or not isinstance(gap_checksum, str)
        or len(gap_checksum) != 64
    ):
        raise CryptoDelayedPaperRuntimeError("runtime_data_gap_invalid")
    try:
        ledger = CryptoCapitalLedger(root / "capital")
        anchor = ledger.event_by_checksum(gap_checksum)
    except (CryptoLedgerError, OSError, TypeError, ValueError) as exc:
        raise CryptoDelayedPaperRuntimeError("runtime_gap_capital_invalid") from exc
    if (
        not isinstance(anchor, Mapping)
        or anchor.get("sequence") != gap_sequence
        or (require_current and (gap_sequence != sequence or gap_checksum != checksum))
    ):
        raise CryptoDelayedPaperRuntimeError("runtime_gap_capital_invalid")


def _data_gap_id_material(
    *,
    prior_market_slot: str,
    skipped_from: str,
    skipped_to: str,
    recovery_market_slot: str,
    reason_code: str,
    recovery_observation_content_sha256: str,
) -> dict[str, Any]:
    return {
        "gap_contract": OUTAGE_GAP_CONTRACT,
        "prior_market_slot": prior_market_slot,
        "skipped_from": skipped_from,
        "skipped_to": skipped_to,
        "recovery_market_slot": recovery_market_slot,
        "reason_code": reason_code,
        "recovery_observation_content_sha256": (recovery_observation_content_sha256),
    }


def _validate_data_gap_event(
    gap: Mapping[str, Any],
) -> tuple[datetime, datetime]:
    if (
        gap.get("contract") != DECISION_LEDGER_CONTRACT
        or gap.get("gap_contract") != OUTAGE_GAP_CONTRACT
        or gap.get("event_type") != "data_gap"
        or gap.get("market") != "crypto"
        or gap.get("market_session") != "24x7"
        or gap.get("reason_code") not in HISTORICAL_GAP_RECOVERY_REASONS
        or gap.get("candidate_generated") is not False
        or gap.get("order_generated") is not False
        or gap.get("fill_generated") is not False
        or gap.get("capital_effect") != "none_preserved_outage_recovery"
    ):
        raise CryptoDelayedPaperRuntimeError("runtime_data_gap_invalid")
    for key, expected in _non_authority_fields().items():
        if gap.get(key) != expected:
            raise CryptoDelayedPaperRuntimeError("runtime_data_gap_invalid")

    prior = _observation_slot(gap.get("prior_market_slot"))
    skipped_from = _observation_slot(gap.get("skipped_from"))
    skipped_to = _observation_slot(gap.get("skipped_to"))
    recovery = _observation_slot(gap.get("recovery_market_slot"))
    rejected_window_end = _observation_slot(gap.get("rejected_target_window_end"))
    rejected_cutoff = gap.get("rejected_target_observation_cutoff")
    if (
        skipped_from != prior + timedelta(minutes=5)
        or skipped_to != recovery - timedelta(minutes=5)
        or skipped_from > skipped_to
        or rejected_window_end != skipped_from + timedelta(minutes=5)
        or rejected_cutoff
        != _iso_utc(rejected_window_end + timedelta(seconds=SLOT_CUTOFF_DELAY_SECONDS))
    ):
        raise CryptoDelayedPaperRuntimeError("runtime_data_gap_invalid")

    observation = gap.get("recovery_observation")
    source_proof = gap.get("source_proof")
    counterfactuals = gap.get("recovery_counterfactuals")
    if (
        not isinstance(observation, Mapping)
        or observation.get("market_slot") != _iso_utc(recovery)
        or not isinstance(source_proof, Mapping)
        or source_proof.get("same_observation") is not True
        or source_proof.get("evidence_gate")
        != {
            "state": "ready",
            "freshness": "fresh",
            "quality_valid": True,
            "degraded": False,
            "receipt_lineage_complete": True,
            "same_observation": True,
        }
        or source_proof.get("profile_sha256") != observation.get("profile_sha256")
        or source_proof.get("market_content_sha256")
        != observation.get("market_content_sha256")
        or source_proof.get("source_observation_sha256")
        != observation.get("source_observation_sha256")
        or source_proof.get("recovery_observation_content_sha256")
        != observation.get("observation_content_sha256")
        or not isinstance(source_proof.get("source_bindings"), Mapping)
        or source_proof.get("source_bindings") != observation.get("source_bindings")
        or not isinstance(source_proof.get("catalog_version"), str)
        or not source_proof.get("catalog_version")
        or not isinstance(counterfactuals, Mapping)
        or set(counterfactuals) != set(FROZEN_SYMBOLS)
    ):
        raise CryptoDelayedPaperRuntimeError("runtime_data_gap_invalid")
    if any(
        not isinstance(proof, Mapping)
        or proof.get("catalog_version") != source_proof.get("catalog_version")
        for proof in source_proof["source_bindings"].values()
    ):
        raise CryptoDelayedPaperRuntimeError("runtime_data_gap_invalid")
    observation_material = dict(observation)
    claimed_observation_sha = observation_material.pop(
        "observation_content_sha256",
        None,
    )
    if claimed_observation_sha != _sha256(observation_material):
        raise CryptoDelayedPaperRuntimeError("runtime_data_gap_invalid")
    for symbol, item in counterfactuals.items():
        if (
            not isinstance(item, Mapping)
            or item.get("symbol") != symbol
            or item.get("authority") != "none"
            or item.get("candidate_generated") is not False
            or item.get("order_generated") is not False
            or item.get("fill_generated") is not False
            or not isinstance(item.get("counterfactual"), Mapping)
            or not isinstance(item.get("counterfactual_decision"), Mapping)
        ):
            raise CryptoDelayedPaperRuntimeError("runtime_data_gap_invalid")

    id_material = _data_gap_id_material(
        prior_market_slot=_iso_utc(prior),
        skipped_from=_iso_utc(skipped_from),
        skipped_to=_iso_utc(skipped_to),
        recovery_market_slot=_iso_utc(recovery),
        reason_code=str(gap.get("reason_code")),
        recovery_observation_content_sha256=str(claimed_observation_sha),
    )
    expected_event_id = f"crypto-delayed-data-gap-{_sha256(id_material)[:24]}"
    if gap.get("event_id") != expected_event_id:
        raise CryptoDelayedPaperRuntimeError("runtime_data_gap_invalid")
    return prior, recovery


def _runtime_observation_state(root: Path) -> _RuntimeObservationState:
    delayed_root = root / "delayed_paper"
    if not delayed_root.exists():
        return _RuntimeObservationState(
            pending=None,
            latest_market_slot=None,
            latest_observation_slot=None,
            latest_data_gap=None,
            had_prior_evidence=(root / "capital").exists(),
        )
    try:
        store = CryptoDelayedPaperObservationStore(root)
        checkpoint = store.runtime_checkpoint()
        gap_events = store.data_gap_events()
    except (
        CryptoDelayedPaperLedgerError,
        OSError,
        RuntimeError,
        ValueError,
    ) as exc:
        raise CryptoDelayedPaperRuntimeError(
            "runtime_observation_state_invalid"
        ) from exc
    latest_raw = checkpoint.get("latest_market_slot")
    latest_observation = (
        _observation_slot(latest_raw) if latest_raw is not None else None
    )
    latest_gap: Mapping[str, Any] | None = None
    latest_gap_slot: datetime | None = None
    previous_gap_slot: datetime | None = None
    for gap in gap_events:
        _, recovery_slot = _validate_data_gap_event(gap)
        if previous_gap_slot is not None and recovery_slot <= previous_gap_slot:
            raise CryptoDelayedPaperRuntimeError("runtime_data_gap_invalid")
        previous_gap_slot = recovery_slot
        latest_gap = gap
        latest_gap_slot = recovery_slot
    if latest_gap is not None:
        _validate_gap_capital_anchor(
            root,
            latest_gap,
            require_current=(
                latest_observation is None
                or (
                    latest_gap_slot is not None and latest_gap_slot > latest_observation
                )
            ),
        )
    latest = latest_observation
    if latest_gap_slot is not None and (latest is None or latest_gap_slot > latest):
        latest = latest_gap_slot
    pending = checkpoint.get("pending")
    if (
        pending is not None
        and latest_gap_slot is not None
        and (latest_observation is None or latest_gap_slot > latest_observation)
    ):
        raise CryptoDelayedPaperRuntimeError("runtime_data_gap_pending_conflict")
    return _RuntimeObservationState(
        pending=pending,
        latest_market_slot=latest,
        latest_observation_slot=latest_observation,
        latest_data_gap=latest_gap,
        had_prior_evidence=bool(
            int(checkpoint.get("observation_count") or 0) > 0
            or store.ledger_path.exists()
            or (root / "capital").exists()
        ),
    )


def _window_request_for_end(window_end: datetime) -> CryptoFiveMinuteWindowRequest:
    normalized = _utc_now(window_end)
    if (
        normalized.second != 0
        or normalized.microsecond != 0
        or normalized.minute % 5 != 0
    ):
        raise CryptoDelayedPaperRuntimeError("runtime_window_end_invalid")
    return CryptoFiveMinuteWindowRequest(
        window_end=normalized,
        observation_cutoff=normalized + timedelta(seconds=SLOT_CUTOFF_DELAY_SECONDS),
    )


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _run_core_safely(
    *,
    port: _LazyCryptoFiveMinutePort,
    profile: CryptoFiveMinuteDataProfile,
    request: CryptoFiveMinuteWindowRequest,
    root: Path,
) -> dict[str, Any]:
    try:
        return run_crypto_delayed_paper_once(
            port=port,
            profile=profile,
            request=request,
            output_root=root,
        )
    except (
        CryptoDelayedPaperLedgerError,
        CryptoFixtureAutoSimError,
        CryptoFiveMinuteDataError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        raise CryptoDelayedPaperRuntimeError("runtime_core_cycle_failed") from exc


def _build_outage_gap_event(
    *,
    prior_market_slot: datetime,
    historical_request: CryptoFiveMinuteWindowRequest,
    current_request: CryptoFiveMinuteWindowRequest,
    catalog_version: str,
    observation: Mapping[str, Any],
    prepared: Mapping[
        str,
        tuple[dict[str, Any], dict[str, Any], dict[str, Any]],
    ],
    capital_head_sequence: int,
    capital_head_checksum: str,
    reason_code: str,
) -> dict[str, Any]:
    recovery_market_slot = _observation_slot(observation.get("market_slot"))
    skipped_from = prior_market_slot + timedelta(minutes=5)
    skipped_to = recovery_market_slot - timedelta(minutes=5)
    if (
        historical_request.window_end != skipped_from + timedelta(minutes=5)
        or current_request.window_end != recovery_market_slot + timedelta(minutes=5)
        or skipped_from > skipped_to
    ):
        raise CryptoDelayedPaperRuntimeError("runtime_outage_gap_not_recoverable")
    observation_sha = observation.get("observation_content_sha256")
    if not isinstance(observation_sha, str) or len(observation_sha) != 64:
        raise CryptoDelayedPaperRuntimeError("runtime_outage_gap_observation_invalid")
    id_material = _data_gap_id_material(
        prior_market_slot=_iso_utc(prior_market_slot),
        skipped_from=_iso_utc(skipped_from),
        skipped_to=_iso_utc(skipped_to),
        recovery_market_slot=_iso_utc(recovery_market_slot),
        reason_code=reason_code,
        recovery_observation_content_sha256=observation_sha,
    )
    counterfactuals: dict[str, Any] = {}
    for symbol in FROZEN_SYMBOLS:
        _, counterfactual, decision = prepared[symbol]
        counterfactuals[symbol] = {
            "symbol": symbol,
            "disposition": "outage_recovery_counterfactual_only",
            "counterfactual": counterfactual,
            "counterfactual_decision": decision,
            "candidate_generated": False,
            "order_generated": False,
            "fill_generated": False,
            "authority": "none",
            **_non_authority_fields(),
        }
    event = {
        "contract": DECISION_LEDGER_CONTRACT,
        "gap_contract": OUTAGE_GAP_CONTRACT,
        "event_id": f"crypto-delayed-data-gap-{_sha256(id_material)[:24]}",
        "event_type": "data_gap",
        "market": "crypto",
        "market_session": "24x7",
        "prior_market_slot": _iso_utc(prior_market_slot),
        "skipped_from": _iso_utc(skipped_from),
        "skipped_to": _iso_utc(skipped_to),
        "recovery_market_slot": _iso_utc(recovery_market_slot),
        "reason_code": reason_code,
        "rejected_target_window_end": _iso_utc(historical_request.window_end),
        "rejected_target_observation_cutoff": _iso_utc(
            historical_request.observation_cutoff
        ),
        "source_proof": {
            "catalog_version": catalog_version,
            "profile_sha256": observation.get("profile_sha256"),
            "market_content_sha256": observation.get("market_content_sha256"),
            "source_observation_sha256": observation.get("source_observation_sha256"),
            "recovery_observation_content_sha256": observation_sha,
            "same_observation": observation.get("same_observation"),
            "evidence_gate": {
                "state": "ready",
                "freshness": "fresh",
                "quality_valid": True,
                "degraded": False,
                "receipt_lineage_complete": True,
                "same_observation": True,
            },
            "source_bindings": observation.get("source_bindings"),
            "current_window_end": _iso_utc(current_request.window_end),
            "current_observation_cutoff": _iso_utc(current_request.observation_cutoff),
        },
        "recovery_observation": observation,
        "recovery_counterfactuals": counterfactuals,
        "capital_head_sequence": capital_head_sequence,
        "capital_head_checksum": capital_head_checksum,
        "capital_effect": "none_preserved_outage_recovery",
        "candidate_generated": False,
        "order_generated": False,
        "fill_generated": False,
        **_non_authority_fields(),
    }
    _validate_data_gap_event(event)
    return event


def _attempt_outage_gap_recovery(
    *,
    port: _LazyCryptoFiveMinutePort,
    profile: CryptoFiveMinuteDataProfile,
    historical_request: CryptoFiveMinuteWindowRequest,
    current_request: CryptoFiveMinuteWindowRequest,
    catalog_version: str,
    root: Path,
    expected_latest_observation_slot: datetime,
    reason_code: str,
) -> dict[str, Any]:
    store = CryptoDelayedPaperObservationStore(root)
    with store.cycle():
        checkpoint = store.runtime_checkpoint()
        if checkpoint.get("pending") is not None:
            raise CryptoDelayedPaperRuntimeError("runtime_outage_gap_pending_forbidden")
        checkpoint_slot = checkpoint.get("latest_market_slot")
        if (
            checkpoint_slot is None
            or _observation_slot(checkpoint_slot) != expected_latest_observation_slot
        ):
            raise CryptoDelayedPaperRuntimeError("runtime_outage_gap_state_changed")
        existing_gaps = store.data_gap_events()
        if existing_gaps:
            _, latest_gap_slot = _validate_data_gap_event(existing_gaps[-1])
            if latest_gap_slot >= expected_latest_observation_slot:
                raise CryptoDelayedPaperRuntimeError("runtime_outage_gap_state_changed")
        try:
            snapshot = port.load_snapshot(
                profile=profile,
                request=current_request,
            )
            if not isinstance(snapshot, CryptoFiveMinuteSnapshot):
                raise CryptoFiveMinuteDataError("crypto_5m_snapshot_type_invalid")
            snapshot.verify_against(
                profile=profile,
                request=current_request,
            )
            observation = _snapshot_to_observation(snapshot)
            prepared = _prepare_observation(
                observation,
                llm_evidence=None,
            )
        except CryptoFiveMinuteDataError as exc:
            return _data_reject(
                store=store,
                profile=profile,
                request=current_request,
                reason_code=(getattr(exc, "reason_code", None) or str(exc)),
            )
        capital_head_sequence, capital_head_checksum = _verified_capital_head(root)
        event = _build_outage_gap_event(
            prior_market_slot=expected_latest_observation_slot,
            historical_request=historical_request,
            current_request=current_request,
            catalog_version=catalog_version,
            observation=observation,
            prepared=prepared,
            capital_head_sequence=capital_head_sequence,
            capital_head_checksum=capital_head_checksum,
            reason_code=reason_code,
        )
        stored = store.append_event(event)
        _, recovery_slot = _validate_data_gap_event(stored)
        _validate_gap_capital_anchor(root, stored, require_current=True)
        return {
            "contract": OUTAGE_GAP_CONTRACT,
            "status": "completed",
            "reason_code": "crypto_outage_gap_recovered",
            "event_id": stored["event_id"],
            "event_checksum": stored["checksum"],
            "skipped_from": stored["skipped_from"],
            "skipped_to": stored["skipped_to"],
            "recovery_market_slot": _iso_utc(recovery_slot),
            "capital_head_sequence": capital_head_sequence,
            "capital_head_checksum": capital_head_checksum,
            "capital_effect": "none_preserved_outage_recovery",
            "candidate_generated": False,
            "order_generated": False,
            "fill_generated": False,
            **_non_authority_fields(),
        }


def run_crypto_delayed_paper_server_once(
    *,
    runtime_manifest: Path | str,
    token_file: Path | str,
    output_root: Path | str,
    now: datetime,
    transport_factory: Callable[..., HTTPTransport] = (build_runtime_transport),
    epoch_context: Any | None = None,
) -> dict[str, Any]:
    """Recover pending work, then process up to one additional missing window."""

    _assert_simulation_only()
    token, root = _require_exact_service_paths(
        token_file=token_file,
        output_root=output_root,
        epoch_context=epoch_context,
    )
    epoch_receipt_fields: dict[str, Any] = {}
    if epoch_context is not None:
        from Crypto.delayed_paper_epoch import (
            epoch_runtime_receipt_fields as build_epoch_receipt_fields,
        )

        epoch_receipt_fields = build_epoch_receipt_fields(epoch_context)
    manifest = load_crypto_delayed_paper_runtime_manifest(runtime_manifest)
    current_request = crypto_runtime_window_request(now)
    state_before = _runtime_observation_state(root)
    pending = state_before.pending
    lazy_port = _LazyCryptoFiveMinutePort(
        manifest=manifest,
        token_file=token,
        transport_factory=transport_factory,
    )
    cycle_results: list[dict[str, Any]] = []
    recovered_observations: list[dict[str, Any]] = []
    latest_completed_window_end = (
        state_before.latest_market_slot + timedelta(minutes=5)
        if state_before.latest_market_slot is not None and pending is None
        else None
    )

    if pending is not None:
        pending_market_slot = _observation_slot(pending.get("market_slot"))
        pending_window_end = pending_market_slot + timedelta(minutes=5)
        pending_request = _window_request_for_end(pending_window_end)
        recovered_result = _run_core_safely(
            port=lazy_port,
            profile=manifest.profile,
            request=pending_request,
            root=root,
        )
        cycle_results.append(
            {
                "cycle_kind": "pending_recovery",
                "target_window_end": _iso_utc(pending_window_end),
                "result": recovered_result,
            }
        )
        recovered_observations.append(
            {
                "observation_id": pending.get("observation_id"),
                "market_slot": pending.get("market_slot"),
                "source_profile_sha256": pending.get("profile_sha256"),
                "source_catalog_version": None,
                "runtime_manifest_profile_used_for_recovery": False,
            }
        )
        if recovered_result.get("status") != "completed":
            raise CryptoDelayedPaperRuntimeError(
                "runtime_pending_recovery_not_completed"
            )
        latest_completed_window_end = pending_window_end

    while len(cycle_results) < MAX_CYCLES_PER_INVOCATION:
        if latest_completed_window_end is None:
            target_window_end = current_request.window_end
        elif latest_completed_window_end < current_request.window_end:
            target_window_end = latest_completed_window_end + timedelta(minutes=5)
        elif latest_completed_window_end == current_request.window_end:
            break
        else:
            raise CryptoDelayedPaperRuntimeError(
                "runtime_clock_before_latest_observation"
            )
        request = _window_request_for_end(target_window_end)
        result = _run_core_safely(
            port=lazy_port,
            profile=manifest.profile,
            request=request,
            root=root,
        )
        cycle_results.append(
            {
                "cycle_kind": "fresh_query",
                "target_window_end": _iso_utc(target_window_end),
                "result": result,
            }
        )
        if (
            result.get("status") == "data_reject"
            and result.get("reason_code") in HISTORICAL_GAP_RECOVERY_REASONS
            and pending is None
            and state_before.latest_observation_slot is not None
            and state_before.latest_observation_slot == state_before.latest_market_slot
            and target_window_end < current_request.window_end
            and len(cycle_results) < MAX_CYCLES_PER_INVOCATION
        ):
            gap_result = _attempt_outage_gap_recovery(
                port=lazy_port,
                profile=manifest.profile,
                historical_request=request,
                current_request=current_request,
                catalog_version=manifest.catalog_version,
                root=root,
                expected_latest_observation_slot=(state_before.latest_observation_slot),
                reason_code=str(result.get("reason_code")),
            )
            cycle_results.append(
                {
                    "cycle_kind": "outage_gap_recovery",
                    "target_window_end": _iso_utc(current_request.window_end),
                    "result": gap_result,
                }
            )
            if gap_result.get("status") == "completed":
                latest_completed_window_end = current_request.window_end
            break
        if result.get("status") != "completed":
            break
        latest_completed_window_end = target_window_end

    outage_gap_attempt_failed = bool(
        cycle_results
        and cycle_results[-1]["cycle_kind"] == "outage_gap_recovery"
        and cycle_results[-1]["result"].get("status") != "completed"
    )
    backlog_remaining = bool(
        latest_completed_window_end is not None
        and latest_completed_window_end < current_request.window_end
        and len(cycle_results) >= MAX_CYCLES_PER_INVOCATION
        and not outage_gap_attempt_failed
    )
    if not cycle_results:
        core_result: Mapping[str, Any] = {
            "status": "noop",
            "reason_code": "crypto_slot_already_completed",
            "execution_authority": False,
            "production_eligible": False,
        }
    else:
        core_result = cycle_results[-1]["result"]

    warmup_eligible = bool(
        not state_before.had_prior_evidence
        and len(cycle_results) == 1
        and core_result.get("status") == "data_reject"
        and core_result.get("reason_code") == "crypto_5m_window_incomplete"
    )
    if backlog_remaining:
        status = "backlog_pending"
    else:
        status = str(core_result.get("status") or "failed_closed")
    return {
        "contract": CRYPTO_RUNTIME_CONTRACT,
        "status": status,
        "market": "crypto",
        "market_session": "24x7",
        "runtime_manifest_sha256": manifest.sha256,
        "fresh_query_catalog_version": manifest.catalog_version,
        "fresh_query_profile_sha256": manifest.profile.sha256,
        "requested_window_end": _iso_utc(current_request.window_end),
        "requested_observation_cutoff": _iso_utc(current_request.observation_cutoff),
        "requested_window_consumed": bool(
            latest_completed_window_end is not None
            and latest_completed_window_end >= current_request.window_end
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
        "market_data_access_attempt_count": lazy_port.load_snapshot_calls,
        "transport_factory_attempt_count": lazy_port.transport_factory_attempts,
        "market_data_network_used": (lazy_port.transport_constructed_count > 0),
        "model_network_used": False,
        "learning_mode": "detached_offline_worker",
        "learning_authority": False,
        "learning_invoked": False,
        "broker_network_used": False,
        "testnet_used": False,
        "live_broker_used": False,
        "real_trading_enabled": False,
        "execution_eligible": False,
        "execution_authority": False,
        "production_eligible": False,
        "promotion_authorized": False,
        "automatic_promotion_enabled": False,
        "automatic_risk_expansion_enabled": False,
        "outbox_id": None,
        "capital_commit_id": None,
        "core_result": core_result,
        "cycle_results": cycle_results,
        "recovered_observations": recovered_observations,
        **epoch_receipt_fields,
    }


def crypto_runtime_receipt_exit_code(
    receipt: Mapping[str, Any],
) -> int:
    """Map expected warm-up to success and operational rejects to failure."""

    if not isinstance(receipt, Mapping):
        return 2
    if receipt.get("backlog_remaining") is True:
        return 2
    if receipt.get("status") in {"completed", "noop"}:
        return 0
    core = receipt.get("core_result")
    if (
        receipt.get("status") == "data_reject"
        and isinstance(core, Mapping)
        and core.get("reason_code") == "crypto_5m_window_incomplete"
        and receipt.get("warmup_eligible") is True
    ):
        return 0
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=("Run one loopback-only Crypto closed-5m delayed-paper cycle")
    )
    parser.add_argument(
        "--runtime-manifest",
        type=Path,
        required=True,
    )
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        receipt = run_crypto_delayed_paper_server_once(
            runtime_manifest=args.runtime_manifest,
            token_file=args.token_file,
            output_root=args.output_root,
            now=datetime.now(tz=timezone.utc),
        )
        exit_code = crypto_runtime_receipt_exit_code(receipt)
    except Exception:
        print(
            "crypto delayed paper runtime failed closed",
            file=sys.stderr,
        )
        return 2
    if exit_code != 0:
        print(
            "crypto delayed paper runtime failed closed",
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
    "CRYPTO_RUNTIME_CONTRACT",
    "CryptoDelayedPaperRuntimeError",
    "CryptoDelayedPaperRuntimeManifest",
    "RUNTIME_MANIFEST_CONTRACT",
    "RUNTIME_OUTPUT_ROOT",
    "RUNTIME_TOKEN_FILE",
    "SLOT_CUTOFF_DELAY_SECONDS",
    "crypto_runtime_receipt_exit_code",
    "crypto_runtime_window_request",
    "load_crypto_delayed_paper_runtime_manifest",
    "main",
    "run_crypto_delayed_paper_server_once",
]
