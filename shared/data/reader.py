#!/usr/bin/env python3
"""Unified data readers for TradingAgent market and research inputs.

TradingagentDataReader uses SharedSignals API as the production data entry.
Direct SQLite read-model access is allowed only for explicitly injected test
readers or explicit emergency/local-read configuration.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .marketgraph_api import DEFAULT_API_URL as DEFAULT_MARKETGRAPH_API_URL
from .marketgraph_api import MarketGraphAPIClient
from .shared_signals_api import DEFAULT_API_URL as DEFAULT_SHARED_SIGNALS_API_URL
from .shared_signals_api import SharedSignalsAPIClient


# -- Default paths -----------------------------------------------------------

def _default_shared_signals_db() -> Path:
    configured = os.environ.get("SHARED_SIGNALS_DB")
    if configured:
        return Path(configured)
    return Path("/nonexistent/tradingagent-sharedsignals-diagnostic.sqlite")


DEFAULT_SHARED_SIGNALS_DB = _default_shared_signals_db()


# -- SharedSignals SQLite Reader ---------------------------------------------


class SharedSignalsReader:
    """Read from SharedSignals DuckDB/SQLite read-model.

    Provides typed accessors for assets, daily bars, intraday bars,
    events, factors, and coverage status.
    """

    def __init__(self, db_path: Path | str | None = None):
        self._db_path = Path(db_path or DEFAULT_SHARED_SIGNALS_DB)
        self._conn: sqlite3.Connection | None = None
        self.last_error: str | None = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True)
            self._conn.execute("PRAGMA busy_timeout = 5000")
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def _query(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        try:
            self.last_error = None
            cur = self.conn.execute(sql, params)
            return [dict(row) for row in cur.fetchall()]
        except (sqlite3.OperationalError, sqlite3.DatabaseError) as e:
            self.last_error = str(e)
            return []

    # --- Accessors ---

    def get_asset(self, market: str, symbol: str) -> dict[str, Any] | None:
        rows = self._query(
            "SELECT * FROM market_assets WHERE market=? AND symbol=?",
            (market, symbol),
        )
        return rows[0] if rows else None

    def get_assets(self, market: str | None = None) -> list[dict[str, Any]]:
        if market:
            return self._query("SELECT * FROM market_assets WHERE market=?", (market,))
        return self._query("SELECT * FROM market_assets")

    def get_bars_daily(
        self,
        market: str,
        symbol: str,
        start_date: str = "",
        end_date: str = "",
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM market_bars_daily WHERE market=? AND symbol=?"
        params: list = [market, symbol]
        if start_date:
            sql += " AND trade_date >= ?"
            params.append(start_date)
        if end_date:
            sql += " AND trade_date <= ?"
            params.append(end_date)
        sql += " ORDER BY trade_date"
        return self._query(sql, tuple(params))

    def get_bars_intraday(
        self,
        market: str,
        symbol: str,
        interval: str = "5m",
        start_time: str = "",
        end_time: str = "",
    ) -> list[dict[str, Any]]:
        interval_values = [interval]
        if interval in {"5m", "5min"}:
            interval_values = ["5m", "5min"]
        placeholders = ",".join("?" for _ in interval_values)
        sql = (
            "SELECT * FROM market_bars_intraday "
            f"WHERE market=? AND symbol=? AND interval IN ({placeholders})"
        )
        params: list = [market, symbol, *interval_values]
        if start_time:
            compact = start_time.replace("-", "")
            if len(compact) == 8 and compact.isdigit():
                iso_date = f"{compact[:4]}-{compact[4:6]}-{compact[6:8]}"
                sql += " AND (replace(COALESCE(trade_date,''),'-','') >= ? OR substr(bar_time,1,10) >= ?)"
                params.extend([compact, iso_date])
            else:
                sql += " AND bar_time >= ?"
                params.append(start_time)
        if end_time:
            compact = end_time.replace("-", "")
            if len(compact) == 8 and compact.isdigit():
                iso_date = f"{compact[:4]}-{compact[4:6]}-{compact[6:8]}"
                sql += " AND (replace(COALESCE(trade_date,''),'-','') <= ? OR substr(bar_time,1,10) <= ?)"
                params.extend([compact, iso_date])
            else:
                sql += " AND bar_time <= ?"
                params.append(end_time + "T23:59:59" if len(end_time) == 10 else end_time)
        sql += " ORDER BY bar_time"
        return self._query(sql, tuple(params))

    def get_events(
        self,
        market: str | None = None,
        symbol: str = "",
        start_date: str = "",
        end_date: str = "",
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM market_events WHERE 1=1"
        params: list = []
        if market:
            sql += " AND market=?"
            params.append(market)
        if symbol:
            sql += " AND symbol=?"
            params.append(symbol)
        if start_date:
            sql += " AND trade_date >= ?"
            params.append(start_date)
        if end_date:
            sql += " AND trade_date <= ?"
            params.append(end_date)
        sql += " ORDER BY event_time DESC"
        return self._query(sql, tuple(params))

    def get_factors(
        self,
        market: str | None = None,
        symbol: str = "",
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM market_factors WHERE 1=1"
        params: list = []
        if market:
            sql += " AND market=?"
            params.append(market)
        if symbol:
            sql += " AND symbol=?"
            params.append(symbol)
        return self._query(sql, tuple(params))

    def get_coverage(
        self,
        market: str,
        trade_date: str,
    ) -> list[dict[str, Any]]:
        return self._query(
            "SELECT * FROM market_coverage_status WHERE market=? AND trade_date=?",
            (market, trade_date),
        )


# -- MarketGraph CSV Reader --------------------------------------------------


class MarketGraphCSVReader:
    """API-only MarketGraph/SharedSignals-facing reader.

    The class name is retained for compatibility with existing call sites, but
    it no longer reads MarketGraph CSV files. MarketGraph research data must
    come from a public API/client boundary.
    """

    def __init__(
        self,
        root: Path | str,
        api_client: SharedSignalsAPIClient | None = None,
        marketgraph_client: MarketGraphAPIClient | None = None,
        api_enabled: bool | None = None,
    ):
        self.root = Path(root)
        data_intake = self.root / "data" / "intake"
        self.intake = data_intake if data_intake.exists() else self.root / "intake"
        self._api_client = api_client
        self._marketgraph_client = marketgraph_client
        self._api_enabled = bool(api_client) if api_enabled is None else bool(api_enabled)
        self._logger = logging.getLogger("tradingagent.data")

    def _read_csv(self, path: Path) -> list[dict[str, str]]:
        return []

    @staticmethod
    def _unwrap_api_rows(rows: Any) -> list[dict[str, Any]]:
        if rows is None:
            return []
        if isinstance(rows, dict):
            data = rows.get("data")
            if isinstance(data, list):
                rows = data
            else:
                return [dict(rows)]
        if not isinstance(rows, list):
            return []
        normalized: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            data = row.get("data")
            if isinstance(data, dict):
                normalized.append(dict(data))
            else:
                normalized.append(dict(row))
        return normalized

    @staticmethod
    def _has_meaningful_rows(rows: list[dict[str, Any]]) -> bool:
        return any(any(value not in ("", None, [], {}) for value in row.values()) for row in rows)

    def _api_rows(
        self,
        csv_name: str,
        operations: list[tuple[str, tuple[Any, ...], dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        if not self._api_enabled or self._api_client is None:
            return []

        before_error_count = len(getattr(self._api_client, "errors", []))
        for method_name, args, kwargs in operations:
            method = getattr(self._api_client, method_name, None)
            if not callable(method):
                continue
            try:
                rows = self._unwrap_api_rows(method(*args, **kwargs))
            except Exception as exc:  # pragma: no cover - defensive API boundary
                self._logger.warning(
                    "MarketGraphCSVReader %s API call %s failed; fail-closed: %s",
                    csv_name,
                    method_name,
                    exc,
                )
                return []

            api_errors = getattr(self._api_client, "errors", [])
            if len(api_errors) > before_error_count:
                self._logger.warning(
                    "MarketGraphCSVReader %s API call %s reported error; fail-closed: %s",
                    csv_name,
                    method_name,
                    api_errors[-1],
                )
                return []
            if self._has_meaningful_rows(rows):
                self._logger.info(
                    "MarketGraphCSVReader %s loaded via SharedSignals API (%s)",
                    csv_name,
                    method_name,
                )
                return rows

        self._logger.info(
            "MarketGraphCSVReader %s unavailable from API; fail-closed",
            csv_name,
        )
        return []

    def _read_csv_with_log(self, csv_name: str, path: Path) -> list[dict[str, str]]:
        return []

    def get_regime(self) -> dict[str, Any] | None:
        if self._marketgraph_client is not None:
            if isinstance(self._marketgraph_client, MarketGraphAPIClient) and not self._marketgraph_client.api_token:
                self._logger.info(
                    "MarketGraphCSVReader all_weather_regime.csv skipped: MARKETGRAPH_API_TOKEN is not configured"
                )
            else:
                before_error_count = len(getattr(self._marketgraph_client, "errors", []))
                row = self._marketgraph_client.get_regime()
                if row:
                    return row
                if len(getattr(self._marketgraph_client, "errors", [])) > before_error_count:
                    self._logger.warning(
                        "MarketGraphCSVReader all_weather_regime.csv MarketGraph API call failed; fail-closed: %s",
                        self._marketgraph_client.errors[-1],
                    )
        api_rows = self._api_rows(
            "all_weather_regime.csv",
            [
                ("get_regime", (), {}),
                ("get_macro_factors", (), {}),
            ],
        )
        if api_rows:
            regime_rows = [row for row in api_rows if row.get("regime")]
            if regime_rows:
                return dict(regime_rows[-1])
            self._logger.info("MarketGraphCSVReader all_weather_regime.csv API rows did not include regime")
        return None

    def get_event_candidates(self) -> list[dict[str, str]]:
        if self._marketgraph_client is not None:
            if isinstance(self._marketgraph_client, MarketGraphAPIClient) and not self._marketgraph_client.api_token:
                self._logger.info(
                    "MarketGraphCSVReader association_impact_relations skipped: MARKETGRAPH_API_TOKEN is not configured"
                )
                return []
            before_error_count = len(getattr(self._marketgraph_client, "errors", []))
            get_contract_table = getattr(self._marketgraph_client, "get_contract_table", None)
            if callable(get_contract_table):
                payload = get_contract_table(
                    "association_impact_relations",
                    market="Ashare",
                    include_rows=True,
                    limit=int(os.environ.get("MARKETGRAPH_EVENT_IMPACT_LIMIT", "50000")),
                    record_usage=False,
                )
                if payload and not payload.get("error"):
                    rows = self._normalize_impact_relation_rows(payload.get("rows") or [])
                    if rows:
                        return rows
                if len(getattr(self._marketgraph_client, "errors", [])) > before_error_count:
                    self._logger.warning(
                        "MarketGraphCSVReader association_impact_relations MarketGraph API call failed; fail-closed: %s",
                        self._marketgraph_client.errors[-1],
                    )
        return []

    @staticmethod
    def _normalize_impact_relation_rows(rows: Any) -> list[dict[str, Any]]:
        if not isinstance(rows, list):
            return []
        normalized: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            target_type = str(row.get("target_type") or "").strip().lower()
            if target_type and target_type not in {"stock", "equity", "asset"}:
                continue
            target_id = str(row.get("target_id") or row.get("subject_code") or "").strip()
            if not target_id:
                continue
            polarity = str(row.get("polarity") or row.get("proposed_impact_hint") or "").strip().lower()
            direction = {
                "+": "positive",
                "bullish": "positive",
                "positive": "positive",
                "-": "negative",
                "bearish": "negative",
                "negative": "negative",
                "mixed": "mixed",
                "neutral": "neutral",
            }.get(polarity.split(":", 1)[0], "neutral")
            item = dict(row)
            item["subject_code"] = target_id
            item["subject_type"] = "stock"
            item.setdefault("status", "verified")
            item["proposed_impact_hint"] = direction
            item.setdefault("confidence", row.get("strength") or 0.5)
            item.setdefault("event_time", row.get("valid_from") or row.get("event_date") or "")
            item.setdefault("source", "MarketGraph association_impact_relations")
            normalized.append(item)
        return normalized

    def get_sentiment_signals(self) -> list[dict[str, str]]:
        api_rows = self._api_rows(
            "sentiment_signals.csv",
            [
                ("get_sentiment_signals", (), {}),
                ("get_sentiment", (), {}),
            ],
        )
        if api_rows:
            return api_rows
        return []

    def get_sentiment(self) -> list[dict[str, str]]:
        return self.get_sentiment_signals()

    def health_check(self) -> dict[str, Any]:
        if not self._api_enabled or self._api_client is None:
            return {"api_enabled": False, "status": "disabled", "source": "api"}
        try:
            health = self._api_client.get_health()
        except Exception as exc:  # pragma: no cover - defensive API boundary
            return {"api_enabled": True, "status": "unreachable", "error": str(exc)}
        if not isinstance(health, dict):
            return {"api_enabled": True, "status": "unknown", "raw": health}
        return {"api_enabled": True, **health}


# -- Unified TradingagentDataReader ----------------------------------------------


class TradingagentDataReader:
    """Fail-safe unified reader: SharedSignals API + explicit local read model.

    Uses SharedSignals HTTP API first. Direct SQLite reads are allowed only when
    a caller injects a SharedSignalsReader or explicitly enables local read-model
    fallback for tests/emergency diagnostics.

    All methods are safe to call regardless of whether the underlying data
    sources are available — missing data returns empty lists / None rather
    than raising.
    """

    def __init__(
        self,
        shared: SharedSignalsReader | None = None,
        marketgraph: MarketGraphCSVReader | None = None,
        api_client: SharedSignalsAPIClient | None = None,
    ):
        self._shared = shared
        self._marketgraph = marketgraph
        api_url = os.environ.get("SHAREDSIGNALS_API_URL", DEFAULT_SHARED_SIGNALS_API_URL).strip()
        marketgraph_api_url = os.environ.get("MARKETGRAPH_API_URL", DEFAULT_MARKETGRAPH_API_URL).strip()
        self._api_client = api_client
        if self._api_client is None and api_url:
            self._api_client = SharedSignalsAPIClient(base_url=api_url)
        self._marketgraph_api_client = MarketGraphAPIClient(base_url=marketgraph_api_url) if marketgraph_api_url else None
        self.errors: list[str] = []
        self.stale = False
        self.degraded = False
        self._error_count_at_last_log = 0
        self._last_api_used = False

    def _can_use_sqlite_fallback(self) -> bool:
        if self._shared is not None:
            return True
        return str(os.environ.get("TRADINGAGENT_ALLOW_SHARED_SIGNALS_SQLITE", "")).strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    def _maybe_alert(self) -> None:
        """Log a warning when errors accumulate beyond threshold — dead-man switch."""
        if len(self.errors) > self._error_count_at_last_log and len(self.errors) % 10 == 0:
            logger = logging.getLogger("tradingagent.data")
            logger.warning(
                "TradingagentDataReader: %d errors accumulated (stale=%s) — last: %s",
                len(self.errors), self.stale, self.errors[-1]
            )
            self._error_count_at_last_log = len(self.errors)

    def _record_api_fallback(self, op: str, reason: str) -> None:
        self.degraded = True
        self.stale = True
        message = f"{op}: API unavailable, using explicit SQLite diagnostic read ({reason})"
        if not self.errors or self.errors[-1] != message:
            self.errors.append(message)
        self._maybe_alert()

    def _api_call(
        self,
        op: str,
        fallback: Callable[[], Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Prefer SharedSignals API and only use explicit local read-model diagnostics."""
        if self._api_client is None:
            if not self._can_use_sqlite_fallback():
                message = f"{op}: SharedSignals API unavailable and SQLite diagnostic read disabled"
                if not self.errors or self.errors[-1] != message:
                    self.errors.append(message)
                self.stale = True
                self._maybe_alert()
                return False if op == "is_trading_day" else []
            return fallback()

        before_error_count = len(getattr(self._api_client, "errors", []))
        self._last_api_used = False
        try:
            method = getattr(self._api_client, op)
            result = method(*args, **kwargs)
            self._last_api_used = True
        except Exception as exc:  # pragma: no cover - defensive API boundary
            if self._can_use_sqlite_fallback():
                self._record_api_fallback(op, str(exc))
                return fallback()
            self.errors.append(f"{op}: SharedSignals API failed ({exc}); SQLite diagnostic read disabled")
            self.stale = True
            self._maybe_alert()
            return False if op == "is_trading_day" else []

        api_errors = getattr(self._api_client, "errors", [])
        if len(api_errors) > before_error_count:
            if self._can_use_sqlite_fallback():
                self._record_api_fallback(op, api_errors[-1])
                return fallback()
            self.errors.append(f"{op}: SharedSignals API error ({api_errors[-1]}); SQLite diagnostic read disabled")
            self.stale = True
            self._maybe_alert()
            return False if op == "is_trading_day" else []
        return result

    def _record_shared_error(self, op: str) -> None:
        if self._last_api_used:
            self._last_api_used = False
            return  # API succeeded; stale shared error is from a different operation
        self._last_api_used = False
        error = getattr(self._shared, "last_error", None)
        if error:
            self.stale = True
            message = f"{op}: {error}"
            if not self.errors or self.errors[-1] != message:
                self.errors.append(message)
            self._maybe_alert()

    @property
    def shared(self) -> SharedSignalsReader:
        if self._shared is None:
            if not self._can_use_sqlite_fallback():
                raise RuntimeError("SharedSignals SQLite diagnostic read is disabled; use SHAREDSIGNALS_API_URL")
            try:
                self._shared = SharedSignalsReader()
            except Exception as e:
                self.errors.append(f"SharedSignalsReader init failed: {e}")
                self.stale = True
                self._maybe_alert()
                # Don't silently mask data loss with /dev/null.
                # Keep _shared as None so callers see AttributeError
                # instead of operating on empty data.
                logger = logging.getLogger("tradingagent.data")
                logger.critical(
                    "TradingagentDataReader: SharedSignalsReader init failed — "
                    "ALL data reads will fail. Fix DB path or connectivity. Error: %s", e
                )
                raise RuntimeError(
                    f"SharedSignalsReader unavailable: {e}"
                ) from e
        return self._shared

    @property
    def marketgraph(self) -> MarketGraphCSVReader:
        if self._marketgraph is None:
            marketgraph_data = os.environ.get("MARKETGRAPH_DATA", "").strip()
            self._marketgraph = MarketGraphCSVReader(
                Path(marketgraph_data) if marketgraph_data else Path("/nonexistent/tradingagent/marketgraph_api_only"),
                api_client=self._api_client,
                marketgraph_client=self._marketgraph_api_client,
            )
        return self._marketgraph

    @staticmethod
    def _canonical_market(market: str | None) -> str:
        key = str(market or "").strip().lower()
        if key in {"", "ashare", "a_share", "a-share", "a股", "cn", "china"}:
            return "Ashare"
        return str(market or "").strip()

    @staticmethod
    def _to_ts_code(market: str, symbol: str) -> str:
        if "." in symbol:
            return symbol
        if TradingagentDataReader._canonical_market(market) == "Ashare" and symbol.isdigit() and len(symbol) == 6:
            suffix = "SH" if symbol.startswith(("5", "6", "9")) else "SZ"
            return f"{symbol}.{suffix}"
        return symbol

    @staticmethod
    def _market_symbol_from_ts_code(ts_code: str, market: str | None = None) -> tuple[str, str]:
        code = ts_code.strip()
        if "." not in code:
            return market or "Ashare", code
        symbol, suffix = code.rsplit(".", 1)
        suffix = suffix.upper()
        if suffix in {"SH", "SZ", "BJ"}:
            return market or "Ashare", symbol
        if suffix == "HK":
            return market or "HK", code
        return market or suffix, symbol

    @staticmethod
    def _normalize_market_rows(
        rows: list[dict[str, Any]], market: str, symbol: str
    ) -> list[dict[str, Any]]:
        normalized = []
        for row in rows:
            item = dict(row)
            if not item.get("symbol"):
                for key in ("ts_code", "code", "ticker", "contract", "contract_code"):
                    if item.get(key):
                        item["symbol"] = item.get(key)
                        break
            item.setdefault("market", market)
            item.setdefault("symbol", symbol)
            if "volume" not in item and "vol" in item:
                item["volume"] = item["vol"]
            normalized.append(item)
        return normalized

    @staticmethod
    def _symbol_aliases(symbol: str, market: str | None = None) -> set[str]:
        raw = str(symbol or "").strip().upper()
        if not raw:
            return {""}
        aliases = {raw}
        ts_code = TradingagentDataReader._to_ts_code(market or "", raw).upper()
        aliases.add(ts_code)
        if "." in ts_code:
            aliases.add(ts_code.split(".", 1)[0])
        return aliases

    @staticmethod
    def _row_symbol_aliases(row: dict[str, Any], market: str | None = None) -> set[str]:
        aliases: set[str] = set()
        for key in ("symbol", "ts_code", "code", "ticker", "contract", "contract_code"):
            value = str(row.get(key) or "").strip().upper()
            if value:
                aliases.update(TradingagentDataReader._symbol_aliases(value, market))
        return aliases

    @staticmethod
    def _filter_rows_for_symbol(
        rows: list[dict[str, Any]], market: str, requested_symbol: str, read_symbol: str | None = None
    ) -> list[dict[str, Any]]:
        target_aliases = TradingagentDataReader._symbol_aliases(requested_symbol, market)
        if read_symbol:
            target_aliases.update(TradingagentDataReader._symbol_aliases(read_symbol, market))
        filtered: list[dict[str, Any]] = []
        for row in rows:
            row_aliases = TradingagentDataReader._row_symbol_aliases(row, market)
            if row_aliases and not (row_aliases & target_aliases):
                continue
            item = dict(row)
            item["symbol"] = read_symbol or requested_symbol
            filtered.append(item)
        return filtered

    @staticmethod
    def _row_date_aliases(row: dict[str, Any]) -> set[str]:
        aliases: set[str] = set()
        for key in ("trade_date", "date", "bar_date"):
            value = str(row.get(key) or "").strip()
            if value:
                aliases.add(value.replace("-", "")[:8])
        bar_time = str(row.get("bar_time") or row.get("datetime") or row.get("timestamp") or "").strip()
        if len(bar_time) >= 10:
            aliases.add(bar_time[:10].replace("-", ""))
        return {alias for alias in aliases if len(alias) == 8 and alias.isdigit()}

    @staticmethod
    def _filter_rows_for_date(rows: list[dict[str, Any]], date_value: str | None) -> list[dict[str, Any]]:
        target = str(date_value or "").strip().replace("-", "")[:8]
        if len(target) != 8 or not target.isdigit():
            return rows
        filtered: list[dict[str, Any]] = []
        for row in rows:
            row_dates = TradingagentDataReader._row_date_aliases(row)
            if row_dates and target not in row_dates:
                continue
            filtered.append(row)
        return filtered

    @staticmethod
    def _has_event_payload(rows: list[dict[str, Any]]) -> bool:
        payload_keys = (
            "event_hash",
            "event_id",
            "title",
            "content",
            "event_type",
            "raw_json",
            "direction",
            "impact_hint",
            "proposed_impact_hint",
        )
        return any(any(row.get(key) for key in payload_keys) for row in rows or [] if isinstance(row, dict))

    @staticmethod
    def _has_priced_market_rows(rows: list[dict[str, Any]]) -> bool:
        for row in rows:
            for key in ("adjusted_close", "close", "price", "latest_price", "last_price", "market_price", "yes_price"):
                try:
                    value = float(row.get(key))
                except (TypeError, ValueError):
                    continue
                if value > 0 and value == value:
                    return True
        return False

    @staticmethod
    def _normalize_asset_rows(rows: list[dict[str, Any]], market: str | None = None) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            item = dict(row)
            symbol = str(item.get("symbol") or item.get("ts_code") or "").strip().upper()
            if not symbol:
                continue
            if "." not in symbol and market and market.lower() in {"ashare", "a_share", "a-share"}:
                symbol = TradingagentDataReader._to_ts_code("Ashare", symbol)
            item["symbol"] = symbol
            item.setdefault("market", market or item.get("market") or ("Ashare" if symbol.endswith((".SH", ".SZ", ".BJ")) else ""))
            if "sector" not in item and item.get("industry"):
                item["sector"] = item.get("industry")
            if "status" not in item:
                item["status"] = "active"
            normalized.append(item)
        return normalized

    @staticmethod
    def _asset_api_name_for_market(market: str | None) -> str:
        key = str(market or "").strip().lower()
        if key in {"futures", "cn_futures", "cnfutures", "期货"}:
            return "fut_basic"
        if key in {"us", "usa", "usstock", "美股"}:
            return "us_basic"
        if key in {"hk", "hongkong", "港股"}:
            return "hk_basic"
        if key in {"etf"}:
            return "etf_basic"
        return ""

    @staticmethod
    def _is_ashare_market(market: str | None) -> bool:
        key = str(market or "").strip().lower()
        return key in {"", "ashare", "a_share", "a-share", "a股", "cn", "china"}

    def get_assets(self, market: str | None = None) -> list[dict[str, Any]]:
        try:
            if self._is_ashare_market(market):
                def fallback() -> list[dict[str, Any]]:
                    rows = self.shared.get_assets("Ashare")
                    if not rows:
                        rows = self.shared.get_assets("ashare")
                    return rows

                result = self._api_call("get_tushare", fallback, api_name="stock_basic")
                self._record_shared_error("get_assets")
                return self._normalize_asset_rows(result, "Ashare")

            api_name = self._asset_api_name_for_market(market)
            if api_name:
                def fallback() -> list[dict[str, Any]]:
                    return self.shared.get_assets(market)

                result = self._api_call("get_tushare", fallback, api_name=api_name, market=market)
            elif self._can_use_sqlite_fallback():
                result = self.shared.get_assets(market)
            else:
                result = []
            self._record_shared_error("get_assets")
            return self._normalize_asset_rows(result, market)
        except Exception as e:
            self.errors.append(f"get_assets: {e}")
            self.stale = True
            self._maybe_alert()
            return []

    def get_asset(self, market: str, symbol: str) -> dict[str, Any] | None:
        try:
            symbol_key = str(symbol or "").strip().upper()
            for row in self.get_assets(market):
                row_symbol = str(row.get("symbol") or row.get("ts_code") or "").strip().upper()
                if row_symbol == symbol_key:
                    return row
            return None
        except Exception as e:
            self.errors.append(f"get_asset: {e}")
            self.stale = True
            self._maybe_alert()
            return None

    def get_bars_daily(
        self, market: str, symbol: str, start: str = "", end: str = ""
    ) -> list[dict[str, Any]]:
        try:
            market_name = self._canonical_market(market)
            ts_code = self._to_ts_code(market_name, symbol)

            start_value = start or end or None
            end_value = end or start or None

            def fallback() -> list[dict[str, Any]]:
                rows = self.shared.get_bars_daily(market_name, ts_code, start_value or "", end_value or "")
                if rows:
                    return rows
                return self.shared.get_bars_daily(market, symbol, start_value or "", end_value or "")

            result = self._api_call(
                "get_market_data",
                fallback,
                ts_code=ts_code,
                start=start_value,
                end=end_value,
                freq="daily",
            )
            normalized = self._normalize_market_rows(result, market_name, ts_code)
            if not self._has_priced_market_rows(normalized):
                if self._can_use_sqlite_fallback():
                    fallback_rows = fallback()
                    if fallback_rows:
                        self._last_api_used = False
                        self._record_shared_error("get_bars_daily")
                        return self._normalize_market_rows(fallback_rows, market_name, ts_code)
                return []
            self._record_shared_error("get_bars_daily")
            return normalized
        except Exception as e:
            self.errors.append(f"get_bars_daily: {e}")
            self.stale = True
            self._maybe_alert()
            return []

    def get_market_data(
        self,
        ts_code: str,
        start: str | None = None,
        end: str | None = None,
        freq: str = "daily",
        market: str | None = None,
    ) -> list[dict[str, Any]]:
        market_name, symbol = self._market_symbol_from_ts_code(ts_code, market)

        start_value = start or end or None
        end_value = end or start or None

        def fallback() -> list[dict[str, Any]]:
            if freq in {"5m", "5min", "intraday"}:
                return self.shared.get_bars_intraday(
                    market_name, symbol, "5m", start_value or "", end_value or ""
                )
            return self.shared.get_bars_daily(
                market_name, symbol, start_value or "", end_value or ""
            )

        try:
            result = self._api_call(
                "get_market_data",
                fallback,
                ts_code=ts_code,
                start=start_value,
                end=end_value,
                freq=freq,
            )
            normalized = self._normalize_market_rows(result, market_name, symbol)
            if not self._has_priced_market_rows(normalized):
                if self._can_use_sqlite_fallback():
                    fallback_rows = fallback()
                    if fallback_rows:
                        self._last_api_used = False
                        self._record_shared_error("get_market_data")
                        return self._normalize_market_rows(fallback_rows, market_name, symbol)
                return []
            self._record_shared_error("get_market_data")
            return normalized
        except Exception as e:
            self.errors.append(f"get_market_data: {e}")
            self.stale = True
            self._maybe_alert()
            return []

    def get_event_candidates(self) -> list[dict[str, Any]]:
        try:
            raw = self.marketgraph.get_event_candidates()
            return [dict(r) for r in raw]
        except Exception as e:
            self.errors.append(f"get_event_candidates: {e}")
            self.stale = True
            self._maybe_alert()
            return []

    def get_regime(self) -> dict[str, Any] | None:
        try:
            return self.marketgraph.get_regime()
        except Exception as e:
            self.errors.append(f"get_regime: {e}")
            self.stale = True
            self._maybe_alert()
            return None

    def get_events(
        self, market: str | None = None, symbol: str = "",
        start: str = "", end: str = "",
    ) -> list[dict[str, Any]]:
        try:
            def fallback() -> list[dict[str, Any]]:
                return self.shared.get_events(
                    market=market or "", symbol=symbol,
                    start_date=start, end_date=end,
                )

            result = self._api_call(
                "get_events",
                fallback,
                start=start or None,
                end=end or None,
                market=market or None,
                symbol=symbol or None,
                subject_code=self._to_ts_code(market or "", symbol) if symbol else None,
            )
            if isinstance(result, list) and (not result or not self._has_event_payload(result)):
                if self._can_use_sqlite_fallback():
                    fallback_rows = fallback()
                    if fallback_rows:
                        self._last_api_used = False
                        self._record_shared_error("get_events")
                        result = fallback_rows
                else:
                    result = []
            if market:
                result = [r for r in result if not r.get("market") or r.get("market") == market]
            if symbol:
                result = [
                    r for r in result
                    if not r.get("symbol") or r.get("symbol") == symbol
                    or r.get("subject_code") in {symbol, self._to_ts_code(market or "", symbol)}
                ]
            self._record_shared_error("get_events")
            return result
        except Exception as e:
            self.errors.append(f"get_events: {e}")
            self.stale = True
            self._maybe_alert()
            return []

    def get_factors(
        self, market: str | None = None, symbol: str = "",
    ) -> list[dict[str, Any]]:
        try:
            market_name = self._canonical_market(market)
            ts_code = self._to_ts_code(market_name, symbol) if symbol else ""
            result: list[dict[str, Any]] = []
            if ts_code:
                result.extend(self._factor_rows_from_api(self.get_fundamentals(ts_code)))
                result.extend(self._factor_rows_from_api(self.get_capital_flow(ts_code=ts_code)))
            return result
        except Exception as e:
            self.errors.append(f"get_factors: {e}")
            self.stale = True
            self._maybe_alert()
            return []

    @staticmethod
    def _factor_rows_from_api(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        passthrough_keys = {
            "market",
            "symbol",
            "ts_code",
            "trade_date",
            "event_time",
            "end_date",
            "provider",
            "source",
            "source_file",
            "collected_at",
        }
        metric_keys = {"factor_name", "metric", "metric_name", "name"}
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            metric = next((str(row.get(key) or "").strip() for key in metric_keys if row.get(key)), "")
            if metric:
                item = dict(row)
                item["factor_name"] = metric
                item.setdefault("value", row.get("value", row.get("factor_value", row.get("metric_value"))))
                normalized.append(item)
                continue
            base = {key: row.get(key) for key in passthrough_keys if key in row}
            for key, value in row.items():
                if key in passthrough_keys or key in metric_keys:
                    continue
                try:
                    numeric = float(value)
                except (TypeError, ValueError):
                    continue
                if numeric != numeric:
                    continue
                item = dict(base)
                item["factor_name"] = str(key)
                item["value"] = numeric
                normalized.append(item)
        return normalized

    def get_sentiment(
        self, start: str | None = None, end: str | None = None,
    ) -> list[dict[str, Any]]:
        try:
            def fallback() -> list[dict[str, Any]]:
                raw = self.marketgraph.get_sentiment_signals()
                return [dict(r) for r in raw]

            result = self._api_call(
                "get_sentiment",
                fallback,
                start=start,
                end=end,
            )
            self._record_shared_error("get_sentiment")
            return result
        except Exception as e:
            self.errors.append(f"get_sentiment: {e}")
            self.stale = True
            self._maybe_alert()
            return []

    def get_bars_intraday(
        self, market: str, symbol: str, interval: str = "5m",
        start: str = "", end: str = "",
    ) -> list[dict[str, Any]]:
        try:
            read_symbol = self._to_ts_code(market, symbol)
            date_value = end or start or None

            def fallback() -> list[dict[str, Any]]:
                return self.shared.get_bars_intraday(market, symbol, interval, start, end)

            result = self._api_call(
                "get_realtime_5min",
                fallback,
                ts_code=read_symbol,
                date=date_value,
                market=market,
            )
            normalized = self._normalize_market_rows(result, market, symbol)
            normalized = self._filter_rows_for_symbol(normalized, market, symbol, read_symbol)
            normalized = self._filter_rows_for_date(normalized, date_value)
            if not self._has_priced_market_rows(normalized):
                if self._can_use_sqlite_fallback():
                    fallback_rows = fallback()
                    if fallback_rows:
                        self._last_api_used = False
                        self._record_shared_error("get_bars_intraday")
                        fallback_normalized = self._normalize_market_rows(fallback_rows, market, symbol)
                        fallback_normalized = self._filter_rows_for_symbol(fallback_normalized, market, symbol, read_symbol)
                        return self._filter_rows_for_date(fallback_normalized, date_value)
                return []
            self._record_shared_error("get_bars_intraday")
            return normalized
        except Exception as e:
            self.errors.append(f"get_bars_intraday: {e}")
            self.stale = True
            self._maybe_alert()
            return []

    def get_realtime_5min_batch(
        self,
        market: str,
        date: str | None = None,
        *,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return the latest 5-minute batch for a market through SharedSignals API."""

        def fallback() -> list[dict[str, Any]]:
            return []

        try:
            if self._api_client is None:
                message = "get_realtime_5min: SharedSignals API unavailable; batch read disabled"
                if not self.errors or self.errors[-1] != message:
                    self.errors.append(message)
                self.stale = True
                self._maybe_alert()
                return []
            rows = self._api_call(
                "get_realtime_5min",
                fallback,
                ts_code="",
                date=date,
                market=market,
            )
            normalized = self._normalize_market_rows(rows, market, "")
            if limit is not None:
                return normalized[: max(1, int(limit))]
            return normalized
        except Exception as e:
            self.errors.append(f"get_realtime_5min_batch: {e}")
            self.stale = True
            self._maybe_alert()
            return []

    def is_trading_day(self, date: str | None = None) -> bool:
        date_value = date or datetime.now(timezone.utc).strftime("%Y%m%d")

        def fallback() -> bool:
            normalized = date_value.replace("-", "")
            try:
                rows = self.shared._query(
                    "SELECT 1 FROM market_bars_daily WHERE trade_date=? LIMIT 1",
                    (normalized,),
                )
                self._record_shared_error("is_trading_day")
                return bool(rows)
            except Exception as exc:
                self.errors.append(f"is_trading_day: {exc}")
                self.stale = True
                self._maybe_alert()
                return False

        try:
            return bool(self._api_call("is_trading_day", fallback, date=date_value))
        except Exception as e:
            self.errors.append(f"is_trading_day: {e}")
            self.stale = True
            self._maybe_alert()
            return False

    def get_fundamentals(
        self, ts_code: str, end_date: str | None = None,
    ) -> list[dict[str, Any]]:
        try:
            def fallback() -> list[dict[str, Any]]:
                return []

            result = self._api_call(
                "get_fundamentals",
                fallback,
                ts_code=ts_code,
                end_date=end_date,
            )
            self._record_shared_error("get_fundamentals")
            return result
        except Exception as e:
            self.errors.append(f"get_fundamentals: {e}")
            self.stale = True
            self._maybe_alert()
            return []

    def get_reference(self, table: str) -> list[dict[str, Any]]:
        try:
            def fallback() -> list[dict[str, Any]]:
                return []

            result = self._api_call("get_reference", fallback, table=table)
            self._record_shared_error("get_reference")
            return result
        except Exception as e:
            self.errors.append(f"get_reference: {e}")
            self.stale = True
            self._maybe_alert()
            return []

    def get_macro_factors(
        self, start: str | None = None, end: str | None = None,
    ) -> list[dict[str, Any]]:
        try:
            def fallback() -> list[dict[str, Any]]:
                return []

            result = self._api_call(
                "get_macro_factors",
                fallback,
                start=start,
                end=end,
            )
            self._record_shared_error("get_macro_factors")
            return result
        except Exception as e:
            self.errors.append(f"get_macro_factors: {e}")
            self.stale = True
            self._maybe_alert()
            return []

    def get_capital_flow(
        self, ts_code: str | None = None,
        start: str | None = None, end: str | None = None,
    ) -> list[dict[str, Any]]:
        try:
            def fallback() -> list[dict[str, Any]]:
                return []

            result = self._api_call(
                "get_capital_flow",
                fallback,
                ts_code=ts_code,
                start=start,
                end=end,
            )
            self._record_shared_error("get_capital_flow")
            return result
        except Exception as e:
            self.errors.append(f"get_capital_flow: {e}")
            self.stale = True
            self._maybe_alert()
            return []

    def get_crypto_klines(
        self, symbol: str, limit: int | None = None,
    ) -> list[dict[str, Any]]:
        try:
            def fallback() -> list[dict[str, Any]]:
                return []

            result = self._api_call(
                "get_crypto_klines",
                fallback,
                symbol=symbol,
                limit=limit,
            )
            self._record_shared_error("get_crypto_klines")
            return result
        except Exception as e:
            self.errors.append(f"get_crypto_klines: {e}")
            self.stale = True
            self._maybe_alert()
            return []

    def get_pm_markets(self, limit: int = 100) -> list[dict[str, Any]]:
        try:
            def fallback() -> list[dict[str, Any]]:
                return []

            result = self._api_call("get_pm_markets", fallback, limit=limit)
            self._record_shared_error("get_pm_markets")
            return result
        except Exception as e:
            self.errors.append(f"get_pm_markets: {e}")
            self.stale = True
            self._maybe_alert()
            return []

    def get_pm_prices(
        self,
        market_id: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int = 200,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        try:
            def fallback() -> list[dict[str, Any]]:
                return []

            if "symbol" in kwargs and not market_id:
                market_id = str(kwargs["symbol"])
            result = self._api_call("get_pm_prices", fallback, market_id=market_id, limit=limit)
            self._record_shared_error("get_pm_prices")
            return result
        except Exception as e:
            self.errors.append(f"get_pm_prices: {e}")
            self.stale = True
            self._maybe_alert()
            return []

    def get_associations(
        self, ts_code: str | None = None, event_id: str | None = None,
    ) -> list[dict[str, Any]]:
        try:
            def fallback() -> list[dict[str, Any]]:
                return []

            result = self._api_call(
                "get_associations",
                fallback,
                ts_code=ts_code,
                event_id=event_id,
            )
            self._record_shared_error("get_associations")
            return result
        except Exception as e:
            self.errors.append(f"get_associations: {e}")
            self.stale = True
            self._maybe_alert()
            return []

    def get_impacts(
        self, event_type: str | None = None, target: str | None = None,
    ) -> list[dict[str, Any]]:
        try:
            def fallback() -> list[dict[str, Any]]:
                return []

            result = self._api_call(
                "get_impacts",
                fallback,
                event_type=event_type,
                target=target,
            )
            self._record_shared_error("get_impacts")
            return result
        except Exception as e:
            self.errors.append(f"get_impacts: {e}")
            self.stale = True
            self._maybe_alert()
            return []

    def get_industry(self, ts_code: str) -> list[dict[str, Any]]:
        try:
            def fallback() -> list[dict[str, Any]]:
                return []

            result = self._api_call("get_industry", fallback, ts_code=ts_code)
            self._record_shared_error("get_industry")
            return result
        except Exception as e:
            self.errors.append(f"get_industry: {e}")
            self.stale = True
            self._maybe_alert()
            return []

    def get_realtime_5min(
        self, ts_code: str, date: str | None = None,
    ) -> list[dict[str, Any]]:
        try:
            def fallback() -> list[dict[str, Any]]:
                return []

            result = self._api_call(
                "get_realtime_5min",
                fallback,
                ts_code=ts_code,
                date=date,
            )
            self._record_shared_error("get_realtime_5min")
            return result
        except Exception as e:
            self.errors.append(f"get_realtime_5min: {e}")
            self.stale = True
            self._maybe_alert()
            return []

    def get_tushare(
        self, api_name: str, ts_code: str | None = None,
        start_date: str | None = None, end_date: str | None = None,
        **params: Any,
    ) -> list[dict[str, Any]]:
        try:
            def fallback() -> list[dict[str, Any]]:
                return []

            result = self._api_call(
                "get_tushare",
                fallback,
                api_name=api_name,
                ts_code=ts_code,
                start_date=start_date,
                end_date=end_date,
                **params,
            )
            self._record_shared_error("get_tushare")
            return result
        except Exception as e:
            self.errors.append(f"get_tushare: {e}")
            self.stale = True
            self._maybe_alert()
            return []

    def get_latest_daily_batch(
        self,
        market: str = "Ashare",
        *,
        limit: int = 5000,
    ) -> list[dict[str, Any]]:
        """Return recent daily bars through SharedSignals API for market-level ranking."""
        market_name = self._canonical_market(market)
        api_name = {
            "Ashare": "daily",
            "US": "us_daily",
            "HK": "hk_daily",
            "Futures": "fut_daily",
        }.get(market_name)
        if not api_name:
            return []
        rows = self.get_tushare(api_name, limit=max(1, int(limit)))
        normalized = self._normalize_market_rows(rows, market_name, "")
        if market_name:
            normalized = [
                row for row in normalized
                if not row.get("market") or str(row.get("market")).lower() == market_name.lower()
            ]
        return normalized

    def get_market_interface_snapshot(
        self,
        market: str = "Ashare",
        *,
        table_ids: list[str] | None = None,
        include_rows: bool = False,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Read MarketGraph 08 stable interface through its public API.

        This is read-only research context. Missing API/data degrades to an
        empty snapshot and must never relax market gates.
        """
        def degraded(reason: str) -> dict[str, Any]:
            return {
                "market": market,
                "contract_status": "missing_api",
                "tables": {},
                "readiness_summary": {},
                "degraded": True,
                "degrade_reason": reason,
                "is_trading_permission": False,
                "can_affect_real_money": False,
            }

        base_url = os.environ.get("MARKETGRAPH_API_URL", "").strip().rstrip("/")
        if not base_url:
            return degraded("marketgraph_api_url_missing")

        try:
            params: dict[str, str] = {
                "market": market,
                "include_rows": "1" if include_rows else "0",
            }
            if table_ids:
                params["table_ids"] = ",".join(table_ids)
            if limit is not None:
                params["limit"] = str(limit)
            query = urllib.parse.urlencode(params)
            req = urllib.request.Request(
                f"{base_url}/market/interface?{query}",
                headers={"Accept": "application/json"},
                method="GET",
            )
            timeout = float(os.environ.get("MARKETGRAPH_API_TIMEOUT", "10"))
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="replace"))
            if isinstance(payload, dict):
                data = payload.get("data")
                if isinstance(data, dict):
                    return data
                return payload
            return degraded("marketgraph_api_unexpected_payload")
        except (OSError, TimeoutError, urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
            return degraded(f"marketgraph_api_unavailable:{exc}")
        except Exception as e:
            self.errors.append(f"get_market_interface_snapshot: {e}")
            self.stale = True
            self._maybe_alert()
            return degraded(str(e))

    def get_market_readiness_summary(self, market: str = "Ashare") -> dict[str, Any]:
        snapshot = self.get_market_interface_snapshot(market=market, include_rows=False)
        summary = snapshot.get("readiness_summary")
        return dict(summary) if isinstance(summary, dict) else {}

    def get_market_knowledge_edges(
        self,
        market: str = "Ashare",
        *,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        snapshot = self.get_market_interface_snapshot(
            market=market,
            table_ids=["market_knowledge_edges"],
            include_rows=True,
            limit=limit,
        )
        table = (snapshot.get("tables") or {}).get("market_knowledge_edges")
        rows = table.get("rows") if isinstance(table, dict) else []
        return [dict(row) for row in rows or [] if isinstance(row, dict)]
