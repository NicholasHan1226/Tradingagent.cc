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


def test_worker_full_scrub_runs_evaluation_downstream(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token_file, output_root, _ = _bind_worker_manifest(monkeypatch, tmp_path)
    _accumulate(monkeypatch, tmp_path, token_file, output_root, 14)

    result = run_ten_symbol_factor_research_worker_once(mode="full-scrub")

    assert result["status"] == "recovered"
    evaluation = result["strategy_evaluation"]
    assert evaluation["status"] == "shadow_evaluated"
    assert evaluation["resolved_count"] == 20
    assert set(evaluation["evaluations"]["60"]) == {
        "momentum",
        "trend",
        "volatility",
    }

    again = run_ten_symbol_factor_research_worker_once(mode="full-scrub")
    assert again["status"] == "scrubbed"
    assert again["strategy_evaluation"]["status"] == "no_new_outcome"

    incremental = run_ten_symbol_factor_research_worker_once(mode="incremental")
    assert incremental["status"] == "up_to_date"
    assert incremental["strategy_evaluation"]["status"] == "no_new_outcome"
    assert incremental["strategy_evaluation"]["artifact_sha256"] == (
        again["strategy_evaluation"]["artifact_sha256"]
    )


def test_worker_incremental_skips_evaluation_before_first_scrub(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token_file, output_root, _ = _bind_worker_manifest(monkeypatch, tmp_path)
    _accumulate(monkeypatch, tmp_path, token_file, output_root, 1)

    result = run_ten_symbol_factor_research_worker_once(mode="incremental")

    assert result["status"] == "full_scrub_required"
    evaluation = result["strategy_evaluation"]
    assert evaluation["status"] == "no_evaluation_checkpoint"
    assert evaluation["reason"] == "evaluation_checkpoint_missing_pre_first_scrub"


def test_worker_full_scrub_with_insufficient_samples_reports_explicit_status(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token_file, output_root, _ = _bind_worker_manifest(monkeypatch, tmp_path)
    _accumulate(monkeypatch, tmp_path, token_file, output_root, 2)

    result = run_ten_symbol_factor_research_worker_once(mode="full-scrub")

    assert result["status"] == "recovered"
    assert result["strategy_evaluation"]["status"] == (
        "insufficient_resolved_samples"
    )


def test_worker_evaluation_failure_does_not_change_scrub_exit_semantics(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    token_file, output_root, _ = _bind_worker_manifest(monkeypatch, tmp_path)
    _accumulate(monkeypatch, tmp_path, token_file, output_root, 14)

    def broken_evaluation(*, store_root: Path, **_: Any) -> dict[str, Any]:
        from Crypto.ten_symbol_factor_strategy_evaluation import (
            CryptoTenSymbolFactorStrategyEvaluationError,
        )

        raise CryptoTenSymbolFactorStrategyEvaluationError("sensitive/path/detail")

    monkeypatch.setattr(
        worker,
        "run_ten_symbol_factor_strategy_evaluation",
        broken_evaluation,
    )

    result = run_ten_symbol_factor_research_worker_once(mode="full-scrub")

    assert result["status"] == "recovered"
    evaluation = result["strategy_evaluation"]
    assert evaluation["status"] == "evaluation_failed"
    assert evaluation["reason"] == "ten_symbol_factor_strategy_evaluation_failed"
    assert "sensitive" not in json.dumps(result)

    exit_code = worker.main(["--mode", "full-scrub"])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.err == ""
    printed = json.loads(captured.out)
    assert printed["status"] == "scrubbed"
    assert printed["strategy_evaluation"]["status"] == "evaluation_failed"


def test_worker_defers_evaluation_when_scrub_defers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token_file, output_root, _ = _bind_worker_manifest(monkeypatch, tmp_path)
    _accumulate(monkeypatch, tmp_path, token_file, output_root, 1)
    from Crypto.ten_symbol_observation_store import (
        CryptoTenSymbolObservationStore,
    )
    from tests.test_crypto_ten_symbol_support import (
        CATALOG_VERSION,
        WINDOW_END,
        iso,
    )

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

    result = run_ten_symbol_factor_research_worker_once(mode="full-scrub")

    assert result["status"] == "deferred_core_pending"
    evaluation = result["strategy_evaluation"]
    assert evaluation["status"] == "evaluation_deferred"
    assert evaluation["reason"] == "scrub_status_deferred_core_pending"


def test_worker_incremental_catches_up_bounded_backlog(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    token_file, output_root, _ = _bind_worker_manifest(monkeypatch, tmp_path)
    _accumulate(monkeypatch, tmp_path, token_file, output_root, 2)
    scrubbed = run_ten_symbol_factor_research_worker_once(mode="full-scrub")
    assert scrubbed["status"] == "recovered"
    for index in (2, 3, 4):
        end = WINDOW_END + index * timedelta(minutes=5)
        receipt = _run(
            tmp_path,
            token_file,
            output_root,
            now=end + timedelta(seconds=55),
            transport_factory=_factory(TenSymbolFixtureTransport()),
        )
        assert receipt["status"] == "completed"

    result = run_ten_symbol_factor_research_worker_once(mode="incremental")

    assert result["status"] == "projected_incremental"
    assert result["projected_count"] == 3
    assert result["remaining_count"] == 0
    assert result["strategy_evaluation"]["status"] == "no_evaluation_checkpoint"

    follow_up = run_ten_symbol_factor_research_worker_once(mode="incremental")
    assert follow_up["status"] == "up_to_date"

    exit_code = worker.main(["--mode", "incremental"])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.err == ""
    assert json.loads(captured.out)["status"] == "up_to_date"
