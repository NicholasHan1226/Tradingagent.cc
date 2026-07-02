#!/usr/bin/env python3
"""HTTP client for SharedSignals API (port 8082).

Mirrors the 15 canonical reader functions via HTTP instead of direct SQLite reads.
Provides fail-safe access: network errors return empty data rather than raising.

Usage:
    from shared.data.shared_signals_api import SharedSignalsAPIClient
    client = SharedSignalsAPIClient()
    rows = client.get_market_data(ts_code="000001.SZ")
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_API_URL = os.environ.get("SHAREDSIGNALS_API_URL", "http://127.0.0.1:8082")
DEFAULT_API_KEY = os.environ.get("SHAREDSIGNALS_API_KEY", "")
DEFAULT_TIMEOUT = float(os.environ.get("SHAREDSIGNALS_API_TIMEOUT", "10"))
DEFAULT_RETRIES = int(os.environ.get("SHAREDSIGNALS_API_RETRIES", "1"))


class SharedSignalsAPIClient:
    """HTTP client for SharedSignals REST API.

    All methods return list[dict] or None on failure.
    Cache is shared across instances via class-level TTL cache.
    """

    _cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
    _cache_ttl: float = 30.0  # seconds

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
    ):
        self.base_url = (base_url or DEFAULT_API_URL).rstrip("/")
        self.api_key = api_key or DEFAULT_API_KEY
        self.timeout = timeout if timeout is not None else DEFAULT_TIMEOUT
        self.max_retries = max_retries if max_retries is not None else DEFAULT_RETRIES
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

    def _get(self, path: str, params: dict[str, str] | None = None) -> list[dict[str, Any]]:
        """Execute GET request with retry. Returns decoded data list on success."""
        clean_params = {k: str(v) for k, v in (params or {}).items() if v}

        cache_key = self._cache_key(path, clean_params)
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        query = "&".join(f"{k}={urllib.request.quote(v)}" for k, v in sorted(clean_params.items()))
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{query}"

        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
            headers["X-API-Key"] = self.api_key

        last_error = ""
        for attempt in range(self.max_retries + 1):
            try:
                req = urllib.request.Request(url, headers=headers, method="GET")
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read().decode("utf-8", errors="replace")
                result = json.loads(raw)
                data = result.get("data", [])
                if not isinstance(data, list):
                    data = []
                self._cache_set(cache_key, data)
                return data
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, json.JSONDecodeError) as exc:
                last_error = str(exc)
                if attempt < self.max_retries:
                    time.sleep(0.5)

        self.errors.append(f"{path}: {last_error}")
        return []

    # ── 15 canonical functions ───────────────────────────────────────────────

    def is_trading_day(self, date: str | None = None) -> bool:
        """Check if date is a trading day. Returns True/False (defaults True)."""
        if date is None:
            date = datetime.now().strftime("%Y%m%d")
        rows = self._get("/is_trading_day", {"date": date})
        if rows:
            return bool(rows[0].get("is_trading_day", True))
        return True

    def get_market_data(
        self, ts_code: str, start: str | None = None, end: str | None = None,
        freq: str = "daily",
    ) -> list[dict[str, Any]]:
        return self._get("/market_data", {
            "ts_code": ts_code, "start": start or "", "end": end or "", "freq": freq,
        })

    def get_fundamentals(self, ts_code: str) -> list[dict[str, Any]]:
        return self._get("/fundamentals", {"ts_code": ts_code})

    def get_reference(self, table: str) -> list[dict[str, Any]]:
        return self._get("/reference", {"table": table})

    def get_macro_factors(
        self, start: str | None = None, end: str | None = None,
    ) -> list[dict[str, Any]]:
        return self._get("/macro", {"start": start or "", "end": end or ""})

    def get_capital_flow(self, date: str) -> list[dict[str, Any]]:
        return self._get("/capital_flow", {"date": date})

    def get_events(
        self, start: str | None = None, end: str | None = None,
    ) -> list[dict[str, Any]]:
        return self._get("/events", {"start": start or "", "end": end or ""})

    def get_sentiment(
        self, start: str | None = None, end: str | None = None,
    ) -> list[dict[str, Any]]:
        return self._get("/sentiment", {"start": start or "", "end": end or ""})

    def get_crypto_klines(self, symbol: str) -> list[dict[str, Any]]:
        return self._get("/crypto", {"symbol": symbol})

    def get_pm_markets(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._get("/pm_markets", {"limit": str(limit)})

    def get_associations(
        self, ts_code: str | None = None, event_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return self._get("/associations", {
            "ts_code": ts_code or "", "event_id": event_id or "",
        })

    def get_impacts(
        self, event_type: str | None = None, target: str | None = None,
    ) -> list[dict[str, Any]]:
        return self._get("/impacts", {
            "event_type": event_type or "", "target": target or "",
        })

    def get_industry(self, ts_code: str) -> list[dict[str, Any]]:
        return self._get("/industry", {"ts_code": ts_code})

    def get_realtime_5min(
        self, ts_code: str, date: str | None = None,
    ) -> list[dict[str, Any]]:
        if date is None:
            date = datetime.now().strftime("%Y%m%d")
        return self._get("/realtime_5min", {"ts_code": ts_code, "date": date})

    def get_tushare(
        self, api_name: str, ts_code: str | None = None,
        start_date: str | None = None, end_date: str | None = None,
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
            req = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception as e:
            return {"status": "unreachable", "error": str(e)}
