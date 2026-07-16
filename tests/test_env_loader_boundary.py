from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
ENV_LOADER = ROOT / "shared" / "env_loader.sh"
ENV_EXAMPLE = ROOT / ".env.example"
SHAREDSIGNALS_V1_CONFIG = (
    "SHAREDSIGNALS_API_URL",
    "SHAREDSIGNALS_CATALOG_VERSION",
    "SHAREDSIGNALS_ACCESS_POLICY_ID",
    "SHAREDSIGNALS_MARKET_PULSE_DATASET_IDS_JSON",
)


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
    env.pop("ASHARE_SIM_HERMES_ENABLED", None)
    env.pop("ASHARE_SIM_WEBHOOK_ENABLED", None)
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
            'source "$1"; printf "%s|%s|%s|%s" "$REAL_TRADING_ENABLED" "$TZ" "$ASHARE_SIM_HERMES_ENABLED" "$ASHARE_SIM_WEBHOOK_ENABLED"',
            "bash",
            str(ENV_LOADER),
        ],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _source_loader_v1_config(
    tmp_path: Path,
    *,
    inherited_config: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env_file = tmp_path / "tradingagent.env"
    env_file.write_text("", encoding="utf-8")
    shared_env_file = tmp_path / "shared.env"
    shared_env_file.write_text("", encoding="utf-8")
    env = dict(os.environ)
    env.pop("TRADINGAGENT_ENV_LOADER_READY", None)
    env.pop("REAL_TRADING_ENABLED", None)
    for variable_name in SHAREDSIGNALS_V1_CONFIG:
        env.pop(variable_name, None)
    env.update(inherited_config or {})
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
            'source "$1"; for name in "${@:2}"; do '
            'printf "%s:%s\\n" "${!name+x}" "${!name-}"; done',
            "bash",
            str(ENV_LOADER),
            *SHAREDSIGNALS_V1_CONFIG,
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


def test_env_loader_does_not_reexport_legacy_deepseek_secret_name() -> None:
    source = ENV_LOADER.read_text(encoding="utf-8")

    assert "TRADINGS_" + "DEEPSEEK_API_KEY" not in source


def test_sharedsignals_v1_configuration_has_no_implicit_localhost_default() -> None:
    forbidden_default = "http://127.0.0.1:8082"

    assert forbidden_default not in ENV_LOADER.read_text(encoding="utf-8")
    assert forbidden_default not in ENV_EXAMPLE.read_text(encoding="utf-8")


def test_env_example_requires_all_sharedsignals_v1_configuration_explicitly() -> None:
    assignments = {
        name.strip(): value.strip()
        for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
        if line and not line.lstrip().startswith("#") and "=" in line
        for name, value in [line.split("=", 1)]
    }

    assert {name: assignments.get(name) for name in SHAREDSIGNALS_V1_CONFIG} == {
        name: "" for name in SHAREDSIGNALS_V1_CONFIG
    }


def test_env_loader_exports_missing_sharedsignals_v1_configuration_as_empty(
    tmp_path: Path,
) -> None:
    result = _source_loader_v1_config(tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["x:" for _ in SHAREDSIGNALS_V1_CONFIG]


def test_env_loader_preserves_explicit_sharedsignals_v1_configuration(
    tmp_path: Path,
) -> None:
    explicit_config = {
        "SHAREDSIGNALS_API_URL": "https://sharedsignals.fixture.invalid",
        "SHAREDSIGNALS_CATALOG_VERSION": "catalog-fixture-v1",
        "SHAREDSIGNALS_ACCESS_POLICY_ID": "ta-paper-read-v1",
        "SHAREDSIGNALS_MARKET_PULSE_DATASET_IDS_JSON": '{"ashare":"market-pulse-v1"}',
    }
    result = _source_loader_v1_config(tmp_path, inherited_config=explicit_config)

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        f"x:{explicit_config[name]}" for name in SHAREDSIGNALS_V1_CONFIG
    ]


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
    assert result.stdout == "false|Asia/Shanghai|0|0"


@pytest.mark.parametrize(
    "variable_name", ["ASHARE_SIM_HERMES_ENABLED", "ASHARE_SIM_WEBHOOK_ENABLED"]
)
@pytest.mark.parametrize("dangerous_value", ["1", "true", "yes", "on", "enabled"])
def test_env_loader_rejects_external_sim_bridge_enablement_from_env(
    tmp_path: Path, variable_name: str, dangerous_value: str
) -> None:
    result = _source_loader(
        tmp_path,
        tradingagent_env=f"export {variable_name}={dangerous_value}\n",
    )

    assert result.returncode != 0
    assert variable_name in result.stderr
    assert "external Mini/Hermes simulation bridge" in result.stderr
    assert result.stdout == ""


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


def test_bash_env_mode_never_executes_arbitrary_env_file_commands(
    tmp_path: Path,
) -> None:
    sentinel = tmp_path / "must-not-run"
    env_file = tmp_path / "tradingagent.env"
    env_file.write_text(
        f"touch {sentinel!s}\nexport REAL_TRADING_ENABLED=true\n",
        encoding="utf-8",
    )
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
    assert not sentinel.exists()
