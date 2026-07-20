from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

from Ashare.adapter import AshareAdapter
from CNFutures import (
    calibration,
    observation_report,
    opening_validator,
    replay,
    run_simulation,
)
from CNFutures.adapter import CNFuturesAdapter
from Crypto.adapter import CryptoAdapter
from Crypto.sim_executor import CryptoLegacyExecutionRetired
from Crypto.workflow import CryptoWorkflow
from shared.execution import auto_pipeline
from shared.data.marketgraph_api import DEFAULT_API_URL as DEFAULT_MARKETGRAPH_API_URL
from shared.data.shared_signals_api import (
    DEFAULT_API_URL as DEFAULT_LEGACY_DATA_API_URL,
)
from shared.governance.retirement import (
    RETIRED_RUNTIME_EXIT_CODE,
    RetiredRuntimeError,
)
from shared.runtime_test import ashare_opening_validator, ashare_preopen_dry_run


def test_legacy_clients_have_no_implicit_localhost_runtime() -> None:
    assert DEFAULT_LEGACY_DATA_API_URL == ""
    assert DEFAULT_MARKETGRAPH_API_URL == ""


def test_direct_python_clis_fail_before_data_or_output(
    monkeypatch, tmp_path: Path
) -> None:
    def forbidden(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("retired CLI touched an external or output path")

    monkeypatch.setattr(Path, "write_text", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)

    assert run_simulation.main() == RETIRED_RUNTIME_EXIT_CODE
    assert opening_validator.main(["--sqlite-db", str(tmp_path / "legacy.sqlite")]) == (
        RETIRED_RUNTIME_EXIT_CODE
    )
    assert replay.main(["--output", str(tmp_path / "replay.json")]) == (
        RETIRED_RUNTIME_EXIT_CODE
    )
    assert observation_report.main() == RETIRED_RUNTIME_EXIT_CODE
    assert calibration.main() == RETIRED_RUNTIME_EXIT_CODE
    assert auto_pipeline.main(["--market", "crypto"]) == RETIRED_RUNTIME_EXIT_CODE
    assert ashare_opening_validator.main() == RETIRED_RUNTIME_EXIT_CODE
    assert ashare_preopen_dry_run.main() == RETIRED_RUNTIME_EXIT_CODE
    assert list(tmp_path.iterdir()) == []


def test_market_adapters_never_create_implicit_legacy_reader(monkeypatch) -> None:
    monkeypatch.setenv("SHAREDSIGNALS_API_URL", "http://127.0.0.1:8082")
    monkeypatch.setenv("SHARED_SIGNALS_DB", "/tmp/legacy.sqlite")
    monkeypatch.setenv("TRADINGAGENT_ALLOW_SHARED_SIGNALS_SQLITE", "1")

    ashare = AshareAdapter()
    futures = CNFuturesAdapter()
    crypto = CryptoAdapter()

    assert ashare.reader is None
    assert futures.reader is None
    assert crypto.reader is None
    assert ashare.get_universe("20260720") == []
    assert futures.get_universe("20260720") == []
    assert futures.get_intraday_universe("20260720") == []
    assert futures.get_bars_daily("Futures", "RB2610.SHF") == []
    assert futures.get_bars_intraday("Futures", "RB2610.SHF") == []
    assert not hasattr(futures, "_allow_direct_sqlite_fallback")
    assert not hasattr(futures, "_get_assets_from_sqlite")


def test_library_workflows_require_explicit_data_port(tmp_path: Path) -> None:
    with pytest.raises(
        RetiredRuntimeError, match="tradingdatas_fixture_or_v1_port_required"
    ):
        replay.build_replay_report(
            date="20260720",
            reader=None,
            output=tmp_path / "replay.json",
            history=tmp_path / "history.jsonl",
        )

    with pytest.raises(
        RetiredRuntimeError, match="tradingdatas_fixture_or_v1_port_required"
    ):
        auto_pipeline.run_auto_pipeline(reader=None)

    with pytest.raises(CryptoLegacyExecutionRetired, match="legacy_runtime_retired"):
        CryptoWorkflow(reader=None)

    with pytest.raises(
        RetiredRuntimeError, match="tradingdatas_fixture_or_v1_port_required"
    ):
        observation_report.build_observation_report(
            live_report=None,
            review_root=tmp_path / "review",
            review_path=tmp_path / "review.jsonl",
        )

    with pytest.raises(
        RetiredRuntimeError, match="tradingdatas_fixture_or_v1_port_required"
    ):
        calibration.build_calibration_report(
            date="20260720",
            reader=None,
            signals_dir=tmp_path / "signals",
            review_path=tmp_path / "review.jsonl",
            labels_path=tmp_path / "labels.jsonl",
        )

    with pytest.raises(
        RetiredRuntimeError, match="tradingdatas_fixture_or_v1_port_required"
    ):
        ashare_opening_validator.validate_pre_open(reader=None)

    with pytest.raises(
        RetiredRuntimeError, match="tradingdatas_fixture_or_v1_port_required"
    ):
        ashare_preopen_dry_run.run_preopen_dry_run(reader=None)

    assert list(tmp_path.iterdir()) == []


def test_retired_sources_do_not_contain_direct_data_clients() -> None:
    sources = [
        Path(run_simulation.__file__).read_text(encoding="utf-8"),
        Path(opening_validator.__file__).read_text(encoding="utf-8"),
        Path(replay.__file__).read_text(encoding="utf-8"),
        Path(observation_report.__file__).read_text(encoding="utf-8"),
        Path(calibration.__file__).read_text(encoding="utf-8"),
        Path(ashare_opening_validator.__file__).read_text(encoding="utf-8"),
        Path(ashare_preopen_dry_run.__file__).read_text(encoding="utf-8"),
        (Path(__file__).parents[1] / "CNFutures" / "adapter.py").read_text(
            encoding="utf-8"
        ),
    ]
    for source in sources:
        for forbidden in (
            "import sqlite3",
            "sqlite3.connect",
            "SharedSignalsAPIClient(",
            "TradingagentDataReader(",
            "SHAREDSIGNALS_API_URL",
            "SHARED_SIGNALS_DB",
            "TRADINGAGENT_ALLOW_SHARED_SIGNALS_SQLITE",
            "127.0.0.1:8082",
            '"/tushare',
            '"/source_status',
        ):
            assert forbidden not in source


def test_retired_python_modules_exit_before_external_side_effects(
    tmp_path: Path,
) -> None:
    """Exercise real ``python -m`` paths under a side-effect tripwire.

    This closes the gap left by in-process ``main()`` tests: import-time code,
    network, SQLite, subprocess and file writes are all forbidden in the child.
    """

    tripwire = tmp_path / "tripwire"
    tripwire.mkdir()
    (tripwire / "sitecustomize.py").write_text(
        textwrap.dedent(
            """
            import builtins
            import pathlib
            import socket
            import sqlite3
            import subprocess
            import urllib.request

            def blocked(*args, **kwargs):
                raise AssertionError("retired entry touched an external side effect")

            urllib.request.urlopen = blocked
            sqlite3.connect = blocked
            subprocess.Popen = blocked
            socket.socket.connect = blocked

            _open = builtins.open
            def guarded_open(file, mode="r", *args, **kwargs):
                if any(token in str(mode) for token in ("w", "a", "x", "+")):
                    blocked(file, mode)
                return _open(file, mode, *args, **kwargs)
            builtins.open = guarded_open

            _path_open = pathlib.Path.open
            def guarded_path_open(self, mode="r", *args, **kwargs):
                if any(token in str(mode) for token in ("w", "a", "x", "+")):
                    blocked(self, mode)
                return _path_open(self, mode, *args, **kwargs)
            pathlib.Path.open = guarded_path_open
            pathlib.Path.write_text = blocked
            pathlib.Path.write_bytes = blocked
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    root = Path(__file__).resolve().parents[1]
    env = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": os.pathsep.join((str(tripwire), str(root))),
        "REAL_TRADING_ENABLED": "true",
        "SHAREDSIGNALS_API_URL": "http://127.0.0.1:8082",
        "SHARED_SIGNALS_DB": str(tmp_path / "must-not-open.sqlite"),
        "TRADINGAGENT_ALLOW_SHARED_SIGNALS_SQLITE": "1",
    }
    invocations = (
        ("shared.wrappers.run_sim",),
        ("shared.wrappers.tradings_cron_entry", "--job", "job_crypto_daily"),
        ("shared.runtime_test.sharedsignals_evidence_contract",),
        ("shared.runtime_test.opening_acceptance",),
        ("shared.runtime_test.market_health",),
        ("shared.runtime_test.ashare_health_alert",),
        ("shared.runtime_test.cn_futures_live_check",),
        ("CNFutures.run_simulation",),
        ("CNFutures.opening_validator",),
        ("CNFutures.replay",),
        ("CNFutures.observation_report",),
        ("CNFutures.calibration",),
        ("shared.execution.auto_pipeline",),
        ("shared.runtime_test.ashare_opening_validator",),
        ("shared.runtime_test.ashare_preopen_dry_run",),
    )

    for invocation in invocations:
        result = subprocess.run(
            [sys.executable, "-m", *invocation],
            cwd=root,
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == RETIRED_RUNTIME_EXIT_CODE, (
            invocation,
            result.stdout,
            result.stderr,
        )
        assert "legacy_runtime_retired" in result.stderr, invocation
        assert "external side effect" not in result.stderr, invocation

    direct_files = (
        root / "CNFutures/run_simulation.py",
        root / "CNFutures/opening_validator.py",
        root / "CNFutures/replay.py",
        root / "CNFutures/observation_report.py",
        root / "CNFutures/calibration.py",
        root / "shared/runtime_test/ashare_opening_validator.py",
        root / "shared/runtime_test/ashare_preopen_dry_run.py",
    )
    for direct_file in direct_files:
        result = subprocess.run(
            [sys.executable, str(direct_file)],
            cwd=root,
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == RETIRED_RUNTIME_EXIT_CODE, (
            direct_file,
            result.stdout,
            result.stderr,
        )
        assert "legacy_runtime_retired" in result.stderr, direct_file
        assert "external side effect" not in result.stderr, direct_file

    assert not (tmp_path / "must-not-open.sqlite").exists()
