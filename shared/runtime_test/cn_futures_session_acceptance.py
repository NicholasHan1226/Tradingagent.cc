#!/usr/bin/env python3
"""Read-only acceptance for CNFutures decision evidence by valid session.

The validator never creates an account, reserves capital, or invokes an
executor.  Callers supply the sessions that are valid for the product/style
set under review so this module does not guess product-specific night hours.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from CNFutures.execution_evidence import validate_execution_evidence


SCHEMA_VERSION = "cn_futures_session_acceptance.v1"
ACCEPTED_RECORD_TYPES = (
    "prediction",
    "candidate",
    "hold",
    "risk_reject",
    "simulated_fill",
    "session_contract_violation",
)

_RECORD_TYPE_ALIASES = {
    "prediction": "prediction",
    "predict": "prediction",
    "forecast": "prediction",
    "candidate": "candidate",
    "signal_candidate": "candidate",
    "actionable_candidate": "candidate",
    "hold": "hold",
    "strategy_hold": "hold",
    "no_trade_hold": "hold",
    "risk_reject": "risk_reject",
    "risk_rejected": "risk_reject",
    "risk_rejection": "risk_reject",
    "reject": "risk_reject",
    "rejected": "risk_reject",
    "simulated_fill": "simulated_fill",
    "sim_fill": "simulated_fill",
    "filled": "simulated_fill",
    "fill": "simulated_fill",
    "partial": "simulated_fill",
    "session_contract_violation": "session_contract_violation",
}

_SAMPLE_INSUFFICIENCY_MARKERS = (
    "sample_insufficient",
    "samples_insufficient",
    "insufficient_sample",
    "insufficient_samples",
    "insufficient_training_sample",
    "awaiting_sample",
    "waiting_for_sample",
    "样本不足",
    "样本量不足",
    "等待样本",
)

_NON_CONCRETE_REASONS = {
    "",
    "hold",
    "risk_reject",
    "reject",
    "rejected",
    "no_trade",
    "no_signal",
    "unknown",
    "unspecified",
    "none",
    "null",
    "n_a",
    "na",
    "not_applicable",
    "observation_only",
}


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value == 1
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _normalize_token(value: Any) -> str:
    token = str(value or "").strip().lower()
    token = re.sub(r"[\s\-/]+", "_", token)
    return re.sub(r"_+", "_", token).strip("_")


def _normalize_trade_date(value: Any) -> str:
    raw = str(value or "").strip()
    if "T" in raw:
        raw = raw.split("T", 1)[0]
    if " " in raw:
        raw = raw.split(" ", 1)[0]
    normalized = re.sub(r"[^0-9]", "", raw)
    return normalized if len(normalized) == 8 else ""


def _record_trade_date(record: Mapping[str, Any]) -> str:
    for key in ("trade_date", "trading_date", "active_trade_date", "date"):
        value = _normalize_trade_date(record.get(key))
        if value:
            return value
    return ""


def _record_session(record: Mapping[str, Any]) -> str:
    for key in ("session", "session_name", "trading_session"):
        value = _normalize_token(record.get(key))
        if value:
            return value
    return ""


def _record_type(record: Mapping[str, Any]) -> str:
    for key in ("record_type", "event_type", "decision_type", "action", "status"):
        value = _normalize_token(record.get(key))
        if value in _RECORD_TYPE_ALIASES:
            return _RECORD_TYPE_ALIASES[value]
    receipt = record.get("receipt")
    if isinstance(receipt, Mapping):
        status = _normalize_token(receipt.get("status"))
        try:
            filled_qty = float(receipt.get("filled_qty") or 0)
        except (TypeError, ValueError):
            filled_qty = 0.0
        if status in {"filled", "partial"} and filled_qty > 0:
            return "simulated_fill"
    stage = _normalize_token(record.get("stage"))
    if stage in {"risk", "capital", "execution"} or _record_counterfactual(record):
        return "risk_reject"
    if stage or _record_reasons(record):
        return "hold"
    return ""


def _nested_mappings(record: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    mappings: list[Mapping[str, Any]] = [record]
    for key in ("order", "receipt", "size_decision"):
        value = record.get(key)
        if isinstance(value, Mapping):
            mappings.append(value)
            raw_response = value.get("raw_response")
            if isinstance(raw_response, Mapping):
                mappings.append(raw_response)
    return mappings


def _record_real_enabled(record: Mapping[str, Any]) -> bool:
    for mapping in _nested_mappings(record):
        if _truthy(mapping.get("real_trading_enabled")) or _truthy(mapping.get("real")):
            return True
        if _normalize_token(mapping.get("capital_layer")) == "real":
            return True
        if _normalize_token(mapping.get("account_type")) == "real":
            return True
    return False


def _record_counterfactual(record: Mapping[str, Any]) -> bool:
    for mapping in _nested_mappings(record):
        if _truthy(mapping.get("counterfactual_only")):
            return True
        if _normalize_token(mapping.get("execution_class")) == "counterfactual_only":
            return True
    return False


def _record_execution_eligible(record: Mapping[str, Any], record_type: str) -> bool:
    for mapping in _nested_mappings(record):
        if _truthy(mapping.get("execution_eligible")):
            return True
        if _normalize_token(mapping.get("execution_class")) == "execution_eligible":
            return True
    # Simulated scope and a positive quantity are not enough to prove a
    # real-spec, evidence-backed fill.  The runner must classify it explicitly.
    return False


def _record_pit_lineage_complete(record: Mapping[str, Any]) -> bool:
    """Require verifiable current-authority PIT lineage for eligible fills."""
    lineage_status = str(
        record.get("lineage_status") or record.get("pit_lineage_status") or ""
    ).strip()
    authority = str(record.get("authority") or "").strip()
    pit_as_of = str(record.get("point_in_time_as_of") or "").strip()
    source_event_time = str(record.get("source_event_time") or "").strip()
    snapshot_id = str(record.get("source_snapshot_id") or "").strip()
    snapshot_sha256 = str(record.get("source_snapshot_sha256") or "").strip().lower()
    if lineage_status != "complete" or authority != "market_capital_ledger":
        return False
    if not re.fullmatch(r"[0-9a-f]{64}", snapshot_sha256):
        return False
    if snapshot_id != f"CNF-SNAP-{snapshot_sha256[:16]}":
        return False
    try:
        pit = datetime.fromisoformat(pit_as_of.replace("Z", "+00:00"))
        event_time = datetime.fromisoformat(source_event_time.replace("Z", "+00:00"))
    except ValueError:
        return False
    if (
        pit.tzinfo is None
        or pit.utcoffset() is None
        or event_time.tzinfo is None
        or event_time.utcoffset() is None
    ):
        return False
    return event_time <= pit


def _record_execution_evidence_complete(record: Mapping[str, Any]) -> bool:
    evidence = record.get("execution_evidence")
    if not isinstance(evidence, Mapping):
        receipt = record.get("receipt")
        if isinstance(receipt, Mapping):
            evidence = receipt.get("execution_evidence")
    if not isinstance(evidence, Mapping):
        return False
    valid, _ = validate_execution_evidence(
        evidence,
        source_snapshot_sha256=str(record.get("source_snapshot_sha256") or ""),
    )
    return valid


def _split_reasons(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        parts: list[Any] = list(value)
    elif value is None:
        parts = []
    else:
        parts = re.split(r"[,;|，；]", str(value))
    reasons: list[str] = []
    for part in parts:
        normalized = _normalize_token(part)
        if normalized and normalized not in reasons:
            reasons.append(normalized)
    return reasons


def _record_reasons(record: Mapping[str, Any]) -> list[str]:
    keys = (
        "reasons",
        "reason",
        "hold_reason",
        "reject_reason",
        "risk_reason",
        "risk_reject_reason",
        "execution_reason",
        "blocked_reason",
        "not_traded_reason",
    )
    reasons: list[str] = []
    for key in keys:
        for reason in _split_reasons(record.get(key)):
            if reason not in reasons:
                reasons.append(reason)
    for mapping in _nested_mappings(record)[1:]:
        for key in keys:
            for reason in _split_reasons(mapping.get(key)):
                if reason not in reasons:
                    reasons.append(reason)
    return reasons


def _is_sample_insufficiency(reason: str) -> bool:
    return any(marker in reason for marker in _SAMPLE_INSUFFICIENCY_MARKERS)


def _is_concrete_reason(reason: str) -> bool:
    return (
        bool(reason)
        and reason not in _NON_CONCRETE_REASONS
        and not _is_sample_insufficiency(reason)
    )


def _flatten_payload(
    payload: Any, inherited: Mapping[str, Any] | None = None
) -> list[dict[str, Any]]:
    context = dict(inherited or {})
    if isinstance(payload, list):
        rows: list[dict[str, Any]] = []
        for item in payload:
            rows.extend(_flatten_payload(item, context))
        return rows
    if not isinstance(payload, dict):
        raise ValueError("runtime payload rows must be JSON objects")

    # Self-contained decision rows (e.g. session_decisions) carry their own
    # trade_date/session and must not be polluted by envelope context.
    is_self_contained = bool(payload.get("_row_type")) or bool(
        payload.get("trade_date")
    )

    envelope = {
        key: value
        for key, value in payload.items()
        if key
        in {
            "trade_date",
            "trading_date",
            "active_trade_date",
            "date",
            "session",
            "session_name",
            "trading_session",
            "real_trading_enabled",
        }
    }
    merged_context = {**context, **envelope}

    has_runtime_rows = (
        isinstance(payload.get("records"), list)
        or isinstance(payload.get("holds"), list)
        or isinstance(payload.get("session_decisions"), list)
        or isinstance(payload.get("session_contract_rejections"), list)
    )
    if has_runtime_rows:
        rows: list[dict[str, Any]] = []
        for key in ("records", "holds", "session_decisions"):
            items = payload.get(key)
            if not isinstance(items, list):
                continue
            for item in items:
                rows.extend(_flatten_payload(item, merged_context))
        # Emit session contract violations as synthetic rows
        contract_rejections = payload.get("session_contract_rejections")
        if isinstance(contract_rejections, list):
            for rej in contract_rejections:
                if isinstance(rej, dict):
                    rows.append(
                        {
                            "trade_date": merged_context.get("trade_date")
                            or merged_context.get("date", ""),
                            "session": "unknown",
                            "record_type": "session_contract_violation",
                            "real_trading_enabled": bool(
                                _truthy(merged_context.get("real_trading_enabled"))
                            ),
                            "violation": dict(rej),
                        }
                    )
        return rows
    if isinstance(payload.get("sessions"), list):
        rows = []
        for item in payload["sessions"]:
            rows.extend(_flatten_payload(item, merged_context))
        return rows

    if is_self_contained:
        return [dict(payload)]
    return [{**context, **payload}]


def load_runtime_records(
    path: str | Path,
    *,
    verify_checksums: bool = False,
) -> list[dict[str, Any]]:
    """Load a JSON document or JSONL stream without writing to the input.

    When verify_checksums=True, rows with a _checksum field are validated
    and a corrupt checksum raises ValueError (fail-closed).
    """

    source = Path(path)
    text = source.read_text(encoding="utf-8")

    def _verify_row(row: dict[str, Any], line_num: int) -> None:
        if not verify_checksums:
            return
        checksum = row.get("_checksum")
        if checksum is None:
            return
        content = {k: v for k, v in row.items() if k != "_checksum"}
        expected = hashlib.sha256(
            json.dumps(content, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        if checksum != expected:
            raise ValueError(
                f"Checksum mismatch at {source}:{line_num}: "
                f"expected={expected[:16]}..., got={checksum[:16]}..."
            )

    if source.suffix.lower() == ".jsonl":
        rows: list[dict[str, Any]] = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except (json.JSONDecodeError, ValueError) as exc:
                raise ValueError(f"invalid JSONL row {line_number}: {exc}") from exc
            if isinstance(row, dict):
                _verify_row(row, line_number)
            rows.extend(_flatten_payload(row))
        if verify_checksums:
            _verify_flattened_checksums(rows, source)
        return rows

    try:
        payload = json.loads(text)
        rows = _flatten_payload(payload)
        if verify_checksums:
            _verify_flattened_checksums(rows, source)
        return rows
    except json.JSONDecodeError:
        rows = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except (json.JSONDecodeError, ValueError) as exc:
                raise ValueError(
                    f"invalid JSON/JSONL row {line_number}: {exc}"
                ) from exc
            if isinstance(row, dict):
                _verify_row(row, line_number)
            rows.extend(_flatten_payload(row))
        if verify_checksums:
            _verify_flattened_checksums(rows, source)
        return rows


def _verify_flattened_checksums(
    rows: list[dict[str, Any]],
    source: Path,
) -> None:
    """Verify _checksum on every flattened row that carries one (fail-closed)."""
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        checksum = row.get("_checksum")
        if checksum is None:
            continue
        content = {k: v for k, v in row.items() if k != "_checksum"}
        expected = hashlib.sha256(
            json.dumps(content, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        if checksum != expected:
            row_id = row.get("_identity", row.get("_row_type", f"row[{i}]"))
            raise ValueError(
                f"Checksum mismatch in {source} for {row_id}: "
                f"expected={expected[:16]}..., got={checksum[:16]}..."
            )


def _dedupe_sessions(valid_sessions: Iterable[Any]) -> list[str]:
    sessions: list[str] = []
    for value in valid_sessions:
        session = _normalize_token(value)
        if session and session not in sessions:
            sessions.append(session)
    return sessions


def evaluate_session_acceptance(
    records: Sequence[Mapping[str, Any]],
    *,
    trade_date: str,
    valid_sessions: Sequence[str],
    real_trading_enabled: bool = False,
) -> dict[str, Any]:
    """Evaluate whether every supplied valid session has reviewable evidence."""

    normalized_date = _normalize_trade_date(trade_date)
    sessions = _dedupe_sessions(valid_sessions)
    failure_reasons: list[str] = []
    if not normalized_date:
        failure_reasons.append("invalid_trade_date")
    if not sessions:
        failure_reasons.append("no_valid_sessions_supplied")

    global_real_enabled = _truthy(real_trading_enabled)
    if global_real_enabled:
        failure_reasons.append("real_trading_enabled")

    by_session: dict[str, list[tuple[str, Mapping[str, Any]]]] = {
        session: [] for session in sessions
    }
    ignored_wrong_date = 0
    ignored_other_session = 0
    ignored_record_type = 0
    record_real_violations = 0
    session_contract_violation_count = 0

    for record in records:
        if not isinstance(record, Mapping):
            ignored_record_type += 1
            continue
        if _record_real_enabled(record):
            record_real_violations += 1
        # Count session contract violations before any filtering
        if _record_type(record) == "session_contract_violation":
            session_contract_violation_count += 1
        if _record_trade_date(record) != normalized_date:
            ignored_wrong_date += 1
            continue
        session = _record_session(record)
        if session not in by_session:
            ignored_other_session += 1
            continue
        record_type = _record_type(record)
        if record_type not in ACCEPTED_RECORD_TYPES:
            ignored_record_type += 1
            continue
        by_session[session].append((record_type, record))

    if record_real_violations:
        failure_reasons.append("real_trading_enabled_in_runtime_record")

    session_reports: dict[str, dict[str, Any]] = {}
    all_type_counts: Counter[str] = Counter()
    execution_eligible_fill_count = 0
    counterfactual_only_count = 0
    ambiguous_fill_count = 0
    lineage_incomplete_fill_count = 0
    execution_evidence_invalid_fill_count = 0
    sessions_accepted = 0

    for session in sessions:
        decision_rows = by_session[session]
        type_counts = Counter(record_type for record_type, _ in decision_rows)
        all_type_counts.update(type_counts)
        concrete_reasons: list[str] = []
        sample_reasons: list[str] = []
        session_execution_fills = 0
        session_counterfactual = 0
        session_ambiguous_fills = 0
        session_lineage_incomplete_fills = 0
        session_execution_evidence_invalid_fills = 0
        session_real_violations = 0

        for record_type, record in decision_rows:
            reasons = _record_reasons(record)
            for reason in reasons:
                if _is_sample_insufficiency(reason) and reason not in sample_reasons:
                    sample_reasons.append(reason)
                elif _is_concrete_reason(reason) and reason not in concrete_reasons:
                    concrete_reasons.append(reason)
            if _record_real_enabled(record):
                session_real_violations += 1
            counterfactual_only = _record_counterfactual(record)
            if counterfactual_only:
                session_counterfactual += 1
            if record_type != "simulated_fill":
                continue
            execution_eligible = _record_execution_eligible(record, record_type)
            # PIT lineage gate: execution-eligible requires complete lineage
            pit_lineage_ok = _record_pit_lineage_complete(record)
            if execution_eligible and not pit_lineage_ok:
                execution_eligible = False
                session_lineage_incomplete_fills += 1
            elif execution_eligible and not _record_execution_evidence_complete(record):
                execution_eligible = False
                session_execution_evidence_invalid_fills += 1
            elif execution_eligible == counterfactual_only:
                session_ambiguous_fills += 1
            elif execution_eligible:
                session_execution_fills += 1

        session_failures: list[str] = []
        if not decision_rows:
            session_failures.append("missing_session_decision_record")
            failure_reasons.append(f"missing_session:{session}")
        else:
            only_non_action = all(
                record_type in {"hold", "risk_reject"}
                for record_type, _ in decision_rows
            )
            if only_non_action and not concrete_reasons:
                if sample_reasons:
                    session_failures.append(
                        "sample_insufficiency_without_concrete_reason"
                    )
                else:
                    session_failures.append("hold_or_reject_without_concrete_reason")
                failure_reasons.append(f"non_concrete_hold_or_reject:{session}")
            if session_counterfactual and not concrete_reasons:
                session_failures.append("counterfactual_without_concrete_reason")
                failure_reasons.append(f"counterfactual_without_reason:{session}")
            if session_ambiguous_fills:
                session_failures.append("ambiguous_simulated_fill_class")
                failure_reasons.append(f"ambiguous_simulated_fill_class:{session}")
            if session_lineage_incomplete_fills:
                session_failures.append("execution_fill_lineage_incomplete")
                failure_reasons.append(f"execution_fill_lineage_incomplete:{session}")
            if session_execution_evidence_invalid_fills:
                session_failures.append("execution_fill_evidence_invalid")
                failure_reasons.append(f"execution_fill_evidence_invalid:{session}")

        accepted = not session_failures
        if accepted:
            sessions_accepted += 1
        execution_eligible_fill_count += session_execution_fills
        counterfactual_only_count += session_counterfactual
        ambiguous_fill_count += session_ambiguous_fills
        lineage_incomplete_fill_count += session_lineage_incomplete_fills
        execution_evidence_invalid_fill_count += (
            session_execution_evidence_invalid_fills
        )
        session_reports[session] = {
            "status": "pass" if accepted else "fail",
            "accepted": accepted,
            "record_count": len(decision_rows),
            "record_type_counts": {
                key: type_counts[key]
                for key in ACCEPTED_RECORD_TYPES
                if type_counts[key]
            },
            "execution_eligible_simulated_fill_count": session_execution_fills,
            "counterfactual_only_count": session_counterfactual,
            "ambiguous_fill_count": session_ambiguous_fills,
            "lineage_incomplete_fill_count": session_lineage_incomplete_fills,
            "execution_evidence_invalid_fill_count": (
                session_execution_evidence_invalid_fills
            ),
            "real_trading_violation_count": session_real_violations,
            "concrete_reasons": concrete_reasons,
            "sample_insufficiency_reasons": sample_reasons,
            "reasons": session_failures,
        }

    ready = not failure_reasons
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "pass" if ready else "fail",
        "ready": ready,
        "trade_date": normalized_date,
        "valid_sessions": sessions,
        "read_only": True,
        "real_trading_enabled": global_real_enabled,
        "failure_reasons": list(dict.fromkeys(failure_reasons)),
        "summary": {
            "sessions_expected": len(sessions),
            "sessions_accepted": sessions_accepted,
            "sessions_failed": len(sessions) - sessions_accepted,
            "decision_record_count": sum(all_type_counts.values()),
            "record_type_counts": {
                key: all_type_counts[key]
                for key in ACCEPTED_RECORD_TYPES
                if all_type_counts[key]
            },
            "execution_eligible_simulated_fill_count": execution_eligible_fill_count,
            "counterfactual_only_count": counterfactual_only_count,
            "ambiguous_fill_count": ambiguous_fill_count,
            "lineage_incomplete_fill_count": lineage_incomplete_fill_count,
            "execution_evidence_invalid_fill_count": (
                execution_evidence_invalid_fill_count
            ),
            "session_contract_violation_count": session_contract_violation_count,
            "real_trading_violation_count": record_real_violations
            + int(global_real_enabled),
            "ignored_wrong_trade_date_count": ignored_wrong_date,
            "ignored_other_session_count": ignored_other_session,
            "ignored_record_type_count": ignored_record_type,
        },
        "sessions": session_reports,
    }


def _parse_sessions(raw: str) -> list[str]:
    return _dedupe_sessions(re.split(r"[,;]", raw))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only CNFutures per-session decision-sample acceptance."
    )
    parser.add_argument("--input", required=True, help="Runtime JSON or JSONL file.")
    parser.add_argument(
        "--trade-date", required=True, help="Target futures trade date."
    )
    parser.add_argument(
        "--sessions",
        required=True,
        help="Comma-separated valid sessions, for example day_morning,day_afternoon.",
    )
    parser.add_argument("--pretty", action="store_true")
    parser.add_argument(
        "--verify-checksums",
        action="store_true",
        help="Fail closed when a row _checksum does not match its content.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    real_enabled = _truthy(os.environ.get("REAL_TRADING_ENABLED", "false"))
    try:
        records = load_runtime_records(
            args.input,
            verify_checksums=args.verify_checksums,
        )
        report = evaluate_session_acceptance(
            records,
            trade_date=args.trade_date,
            valid_sessions=_parse_sessions(args.sessions),
            real_trading_enabled=real_enabled,
        )
    except (OSError, ValueError) as exc:
        report = {
            "schema_version": SCHEMA_VERSION,
            "status": "fail",
            "ready": False,
            "trade_date": _normalize_trade_date(args.trade_date),
            "valid_sessions": _parse_sessions(args.sessions),
            "read_only": True,
            "real_trading_enabled": real_enabled,
            "failure_reasons": ["runtime_input_unreadable_or_invalid"],
            "error": f"{type(exc).__name__}: {exc}",
            "summary": {},
            "sessions": {},
        }
    print(json.dumps(report, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0 if report.get("status") == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ACCEPTED_RECORD_TYPES",
    "evaluate_session_acceptance",
    "load_runtime_records",
    "main",
    "_record_pit_lineage_complete",
]
