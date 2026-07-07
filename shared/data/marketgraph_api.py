from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


DEFAULT_MARKETGRAPH_API_URL = os.environ.get("MARKETGRAPH_API_URL", "http://127.0.0.1:8080")
DEFAULT_MARKETGRAPH_API_TOKEN = os.environ.get("MARKETGRAPH_API_TOKEN", "")
DEFAULT_TIMEOUT = float(os.environ.get("MARKETGRAPH_API_TIMEOUT", "10"))
DEFAULT_RETRIES = int(os.environ.get("MARKETGRAPH_API_RETRIES", "1"))
DEFAULT_RETRY_BACKOFF = float(os.environ.get("MARKETGRAPH_API_RETRY_BACKOFF", "0.5"))


class MarketGraphAPIClient:
    """Read-only client for MarketGraph REST research/evidence surfaces."""

    def __init__(
        self,
        base_url: str | None = None,
        api_token: str | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
        retry_backoff: float | None = None,
    ) -> None:
        self.base_url = (base_url or DEFAULT_MARKETGRAPH_API_URL).rstrip("/")
        self.api_token = api_token if api_token is not None else DEFAULT_MARKETGRAPH_API_TOKEN
        self.timeout = timeout if timeout is not None else DEFAULT_TIMEOUT
        self.max_retries = max_retries if max_retries is not None else DEFAULT_RETRIES
        self.retry_backoff = retry_backoff if retry_backoff is not None else DEFAULT_RETRY_BACKOFF
        self.errors: list[str] = []
        self.degraded = False

    def _unwrap_rows(self, payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, dict):
            data = payload.get("data")
            if isinstance(data, dict):
                rows = data.get("rows")
                if isinstance(rows, list):
                    return [dict(row) for row in rows if isinstance(row, dict)]
            rows = payload.get("rows")
            if isinstance(rows, list):
                return [dict(row) for row in rows if isinstance(row, dict)]
            if isinstance(data, list):
                return [dict(row) for row in data if isinstance(row, dict)]
        if isinstance(payload, list):
            return [dict(row) for row in payload if isinstance(row, dict)]
        return []

    def _get(self, path: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        clean_params = {key: str(value) for key, value in (params or {}).items() if value not in ("", None)}
        query = urllib.parse.urlencode(sorted(clean_params.items()))
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{query}"
        headers = {"Accept": "application/json"}
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"

        last_error = ""
        for attempt in range(self.max_retries + 1):
            try:
                req = urllib.request.Request(url, headers=headers, method="GET")
                with urllib.request.urlopen(req, timeout=self.timeout) as response:
                    payload = json.loads(response.read().decode("utf-8", errors="replace"))
                return self._unwrap_rows(payload)
            except urllib.error.HTTPError as exc:
                last_error = f"HTTP {exc.code}: {exc.reason}"
                if exc.code < 500 and exc.code != 429:
                    break
            except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
                last_error = str(exc)
            if attempt < self.max_retries:
                time.sleep(self.retry_backoff * (attempt + 1))

        self.degraded = True
        self.errors.append(f"{path}: {last_error}")
        return []

    def get_pm_research_probabilities(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._get("/pm/research-probabilities", {"limit": limit})


__all__ = ["MarketGraphAPIClient"]
