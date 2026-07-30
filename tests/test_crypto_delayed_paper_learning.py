from __future__ import annotations

import copy
import hashlib
import inspect
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import Crypto.delayed_paper_epoch as epoch_module
import Crypto.delayed_paper_learning as learning_module
import Crypto.delayed_paper_runtime as runtime_module
import Crypto.delayed_paper_learning_worker as worker_module
from Crypto.delayed_paper_epoch import (
    load_crypto_delayed_paper_epoch_manifest,
    prepare_crypto_delayed_paper_epoch,
)
from Crypto.delayed_paper_learning import (
    CryptoDelayedPaperLearningError,
    project_crypto_delayed_paper_learning,
    recover_crypto_delayed_paper_learning,
    run_crypto_delayed_paper_learning_full_scrub,
    run_crypto_delayed_paper_learning_incremental,
)
from Crypto.delayed_paper_learning_worker import learning_worker_exit_code
from Crypto.delayed_paper_ledger import CryptoDelayedPaperObservationStore
from Crypto.delayed_paper_runtime import crypto_runtime_receipt_exit_code
from Crypto.delayed_paper_runner import run_crypto_delayed_paper_once
from Crypto.five_minute_data import (
    CryptoFiveMinuteWindowRequest,
    TradingDatasCryptoFiveMinuteDataPort,
)
from tests.test_crypto_5m_support import (
    BAR_DATASETS,
    RULE_DATASETS,
    SYMBOLS,
    WINDOW_END,
    FixtureTradingDatasTransport,
    bar_rows,
    client,
    iso,
    metadata,
    profile,
    window_request,
)


def _completed_result(tmp_path: Path) -> dict[str, Any]:
    transport = FixtureTradingDatasTransport()
    tradingdatas_client = client(transport)
    return run_crypto_delayed_paper_once(
        port=TradingDatasCryptoFiveMinuteDataPort(tradingdatas_client),
        profile=profile(tradingdatas_client),
        request=window_request(),
        output_root=tmp_path,
    )


def _shifted_completed_result(tmp_path: Path, minutes: int) -> dict[str, Any]:
    delta = timedelta(minutes=minutes)
    shifted = bar_rows()
    for row in shifted:
        for field_name in ("open_time", "close_time"):
            parsed = datetime.fromisoformat(str(row[field_name]).replace("Z", "+00:00"))
            row[field_name] = iso(parsed + delta)
    shifted_end = WINDOW_END + delta
    metadata_by_dataset: dict[str, dict[str, Any]] = {}
    for symbol in SYMBOLS:
        metadata_by_dataset[BAR_DATASETS[symbol]] = metadata(
            dataset_id=BAR_DATASETS[symbol],
            data_through=shifted_end - timedelta(milliseconds=1),
            observed_at=shifted_end + timedelta(seconds=20),
        )
        metadata_by_dataset[RULE_DATASETS[symbol]] = metadata(
            dataset_id=RULE_DATASETS[symbol],
            data_through=shifted_end + timedelta(seconds=5),
            observed_at=shifted_end + timedelta(seconds=10),
        )
    transport = FixtureTradingDatasTransport(
        bars=shifted,
        metadata_by_dataset=metadata_by_dataset,
    )
    tradingdatas_client = client(transport)
    return run_crypto_delayed_paper_once(
        port=TradingDatasCryptoFiveMinuteDataPort(tradingdatas_client),
        profile=profile(tradingdatas_client),
        request=CryptoFiveMinuteWindowRequest(
            window_end=shifted_end,
            observation_cutoff=shifted_end + timedelta(seconds=30),
        ),
        output_root=tmp_path,
    )


def _rows(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


def _projection_paths(
    root: Path,
    observation_id: str,
) -> tuple[Path, Path, Path, Path]:
    evolution = root / "evolution"
    return (
        evolution / "sample_journal" / f"{observation_id}.jsonl",
        evolution / "kpi_journal" / f"{observation_id}.jsonl",
        evolution / "challenger_suggestions" / f"{observation_id}.jsonl",
        evolution / "projection_receipts" / f"{observation_id}.json",
    )


def _learning_epoch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[Path, Path]:
    epoch_id = "crypto-delayed-paper-epoch-g2-20260729"
    archived_root = tmp_path / "crypto-delayed-paper"
    epoch_parent = tmp_path / "crypto-delayed-paper-epochs"
    output_root = epoch_parent / epoch_id
    manifest_path = tmp_path / "crypto-delayed-paper.epoch.json"
    archived_root.mkdir(mode=0o700)
    epoch_parent.mkdir(mode=0o700)
    monkeypatch.setattr(epoch_module, "LEGACY_ARCHIVE_ROOT", archived_root)
    monkeypatch.setattr(epoch_module, "EPOCH_ROOT_PARENT", epoch_parent)
    monkeypatch.setattr(epoch_module, "EPOCH_MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(
        worker_module,
        "PRODUCTION_EPOCH_MANIFEST",
        manifest_path,
    )
    payload = {
        "schema": "tradingagent.crypto.delayed_paper_epoch_manifest.v1",
        "epoch_id": epoch_id,
        "epoch_generation": 2,
        "current_output_root": str(output_root),
        "archived_output_root": str(archived_root),
        "archived_epoch_policy": "read_only_archive_no_resume",
        "capital_baseline_policy_id": "crypto-capital-v1",
        "aggregate_with_archived_epoch": False,
        "safety": {
            "real_trading_enabled": False,
            "production_eligible": False,
            "execution_authority": False,
            "testnet_enabled": False,
            "live_broker_enabled": False,
            "model_network_enabled": False,
            "automatic_promotion_enabled": False,
            "automatic_risk_expansion_enabled": False,
        },
    }
    manifest_path.write_text(
        json.dumps(
            payload,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    manifest_path.chmod(0o600)
    context = load_crypto_delayed_paper_epoch_manifest(manifest_path)
    prepare_crypto_delayed_paper_epoch(context)
    return manifest_path, output_root


def test_learning_projection_is_append_only_idempotent_and_non_authoritative(
    tmp_path: Path,
) -> None:
    result = _completed_result(tmp_path)

    first = project_crypto_delayed_paper_learning(
        result=result,
        output_root=tmp_path,
    )
    sample_path, kpi_path, challenger_path, receipt_path = _projection_paths(
        tmp_path,
        result["observation_id"],
    )
    bytes_before = {
        path.name: path.read_bytes()
        for path in (sample_path, kpi_path, challenger_path, receipt_path)
    }
    second = project_crypto_delayed_paper_learning(
        result=result,
        output_root=tmp_path,
    )

    assert first["status"] == second["status"] == "projected"
    assert first["sample_count"] == second["sample_count"] == 2
    assert len(_rows(sample_path)) == 2
    assert len(_rows(kpi_path)) == 1
    assert len(_rows(challenger_path)) == 1
    assert {
        path.name: path.read_bytes()
        for path in (sample_path, kpi_path, challenger_path, receipt_path)
    } == bytes_before

    for path in (sample_path, kpi_path, challenger_path):
        for row in _rows(path):
            assert row["execution_authority"] is False
            assert row["production_eligible"] is False
            assert row["real_trading_enabled"] is False
            assert row["promotion_authorized"] is False
            assert row["automatic_promotion_enabled"] is False
            assert row["automatic_risk_expansion_enabled"] is False
            assert row["live_transition_enabled"] is False
            assert row["model_network_used"] is False
            assert row["outbox_id"] is None
            assert row["capital_commit_id"] is None

    challenger = _rows(challenger_path)[0]
    assert challenger["suggestion"] == "collect_mature_labels_before_parameter_change"
    assert challenger["proposed_parameter_changes"] == []
    assert challenger["manual_review_required"] is True
    assert challenger["eligible_for_champion_replacement"] is False
    incremental = run_crypto_delayed_paper_learning_incremental(output_root=tmp_path)
    assert incremental["status"] == "projected"
    assert (tmp_path / "evolution" / "worker_state.json").is_file()
    assert not (tmp_path / "evolution" / "sample_journal.jsonl").exists()


def test_data_reject_does_not_create_false_learning_sample(tmp_path: Path) -> None:
    projection = project_crypto_delayed_paper_learning(
        result={
            "contract": "tradingagent.crypto.delayed_paper_runner.v1",
            "status": "data_reject",
            "reason_code": "crypto_5m_window_incomplete",
            "production_eligible": False,
            "execution_authority": False,
        },
        output_root=tmp_path,
    )

    assert projection == {
        "status": "skipped",
        "reason": "no_completed_observation",
        "execution_authority": False,
        "production_eligible": False,
        "promotion_authorized": False,
    }
    assert not (tmp_path / "evolution").exists()


def test_learning_journal_corruption_fails_closed(tmp_path: Path) -> None:
    result = _completed_result(tmp_path)
    project_crypto_delayed_paper_learning(result=result, output_root=tmp_path)
    sample_path, _, _, _ = _projection_paths(
        tmp_path,
        result["observation_id"],
    )
    sample_path.write_text("{not-json}\n", encoding="utf-8")

    with pytest.raises(
        CryptoDelayedPaperLearningError,
        match="learning_immutable_artifact_conflict",
    ):
        project_crypto_delayed_paper_learning(
            result=result,
            output_root=tmp_path,
        )


def test_learning_rejects_symlink_output_root(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)

    with pytest.raises(
        CryptoDelayedPaperLearningError,
        match="learning_output_root_symlink_forbidden",
    ):
        project_crypto_delayed_paper_learning(
            result={
                "contract": "tradingagent.crypto.delayed_paper_runner.v1",
                "status": "completed",
                "observation_id": "crypto-delayed-observation-test",
                "symbols": {},
            },
            output_root=linked,
        )


def test_learning_rejects_forged_memory_result_and_tampered_bundle(
    tmp_path: Path,
) -> None:
    result = _completed_result(tmp_path)
    forged = copy.deepcopy(result)
    first_symbol = sorted(forged["symbols"])[0]
    forged["symbols"][first_symbol]["bundle"]["sample_review"]["sample_id"] = (
        "forged-sample"
    )
    forged["symbols"][first_symbol]["bundle"]["sample_review"]["label_status"] = (
        "mature"
    )
    forged["symbols"][first_symbol]["counterfactual"]["label_status"] = "mature"

    with pytest.raises(
        CryptoDelayedPaperLearningError,
        match="learning_result_source_mismatch",
    ):
        project_crypto_delayed_paper_learning(
            result=forged,
            output_root=tmp_path,
        )

    run_id = result["symbols"][first_symbol]["bundle"]["run_id"]
    bundle_path = tmp_path / "runs" / f"{run_id}.json"
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    bundle["sample_review"]["sample_id"] = "tampered-on-disk"
    bundle_path.write_text(
        json.dumps(
            bundle,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        CryptoDelayedPaperLearningError,
        match="learning_source_bundle_invalid",
    ):
        recover_crypto_delayed_paper_learning(output_root=tmp_path)


def test_learning_rejects_rechecksummed_indexes_not_anchored_in_ledger(
    tmp_path: Path,
) -> None:
    result = _completed_result(tmp_path)
    observation_id = result["observation_id"]
    symbol = sorted(result["symbols"])[0]
    delayed_root = tmp_path / "delayed_paper"
    observation_index = (
        delayed_root / "observation_event_index" / observation_id / f"{symbol}.json"
    )
    row = json.loads(observation_index.read_text(encoding="utf-8"))
    row["counterfactual"] = {
        "forged": True,
        "label_status": "mature",
        "realized_return": "999",
    }
    material = dict(row)
    material.pop("checksum")
    row["checksum"] = hashlib.sha256(
        json.dumps(
            material,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    encoded = (
        json.dumps(
            row,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    )
    observation_index.write_text(encoded, encoding="utf-8")
    event_index = delayed_root / "event_index" / f"{row['event_id']}.json"
    event_index.write_text(encoded, encoding="utf-8")

    with pytest.raises(
        CryptoDelayedPaperLearningError,
        match="learning_source_completion_invalid",
    ):
        recover_crypto_delayed_paper_learning(output_root=tmp_path)


def test_learning_recovery_validates_filename_before_any_source_read(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    result = _completed_result(tmp_path)
    completion_dir = tmp_path / "delayed_paper" / "completions"
    malicious = completion_dir / "evil.json"
    malicious.write_text(
        '{"observation_id":"../../../outside"}\n',
        encoding="utf-8",
    )
    malicious.chmod(0o600)
    reads: list[Path] = []
    real_read = learning_module._read_json

    def recording_read(path: Path) -> dict[str, Any]:
        reads.append(path)
        return real_read(path)

    monkeypatch.setattr(learning_module, "_read_json", recording_read)
    with pytest.raises(
        CryptoDelayedPaperLearningError,
        match="learning_observation_id_invalid",
    ):
        recover_crypto_delayed_paper_learning(output_root=tmp_path)

    assert malicious not in reads
    assert all(tmp_path in path.parents or path == tmp_path for path in reads)
    assert result["observation_id"]


def test_partial_projection_recovers_missing_receipt_and_watermark_is_constant_time(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    result = _completed_result(tmp_path)
    real_write = learning_module._write_immutable_bytes
    writes = 0

    def crash_after_two_segments(*args: Any, **kwargs: Any) -> None:
        nonlocal writes
        writes += 1
        if writes == 3:
            raise OSError("fixture projection interruption")
        real_write(*args, **kwargs)

    monkeypatch.setattr(
        learning_module,
        "_write_immutable_bytes",
        crash_after_two_segments,
    )
    with pytest.raises(OSError, match="fixture projection interruption"):
        project_crypto_delayed_paper_learning(result=result, output_root=tmp_path)
    monkeypatch.setattr(
        learning_module,
        "_write_immutable_bytes",
        real_write,
    )

    recovered = recover_crypto_delayed_paper_learning(output_root=tmp_path)
    assert recovered["status"] == "recovered"
    assert recovered["recovered_observation_count"] == 1
    paths = _projection_paths(tmp_path, result["observation_id"])
    assert all(path.is_file() for path in paths)

    def should_not_reconstruct(*_: Any, **__: Any) -> tuple[dict[str, Any], str]:
        raise AssertionError("current watermark must skip historical reconstruction")

    monkeypatch.setattr(
        learning_module,
        "_result_for_recovery",
        should_not_reconstruct,
    )
    no_work = recover_crypto_delayed_paper_learning(output_root=tmp_path)
    assert no_work["status"] == "scrubbed"
    assert no_work["recovered_observation_count"] == 0


def test_learning_watermark_recovers_earlier_missing_projection(
    tmp_path: Path,
) -> None:
    first = _completed_result(tmp_path)
    second = _shifted_completed_result(tmp_path, 5)
    project_crypto_delayed_paper_learning(result=second, output_root=tmp_path)

    first_receipt = _projection_paths(tmp_path, first["observation_id"])[3]
    second_receipt = _projection_paths(tmp_path, second["observation_id"])[3]
    assert not first_receipt.exists()
    assert second_receipt.exists()

    recovered = recover_crypto_delayed_paper_learning(output_root=tmp_path)
    assert recovered["status"] == "recovered"
    assert recovered["recovered_observation_count"] == 2
    assert recovered["observation_ids"] == [
        first["observation_id"],
        second["observation_id"],
    ]
    assert first_receipt.exists()


def test_learning_watermark_revalidates_older_segments(
    tmp_path: Path,
) -> None:
    first = _completed_result(tmp_path)
    project_crypto_delayed_paper_learning(result=first, output_root=tmp_path)
    second = _shifted_completed_result(tmp_path, 5)
    project_crypto_delayed_paper_learning(result=second, output_root=tmp_path)
    first_sample = _projection_paths(tmp_path, first["observation_id"])[0]
    first_sample.write_text("{not-json}\n", encoding="utf-8")
    first_sample.chmod(0o600)

    with pytest.raises(
        CryptoDelayedPaperLearningError,
        match="learning_segment_invalid",
    ):
        recover_crypto_delayed_paper_learning(output_root=tmp_path)


def test_incremental_worker_requires_full_scrub_for_a_gap_then_becomes_current(
    tmp_path: Path,
) -> None:
    first = _completed_result(tmp_path)
    second = _shifted_completed_result(tmp_path, 5)

    incremental = run_crypto_delayed_paper_learning_incremental(output_root=tmp_path)
    assert incremental["status"] == "full_scrub_required"
    assert incremental["projected_completion_count"] == 0
    assert incremental["core_completion_count"] == 2
    assert learning_worker_exit_code(incremental) == 2

    scrub = run_crypto_delayed_paper_learning_full_scrub(output_root=tmp_path)
    assert scrub["status"] == "recovered"
    assert scrub["recovered_observation_count"] == 2
    assert scrub["observation_ids"] == [
        first["observation_id"],
        second["observation_id"],
    ]
    assert learning_worker_exit_code(scrub) == 0

    current = run_crypto_delayed_paper_learning_incremental(output_root=tmp_path)
    assert current["status"] == "current"
    assert current["projected_completion_count"] == 2


def test_incremental_worker_reads_only_bounded_new_projection_material(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _completed_result(tmp_path)
    first = run_crypto_delayed_paper_learning_incremental(output_root=tmp_path)
    assert first["status"] == "projected"

    real_read = learning_module._read_exact_bytes
    read_counts: list[int] = []
    for minutes in (5, 10, 15, 20):
        _shifted_completed_result(tmp_path, minutes)
        reads = 0

        def recording_read(*args: Any, **kwargs: Any) -> bytes:
            nonlocal reads
            reads += 1
            return real_read(*args, **kwargs)

        monkeypatch.setattr(
            learning_module,
            "_read_exact_bytes",
            recording_read,
        )
        projected = run_crypto_delayed_paper_learning_incremental(output_root=tmp_path)
        monkeypatch.setattr(
            learning_module,
            "_read_exact_bytes",
            real_read,
        )
        assert projected["status"] == "projected"
        read_counts.append(reads)

    # Sequence 1 -> 2 reads one extra predecessor checkpoint to anchor the
    # initial chain. Later runs remain constant and never grow with history.
    assert max(read_counts) - min(read_counts) <= 1
    assert read_counts[1:] == [18, 18, 18]
    assert max(read_counts) <= 18


def test_full_scrub_fails_closed_when_earlier_claimed_receipt_is_missing(
    tmp_path: Path,
) -> None:
    first = _completed_result(tmp_path)
    run_crypto_delayed_paper_learning_incremental(output_root=tmp_path)
    _shifted_completed_result(tmp_path, 5)
    run_crypto_delayed_paper_learning_incremental(output_root=tmp_path)
    first_receipt = _projection_paths(
        tmp_path,
        first["observation_id"],
    )[3]
    first_receipt.unlink()

    with pytest.raises(
        CryptoDelayedPaperLearningError,
        match="learning_claimed_projection_missing",
    ):
        run_crypto_delayed_paper_learning_full_scrub(output_root=tmp_path)


def test_full_scrub_fails_closed_when_older_claimed_segment_is_tampered(
    tmp_path: Path,
) -> None:
    first = _completed_result(tmp_path)
    run_crypto_delayed_paper_learning_incremental(output_root=tmp_path)
    _shifted_completed_result(tmp_path, 5)
    run_crypto_delayed_paper_learning_incremental(output_root=tmp_path)
    first_sample = _projection_paths(
        tmp_path,
        first["observation_id"],
    )[0]
    first_sample.write_text("{not-json}\n", encoding="utf-8")
    first_sample.chmod(0o600)

    with pytest.raises(
        CryptoDelayedPaperLearningError,
        match="learning_segment_invalid",
    ):
        run_crypto_delayed_paper_learning_full_scrub(output_root=tmp_path)


def test_full_scrub_fails_closed_when_checkpoint_chain_is_broken(
    tmp_path: Path,
) -> None:
    _completed_result(tmp_path)
    run_crypto_delayed_paper_learning_incremental(output_root=tmp_path)
    _shifted_completed_result(tmp_path, 5)
    run_crypto_delayed_paper_learning_incremental(output_root=tmp_path)
    checkpoint = (
        tmp_path / "evolution" / "incremental_checkpoints" / "000000000002.json"
    )
    row = json.loads(checkpoint.read_text(encoding="utf-8"))
    row["previous_checkpoint_sha256"] = "0" * 64
    checkpoint.write_text(
        json.dumps(
            row,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    checkpoint.chmod(0o600)

    with pytest.raises(
        CryptoDelayedPaperLearningError,
        match="learning_checkpoint_chain_invalid",
    ):
        run_crypto_delayed_paper_learning_full_scrub(output_root=tmp_path)


def test_incremental_rejects_rechecksummed_immediate_checkpoint_fork(
    tmp_path: Path,
) -> None:
    _completed_result(tmp_path)
    run_crypto_delayed_paper_learning_incremental(output_root=tmp_path)
    _shifted_completed_result(tmp_path, 5)
    run_crypto_delayed_paper_learning_incremental(output_root=tmp_path)
    evolution = tmp_path / "evolution"
    checkpoint_path = evolution / "incremental_checkpoints" / "000000000002.json"
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    checkpoint["previous_checkpoint_sha256"] = "0" * 64
    checkpoint.pop("checkpoint_sha256")
    checkpoint["checkpoint_sha256"] = learning_module._sha256(checkpoint)
    checkpoint_path.write_text(
        learning_module._canonical_json(checkpoint) + "\n",
        encoding="utf-8",
    )
    checkpoint_path.chmod(0o600)
    state_path = evolution / "worker_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["checkpoint_head_sha256"] = checkpoint["checkpoint_sha256"]
    state.pop("worker_state_sha256")
    state["worker_state_sha256"] = learning_module._sha256(state)
    state_path.write_text(
        learning_module._canonical_json(state) + "\n",
        encoding="utf-8",
    )
    state_path.chmod(0o600)

    with pytest.raises(
        CryptoDelayedPaperLearningError,
        match="learning_checkpoint_chain_invalid",
    ):
        run_crypto_delayed_paper_learning_incremental(output_root=tmp_path)


def test_full_scrub_rejects_newer_claim_with_earlier_completion_gap(
    tmp_path: Path,
) -> None:
    _completed_result(tmp_path)
    second = _shifted_completed_result(tmp_path, 5)
    project_crypto_delayed_paper_learning(
        result=second,
        output_root=tmp_path,
    )
    evolution_root = learning_module._ensure_learning_directories(tmp_path)
    store = CryptoDelayedPaperObservationStore(tmp_path)
    completion = learning_module._verified_completion_record(
        store=store,
        observation_id=second["observation_id"],
    )
    receipt = learning_module._verify_projection_receipt(
        evolution_root=evolution_root,
        observation_id=second["observation_id"],
    )
    learning_module._append_checkpoint(
        evolution_root=evolution_root,
        state=learning_module._worker_state_payload(checkpoint=None),
        completion=completion,
        receipt=receipt,
    )

    with pytest.raises(
        CryptoDelayedPaperLearningError,
        match="learning_checkpoint_completion_order_mismatch",
    ):
        run_crypto_delayed_paper_learning_full_scrub(output_root=tmp_path)


def test_full_scrub_rederives_projection_instead_of_trusting_rechecksums(
    tmp_path: Path,
) -> None:
    result = _completed_result(tmp_path)
    run_crypto_delayed_paper_learning_incremental(output_root=tmp_path)
    evolution = tmp_path / "evolution"
    challenger_path = _projection_paths(
        tmp_path,
        result["observation_id"],
    )[2]
    challenger = json.loads(challenger_path.read_text(encoding="utf-8"))
    challenger["eligible_for_champion_replacement"] = True
    challenger["proposed_parameter_changes"] = [
        {"field": "risk_budget", "value": "unbounded"}
    ]
    challenger.pop("checksum")
    challenger["checksum"] = learning_module._sha256(challenger)
    challenger_bytes = (learning_module._canonical_json(challenger) + "\n").encode(
        "utf-8"
    )
    challenger_path.write_bytes(challenger_bytes)
    challenger_path.chmod(0o600)

    receipt_path = _projection_paths(
        tmp_path,
        result["observation_id"],
    )[3]
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["challenger_segment_sha256"] = hashlib.sha256(challenger_bytes).hexdigest()
    receipt.pop("projection_receipt_sha256")
    receipt["projection_receipt_sha256"] = learning_module._sha256(receipt)
    receipt_path.write_text(
        learning_module._canonical_json(receipt) + "\n",
        encoding="utf-8",
    )
    receipt_path.chmod(0o600)

    checkpoint_path = evolution / "incremental_checkpoints" / "000000000001.json"
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    checkpoint["projection_receipt_sha256"] = receipt["projection_receipt_sha256"]
    checkpoint.pop("checkpoint_sha256")
    checkpoint["checkpoint_sha256"] = learning_module._sha256(checkpoint)
    checkpoint_path.write_text(
        learning_module._canonical_json(checkpoint) + "\n",
        encoding="utf-8",
    )
    checkpoint_path.chmod(0o600)
    state_path = evolution / "worker_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["latest_projection_receipt_sha256"] = receipt["projection_receipt_sha256"]
    state["checkpoint_head_sha256"] = checkpoint["checkpoint_sha256"]
    state.pop("worker_state_sha256")
    state["worker_state_sha256"] = learning_module._sha256(state)
    state_path.write_text(
        learning_module._canonical_json(state) + "\n",
        encoding="utf-8",
    )
    state_path.chmod(0o600)

    with pytest.raises(
        CryptoDelayedPaperLearningError,
        match="learning_projection_not_derived_from_core",
    ):
        run_crypto_delayed_paper_learning_full_scrub(output_root=tmp_path)


def test_full_scrub_repairs_checkpoint_written_before_worker_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _completed_result(tmp_path)
    real_write_state = learning_module._write_json_atomic

    def crash_before_state(*_: Any, **__: Any) -> None:
        raise OSError("fixture state interruption")

    monkeypatch.setattr(
        learning_module,
        "_write_json_atomic",
        crash_before_state,
    )
    with pytest.raises(OSError, match="fixture state interruption"):
        run_crypto_delayed_paper_learning_incremental(output_root=tmp_path)
    monkeypatch.setattr(
        learning_module,
        "_write_json_atomic",
        real_write_state,
    )

    scrub = run_crypto_delayed_paper_learning_full_scrub(output_root=tmp_path)
    assert scrub["status"] == "scrubbed"
    assert scrub["completion_count"] == 1
    assert (tmp_path / "evolution" / "worker_state.json").is_file()


def test_learning_failure_cannot_change_core_exit_capital_or_orders(
    tmp_path: Path,
) -> None:
    result = _completed_result(tmp_path)
    projected = run_crypto_delayed_paper_learning_incremental(output_root=tmp_path)
    assert projected["status"] == "projected"
    protected = {
        str(path.relative_to(tmp_path)): path.read_bytes()
        for directory in ("capital", "orders", "runs")
        for path in (tmp_path / directory).rglob("*")
        if path.is_file()
    }
    sample = _projection_paths(
        tmp_path,
        result["observation_id"],
    )[0]
    sample.write_text("{not-json}\n", encoding="utf-8")
    sample.chmod(0o600)

    with pytest.raises(CryptoDelayedPaperLearningError):
        run_crypto_delayed_paper_learning_full_scrub(output_root=tmp_path)

    assert {
        str(path.relative_to(tmp_path)): path.read_bytes()
        for directory in ("capital", "orders", "runs")
        for path in (tmp_path / directory).rglob("*")
        if path.is_file()
    } == protected
    assert (
        crypto_runtime_receipt_exit_code(
            {
                "status": "completed",
                "backlog_remaining": False,
            }
        )
        == 0
    )
    assert (
        learning_worker_exit_code(
            {
                "status": "failed",
                "learning_mode": "detached_offline_worker",
                "execution_authority": False,
                "production_eligible": False,
                "real_trading_enabled": False,
                "automatic_promotion_enabled": False,
                "automatic_risk_expansion_enabled": False,
            }
        )
        == 2
    )
    source = inspect.getsource(runtime_module)
    assert "delayed_paper_learning" not in source
    assert "evolution" not in source


def test_learning_worker_cli_is_path_pinned_and_emits_non_authority(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    manifest_path, output_root = _learning_epoch(monkeypatch, tmp_path)

    assert (
        worker_module.main(
            [
                "--mode",
                "incremental",
                "--epoch-manifest",
                str(manifest_path),
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "none"
    assert payload["learning_mode"] == "detached_offline_worker"
    assert payload["execution_authority"] is False
    assert payload["production_eligible"] is False
    assert payload["outbox_id"] is None
    assert payload["capital_commit_id"] is None
    assert payload["epoch_id"] == "crypto-delayed-paper-epoch-g2-20260729"
    assert payload["epoch_generation"] == 2
    assert payload["epoch_output_root"] == str(output_root)
    assert payload["aggregate_with_archived_epoch"] is False

    with pytest.raises(SystemExit, match="2"):
        worker_module.main(
            [
                "--mode",
                "incremental",
                "--output-root",
                str(tmp_path / "different-output"),
            ]
        )
    assert not (tmp_path / "different-output").exists()
    assert "unrecognized arguments" in capsys.readouterr().err


def test_learning_worker_rejects_stale_current_epoch_anchor(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    manifest_path, output_root = _learning_epoch(monkeypatch, tmp_path)
    current_path = output_root.parent / ".current_epoch.json"
    current = json.loads(current_path.read_text(encoding="utf-8"))
    current["epoch_id"] = "crypto-delayed-paper-epoch-g2-stale"
    current_path.write_text(
        json.dumps(
            current,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    assert (
        worker_module.main(
            [
                "--mode",
                "incremental",
                "--epoch-manifest",
                str(manifest_path),
            ]
        )
        == 2
    )
    assert "failed closed" in capsys.readouterr().err
    assert not (output_root / "evolution").exists()
