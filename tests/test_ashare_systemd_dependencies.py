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
    assert "IPAddressDeny=any" in text
    assert "IPAddressAllow=localhost" in text
    assert "ReadWritePaths=/var/lib/tradingagent/ashare-minute-paper" in text
    assert "[Install]" not in text
