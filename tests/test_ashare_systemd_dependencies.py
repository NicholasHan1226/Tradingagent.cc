from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ASHARE_DATA_UNITS = (
    ROOT / "deploy/systemd/tradingagent-ashare-minute-session.service",
    ROOT / "deploy/systemd/tradingagent-ashare-minute-paper.service",
    ROOT / "Ashare/systemd/tradingagent-ashare-minute-scale500-paper.service",
    ROOT / "Ashare/systemd/tradingagent-ashare-minute-scale500-session.service",
    ROOT / "Ashare/systemd/tradingagent-ashare-minute-scale500-late-start.service",
)

BOOTSTRAP = ROOT / "deploy/systemd/tradingagent-ashare-minute-bootstrap.service"
MINUTE_SESSION = ROOT / "deploy/systemd/tradingagent-ashare-minute-session.service"
TRACKING_UNIVERSE_PATH = (
    "/var/lib/tradingagent/trading-copilot/tracking-universe.json"
)


def test_ashare_data_units_order_after_the_formal_tradingdatas_api() -> None:
    for path in ASHARE_DATA_UNITS:
        text = path.read_text(encoding="utf-8")
        assert "After=network-online.target tradingdatas-v1-internal.service" in text
        assert "tradingdatas-api.service" not in text


def test_first_session_bootstrap_is_manual_simulation_only_and_loopback_bound() -> None:
    text = BOOTSTRAP.read_text(encoding="utf-8")
    assert "Type=oneshot" in text
    assert "User=tradingagent" in text
    assert "Group=tradingagent" in text
    assert "After=network-online.target tradingdatas-v1-internal.service" in text
    assert "REAL_TRADING_ENABLED=false" in text
    assert "-m Ashare.minute_session_initializer" in text
    assert "--bootstrap-manifest /etc/tradingagent/ashare-minute-bootstrap-manifest.json" in text
    assert "--universe-source /etc/tradingagent/ashare-minute-bootstrap-universe.json" in text
    assert "--token-file /run/secrets/tradingagent/tradingdatas-read.token" in text
    assert f"--tracking-universe-output {TRACKING_UNIVERSE_PATH}" in text
    assert "IPAddressDeny=any" in text
    assert "IPAddressAllow=localhost" in text
    assert "ReadWritePaths=/var/lib/tradingagent/ashare-minute-paper" in text
    assert "ReadWritePaths=/var/lib/tradingagent/trading-copilot" in text
    assert "[Install]" not in text


def test_minute_session_projects_the_verified_named_universe_for_copilot() -> None:
    text = MINUTE_SESSION.read_text(encoding="utf-8")

    assert f"--tracking-universe-output {TRACKING_UNIVERSE_PATH}" in text
    assert "ReadWritePaths=/var/lib/tradingagent/trading-copilot" in text


def test_scale500_units_use_rolling_membership_but_late_start_stays_separate() -> None:
    session = (
        ROOT / "Ashare/systemd/tradingagent-ashare-minute-scale500-session.service"
    ).read_text(encoding="utf-8")
    paper = (
        ROOT / "Ashare/systemd/tradingagent-ashare-minute-scale500-paper.service"
    ).read_text(encoding="utf-8")
    late_start = (
        ROOT
        / "Ashare/systemd/tradingagent-ashare-minute-scale500-late-start.service"
    ).read_text(encoding="utf-8")

    assert "--rolling-eligible" in session
    assert "--rolling-eligible" in paper
    assert "--rolling-eligible" not in late_start


def test_baseline_recovers_late_start_and_scale_timeout_fits_timer_cadence() -> None:
    baseline = (ROOT / "deploy/systemd/tradingagent-ashare-minute-paper.service").read_text()
    scale = (ROOT / "Ashare/systemd/tradingagent-ashare-minute-scale500-paper.service").read_text()
    assert "--allow-late-start" in baseline
    # 180s query budget plus processing headroom, below the 300s cadence.
    assert "TimeoutStartSec=240s" in scale
