from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


MODEL_PROBABILITY_KEYS = (
    "model_probability",
)
MARKET_PROBABILITY_KEYS = (
    "yes_price",
    "market_price",
    "last_price",
    "price",
    "probability",
    "implied_probability",
)


def default_probability_file() -> Path:
    configured = os.environ.get("TRADINGAGENT_PM_MODEL_PROBABILITY_FILE") or os.environ.get("PM_MODEL_PROBABILITY_FILE")
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).resolve().parents[1] / "shared" / "review" / "pm" / "model_probabilities.jsonl"


def _safe_probability(value: Any) -> float:
    try:
        prob = float(value)
    except (TypeError, ValueError):
        return 0.0
    return prob if 0 < prob < 1 else 0.0


def _probability(row: dict[str, Any], keys: tuple[str, ...]) -> float:
    for key in keys:
        prob = _safe_probability(row.get(key))
        if prob:
            return prob
    return 0.0


def _market_keys(row: dict[str, Any]) -> tuple[str, ...]:
    keys: list[str] = []
    for key in ("market_id", "condition_id", "symbol", "slug", "question"):
        value = str(row.get(key) or "").strip()
        if value:
            keys.append(value)
            keys.append(value.lower())
    return tuple(dict.fromkeys(keys))


def _read_probability_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        records: list[dict[str, Any]] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                records.append(item)
        return records
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("probabilities", "records", "data", "items"):
            items = payload.get(key)
            if isinstance(items, list):
                return [item for item in items if isinstance(item, dict)]
        return [payload]
    return []


def load_model_probabilities(path: Path | None = None) -> dict[str, dict[str, Any]]:
    source = path or default_probability_file()
    forecasts: dict[str, dict[str, Any]] = {}
    for record in _read_probability_records(source):
        probability = _probability(record, MODEL_PROBABILITY_KEYS + ("probability",))
        if not probability:
            continue
        normalized = {
            **record,
            "model_probability": probability,
            "model_source": str(record.get("model_source") or record.get("source") or "marketgraph_pm_research"),
        }
        for key in _market_keys(record):
            forecasts[key] = normalized
    return forecasts


def enrich_pm_rows(rows: list[dict[str, Any]], probability_file: Path | None = None) -> list[dict[str, Any]]:
    forecasts = load_model_probabilities(probability_file)
    enriched_rows: list[dict[str, Any]] = []
    for row in rows:
        enriched = dict(row)
        forecast = next((forecasts[key] for key in _market_keys(enriched) if key in forecasts), None)
        if forecast:
            enriched["model_probability"] = forecast["model_probability"]
            enriched["model_source"] = forecast.get("model_source", "marketgraph_pm_research")
            if forecast.get("model_confidence") is not None:
                enriched["model_confidence"] = forecast.get("model_confidence")
            if forecast.get("model_reason") is not None:
                enriched["model_reason"] = forecast.get("model_reason")
            enriched_rows.append(enriched)
            continue

        market_probability = _probability(enriched, MARKET_PROBABILITY_KEYS)
        if market_probability:
            enriched["model_probability"] = market_probability
            enriched["model_source"] = "pm_market_consensus_baseline"
            enriched["model_confidence"] = 0.0
            enriched["model_reason"] = "baseline_equals_market_probability_no_independent_edge"
        enriched_rows.append(enriched)
    return enriched_rows


__all__ = [
    "MARKET_PROBABILITY_KEYS",
    "MODEL_PROBABILITY_KEYS",
    "default_probability_file",
    "enrich_pm_rows",
    "load_model_probabilities",
]
