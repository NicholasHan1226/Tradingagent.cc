from __future__ import annotations

import json

from PM.probability_model import enrich_pm_rows, load_model_probabilities


def test_pm_baseline_model_probability_matches_market_price_when_no_research_file(tmp_path):
    missing = tmp_path / "missing.jsonl"
    rows = [{"market_id": "pm-1", "yes_price": 0.47}]

    enriched = enrich_pm_rows(rows, probability_file=missing)

    assert enriched[0]["model_probability"] == 0.47
    assert enriched[0]["model_source"] == "pm_market_consensus_baseline"
    assert enriched[0]["model_confidence"] == 0.0


def test_pm_research_probability_file_overrides_market_baseline(tmp_path):
    source = tmp_path / "model_probabilities.jsonl"
    source.write_text(
        json.dumps({
            "market_id": "pm-1",
            "model_probability": 0.63,
            "model_source": "marketgraph_research",
            "model_reason": "research_edge",
        })
        + "\n",
        encoding="utf-8",
    )

    enriched = enrich_pm_rows([{"market_id": "pm-1", "yes_price": 0.47}], probability_file=source)

    assert enriched[0]["model_probability"] == 0.63
    assert enriched[0]["model_source"] == "marketgraph_research"
    assert enriched[0]["model_reason"] == "research_edge"


def test_pm_invalid_research_probability_is_ignored(tmp_path):
    source = tmp_path / "model_probabilities.jsonl"
    source.write_text(json.dumps({"market_id": "pm-1", "model_probability": 1.2}) + "\n", encoding="utf-8")

    forecasts = load_model_probabilities(source)

    assert forecasts == {}
