#!/usr/bin/env python3
"""HTTP client for SharedSignals API.

Mirrors the 15 canonical reader functions via HTTP instead of direct SQLite reads.
Provides fail-safe access: network errors return empty data rather than raising.

Usage:
    from shared.data.shared_signals_api import SharedSignalsAPIClient
    client = SharedSignalsAPIClient()
    rows = client.get_market_data(ts_code="000001.SZ")
"""

from __future__ import annotations

from copy import deepcopy
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any


DEFAULT_API_URL = os.environ.get("SHAREDSIGNALS_API_URL", "http://127.0.0.1:8082")
DEFAULT_API_KEY = os.environ.get("SHAREDSIGNALS_API_KEY", "")
DEFAULT_TIMEOUT = float(os.environ.get("SHAREDSIGNALS_API_TIMEOUT", "10"))
DEFAULT_RETRIES = int(
    os.environ.get(
        "SHAREDSIGNALS_API_RETRIES",
        os.environ.get("SHAREDSIGNALS_API_MAX_RETRIES", "1"),
    )
)
DEFAULT_RETRY_BACKOFF = float(os.environ.get("SHAREDSIGNALS_API_RETRY_BACKOFF", "0.5"))

CANONICAL_ENDPOINTS: dict[str, str] = {
    "is_trading_day": "/is_trading_day",
    "get_market_data": "/market_data",
    "get_fundamentals": "/fundamentals",
    "get_reference": "/reference",
    "get_macro_factors": "/macro",
    "get_capital_flow": "/capital_flow",
    "get_events": "/events",
    "get_sentiment": "/sentiment",
    "get_crypto_klines": "/crypto",
    "get_pm_markets": "/pm_markets",
    "get_pm_prices": "/pm_prices",
    "get_associations": "/associations",
    "get_impacts": "/impacts",
    "get_industry": "/industry",
    "get_realtime_5min": "/realtime_5min",
    "get_tushare": "/tushare",
}


class SharedSignalsAPIClient:
    """HTTP client for SharedSignals REST API.

    All methods return list[dict] or None on failure.
    Cache is shared across instances via class-level TTL cache.
    """

    _cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
    _cache_ttl: float = 30.0  # seconds
    _v1_page_limit: int = 500
    _v1_max_pages: int = 1000

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
        retry_backoff: float | None = None,
    ):
        self.base_url = (base_url or DEFAULT_API_URL).rstrip("/")
        self.api_key = api_key or DEFAULT_API_KEY
        self.timeout = timeout if timeout is not None else DEFAULT_TIMEOUT
        self.max_retries = max_retries if max_retries is not None else DEFAULT_RETRIES
        self.retry_backoff = (
            retry_backoff if retry_backoff is not None else DEFAULT_RETRY_BACKOFF
        )
        self.errors: list[str] = []

    @classmethod
    def _cache_key(cls, path: str, params: dict[str, str]) -> str:
        return f"{path}?{'&'.join(f'{k}={v}' for k, v in sorted(params.items()))}"

    @classmethod
    def _cache_get(cls, key: str) -> list[dict[str, Any]] | None:
        if key in cls._cache:
            ts, data = cls._cache[key]
            if time.monotonic() - ts < cls._cache_ttl:
                return data
            del cls._cache[key]
        return None

    @classmethod
    def _cache_set(cls, key: str, data: list[dict[str, Any]]) -> None:
        cls._cache[key] = (time.monotonic(), data)

    @staticmethod
    def _attach_http_response_receipt(
        rows: list[dict[str, Any]], *, path: str, received_at: str
    ) -> list[dict[str, Any]]:
        """Bind rows to the actual HTTP response receipt without overwriting source PIT."""

        enriched: list[dict[str, Any]] = []
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            row = dict(raw)
            row["sharedsignals_response_lineage"] = {
                "transport": "http_response",
                "endpoint": path,
                "received_at": received_at,
            }
            existing_envelope = row.get("evidence_envelope")
            if existing_envelope is None:
                envelope: dict[str, Any] = {}
            elif isinstance(existing_envelope, dict):
                envelope = {
                    key: dict(value) if isinstance(value, dict) else value
                    for key, value in existing_envelope.items()
                }
            else:
                # Preserve an invalid provider envelope so the downstream gate
                # rejects it.  The sibling transport lineage still audits this
                # concrete HTTP response without laundering provider evidence.
                enriched.append(row)
                continue
            retrieval = envelope.get("retrieval_time_fields")
            if retrieval is None:
                retrieval_fields: dict[str, Any] = {}
            elif isinstance(retrieval, dict):
                retrieval_fields = dict(retrieval)
            else:
                # As above, preserve the invalid provider group byte-for-byte
                # while retaining the independently observed transport audit.
                enriched.append(row)
                continue
            retrieval_fields["sharedsignals_http_response.received_at"] = received_at
            envelope["retrieval_time_fields"] = retrieval_fields
            row["evidence_envelope"] = envelope
            enriched.append(row)
        return enriched

    def _request_json(
        self,
        path: str,
        url: str,
        *,
        method: str,
        body: bytes | None = None,
    ) -> tuple[dict[str, Any], str] | None:
        """Execute one JSON HTTP request through the shared auth/retry transport."""
        if not self.base_url:
            self.errors.append(f"{path}: SHAREDSIGNALS_API_URL is not configured")
            return None

        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if body is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
            headers["Content-Length"] = str(len(body))

        last_error = ""
        for attempt in range(self.max_retries + 1):
            try:
                req = urllib.request.Request(
                    url,
                    data=body,
                    headers=headers,
                    method=method,
                )
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read().decode("utf-8", errors="replace")
                    received_at = datetime.now(timezone.utc).isoformat(
                        timespec="seconds"
                    )
                result = json.loads(raw)
                if not isinstance(result, dict):
                    last_error = "response is not a JSON object"
                    break
                return result, received_at
            except urllib.error.HTTPError as exc:
                last_error = f"HTTP {exc.code}: {exc.reason}"
                if exc.code < 500 and exc.code != 429:
                    break
                if attempt < self.max_retries:
                    time.sleep(self.retry_backoff * (attempt + 1))
            except (
                urllib.error.URLError,
                TimeoutError,
                OSError,
                json.JSONDecodeError,
            ) as exc:
                last_error = str(exc)
                if attempt < self.max_retries:
                    time.sleep(self.retry_backoff * (attempt + 1))

        self.errors.append(f"{path}: {last_error}")
        return None

    def _get(
        self, path: str, params: dict[str, str] | None = None
    ) -> list[dict[str, Any]]:
        """Execute GET request with retry. Returns decoded data list on success."""
        if not self.base_url:
            self.errors.append(f"{path}: SHAREDSIGNALS_API_URL is not configured")
            return []

        clean_params = {k: str(v) for k, v in (params or {}).items() if v}

        cache_key = self._cache_key(path, clean_params)
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        query = urllib.parse.urlencode(sorted(clean_params.items()))
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{query}"

        response = self._request_json(path, url, method="GET")
        if response is None:
            return []
        result, received_at = response
        data = result.get("data", [])
        if not isinstance(data, list):
            data = []
        data = self._attach_http_response_receipt(
            data,
            path=path,
            received_at=received_at,
        )
        self._cache_set(cache_key, data)
        return data

    @staticmethod
    def _is_aware_timestamp(value: object) -> bool:
        if type(value) is not str or not value.strip():
            return False
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False
        return parsed.tzinfo is not None and parsed.utcoffset() is not None

    @classmethod
    def _validate_v1_metadata(
        cls,
        metadata: object,
        *,
        dataset_id: str,
    ) -> str | None:
        if not isinstance(metadata, dict) or not metadata:
            return "missing or malformed metadata"

        required = {
            "state",
            "runtime_state",
            "degraded",
            "freshness",
            "quality",
            "lineage",
            "receipt_id",
            "data_through",
            "observed_at",
            "requested_as_of",
            "resolved_as_of",
            "reasons",
        }
        if not required.issubset(metadata):
            return "missing or malformed metadata"
        if (
            metadata.get("state") != "ready"
            or metadata.get("runtime_state") != "success"
            or metadata.get("degraded") is not False
        ):
            return "metadata is not healthy success"

        freshness = metadata.get("freshness")
        if (
            not isinstance(freshness, dict)
            or freshness.get("state") != "fresh"
            or freshness.get("stale") is not False
            or type(freshness.get("sla_seconds")) is not int
            or freshness["sla_seconds"] < 0
        ):
            return "missing or malformed metadata freshness"

        quality = metadata.get("quality")
        if (
            not isinstance(quality, dict)
            or quality.get("state") != "valid"
            or quality.get("valid") is not True
            or not isinstance(quality.get("evidence"), list)
        ):
            return "missing or malformed metadata quality"

        lineage = metadata.get("lineage")
        if (
            not isinstance(lineage, dict)
            or lineage.get("state") != "complete"
            or lineage.get("complete") is not True
            or lineage.get("provider_neutral") is not True
            or type(lineage.get("authority")) is not str
            or not lineage["authority"].strip()
            or lineage.get("dataset_id") != dataset_id
            or not isinstance(lineage.get("providers"), list)
            or not lineage["providers"]
            or not all(
                type(provider) is str and bool(provider.strip())
                for provider in lineage["providers"]
            )
            or type(lineage.get("receipt_watermark")) is not str
            or not lineage["receipt_watermark"].strip()
        ):
            return "missing or malformed metadata lineage"

        if type(metadata.get("receipt_id")) is not str or not metadata[
            "receipt_id"
        ].strip():
            return "missing or malformed metadata receipt_id"
        for field in ("data_through", "observed_at"):
            if not cls._is_aware_timestamp(metadata.get(field)):
                return f"missing or malformed metadata {field}"
        for field in ("requested_as_of", "resolved_as_of"):
            value = metadata.get(field)
            if value is not None and not cls._is_aware_timestamp(value):
                return f"missing or malformed metadata {field}"
        reasons = metadata.get("reasons")
        if not isinstance(reasons, list) or not all(
            type(reason) is str for reason in reasons
        ):
            return "missing or malformed metadata reasons"
        return None

    def _query_v1_page(
        self,
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any], str] | None:
        try:
            body = json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            self.errors.append(f"/v1/query: invalid request body ({exc})")
            return None
        return self._request_json(
            "/v1/query",
            f"{self.base_url}/v1/query",
            method="POST",
            body=body,
        )

    def query_v1_all(
        self,
        dataset_id: str,
        *,
        schema_major: int = 1,
        total_limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Exhaust bounded V1 query pages and return rows only when complete."""
        if type(dataset_id) is not str or not dataset_id.strip():
            self.errors.append("/v1/query: invalid dataset_id")
            return []
        if type(schema_major) is not int or schema_major <= 0:
            self.errors.append("/v1/query: invalid schema_major")
            return []
        if total_limit is not None and (
            type(total_limit) is not int or total_limit <= 0
        ):
            self.errors.append("/v1/query: invalid total_limit")
            return []

        rows: list[dict[str, Any]] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        expected_schema_version: str | None = None
        page_limit = self._v1_page_limit
        if total_limit is not None:
            page_limit = min(page_limit, total_limit)

        for page_number in range(1, self._v1_max_pages + 1):
            if total_limit is not None and len(rows) >= total_limit:
                return rows
            payload: dict[str, Any] = {
                "dataset_id": dataset_id,
                "schema_major": schema_major,
                "limit": page_limit,
            }
            if cursor is not None:
                payload["cursor"] = cursor

            response = self._query_v1_page(payload)
            if response is None:
                return []
            page, received_at = response

            required_page_fields = {
                "api_version",
                "catalog_version",
                "request_id",
                "dataset_id",
                "schema_version",
                "data",
                "next_cursor",
                "metadata",
            }
            if "metadata" not in page:
                self.errors.append("/v1/query: missing or malformed metadata")
                return []
            if not required_page_fields.issubset(page):
                self.errors.append("/v1/query: malformed V1 page")
                return []
            if page.get("api_version") != "v1":
                self.errors.append("/v1/query: malformed V1 page")
                return []
            for field in ("catalog_version", "request_id"):
                value = page.get(field)
                if type(value) is not str or not value.strip():
                    self.errors.append(f"/v1/query: malformed V1 page {field}")
                    return []
            if page.get("dataset_id") != dataset_id:
                self.errors.append("/v1/query: dataset drift between pages")
                return []

            schema_version = page.get("schema_version")
            if type(schema_version) is not str or not schema_version.strip():
                self.errors.append("/v1/query: schema drift between pages")
                return []
            schema_prefix = schema_version.split(".", 1)[0]
            if not schema_prefix.isdigit() or int(schema_prefix) != schema_major:
                self.errors.append("/v1/query: schema drift between pages")
                return []
            if expected_schema_version is None:
                expected_schema_version = schema_version
            elif schema_version != expected_schema_version:
                self.errors.append("/v1/query: schema drift between pages")
                return []

            metadata = page.get("metadata")
            metadata_error = self._validate_v1_metadata(
                metadata,
                dataset_id=dataset_id,
            )
            if metadata_error is not None:
                self.errors.append(f"/v1/query: {metadata_error}")
                return []

            page_rows = page.get("data")
            if not isinstance(page_rows, list) or not all(
                isinstance(row, dict) for row in page_rows
            ):
                self.errors.append("/v1/query: malformed V1 page data")
                return []
            if len(page_rows) > page_limit:
                self.errors.append("/v1/query: malformed V1 page exceeds limit")
                return []

            next_cursor = page.get("next_cursor")
            if next_cursor is not None and (
                type(next_cursor) is not str or not next_cursor.strip()
            ):
                self.errors.append("/v1/query: malformed cursor")
                return []
            if not page_rows and next_cursor is not None:
                self.errors.append("/v1/query: empty page with cursor")
                return []
            if next_cursor is not None and next_cursor in seen_cursors:
                self.errors.append("/v1/query: repeated cursor")
                return []

            page_evidence = {
                field: deepcopy(page[field])
                for field in (
                    "api_version",
                    "catalog_version",
                    "request_id",
                    "dataset_id",
                    "schema_version",
                    "next_cursor",
                    "metadata",
                )
            }
            enriched_page: list[dict[str, Any]] = []
            for raw_row in page_rows:
                if "sharedsignals_v1_page_evidence" in raw_row:
                    self.errors.append("/v1/query: reserved evidence field collision")
                    return []
                row = dict(raw_row)
                row["sharedsignals_v1_page_evidence"] = deepcopy(page_evidence)
                enriched_page.append(row)
            enriched_page = self._attach_http_response_receipt(
                enriched_page,
                path="/v1/query",
                received_at=received_at,
            )
            rows.extend(enriched_page)

            if total_limit is not None and len(rows) >= total_limit:
                return rows[:total_limit]
            if next_cursor is None:
                return rows
            if page_number >= self._v1_max_pages:
                self.errors.append("/v1/query: page cap exceeded")
                return []
            seen_cursors.add(next_cursor)
            cursor = next_cursor

        self.errors.append("/v1/query: page cap exceeded")
        return []

    # ── 15 canonical functions ───────────────────────────────────────────────

    def is_trading_day(self, date: str | None = None) -> bool:
        """Check if date is a trading day. Returns False on API failure."""
        if date is None:
            date = datetime.now().strftime("%Y%m%d")
        rows = self._get("/is_trading_day", {"date": date})
        if rows:
            return bool(rows[0].get("is_trading_day", False))
        return False

    def get_market_data(
        self,
        ts_code: str,
        start: str | None = None,
        end: str | None = None,
        freq: str = "daily",
    ) -> list[dict[str, Any]]:
        return self._get(
            "/market_data",
            {
                "ts_code": ts_code,
                "start": start or "",
                "end": end or "",
                "freq": freq,
            },
        )

    def get_fundamentals(
        self, ts_code: str, end_date: str | None = None
    ) -> list[dict[str, Any]]:
        params: dict[str, str] = {"ts_code": ts_code}
        if end_date:
            params["end_date"] = end_date
        return self._get("/fundamentals", params)

    def get_reference(self, table: str) -> list[dict[str, Any]]:
        return self._get("/reference", {"table": table})

    def get_macro_factors(
        self,
        start: str | None = None,
        end: str | None = None,
    ) -> list[dict[str, Any]]:
        return self._get("/macro", {"start": start or "", "end": end or ""})

    def get_capital_flow(
        self,
        ts_code: str | None = None,
        start: str | None = None,
        end: str | None = None,
    ) -> list[dict[str, Any]]:
        return self._get(
            "/capital_flow",
            {
                "ts_code": ts_code or "",
                "start": start or "",
                "end": end or "",
            },
        )

    def get_events(
        self,
        start: str | None = None,
        end: str | None = None,
        market: str | None = None,
        symbol: str | None = None,
        subject_code: str | None = None,
        event_type: str | None = None,
    ) -> list[dict[str, Any]]:
        return self._get(
            "/events",
            {
                "start": start or "",
                "end": end or "",
                "market": market or "",
                "symbol": symbol or "",
                "subject_code": subject_code or "",
                "event_type": event_type or "",
            },
        )

    def get_sentiment(
        self,
        start: str | None = None,
        end: str | None = None,
    ) -> list[dict[str, Any]]:
        return self._get("/sentiment", {"start": start or "", "end": end or ""})

    def get_crypto_klines(
        self, symbol: str, limit: int | None = None
    ) -> list[dict[str, Any]]:
        params: dict[str, str] = {"symbol": symbol}
        if limit is not None:
            params["limit"] = str(limit)
        return self._get("/crypto", params)

    def get_pm_markets(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._get("/pm_markets", {"limit": str(limit)})

    def get_pm_prices(
        self,
        market_id: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        return self._get(
            "/pm_prices", {"market_id": market_id or "", "limit": str(limit)}
        )

    def get_associations(
        self,
        ts_code: str | None = None,
        event_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return self._get(
            "/associations",
            {
                "ts_code": ts_code or "",
                "event_id": event_id or "",
            },
        )

    def get_impacts(
        self,
        event_type: str | None = None,
        target: str | None = None,
    ) -> list[dict[str, Any]]:
        return self._get(
            "/impacts",
            {
                "event_type": event_type or "",
                "target": target or "",
            },
        )

    def get_industry(self, ts_code: str) -> list[dict[str, Any]]:
        return self._get("/industry", {"ts_code": ts_code})

    def get_realtime_5min(
        self,
        ts_code: str = "",
        date: str | None = None,
        market: str | None = None,
    ) -> list[dict[str, Any]]:
        date_value = date or ""
        return self._get(
            "/realtime_5min",
            {
                "ts_code": ts_code,
                "symbol": ts_code,
                "date": date_value,
                "trade_date": date_value,
                "market": market or "",
            },
        )

    def get_tushare(
        self,
        api_name: str,
        ts_code: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        params: dict[str, str] = {"api_name": api_name}
        if ts_code:
            params["ts_code"] = ts_code
        if start_date:
            params["start_date"] = start_date
        if end_date:
            params["end_date"] = end_date
        params.update({k: str(v) for k, v in kwargs.items() if v})
        return self._get("/tushare", params)

    def get_health(self) -> dict[str, Any]:
        """Get API health status (uncached)."""
        try:
            url = f"{self.base_url}/health"
            req = urllib.request.Request(
                url, headers={"Accept": "application/json"}, method="GET"
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception as e:
            return {"status": "unreachable", "error": str(e)}
