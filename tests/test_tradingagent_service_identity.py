from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SYSTEMD_ROOT = REPO_ROOT / "deploy" / "systemd"
FRONT_SERVICE = SYSTEMD_ROOT / "tradingagent-front-api.service"
OBSERVATION_SERVICE = SYSTEMD_ROOT / "tradingagent-ashare-observation.service"
SYSUSERS = SYSTEMD_ROOT / "tradingagent-runtime.sysusers.conf"


def _text(path: Path) -> str:
    assert path.is_file(), f"missing tracked deployment artifact: {path}"
    return path.read_text(encoding="utf-8")


def test_sysusers_contract_creates_a_non_login_dedicated_identity() -> None:
    text = _text(SYSUSERS)
    lines = {
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert lines == {
        "g tradingagent -",
        (
            'u tradingagent -:tradingagent "TradingAgent service identity" '
            "/nonexistent /usr/sbin/nologin"
        ),
    }
    assert "marketgraph" not in text.lower()
    assert "token" not in text.lower()


def test_front_api_uses_dedicated_primary_identity_and_read_only_release() -> None:
    text = _text(FRONT_SERVICE)

    for required in (
        "User=tradingagent",
        "Group=tradingagent",
        # Transitional read compatibility only; the process UID and primary
        # group remain dedicated and the sandbox has no writable legacy path.
        "SupplementaryGroups=marketgraph",
        "WorkingDirectory=/opt/investment/releases/tradingagent/current/front",
        "Environment=FINANCE_WORKSPACE_ROOT=/opt/investment/tradingagent",
        "Environment=TRADING_AGENT_SNAPSHOT_HOST=127.0.0.1",
        "Environment=TRADING_AGENT_SNAPSHOT_PORT=8787",
        "Environment=TRADING_AGENT_RUNTIME_PYTHON=/opt/investment/tools/venvs/tradingagent-observation-py312-pyyaml603-v1/bin/python3",
        "Environment=TRADING_AGENT_RUNTIME_READER=/opt/investment/releases/tradingagent/current/tools/read_runtime_observations.py",
        (
            "Environment=TRADING_COPILOT_EVENT_TIMELINE_DIR="
            "/var/lib/tradingagent/trading-copilot/event-timeline"
        ),
        (
            "Environment=TRADING_COPILOT_TRACKING_UNIVERSE_PATH="
            "/var/lib/tradingagent/trading-copilot/tracking-universe.json"
        ),
        "Environment=REAL_TRADING_ENABLED=false",
        "Environment=TRADINGDATAS_API_URL=",
        "Environment=MARKETGRAPH_API_URL=",
        (
            "ExecStart=/opt/investment/tools/node-v24.4.1/bin/node "
            "/opt/investment/releases/tradingagent/current/front/dist-server/"
            "server/tradingAgentSnapshotHttp.js"
        ),
        "NoNewPrivileges=true",
        "PrivateTmp=true",
        "PrivateDevices=true",
        "ProtectSystem=strict",
        "ProtectHome=true",
        "IPAddressDeny=any",
        "IPAddressAllow=localhost",
        "InaccessiblePaths=/run/secrets/tradingagent",
        "ReadOnlyPaths=/opt/investment/releases/tradingagent",
        "ReadOnlyPaths=/opt/investment/tradingagent",
        "ReadOnlyPaths=/var/lib/tradingagent/trading-copilot",
    ):
        assert required in text

    lowered = text.lower()
    for forbidden in (
        "sharedsignals",
        ":8082",
        "/tushare",
        "/source_status",
        "sqlite",
        "tradingdatas-read.token",
        "authorization",
        "broker",
        "deepseek",
        "openai",
        "llm",
        "0.0.0.0",
        "readwritepaths=",
    ):
        assert forbidden not in lowered


def test_active_deployment_units_have_no_legacy_data_route_or_secret_value() -> None:
    combined = "\n".join(
        _text(path) for path in (FRONT_SERVICE, OBSERVATION_SERVICE)
    ).lower()

    assert ":18082" not in combined  # authority stays in the external manifest
    assert ":8082" not in combined
    assert "sharedsignals_api_url" not in combined
    assert "/tushare" not in combined
    assert "/source_status" not in combined
    assert "sqlite" not in combined
    assert "real_trading_enabled=true" not in combined
    assert "environment=real_trading_enabled=false" in combined
