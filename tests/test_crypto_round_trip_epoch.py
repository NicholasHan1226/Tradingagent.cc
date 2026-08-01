from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

import Crypto.delayed_paper_round_trip_epoch as epoch_module
from Crypto.delayed_paper_round_trip_epoch import (
    CryptoRoundTripEpochError,
    load_round_trip_epoch_manifest,
    prepare_successor_round_trip_epoch_manifest,
    prepare_versioned_round_trip_epoch_manifest,
    prepare_round_trip_epoch_candidate,
)


def _canonical(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode()


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def _candidate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[Path, Path, Path]:
    parent = tmp_path / "epochs"
    archived = parent / "crypto-delayed-paper-epoch-g2-existing"
    target = parent / "crypto-delayed-paper-round-trip-epoch-g3-candidate"
    manifest = tmp_path / "crypto-round-trip.epoch.json"
    parent.mkdir(mode=0o700)
    archived.mkdir(mode=0o700)
    identity = _canonical({"epoch_id": archived.name, "generation": 2})
    (archived / ".epoch_identity.json").write_bytes(identity)
    (archived / ".epoch_identity.json").chmod(0o600)
    capital = archived / "capital"
    capital.mkdir(mode=0o700)
    (capital / "head.json").write_text("authority-reader-fixture\n", encoding="utf-8")
    monkeypatch.setattr(epoch_module, "ROUND_TRIP_EPOCH_MANIFEST_PATH", manifest)
    monkeypatch.setattr(epoch_module, "ROUND_TRIP_EPOCH_ROOT_PARENT", parent)

    class _ArchiveLedger:
        def __init__(self, root: Path) -> None:
            assert root == capital

        def head(self) -> tuple[int, str]:
            return 42, "c" * 64

    monkeypatch.setattr(epoch_module, "CryptoCapitalLedger", _ArchiveLedger)
    payload = {
        "schema": "tradingagent.crypto.round_trip_epoch_manifest.v1",
        "epoch_id": target.name,
        "epoch_generation": 3,
        "current_output_root": str(target),
        "archived_output_root": str(archived),
        "archived_epoch_id": archived.name,
        "archived_epoch_identity_file_sha256": hashlib.sha256(identity).hexdigest(),
        "archived_capital_head_checksum": "c" * 64,
        "archived_epoch_policy": "read_only_archive_no_resume_no_aggregation",
        "capital_authority_id": "crypto-round-trip-capital-v1",
        "capital_generation": 2,
        "capital_baseline_usdt": "10000",
        "aggregate_with_archived_epoch": False,
        "activate_current_epoch": False,
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
    manifest.write_bytes(_canonical(payload))
    manifest.chmod(0o600)
    return manifest, archived, target


def test_round_trip_epoch_candidate_anchors_archive_without_mutating_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest, archived, target = _candidate(monkeypatch, tmp_path)
    before = {
        path.relative_to(archived).as_posix(): path.read_bytes()
        for path in archived.rglob("*")
        if path.is_file()
    }
    context = load_round_trip_epoch_manifest(manifest)
    prepared = prepare_round_trip_epoch_candidate(context)
    assert prepared.output_root == target
    assert prepared.context.epoch_generation == 3
    assert prepared.context.capital_generation == 2
    assert prepared.context.aggregate_with_archived_epoch is False
    assert not (target.parent / ".current_epoch.json").exists()
    assert {
        path.relative_to(archived).as_posix(): path.read_bytes()
        for path in archived.rglob("*")
        if path.is_file()
    } == before

    identity_before = prepared.identity_path.read_bytes()
    assert prepare_round_trip_epoch_candidate(context).identity_path.read_bytes() == (
        identity_before
    )


def test_round_trip_epoch_rejects_tampered_archived_head(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest, archived, _ = _candidate(monkeypatch, tmp_path)
    context = load_round_trip_epoch_manifest(manifest)

    class _TamperedArchiveLedger:
        def __init__(self, root: Path) -> None:
            assert root == archived / "capital"

        def head(self) -> tuple[int, str]:
            return 42, "d" * 64

    monkeypatch.setattr(epoch_module, "CryptoCapitalLedger", _TamperedArchiveLedger)
    with pytest.raises(
        CryptoRoundTripEpochError, match="archive_capital_head_mismatch"
    ):
        prepare_round_trip_epoch_candidate(context)


def test_round_trip_epoch_never_aggregates_or_activates_old_generation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest, _, _ = _candidate(monkeypatch, tmp_path)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["aggregate_with_archived_epoch"] = True
    manifest.write_bytes(_canonical(payload))
    with pytest.raises(CryptoRoundTripEpochError, match="manifest_safety_invalid"):
        load_round_trip_epoch_manifest(manifest)


def test_round_trip_epoch_rejects_manifest_changed_after_context_load(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest, _, _ = _candidate(monkeypatch, tmp_path)
    context = load_round_trip_epoch_manifest(manifest)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["archived_capital_head_checksum"] = "d" * 64
    manifest.write_bytes(_canonical(payload))
    with pytest.raises(
        CryptoRoundTripEpochError,
        match="context_stale",
    ):
        prepare_round_trip_epoch_candidate(context)


def _configure_versioned_migration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[Path, Path, Path, Path]:
    legacy, archived, _ = _candidate(monkeypatch, tmp_path)
    directory_parent = tmp_path / "etc" / "tradingagent"
    directory_parent.mkdir(parents=True, mode=0o700)
    directory = directory_parent / "round-trip-epochs"
    monkeypatch.setattr(
        epoch_module, "ROUND_TRIP_EPOCH_MANIFEST_PARENT", directory_parent
    )
    monkeypatch.setattr(epoch_module, "ROUND_TRIP_EPOCH_MANIFEST_DIRECTORY", directory)
    monkeypatch.setattr(epoch_module, "_runtime_reader_gid", os.getegid)
    return legacy, archived, directory, tmp_path / "epochs"


def test_versioned_migration_binds_frozen_g2_and_preserves_old_manifest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    legacy, archived, directory, _ = _configure_versioned_migration(
        monkeypatch, tmp_path
    )
    legacy_before = legacy.read_bytes()
    archive_before = {
        path.relative_to(archived).as_posix(): path.read_bytes()
        for path in archived.rglob("*")
        if path.is_file()
    }
    context = prepare_versioned_round_trip_epoch_manifest(
        epoch_id="crypto-delayed-paper-round-trip-epoch-g3-migration",
        archived_output_root=archived,
        migration_reason="replace_stale_preflight_manifest",
    )
    manifest_before = context.manifest_path.read_bytes()
    receipt_before = context.supersession_receipt_path.read_bytes()  # type: ignore[union-attr]

    replay = prepare_versioned_round_trip_epoch_manifest(
        epoch_id=context.epoch_id,
        archived_output_root=archived,
        migration_reason="replace_stale_preflight_manifest",
    )

    assert context.versioned is True
    assert context.manifest_path.parent == directory
    assert replay == context
    assert stat.S_IMODE(directory.stat().st_mode) == 0o750
    assert stat.S_IMODE(context.manifest_path.stat().st_mode) == 0o640
    assert stat.S_IMODE(context.supersession_receipt_path.stat().st_mode) == 0o640  # type: ignore[union-attr]
    assert legacy.read_bytes() == legacy_before
    assert context.manifest_path.read_bytes() == manifest_before
    assert context.supersession_receipt_path.read_bytes() == receipt_before  # type: ignore[union-attr]
    assert {
        path.relative_to(archived).as_posix(): path.read_bytes()
        for path in archived.rglob("*")
        if path.is_file()
    } == archive_before


def test_versioned_migration_rejects_reversion_or_second_g3_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    legacy, archived, directory, _ = _configure_versioned_migration(
        monkeypatch, tmp_path
    )
    context = prepare_versioned_round_trip_epoch_manifest(
        epoch_id="crypto-delayed-paper-round-trip-epoch-g3-migration",
        archived_output_root=archived,
        migration_reason="replace_stale_preflight_manifest",
    )
    with pytest.raises(
        CryptoRoundTripEpochError,
        match="supersession_receipt_invalid",
    ):
        prepare_versioned_round_trip_epoch_manifest(
            epoch_id="crypto-delayed-paper-round-trip-epoch-g3-other",
            archived_output_root=archived,
            migration_reason="replace_stale_preflight_manifest",
        )
    legacy.write_bytes(_canonical({"tampered": True}))
    with pytest.raises(
        CryptoRoundTripEpochError,
        match="superseded_manifest_mismatch",
    ):
        load_round_trip_epoch_manifest(context.manifest_path)
    assert context.manifest_path.parent == directory


def test_versioned_migration_rejects_g2_head_advance_before_g3_prepare(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _, archived, _, _ = _configure_versioned_migration(monkeypatch, tmp_path)
    context = prepare_versioned_round_trip_epoch_manifest(
        epoch_id="crypto-delayed-paper-round-trip-epoch-g3-migration",
        archived_output_root=archived,
        migration_reason="replace_stale_preflight_manifest",
    )

    class _AdvancedArchiveLedger:
        def __init__(self, root: Path) -> None:
            assert root == archived / "capital"

        def head(self) -> tuple[int, str]:
            return 43, "d" * 64

    monkeypatch.setattr(epoch_module, "CryptoCapitalLedger", _AdvancedArchiveLedger)
    with pytest.raises(
        CryptoRoundTripEpochError,
        match="archive_capital_head_mismatch",
    ):
        prepare_round_trip_epoch_candidate(context)


def test_versioned_migration_conflict_never_leaves_new_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _, archived, directory, _ = _configure_versioned_migration(monkeypatch, tmp_path)
    directory.mkdir(mode=0o750)
    conflicting = directory / "crypto-delayed-paper-round-trip-epoch-g3-migration.json"
    conflicting.write_bytes(_canonical({"foreign": True}))
    conflicting.chmod(0o600)

    with pytest.raises(
        CryptoRoundTripEpochError,
        match="versioned_manifest_conflict",
    ):
        prepare_versioned_round_trip_epoch_manifest(
            epoch_id="crypto-delayed-paper-round-trip-epoch-g3-migration",
            archived_output_root=archived,
            migration_reason="replace_stale_preflight_manifest",
        )
    assert not (directory / "generation-3.supersession.json").exists()


def test_g4_successor_preserves_failed_g3_and_binds_current_g2_head(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _, archived, directory, _ = _configure_versioned_migration(monkeypatch, tmp_path)
    g3 = prepare_versioned_round_trip_epoch_manifest(
        epoch_id="crypto-delayed-paper-round-trip-epoch-g3-failed-evidence",
        archived_output_root=archived,
        migration_reason="preserve_failed_g3",
    )
    prepare_round_trip_epoch_candidate(g3)
    g3_tree_before = _tree(g3.output_root)
    g3_manifest_before = g3.manifest_path.read_bytes()
    g3_receipt_before = g3.supersession_receipt_path.read_bytes()  # type: ignore[union-attr]

    class _AdvancedArchiveLedger:
        def __init__(self, root: Path) -> None:
            assert root == archived / "capital"

        def head(self) -> tuple[int, str]:
            return 43, "d" * 64

    monkeypatch.setattr(epoch_module, "CryptoCapitalLedger", _AdvancedArchiveLedger)
    g4 = prepare_successor_round_trip_epoch_manifest(
        epoch_id="crypto-delayed-paper-round-trip-epoch-g4-current-head",
        archived_output_root=archived,
        supersedes_manifest_path=g3.manifest_path,
        migration_reason="g2_advanced_after_g3_failed_preflight",
    )
    assert g4.epoch_generation == 4
    assert g4.archived_capital_head_sequence == 43
    assert g4.supersedes_manifest_path == g3.manifest_path
    assert g4.supersedes_receipt_path == g3.supersession_receipt_path
    assert (
        prepare_successor_round_trip_epoch_manifest(
            epoch_id=g4.epoch_id,
            archived_output_root=archived,
            supersedes_manifest_path=g3.manifest_path,
            migration_reason="g2_advanced_after_g3_failed_preflight",
        )
        == g4
    )
    assert _tree(g3.output_root) == g3_tree_before
    assert g3.manifest_path.read_bytes() == g3_manifest_before
    assert g3.supersession_receipt_path.read_bytes() == g3_receipt_before  # type: ignore[union-attr]
    assert g4.manifest_path.parent == directory
    assert prepare_round_trip_epoch_candidate(g4).output_root == g4.output_root
    with pytest.raises(
        CryptoRoundTripEpochError,
        match="supersession_receipt_invalid",
    ):
        prepare_successor_round_trip_epoch_manifest(
            epoch_id="crypto-delayed-paper-round-trip-epoch-g4-other-root",
            archived_output_root=archived,
            supersedes_manifest_path=g3.manifest_path,
            migration_reason="g2_advanced_after_g3_failed_preflight",
        )
    assert not (
        directory / "crypto-delayed-paper-round-trip-epoch-g4-other-root.json"
    ).exists()


def test_g4_successor_rejects_tampered_chain_and_later_g2_head(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _, archived, _, _ = _configure_versioned_migration(monkeypatch, tmp_path)
    g3 = prepare_versioned_round_trip_epoch_manifest(
        epoch_id="crypto-delayed-paper-round-trip-epoch-g3-failed-evidence",
        archived_output_root=archived,
        migration_reason="preserve_failed_g3",
    )

    class _AdvancedArchiveLedger:
        def __init__(self, root: Path) -> None:
            assert root == archived / "capital"

        def head(self) -> tuple[int, str]:
            return 43, "d" * 64

    monkeypatch.setattr(epoch_module, "CryptoCapitalLedger", _AdvancedArchiveLedger)
    g4 = prepare_successor_round_trip_epoch_manifest(
        epoch_id="crypto-delayed-paper-round-trip-epoch-g4-current-head",
        archived_output_root=archived,
        supersedes_manifest_path=g3.manifest_path,
        migration_reason="g2_advanced_after_g3_failed_preflight",
    )
    original_receipt = g3.supersession_receipt_path.read_bytes()  # type: ignore[union-attr]
    g3.supersession_receipt_path.write_bytes(_canonical({"tampered": True}))  # type: ignore[union-attr]
    with pytest.raises(
        CryptoRoundTripEpochError,
        match="superseded_receipt_mismatch",
    ):
        load_round_trip_epoch_manifest(g4.manifest_path)

    # Restore the test fixture, then prove a later g2 writer invalidates g4.
    g3.supersession_receipt_path.write_bytes(original_receipt)  # type: ignore[union-attr]

    class _LaterArchiveLedger:
        def __init__(self, root: Path) -> None:
            assert root == archived / "capital"

        def head(self) -> tuple[int, str]:
            return 44, "e" * 64

    monkeypatch.setattr(epoch_module, "CryptoCapitalLedger", _LaterArchiveLedger)
    with pytest.raises(
        CryptoRoundTripEpochError,
        match="archive_capital_head_mismatch",
    ):
        prepare_round_trip_epoch_candidate(g4)


def test_g5_recovery_successor_freezes_g4_head_without_rewriting_g4(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _, archived, _, _ = _configure_versioned_migration(monkeypatch, tmp_path)
    g3 = prepare_versioned_round_trip_epoch_manifest(
        epoch_id="crypto-delayed-paper-round-trip-epoch-g3-failed-evidence",
        archived_output_root=archived,
        migration_reason="preserve_failed_g3",
    )

    class _AdvancedArchiveLedger:
        def __init__(self, root: Path) -> None:
            assert root == archived / "capital"

        def head(self) -> tuple[int, str]:
            return 43, "d" * 64

    monkeypatch.setattr(epoch_module, "CryptoCapitalLedger", _AdvancedArchiveLedger)
    g4 = prepare_successor_round_trip_epoch_manifest(
        epoch_id="crypto-delayed-paper-round-trip-epoch-g4-frozen-evidence",
        archived_output_root=archived,
        supersedes_manifest_path=g3.manifest_path,
        migration_reason="g2_advanced_after_g3_failed_preflight",
    )
    prepare_round_trip_epoch_candidate(g4)
    g4_tree_before = _tree(g4.output_root)
    g4_manifest_before = g4.manifest_path.read_bytes()

    class _RoundTripLedger:
        def __init__(self, root: Path) -> None:
            assert root == g4.output_root / "round_trip_capital"

        def head(self) -> tuple[int, str]:
            return 409, "e" * 64

    monkeypatch.setattr(epoch_module, "RoundTripCapitalLedger", _RoundTripLedger)
    g5 = epoch_module.prepare_recovery_successor_round_trip_epoch_manifest(
        epoch_id="crypto-delayed-paper-round-trip-epoch-g5-recovery",
        supersedes_manifest_path=g4.manifest_path,
        migration_reason="g4_runtime_manifest_contract_superseded",
    )

    assert g5.epoch_generation == 5
    assert g5.supersedes_output_root == g4.output_root
    assert g5.supersedes_capital_head_sequence == 409
    assert prepare_round_trip_epoch_candidate(g5).output_root == g5.output_root
    assert _tree(g4.output_root) == g4_tree_before
    assert g4.manifest_path.read_bytes() == g4_manifest_before
    assert (
        epoch_module.prepare_recovery_successor_round_trip_epoch_manifest(
            epoch_id=g5.epoch_id,
            supersedes_manifest_path=g4.manifest_path,
            migration_reason="g4_runtime_manifest_contract_superseded",
        )
        == g5
    )
    with pytest.raises(CryptoRoundTripEpochError, match="supersession_receipt_invalid"):
        epoch_module.prepare_recovery_successor_round_trip_epoch_manifest(
            epoch_id="crypto-delayed-paper-round-trip-epoch-g5-other-root",
            supersedes_manifest_path=g4.manifest_path,
            migration_reason="g4_runtime_manifest_contract_superseded",
        )
