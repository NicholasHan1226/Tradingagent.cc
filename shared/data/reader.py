#!/usr/bin/env python3
"""SharedSignals and MarketGraph read-only data access for Tradings."""

from __future__ import annotations

import csv
import json
import subprocess
import sys
import warnings
import importlib
import importlib.util
import os
import select
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Iterable
from urllib.parse import quote


DEFAULT_SHARED_SIGNALS_DB = Path(
    "/opt/investment/MarketGraphRuntime/read_model/marketdata.sqlite"
)
DEFAULT_MARKETGRAPH_DATA = Path("/opt/investment/MarketGraph/data")
DEFAULT_SHARED_SIGNALS_ROOT = Path("/opt/investment/SharedSignals")


# Conservative built-in fallback for major A-share market holidays in 2026.
KNOWN_A_SHARE_HOLIDAYS_2026 = frozenset(
    {
        date(2026, 1, 1),
        date(2026, 1, 2),
        date(2026, 2, 16),
        date(2026, 2, 17),
        date(2026, 2, 18),
        date(2026, 2, 19),
        date(2026, 2, 20),
        date(2026, 2, 23),
        date(2026, 4, 6),
        date(2026, 5, 1),
        date(2026, 5, 4),
        date(2026, 5, 5),
        date(2026, 6, 19),
        date(2026, 9, 25),
        date(2026, 10, 1),
        date(2026, 10, 2),
        date(2026, 10, 5),
        date(2026, 10, 6),
        date(2026, 10, 7),
    }
)


DateLike = date | datetime | str


def _env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    return Path(value).expanduser() if value else default


def _sqlite_uri(path: Path) -> str:
    return "file:" + quote(str(path), safe="/:") + "?mode=ro"


def _rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def _first_present(row: dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
    return None


def _to_date(d: DateLike | None) -> date:
    if d is None:
        return date.today()
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    if isinstance(d, str):
        value = d.strip()
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
            try:
                return datetime.strptime(value, fmt).date()
            except ValueError:
                continue
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
        except ValueError as exc:
            raise ValueError(f"Cannot parse date: {d!r}") from exc
    raise TypeError(f"Unsupported date type: {type(d)!r}")


def _date_key(d: DateLike) -> str:
    value = str(d).strip()
    if value.isdigit() and len(value) == 8:
        return value
    return _to_date(d).strftime("%Y%m%d")


def _date_key_or_none(d: DateLike | None) -> str | None:
    return None if d is None else _date_key(d)


def _row_date_key(row: dict[str, Any], keys: Iterable[str]) -> str:
    value = _first_present(row, keys)
    if value is None:
        return ""
    raw = str(value).strip()
    if not raw:
        return ""
    try:
        return _date_key(raw[:10] if "-" in raw[:10] else raw[:8])
    except Exception:
        digits = "".join(ch for ch in raw if ch.isdigit())
        return digits[:8] if len(digits) >= 8 else ""


def _market_sql() -> str:
    return "LOWER(market)=LOWER(?)"


def _date_expr(*columns: str) -> str:
    parts = [
        "REPLACE(REPLACE("
        f"COALESCE({', '.join(f'SUBSTR({column}, 1, 10)' for column in columns)}), "
        "'-', ''), '/', '')"
    ]
    return parts[0]


def _pm_market_is_active(row: dict[str, Any]) -> bool:
    active_value = _first_present(row, ("active", "is_active", "enabled"))
    if active_value is not None:
        if isinstance(active_value, str):
            return active_value.strip().lower() in {"1", "true", "yes", "y", "active", "open"}
        return bool(active_value)

    status = str(_first_present(row, ("status", "state", "market_status")) or "").strip().upper()
    if not status:
        return True
    return status in {"ACTIVE", "OPEN", "TRADING", "LIVE"}


class SharedSignalsReader:
    """Read canonical SharedSignals marketdata.sqlite in read-only mode."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path else _env_path(
            "SHARED_SIGNALS_DB", DEFAULT_SHARED_SIGNALS_DB
        )
        self._conn: sqlite3.Connection | None = None
        self._table_columns_cache: dict[str, set[str]] = {}

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        self._table_columns_cache.clear()

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            conn = sqlite3.connect(_sqlite_uri(self.db_path), uri=True)
            conn.row_factory = sqlite3.Row
            self._conn = conn
        return self._conn

    def _table_columns(self, table: str) -> set[str]:
        cached = self._table_columns_cache.get(table)
        if cached is not None:
            return cached
        rows = self._connect().execute(f"PRAGMA table_info({table})").fetchall()
        columns = {str(row["name"]) for row in rows}
        self._table_columns_cache[table] = columns
        return columns

    def get_bars_daily(
        self,
        market: str,
        symbol: str,
        start: DateLike | None = None,
        end: DateLike | None = None,
    ) -> list[dict[str, Any]]:
        sql = (
            "SELECT * FROM market_bars_daily "
            f"WHERE {_market_sql()} AND symbol=?"
        )
        params: list[Any] = [market, symbol]
        start_key = _date_key_or_none(start)
        end_key = _date_key_or_none(end)
        if start_key is not None:
            sql += " AND REPLACE(trade_date, '-', '')>=?"
            params.append(start_key)
        if end_key is not None:
            sql += " AND REPLACE(trade_date, '-', '')<=?"
            params.append(end_key)
        sql += " ORDER BY trade_date ASC"
        return _rows_to_dicts(self._connect().execute(sql, params).fetchall())

    def get_bars_intraday(
        self,
        market: str,
        symbol: str,
        interval: str,
        start: DateLike | None = None,
        end: DateLike | None = None,
    ) -> list[dict[str, Any]]:
        sql = (
            "SELECT * FROM market_bars_intraday "
            f"WHERE {_market_sql()} AND symbol=? AND interval=?"
        )
        params: list[Any] = [market, symbol, interval]
        start_key = _date_key_or_none(start)
        end_key = _date_key_or_none(end)
        if start_key is not None:
            sql += " AND REPLACE(trade_date, '-', '')>=?"
            params.append(start_key)
        if end_key is not None:
            sql += " AND REPLACE(trade_date, '-', '')<=?"
            params.append(end_key)
        sql += " ORDER BY bar_time ASC"
        return _rows_to_dicts(self._connect().execute(sql, params).fetchall())

    def get_events(
        self,
        market: str,
        symbol: str,
        start: DateLike | None = None,
        end: DateLike | None = None,
    ) -> list[dict[str, Any]]:
        sql = f"SELECT * FROM market_events WHERE {_market_sql()} AND symbol=?"
        params: list[Any] = [market, symbol]
        start_key = _date_key_or_none(start)
        end_key = _date_key_or_none(end)
        date_expr = "REPLACE(COALESCE(trade_date, SUBSTR(event_time, 1, 10)), '-', '')"
        if start_key is not None:
            sql += f" AND {date_expr}>=?"
            params.append(start_key)
        if end_key is not None:
            sql += f" AND {date_expr}<=?"
            params.append(end_key)
        sql += " ORDER BY COALESCE(event_time, trade_date) ASC"
        return _rows_to_dicts(self._connect().execute(sql, params).fetchall())

    def get_factors(self, market: str, symbol: str) -> list[dict[str, Any]]:
        rows = self._connect().execute(
            "SELECT * FROM market_factors "
            f"WHERE {_market_sql()} AND symbol=? "
            "ORDER BY event_time DESC, collected_at DESC",
            (market, symbol),
        ).fetchall()
        return _rows_to_dicts(rows)

    def get_assets(self, market: str) -> list[dict[str, Any]]:
        rows = self._connect().execute(
            f"SELECT * FROM market_assets WHERE {_market_sql()} ORDER BY symbol ASC",
            (market,),
        ).fetchall()
        return _rows_to_dicts(rows)

    def get_asset(self, market: str, symbol: str) -> dict[str, Any] | None:
        row = self._connect().execute(
            f"SELECT * FROM market_assets WHERE {_market_sql()} AND symbol=?",
            (market, symbol),
        ).fetchone()
        return dict(row) if row is not None else None

    def get_coverage(self, market: str, date_value: DateLike) -> list[dict[str, Any]]:
        rows = self._connect().execute(
            "SELECT * FROM market_coverage_status "
            f"WHERE {_market_sql()} AND trade_date=? "
            "ORDER BY symbol ASC",
            (market, _date_key(date_value)),
        ).fetchall()
        return _rows_to_dicts(rows)

    def get_pm_markets(self, active_only: bool = True) -> list[dict[str, Any]]:
        rows = _rows_to_dicts(
            self._connect().execute("SELECT * FROM market_pm_markets").fetchall()
        )
        if active_only:
            rows = [row for row in rows if _pm_market_is_active(row)]
        return rows

    def get_pm_prices(
        self,
        market_id: str,
        start: DateLike | None = None,
        end: DateLike | None = None,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM market_pm_prices WHERE market_id=?"
        params: list[Any] = [market_id]
        start_key = _date_key_or_none(start)
        end_key = _date_key_or_none(end)
        columns = self._table_columns("market_pm_prices")
        date_columns = [
            column
            for column in ("trade_date", "price_date", "price_time", "timestamp", "collected_at", "updated_at")
            if column in columns
        ]
        if date_columns:
            date_expr = _date_expr(*date_columns)
            if start_key is not None:
                sql += f" AND {date_expr}>=?"
                params.append(start_key)
            if end_key is not None:
                sql += f" AND {date_expr}<=?"
                params.append(end_key)
            sql += f" ORDER BY COALESCE({', '.join(date_columns)}) ASC"
        else:
            sql += " ORDER BY market_id ASC"
        rows = self._connect().execute(sql, params).fetchall()
        return _rows_to_dicts(rows)

    def get_pm_universe(self) -> list[str]:
        markets = self.get_pm_markets(active_only=True)
        universe: list[str] = []
        for row in markets:
            market_id = _first_present(row, ("market_id", "id", "slug", "condition_id"))
            if market_id is not None:
                universe.append(str(market_id))
        return universe


class MarketGraphCSVReader:
    """Read MarketGraph CSV outputs used by screening.

    .. deprecated::
        Use :class:`MarketGraphMCPReader` instead.
    """

    def __init__(self, data_root: str | Path | None = None) -> None:
        warnings.warn(
            "MarketGraphCSVReader is deprecated; use MarketGraphMCPReader instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        self.data_root = Path(data_root) if data_root else _env_path(
            "MARKETGRAPH_DATA", DEFAULT_MARKETGRAPH_DATA
        )
        self.regime_file = self.data_root / "all_weather_regime.csv"
        self.event_file = self.data_root / "intake" / "event_candidates.csv"
        self.sentiment_file = self.data_root / "intake" / "sentiment_signals.csv"

    @staticmethod
    def _read_csv(path: Path) -> list[dict[str, str]]:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))

    def get_regime(self) -> dict[str, str] | None:
        rows = self._read_csv(self.regime_file)
        if not rows:
            return None
        return max(rows, key=lambda row: str(row.get("generated_at") or ""))

    def get_event_candidates(self) -> list[dict[str, str]]:
        return self._read_csv(self.event_file)

    def get_sentiment(self) -> list[dict[str, str]]:
        return self._read_csv(self.sentiment_file)

class MarketGraphLocalReader(MarketGraphCSVReader):
    """Cron-safe MarketGraph reader that avoids spawning MCP subprocesses."""

    def get_impacts(self, event: str, top_n: int = 20) -> list[dict[str, str]]:
        return []



class MarketGraphMCPReader:
    """Read MarketGraph data via MCP server subprocess with CSV fallback.

    Communicates with the MarketGraph MCP server over stdio JSON-RPC.
    Falls back to :class:`MarketGraphCSVReader` when the subprocess fails.
    """

    MCP_SERVER_SCRIPT = (
        "/opt/investment/MarketGraph/08-Market-Interfaces/tools/"
        "marketgraph_mcp_server.py"
    )

    def __init__(
        self,
        data_root: str | Path | None = None,
        mcp_server_script: str | Path | None = None,
    ) -> None:
        self.data_root = Path(data_root) if data_root else _env_path(
            "MARKETGRAPH_DATA", DEFAULT_MARKETGRAPH_DATA
        )
        self.mcp_server_script = (
            Path(mcp_server_script) if mcp_server_script
            else Path(self.MCP_SERVER_SCRIPT)
        )
        self._process: subprocess.Popen | None = None
        self._request_id: int = 0
        self._csv_reader: MarketGraphCSVReader | None = None
        self.mcp_timeout_seconds = float(os.environ.get("MARKETGRAPH_MCP_TIMEOUT_SECONDS", "3"))

    def _csv(self) -> MarketGraphCSVReader:
        if self._csv_reader is None:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                self._csv_reader = MarketGraphCSVReader(self.data_root)
        return self._csv_reader

    def _readline_with_timeout(self, proc: subprocess.Popen) -> str:
        assert proc.stdout is not None
        ready, _, _ = select.select([proc.stdout], [], [], self.mcp_timeout_seconds)
        if not ready:
            raise TimeoutError(f"MarketGraph MCP response timed out after {self.mcp_timeout_seconds:g}s")
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError("MarketGraph MCP server closed stdout")
        return line

    def _start(self) -> subprocess.Popen:
        if self._process is not None and self._process.poll() is None:
            return self._process
        if self._process is not None:
            self.close()
        self._process = subprocess.Popen(
            [sys.executable, str(self.mcp_server_script)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        self._request_id += 1
        init_payload = json.dumps({
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "TradingsReader", "version": "1.0.0"},
            },
        })
        proc = self._process
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(init_payload + "\n")
        proc.stdin.flush()
        # Read initialize response (discard; just need handshake to complete).
        json.loads(self._readline_with_timeout(proc))
        # Send initialized notification.
        proc.stdin.write(
            json.dumps({
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
            }) + "\n"
        )
        proc.stdin.flush()
        return proc

    def _call(
        self, tool_name: str, arguments: dict[str, Any] | None = None
    ) -> Any:
        """Call an MCP tool; returns parsed JSON result or None on failure."""
        try:
            proc = self._start()
            assert proc.stdin is not None and proc.stdout is not None
            self._request_id += 1
            request = json.dumps({
                "jsonrpc": "2.0",
                "id": self._request_id,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments or {}},
            })
            proc.stdin.write(request + "\n")
            proc.stdin.flush()
            response = json.loads(self._readline_with_timeout(proc))
            if "error" in response:
                return None
            result = response.get("result", {})
            content = result.get("content", [])
            if (
                content
                and isinstance(content, list)
                and content[0].get("type") == "text"
            ):
                return json.loads(content[0]["text"])
            return result
        except Exception:
            self.close()
            return None

    def get_impacts(self, event: str, top_n: int = 20) -> Any:
        """Query event impact via MCP ``query_event_impact``.

        Returns empty list on failure.
        """
        result = self._call("query_event_impact", {"event": event, "top_n": top_n})
        return result if result is not None else []

    def get_events(self, limit: int = 60) -> Any:
        """Get latest events via MCP ``news_brief``; fall back to CSV event candidates."""
        result = self._call("news_brief", {"limit": limit})
        if result is not None:
            return result
        return self._csv().get_event_candidates()

    def get_regime(self) -> Any:
        """Get macro regime via MCP ``get_regime``; fall back to CSV."""
        result = self._call("get_regime", {})
        if result is not None:
            return result
        return self._csv().get_regime()

    def get_event_candidates(self) -> list[dict[str, str]]:
        """Get event candidates; via MCP ``news_brief`` or CSV fallback."""
        result = self._call("news_brief", {"limit": 200})
        if result is not None:
            return result if isinstance(result, list) else []
        return self._csv().get_event_candidates()

    def get_sentiment(self) -> list[dict[str, str]]:
        """Get sentiment signals; CSV-only fallback."""
        return self._csv().get_sentiment()

    def close(self) -> None:
        if self._process is not None:
            try:
                if self._process.stdin is not None:
                    self._process.stdin.close()
                if self._process.stdout is not None:
                    self._process.stdout.close()
            except Exception:
                pass
            try:
                self._process.terminate()
                self._process.wait(timeout=5)
            except Exception:
                try:
                    self._process.kill()
                except Exception:
                    pass
            self._process = None

    def __del__(self) -> None:
        self.close()


@dataclass
class TradingsDataReader:
    """Fail-safe data facade for Tradings consumers.

    Missing upstream files, absent SQLite tables, and import failures are
    converted to neutral empty reads. Callers can inspect ``stale`` and
    ``errors`` to decide whether to reduce confidence.
    """

    shared: SharedSignalsReader = field(default_factory=SharedSignalsReader)
    marketgraph: MarketGraphLocalReader = field(default_factory=MarketGraphLocalReader)
    stale: bool = False
    errors: list[str] = field(default_factory=list)

    def _mark_stale(self, source: str, exc: Exception) -> None:
        self.stale = True
        self.errors.append(f"{source}: {exc.__class__.__name__}: {exc}")

    def _safe(self, source: str, default: Any, func: Callable[[], Any]) -> Any:
        try:
            result = func()
        except Exception as exc:
            self._mark_stale(source, exc)
            return default
        if result is None or result == []:
            self.stale = True
        return result

    def get_bars_daily(
        self,
        market: str,
        symbol: str,
        start: DateLike | None = None,
        end: DateLike | None = None,
    ) -> list[dict[str, Any]]:
        return self._safe(
            "sharedsignals.market_bars_daily",
            [],
            lambda: self.shared.get_bars_daily(market, symbol, start, end),
        )

    def get_bars_intraday(
        self,
        market: str,
        symbol: str,
        interval: str,
        start: DateLike | None = None,
        end: DateLike | None = None,
    ) -> list[dict[str, Any]]:
        return self._safe(
            "sharedsignals.market_bars_intraday",
            [],
            lambda: self.shared.get_bars_intraday(market, symbol, interval, start, end),
        )

    def get_events(
        self,
        market: str,
        symbol: str,
        start: DateLike | None = None,
        end: DateLike | None = None,
    ) -> list[dict[str, Any]]:
        return self._safe(
            "sharedsignals.market_events",
            [],
            lambda: self.shared.get_events(market, symbol, start, end),
        )

    def get_factors(self, market: str, symbol: str) -> list[dict[str, Any]]:
        return self._safe(
            "sharedsignals.market_factors",
            [],
            lambda: self.shared.get_factors(market, symbol),
        )

    def get_asset(self, market: str, symbol: str) -> dict[str, Any] | None:
        return self._safe(
            "sharedsignals.market_assets",
            None,
            lambda: self.shared.get_asset(market, symbol),
        )

    def get_assets(self, market: str) -> list[dict[str, Any]]:
        return self._safe(
            "sharedsignals.market_assets",
            [],
            lambda: self.shared.get_assets(market),
        )

    def get_coverage(self, market: str, date_value: DateLike) -> list[dict[str, Any]]:
        return self._safe(
            "sharedsignals.market_coverage_status",
            [],
            lambda: self.shared.get_coverage(market, date_value),
        )

    def get_pm_markets(self, active_only: bool = True) -> list[dict[str, Any]]:
        return self._safe(
            "sharedsignals.market_pm_markets",
            [],
            lambda: self.shared.get_pm_markets(active_only=active_only),
        )

    def get_pm_prices(
        self,
        market_id: str,
        start: DateLike | None = None,
        end: DateLike | None = None,
    ) -> list[dict[str, Any]]:
        return self._safe(
            "sharedsignals.market_pm_prices",
            [],
            lambda: self.shared.get_pm_prices(market_id, start, end),
        )

    def get_pm_universe(self) -> list[str]:
        return self._safe(
            "sharedsignals.market_pm_markets.universe",
            [],
            self.shared.get_pm_universe,
        )

    def get_regime(self) -> dict[str, Any] | None:
        return self._safe(
            "marketgraph.regime",
            None,
            self.marketgraph.get_regime,
        )

    def get_event_candidates(self) -> list[dict[str, Any]]:
        return self._safe(
            "marketgraph.event_candidates",
            [],
            self.marketgraph.get_event_candidates,
        )

    def get_sentiment(self) -> list[dict[str, Any]]:
        return self._safe(
            "marketgraph.sentiment_signals",
            [],
            self.marketgraph.get_sentiment,
        )

    def is_trading_day(self, d: DateLike | None = None) -> bool:
        try:
            return is_trading_day(d)
        except Exception as exc:
            self._mark_stale("sharedsignals.market_calendar", exc)
            return _fallback_is_trading_day(_to_date(d))

    def next_trading_day(self, d: DateLike | None = None) -> date:
        try:
            return next_trading_day(d)
        except Exception as exc:
            self._mark_stale("sharedsignals.market_calendar", exc)
            return _fallback_next_trading_day(_to_date(d))

    def get_trading_days(self, start: DateLike, end: DateLike) -> list[date]:
        try:
            return get_trading_days(start, end)
        except Exception as exc:
            self._mark_stale("sharedsignals.market_calendar", exc)
            return _fallback_get_trading_days(_to_date(start), _to_date(end))

    def status(self) -> dict[str, Any]:
        return {"stale": self.stale, "errors": list(self.errors)}


_CALENDAR_MODULE: ModuleType | None = None
_CALENDAR_IMPORT_ATTEMPTED = False


def _import_shared_calendar() -> ModuleType | None:
    global _CALENDAR_MODULE, _CALENDAR_IMPORT_ATTEMPTED
    if _CALENDAR_IMPORT_ATTEMPTED:
        return _CALENDAR_MODULE
    _CALENDAR_IMPORT_ATTEMPTED = True

    for module_name in (
        "SharedSignals.reference.market_calendar",
        "reference.market_calendar",
    ):
        try:
            _CALENDAR_MODULE = importlib.import_module(module_name)
            return _CALENDAR_MODULE
        except Exception:
            continue

    root = _env_path("SHARED_SIGNALS_ROOT", DEFAULT_SHARED_SIGNALS_ROOT)
    calendar_path = root / "reference" / "market_calendar.py"
    try:
        if calendar_path.exists():
            spec = importlib.util.spec_from_file_location(
                "_sharedsignals_market_calendar", calendar_path
            )
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                _CALENDAR_MODULE = module
                return _CALENDAR_MODULE
    except Exception:
        return None
    return None


def _fallback_is_trading_day(trading_day: date) -> bool:
    return trading_day.weekday() < 5 and trading_day not in KNOWN_A_SHARE_HOLIDAYS_2026


def _fallback_next_trading_day(trading_day: date) -> date:
    current = trading_day + timedelta(days=1)
    while not _fallback_is_trading_day(current):
        current += timedelta(days=1)
    return current


def _fallback_get_trading_days(start: date, end: date) -> list[date]:
    if start > end:
        start, end = end, start
    days: list[date] = []
    current = start
    while current <= end:
        if _fallback_is_trading_day(current):
            days.append(current)
        current += timedelta(days=1)
    return days


def is_trading_day(d: DateLike | None = None) -> bool:
    """Return whether d is an A-share trading day.

    Uses SharedSignals/reference/market_calendar.py when importable. If that
    layer is unavailable, falls back to the local conservative 2026 calendar.
    """

    target = _to_date(d)
    module = _import_shared_calendar()
    if module is not None and hasattr(module, "is_trading_day"):
        return bool(module.is_trading_day(target))
    return _fallback_is_trading_day(target)


def next_trading_day(d: DateLike | None = None) -> date:
    target = _to_date(d)
    module = _import_shared_calendar()
    if module is not None:
        if hasattr(module, "get_next_trading_day"):
            next_day = module.get_next_trading_day(target)
            if next_day is not None:
                return _to_date(next_day)
        if hasattr(module, "next_trading_day"):
            return _to_date(module.next_trading_day(target))
    return _fallback_next_trading_day(target)


def get_trading_days(start: DateLike, end: DateLike) -> list[date]:
    start_d = _to_date(start)
    end_d = _to_date(end)
    module = _import_shared_calendar()
    if module is not None:
        if hasattr(module, "get_trading_days"):
            return [_to_date(day) for day in module.get_trading_days(start_d, end_d)]
        if hasattr(module, "get_trading_calendar"):
            return [_to_date(day) for day in module.get_trading_calendar(start_d, end_d)]
    return _fallback_get_trading_days(start_d, end_d)


def _reset_calendar_import_for_tests() -> None:
    global _CALENDAR_MODULE, _CALENDAR_IMPORT_ATTEMPTED
    _CALENDAR_MODULE = None
    _CALENDAR_IMPORT_ATTEMPTED = False


# Backward-compatible name kept after the Tradings -> tradingagent rename.
TradingagentDataReader = TradingsDataReader
