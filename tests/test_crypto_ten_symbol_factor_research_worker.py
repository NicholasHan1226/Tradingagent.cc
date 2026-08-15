"""Tests for the detached ten-symbol factor-research worker CLI boundary."""

from __future__ import annotations

from datetime import timedelta
import json
from pathlib import Path

import pytest

import Crypto.ten_symbol_factor_research_worker as worker
from Crypto.ten_symbol_factor_research import CryptoTenSymbolFactorProjectionError
from Crypto.ten_symbol_factor_research_worker import (
    run_ten_symbol_factor_research_worker_once,
)
from tests.test_crypto_ten_symbol_observation_runtime import (
    _factory,
    _manifest_payload,
    _run,
    _runtime_paths,
    _write_manifest,
)
from tests.test_crypto_ten_symbol_support import (
    WINDOW_END,
    TenSymbolFixtureTransport,
)


def _bind_worker_manifest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[Path, Path, Path]:
    token_file, output_root = _runtime_paths(monkeypatch, tmp_path)
    manifest_path = _write_manifest(
        tmp_path, payload=_manifest_payload(output_root)
    )
    monkeypatch.setattr(
        worker, "TEN_SYMBOL_OBSERVATION_RUNTIME_MANIFEST", manifest_path
    )
    return token_file, output_root, manifest_path


def _accumulate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    token_file: Path,
    output_root: Path,
    count: int,
) -> None:
    for index in range(count):
        end = WINDOW_END + index * timedelta(minutes=5)
        receipt = _run(
            tmp_path,
            token_file,
            output_root,
            now=end + timedelta(seconds=55),
            transport_factory=_factory(TenSymbolFixtureTransport()),
        )
        assert receipt["status"] == "completed"


def test_worker_full_scrub_binds_manifest_and_store_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token_file, output_root, manifest_path = _bind_worker_manifest(
        monkeypatch, tmp_path
    )
    _accumulate(monkeypatch, tmp_path, token_file, output_root, 2)

    result = run_ten_symbol_factor_research_worker_once(mode="full-scrub")

    assert result["status"] == "recovered"
    assert result["mode"] == "full-scrub"
    assert result["store_root"] == str(output_root)
    assert len(result["runtime_manifest_sha256"]) == 64
    assert result["observation_count"] == 2
    assert (output_root / "evolution" / "ten_symbol_factor_research").is_dir()

    incremental = run_ten_symbol_factor_research_worker_once(mode="incremental")
    assert incremental["status"] == "up_to_date"
    assert incremental["store_root"] == str(output_root)
    assert manifest_path.is_file()


def test_worker_rejects_free_manifest_path_and_unknown_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _bind_worker_manifest(monkeypatch, tmp_path)

    with pytest.raises(
        CryptoTenSymbolFactorProjectionError,
        match="manifest_path_invalid",
    ):
        run_ten_symbol_factor_research_worker_once(
            runtime_manifest=tmp_path / "other.json",
            mode="incremental",
        )
    with pytest.raises(
        CryptoTenSymbolFactorProjectionError,
        match="mode_invalid",
    ):
        run_ten_symbol_factor_research_worker_once(mode="replay")


def test_worker_cli_success_and_redacted_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    token_file, output_root, _ = _bind_worker_manifest(monkeypatch, tmp_path)
    _accumulate(monkeypatch, tmp_path, token_file, output_root, 1)

    exit_code = worker.main(["--mode", "full-scrub"])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.err == ""
    printed = json.loads(captured.out)
    assert printed["status"] == "recovered"
    assert printed["mode"] == "full-scrub"

    assert worker.main(["--mode", "incremental"]) == 0
    capsys.readouterr()

    monkeypatch.setenv("REAL_TRADING_ENABLED", "true")
    exit_code = worker.main(["--mode", "incremental"])
    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.out == ""
    assert captured.err.strip() == (
        "crypto ten-symbol factor-research worker failed closed"
    )
    assert "traceback" not in captured.err.lower()


def test_worker_cli_has_no_output_root_or_manifest_flag(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _bind_worker_manifest(monkeypatch, tmp_path)

    with pytest.raises(SystemExit):
        worker.main(["--mode", "incremental", "--output-root", str(tmp_path)])
    capsys.readouterr()
    with pytest.raises(SystemExit):
        worker.main(
            ["--mode", "incremental", "--runtime-manifest", str(tmp_path / "x.json")]
        )
    capsys.readouterr()
