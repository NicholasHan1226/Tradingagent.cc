from __future__ import annotations

import json
from pathlib import Path

import pytest

from Ashare.minute_day_report import MinuteDayReportError, build_minute_day_report
from Ashare.minute_loop import MinuteFixtureClosedLoop, _canonical_sha256
from Ashare.minute_research import MinuteResearchUniverse


def _bundle(path: Path, *, mismatch: bool = False) -> Path:
    loop = MinuteFixtureClosedLoop(universe=MinuteResearchUniverse(instruments=()))
    state = loop.export_state()
    payload = dict(state)
    payload.pop("state_sha256")
    payload["processed_snapshot_hashes"] = ["a" * 64]
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
            "bar_end": "2026-07-28 09:35:00",
            "snapshot_sha256": "b" * 64 if mismatch else "a" * 64,
            "audit_rejections": 2,
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
