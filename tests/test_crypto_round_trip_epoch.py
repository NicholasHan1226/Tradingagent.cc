from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import Crypto.delayed_paper_round_trip_epoch as epoch_module
from Crypto.delayed_paper_round_trip_epoch import (
    CryptoRoundTripEpochError,
    load_round_trip_epoch_manifest,
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
    (capital / "head.json").write_bytes(
        _canonical(
            {
                "sequence": 42,
                "checksum": "c" * 64,
            }
        )
    )
    (capital / "head.json").chmod(0o600)
    monkeypatch.setattr(epoch_module, "ROUND_TRIP_EPOCH_MANIFEST_PATH", manifest)
    monkeypatch.setattr(epoch_module, "ROUND_TRIP_EPOCH_ROOT_PARENT", parent)
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
    head = archived / "capital" / "head.json"
    head.write_bytes(_canonical({"sequence": 42, "checksum": "d" * 64}))
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
