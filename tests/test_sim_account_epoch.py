from __future__ import annotations

import json
from pathlib import Path

import pytest

from shared.execution import local_sim_ledger, sim_account_epoch


def _fresh_root(tmp_path: Path) -> Path:
    root = tmp_path / local_sim_ledger.ASHARE_EXECUTION_LINEAGE_ID
    local_sim_ledger.bootstrap_fresh_local_sim(
        root=root,
        lineage_started_at="2026-07-12T00:00:00+08:00",
        point_in_time_as_of="2026-07-12T00:00:00+08:00",
    )
    return root


def test_numeric_epochs_are_frozen_history_not_current_authority() -> None:
    assert sim_account_epoch.CURRENT_EPOCH_ID is None
    assert sim_account_epoch.EPOCHS[1]["status"] == "immutable_legacy"
    assert sim_account_epoch.EPOCHS[1]["capital_cny"] == 200_000.0
    assert sim_account_epoch.EPOCHS[2]["status"] == "immutable_legacy"
    assert sim_account_epoch.EPOCHS[2]["capital_cny"] == 50_000.0
    with pytest.raises(TypeError):
        sim_account_epoch.EPOCHS[2]["capital_cny"] = 1.0  # type: ignore[index]


@pytest.mark.parametrize(
    ("call", "reason"),
    [
        (
            lambda: sim_account_epoch.get_current_epoch(),
            "numeric_epoch_authority_retired",
        ),
        (lambda: sim_account_epoch.get_epoch(2), "numeric_epoch_authority_retired"),
        (
            lambda: sim_account_epoch.epoch_capital_cny(2),
            "numeric_epoch_authority_retired",
        ),
        (
            lambda: sim_account_epoch.epoch_ledger_root(2),
            "numeric_epoch_authority_retired",
        ),
        (
            lambda: sim_account_epoch.require_authoritative_epoch_metadata(
                {"current_epoch_id": 2}
            ),
            "numeric_epoch_authority_retired",
        ),
        (
            lambda: sim_account_epoch.dry_run_cutover(),
            "runtime_cutover_retired_fresh_bootstrap_required",
        ),
        (
            lambda: sim_account_epoch.apply_cutover(),
            "runtime_cutover_retired_fresh_bootstrap_required",
        ),
    ],
)
def test_every_retired_authority_or_cutover_entrypoint_fails_closed(
    call, reason: str
) -> None:
    with pytest.raises(sim_account_epoch.LegacyExecutionFreezeError, match=reason):
        call()


def test_missing_legacy_state_is_reported_absent_without_creating_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "missing_epoch_state.json"
    monkeypatch.setattr(sim_account_epoch, "LEGACY_EPOCH_STATE_PATH", path)

    report = sim_account_epoch.read_epoch_state()

    assert report["status"] == "legacy_epoch_state_absent"
    assert report["authority_status"] == "retired"
    assert report["real_trading_enabled"] is False
    assert not path.exists()


def test_existing_legacy_state_is_nested_as_read_only_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "epoch_state.json"
    payload = {"current_epoch_id": 2, "capital_cny": 50_000.0}
    path.write_text(json.dumps(payload), encoding="utf-8")
    before = path.read_bytes()
    monkeypatch.setattr(sim_account_epoch, "LEGACY_EPOCH_STATE_PATH", path)

    report = sim_account_epoch.read_epoch_state()

    assert report["status"] == "legacy_epoch_state_frozen"
    assert report["legacy_state"] == payload
    assert path.read_bytes() == before


def test_freeze_verifier_proves_zero_import_and_fingerprints_legacy_tree(
    tmp_path: Path,
) -> None:
    fresh = _fresh_root(tmp_path)
    legacy = tmp_path / "legacy_local_sim"
    legacy.mkdir()
    (legacy / "trades.jsonl").write_text('{"legacy":true}\n', encoding="utf-8")

    report = sim_account_epoch.verify_legacy_execution_freeze(
        fresh_root=fresh,
        legacy_roots=[legacy],
    )
    fingerprint = report["legacy_roots"][0]["tree_sha256"]
    verified = sim_account_epoch.verify_legacy_execution_freeze(
        fresh_root=fresh,
        legacy_roots=[legacy],
        expected_fingerprints={str(legacy): fingerprint},
    )

    assert verified["status"] == "legacy_execution_frozen"
    assert verified["fresh_zero_import_verified"] is True
    assert verified["legacy_roots"][0]["record_count"] == 1
    assert verified["legacy_roots"][0]["expected_fingerprint_verified"] is True
    assert verified["real_trading_enabled"] is False


def test_freeze_verifier_detects_legacy_mutation(tmp_path: Path) -> None:
    fresh = _fresh_root(tmp_path)
    legacy = tmp_path / "legacy_local_sim"
    legacy.mkdir()
    evidence = legacy / "trades.jsonl"
    evidence.write_text('{"legacy":true}\n', encoding="utf-8")
    initial = sim_account_epoch.verify_legacy_execution_freeze(
        fresh_root=fresh,
        legacy_roots=[legacy],
    )["legacy_roots"][0]["tree_sha256"]
    evidence.write_text('{"legacy":false}\n', encoding="utf-8")

    with pytest.raises(
        sim_account_epoch.LegacyExecutionFreezeError,
        match="legacy_fingerprint_mismatch",
    ):
        sim_account_epoch.verify_legacy_execution_freeze(
            fresh_root=fresh,
            legacy_roots=[legacy],
            expected_fingerprints={str(legacy): initial},
        )


def test_freeze_verifier_rejects_fresh_legacy_overlap(tmp_path: Path) -> None:
    fresh = _fresh_root(tmp_path)
    with pytest.raises(
        sim_account_epoch.LegacyExecutionFreezeError,
        match="fresh_execution_root_overlaps_legacy_root",
    ):
        sim_account_epoch.verify_legacy_execution_freeze(
            fresh_root=fresh,
            legacy_roots=[fresh],
        )
