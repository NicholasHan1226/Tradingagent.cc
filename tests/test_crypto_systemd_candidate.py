from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SYSTEMD_ROOT = ROOT / "Crypto" / "systemd"
SERVICE = SYSTEMD_ROOT / "tradingagent-crypto-delayed-paper.service"
TIMER = SYSTEMD_ROOT / "tradingagent-crypto-delayed-paper.timer"
TOKEN_TMPFILES = SYSTEMD_ROOT / "tradingagent-crypto-read-token.tmpfiles.conf"


def test_crypto_runtime_service_is_loopback_only_and_simulation_only() -> None:
    text = SERVICE.read_text(encoding="utf-8")

    assert "Type=oneshot" in text
    assert "User=tradingagent" in text
    assert "Group=tradingagent" in text
    assert "Environment=REAL_TRADING_ENABLED=false" in text
    assert "Environment=PYTHONDONTWRITEBYTECODE=1" in text
    assert "Environment=PYTHONUNBUFFERED=1" in text
    assert (
        "--runtime-manifest /etc/tradingagent/crypto-delayed-paper.runtime.json"
    ) in text
    assert "-m Crypto.delayed_paper_epoch_runtime" in text
    assert "-m Crypto.delayed_paper_runtime " not in text
    assert (
        "--token-file /run/secrets/tradingagent/tradingdatas-crypto-read.token"
    ) in text
    assert (
        "--epoch-manifest /etc/tradingagent/crypto-delayed-paper.epoch.json"
    ) in text
    assert "--output-root" not in text
    assert "StateDirectory=tradingagent/crypto-delayed-paper-epochs" in text
    assert "StateDirectoryMode=0700" in text
    assert "UMask=0077" in text
    assert "TimeoutStartSec=180s" in text
    assert "NoNewPrivileges=true" in text
    assert "PrivateTmp=true" in text
    assert "PrivateDevices=true" in text
    assert "ProtectSystem=strict" in text
    assert "ProtectHome=true" in text
    assert "ProtectKernelTunables=true" in text
    assert "ProtectKernelModules=true" in text
    assert "ProtectKernelLogs=true" in text
    assert "ProtectControlGroups=true" in text
    assert "IPAddressDeny=any" in text
    assert "IPAddressAllow=localhost" in text
    assert "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6" in text
    assert "ReadOnlyPaths=/var/lib/tradingagent/crypto-delayed-paper" in text
    assert "ReadWritePaths=/var/lib/tradingagent/crypto-delayed-paper-epochs" in text
    assert (
        "AssertPathExists=/etc/tradingagent/crypto-delayed-paper.runtime.json"
    ) in text
    assert (
        "AssertPathExists=/run/secrets/tradingagent/tradingdatas-crypto-read.token"
    ) in text
    assert "AssertPathExists=/etc/tradingagent/crypto-delayed-paper.epoch.json" in text
    assert "AssertPathExists=/var/lib/tradingagent/crypto-delayed-paper" in text
    assert "ConditionPathExists=" not in text
    assert "EnvironmentFile=" not in text
    assert "tradingdatas-api.service" not in text
    assert "tradingdatas-crypto" not in text.replace(
        "tradingdatas-crypto-read.token",
        "",
    )
    assert "[Install]" not in text

    lowered = text.lower()
    for forbidden in (
        "crypto.spot.binance",
        "api.binance",
        "sqlite",
        "testnet",
        "livebroker",
        "deepseek",
        "openai",
        "anthropic",
        "catalog_version",
        "access_policy",
        "base_url",
        "real_trading_enabled=true",
    ):
        assert forbidden not in lowered


def test_crypto_runtime_timers_are_installable_but_not_enabled_by_repo() -> None:
    text = TIMER.read_text(encoding="utf-8")

    assert "OnCalendar=*-*-* *:0/5:55" in text
    assert "AccuracySec=1s" in text
    assert "RandomizedDelaySec=3s" in text
    assert "Persistent=false" in text
    assert "Unit=tradingagent-crypto-delayed-paper.service" in text
    assert "[Install]" in text
    assert "WantedBy=timers.target" in text
    assert "systemctl enable" not in text
    assert "systemctl start" not in text
    assert "OnBootSec=" not in text
    assert "OnUnitActiveSec=" not in text
    assert sorted(
        path.name
        for path in SYSTEMD_ROOT.glob("tradingagent-crypto-delayed-paper*.timer")
    ) == [
        "tradingagent-crypto-delayed-paper-learning-scrub.timer",
        "tradingagent-crypto-delayed-paper-learning.timer",
        "tradingagent-crypto-delayed-paper.timer",
    ]


def test_crypto_runtime_token_is_recreated_from_canonical_source() -> None:
    text = TOKEN_TMPFILES.read_text(encoding="utf-8")

    assert (
        "C /run/secrets/tradingagent/tradingdatas-crypto-read.token "
        "0600 tradingagent tradingagent - "
        "/etc/tradingagent/tradingdatas-crypto-read.token"
    ) in text
    assert "token=" not in text.lower()
    assert "bearer" not in text.lower()


TEN_SYMBOL_SERVICE = SYSTEMD_ROOT / "tradingagent-crypto-ten-symbol-observation.service"
TEN_SYMBOL_TIMER = SYSTEMD_ROOT / "tradingagent-crypto-ten-symbol-observation.timer"


def test_crypto_ten_symbol_observation_service_is_loopback_only_sim_only() -> None:
    text = TEN_SYMBOL_SERVICE.read_text(encoding="utf-8")

    assert "Type=oneshot" in text
    assert "User=tradingagent" in text
    assert "Group=tradingagent" in text
    assert "Environment=REAL_TRADING_ENABLED=false" in text
    assert "Environment=PYTHONDONTWRITEBYTECODE=1" in text
    assert "Environment=PYTHONUNBUFFERED=1" in text
    assert "-m Crypto.ten_symbol_observation_runtime" in text
    assert (
        "--runtime-manifest /etc/tradingagent/crypto-ten-symbol-observation.runtime.json"
    ) in text
    assert (
        "--token-file /run/secrets/tradingagent/tradingdatas-crypto-read.token"
    ) in text
    assert "--output-root" not in text
    assert "UMask=0077" in text
    assert "TimeoutStartSec=180" in text
    assert "NoNewPrivileges=true" in text
    assert "PrivateTmp=true" in text
    assert "PrivateDevices=true" in text
    assert "ProtectSystem=strict" in text
    assert "ProtectHome=true" in text
    assert "ProtectKernelTunables=true" in text
    assert "ProtectKernelModules=true" in text
    assert "ProtectKernelLogs=true" in text
    assert "ProtectControlGroups=true" in text
    assert "IPAddressDeny=any" in text
    assert "IPAddressAllow=localhost" in text
    assert "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6" in text
    assert "StateDirectory=tradingagent/crypto-ten-symbol-observation" in text
    assert "StateDirectoryMode=0700" in text
    assert (
        "ReadWritePaths=/var/lib/tradingagent/crypto-ten-symbol-observation"
    ) in text
    assert "ReadWritePaths=/var/lib/tradingagent/crypto-delayed-paper" not in text
    assert (
        "AssertPathExists=/etc/tradingagent/crypto-ten-symbol-observation.runtime.json"
    ) in text
    assert (
        "AssertPathExists=/run/secrets/tradingagent/tradingdatas-crypto-read.token"
    ) in text
    assert "ConditionPathExists=" not in text
    assert "EnvironmentFile=" not in text
    assert "[Install]" not in text

    lowered = text.lower()
    for forbidden in (
        "crypto.spot.binance",
        "api.binance",
        "sqlite",
        "testnet",
        "livebroker",
        "deepseek",
        "openai",
        "anthropic",
        "catalog_version",
        "access_policy",
        "base_url",
        "real_trading_enabled=true",
    ):
        assert forbidden not in lowered


def test_crypto_ten_symbol_observation_timer_is_install_default_not_enabled() -> None:
    text = TEN_SYMBOL_TIMER.read_text(encoding="utf-8")

    assert "OnCalendar=*-*-* *:2/5:25" in text
    assert "AccuracySec=1s" in text
    assert "RandomizedDelaySec=3s" in text
    assert "Persistent=false" in text
    assert "Unit=tradingagent-crypto-ten-symbol-observation.service" in text
    assert "[Install]" in text
    assert "WantedBy=timers.target" in text
    assert "systemctl enable" not in text
    assert "systemctl start" not in text
    assert "OnBootSec=" not in text
    assert "OnUnitActiveSec=" not in text


def test_crypto_ten_symbol_timer_is_staggered_after_existing_core_budget() -> None:
    text = TEN_SYMBOL_TIMER.read_text(encoding="utf-8")
    service = TEN_SYMBOL_SERVICE.read_text(encoding="utf-8")
    core_text = TIMER.read_text(encoding="utf-8")

    assert "OnCalendar=*-*-* *:0/5:55" in core_text
    assert "OnCalendar=*-*-* *:2/5:25" in text
    assert "TimeoutStartSec=180" in service
