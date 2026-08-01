from __future__ import annotations

from datetime import date
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from Ashare.minute_auto_runner import session_bar_ends
from Ashare.minute_data import SHANGHAI
from Ashare.minute_loop import MinuteFixtureClosedLoop, _canonical_sha256
from Ashare.minute_offline_learning import (
    JOURNAL_NAME,
    LOCAL_CONTIGUOUS_LEARNING_PROFILE,
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
    receipt_history: bool = True,
    accepted_indexes: list[int] | None = None,
    label_evidence: bool = False,
) -> Path:
    loop = MinuteFixtureClosedLoop(universe=MinuteResearchUniverse(instruments=()))
    state = loop.export_state()
    payload = dict(state)
    payload.pop("state_sha256")
    count = 48 if complete else 1
    indexes = accepted_indexes if accepted_indexes is not None else list(range(count))
    payload["processed_snapshot_hashes"] = [f"{index:064x}" for index in range(count)]
    payload["processed_snapshot_hashes"] = [f"{index:064x}" for index in indexes]
    payload["accepted_bar_ends"] = [
        value.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")
        for value in [session_bar_ends(date(2026, 7, 28))[index] for index in indexes]
    ]
    last_index = max(indexes)
    payload["session_gaps"] = [
        value.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")
        for index, value in enumerate(session_bar_ends(date(2026, 7, 28))[:last_index])
        if index not in indexes
    ]
    state = {**payload, "state_sha256": _canonical_sha256(payload)}
    accepted_bar_ends = payload["accepted_bar_ends"]
    processed_hashes = payload["processed_snapshot_hashes"]
    assert len(accepted_bar_ends) == len(processed_hashes)
    receipts = [
        {
            "status": "pass",
            "authority_tier": "non_production_fixture",
            "real_trading_enabled": False,
            "bar_end": bar_end,
            "snapshot_sha256": snapshot_sha256,
            "audit_rejections": 0,
        }
        for bar_end, snapshot_sha256 in zip(accepted_bar_ends, processed_hashes)
    ]
    receipts[-1]["audit_rejections"] = (
        (0 if complete else 2) if audit_rejections is None else audit_rejections
    )
    if mismatch:
        receipts[-1]["snapshot_sha256"] = "b" * 64
    bundle = {
        "schema": "tradingagent.ashare.delayed_minute_paper_bundle.v1",
        "authority_tier": "non_production_fixture",
        "real_trading_enabled": False,
        "loop_state": state,
        "last_receipt": receipts[-1],
    }
    if receipt_history:
        bundle["receipt_history"] = receipts
    if label_evidence:
        bundle["local_contiguous_label_evidence"] = {
            "profile_id": LOCAL_CONTIGUOUS_LEARNING_PROFILE.profile_id,
            "status": "complete_fixture_label_evidence",
            "receipt_sha256": "a" * 64,
            "labelled_bar_ends": [accepted_bar_ends[-1]],
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


def test_complete_legacy_bundle_without_receipt_history_remains_blocked(
    tmp_path: Path,
) -> None:
    projection = build_minute_offline_learning_projection(
        state_bundle=_bundle(
            tmp_path / "legacy.json", complete=True, receipt_history=False
        )
    )
    assert projection["status"] == "blocked"
    assert projection["blockers"] == ["fixture_receipt_history_incomplete"]
    assert projection["sample_summary"]["training_sample_count"] == 0


def test_projection_records_only_a_blocked_forward_label_requirement(
    tmp_path: Path,
) -> None:
    projection = build_minute_offline_learning_projection(
        state_bundle=_bundle(tmp_path / "complete.json", complete=True)
    )
    assert projection["forward_label_state"] == {
        "status": "blocked_missing_authoritative_daily_receipt",
        "planned_horizons": ["m30", "m60", "close", "1d", "3d", "5d"],
        "fixture_candidate_count": 0,
        "labels_appended": 0,
        "authoritative_market_data_consumed": False,
    }


def test_local_contiguous_eligibility_uses_preregistered_profile_and_label_proof(
    tmp_path: Path,
) -> None:
    projection = build_minute_offline_learning_projection(
        state_bundle=_bundle(
            tmp_path / "local-window.json",
            accepted_indexes=[0, 1, 2],
            label_evidence=True,
            audit_rejections=0,
        )
    )
    local = projection["local_contiguous_learning"]
    assert local["local_learning_eligible"] is True
    assert local["minimum_slots"] == (
        local["feature_slots"] + local["label_horizon_slots"]
    )
    assert local["gap_crossing_allowed"] is False
    assert projection["sample_summary"]["training_eligible"] is False
    assert projection["authority"]["training_authority"] is False


def test_local_contiguous_learning_never_crosses_a_session_gap(tmp_path: Path) -> None:
    projection = build_minute_offline_learning_projection(
        state_bundle=_bundle(
            tmp_path / "gap.json",
            accepted_indexes=[0, 2, 3],
            label_evidence=True,
            audit_rejections=0,
        )
    )
    local = projection["local_contiguous_learning"]
    assert local["local_learning_eligible"] is False
    assert local["contiguous_segment_lengths"] == [1, 2]
    assert "local_contiguous_window_too_short" in local["blockers"]


def test_local_contiguous_learning_treats_lunch_as_a_hard_boundary(
    tmp_path: Path,
) -> None:
    projection = build_minute_offline_learning_projection(
        state_bundle=_bundle(
            tmp_path / "lunch.json",
            accepted_indexes=[22, 23, 24],
            label_evidence=True,
            audit_rejections=0,
        )
    )
    local = projection["local_contiguous_learning"]
    assert local["local_learning_eligible"] is False
    assert local["contiguous_segment_lengths"] == [2, 1]


def test_local_contiguous_learning_rejects_window_with_rejected_evidence(
    tmp_path: Path,
) -> None:
    projection = build_minute_offline_learning_projection(
        state_bundle=_bundle(
            tmp_path / "rejected-local.json",
            accepted_indexes=[0, 1, 2],
            label_evidence=True,
            audit_rejections=1,
        )
    )
    local = projection["local_contiguous_learning"]
    assert local["local_learning_eligible"] is False
    assert "local_fixture_evidence_incomplete" in local["blockers"]


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


def test_module_cli_persists_projection_and_prints_result(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path / "cli-incomplete.json")
    root = (tmp_path / "cli-learning").resolve()
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "Ashare.minute_offline_learning",
            "--state-bundle",
            str(bundle),
            "--learning-root",
            str(root),
        ],
        cwd=Path(__file__).resolve().parents[1],
        env={"PATH": os.environ["PATH"], "REAL_TRADING_ENABLED": "false"},
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0
    result = json.loads(completed.stdout)
    assert result["appended"] is True
    assert result["projection"]["status"] == "blocked"
    assert (root / JOURNAL_NAME).is_file()
    assert (root / "minute_fixture_learning_latest.json").is_file()
