"""Read-only health watch for the Crypto ten-symbol observation chain.

This watchdog is a no-write, zero-authority observer of the ten-symbol
5-minute observation accumulator and its TradingDatas data plane.  It reads
the append-only store through the store's lock-free read-only path, never
rebuilds heads or indexes, never touches capital/order/model state, and
emits one machine-readable JSON status.  Every store corruption or contract
drift fails closed: the affected checks report ``failed`` and the process
exits non-zero.

Checks:
  - ``observation_chain_lag``: lag of ``latest_terminal_slot`` behind now.
  - ``reject_gap_rate``: data_reject/data_gap share over the trailing slots.
  - ``spread_sampling``: spreads sidecar presence and per-slot spread status.
  - ``tradingdatas``: catalog/query liveness plus freshness of the frozen
    bar datasets, the book_ticker family and any catalog open_interest
    dataset (only when a runtime manifest is supplied).
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import http.client
import json
import os
from pathlib import Path
import sys
from typing import Any, Callable, Mapping, Sequence
import urllib.error

from Crypto.market_observation import (
    BAR_FIELDS,
    BOOK_TICKER_FIELDS,
    OBSERVATION_SYMBOLS,
    TEN_SYMBOL_SPREADS_SIDECAR_CONTRACT,
    _book_ticker_dataset_id,
    validate_ten_symbol_spreads_sidecar,
)
from Crypto.market_observation import CryptoMarketObservationError
from Crypto.ten_symbol_observation_profile import CryptoTenSymbolProfileError
from Crypto.ten_symbol_observation_runtime import (
    RUNTIME_TOKEN_FILE,
    TEN_SYMBOL_RUNTIME_CONFIG,
    CryptoTenSymbolObservationRuntimeConfig,
    CryptoTenSymbolObservationRuntimeManifest,
    crypto_ten_symbol_observation_window,
    load_crypto_ten_symbol_observation_runtime_manifest,
)
from Crypto.ten_symbol_observation_store import (
    TEN_SYMBOL_CONTRACTS,
    TERMINAL_SLOT_TYPES,
    CryptoTenSymbolObservationContracts,
    CryptoTenSymbolObservationStore,
    CryptoTenSymbolObservationStoreError,
    _market_slot,
)
from shared.data.sharedsignals_v1 import (
    HTTPStatusError,
    HTTPTransport,
    QueryRequest,
    SharedSignalsV1Client,
    SharedSignalsV1Config,
    SharedSignalsV1Error,
)
from shared.data.tradingdatas_transport import (
    RuntimeGateConfigurationError,
    TradingDatasAuthenticationError,
    build_runtime_transport,
)


HEALTH_WATCH_CONTRACT = "tradingagent.crypto.ten_symbol_health_watch.v1"
STATUS_ORDER = {"ok": 0, "degraded": 1, "failed": 2}

# The observation timer starts each slot at bar close +3m25s and the slot
# cutoff is close +55s, so a healthy chain's latest terminal slot never lags
# now by more than ~9 minutes.  Ten minutes therefore means one full timer
# cycle was missed; fifteen minutes (the postmortem threshold) means the
# chain is stalled.
STALE_DEGRADED_SECONDS = 600
STALE_FAILED_SECONDS = 900
# Trailing one hour of 5-minute slots for reject/gap and spread sampling.
WINDOW_SLOTS = 12
REJECT_GAP_FAILED_RATIO = 0.25
# Data-plane freshness budgets.  Bars close every 5 minutes; 30 minutes is
# six missed bars.  book_ticker is a current snapshot sampled per slot.
# open_interest has no frozen cadence contract in this repository yet, so it
# gets a tolerant one-hour budget and can only degrade, never fail alone.
BAR_FRESHNESS_SECONDS = 1800
BOOK_TICKER_FRESHNESS_SECONDS = 600
OPEN_INTEREST_FRESHNESS_SECONDS = 3600
HEALTH_WATCH_TIMEOUT_SECONDS = 30.0

DEFAULT_TOKEN_FILE = RUNTIME_TOKEN_FILE


class CryptoTenSymbolHealthWatchError(RuntimeError):
    """Stable, redacted fail-closed health-watch error."""


def _non_authority_fields() -> dict[str, Any]:
    return {
        "authority": "none",
        "read_only": True,
        "store_write_eligible": False,
        "execution_eligible": False,
        "execution_authority": False,
        "capital_write_eligible": False,
        "model_authority": False,
        "production_eligible": False,
        "real_trading_enabled": False,
        "promotion_authorized": False,
        "automatic_promotion_enabled": False,
        "automatic_risk_expansion_enabled": False,
    }


def _utc_now(value: datetime) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
        or value.utcoffset() != timedelta(0)
    ):
        raise CryptoTenSymbolHealthWatchError("health_watch_now_must_be_utc")
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _worst(statuses: list[str]) -> str:
    return max(statuses, key=lambda status: STATUS_ORDER[status])


def _check(
    status: str,
    *,
    reason_code: str | None,
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    if status not in STATUS_ORDER:
        raise CryptoTenSymbolHealthWatchError("health_watch_status_invalid")
    return {
        "status": status,
        "reason_code": reason_code,
        "evidence": dict(evidence),
    }


def _failed_store_checks(reason_code: str) -> dict[str, dict[str, Any]]:
    return {
        name: _check("failed", reason_code=reason_code, evidence={})
        for name in ("observation_chain_lag", "reject_gap_rate", "spread_sampling")
    }


def _terminal_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        event
        for event in events
        if event.get("event_type") in TERMINAL_SLOT_TYPES
    ]


def _check_observation_chain_lag(
    *,
    events: list[dict[str, Any]],
    pending: Mapping[str, Any] | None,
    now: datetime,
) -> dict[str, Any]:
    terminal = _terminal_events(events)
    expected_slot = crypto_ten_symbol_observation_window(now).window_end
    evidence: dict[str, Any] = {
        "latest_terminal_slot": None,
        "expected_current_slot": _iso(expected_slot),
        "lag_seconds": None,
        "pending_window_end": (
            str(pending.get("window_end")) if pending is not None else None
        ),
        "event_count": len(events),
    }
    if not terminal:
        return _check(
            "degraded",
            reason_code="crypto_ten_symbol_health_no_terminal_evidence",
            evidence=evidence,
        )
    latest = max(_market_slot(str(event.get("window_end"))) for event in terminal)
    lag_seconds = (now - latest).total_seconds()
    evidence["latest_terminal_slot"] = _iso(latest)
    evidence["lag_seconds"] = int(lag_seconds)
    if lag_seconds < -STALE_DEGRADED_SECONDS:
        return _check(
            "failed",
            reason_code="crypto_ten_symbol_health_clock_before_latest_slot",
            evidence=evidence,
        )
    if lag_seconds > STALE_FAILED_SECONDS:
        return _check(
            "failed",
            reason_code="crypto_ten_symbol_health_chain_stalled",
            evidence=evidence,
        )
    if lag_seconds > STALE_DEGRADED_SECONDS:
        return _check(
            "degraded",
            reason_code="crypto_ten_symbol_health_chain_lagging",
            evidence=evidence,
        )
    return _check("ok", reason_code=None, evidence=evidence)


def _check_reject_gap_rate(
    *,
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    terminal = _terminal_events(events)
    evidence: dict[str, Any] = {
        "window_slots": WINDOW_SLOTS,
        "terminal_slots_in_window": 0,
        "data_gap_count": 0,
        "data_reject_count": 0,
        "reject_gap_ratio": None,
    }
    if not terminal:
        return _check(
            "degraded",
            reason_code="crypto_ten_symbol_health_no_terminal_evidence",
            evidence=evidence,
        )
    window = terminal[-WINDOW_SLOTS:]
    window_start = _market_slot(str(window[0].get("window_end")))
    gaps = sum(event.get("event_type") == "data_gap" for event in window)
    rejects = sum(
        event.get("event_type") == "data_reject"
        and _market_slot(str(event.get("window_end"))) >= window_start
        for event in events
    )
    ratio = (gaps + rejects) / len(window)
    evidence.update(
        {
            "terminal_slots_in_window": len(window),
            "data_gap_count": gaps,
            "data_reject_count": rejects,
            "reject_gap_ratio": ratio,
        }
    )
    if ratio > REJECT_GAP_FAILED_RATIO:
        return _check(
            "failed",
            reason_code="crypto_ten_symbol_health_reject_gap_rate_exceeded",
            evidence=evidence,
        )
    if ratio > 0:
        return _check(
            "degraded",
            reason_code="crypto_ten_symbol_health_reject_gap_present",
            evidence=evidence,
        )
    return _check("ok", reason_code=None, evidence=evidence)


def _check_spread_sampling(
    *,
    store: CryptoTenSymbolObservationStore,
    events: list[dict[str, Any]],
    symbols: Sequence[str] = OBSERVATION_SYMBOLS,
    spreads_sidecar_contract: str = TEN_SYMBOL_SPREADS_SIDECAR_CONTRACT,
) -> dict[str, Any]:
    terminal = _terminal_events(events)[-WINDOW_SLOTS:]
    eligible = [event for event in terminal if "spread" in event]
    evidence: dict[str, Any] = {
        "window_slots": WINDOW_SLOTS,
        "eligible_slots": len(eligible),
        "completed_slots": 0,
        "degraded_slots": 0,
        "unavailable_slots": 0,
        "missing_sidecar_slots": 0,
        "sampled_symbol_ratio": None,
    }
    if not eligible:
        # Slots recorded before the spread feature carry no spread block and
        # are contractually feature-ineligible; they must not penalize health.
        return _check(
            "ok",
            reason_code="crypto_ten_symbol_health_spread_feature_ineligible",
            evidence=evidence,
        )
    sampled_symbols = 0
    for event in eligible:
        block = event.get("spread")
        if not isinstance(block, Mapping):
            raise CryptoTenSymbolHealthWatchError(
                "crypto_ten_symbol_health_spread_block_invalid"
            )
        status = block.get("status")
        if status == "completed":
            evidence["completed_slots"] += 1
        elif status == "degraded":
            evidence["degraded_slots"] += 1
        elif status == "unavailable":
            evidence["unavailable_slots"] += 1
        else:
            raise CryptoTenSymbolHealthWatchError(
                "crypto_ten_symbol_health_spread_block_invalid"
            )
        sampled = block.get("sampled_symbol_count")
        if isinstance(sampled, bool) or not isinstance(sampled, int) or sampled < 0:
            raise CryptoTenSymbolHealthWatchError(
                "crypto_ten_symbol_health_spread_block_invalid"
            )
        sampled_symbols += sampled
        spread_sha256 = block.get("spread_sha256")
        if spread_sha256 is None:
            continue
        sidecar = store.read_spreads_sidecar(str(event.get("window_end")))
        if sidecar is None:
            # A recorded missing sidecar is a degradation, not corruption.
            evidence["missing_sidecar_slots"] += 1
            continue
        entries = validate_ten_symbol_spreads_sidecar(
            sidecar,
            symbols=symbols,
            spreads_sidecar_contract=spreads_sidecar_contract,
        )
        del entries
        if sidecar.get("spread_sha256") != spread_sha256:
            raise CryptoTenSymbolHealthWatchError(
                "crypto_ten_symbol_health_spread_sidecar_digest_mismatch"
            )
    evidence["sampled_symbol_ratio"] = sampled_symbols / (
        len(symbols) * len(eligible)
    )
    if (
        evidence["missing_sidecar_slots"]
        or evidence["degraded_slots"]
        or evidence["unavailable_slots"]
    ):
        return _check(
            "degraded",
            reason_code="crypto_ten_symbol_health_spread_sampling_impaired",
            evidence=evidence,
        )
    return _check("ok", reason_code=None, evidence=evidence)


def _td_failure_reason(exc: BaseException) -> str:
    """Map one data-plane failure to a stable, redacted reason code."""

    if isinstance(exc, TradingDatasAuthenticationError):
        return "crypto_ten_symbol_health_td_auth_unavailable"
    if isinstance(exc, (urllib.error.HTTPError, HTTPStatusError)):
        return "crypto_ten_symbol_health_td_http_unavailable"
    if isinstance(
        exc,
        (
            CryptoTenSymbolProfileError,
            CryptoMarketObservationError,
            SharedSignalsV1Error,
            RuntimeGateConfigurationError,
        ),
    ):
        return "crypto_ten_symbol_health_td_contract_unavailable"
    if isinstance(
        exc,
        (
            TimeoutError,
            ConnectionError,
            urllib.error.URLError,
            http.client.HTTPException,
        ),
    ):
        return "crypto_ten_symbol_health_td_transport_unavailable"
    return "crypto_ten_symbol_health_td_unexpected_error"


def _parse_stamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _freshness_status(
    *,
    envelope_metadata: Any,
    now: datetime,
    budget_seconds: int,
    row_count: int,
    next_cursor: Any,
) -> tuple[str, dict[str, Any]]:
    """Grade one bounded probe response; never raises on stale data."""

    data_through = _parse_stamp(getattr(envelope_metadata, "data_through", None))
    observed_at = _parse_stamp(getattr(envelope_metadata, "observed_at", None))
    stamp = data_through or observed_at
    state = str(getattr(envelope_metadata, "state", ""))
    degraded_flag = getattr(envelope_metadata, "degraded", None) is True
    age_seconds = (
        int((now - stamp).total_seconds()) if stamp is not None else None
    )
    evidence = {
        "data_through": (
            _iso(data_through) if data_through is not None else None
        ),
        "observed_at": _iso(observed_at) if observed_at is not None else None,
        "age_seconds": age_seconds,
        "row_count": row_count,
        "metadata_state": state,
        "metadata_degraded": degraded_flag,
        "freshness_budget_seconds": budget_seconds,
    }
    if next_cursor is not None:
        evidence["note"] = "unexpected_next_cursor"
    if stamp is None:
        return "degraded", {**evidence, "freshness_reason": "timestamp_missing"}
    if age_seconds is not None and age_seconds < -STALE_DEGRADED_SECONDS:
        return "failed", {**evidence, "freshness_reason": "future_timestamp"}
    if age_seconds is not None and age_seconds > budget_seconds:
        return "degraded", {**evidence, "freshness_reason": "stale"}
    if degraded_flag or state.lower() not in {"ready", "healthy", "ok", "available"}:
        return "degraded", {**evidence, "freshness_reason": "metadata_not_ready"}
    return "ok", evidence


def _probe_client(
    *,
    manifest: CryptoTenSymbolObservationRuntimeManifest,
    transport: HTTPTransport,
    dataset_ids: frozenset[str],
    timeout_seconds: float,
) -> tuple[SharedSignalsV1Client, Any]:
    client = SharedSignalsV1Client(
        SharedSignalsV1Config(
            base_url=manifest.base_url,
            expected_catalog_version=manifest.catalog_version,
            dataset_ids=dataset_ids,
            access_policy_id=manifest.access_policy_id,
            catalog_version_policy="evidence_only",
            timeout_seconds=timeout_seconds,
            max_limit=500,
            cache_ttl_seconds=0,
        ),
        transport=transport,
    )
    return client, client.get_catalog()


def _check_tradingdatas(
    *,
    manifest: CryptoTenSymbolObservationRuntimeManifest,
    token_file: Path,
    now: datetime,
    transport_factory: Callable[..., HTTPTransport],
    timeout_seconds: float,
) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "catalog_version": None,
        "catalog_dataset_count": None,
        "families": {},
    }
    if os.environ.get("REAL_TRADING_ENABLED") != "false":
        return _check(
            "failed",
            reason_code="crypto_ten_symbol_health_real_trading_gate",
            evidence=evidence,
        )
    try:
        transport = transport_factory(
            "http-json-v1",
            token_file=token_file,
            base_url=manifest.base_url,
        )
        client, catalog = _probe_client(
            manifest=manifest,
            transport=transport,
            dataset_ids=manifest.dataset_ids,
            timeout_seconds=timeout_seconds,
        )
        # The frozen ten bar datasets are the observation chain's hard
        # contract; any fingerprint drift fails the data-plane check closed.
        manifest.profile.verify_catalog(catalog)
    except Exception as exc:  # noqa: BLE001 - mapped to a redacted reason
        return _check(
            "failed",
            reason_code=_td_failure_reason(exc),
            evidence=evidence,
        )
    evidence["catalog_version"] = catalog.catalog_version
    evidence["catalog_dataset_count"] = len(catalog.dataset_ids)
    catalog_ids = catalog.dataset_ids

    family_statuses: list[str] = []
    families: dict[str, Any] = {}

    bar_dataset = next(
        dataset for dataset in manifest.profile.datasets if dataset.symbol == "BTCUSDT"
    )
    expected_slot = crypto_ten_symbol_observation_window(now).window_end
    bar_evidence: dict[str, Any] = {"dataset_id": bar_dataset.dataset_id}
    try:
        envelope = client.query_uncached(
            QueryRequest(
                dataset_id=bar_dataset.dataset_id,
                schema_major=1,
                fields=BAR_FIELDS,
                filters={
                    "symbol": {"eq": "BTCUSDT"},
                    "open_time": {
                        "between": (
                            _iso(expected_slot - timedelta(minutes=65)),
                            _iso(expected_slot - timedelta(minutes=5)),
                        )
                    },
                },
                order=("symbol:asc", "open_time:asc"),
                limit=13,
            )
        )
        status, fresh = _freshness_status(
            envelope_metadata=envelope.metadata,
            now=now,
            budget_seconds=BAR_FRESHNESS_SECONDS,
            row_count=len(envelope.data),
            next_cursor=envelope.next_cursor,
        )
        if envelope.next_cursor is not None or not envelope.data:
            status = "failed"
            fresh["freshness_reason"] = "bar_probe_window_incomplete"
        families["bars"] = {"status": status, **bar_evidence, **fresh}
    except Exception as exc:  # noqa: BLE001 - mapped to a redacted reason
        families["bars"] = {
            "status": "failed",
            **bar_evidence,
            "reason_code": _td_failure_reason(exc),
        }
    family_statuses.append(families["bars"]["status"])

    book_ticker_id = _book_ticker_dataset_id("BTCUSDT")
    ticker_evidence: dict[str, Any] = {"dataset_id": book_ticker_id}
    if book_ticker_id not in catalog_ids:
        families["book_ticker"] = {
            "status": "degraded",
            **ticker_evidence,
            "reason_code": "crypto_ten_symbol_health_td_dataset_absent",
        }
    else:
        try:
            ticker_client, _ = _probe_client(
                manifest=manifest,
                transport=transport,
                dataset_ids=frozenset({book_ticker_id}),
                timeout_seconds=timeout_seconds,
            )
            envelope = ticker_client.query_uncached(
                QueryRequest(
                    dataset_id=book_ticker_id,
                    schema_major=1,
                    fields=BOOK_TICKER_FIELDS,
                    filters={"symbol": {"eq": "BTCUSDT"}},
                    order=("symbol:asc",),
                    limit=1,
                )
            )
            status, fresh = _freshness_status(
                envelope_metadata=envelope.metadata,
                now=now,
                budget_seconds=BOOK_TICKER_FRESHNESS_SECONDS,
                row_count=len(envelope.data),
                next_cursor=envelope.next_cursor,
            )
            families["book_ticker"] = {"status": status, **ticker_evidence, **fresh}
        except Exception as exc:  # noqa: BLE001 - mapped to a redacted reason
            families["book_ticker"] = {
                "status": "degraded",
                **ticker_evidence,
                "reason_code": _td_failure_reason(exc),
            }
    family_statuses.append(families["book_ticker"]["status"])

    open_interest_ids = sorted(
        dataset_id
        for dataset_id in catalog_ids
        if dataset_id.endswith(".open_interest")
    )
    if not open_interest_ids:
        families["open_interest"] = {
            "status": "degraded",
            "dataset_id": None,
            "reason_code": "crypto_ten_symbol_health_td_dataset_absent",
        }
    else:
        open_interest_id = open_interest_ids[0]
        oi_evidence: dict[str, Any] = {"dataset_id": open_interest_id}
        try:
            row = next(
                item for item in catalog.data if item.get("dataset_id") == (
                    open_interest_id
                )
            )
            schema_major = row.get("schema_major")
            default_fields = row.get("default_fields")
            fields = (
                tuple(default_fields)
                if isinstance(default_fields, list)
                and default_fields
                and all(isinstance(item, str) for item in default_fields)
                else None
            )
            oi_client, _ = _probe_client(
                manifest=manifest,
                transport=transport,
                dataset_ids=frozenset({open_interest_id}),
                timeout_seconds=timeout_seconds,
            )
            envelope = oi_client.query_uncached(
                QueryRequest(
                    dataset_id=open_interest_id,
                    schema_major=(
                        schema_major
                        if isinstance(schema_major, int)
                        and not isinstance(schema_major, bool)
                        and schema_major > 0
                        else 1
                    ),
                    fields=fields,
                    limit=1,
                )
            )
            status, fresh = _freshness_status(
                envelope_metadata=envelope.metadata,
                now=now,
                budget_seconds=OPEN_INTEREST_FRESHNESS_SECONDS,
                row_count=len(envelope.data),
                next_cursor=envelope.next_cursor,
            )
            # open_interest has no frozen cadence contract in this
            # repository; it can only degrade the data-plane check.
            if status == "failed":
                status = "degraded"
            families["open_interest"] = {"status": status, **oi_evidence, **fresh}
        except Exception as exc:  # noqa: BLE001 - mapped to a redacted reason
            families["open_interest"] = {
                "status": "degraded",
                **oi_evidence,
                "reason_code": _td_failure_reason(exc),
            }
    family_statuses.append(families["open_interest"]["status"])

    evidence["families"] = families
    status = _worst(family_statuses)
    return _check(
        status,
        reason_code=(
            None if status == "ok" else "crypto_ten_symbol_health_td_impaired"
        ),
        evidence=evidence,
    )


def _open_store_read_only(
    store_root: Path,
    contracts: CryptoTenSymbolObservationContracts = TEN_SYMBOL_CONTRACTS,
) -> CryptoTenSymbolObservationStore:
    root = Path(store_root)
    if not root.is_absolute():
        raise CryptoTenSymbolHealthWatchError(
            "crypto_ten_symbol_health_store_root_invalid"
        )
    if root.is_symlink() or not root.is_dir():
        raise CryptoTenSymbolHealthWatchError(
            "crypto_ten_symbol_health_store_root_missing"
        )
    # The store constructor lazily creates the slot-index directory; a root
    # that lacks it was never initialized and must fail closed instead of
    # being mutated by a read-only watchdog.
    slot_index = root / "slot_index"
    if slot_index.is_symlink() or not slot_index.is_dir():
        raise CryptoTenSymbolHealthWatchError(
            "crypto_ten_symbol_health_store_uninitialized"
        )
    return CryptoTenSymbolObservationStore(root, contracts=contracts)


def build_ten_symbol_health_report(
    *,
    store_root: Path | str,
    now: datetime,
    runtime_manifest: CryptoTenSymbolObservationRuntimeManifest | None = None,
    token_file: Path | str = DEFAULT_TOKEN_FILE,
    transport_factory: Callable[..., HTTPTransport] | None = None,
    timeout_seconds: float = HEALTH_WATCH_TIMEOUT_SECONDS,
    symbols: Sequence[str] = OBSERVATION_SYMBOLS,
    contracts: CryptoTenSymbolObservationContracts = TEN_SYMBOL_CONTRACTS,
    spreads_sidecar_contract: str = TEN_SYMBOL_SPREADS_SIDECAR_CONTRACT,
) -> dict[str, Any]:
    """Build one no-write health snapshot for one observation chain."""

    factory = build_runtime_transport if transport_factory is None else transport_factory
    observed = _utc_now(now)
    checks: dict[str, dict[str, Any]] = {}
    network_used = False
    try:
        store = _open_store_read_only(store_root, contracts)
        events = store.events_read_only()
        pending = store.pending_record_read_only()
        checks["observation_chain_lag"] = _check_observation_chain_lag(
            events=events,
            pending=pending,
            now=observed,
        )
        checks["reject_gap_rate"] = _check_reject_gap_rate(events=events)
        checks["spread_sampling"] = _check_spread_sampling(
            store=store,
            events=events,
            symbols=symbols,
            spreads_sidecar_contract=spreads_sidecar_contract,
        )
    except CryptoTenSymbolObservationStoreError as exc:
        checks.update(_failed_store_checks(str(exc)))
    except (
        CryptoTenSymbolHealthWatchError,
        CryptoMarketObservationError,
        OSError,
        TypeError,
        ValueError,
    ) as exc:
        reason = (
            str(exc)
            if isinstance(exc, CryptoTenSymbolHealthWatchError)
            else "crypto_ten_symbol_health_store_invalid"
        )
        checks.update(_failed_store_checks(reason))
    if runtime_manifest is not None:
        checks["tradingdatas"] = _check_tradingdatas(
            manifest=runtime_manifest,
            token_file=Path(token_file),
            now=observed,
            transport_factory=factory,
            timeout_seconds=timeout_seconds,
        )
        network_used = (
            checks["tradingdatas"]["evidence"].get("catalog_version") is not None
        )
    status = _worst([check["status"] for check in checks.values()])
    return {
        "contract": HEALTH_WATCH_CONTRACT,
        "status": status,
        "market": "crypto",
        "market_session": "24x7",
        "generated_at": _iso(observed),
        "store_root": str(store_root),
        "runtime_manifest_sha256": (
            runtime_manifest.sha256 if runtime_manifest is not None else None
        ),
        "checks": checks,
        "thresholds": {
            "stale_degraded_seconds": STALE_DEGRADED_SECONDS,
            "stale_failed_seconds": STALE_FAILED_SECONDS,
            "window_slots": WINDOW_SLOTS,
            "reject_gap_failed_ratio": REJECT_GAP_FAILED_RATIO,
            "bar_freshness_seconds": BAR_FRESHNESS_SECONDS,
            "book_ticker_freshness_seconds": BOOK_TICKER_FRESHNESS_SECONDS,
            "open_interest_freshness_seconds": OPEN_INTEREST_FRESHNESS_SECONDS,
        },
        "network_used": network_used,
        **_non_authority_fields(),
    }


def health_watch_exit_code(report: Mapping[str, Any]) -> int:
    if not isinstance(report, Mapping):
        return 2
    if any(
        report.get(field) != expected
        for field, expected in _non_authority_fields().items()
    ):
        return 2
    status = report.get("status")
    if status == "ok":
        return 0
    if status == "degraded":
        return 1
    return 2


def run_health_watch_once(
    *,
    store_root: Path | str,
    runtime_manifest: Path | str,
    token_file: Path | str = DEFAULT_TOKEN_FILE,
    now: datetime,
    transport_factory: Callable[..., HTTPTransport] | None = None,
    runtime_config: CryptoTenSymbolObservationRuntimeConfig = TEN_SYMBOL_RUNTIME_CONFIG,
) -> dict[str, Any]:
    root = Path(store_root)
    token = Path(token_file)
    if token != DEFAULT_TOKEN_FILE:
        raise CryptoTenSymbolHealthWatchError(
            "crypto_ten_symbol_health_token_file_must_equal_dedicated_leaf"
        )
    if not root.is_absolute() or not token.is_absolute():
        raise CryptoTenSymbolHealthWatchError(
            "crypto_ten_symbol_health_paths_must_be_absolute"
        )
    manifest = load_crypto_ten_symbol_observation_runtime_manifest(
        runtime_manifest,
        config=runtime_config,
    )
    # The loader already pins the manifest output root to the frozen runtime
    # root; the caller-supplied store root must be exactly that root.
    if root != manifest.output_root:
        raise CryptoTenSymbolHealthWatchError(
            "crypto_ten_symbol_health_store_root_mismatch"
        )
    return build_ten_symbol_health_report(
        store_root=root,
        now=now,
        runtime_manifest=manifest,
        token_file=token,
        transport_factory=transport_factory,
        symbols=runtime_config.symbols,
        contracts=runtime_config.store_contracts,
        spreads_sidecar_contract=runtime_config.spreads_sidecar_contract,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only health watch for the Crypto ten-symbol observation chain"
        )
    )
    parser.add_argument("--store-root", type=Path, required=True)
    parser.add_argument("--runtime-manifest", type=Path, required=True)
    parser.add_argument("--token-file", type=Path, default=DEFAULT_TOKEN_FILE)
    args = parser.parse_args(argv)
    try:
        report = run_health_watch_once(
            store_root=args.store_root,
            runtime_manifest=args.runtime_manifest,
            token_file=args.token_file,
            now=datetime.now(tz=timezone.utc),
        )
        code = health_watch_exit_code(report)
    except Exception:
        print("crypto ten symbol health watch failed closed", file=sys.stderr)
        return 2
    print(
        json.dumps(
            report,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return code


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "BAR_FRESHNESS_SECONDS",
    "BOOK_TICKER_FRESHNESS_SECONDS",
    "CryptoTenSymbolHealthWatchError",
    "DEFAULT_TOKEN_FILE",
    "HEALTH_WATCH_CONTRACT",
    "HEALTH_WATCH_TIMEOUT_SECONDS",
    "OPEN_INTEREST_FRESHNESS_SECONDS",
    "REJECT_GAP_FAILED_RATIO",
    "STALE_DEGRADED_SECONDS",
    "STALE_FAILED_SECONDS",
    "WINDOW_SLOTS",
    "build_ten_symbol_health_report",
    "health_watch_exit_code",
    "main",
    "run_health_watch_once",
]
