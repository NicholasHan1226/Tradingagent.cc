from __future__ import annotations

import pytest

from shared.data.sharedsignals_v1 import HTTPResponse
from shared.screening import six_dimension_scorer as scorer
from shared.screening.news_sentiment_reader import (
    FLASH_DATASET_ID,
    NEWS_DATASET_ID,
    NewsSentimentEvidence,
    NewsSentimentReader,
)

CATALOG_VERSION = "news-catalog-2026-08-16"


class FakeEnvelope:
    def __init__(self, data):
        self.data = data
        self.next_cursor = None


class FakeClient:
    def __init__(
        self,
        rows=None,
        *,
        news_rows=None,
        flash_rows=None,
        error=None,
        news_error=None,
        flash_error=None,
    ):
        self.news_rows = list(news_rows if news_rows is not None else (rows or []))
        self.flash_rows = list(flash_rows or [])
        # ``error`` applies to both sources by default; per-source errors
        # override it for the individual source.
        self.news_error = news_error if news_error is not None else error
        self.flash_error = flash_error if flash_error is not None else error
        self.requests = []

    def query(self, request):
        self.requests.append(request)
        dataset_id = request.dataset_id
        if dataset_id == NEWS_DATASET_ID:
            if self.news_error is not None:
                raise self.news_error
            return FakeEnvelope(list(self.news_rows))
        if dataset_id == FLASH_DATASET_ID:
            if self.flash_error is not None:
                raise self.flash_error
            return FakeEnvelope(list(self.flash_rows))
        raise AssertionError(f"unexpected dataset_id: {dataset_id}")


def _row(datetime_value, title="", content="", channels=None):
    return {
        "datetime": datetime_value,
        "title": title,
        "content": content,
        "channels": channels,
    }


def _flash_row(
    published_at="2026-08-16T10:00:00+08:00",
    title="",
    summary="",
    *,
    source="财联社",
    content_uid=None,
    published_local=None,
    event_date=None,
    url=None,
):
    return {
        "source": source,
        "content_uid": content_uid,
        "published_at": published_at,
        "published_local": published_local,
        "event_date": event_date,
        "title": title,
        "url": url,
        "summary": summary,
    }


def _reader(
    rows=None,
    *,
    flash_rows=None,
    error=None,
    news_error=None,
    flash_error=None,
    lookback_minutes=1440,
    schema_major=1,
):
    client = FakeClient(
        rows,
        flash_rows=flash_rows,
        error=error,
        news_error=news_error,
        flash_error=flash_error,
    )
    return (
        NewsSentimentReader(
            client,
            schema_major=schema_major,
            lookback_minutes=lookback_minutes,
        ),
        client,
    )


def test_code_match_counts_and_channel_breakdown():
    rows = [
        _row("2026-08-16 10:00:00", title="华测检测获订单", content="300012 中标项目", channels="要闻,行业"),
        _row("2026-08-16 11:00:00", title="大盘上涨", content="市场情绪回暖", channels="要闻"),
        _row("2026-08-16 12:00:00", title="公告", content="300012 发布业绩预告", channels="公司"),
    ]
    reader, client = _reader(rows, lookback_minutes=1440)

    evidence = reader.read_evidence("300012.SZ", "20260816")

    assert evidence.has_evidence is True
    assert evidence.status == "evidence"
    assert evidence.total_rows == 3
    assert evidence.code_matches == 2
    assert evidence.name_matches == 0
    assert evidence.stock_matches == 2
    assert evidence.unique_events == 3
    assert evidence.raw_rows == 3
    assert evidence.channel_counts == {"要闻": 2, "行业": 1, "公司": 1}
    assert "code_matches=2" in evidence.reason
    assert client.requests[0].to_payload()["dataset_id"] == NEWS_DATASET_ID


def test_name_match_when_name_provided():
    rows = [
        _row("2026-08-16 10:00:00", title="华测检测获订单", content="公司中标", channels=None),
        _row("2026-08-16 11:00:00", title="无关新闻", content="", channels=None),
    ]
    reader, _ = _reader(rows, lookback_minutes=1440)

    evidence = reader.read_evidence("300012.SZ", "20260816", name="华测检测")

    assert evidence.has_evidence is True
    assert evidence.name_matches == 1
    assert evidence.code_matches == 0
    assert evidence.stock_matches == 1
    assert "name_matches=1" in evidence.reason


def test_window_filters_older_rows_out():
    rows = [
        _row("2026-08-16 10:00:00", title="最近", content="", channels="要闻"),
        _row("2026-08-14 10:00:00", title="太旧", content="300012", channels="要闻"),
    ]
    reader, _ = _reader(rows, lookback_minutes=1440)

    evidence = reader.read_evidence("300012.SZ", "20260816")

    assert evidence.has_evidence is True
    assert evidence.total_rows == 1
    assert evidence.code_matches == 0


def test_empty_rows_returns_no_evidence():
    reader, _ = _reader([], lookback_minutes=1440)

    evidence = reader.read_evidence("300012.SZ", "20260816")

    assert evidence.has_evidence is False
    assert evidence.reason == "news_no_rows"


def test_window_empty_returns_no_evidence():
    rows = [_row("2026-08-01 10:00:00", title="旧", content="", channels="要闻")]
    reader, _ = _reader(rows, lookback_minutes=1440)

    evidence = reader.read_evidence("300012.SZ", "20260816")

    assert evidence.has_evidence is False
    assert evidence.reason == "news_window_empty"


def test_client_exception_degrades_to_no_evidence():
    reader, _ = _reader([], error=RuntimeError("down"))

    evidence = reader.read_evidence("300012.SZ", "20260816")

    assert evidence.has_evidence is False
    assert evidence.reason == "news_client_error"


def test_invalid_date_returns_no_evidence():
    reader, _ = _reader([], lookback_minutes=1440)

    evidence = reader.read_evidence("300012.SZ", "not-a-date")

    assert evidence.has_evidence is False
    assert evidence.reason == "news_invalid_date"


def test_unparseable_datetime_row_is_skipped():
    rows = [
        _row("2026-08-16 10:00:00", title="ok", content="", channels="要闻"),
        _row("garbage", title="bad", content="300012", channels="要闻"),
    ]
    reader, _ = _reader(rows, lookback_minutes=1440)

    evidence = reader.read_evidence("300012.SZ", "20260816")

    assert evidence.has_evidence is True
    assert evidence.total_rows == 1
    assert evidence.code_matches == 0


def test_constructor_rejects_invalid_schema_major():
    client = FakeClient([])

    with pytest.raises(ValueError, match="schema_major"):
        NewsSentimentReader(client, schema_major=0)


def test_cross_source_same_title_deduped():
    news_rows = [
        _row(
            "2026-08-16 10:00:00",
            title="华测检测发布半年度报告",
            content="300012 披露半年报",
            channels="公司",
        ),
    ]
    flash_rows = [
        _flash_row(
            "2026-08-16T10:05:00+08:00",
            title="华测检测发布半年度报告",
            summary="300012 披露半年报",
        ),
    ]
    reader, _ = _reader(news_rows, flash_rows=flash_rows)

    evidence = reader.read_evidence("300012.SZ", "20260816")

    assert evidence.has_evidence is True
    assert evidence.unique_events == 1
    assert evidence.total_rows == 1
    assert evidence.raw_rows == 2
    assert evidence.code_matches == 1
    assert evidence.name_matches == 0
    assert evidence.stock_matches == 1


def test_cross_source_title_differing_only_by_punctuation_deduped():
    news_rows = [
        _row(
            "2026-08-16 10:00:00",
            title="华测检测发布半年度报告",
            content="300012",
            channels="公司",
        ),
    ]
    flash_rows = [
        _flash_row(
            "2026-08-16T10:05:00+08:00",
            title="华测检测 发布半年度报告！",
            summary="300012",
        ),
    ]
    reader, _ = _reader(news_rows, flash_rows=flash_rows)

    evidence = reader.read_evidence("300012.SZ", "20260816")

    assert evidence.has_evidence is True
    assert evidence.unique_events == 1
    assert evidence.total_rows == 1
    assert evidence.raw_rows == 2
    assert evidence.code_matches == 1


def test_cross_source_different_titles_kept_separate():
    news_rows = [
        _row("2026-08-16 10:00:00", title="华测检测中标大单", content="300012", channels="公司"),
    ]
    flash_rows = [
        _flash_row(
            "2026-08-16T10:05:00+08:00",
            title="华测检测发布半年度报告",
            summary="300012",
        ),
    ]
    reader, _ = _reader(news_rows, flash_rows=flash_rows)

    evidence = reader.read_evidence("300012.SZ", "20260816")

    assert evidence.has_evidence is True
    assert evidence.unique_events == 2
    assert evidence.total_rows == 2
    assert evidence.raw_rows == 2
    assert evidence.code_matches == 2


def test_flash_source_failure_degrades_to_news():
    news_rows = [
        _row("2026-08-16 10:00:00", title="华测检测获订单", content="300012", channels="要闻"),
    ]
    reader, _ = _reader(news_rows, flash_error=RuntimeError("flash down"))

    evidence = reader.read_evidence("300012.SZ", "20260816")

    assert evidence.has_evidence is True
    assert evidence.total_rows == 1
    assert evidence.code_matches == 1
    assert "degraded_sources=cn.news.flash" in evidence.reason


def test_news_source_failure_degrades_to_flash():
    flash_rows = [
        _flash_row(
            "2026-08-16T10:00:00+08:00",
            title="华测检测获订单",
            summary="300012 中标项目",
        ),
    ]
    reader, _ = _reader(None, flash_rows=flash_rows, news_error=RuntimeError("news down"))

    evidence = reader.read_evidence("300012.SZ", "20260816")

    assert evidence.has_evidence is True
    assert evidence.total_rows == 1
    assert evidence.code_matches == 1
    assert "degraded_sources=cn.dataset.news" in evidence.reason


def _catalog_payload():
    return {
        "api_version": "v1",
        "catalog_version": CATALOG_VERSION,
        "request_id": "catalog-req",
        "data": [
            {
                "dataset_id": NEWS_DATASET_ID,
                "limits": {"max_page_size": 250, "max_lookback_days": 36500},
            },
            {
                "dataset_id": FLASH_DATASET_ID,
                "limits": {"max_page_size": 100, "max_lookback_days": 36500},
            },
        ],
    }


def _query_payload(dataset_id, rows):
    return {
        "api_version": "v1",
        "catalog_version": CATALOG_VERSION,
        "request_id": "query-req",
        "dataset_id": dataset_id,
        "data": rows,
        "next_cursor": None,
        "metadata": {
            "state": "ready",
            "degraded": False,
            "freshness": {"state": "fresh", "age_seconds": 1},
            "quality": {"state": "valid", "score": 1.0},
            "lineage": {"complete": True, "provider_neutral": True},
            "receipt_id": "receipt-001",
            "data_through": "2026-08-16T08:00:00+00:00",
            "observed_at": "2026-08-16T08:00:00+00:00",
            "reasons": [],
        },
    }


def _query_payloads(news_rows=(), flash_rows=()):
    return {
        NEWS_DATASET_ID: _query_payload(NEWS_DATASET_ID, list(news_rows)),
        FLASH_DATASET_ID: _query_payload(FLASH_DATASET_ID, list(flash_rows)),
    }


class FakeTransport:
    def __init__(self, catalog_payload, query_payloads):
        self.catalog_payload = catalog_payload
        self.query_payloads = query_payloads
        self.calls = []

    def __call__(self, *, method, url, headers, json_body, timeout_seconds):
        self.calls.append((method, url))
        if method == "GET" and url.endswith("/v1/catalog"):
            return HTTPResponse(200, self.catalog_payload)
        if method == "POST" and url.endswith("/v1/query"):
            dataset_id = json_body.get("dataset_id")
            return HTTPResponse(200, self.query_payloads[dataset_id])
        raise AssertionError(f"unexpected transport call: {method} {url}")


def test_from_runtime_observes_catalog_before_query():
    rows = [_row("2026-08-16 10:00:00", title="新闻", content="300012", channels="要闻")]
    transport = FakeTransport(_catalog_payload(), _query_payloads(news_rows=rows))

    def transport_factory(transport_id, *, token_file, base_url):
        assert transport_id == "http-json-v1"
        return transport

    reader = NewsSentimentReader.from_runtime(
        base_url="https://fixture.invalid",
        token_file="/run/secrets/tradingagent/fixture.token",
        expected_catalog_version=CATALOG_VERSION,
        schema_major=1,
        access_policy_id="ta-paper-read-v1",
        transport_factory=transport_factory,
        lookback_minutes=1440,
        max_rows=1000,
    )

    evidence = reader.read_evidence("300012.SZ", "20260816")

    assert evidence.has_evidence is True
    assert evidence.code_matches == 1
    assert any(url.endswith("/v1/catalog") for _, url in transport.calls)
    assert any(url.endswith("/v1/query") for _, url in transport.calls)


def test_from_runtime_clamps_both_sources_to_their_page_sizes():
    transport = FakeTransport(_catalog_payload(), _query_payloads())

    def transport_factory(transport_id, *, token_file, base_url):
        return transport

    reader = NewsSentimentReader.from_runtime(
        base_url="https://fixture.invalid",
        token_file="/run/secrets/tradingagent/fixture.token",
        expected_catalog_version=CATALOG_VERSION,
        schema_major=1,
        access_policy_id="ta-paper-read-v1",
        transport_factory=transport_factory,
        max_rows=2000,
    )
    # The fixture catalog declares max_page_size=250 for news and 100 for
    # flash, so the reader must clamp 2000 down to each source's own limit.
    assert reader._max_rows == 250
    assert reader._flash_max_rows == 100


class EmptySentimentReader:
    def get_sentiment(self, *args, **kwargs):
        return []


class FakeNewsReader:
    def __init__(self, evidence):
        self.evidence = evidence
        self.calls = []

    def read_evidence(self, ts_code, date, *, name=None):
        self.calls.append((ts_code, date, name))
        return self.evidence


def test_score_sentiment_uses_news_evidence_when_sharedsignals_empty():
    evidence = NewsSentimentEvidence(
        status="evidence",
        reason="code_matches=2, name_matches=0, total_rows=3",
        window_start="2026-08-15 23:59:59",
        window_end="2026-08-16 23:59:59",
        total_rows=3,
        code_matches=2,
        name_matches=0,
        stock_matches=2,
        channel_counts={"要闻": 3},
    )
    news_reader = FakeNewsReader(evidence)
    config = {
        "_data_reader": EmptySentimentReader(),
        "_news_sentiment_reader": news_reader,
        "_symbol_name": "华测检测",
        "dimensions": {"sentiment": {"extreme_threshold": 0.95}},
    }

    score = scorer._score_sentiment("300012.SZ", "20260816", config)

    assert score == 0.5
    assert config["_dimension_evidence"]["sentiment"]["has_evidence"] is True
    assert config["_dimension_evidence"]["sentiment"]["source"] == "TradingDatas news sentiment"
    assert config["_dimension_evidence"]["sentiment"]["row_count"] == 3
    assert news_reader.calls == [("300012.SZ", "20260816", "华测检测")]


def test_score_sentiment_news_no_evidence_marks_missing():
    evidence = NewsSentimentEvidence(
        status="no_evidence",
        reason="news_client_error",
        window_start="2026-08-15 23:59:59",
        window_end="2026-08-16 23:59:59",
        total_rows=0,
        code_matches=0,
        name_matches=0,
        stock_matches=0,
        channel_counts={},
    )
    news_reader = FakeNewsReader(evidence)
    config = {
        "_data_reader": EmptySentimentReader(),
        "_news_sentiment_reader": news_reader,
        "dimensions": {"sentiment": {"extreme_threshold": 0.95}},
    }

    score = scorer._score_sentiment("300012.SZ", "20260816", config)

    assert score == 0.5
    sent = config["_dimension_evidence"]["sentiment"]
    assert sent["has_evidence"] is False
    assert sent["source"] == "TradingDatas news sentiment"
    assert sent["reason"] == "news_client_error"
