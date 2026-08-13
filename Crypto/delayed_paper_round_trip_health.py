"""Read-only health and sample-quality snapshot for the active round-trip epoch.

This module is deliberately separate from the five-minute accumulator.  It
never queries TradingDatas, never creates an epoch/root/lock, and never writes
learning, capital, order, completion, or decision evidence.  A failed health
read is therefore an alert only; it cannot repair, restart, or alter a run.

``failure_count`` is the count of durable ``data_reject`` facts in the same
checksum-verified runtime ledger read for this snapshot.  It deliberately does
not represent journal-only runtime failures, ``data_gap`` audit events,
``risk_reject`` decisions, or ordinary ``decision`` events.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from typing import Any, Mapping

from Crypto.delayed_paper_ledger import (
    CryptoDelayedPaperLedgerError,
    CryptoDelayedPaperObservationStore,
    _market_slot,
    _read_json,
    _sha256,
)
from Crypto.delayed_paper_round_trip_epoch import (
    ROUND_TRIP_EPOCH_MANIFEST_DIRECTORY,
    CryptoRoundTripEpochError,
    CryptoRoundTripEpochContext,
    load_round_trip_epoch_manifest,
    prepare_round_trip_epoch_candidate,
)
from Crypto.fixture_sim.contracts import _assert_simulation_only
from Crypto.round_trip_capital import (
    ROUND_TRIP_CAPITAL_POLICY,
    CryptoRoundTripError,
    RoundTripCapitalLedger,
)


ROUND_TRIP_HEALTH_CONTRACT = "tradingagent.crypto.round_trip_health.v1"
MAX_COMPLETION_LAG = timedelta(minutes=30)


class CryptoRoundTripHealthError(RuntimeError):
    """Stable fail-closed error for non-authoritative health evidence."""


def _failure_count(ledger_rows: list[Mapping[str, Any]]) -> int:
    """Legacy pure helper retained for the failure-count definition test."""

    return sum(1 for row in ledger_rows if row.get("event_type") == "data_reject")


def _non_authority_fields() -> dict[str, Any]:
    return {
        "authority": "none",
        "read_only": True,
        "execution_eligible": False,
        "execution_authority": False,
        "durable_execution_receipt": False,
        "production_eligible": False,
        "real_trading_enabled": False,
        "network_used": False,
        "model_network_used": False,
        "testnet_used": False,
        "live_broker_used": False,
        "promotion_authorized": False,
        "automatic_promotion_enabled": False,
        "automatic_risk_expansion_enabled": False,
        "outbox_id": None,
        "capital_commit_id": None,
    }


def _utc_now(value: datetime | None) -> datetime:
    now = value or datetime.now(tz=timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise CryptoRoundTripHealthError("round_trip_health_now_timezone_invalid")
    return now.astimezone(timezone.utc)


def _existing_runtime_root(root: Path) -> None:
    """Reject a root that would cause store construction to write."""

    required = (
        root,
        root / "delayed_paper",
        root / "delayed_paper" / "observations",
        root / "delayed_paper" / "completions",
        root / "delayed_paper" / ".lock",
        root / "round_trip_capital",
        root / "round_trip_capital" / ".lock",
    )
    for path in required:
        if not path.exists() or path.is_symlink():
            raise CryptoRoundTripHealthError("round_trip_health_root_incomplete")


def _existing_epoch_root(context: CryptoRoundTripEpochContext) -> None:
    """Reject a root that would cause prepare/store construction to write."""

    _existing_runtime_root(context.output_root)
    if not context.identity_path.exists() or context.identity_path.is_symlink():
        raise CryptoRoundTripHealthError("round_trip_health_root_incomplete")


def _receipt_counts(orders: Mapping[str, Any]) -> dict[str, int]:
    counts = {
        "buy": 0,
        "sell": 0,
        "fixture_simulated": 0,
        "fixture_partially_simulated": 0,
        "fixture_rejected": 0,
    }
    for order in orders.values():
        if not isinstance(order, Mapping):
            raise CryptoRoundTripHealthError("round_trip_health_orders_invalid")
        side = order.get("side")
        status = order.get("status")
        if side not in {"buy", "sell"} or status not in {
            "fixture_simulated",
            "fixture_partially_simulated",
            "fixture_rejected",
        }:
            raise CryptoRoundTripHealthError("round_trip_health_orders_invalid")
        counts[str(side)] += 1
        counts[str(status)] += 1
    return counts


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _state_snapshot_fast(store: CryptoDelayedPaperObservationStore) -> dict[str, Any]:
    raw = _read_json(store.observation_state_path)
    material = dict(raw)
    if raw.get("contract") != "tradingagent.crypto.delayed_paper_state.v1":
        raise CryptoRoundTripHealthError("round_trip_health_source_invalid")
    if material.pop("state_sha256", None) != _sha256(material):
        raise CryptoRoundTripHealthError("round_trip_health_source_invalid")
    counts = (raw.get("observation_count"), raw.get("completion_count"))
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in counts):
        raise CryptoRoundTripHealthError("round_trip_health_source_invalid")
    if counts[1] > counts[0]:
        raise CryptoRoundTripHealthError("round_trip_health_source_invalid")
    return raw


def _pending_writer_overlap_health(
    *, root: Path, state: Mapping[str, Any], checked_at: datetime
) -> dict[str, Any]:
    """Return the last validated state without reading any runtime history."""

    observed_at = checked_at.isoformat().replace("+00:00", "Z")
    observation_count = state.get("observation_count")
    completion_count = state.get("completion_count")
    if (
        isinstance(observation_count, bool)
        or not isinstance(observation_count, int)
        or isinstance(completion_count, bool)
        or not isinstance(completion_count, int)
        or completion_count > observation_count
    ):
        raise CryptoRoundTripHealthError("round_trip_health_source_invalid")
    latest_completed_market_slot = (
        state.get("latest_market_slot")
        if observation_count == completion_count
        else None
    )
    return {
        "contract": ROUND_TRIP_HEALTH_CONTRACT,
        "status": "pending",
        "health_outcome": "pending_writer_overlap",
        "authoritative": False,
        "non_authoritative_reason": "writer_lock_busy",
        "market": "crypto",
        "market_session": "24x7",
        "epoch_output_root": str(root),
        "observed_at": observed_at,
        "effective_release": "unavailable",
        "core": {
            "observed_at": observed_at,
            "observation_count": observation_count,
            "completion_count": completion_count,
            "pending": True,
            "latest_observation_id": state.get("latest_observation_id"),
            "latest_market_slot": state.get("latest_market_slot"),
            "latest_completed_market_slot": latest_completed_market_slot,
            "latest_completion_sha256": state.get("latest_completion_sha256"),
        },
        "failure_count": "unavailable",
        "freshness": {"checked_at": observed_at, "state": "pending"},
        "sample_kpis": {
            "usable_completed_observations": completion_count,
            "verified_decision_events": None,
            "expected_decision_events": completion_count * 2,
            "capital_cycle_events": None,
            "symbol_decisions_per_observation": 2,
        },
        "capital": {"status": "unavailable_due_to_pending_writer"},
        **_non_authority_fields(),
    }


def build_crypto_delayed_paper_round_trip_health(
    *,
    output_root: Path | str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build a checksum-bound health/KPI snapshot without modifying ``output_root``."""

    _assert_simulation_only()
    root = Path(output_root)
    if not root.exists() or root.is_symlink() or not root.is_dir():
        raise CryptoRoundTripHealthError("round_trip_health_root_incomplete")
    _existing_runtime_root(root)
    checked_at = _utc_now(now)
    try:
        store = CryptoDelayedPaperObservationStore(root)
        try:
            checkpoint = store.runtime_checkpoint_read_only(
                nonblocking=True, include_ledger=True
            )
        except CryptoDelayedPaperLedgerError as exc:
            if str(exc) != "delayed_paper_readonly_lock_busy":
                raise
            state = _state_snapshot_fast(store)
            return _pending_writer_overlap_health(
                root=root,
                state=state,
                checked_at=checked_at,
            )
        state = store._observation_state_read_only()
        observation_id = state.get("latest_observation_id")
        if (
            not isinstance(observation_id, str)
            or checkpoint.get("pending") is not None
            or checkpoint.get("observation_count") != checkpoint.get("completion_count")
            or int(checkpoint.get("completion_count") or 0) <= 0
        ):
            raise CryptoRoundTripHealthError("round_trip_health_core_incomplete")
        observation = _read_json(store._observation_path(observation_id))
        store._verify_observation(observation)
        completion = _read_json(store._completion_path(observation_id))
        store._verify_completion(completion, observation=observation)
        ledger_state = checkpoint.get("ledger_state")
        if not isinstance(ledger_state, Mapping):
            raise CryptoRoundTripHealthError(
                "round_trip_health_ledger_aggregate_missing"
            )
        expected_decisions = int(checkpoint["completion_count"]) * 2
        decision_count = ledger_state.get("decision_count")
        if decision_count != expected_decisions:
            raise CryptoRoundTripHealthError("round_trip_health_decision_count_invalid")
        failure_count = ledger_state.get("failure_count")
        capital = RoundTripCapitalLedger(root / "round_trip_capital").state_read_only()
    except CryptoRoundTripHealthError:
        raise
    except CryptoDelayedPaperLedgerError as exc:
        reason = str(exc)
        if reason.endswith("aggregate_missing"):
            raise CryptoRoundTripHealthError(
                "round_trip_health_ledger_aggregate_missing"
            ) from exc
        if reason.endswith("aggregate_invalid"):
            raise CryptoRoundTripHealthError(
                "round_trip_health_ledger_aggregate_invalid"
            ) from exc
        if reason.endswith("current_file_invalid"):
            raise CryptoRoundTripHealthError(
                "round_trip_health_ledger_current_file_invalid"
            ) from exc
        raise CryptoRoundTripHealthError("round_trip_health_source_invalid") from exc
    except (
        CryptoRoundTripError,
        OSError,
        ValueError,
    ) as exc:
        raise CryptoRoundTripHealthError("round_trip_health_source_invalid") from exc

    orders = capital.get("orders")
    if (
        capital.get("authority_id") != ROUND_TRIP_CAPITAL_POLICY.authority_id
        or capital.get("account_id") != ROUND_TRIP_CAPITAL_POLICY.account_id
        or capital.get("generation") != ROUND_TRIP_CAPITAL_POLICY.generation
        or capital.get("initial_cash")
        != format(ROUND_TRIP_CAPITAL_POLICY.initial_cash, "f")
        or capital.get("aggregate_with_prior_generations") is not False
        or capital.get("balanced") is not True
        or not isinstance(orders, Mapping)
        or any(
            capital.get(key) != expected
            for key, expected in _non_authority_fields().items()
            if key in capital
        )
    ):
        raise CryptoRoundTripHealthError("round_trip_health_capital_invalid")
    latest_slot = _market_slot(observation.get("market_slot"))
    lag_seconds = int((checked_at - latest_slot).total_seconds())
    if lag_seconds < 0:
        raise CryptoRoundTripHealthError("round_trip_health_market_slot_future")
    freshness_state = (
        "fresh" if lag_seconds <= int(MAX_COMPLETION_LAG.total_seconds()) else "stale"
    )

    return {
        "contract": ROUND_TRIP_HEALTH_CONTRACT,
        "status": "healthy" if freshness_state == "fresh" else "stale",
        "market": "crypto",
        "market_session": "24x7",
        "epoch_output_root": str(root),
        "core": {
            "observation_count": checkpoint["observation_count"],
            "completion_count": checkpoint["completion_count"],
            "pending": False,
            "latest_observation_id": observation_id,
            "latest_market_slot": observation.get("market_slot"),
            "latest_completion_sha256": completion.get("completion_sha256"),
        },
        "failure_count": failure_count,
        "freshness": {
            "checked_at": checked_at.isoformat().replace("+00:00", "Z"),
            "completion_lag_seconds": lag_seconds,
            "maximum_completion_lag_seconds": int(MAX_COMPLETION_LAG.total_seconds()),
            "state": freshness_state,
        },
        "sample_kpis": {
            "usable_completed_observations": checkpoint["completion_count"],
            "verified_decision_events": decision_count,
            "expected_decision_events": expected_decisions,
            "capital_cycle_events": int(capital["head_sequence"]) - 1,
            "symbol_decisions_per_observation": 2,
        },
        "capital": {
            "account_id": capital["account_id"],
            "generation": capital["generation"],
            "currency": capital["currency"],
            "cash": capital["cash"],
            "equity": capital["equity"],
            "fees": capital["fees"],
            "realized_pnl": capital["realized_pnl"],
            "position_count": len(capital["positions"]),
            "order_count": len(orders),
            "receipt_counts": _receipt_counts(orders),
            "balanced": True,
            "head_sequence": capital["head_sequence"],
            "head_checksum": capital["head_checksum"],
        },
        **_non_authority_fields(),
    }


def run_crypto_delayed_paper_round_trip_health_once(
    *,
    epoch_manifest: Path | str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Read one already-prepared versioned round-trip epoch without mutation."""

    _assert_simulation_only()
    manifest_path = Path(epoch_manifest)
    if (
        manifest_path.parent != ROUND_TRIP_EPOCH_MANIFEST_DIRECTORY
        or manifest_path.name.startswith("generation-")
    ):
        raise CryptoRoundTripHealthError("round_trip_health_manifest_path_invalid")
    try:
        context = load_round_trip_epoch_manifest(manifest_path)
        _existing_epoch_root(context)
        prepared = prepare_round_trip_epoch_candidate(context)
        identity_before = prepared.identity_path.read_bytes()
        result = build_crypto_delayed_paper_round_trip_health(
            output_root=prepared.output_root,
            now=now,
        )
        prepared_after = prepare_round_trip_epoch_candidate(context)
        if prepared_after.identity_path.read_bytes() != identity_before:
            raise CryptoRoundTripHealthError("round_trip_health_identity_changed")
    except CryptoRoundTripHealthError:
        raise
    except (CryptoRoundTripEpochError, OSError) as exc:
        raise CryptoRoundTripHealthError("round_trip_health_epoch_invalid") from exc
    return {
        **result,
        "epoch_id": context.epoch_id,
        "epoch_generation": context.epoch_generation,
        "epoch_manifest_sha256": context.manifest_sha256,
    }


def health_exit_code(result: Mapping[str, Any]) -> int:
    if not isinstance(result, Mapping) or result.get("status") not in {"healthy", "pending"}:
        return 2
    return (
        int(
            any(
                result.get(key) != value
                for key, value in _non_authority_fields().items()
            )
        )
        * 2
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read one Crypto round-trip epoch health/KPI snapshot"
    )
    parser.add_argument("--epoch-manifest", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = run_crypto_delayed_paper_round_trip_health_once(
            epoch_manifest=args.epoch_manifest
        )
    except Exception:
        print("crypto round-trip health failed closed", file=sys.stderr)
        return 2
    code = health_exit_code(result)
    if code:
        print("crypto round-trip health failed closed", file=sys.stderr)
        return code
    print(
        json.dumps(
            result,
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
    "MAX_COMPLETION_LAG",
    "ROUND_TRIP_HEALTH_CONTRACT",
    "CryptoRoundTripHealthError",
    "build_crypto_delayed_paper_round_trip_health",
    "health_exit_code",
    "main",
    "run_crypto_delayed_paper_round_trip_health_once",
]
