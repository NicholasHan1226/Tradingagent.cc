from __future__ import annotations

from pathlib import Path
import subprocess
from typing import Dict


ROOT = Path(__file__).resolve().parents[1]
SYSTEMD = ROOT / "Crypto" / "systemd"


def _unit_settings(name: str) -> Dict[str, str]:
    settings: Dict[str, str] = {}
    for line in (SYSTEMD / name).read_text(encoding="utf-8").splitlines():
        if "=" not in line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        settings[key] = value
    return settings


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


def test_g5_acceptance_orders_after_the_accumulator_writer() -> None:
    acceptance = _unit_settings(
        "tradingagent-crypto-round-trip-g5-acceptance.service"
    )
    accumulator = _unit_settings(
        "tradingagent-crypto-round-trip-g5-delayed-paper.service"
    )

    assert acceptance["After"] == (
        "tradingagent-crypto-round-trip-g5-delayed-paper.service"
    )
    assert accumulator["After"] == "network-online.target"
    assert accumulator["ExecCondition"].startswith("/bin/sh -c")
    assert "ActiveState" in accumulator["ExecCondition"]
    assert "Conflicts" not in acceptance
    assert "Conflicts" not in accumulator


def _accumulator_condition_allows(acceptance_state: str, condition: str) -> bool:
    """Model the state allow-list encoded by the installed ExecCondition."""

    assert "inactive|failed" in condition
    return acceptance_state in {"inactive", "failed"}


def _simulate_start_order(first: str) -> tuple[str, str]:
    """Return outcomes for one transaction using the two unit declarations."""

    acceptance = _unit_settings(
        "tradingagent-crypto-round-trip-g5-acceptance.service"
    )
    accumulator = _unit_settings(
        "tradingagent-crypto-round-trip-g5-delayed-paper.service"
    )
    if first == "acceptance":
        # Acceptance starts while the writer is inactive; a later writer start
        # sees acceptance active and ExecCondition skips only that occurrence.
        assert "tradingagent-crypto-round-trip-g5-delayed-paper.service" in acceptance[
            "After"
        ]
        assert not _accumulator_condition_allows("active", accumulator["ExecCondition"])
        return ("active", "skipped")
    if first == "accumulator":
        # Acceptance's After= edge queues it behind the active writer; no
        # Conflicts= edge exists to stop either service.
        assert "tradingagent-crypto-round-trip-g5-delayed-paper.service" in acceptance[
            "After"
        ]
        assert "Conflicts" not in acceptance
        return ("queued", "active")
    raise AssertionError(first)


def test_g5_acceptance_first_skips_only_the_conflicting_accumulator() -> None:
    """An active acceptance makes the late accumulator occurrence skip cleanly."""

    condition = _unit_settings(
        "tradingagent-crypto-round-trip-g5-delayed-paper.service"
    )["ExecCondition"]
    assert _accumulator_condition_allows("active", condition) is False
    assert _accumulator_condition_allows("activating", condition) is False
    assert _accumulator_condition_allows("deactivating", condition) is False
    assert _accumulator_condition_allows("inactive", condition) is True
    assert _accumulator_condition_allows("failed", condition) is True
    assert _simulate_start_order("acceptance") == ("active", "skipped")


def test_g5_exec_condition_returns_skip_for_each_active_state(tmp_path: Path) -> None:
    """Execute the unit's exact shell condition against a read-only fake manager."""

    condition = _unit_settings(
        "tradingagent-crypto-round-trip-g5-delayed-paper.service"
    )["ExecCondition"]
    shell = (
        condition.removeprefix("/bin/sh -c ")
        .strip()
        .strip("'")
        .replace("$$", "$")
    )
    fake_systemctl = tmp_path / "systemctl"
    fake_systemctl.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$FAKE_ACCEPTANCE_STATE\"\n",
        encoding="utf-8",
    )
    fake_systemctl.chmod(0o700)
    for state, expected in (
        ("active", 1),
        ("activating", 1),
        ("deactivating", 1),
        ("inactive", 0),
        ("failed", 0),
    ):
        result = subprocess.run(
            ["/bin/sh", "-c", shell],
            env={
                "PATH": f"{tmp_path}:/usr/bin:/bin",
                "FAKE_ACCEPTANCE_STATE": state,
            },
            check=False,
        )
        assert result.returncode == expected


def test_g5_accumulator_first_queues_acceptance_without_termination() -> None:
    """The acceptance After= edge queues it behind an active accumulator."""

    assert _simulate_start_order("accumulator") == ("queued", "active")
    condition = _unit_settings(
        "tradingagent-crypto-round-trip-g5-delayed-paper.service"
    )["ExecCondition"]
    assert _accumulator_condition_allows("active", condition) is False
