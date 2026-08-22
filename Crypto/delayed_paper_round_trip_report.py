"""Read-only KPI and acceptance evidence for versioned round-trip epochs.

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
    ROUND_TRIP_EPOCH_RECOVERY_GENERATION,
    ROUND_TRIP_EPOCH_MANIFEST_DIRECTORY,
    ROUND_TRIP_EPOCH_SUCCESSOR_GENERATION,
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
FORTY_EIGHT_HOUR_WINDOWS = 48 * 60 // 5
MINIMUM_COVERAGE_RATIO = 0.90


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
            "latest_continuous_completion_count": 0,
            "latest_continuous_first_market_slot": None,
            "latest_continuous_covered_minutes": 0,
        }
    continuous = all(
        later - earlier == FIVE_MINUTES for earlier, later in zip(slots, slots[1:])
    )
    covered_minutes = int((slots[-1] - slots[0]).total_seconds() // 60) + 5
    latest_streak = 1
    for earlier, later in zip(reversed(slots[:-1]), reversed(slots[1:])):
        if later - earlier != FIVE_MINUTES:
            break
        latest_streak += 1
    latest_start = slots[-latest_streak]
    return {
        "completion_count": len(slots),
        "continuous": continuous,
        "first_completed_market_slot": slots[0].isoformat().replace("+00:00", "Z"),
        "latest_completed_market_slot": slots[-1].isoformat().replace("+00:00", "Z"),
        "covered_minutes": covered_minutes,
        "latest_continuous_completion_count": latest_streak,
        "latest_continuous_first_market_slot": latest_start.isoformat().replace(
            "+00:00", "Z"
        ),
        "latest_continuous_covered_minutes": latest_streak * 5,
    }


def _continuity_segments(
    slots: list[datetime],
    *,
    runtime_rejects: Mapping[datetime, tuple[str, ...]] | None = None,
) -> dict[str, Any]:
    """Describe observed gaps without inventing their external cause.

    A completion gap is an audit fact. This report deliberately cannot label it
    a TradingDatas, transport, systemd, or ledger fault: that needs separate
    operational evidence. Research consumers may select a whole segment, but
    must never bridge one of the reported gaps.
    """

    if not slots:
        return {
            "continuous_segment_count": 0,
            "longest_continuous_completion_count": 0,
            "segments": [],
            "gaps": [],
        }
    segments: list[list[datetime]] = [[slots[0]]]
    gaps: list[dict[str, Any]] = []
    for previous, current in zip(slots, slots[1:]):
        if current - previous == FIVE_MINUTES:
            segments[-1].append(current)
            continue
        missing = int((current - previous).total_seconds() // 300) - 1
        if missing <= 0:
            raise CryptoRoundTripReportError("round_trip_report_gap_invalid")
        gap = {
            "previous_completed_market_slot": previous.isoformat().replace(
                "+00:00", "Z"
            ),
            "next_completed_market_slot": current.isoformat().replace(
                "+00:00", "Z"
            ),
            "missing_completion_count": missing,
            "gap_minutes": missing * 5,
            "cause": "unclassified_completion_gap",
        }
        reject_evidence = []
        for offset in range(1, missing + 1):
            market_slot = previous + FIVE_MINUTES * offset
            reason_codes = (runtime_rejects or {}).get(market_slot, ())
            if reason_codes:
                reject_evidence.append(
                    {
                        "market_slot": market_slot.isoformat().replace("+00:00", "Z"),
                        "reason_codes": list(reason_codes),
                    }
                )
        if reject_evidence:
            gap["runtime_rejects"] = reject_evidence
        gaps.append(gap)
        segments.append([current])
    return {
        "continuous_segment_count": len(segments),
        "longest_continuous_completion_count": max(
            len(segment) for segment in segments
        ),
        "segments": [
            {
                "first_completed_market_slot": segment[0]
                .isoformat()
                .replace("+00:00", "Z"),
                "latest_completed_market_slot": segment[-1]
                .isoformat()
                .replace("+00:00", "Z"),
                "completion_count": len(segment),
                "covered_minutes": len(segment) * 5,
            }
            for segment in segments
        ],
        "gaps": gaps,
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
    symbols = Counter(str(row["symbol"]) for row in exits)
    total_realized_pnl = sum(
        (Decimal(str(row["realized_pnl_delta"])) for row in exits),
        Decimal("0"),
    )
    return {
        "completed_round_trip_count": len(exits),
        "rejected_exit_count": rejected,
        "exit_reason_counts": dict(sorted(reasons.items())),
        "completed_round_trip_count_by_symbol": dict(sorted(symbols.items())),
        "completed_round_trip_realized_pnl_total": format(total_realized_pnl, "f"),
        "completed_round_trips": exits,
    }


def _runtime_rejects_by_slot(
    store: CryptoDelayedPaperObservationStore,
) -> dict[datetime, tuple[str, ...]]:
    """Bind recorded local data rejects to slots without attributing a cause.

    Older append-only receipts did not record the request window and stay
    intentionally unclassified. A reject is local runtime evidence, not proof
    of an upstream collector, transport, timer, or ledger fault.
    """

    grouped: dict[datetime, set[str]] = {}
    for event in store.data_reject_events():
        raw_window_end = event.get("request_window_end")
        reason_code = event.get("reason_code")
        if not isinstance(raw_window_end, str) or not isinstance(reason_code, str):
            continue
        try:
            window_end = _market_slot(raw_window_end)
        except (CryptoDelayedPaperLedgerError, ValueError):
            continue
        market_slot = window_end - FIVE_MINUTES
        grouped.setdefault(market_slot, set()).add(reason_code)
    return {
        market_slot: tuple(sorted(reason_codes))
        for market_slot, reason_codes in sorted(grouped.items())
    }


def _data_gap_slots(
    store: CryptoDelayedPaperObservationStore,
) -> list[datetime]:
    """Expand checksum-bound data-gap receipts into skipped 5-minute slots."""

    slots: set[datetime] = set()
    try:
        events = store.data_gap_events()
        for event in events:
            skipped_from = _market_slot(event.get("skipped_from"))
            skipped_to = _market_slot(event.get("skipped_to"))
            if skipped_to < skipped_from:
                raise CryptoRoundTripReportError("round_trip_report_gap_invalid")
            span = (skipped_to - skipped_from).total_seconds()
            if span % FIVE_MINUTES.total_seconds() != 0:
                raise CryptoRoundTripReportError("round_trip_report_gap_invalid")
            for offset in range(int(span // FIVE_MINUTES.total_seconds()) + 1):
                slots.add(skipped_from + FIVE_MINUTES * offset)
    except CryptoRoundTripReportError:
        raise
    except (CryptoDelayedPaperLedgerError, OSError, TypeError, ValueError) as exc:
        raise CryptoRoundTripReportError("round_trip_report_gap_invalid") from exc
    return sorted(slots)


def _terminal_window_summary(
    completed_slots: list[datetime], gap_slots: list[datetime]
) -> dict[str, Any]:
    """Summarize completed or explicitly skipped windows for simulation coverage."""

    terminal_slots = sorted(set(completed_slots).union(gap_slots))
    if not terminal_slots:
        return {
            "terminal_window_count": 0,
            "terminal_window_span_count": 0,
            "terminal_coverage_ratio": 0.0,
            "data_gap_window_count": 0,
            "integrity_error_count": 0,
        }
    span_count = int(
        (terminal_slots[-1] - terminal_slots[0]).total_seconds()
        // FIVE_MINUTES.total_seconds()
    ) + 1
    return {
        "terminal_window_count": len(terminal_slots),
        "terminal_window_span_count": span_count,
        "terminal_coverage_ratio": round(len(terminal_slots) / span_count, 6),
        "data_gap_window_count": len(gap_slots),
        # A successful checksum-bound report read is direct evidence that no
        # configuration, ledger, or state-integrity error occurred in this snapshot.
        "integrity_error_count": 0,
    }


def _latest_terminal_window_summary(
    completed_slots: list[datetime],
    gap_slots: list[datetime],
    *,
    minimum_window_count: int,
) -> dict[str, Any]:
    """Evaluate the latest bounded runtime window, not the whole epoch history."""

    terminal_slots = sorted(set(completed_slots).union(gap_slots))
    if not terminal_slots:
        return {
            "latest_window_available_span_count": 0,
            "latest_window_terminal_window_count": 0,
            "latest_window_coverage_ratio": 0.0,
            "latest_window_data_gap_window_count": 0,
            "latest_window_first_market_slot": None,
            "latest_window_latest_market_slot": None,
        }
    earliest = terminal_slots[0]
    latest = terminal_slots[-1]
    available_span_count = int(
        (latest - earliest).total_seconds() // FIVE_MINUTES.total_seconds()
    ) + 1
    window_start = latest - FIVE_MINUTES * (minimum_window_count - 1)
    expected_slots = {
        window_start + FIVE_MINUTES * offset
        for offset in range(minimum_window_count)
    }
    terminal_set = set(terminal_slots)
    gap_set = set(gap_slots)
    covered_slots = expected_slots.intersection(terminal_set)
    return {
        "latest_window_available_span_count": available_span_count,
        "latest_window_terminal_window_count": len(covered_slots),
        "latest_window_coverage_ratio": round(
            len(covered_slots) / minimum_window_count, 6
        ),
        "latest_window_data_gap_window_count": len(
            expected_slots.intersection(gap_set)
        ),
        "latest_window_first_market_slot": window_start.isoformat().replace(
            "+00:00", "Z"
        ),
        "latest_window_latest_market_slot": latest.isoformat().replace(
            "+00:00", "Z"
        ),
    }


def build_crypto_delayed_paper_round_trip_report(
    *,
    output_root: Path | str,
    now: datetime | None = None,
    minimum_window_count: int = FORTY_EIGHT_HOUR_WINDOWS,
) -> dict[str, Any]:
    """Return audit-bound G4 KPIs without mutating the epoch root."""

    _assert_simulation_only()
    root = Path(output_root)
    _existing_root(root)
    try:
        health = build_crypto_delayed_paper_round_trip_health(output_root=root, now=now)
        store = CryptoDelayedPaperObservationStore(root)
        slots = _completed_slots(store)
        gap_slots = _data_gap_slots(store)
        runtime_rejects = _runtime_rejects_by_slot(store)
        capital_events = RoundTripCapitalLedger(
            root / "round_trip_capital"
        ).events_read_only()
    except CryptoRoundTripReportError:
        raise
    except (CryptoRoundTripHealthError, CryptoRoundTripError) as exc:
        raise CryptoRoundTripReportError("round_trip_report_source_invalid") from exc
    slot_summary = _slot_summary(slots)
    terminal_summary = _terminal_window_summary(slots, gap_slots)
    latest_window_summary = _latest_terminal_window_summary(
        slots,
        gap_slots,
        minimum_window_count=minimum_window_count,
    )
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
            **terminal_summary,
            **latest_window_summary,
            "data_reject_count": health["failure_count"],
            "continuity_segments": _continuity_segments(
                slots, runtime_rejects=runtime_rejects
            ),
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
    minimum_completion_count: int = FORTY_EIGHT_HOUR_WINDOWS,
) -> dict[str, Any]:
    """Evaluate fixed readiness gates; never enables a timer or writes state."""

    if minimum_completion_count <= 0:
        raise ValueError("round_trip_acceptance_minimum_invalid")
    report = build_crypto_delayed_paper_round_trip_report(
        output_root=output_root,
        now=now,
        minimum_window_count=minimum_completion_count,
    )
    reliability = report["service_reliability"]
    capital = report["simulated_capital_only"]
    reasons: list[str] = []
    if (
        reliability["latest_window_available_span_count"]
        < minimum_completion_count
    ):
        reasons.append("insufficient_48h_runtime")
    elif (
        reliability["latest_window_coverage_ratio"] < MINIMUM_COVERAGE_RATIO
    ):
        reasons.append("coverage_below_90_percent")
    if reliability["integrity_error_count"] != 0:
        reasons.append("integrity_errors_present")
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
        "minimum_runtime_window_count": minimum_completion_count,
        "minimum_coverage_ratio": MINIMUM_COVERAGE_RATIO,
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
        or not path.name.startswith(
            (
                "crypto-delayed-paper-round-trip-epoch-g4-",
                "crypto-delayed-paper-round-trip-epoch-g5-",
            )
        )
        or path.suffix != ".json"
    ):
        raise CryptoRoundTripReportError("round_trip_report_manifest_path_invalid")
    return path


def run_crypto_delayed_paper_round_trip_acceptance_once(
    *, epoch_manifest: Path | str, now: datetime | None = None
) -> dict[str, Any]:
    """Bind one read-only acceptance evaluation to a selected G4/G5 epoch."""

    manifest_path = _manifest_path(epoch_manifest)
    try:
        context = load_round_trip_epoch_manifest(manifest_path)
        if context.epoch_generation not in {
            ROUND_TRIP_EPOCH_SUCCESSOR_GENERATION,
            ROUND_TRIP_EPOCH_RECOVERY_GENERATION,
        }:
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
    parser = argparse.ArgumentParser(
        description="Read Crypto round-trip acceptance KPIs"
    )
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
    "FORTY_EIGHT_HOUR_WINDOWS",
    "MINIMUM_COVERAGE_RATIO",
    "ROUND_TRIP_ACCEPTANCE_CONTRACT",
    "ROUND_TRIP_REPORT_CONTRACT",
    "TWENTY_FOUR_HOUR_COMPLETIONS",
    "build_crypto_delayed_paper_round_trip_report",
    "evaluate_crypto_delayed_paper_round_trip_acceptance",
    "run_crypto_delayed_paper_round_trip_acceptance_once",
]
