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
    bundle = {
        "schema": "tradingagent.ashare.delayed_minute_paper_bundle.v1",
        "authority_tier": "non_production_fixture",
        "real_trading_enabled": False,
        "loop_state": state,
        "last_receipt": {
            "status": "pass",
            "authority_tier": "non_production_fixture",
            "real_trading_enabled": False,
            "bar_end": accepted[-1],
            "snapshot_sha256": (
                "f" * 64 if mismatch else payload["processed_snapshot_hashes"][-1]
            ),
            "audit_rejections": audit_rejections,
        },
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


def test_day_report_marks_complete_clean_fixture_as_learning_projection_ready(
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

    assert report["session_integrity"]["learning_eligible"] is True
    assert report["operational_readiness"] == {
        "status": "learning_projection_ready",
        "blocker_codes": [],
        "expected_bar_slot_count": 48,
        "observed_bar_slot_count": 48,
        "missing_bar_slot_count": 0,
        "audit_rejection_count": 0,
        "reconciliation_complete": True,
    }


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
