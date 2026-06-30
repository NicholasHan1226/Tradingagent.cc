"""Prediction Markets strategy configuration registry."""

from __future__ import annotations

from PM.strategies.calibration_arbitrage import CONFIG as CALIBRATION_ARBITRAGE
from PM.strategies.early_exit import CONFIG as EARLY_EXIT
from PM.strategies.event_driven import CONFIG as EVENT_DRIVEN
from PM.strategies.kelly_sizing import CONFIG as KELLY_SIZING
from PM.strategies.nlp_sentiment import CONFIG as NLP_SENTIMENT
from PM.strategies.probability_arbitrage import CONFIG as PROBABILITY_ARBITRAGE


STRATEGY_CONFIGS = {
    "probability_arbitrage": PROBABILITY_ARBITRAGE,
    "event_driven": EVENT_DRIVEN,
    "kelly_sizing": KELLY_SIZING,
    "nlp_sentiment": NLP_SENTIMENT,
    "early_exit": EARLY_EXIT,
    "calibration_arbitrage": CALIBRATION_ARBITRAGE,
}

