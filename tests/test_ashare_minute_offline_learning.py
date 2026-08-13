from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from Ashare import minute_canary as minute_canary_module
from Ashare.minute_canary import run_minute_canary
from Ashare.minute_auto_runner import session_bar_ends
from Ashare.minute_data import MinuteEvidenceUse, MinuteValidatedProofSummary, SHANGHAI
from Ashare.minute_loop import MinuteFixtureClosedLoop, _canonical_sha256
from Ashare.minute_offline_learning import (
    JOURNAL_NAME,
    LOCAL_CONTIGUOUS_LEARNING_PROFILE,
    MinuteOfflineLearningError,
    OBSERVATION_OUTCOME_JOURNAL_NAME,
    build_minute_forward_label,
    build_minute_observation_outcome,
    build_minute_offline_learning_projection,
    write_minute_observation_outcome,
    write_minute_offline_learning_projection,
)
from Ashare.minute_research import MinuteResearchUniverse, MinuteUniverseInstrument
from shared.data.sharedsignals_v1 import SharedSignalsV1Client

from tests.test_ashare_minute_canary import (
    _RealRtMinTransport,
    _real_rt_min_config,
    _real_rt_min_references,
    _real_rt_min_rows,
)


def _real_observation_receipt() -> tuple[dict, object]:
    rows = _real_rt_min_rows()
    config = _real_rt_min_config()
    receipt = run_minute_canary(
        config,
        token_file=Path("/run/secrets/fixture.token"),
        decision_time=datetime.fromisoformat("2026-08-13T13:50:00+08:00"),
        trading_date=date(2026, 8, 13),
        reference_facts=_real_rt_min_references(rows),
        bar_end="2026-08-13 09:40:00",
        evidence_use=MinuteEvidenceUse.HISTORICAL_DISPLAY,
        transport_factory=lambda *args, **kwargs: _RealRtMinTransport(rows),
    )
    profile = config.build_profile(
        SharedSignalsV1Client(
            config.client_config(), transport=_RealRtMinTransport(rows)
        )
    )
    return receipt, profile


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


def test_exact_receipt_bound_observation_is_append_only_and_label_blocked(
    tmp_path: Path,
) -> None:
    receipt, profile = _real_observation_receipt()
    root = (tmp_path / "learning").resolve()
    first = write_minute_observation_outcome(
        canary_receipt=receipt,
        profile=profile,
        decision_as_of="2026-08-13T13:50:00+08:00",
        learning_root=root,
    )
    second = write_minute_observation_outcome(
        canary_receipt=receipt,
        profile=profile,
        decision_as_of="2026-08-13T13:50:00+08:00",
        learning_root=root,
    )
    assert first["appended"] is True
    assert second["appended"] is False
    event = first["observation"]
    assert event["observation"]["accepted_count"] == 30
    assert event["observation"]["row_count"] == 30
    assert len(event["observation"]["proof"]["receipt_ids"]) == 5
    assert event["outcome"] == {
        "status": "pending_forward_labels",
        "forward_label_state": "blocked_missing_authoritative_forward_labels",
        "planned_horizons": ["m30", "m60", "close", "1d", "3d", "5d"],
        "training_sample_count": 0,
        "training_eligible": False,
        "labels_appended": 0,
    }
    assert event["authority"]["observation_authority"] is False
    assert event["authority"]["durable_observation"] is True
    assert event["authority"]["training_authority"] is False
    assert len((root / OBSERVATION_OUTCOME_JOURNAL_NAME).read_text().splitlines()) == 1


def test_exact_receipt_bound_summary_is_authenticated_and_replayable() -> None:
    receipt, profile = _real_observation_receipt()
    restored = minute_canary_module.snapshot_from_canary_receipt(
        receipt, profile=profile
    )
    assert restored.validated_proof_summary is not None
    assert restored.validated_proof_summary.provider == "tushare"
    tampered = json.loads(json.dumps(receipt))
    tampered["validated_proof_summary"]["provider"] = "other"
    with pytest.raises(MinuteOfflineLearningError, match="minute_observation_receipt_invalid"):
        build_minute_observation_outcome(
            canary_receipt=tampered,
            profile=profile,
            decision_as_of="2026-08-13T13:50:00+08:00",
        )


@pytest.mark.parametrize(
    "mutate, reason",
    [
        (lambda value: value["snapshot_rows"]["items"].__setitem__(0, {
            **value["snapshot_rows"]["items"][0],
            "receipt_id": value["receipt_ids"][1],
        }), "minute_observation_receipt_invalid"),
        (lambda value: value["snapshot_rows"]["items"].__setitem__(0, {
            **value["snapshot_rows"]["items"][0],
            "market_session": "afternoon",
        }), "minute_observation_receipt_invalid"),
    ],
)
def test_exact_receipt_bound_observation_rejects_tampered_rows(
    mutate, reason, tmp_path: Path
) -> None:
    receipt, profile = _real_observation_receipt()
    tampered = json.loads(json.dumps(receipt))
    mutate(tampered)
    with pytest.raises(MinuteOfflineLearningError, match=reason):
        build_minute_observation_outcome(
            canary_receipt=tampered,
            profile=profile,
            decision_as_of=datetime.fromisoformat("2026-08-13T13:50:00+08:00"),
        )


def test_exact_receipt_bound_observation_rejects_future_pit(tmp_path: Path) -> None:
    receipt, profile = _real_observation_receipt()
    with pytest.raises(MinuteOfflineLearningError, match="minute_observation_pit_or_segment_invalid"):
        build_minute_observation_outcome(
            canary_receipt=receipt,
            profile=profile,
            decision_as_of="2026-08-13T09:44:00+08:00",
        )


def test_single_symbol_m60_label_is_usable_degraded_and_shadow_only() -> None:
    receipt, profile = _real_observation_receipt()
    source_snapshot = minute_canary_module.snapshot_from_canary_receipt(
        receipt, profile=profile
    )
    source_bar = next(bar for bar in source_snapshot.bars if bar.symbol == "000001.SZ")
    target = datetime.fromisoformat("2026-08-13T10:40:00+08:00")
    future_bars = tuple(
        replace(
            bar,
            bar_start=target - timedelta(minutes=5),
            bar_end=target,
            data_through=target,
            observed_at=datetime.fromisoformat("2026-08-13T10:45:00+08:00"),
            available_at=datetime.fromisoformat("2026-08-13T10:45:00+08:00"),
            decision_time=datetime.fromisoformat("2026-08-13T13:50:00+08:00"),
            receipt_id="future-receipt-10-40",
            source_row_sha256=("d" * 64 if bar.symbol == source_bar.symbol else bar.source_row_sha256),
        )
        for bar in source_snapshot.bars
    )
    future_snapshot = replace(
        source_snapshot,
        bars=future_bars,
        validated_proof_summary=MinuteValidatedProofSummary(
            dataset_id=source_snapshot.profile.dataset_id,
            provider="tushare",
            execution_id="execution-10-40",
            config_hash="b" * 64,
            data_through="2026-08-13 10:40:00",
            receipt_ids=("future-receipt-10-40",),
            content_sha256="e" * 64,
        ),
    )
    universe = MinuteResearchUniverse(
        instruments=(
            MinuteUniverseInstrument(
                symbol="000001.SZ",
                name="Ping An Bank",
                industry="banking",
                research_theme="mainboard_opportunity_scan",
                list_date=date(1991, 4, 3),
            ),
        )
    )
    result = build_minute_forward_label(
        source_snapshot=source_snapshot,
        future_snapshot=future_snapshot,
        symbol="000001.SZ",
        target_slot=target,
        decision_as_of="2026-08-13T13:50:00+08:00",
        research_universe=universe,
    )
    assert result["status"] == "usable_degraded"
    assert result["requested_symbols"] == result["resolved_symbols"] == ["000001.SZ"]
    assert result["missing_symbols"] == []
    assert result["horizon"] == "m60"
    assert result["outcome"]["sample_count"] == 1
    assert result["outcome"]["resolved_count"] == 1
    assert result["outcome"]["pending"] == 0
    assert result["outcome"]["excluded"] == 0
    assert result["outcome"]["evaluated_status"] == "exploratory_insufficient_edge"
    assert result["shadow_suggestion"]["action"] in {"retain_for_more_evidence", "downweight"}
    assert result["shadow_suggestion"]["execution_authority"] is False
    assert len(result["artifact_sha256"]) == 64
    assert result["labels"][0]["source_receipt_id"] == source_bar.receipt_id
    assert result["labels"][0]["future_receipt_id"] == "future-receipt-10-40"


def test_single_symbol_forward_label_is_deterministic_and_rejects_pit_drift() -> None:
    receipt, profile = _real_observation_receipt()
    source_snapshot = minute_canary_module.snapshot_from_canary_receipt(receipt, profile=profile)
    source_bar = next(bar for bar in source_snapshot.bars if bar.symbol == "000001.SZ")
    target = datetime.fromisoformat("2026-08-13T10:40:00+08:00")
    future_snapshot = replace(
        source_snapshot,
        bars=tuple(replace(
            bar, bar_start=target - timedelta(minutes=5), bar_end=target,
            data_through=target,
            observed_at=datetime.fromisoformat("2026-08-13T10:45:00+08:00"),
            available_at=datetime.fromisoformat("2026-08-13T10:45:00+08:00"),
            decision_time=datetime.fromisoformat("2026-08-13T13:50:00+08:00"),
            receipt_id="future-receipt-10-40",
        ) for bar in source_snapshot.bars),
        validated_proof_summary=MinuteValidatedProofSummary(
            dataset_id=source_snapshot.profile.dataset_id, provider="tushare",
            execution_id="execution-10-40", config_hash="b" * 64,
            data_through="2026-08-13 10:40:00",
            receipt_ids=("future-receipt-10-40",), content_sha256="e" * 64,
        ),
    )
    universe = MinuteResearchUniverse(instruments=(MinuteUniverseInstrument(
        symbol="000001.SZ", name="Ping An Bank", industry="banking",
        research_theme="mainboard_opportunity_scan", list_date=date(1991, 4, 3),
    ),))
    kwargs = dict(
        source_snapshot=source_snapshot,
        future_snapshot=future_snapshot, symbol="000001.SZ", target_slot=target,
        decision_as_of="2026-08-13T13:50:00+08:00", research_universe=universe,
    )
    label = build_minute_forward_label(**kwargs)
    assert build_minute_forward_label(**kwargs) == label
    with pytest.raises(MinuteOfflineLearningError, match="minute_forward_label_pit_invalid"):
        build_minute_forward_label(**{**kwargs, "decision_as_of": "2026-08-13T09:44:00+08:00"})


@pytest.mark.parametrize("delta", [timedelta(minutes=55), timedelta(days=1)])
def test_single_symbol_forward_label_rejects_non_m60_or_cross_session(delta: timedelta) -> None:
    receipt, profile = _real_observation_receipt()
    source_snapshot = minute_canary_module.snapshot_from_canary_receipt(receipt, profile=profile)
    source_bar = next(bar for bar in source_snapshot.bars if bar.symbol == "000001.SZ")
    target = source_bar.bar_end + delta
    future_snapshot = replace(
        source_snapshot,
        bars=tuple(replace(
            bar, bar_start=target - timedelta(minutes=5), bar_end=target,
            data_through=target,
            observed_at=target + timedelta(minutes=5),
            available_at=target + timedelta(minutes=5),
            decision_time=target + timedelta(minutes=10),
            receipt_id="future-receipt-invalid",
        ) for bar in source_snapshot.bars),
        validated_proof_summary=MinuteValidatedProofSummary(
            dataset_id=source_snapshot.profile.dataset_id, provider="tushare",
            execution_id="execution-invalid", config_hash="b" * 64,
            data_through=target.strftime("%Y-%m-%d %H:%M:%S"),
            receipt_ids=("future-receipt-invalid",), content_sha256="e" * 64,
        ),
    )
    universe = MinuteResearchUniverse(instruments=(MinuteUniverseInstrument(
        symbol="000001.SZ", name="Ping An Bank", industry="banking",
        research_theme="mainboard_opportunity_scan", list_date=date(1991, 4, 3),
    ),))
    with pytest.raises(MinuteOfflineLearningError):
        build_minute_forward_label(
            source_snapshot=source_snapshot, future_snapshot=future_snapshot,
            symbol="000001.SZ", target_slot=target,
            decision_as_of="2026-08-13T13:50:00+08:00", research_universe=universe,
        )


def test_single_symbol_forward_label_rejects_proof_data_through_mismatch() -> None:
    receipt, profile = _real_observation_receipt()
    source_snapshot = minute_canary_module.snapshot_from_canary_receipt(receipt, profile=profile)
    source_bar = next(bar for bar in source_snapshot.bars if bar.symbol == "000001.SZ")
    target = datetime.fromisoformat("2026-08-13T10:40:00+08:00")
    future_snapshot = replace(
        source_snapshot,
        bars=tuple(replace(
            bar, bar_start=target - timedelta(minutes=5), bar_end=target,
            data_through=target,
            observed_at=datetime.fromisoformat("2026-08-13T10:45:00+08:00"),
            available_at=datetime.fromisoformat("2026-08-13T10:45:00+08:00"),
            decision_time=datetime.fromisoformat("2026-08-13T13:50:00+08:00"),
            receipt_id="future-receipt-mismatch",
        ) for bar in source_snapshot.bars),
        validated_proof_summary=MinuteValidatedProofSummary(
            dataset_id=source_snapshot.profile.dataset_id, provider="tushare",
            execution_id="execution-mismatch", config_hash="b" * 64,
            data_through="2026-08-13 10:41:00",
            receipt_ids=("future-receipt-mismatch",), content_sha256="e" * 64,
        ),
    )
    universe = MinuteResearchUniverse(instruments=(MinuteUniverseInstrument(
        symbol="000001.SZ", name="Ping An Bank", industry="banking",
        research_theme="mainboard_opportunity_scan", list_date=date(1991, 4, 3),
    ),))
    with pytest.raises(MinuteOfflineLearningError, match="minute_forward_label_target_mismatch"):
        build_minute_forward_label(
            source_snapshot=source_snapshot, future_snapshot=future_snapshot,
            symbol="000001.SZ", target_slot=target,
            decision_as_of="2026-08-13T13:50:00+08:00", research_universe=universe,
        )


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
