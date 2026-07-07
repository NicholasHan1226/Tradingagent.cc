from __future__ import annotations

from shared.screening import six_dimension_scorer as scorer


class FakeEvidenceReader:
    def get_events(self, *args, **kwargs):
        return []

    def get_event_candidates(self):
        return [
            {
                "subject_code": "SH600276",
                "subject_type": "stock",
                "status": "verified",
                "confidence": "0.80",
                "proposed_impact_hint": "positive",
            }
        ]

    def get_sentiment(self):
        return [
            {
                "symbol": "600276",
                "status": "needs_review",
                "confidence": "0.60",
                "proposed_impact_hint": "positive",
            }
        ]


def test_marketgraph_event_candidates_match_exchange_prefixed_codes():
    config = {"_data_reader": FakeEvidenceReader(), "dimensions": {"event": {"min_confidence": 0.3}}}

    score = scorer._score_event("600276.SH", "20260708", config)

    assert score and score > 0.5
    assert config["_dimension_evidence"]["event"]["has_evidence"] is True
    assert config["_dimension_evidence"]["event"]["row_count"] == 1


def test_sentiment_matches_bare_symbol_field():
    config = {"_data_reader": FakeEvidenceReader(), "dimensions": {"sentiment": {"extreme_threshold": 0.95}}}

    score = scorer._score_sentiment("600276.SH", "20260708", config)

    assert score is not None
    assert config["_dimension_evidence"]["sentiment"]["has_evidence"] is True
    assert config["_dimension_evidence"]["sentiment"]["row_count"] == 1
