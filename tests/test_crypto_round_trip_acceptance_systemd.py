from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SYSTEMD = ROOT / "Crypto" / "systemd"


def test_g4_acceptance_unit_is_read_only_and_network_closed() -> None:
    unit = (SYSTEMD / "tradingagent-crypto-round-trip-g4-acceptance.service").read_text(
        encoding="utf-8"
    )
    assert "REAL_TRADING_ENABLED=false" in unit
    assert "--epoch-manifest ${ROUND_TRIP_EPOCH_MANIFEST}" in unit
    assert "ReadWritePaths=" not in unit
    assert "IPAddressDeny=any" in unit
    assert "--output-root" not in unit


def test_g4_acceptance_timer_is_installable_and_daily() -> None:
    timer = (SYSTEMD / "tradingagent-crypto-round-trip-g4-acceptance.timer").read_text(
        encoding="utf-8"
    )
    assert "OnCalendar=*-*-* 09:05:30" in timer
    assert "Persistent=true" in timer
    assert "Unit=tradingagent-crypto-round-trip-g4-acceptance.service" in timer
    assert "WantedBy=timers.target" in timer
