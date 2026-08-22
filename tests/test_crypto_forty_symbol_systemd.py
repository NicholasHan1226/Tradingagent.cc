from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SYSTEMD_ROOT = ROOT / "Crypto" / "systemd"
SERVICE = SYSTEMD_ROOT / "tradingagent-crypto-forty-symbol-observation.service"
TIMER = SYSTEMD_ROOT / "tradingagent-crypto-forty-symbol-observation.timer"


def test_forty_symbol_observation_service_is_simulation_only_and_isolated() -> None:
    text = SERVICE.read_text(encoding="utf-8")
    assert "Type=oneshot" in text
    assert "User=tradingagent" in text
    assert "Group=tradingagent" in text
    assert "Environment=REAL_TRADING_ENABLED=false" in text
    assert "-m Crypto.forty_symbol_observation_runtime" in text
    assert (
        "--runtime-manifest /etc/tradingagent/crypto-forty-symbol-observation.runtime.json"
    ) in text
    assert "StateDirectory=tradingagent/crypto-40-symbol-observation" in text
    assert "ReadWritePaths=/var/lib/tradingagent/crypto-40-symbol-observation" in text
    assert "ReadWritePaths=/var/lib/tradingagent/crypto-ten-symbol-observation" not in text
    assert "IPAddressDeny=any" in text
    assert "IPAddressAllow=localhost" in text
    assert "NoNewPrivileges=true" in text
    assert "[Install]" not in text
    lowered = text.lower()
    for forbidden in (
        "api.binance",
        "sqlite",
        "testnet",
        "livebroker",
        "real_trading_enabled=true",
    ):
        assert forbidden not in lowered


def test_forty_symbol_observation_timer_is_installable_and_not_enabled_by_repo() -> None:
    text = TIMER.read_text(encoding="utf-8")
    assert "OnCalendar=*-*-* *:3/5:45" in text
    assert "AccuracySec=1s" in text
    assert "RandomizedDelaySec=3s" in text
    assert "Persistent=false" in text
    assert "Unit=tradingagent-crypto-forty-symbol-observation.service" in text
    assert "[Install]" in text
    assert "WantedBy=timers.target" in text
    assert "systemctl enable" not in text
    assert "systemctl start" not in text
