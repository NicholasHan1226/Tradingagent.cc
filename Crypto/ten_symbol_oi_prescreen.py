"""Offline B-class OI-family factor pre-screen over ten-symbol 5m history.

This module evaluates the three frozen open-interest candidate families
(``oi_change_rate``, ``price_oi_divergence``, ``oi_weighted_momentum``) from
the stage-2 hypothesis generator against historical 5-minute bars plus the
perp open-interest 5m series.

The history is a *backfill without PIT proof*: every artifact this module
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

from Crypto.market_observation import (
    BAR_FIELDS,
    OBSERVATION_SYMBOLS,
    OBSERVATION_SYMBOLS_V40,
    _verify_catalog,
)
from Crypto.round_trip_capital import SLIPPAGE_BPS, TAKER_FEE_RATE
from Crypto.ten_symbol_hypothesis_generator import (
    GENERATION_CONFIG,
    _candidate_lookback_bars,
)
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


OI_PRESCREEN_CONTRACT = "tradingagent.crypto.ten_symbol_oi_prescreen.v1"
OI_RAW_CONTRACT = "tradingagent.crypto.ten_symbol_oi_prescreen_raw.v1"
FIVE_MINUTES = timedelta(minutes=5)
ALLOWED_HORIZON_BARS = (12, 48, 144, 288)
HORIZON_BARS = 12
PAGE_LIMIT = 500
MAX_FETCH_WINDOWS = 400
MAX_RAW_BYTES = 512 * 1024 * 1024
OI_FAMILY_IDS = ("oi_change_rate", "price_oi_divergence", "oi_weighted_momentum")
OI_DATASET_SUFFIX = ".open_interest"
OI_FIELDS = ("symbol", "timestamp", "sum_open_interest", "sum_open_interest_value")
OI_IDENTITY_FIELDS = ("symbol", "timestamp")

_FEE = TAKER_FEE_RATE
_SLIP = SLIPPAGE_BPS / Decimal("10000")


class CryptoTenSymbolOIPrescreenError(RuntimeError):
    """Stable fail-closed error for OI pre-screen fetch or analysis."""


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
        raise CryptoTenSymbolOIPrescreenError(
            "oi_prescreen_payload_not_canonical"
        ) from exc


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


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
        raise CryptoTenSymbolOIPrescreenError(
            "oi_prescreen_real_trading_must_be_disabled"
        )


def _parse_utc(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise CryptoTenSymbolOIPrescreenError("oi_prescreen_timestamp_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CryptoTenSymbolOIPrescreenError(
            "oi_prescreen_timestamp_invalid"
        ) from exc
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() is None
        or parsed.utcoffset() != timedelta(0)
    ):
        raise CryptoTenSymbolOIPrescreenError("oi_prescreen_timestamp_invalid")
    return parsed.astimezone(timezone.utc)


def _aligned_slot(value: datetime) -> datetime:
    if (
        value.minute % 5 != 0
        or value.second != 0
        or value.microsecond != 0
    ):
        raise CryptoTenSymbolOIPrescreenError("oi_prescreen_slot_invalid")
    return value


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _wire_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _decimal(value: Any, *, positive: bool = False) -> Decimal:
    if not isinstance(value, str):
        raise CryptoTenSymbolOIPrescreenError("oi_prescreen_decimal_invalid")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise CryptoTenSymbolOIPrescreenError(
            "oi_prescreen_decimal_invalid"
        ) from exc
    if not parsed.is_finite() or (positive and parsed <= 0):
        raise CryptoTenSymbolOIPrescreenError("oi_prescreen_decimal_invalid")
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


def _bar_dataset_id(symbol: str) -> str:
    return f"crypto.spot.binance.{symbol.lower()}.5m"


def _oi_dataset_id(symbol: str) -> str:
    return f"crypto.perp.binance.{symbol.lower()}{OI_DATASET_SUFFIX}"


# ---------------------------------------------------------------------------
# Frozen OI candidate grid (derived from the stage-2 generation config)
# ---------------------------------------------------------------------------


def _oi_candidates() -> list[dict[str, Any]]:
    families = [
        family
        for family in GENERATION_CONFIG["families"]
        if family["family_id"] in OI_FAMILY_IDS
    ]
    if [family["family_id"] for family in families] != list(OI_FAMILY_IDS):
        raise CryptoTenSymbolOIPrescreenError("oi_prescreen_family_drift")
    candidates: list[dict[str, Any]] = []
    for family in families:
        for entry in family["parameter_sets"]:
            parameters = dict(entry["parameters"])
            candidates.append(
                {
                    "candidate_id": f"{family['family_id']}__{entry['variant']}",
                    "family": family["family_id"],
                    "hypothesis": family["hypothesis_template"].format(**parameters),
                    "parameters": parameters,
                    "lookback": _candidate_lookback_bars(parameters),
                }
            )
    return candidates


# ---------------------------------------------------------------------------
# Row validation
# ---------------------------------------------------------------------------


def _validate_bar_rows(
    rows: Sequence[Mapping[str, Any]], *, symbol: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    validated: list[dict[str, Any]] = []
    previous_open: datetime | None = None
    gaps: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != set(BAR_FIELDS):
            raise CryptoTenSymbolOIPrescreenError("oi_prescreen_row_shape_invalid")
        if row.get("symbol") != symbol:
            raise CryptoTenSymbolOIPrescreenError("oi_prescreen_row_shape_invalid")
        open_time = _aligned_slot(_parse_utc(row.get("open_time")))
        close_time = _parse_utc(row.get("close_time"))
        if close_time != open_time + FIVE_MINUTES - timedelta(milliseconds=1):
            raise CryptoTenSymbolOIPrescreenError(
                "oi_prescreen_bar_continuity_invalid"
            )
        open_price = _decimal(row.get("open"), positive=True)
        high = _decimal(row.get("high"), positive=True)
        low = _decimal(row.get("low"), positive=True)
        close = _decimal(row.get("close"), positive=True)
        volume = _decimal(row.get("volume"))
        quote_volume = _decimal(row.get("quote_volume"))
        if volume < 0 or quote_volume < 0:
            raise CryptoTenSymbolOIPrescreenError("oi_prescreen_decimal_invalid")
        trade_count = row.get("trade_count")
        if not isinstance(trade_count, int) or trade_count < 0:
            raise CryptoTenSymbolOIPrescreenError(
                "oi_prescreen_trade_count_invalid"
            )
        if low > min(open_price, close) or high < max(open_price, close) or low > high:
            raise CryptoTenSymbolOIPrescreenError("oi_prescreen_ohlc_invalid")
        if previous_open is not None:
            if open_time <= previous_open:
                raise CryptoTenSymbolOIPrescreenError(
                    "oi_prescreen_rows_not_increasing"
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


def _validate_oi_rows(
    rows: Sequence[Mapping[str, Any]], *, symbol: str
) -> list[dict[str, Any]]:
    validated: list[dict[str, Any]] = []
    seen: set[datetime] = set()
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != set(OI_FIELDS):
            raise CryptoTenSymbolOIPrescreenError("oi_prescreen_row_shape_invalid")
        if row.get("symbol") != symbol:
            raise CryptoTenSymbolOIPrescreenError("oi_prescreen_row_shape_invalid")
        timestamp = _aligned_slot(_parse_utc(row.get("timestamp")))
        if timestamp in seen:
            raise CryptoTenSymbolOIPrescreenError("oi_prescreen_duplicate_timestamp")
        seen.add(timestamp)
        value = _decimal(row.get("sum_open_interest"), positive=True)
        validated.append({"timestamp": timestamp, "sum_open_interest": value})
    return sorted(validated, key=lambda item: item["timestamp"])


# ---------------------------------------------------------------------------
# Fetch (explicitly injected loopback transport only)
# ---------------------------------------------------------------------------


def _fetch_symbol_bars(
    client: SharedSignalsV1Client,
    *,
    catalog: CatalogEnvelope,
    symbol: str,
    start_open_time: datetime,
    end_open_time: datetime,
    max_windows: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    dataset_id = _bar_dataset_id(symbol)
    _verify_catalog(catalog, dataset_id)
    rows: list[dict[str, Any]] = []
    receipts: list[dict[str, Any]] = []
    lower = start_open_time
    for _ in range(max_windows):
        upper = min(lower + (PAGE_LIMIT - 1) * FIVE_MINUTES, end_open_time)
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
        envelope = run.envelope
        metadata = envelope.metadata
        if (
            metadata.state.lower() != "ready"
            or metadata.degraded is not False
            or metadata.quality.get("state") != "valid"
            or not isinstance(metadata.receipt_id, str)
            or not metadata.receipt_id
        ):
            raise CryptoTenSymbolOIPrescreenError(
                "oi_prescreen_fetch_metadata_invalid"
            )
        page = [dict(row) for row in envelope.data]
        for row in page:
            open_time = _parse_utc(row.get("open_time"))
            if open_time < lower or open_time > upper:
                raise CryptoTenSymbolOIPrescreenError(
                    "oi_prescreen_fetch_window_overflow"
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
        raise CryptoTenSymbolOIPrescreenError(
            "oi_prescreen_fetch_window_budget_exceeded"
        )
    if not rows:
        raise CryptoTenSymbolOIPrescreenError("oi_prescreen_fetch_empty_history")
    return rows, receipts


def _fetch_symbol_oi(
    client: SharedSignalsV1Client,
    *,
    catalog: CatalogEnvelope,
    symbol: str,
    start_open_time: datetime,
    end_open_time: datetime,
    max_windows: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    dataset_id = _oi_dataset_id(symbol)
    rows: list[dict[str, Any]] = []
    receipts: list[dict[str, Any]] = []
    lower = start_open_time
    for _ in range(max_windows):
        upper = min(lower + (PAGE_LIMIT - 1) * FIVE_MINUTES, end_open_time)
        run = collect_query_pages(
            client=client,
            request=QueryRequest(
                dataset_id=dataset_id,
                schema_major=1,
                fields=OI_FIELDS,
                filters={
                    "symbol": {"eq": symbol},
                    "timestamp": {
                        "between": [_wire_iso(lower), _wire_iso(upper)]
                    },
                },
                order=("symbol:asc", "timestamp:asc"),
                limit=PAGE_LIMIT,
            ),
            identity_fields=OI_IDENTITY_FIELDS,
            max_pages=1,
            max_rows=PAGE_LIMIT,
        )
        envelope = run.envelope
        metadata = envelope.metadata
        if (
            metadata.state.lower() != "ready"
            or metadata.degraded is not False
            or metadata.quality.get("state") != "valid"
            or not isinstance(metadata.receipt_id, str)
            or not metadata.receipt_id
        ):
            raise CryptoTenSymbolOIPrescreenError(
                "oi_prescreen_oi_fetch_metadata_invalid"
            )
        page = [dict(row) for row in envelope.data]
        rows.extend(page)
        if upper >= end_open_time:
            break
        lower = upper + FIVE_MINUTES
    else:
        raise CryptoTenSymbolOIPrescreenError(
            "oi_prescreen_fetch_window_budget_exceeded"
        )
    if not rows:
        raise CryptoTenSymbolOIPrescreenError("oi_prescreen_fetch_empty_oi_history")
    return rows, receipts


def fetch_raw_history(
    *,
    client: SharedSignalsV1Client,
    raw_dir: Path | str,
    start_open_time: datetime,
    end_open_time: datetime,
    symbols: Sequence[str] = OBSERVATION_SYMBOLS,
    max_windows: int = MAX_FETCH_WINDOWS,
) -> dict[str, Any]:
    """Pull bars + OI history and persist canonical per-symbol raw files."""

    _assert_simulation_only()
    if not isinstance(client, SharedSignalsV1Client):
        raise CryptoTenSymbolOIPrescreenError("oi_prescreen_client_invalid")
    start = _aligned_slot(start_open_time)
    end = _aligned_slot(end_open_time)
    if end < start:
        raise CryptoTenSymbolOIPrescreenError("oi_prescreen_window_invalid")
    if tuple(symbols) not in (OBSERVATION_SYMBOLS, OBSERVATION_SYMBOLS_V40):
        raise CryptoTenSymbolOIPrescreenError("oi_prescreen_symbols_invalid")
    directory = Path(raw_dir)
    if directory.exists() and (directory.is_symlink() or not directory.is_dir()):
        raise CryptoTenSymbolOIPrescreenError("oi_prescreen_raw_dir_invalid")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    catalog = client.get_catalog()
    summaries: list[dict[str, Any]] = []
    for symbol in symbols:
        bar_rows, bar_receipts = _fetch_symbol_bars(
            client,
            catalog=catalog,
            symbol=symbol,
            start_open_time=start,
            end_open_time=end,
            max_windows=max_windows,
        )
        oi_rows, oi_receipts = _fetch_symbol_oi(
            client,
            catalog=catalog,
            symbol=symbol,
            start_open_time=start,
            end_open_time=end,
            max_windows=max_windows,
        )
        bars, gaps = _validate_bar_rows(bar_rows, symbol=symbol)
        oi = _validate_oi_rows(oi_rows, symbol=symbol)
        payload = {
            "contract": OI_RAW_CONTRACT,
            "symbol": symbol,
            "bars_dataset_id": _bar_dataset_id(symbol),
            "oi_dataset_id": _oi_dataset_id(symbol),
            "oi_source": "tradingdatas_loopback_query",
            "catalog_version": catalog.catalog_version,
            "query": {
                "start_open_time": _iso(start),
                "end_open_time": _iso(end),
            },
            "row_count": len(bars),
            "oi_count": len(oi),
            "first_open_time": _iso(bars[0]["open_time"]),
            "last_open_time": _iso(bars[-1]["open_time"]),
            "gaps": gaps,
            "bar_receipts": list(bar_receipts),
            "oi_receipts": list(oi_receipts),
            "bars_sha256": _sha256([dict(row) for row in bar_rows]),
            "oi_sha256": _sha256([dict(row) for row in oi_rows]),
            "bars": [dict(row) for row in bar_rows],
            "oi": [dict(row) for row in oi_rows],
            **_non_evidence_fields(),
        }
        encoded = (_canonical_json(payload) + "\n").encode("utf-8")
        _write_file_atomic(directory / f"{symbol}.json", encoded)
        summaries.append(
            {
                "symbol": symbol,
                "row_count": len(bars),
                "oi_count": len(oi),
                "first_open_time": payload["first_open_time"],
                "last_open_time": payload["last_open_time"],
                "gap_count": len(gaps),
            }
        )
    return {
        "contract": OI_PRESCREEN_CONTRACT,
        "event_type": "oi_fetch_summary",
        "raw_dir": str(directory),
        "catalog_version": catalog.catalog_version,
        "network_used": True,
        "datasets": summaries,
        **_non_evidence_fields(),
    }


# ---------------------------------------------------------------------------
# Raw loading (offline)
# ---------------------------------------------------------------------------


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
        raise CryptoTenSymbolOIPrescreenError(
            "oi_prescreen_raw_write_failed"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _read_raw_file(path: Path, *, symbol: str) -> dict[str, Any]:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise CryptoTenSymbolOIPrescreenError(
            "oi_prescreen_raw_file_invalid"
        ) from exc
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
            raise CryptoTenSymbolOIPrescreenError("oi_prescreen_raw_file_invalid")
        encoded = os.read(descriptor, metadata.st_size)
        after = os.fstat(descriptor)
        if len(encoded) != metadata.st_size or after.st_size != metadata.st_size:
            raise CryptoTenSymbolOIPrescreenError("oi_prescreen_raw_file_invalid")
    except CryptoTenSymbolOIPrescreenError:
        raise
    except OSError as exc:
        raise CryptoTenSymbolOIPrescreenError(
            "oi_prescreen_raw_file_invalid"
        ) from exc
    finally:
        os.close(descriptor)
    if not encoded.endswith(b"\n") or b"\x00" in encoded:
        raise CryptoTenSymbolOIPrescreenError("oi_prescreen_raw_file_invalid")
    try:
        payload = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CryptoTenSymbolOIPrescreenError(
            "oi_prescreen_raw_file_invalid"
        ) from exc
    if not isinstance(payload, dict):
        raise CryptoTenSymbolOIPrescreenError("oi_prescreen_raw_file_invalid")
    if (
        payload.get("contract") != OI_RAW_CONTRACT
        or payload.get("symbol") != symbol
        or payload.get("bars_dataset_id") != _bar_dataset_id(symbol)
        or payload.get("oi_dataset_id") != _oi_dataset_id(symbol)
    ):
        raise CryptoTenSymbolOIPrescreenError("oi_prescreen_raw_contract_invalid")
    for key, expected in _non_evidence_fields().items():
        if payload.get(key) != expected:
            raise CryptoTenSymbolOIPrescreenError(
                "oi_prescreen_raw_authority_invalid"
            )
    bar_rows = payload.get("bars")
    oi_rows = payload.get("oi")
    if not isinstance(bar_rows, list) or not bar_rows:
        raise CryptoTenSymbolOIPrescreenError("oi_prescreen_raw_bars_invalid")
    if not isinstance(oi_rows, list) or not oi_rows:
        raise CryptoTenSymbolOIPrescreenError("oi_prescreen_raw_oi_invalid")
    bars, gaps = _validate_bar_rows(bar_rows, symbol=symbol)
    oi = _validate_oi_rows(oi_rows, symbol=symbol)
    return {"payload": payload, "bars": bars, "oi": oi, "gaps": gaps}


def load_raw_dir(
    raw_dir: Path | str,
    *,
    expected_symbols: Sequence[str] = OBSERVATION_SYMBOLS,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    directory = Path(raw_dir)
    if not directory.is_dir() or directory.is_symlink():
        raise CryptoTenSymbolOIPrescreenError("oi_prescreen_raw_dir_invalid")
    aligned: dict[str, dict[str, Any]] = {}
    meta: dict[str, Any] = {}
    for symbol in expected_symbols:
        loaded = _read_raw_file(directory / f"{symbol}.json", symbol=symbol)
        oi_by_time = {item["timestamp"]: item["sum_open_interest"] for item in loaded["oi"]}
        aligned[symbol] = {
            "bars": loaded["bars"],
            "oi": oi_by_time,
        }
        meta[symbol] = {
            "row_count": len(loaded["bars"]),
            "oi_count": len(loaded["oi"]),
            "first_open_time": _iso(loaded["bars"][0]["open_time"]),
            "last_open_time": _iso(loaded["bars"][-1]["open_time"]),
            "gap_count": len(loaded["gaps"]),
            "oi_source": loaded["payload"].get("oi_source"),
            "catalog_version": loaded["payload"].get("catalog_version"),
        }
    return aligned, meta


# ---------------------------------------------------------------------------
# Evaluation primitives (same cost model as the evidence chain)
# ---------------------------------------------------------------------------


def _cost_adjusted_return(entry: Decimal, exit_: Decimal) -> Decimal:
    net = exit_ * (Decimal("1") - _FEE) / (entry * (Decimal("1") + _FEE)) - Decimal(
        "1"
    )
    return (Decimal("1") + net) * (Decimal("1") - _SLIP) ** 2 - Decimal("1")


def _horizon_label(horizon_bars: int) -> str:
    return "1h" if horizon_bars == 12 else f"{horizon_bars * 5}min"


def _metric_basis(horizon_bars: int) -> str:
    label = _horizon_label(horizon_bars)
    return (
        "cost model identical to the evidence chain (fee 0.001 each side plus"
        " 2bps slippage each side); equal-weight equity curve per 5m slot;"
        f" overlapping {label} labels across 5m slots inflate the effective"
        f" sample size about {horizon_bars}x; non_overlapping keeps every"
        f" {horizon_bars}th slot; historical backfill without PIT proof;"
        " not promotion evidence"
    )


def _sample_metrics(
    samples: Sequence[tuple[datetime, Decimal, Decimal]],
    *,
    universe_count: int,
    baseline_mean: Decimal | None,
    universe_slots: Sequence[datetime],
    stride: int = 1,
    horizon_bars: int = HORIZON_BARS,
) -> dict[str, Any]:
    selected = list(samples)
    returns = [net for _, net, _gross in selected]
    gross_returns = [gross for _, _net, gross in selected]
    count = len(returns)
    mean = sum(returns, Decimal("0")) / Decimal(count) if returns else None
    mean_gross = (
        sum(gross_returns, Decimal("0")) / Decimal(count) if gross_returns else None
    )
    slot_returns: dict[datetime, list[Decimal]] = {}
    for slot, net, _gross in selected:
        slot_returns.setdefault(slot, []).append(net)
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
    kept = [(net, gross) for slot, net, gross in selected if slot in kept_slots]
    kept_net = [net for net, _gross in kept]
    kept_gross = [gross for _net, gross in kept]
    kept_mean = sum(kept_net, Decimal("0")) / Decimal(len(kept)) if kept else None
    kept_mean_gross = (
        sum(kept_gross, Decimal("0")) / Decimal(len(kept)) if kept else None
    )
    metrics: dict[str, Any] = {
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
        "mean_gross": _text(mean_gross),
        "mean_net": _text(mean),
        "median_gross": _text(_median(gross_returns)),
        "median_net": _text(_median(returns)),
        "baseline_delta": _text(mean - baseline_mean)
        if mean is not None and baseline_mean is not None
        else None,
        "cash_delta": _text(mean),
        "max_drawdown": _text(maximum_drawdown),
        "turnover": _text(Decimal(count) / Decimal(universe_count))
        if universe_count
        else "0",
        "metric_basis": _metric_basis(horizon_bars),
    }
    if stride > 1:
        metrics["non_overlapping"] = {
            "stride": stride,
            "slot_count": len(kept_slots),
            "signal_count": len(kept),
            "hit_rate": _text(
                Decimal(sum(value > 0 for value in kept_net)) / Decimal(len(kept))
            )
            if kept
            else None,
            "mean_gross": _text(kept_mean_gross),
            "mean_net": _text(kept_mean),
            "median_net": _text(_median(kept_net)),
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


# ---------------------------------------------------------------------------
# Universe building
# ---------------------------------------------------------------------------


def _build_universe(
    aligned: Mapping[str, Mapping[str, Any]],
    *,
    horizon_bars: int,
) -> dict[str, dict[datetime, dict[str, Any]]]:
    universe: dict[str, dict[datetime, dict[str, Any]]] = {}
    for symbol, material in aligned.items():
        bars = material["bars"]
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
            future_open = slot + horizon_bars * FIVE_MINUTES
            future = by_open.get(future_open)
            if future is None:
                continue
            rows[slot] = {
                "entry": by_open[slot]["close"],
                "exit": future["close"],
                "forward_gross": future["close"] / by_open[slot]["close"]
                - Decimal("1"),
                "forward_return": _cost_adjusted_return(
                    by_open[slot]["close"], future["close"]
                ),
                "return_1h": by_open[slot]["close"] / by_open[window_opens[0]]["close"]
                - Decimal("1"),
                "by_open": by_open,
                "oi": material["oi"],
            }
        universe[symbol] = rows
    return universe


def _close_at(by_open: Mapping[datetime, Mapping[str, Any]], slot: datetime) -> Decimal | None:
    bar = by_open.get(slot)
    return None if bar is None else bar["close"]


def _oi_at(oi: Mapping[datetime, Decimal], slot: datetime) -> Decimal | None:
    return oi.get(slot)


# ---------------------------------------------------------------------------
# Candidate evaluators
# ---------------------------------------------------------------------------


def _evaluate_oi_change_rate(
    universe: Mapping[str, Mapping[datetime, Mapping[str, Any]]],
    *,
    symbols: Sequence[str],
    parameters: Mapping[str, Any],
    horizon_bars: int,
) -> dict[str, Any]:
    lookback = int(parameters["lookback_bars"])
    threshold = Decimal(parameters["oi_change_threshold"])
    universe_rows: list[tuple[str, datetime, Decimal]] = []
    selected: list[tuple[datetime, Decimal, Decimal]] = []
    per_symbol: dict[str, dict[str, Any]] = {}
    for symbol in symbols:
        entry = {"signal_count": 0, "returns": [], "positive": 0}
        for slot in sorted(universe[symbol]):
            row = universe[symbol][slot]
            by_open = row["by_open"]
            oi = row["oi"]
            past_slot = slot - lookback * FIVE_MINUTES
            current_oi = _oi_at(oi, slot)
            past_oi = _oi_at(oi, past_slot)
            if current_oi is None or past_oi is None or past_oi == 0:
                continue
            oi_change = current_oi / past_oi - Decimal("1")
            return_1h = row["return_1h"]
            universe_rows.append((symbol, slot, row["forward_return"]))
            if oi_change >= threshold and return_1h >= 0:
                selected.append((slot, row["forward_return"], row["forward_gross"]))
                entry["signal_count"] += 1
                entry["returns"].append(row["forward_return"])
                if row["forward_return"] > 0:
                    entry["positive"] += 1
        returns = entry["returns"]
        count = len(returns)
        per_symbol[symbol] = {
            "signal_count": entry["signal_count"],
            "hit_rate": _text(Decimal(entry["positive"]) / Decimal(count))
            if count
            else None,
            "mean_net": _text(
                sum(returns, Decimal("0")) / Decimal(count) if count else None
            ),
        }
    universe_count = len(universe_rows)
    universe_slots = sorted({slot for _, slot, _ in universe_rows})
    baseline_mean = (
        sum((value for _, _, value in universe_rows), Decimal("0"))
        / Decimal(universe_count)
        if universe_count
        else None
    )
    metrics = _sample_metrics(
        selected,
        universe_count=universe_count,
        baseline_mean=baseline_mean,
        universe_slots=universe_slots,
        stride=horizon_bars,
        horizon_bars=horizon_bars,
    )
    return {
        "metrics": metrics,
        "per_symbol": per_symbol,
        "universe_slots": universe_slots,
        "baseline": [(slot, value) for _, slot, value in universe_rows],
    }


def _evaluate_price_oi_divergence(
    universe: Mapping[str, Mapping[datetime, Mapping[str, Any]]],
    *,
    symbols: Sequence[str],
    parameters: Mapping[str, Any],
    horizon_bars: int,
) -> dict[str, Any]:
    lookback = int(parameters["lookback_bars"])
    direction = parameters["direction"]
    universe_rows: list[tuple[str, datetime, Decimal]] = []
    selected: list[tuple[datetime, Decimal, Decimal]] = []
    per_symbol: dict[str, dict[str, Any]] = {}
    for symbol in symbols:
        entry = {"signal_count": 0, "returns": [], "positive": 0}
        for slot in sorted(universe[symbol]):
            row = universe[symbol][slot]
            by_open = row["by_open"]
            oi = row["oi"]
            past_slot = slot - lookback * FIVE_MINUTES
            current_oi = _oi_at(oi, slot)
            past_oi = _oi_at(oi, past_slot)
            past_close = _close_at(by_open, past_slot)
            if current_oi is None or past_oi is None or past_oi == 0 or past_close is None:
                continue
            oi_change = current_oi / past_oi - Decimal("1")
            price_ret = row["entry"] / past_close - Decimal("1")
            universe_rows.append((symbol, slot, row["forward_return"]))
            divergence = (price_ret * oi_change) < 0
            if direction == "fade":
                signaled = divergence and price_ret < 0 and oi_change > 0
            elif direction == "follow":
                signaled = divergence and price_ret > 0 and oi_change < 0
            else:
                raise CryptoTenSymbolOIPrescreenError(
                    "oi_prescreen_direction_invalid"
                )
            if signaled:
                selected.append((slot, row["forward_return"], row["forward_gross"]))
                entry["signal_count"] += 1
                entry["returns"].append(row["forward_return"])
                if row["forward_return"] > 0:
                    entry["positive"] += 1
        returns = entry["returns"]
        count = len(returns)
        per_symbol[symbol] = {
            "signal_count": entry["signal_count"],
            "hit_rate": _text(Decimal(entry["positive"]) / Decimal(count))
            if count
            else None,
            "mean_net": _text(
                sum(returns, Decimal("0")) / Decimal(count) if count else None
            ),
        }
    universe_count = len(universe_rows)
    universe_slots = sorted({slot for _, slot, _ in universe_rows})
    baseline_mean = (
        sum((value for _, _, value in universe_rows), Decimal("0"))
        / Decimal(universe_count)
        if universe_count
        else None
    )
    metrics = _sample_metrics(
        selected,
        universe_count=universe_count,
        baseline_mean=baseline_mean,
        universe_slots=universe_slots,
        stride=horizon_bars,
        horizon_bars=horizon_bars,
    )
    return {
        "metrics": metrics,
        "per_symbol": per_symbol,
        "universe_slots": universe_slots,
        "baseline": [(slot, value) for _, slot, value in universe_rows],
    }


def _evaluate_oi_weighted_momentum(
    universe: Mapping[str, Mapping[datetime, Mapping[str, Any]]],
    *,
    symbols: Sequence[str],
    parameters: Mapping[str, Any],
    horizon_bars: int,
) -> dict[str, Any]:
    momentum_lookback = int(parameters["momentum_lookback_bars"])
    oi_lookback = int(parameters["oi_lookback_bars"])
    top_k = int(parameters["top_k"])
    slots = sorted(
        slot
        for slot in universe[symbols[0]]
        if all(slot in universe[symbol] for symbol in symbols)
    )
    portfolio: list[tuple[datetime, Decimal, Decimal]] = []
    baseline: list[tuple[datetime, Decimal]] = []
    inclusion: dict[str, dict[str, Any]] = {
        symbol: {"inclusion_count": 0, "returns": []} for symbol in symbols
    }
    evaluated_slots = 0
    for slot in slots:
        scores: dict[str, Decimal] = {}
        valid = True
        for symbol in symbols:
            row = universe[symbol][slot]
            by_open = row["by_open"]
            oi = row["oi"]
            mom_past = slot - momentum_lookback * FIVE_MINUTES
            oi_past = slot - oi_lookback * FIVE_MINUTES
            past_close = _close_at(by_open, mom_past)
            current_oi = _oi_at(oi, slot)
            past_oi = _oi_at(oi, oi_past)
            if past_close is None or current_oi is None or past_oi is None or past_oi == 0:
                valid = False
                break
            mom_ret = row["entry"] / past_close - Decimal("1")
            oi_change = current_oi / past_oi - Decimal("1")
            scores[symbol] = mom_ret * oi_change
        if not valid:
            continue
        evaluated_slots += 1
        ranked = sorted(
            symbols,
            key=lambda symbol: (-scores[symbol], symbols.index(symbol)),
        )
        chosen = ranked[: min(top_k, len(symbols))]
        value = sum(
            (universe[symbol][slot]["forward_return"] for symbol in chosen),
            Decimal("0"),
        ) / Decimal(len(chosen))
        gross = sum(
            (universe[symbol][slot]["forward_gross"] for symbol in chosen),
            Decimal("0"),
        ) / Decimal(len(chosen))
        portfolio.append((slot, value, gross))
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
        universe_slots=[slot for slot, _, _ in portfolio],
        stride=horizon_bars,
        horizon_bars=horizon_bars,
    )
    kept_slots = set(sorted(slot for slot, _, _ in portfolio)[::horizon_bars])
    metrics["non_overlapping"]["baseline_delta"] = _text(
        _baseline_delta_subset(
            Decimal(metrics["non_overlapping"]["mean_net"])
            if metrics["non_overlapping"]["mean_net"] is not None
            else None,
            baseline,
            kept_slots,
        )
    )
    per_symbol = {
        symbol: {
            "inclusion_count": inclusion[symbol]["inclusion_count"],
            "inclusion_rate": _text(
                Decimal(inclusion[symbol]["inclusion_count"])
                / Decimal(evaluated_slots)
            )
            if evaluated_slots
            else "0",
            "mean_net_when_included": _text(
                sum(inclusion[symbol]["returns"], Decimal("0"))
                / Decimal(len(inclusion[symbol]["returns"]))
                if inclusion[symbol]["returns"]
                else None
            ),
        }
        for symbol in symbols
    }
    return {"metrics": metrics, "per_symbol": per_symbol, "evaluation_slots": evaluated_slots}


def analyze(
    aligned: Mapping[str, Mapping[str, Any]],
    *,
    meta: Mapping[str, Any] | None = None,
    horizon_bars: int = HORIZON_BARS,
) -> dict[str, Any]:
    """Evaluate all three OI families across every allowed horizon."""

    if (
        isinstance(horizon_bars, bool)
        or not isinstance(horizon_bars, int)
        or horizon_bars not in ALLOWED_HORIZON_BARS
    ):
        raise CryptoTenSymbolOIPrescreenError("oi_prescreen_horizon_invalid")
    symbols = tuple(aligned.keys())
    if not symbols:
        raise CryptoTenSymbolOIPrescreenError("oi_prescreen_symbols_invalid")
    data_window = (
        dict(meta)
        if meta is not None
        else {
            symbol: {
                "row_count": len(material["bars"]),
                "oi_count": len(material["oi"]),
                "first_open_time": _iso(material["bars"][0]["open_time"]),
                "last_open_time": _iso(material["bars"][-1]["open_time"]),
            }
            for symbol, material in aligned.items()
        }
    )
    universes = {
        horizon: _build_universe(aligned, horizon_bars=horizon)
        for horizon in ALLOWED_HORIZON_BARS
    }
    candidates: list[dict[str, Any]] = []
    for candidate in _oi_candidates():
        family = candidate["family"]
        parameters = candidate["parameters"]
        horizon_metrics: dict[str, Any] = {}
        for horizon in ALLOWED_HORIZON_BARS:
            universe = universes[horizon]
            if family == "oi_change_rate":
                evaluated = _evaluate_oi_change_rate(
                    universe, symbols=symbols, parameters=parameters, horizon_bars=horizon
                )
            elif family == "price_oi_divergence":
                evaluated = _evaluate_price_oi_divergence(
                    universe, symbols=symbols, parameters=parameters, horizon_bars=horizon
                )
            elif family == "oi_weighted_momentum":
                evaluated = _evaluate_oi_weighted_momentum(
                    universe, symbols=symbols, parameters=parameters, horizon_bars=horizon
                )
            else:
                raise CryptoTenSymbolOIPrescreenError("oi_prescreen_family_invalid")
            metrics = dict(evaluated["metrics"])
            if (
                family in ("oi_change_rate", "price_oi_divergence")
                and "non_overlapping" in metrics
            ):
                kept_slots = set(
                    sorted(evaluated["universe_slots"])[::horizon]
                )
                subset_mean = (
                    Decimal(metrics["non_overlapping"]["mean_net"])
                    if metrics["non_overlapping"]["mean_net"] is not None
                    else None
                )
                metrics["non_overlapping"]["baseline_delta"] = _text(
                    _baseline_delta_subset(subset_mean, evaluated["baseline"], kept_slots)
                )
            horizon_metrics[f"h{horizon}"] = {
                "horizon_bars": horizon,
                "horizon_minutes": horizon * 5,
                "metrics": metrics,
                "per_symbol": evaluated["per_symbol"],
            }
        candidates.append(
            {
                "candidate_id": candidate["candidate_id"],
                "family": family,
                "hypothesis": candidate["hypothesis"],
                "parameters": parameters,
                "horizons": horizon_metrics,
            }
        )
    return {
        "contract": OI_PRESCREEN_CONTRACT,
        "event_type": "oi_factor_prescreen_analysis",
        "symbols": list(symbols),
        "data_window": data_window,
        "oi_source": meta.get("oi_source") if meta else None,
        "cost_policy": {
            "cost_policy_id": "crypto-round-trip-taker-v1",
            "fee_rate": format(TAKER_FEE_RATE, "f"),
            "slippage_bps_each_side": format(SLIPPAGE_BPS, "f"),
        },
        "non_overlap_stride_note": "stride=horizon bars",
        "candidates": candidates,
        **_non_evidence_fields(),
    }


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------


def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    return str(value)


def _pct(value: Any) -> str:
    if value is None:
        return "—"
    try:
        return f"{Decimal(value) * 100:.4f}%"
    except (InvalidOperation, TypeError):
        return str(value)


def render_report(result: Mapping[str, Any]) -> str:
    window = result["data_window"]
    lines = [
        "# Crypto 10 币 OI 族候选因子预筛（非证据研究）",
        "",
        "> **非证据声明**：本报告全部数字来自无 PIT 证明的历史回填数据"
        "（`historical_backfill_no_pit=true`），仅供工程/定义检查"
        "（`not_promotion_evidence=true`、`authority=none`），"
        "**不得进入任何晋级证据**，不构成 edge、概率校准或参数变更授权。",
        "",
        "## 方法",
        "",
        "- 数据：10 币（固定 `market_observation.OBSERVATION_SYMBOLS` 顺序）5m"
        " OHLCV bar + 同币 perp open-interest 5m 序列（`crypto.perp.binance."
        "<symbol>.open_interest`，字段 `sum_open_interest`）。",
        "- OI 数据来源：见下方 `oi_source` 与数据窗口表；本报告的 OI 序列来自"
        "服务器只读诊断抽取（生产 18083 对该 dataset 的 query 因"
        "`transport_profile_unverified` fail-closed 返回空），"
        "`authority=none`、`research_only=true`。",
        "- 标签：forward 60/240/720/1440min（12/48/144/288 槽）close→close；"
        "成本与证据链同一口径：fee 0.001 双边 + slippage 2bps 双边，"
        "`(1+net)*(1-slip)^2-1`（cost policy `crypto-round-trip-taker-v1`，"
        "往返约 0.24%）。",
        "- 口径：每个候选×horizon 报全样本与非重叠子样本（stride=horizon 槽数）。",
        "- 指标：signal/universe、hit_rate、mean gross（费用前）/mean net"
        "（费用后）、vs always-invest 基线；非重叠子样本的 gross/net 并列。",
        "",
        "## 数据窗口",
        "",
        "| symbol | bars | oi | first_open_time | last_open_time | gaps | oi_source |",
        "|---|---|---|---|---|---|---|",
    ]
    for symbol in result["symbols"]:
        item = window[symbol]
        lines.append(
            f"| {symbol} | {item['row_count']} | {item['oi_count']}"
            f" | {item['first_open_time']} | {item['last_open_time']}"
            f" | {item.get('gap_count', 0)} | {item.get('oi_source', '—')} |"
        )
    header = (
        "| horizon | signal/universe | hit_rate | mean_gross | mean_net"
        " | Δ baseline | 非重叠 n | 非重叠 gross | 非重叠 net |"
    )
    divider = "|---|---|---|---|---|---|---|---|---|"
    for candidate in result["candidates"]:
        family = candidate["family"]
        candidate_id = candidate["candidate_id"]
        lines += [
            "",
            f"## 候选：{candidate_id}（族 `{family}`）",
            "",
            f"假设：{candidate['hypothesis']}",
            "",
            header,
            divider,
        ]
        for horizon_key, entry in candidate["horizons"].items():
            m = entry["metrics"]
            subset = m.get("non_overlapping") or {}
            label = _horizon_label(entry["horizon_bars"])
            lines.append(
                f"| {label} | {m.get('signal_count')} / {m.get('universe_count')}"
                f" | {_pct(m.get('hit_rate'))} | {_pct(m.get('mean_gross'))}"
                f" | {_pct(m.get('mean_net'))} | {_pct(m.get('baseline_delta'))}"
                f" | {subset.get('signal_count', '—')}"
                f" | {_pct(subset.get('mean_gross'))} | {_pct(subset.get('mean_net'))} |"
            )
        lines += ["", "判定：（见下方结论表）"]
    lines += [
        "",
        "## 结论汇总",
        "",
        "（逐候选×horizon 判定见报告末尾的结论摘要表；全部数字仅基于费用后"
        "非重叠口径也成立时才考虑预注册，本报告不构成任何晋级证据。）",
        "",
        "---",
        "",
        f"生成：`Crypto/ten_symbol_oi_prescreen.py --report`；contract "
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
    symbols: Sequence[str] = OBSERVATION_SYMBOLS,
) -> SharedSignalsV1Client:
    try:
        transport = build_runtime_transport(
            "http-json-v1",
            token_file=token_file,
            base_url=base_url,
        )
    except RuntimeGateConfigurationError as exc:
        raise CryptoTenSymbolOIPrescreenError(
            "oi_prescreen_transport_invalid"
        ) from exc
    return SharedSignalsV1Client(
        SharedSignalsV1Config(
            base_url=base_url,
            expected_catalog_version=catalog_version,
            dataset_ids=frozenset(
                _bar_dataset_id(symbol) for symbol in symbols
            )
            | frozenset(_oi_dataset_id(symbol) for symbol in symbols),
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
        description="Offline ten-symbol OI-family factor pre-screen research"
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
    parser.add_argument("--horizon-bars", type=int, default=HORIZON_BARS)
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
                raise CryptoTenSymbolOIPrescreenError(
                    "oi_prescreen_fetch_arguments_incomplete"
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
        aligned, meta = load_raw_dir(args.raw_dir)
        result = analyze(aligned, meta=meta, horizon_bars=args.horizon_bars)
        if args.report is not None:
            report_text = render_report(result)
            _write_file_atomic(args.report, report_text.encode("utf-8"))
        _emit(result)
        return 0
    except Exception:
        print("crypto ten-symbol oi prescreen failed closed", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ALLOWED_HORIZON_BARS",
    "HORIZON_BARS",
    "OI_FAMILY_IDS",
    "OI_PRESCREEN_CONTRACT",
    "OI_RAW_CONTRACT",
    "CryptoTenSymbolOIPrescreenError",
    "analyze",
    "fetch_raw_history",
    "load_raw_dir",
    "main",
    "render_report",
]
