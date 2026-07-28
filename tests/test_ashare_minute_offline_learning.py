from __future__ import annotations

import json
from pathlib import Path

import pytest

from Ashare.minute_loop import MinuteFixtureClosedLoop, _canonical_sha256
from Ashare.minute_offline_learning import (
    JOURNAL_NAME,
    MinuteOfflineLearningError,
    build_minute_offline_learning_projection,
    write_minute_offline_learning_projection,
)
from Ashare.minute_research import MinuteResearchUniverse


def _bundle(
    path: Path,
    *,
    complete: bool = False,
    mismatch: bool = False,
    audit_rejections: int | None = None,
) -> Path:
    loop = MinuteFixtureClosedLoop(universe=MinuteResearchUniverse(instruments=()))
    state = loop.export_state()
    payload = dict(state)
    payload.pop("state_sha256")
    count = 48 if complete else 1
    payload["processed_snapshot_hashes"] = [f"{index:064x}" for index in range(count)]
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
            "bar_end": "2026-07-28 15:00:00" if complete else "2026-07-28 09:35:00",
            "snapshot_sha256": "b" * 64
            if mismatch
            else payload["processed_snapshot_hashes"][-1],
            "audit_rejections": (0 if complete else 2)
            if audit_rejections is None
            else audit_rejections,
        },
    }
    path.write_text(json.dumps(bundle), encoding="utf-8")
    return path.resolve()


def test_incomplete_first_day_is_blocked_without_training_sample(
    tmp_path: Path,
) -> None:
    projection = build_minute_offline_learning_projection(
        state_bundle=_bundle(tmp_path / "incomplete.json")
    )
    assert projection["status"] == "blocked"
    assert projection["blockers"] == [
        "fixture_session_incomplete",
        "fixture_evidence_rejected",
    ]
    assert projection["sample_summary"]["training_sample_count"] == 0
    assert projection["calibration"]["calibrated_probability"] is None
    assert projection["challenger"]["recommendation"] == "observe_only"


def test_complete_projection_is_append_only_and_idempotent(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path / "complete.json", complete=True)
    root = (tmp_path / "learning").resolve()
    first = write_minute_offline_learning_projection(
        state_bundle=bundle, learning_root=root
    )
    second = write_minute_offline_learning_projection(
        state_bundle=bundle, learning_root=root
    )
    assert first["appended"] is True
    assert second["appended"] is False
    lines = (root / JOURNAL_NAME).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["status"] == "complete_fixture_projection"
    assert event["coverage"] == {
        "expected_bar_count": 48,
        "observed_bar_count": 48,
        "missing_bar_count": 0,
    }
    assert set(event["missed_opportunities"]["by_sleeve"]) == {
        "baseline",
        "event",
        "flow",
        "dynamic_position",
    }
    (root / "minute_fixture_learning_latest.json").unlink()
    replay = write_minute_offline_learning_projection(
        state_bundle=bundle, learning_root=root
    )
    assert replay["appended"] is False
    assert (root / "minute_fixture_learning_latest.json").is_file()


def test_full_coverage_with_rejected_evidence_remains_blocked(tmp_path: Path) -> None:
    projection = build_minute_offline_learning_projection(
        state_bundle=_bundle(
            tmp_path / "rejected.json", complete=True, audit_rejections=1
        )
    )
    assert projection["status"] == "blocked"
    assert projection["blockers"] == ["fixture_evidence_rejected"]


def test_invalid_or_conflicting_bundle_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(MinuteOfflineLearningError):
        build_minute_offline_learning_projection(
            state_bundle=_bundle(tmp_path / "bad.json", mismatch=True)
        )


def test_projection_never_grants_authority(tmp_path: Path) -> None:
    projection = build_minute_offline_learning_projection(
        state_bundle=_bundle(tmp_path / "complete.json", complete=True)
    )
    assert projection["capital_layer"] == "simulated"
    assert projection["account_type"] == "simulated"
    assert projection["authority"] == {
        "capital_authority": False,
        "execution_authority": False,
        "training_authority": False,
        "promotion_authority": False,
        "durable": False,
        "automatic_model_change_enabled": False,
        "automatic_promotion_enabled": False,
        "automatic_risk_expansion_enabled": False,
        "real_trading_enabled": False,
    }


def test_tracked_timer_candidate_is_not_an_enabled_runtime() -> None:
    timer = (
        Path(__file__).resolve().parents[1]
        / "Ashare/systemd/tradingagent-ashare-minute-learning.timer"
    ).read_text(encoding="utf-8")
    service = (
        Path(__file__).resolve().parents[1]
        / "Ashare/systemd/tradingagent-ashare-minute-learning.service"
    ).read_text(encoding="utf-8")
    assert "15:15:00" in timer
    assert "disabled by default" in timer
    assert "REAL_TRADING_ENABLED=false" in service
    assert "shared/review" not in service
