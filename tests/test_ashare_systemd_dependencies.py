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


def test_ashare_data_units_order_after_the_formal_tradingdatas_api() -> None:
    for path in ASHARE_DATA_UNITS:
        text = path.read_text(encoding="utf-8")
        assert "After=network-online.target tradingdatas-v1-internal.service" in text
        assert "tradingdatas-api.service" not in text
