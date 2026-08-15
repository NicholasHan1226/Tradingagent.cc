"""Tests for the detached ten-symbol factor-research projection (v2)."""

from __future__ import annotations

from datetime import datetime, timedelta
import json
from pathlib import Path
from typing import Any

import pytest

from Crypto.factor_research import (
    TEN_SYMBOL_FACTOR_SET_ID,
    TEN_SYMBOL_FACTOR_SET_VERSION,
)
import Crypto.ten_symbol_factor_research as research
from Crypto.ten_symbol_factor_research import (
    CryptoTenSymbolFactorProjectionError,
    run_crypto_ten_symbol_factor_research_full_scrub,
    run_crypto_ten_symbol_factor_research_incremental,
    ten_symbol_factor_projection_exit_code,
)
from Crypto.ten_symbol_observation_store import CryptoTenSymbolObservationStore
from tests.test_crypto_ten_symbol_observation_runtime import (
    _assert_recursive_non_authority,
    _factory,
    _run,
    _runtime_paths,
)
from tests.test_crypto_ten_symbol_support import (
    CATALOG_VERSION,
    OBSERVATION_SYMBOLS,
    WINDOW_END,
    TenSymbolFixtureTransport,
    iso,
)


def _accumulate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    count: int,
    *,
    start: datetime = WINDOW_END,
) -> Path:
    token_file, output_root = _runtime_paths(monkeypatch, tmp_path)
    for index in range(count):
        end = start + index * timedelta(minutes=5)
        receipt = _run(
            tmp_path,
            token_file,
            output_root,
            now=end + timedelta(seconds=55),
            transport_factory=_factory(TenSymbolFixtureTransport()),
        )
        assert receipt["status"] == "completed"
    return output_root


def _store_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and "evolution" not in path.as_posix()
    }


def _projection_files(root: Path) -> dict[str, bytes]:
    evolution = root / "evolution" / "ten_symbol_factor_research"
    return {
        path.relative_to(evolution).as_posix(): path.read_bytes()
        for path in sorted(evolution.rglob("*"))
        if path.is_file() and path.name != ".lock"
    }


def _records(root: Path) -> list[dict[str, Any]]:
    records = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(
            (root / "evolution" / "ten_symbol_factor_research" / "records").glob(
                "*.json"
            )
        )
    ]
    return sorted(records, key=lambda record: record["market_slot"])


def _checkpoints(root: Path) -> list[dict[str, Any]]:
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(
            (root / "evolution" / "ten_symbol_factor_research" / "checkpoints").glob(
                "*.json"
            )
        )
    ]


def _labels(root: Path) -> list[dict[str, Any]]:
    labels_dir = root / "evolution" / "ten_symbol_factor_research" / "labels"
    if not labels_dir.exists():
        return []
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(labels_dir.glob("*.json"))
    ]


def test_full_scrub_projects_ten_symbol_records_with_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_root = _accumulate(monkeypatch, tmp_path, 3)
    before = _store_bytes(output_root)

    result = run_crypto_ten_symbol_factor_research_full_scrub(
        output_root=output_root
    )

    assert result["status"] == "recovered"
    assert result["observation_count"] == 3
    assert result["ineligible_slot_count"] == 0
    assert result["recovered_observation_count"] == 3
    assert result["label_count"] == 0
    assert result["operational_maturity"] is False
    assert result["latest_continuous_completion_count"] == 3
    assert result["segmented_learning_profile"]["consumer_profile_id"] == (
        "crypto-5m-ohlcv-13bar-forward-labels-v2"
    )
    assert result["segmented_learning_profile"]["required_label_horizon_minutes"] == 60
    assert result["segmented_learning_profile"]["symbols"] == list(OBSERVATION_SYMBOLS)
    assert result["segmented_learning_policy"]["gap_crossing_allowed"] is False
    assert result["hypothesis_report"]["sample_count"] == 0
    assert len(result["hypothesis_report"]["hypotheses"]) == 3
    assert ten_symbol_factor_projection_exit_code(result) == 0
    _assert_recursive_non_authority(result)
    assert _store_bytes(output_root) == before

    records = _records(output_root)
    assert len(records) == 3
    record = records[0]
    _assert_recursive_non_authority(record)
    assert list(record["snapshots"]) == list(OBSERVATION_SYMBOLS)
    assert record["segmented_learning_consumer_profile_id"] == (
        "crypto-5m-ohlcv-13bar-forward-labels-v2"
    )
    for symbol in OBSERVATION_SYMBOLS:
        snapshot = record["snapshots"][symbol]
        assert snapshot["feature_set_id"] == TEN_SYMBOL_FACTOR_SET_ID
        assert snapshot["feature_set_version"] == TEN_SYMBOL_FACTOR_SET_VERSION
        assert snapshot["symbol"] == symbol
        assert snapshot["market_slot"] == record["market_slot"]
        assert snapshot["evidence_receipt_id"]
        assert len(snapshot["evidence_lineage_sha256"]) == 64
    context = record["cross_section_context"]
    assert context["is_research_hypothesis"] is False
    assert context["adds_new_hypothesis"] is False
    assert context["symbol_order"] == list(OBSERVATION_SYMBOLS)
    assert sorted(context["return_1h_rank"]) == list(OBSERVATION_SYMBOLS)
    assert sorted(context["return_1h_rank"].values()) == list(range(1, 11))
    assert sorted(context["return_15m_rank"].values()) == list(range(1, 11))
    assert {record["segment_id"] for record in records} == {
        record["segment_id"]
    }
    assert record["segment_id"].startswith("crypto-5m-segment-")

    events = CryptoTenSymbolObservationStore(output_root).events()
    assert record["source_event_checksum"] == events[0]["checksum"]
    assert record["source_observation_sha256"] == (
        events[0]["observation"]["observation_sha256"]
    )
    assert len(record["source_bars_sidecar_sha256"]) == 64
    checkpoints = _checkpoints(output_root)
    assert len(checkpoints) == 3
    assert checkpoints[0]["previous_checkpoint_sha256"] is None
    assert checkpoints[1]["previous_checkpoint_sha256"] == (
        checkpoints[0]["checkpoint_sha256"]
    )
    assert all(
        checkpoint["projection_outcome"] == "projected"
        for checkpoint in checkpoints
    )
    assert result["checkpoint_head_sha256"] == (
        checkpoints[-1]["checkpoint_sha256"]
    )


def test_full_scrub_is_idempotent_and_does_not_mutate_store(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_root = _accumulate(monkeypatch, tmp_path, 2)
    before = _store_bytes(output_root)

    first = run_crypto_ten_symbol_factor_research_full_scrub(output_root=output_root)
    after_first = _projection_files(output_root)
    second = run_crypto_ten_symbol_factor_research_full_scrub(output_root=output_root)
    after_second = _projection_files(output_root)

    assert first["status"] == "recovered"
    assert second["status"] == "scrubbed"
    assert second["recovered_observation_count"] == 0
    assert after_first == after_second
    assert _store_bytes(output_root) == before


def test_missing_sidecar_slot_cuts_segment_and_is_checkpointed_ineligible(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_root = _accumulate(monkeypatch, tmp_path, 4)
    missing_slot = WINDOW_END + timedelta(minutes=10)
    store = CryptoTenSymbolObservationStore(output_root)
    store.bars_sidecar_path(iso(missing_slot)).unlink()

    result = run_crypto_ten_symbol_factor_research_full_scrub(
        output_root=output_root
    )

    assert result["status"] == "recovered"
    assert result["observation_count"] == 4
    assert result["ineligible_slot_count"] == 1
    records = _records(output_root)
    assert len(records) == 3
    record_slots = [record["market_slot"] for record in records]
    assert iso(missing_slot - timedelta(minutes=5)) not in record_slots
    segments = {record["market_slot"]: record["segment_id"] for record in records}
    first_segment = segments[iso(WINDOW_END - timedelta(minutes=5))]
    assert segments[iso(WINDOW_END)] == first_segment
    last_segment = segments[iso(WINDOW_END + timedelta(minutes=10))]
    assert last_segment != first_segment
    checkpoints = _checkpoints(output_root)
    assert len(checkpoints) == 4
    ineligible = checkpoints[2]
    assert ineligible["projection_outcome"] == "sidecar_ineligible"
    assert ineligible["ineligible_reason"] == "sidecar_missing"
    assert ineligible["projection_receipt_sha256"] is None
    assert ineligible["market_slot"] == iso(missing_slot - timedelta(minutes=5))

    # A resume after the same scrub is a no-op verification.
    second = run_crypto_ten_symbol_factor_research_full_scrub(
        output_root=output_root
    )
    assert second["status"] == "scrubbed"


def test_digest_mismatch_sidecar_cuts_segment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_root = _accumulate(monkeypatch, tmp_path, 2)
    store = CryptoTenSymbolObservationStore(output_root)
    first_sidecar = store.read_bars_sidecar(iso(WINDOW_END))
    assert first_sidecar is not None
    second_path = store.bars_sidecar_path(iso(WINDOW_END + timedelta(minutes=5)))
    second_path.unlink()
    second_path.write_text(
        json.dumps(first_sidecar, ensure_ascii=True, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    result = run_crypto_ten_symbol_factor_research_full_scrub(
        output_root=output_root
    )

    assert result["observation_count"] == 2
    assert result["ineligible_slot_count"] == 1
    checkpoints = _checkpoints(output_root)
    assert checkpoints[1]["projection_outcome"] == "sidecar_ineligible"
    assert checkpoints[1]["ineligible_reason"] == "sidecar_digest_mismatch"
    assert len(_records(output_root)) == 1


def test_labels_settle_only_within_a_segment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_root = _accumulate(monkeypatch, tmp_path, 14)

    result = run_crypto_ten_symbol_factor_research_full_scrub(
        output_root=output_root
    )

    assert result["status"] == "recovered"
    assert result["label_count"] == 20
    assert result["label_learning_eligible_sample_count"] == 20
    assert len(result["label_learning_eligible_observation_ids"]) == 2
    report = result["hypothesis_report"]
    assert report["sample_count"] == 20
    assert report["feature_set_id"] == TEN_SYMBOL_FACTOR_SET_ID
    assert {item["screening_sample_met"] for item in report["hypotheses"]} == {False}
    assert {item["strategy_edge_established"] for item in report["hypotheses"]} == {
        False
    }
    labels = _labels(output_root)
    assert len(labels) == 20
    assert {label["horizon_minutes"] for label in labels} == {60}
    assert {label["symbol"] for label in labels} == set(OBSERVATION_SYMBOLS)
    label = labels[0]
    _assert_recursive_non_authority(label)
    assert label["feature_set_id"] == TEN_SYMBOL_FACTOR_SET_ID
    assert label["feature_set_version"] == TEN_SYMBOL_FACTOR_SET_VERSION
    records = _records(output_root)
    by_slot = {record["market_slot"]: record for record in records}
    source_record = next(
        record
        for record in records
        if record["observation_id"] == label["observation_id"]
    )
    symbol = label["symbol"]
    assert label["source_factor_snapshot_sha256"] == (
        source_record["snapshots"][symbol]["factor_snapshot_sha256"]
    )
    assert label["entry_price"] == source_record["label_anchor_prices"][symbol]
    target_slot = iso(
        datetime.fromisoformat(label["future_market_slot"].replace("Z", "+00:00"))
    )
    assert label["exit_price"] == by_slot[target_slot]["label_anchor_prices"][symbol]
    assert by_slot[target_slot]["segment_id"] == source_record["segment_id"]


def test_labels_never_cross_a_missing_sidecar_segment_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_root = _accumulate(monkeypatch, tmp_path, 14)
    store = CryptoTenSymbolObservationStore(output_root)
    # Slot 13 (the +60m label target of slot 1) loses its sidecar.
    store.bars_sidecar_path(iso(WINDOW_END + timedelta(minutes=60))).unlink()

    result = run_crypto_ten_symbol_factor_research_full_scrub(
        output_root=output_root
    )

    assert result["ineligible_slot_count"] == 1
    assert result["label_count"] == 0
    assert result["label_learning_eligible_sample_count"] == 0
    assert _labels(output_root) == []


def test_full_scrub_fails_closed_for_tampered_record_and_missing_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_root = _accumulate(monkeypatch, tmp_path, 2)
    run_crypto_ten_symbol_factor_research_full_scrub(output_root=output_root)
    record = next(
        (output_root / "evolution" / "ten_symbol_factor_research" / "records").glob(
            "*.json"
        )
    )
    record.write_text("{}\n", encoding="utf-8")

    with pytest.raises(
        CryptoTenSymbolFactorProjectionError,
        match="record_invalid|not_derived",
    ):
        run_crypto_ten_symbol_factor_research_full_scrub(output_root=output_root)

    second_root = tmp_path / "second"
    second_root.mkdir()
    monkeypatch.undo()
    token_file, second_output = _runtime_paths(monkeypatch, second_root)
    for index in range(2):
        end = WINDOW_END + index * timedelta(minutes=5)
        receipt = _run(
            second_root,
            token_file,
            second_output,
            now=end + timedelta(seconds=55),
            transport_factory=_factory(TenSymbolFixtureTransport()),
        )
        assert receipt["status"] == "completed"
    run_crypto_ten_symbol_factor_research_full_scrub(output_root=second_output)
    receipt_path = next(
        (second_output / "evolution" / "ten_symbol_factor_research" / "receipts").glob(
            "*.json"
        )
    )
    receipt_path.unlink()

    with pytest.raises(
        CryptoTenSymbolFactorProjectionError,
        match="claimed_record_missing",
    ):
        run_crypto_ten_symbol_factor_research_full_scrub(output_root=second_output)


def test_checkpoint_chain_tamper_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_root = _accumulate(monkeypatch, tmp_path, 2)
    run_crypto_ten_symbol_factor_research_full_scrub(output_root=output_root)
    checkpoints_dir = (
        output_root / "evolution" / "ten_symbol_factor_research" / "checkpoints"
    )
    second = sorted(checkpoints_dir.glob("*.json"))[1]
    payload = json.loads(second.read_text(encoding="utf-8"))
    payload["observation_id"] = "tampered"
    second.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    with pytest.raises(
        CryptoTenSymbolFactorProjectionError,
        match="checkpoint_invalid",
    ):
        run_crypto_ten_symbol_factor_research_full_scrub(output_root=output_root)


def test_incremental_requires_scrub_then_projects_one_slot_at_a_time(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token_file, output_root = _runtime_paths(monkeypatch, tmp_path)
    for index in range(2):
        end = WINDOW_END + index * timedelta(minutes=5)
        receipt = _run(
            tmp_path,
            token_file,
            output_root,
            now=end + timedelta(seconds=55),
            transport_factory=_factory(TenSymbolFixtureTransport()),
        )
        assert receipt["status"] == "completed"
    before = _store_bytes(output_root)

    deferred = run_crypto_ten_symbol_factor_research_incremental(
        output_root=output_root
    )
    assert deferred["status"] == "full_scrub_required"
    assert deferred["label_count"] == 0
    assert _store_bytes(output_root) == before

    run_crypto_ten_symbol_factor_research_full_scrub(output_root=output_root)
    up_to_date = run_crypto_ten_symbol_factor_research_incremental(
        output_root=output_root
    )
    assert up_to_date["status"] == "up_to_date"
    assert ten_symbol_factor_projection_exit_code(up_to_date) == 0

    third_end = WINDOW_END + timedelta(minutes=10)
    receipt = _run(
        tmp_path,
        token_file,
        output_root,
        now=third_end + timedelta(seconds=55),
        transport_factory=_factory(TenSymbolFixtureTransport()),
    )
    assert receipt["status"] == "completed"
    projected = run_crypto_ten_symbol_factor_research_incremental(
        output_root=output_root
    )
    assert projected["status"] == "projected_incremental"
    assert projected["label_count"] == 0
    assert projected["label_status"] == "observation_only_pending_daily_scrub"
    assert ten_symbol_factor_projection_exit_code(projected) == 0
    assert len(_records(output_root)) == 3
    assert len(_checkpoints(output_root)) == 3
    again = run_crypto_ten_symbol_factor_research_incremental(
        output_root=output_root
    )
    assert again["status"] == "up_to_date"

    for index in (3, 4):
        end = WINDOW_END + index * timedelta(minutes=5)
        receipt = _run(
            tmp_path,
            token_file,
            output_root,
            now=end + timedelta(seconds=55),
            transport_factory=_factory(TenSymbolFixtureTransport()),
        )
        assert receipt["status"] == "completed"
    backlog = run_crypto_ten_symbol_factor_research_incremental(
        output_root=output_root
    )
    assert backlog["status"] == "full_scrub_required"
    assert backlog["reason"] == "ten_symbol_factor_projection_incremental_backlog"
    assert _store_bytes(output_root) != before

    scrubbed = run_crypto_ten_symbol_factor_research_full_scrub(
        output_root=output_root
    )
    assert scrubbed["observation_count"] == 5
    assert len(_checkpoints(output_root)) == 5


def test_incremental_projects_ineligible_slot_without_record(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_root = _accumulate(monkeypatch, tmp_path, 2)
    run_crypto_ten_symbol_factor_research_full_scrub(output_root=output_root)
    token_file = tmp_path / "tradingdatas-crypto-read.token"
    third_end = WINDOW_END + timedelta(minutes=10)
    receipt = _run(
        tmp_path,
        token_file,
        output_root,
        now=third_end + timedelta(seconds=55),
        transport_factory=_factory(TenSymbolFixtureTransport()),
    )
    assert receipt["status"] == "completed"
    store = CryptoTenSymbolObservationStore(output_root)
    store.bars_sidecar_path(iso(third_end)).unlink()

    projected = run_crypto_ten_symbol_factor_research_incremental(
        output_root=output_root
    )

    assert projected["status"] == "projected_incremental"
    assert len(_records(output_root)) == 2
    checkpoints = _checkpoints(output_root)
    assert len(checkpoints) == 3
    assert checkpoints[2]["projection_outcome"] == "sidecar_ineligible"
    assert checkpoints[2]["ineligible_reason"] == "sidecar_missing"


def test_projection_defers_while_observation_pending(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_root = _accumulate(monkeypatch, tmp_path, 1)
    run_crypto_ten_symbol_factor_research_full_scrub(output_root=output_root)
    store = CryptoTenSymbolObservationStore(output_root)
    profile_sha256 = store.events()[0]["profile_sha256"]
    store.set_pending(
        {
            "window_end": iso(WINDOW_END + timedelta(minutes=5)),
            "observation_cutoff": iso(WINDOW_END + timedelta(minutes=5, seconds=55)),
            "profile_sha256": profile_sha256,
            "catalog_version": CATALOG_VERSION,
        }
    )

    with pytest.raises(
        CryptoTenSymbolFactorProjectionError,
        match="core_pending",
    ):
        run_crypto_ten_symbol_factor_research_incremental(output_root=output_root)
    deferred = run_crypto_ten_symbol_factor_research_full_scrub(
        output_root=output_root
    )
    assert deferred["status"] == "deferred_core_pending"
    assert ten_symbol_factor_projection_exit_code(deferred) == 0


def test_full_scrub_budget_deferral_is_retriable_and_deterministic(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    interrupted = _accumulate(monkeypatch, tmp_path, 3)
    clock = iter((0.0, 10**9))
    monkeypatch.setattr(research, "monotonic", lambda: next(clock))

    deferred = run_crypto_ten_symbol_factor_research_full_scrub(
        output_root=interrupted
    )

    assert deferred["status"] == "deferred_inventory_time_budget"
    assert deferred["inventory_complete"] is False
    assert ten_symbol_factor_projection_exit_code(deferred) == 0

    monkeypatch.setattr(research, "monotonic", lambda: 0.0)
    resumed = run_crypto_ten_symbol_factor_research_full_scrub(
        output_root=interrupted
    )
    assert resumed["status"] == "recovered"

    monkeypatch.undo()
    uninterrupted = _accumulate(monkeypatch, tmp_path / "plain", 3)
    completed = run_crypto_ten_symbol_factor_research_full_scrub(
        output_root=uninterrupted
    )
    assert completed["status"] == "recovered"
    assert _projection_files(interrupted) == _projection_files(uninterrupted)


def test_mid_scrub_budget_deferral_keeps_completed_prefix_retriable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_root = _accumulate(monkeypatch, tmp_path, 3)
    # Three eligibility reads plus the post-inventory check pass, then the
    # first record-loop deadline check trips.
    clock = iter((0.0, 0.0, 0.0, 0.0, 0.0, 10**9))
    monkeypatch.setattr(research, "monotonic", lambda: next(clock))

    deferred = run_crypto_ten_symbol_factor_research_full_scrub(
        output_root=output_root
    )

    assert deferred["status"] == "deferred_time_budget"
    assert deferred["verified_record_count"] == 0
    assert deferred["label_count"] == 0
    assert ten_symbol_factor_projection_exit_code(deferred) == 0

    monkeypatch.setattr(research, "monotonic", lambda: 0.0)
    resumed = run_crypto_ten_symbol_factor_research_full_scrub(
        output_root=output_root
    )
    assert resumed["status"] == "recovered"
    assert len(_checkpoints(output_root)) == 3


def test_real_trading_enabled_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_root = _accumulate(monkeypatch, tmp_path, 1)
    monkeypatch.setenv("REAL_TRADING_ENABLED", "true")

    with pytest.raises(Exception, match="real_trading"):
        run_crypto_ten_symbol_factor_research_full_scrub(output_root=output_root)
    with pytest.raises(Exception, match="real_trading"):
        run_crypto_ten_symbol_factor_research_incremental(output_root=output_root)
