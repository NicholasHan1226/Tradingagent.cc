#!/usr/bin/env python3
"""Read-only HTTP client for MarketGraph research APIs."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


DEFAULT_API_URL = os.environ.get("MARKETGRAPH_API_URL", "http://127.0.0.1:8080")
DEFAULT_API_TOKEN = os.environ.get("MARKETGRAPH_API_TOKEN", "")
DEFAULT_TIMEOUT = float(os.environ.get("MARKETGRAPH_API_TIMEOUT", "10"))
DEFAULT_RETRIES = int(os.environ.get("MARKETGRAPH_API_RETRIES", "1"))
DEFAULT_RETRY_BACKOFF = float(os.environ.get("MARKETGRAPH_API_RETRY_BACKOFF", "0.5"))
DEFAULT_CACHE_TTL_SECONDS = float(os.environ.get("MARKETGRAPH_API_CACHE_TTL_SECONDS", "30"))


class MarketGraphAPIClient:
    """Fail-closed client for MarketGraph research/read-model endpoints."""

    def __init__(
        self,
        base_url: str | None = None,
        api_token: str | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
        retry_backoff: float | None = None,
    ) -> None:
        self.base_url = (base_url or DEFAULT_API_URL).rstrip("/")
        self.api_token = api_token if api_token is not None else DEFAULT_API_TOKEN
        self.timeout = timeout if timeout is not None else DEFAULT_TIMEOUT
        self.max_retries = max_retries if max_retries is not None else DEFAULT_RETRIES
        self.retry_backoff = retry_backoff if retry_backoff is not None else DEFAULT_RETRY_BACKOFF
        self.cache_ttl_seconds = DEFAULT_CACHE_TTL_SECONDS
        self._cache: dict[tuple[str, tuple[tuple[str, str], ...]], tuple[float, dict[str, Any]]] = {}
        self.errors: list[str] = []

    def _get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        if not self.base_url:
            self.errors.append(f"{path}: MARKETGRAPH_API_URL is not configured")
            return {}

        clean_params = {k: str(v) for k, v in (params or {}).items() if v not in ("", None)}
        url = f"{self.base_url}{path}"
        query = urllib.parse.urlencode(sorted(clean_params.items()))
        cache_key = (path, tuple(sorted(clean_params.items())))
        cached = self._cache.get(cache_key)
        if cached and time.time() - cached[0] <= self.cache_ttl_seconds:
            return dict(cached[1])
        if query:
            url = f"{url}?{query}"

        headers = {"Accept": "application/json"}
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"

        last_error = ""
        for attempt in range(self.max_retries + 1):
            try:
                req = urllib.request.Request(url, headers=headers, method="GET")
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    payload = json.loads(resp.read().decode("utf-8", errors="replace"))
                    if isinstance(payload, dict):
                        self._cache[cache_key] = (time.time(), dict(payload))
                        return payload
                    return {}
            except urllib.error.HTTPError as exc:
                last_error = f"HTTP {exc.code}: {exc.reason}"
                if exc.code < 500 and exc.code != 429:
                    break
            except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
                last_error = str(exc)
            if attempt < self.max_retries:
                time.sleep(self.retry_backoff * (attempt + 1))

        self.errors.append(f"{path}: {last_error}")
        return {}

    def get_regime(self, days: int = 14) -> dict[str, Any] | None:
        payload = self._get("/regime", {"days": str(days)})
        if not payload or payload.get("error"):
            return None
        regime = payload.get("regime")
        if isinstance(regime, dict):
            row = dict(regime)
        else:
            row = dict(payload)
        label = row.get("regime") or row.get("label") or row.get("name")
        if label:
            row["regime"] = str(label)
            return row
        return None

    def get_pm_research_probabilities(self, limit: int = 100) -> list[dict[str, Any]]:
        payload = self._get("/pm/research-probabilities", {"limit": str(limit)})
        data = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(data, list):
            return [dict(row) for row in data if isinstance(row, dict)]
        return []

    def get_contract_table(
        self,
        table_id: str,
        *,
        market: str = "Ashare",
        include_rows: bool = True,
        limit: int = 50000,
        record_usage: bool = False,
    ) -> dict[str, Any]:
        payload = self._get(
            "/contract",
            {
                "table_id": table_id,
                "market": market,
                "include_rows": "1" if include_rows else "0",
                "limit": str(limit),
                "record_usage": "1" if record_usage else "0",
            },
        )
        if not payload or payload.get("error"):
            return {}
        return payload
