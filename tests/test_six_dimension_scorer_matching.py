from __future__ import annotations

from unittest import mock

from shared.screening import six_dimension_scorer as scorer
from shared.orchestrator import _score_diagnostics


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


class SharedSignalsEventsReader:
    """Simulates SharedSignals /events API returning announcement/news events
    WITHOUT explicit confidence or direction fields — only title/content text.
    This is the real-world scenario for Tushare disclosure/news data."""

    def get_events(self, market=None, symbol=None, start=None, end=None):
        return [
            {
                "event_time": "2026-07-08",
                "event_type": "announcement",
                "symbol": "600519",
                "market": "Ashare",
                "title": "关于控股股东增持公司股份的公告",
                "content": "控股股东计划在未来6个月内增持...",
            }
        ]

    def get_event_candidates(self):
        return []

    def get_sentiment(self):
        return []


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


def test_score_diagnostics_reports_batch_inactive_dimensions():
    scores = {
        "600000.SH": {
            "combined": 0.6875,
            "macro": 0.5,
            "event": 0.5,
            "fundamental": 0.5,
            "capital": 1.0,
            "technical": 1.0,
            "sentiment": 0.5,
            "batch_inactive_dimensions": ["event"],
            "batch_evidence_availability": {"event": 0.0, "capital": 1.0},
        },
        "000001.SZ": {
            "combined": 0.6875,
            "macro": 0.5,
            "event": 0.5,
            "fundamental": 0.5,
            "capital": 1.0,
            "technical": 1.0,
            "sentiment": 0.5,
            "batch_inactive_dimensions": ["event"],
            "batch_evidence_availability": {"event": 0.0, "capital": 1.0},
        },
    }

    diagnostics = _score_diagnostics(scores)

    assert diagnostics["batch_inactive_dimensions"] == ["event"]
    assert diagnostics["batch_evidence_availability"] == {"event": 0.0, "capital": 1.0}


def test_text_inferred_event_not_discarded_by_min_confidence():
    """Text-inferred events (no explicit confidence field) must pass the
    min_confidence gate — regression test for the 0.25 < 0.30 silent discard."""
    config = {
        "_data_reader": SharedSignalsEventsReader(),
        "dimensions": {"event": {"min_confidence": 0.30}},
    }

    score = scorer._score_event("600519.SH", "20260710", config)

    assert score is not None
    # Text-inferred confidence should be >= min_conf, so the event is used
    assert score > 0.5, f"Expected score > 0.5 for positive text-inferred event, got {score}"
    evidence = config.get("_dimension_evidence", {}).get("event", {})
    assert evidence.get("has_evidence") is True, (
        f"Expected SharedSignals events evidence, got {evidence}"
    )
    assert evidence.get("source") == "SharedSignals events"
    assert evidence.get("row_count", 0) >= 1


def test_text_inferred_event_with_custom_min_confidence():
    """Raising the gate must not raise text-inferred confidence with it."""
    config = {
        "_data_reader": SharedSignalsEventsReader(),
        "dimensions": {"event": {"min_confidence": 0.40}},
    }

    score = scorer._score_event("600519.SH", "20260710", config)

    assert score == 0.5
    evidence = config.get("_dimension_evidence", {}).get("event", {})
    assert evidence.get("has_evidence") is False
    assert "skipped_low_conf=1" in evidence.get("reason", "")


def test_event_lookback_window_captures_recent_events():
    """Short-cycle event lookback (default 3 days) must capture events
    within the window, not just today's events."""
    config = {
        "_data_reader": SharedSignalsEventsReader(),
        "dimensions": {"event": {"min_confidence": 0.30}},
    }

    # Event dated 2026-07-08, scoring on 2026-07-10 → within 3-day window
    score = scorer._score_event("600519.SH", "20260710", config)

    assert score is not None
    assert score > 0.5


def test_event_outside_lookback_window_returns_neutral():
    """A stale row is ignored even if an upstream reader fails to filter it."""

    class StaleEventsReader:
        def get_events(self, market=None, symbol=None, start=None, end=None):
            return [
                {
                    "event_time": "2026-07-01",
                    "event_type": "announcement",
                    "symbol": "600519",
                    "market": "Ashare",
                    "title": "关于控股股东增持公司股份的公告",
                }
            ]

        def get_event_candidates(self):
            return []

    config = {
        "_data_reader": StaleEventsReader(),
        "dimensions": {"event": {"min_confidence": 0.30}},
    }
    assert scorer._score_event("600519.SH", "20260710", config) == 0.5
    assert config["_dimension_evidence"]["event"]["has_evidence"] is False

    # The scorer must also request the bounded window from SharedSignals.
    captured_args = []

    class CapturingReader:
        def get_events(self, *args, **kwargs):
            captured_args.append((args, kwargs))
            return []

        def get_event_candidates(self):
            return []

    config2 = {
        "_data_reader": CapturingReader(),
        "dimensions": {"event": {"min_confidence": 0.30}},
    }
    scorer._score_event("600519.SH", "20260710", config2)
    assert len(captured_args) > 0
    # First positional arg after market and symbol should be start_date
    # get_events(market, symbol, start_date, date)
    call_args = captured_args[0][0]
    assert len(call_args) >= 3
    start_date = str(call_args[2])
    assert start_date == "20260708"


def test_event_no_evidence_produces_diagnostic_reason():
    """When no events match, the evidence marker must include diagnostic
    detail about why (raw row count, skipped reasons)."""
    config = {
        "_data_reader": FakeEvidenceReader(),  # get_events returns []
        "dimensions": {"event": {"min_confidence": 0.30}},
    }

    score = scorer._score_event("600519.SH", "20260710", config)

    assert score == 0.5
    evidence = config.get("_dimension_evidence", {}).get("event", {})
    assert evidence.get("has_evidence") is False
    reason = evidence.get("reason", "")
    assert "no_matched_event_evidence" in reason


class NeutralAnnouncementReader:
    """Simulates SharedSignals events with neutral announcements — no explicit
    impact/direction/sentiment fields and text that contains neither positive
    nor negative tokens, so _text_direction_hint returns ''."""

    def get_events(self, market=None, symbol=None, start=None, end=None):
        return [
            {
                "event_time": "2026-07-08",
                "event_type": "announcement",
                "symbol": "000776",
                "market": "Ashare",
                "title": "关于召开股东大会的通知",
                "content": "公司定于2026年7月20日召开股东大会...",
            }
        ]

    def get_event_candidates(self):
        return []


def test_neutral_announcement_without_direction_is_skipped():
    """An announcement with neither explicit impact fields nor text-inferable
    direction must be counted as skipped_no_impact, must NOT contribute to
    candidate weight, and must result in event=0.5 with has_evidence=False."""
    config = {
        "_data_reader": NeutralAnnouncementReader(),
        "dimensions": {"event": {"min_confidence": 0.30}},
    }

    score = scorer._score_event("000776.SZ", "20260710", config)

    assert score == 0.5
    evidence = config.get("_dimension_evidence", {}).get("event", {})
    assert evidence.get("has_evidence") is False, (
        f"Expected no evidence for neutral announcement, got {evidence}"
    )
    reason = evidence.get("reason", "")
    assert "ss_rows=1" in reason, f"Expected ss_rows in reason, got: {reason}"
    assert "skipped_no_impact=1" in reason, f"Expected skipped_no_impact in reason, got: {reason}"


def test_empty_direction_field_falls_through_to_text_inference():
    """When a row has an explicit but empty direction/sentiment field,
    text inference is still attempted, and if text is neutral the row is
    skipped as no_impact."""

    class EmptyDirectionReader:
        def get_events(self, market=None, symbol=None, start=None, end=None):
            return [
                {
                    "event_time": "2026-07-08",
                    "event_type": "announcement",
                    "symbol": "600030",
                    "market": "Ashare",
                    "direction": "",  # explicit empty field
                    "sentiment": "",  # explicit empty field
                    "title": "关于变更会计师事务所的公告",
                    "content": "公司拟变更2026年度审计机构...",
                }
            ]

        def get_event_candidates(self):
            return []

    config = {
        "_data_reader": EmptyDirectionReader(),
        "dimensions": {"event": {"min_confidence": 0.30}},
    }

    score = scorer._score_event("600030.SH", "20260710", config)

    assert score == 0.5
    evidence = config.get("_dimension_evidence", {}).get("event", {})
    assert evidence.get("has_evidence") is False
    reason = evidence.get("reason", "")
    assert "skipped_no_impact=1" in reason


def test_text_inferred_negative_event_still_valid_evidence():
    """Explicit negative text must still be treated as valid evidence with
    fixed 0.30 confidence — regression check that the 'not impact' guard
    does not discard genuinely inferred directions."""

    class NegativeTextReader:
        def get_events(self, market=None, symbol=None, start=None, end=None):
            return [
                {
                    "event_time": "2026-07-08",
                    "event_type": "announcement",
                    "symbol": "600030",
                    "market": "Ashare",
                    "title": "关于股东减持公司股份的公告",
                    "content": "股东计划减持不超过2%的股份...",
                }
            ]

        def get_event_candidates(self):
            return []

    config = {
        "_data_reader": NegativeTextReader(),
        "dimensions": {"event": {"min_confidence": 0.30}},
    }

    score = scorer._score_event("600030.SH", "20260710", config)

    assert score is not None
    assert score < 0.5, f"Expected bearish score < 0.5 for negative text, got {score}"
    evidence = config.get("_dimension_evidence", {}).get("event", {})
    assert evidence.get("has_evidence") is True, (
        f"Expected valid evidence for negative text event, got {evidence}"
    )
    assert evidence.get("source") == "SharedSignals events"
    assert evidence.get("row_count", 0) >= 1
