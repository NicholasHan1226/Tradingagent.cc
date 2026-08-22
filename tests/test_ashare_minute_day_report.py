from __future__ import annotations

from datetime import date
import json
from pathlib import Path

import pytest

import Ashare.minute_day_report as minute_day_report
from Ashare.minute_auto_runner import session_bar_ends
from Ashare.minute_day_report import MinuteDayReportError, build_minute_day_report
from Ashare.minute_loop import MinuteFixtureClosedLoop, _canonical_sha256
from Ashare.minute_research import MinuteResearchUniverse


def _bundle(
    path: Path,
    *,
    mismatch: bool = False,
    accepted_bar_ends: list[str] | None = None,
    session_gaps: list[str] | None = None,
    audit_rejections: int = 2,
    row_rejections: list[dict[str, object]] | None = None,
) -> Path:
    loop = MinuteFixtureClosedLoop(universe=MinuteResearchUniverse(instruments=()))
    state = loop.export_state()
    payload = dict(state)
    payload.pop("state_sha256")
    accepted = (
        accepted_bar_ends if accepted_bar_ends is not None else ["2026-07-28 09:35:00"]
    )
    payload["processed_snapshot_hashes"] = [
        f"{index:064x}" for index in range(len(accepted))
    ]
    payload["accepted_bar_ends"] = accepted
    payload["session_gaps"] = session_gaps or []
    state = {**payload, "state_sha256": _canonical_sha256(payload)}
    last_receipt = {
        "status": "pass",
        "authority_tier": "non_production_fixture",
        "real_trading_enabled": False,
        "bar_end": accepted[-1],
        "snapshot_sha256": (
            "f" * 64 if mismatch else payload["processed_snapshot_hashes"][-1]
        ),
        "audit_rejections": audit_rejections,
    }
    if row_rejections is not None:
        last_receipt["row_rejection_count"] = len(row_rejections)
        last_receipt["row_rejections"] = row_rejections
    bundle = {
        "schema": "tradingagent.ashare.delayed_minute_paper_bundle.v1",
        "authority_tier": "non_production_fixture",
        "real_trading_enabled": False,
        "loop_state": state,
        "last_receipt": last_receipt,
    }
    path.write_text(json.dumps(bundle), encoding="utf-8")
    return path


def test_day_report_is_non_authoritative_and_covers_required_sections(
    tmp_path: Path,
) -> None:
    report = build_minute_day_report(state_bundle=_bundle(tmp_path / "state.json"))
    assert report["trading_date"] == "2026-07-28"
    assert len(report["expected_bar_slots"]) == 48
    assert report["observed_bar_slots"] == ["2026-07-28 09:35:00"]
    assert len(report["missing_bar_slots"]) == 47
    assert report["evidence"]["rejected_count"] == 2
    assert set(report["shadow_book_differences"]) == {
        "baseline",
        "event",
        "flow",
        "dynamic_position",
    }
    assert report["authority"] == {
        "execution_authority": False,
        "training_authority": False,
        "promotion_authority": False,
        "real_trading_enabled": False,
    }
    assert report["operational_readiness"] == {
        "status": "learning_projection_blocked",
        "blocker_codes": ["evidence_rejections", "session_incomplete"],
        "expected_bar_slot_count": 48,
        "observed_bar_slot_count": 1,
        "missing_bar_slot_count": 47,
        "audit_rejection_count": 2,
        "row_quality_rejection_count": 0,
        "reconciliation_complete": True,
    }


def test_day_report_keeps_post_gap_observations_but_blocks_learning(
    tmp_path: Path,
) -> None:
    report = build_minute_day_report(
        state_bundle=_bundle(
            tmp_path / "state.json",
            accepted_bar_ends=[
                "2026-07-28 09:35:00",
                "2026-07-28 09:45:00",
            ],
            session_gaps=["2026-07-28 09:40:00"],
        )
    )

    assert report["observed_bar_slots"] == [
        "2026-07-28 09:35:00",
        "2026-07-28 09:45:00",
    ]
    assert "2026-07-28 09:40:00" in report["missing_bar_slots"]
    assert report["session_integrity"] == {
        "full_session_complete": False,
        "learning_eligible": False,
        "gap_count": 1,
        "gaps": ["2026-07-28 09:40:00"],
    }
    assert report["operational_readiness"]["blocker_codes"] == [
        "evidence_rejections",
        "session_incomplete",
    ]


def test_day_report_uses_cumulative_per_bar_receipt_history(
    tmp_path: Path,
) -> None:
    path = _bundle(
        tmp_path / "state.json",
        accepted_bar_ends=["2026-07-28 09:35:00", "2026-07-28 09:40:00"],
        audit_rejections=0,
    )
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["receipt_history"] = [
        {
            **raw["last_receipt"],
            "bar_end": "2026-07-28 09:35:00",
            "snapshot_sha256": raw["loop_state"]["processed_snapshot_hashes"][0],
            "audit_rejections": 1,
        },
        raw["last_receipt"],
    ]
    path.write_text(json.dumps(raw), encoding="utf-8")

    report = build_minute_day_report(state_bundle=path)

    assert report["evidence"] == {
        "accepted_count": 2,
        "rejected_count": 1,
        "row_quality_rejected_count": 0,
        "row_quality_rejection_reason_counts": {},
        "receipt_history_complete": True,
        "status": "accepted_fixture_evidence_with_rejections",
    }
    assert report["operational_readiness"]["audit_rejection_count"] == 1


def test_day_report_marks_complete_clean_fixture_as_learning_projection_ready(
    tmp_path: Path,
) -> None:
    slots = [
        value.strftime("%Y-%m-%d %H:%M:%S")
        for value in session_bar_ends(date(2026, 7, 28))
    ]
    path = _bundle(
        tmp_path / "state.json",
        accepted_bar_ends=slots,
        audit_rejections=0,
    )
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["receipt_history"] = [
        {
            **raw["last_receipt"],
            "bar_end": bar_end,
            "snapshot_sha256": snapshot_sha256,
        }
        for bar_end, snapshot_sha256 in zip(
            raw["loop_state"]["accepted_bar_ends"],
            raw["loop_state"]["processed_snapshot_hashes"],
            strict=True,
        )
    ]
    path.write_text(json.dumps(raw), encoding="utf-8")

    report = build_minute_day_report(state_bundle=path)

    assert report["session_integrity"]["learning_eligible"] is True
    assert report["operational_readiness"] == {
        "status": "learning_projection_ready",
        "blocker_codes": [],
        "expected_bar_slot_count": 48,
        "observed_bar_slot_count": 48,
        "missing_bar_slot_count": 0,
        "audit_rejection_count": 0,
        "row_quality_rejection_count": 0,
        "reconciliation_complete": True,
    }


def test_day_report_exposes_row_quality_rejections_without_batch_failure(
    tmp_path: Path,
) -> None:
    row_rejections = [
        {
            "symbol": "000001.SZ",
            "reason_code": "minute_open_invalid",
            "rejected_payload_sha256": "a" * 64,
        },
        {
            "symbol": "600000.SH",
            "reason_code": "minute_open_invalid",
            "rejected_payload_sha256": "b" * 64,
        },
    ]

    report = build_minute_day_report(
        state_bundle=_bundle(
            tmp_path / "state.json",
            audit_rejections=0,
            row_rejections=row_rejections,
        )
    )

    assert report["evidence"]["row_quality_rejected_count"] == 2
    assert report["evidence"]["row_quality_rejection_reason_counts"] == {
        "minute_open_invalid": 2
    }
    assert report["evidence"]["status"] == (
        "accepted_fixture_evidence_with_quality_rejections"
    )
    assert report["operational_readiness"]["audit_rejection_count"] == 0
    assert report["operational_readiness"]["row_quality_rejection_count"] == 2


def test_day_report_blocks_full_session_without_per_bar_receipt_history(
    tmp_path: Path,
) -> None:
    slots = [
        value.strftime("%Y-%m-%d %H:%M:%S")
        for value in session_bar_ends(date(2026, 7, 28))
    ]

    report = build_minute_day_report(
        state_bundle=_bundle(
            tmp_path / "state.json",
            accepted_bar_ends=slots,
            audit_rejections=0,
        )
    )

    assert report["session_integrity"]["full_session_complete"] is True
    assert report["session_integrity"]["learning_eligible"] is False
    assert report["evidence"]["receipt_history_complete"] is False
    assert report["operational_readiness"]["blocker_codes"] == [
        "receipt_history_incomplete"
    ]


def test_day_report_rejects_truncated_per_bar_receipt_history(
    tmp_path: Path,
) -> None:
    slots = [
        value.strftime("%Y-%m-%d %H:%M:%S")
        for value in session_bar_ends(date(2026, 7, 28))
    ]
    path = _bundle(
        tmp_path / "state.json",
        accepted_bar_ends=slots,
        audit_rejections=0,
    )
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["receipt_history"] = [raw["last_receipt"]]
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(MinuteDayReportError, match="receipt_history_invalid"):
        build_minute_day_report(state_bundle=path)


def test_day_report_exposes_reconciliation_blocker_deterministically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = minute_day_report._book_summary

    def blocked_event_book(*args: object, **kwargs: object) -> dict[str, object]:
        result = original(*args, **kwargs)
        if args[1] == "event":
            return {**result, "reconciliation_status": "fixture_blocked"}
        return result

    monkeypatch.setattr(minute_day_report, "_book_summary", blocked_event_book)

    report = build_minute_day_report(state_bundle=_bundle(tmp_path / "state.json"))

    assert report["operational_readiness"]["blocker_codes"] == [
        "evidence_rejections",
        "reconciliation_incomplete",
        "session_incomplete",
    ]
    assert report["operational_readiness"]["reconciliation_complete"] is False


def test_day_report_keeps_primary_kpis_separate_from_counterfactual_totals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def summary(_: object, sleeve_id: str, __: object) -> dict[str, object]:
        fills = {"baseline": 1, "event": 2, "flow": 3, "dynamic_position": 4}
        records = {"baseline": 5, "event": 6, "flow": 7, "dynamic_position": 8}
        return {
            "cash_cny": 50_000.0,
            "positions": 0.0,
            "equity_cny": 50_000.0,
            "realized_pnl_cny": 0.0,
            "unrealized_pnl_cny": 0.0,
            "reconciliation_status": "fixture_reconciled",
            "candidate_count": records[sleeve_id],
            "disposition_counts": {},
            "rejection_reason_counts": {"fixture_rejected": records[sleeve_id]},
            "simulated_fills": fills[sleeve_id],
            "simulated_not_filled": records[sleeve_id] - fills[sleeve_id],
            "fees_cny": float(fills[sleeve_id]),
            "t_plus_1_positions": {},
        }

    monkeypatch.setattr(minute_day_report, "_book_summary", summary)
    report = build_minute_day_report(state_bundle=_bundle(tmp_path / "state.json"))

    assert report["candidate_and_rejections"] == {
        "scope": "baseline_primary_sleeve",
        "candidate_count": 0,
        "rejection_reason_counts": {},
    }
    assert report["simulated_execution"] == {
        "scope": "baseline_primary_sleeve",
        "simulated_fills": 1,
        "simulated_not_filled": 4,
        "fees_cny": 1.0,
    }
    assert report["counterfactual_execution"] == {
        "scope": "non_comparable_shadow_aggregate",
        "sleeve_ids": ["event", "flow", "dynamic_position"],
        "candidate_count": 0,
        "simulated_fills": 9,
        "simulated_not_filled": 12,
        "fees_cny": 9.0,
    }


@pytest.mark.parametrize("mismatch", [True, False])
def test_day_report_fails_closed_on_receipt_or_hash_conflict(
    tmp_path: Path, mismatch: bool
) -> None:
    path = _bundle(tmp_path / "state.json", mismatch=mismatch)
    if not mismatch:
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["loop_state"]["state_sha256"] = "0" * 64
        path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(MinuteDayReportError):
        build_minute_day_report(state_bundle=path)
