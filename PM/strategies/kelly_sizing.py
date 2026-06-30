"""Fractional Kelly sizing configuration for PM shadow trades."""

CONFIG = {
    "name": "kelly_sizing",
    "capital_layer": "shadow",
    "enabled": True,
    "fraction": 0.25,
    "max_market_weight": 0.05,
    "min_probability_edge": 0.05,
}

