#!/usr/bin/env python3
"""Unified data readers bridging SharedSignals SQLite and MarketGraph CSV outputs.

SharedSignalsReader reads the read-model SQLite database (marketdata.sqlite).
MarketGraphCSVReader reads MarketGraph CSV outputs (regime, events, sentiment).
TradingagentDataReader composes both into a fail-safe unified interface.
"""

from __future__ import annotations

import csv
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# -- Default paths -----------------------------------------------------------

DEFAULT_SHARED_SIGNALS_DB = Path(
    os.environ.get(
        "SHARED_SIGNALS_DB",
        os.environ.get(
            "SHAREDSIGNALS_ROOT",
            "/opt/investment/SharedSignals",
        )
        + "/read_model/marketdata.sqlite",
    )
)


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
        sql = "SELECT * FROM market_bars_intraday WHERE market=? AND symbol=? AND interval=?"
        params: list = [market, symbol, interval]
        if start_time:
            sql += " AND bar_time >= ?"
            params.append(start_time)
        if end_time:
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
    """Read from MarketGraph CSV outputs (regime, event_candidates, sentiment)."""

    def __init__(self, root: Path | str):
        self.root = Path(root)
        data_intake = self.root / "data" / "intake"
        self.intake = data_intake if data_intake.exists() else self.root / "intake"

    def _read_csv(self, path: Path) -> list[dict[str, str]]:
        if not path.exists():
            return []
        with path.open(encoding="utf-8-sig") as f:
            return list(csv.DictReader(f))

    def get_regime(self) -> dict[str, Any] | None:
        candidates = [
            self.root / "data" / "all_weather_regime.csv",
            self.root / "all_weather_regime.csv",
        ]
        for path in candidates:
            rows = self._read_csv(path)
            if rows:
                return dict(rows[-1])
        return None

    def get_event_candidates(self) -> list[dict[str, str]]:
        return self._read_csv(self.intake / "event_candidates.csv")

    def get_sentiment_signals(self) -> list[dict[str, str]]:
        return self._read_csv(self.intake / "sentiment_signals.csv")

    def get_sentiment(self) -> list[dict[str, str]]:
        return self.get_sentiment_signals()


# -- Unified TradingagentDataReader ----------------------------------------------


class TradingagentDataReader:
    """Fail-safe unified reader: SharedSignals API + SQLite + MarketGraph CSV.

    Uses SharedSignals HTTP API first, falls back to direct SQLite reads.
    MarketGraph data is read from CSV (same-machine file access).

    All methods are safe to call regardless of whether the underlying data
    sources are available — missing data returns empty lists / None rather
    than raising.
    """

    def __init__(
        self,
        shared: SharedSignalsReader | None = None,
        marketgraph: MarketGraphCSVReader | None = None,
    ):
        self._shared = shared
        self._marketgraph = marketgraph
        self.errors: list[str] = []
        self.stale = False
        self._error_count_at_last_log = 0

    def _maybe_alert(self) -> None:
        """Log a warning when errors accumulate beyond threshold — dead-man switch."""
        if len(self.errors) > self._error_count_at_last_log and len(self.errors) % 10 == 0:
            import logging
            logger = logging.getLogger("tradingagent.data")
            logger.warning(
                "TradingagentDataReader: %d errors accumulated (stale=%s) — last: %s",
                len(self.errors), self.stale, self.errors[-1]
            )
            self._error_count_at_last_log = len(self.errors)


    def _record_shared_error(self, op: str) -> None:
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
            try:
                self._shared = SharedSignalsReader()
            except Exception as e:
                self.errors.append(f"SharedSignalsReader init failed: {e}")
                self.stale = True
                self._maybe_alert()
                # Don't silently mask data loss with /dev/null.
                # Keep _shared as None so callers see AttributeError
                # instead of operating on empty data.
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
            self._marketgraph = MarketGraphCSVReader(
                Path(
                    os.environ.get("MARKETGRAPH_ROOT", "/opt/investment/MarketGraph")
                )
            )
        return self._marketgraph

    def get_asset(self, market: str, symbol: str) -> dict[str, Any] | None:
        try:
            result = self.shared.get_asset(market, symbol)
            self._record_shared_error("get_asset")
            return result
        except Exception as e:
            self.errors.append(f"get_asset: {e}")
            self.stale = True
            self._maybe_alert()
            return None

    def get_bars_daily(
        self, market: str, symbol: str, start: str = "", end: str = ""
    ) -> list[dict[str, Any]]:
        try:
            result = self.shared.get_bars_daily(market, symbol, start, end)
            self._record_shared_error("get_bars_daily")
            return result
        except Exception as e:
            self.errors.append(f"get_bars_daily: {e}")
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
            result = self.shared.get_events(market=market or "", symbol=symbol,
                                            start_date=start, end_date=end)
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
            result = self.shared.get_factors(market=market or "", symbol=symbol)
            self._record_shared_error("get_factors")
            return result
        except Exception as e:
            self.errors.append(f"get_factors: {e}")
            self.stale = True
            self._maybe_alert()
            return []

    def get_sentiment(self) -> list[dict[str, Any]]:
        try:
            raw = self.marketgraph.get_sentiment_signals()
            return [dict(r) for r in raw]
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
            result = self.shared.get_bars_intraday(market, symbol, interval, start, end)
            self._record_shared_error("get_bars_intraday")
            return result
        except Exception as e:
            self.errors.append(f"get_bars_intraday: {e}")
            self.stale = True
            self._maybe_alert()
            return []
