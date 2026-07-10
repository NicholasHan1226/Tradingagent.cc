from __future__ import annotations

from unittest import mock

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


def test_score_universe_removes_globally_missing_event_weight_from_all_symbols():
    def score(symbol, *args, **kwargs):
        return {
            "macro": 0.5,
            "event": 0.5,
            "fundamental": 0.5,
            "capital": 1.0,
            "technical": 1.0,
            "sentiment": 0.5,
            "combined": 0.65,
            "evidence_sources": {
                "macro": {"has_evidence": True},
                "event": {"has_evidence": False, "reason": "no_matched_event_evidence"},
                "fundamental": {"has_evidence": True},
                "capital": {"has_evidence": True},
                "technical": {"has_evidence": True},
                "sentiment": {"has_evidence": True},
            },
            "missing_evidence_dimensions": ["event"],
        }

    with mock.patch.object(scorer, "score_stock", side_effect=score):
        rows = scorer.score_universe("20260710", ["600000.SH", "000001.SZ"], market="ashare")

    assert all("event" in values["batch_inactive_dimensions"] for _, values in rows)
    assert all(values["combined"] == 0.6875 for _, values in rows)
