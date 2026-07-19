from __future__ import annotations

import json
import hashlib
import os
import stat
from pathlib import Path

import pytest

from shared.runtime.day_loop import ConcurrentRunUpdate
from shared.runtime.file_store import FileRunBundleStore, RunBundleStoreCorruption
from shared.runtime.run_bundle import (
    ComponentIdentity,
    RunBundle,
    RunContext,
    RunStage,
    STAGE_ORDER,
    StageReceipt,
)


def _digest(character: str) -> str:
    return character * 64


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _context() -> RunContext:
    return RunContext(
        trade_date="2026-07-16",
        market="ashare",
        authority_id="ashare-capital-v1",
        authority_generation=1,
        execution_lineage="ashare-sim-fixture-v1",
        account_type="simulated",
        real_trading_enabled=False,
        champion_manifest_sha256=_digest("c"),
    )


def _initial_bundle() -> RunBundle:
    components = tuple(
        ComponentIdentity(
            stage=stage,
            component_id=f"fixture-{stage.value}",
            version="1",
            artifact_sha256=_digest(str(index + 1)),
        )
        for index, stage in enumerate(STAGE_ORDER)
    ) + (
        ComponentIdentity(
            stage=None,
            component_id="mainboard-scope",
            version="1",
            artifact_sha256=_digest("a"),
        ),
    )
    return RunBundle.create(_context(), components)


def _after_preopen(bundle: RunBundle) -> RunBundle:
    component = bundle.component_for(RunStage.PREOPEN)
    idempotency_key = _canonical_sha256(
        {
            "run_id": bundle.run_id,
            "stage": RunStage.PREOPEN.value,
            "input_bundle_sha256": bundle.bundle_sha256,
            "component_id": component.component_id,
            "component_version": component.version,
            "component_artifact_sha256": component.artifact_sha256,
        }
    )
    receipt = StageReceipt.create(
        stage=RunStage.PREOPEN,
        status="completed",
        idempotency_key=idempotency_key,
        component=component,
        input_bundle_sha256=bundle.bundle_sha256,
        payload={
            "market": "ashare",
            "account_type": "simulated",
            "real_trading_enabled": False,
            "account_authority_valid": True,
            "position_authority_valid": True,
        },
        reason_codes=(),
    )
    return bundle.append(
        receipt,
        stop_new_risk=False,
        position_authority_valid=True,
        block_reasons=(),
        permitted_order_ids=None,
    )


def _event_files(root: Path, run_id: str) -> list[Path]:
    event_dir = root / f"{run_id}.events"
    if not event_dir.exists():
        return []
    return sorted(event_dir.glob("[0-9]" * 20 + ".json"))


def test_file_store_survives_new_process_instance_and_is_append_only(
    tmp_path: Path,
) -> None:
    root = tmp_path / "run-bundles"
    initial = _initial_bundle()
    advanced = _after_preopen(initial)

    store = FileRunBundleStore(root)
    store.compare_and_swap(
        run_id=initial.run_id,
        expected_bundle_sha256=None,
        bundle=initial,
    )
    store.compare_and_swap(
        run_id=initial.run_id,
        expected_bundle_sha256=initial.bundle_sha256,
        bundle=advanced,
    )

    recovered = FileRunBundleStore(root).load(initial.run_id)
    assert recovered == advanced
    event_files = _event_files(root, initial.run_id)
    assert [path.name for path in event_files] == [
        "00000000000000000000.json",
        "00000000000000000001.json",
    ]
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o400 for path in event_files)
    assert (
        json.loads(event_files[0].read_text(encoding="utf-8"))["previous_bundle_sha256"]
        is None
    )
    assert (
        json.loads(event_files[1].read_text(encoding="utf-8"))["previous_bundle_sha256"]
        == initial.bundle_sha256
    )


def test_replayed_compare_and_swap_is_idempotent_but_conflict_fails(
    tmp_path: Path,
) -> None:
    store = FileRunBundleStore(tmp_path / "run-bundles")
    initial = _initial_bundle()
    advanced = _after_preopen(initial)
    store.compare_and_swap(
        run_id=initial.run_id,
        expected_bundle_sha256=None,
        bundle=initial,
    )
    store.compare_and_swap(
        run_id=initial.run_id,
        expected_bundle_sha256=initial.bundle_sha256,
        bundle=advanced,
    )
    store.compare_and_swap(
        run_id=initial.run_id,
        expected_bundle_sha256=initial.bundle_sha256,
        bundle=advanced,
    )

    with pytest.raises(ConcurrentRunUpdate):
        store.compare_and_swap(
            run_id=initial.run_id,
            expected_bundle_sha256=None,
            bundle=advanced,
        )
    assert len(_event_files(tmp_path / "run-bundles", initial.run_id)) == 2


def test_corruption_and_symlink_paths_fail_closed(tmp_path: Path) -> None:
    root = tmp_path / "run-bundles"
    store = FileRunBundleStore(root)
    initial = _initial_bundle()
    store.compare_and_swap(
        run_id=initial.run_id,
        expected_bundle_sha256=None,
        bundle=initial,
    )
    with (root / f"{initial.run_id}.jsonl").open("a", encoding="utf-8") as handle:
        handle.write('{"truncated":')
    with pytest.raises(RunBundleStoreCorruption):
        FileRunBundleStore(root).load(initial.run_id)

    target = tmp_path / "target"
    target.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(target, target_is_directory=True)
    with pytest.raises(RunBundleStoreCorruption):
        FileRunBundleStore(linked).load(initial.run_id)


def test_short_write_never_becomes_an_authoritative_partial_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "run-bundles"
    store = FileRunBundleStore(root)
    initial = _initial_bundle()
    advanced = _after_preopen(initial)
    store.compare_and_swap(
        run_id=initial.run_id,
        expected_bundle_sha256=None,
        bundle=initial,
    )

    original_write = os.write

    def _short_write(fd: int, payload: bytes) -> int:
        prefix = payload[: max(1, len(payload) // 3)]
        return original_write(fd, prefix)

    monkeypatch.setattr(os, "write", _short_write)
    with pytest.raises(
        RunBundleStoreCorruption,
        match="run_bundle_store_short_write",
    ):
        store.compare_and_swap(
            run_id=initial.run_id,
            expected_bundle_sha256=initial.bundle_sha256,
            bundle=advanced,
        )
    monkeypatch.setattr(os, "write", original_write)

    assert FileRunBundleStore(root).load(initial.run_id) == initial
    assert len(_event_files(root, initial.run_id)) == 1


def test_valid_legacy_jsonl_recovers_and_new_events_continue_atomically(
    tmp_path: Path,
) -> None:
    initial = _initial_bundle()
    advanced = _after_preopen(initial)
    seed_root = tmp_path / "seed"
    FileRunBundleStore(seed_root).compare_and_swap(
        run_id=initial.run_id,
        expected_bundle_sha256=None,
        bundle=initial,
    )
    seed_jsonl = seed_root / f"{initial.run_id}.jsonl"
    if seed_jsonl.exists():
        legacy_event = seed_jsonl.read_text(encoding="utf-8")
    else:
        legacy_event = _event_files(seed_root, initial.run_id)[0].read_text(
            encoding="utf-8"
        )

    root = tmp_path / "legacy-run-bundles"
    root.mkdir()
    (root / f"{initial.run_id}.jsonl").write_text(
        legacy_event,
        encoding="utf-8",
    )
    store = FileRunBundleStore(root)
    assert store.load(initial.run_id) == initial

    store.compare_and_swap(
        run_id=initial.run_id,
        expected_bundle_sha256=initial.bundle_sha256,
        bundle=advanced,
    )

    assert FileRunBundleStore(root).load(initial.run_id) == advanced
    assert [path.name for path in _event_files(root, initial.run_id)] == [
        "00000000000000000001.json"
    ]


def test_atomic_publish_fsyncs_event_file_and_parent_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "run-bundles"
    observed_modes: list[int] = []
    original_fsync = os.fsync

    def _recording_fsync(fd: int) -> None:
        observed_modes.append(os.fstat(fd).st_mode)
        original_fsync(fd)

    monkeypatch.setattr(os, "fsync", _recording_fsync)
    initial = _initial_bundle()
    FileRunBundleStore(root).compare_and_swap(
        run_id=initial.run_id,
        expected_bundle_sha256=None,
        bundle=initial,
    )

    assert any(stat.S_ISREG(mode) for mode in observed_modes)
    assert any(stat.S_ISDIR(mode) for mode in observed_modes)
