from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SYSTEMD = ROOT / "Crypto" / "systemd"
G4_ROOT = (
    "/var/lib/tradingagent/crypto-delayed-paper-epochs/"
    "crypto-delayed-paper-round-trip-epoch-g4-20260731"
)
G5_ROOT = (
    "/var/lib/tradingagent/crypto-delayed-paper-epochs/"
    "crypto-delayed-paper-round-trip-epoch-g5-20260801"
)


def _unit(name: str) -> str:
    return (SYSTEMD / name).read_text(encoding="utf-8")


def test_g4_learning_units_are_epoch_pinned_and_simulation_only() -> None:
    for name in (
        "tradingagent-crypto-round-trip-g4-learning.service",
        "tradingagent-crypto-round-trip-g4-learning-scrub.service",
    ):
        unit = _unit(name)
        assert "REAL_TRADING_ENABLED=false" in unit
        assert "--epoch-manifest ${ROUND_TRIP_EPOCH_MANIFEST}" in unit
        assert "--output-root" not in unit
        assert "IPAddressDeny=any" in unit
        assert f"AssertPathExists={G4_ROOT}/evolution" in unit
        assert f"ReadWritePaths={G4_ROOT}/evolution" in unit
        assert f"ReadOnlyPaths={G4_ROOT}" not in unit
        assert "crypto-delayed-paper-epoch-g2" not in unit


def test_g4_learning_timers_are_installable_but_default_passive() -> None:
    for name, unit_name in (
        (
            "tradingagent-crypto-round-trip-g4-learning.timer",
            "tradingagent-crypto-round-trip-g4-learning.service",
        ),
        (
            "tradingagent-crypto-round-trip-g4-learning-scrub.timer",
            "tradingagent-crypto-round-trip-g4-learning-scrub.service",
        ),
    ):
        timer = _unit(name)
        assert "WantedBy=timers.target" in timer
        assert f"Unit={unit_name}" in timer
        assert "Persistent=true" not in timer


def test_g5_learning_units_are_epoch_pinned_and_simulation_only() -> None:
    for name, mode in (
        ("tradingagent-crypto-round-trip-g5-learning.service", "incremental"),
        ("tradingagent-crypto-round-trip-g5-learning-scrub.service", "full-scrub"),
    ):
        unit = _unit(name)
        assert "REAL_TRADING_ENABLED=false" in unit
        assert "/etc/tradingagent/crypto-delayed-paper-round-trip-g5.env" in unit
        assert "/etc/tradingagent/crypto-delayed-paper-round-trip-g4.env" not in unit
        assert "After=tradingagent-crypto-round-trip-g5-delayed-paper.service" in unit
        assert f"--mode {mode}" in unit
        assert "--epoch-manifest ${ROUND_TRIP_EPOCH_MANIFEST}" in unit
        assert "--output-root" not in unit
        assert "IPAddressDeny=any" in unit
        assert f"AssertPathExists={G5_ROOT}/evolution" in unit
        assert f"ReadWritePaths={G5_ROOT}/evolution" in unit
        assert G4_ROOT not in unit
        assert "round-trip-epoch-g4" not in unit
        assert "crypto-delayed-paper-epoch-g2" not in unit


def test_g5_learning_timers_are_installable_but_default_passive() -> None:
    for name, unit_name in (
        (
            "tradingagent-crypto-round-trip-g5-learning.timer",
            "tradingagent-crypto-round-trip-g5-learning.service",
        ),
        (
            "tradingagent-crypto-round-trip-g5-learning-scrub.timer",
            "tradingagent-crypto-round-trip-g5-learning-scrub.service",
        ),
    ):
        timer = _unit(name)
        assert "WantedBy=timers.target" in timer
        assert f"Unit={unit_name}" in timer
        assert "Persistent=true" not in timer


def test_round_trip_learning_scrub_units_keep_the_existing_120_second_timeout() -> None:
    for generation in (4, 5):
        scrub = _unit(
            f"tradingagent-crypto-round-trip-g{generation}-learning-scrub.service"
        )
        incremental = _unit(
            f"tradingagent-crypto-round-trip-g{generation}-learning.service"
        )
        assert "TimeoutStartSec=120s" in scrub
        assert "TimeoutStartSec=45s" in incremental
