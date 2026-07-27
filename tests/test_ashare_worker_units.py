from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SYSTEMD_ROOT = REPO_ROOT / "deploy" / "systemd"
SERVICE = SYSTEMD_ROOT / "tradingagent-ashare-observation.service"
TIMER = SYSTEMD_ROOT / "tradingagent-ashare-observation.timer"
ENV_EXAMPLE = SYSTEMD_ROOT / "tradingagent-ashare-worker.env.example"
TMPFILES = SYSTEMD_ROOT / "tradingagent-runtime.tmpfiles.conf"
OBSERVATION_REQUIREMENTS = (
    REPO_ROOT / "deploy" / "ashare-observation-requirements.txt"
)
OBSERVATION_PYTHON = (
    "/opt/investment/tools/venvs/"
    "tradingagent-observation-py312-pyyaml603-v1/bin/python3"
)


def _text(path: Path) -> str:
    assert path.is_file(), f"missing candidate artifact: {path}"
    return path.read_text(encoding="utf-8")


def _environment(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        assert separator == "="
        assert key and key not in values
        values[key] = value
    return values


def test_observation_runtime_dependency_is_exactly_version_and_hash_locked() -> None:
    requirements = _text(OBSERVATION_REQUIREMENTS)
    assert "PyYAML==6.0.3" in requirements
    assert (
        "--hash=sha256:"
        "ba1cc08a7ccde2d2ec775841541641e4548226580ab850948cbfda66a1befcdc"
    ) in requirements
    assert ">=" not in requirements


def test_observation_service_is_dedicated_sim_only_and_sandboxed() -> None:
    text = _text(SERVICE)

    for required in (
        "Type=oneshot",
        "User=tradingagent",
        "Group=tradingagent",
        "EnvironmentFile=/etc/tradingagent/ashare-worker.env",
        "Environment=REAL_TRADING_ENABLED=false",
        "Environment=MARKETGRAPH_MODE=mg_off",
        "ConditionPathExists=/run/secrets/tradingagent/tradingdatas-read.token",
        f"ConditionPathExists={OBSERVATION_PYTHON}",
        (
            f"ExecStartPre={OBSERVATION_PYTHON} "
            "/opt/investment/releases/tradingagent/current/tools/"
            "audit_ashare_worker_runtime.py"
        ),
        (
            f"ExecStartPre={OBSERVATION_PYTHON} "
            "/opt/investment/releases/tradingagent/current/tools/"
            "build_ashare_observation_manifest.py"
        ),
        (
            f"ExecStart={OBSERVATION_PYTHON} "
            "/opt/investment/releases/tradingagent/current/tools/"
            "run_ashare_observation.py"
        ),
        f"--python-runtime {OBSERVATION_PYTHON}",
        "ProtectSystem=strict",
        "ProtectHome=true",
        "NoNewPrivileges=true",
        "PrivateTmp=true",
        "PrivateDevices=true",
        "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
        "IPAddressDeny=any",
        "IPAddressAllow=localhost",
        "ReadOnlyPaths=/opt/investment/releases/tradingagent",
        (
            "ReadOnlyPaths=/opt/investment/tools/venvs/"
            "tradingagent-observation-py312-pyyaml603-v1"
        ),
        "ReadOnlyPaths=/run/secrets/tradingagent",
        "ReadWritePaths=/var/lib/tradingagent/ashare-observation",
        "ReadWritePaths=/run/tradingagent/ashare-observation",
        "ReadWritePaths=/var/log/tradingagent/ashare-observation",
        "StateDirectory=tradingagent/ashare-observation",
        "RuntimeDirectory=tradingagent/ashare-observation",
        "LogsDirectory=tradingagent/ashare-observation",
        "UMask=0077",
    ):
        assert required in text

    lowered = text.lower()
    for forbidden in (
        "broker",
        "deepseek",
        "openai",
        "llm",
        "cloudflare",
        "0.0.0.0",
        "public ingress",
        "/tushare",
        "/source_status",
        "sqlite",
        ":8082",
        "/opt/tradingagent",
    ):
        assert forbidden not in lowered

    exec_start_pre = next(
        line
        for line in text.splitlines()
        if line.startswith("ExecStartPre=")
        and "audit_ashare_worker_runtime.py" in line
    )
    manifest_exec_start_pre = next(
        line
        for line in text.splitlines()
        if line.startswith("ExecStartPre=")
        and "build_ashare_observation_manifest.py" in line
    )
    exec_start = next(
        line for line in text.splitlines() if line.startswith("ExecStart=")
    )
    assert (
        "--token-file /run/secrets/tradingagent/tradingdatas-read.token" in exec_start
    )
    assert "--base-url ${TRADINGDATAS_API_URL}" in manifest_exec_start_pre
    assert (
        "--access-policy-id ${TRADINGDATAS_ACCESS_POLICY_ID}"
        in manifest_exec_start_pre
    )
    assert (
        "--token-file /run/secrets/tradingagent/tradingdatas-read.token"
        in manifest_exec_start_pre
    )
    assert (
        "--manifest-root ${ASHARE_OBSERVATION_MANIFEST_ROOT}"
        in manifest_exec_start_pre
    )
    for variable in (
        "${ASHARE_OBSERVATION_STATE_ROOT}",
        "${ASHARE_OBSERVATION_RUNTIME_ROOT}",
        "${ASHARE_OBSERVATION_LOG_ROOT}",
    ):
        assert variable in exec_start_pre
        assert variable in exec_start
    assert "--state-root /var/lib/tradingagent" not in exec_start_pre
    assert "--runtime-root /run/tradingagent" not in exec_start_pre
    assert "--log-root /var/log/tradingagent" not in exec_start_pre


def test_observation_timer_is_a_non_enableable_code_candidate() -> None:
    text = _text(TIMER)

    assert "Unit=tradingagent-ashare-observation.service" in text
    assert "OnCalendar=" in text
    assert "Persistent=false" in text
    assert "[Install]" not in text
    assert "WantedBy=" not in text


def test_worker_environment_is_path_only_simulation_configuration() -> None:
    values = _environment(_text(ENV_EXAMPLE))

    assert values == {
        "ASHARE_OBSERVATION_LOG_ROOT": "/var/log/tradingagent/ashare-observation",
        "ASHARE_OBSERVATION_MANIFEST": (
            "/var/lib/tradingagent/ashare-observation/manifests/current.json"
        ),
        "ASHARE_OBSERVATION_MANIFEST_ROOT": (
            "/var/lib/tradingagent/ashare-observation/manifests"
        ),
        "ASHARE_OBSERVATION_RUNTIME_ROOT": "/run/tradingagent/ashare-observation",
        "ASHARE_OBSERVATION_STATE_ROOT": "/var/lib/tradingagent/ashare-observation",
        "MARKETGRAPH_MODE": "mg_off",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
        "REAL_TRADING_ENABLED": "false",
        "TRADINGDATAS_ACCESS_POLICY_ID": "ta-ashare-observation-read-v1",
        "TRADINGDATAS_API_URL": "http://127.0.0.1:18082",
    }
    encoded = "\n".join(values).lower()
    for forbidden in ("token=", "api_key", "broker", "llm", "deepseek", "0.0.0.0"):
        assert forbidden not in encoded


def test_tmpfiles_keep_state_runtime_log_and_secret_parent_separate() -> None:
    lines = {
        line.strip()
        for line in _text(TMPFILES).splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert lines == {
        "d /var/lib/tradingagent/ashare-observation 0700 tradingagent tradingagent -",
        "d /run/tradingagent/ashare-observation 0700 tradingagent tradingagent -",
        "d /var/log/tradingagent/ashare-observation 0700 tradingagent tradingagent -",
        "d /run/secrets/tradingagent 0710 root tradingagent -",
    }
    assert not any("tradingdatas-read.token" in line for line in lines)
