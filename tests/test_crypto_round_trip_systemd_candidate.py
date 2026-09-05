from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _minute_second_points(timer: str) -> list[int]:
    calendar = next(
        line.split("=", 1)[1]
        for line in timer.splitlines()
        if line.startswith("OnCalendar=")
    )
    clock = calendar.split()[1]
    _, minute_spec, second = clock.split(":")
    start, step = (int(value) for value in minute_spec.split("/"))
    return [minute * 60 + int(second) for minute in range(start, 60, step)]


def test_round_trip_service_is_sim_only_and_keeps_g2_read_only() -> None:
    service = (
        ROOT / "Crypto/systemd/tradingagent-crypto-round-trip-delayed-paper.service"
    ).read_text()
    assert "REAL_TRADING_ENABLED=false" in service
    assert "Crypto.delayed_paper_round_trip_runtime" in service
    assert "crypto-delayed-paper-round-trip.env" in service
    assert "${ROUND_TRIP_EPOCH_MANIFEST}" in service
    assert "crypto-delayed-paper-round-trip-epochs" in service
    assert (
        "--epoch-manifest /etc/tradingagent/crypto-delayed-paper-round-trip.epoch.json"
        not in service
    )
    assert (
        "ReadOnlyPaths=/var/lib/tradingagent/crypto-delayed-paper-epochs/crypto-delayed-paper-epoch-g2-20260729"
        in service
    )
    assert "ReadWritePaths=/var/lib/tradingagent/crypto-delayed-paper-epochs" in service
    assert "binance" not in service.lower()


def test_round_trip_timer_is_installable_but_repository_default_is_not_enabled() -> (
    None
):
    timer = (
        ROOT / "Crypto/systemd/tradingagent-crypto-round-trip-delayed-paper.timer"
    ).read_text()
    assert "OnCalendar=*-*-* *:0/5:55" in timer
    assert "WantedBy=timers.target" in timer
    assert "enable" not in timer.lower()


def test_g4_service_uses_its_own_manifest_selection_file() -> None:
    service = (
        ROOT / "Crypto/systemd/tradingagent-crypto-round-trip-g4-delayed-paper.service"
    ).read_text()
    timer = (
        ROOT / "Crypto/systemd/tradingagent-crypto-round-trip-g4-delayed-paper.timer"
    ).read_text()
    assert "REAL_TRADING_ENABLED=false" in service
    assert "Crypto.delayed_paper_round_trip_runtime" in service
    assert "crypto-delayed-paper-round-trip-g4.env" in service
    assert "crypto-delayed-paper-round-trip.env" not in service
    assert "${ROUND_TRIP_EPOCH_MANIFEST}" in service
    assert (
        "ReadOnlyPaths=/var/lib/tradingagent/crypto-delayed-paper-epochs/crypto-delayed-paper-epoch-g2-20260729"
        in service
    )
    assert "binance" not in service.lower()
    assert "Unit=tradingagent-crypto-round-trip-g4-delayed-paper.service" in timer
    assert "WantedBy=timers.target" in timer


def test_g4_health_service_is_read_only_and_timer_is_installable() -> None:
    service = (
        ROOT / "Crypto/systemd/tradingagent-crypto-round-trip-g4-health.service"
    ).read_text()
    timer = (
        ROOT / "Crypto/systemd/tradingagent-crypto-round-trip-g4-health.timer"
    ).read_text()
    assert "REAL_TRADING_ENABLED=false" in service
    assert "Crypto.delayed_paper_round_trip_health" in service
    assert "${ROUND_TRIP_EPOCH_MANIFEST}" in service
    assert "ReadWritePaths=" not in service
    assert "IPAddressDeny=any" in service
    assert "RestrictAddressFamilies=AF_UNIX" in service
    assert "tradingdatas-crypto-read.token" not in service
    assert "binance" not in service.lower()
    assert "OnCalendar=*-*-* *:2/15:30" in timer
    assert "WantedBy=timers.target" in timer
    assert "enable" not in timer.lower()


def test_g5_services_bind_only_the_g5_manifest_and_keep_g4_read_only() -> None:
    service = (
        ROOT / "Crypto/systemd/tradingagent-crypto-round-trip-g5-delayed-paper.service"
    ).read_text()
    timer = (
        ROOT / "Crypto/systemd/tradingagent-crypto-round-trip-g5-delayed-paper.timer"
    ).read_text()
    health = (
        ROOT / "Crypto/systemd/tradingagent-crypto-round-trip-g5-health.service"
    ).read_text()

    assert "REAL_TRADING_ENABLED=false" in service
    assert "crypto-delayed-paper-round-trip-g5.env" in service
    assert "crypto-delayed-paper-round-trip-g4.env" not in service
    assert "--epoch-manifest ${ROUND_TRIP_EPOCH_MANIFEST}" in service
    assert "--runtime-manifest ${ROUND_TRIP_RUNTIME_MANIFEST}" in service
    assert "crypto-delayed-paper-round-trip-epoch-g5-20260801" in service
    assert "crypto-delayed-paper-round-trip-epoch-g4-20260731" in service
    assert (
        "ReadWritePaths=/var/lib/tradingagent/crypto-delayed-paper-epochs/crypto-delayed-paper-round-trip-epoch-g5-20260801"
        in service
    )
    assert (
        "AssertPathIsDirectory=/var/lib/tradingagent/crypto-delayed-paper-epochs/crypto-delayed-paper-round-trip-epoch-g5-20260801"
        in service
    )
    assert "binance" not in service.lower()
    assert "Unit=tradingagent-crypto-round-trip-g5-delayed-paper.service" in timer
    assert "WantedBy=timers.target" in timer

    assert "REAL_TRADING_ENABLED=false" in health
    assert "crypto-delayed-paper-round-trip-g5.env" in health
    assert "ReadWritePaths=" not in health
    assert "IPAddressDeny=any" in health
    assert "RestrictAddressFamilies=AF_UNIX" in health


def test_g5_health_timer_runs_between_observed_core_cadences() -> None:
    core_timer = (
        ROOT / "Crypto/systemd/tradingagent-crypto-round-trip-g5-delayed-paper.timer"
    ).read_text()
    health_timer = (
        ROOT / "Crypto/systemd/tradingagent-crypto-round-trip-g5-health.timer"
    ).read_text()

    core_points = _minute_second_points(core_timer)
    health_points = _minute_second_points(health_timer)
    # G5 waits an additional two minutes after its already-settled source bar
    # instead of repeatedly turning ordinary source publication lag into a
    # runtime backlog recovery.
    assert "OnCalendar=*-*-* *:2/5:55" in core_timer
    assert "OnCalendar=*-*-* *:4/15:30" in health_timer
    assert health_points == [
        4 * 60 + 30,
        19 * 60 + 30,
        34 * 60 + 30,
        49 * 60 + 30,
    ]

    for point in health_points:
        previous_core = max(
            candidate for candidate in core_points if candidate < point
        )
        next_core = min(candidate for candidate in core_points if candidate > point)
        assert previous_core < point < next_core
