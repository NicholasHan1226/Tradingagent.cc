from __future__ import annotations

import json

from PM.research_probability import generate_pm_model_probabilities


class FakePMReader:
    degraded = False
    errors: list[str] = []

    def __init__(self, rows: list[dict[str, object]], prices: list[dict[str, object]] | None = None) -> None:
        self.rows = rows
        self.prices = prices or []

    def get_pm_markets(self, limit: int = 100, active_only: bool = True) -> list[dict[str, object]]:
        return self.rows[:limit]

    def get_pm_prices(self, limit: int = 100) -> list[dict[str, object]]:
        return self.prices[:limit]


class FakeMarketGraphClient:
    degraded = False
    errors: list[str] = []

    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def get_pm_research_probabilities(self, limit: int = 100) -> list[dict[str, object]]:
        return self.rows[:limit]


class NestedMarketGraphClient:
    degraded = False
    errors: list[str] = []

    def get_pm_research_probabilities(self, limit: int = 100) -> dict[str, object]:
        return {
            "data": {
                "rows": [
                    {
                        "market_id": "pm-1",
                        "research_probability": 0.61,
                        "confidence": 0.66,
                    }
                ]
            }
        }


class EmptyEnvelopeMarketGraphClient:
    degraded = False
    errors: list[str] = []

    def get_pm_research_probabilities(self, limit: int = 100) -> dict[str, object]:
        return {
            "source": "marketgraph_pm_research_read_model",
            "row_count": 0,
            "rows": [],
            "degraded": True,
            "degrade_reason": "pm_research_probability_read_model_empty",
            "readiness": {
                "readiness_status": "needs_review_samples",
                "primary_blocker": "存在 3 个复盘采样任务，还需 7 条样本",
            },
        }


def _jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_pm_research_probability_reads_marketgraph_api_forecast(tmp_path):
    output = tmp_path / "model_probabilities.jsonl"
    reader = FakePMReader([
        {
            "market_id": "pm-1",
            "slug": "will-fed-cut-rates",
            "price": 0.42,
        }
    ])
    marketgraph = FakeMarketGraphClient([
        {
            "market_id": "pm-1",
            "research_probability": 0.58,
            "probability_source": "marketgraph_event_research",
            "confidence": 0.73,
        }
    ])

    result = generate_pm_model_probabilities(
        reader=reader,
        marketgraph_client=marketgraph,
        output_path=output,
        generated_at="2026-07-07T00:00:00+00:00",
    )

    rows = _jsonl(output)
    assert result["state"] == "ok"
    assert result["record_count"] == 1
    assert rows[0]["market_id"] == "pm-1"
    assert rows[0]["model_probability"] == 0.58
    assert rows[0]["market_probability"] == 0.42
    assert rows[0]["model_source"] == "marketgraph_event_research"
    assert rows[0]["model_confidence"] == 0.73


def test_pm_research_probability_clears_stale_file_when_only_market_prices_exist(tmp_path):
    output = tmp_path / "model_probabilities.jsonl"
    output.write_text('{"market_id":"stale","model_probability":0.99}\n', encoding="utf-8")
    reader = FakePMReader([{"market_id": "pm-1", "price": 0.42}])
    marketgraph = FakeMarketGraphClient([])

    result = generate_pm_model_probabilities(
        reader=reader,
        marketgraph_client=marketgraph,
        output_path=output,
        generated_at="2026-07-07T00:00:00+00:00",
    )

    assert result["record_count"] == 0
    assert result["marketgraph_rows"] == 0
    assert result["skip_reasons"]["marketgraph_research_empty"] == 1
    assert result["next_action"] == "repair_marketgraph_pm_research_probability_samples"
    assert output.read_text(encoding="utf-8") == ""


def test_pm_research_probability_surfaces_marketgraph_empty_reason(tmp_path):
    output = tmp_path / "model_probabilities.jsonl"
    reader = FakePMReader([{"market_id": "pm-1", "price": 0.42}])

    result = generate_pm_model_probabilities(
        reader=reader,
        marketgraph_client=EmptyEnvelopeMarketGraphClient(),
        output_path=output,
        generated_at="2026-07-07T00:00:00+00:00",
    )

    assert result["record_count"] == 0
    assert result["marketgraph_research_meta"]["degrade_reason"] == "pm_research_probability_read_model_empty"
    assert "存在 3 个复盘采样任务" in result["next_action"]
    assert output.read_text(encoding="utf-8") == ""


def test_pm_research_probability_ignores_sharedsignals_inline_research_fields(tmp_path):
    output = tmp_path / "model_probabilities.jsonl"
    reader = FakePMReader([
        {
            "market_id": "pm-1",
            "price": 0.50,
            "research_probability": 0.99,
            "marketgraph_probability": 0.99,
        }
    ])
    marketgraph = FakeMarketGraphClient([])

    result = generate_pm_model_probabilities(
        reader=reader,
        marketgraph_client=marketgraph,
        output_path=output,
        generated_at="2026-07-07T00:00:00+00:00",
    )

    assert result["record_count"] == 0
    assert result["skip_reasons"]["marketgraph_research_empty"] == 1
    assert _jsonl(output) == []


def test_pm_research_probability_accepts_marketgraph_api_envelope(tmp_path):
    output = tmp_path / "model_probabilities.jsonl"
    reader = FakePMReader([{"market_id": "pm-1", "price": 0.44}])

    result = generate_pm_model_probabilities(
        reader=reader,
        marketgraph_client=NestedMarketGraphClient(),
        output_path=output,
        generated_at="2026-07-07T00:00:00+00:00",
    )

    rows = _jsonl(output)
    assert result["record_count"] == 1
    assert rows[0]["model_probability"] == 0.61
    assert rows[0]["market_probability"] == 0.44


def test_pm_research_probability_uses_sharedsignals_price_rows_when_market_rows_lack_price(tmp_path):
    output = tmp_path / "model_probabilities.jsonl"
    reader = FakePMReader(
        [{"market_id": "pm-1", "slug": "will-fed-cut-rates"}],
        prices=[{"market_id": "pm-1", "price": 0.43, "price_time": "2026-07-07T00:00:00Z"}],
    )
    marketgraph = FakeMarketGraphClient([
        {
            "market_id": "pm-1",
            "research_probability": 0.61,
            "confidence": 0.7,
        }
    ])

    result = generate_pm_model_probabilities(
        reader=reader,
        marketgraph_client=marketgraph,
        output_path=output,
        generated_at="2026-07-07T00:00:00+00:00",
    )

    rows = _jsonl(output)
    assert result["record_count"] == 1
    assert result["price_rows"] == 1
    assert rows[0]["market_probability"] == 0.43


def test_pm_research_probability_requires_sharedsignals_market_price(tmp_path):
    output = tmp_path / "model_probabilities.jsonl"
    reader = FakePMReader([])
    marketgraph = FakeMarketGraphClient([
        {
            "market_id": "pm-1",
            "research_probability": 0.61,
            "market_probability": 0.44,
            "price": 0.44,
        }
    ])

    result = generate_pm_model_probabilities(
        reader=reader,
        marketgraph_client=marketgraph,
        output_path=output,
        generated_at="2026-07-07T00:00:00+00:00",
    )

    assert result["record_count"] == 0
    assert result["skip_reasons"]["missing_market_probability"] == 1
    assert _jsonl(output) == []
