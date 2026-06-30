"""Calibration-bias strategy configuration."""

CONFIG = {
    "name": "calibration_arbitrage",
    "capital_layer": "shadow",
    "enabled": True,
    "min_brier_advantage": 0.03,
    "lookback_markets": 100,
    "max_market_weight": 0.03,
}

