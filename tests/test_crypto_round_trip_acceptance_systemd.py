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


def test_g5_acceptance_unit_is_read_only_and_separate_from_g4() -> None:
    unit = (SYSTEMD / "tradingagent-crypto-round-trip-g5-acceptance.service").read_text(
        encoding="utf-8"
    )
    timer = (SYSTEMD / "tradingagent-crypto-round-trip-g5-acceptance.timer").read_text(
        encoding="utf-8"
    )
    assert "REAL_TRADING_ENABLED=false" in unit
    assert "crypto-delayed-paper-round-trip-g5.env" in unit
    assert "crypto-delayed-paper-round-trip-g4.env" not in unit
    assert "--epoch-manifest ${ROUND_TRIP_EPOCH_MANIFEST}" in unit
    assert "ReadWritePaths=" not in unit
    assert "IPAddressDeny=any" in unit
    assert "--output-root" not in unit
    assert "Unit=tradingagent-crypto-round-trip-g5-acceptance.service" in timer
    assert "WantedBy=timers.target" in timer
