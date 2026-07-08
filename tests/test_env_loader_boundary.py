from __future__ import annotations

from pathlib import Path


def test_env_loader_does_not_export_market_data_provider_secrets() -> None:
    source = (Path(__file__).resolve().parents[1] / "shared" / "env_loader.sh").read_text(encoding="utf-8")
    forbidden = [
        "TRADINGS_" + "TUSHARE_TOKEN",
        "TRADINGS_" + "ALPACA_API_KEY",
        "TRADINGS_" + "ALPACA_SECRET_KEY",
        "TRADINGS_" + "POLYMARKET_API_KEY",
        "TRADINGS_" + "POLYMARKET_SECRET",
        "TRADINGS_" + "FINNHUB_API_KEY",
        "TRADINGS_" + "FMP_API_KEY",
    ]

    assert [token for token in forbidden if token in source] == []
