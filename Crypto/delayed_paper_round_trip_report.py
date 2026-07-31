"""Read-only KPI and acceptance evidence for the active G4 round-trip epoch.

The report separates service reliability, audited simulation samples, and
simulated capital outcomes.  It never interprets a simulated PnL as strategy
edge and never writes learning, capital, decisions, or runtime state.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import sys
from typing import Any, Mapping

from Crypto.delayed_paper_ledger import (
    CryptoDelayedPaperLedgerError,
    CryptoDelayedPaperObservationStore,
    _market_slot,
    _read_json,
)
from Crypto.delayed_paper_round_trip_epoch import (
    ROUND_TRIP_EPOCH_MANIFEST_DIRECTORY,
    CryptoRoundTripEpochError,
    load_round_trip_epoch_manifest,
    prepare_round_trip_epoch_candidate,
)
from Crypto.delayed_paper_round_trip_health import (
    CryptoRoundTripHealthError,
    build_crypto_delayed_paper_round_trip_health,
)
from Crypto.fixture_sim.contracts import _assert_simulation_only
from Crypto.round_trip_capital import CryptoRoundTripError, RoundTripCapitalLedger


ROUND_TRIP_REPORT_CONTRACT = "tradingagent.crypto.round_trip_report.v1"
ROUND_TRIP_ACCEPTANCE_CONTRACT = "tradingagent.crypto.round_trip_acceptance.v1"
FIVE_MINUTES = timedelta(minutes=5)
TWENTY_FOUR_HOUR_COMPLETIONS = 288


class CryptoRoundTripReportError(RuntimeError):
    """Stable fail-closed error for non-authoritative G4 reporting."""


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


def _existing_root(root: Path) -> None:
    required = (
        root,
        root / "delayed_paper",
        root / "delayed_paper" / "observations",
        root / "delayed_paper" / "completions",
        root / "delayed_paper" / ".lock",
        root / "round_trip_capital",
        root / "round_trip_capital" / ".lock",
    )
    if any(not path.exists() or path.is_symlink() for path in required):
        raise CryptoRoundTripReportError("round_trip_report_root_incomplete")


def _completed_slots(store: CryptoDelayedPaperObservationStore) -> list[datetime]:
    slots: list[datetime] = []
    try:
        for path in store.completions_dir.glob("*.json"):
            observation = _read_json(store._observation_path(path.stem))
            completion = _read_json(path)
            store._verify_observation(observation)
            store._verify_completion(completion, observation=observation)
            if completion.get("status") != "completed":
                raise CryptoRoundTripReportError("round_trip_report_completion_invalid")
            slots.append(_market_slot(observation.get("market_slot")))
    except CryptoRoundTripReportError:
        raise
    except (CryptoDelayedPaperLedgerError, OSError, ValueError) as exc:
        raise CryptoRoundTripReportError(
            "round_trip_report_completion_invalid"
        ) from exc
    slots.sort()
    if len(set(slots)) != len(slots):
        raise CryptoRoundTripReportError("round_trip_report_slot_duplicate")
    return slots


def _slot_summary(slots: list[datetime]) -> dict[str, Any]:
    if not slots:
        return {
            "completion_count": 0,
            "continuous": False,
            "first_completed_market_slot": None,
            "latest_completed_market_slot": None,
            "covered_minutes": 0,
        }
    continuous = all(
        later - earlier == FIVE_MINUTES for earlier, later in zip(slots, slots[1:])
    )
    covered_minutes = int((slots[-1] - slots[0]).total_seconds() // 60) + 5
    return {
        "completion_count": len(slots),
        "continuous": continuous,
        "first_completed_market_slot": slots[0].isoformat().replace("+00:00", "Z"),
        "latest_completed_market_slot": slots[-1].isoformat().replace("+00:00", "Z"),
        "covered_minutes": covered_minutes,
    }


def _decimal_delta(after: Any, before: Any) -> str:
    try:
        return format(Decimal(str(after)) - Decimal(str(before)), "f")
    except (InvalidOperation, ValueError) as exc:
        raise CryptoRoundTripReportError(
            "round_trip_report_capital_delta_invalid"
        ) from exc


def _outcomes(events: list[Mapping[str, Any]]) -> dict[str, Any]:
    exits: list[dict[str, Any]] = []
    rejected = 0
    for row in events:
        if row.get("event_type") != "cycle":
            continue
        payload = row.get("payload")
        if not isinstance(payload, Mapping):
            raise CryptoRoundTripReportError("round_trip_report_cycle_invalid")
        order = payload.get("order")
        receipt = payload.get("receipt")
        if not isinstance(order, Mapping) or not isinstance(receipt, Mapping):
            continue
        if order.get("side") != "sell":
            continue
        status = receipt.get("status")
        if status == "fixture_rejected":
            rejected += 1
            continue
        if status not in {"fixture_simulated", "fixture_partially_simulated"}:
            raise CryptoRoundTripReportError("round_trip_report_exit_status_invalid")
        before = payload.get("before")
        after = payload.get("after")
        if not isinstance(before, Mapping) or not isinstance(after, Mapping):
            raise CryptoRoundTripReportError("round_trip_report_cycle_invalid")
        exits.append(
            {
                "cycle_id": payload.get("cycle_id"),
                "symbol": payload.get("symbol"),
                "market_slot": payload.get("execution_slot"),
                "exit_reason": payload.get("exit_reason"),
                "receipt_status": status,
                "realized_pnl_delta": _decimal_delta(
                    after.get("realized_pnl"), before.get("realized_pnl")
                ),
                "fee": receipt.get("fee"),
            }
        )
    reasons = Counter(str(row["exit_reason"]) for row in exits)
    return {
        "completed_round_trip_count": len(exits),
        "rejected_exit_count": rejected,
        "exit_reason_counts": dict(sorted(reasons.items())),
        "completed_round_trips": exits,
    }


def build_crypto_delayed_paper_round_trip_report(
    *, output_root: Path | str, now: datetime | None = None
) -> dict[str, Any]:
    """Return audit-bound G4 KPIs without mutating the epoch root."""

    _assert_simulation_only()
    root = Path(output_root)
    _existing_root(root)
    try:
        health = build_crypto_delayed_paper_round_trip_health(output_root=root, now=now)
        store = CryptoDelayedPaperObservationStore(root)
        slots = _completed_slots(store)
        capital_events = RoundTripCapitalLedger(
            root / "round_trip_capital"
        ).events_read_only()
    except CryptoRoundTripReportError:
        raise
    except (CryptoRoundTripHealthError, CryptoRoundTripError) as exc:
        raise CryptoRoundTripReportError("round_trip_report_source_invalid") from exc
    slot_summary = _slot_summary(slots)
    if slot_summary["completion_count"] != health["core"]["completion_count"]:
        raise CryptoRoundTripReportError("round_trip_report_completion_count_invalid")
    outcome = _outcomes(capital_events)
    return {
        "contract": ROUND_TRIP_REPORT_CONTRACT,
        "market": "crypto",
        "epoch_output_root": str(root),
        "service_reliability": {
            "core_status": health["status"],
            "pending": health["core"]["pending"],
            "completion_freshness": health["freshness"],
            **slot_summary,
        },
        "audited_samples": {
            "verified_decision_events": health["sample_kpis"][
                "verified_decision_events"
            ],
            "symbol_decisions_per_observation": 2,
            **outcome,
        },
        "simulated_capital_only": {
            "equity": health["capital"]["equity"],
            "cash": health["capital"]["cash"],
            "fees": health["capital"]["fees"],
            "realized_pnl": health["capital"]["realized_pnl"],
            "balanced": health["capital"]["balanced"],
            "not_strategy_edge": True,
        },
        "strategy_assessment": {
            "status": "not_assessed",
            "reason_codes": [
                "simulated_outcomes_only",
                "time_series_samples_not_independent",
                "manual_review_required",
            ],
        },
        **_non_authority_fields(),
    }


def evaluate_crypto_delayed_paper_round_trip_acceptance(
    *,
    output_root: Path | str,
    now: datetime | None = None,
    minimum_completion_count: int = TWENTY_FOUR_HOUR_COMPLETIONS,
) -> dict[str, Any]:
    """Evaluate fixed readiness gates; never enables a timer or writes state."""

    if minimum_completion_count <= 0:
        raise ValueError("round_trip_acceptance_minimum_invalid")
    report = build_crypto_delayed_paper_round_trip_report(
        output_root=output_root, now=now
    )
    reliability = report["service_reliability"]
    capital = report["simulated_capital_only"]
    reasons: list[str] = []
    if reliability["completion_count"] < minimum_completion_count:
        reasons.append("insufficient_completed_5m_windows")
    if reliability["covered_minutes"] < minimum_completion_count * 5:
        reasons.append("insufficient_covered_minutes")
    if reliability["continuous"] is not True:
        reasons.append("non_continuous_5m_windows")
    if reliability["core_status"] != "healthy":
        reasons.append("core_health_not_healthy")
    if reliability["pending"] is not False:
        reasons.append("core_pending")
    if capital["balanced"] is not True:
        reasons.append("capital_not_balanced")
    return {
        "contract": ROUND_TRIP_ACCEPTANCE_CONTRACT,
        "status": "eligible" if not reasons else "not_ready",
        "gate_reason_codes": reasons,
        "minimum_completion_count": minimum_completion_count,
        "learning_timer_enable_authorized": False,
        "next_action": (
            "run_disabled_full_scrub_then_idempotent_replay"
            if not reasons
            else "continue_core_accumulation"
        ),
        "report": report,
        **_non_authority_fields(),
    }


def _manifest_path(value: Path | str) -> Path:
    path = Path(value)
    if (
        path.parent != ROUND_TRIP_EPOCH_MANIFEST_DIRECTORY
        or not path.name.startswith("crypto-delayed-paper-round-trip-epoch-g4-")
        or path.suffix != ".json"
    ):
        raise CryptoRoundTripReportError("round_trip_report_manifest_path_invalid")
    return path


def run_crypto_delayed_paper_round_trip_acceptance_once(
    *, epoch_manifest: Path | str, now: datetime | None = None
) -> dict[str, Any]:
    """Bind one read-only acceptance evaluation to the selected G4 epoch."""

    manifest_path = _manifest_path(epoch_manifest)
    try:
        context = load_round_trip_epoch_manifest(manifest_path)
        if context.epoch_generation != 4:
            raise CryptoRoundTripReportError(
                "round_trip_report_epoch_generation_invalid"
            )
        _existing_root(context.output_root)
        prepared = prepare_round_trip_epoch_candidate(context)
        identity_before = prepared.identity_path.read_bytes()
        result = evaluate_crypto_delayed_paper_round_trip_acceptance(
            output_root=prepared.output_root, now=now
        )
        if (
            prepare_round_trip_epoch_candidate(context).identity_path.read_bytes()
            != identity_before
        ):
            raise CryptoRoundTripReportError("round_trip_report_epoch_identity_changed")
    except CryptoRoundTripReportError:
        raise
    except (CryptoRoundTripEpochError, OSError) as exc:
        raise CryptoRoundTripReportError("round_trip_report_epoch_invalid") from exc
    return {
        **result,
        "epoch_id": context.epoch_id,
        "epoch_generation": context.epoch_generation,
        "epoch_manifest_sha256": context.manifest_sha256,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read Crypto G4 acceptance KPIs")
    parser.add_argument("--epoch-manifest", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = run_crypto_delayed_paper_round_trip_acceptance_once(
            epoch_manifest=args.epoch_manifest
        )
    except Exception:
        print("crypto round-trip acceptance failed closed", file=sys.stderr)
        return 2
    print(
        json.dumps(
            result,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CryptoRoundTripReportError",
    "ROUND_TRIP_ACCEPTANCE_CONTRACT",
    "ROUND_TRIP_REPORT_CONTRACT",
    "TWENTY_FOUR_HOUR_COMPLETIONS",
    "build_crypto_delayed_paper_round_trip_report",
    "evaluate_crypto_delayed_paper_round_trip_acceptance",
    "run_crypto_delayed_paper_round_trip_acceptance_once",
]
