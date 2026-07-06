from __future__ import annotations

import json

from PM.research_probability import generate_pm_model_probabilities


class FakePMReader:
    degraded = False
    errors: list[str] = []

    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def get_pm_markets(self, limit: int = 100, active_only: bool = True) -> list[dict[str, object]]:
        return self.rows[:limit]


def _jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_pm_research_probability_writes_explicit_independent_forecast(tmp_path):
    output = tmp_path / "model_probabilities.jsonl"
    reader = FakePMReader([
        {
            "market_id": "pm-1",
            "slug": "will-fed-cut-rates",
            "price": 0.42,
            "research_probability": 0.58,
            "research_source": "marketgraph_event_research",
        }
    ])

    result = generate_pm_model_probabilities(reader=reader, output_path=output, generated_at="2026-07-07T00:00:00+00:00")

    rows = _jsonl(output)
    assert result["state"] == "ok"
    assert result["record_count"] == 1
    assert rows[0]["market_id"] == "pm-1"
    assert rows[0]["model_probability"] == 0.58
    assert rows[0]["market_probability"] == 0.42
    assert rows[0]["model_source"] == "marketgraph_event_research"


def test_pm_research_probability_clears_stale_file_when_only_market_prices_exist(tmp_path):
    output = tmp_path / "model_probabilities.jsonl"
    output.write_text('{"market_id":"stale","model_probability":0.99}\n', encoding="utf-8")
    reader = FakePMReader([{"market_id": "pm-1", "price": 0.42}])

    result = generate_pm_model_probabilities(reader=reader, output_path=output, generated_at="2026-07-07T00:00:00+00:00")

    assert result["record_count"] == 0
    assert result["skipped_count"] == 1
    assert output.read_text(encoding="utf-8") == ""


def test_pm_research_probability_can_use_bounded_sentiment_evidence(tmp_path):
    output = tmp_path / "model_probabilities.jsonl"
    reader = FakePMReader([
        {
            "market_id": "pm-1",
            "question": "Will candidate A win?",
            "description": "Resolves from official election results.",
            "resolution_source": "official result",
            "price": 0.50,
            "sentiment_score": 0.95,
            "liquidity": 25000,
            "end_date": "2026-07-20",
        }
    ])

    result = generate_pm_model_probabilities(reader=reader, output_path=output, generated_at="2026-07-07T00:00:00+00:00")

    rows = _jsonl(output)
    assert result["record_count"] == 1
    assert rows[0]["model_source"] == "pm_research_sentiment_v1"
    assert rows[0]["model_probability"] > 0.50
    assert rows[0]["model_probability"] <= 0.56
    assert rows[0]["model_confidence"] <= 0.50
