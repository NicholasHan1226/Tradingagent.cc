"""NLP sentiment adjustment strategy configuration."""

CONFIG = {
    "name": "nlp_sentiment",
    "capital_layer": "shadow",
    "enabled": True,
    "min_sentiment_score": 0.60,
    "max_adjustment": 0.08,
    "stale_after_minutes": 120,
}

