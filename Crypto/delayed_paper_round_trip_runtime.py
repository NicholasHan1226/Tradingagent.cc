"""Closed-5m server wrapper for the isolated Crypto round-trip candidate."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import http.client
import json
from pathlib import Path
import re
import sys
import time
from typing import Any, Callable, Mapping
import urllib.error

from Crypto.delayed_paper_ledger import (
    DECISION_LEDGER_CONTRACT,
    CryptoDelayedPaperLedgerError,
    CryptoDelayedPaperObservationStore,
    _non_authority_fields,
)
from Crypto.delayed_paper_round_trip import run_crypto_delayed_paper_round_trip_once
from Crypto.delayed_paper_round_trip_epoch import (
    ROUND_TRIP_EPOCH_MANIFEST_DIRECTORY,
    ROUND_TRIP_EPOCH_MANIFEST_PATH,
    CryptoRoundTripEpochError,
    load_round_trip_epoch_manifest,
    prepare_round_trip_epoch_candidate,
)
from Crypto.delayed_paper_runtime import (
    RUNTIME_TOKEN_FILE,
    CryptoDelayedPaperRuntimeError,
    _LazyCryptoFiveMinutePort,
    crypto_runtime_receipt_exit_code,
    crypto_runtime_window_request,
    load_crypto_delayed_paper_runtime_manifest,
)
from Crypto.fixture_sim.contracts import _assert_simulation_only
from Crypto.five_minute_data import (
    CryptoFiveMinuteDataError,
    CryptoFiveMinuteWindowRequest,
)
from Crypto.round_trip_capital import CryptoRoundTripError
from shared.data.sharedsignals_v1 import HTTPStatusError, HTTPTransport
from shared.data.tradingdatas_transport import (
    TradingDatasAuthenticationError,
    build_runtime_transport,
)


ROUND_TRIP_RUNTIME_CONTRACT = "tradingagent.crypto.round_trip_server_runtime.v1"
ROUND_TRIP_RUNTIME_JOURNAL_CONTRACT = "tradingagent.crypto.round_trip_server_journal.v1"
ROUND_TRIP_RUNTIME_FAILURE_CONTRACT = "tradingagent.crypto.round_trip_runtime_failure.v1"
ROUND_TRIP_SETTLED_BAR_DELAY = timedelta(minutes=5)
ROUND_TRIP_MAX_CYCLES_PER_INVOCATION = 24
ROUND_TRIP_TIMEOUT_SECONDS = 60.0
ROUND_TRIP_DATA_GAP_CONTRACT = "tradingagent.crypto.round_trip_data_gap.v1"
ROUND_TRIP_GAP_ELIGIBLE_REASONS = frozenset(
    {
        "crypto_5m_observation_after_cutoff",
        "crypto_5m_data_through_mismatch",
        "crypto_5m_window_incomplete",
        # A slot whose source data is permanently missing/stale is
        # point-in-time unrecoverable, not a transient contract error.  Gap it
        # instead of failing the whole backlog closed so one outage window
        # cannot pin the round-trip accumulator forever.
        "crypto_5m_metadata_not_ready",
        "crypto_5m_metadata_not_fresh",
        "crypto_5m_observation_stale",
        "crypto_5m_observation_stale_by_cutoff",
        # load_snapshot can succeed while verify_against / observation mapping
        # still reject the frozen cutoff.  Those codes used to escape core_cycle
        # unclassified (generic runtime_validation_failed) and pin the timer.
        "crypto_5m_metadata_lineage_incomplete",
        "crypto_5m_snapshot_incomplete",
        "crypto_5m_bar_order_or_gap_invalid",
        "crypto_5m_snapshot_window_binding_mismatch",
        "crypto_5m_snapshot_source_window_mismatch",
        "crypto_5m_snapshot_source_budget_or_cutoff_invalid",
        "crypto_5m_snapshot_source_freshness_invalid",
    }
)
_STABLE_REASON_CODE = re.compile(r"^[a-z][a-z0-9_]{1,95}$")
_TRANSPORT_UNAVAILABLE_REASON = "crypto_5m_transport_unavailable"
_HTTP_STATUS_REASON = "crypto_5m_http_status_invalid"
_AUTHENTICATION_REJECTED_REASON = "crypto_5m_authentication_rejected"
ROUND_TRIP_MAX_GAPS_PER_INVOCATION = 24
# The deployed service trial has a 300-second hard stop.  Reserve more than one
# full TD call for final anchor validation, serialization, and scheduling jitter.
ROUND_TRIP_INVOCATION_BUDGET_SECONDS = 120.0

_FAILURE_PROVENANCE = {
    "pre_network_validation": "runtime_pre_network_validation_failed",
    "checkpoint_recovery_selection": "runtime_checkpoint_recovery_selection_failed",
    "market_data_query": "runtime_market_data_query_failed",
    "core_cycle": "runtime_core_cycle_failed",
    "post_write_anchor_validation": "runtime_post_write_anchor_validation_failed",
}
_GENERIC_FAILURE_PHASE = "runtime_validation"
_GENERIC_FAILURE_REASON = "runtime_validation_failed"


class CryptoRoundTripRuntimeFailure(RuntimeError):
    """A finite public failure classification with no underlying error detail."""

    def __init__(
        self,
        *,
        phase: str,
        reason: str,
        detail: str | None = None,
    ) -> None:
        if _FAILURE_PROVENANCE.get(phase) != reason:
            raise ValueError("round_trip_runtime_failure_provenance_invalid")
        self.phase = phase
        self.reason = reason
        super().__init__(detail if detail is not None else reason)


class _InvocationBudgetExhausted(RuntimeError):
    """Private control flow for a safely deferred, not rejected, backlog slot."""


def _is_stable_reason_code(value: object) -> bool:
    """True when a public reason is a secret-free snake_case code."""

    return bool(isinstance(value, str) and _STABLE_REASON_CODE.fullmatch(value))


def _stable_public_detail(error: Exception) -> str | None:
    """Return an allowlisted reason code, never paths, tokens, or payloads."""

    if isinstance(error, CryptoFiveMinuteDataError):
        code = error.reason_code
        return code if _is_stable_reason_code(code) else None
    text = str(error)
    return text if _is_stable_reason_code(text) else None


def _exception_chain(error: BaseException) -> tuple[BaseException, ...]:
    """Walk cause/context/URLError.reason without interpolating payloads."""

    pending: list[BaseException] = [error]
    seen: set[int] = set()
    ordered: list[BaseException] = []
    while pending:
        current = pending.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        ordered.append(current)
        related: list[BaseException] = []
        if current.__cause__ is not None:
            related.append(current.__cause__)
        if current.__context__ is not None:
            related.append(current.__context__)
        reason = getattr(current, "reason", None)
        if isinstance(reason, BaseException):
            related.append(reason)
        pending.extend(candidate for candidate in related if id(candidate) not in seen)
    return tuple(ordered)


def _is_retryable_transport_error(error: BaseException) -> bool:
    """Timeout/connection faults are classified, retried next timer, never gapped."""

    chain = _exception_chain(error)
    if any(
        isinstance(
            current,
            (
                urllib.error.HTTPError,
                HTTPStatusError,
                TradingDatasAuthenticationError,
                _InvocationBudgetExhausted,
            ),
        )
        for current in chain
    ):
        return False
    return any(
        isinstance(
            current,
            (
                TimeoutError,
                ConnectionError,
                urllib.error.URLError,
                http.client.HTTPException,
            ),
        )
        for current in chain
    )


def _classified_failure(phase: str, error: Exception) -> CryptoRoundTripRuntimeFailure | None:
    """Map only declared stable errors to a secret-free public category."""

    if isinstance(error, _InvocationBudgetExhausted):
        return None
    if isinstance(error, CryptoFiveMinuteDataError) and phase in {
        "market_data_query",
        "core_cycle",
    }:
        # Observation mapping and verify_against run after load_snapshot, so
        # the same PIT codes used to escape as unclassified core_cycle errors.
        return CryptoRoundTripRuntimeFailure(
            phase="market_data_query",
            reason=_FAILURE_PROVENANCE["market_data_query"],
            detail=_stable_public_detail(error),
        )
    if phase in {"market_data_query", "core_cycle"}:
        chain = _exception_chain(error)
        if any(isinstance(item, TradingDatasAuthenticationError) for item in chain):
            return CryptoRoundTripRuntimeFailure(
                phase="market_data_query",
                reason=_FAILURE_PROVENANCE["market_data_query"],
                detail=_AUTHENTICATION_REJECTED_REASON,
            )
        if any(
            isinstance(item, (urllib.error.HTTPError, HTTPStatusError))
            for item in chain
        ):
            return CryptoRoundTripRuntimeFailure(
                phase="market_data_query",
                reason=_FAILURE_PROVENANCE["market_data_query"],
                detail=_HTTP_STATUS_REASON,
            )
        if _is_retryable_transport_error(error):
            return CryptoRoundTripRuntimeFailure(
                phase="market_data_query",
                reason=_FAILURE_PROVENANCE["market_data_query"],
                detail=_TRANSPORT_UNAVAILABLE_REASON,
            )
    if phase == "pre_network_validation" and (
        isinstance(error, (CryptoRoundTripEpochError, CryptoDelayedPaperRuntimeError))
        or str(error)
        in {
            "round_trip_epoch_manifest_path_invalid",
            "round_trip_token_file_path_invalid",
        }
    ):
        return CryptoRoundTripRuntimeFailure(
            phase=phase,
            reason=_FAILURE_PROVENANCE[phase],
        )
    if phase == "checkpoint_recovery_selection" and (
        isinstance(error, CryptoDelayedPaperLedgerError)
        or str(error)
        in {
            "round_trip_checkpoint_market_slot_invalid",
            "round_trip_clock_before_latest_observation",
        }
    ):
        return CryptoRoundTripRuntimeFailure(
            phase=phase,
            reason=_FAILURE_PROVENANCE[phase],
        )
    if phase == "core_cycle" and (
        isinstance(error, (CryptoDelayedPaperLedgerError, CryptoRoundTripError))
        or str(error)
        in {
            "round_trip_pending_recovery_not_completed",
            "round_trip_cycle_not_completed",
            "round_trip_snapshot_type_invalid",
            "round_trip_prepared_fixture_invalid",
            "round_trip_fill_capacities_invalid",
        }
    ):
        return CryptoRoundTripRuntimeFailure(
            phase=phase,
            reason=_FAILURE_PROVENANCE[phase],
            detail=_stable_public_detail(error),
        )
    if phase == "post_write_anchor_validation" and (
        isinstance(error, CryptoRoundTripEpochError)
        or str(error) == "round_trip_epoch_identity_changed"
    ):
        return CryptoRoundTripRuntimeFailure(
            phase=phase,
            reason=_FAILURE_PROVENANCE[phase],
        )
    return None


def _run_failure_stage(phase: str, callback: Callable[[], Any]) -> Any:
    """Run a stage without allowing raw exception details into the journal."""

    try:
        return callback()
    except CryptoRoundTripRuntimeFailure:
        raise
    except _InvocationBudgetExhausted:
        raise
    except Exception as exc:
        classified = _classified_failure(phase, exc)
        if classified is not None:
            raise classified from None
        raise


class _FailureClassifyingPort:
    """Tag only the market-data boundary while retaining the existing port API."""

    def __init__(self, port: _LazyCryptoFiveMinutePort) -> None:
        self._port = port

    def load_snapshot(self, **kwargs: Any) -> Any:
        return _run_failure_stage(
            "market_data_query",
            lambda: self._port.load_snapshot(**kwargs),
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._port, name)


def crypto_round_trip_window_request(now: datetime) -> CryptoFiveMinuteWindowRequest:
    """Observe one fully settled bar without relaxing the PIT cutoff.

    The Crypto collector and this paper runtime are deliberately independent.
    Consuming the prior closed bar gives the collector a full five-minute
    interval to publish its receipt. The fixed cutoff remains bound to that
    settled window, so its receipt watermark cannot include the next bar.
    """

    current = crypto_runtime_window_request(now)
    window_end = current.window_end - ROUND_TRIP_SETTLED_BAR_DELAY
    return CryptoFiveMinuteWindowRequest(
        window_end=window_end,
        observation_cutoff=window_end + timedelta(seconds=55),
    )


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _round_trip_market_slot(request: CryptoFiveMinuteWindowRequest) -> datetime:
    """Return the first closed bar represented by one round-trip request."""

    return request.window_end - timedelta(minutes=5)


def _round_trip_request_for_market_slot(
    market_slot: datetime,
) -> CryptoFiveMinuteWindowRequest:
    """Rebuild the fixed PIT request for one previously eligible closed bar."""

    window_end = market_slot + timedelta(minutes=5)
    return CryptoFiveMinuteWindowRequest(
        window_end=window_end,
        observation_cutoff=window_end + timedelta(seconds=55),
    )


def _checkpoint_market_slot(value: object) -> datetime:
    if not isinstance(value, str):
        raise RuntimeError("round_trip_checkpoint_market_slot_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError("round_trip_checkpoint_market_slot_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise RuntimeError("round_trip_checkpoint_market_slot_invalid")
    return parsed.astimezone(timezone.utc)


def _round_trip_gap_eligible(failure: CryptoRoundTripRuntimeFailure) -> bool:
    """Only PIT-unrecoverable market-data failures on historical slots gap."""

    return bool(
        isinstance(failure, CryptoRoundTripRuntimeFailure)
        and failure.phase == "market_data_query"
        and failure.reason == _FAILURE_PROVENANCE["market_data_query"]
        and str(failure) in ROUND_TRIP_GAP_ELIGIBLE_REASONS
    )


def _round_trip_data_gap_event(
    *,
    prior_market_slot: datetime,
    reason_code: str,
    recorded_at: datetime,
) -> dict[str, Any]:
    """Freeze one round-trip outage gap covering the single skipped slot."""

    skipped_from = prior_market_slot + timedelta(minutes=5)
    skipped_to = skipped_from
    recovery_market_slot = skipped_to + timedelta(minutes=5)
    rejected_window_end = skipped_from + timedelta(minutes=5)
    if reason_code not in ROUND_TRIP_GAP_ELIGIBLE_REASONS:
        raise CryptoRoundTripRuntimeFailure(
            phase="checkpoint_recovery_selection",
            reason=_FAILURE_PROVENANCE["checkpoint_recovery_selection"],
            detail="round_trip_gap_reason_not_eligible",
        )
    event_id_material = json.dumps(
        {
            "gap_contract": ROUND_TRIP_DATA_GAP_CONTRACT,
            "event_type": "data_gap",
            "prior_market_slot": _iso_utc(prior_market_slot),
            "recovery_market_slot": _iso_utc(recovery_market_slot),
            "reason_code": reason_code,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    payload = {
        "contract": DECISION_LEDGER_CONTRACT,
        "gap_contract": ROUND_TRIP_DATA_GAP_CONTRACT,
        "event_type": "data_gap",
        "market": "crypto",
        "market_session": "24x7",
        "prior_market_slot": _iso_utc(prior_market_slot),
        "skipped_from": _iso_utc(skipped_from),
        "skipped_to": _iso_utc(skipped_to),
        "recovery_market_slot": _iso_utc(recovery_market_slot),
        "rejected_target_window_end": _iso_utc(rejected_window_end),
        "rejected_target_observation_cutoff": _iso_utc(
            rejected_window_end + timedelta(seconds=55)
        ),
        "reason_code": reason_code,
        "candidate_generated": False,
        "order_generated": False,
        "fill_generated": False,
        "capital_effect": "none_preserved_outage_recovery",
        "recorded_at": _iso_utc(recorded_at),
        "event_id": hashlib.sha256(event_id_material.encode("utf-8")).hexdigest(),
    }
    payload.update(_non_authority_fields())
    return payload


def _round_trip_latest_gap_slot(
    store: CryptoDelayedPaperObservationStore,
) -> datetime | None:
    """Return the newest round-trip gap recovery slot, if any."""

    latest: datetime | None = None
    for gap in store.data_gap_events():
        if gap.get("gap_contract") != ROUND_TRIP_DATA_GAP_CONTRACT:
            continue
        recovery = _checkpoint_market_slot(gap.get("recovery_market_slot"))
        if latest is None or recovery > latest:
            latest = recovery
    return latest


def _invocation_budget_exceeded(started_at: float, budget_seconds: float) -> bool:
    """True when the invocation has consumed its wall-clock cycle budget."""
    return time.monotonic() - started_at >= budget_seconds


def _remaining_invocation_seconds(started_at: float, budget_seconds: float) -> float:
    """Return the positive wall-clock budget available to the next wire call."""

    remaining = budget_seconds - (time.monotonic() - started_at)
    if remaining <= 0:
        raise _InvocationBudgetExhausted("round_trip_invocation_budget_exhausted")
    return remaining


def _deadline_bound_transport_factory(
    transport_factory: Callable[..., HTTPTransport],
    *,
    started_at: float,
    budget_seconds: float,
) -> Callable[..., HTTPTransport]:
    """Clamp every existing TD wire call to the absolute invocation deadline."""

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
                        "round_trip_invocation_budget_exhausted"
                    ) from None
                raise
            if time.monotonic() - started_at >= budget_seconds:
                raise _InvocationBudgetExhausted(
                    "round_trip_invocation_budget_exhausted"
                )
            return response

        return send

    return build


def round_trip_receipt_exit_code(receipt: Mapping[str, Any]) -> int:
    """Map a bounded backlog batch that advanced the ledger to success."""
    # A data-incomplete window is an explicit, checksum-bound no-capital
    # progress event.  It is observable and must not make the simulation
    # timer look dead, while every integrity/configuration failure still uses
    # the existing fail-closed mapping below.
    if (
        receipt.get("data_incomplete") is True
        and int(receipt.get("data_incomplete_count") or 0) > 0
    ):
        return 0
    if (
        receipt.get("status") == "backlog_pending"
        and receipt.get("backlog_remaining") is True
        and (
            int(receipt.get("processed_cycle_count") or 0) > 0
            or receipt.get("budget_deferred") is True
        )
    ):
        return 0
    return crypto_runtime_receipt_exit_code(receipt)


def round_trip_runtime_journal_summary(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Return the bounded systemd-journal projection of one runtime receipt.

    The complete nested core result remains in the Crypto audit ledger.  The
    process journal contains only the fields needed to identify a cycle, its
    data contract, and its simulation-only boundary.
    """

    core_result = receipt.get("core_result")
    if not isinstance(core_result, Mapping):
        raise RuntimeError("round_trip_runtime_core_result_invalid")
    return {
        "contract": ROUND_TRIP_RUNTIME_JOURNAL_CONTRACT,
        "runtime_contract": receipt.get("contract"),
        "status": receipt.get("status"),
        "market_slot": core_result.get("market_slot"),
        "recovered_pending": core_result.get("recovered_pending"),
        "idempotent_replay": core_result.get("idempotent_replay"),
        "replay_mode": core_result.get("replay_mode"),
        "recovery_mode": receipt.get("recovery_mode"),
        "requested_window_consumed": receipt.get("requested_window_consumed"),
        "processed_cycle_count": receipt.get("processed_cycle_count"),
        "backlog_recovery_cycle_count": receipt.get("backlog_recovery_cycle_count"),
        "backlog_gap_cycle_count": receipt.get("backlog_gap_cycle_count"),
        "data_incomplete": receipt.get("data_incomplete"),
        "data_incomplete_count": receipt.get("data_incomplete_count"),
        "backlog_remaining": receipt.get("backlog_remaining"),
        "requested_window_end": receipt.get("requested_window_end"),
        "requested_observation_cutoff": receipt.get("requested_observation_cutoff"),
        "settled_bar_delay_seconds": receipt.get("settled_bar_delay_seconds"),
        "runtime_manifest_sha256": receipt.get("runtime_manifest_sha256"),
        "fresh_query_catalog_version": receipt.get("fresh_query_catalog_version"),
        "fresh_query_profile_sha256": receipt.get("fresh_query_profile_sha256"),
        "epoch_id": receipt.get("epoch_id"),
        "epoch_generation": receipt.get("epoch_generation"),
        "market_data_access_attempt_count": receipt.get(
            "market_data_access_attempt_count"
        ),
        "market_data_network_used": receipt.get("market_data_network_used"),
        "learning_mode": receipt.get("learning_mode"),
        "learning_authority": receipt.get("learning_authority"),
        "learning_invoked": receipt.get("learning_invoked"),
        "real_trading_enabled": receipt.get("real_trading_enabled"),
        "execution_eligible": receipt.get("execution_eligible"),
        "execution_authority": receipt.get("execution_authority"),
        "production_eligible": receipt.get("production_eligible"),
        "testnet_used": receipt.get("testnet_used"),
        "live_broker_used": receipt.get("live_broker_used"),
        "model_network_used": receipt.get("model_network_used"),
        "promotion_authorized": receipt.get("promotion_authorized"),
        "automatic_promotion_enabled": receipt.get(
            "automatic_promotion_enabled"
        ),
        "automatic_risk_expansion_enabled": receipt.get(
            "automatic_risk_expansion_enabled"
        ),
        "outbox_id": receipt.get("outbox_id"),
        "capital_commit_id": receipt.get("capital_commit_id"),
    }


def round_trip_runtime_validation_failure_summary(
    now: datetime,
    failure: CryptoRoundTripRuntimeFailure | None = None,
) -> dict[str, str]:
    """Describe a failed runtime cycle without exposing exception details."""

    request = crypto_round_trip_window_request(now)
    payload = {
        "contract": ROUND_TRIP_RUNTIME_FAILURE_CONTRACT,
        "status": "failed_closed",
        "failure_phase": (
            failure.phase if failure is not None else _GENERIC_FAILURE_PHASE
        ),
        "failure_reason": (
            failure.reason if failure is not None else _GENERIC_FAILURE_REASON
        ),
        "target_window_end": _iso_utc(request.window_end),
    }
    if failure is not None:
        detail = str(failure)
        if (
            detail
            and detail != failure.reason
            and _is_stable_reason_code(detail)
        ):
            payload["failure_code"] = detail
    return payload


def run_crypto_delayed_paper_round_trip_server_once(
    *,
    epoch_manifest: Path | str,
    runtime_manifest: Path | str,
    token_file: Path | str,
    now: datetime,
    transport_factory: Callable[..., HTTPTransport] = build_runtime_transport,
    invocation_budget_seconds: float | None = None,
) -> dict[str, Any]:
    """Run exactly one new/pending closed-bar cycle in the isolated epoch."""

    budget_seconds = (
        ROUND_TRIP_INVOCATION_BUDGET_SECONDS
        if invocation_budget_seconds is None
        else invocation_budget_seconds
    )
    if (
        isinstance(budget_seconds, bool)
        or not isinstance(budget_seconds, (int, float))
        or budget_seconds <= 0
    ):
        raise ValueError("round_trip_invocation_budget_invalid")
    invocation_started_at = time.monotonic()
    bounded_transport_factory = _deadline_bound_transport_factory(
        transport_factory,
        started_at=invocation_started_at,
        budget_seconds=float(budget_seconds),
    )

    def prepare_pre_network() -> tuple[Any, Any, bytes, Any, Any, Any]:
        _assert_simulation_only()
        manifest_path = Path(epoch_manifest)
        if (
            manifest_path != ROUND_TRIP_EPOCH_MANIFEST_PATH
            and manifest_path.parent != ROUND_TRIP_EPOCH_MANIFEST_DIRECTORY
        ):
            raise RuntimeError("round_trip_epoch_manifest_path_invalid")
        if Path(token_file) != RUNTIME_TOKEN_FILE:
            raise RuntimeError("round_trip_token_file_path_invalid")
        context = load_round_trip_epoch_manifest(manifest_path)
        prepared = prepare_round_trip_epoch_candidate(context)
        identity_before = prepared.identity_path.read_bytes()
        manifest = load_crypto_delayed_paper_runtime_manifest(runtime_manifest)
        request = crypto_round_trip_window_request(now)
        port = _FailureClassifyingPort(
            _LazyCryptoFiveMinutePort(
                manifest=manifest,
                token_file=RUNTIME_TOKEN_FILE,
                transport_factory=bounded_transport_factory,
                timeout_seconds=ROUND_TRIP_TIMEOUT_SECONDS,
            )
        )
        return context, prepared, identity_before, manifest, request, port

    (
        context,
        prepared,
        identity_before,
        manifest,
        request,
        port,
    ) = _run_failure_stage("pre_network_validation", prepare_pre_network)

    def load_checkpoint() -> tuple[Any, Any, Any, Any]:
        store = CryptoDelayedPaperObservationStore(prepared.output_root)
        checkpoint = store.runtime_checkpoint()
        requested_market_slot = _round_trip_market_slot(request)
        pending = checkpoint.get("pending")
        latest_market_slot = (
            _checkpoint_market_slot(checkpoint["latest_market_slot"])
            if checkpoint.get("latest_market_slot") is not None
            else None
        )
        latest_gap_slot = _round_trip_latest_gap_slot(store)
        if latest_gap_slot is not None and (
            latest_market_slot is None or latest_gap_slot > latest_market_slot
        ):
            latest_market_slot = latest_gap_slot
        return store, requested_market_slot, pending, latest_market_slot

    store, requested_market_slot, pending, latest_market_slot = _run_failure_stage(
        "checkpoint_recovery_selection", load_checkpoint
    )
    cycle_results: list[dict[str, Any]] = []
    gap_count = 0
    budget_deferred = False
    if pending is not None:
        pending_slot = _run_failure_stage(
            "checkpoint_recovery_selection",
            lambda: _checkpoint_market_slot(pending.get("market_slot")),
        )
        try:
            pending_result = _run_failure_stage(
                "core_cycle",
                lambda: run_crypto_delayed_paper_round_trip_once(
                    port=port,
                    profile=manifest.profile,
                    request=_round_trip_request_for_market_slot(pending_slot),
                    output_root=prepared.output_root,
                ),
            )
        except _InvocationBudgetExhausted:
            pending_result = None
            budget_deferred = True
        if pending_result is not None:
            if pending_result.get("status") != "completed":
                raise CryptoRoundTripRuntimeFailure(
                    phase="core_cycle",
                    reason=_FAILURE_PROVENANCE["core_cycle"],
                )
            cycle_results.append(
                {
                    "cycle_kind": "pending_recovery",
                    "target_window_end": _iso_utc(pending_slot + timedelta(minutes=5)),
                    "result": pending_result,
                }
            )
            latest_market_slot = pending_slot

    while len(cycle_results) < ROUND_TRIP_MAX_CYCLES_PER_INVOCATION:
        if budget_deferred:
            break
        if _invocation_budget_exceeded(invocation_started_at, budget_seconds):
            budget_deferred = True
            break
        def select_next_cycle() -> tuple[CryptoFiveMinuteWindowRequest, str] | None:
            if latest_market_slot is None:
                return request, "fresh_query"
            if latest_market_slot < requested_market_slot:
                return (
                    _round_trip_request_for_market_slot(
                        latest_market_slot + timedelta(minutes=5)
                    ),
                    "backlog_recovery",
                )
            if latest_market_slot == requested_market_slot:
                return None
            raise RuntimeError("round_trip_clock_before_latest_observation")

        selected = _run_failure_stage(
            "checkpoint_recovery_selection", select_next_cycle
        )
        if selected is None:
            break
        target_request, cycle_kind = selected
        try:
            result = _run_failure_stage(
                "core_cycle",
                lambda: run_crypto_delayed_paper_round_trip_once(
                    port=port,
                    profile=manifest.profile,
                    request=target_request,
                    output_root=prepared.output_root,
                ),
            )
        except _InvocationBudgetExhausted:
            budget_deferred = True
            break
        except CryptoRoundTripRuntimeFailure as failure:
            # A wire timeout at the absolute invocation deadline is a bounded
            # defer, not evidence that an immutable historical slot is bad.
            if time.monotonic() - invocation_started_at >= budget_seconds:
                budget_deferred = True
                break
            if (
                cycle_kind in {"backlog_recovery", "fresh_query"}
                and _round_trip_gap_eligible(failure)
            ):
                if gap_count >= ROUND_TRIP_MAX_GAPS_PER_INVOCATION:
                    break
                target_market_slot = _round_trip_market_slot(target_request)
                # With an empty accumulator there is no prior completed slot
                # to cite.  Use the immediately preceding aligned slot as a
                # deterministic boundary; the event's skipped/recovery slots
                # remain the authoritative range and no capital state moves.
                prior_market_slot = (
                    latest_market_slot
                    if latest_market_slot is not None
                    else target_market_slot - timedelta(minutes=5)
                )
                store.append_event(
                    _round_trip_data_gap_event(
                        prior_market_slot=prior_market_slot,
                        reason_code=str(failure),
                        recorded_at=now,
                    )
                )
                gap_count += 1
                latest_market_slot = _round_trip_market_slot(target_request)
                cycle_results.append(
                    {
                        "cycle_kind": (
                            "backlog_gap"
                            if cycle_kind == "backlog_recovery"
                            else "fresh_data_incomplete"
                        ),
                        "target_window_end": _iso_utc(target_request.window_end),
                        "gap_reason": str(failure),
                        "data_incomplete": True,
                    }
                )
                continue
            raise
        if result.get("status") != "completed":
            raise CryptoRoundTripRuntimeFailure(
                phase="core_cycle",
                reason=_FAILURE_PROVENANCE["core_cycle"],
            )
        cycle_results.append(
            {
                "cycle_kind": cycle_kind,
                "target_window_end": _iso_utc(target_request.window_end),
                "result": result,
            }
        )
        latest_market_slot = _round_trip_market_slot(target_request)

    if not cycle_results:
        # A completed slot is immutable. Do not re-query a mutable current
        # view and risk accepting a different payload for the same slot.
        result: Mapping[str, Any] = {
            "contract": "tradingagent.crypto.delayed_paper_round_trip_runner.v1",
            "status": "completed",
            "market": "crypto",
            "market_slot": _iso_utc(requested_market_slot),
            "recovered_pending": False,
            "idempotent_replay": True,
            "replay_mode": "completed_slot_without_fresh_query",
        }
    else:
        last = cycle_results[-1]
        if "result" in last:
            result = last["result"]
        else:
            result = {
                "contract": "tradingagent.crypto.delayed_paper_round_trip_runner.v1",
                "status": "completed",
                "market": "crypto",
                "market_slot": (
                    _iso_utc(latest_market_slot)
                    if latest_market_slot is not None
                    else _iso_utc(requested_market_slot)
                ),
                "recovered_pending": False,
                "idempotent_replay": False,
                "replay_mode": "backlog_gap",
            }
    backlog_remaining = bool(
        latest_market_slot is not None and latest_market_slot < requested_market_slot
    )
    data_incomplete = gap_count > 0
    recovery_mode = (
        "pending_recovery"
        if any(item["cycle_kind"] == "pending_recovery" for item in cycle_results)
        else (
            "backlog_recovery"
            if any(item["cycle_kind"] == "backlog_recovery" for item in cycle_results)
            else "none"
        )
    )
    # Re-read both anchors after the write: neither a changed g3 manifest nor a
    # changed g2 archive may be hidden by a successful local capital cycle.
    def validate_post_write_anchor() -> None:
        prepared_after = prepare_round_trip_epoch_candidate(context)
        if prepared_after.identity_path.read_bytes() != identity_before:
            raise RuntimeError("round_trip_epoch_identity_changed")

    _run_failure_stage("post_write_anchor_validation", validate_post_write_anchor)
    return {
        "contract": ROUND_TRIP_RUNTIME_CONTRACT,
        "status": (
            "backlog_pending"
            if backlog_remaining
            else ("data_incomplete" if data_incomplete else result.get("status"))
        ),
        "core_result": result,
        "requested_window_end": _iso_utc(request.window_end),
        "requested_observation_cutoff": _iso_utc(request.observation_cutoff),
        "requested_window_consumed": bool(
            latest_market_slot is not None and latest_market_slot >= requested_market_slot
        ),
        "processed_cycle_count": len(cycle_results),
        "backlog_recovery_cycle_count": sum(
            item["cycle_kind"] == "backlog_recovery" for item in cycle_results
        ),
        "backlog_gap_cycle_count": sum(
            item["cycle_kind"] == "backlog_gap" for item in cycle_results
        ),
        "data_incomplete": data_incomplete,
        "data_incomplete_count": gap_count,
        "backlog_remaining": backlog_remaining,
        "budget_deferred": budget_deferred,
        "recovery_mode": recovery_mode,
        "cycle_results": cycle_results,
        "settled_bar_delay_seconds": int(ROUND_TRIP_SETTLED_BAR_DELAY.total_seconds()),
        "invocation_budget_seconds": float(budget_seconds),
        "runtime_manifest_sha256": manifest.sha256,
        "fresh_query_catalog_version": manifest.catalog_version,
        "fresh_query_profile_sha256": manifest.profile.sha256,
        "epoch_id": context.epoch_id,
        "epoch_generation": context.epoch_generation,
        "capital_authority_id": "crypto-round-trip-capital-v1",
        "capital_generation": 2,
        "aggregate_with_prior_generations": False,
        "market_data_transport": "loopback_tradingdatas_v1",
        "market_data_access_attempt_count": port.load_snapshot_calls,
        "market_data_network_used": port.transport_constructed_count > 0,
        "learning_mode": "detached_offline_worker",
        "learning_authority": False,
        "learning_invoked": False,
        "real_trading_enabled": False,
        "execution_eligible": False,
        "execution_authority": False,
        "production_eligible": False,
        "testnet_used": False,
        "live_broker_used": False,
        "model_network_used": False,
        "promotion_authorized": False,
        "automatic_promotion_enabled": False,
        "automatic_risk_expansion_enabled": False,
        "outbox_id": None,
        "capital_commit_id": None,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run one Crypto round-trip simulated cycle"
    )
    parser.add_argument("--epoch-manifest", type=Path, required=True)
    parser.add_argument("--runtime-manifest", type=Path, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    args = parser.parse_args(argv)
    now = datetime.now(tz=timezone.utc)
    try:
        receipt = run_crypto_delayed_paper_round_trip_server_once(
            epoch_manifest=args.epoch_manifest,
            runtime_manifest=args.runtime_manifest,
            token_file=args.token_file,
            now=now,
        )
        code = round_trip_receipt_exit_code(receipt)
    except CryptoRoundTripRuntimeFailure as exc:
        failure_summary = round_trip_runtime_validation_failure_summary(now, exc)
        print(
            json.dumps(
                failure_summary,
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        print("crypto round-trip runtime failed closed", file=sys.stderr)
        return 2
    except Exception:
        failure_summary = round_trip_runtime_validation_failure_summary(now)
        print(
            json.dumps(
                failure_summary,
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        print("crypto round-trip runtime failed closed", file=sys.stderr)
        return 2
    try:
        rendered = json.dumps(
            round_trip_runtime_journal_summary(receipt),
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except Exception:
        print("crypto round-trip runtime failed closed", file=sys.stderr)
        return 2
    print(rendered)
    if code:
        print("crypto round-trip runtime failed closed", file=sys.stderr)
        return code
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ROUND_TRIP_RUNTIME_CONTRACT",
    "ROUND_TRIP_RUNTIME_FAILURE_CONTRACT",
    "ROUND_TRIP_RUNTIME_JOURNAL_CONTRACT",
    "ROUND_TRIP_SETTLED_BAR_DELAY",
    "ROUND_TRIP_DATA_GAP_CONTRACT",
    "ROUND_TRIP_GAP_ELIGIBLE_REASONS",
    "ROUND_TRIP_MAX_GAPS_PER_INVOCATION",
    "ROUND_TRIP_INVOCATION_BUDGET_SECONDS",
    "crypto_round_trip_window_request",
    "main",
    "round_trip_runtime_validation_failure_summary",
    "round_trip_runtime_journal_summary",
    "round_trip_receipt_exit_code",
    "run_crypto_delayed_paper_round_trip_server_once",
    "_invocation_budget_exceeded",
    "_round_trip_data_gap_event",
    "_round_trip_gap_eligible",
    "_round_trip_latest_gap_slot",
    "_classified_failure",
    "_is_retryable_transport_error",
]
