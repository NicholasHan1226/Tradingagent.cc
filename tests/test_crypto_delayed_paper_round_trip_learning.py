from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import Crypto.delayed_paper_round_trip_learning_worker as worker_module
from Crypto.delayed_paper_round_trip import run_crypto_delayed_paper_round_trip_once
from Crypto.delayed_paper_round_trip_learning import (
    CryptoRoundTripLearningError,
    round_trip_learning_exit_code,
    run_crypto_delayed_paper_round_trip_learning_full_scrub,
    run_crypto_delayed_paper_round_trip_learning_incremental,
)
from Crypto.delayed_paper_round_trip_learning_worker import (
    _existing_epoch_root,
    run_round_trip_learning_worker_once,
)
from Crypto.five_minute_data import TradingDatasCryptoFiveMinuteDataPort
from tests.test_crypto_5m_support import (
    FixtureTradingDatasTransport,
    client,
    profile,
    window_request,
)


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and "evolution/round_trip_learning" not in path.as_posix()
    }


def _completed_round_trip(root: Path) -> None:
    transport = FixtureTradingDatasTransport()
    tradingdatas_client = client(transport)
    result = run_crypto_delayed_paper_round_trip_once(
        port=TradingDatasCryptoFiveMinuteDataPort(tradingdatas_client),
        profile=profile(tradingdatas_client),
        request=window_request(),
        output_root=root,
    )
    assert result["status"] == "completed"


def test_full_scrub_projects_g4_learning_without_mutating_core(tmp_path: Path) -> None:
    _completed_round_trip(tmp_path)
    before = _tree_bytes(tmp_path)

    result = run_crypto_delayed_paper_round_trip_learning_full_scrub(
        output_root=tmp_path
    )

    assert _tree_bytes(tmp_path) == before
    assert result["status"] == "recovered"
    assert result["completion_count"] == 1
    assert result["learning_authority"] is False
    assert result["execution_authority"] is False
    assert result["promotion_authorized"] is False
    assert round_trip_learning_exit_code(result) == 0
    learning = tmp_path / "evolution" / "round_trip_learning"
    assert len(list((learning / "samples").glob("*.json"))) == 1
    assert len(list((learning / "kpis").glob("*.json"))) == 1
    assert len(list((learning / "challengers").glob("*.json"))) == 1
    assert len(list((learning / "receipts").glob("*.json"))) == 1
    assert len(list((learning / "checkpoints").glob("*.json"))) == 1


def test_incremental_requires_full_scrub_then_is_idempotent(tmp_path: Path) -> None:
    _completed_round_trip(tmp_path)
    required = run_crypto_delayed_paper_round_trip_learning_incremental(
        output_root=tmp_path
    )
    assert required["status"] == "full_scrub_required"

    run_crypto_delayed_paper_round_trip_learning_full_scrub(output_root=tmp_path)
    before = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in sorted((tmp_path / "evolution").rglob("*"))
        if path.is_file()
    }
    current = run_crypto_delayed_paper_round_trip_learning_incremental(
        output_root=tmp_path
    )
    after = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in sorted((tmp_path / "evolution").rglob("*"))
        if path.is_file()
    }
    assert current["status"] == "current"
    assert before == after


def test_full_scrub_fails_closed_for_tampered_prior_projection(tmp_path: Path) -> None:
    _completed_round_trip(tmp_path)
    run_crypto_delayed_paper_round_trip_learning_full_scrub(output_root=tmp_path)
    receipt = next(
        (tmp_path / "evolution" / "round_trip_learning" / "receipts").glob("*.json")
    )
    receipt.write_text("{}\n", encoding="utf-8")

    with pytest.raises(
        CryptoRoundTripLearningError,
        match="round_trip_learning_projection_invalid|round_trip_learning_projection_not_derived",
    ):
        run_crypto_delayed_paper_round_trip_learning_full_scrub(output_root=tmp_path)


def test_full_scrub_fails_closed_for_missing_checkpoint_claimed_receipt(
    tmp_path: Path,
) -> None:
    _completed_round_trip(tmp_path)
    run_crypto_delayed_paper_round_trip_learning_full_scrub(output_root=tmp_path)
    receipt = next(
        (tmp_path / "evolution" / "round_trip_learning" / "receipts").glob("*.json")
    )
    receipt.unlink()

    with pytest.raises(
        CryptoRoundTripLearningError,
        match="round_trip_learning_claimed_projection_missing",
    ):
        run_crypto_delayed_paper_round_trip_learning_full_scrub(output_root=tmp_path)


def test_worker_rejects_free_or_non_g4_manifest_paths(tmp_path: Path) -> None:
    with pytest.raises(
        CryptoRoundTripLearningError,
        match="round_trip_learning_manifest_path_invalid",
    ):
        run_round_trip_learning_worker_once(
            mode="incremental", epoch_manifest=tmp_path / "epoch.json"
        )


@pytest.mark.parametrize("generation", (4, 5))
def test_worker_admits_only_matching_g4_or_g5_manifest_generation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, generation: int
) -> None:
    manifest_directory = tmp_path / "round-trip-epochs"
    manifest_directory.mkdir()
    manifest = manifest_directory / f"crypto-delayed-paper-round-trip-epoch-g{generation}-test.json"
    identity = tmp_path / "identity.json"
    identity.write_bytes(b"identity")
    root = worker_module.ROUND_TRIP_LEARNING_EPOCH_ROOTS[generation]
    context = SimpleNamespace(
        epoch_generation=generation,
        output_root=root,
        identity_path=identity,
        epoch_id=f"g{generation}-test",
        manifest_sha256="a" * 64,
    )
    prepared = SimpleNamespace(output_root=root, identity_path=identity)
    seen: list[Path] = []
    monkeypatch.setattr(
        worker_module, "ROUND_TRIP_EPOCH_MANIFEST_DIRECTORY", manifest_directory
    )
    monkeypatch.setattr(worker_module, "load_round_trip_epoch_manifest", lambda _: context)
    monkeypatch.setattr(worker_module, "_existing_epoch_root", lambda _: None)
    monkeypatch.setattr(worker_module, "prepare_round_trip_epoch_candidate", lambda _: prepared)
    monkeypatch.setattr(
        worker_module,
        "run_crypto_delayed_paper_round_trip_learning_full_scrub",
        lambda *, output_root: seen.append(output_root) or {"status": "recovered"},
    )

    result = worker_module.run_round_trip_learning_worker_once(
        mode="full-scrub", epoch_manifest=manifest
    )

    assert seen == [root]
    assert result["epoch_generation"] == generation
    assert result["epoch_output_root"] == str(root)


def test_worker_rejects_g5_manifest_with_g4_context_before_root_access(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest_directory = tmp_path / "round-trip-epochs"
    manifest_directory.mkdir()
    manifest = manifest_directory / "crypto-delayed-paper-round-trip-epoch-g5-test.json"
    context = SimpleNamespace(epoch_generation=4)
    monkeypatch.setattr(
        worker_module, "ROUND_TRIP_EPOCH_MANIFEST_DIRECTORY", manifest_directory
    )
    monkeypatch.setattr(worker_module, "load_round_trip_epoch_manifest", lambda _: context)
    monkeypatch.setattr(
        worker_module,
        "_existing_epoch_root",
        lambda _: pytest.fail("mismatched generation reached root access"),
    )

    with pytest.raises(
        CryptoRoundTripLearningError,
        match="round_trip_learning_epoch_generation_invalid",
    ):
        worker_module.run_round_trip_learning_worker_once(
            mode="full-scrub", epoch_manifest=manifest
        )


@pytest.mark.parametrize("generation", (4, 5))
def test_worker_rejects_matching_generation_with_noncanonical_root_before_root_access(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, generation: int
) -> None:
    manifest_directory = tmp_path / "round-trip-epochs"
    manifest_directory.mkdir()
    manifest = (
        manifest_directory
        / f"crypto-delayed-paper-round-trip-epoch-g{generation}-test.json"
    )
    context = SimpleNamespace(
        epoch_generation=generation,
        output_root=tmp_path / f"wrong-g{generation}-root",
    )
    monkeypatch.setattr(
        worker_module, "ROUND_TRIP_EPOCH_MANIFEST_DIRECTORY", manifest_directory
    )
    monkeypatch.setattr(worker_module, "load_round_trip_epoch_manifest", lambda _: context)
    monkeypatch.setattr(
        worker_module,
        "_existing_epoch_root",
        lambda _: pytest.fail("noncanonical root reached root access"),
    )
    monkeypatch.setattr(
        worker_module,
        "prepare_round_trip_epoch_candidate",
        lambda _: pytest.fail("noncanonical root reached prepare access"),
    )

    with pytest.raises(
        CryptoRoundTripLearningError,
        match="round_trip_learning_epoch_root_invalid",
    ):
        worker_module.run_round_trip_learning_worker_once(
            mode="full-scrub", epoch_manifest=manifest
        )


def test_worker_rejects_non_round_trip_or_unknown_generation_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest_directory = tmp_path / "round-trip-epochs"
    manifest_directory.mkdir()
    monkeypatch.setattr(
        worker_module, "ROUND_TRIP_EPOCH_MANIFEST_DIRECTORY", manifest_directory
    )

    for name in (
        "crypto-delayed-paper-epoch-g5-test.json",
        "crypto-delayed-paper-round-trip-epoch-g6-test.json",
    ):
        with pytest.raises(
            CryptoRoundTripLearningError,
            match="round_trip_learning_manifest_path_invalid",
        ):
            worker_module.run_round_trip_learning_worker_once(
                mode="full-scrub", epoch_manifest=manifest_directory / name
            )


def test_worker_refuses_incomplete_epoch_before_any_prepare_write(
    tmp_path: Path,
) -> None:
    root = tmp_path / "incomplete-g4"
    root.mkdir()
    context = SimpleNamespace(output_root=root, identity_path=root / ".identity.json")

    with pytest.raises(
        CryptoRoundTripLearningError, match="round_trip_learning_root_incomplete"
    ):
        _existing_epoch_root(context)

    assert list(root.iterdir()) == []


def test_worker_cli_emits_allowed_failure_reason_without_exception_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        worker_module,
        "run_round_trip_learning_worker_once",
        lambda **_: (_ for _ in ()).throw(
            CryptoRoundTripLearningError("round_trip_learning_source_invalid")
        ),
    )

    assert worker_module.main(
        ["--mode", "full-scrub", "--epoch-manifest", str(tmp_path / "epoch.json")]
    ) == 2

    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        "contract": "tradingagent.crypto.round_trip_learning_worker_failure.v1",
        "failure_phase": "full_scrub",
        "failure_reason": "round_trip_learning_source_invalid",
        "status": "failed_closed",
    }
    assert captured.err == "crypto round-trip learning worker failed closed\n"


def test_worker_cli_maps_unexpected_failure_to_generic_reason(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        worker_module,
        "run_round_trip_learning_worker_once",
        lambda **_: (_ for _ in ()).throw(RuntimeError("do not disclose this text")),
    )

    assert worker_module.main(
        ["--mode", "incremental", "--epoch-manifest", str(tmp_path / "epoch.json")]
    ) == 2

    captured = capsys.readouterr()
    assert json.loads(captured.out)["failure_phase"] == "incremental"
    assert json.loads(captured.out)["failure_reason"] == "round_trip_learning_failed"
    assert "do not disclose this text" not in captured.out + captured.err


def test_worker_cli_invalid_arguments_do_not_emit_failure_event(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exited:
        worker_module.main(["--mode", "invalid", "--epoch-manifest", "epoch.json"])

    assert exited.value.code == 2
    assert capsys.readouterr().out == ""
