from __future__ import annotations

import copy
import inspect
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import pytest

from shared.data.sharedsignals_v1 import (
    QUERY_RESPONSE_SCHEMA_ID,
    CacheKey,
    CatalogContractError,
    ContractViolation,
    HTTPResponse,
    HTTPStatusError,
    QueryRequest,
    SharedSignalsV1Client,
    SharedSignalsV1Config,
    TransportNotConfigured,
    parse_catalog_envelope,
    parse_query_envelope,
)


DATASET_ID = "fixture.cn.equity.daily.mainboard.v1"
CATALOG_VERSION = "fixture-catalog-2026-07-16"
SCHEMA_MAJOR = 1


def _config(
    *,
    access_policy_id: str = "ta-paper-read-v1",
    dataset_ids: frozenset[str] = frozenset({DATASET_ID}),
) -> SharedSignalsV1Config:
    return SharedSignalsV1Config(
        base_url="https://tradingdatas.fixture.invalid",
        expected_catalog_version=CATALOG_VERSION,
        dataset_ids=dataset_ids,
        access_policy_id=access_policy_id,
        cache_ttl_seconds=30.0,
    )


def _metadata(
    *,
    state: str = "ready",
    degraded: bool = False,
    receipt_id: str = "receipt-001",
) -> dict[str, Any]:
    return {
        "state": state,
        "degraded": degraded,
        "freshness": {"state": "fresh", "age_seconds": 3},
        "quality": {"state": "valid", "score": 0.99},
        "lineage": {"complete": True, "provider_neutral": True},
        "receipt_id": receipt_id,
        "data_through": "2026-07-15T07:00:00+00:00",
        "observed_at": "2026-07-16T01:00:00+00:00",
        "reasons": [],
    }


def _query_payload(
    *,
    state: str = "ready",
    degraded: bool = False,
    receipt_id: str = "receipt-001",
    catalog_version: str = CATALOG_VERSION,
    next_cursor: str | None = "cursor-next",
) -> dict[str, Any]:
    return {
        "api_version": "v1",
        "catalog_version": catalog_version,
        "request_id": "request-001",
        "dataset_id": DATASET_ID,
        "data": [{"ts_code": "600000.SH", "close": 10.5}],
        "next_cursor": next_cursor,
        "metadata": _metadata(
            state=state,
            degraded=degraded,
            receipt_id=receipt_id,
        ),
    }


@dataclass
class FakeTransport:
    responses: list[HTTPResponse]
    calls: list[dict[str, Any]] = field(default_factory=list)

    def __call__(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        json_body: Mapping[str, Any] | None,
        timeout_seconds: float,
    ) -> HTTPResponse:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "json_body": dict(json_body) if json_body is not None else None,
                "timeout_seconds": timeout_seconds,
            }
        )
        if not self.responses:
            raise AssertionError("unexpected transport call")
        return self.responses.pop(0)


def test_configuration_requires_explicit_contract_identity() -> None:
    with pytest.raises(ValueError, match="base_url"):
        SharedSignalsV1Config(
            base_url="",
            expected_catalog_version=CATALOG_VERSION,
            dataset_ids=frozenset({DATASET_ID}),
            access_policy_id="ta-paper-read-v1",
        )
    with pytest.raises(ValueError, match="expected_catalog_version"):
        SharedSignalsV1Config(
            base_url="http://fixture.invalid",
            expected_catalog_version="",
            dataset_ids=frozenset({DATASET_ID}),
            access_policy_id="ta-paper-read-v1",
        )
    with pytest.raises(ValueError, match="dataset_ids"):
        SharedSignalsV1Config(
            base_url="http://fixture.invalid",
            expected_catalog_version=CATALOG_VERSION,
            dataset_ids=frozenset(),
            access_policy_id="ta-paper-read-v1",
        )
    with pytest.raises(ValueError, match="access_policy_id"):
        SharedSignalsV1Config(
            base_url="http://fixture.invalid",
            expected_catalog_version=CATALOG_VERSION,
            dataset_ids=frozenset({DATASET_ID}),
            access_policy_id="",
        )


def test_client_has_no_default_network_transport() -> None:
    client = SharedSignalsV1Client(_config())

    with pytest.raises(TransportNotConfigured):
        client.get_catalog()
    with pytest.raises(TransportNotConfigured):
        client.query(QueryRequest(dataset_id=DATASET_ID, schema_major=SCHEMA_MAJOR))


def test_catalog_uses_only_v1_catalog_and_validates_configured_datasets() -> None:
    transport = FakeTransport(
        [
            HTTPResponse(
                status_code=200,
                json_body={
                    "api_version": "v1",
                    "catalog_version": CATALOG_VERSION,
                    "request_id": "catalog-request-001",
                    "data": [{"dataset_id": DATASET_ID, "fields": ["ts_code"]}],
                },
            )
        ]
    )
    client = SharedSignalsV1Client(_config(), transport=transport)

    catalog = client.get_catalog()

    assert catalog.api_version == "v1"
    assert catalog.catalog_version == CATALOG_VERSION
    assert catalog.request_id == "catalog-request-001"
    assert catalog.dataset_ids == frozenset({DATASET_ID})
    assert transport.calls == [
        {
            "method": "GET",
            "url": "https://tradingdatas.fixture.invalid/v1/catalog",
            "headers": {
                "Accept": "application/json",
            },
            "json_body": None,
            "timeout_seconds": 10.0,
        }
    ]


def test_catalog_version_and_dataset_mismatch_fail_closed() -> None:
    wrong_version = FakeTransport(
        [
            HTTPResponse(
                200,
                {
                    "api_version": "v1",
                    "catalog_version": "unexpected",
                    "request_id": "catalog-request-001",
                    "data": [{"dataset_id": DATASET_ID}],
                },
            )
        ]
    )
    with pytest.raises(CatalogContractError, match="catalog_version"):
        SharedSignalsV1Client(_config(), transport=wrong_version).get_catalog()

    missing_dataset = FakeTransport(
        [
            HTTPResponse(
                200,
                {
                    "api_version": "v1",
                    "catalog_version": CATALOG_VERSION,
                    "request_id": "catalog-request-001",
                    "data": [{"dataset_id": "another.dataset.v1"}],
                },
            )
        ]
    )
    with pytest.raises(CatalogContractError, match="configured dataset"):
        SharedSignalsV1Client(_config(), transport=missing_dataset).get_catalog()


def test_query_posts_provider_neutral_contract_and_preserves_envelope() -> None:
    transport = FakeTransport([HTTPResponse(200, _query_payload())])
    client = SharedSignalsV1Client(_config(), transport=transport)
    request = QueryRequest(
        dataset_id=DATASET_ID,
        schema_major=SCHEMA_MAJOR,
        fields=("ts_code", "close"),
        filters={"ts_code": ["600000.SH"], "trade_date_gte": "20260701"},
        as_of="2026-07-16T01:00:00+00:00",
        limit=250,
        cursor="cursor-001",
    )

    result = client.query(request)

    assert transport.calls[0]["method"] == "POST"
    assert transport.calls[0]["url"].endswith("/v1/query")
    assert transport.calls[0]["json_body"] == {
        "dataset_id": DATASET_ID,
        "schema_major": SCHEMA_MAJOR,
        "fields": ["ts_code", "close"],
        "filters": {
            "ts_code": ["600000.SH"],
            "trade_date_gte": "20260701",
        },
        "as_of": "2026-07-16T01:00:00+00:00",
        "limit": 250,
        "cursor": "cursor-001",
    }
    assert "provider" not in transport.calls[0]["json_body"]
    assert "order" not in transport.calls[0]["json_body"]
    assert result.api_version == "v1"
    assert result.catalog_version == CATALOG_VERSION
    assert result.request_id == "request-001"
    assert result.dataset_id == DATASET_ID
    assert result.data == ({"ts_code": "600000.SH", "close": 10.5},)
    assert result.next_cursor == "cursor-next"
    assert result.metadata.state == "ready"
    assert result.metadata.degraded is False
    assert result.metadata.freshness == {"state": "fresh", "age_seconds": 3}
    assert result.metadata.quality == {"state": "valid", "score": 0.99}
    assert result.metadata.lineage == {
        "complete": True,
        "provider_neutral": True,
    }
    assert result.metadata.receipt_id == "receipt-001"
    assert result.metadata.data_through == "2026-07-15T07:00:00+00:00"
    assert result.metadata.observed_at == "2026-07-16T01:00:00+00:00"
    assert result.metadata.reasons == ()


def test_query_request_snapshots_nested_filters_for_stable_identity() -> None:
    raw_filters = {"ts_code": ["600000.SH"]}
    request = QueryRequest(
        dataset_id=DATASET_ID,
        schema_major=SCHEMA_MAJOR,
        filters=raw_filters,
    )
    original_sha = request.sha256

    raw_filters["ts_code"].append("600001.SH")

    assert request.sha256 == original_sha
    assert request.to_payload()["filters"] == {"ts_code": ["600000.SH"]}


def test_query_request_requires_positive_native_schema_major() -> None:
    with pytest.raises(TypeError, match="schema_major"):
        QueryRequest(dataset_id=DATASET_ID)  # type: ignore[call-arg]
    for invalid in (True, 0, -1, 1.0, "1"):
        with pytest.raises(ContractViolation, match="schema_major"):
            QueryRequest(
                dataset_id=DATASET_ID,
                schema_major=invalid,  # type: ignore[arg-type]
            )


def test_explicit_order_is_copied_and_omitted_when_not_requested() -> None:
    raw_order = ["trade_date:desc", "ts_code:asc"]
    ordered = QueryRequest(
        dataset_id=DATASET_ID,
        schema_major=SCHEMA_MAJOR,
        order=raw_order,
    )
    defaulted = QueryRequest(dataset_id=DATASET_ID, schema_major=SCHEMA_MAJOR)
    original_sha = ordered.sha256

    raw_order.append("close:desc")

    assert ordered.to_payload()["order"] == ["trade_date:desc", "ts_code:asc"]
    assert ordered.sha256 == original_sha
    assert "order" not in defaulted.to_payload()


def test_as_of_is_omitted_when_not_requested() -> None:
    defaulted = QueryRequest(dataset_id=DATASET_ID, schema_major=SCHEMA_MAJOR)
    bounded = QueryRequest(
        dataset_id=DATASET_ID,
        schema_major=SCHEMA_MAJOR,
        as_of="2026-07-16T01:00:00+00:00",
    )

    assert "as_of" not in defaulted.to_payload()
    assert bounded.to_payload()["as_of"] == "2026-07-16T01:00:00+00:00"


@pytest.mark.parametrize("invalid", ["trade_date:desc", [""], ["x", "x"]])
def test_explicit_order_must_be_unique_nonempty_terms(invalid: object) -> None:
    with pytest.raises(ContractViolation, match="order"):
        QueryRequest(
            dataset_id=DATASET_ID,
            schema_major=SCHEMA_MAJOR,
            order=invalid,  # type: ignore[arg-type]
        )


def test_http_200_does_not_launder_dataset_degraded_state() -> None:
    payload = _query_payload(state="degraded", degraded=True)
    payload["metadata"]["reasons"] = ["coverage_partial"]
    transport = FakeTransport([HTTPResponse(200, payload)])
    client = SharedSignalsV1Client(_config(), transport=transport)

    result = client.query(
        QueryRequest(dataset_id=DATASET_ID, schema_major=SCHEMA_MAJOR)
    )

    assert result.metadata.state == "degraded"
    assert result.metadata.degraded is True
    assert result.metadata.reasons == ("coverage_partial",)


@pytest.mark.parametrize(
    ("state", "degraded"),
    [
        ("degraded", True),
        ("stale", False),
        ("failed", False),
        ("paused", False),
        ("unobserved", False),
        ("empty", False),
        ("healthy", False),
        ("ready", True),
    ],
)
def test_complete_proof_impaired_dataset_is_never_cached(
    state: str,
    degraded: bool,
) -> None:
    payload = _query_payload(state=state, degraded=degraded)
    payload["metadata"]["reasons"] = [f"dataset_{state}"]
    transport = FakeTransport([HTTPResponse(200, payload), HTTPResponse(200, payload)])
    client = SharedSignalsV1Client(_config(), transport=transport)
    request = QueryRequest(dataset_id=DATASET_ID, schema_major=SCHEMA_MAJOR)

    first = client.query(request)
    second = client.query(request)

    assert first.metadata.state == state
    assert first.metadata.degraded is degraded
    assert second == first
    assert client.cache_keys == ()
    assert len(transport.calls) == 2


@pytest.mark.parametrize(
    "state",
    ["unobserved", "paused", "failed", "stale", "empty", "degraded"],
)
def test_impaired_dataset_may_omit_source_proof_without_fabrication_or_cache(
    state: str,
) -> None:
    def impaired_payload() -> dict[str, Any]:
        payload = _query_payload(state=state, degraded=state == "degraded")
        payload["data"] = []
        payload["next_cursor"] = None
        payload["metadata"].update(
            {
                "lineage": None,
                "receipt_id": None,
                "data_through": None,
                "observed_at": None,
                "reasons": [f"dataset_{state}"],
            }
        )
        return payload

    transport = FakeTransport(
        [HTTPResponse(200, impaired_payload()), HTTPResponse(200, impaired_payload())]
    )
    client = SharedSignalsV1Client(_config(), transport=transport)
    request = QueryRequest(dataset_id=DATASET_ID, schema_major=SCHEMA_MAJOR)

    first = client.query(request)
    second = client.query(request)

    assert first.metadata.lineage is None
    assert first.metadata.receipt_id is None
    assert first.metadata.data_through is None
    assert first.metadata.observed_at is None
    assert second == first
    assert client.cache_keys == ()
    assert len(transport.calls) == 2


@pytest.mark.parametrize(
    "field_name",
    ["lineage", "receipt_id", "data_through", "observed_at"],
)
def test_ready_dataset_requires_complete_source_proof(field_name: str) -> None:
    payload = _query_payload()
    payload["metadata"][field_name] = None
    client = SharedSignalsV1Client(
        _config(),
        transport=FakeTransport([HTTPResponse(200, payload)]),
    )

    with pytest.raises(ContractViolation, match=field_name):
        client.query(QueryRequest(dataset_id=DATASET_ID, schema_major=SCHEMA_MAJOR))

    assert client.cache_keys == ()


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda payload: payload.pop("metadata"), "metadata"),
        (lambda payload: payload["metadata"].pop("receipt_id"), "receipt_id"),
        (
            lambda payload: payload["metadata"].update({"degraded": "false"}),
            "degraded",
        ),
        (
            lambda payload: payload["metadata"].update({"freshness": "fresh"}),
            "freshness",
        ),
        (
            lambda payload: payload["metadata"].update({"observed_at": "2026-07-16"}),
            "observed_at",
        ),
        (lambda payload: payload.update({"data": ["not-a-row"]}), "data"),
    ],
)
def test_query_schema_is_strict_and_invalid_results_are_not_cached(
    mutation: Any,
    message: str,
) -> None:
    invalid = _query_payload()
    mutation(invalid)
    transport = FakeTransport(
        [HTTPResponse(200, invalid), HTTPResponse(200, _query_payload())]
    )
    client = SharedSignalsV1Client(_config(), transport=transport)
    request = QueryRequest(dataset_id=DATASET_ID, schema_major=SCHEMA_MAJOR)

    with pytest.raises(ContractViolation, match=message):
        client.query(request)

    valid = client.query(request)
    assert valid.metadata.receipt_id == "receipt-001"
    assert len(transport.calls) == 2


def test_non_200_response_never_becomes_dataset_evidence_or_cache() -> None:
    transport = FakeTransport([HTTPResponse(503, {"error": "upstream unavailable"})])
    client = SharedSignalsV1Client(_config(), transport=transport)

    with pytest.raises(HTTPStatusError, match="HTTP 503"):
        client.query(QueryRequest(dataset_id=DATASET_ID, schema_major=SCHEMA_MAJOR))

    assert client.cache_keys == ()


def test_optional_pagination_cursor_is_strict_and_preserved() -> None:
    payload = _query_payload()
    payload["next_cursor"] = 123
    client = SharedSignalsV1Client(
        _config(),
        transport=FakeTransport([HTTPResponse(200, payload)]),
    )

    with pytest.raises(ContractViolation, match="next_cursor"):
        client.query(QueryRequest(dataset_id=DATASET_ID, schema_major=SCHEMA_MAJOR))


def test_query_rejects_unconfigured_dataset_before_transport() -> None:
    transport = FakeTransport([HTTPResponse(200, _query_payload())])
    client = SharedSignalsV1Client(_config(), transport=transport)

    with pytest.raises(ContractViolation, match="dataset_id"):
        client.query(
            QueryRequest(
                dataset_id="fixture.cn.equity.star.daily.v1",
                schema_major=SCHEMA_MAJOR,
            )
        )

    assert transport.calls == []


def test_query_catalog_and_dataset_identity_must_match_request() -> None:
    wrong_catalog = FakeTransport(
        [HTTPResponse(200, _query_payload(catalog_version="unexpected"))]
    )
    with pytest.raises(ContractViolation, match="catalog_version"):
        SharedSignalsV1Client(_config(), transport=wrong_catalog).query(
            QueryRequest(dataset_id=DATASET_ID, schema_major=SCHEMA_MAJOR)
        )

    wrong_dataset_payload = _query_payload()
    wrong_dataset_payload["dataset_id"] = "another.dataset.v1"
    wrong_dataset = FakeTransport([HTTPResponse(200, wrong_dataset_payload)])
    with pytest.raises(ContractViolation, match="dataset_id"):
        SharedSignalsV1Client(_config(), transport=wrong_dataset).query(
            QueryRequest(dataset_id=DATASET_ID, schema_major=SCHEMA_MAJOR)
        )


def test_query_rejects_data_through_after_requested_as_of() -> None:
    payload = _query_payload()
    payload["metadata"]["data_through"] = "2026-07-16T02:00:00+00:00"
    payload["metadata"]["observed_at"] = "2026-07-16T03:00:00+00:00"
    transport = FakeTransport([HTTPResponse(200, payload)])
    client = SharedSignalsV1Client(_config(), transport=transport)

    with pytest.raises(ContractViolation, match="data_through.*as_of"):
        client.query(
            QueryRequest(
                dataset_id=DATASET_ID,
                schema_major=SCHEMA_MAJOR,
                as_of="2026-07-16T01:00:00+00:00",
            )
        )

    assert client.cache_keys == ()


def test_query_rejects_data_through_after_observed_at() -> None:
    payload = _query_payload()
    payload["metadata"]["data_through"] = "2026-07-17T01:00:00+00:00"
    transport = FakeTransport([HTTPResponse(200, payload)])
    client = SharedSignalsV1Client(_config(), transport=transport)

    with pytest.raises(ContractViolation, match="data_through.*observed_at"):
        client.query(QueryRequest(dataset_id=DATASET_ID, schema_major=SCHEMA_MAJOR))

    assert client.cache_keys == ()


def test_cache_only_contains_validated_envelopes_and_binds_full_identity() -> None:
    transport = FakeTransport([HTTPResponse(200, _query_payload(next_cursor=None))])
    client = SharedSignalsV1Client(_config(), transport=transport)
    request = QueryRequest(
        dataset_id=DATASET_ID,
        schema_major=SCHEMA_MAJOR,
        limit=20,
    )

    first = client.query(request)
    second = client.query(request)

    assert second == first
    assert len(transport.calls) == 1
    assert client.cache_keys == (
        CacheKey(
            query_sha256=request.sha256,
            catalog_version=CATALOG_VERSION,
            schema_id=QUERY_RESPONSE_SCHEMA_ID,
            receipt_id="receipt-001",
            access_policy_id="ta-paper-read-v1",
        ),
    )


def test_cached_envelope_cannot_be_mutated_through_a_prior_result() -> None:
    transport = FakeTransport([HTTPResponse(200, _query_payload(next_cursor=None))])
    client = SharedSignalsV1Client(_config(), transport=transport)
    request = QueryRequest(dataset_id=DATASET_ID, schema_major=SCHEMA_MAJOR)

    first = client.query(request)
    first.data[0]["close"] = 999.0
    first.metadata.quality["state"] = "failed"
    second = client.query(request)

    assert first.data[0]["close"] == 10.5
    assert first.metadata.quality["state"] == "valid"
    assert second.data[0]["close"] == 10.5
    assert second.metadata.quality["state"] == "valid"
    assert len(transport.calls) == 1


def test_query_envelope_recursively_snapshots_source_and_materialized_copies() -> None:
    payload = _query_payload()
    payload["data"][0]["nested"] = {
        "tags": ["original"],
        "attributes": {"rank": 1},
    }
    payload["metadata"]["freshness"]["windows"] = [{"name": "daily", "ready": True}]
    envelope = parse_query_envelope(payload)

    payload["data"][0]["nested"]["tags"].append("source-mutated")
    payload["metadata"]["freshness"]["windows"][0]["ready"] = False
    leaked_row = envelope.data[0]
    leaked_row["nested"]["attributes"]["rank"] = 999
    leaked_freshness = envelope.metadata.freshness
    leaked_freshness["windows"][0]["ready"] = False

    assert envelope.data[0]["nested"] == {
        "tags": ["original"],
        "attributes": {"rank": 1},
    }
    assert envelope.metadata.freshness["windows"] == [{"name": "daily", "ready": True}]
    assert copy.deepcopy(envelope) == envelope


def test_catalog_envelope_recursively_snapshots_nested_fields() -> None:
    payload = {
        "api_version": "v1",
        "catalog_version": CATALOG_VERSION,
        "request_id": "catalog-request-001",
        "data": [
            {
                "dataset_id": DATASET_ID,
                "fields": [{"name": "close", "aliases": ["px_close"]}],
            }
        ],
    }
    envelope = parse_catalog_envelope(payload)

    payload["data"][0]["fields"][0]["aliases"].append("source-mutated")
    leaked = envelope.data[0]
    leaked["fields"][0]["aliases"].append("copy-mutated")

    assert envelope.data[0]["fields"] == [{"name": "close", "aliases": ["px_close"]}]
    assert copy.deepcopy(envelope) == envelope


def test_access_policy_is_local_cache_identity_not_an_invented_wire_header() -> None:
    left_transport = FakeTransport(
        [
            HTTPResponse(
                200,
                _query_payload(receipt_id="receipt-left", next_cursor=None),
            )
        ]
    )
    right_transport = FakeTransport(
        [
            HTTPResponse(
                200,
                _query_payload(receipt_id="receipt-right", next_cursor=None),
            )
        ]
    )
    request = QueryRequest(dataset_id=DATASET_ID, schema_major=SCHEMA_MAJOR)
    left = SharedSignalsV1Client(
        _config(access_policy_id="paper-left"), transport=left_transport
    )
    right = SharedSignalsV1Client(
        _config(access_policy_id="paper-right"), transport=right_transport
    )

    assert left.query(request).metadata.receipt_id == "receipt-left"
    assert right.query(request).metadata.receipt_id == "receipt-right"
    assert left.cache_keys[0].access_policy_id == "paper-left"
    assert right.cache_keys[0].access_policy_id == "paper-right"
    assert left_transport.calls[0]["headers"] == {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    assert right_transport.calls[0]["headers"] == {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    assert "X-Access-Policy" not in inspect.getsource(SharedSignalsV1Client._headers)


def test_nonterminal_page_is_never_cached_and_uncached_reads_bypass_cache() -> None:
    transport = FakeTransport(
        [
            HTTPResponse(200, _query_payload(next_cursor="opaque-next")),
            HTTPResponse(200, _query_payload(next_cursor=None)),
            HTTPResponse(200, _query_payload(next_cursor=None)),
        ]
    )
    client = SharedSignalsV1Client(_config(), transport=transport)
    request = QueryRequest(dataset_id=DATASET_ID, schema_major=SCHEMA_MAJOR)

    assert client.query(request).next_cursor == "opaque-next"
    assert client.cache_keys == ()
    assert client.query(request).next_cursor is None
    assert len(client.cache_keys) == 1
    assert client.query_uncached(request).next_cursor is None
    assert len(transport.calls) == 3


def test_client_source_contains_no_legacy_or_direct_storage_fallbacks() -> None:
    source = inspect.getsource(
        __import__("shared.data.sharedsignals_v1", fromlist=["*"])
    )

    assert '"/v1/catalog"' in source
    assert '"/v1/query"' in source
    assert '"/tushare"' not in source
    assert '"/market_data"' not in source
    assert "sqlite" not in source.lower()
    assert "duckdb" not in source.lower()
    assert "csv" not in source.lower()
