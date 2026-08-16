#!/usr/bin/env python3
"""Objective ``cn.dataset.news`` evidence reader for the six-dimension sentiment slot.

The reader wraps an already-configured :class:`SharedSignalsV1Client` and pulls
the most recent ``cn.dataset.news`` flash-news rows within a bounded lookback
window.  It performs only objective, explainable association and counting:

* exact stock-code tokens (bare code / full ``ts_code`` / exchange-prefixed
  variants) appearing in ``title`` or ``content``;
* an optional literal stock name (e.g. ``华测检测``) appearing in the same
  text fields;
* ``channels`` classification counts over every row in the window.

It deliberately performs **no** sentiment judgement, no model inference, and no
direction/confidence scoring.  Provider rows remain objective facts; this
module only turns them into countable evidence the six-dimension scorer can
attach to the ``sentiment`` slot.

Failure is fail-closed: any client/transport/contract error yields a
``no_evidence`` result instead of raising, so a TradingDatas outage can never
break the six-dimension scoring pipeline.

The query requests the most recent ``max_rows`` rows ordered by ``datetime``
descending and then re-filters them by the configured lookback window in
Python.  ``datetime`` uses the provider-native ``YYYY-MM-DD HH:MM:SS`` text
format; rows whose timestamp cannot be parsed are skipped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import re
from typing import Any, Mapping

from shared.data.sharedsignals_v1 import (
    QueryRequest,
    SharedSignalsV1Client,
    SharedSignalsV1Config,
)
from shared.data.tradingdatas_transport import build_runtime_transport

NEWS_DATASET_ID = "cn.dataset.news"
_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"
_DEFAULT_LOOKBACK_MINUTES = 1440  # one day; configurable by the runtime caller
_DEFAULT_MAX_ROWS = 1000

_STATUS_EVIDENCE = "evidence"
_STATUS_NO_EVIDENCE = "no_evidence"

_CHANNEL_SPLIT_RE = re.compile(r"[,，;；|/]+")


def _code_variants(ts_code: str) -> tuple[str, ...]:
    """Return the exact code tokens that may appear in free-form news text."""
    code = str(ts_code or "").strip()
    if not code:
        return ()
    variants = {code}
    if "." in code:
        head, tail = code.split(".", 1)
        if head:
            variants.add(head)
        if tail and head:
            variants.add(f"{tail}{head}")
    if len(code) == 8 and code[:2] in {"SH", "SZ"} and code[2:].isdigit():
        variants.add(code[2:])
        variants.add(f"{code[2:]}.{code[:2]}")
    return tuple(sorted(variants, key=len, reverse=True))


def _haystack(row: Mapping[str, Any]) -> str:
    return f"{str(row.get('title') or '')} {str(row.get('content') or '')}"


def _split_channels(value: Any) -> tuple[str, ...]:
    text = str(value or "").strip()
    if not text:
        return ()
    return tuple(part for part in _CHANNEL_SPLIT_RE.split(text) if part)


@dataclass(frozen=True)
class NewsSentimentEvidence:
    """Objective news-count evidence; never a direction or confidence."""

    status: str
    reason: str
    window_start: str
    window_end: str
    total_rows: int
    code_matches: int
    name_matches: int
    stock_matches: int
    channel_counts: Mapping[str, int] = field(default_factory=dict)

    @property
    def has_evidence(self) -> bool:
        return self.status == _STATUS_EVIDENCE


class NewsSentimentReader:
    """Read objective news-row counts for one stock through a V1 client."""

    def __init__(
        self,
        client: SharedSignalsV1Client,
        *,
        schema_major: int,
        dataset_id: str = NEWS_DATASET_ID,
        lookback_minutes: int = _DEFAULT_LOOKBACK_MINUTES,
        max_rows: int = _DEFAULT_MAX_ROWS,
    ) -> None:
        if not callable(getattr(client, "query", None)):
            raise ValueError("client must expose a query(request) method")
        if (
            isinstance(schema_major, bool)
            or not isinstance(schema_major, int)
            or schema_major <= 0
        ):
            raise ValueError("schema_major must be a positive integer")
        if (
            isinstance(lookback_minutes, bool)
            or not isinstance(lookback_minutes, int)
            or lookback_minutes <= 0
        ):
            raise ValueError("lookback_minutes must be a positive integer")
        if (
            isinstance(max_rows, bool)
            or not isinstance(max_rows, int)
            or max_rows <= 0
        ):
            raise ValueError("max_rows must be a positive integer")
        if not isinstance(dataset_id, str) or not dataset_id.strip():
            raise ValueError("dataset_id must be a non-empty string")

        self._client = client
        self._schema_major = schema_major
        self._dataset_id = dataset_id
        self._lookback_minutes = lookback_minutes
        self._max_rows = max_rows

    @classmethod
    def from_runtime(
        cls,
        *,
        base_url: str,
        token_file: Any,
        expected_catalog_version: str,
        schema_major: int,
        dataset_id: str = NEWS_DATASET_ID,
        access_policy_id: str,
        transport_id: str = "http-json-v1",
        transport_factory: Any = build_runtime_transport,
        timeout_seconds: float = 10.0,
        max_limit: int = 2000,
        cache_ttl_seconds: float = 0.0,
        lookback_minutes: int = _DEFAULT_LOOKBACK_MINUTES,
        max_rows: int = _DEFAULT_MAX_ROWS,
    ) -> "NewsSentimentReader":
        """Build a reader from the explicit runtime contract.

        ``evidence_only`` requires a successful catalog read before any query,
        so the catalog is observed here once before the reader is returned.
        No catalog version, dataset id, base URL, token path or policy is
        invented; every authority input is explicit.
        """
        transport = transport_factory(
            transport_id,
            token_file=token_file,
            base_url=base_url,
        )
        client = SharedSignalsV1Client(
            SharedSignalsV1Config(
                base_url=base_url,
                expected_catalog_version=expected_catalog_version,
                dataset_ids=frozenset({dataset_id}),
                access_policy_id=access_policy_id,
                catalog_version_policy="evidence_only",
                timeout_seconds=float(timeout_seconds),
                max_limit=int(max_limit),
                cache_ttl_seconds=float(cache_ttl_seconds),
            ),
            transport=transport,
        )
        catalog = client.get_catalog()
        # Clamp the row request to the dataset's declared page limit so a
        # generous default never produces a 413 payload-too-large response.
        page_size: int | None = None
        for item in getattr(catalog, "data", ()) or ():
            if isinstance(item, Mapping) and item.get("dataset_id") == dataset_id:
                limits = item.get("limits")
                if isinstance(limits, Mapping):
                    declared = limits.get("max_page_size")
                    if type(declared) is int and declared > 0:
                        page_size = declared
                break
        if page_size is not None:
            max_rows = min(int(max_rows), page_size)
        return cls(
            client,
            schema_major=schema_major,
            dataset_id=dataset_id,
            lookback_minutes=lookback_minutes,
            max_rows=max_rows,
        )

    def read_evidence(
        self,
        ts_code: str,
        date: str,
        *,
        name: str | None = None,
    ) -> NewsSentimentEvidence:
        """Count objective news rows for ``ts_code`` in the configured window.

        ``date`` is ``YYYYMMDD``.  The decision time is the end of that day and
        the window extends ``lookback_minutes`` backwards from it.  Returns
        ``no_evidence`` instead of raising on any client, transport, contract
        or date-format failure.
        """
        try:
            decision_time = datetime.strptime(str(date or ""), "%Y%m%d").replace(
                hour=23, minute=59, second=59, microsecond=0
            )
        except (TypeError, ValueError):
            return NewsSentimentEvidence(
                status=_STATUS_NO_EVIDENCE,
                reason="news_invalid_date",
                window_start="",
                window_end="",
                total_rows=0,
                code_matches=0,
                name_matches=0,
                stock_matches=0,
                channel_counts={},
            )

        window_start_dt = decision_time - timedelta(minutes=self._lookback_minutes)
        window_start = window_start_dt.strftime(_DATETIME_FORMAT)
        window_end = decision_time.strftime(_DATETIME_FORMAT)

        try:
            envelope = self._client.query(
                QueryRequest(
                    dataset_id=self._dataset_id,
                    schema_major=self._schema_major,
                    order=("datetime:desc",),
                    limit=self._max_rows,
                )
            )
        except Exception:
            return NewsSentimentEvidence(
                status=_STATUS_NO_EVIDENCE,
                reason="news_client_error",
                window_start=window_start,
                window_end=window_end,
                total_rows=0,
                code_matches=0,
                name_matches=0,
                stock_matches=0,
                channel_counts={},
            )

        rows = getattr(envelope, "data", None)
        if not rows:
            return NewsSentimentEvidence(
                status=_STATUS_NO_EVIDENCE,
                reason="news_no_rows",
                window_start=window_start,
                window_end=window_end,
                total_rows=0,
                code_matches=0,
                name_matches=0,
                stock_matches=0,
                channel_counts={},
            )

        code_variants = _code_variants(ts_code)
        name_text = str(name or "").strip()
        channel_counts: dict[str, int] = {}
        total_rows = 0
        code_matches = 0
        name_matches = 0
        matched_row_indexes: set[int] = set()

        for index, row in enumerate(rows):
            if not isinstance(row, Mapping):
                continue
            try:
                row_dt = datetime.strptime(
                    str(row.get("datetime") or ""), _DATETIME_FORMAT
                )
            except (TypeError, ValueError):
                continue
            if not (window_start_dt <= row_dt <= decision_time):
                continue

            total_rows += 1
            text = _haystack(row)
            code_hit = any(variant in text for variant in code_variants)
            name_hit = bool(name_text) and name_text in text
            if code_hit:
                code_matches += 1
            if name_hit:
                name_matches += 1
            if code_hit or name_hit:
                matched_row_indexes.add(index)

            for channel in _split_channels(row.get("channels")):
                channel_counts[channel] = channel_counts.get(channel, 0) + 1

        stock_matches = len(matched_row_indexes)
        if total_rows == 0:
            return NewsSentimentEvidence(
                status=_STATUS_NO_EVIDENCE,
                reason="news_window_empty",
                window_start=window_start,
                window_end=window_end,
                total_rows=0,
                code_matches=0,
                name_matches=0,
                stock_matches=0,
                channel_counts=channel_counts,
            )

        reason = (
            f"code_matches={code_matches}, "
            f"name_matches={name_matches}, total_rows={total_rows}"
        )
        return NewsSentimentEvidence(
            status=_STATUS_EVIDENCE,
            reason=reason,
            window_start=window_start,
            window_end=window_end,
            total_rows=total_rows,
            code_matches=code_matches,
            name_matches=name_matches,
            stock_matches=stock_matches,
            channel_counts=channel_counts,
        )


__all__ = [
    "NEWS_DATASET_ID",
    "NewsSentimentEvidence",
    "NewsSentimentReader",
]
