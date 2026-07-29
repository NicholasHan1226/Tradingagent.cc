from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SYSTEMD_ROOT = ROOT / "Crypto" / "systemd"
INCREMENTAL_SERVICE = (
    SYSTEMD_ROOT / "tradingagent-crypto-delayed-paper-learning.service"
)
INCREMENTAL_TIMER = SYSTEMD_ROOT / "tradingagent-crypto-delayed-paper-learning.timer"
SCRUB_SERVICE = (
    SYSTEMD_ROOT / "tradingagent-crypto-delayed-paper-learning-scrub.service"
)
SCRUB_TIMER = SYSTEMD_ROOT / "tradingagent-crypto-delayed-paper-learning-scrub.timer"


def test_learning_services_are_offline_simulation_only_and_detached() -> None:
    for service, mode in (
        (INCREMENTAL_SERVICE, "incremental"),
        (SCRUB_SERVICE, "full-scrub"),
    ):
        text = service.read_text(encoding="utf-8")
        assert "Type=oneshot" in text
        assert "User=tradingagent" in text
        assert "Group=tradingagent" in text
        assert "Environment=REAL_TRADING_ENABLED=false" in text
        assert (
            "-m Crypto.delayed_paper_learning_worker "
            f"--mode {mode} "
            "--output-root /var/lib/tradingagent/crypto-delayed-paper"
        ) in text
        assert "RestrictAddressFamilies=AF_UNIX" in text
        assert "IPAddressDeny=any" in text
        assert "IPAddressAllow=" not in text
        assert ("ReadWritePaths=/var/lib/tradingagent/crypto-delayed-paper") in text
        assert "EnvironmentFile=" not in text
        assert "/run/secrets/" not in text
        assert "/etc/tradingagent/" not in text
        assert "network-online.target" not in text
        assert "[Install]" not in text
        lowered = text.lower()
        for forbidden in (
            "18083",
            "binance",
            "catalog",
            "query",
            "token",
            "testnet",
            "livebroker",
            "openai",
            "deepseek",
        ):
            assert forbidden not in lowered


def test_learning_timers_are_installable_but_not_enabled_by_repo() -> None:
    incremental = INCREMENTAL_TIMER.read_text(encoding="utf-8")
    scrub = SCRUB_TIMER.read_text(encoding="utf-8")

    assert "OnCalendar=*-*-* *:1/5:30" in incremental
    assert "Unit=tradingagent-crypto-delayed-paper-learning.service" in incremental
    assert "OnCalendar=*-*-* 03:30:00" in scrub
    assert "RandomizedDelaySec=5m" in scrub
    assert "Unit=tradingagent-crypto-delayed-paper-learning-scrub.service" in scrub
    for text in (incremental, scrub):
        assert "Persistent=false" in text
        assert "[Install]" in text
        assert "WantedBy=timers.target" in text
        assert "systemctl enable" not in text
        assert "systemctl start" not in text
