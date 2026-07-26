from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

import pytest

from shared.data import tradingdatas_pagination as pagination
from shared.data.sharedsignals_v1 import (
    HTTPResponse,
    QueryRequest,
    SharedSignalsV1Client,
    SharedSignalsV1Config,
)


CATALOG = "fixture-catalog-identityless-v1"
DATASET = "fixture.provider.identityless"


def _metadata() -> dict[str, Any]:
    return {
        "state": "partial",
        "degraded": True,
        "freshness": {"state": "unknown", "fresh": False},
        "quality": {"state": "degraded", "valid": True},
        "lineage": {
            "state": "complete",
            "complete": True,
            "provider_neutral": True,
            "provider": "fixture",
            "transport_service": "fixture",
        },
        "receipt_id": "receipt-identityless",
        "data_through": "2026-07-22T15:00:00+08:00",
        "observed_at": "2026-07-22T15:01:00+08:00",
        "reasons": ["fixture-impaired"],
    }


def _response(
    *,
    request_id: str,
    rows: list[dict[str, Any]],
    next_cursor: str | None = None,
) -> HTTPResponse:
    return HTTPResponse(
        status_code=200,
        json_body={
            "api_version": "v1",
            "catalog_version": CATALOG,
            "request_id": request_id,
            "dataset_id": DATASET,
            "data": copy.deepcopy(rows),
            "next_cursor": next_cursor,
            "metadata": _metadata(),
        },
    )


class _Transport:
    def __init__(self, responses: list[HTTPResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> HTTPResponse:
        self.calls.append(copy.deepcopy(kwargs))
        if not self.responses:
            raise AssertionError("unexpected extra request")
        return self.responses.pop(0)


def _client(transport: _Transport) -> SharedSignalsV1Client:
    return SharedSignalsV1Client(
        SharedSignalsV1Config(
            base_url="https://tradingdatas.fixture.invalid",
            expected_catalog_version=CATALOG,
            dataset_ids=frozenset({DATASET}),
            access_policy_id="fixture-read",
            max_limit=500,
            cache_ttl_seconds=0,
        ),
        transport=transport,
    )


def _request(*, filters: dict[str, Any] | None = None) -> QueryRequest:
    return QueryRequest(
        dataset_id=DATASET,
        schema_major=1,
        fields=("provider_value", "provider_label"),
        filters=filters or {},
        as_of=None,
        order=None,
        limit=500,
    )


def _identityless_collector() -> Callable[..., Any]:
    collector = getattr(pagination, "collect_identityless_single_page", None)
    assert callable(collector), "identityless single-page collector is missing"
    return collector


def test_identityless_empty_terminal_page_allows_zero_row_budget() -> None:
    collector = _identityless_collector()
    transport = _Transport([_response(request_id="empty", rows=[])])

    run = collector(
        client=_client(transport),
        request=_request(),
        max_pages=1,
        max_rows=0,
    )

    run.verify_integrity()
    assert run.page_count == 1
    assert run.row_count == 0
    assert run.envelope.next_cursor is None
    assert run.identity_authority_available is False
    assert run.identity_sha256 is None
    assert len(transport.calls) == 1
    assert transport.calls[0]["json_body"]["limit"] == 500
    assert "as_of" not in transport.calls[0]["json_body"]
    assert "order" not in transport.calls[0]["json_body"]


def test_identityless_semantics_bind_exact_request_ordered_rows_and_envelope() -> None:
    collector = _identityless_collector()
    rows = [
        {"provider_value": 1, "provider_label": "same"},
        {"provider_value": 1, "provider_label": "same"},
    ]
    first = collector(
        client=_client(_Transport([_response(request_id="first", rows=rows)])),
        request=_request(),
        max_pages=1,
        max_rows=2,
    )
    second = collector(
        client=_client(_Transport([_response(request_id="second", rows=rows)])),
        request=_request(),
        max_pages=1,
        max_rows=2,
    )
    changed_request = collector(
        client=_client(_Transport([_response(request_id="third", rows=rows)])),
        request=_request(filters={"provider_value": {"eq": 1}}),
        max_pages=1,
        max_rows=2,
    )

    first.verify_integrity()
    second.verify_integrity()
    changed_request.verify_integrity()
    assert first.semantic_sha256 == second.semantic_sha256
    assert first.semantic_trace_sha256 == second.semantic_trace_sha256
    assert first.pagination_trace_sha256 != second.pagination_trace_sha256
    assert first.semantic_sha256 != changed_request.semantic_sha256
    assert first.identity_authority_available is False
    assert first.identity_sha256 is None
    assert first.row_count == 2


def test_identityless_nonterminal_page_is_rejected_without_following_cursor() -> None:
    collector = _identityless_collector()
    transport = _Transport(
        [_response(request_id="nonterminal", rows=[], next_cursor="opaque-cursor")]
    )

    with pytest.raises(
        pagination.PaginationContractError,
        match="^pagination_page_budget_exceeded$",
    ):
        collector(
            client=_client(transport),
            request=_request(),
            max_pages=1,
            max_rows=0,
        )

    assert len(transport.calls) == 1


def test_identityless_response_over_frozen_row_budget_fails_after_read() -> None:
    collector = _identityless_collector()
    transport = _Transport(
        [_response(request_id="over-budget", rows=[{"provider_value": 1}])]
    )

    with pytest.raises(
        pagination.PaginationContractError,
        match="^pagination_row_budget_exceeded$",
    ):
        collector(
            client=_client(transport),
            request=_request(),
            max_pages=1,
            max_rows=0,
        )

    assert len(transport.calls) == 1


def test_keyed_limit_above_row_budget_accepts_terminal_bounded_response() -> None:
    transport = _Transport(
        [
            _response(
                request_id="keyed-terminal",
                rows=[{"provider_value": 1, "provider_label": "identity"}],
            )
        ]
    )

    run = pagination.collect_query_pages(
        client=_client(transport),
        request=_request(),
        identity_fields=("provider_value",),
        max_pages=1,
        max_rows=1,
    )

    run.verify_integrity(identity_fields=("provider_value",))
    assert run.row_count == 1
    assert run.envelope.next_cursor is None


def test_keyed_large_query_limit_still_enforces_runtime_row_budget() -> None:
    transport = _Transport(
        [
            _response(
                request_id="keyed-over-budget",
                rows=[{"provider_value": 1}, {"provider_value": 2}],
            )
        ]
    )

    with pytest.raises(
        pagination.PaginationContractError,
        match="^pagination_row_budget_exceeded$",
    ):
        pagination.collect_query_pages(
            client=_client(transport),
            request=_request(),
            identity_fields=("provider_value",),
            max_pages=1,
            max_rows=1,
        )


def test_keyed_collection_does_not_silently_become_identityless() -> None:
    with pytest.raises(
        pagination.PaginationContractError,
        match="^pagination_identity_fields_invalid$",
    ):
        pagination.collect_query_pages(
            client=_client(_Transport([])),
            request=_request(),
            identity_fields=(),
            max_pages=1,
            max_rows=1,
        )
