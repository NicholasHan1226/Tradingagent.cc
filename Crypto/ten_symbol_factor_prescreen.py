"""Offline A-class factor pre-screen research over ten-symbol 5m history.

This module evaluates four candidate factors against historical 5-minute
bars pulled through the existing TradingDatas catalog/query contract.  The
history is a *backfill without PIT proof*: every artifact this module
produces is fixed ``not_promotion_evidence=true`` and may only ever feed
engineering/definition checks, never promotion evidence.  Analysis is pure
and offline; the fetch path needs an explicitly injected loopback transport
and never runs by default inside the repository.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
from typing import Any, Mapping, Sequence
import uuid

from Crypto.factor_research import _signal
from Crypto.market_observation import (
    BAR_FIELDS,
    OBSERVATION_SYMBOLS,
    _verify_catalog,
)
from Crypto.round_trip_capital import SLIPPAGE_BPS, TAKER_FEE_RATE
from shared.data.sharedsignals_v1 import (
    CatalogEnvelope,
    QueryRequest,
    SharedSignalsV1Client,
    SharedSignalsV1Config,
    SharedSignalsV1Error,
)
from shared.data.tradingdatas_pagination import (
    PaginationContractError,
    collect_query_pages,
)
from shared.data.tradingdatas_transport import (
    RuntimeGateConfigurationError,
    build_runtime_transport,
)


PRESCREEN_CONTRACT = "tradingagent.crypto.ten_symbol_factor_prescreen.v1"
RAW_CONTRACT = "tradingagent.crypto.ten_symbol_factor_prescreen_raw.v1"
MACHINE_ARTIFACT_CONTRACT = (
    "tradingagent.crypto.ten_symbol_factor_prescreen.machine_artifact.v1"
)
FIVE_MINUTES = timedelta(minutes=5)
HORIZON_BARS = 12
PAGE_LIMIT = 500
MAX_FETCH_WINDOWS = 400
MAX_RAW_BYTES = 512 * 1024 * 1024
NON_OVERLAP_STRIDE = 12
_CANDIDATE_IDS = (
    "xs_rs",
    "short_reversal",
    "amihud_illiquidity",
    "momentum_vol_regime",
)


class CryptoTenSymbolFactorPrescreenError(RuntimeError):
    """Stable fail-closed error for pre-screen fetch or analysis."""


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise CryptoTenSymbolFactorPrescreenError(
            "prescreen_payload_not_canonical"
        ) from exc


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def render_machine_artifact(result: Mapping[str, Any]) -> str:
    """Return the byte-stable, verdict-free machine artifact content.

    The artifact deliberately contains only the analysis result.  Human
    interpretation belongs to the Markdown report and is therefore excluded
    from the bytes that are hashed and persisted.
    """

    payload = {
        "artifact_contract": MACHINE_ARTIFACT_CONTRACT,
        "analysis": result,
    }
    return _canonical_json(payload) + "\n"


def machine_artifact_sha256(result: Mapping[str, Any]) -> str:
    """Return the SHA-256 of the exact machine artifact bytes."""

    return hashlib.sha256(render_machine_artifact(result).encode("utf-8")).hexdigest()


def _non_evidence_fields() -> dict[str, Any]:
    return {
        "authority": "none",
        "research_only": True,
        "not_promotion_evidence": True,
        "historical_backfill_no_pit": True,
        "execution_eligible": False,
        "execution_authority": False,
        "capital_write_eligible": False,
        "production_eligible": False,
        "real_trading_enabled": False,
        "promotion_authorized": False,
        "automatic_champion_replacement": False,
        "automatic_risk_expansion_enabled": False,
        "model_network_used": False,
    }


def _assert_simulation_only() -> None:
    if os.environ.get("REAL_TRADING_ENABLED") != "false":
        raise CryptoTenSymbolFactorPrescreenError(
            "prescreen_real_trading_must_be_disabled"
        )


def _parse_utc(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise CryptoTenSymbolFactorPrescreenError("prescreen_timestamp_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CryptoTenSymbolFactorPrescreenError(
            "prescreen_timestamp_invalid"
        ) from exc
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() is None
        or parsed.utcoffset() != timedelta(0)
    ):
        raise CryptoTenSymbolFactorPrescreenError("prescreen_timestamp_invalid")
    return parsed.astimezone(timezone.utc)


def _aligned_slot(value: datetime) -> datetime:
    if (
        value.minute % 5 != 0
        or value.second != 0
        or value.microsecond != 0
    ):
        raise CryptoTenSymbolFactorPrescreenError("prescreen_slot_invalid")
    return value


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _wire_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _decimal(value: Any, *, positive: bool = False) -> Decimal:
    if not isinstance(value, str):
        raise CryptoTenSymbolFactorPrescreenError("prescreen_decimal_invalid")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise CryptoTenSymbolFactorPrescreenError(
            "prescreen_decimal_invalid"
        ) from exc
    if not parsed.is_finite() or (positive and parsed <= 0):
        raise CryptoTenSymbolFactorPrescreenError("prescreen_decimal_invalid")
    return parsed


def _text(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _median(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    count = len(ordered)
    middle = count // 2
    if count % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / Decimal("2")


# ---------------------------------------------------------------------------
# Typed bars and row validation
# ---------------------------------------------------------------------------


class _Bars(dict):
    """dict subclass marker for validated typed-bar dictionaries."""


def _validate_history_rows(
    rows: Sequence[Mapping[str, Any]], *, symbol: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Validate raw history rows; record gaps without ever filling them."""

    validated: list[dict[str, Any]] = []
    previous_open: datetime | None = None
    gaps: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != set(BAR_FIELDS):
            raise CryptoTenSymbolFactorPrescreenError("prescreen_row_shape_invalid")
        if row.get("symbol") != symbol:
            raise CryptoTenSymbolFactorPrescreenError("prescreen_row_shape_invalid")
        open_time = _aligned_slot(_parse_utc(row.get("open_time")))
        close_time = _parse_utc(row.get("close_time"))
        if close_time != open_time + FIVE_MINUTES - timedelta(milliseconds=1):
            raise CryptoTenSymbolFactorPrescreenError(
                "prescreen_bar_continuity_invalid"
            )
        open_price = _decimal(row.get("open"), positive=True)
        high = _decimal(row.get("high"), positive=True)
        low = _decimal(row.get("low"), positive=True)
        close = _decimal(row.get("close"), positive=True)
        volume = _decimal(row.get("volume"))
        quote_volume = _decimal(row.get("quote_volume"))
        if volume < 0 or quote_volume < 0:
            raise CryptoTenSymbolFactorPrescreenError("prescreen_decimal_invalid")
        trade_count = row.get("trade_count")
        if not isinstance(trade_count, int) or trade_count < 0:
            raise CryptoTenSymbolFactorPrescreenError(
                "prescreen_trade_count_invalid"
            )
        if low > min(open_price, close) or high < max(open_price, close) or low > high:
            raise CryptoTenSymbolFactorPrescreenError("prescreen_ohlc_invalid")
        if previous_open is not None:
            if open_time <= previous_open:
                raise CryptoTenSymbolFactorPrescreenError(
                    "prescreen_rows_not_increasing"
                )
            missing = int((open_time - previous_open) / FIVE_MINUTES) - 1
            if missing > 0:
                gaps.append(
                    {
                        "from_open_time": _iso(previous_open),
                        "to_open_time": _iso(open_time),
                        "missing_bars": missing,
                    }
                )
        previous_open = open_time
        validated.append(
            {
                "open_time": open_time,
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
                "quote_volume": quote_volume,
            }
        )
    return validated, gaps


# ---------------------------------------------------------------------------
# Fetch (explicitly injected loopback transport only)
# ---------------------------------------------------------------------------


def _fetch_symbol_rows(
    client: SharedSignalsV1Client,
    *,
    catalog: CatalogEnvelope,
    dataset_id: str,
    symbol: str,
    start_open_time: datetime,
    end_open_time: datetime,
    max_windows: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Bounded fixed-window history pull; every budget breach fails closed.

    Each window covers at most ``PAGE_LIMIT`` consecutive 5-minute opens, so
    a complete single-page read is always possible: a returned cursor, a row
    outside the window, a duplicate identity or any budget breach aborts the
    fetch instead of silently truncating.  Windows advance by time, never by
    row count, so sparse gaps cannot stall or skip the traversal.
    """

    _verify_catalog(catalog, dataset_id)
    rows: list[dict[str, Any]] = []
    receipts: list[dict[str, Any]] = []
    lower = start_open_time
    for _ in range(max_windows):
        upper = min(lower + (PAGE_LIMIT - 1) * FIVE_MINUTES, end_open_time)
        try:
            run = collect_query_pages(
                client=client,
                request=QueryRequest(
                    dataset_id=dataset_id,
                    schema_major=1,
                    fields=BAR_FIELDS,
                    filters={
                        "symbol": {"eq": symbol},
                        "open_time": {
                            "between": [_wire_iso(lower), _wire_iso(upper)]
                        },
                    },
                    order=("symbol:asc", "open_time:asc"),
                    limit=PAGE_LIMIT,
                ),
                identity_fields=("symbol", "open_time"),
                max_pages=1,
                max_rows=PAGE_LIMIT,
            )
        except (PaginationContractError, SharedSignalsV1Error) as exc:
            raise CryptoTenSymbolFactorPrescreenError(
                "prescreen_fetch_query_invalid"
            ) from exc
        envelope = run.envelope
        metadata = envelope.metadata
        if (
            metadata.state.lower() != "ready"
            or metadata.degraded is not False
            or metadata.quality.get("state") != "valid"
            or not isinstance(metadata.receipt_id, str)
            or not metadata.receipt_id
        ):
            raise CryptoTenSymbolFactorPrescreenError(
                "prescreen_fetch_metadata_invalid"
            )
        page = [dict(row) for row in envelope.data]
        for row in page:
            open_time = _parse_utc(row.get("open_time"))
            if open_time < lower or open_time > upper:
                raise CryptoTenSymbolFactorPrescreenError(
                    "prescreen_fetch_window_overflow"
                )
        receipts.append(
            {
                "receipt_id": metadata.receipt_id,
                "data_through": metadata.data_through,
                "observed_at": metadata.observed_at,
                "window_start_open_time": _iso(lower),
                "window_end_open_time": _iso(upper),
                "row_count": len(page),
            }
        )
        rows.extend(page)
        if upper >= end_open_time:
            break
        lower = upper + FIVE_MINUTES
    else:
        raise CryptoTenSymbolFactorPrescreenError(
            "prescreen_fetch_window_budget_exceeded"
        )
    if not rows:
        raise CryptoTenSymbolFactorPrescreenError("prescreen_fetch_empty_history")
    return rows, receipts


def _raw_payload(
    *,
    symbol: str,
    dataset_id: str,
    catalog_version: str,
    start_open_time: datetime,
    end_open_time: datetime,
    rows: Sequence[Mapping[str, Any]],
    receipts: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    # Validate first, then persist the exact validated wire rows so the raw
    # file is a complete, re-checkable record of the pull.
    validated, gaps = _validate_history_rows(rows, symbol=symbol)
    wire_rows = [dict(row) for row in rows]
    payload: dict[str, Any] = {
        "contract": RAW_CONTRACT,
        "symbol": symbol,
        "dataset_id": dataset_id,
        "catalog_version": catalog_version,
        "query": {
            "start_open_time": _iso(start_open_time),
            "end_open_time": _iso(end_open_time),
        },
        "row_count": len(validated),
        "first_open_time": _iso(validated[0]["open_time"]),
        "last_open_time": _iso(validated[-1]["open_time"]),
        "gaps": gaps,
        "receipts": list(receipts),
        "rows_sha256": _sha256(wire_rows),
        "rows": wire_rows,
        **_non_evidence_fields(),
    }
    return payload


def _write_file_atomic(path: Path, encoded: bytes) -> None:
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    descriptor: int | None = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary, flags, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = None
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as exc:
        raise CryptoTenSymbolFactorPrescreenError(
            "prescreen_raw_write_failed"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _dataset_id(symbol: str) -> str:
    return f"crypto.spot.binance.{symbol.lower()}.5m"


def fetch_raw_history(
    *,
    client: SharedSignalsV1Client,
    raw_dir: Path | str,
    start_open_time: datetime,
    end_open_time: datetime,
    symbols: Sequence[str] = OBSERVATION_SYMBOLS,
    max_windows: int = MAX_FETCH_WINDOWS,
) -> dict[str, Any]:
    """Pull every symbol's available history and persist canonical raw files."""

    _assert_simulation_only()
    if not isinstance(client, SharedSignalsV1Client):
        raise CryptoTenSymbolFactorPrescreenError("prescreen_client_invalid")
    start = _aligned_slot(start_open_time)
    end = _aligned_slot(end_open_time)
    if end < start:
        raise CryptoTenSymbolFactorPrescreenError("prescreen_window_invalid")
    if tuple(symbols) != OBSERVATION_SYMBOLS:
        raise CryptoTenSymbolFactorPrescreenError("prescreen_symbols_invalid")
    directory = Path(raw_dir)
    if directory.exists() and (directory.is_symlink() or not directory.is_dir()):
        raise CryptoTenSymbolFactorPrescreenError("prescreen_raw_dir_invalid")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    catalog = client.get_catalog()
    summaries: list[dict[str, Any]] = []
    for symbol in symbols:
        dataset_id = _dataset_id(symbol)
        rows, receipts = _fetch_symbol_rows(
            client,
            catalog=catalog,
            dataset_id=dataset_id,
            symbol=symbol,
            start_open_time=start,
            end_open_time=end,
            max_windows=max_windows,
        )
        payload = _raw_payload(
            symbol=symbol,
            dataset_id=dataset_id,
            catalog_version=catalog.catalog_version,
            start_open_time=start,
            end_open_time=end,
            rows=rows,
            receipts=receipts,
        )
        encoded = (_canonical_json(payload) + "\n").encode("utf-8")
        _write_file_atomic(directory / f"{symbol}.json", encoded)
        summaries.append(
            {
                "symbol": symbol,
                "row_count": payload["row_count"],
                "first_open_time": payload["first_open_time"],
                "last_open_time": payload["last_open_time"],
                "gap_count": len(payload["gaps"]),
                "window_count": len(receipts),
            }
        )
    return {
        "contract": PRESCREEN_CONTRACT,
        "event_type": "fetch_summary",
        "raw_dir": str(directory),
        "catalog_version": catalog.catalog_version,
        "network_used": True,
        "datasets": summaries,
        **_non_evidence_fields(),
    }


# ---------------------------------------------------------------------------
# Raw loading (offline)
# ---------------------------------------------------------------------------


def _read_raw_file(path: Path, *, symbol: str) -> dict[str, Any]:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise CryptoTenSymbolFactorPrescreenError("prescreen_raw_file_invalid") from exc
    try:
        metadata = os.fstat(descriptor)
        node = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size <= 0
            or metadata.st_size > MAX_RAW_BYTES
            or node.st_dev != metadata.st_dev
            or node.st_ino != metadata.st_ino
        ):
            raise CryptoTenSymbolFactorPrescreenError("prescreen_raw_file_invalid")
        encoded = os.read(descriptor, metadata.st_size)
        after = os.fstat(descriptor)
        if len(encoded) != metadata.st_size or after.st_size != metadata.st_size:
            raise CryptoTenSymbolFactorPrescreenError("prescreen_raw_file_invalid")
    except CryptoTenSymbolFactorPrescreenError:
        raise
    except OSError as exc:
        raise CryptoTenSymbolFactorPrescreenError("prescreen_raw_file_invalid") from exc
    finally:
        os.close(descriptor)
    if not encoded.endswith(b"\n") or b"\x00" in encoded:
        raise CryptoTenSymbolFactorPrescreenError("prescreen_raw_file_invalid")
    try:
        payload = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CryptoTenSymbolFactorPrescreenError(
            "prescreen_raw_file_invalid"
        ) from exc
    if not isinstance(payload, dict):
        raise CryptoTenSymbolFactorPrescreenError("prescreen_raw_file_invalid")
    if (
        payload.get("contract") != RAW_CONTRACT
        or payload.get("symbol") != symbol
        or payload.get("dataset_id") != _dataset_id(symbol)
    ):
        raise CryptoTenSymbolFactorPrescreenError("prescreen_raw_contract_invalid")
    for key, expected in _non_evidence_fields().items():
        if payload.get(key) != expected:
            raise CryptoTenSymbolFactorPrescreenError(
                "prescreen_raw_authority_invalid"
            )
    rows = payload.get("rows")
    if not isinstance(rows, list) or not rows:
        raise CryptoTenSymbolFactorPrescreenError("prescreen_raw_rows_invalid")
    validated, gaps = _validate_history_rows(rows, symbol=symbol)
    wire_rows = [dict(row) for row in rows]
    if _sha256(wire_rows) != payload.get("rows_sha256") or gaps != payload.get(
        "gaps"
    ):
        raise CryptoTenSymbolFactorPrescreenError("prescreen_raw_rows_invalid")
    return {"payload": payload, "rows": validated}


def load_raw_dir(
    raw_dir: Path | str,
    *,
    expected_symbols: Sequence[str] = OBSERVATION_SYMBOLS,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    directory = Path(raw_dir)
    if not directory.is_dir() or directory.is_symlink():
        raise CryptoTenSymbolFactorPrescreenError("prescreen_raw_dir_invalid")
    rows_by_symbol: dict[str, list[dict[str, Any]]] = {}
    meta: dict[str, Any] = {}
    for symbol in expected_symbols:
        loaded = _read_raw_file(directory / f"{symbol}.json", symbol=symbol)
        rows_by_symbol[symbol] = loaded["rows"]
        payload = loaded["payload"]
        meta[symbol] = {
            "row_count": len(loaded["rows"]),
            "first_open_time": _iso(loaded["rows"][0]["open_time"]),
            "last_open_time": _iso(loaded["rows"][-1]["open_time"]),
            "gap_count": len(payload["gaps"]),
            "catalog_version": payload["catalog_version"],
        }
    return rows_by_symbol, meta


# ---------------------------------------------------------------------------
# Evaluation primitives (same cost model as the evidence chain)
# ---------------------------------------------------------------------------

_FEE = TAKER_FEE_RATE
_SLIP = SLIPPAGE_BPS / Decimal("10000")


def _cost_adjusted_return(entry: Decimal, exit_: Decimal) -> Decimal:
    net = exit_ * (Decimal("1") - _FEE) / (entry * (Decimal("1") + _FEE)) - Decimal(
        "1"
    )
    return (Decimal("1") + net) * (Decimal("1") - _SLIP) ** 2 - Decimal("1")


def _features(bars: Sequence[Mapping[str, Any]]) -> dict[str, Decimal]:
    """Features of one exact 13-bar window ending at the evaluation slot."""

    closes = [bar["close"] for bar in bars]
    returns = [
        current / previous - Decimal("1")
        for previous, current in zip(closes, closes[1:])
    ]
    quote_volume_1h = sum((bar["quote_volume"] for bar in bars[-12:]), Decimal("0"))
    return_1h = closes[-1] / closes[-13] - Decimal("1")
    return_15m = closes[-1] / closes[-4] - Decimal("1")
    volatility = (
        (sum(value * value for value in returns) / Decimal(len(returns))).sqrt()
    )
    amihud = (
        abs(return_1h) / quote_volume_1h if quote_volume_1h > 0 else None
    )
    return {
        "return_1h": return_1h,
        "return_15m": return_15m,
        "realized_volatility_1h": volatility,
        "quote_volume_1h": quote_volume_1h,
        "amihud_illiquidity": amihud,
    }


def _symbol_evaluation_rows(
    bars_by_symbol: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, dict[datetime, dict[str, Any]]]:
    """Per symbol, map slot -> {features, forward_return} where fully proven."""

    result: dict[str, dict[datetime, dict[str, Any]]] = {}
    for symbol, bars in bars_by_symbol.items():
        by_open = {bar["open_time"]: bar for bar in bars}
        ordered = sorted(by_open)
        rows: dict[datetime, dict[str, Any]] = {}
        for index in range(12, len(ordered)):
            slot = ordered[index]
            window_opens = ordered[index - 12 : index + 1]
            if window_opens[-1] != slot:
                continue
            if any(
                later - earlier != FIVE_MINUTES
                for earlier, later in zip(window_opens, window_opens[1:])
            ):
                continue
            future_open = slot + HORIZON_BARS * FIVE_MINUTES
            future = by_open.get(future_open)
            if future is None:
                continue
            window = [by_open[open_time] for open_time in window_opens]
            rows[slot] = {
                "features": _features(window),
                "entry": by_open[slot]["close"],
                "exit": future["close"],
                "forward_return": _cost_adjusted_return(
                    by_open[slot]["close"], future["close"]
                ),
            }
        result[symbol] = rows
    return result


_METRIC_BASIS = (
    "cost model identical to the evidence chain (fee 0.001 each side plus"
    " 2bps slippage each side); equal-weight equity curve per 5m slot;"
    " overlapping 1h labels across 5m slots inflate the effective sample"
    " size about 12x; non_overlapping keeps every 12th slot; historical"
    " backfill without PIT proof; not promotion evidence"
)


def _sample_metrics(
    samples: Sequence[tuple[datetime, Decimal]],
    *,
    universe_count: int,
    baseline_mean: Decimal | None,
    universe_slots: Sequence[datetime],
    stride: int = 1,
) -> dict[str, Any]:
    selected = list(samples)
    returns = [value for _, value in selected]
    count = len(returns)
    mean = sum(returns, Decimal("0")) / Decimal(count) if returns else None
    slot_returns: dict[datetime, list[Decimal]] = {}
    for slot, value in selected:
        slot_returns.setdefault(slot, []).append(value)
    equity = Decimal("1")
    peak = equity
    maximum_drawdown = Decimal("0")
    for slot in sorted(slot_returns):
        equity *= Decimal("1") + sum(slot_returns[slot], Decimal("0")) / Decimal(
            len(slot_returns[slot])
        )
        peak = max(peak, equity)
        if peak:
            maximum_drawdown = max(maximum_drawdown, (peak - equity) / peak)
    slots = sorted(universe_slots)
    kept_slots = set(slots[::stride])
    kept = [value for slot, value in selected if slot in kept_slots]
    kept_mean = sum(kept, Decimal("0")) / Decimal(len(kept)) if kept else None
    metrics = {
        "universe_count": universe_count,
        "signal_count": count,
        "coverage": _text(Decimal(count) / Decimal(universe_count))
        if universe_count
        else "0",
        "hit_rate": _text(
            Decimal(sum(value > 0 for value in returns)) / Decimal(count)
        )
        if returns
        else None,
        "mean_net": _text(mean),
        "median_net": _text(_median(returns)),
        "baseline_delta": _text(mean - baseline_mean)
        if mean is not None and baseline_mean is not None
        else None,
        "cash_delta": _text(mean),
        "max_drawdown": _text(maximum_drawdown),
        "turnover": _text(Decimal(count) / Decimal(universe_count))
        if universe_count
        else "0",
        "metric_basis": _METRIC_BASIS,
    }
    if stride > 1:
        metrics["non_overlapping"] = {
            "stride": stride,
            "slot_count": len(kept_slots),
            "signal_count": len(kept),
            "hit_rate": _text(
                Decimal(sum(value > 0 for value in kept)) / Decimal(len(kept))
            )
            if kept
            else None,
            "mean_net": _text(kept_mean),
            "median_net": _text(_median(kept)),
            "baseline_delta": None,
        }
    return metrics


def _baseline_delta_subset(
    selected_mean: Decimal | None,
    baseline_returns: Sequence[tuple[datetime, Decimal]],
    kept_slots: set[datetime],
) -> Decimal | None:
    kept = [value for slot, value in baseline_returns if slot in kept_slots]
    if selected_mean is None or not kept:
        return None
    baseline_mean = sum(kept, Decimal("0")) / Decimal(len(kept))
    return selected_mean - baseline_mean


def _signal_snapshot(features: Mapping[str, Decimal]) -> dict[str, Any]:
    return {
        "features": {
            "return_15m": format(features["return_15m"], "f"),
            "return_1h": format(features["return_1h"], "f"),
            "base_volume_zscore_1h": "0",
        }
    }


def _per_symbol_breakdown(
    rows: Sequence[tuple[str, datetime, bool, Decimal]],
) -> dict[str, Any]:
    breakdown: dict[str, Any] = {}
    for symbol, _slot, signaled, value in rows:
        entry = breakdown.setdefault(
            symbol, {"signal_count": 0, "returns": [], "positive": 0}
        )
        if signaled:
            entry["signal_count"] += 1
            entry["returns"].append(value)
            if value > 0:
                entry["positive"] += 1
    result: dict[str, Any] = {}
    for symbol, entry in breakdown.items():
        returns = entry["returns"]
        count = len(returns)
        result[symbol] = {
            "signal_count": entry["signal_count"],
            "hit_rate": _text(Decimal(entry["positive"]) / Decimal(count))
            if count
            else None,
            "mean_net": _text(
                sum(returns, Decimal("0")) / Decimal(count) if count else None
            ),
        }
    return result


# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------


def _evaluate_xs_rs(
    universe: Mapping[str, Mapping[datetime, Mapping[str, Any]]],
    *,
    symbols: Sequence[str],
) -> dict[str, Any]:
    slots = sorted(
        slot
        for slot in universe[symbols[0]]
        if all(slot in universe[symbol] for symbol in symbols)
    )
    variants: dict[str, Any] = {}
    inclusion: dict[str, dict[str, Any]] = {
        symbol: {"inclusion_count": 0, "returns": []} for symbol in symbols
    }
    for top_k in (1, 2, 3):
        k = min(top_k, len(symbols))
        portfolio: list[tuple[datetime, Decimal]] = []
        baseline: list[tuple[datetime, Decimal]] = []
        for slot in slots:
            ranked = sorted(
                symbols,
                key=lambda symbol: (
                    -universe[symbol][slot]["features"]["return_1h"],
                    symbols.index(symbol),
                ),
            )
            chosen = ranked[:k]
            value = sum(
                (universe[symbol][slot]["forward_return"] for symbol in chosen),
                Decimal("0"),
            ) / Decimal(k)
            portfolio.append((slot, value))
            baseline.append(
                (
                    slot,
                    sum(
                        (
                            universe[symbol][slot]["forward_return"]
                            for symbol in symbols
                        ),
                        Decimal("0"),
                    )
                    / Decimal(len(symbols)),
                )
            )
            if top_k == 2:
                for symbol in chosen:
                    inclusion[symbol]["inclusion_count"] += 1
                    inclusion[symbol]["returns"].append(
                        universe[symbol][slot]["forward_return"]
                    )
        baseline_mean = (
            sum((value for _, value in baseline), Decimal("0"))
            / Decimal(len(baseline))
            if baseline
            else None
        )
        metrics = _sample_metrics(
            portfolio,
            universe_count=len(slots),
            baseline_mean=baseline_mean,
            universe_slots=slots,
            stride=NON_OVERLAP_STRIDE,
        )
        subset = metrics["non_overlapping"]
        kept_slots = set(sorted(slot for slot, _ in portfolio)[::NON_OVERLAP_STRIDE])
        subset["baseline_delta"] = _text(
            _baseline_delta_subset(
                Decimal(metrics["non_overlapping"]["mean_net"])
                if metrics["non_overlapping"]["mean_net"] is not None
                else None,
                baseline,
                kept_slots,
            )
        )
        metrics["non_overlapping"] = subset
        variants[f"top_{top_k}"] = metrics
    per_symbol = {
        "variant": "top_2",
        "symbols": {
            symbol: {
                "inclusion_count": inclusion[symbol]["inclusion_count"],
                "inclusion_rate": _text(
                    Decimal(inclusion[symbol]["inclusion_count"])
                    / Decimal(len(slots))
                )
                if slots
                else "0",
                "mean_net_when_included": _text(
                    sum(inclusion[symbol]["returns"], Decimal("0"))
                    / Decimal(len(inclusion[symbol]["returns"]))
                    if inclusion[symbol]["returns"]
                    else None
                ),
            }
            for symbol in symbols
        },
    }
    return {
        "candidate_id": "xs_rs",
        "hypothesis": (
            "cross-sectional relative strength: rank symbols by 1h return each"
            " slot, long top-k equal weight, always in market"
        ),
        "evaluation_slots": len(slots),
        "variants": variants,
        "per_symbol": per_symbol,
    }


def _evaluate_short_reversal(
    universe: Mapping[str, Mapping[datetime, Mapping[str, Any]]],
    *,
    symbols: Sequence[str],
) -> dict[str, Any]:
    universe_rows: list[tuple[str, datetime, Decimal]] = []
    strict_rows: list[tuple[str, datetime, bool, Decimal]] = []
    naive_rows: list[tuple[str, datetime, bool, Decimal]] = []
    for symbol in symbols:
        for slot in sorted(universe[symbol]):
            row = universe[symbol][slot]
            features = row["features"]
            universe_rows.append((symbol, slot, row["forward_return"]))
            strict = (
                features["return_1h"] <= Decimal("-0.003")
                and features["return_15m"] > Decimal("-0.001")
            )
            naive = features["return_1h"] < 0
            strict_rows.append((symbol, slot, strict, row["forward_return"]))
            naive_rows.append((symbol, slot, naive, row["forward_return"]))
    universe_count = len(universe_rows)
    universe_slots = sorted({slot for _, slot, _ in universe_rows})
    baseline_mean = (
        sum((value for _, _, value in universe_rows), Decimal("0"))
        / Decimal(universe_count)
        if universe_count
        else None
    )
    variants: dict[str, Any] = {}
    for variant, rows in (("strict", strict_rows), ("naive", naive_rows)):
        selected = [(slot, value) for _, slot, signaled, value in rows if signaled]
        metrics = _sample_metrics(
            selected,
            universe_count=universe_count,
            baseline_mean=baseline_mean,
            universe_slots=universe_slots,
            stride=NON_OVERLAP_STRIDE,
        )
        variants[variant] = metrics
    return {
        "candidate_id": "short_reversal",
        "hypothesis": (
            "per-symbol short-term reversal: long when 1h return <= -0.3% and"
            " 15m return > -0.1% (oversold stabilizing); naive variant is"
            " 1h return < 0"
        ),
        "variants": variants,
        "per_symbol": {
            "strict": _per_symbol_breakdown(strict_rows),
            "naive": _per_symbol_breakdown(naive_rows),
        },
    }


def _evaluate_amihud(
    universe: Mapping[str, Mapping[datetime, Mapping[str, Any]]],
    *,
    symbols: Sequence[str],
) -> dict[str, Any]:
    slots = sorted(
        slot
        for slot in universe[symbols[0]]
        if all(slot in universe[symbol] for symbol in symbols)
    )
    portfolio: list[tuple[datetime, Decimal]] = []
    baseline: list[tuple[datetime, Decimal]] = []
    inclusion: dict[str, dict[str, Any]] = {
        symbol: {"inclusion_count": 0, "returns": []} for symbol in symbols
    }
    skipped = 0
    for slot in slots:
        if any(
            universe[symbol][slot]["features"]["amihud_illiquidity"] is None
            for symbol in symbols
        ):
            skipped += 1
            continue
        ranked = sorted(
            symbols,
            key=lambda symbol: (
                -universe[symbol][slot]["features"]["amihud_illiquidity"],
                symbols.index(symbol),
            ),
        )
        chosen = ranked[: min(2, len(symbols))]
        value = sum(
            (universe[symbol][slot]["forward_return"] for symbol in chosen),
            Decimal("0"),
        ) / Decimal(len(chosen))
        portfolio.append((slot, value))
        baseline.append(
            (
                slot,
                sum(
                    (
                        universe[symbol][slot]["forward_return"]
                        for symbol in symbols
                    ),
                    Decimal("0"),
                )
                / Decimal(len(symbols)),
            )
        )
        for symbol in chosen:
            inclusion[symbol]["inclusion_count"] += 1
            inclusion[symbol]["returns"].append(
                universe[symbol][slot]["forward_return"]
            )
    baseline_mean = (
        sum((value for _, value in baseline), Decimal("0")) / Decimal(len(baseline))
        if baseline
        else None
    )
    metrics = _sample_metrics(
        portfolio,
        universe_count=len(portfolio),
        baseline_mean=baseline_mean,
        universe_slots=[slot for slot, _ in portfolio],
        stride=NON_OVERLAP_STRIDE,
    )
    kept_slots = set(sorted(slot for slot, _ in portfolio)[::NON_OVERLAP_STRIDE])
    metrics["non_overlapping"]["baseline_delta"] = _text(
        _baseline_delta_subset(
            Decimal(metrics["non_overlapping"]["mean_net"])
            if metrics["non_overlapping"]["mean_net"] is not None
            else None,
            baseline,
            kept_slots,
        )
    )
    return {
        "candidate_id": "amihud_illiquidity",
        "hypothesis": (
            "Amihud illiquidity: rank symbols by |1h return| / 1h quote_volume"
            " each slot, long top-2 most illiquid equal weight"
        ),
        "evaluation_slots": len(portfolio),
        "skipped_slots_zero_quote_volume": skipped,
        "variants": {"top_2": metrics},
        "per_symbol": {
            symbol: {
                "inclusion_count": inclusion[symbol]["inclusion_count"],
                "mean_net_when_included": _text(
                    sum(inclusion[symbol]["returns"], Decimal("0"))
                    / Decimal(len(inclusion[symbol]["returns"]))
                    if inclusion[symbol]["returns"]
                    else None
                ),
            }
            for symbol in symbols
        },
    }


def _evaluate_momentum_vol_regime(
    universe: Mapping[str, Mapping[datetime, Mapping[str, Any]]],
    *,
    symbols: Sequence[str],
) -> dict[str, Any]:
    rows: list[tuple[str, datetime, Decimal, Decimal, bool]] = []
    for symbol in symbols:
        for slot in sorted(universe[symbol]):
            row = universe[symbol][slot]
            features = row["features"]
            signaled = _signal("time_series_momentum_v1", _signal_snapshot(features))
            rows.append(
                (
                    symbol,
                    slot,
                    features["realized_volatility_1h"],
                    row["forward_return"],
                    signaled,
                )
            )
    vols = sorted(row[2] for row in rows)
    median_vol = _median(vols)
    universe_count = len(rows)
    universe_slots = sorted({row[1] for row in rows})
    baseline_mean = (
        sum((row[3] for row in rows), Decimal("0")) / Decimal(universe_count)
        if universe_count
        else None
    )
    variants: dict[str, Any] = {}
    for variant, predicate in (
        ("high_vol_half", lambda vol: median_vol is not None and vol >= median_vol),
        ("low_vol_half", lambda vol: median_vol is not None and vol < median_vol),
    ):
        half = [row for row in rows if predicate(row[2])]
        selected = [(row[1], row[3]) for row in half if row[4]]
        metrics = _sample_metrics(
            selected,
            universe_count=len(half),
            baseline_mean=baseline_mean,
            universe_slots=universe_slots,
            stride=NON_OVERLAP_STRIDE,
        )
        variants[variant] = metrics
    return {
        "candidate_id": "momentum_vol_regime",
        "hypothesis": (
            "volatility regime modifier: evaluate the pre-registered"
            " time_series_momentum_v1 signal (1h >= 0 and 15m >= 0.001)"
            " separately on the high and low realized-1h-volatility halves"
        ),
        "median_realized_volatility_1h": _text(median_vol),
        "variants": variants,
    }


def analyze(
    rows_by_symbol: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate all four A-class candidates over validated typed bars."""

    symbols = tuple(rows_by_symbol.keys())
    if not symbols:
        raise CryptoTenSymbolFactorPrescreenError("prescreen_symbols_invalid")
    universe = _symbol_evaluation_rows(rows_by_symbol)
    data_window = (
        dict(meta)
        if meta is not None
        else {
            symbol: {
                "row_count": len(bars),
                "first_open_time": _iso(bars[0]["open_time"]),
                "last_open_time": _iso(bars[-1]["open_time"]),
            }
            for symbol, bars in rows_by_symbol.items()
        }
    )
    return {
        "contract": PRESCREEN_CONTRACT,
        "event_type": "factor_prescreen_analysis",
        "symbols": list(symbols),
        "data_window": data_window,
        "cost_policy": {
            "cost_policy_id": "crypto-round-trip-taker-v1",
            "fee_rate": format(TAKER_FEE_RATE, "f"),
            "slippage_bps_each_side": format(SLIPPAGE_BPS, "f"),
        },
        "forward_horizon_bars": HORIZON_BARS,
        "non_overlap_stride": NON_OVERLAP_STRIDE,
        "candidates": [
            _evaluate_xs_rs(universe, symbols=symbols),
            _evaluate_short_reversal(universe, symbols=symbols),
            _evaluate_amihud(universe, symbols=symbols),
            _evaluate_momentum_vol_regime(universe, symbols=symbols),
        ],
        **_non_evidence_fields(),
    }


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------


def _fmt(value: Any, *, digits: int = 6) -> str:
    if value is None:
        return "—"
    if isinstance(value, (int, str)):
        return str(value)
    return str(value)


def _metrics_row(name: str, metrics: Mapping[str, Any]) -> str:
    subset = metrics.get("non_overlapping") or {}
    return (
        f"| {name} | {metrics.get('signal_count')} / {metrics.get('universe_count')}"
        f" | {_fmt(metrics.get('hit_rate'))} | {_fmt(metrics.get('mean_net'))}"
        f" | {_fmt(metrics.get('median_net'))} | {_fmt(metrics.get('baseline_delta'))}"
        f" | {_fmt(metrics.get('cash_delta'))} | {_fmt(metrics.get('max_drawdown'))}"
        f" | {_fmt(metrics.get('turnover'))}"
        f" | {subset.get('signal_count', '—')} / {subset.get('slot_count', '—')}"
        f" | {_fmt(subset.get('mean_net'))} |"
    )


def _per_symbol_table(lines: list[str], table: Mapping[str, Any]) -> None:
    lines += [
        "",
        "| symbol | 信号/入选次数 | hit_rate | mean_net |",
        "|---|---|---|---|",
    ]
    for symbol, item in table.items():
        count = item.get("signal_count", item.get("inclusion_count"))
        mean = item.get("mean_net", item.get("mean_net_when_included"))
        lines.append(
            f"| {symbol} | {count} | {_fmt(item.get('hit_rate'))}"
            f" | {_fmt(mean)} |"
        )


def render_report(result: Mapping[str, Any]) -> str:
    """Render the markdown research report from one analysis result."""

    candidates = {
        candidate["candidate_id"]: candidate
        for candidate in result["candidates"]
    }
    window = result["data_window"]
    lines = [
        "# Crypto 10 币 5m A 类候选因子预筛（非证据研究）",
        "",
        "> **非证据声明**：本报告全部数字来自无 PIT 证明的 TradingDatas 历史回填"
        "数据（`historical_backfill_no_pit=true`），仅供工程/定义检查"
        "（`not_promotion_evidence=true`、`authority=none`），"
        "**不得进入任何晋级证据**，不构成 edge、概率校准或参数变更授权。",
        "",
        "## 方法",
        "",
        "- 数据：10 币（固定 `market_observation.OBSERVATION_SYMBOLS` 顺序）5m"
        " OHLCV，经 catalog/query 契约拉取并落盘为 canonical raw JSON（含"
        " receipt/data_through/observed_at 汇总）；行校验 UTC、OHLC 一致、"
        "Decimal；5 分钟缺口只记录不填补。",
        "- 标签：forward 1h（12 槽）close→close；成本与证据链同一口径：fee"
        " 0.001 双边 + slippage 2bps 双边，"
        "`(1+net)*(1-slip)^2-1`。",
        "- 口径：每个候选同时报全样本与**非重叠子样本**（每 12 槽取 1，"
        f"stride={result['non_overlap_stride']}），重叠标签对样本量的虚增在"
        "结果表中直接对比。",
        "- 指标：signal/universe、hit_rate、mean/median net、vs always-invest"
        " 基线、vs cash、等权权益曲线 max drawdown、turnover；per-symbol"
        " 分解见各候选小节。",
        "",
        "## 数据窗口",
        "",
        "| symbol | rows | first_open_time | last_open_time | gaps |",
        "|---|---|---|---|---|",
    ]
    for symbol in result["symbols"]:
        item = window[symbol]
        lines.append(
            f"| {symbol} | {item['row_count']} | {item['first_open_time']}"
            f" | {item['last_open_time']} | {item.get('gap_count', 0)} |"
        )
    header = (
        "| variant | signal/universe | hit_rate | mean_net | median_net"
        " | Δ baseline | Δ cash | maxDD | turnover"
        " | 非重叠 signal/slots | 非重叠 mean |"
    )
    divider = "|---|---|---|---|---|---|---|---|---|---|---|"
    for candidate_id, title in (
        ("xs_rs", "XS-RS 横截面相对强弱（long top-k 等权，永远在场）"),
        ("short_reversal", "短期反转（per-symbol 超跌企稳做多）"),
        ("amihud_illiquidity", "Amihud 非流动性（long top-2 高非流动性等权）"),
        (
            "momentum_vol_regime",
            "波动率 regime 修饰（time_series_momentum_v1 分高/低波动两半）",
        ),
    ):
        candidate = candidates[candidate_id]
        lines += [
            "",
            f"## 候选：{title}",
            "",
            f"假设：{candidate['hypothesis']}",
            "",
            header,
            divider,
        ]
        for variant, metrics in candidate["variants"].items():
            lines.append(_metrics_row(variant, metrics))
        if candidate_id == "momentum_vol_regime":
            lines += [
                "",
                f"median realized_volatility_1h = "
                f"{candidate.get('median_realized_volatility_1h')}",
            ]
        per_symbol = candidate.get("per_symbol")
        if isinstance(per_symbol, Mapping) and "symbols" in per_symbol:
            _per_symbol_table(lines, per_symbol["symbols"])
        elif isinstance(per_symbol, Mapping) and per_symbol:
            first = next(iter(per_symbol.values()))
            if isinstance(first, Mapping) and (
                "signal_count" in first or "inclusion_count" in first
            ):
                _per_symbol_table(lines, per_symbol)
            else:
                for variant, table in per_symbol.items():
                    lines += ["", f"per-symbol（{variant}）："]
                    _per_symbol_table(lines, table)
    lines += [
        "",
        "## 机器产物与人工结论边界",
        "",
        "以下机器产物只包含上面的分析结果；其 canonical JSON 内容和 SHA-256",
        "由当前 renderer 从同一冻结 raw cohort 生成。人工判断不进入机器产物，",
        "因此可以在不改变可复算数字的情况下单独修订。",
        "",
        f"- artifact contract: `{MACHINE_ARTIFACT_CONTRACT}`",
        f"- canonical JSON content sha256: `{machine_artifact_sha256(result)}`",
        "- exact content: 运行 `--artifact <path>` 写出，文件字节（含末尾换行）",
        "  即为上述 hash 的输入。",
        "",
        "## 结论与预注册建议",
        "",
        "（人工判断：哪些候选值得正式预注册进证据链。注意全部结论仅基于"
        "非重叠子样本口径也成立时才考虑预注册；本报告不构成任何晋级证据。）",
        "",
        "---",
        "",
        f"生成：`Crypto/ten_symbol_factor_prescreen.py --report`；contract "
        f"`{result['contract']}`；cost policy "
        f"`{result['cost_policy']['cost_policy_id']}`。",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_fetch_client(
    *,
    base_url: str,
    token_file: Path,
    catalog_version: str,
    access_policy_id: str,
) -> SharedSignalsV1Client:
    try:
        transport = build_runtime_transport(
            "http-json-v1",
            token_file=token_file,
            base_url=base_url,
        )
    except RuntimeGateConfigurationError as exc:
        raise CryptoTenSymbolFactorPrescreenError(
            "prescreen_transport_invalid"
        ) from exc
    return SharedSignalsV1Client(
        SharedSignalsV1Config(
            base_url=base_url,
            expected_catalog_version=catalog_version,
            dataset_ids=frozenset(_dataset_id(symbol) for symbol in OBSERVATION_SYMBOLS),
            access_policy_id=access_policy_id,
            catalog_version_policy="strict",
            timeout_seconds=60.0,
            max_limit=PAGE_LIMIT,
            cache_ttl_seconds=0,
        ),
        transport=transport,
    )


def _emit(payload: Mapping[str, Any]) -> None:
    print(
        json.dumps(
            payload,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Offline ten-symbol factor pre-screen research"
    )
    parser.add_argument("--raw-dir", type=Path, required=True)
    parser.add_argument("--fetch", action="store_true")
    parser.add_argument("--base-url", type=str)
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--catalog-version", type=str)
    parser.add_argument("--access-policy-id", type=str)
    parser.add_argument("--start", type=str)
    parser.add_argument("--end", type=str)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--artifact", type=Path)
    args = parser.parse_args(argv)
    try:
        _assert_simulation_only()
        if args.fetch:
            missing = [
                name
                for name, value in (
                    ("--base-url", args.base_url),
                    ("--token-file", args.token_file),
                    ("--catalog-version", args.catalog_version),
                    ("--access-policy-id", args.access_policy_id),
                    ("--start", args.start),
                    ("--end", args.end),
                )
                if not value
            ]
            if missing:
                raise CryptoTenSymbolFactorPrescreenError(
                    "prescreen_fetch_arguments_incomplete"
                )
            client = _build_fetch_client(
                base_url=str(args.base_url),
                token_file=Path(args.token_file),
                catalog_version=str(args.catalog_version),
                access_policy_id=str(args.access_policy_id),
            )
            summary = fetch_raw_history(
                client=client,
                raw_dir=args.raw_dir,
                start_open_time=_aligned_slot(_parse_utc(args.start)),
                end_open_time=_aligned_slot(_parse_utc(args.end)),
            )
            _emit(summary)
            return 0
        rows_by_symbol, meta = load_raw_dir(args.raw_dir)
        result = analyze(rows_by_symbol, meta=meta)
        if args.artifact is not None:
            _write_file_atomic(
                args.artifact,
                render_machine_artifact(result).encode("utf-8"),
            )
        if args.report is not None:
            report_text = render_report(result)
            _write_file_atomic(
                args.report, (report_text).encode("utf-8")
            )
        _emit(result)
        return 0
    except Exception:
        print("crypto ten-symbol factor prescreen failed closed", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "NON_OVERLAP_STRIDE",
    "PAGE_LIMIT",
    "PRESCREEN_CONTRACT",
    "RAW_CONTRACT",
    "CryptoTenSymbolFactorPrescreenError",
    "MACHINE_ARTIFACT_CONTRACT",
    "analyze",
    "fetch_raw_history",
    "load_raw_dir",
    "main",
    "machine_artifact_sha256",
    "render_report",
    "render_machine_artifact",
]
