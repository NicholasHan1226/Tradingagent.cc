#!/usr/bin/env python3
"""Bounded, fail-closed pagination for the TradingDatas V1 query contract.

The server owns row shape, ordering and opaque cursor semantics.  TradingAgent
only follows the returned cursor while preserving one exact envelope identity,
enforcing explicit page/row budgets and proving row identity across pages.  It
never logs or persists raw cursors and has no alternate data path.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from typing import Any, Mapping

from .sharedsignals_v1 import (
    QueryEnvelope,
    QueryRequest,
    SharedSignalsV1Client,
    SharedSignalsV1Error,
)


HARD_MAX_PAGES = 1_000
HARD_MAX_ROWS = 5_000_000


class PaginationContractError(SharedSignalsV1Error):
    """A controlled pagination failure safe to expose as a reason code."""


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise PaginationContractError("pagination_noncanonical_value") from exc


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _metadata_identity(envelope: QueryEnvelope) -> dict[str, Any]:
    return {
        "api_version": envelope.api_version,
        "catalog_version": envelope.catalog_version,
        "dataset_id": envelope.dataset_id,
        "metadata": {
            "state": envelope.metadata.state,
            "degraded": envelope.metadata.degraded,
            "freshness": envelope.metadata.freshness,
            "quality": envelope.metadata.quality,
            "lineage": envelope.metadata.lineage,
            "receipt_id": envelope.metadata.receipt_id,
            "data_through": envelope.metadata.data_through,
            "observed_at": envelope.metadata.observed_at,
            "reasons": list(envelope.metadata.reasons),
        },
    }


def _normalize_identity_fields(values: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(values, tuple) or not values:
        raise PaginationContractError("pagination_identity_fields_invalid")
    normalized: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            raise PaginationContractError("pagination_identity_fields_invalid")
        if value in normalized:
            raise PaginationContractError("pagination_identity_fields_invalid")
        normalized.append(value)
    return tuple(normalized)


@dataclass(frozen=True)
class PagedQueryRun:
    """One complete bounded read with only hashed cursor/page trace evidence."""

    envelope: QueryEnvelope
    page_count: int
    row_count: int
    identity_sha256: str
    ordered_rows_sha256: str
    metadata_sha256: str
    semantic_sha256: str
    semantic_trace_sha256: str
    pagination_trace_sha256: str
    page_request_sha256s: tuple[str, ...]
    page_response_sha256s: tuple[str, ...]
    cursor_chain_sha256: str
    _request_ids_json: str = field(repr=False)

    @property
    def request_ids(self) -> tuple[str, ...]:
        decoded = json.loads(self._request_ids_json)
        return tuple(decoded)

    @property
    def dataset_id(self) -> str:
        return self.envelope.dataset_id

    def to_receipt_payload(self) -> dict[str, Any]:
        return {
            "page_count": self.page_count,
            "row_count": self.row_count,
            "identity_sha256": self.identity_sha256,
            "ordered_rows_sha256": self.ordered_rows_sha256,
            "metadata_sha256": self.metadata_sha256,
            "semantic_sha256": self.semantic_sha256,
            "semantic_trace_sha256": self.semantic_trace_sha256,
            "pagination_trace_sha256": self.pagination_trace_sha256,
            "page_request_set_sha256": _sha256(list(self.page_request_sha256s)),
            "page_response_set_sha256": _sha256(list(self.page_response_sha256s)),
            "cursor_chain_sha256": self.cursor_chain_sha256,
        }

    def verify_integrity(self, *, identity_fields: tuple[str, ...]) -> None:
        """Recompute every derivable trace before downstream acceptance.

        Raw cursors are intentionally discarded, so their aggregate hash cannot
        be independently expanded.  Cursor-bearing request hashes, per-page
        response hashes, request IDs and all semantic aggregates remain bound.
        """

        identity_names = _normalize_identity_fields(identity_fields)
        if self.envelope.next_cursor is not None:
            raise PaginationContractError("pagination_incomplete")
        if (
            isinstance(self.page_count, bool)
            or not isinstance(self.page_count, int)
            or self.page_count <= 0
            or isinstance(self.row_count, bool)
            or not isinstance(self.row_count, int)
            or self.row_count < 0
        ):
            raise PaginationContractError("pagination_trace_invalid")
        request_ids = self.request_ids
        if not (
            len(self.page_request_sha256s)
            == len(self.page_response_sha256s)
            == len(request_ids)
            == self.page_count
        ):
            raise PaginationContractError("pagination_trace_invalid")
        hashes = (
            self.identity_sha256,
            self.ordered_rows_sha256,
            self.metadata_sha256,
            self.semantic_sha256,
            self.semantic_trace_sha256,
            self.pagination_trace_sha256,
            self.cursor_chain_sha256,
            *self.page_request_sha256s,
            *self.page_response_sha256s,
        )
        if any(
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
            for value in hashes
        ):
            raise PaginationContractError("pagination_trace_invalid")
        if any(
            not isinstance(value, str) or not value or value != value.strip()
            for value in request_ids
        ):
            raise PaginationContractError("pagination_trace_invalid")

        rows = list(self.envelope.data)
        if len(rows) != self.row_count:
            raise PaginationContractError("pagination_trace_invalid")
        identities: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows:
            identity: dict[str, Any] = {}
            for field_name in identity_names:
                if field_name not in row or row[field_name] is None:
                    raise PaginationContractError("pagination_row_identity_missing")
                identity[field_name] = row[field_name]
            encoded = _canonical_json(identity)
            if encoded in seen:
                raise PaginationContractError("pagination_duplicate_row_identity")
            seen.add(encoded)
            identities.append(identity)

        metadata_sha256 = _sha256(_metadata_identity(self.envelope))
        identity_sha256 = _sha256(identities)
        ordered_rows_sha256 = _sha256(rows)
        semantic_payload = {
            "base_query_sha256": self.page_request_sha256s[0],
            "metadata_sha256": metadata_sha256,
            "identity_sha256": identity_sha256,
            "ordered_rows_sha256": ordered_rows_sha256,
            "page_count": self.page_count,
            "row_count": self.row_count,
        }
        semantic_sha256 = _sha256(semantic_payload)
        semantic_trace_sha256 = _sha256(
            {
                **semantic_payload,
                "page_response_sha256s": list(self.page_response_sha256s),
            }
        )
        pagination_trace_sha256 = _sha256(
            {
                "semantic_trace_sha256": semantic_trace_sha256,
                "page_request_sha256s": list(self.page_request_sha256s),
                "request_ids_sha256": _sha256(list(request_ids)),
                "cursor_chain_sha256": self.cursor_chain_sha256,
            }
        )
        if (
            self.identity_sha256 != identity_sha256
            or self.ordered_rows_sha256 != ordered_rows_sha256
            or self.metadata_sha256 != metadata_sha256
            or self.semantic_sha256 != semantic_sha256
            or self.semantic_trace_sha256 != semantic_trace_sha256
            or self.pagination_trace_sha256 != pagination_trace_sha256
            or self.envelope.request_id != f"ta-paged-{semantic_sha256[:24]}"
        ):
            raise PaginationContractError("pagination_trace_invalid")
        if self.page_count == 1 and self.cursor_chain_sha256 != _sha256([]):
            raise PaginationContractError("pagination_trace_invalid")


def collect_query_pages(
    *,
    client: SharedSignalsV1Client,
    request: QueryRequest,
    identity_fields: tuple[str, ...],
    max_pages: int,
    max_rows: int,
) -> PagedQueryRun:
    """Follow an opaque cursor within explicit limits and return one envelope.

    Cross-page metadata must be byte-semantically identical.  Row identity is
    dataset-specific and supplied by the consumer profile; duplicate identities
    are rejected instead of silently overwritten or locally reordered.
    """

    if not isinstance(client, SharedSignalsV1Client):
        raise TypeError("client must be SharedSignalsV1Client")
    if not isinstance(request, QueryRequest):
        raise TypeError("request must be QueryRequest")
    if request.cursor is not None:
        raise PaginationContractError("pagination_initial_cursor_forbidden")
    normalized_identity_fields = _normalize_identity_fields(identity_fields)
    for field_name, value in (("max_pages", max_pages), ("max_rows", max_rows)):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise PaginationContractError(f"pagination_{field_name}_invalid")
    if max_pages > HARD_MAX_PAGES or max_rows > HARD_MAX_ROWS:
        raise PaginationContractError("pagination_budget_above_hard_ceiling")
    if request.limit > max_rows:
        raise PaginationContractError("pagination_limit_exceeds_row_budget")

    rows: list[dict[str, Any]] = []
    row_identities: list[dict[str, Any]] = []
    seen_identities: set[str] = set()
    seen_request_cursors: set[str] = set()
    cursor_hashes: list[str] = []
    page_request_sha256s: list[str] = []
    page_response_sha256s: list[str] = []
    request_ids: list[str] = []
    first_envelope: QueryEnvelope | None = None
    metadata_json: str | None = None
    cursor: str | None = None
    page_count = 0

    while True:
        if page_count >= max_pages:
            raise PaginationContractError("pagination_page_budget_exceeded")
        if cursor is not None:
            if cursor in seen_request_cursors:
                raise PaginationContractError("pagination_cursor_cycle")
            seen_request_cursors.add(cursor)
            cursor_hashes.append(hashlib.sha256(cursor.encode("utf-8")).hexdigest())

        page_request = replace(request, cursor=cursor)
        envelope = client.query_uncached(page_request)
        page_count += 1
        if len(envelope.data) > request.limit:
            raise PaginationContractError("pagination_page_limit_exceeded")

        current_metadata_json = _canonical_json(_metadata_identity(envelope))
        if first_envelope is None:
            first_envelope = envelope
            metadata_json = current_metadata_json
        elif current_metadata_json != metadata_json:
            raise PaginationContractError("pagination_envelope_identity_mismatch")

        if len(rows) + len(envelope.data) > max_rows:
            raise PaginationContractError("pagination_row_budget_exceeded")

        for row in envelope.data:
            identity: dict[str, Any] = {}
            for field_name in normalized_identity_fields:
                if field_name not in row or row[field_name] is None:
                    raise PaginationContractError("pagination_row_identity_missing")
                identity[field_name] = row[field_name]
            identity_json = _canonical_json(identity)
            if identity_json in seen_identities:
                raise PaginationContractError("pagination_duplicate_row_identity")
            seen_identities.add(identity_json)
            row_identities.append(identity)
            rows.append(dict(row))

        page_request_sha256s.append(page_request.sha256)
        page_response_sha256s.append(
            _sha256(
                {
                    "data": list(envelope.data),
                    "metadata": _metadata_identity(envelope),
                    "has_next_page": envelope.next_cursor is not None,
                }
            )
        )
        request_ids.append(envelope.request_id)

        next_cursor = envelope.next_cursor
        if next_cursor is None:
            break
        if len(rows) >= max_rows:
            # A non-terminal response proves that another row *may* exist.  Once
            # the row budget is exhausted we must fail before issuing another
            # request; returning the accumulated prefix would be a silent
            # truncation and querying again would exceed the declared budget.
            raise PaginationContractError("pagination_row_budget_exceeded")
        if next_cursor == cursor or next_cursor in seen_request_cursors:
            raise PaginationContractError("pagination_cursor_cycle")
        cursor = next_cursor

    assert first_envelope is not None
    assert metadata_json is not None
    identity_sha256 = _sha256(row_identities)
    ordered_rows_sha256 = _sha256(rows)
    metadata_sha256 = hashlib.sha256(metadata_json.encode("utf-8")).hexdigest()
    semantic_payload = {
        "base_query_sha256": request.sha256,
        "metadata_sha256": metadata_sha256,
        "identity_sha256": identity_sha256,
        "ordered_rows_sha256": ordered_rows_sha256,
        "page_count": page_count,
        "row_count": len(rows),
    }
    semantic_sha256 = _sha256(semantic_payload)
    cursor_chain_sha256 = _sha256(cursor_hashes)
    semantic_trace_sha256 = _sha256(
        {
            **semantic_payload,
            "page_response_sha256s": page_response_sha256s,
        }
    )
    pagination_trace_sha256 = _sha256(
        {
            "semantic_trace_sha256": semantic_trace_sha256,
            "page_request_sha256s": page_request_sha256s,
            "request_ids_sha256": _sha256(request_ids),
            "cursor_chain_sha256": cursor_chain_sha256,
        }
    )
    combined = QueryEnvelope(
        api_version=first_envelope.api_version,
        catalog_version=first_envelope.catalog_version,
        request_id=f"ta-paged-{semantic_sha256[:24]}",
        dataset_id=first_envelope.dataset_id,
        data=tuple(rows),
        next_cursor=None,
        metadata=first_envelope.metadata,
    )
    return PagedQueryRun(
        envelope=combined,
        page_count=page_count,
        row_count=len(rows),
        identity_sha256=identity_sha256,
        ordered_rows_sha256=ordered_rows_sha256,
        metadata_sha256=metadata_sha256,
        semantic_sha256=semantic_sha256,
        semantic_trace_sha256=semantic_trace_sha256,
        pagination_trace_sha256=pagination_trace_sha256,
        page_request_sha256s=tuple(page_request_sha256s),
        page_response_sha256s=tuple(page_response_sha256s),
        cursor_chain_sha256=cursor_chain_sha256,
        _request_ids_json=_canonical_json(request_ids),
    )


def bind_complete_page(
    *,
    request: QueryRequest,
    envelope: QueryEnvelope,
    identity_fields: tuple[str, ...],
) -> PagedQueryRun:
    """Bind an already validated terminal page for offline fixtures/replays."""

    if not isinstance(request, QueryRequest) or not isinstance(envelope, QueryEnvelope):
        raise TypeError("request/envelope types are invalid")
    if request.cursor is not None or envelope.next_cursor is not None:
        raise PaginationContractError("pagination_incomplete")
    if request.dataset_id != envelope.dataset_id:
        raise PaginationContractError("pagination_envelope_identity_mismatch")
    identity_names = _normalize_identity_fields(identity_fields)
    identities: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in envelope.data:
        identity: dict[str, Any] = {}
        for field_name in identity_names:
            if field_name not in row or row[field_name] is None:
                raise PaginationContractError("pagination_row_identity_missing")
            identity[field_name] = row[field_name]
        encoded = _canonical_json(identity)
        if encoded in seen:
            raise PaginationContractError("pagination_duplicate_row_identity")
        seen.add(encoded)
        identities.append(identity)
    rows = list(envelope.data)
    identity_sha256 = _sha256(identities)
    rows_sha256 = _sha256(rows)
    metadata_sha256 = _sha256(_metadata_identity(envelope))
    page_response_sha = _sha256(
        {
            "data": rows,
            "metadata": _metadata_identity(envelope),
            "has_next_page": False,
        }
    )
    cursor_chain_sha = _sha256([])
    semantic_sha = _sha256(
        {
            "base_query_sha256": request.sha256,
            "metadata_sha256": metadata_sha256,
            "identity_sha256": identity_sha256,
            "ordered_rows_sha256": rows_sha256,
            "page_count": 1,
            "row_count": len(rows),
        }
    )
    semantic_trace_sha = _sha256(
        {
            "base_query_sha256": request.sha256,
            "metadata_sha256": metadata_sha256,
            "identity_sha256": identity_sha256,
            "ordered_rows_sha256": rows_sha256,
            "page_count": 1,
            "row_count": len(rows),
            "page_response_sha256s": [page_response_sha],
        }
    )
    trace_sha = _sha256(
        {
            "semantic_trace_sha256": semantic_trace_sha,
            "page_request_sha256s": [request.sha256],
            "request_ids_sha256": _sha256([envelope.request_id]),
            "cursor_chain_sha256": cursor_chain_sha,
        }
    )
    combined = QueryEnvelope(
        api_version=envelope.api_version,
        catalog_version=envelope.catalog_version,
        request_id=f"ta-paged-{semantic_sha[:24]}",
        dataset_id=envelope.dataset_id,
        data=envelope.data,
        next_cursor=None,
        metadata=envelope.metadata,
    )
    return PagedQueryRun(
        envelope=combined,
        page_count=1,
        row_count=len(rows),
        identity_sha256=identity_sha256,
        ordered_rows_sha256=rows_sha256,
        metadata_sha256=metadata_sha256,
        semantic_sha256=semantic_sha,
        semantic_trace_sha256=semantic_trace_sha,
        pagination_trace_sha256=trace_sha,
        page_request_sha256s=(request.sha256,),
        page_response_sha256s=(page_response_sha,),
        cursor_chain_sha256=cursor_chain_sha,
        _request_ids_json=_canonical_json([envelope.request_id]),
    )


__all__ = [
    "PagedQueryRun",
    "PaginationContractError",
    "bind_complete_page",
    "collect_query_pages",
]
