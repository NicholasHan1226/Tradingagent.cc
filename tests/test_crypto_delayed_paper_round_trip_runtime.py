from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest

import Crypto.delayed_paper_round_trip_epoch as epoch_module
import Crypto.delayed_paper_round_trip_runtime as runtime_module
from Crypto.delayed_paper_round_trip_runtime import (
    crypto_round_trip_window_request,
    run_crypto_delayed_paper_round_trip_server_once,
)
from tests.test_crypto_5m_support import FixtureTradingDatasTransport, WINDOW_END
from tests.test_crypto_delayed_paper_runtime import (
    _factory,
    _manifest_payload,
    _shifted_transport,
    _write_manifest,
)


def test_module_invocation_reaches_round_trip_cli_parser() -> None:
    """The systemd ``python -m`` entrypoint must not be an empty import."""
    completed = subprocess.run(
        [sys.executable, "-m", "Crypto.delayed_paper_round_trip_runtime", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert "Run one Crypto round-trip simulated cycle" in completed.stdout
    assert "--epoch-manifest" in completed.stdout


def test_round_trip_request_uses_one_closed_bar_settlement_delay() -> None:
    request = crypto_round_trip_window_request(
        WINDOW_END + timedelta(seconds=55)
    )

    assert request.window_end == WINDOW_END - timedelta(minutes=5)
    assert request.observation_cutoff == WINDOW_END + timedelta(seconds=55)


def test_round_trip_runtime_journal_summary_excludes_full_core_payload() -> None:
    receipt = {
        "contract": runtime_module.ROUND_TRIP_RUNTIME_CONTRACT,
        "status": "completed",
        "core_result": {
            "market_slot": "2026-08-03T15:05:00Z",
            "idempotent_replay": False,
            "orders": {"simulated-order": {"quantity": "0.1"}},
            "capital": {"cash": "9999"},
        },
        "requested_window_end": "2026-08-03T15:05:00Z",
        "requested_observation_cutoff": "2026-08-03T15:05:55Z",
        "settled_bar_delay_seconds": 300,
        "runtime_manifest_sha256": "a" * 64,
        "fresh_query_catalog_version": "v1-example",
        "fresh_query_profile_sha256": "b" * 64,
        "epoch_id": "crypto-delayed-paper-round-trip-epoch-g5",
        "epoch_generation": 5,
        "market_data_access_attempt_count": 1,
        "market_data_network_used": True,
        "learning_mode": "detached_offline_worker",
        "learning_authority": False,
        "learning_invoked": False,
        "real_trading_enabled": False,
        "execution_eligible": False,
        "execution_authority": False,
        "production_eligible": False,
        "testnet_used": False,
        "live_broker_used": False,
        "model_network_used": False,
        "promotion_authorized": False,
        "automatic_promotion_enabled": False,
        "automatic_risk_expansion_enabled": False,
        "outbox_id": None,
        "capital_commit_id": None,
    }

    summary = runtime_module.round_trip_runtime_journal_summary(receipt)

    assert summary["contract"] == "tradingagent.crypto.round_trip_server_journal.v1"
    assert summary["runtime_contract"] == runtime_module.ROUND_TRIP_RUNTIME_CONTRACT
    assert summary["status"] == "completed"
    assert summary["market_slot"] == "2026-08-03T15:05:00Z"
    assert summary["idempotent_replay"] is False
    assert summary["real_trading_enabled"] is False
    assert "core_result" not in summary
    encoded = json.dumps(summary, sort_keys=True)
    assert "simulated-order" not in encoded
    assert '"orders"' not in encoded
    assert '"capital"' not in encoded


def test_round_trip_runtime_cli_emits_only_bounded_journal_summary(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    receipt = {
        "contract": runtime_module.ROUND_TRIP_RUNTIME_CONTRACT,
        "status": "completed",
        "core_result": {
            "market_slot": "2026-08-03T15:05:00Z",
            "idempotent_replay": False,
            "orders": {"simulated-order": {"quantity": "0.1"}},
        },
        "real_trading_enabled": False,
        "execution_authority": False,
    }
    monkeypatch.setattr(
        runtime_module,
        "run_crypto_delayed_paper_round_trip_server_once",
        lambda **_kwargs: receipt,
    )

    exit_code = runtime_module.main(
        [
            "--epoch-manifest",
            "/tmp/epoch.json",
            "--runtime-manifest",
            "/tmp/runtime.json",
            "--token-file",
            "/tmp/token",
        ]
    )

    rendered = capsys.readouterr().out
    payload = json.loads(rendered)
    assert exit_code == 0
    assert payload["status"] == "completed"
    assert "core_result" not in payload
    assert "simulated-order" not in rendered


def test_round_trip_runtime_cli_fails_closed_when_journal_summary_is_invalid(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        runtime_module,
        "run_crypto_delayed_paper_round_trip_server_once",
        lambda **_kwargs: {
            "status": "completed",
            "core_result": None,
        },
    )

    exit_code = runtime_module.main(
        [
            "--epoch-manifest",
            "/tmp/epoch.json",
            "--runtime-manifest",
            "/tmp/runtime.json",
            "--token-file",
            "/tmp/token",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.out == ""
    assert captured.err == "crypto round-trip runtime failed closed\n"


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


def _configure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[Path, Path, Path, Path]:
    parent = tmp_path / "epochs"
    archived = parent / "crypto-delayed-paper-epoch-g2-20260729"
    output = parent / "crypto-delayed-paper-round-trip-epoch-g3-20260731"
    manifest = tmp_path / "round-trip.epoch.json"
    token = tmp_path / "tradingdatas-crypto-read.token"
    parent.mkdir(mode=0o700)
    archived.mkdir(mode=0o700)
    identity = _canonical({"epoch_id": archived.name, "generation": 2})
    (archived / ".epoch_identity.json").write_bytes(identity)
    (archived / ".epoch_identity.json").chmod(0o600)
    capital = archived / "capital"
    capital.mkdir(mode=0o700)
    (capital / "head.json").write_bytes(
        _canonical({"sequence": 41, "checksum": "c" * 64})
    )
    payload = {
        "schema": "tradingagent.crypto.round_trip_epoch_manifest.v1",
        "epoch_id": output.name,
        "epoch_generation": 3,
        "current_output_root": str(output),
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

    class _ArchiveLedger:
        def __init__(self, root: Path) -> None:
            assert root == archived / "capital"

        def head(self) -> tuple[int, str]:
            return 41, "c" * 64

    monkeypatch.setattr(epoch_module, "ROUND_TRIP_EPOCH_MANIFEST_PATH", manifest)
    monkeypatch.setattr(epoch_module, "ROUND_TRIP_EPOCH_ROOT_PARENT", parent)
    monkeypatch.setattr(epoch_module, "CryptoCapitalLedger", _ArchiveLedger)
    monkeypatch.setattr(runtime_module, "ROUND_TRIP_EPOCH_MANIFEST_PATH", manifest)
    monkeypatch.setattr(runtime_module, "RUNTIME_TOKEN_FILE", token)
    return manifest, archived, output, token


def test_round_trip_runtime_preserves_g2_and_replays_same_slot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    epoch, archived, output, token = _configure(monkeypatch, tmp_path)
    archived_before = _tree(archived)
    runtime_manifest = _write_manifest(
        tmp_path / "runtime", payload=_manifest_payload()
    )
    first = run_crypto_delayed_paper_round_trip_server_once(
        epoch_manifest=epoch,
        runtime_manifest=runtime_manifest,
        token_file=token,
        now=WINDOW_END + timedelta(minutes=5, seconds=55),
        transport_factory=_factory(FixtureTradingDatasTransport()),
    )
    before_replay = _tree(output)
    replay = run_crypto_delayed_paper_round_trip_server_once(
        epoch_manifest=epoch,
        runtime_manifest=runtime_manifest,
        token_file=token,
        now=WINDOW_END + timedelta(minutes=5, seconds=55),
        transport_factory=_factory(FixtureTradingDatasTransport()),
    )
    assert _tree(output) == before_replay
    adjacent = run_crypto_delayed_paper_round_trip_server_once(
        epoch_manifest=epoch,
        runtime_manifest=runtime_manifest,
        token_file=token,
        now=WINDOW_END + timedelta(minutes=10, seconds=55),
        transport_factory=_factory(_shifted_transport(5)),
    )
    assert _tree(archived) == archived_before
    assert first["status"] == replay["status"] == adjacent["status"] == "completed"
    assert replay["core_result"]["idempotent_replay"] is True
    assert replay["market_data_access_attempt_count"] == 0
    assert first["capital_authority_id"] == "crypto-round-trip-capital-v1"
    assert first["settled_bar_delay_seconds"] == 300
    assert first["real_trading_enabled"] is False
    assert adjacent["core_result"]["capital"]["balanced"] is True


def test_round_trip_runtime_rejects_noncanonical_epoch_or_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    epoch, _, _, token = _configure(monkeypatch, tmp_path)
    runtime_manifest = _write_manifest(
        tmp_path / "runtime", payload=_manifest_payload()
    )
    with pytest.raises(RuntimeError, match="round_trip_token_file_path_invalid"):
        run_crypto_delayed_paper_round_trip_server_once(
            epoch_manifest=epoch,
            runtime_manifest=runtime_manifest,
            token_file=tmp_path / "other",
            now=WINDOW_END + timedelta(seconds=55),
            transport_factory=_factory(FixtureTradingDatasTransport()),
        )


def test_round_trip_runtime_accepts_only_prepared_versioned_migration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    legacy, archived, _, token = _configure(monkeypatch, tmp_path)
    directory_parent = tmp_path / "etc" / "tradingagent"
    directory_parent.mkdir(parents=True, mode=0o700)
    directory = directory_parent / "round-trip-epochs"
    monkeypatch.setattr(
        epoch_module, "ROUND_TRIP_EPOCH_MANIFEST_PARENT", directory_parent
    )
    monkeypatch.setattr(epoch_module, "ROUND_TRIP_EPOCH_MANIFEST_DIRECTORY", directory)
    monkeypatch.setattr(epoch_module, "_runtime_reader_gid", os.getegid)
    monkeypatch.setattr(
        runtime_module, "ROUND_TRIP_EPOCH_MANIFEST_DIRECTORY", directory
    )
    context = epoch_module.prepare_versioned_round_trip_epoch_manifest(
        epoch_id="crypto-delayed-paper-round-trip-epoch-g3-migration",
        archived_output_root=archived,
        migration_reason="replace_stale_preflight_manifest",
    )
    runtime_manifest = _write_manifest(
        tmp_path / "runtime", payload=_manifest_payload()
    )
    receipt = run_crypto_delayed_paper_round_trip_server_once(
        epoch_manifest=context.manifest_path,
        runtime_manifest=runtime_manifest,
        token_file=token,
        now=WINDOW_END + timedelta(minutes=5, seconds=55),
        transport_factory=_factory(FixtureTradingDatasTransport()),
    )
    assert legacy.exists()
    assert receipt["status"] == "completed"
    assert receipt["epoch_id"] == context.epoch_id


def test_round_trip_runtime_accepts_explicit_g4_successor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, archived, _, token = _configure(monkeypatch, tmp_path)
    directory_parent = tmp_path / "etc" / "tradingagent"
    directory_parent.mkdir(parents=True, mode=0o700)
    directory = directory_parent / "round-trip-epochs"
    monkeypatch.setattr(
        epoch_module, "ROUND_TRIP_EPOCH_MANIFEST_PARENT", directory_parent
    )
    monkeypatch.setattr(epoch_module, "ROUND_TRIP_EPOCH_MANIFEST_DIRECTORY", directory)
    monkeypatch.setattr(epoch_module, "_runtime_reader_gid", os.getegid)
    monkeypatch.setattr(
        runtime_module, "ROUND_TRIP_EPOCH_MANIFEST_DIRECTORY", directory
    )
    g3 = epoch_module.prepare_versioned_round_trip_epoch_manifest(
        epoch_id="crypto-delayed-paper-round-trip-epoch-g3-failed-evidence",
        archived_output_root=archived,
        migration_reason="preserve_failed_g3",
    )

    class _AdvancedArchiveLedger:
        def __init__(self, root: Path) -> None:
            assert root == archived / "capital"

        def head(self) -> tuple[int, str]:
            return 42, "d" * 64

    monkeypatch.setattr(epoch_module, "CryptoCapitalLedger", _AdvancedArchiveLedger)
    g4 = epoch_module.prepare_successor_round_trip_epoch_manifest(
        epoch_id="crypto-delayed-paper-round-trip-epoch-g4-current-head",
        archived_output_root=archived,
        supersedes_manifest_path=g3.manifest_path,
        migration_reason="g2_advanced_after_g3_failed_preflight",
    )
    runtime_manifest = _write_manifest(
        tmp_path / "runtime", payload=_manifest_payload()
    )
    receipt = run_crypto_delayed_paper_round_trip_server_once(
        epoch_manifest=g4.manifest_path,
        runtime_manifest=runtime_manifest,
        token_file=token,
        now=WINDOW_END + timedelta(minutes=5, seconds=55),
        transport_factory=_factory(FixtureTradingDatasTransport()),
    )
    assert receipt["status"] == "completed"
    assert receipt["epoch_id"] == g4.epoch_id
    assert receipt["epoch_generation"] == 4
