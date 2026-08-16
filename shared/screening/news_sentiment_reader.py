#!/usr/bin/env python3
"""Objective cross-source news evidence reader for the six-dimension sentiment slot.

The reader wraps an already-configured :class:`SharedSignalsV1Client` and pulls
the most recent rows from two active news datasets within a bounded lookback
window:

* ``cn.dataset.news`` — Tushare Sina flash news (``datetime/title/content/channels``);
* ``cn.news.flash`` — Firecrawl Cailianshe/Eastmoney flash news
  (``published_at/published_local/event_date/title/summary/...``).

Both sources report the same underlying events, so the reader normalises each
row's title and deduplicates across sources **before** counting.  The rule is
objective and explainable: two titles are the same event when they are equal
after ``str.strip``, after removing all whitespace/punctuation, and after
case-folding.  No semantic similarity is performed — different titles remain
two distinct events.

It performs only objective, explainable association and counting:

* exact stock-code tokens (bare code / full ``ts_code`` / exchange-prefixed
  variants) appearing in ``title`` plus the source's body text
  (``content`` for Sina, ``summary`` for Firecrawl);
* an optional literal stock name (e.g. ``华测检测``) appearing in the same
  text fields;
* ``channels`` classification counts over every deduplicated event.

It deliberately performs **no** sentiment judgement, no model inference, and no
direction/confidence scoring.  Provider rows remain objective facts; this
module only turns them into countable evidence the six-dimension scorer can
attach to the ``sentiment`` slot.

Failure is fail-closed: a client/transport/contract error on either source
degrades to the other source instead of raising; if every source fails the
reader returns ``no_evidence``, so a TradingDatas outage can never break the
six-dimension scoring pipeline.

The query requests the most recent ``max_rows`` rows ordered by each source's
native timestamp descending and then re-filters them by the configured lookback
window in Python.  ``cn.dataset.news`` uses the provider-native
``YYYY-MM-DD HH:MM:SS`` ``datetime``; ``cn.news.flash`` uses ``published_at``
(ISO-8601) with ``published_local``/``event_date`` as fallbacks.  Rows whose
timestamp cannot be parsed are skipped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import re
from typing import Any, Mapping

from shared.data.sharedsignals_v1 import (
    QueryRequest,
    SharedSignalsV1Client,
    SharedSignalsV1Config,
)
from shared.data.tradingdatas_transport import build_runtime_transport

NEWS_DATASET_ID = "cn.dataset.news"
FLASH_DATASET_ID = "cn.news.flash"
_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"
_FLASH_DATE_FORMATS = ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d")
_DEFAULT_LOOKBACK_MINUTES = 1440  # one day; configurable by the runtime caller
_DEFAULT_MAX_ROWS = 1000

_STATUS_EVIDENCE = "evidence"
_STATUS_NO_EVIDENCE = "no_evidence"

_CHANNEL_SPLIT_RE = re.compile(r"[,，;；|/]+")
_TITLE_NORMALIZE_RE = re.compile(r"[\W_]+")

_SHANGHAI_OFFSET = timezone(timedelta(hours=8))


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


def _title_key(title: Any) -> str | None:
    """Normalise a title for cross-source deduplication.

    Returns ``None`` when the title is blank, signalling that the row has no
    reliable event identity and must not be merged with any other row.
    """
    text = str(title or "").strip()
    if not text:
        return None
    return _TITLE_NORMALIZE_RE.sub("", text).casefold()


def _split_channels(value: Any) -> tuple[str, ...]:
    text = str(value or "").strip()
    if not text:
        return ()
    return tuple(part for part in _CHANNEL_SPLIT_RE.split(text) if part)


def _to_shanghai_naive(dt: datetime) -> datetime:
    """Convert an aware instant to naive Asia/Shanghai wall-clock time."""
    if dt.tzinfo is not None and dt.utcoffset() is not None:
        return dt.astimezone(_SHANGHAI_OFFSET).replace(tzinfo=None)
    return dt


def _parse_news_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, _DATETIME_FORMAT)
    except ValueError:
        return None


def _parse_iso_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text
    if normalized.endswith(("Z", "z")):
        normalized = normalized[:-1] + "+00:00"
    try:
        return _to_shanghai_naive(datetime.fromisoformat(normalized))
    except ValueError:
        return None


def _parse_flash_local(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, _DATETIME_FORMAT)
    except ValueError:
        return _parse_iso_datetime(text)


def _parse_flash_event_date(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in _FLASH_DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _flash_row_datetime(row: Mapping[str, Any]) -> datetime | None:
    for field, parser in (
        ("published_at", _parse_iso_datetime),
        ("published_local", _parse_flash_local),
        ("event_date", _parse_flash_event_date),
    ):
        dt = parser(row.get(field))
        if dt is not None:
            return dt
    return None


@dataclass(frozen=True)
class _NewsItem:
    """A provider row normalised to the shared window/dedup vocabulary."""

    dataset_id: str
    dt: datetime
    title: str
    text: str
    channels: tuple[str, ...]


def _normalize_news_row(row: Any, dataset_id: str) -> _NewsItem | None:
    if not isinstance(row, Mapping):
        return None
    dt = _parse_news_datetime(row.get("datetime"))
    if dt is None:
        return None
    title = str(row.get("title") or "").strip()
    text = f"{title} {str(row.get('content') or '')}"
    return _NewsItem(
        dataset_id=dataset_id,
        dt=dt,
        title=title,
        text=text,
        channels=_split_channels(row.get("channels")),
    )


def _normalize_flash_row(row: Any, dataset_id: str) -> _NewsItem | None:
    if not isinstance(row, Mapping):
        return None
    dt = _flash_row_datetime(row)
    if dt is None:
        return None
    title = str(row.get("title") or "").strip()
    text = f"{title} {str(row.get('summary') or '')}"
    return _NewsItem(
        dataset_id=dataset_id,
        dt=dt,
        title=title,
        text=text,
        channels=(),
    )


def _no_evidence(reason: str, window_start: str, window_end: str) -> "NewsSentimentEvidence":
    return NewsSentimentEvidence(
        status=_STATUS_NO_EVIDENCE,
        reason=reason,
        window_start=window_start,
        window_end=window_end,
        total_rows=0,
        code_matches=0,
        name_matches=0,
        stock_matches=0,
        channel_counts={},
        unique_events=0,
        raw_rows=0,
    )


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
    unique_events: int = 0
    raw_rows: int = 0

    @property
    def has_evidence(self) -> bool:
        return self.status == _STATUS_EVIDENCE


class NewsSentimentReader:
    """Read objective cross-source news-row counts for one stock through a V1 client."""

    def __init__(
        self,
        client: SharedSignalsV1Client,
        *,
        schema_major: int,
        dataset_id: str = NEWS_DATASET_ID,
        flash_dataset_id: str = FLASH_DATASET_ID,
        lookback_minutes: int = _DEFAULT_LOOKBACK_MINUTES,
        max_rows: int = _DEFAULT_MAX_ROWS,
        flash_max_rows: int | None = None,
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
        if flash_max_rows is not None and (
            isinstance(flash_max_rows, bool)
            or not isinstance(flash_max_rows, int)
            or flash_max_rows <= 0
        ):
            raise ValueError("flash_max_rows must be a positive integer or None")
        if not isinstance(dataset_id, str) or not dataset_id.strip():
            raise ValueError("dataset_id must be a non-empty string")
        if not isinstance(flash_dataset_id, str) or not flash_dataset_id.strip():
            raise ValueError("flash_dataset_id must be a non-empty string")

        self._client = client
        self._schema_major = schema_major
        self._dataset_id = dataset_id
        self._flash_dataset_id = flash_dataset_id
        self._lookback_minutes = lookback_minutes
        self._max_rows = max_rows
        self._flash_max_rows = max_rows if flash_max_rows is None else flash_max_rows

    @classmethod
    def from_runtime(
        cls,
        *,
        base_url: str,
        token_file: Any,
        expected_catalog_version: str,
        schema_major: int,
        dataset_id: str = NEWS_DATASET_ID,
        flash_dataset_id: str = FLASH_DATASET_ID,
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
                dataset_ids=frozenset({dataset_id, flash_dataset_id}),
                access_policy_id=access_policy_id,
                catalog_version_policy="evidence_only",
                timeout_seconds=float(timeout_seconds),
                max_limit=int(max_limit),
                cache_ttl_seconds=float(cache_ttl_seconds),
            ),
            transport=transport,
        )
        catalog = client.get_catalog()

        def _page_size(did: str) -> int | None:
            for item in getattr(catalog, "data", ()) or ():
                if isinstance(item, Mapping) and item.get("dataset_id") == did:
                    limits = item.get("limits")
                    if isinstance(limits, Mapping):
                        declared = limits.get("max_page_size")
                        if type(declared) is int and declared > 0:
                            return declared
                    break
            return None

        # Clamp each source's row request to its own declared page limit so a
        # generous default never produces a 413 payload-too-large response.
        news_max = min(int(max_rows), _page_size(dataset_id) or int(max_rows))
        flash_max = min(int(max_rows), _page_size(flash_dataset_id) or int(max_rows))
        return cls(
            client,
            schema_major=schema_major,
            dataset_id=dataset_id,
            flash_dataset_id=flash_dataset_id,
            lookback_minutes=lookback_minutes,
            max_rows=news_max,
            flash_max_rows=flash_max,
        )

    def _max_rows_for(self, dataset_id: str) -> int:
        return self._max_rows if dataset_id == self._dataset_id else self._flash_max_rows

    def _order_for(self, dataset_id: str) -> tuple[str, ...]:
        if dataset_id == self._dataset_id:
            return ("datetime:desc",)
        return ("published_at:desc",)

    def _query_rows(self, dataset_id: str) -> list[Any] | None:
        """Return one dataset's raw rows, or ``None`` when the query fails."""
        try:
            envelope = self._client.query(
                QueryRequest(
                    dataset_id=dataset_id,
                    schema_major=self._schema_major,
                    order=self._order_for(dataset_id),
                    limit=self._max_rows_for(dataset_id),
                )
            )
        except Exception:
            return None
        data = getattr(envelope, "data", None)
        return list(data) if data else []

    def read_evidence(
        self,
        ts_code: str,
        date: str,
        *,
        name: str | None = None,
    ) -> NewsSentimentEvidence:
        """Count deduplicated news rows for ``ts_code`` in the configured window.

        ``date`` is ``YYYYMMDD``.  The decision time is the end of that day and
        the window extends ``lookback_minutes`` backwards from it.  Both news
        sources are queried; one source failing degrades to the other, and only
        when every source fails does the reader return ``no_evidence``.
        """
        try:
            decision_time = datetime.strptime(str(date or ""), "%Y%m%d").replace(
                hour=23, minute=59, second=59, microsecond=0
            )
        except (TypeError, ValueError):
            return _no_evidence("news_invalid_date", "", "")

        window_start_dt = decision_time - timedelta(minutes=self._lookback_minutes)
        window_start = window_start_dt.strftime(_DATETIME_FORMAT)
        window_end = decision_time.strftime(_DATETIME_FORMAT)

        query_flash = self._flash_dataset_id != self._dataset_id
        news_rows = self._query_rows(self._dataset_id)
        flash_rows = self._query_rows(self._flash_dataset_id) if query_flash else []

        news_failed = news_rows is None
        flash_failed = query_flash and flash_rows is None
        if news_failed and (not query_flash or flash_failed):
            return _no_evidence("news_client_error", window_start, window_end)

        news_rows = news_rows if news_rows is not None else []
        flash_rows = flash_rows if flash_rows is not None else []

        # ``had_any_rows`` distinguishes an empty provider page from a page that
        # has rows but none falling inside the lookback window.
        had_any_rows = bool(news_rows) or bool(flash_rows)

        all_items: list[_NewsItem] = []
        for row in news_rows:
            item = _normalize_news_row(row, self._dataset_id)
            if item is not None and window_start_dt <= item.dt <= decision_time:
                all_items.append(item)
        if query_flash:
            for row in flash_rows:
                item = _normalize_flash_row(row, self._flash_dataset_id)
                if item is not None and window_start_dt <= item.dt <= decision_time:
                    all_items.append(item)

        if not all_items:
            reason = "news_no_rows" if not had_any_rows else "news_window_empty"
            return _no_evidence(reason, window_start, window_end)

        # Group by normalised title; blank titles get a unique key so unrelated
        # rows without a title are never merged.
        groups: dict[object, list[_NewsItem]] = {}
        for index, item in enumerate(all_items):
            key = _title_key(item.title)
            if key is None:
                key = ("__blank_title__", index)
            groups.setdefault(key, []).append(item)

        raw_rows = len(all_items)
        unique_events = len(groups)

        code_variants = _code_variants(ts_code)
        name_text = str(name or "").strip()
        channel_counts: dict[str, int] = {}
        code_matches = 0
        name_matches = 0
        stock_matches = 0

        for group in groups.values():
            code_hit = any(
                variant in item.text for item in group for variant in code_variants
            )
            name_hit = bool(name_text) and any(name_text in item.text for item in group)
            if code_hit:
                code_matches += 1
            if name_hit:
                name_matches += 1
            if code_hit or name_hit:
                stock_matches += 1
            group_channels: set[str] = set()
            for item in group:
                group_channels.update(item.channels)
            for channel in group_channels:
                channel_counts[channel] = channel_counts.get(channel, 0) + 1

        degraded_sources = []
        if news_failed:
            degraded_sources.append(self._dataset_id)
        if flash_failed:
            degraded_sources.append(self._flash_dataset_id)

        reason = (
            f"code_matches={code_matches}, name_matches={name_matches}, "
            f"total_rows={unique_events}, unique_events={unique_events}, "
            f"raw_rows={raw_rows}"
        )
        if degraded_sources:
            reason += f", degraded_sources={','.join(degraded_sources)}"

        return NewsSentimentEvidence(
            status=_STATUS_EVIDENCE,
            reason=reason,
            window_start=window_start,
            window_end=window_end,
            total_rows=unique_events,
            code_matches=code_matches,
            name_matches=name_matches,
            stock_matches=stock_matches,
            channel_counts=channel_counts,
            unique_events=unique_events,
            raw_rows=raw_rows,
        )


__all__ = [
    "NEWS_DATASET_ID",
    "FLASH_DATASET_ID",
    "NewsSentimentEvidence",
    "NewsSentimentReader",
]
