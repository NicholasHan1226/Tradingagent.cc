from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_round_trip_service_is_sim_only_and_keeps_g2_read_only() -> None:
    service = (
        ROOT / "Crypto/systemd/tradingagent-crypto-round-trip-delayed-paper.service"
    ).read_text()
    assert "REAL_TRADING_ENABLED=false" in service
    assert "Crypto.delayed_paper_round_trip_runtime" in service
    assert "crypto-delayed-paper-round-trip.epoch.json" in service
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
