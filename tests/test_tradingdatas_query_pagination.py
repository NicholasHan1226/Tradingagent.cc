from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from shared.data.sharedsignals_v1 import (
    HTTPResponse,
    QueryRequest,
    SharedSignalsV1Client,
    SharedSignalsV1Config,
)
from shared.data.tradingdatas_pagination import (
    PaginationContractError,
    collect_query_pages,
)


CATALOG = "fixture-catalog-v2"
DATASET = "fixture.cn.equity.daily.v2"
AS_OF = "2026-07-22T15:30:00+08:00"


def _metadata() -> dict[str, Any]:
    return {
        "state": "ready",
        "degraded": False,
        "freshness": {"state": "fresh", "fresh": True},
        "quality": {"state": "valid", "valid": True},
        "lineage": {
            "state": "complete",
            "provider": "fixture",
            "transport_service": "fixture",
        },
        "receipt_id": "receipt-daily-20260722",
        "data_through": "2026-07-22T15:00:00+08:00",
        "observed_at": "2026-07-22T15:01:00+08:00",
        "reasons": [],
    }


def _response(
    *,
    request_id: str,
    rows: list[dict[str, Any]],
    next_cursor: str | None,
    metadata: dict[str, Any] | None = None,
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
            "metadata": copy.deepcopy(metadata or _metadata()),
        },
    )


class _Transport:
    def __init__(self, responses: list[HTTPResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> HTTPResponse:
        self.calls.append(copy.deepcopy(kwargs))
        if not self._responses:
            raise AssertionError("unexpected extra request")
        return self._responses.pop(0)


def _client(transport: _Transport) -> SharedSignalsV1Client:
    return SharedSignalsV1Client(
        SharedSignalsV1Config(
            base_url="https://tradingdatas.fixture.invalid",
            expected_catalog_version=CATALOG,
            dataset_ids=frozenset({DATASET}),
            access_policy_id="fixture-read",
            cache_ttl_seconds=0,
        ),
        transport=transport,
    )


def _request(*, limit: int = 2) -> QueryRequest:
    return QueryRequest(
        dataset_id=DATASET,
        schema_major=2,
        fields=("ts_code", "trade_date", "close"),
        filters={"trade_date": {"eq": "20260722"}},
        as_of=AS_OF,
        limit=limit,
    )


def test_collects_provider_native_rows_and_binds_one_envelope_identity() -> None:
    opaque_cursor = "opaque-cursor-must-not-leak"
    transport = _Transport(
        [
            _response(
                request_id="page-1",
                rows=[
                    {"ts_code": "000001.SZ", "trade_date": "20260722", "close": 10.0},
                    {"ts_code": "000002.SZ", "trade_date": "20260722", "close": 20.0},
                ],
                next_cursor=opaque_cursor,
            ),
            _response(
                request_id="page-2",
                rows=[
                    {"ts_code": "600000.SH", "trade_date": "20260722", "close": 30.0}
                ],
                next_cursor=None,
            ),
        ]
    )

    run = collect_query_pages(
        client=_client(transport),
        request=_request(),
        identity_fields=("ts_code", "trade_date"),
        max_pages=3,
        max_rows=5,
    )

    assert run.page_count == 2
    assert run.row_count == 3
    assert run.envelope.next_cursor is None
    assert [row["ts_code"] for row in run.envelope.data] == [
        "000001.SZ",
        "000002.SZ",
        "600000.SH",
    ]
    assert len(run.identity_sha256) == 64
    assert len(run.semantic_sha256) == 64
    assert len(run.pagination_trace_sha256) == 64
    assert [call["json_body"]["cursor"] for call in transport.calls] == [
        None,
        opaque_cursor,
    ]
    assert opaque_cursor not in repr(run)
    assert opaque_cursor not in json.dumps(run.to_receipt_payload(), sort_keys=True)


@pytest.mark.parametrize(
    ("responses", "max_pages", "max_rows", "reason"),
    [
        (
            [
                _response(request_id="p1", rows=[{"ts_code": "1", "trade_date": "20260722", "close": 1}], next_cursor="same"),
                _response(request_id="p2", rows=[{"ts_code": "2", "trade_date": "20260722", "close": 2}], next_cursor="same"),
            ],
            3,
            5,
            "pagination_cursor_cycle",
        ),
        (
            [_response(request_id="p1", rows=[{"ts_code": "1", "trade_date": "20260722", "close": 1}], next_cursor="more")],
            1,
            5,
            "pagination_page_budget_exceeded",
        ),
        (
            [
                _response(
                    request_id="p1",
                    rows=[
                        {
                            "ts_code": "1",
                            "trade_date": "20260722",
                            "close": 1,
                        }
                    ],
                    next_cursor="more",
                )
            ],
            2,
            1,
            "pagination_row_budget_exceeded",
        ),
    ],
)
def test_cursor_and_budget_failures_are_controlled_and_redacted(
    responses: list[HTTPResponse],
    max_pages: int,
    max_rows: int,
    reason: str,
) -> None:
    with pytest.raises(PaginationContractError, match=f"^{reason}$"):
        collect_query_pages(
            client=_client(_Transport(responses)),
            request=_request(limit=min(2, max_rows)),
            identity_fields=("ts_code", "trade_date"),
            max_pages=max_pages,
            max_rows=max_rows,
        )


def test_cross_page_metadata_drift_and_duplicate_identity_fail_closed() -> None:
    drifted = _metadata()
    drifted["receipt_id"] = "different-receipt"
    with pytest.raises(
        PaginationContractError,
        match="^pagination_envelope_identity_mismatch$",
    ):
        collect_query_pages(
            client=_client(
                _Transport(
                    [
                        _response(request_id="p1", rows=[{"ts_code": "1", "trade_date": "20260722", "close": 1}], next_cursor="more"),
                        _response(request_id="p2", rows=[{"ts_code": "2", "trade_date": "20260722", "close": 2}], next_cursor=None, metadata=drifted),
                    ]
                )
            ),
            request=_request(),
            identity_fields=("ts_code", "trade_date"),
            max_pages=3,
            max_rows=5,
        )

    with pytest.raises(
        PaginationContractError,
        match="^pagination_duplicate_row_identity$",
    ):
        collect_query_pages(
            client=_client(
                _Transport(
                    [
                        _response(
                            request_id="p1",
                            rows=[
                                {
                                    "ts_code": "1",
                                    "trade_date": "20260722",
                                    "close": 1,
                                }
                            ],
                            next_cursor="more",
                        ),
                        _response(
                            request_id="p2",
                            rows=[
                                {
                                    "ts_code": "1",
                                    "trade_date": "20260722",
                                    "close": 2,
                                }
                            ],
                            next_cursor=None,
                        ),
                    ]
                )
            ),
            request=_request(),
            identity_fields=("ts_code", "trade_date"),
            max_pages=3,
            max_rows=5,
        )


def test_empty_intermediate_page_is_followed_and_exact_terminal_budget_passes() -> None:
    opaque_cursor = "opaque-empty-page-cursor"
    transport = _Transport(
        [
            _response(request_id="empty", rows=[], next_cursor=opaque_cursor),
            _response(
                request_id="terminal",
                rows=[
                    {
                        "ts_code": "000001.SZ",
                        "trade_date": "20260722",
                        "close": 10.0,
                    }
                ],
                next_cursor=None,
            ),
        ]
    )

    run = collect_query_pages(
        client=_client(transport),
        request=_request(limit=1),
        identity_fields=("ts_code", "trade_date"),
        max_pages=2,
        max_rows=1,
    )

    assert run.page_count == 2
    assert run.row_count == 1
    assert len(transport.calls) == 2
    assert opaque_cursor not in repr(run)


def test_three_node_cursor_cycle_is_rejected_before_an_extra_request() -> None:
    transport = _Transport(
        [
            _response(request_id="p1", rows=[], next_cursor="cursor-a"),
            _response(request_id="p2", rows=[], next_cursor="cursor-b"),
            _response(request_id="p3", rows=[], next_cursor="cursor-a"),
        ]
    )

    with pytest.raises(PaginationContractError, match="^pagination_cursor_cycle$"):
        collect_query_pages(
            client=_client(transport),
            request=_request(limit=1),
            identity_fields=("ts_code", "trade_date"),
            max_pages=5,
            max_rows=5,
        )

    assert len(transport.calls) == 3


def test_request_ids_and_opaque_cursors_do_not_change_observation_semantics() -> None:
    def responses(prefix: str, cursor: str) -> list[HTTPResponse]:
        return [
            _response(
                request_id=f"{prefix}-1",
                rows=[
                    {
                        "ts_code": "000001.SZ",
                        "trade_date": "20260722",
                        "close": 10.0,
                    }
                ],
                next_cursor=cursor,
            ),
            _response(
                request_id=f"{prefix}-2",
                rows=[
                    {
                        "ts_code": "600000.SH",
                        "trade_date": "20260722",
                        "close": 20.0,
                    }
                ],
                next_cursor=None,
            ),
        ]

    first = collect_query_pages(
        client=_client(_Transport(responses("first", "opaque-cursor-first"))),
        request=_request(limit=1),
        identity_fields=("ts_code", "trade_date"),
        max_pages=2,
        max_rows=2,
    )
    second = collect_query_pages(
        client=_client(_Transport(responses("second", "opaque-cursor-second"))),
        request=_request(limit=1),
        identity_fields=("ts_code", "trade_date"),
        max_pages=2,
        max_rows=2,
    )

    assert first.semantic_trace_sha256 == second.semantic_trace_sha256
    assert first.pagination_trace_sha256 != second.pagination_trace_sha256


def test_only_cursor_changes_across_requests_and_page_limit_is_enforced() -> None:
    transport = _Transport(
        [
            _response(request_id="p1", rows=[], next_cursor="next"),
            _response(request_id="p2", rows=[], next_cursor=None),
        ]
    )
    collect_query_pages(
        client=_client(transport),
        request=_request(limit=1),
        identity_fields=("ts_code", "trade_date"),
        max_pages=2,
        max_rows=2,
    )
    first_payload = dict(transport.calls[0]["json_body"])
    second_payload = dict(transport.calls[1]["json_body"])
    assert first_payload.pop("cursor") is None
    assert second_payload.pop("cursor") == "next"
    assert first_payload == second_payload

    with pytest.raises(
        PaginationContractError,
        match="^pagination_page_limit_exceeded$",
    ):
        collect_query_pages(
            client=_client(
                _Transport(
                    [
                        _response(
                            request_id="too-many",
                            rows=[
                                {
                                    "ts_code": "1",
                                    "trade_date": "20260722",
                                    "close": 1,
                                },
                                {
                                    "ts_code": "2",
                                    "trade_date": "20260722",
                                    "close": 2,
                                },
                            ],
                            next_cursor=None,
                        )
                    ]
                )
            ),
            request=_request(limit=1),
            identity_fields=("ts_code", "trade_date"),
            max_pages=2,
            max_rows=2,
        )
