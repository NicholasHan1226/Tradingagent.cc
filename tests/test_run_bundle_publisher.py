from __future__ import annotations

import importlib
import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

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


def _bundle(
    *,
    trade_date: str = "2026-07-16",
    execution_lineage: str = "ashare-sim-publisher-fixture-v1",
) -> RunBundle:
    context = RunContext(
        trade_date=trade_date,
        market="ashare",
        authority_id="ashare-capital-v1",
        authority_generation=1,
        execution_lineage=execution_lineage,
        account_type="simulated",
        real_trading_enabled=False,
        champion_manifest_sha256=_digest("c"),
    )
    components = tuple(
        ComponentIdentity(
            stage=stage,
            component_id=f"fixture-{stage.value}",
            version="1",
            artifact_sha256=_digest(str(index + 1)),
        )
        for index, stage in enumerate(STAGE_ORDER)
    )
    return RunBundle.create(context, components)


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


def test_publishes_full_local_candidate_projection_to_immutable_and_latest(
    tmp_path: Path,
) -> None:
    publisher_module = importlib.import_module("shared.runtime.publisher")
    root = tmp_path / "published"
    bundle = _bundle()

    result = publisher_module.LocalRunBundlePublisher(root).publish(bundle)

    immutable = root / "runs" / bundle.run_id / f"{bundle.bundle_sha256}.json"
    latest = root / "latest.json"
    assert result.immutable_path == immutable
    assert result.latest_path == latest
    assert result.idempotent is False
    assert immutable.read_bytes() == latest.read_bytes()
    projection = json.loads(latest.read_text(encoding="utf-8"))
    assert projection["run_id"] == bundle.run_id
    assert projection["stage_receipts"] == []
    assert projection["_projection"] == {
        "authority": "non_authority",
        "bundle_sha256": bundle.bundle_sha256,
        "environment": "local_candidate",
        "production_verified": False,
        "record_type": "run_bundle_projection",
        "schema_version": 1,
    }


def test_republishing_same_bundle_is_idempotent_without_rewriting_files(
    tmp_path: Path,
) -> None:
    publisher_module = importlib.import_module("shared.runtime.publisher")
    root = tmp_path / "published"
    bundle = _bundle()
    publisher = publisher_module.LocalRunBundlePublisher(root)
    first = publisher.publish(bundle)
    immutable_stat = first.immutable_path.stat()
    latest_stat = first.latest_path.stat()

    second = publisher.publish(bundle)

    assert second.idempotent is True
    assert second.immutable_path.stat().st_ino == immutable_stat.st_ino
    assert second.immutable_path.stat().st_mtime_ns == immutable_stat.st_mtime_ns
    assert second.latest_path.stat().st_ino == latest_stat.st_ino
    assert second.latest_path.stat().st_mtime_ns == latest_stat.st_mtime_ns


def test_conflicting_immutable_projection_fails_without_changing_latest(
    tmp_path: Path,
) -> None:
    publisher_module = importlib.import_module("shared.runtime.publisher")
    root = tmp_path / "published"
    bundle = _bundle()
    first = publisher_module.LocalRunBundlePublisher(root).publish(bundle)
    trusted_latest = first.latest_path.read_bytes()
    first.immutable_path.write_text('{"tampered":true}\n', encoding="utf-8")

    with pytest.raises(
        publisher_module.RunBundlePublishError,
        match="immutable_projection_conflict",
    ):
        publisher_module.LocalRunBundlePublisher(root).publish(bundle)

    assert first.latest_path.read_bytes() == trusted_latest
    assert first.immutable_path.read_text(encoding="utf-8") == '{"tampered":true}\n'


def test_latest_rejects_a_same_run_rollback_and_preserves_newer_projection(
    tmp_path: Path,
) -> None:
    publisher_module = importlib.import_module("shared.runtime.publisher")
    publisher = publisher_module.LocalRunBundlePublisher(tmp_path / "published")
    initial = _bundle()
    advanced = _after_preopen(initial)
    publisher.publish(initial)
    latest = publisher.publish(advanced).latest_path
    trusted_latest = latest.read_bytes()

    with pytest.raises(
        publisher_module.RunBundlePublishError,
        match="latest_projection_rollback",
    ):
        publisher.publish(initial)

    assert latest.read_bytes() == trusted_latest


def test_latest_rejects_a_competing_run_for_the_same_trade_date(
    tmp_path: Path,
) -> None:
    publisher_module = importlib.import_module("shared.runtime.publisher")
    root = tmp_path / "published"
    publisher = publisher_module.LocalRunBundlePublisher(root)
    accepted = _bundle()
    competing = _bundle(execution_lineage="ashare-sim-competing-v1")
    latest = publisher.publish(accepted).latest_path
    trusted_latest = latest.read_bytes()

    with pytest.raises(
        publisher_module.RunBundlePublishError,
        match="latest_projection_competing_run",
    ):
        publisher.publish(competing)

    assert latest.read_bytes() == trusted_latest
    assert not (
        root / "runs" / competing.run_id / f"{competing.bundle_sha256}.json"
    ).exists()


def test_symlinked_publish_root_fails_closed_without_writing_target(
    tmp_path: Path,
) -> None:
    publisher_module = importlib.import_module("shared.runtime.publisher")
    target = tmp_path / "target"
    target.mkdir()
    linked_root = tmp_path / "linked"
    linked_root.symlink_to(target, target_is_directory=True)

    with pytest.raises(
        publisher_module.RunBundlePublishError,
        match="run_bundle_publish_symlink_forbidden",
    ):
        publisher_module.LocalRunBundlePublisher(linked_root).publish(_bundle())

    assert list(target.iterdir()) == []


def test_symlinked_runs_directory_fails_closed_without_writing_target(
    tmp_path: Path,
) -> None:
    publisher_module = importlib.import_module("shared.runtime.publisher")
    root = tmp_path / "published"
    root.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    (root / "runs").symlink_to(target, target_is_directory=True)

    with pytest.raises(
        publisher_module.RunBundlePublishError,
        match="run_bundle_publish_symlink_forbidden",
    ):
        publisher_module.LocalRunBundlePublisher(root).publish(_bundle())

    assert list(target.iterdir()) == []


def test_hardlinked_latest_fails_closed_without_touching_link_target(
    tmp_path: Path,
) -> None:
    publisher_module = importlib.import_module("shared.runtime.publisher")
    root = tmp_path / "published"
    publisher = publisher_module.LocalRunBundlePublisher(root)
    initial = _bundle()
    advanced = _after_preopen(initial)
    latest = publisher.publish(initial).latest_path
    trusted = latest.read_bytes()
    latest.unlink()
    victim = tmp_path / "victim.json"
    victim.write_bytes(trusted)
    os.link(victim, latest)

    with pytest.raises(
        publisher_module.RunBundlePublishError,
        match="run_bundle_publish_hardlink_forbidden",
    ):
        publisher.publish(advanced)

    assert victim.read_bytes() == trusted
    assert latest.read_bytes() == trusted
    assert victim.stat().st_ino == latest.stat().st_ino


def test_symlinked_latest_fails_closed_without_touching_link_target(
    tmp_path: Path,
) -> None:
    publisher_module = importlib.import_module("shared.runtime.publisher")
    root = tmp_path / "published"
    publisher = publisher_module.LocalRunBundlePublisher(root)
    initial = _bundle()
    advanced = _after_preopen(initial)
    latest = publisher.publish(initial).latest_path
    trusted = latest.read_bytes()
    latest.unlink()
    victim = tmp_path / "victim.json"
    victim.write_bytes(trusted)
    latest.symlink_to(victim)

    with pytest.raises(
        publisher_module.RunBundlePublishError,
        match="run_bundle_publish_symlink_forbidden",
    ):
        publisher.publish(advanced)

    assert victim.read_bytes() == trusted
    assert latest.is_symlink()


def test_hardlinked_immutable_path_fails_closed_without_touching_target(
    tmp_path: Path,
) -> None:
    publisher_module = importlib.import_module("shared.runtime.publisher")
    root = tmp_path / "published"
    bundle = _bundle()
    run_root = root / "runs" / bundle.run_id
    run_root.mkdir(parents=True)
    victim = tmp_path / "victim.json"
    victim.write_text('{"do_not_touch":true}\n', encoding="utf-8")
    immutable = run_root / f"{bundle.bundle_sha256}.json"
    os.link(victim, immutable)

    with pytest.raises(
        publisher_module.RunBundlePublishError,
        match="run_bundle_publish_hardlink_forbidden",
    ):
        publisher_module.LocalRunBundlePublisher(root).publish(bundle)

    assert victim.read_text(encoding="utf-8") == '{"do_not_touch":true}\n'
    assert immutable.stat().st_ino == victim.stat().st_ino
    assert not (root / "latest.json").exists()


def test_requires_explicit_root_and_a_revalidated_run_bundle(tmp_path: Path) -> None:
    publisher_module = importlib.import_module("shared.runtime.publisher")
    with pytest.raises(ValueError, match="explicitly configured"):
        publisher_module.LocalRunBundlePublisher("")
    with pytest.raises(TypeError):
        publisher_module.LocalRunBundlePublisher()

    publisher = publisher_module.LocalRunBundlePublisher(tmp_path / "published")
    with pytest.raises(
        publisher_module.RunBundlePublishError,
        match="requires_validated_bundle",
    ):
        publisher.publish({"run_id": "../escape"})

    tampered = _bundle()
    object.__setattr__(tampered, "contract_id", "unsafe.contract")
    with pytest.raises(
        publisher_module.RunBundlePublishError,
        match="validation_failed",
    ):
        publisher.publish(tampered)

    assert not (tmp_path / "escape").exists()


def test_latest_advances_by_trade_date_but_rejects_date_rollback(
    tmp_path: Path,
) -> None:
    publisher_module = importlib.import_module("shared.runtime.publisher")
    publisher = publisher_module.LocalRunBundlePublisher(tmp_path / "published")
    first = _bundle(trade_date="2026-07-16")
    next_day = _bundle(trade_date="2026-07-17")
    publisher.publish(first)

    latest = publisher.publish(next_day).latest_path
    trusted_latest = latest.read_bytes()
    assert json.loads(trusted_latest)["run_id"] == next_day.run_id

    with pytest.raises(
        publisher_module.RunBundlePublishError,
        match="latest_projection_date_rollback",
    ):
        publisher.publish(first)

    assert latest.read_bytes() == trusted_latest


def test_noncanonical_latest_conflict_is_not_silently_repaired(
    tmp_path: Path,
) -> None:
    publisher_module = importlib.import_module("shared.runtime.publisher")
    publisher = publisher_module.LocalRunBundlePublisher(tmp_path / "published")
    initial = _bundle()
    advanced = _after_preopen(initial)
    latest = publisher.publish(initial).latest_path
    value = json.loads(latest.read_text(encoding="utf-8"))
    noncanonical = (json.dumps(value, indent=2) + "\n").encode("utf-8")
    latest.write_bytes(noncanonical)

    with pytest.raises(
        publisher_module.RunBundlePublishError,
        match="latest_projection_invalid",
    ):
        publisher.publish(advanced)

    assert latest.read_bytes() == noncanonical


def test_projection_file_fsync_failure_keeps_latest_and_cleans_temporary_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publisher_module = importlib.import_module("shared.runtime.publisher")
    root = tmp_path / "published"
    publisher = publisher_module.LocalRunBundlePublisher(root)
    initial = _bundle()
    advanced = _after_preopen(initial)
    latest = publisher.publish(initial).latest_path
    trusted_latest = latest.read_bytes()
    real_fsync = publisher_module.os.fsync

    def fail_regular_file_fsync(fd: int) -> None:
        if stat.S_ISREG(publisher_module.os.fstat(fd).st_mode):
            raise OSError("injected file fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(publisher_module.os, "fsync", fail_regular_file_fsync)
    with pytest.raises(
        publisher_module.RunBundlePublishError,
        match="run_bundle_publish_file_fsync_failed",
    ):
        publisher.publish(advanced)

    assert latest.read_bytes() == trusted_latest
    assert not list(root.rglob("*.tmp"))


def test_latest_replace_failure_preserves_previous_projection_and_retry_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publisher_module = importlib.import_module("shared.runtime.publisher")
    root = tmp_path / "published"
    publisher = publisher_module.LocalRunBundlePublisher(root)
    initial = _bundle()
    advanced = _after_preopen(initial)
    latest = publisher.publish(initial).latest_path
    trusted = latest.read_bytes()
    real_replace = publisher_module.os.replace

    def fail_replace(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise OSError("injected replace failure")

    monkeypatch.setattr(publisher_module.os, "replace", fail_replace)
    with pytest.raises(
        publisher_module.RunBundlePublishError,
        match="latest_projection_replace_failed",
    ):
        publisher.publish(advanced)

    assert latest.read_bytes() == trusted
    monkeypatch.setattr(publisher_module.os, "replace", real_replace)
    retried = publisher.publish(advanced)
    assert retried.latest_path.read_bytes() == retried.immutable_path.read_bytes()


def test_publish_fsyncs_projection_files_and_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publisher_module = importlib.import_module("shared.runtime.publisher")
    synced_modes: list[int] = []
    real_fsync = publisher_module.os.fsync

    def track_fsync(fd: int) -> None:
        synced_modes.append(publisher_module.os.fstat(fd).st_mode)
        real_fsync(fd)

    monkeypatch.setattr(publisher_module.os, "fsync", track_fsync)

    publisher_module.LocalRunBundlePublisher(tmp_path / "published").publish(_bundle())

    assert any(stat.S_ISREG(mode) for mode in synced_modes)
    assert any(stat.S_ISDIR(mode) for mode in synced_modes)


def test_symlinked_publisher_lock_fails_closed(
    tmp_path: Path,
) -> None:
    publisher_module = importlib.import_module("shared.runtime.publisher")
    root = tmp_path / "published"
    root.mkdir()
    victim = tmp_path / "victim.lock"
    victim.write_text("do not touch", encoding="utf-8")
    (root / ".publisher.lock").symlink_to(victim)

    with pytest.raises(
        publisher_module.RunBundlePublishError,
        match="run_bundle_publish_lock_unavailable",
    ):
        publisher_module.LocalRunBundlePublisher(root).publish(_bundle())

    assert victim.read_text(encoding="utf-8") == "do not touch"
