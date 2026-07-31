from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SYSTEMD = ROOT / "Crypto" / "systemd"
G4_ROOT = (
    "/var/lib/tradingagent/crypto-delayed-paper-epochs/"
    "crypto-delayed-paper-round-trip-epoch-g4-20260731"
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
