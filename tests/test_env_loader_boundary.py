from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
ENV_LOADER = ROOT / "shared" / "env_loader.sh"


def _source_loader(
    tmp_path: Path,
    *,
    tradingagent_env: str = "",
    shared_env: str = "",
    inherited_real: str | None = None,
) -> subprocess.CompletedProcess[str]:
    env_file = tmp_path / "tradingagent.env"
    env_file.write_text(tradingagent_env, encoding="utf-8")
    shared_env_file = tmp_path / "shared.env"
    shared_env_file.write_text(shared_env, encoding="utf-8")
    env = dict(os.environ)
    env.pop("TRADINGAGENT_ENV_LOADER_READY", None)
    env.pop("REAL_TRADING_ENABLED", None)
    if inherited_real is not None:
        env["REAL_TRADING_ENABLED"] = inherited_real
    env.update(
        {
            "TRADINGAGENT_ROOT": str(ROOT),
            "TRADINGAGENT_SHARED_ROOT": str(ROOT / "shared"),
            "TRADINGAGENT_ENV_FILE": str(env_file),
            "FINANCE_SHARED_ENV_FILE": str(shared_env_file),
            "TRADINGS_RUNTIME_ROOT": str(tmp_path / "runtime"),
            "TRADINGS_STATE_ROOT": str(tmp_path / "runtime" / "state"),
            "TRADINGS_TMP_ROOT": str(tmp_path / "runtime" / "tmp"),
            "TRADINGS_LOG_ROOT": str(tmp_path / "logs"),
            "TRADINGS_CRON_LOG_ROOT": str(tmp_path / "logs" / "cron"),
            "TRADINGS_GATE_ROOT": str(tmp_path / "gate"),
        }
    )
    return subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; printf "%s|%s" "$REAL_TRADING_ENABLED" "$TZ"',
            "bash",
            str(ENV_LOADER),
        ],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_env_loader_does_not_export_market_data_provider_secrets() -> None:
    source = ENV_LOADER.read_text(encoding="utf-8")
    forbidden = [
        "TRADINGS_" + "TUSHARE_TOKEN",
        "TRADINGS_" + "ALPACA_API_KEY",
        "TRADINGS_" + "ALPACA_SECRET_KEY",
        "TRADINGS_" + "POLYMARKET_API_KEY",
        "TRADINGS_" + "POLYMARKET_SECRET",
        "TRADINGS_" + "FINNHUB_API_KEY",
        "TRADINGS_" + "FMP_API_KEY",
    ]

    assert [token for token in forbidden if token in source] == []


@pytest.mark.parametrize(
    "dangerous_value",
    ["1", "true", "TRUE", "yes", "on", "enabled", "live", "real", "production"],
)
def test_env_loader_rejects_truthy_real_value_loaded_from_env(
    tmp_path: Path, dangerous_value: str
) -> None:
    result = _source_loader(
        tmp_path,
        tradingagent_env=f"export REAL_TRADING_ENABLED={dangerous_value}\n",
    )

    assert result.returncode != 0
    assert "REAL_TRADING_ENABLED" in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize(
    "safe_value", [None, "", "0", "false", "FALSE", "no", "off", "disabled"]
)
def test_env_loader_normalizes_unset_or_false_to_explicit_sim_only(
    tmp_path: Path, safe_value: str | None
) -> None:
    inherited = None if safe_value is None else safe_value
    result = _source_loader(tmp_path, inherited_real=inherited)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "false|Asia/Shanghai"


def test_env_loader_rechecks_sim_only_gate_when_ready_marker_is_preseeded(
    tmp_path: Path,
) -> None:
    env = dict(os.environ)
    env.update(
        {
            "TRADINGAGENT_ENV_LOADER_READY": "1",
            "REAL_TRADING_ENABLED": "true",
        }
    )
    result = subprocess.run(
        ["bash", "-c", 'source "$1"', "bash", str(ENV_LOADER)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "REAL_TRADING_ENABLED" in result.stderr


def test_env_loader_rejects_live_value_from_later_shared_env(tmp_path: Path) -> None:
    result = _source_loader(
        tmp_path,
        tradingagent_env="export REAL_TRADING_ENABLED=false\n",
        shared_env="export REAL_TRADING_ENABLED=true\n",
    )

    assert result.returncode != 0
    assert "REAL_TRADING_ENABLED" in result.stderr


def test_env_loader_rejects_unrecognized_real_trading_value(tmp_path: Path) -> None:
    result = _source_loader(tmp_path, inherited_real="maybe")

    assert result.returncode != 0
    assert "not an accepted sim-only value" in result.stderr


def test_bash_env_mode_blocks_command_before_cron_body_runs(tmp_path: Path) -> None:
    env_file = tmp_path / "tradingagent.env"
    env_file.write_text("export REAL_TRADING_ENABLED=true\n", encoding="utf-8")
    env = dict(os.environ)
    env.pop("TRADINGAGENT_ENV_LOADER_READY", None)
    env.update(
        {
            "BASH_ENV": str(ENV_LOADER),
            "TRADINGAGENT_ENV_FILE": str(env_file),
            "FINANCE_SHARED_ENV_FILE": str(tmp_path / "missing.env"),
        }
    )

    result = subprocess.run(
        ["bash", "-c", "printf cron-body-ran"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert "REAL_TRADING_ENABLED" in result.stderr
