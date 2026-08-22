"""Read-only non-authoritative end-of-day report for delayed minute paper."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
import json
import os
from pathlib import Path
from typing import Any, Mapping

from .minute_auto_runner import session_bar_ends
from .minute_data import SHANGHAI
from .minute_loop import (
    PRIMARY_SLEEVE,
    SLEEVE_IDS,
    MinuteFixtureClosedLoop,
    MinuteLoopContractError,
)


class MinuteDayReportError(ValueError):
    """Raised when a read-only fixture report lacks trustworthy inputs."""


def _mapping(value: object, reason: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise MinuteDayReportError(reason)
    return value


def _load_bundle(path: Path) -> Mapping[str, Any]:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise MinuteDayReportError("minute_day_report_state_bundle_invalid")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MinuteDayReportError("minute_day_report_state_bundle_invalid") from exc
    bundle = _mapping(raw, "minute_day_report_state_bundle_invalid")
    if (
        bundle.get("schema") != "tradingagent.ashare.delayed_minute_paper_bundle.v1"
        or bundle.get("authority_tier") != "non_production_fixture"
        or bundle.get("real_trading_enabled") is not False
    ):
        raise MinuteDayReportError("minute_day_report_authority_invalid")
    return bundle


def _slot(value: datetime) -> str:
    return value.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")


def _receipt_slots(
    receipt: Mapping[str, Any],
    processed: list[object],
    accepted_bar_ends: object,
    session_gaps: object,
) -> tuple[str, list[str], list[str], list[str]]:
    raw_end = receipt.get("bar_end")
    if not isinstance(raw_end, str):
        raise MinuteDayReportError("minute_day_report_receipt_invalid")
    try:
        end = datetime.strptime(raw_end, "%Y-%m-%d %H:%M:%S").replace(tzinfo=SHANGHAI)
    except ValueError as exc:
        raise MinuteDayReportError("minute_day_report_receipt_invalid") from exc
    if not processed or any(not isinstance(value, str) for value in processed):
        raise MinuteDayReportError("minute_day_report_processed_state_invalid")
    if receipt.get("snapshot_sha256") != processed[-1]:
        raise MinuteDayReportError("minute_day_report_snapshot_conflict")
    slots = list(session_bar_ends(end.date()))
    if end not in slots:
        raise MinuteDayReportError("minute_day_report_session_invalid")
    expected = [_slot(item) for item in slots]
    end_index = slots.index(end)
    if accepted_bar_ends is None:
        if len(processed) != end_index + 1:
            raise MinuteDayReportError("minute_day_report_bar_continuity_invalid")
        observed = expected[: len(processed)]
        gaps: list[str] = []
    else:
        if (
            not isinstance(accepted_bar_ends, list)
            or not accepted_bar_ends
            or any(not isinstance(value, str) for value in accepted_bar_ends)
            or len(set(accepted_bar_ends)) != len(accepted_bar_ends)
            or len(accepted_bar_ends) != len(processed)
            or accepted_bar_ends[-1] != raw_end
            or any(value not in expected for value in accepted_bar_ends)
        ):
            raise MinuteDayReportError("minute_day_report_session_history_invalid")
        accepted_set = set(accepted_bar_ends)
        ordered_accepted = [value for value in expected if value in accepted_set]
        if (
            accepted_bar_ends != ordered_accepted
            or not isinstance(session_gaps, list)
            or any(not isinstance(value, str) for value in session_gaps)
            or len(set(session_gaps)) != len(session_gaps)
            or any(value not in expected for value in session_gaps)
            or set(accepted_bar_ends) & set(session_gaps)
        ):
            raise MinuteDayReportError("minute_day_report_session_history_invalid")
        gap_set = set(session_gaps)
        ordered_gaps = [value for value in expected if value in gap_set]
        known_missing = [
            value for value in expected[:end_index] if value not in accepted_set
        ]
        if session_gaps != ordered_gaps or session_gaps != known_missing:
            raise MinuteDayReportError("minute_day_report_session_history_invalid")
        observed = list(accepted_bar_ends)
        gaps = list(session_gaps)
    missing = [value for value in expected if value not in set(observed)]
    if any(value not in missing for value in gaps):
        raise MinuteDayReportError("minute_day_report_session_history_invalid")
    return end.date().isoformat(), expected, observed, missing


def _reason_counts(records: list[Any]) -> dict[str, int]:
    values = Counter()
    for record in records:
        reason = record.rejection_reason or record.nonfill_reason
        if reason:
            values[str(reason)] += 1
    return dict(sorted(values.items()))


def _row_quality_summary(
    receipt: Mapping[str, Any], reason: str
) -> tuple[int, dict[str, int]]:
    """Summarize row quarantines without treating them as batch failures."""

    count = receipt.get("row_rejection_count", 0)
    rows = receipt.get("row_rejections", [])
    if (
        isinstance(count, bool)
        or not isinstance(count, int)
        or count < 0
        or not isinstance(rows, list)
        or count != len(rows)
    ):
        raise MinuteDayReportError(reason)
    reasons = Counter()
    for row in rows:
        if not isinstance(row, Mapping):
            raise MinuteDayReportError(reason)
        symbol = row.get("symbol")
        reason_code = row.get("reason_code")
        if (
            not isinstance(symbol, str)
            or not symbol
            or not isinstance(reason_code, str)
            or not reason_code
        ):
            raise MinuteDayReportError(reason)
        payload_hash = row.get("rejected_payload_sha256")
        if payload_hash is not None and (
            not isinstance(payload_hash, str) or len(payload_hash) != 64
        ):
            raise MinuteDayReportError(reason)
        reasons[reason_code] += 1
    return count, dict(sorted(reasons.items()))


def _receipt_history(
    bundle: Mapping[str, Any],
    receipt: Mapping[str, Any],
    *,
    observed_bar_ends: list[str],
    processed_snapshot_hashes: list[object],
) -> tuple[list[Mapping[str, Any]], bool]:
    raw = bundle.get("receipt_history")
    if raw is None:
        return [receipt], False
    if not isinstance(raw, list) or not raw:
        raise MinuteDayReportError("minute_day_report_receipt_history_invalid")
    history = [
        _mapping(item, "minute_day_report_receipt_history_invalid") for item in raw
    ]
    bars: set[str] = set()
    history_bar_ends: list[str] = []
    history_snapshot_hashes: list[str] = []
    for item in history:
        bar_end = item.get("bar_end")
        snapshot_sha256 = item.get("snapshot_sha256")
        audit_rejections = item.get("audit_rejections")
        if (
            not isinstance(bar_end, str)
            or not bar_end
            or bar_end in bars
            or not isinstance(snapshot_sha256, str)
            or not snapshot_sha256
            or isinstance(audit_rejections, bool)
            or not isinstance(audit_rejections, int)
            or audit_rejections < 0
        ):
            raise MinuteDayReportError("minute_day_report_receipt_history_invalid")
        _row_quality_summary(
            item, "minute_day_report_receipt_history_invalid"
        )
        bars.add(bar_end)
        history_bar_ends.append(bar_end)
        history_snapshot_hashes.append(snapshot_sha256)
    if history[-1] != receipt:
        raise MinuteDayReportError("minute_day_report_receipt_history_invalid")
    if (
        len(processed_snapshot_hashes) != len(observed_bar_ends)
        or any(not isinstance(value, str) for value in processed_snapshot_hashes)
        or history_bar_ends != observed_bar_ends
        or history_snapshot_hashes != processed_snapshot_hashes
    ):
        raise MinuteDayReportError("minute_day_report_receipt_history_invalid")
    return history, True


def _book_summary(
    loop: MinuteFixtureClosedLoop, sleeve_id: str, marks: Mapping[str, float]
) -> dict[str, Any]:
    book = loop.counterfactual_books.books[sleeve_id]
    missing = set(book.positions) - set(marks)
    if missing:
        raise MinuteDayReportError("minute_day_report_mark_missing")
    reconciliation = book.reconcile(
        marks={symbol: marks[symbol] for symbol in book.positions}
    )
    records = list(loop.ledgers[sleeve_id].records())
    dispositions = Counter(record.disposition.value for record in records)
    fills = [record for record in records if record.disposition.value == "paper_filled"]
    nonfills = [
        record for record in records if record.disposition.value == "paper_not_filled"
    ]
    return {
        "cash_cny": reconciliation["cash_cny"],
        "positions": reconciliation["positions_market_value"],
        "equity_cny": reconciliation["equity_cny"],
        "realized_pnl_cny": reconciliation["realized_pnl_cny"],
        "unrealized_pnl_cny": reconciliation["unrealized_pnl_cny"],
        "reconciliation_status": "fixture_reconciled"
        if reconciliation["reconciled"]
        else "fixture_blocked",
        "candidate_count": len(records),
        "disposition_counts": dict(sorted(dispositions.items())),
        "rejection_reason_counts": _reason_counts(records),
        "simulated_fills": len(fills),
        "simulated_not_filled": len(nonfills),
        "fees_cny": round(sum(record.actual_cost_cny for record in fills), 6),
        "t_plus_1_positions": {
            symbol: dict(position.acquired_by_date)
            for symbol, position in book.positions.items()
        },
    }


def build_minute_day_report(*, state_bundle: Path | str) -> dict[str, Any]:
    """Project one verified fixture bundle into a secret-free daily report."""
    if os.environ.get("REAL_TRADING_ENABLED", "false").strip().lower() != "false":
        raise MinuteDayReportError("real_trading_must_remain_disabled")
    bundle = _load_bundle(Path(state_bundle))
    receipt = _mapping(bundle.get("last_receipt"), "minute_day_report_receipt_invalid")
    audit_rejections = receipt.get("audit_rejections")
    if (
        receipt.get("status") != "pass"
        or receipt.get("authority_tier") != "non_production_fixture"
        or receipt.get("real_trading_enabled") is not False
        or isinstance(audit_rejections, bool)
        or not isinstance(audit_rejections, int)
        or audit_rejections < 0
    ):
        raise MinuteDayReportError("minute_day_report_receipt_invalid")
    _row_quality_summary(receipt, "minute_day_report_receipt_invalid")
    try:
        loop = MinuteFixtureClosedLoop.restore(
            _mapping(bundle.get("loop_state"), "minute_day_report_state_invalid")
        )
    except (MinuteLoopContractError, ValueError) as exc:
        raise MinuteDayReportError("minute_day_report_state_integrity_failed") from exc
    state = loop.export_state()
    trading_date, expected, observed, missing = _receipt_slots(
        receipt,
        list(state["processed_snapshot_hashes"]),
        state.get("accepted_bar_ends"),
        state.get("session_gaps", []),
    )
    raw_gaps = state.get("session_gaps", [])
    gaps = list(raw_gaps) if isinstance(raw_gaps, list) else []
    receipt_history, receipt_history_complete = _receipt_history(
        bundle,
        receipt,
        observed_bar_ends=observed,
        processed_snapshot_hashes=list(state["processed_snapshot_hashes"]),
    )
    cumulative_audit_rejections = sum(
        int(item["audit_rejections"]) for item in receipt_history
    )
    row_quality_summaries = [
        _row_quality_summary(item, "minute_day_report_receipt_history_invalid")
        for item in receipt_history
    ]
    cumulative_row_quality_rejections = sum(
        count for count, _ in row_quality_summaries
    )
    row_quality_reason_counts = Counter()
    for _, reasons in row_quality_summaries:
        row_quality_reason_counts.update(reasons)
    current = loop.feature_engine.export_state().get("current")
    if not isinstance(current, Mapping):
        raise MinuteDayReportError("minute_day_report_feature_state_invalid")
    marks: dict[str, float] = {}
    for symbol, value in current.items():
        row = _mapping(value, "minute_day_report_feature_state_invalid")
        close = row.get("close_cny")
        if isinstance(close, bool) or not isinstance(close, (int, float)) or close <= 0:
            raise MinuteDayReportError("minute_day_report_mark_invalid")
        marks[str(symbol)] = float(close)
    sleeves = {
        sleeve_id: _book_summary(loop, sleeve_id, marks) for sleeve_id in SLEEVE_IDS
    }
    baseline = sleeves[PRIMARY_SLEEVE]
    differences = {
        sleeve_id: {
            "equity_difference_cny": round(
                summary["equity_cny"] - baseline["equity_cny"], 6
            ),
            "realized_pnl_difference_cny": round(
                summary["realized_pnl_cny"] - baseline["realized_pnl_cny"], 6
            ),
            "fees_difference_cny": round(summary["fees_cny"] - baseline["fees_cny"], 6),
        }
        for sleeve_id, summary in sleeves.items()
    }
    primary_records = list(loop.ledgers[PRIMARY_SLEEVE].records())
    counterfactual_sleeve_ids = [
        sleeve_id for sleeve_id in SLEEVE_IDS if sleeve_id != PRIMARY_SLEEVE
    ]
    counterfactual_records = [
        record
        for sleeve_id in counterfactual_sleeve_ids
        for record in loop.ledgers[sleeve_id].records()
    ]
    counterfactual_summaries = [
        sleeves[sleeve_id] for sleeve_id in counterfactual_sleeve_ids
    ]
    full_session_complete = len(observed) == len(expected) and not gaps
    reconciliation_complete = all(
        summary["reconciliation_status"] == "fixture_reconciled"
        for summary in sleeves.values()
    )
    learning_eligible = (
        full_session_complete
        and receipt_history_complete
        and cumulative_audit_rejections == 0
        and reconciliation_complete
    )
    blocker_codes: list[str] = []
    if cumulative_audit_rejections:
        blocker_codes.append("evidence_rejections")
    if not reconciliation_complete:
        blocker_codes.append("reconciliation_incomplete")
    if not full_session_complete:
        blocker_codes.append("session_incomplete")
    if full_session_complete and not receipt_history_complete:
        blocker_codes.append("receipt_history_incomplete")
    return {
        "trading_date": trading_date,
        "expected_bar_slots": expected,
        "observed_bar_slots": observed,
        "missing_bar_slots": missing,
        "session_integrity": {
            "full_session_complete": full_session_complete,
            "learning_eligible": learning_eligible,
            "gap_count": len(gaps),
            "gaps": gaps,
        },
        "operational_readiness": {
            "status": (
                "learning_projection_ready"
                if learning_eligible
                else "learning_projection_blocked"
            ),
            "blocker_codes": blocker_codes,
            "expected_bar_slot_count": len(expected),
            "observed_bar_slot_count": len(observed),
            "missing_bar_slot_count": len(missing),
            "audit_rejection_count": cumulative_audit_rejections,
            "row_quality_rejection_count": cumulative_row_quality_rejections,
            "reconciliation_complete": reconciliation_complete,
        },
        "evidence": {
            "accepted_count": len(observed),
            "rejected_count": cumulative_audit_rejections,
            "row_quality_rejected_count": cumulative_row_quality_rejections,
            "row_quality_rejection_reason_counts": dict(
                sorted(row_quality_reason_counts.items())
            ),
            "receipt_history_complete": receipt_history_complete,
            "status": (
                "accepted_fixture_evidence_with_quality_rejections"
                if cumulative_audit_rejections == 0
                and cumulative_row_quality_rejections > 0
                else (
                    "accepted_fixture_evidence"
                    if cumulative_audit_rejections == 0
                    else "accepted_fixture_evidence_with_rejections"
                )
            ),
        },
        "candidate_and_rejections": {
            "scope": "baseline_primary_sleeve",
            "candidate_count": len(primary_records),
            "rejection_reason_counts": _reason_counts(primary_records),
        },
        "simulated_execution": {
            "scope": "baseline_primary_sleeve",
            "simulated_fills": baseline["simulated_fills"],
            "simulated_not_filled": baseline["simulated_not_filled"],
            "fees_cny": baseline["fees_cny"],
        },
        "counterfactual_execution": {
            "scope": "non_comparable_shadow_aggregate",
            "sleeve_ids": counterfactual_sleeve_ids,
            "candidate_count": len(counterfactual_records),
            "simulated_fills": sum(
                item["simulated_fills"] for item in counterfactual_summaries
            ),
            "simulated_not_filled": sum(
                item["simulated_not_filled"] for item in counterfactual_summaries
            ),
            "fees_cny": round(
                sum(item["fees_cny"] for item in counterfactual_summaries), 6
            ),
        },
        "sleeves": sleeves,
        "reconciliation_status": {
            sleeve_id: summary["reconciliation_status"]
            for sleeve_id, summary in sleeves.items()
        },
        "shadow_book_differences": differences,
        "attribution": {
            "data": {
                "accepted_bar_count": len(observed),
                "missing_bar_count": len(missing),
            },
            "decision": {
                "scope": "baseline_primary_sleeve",
                "record_count": len(primary_records),
                "reason_counts": _reason_counts(primary_records),
            },
            "execution": {"fixture_only": True, "durable": False},
        },
        "authority": {
            "execution_authority": False,
            "training_authority": False,
            "promotion_authority": False,
            "real_trading_enabled": False,
        },
    }


__all__ = ["MinuteDayReportError", "build_minute_day_report"]
