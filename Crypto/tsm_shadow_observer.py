"""Read-only TSM(1d) shadow observer for Crypto (no execution authority).

This module converts closed 5-minute bars (already-collected TD data, or a
local export of them) into daily bars and appends an immutable shadow-ledger
row per closed daily bar: trailing 20d / 90d time-series-momentum signals and
an ex-ante volatility-scaled suggested notional fraction.  It deliberately
has no network, no capital, no order, no champion, and no promotion path.
Output is research-only and cannot become an execution signal by itself.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, Mapping, Sequence


TSM_SHADOW_CONTRACT = "tradingagent.crypto.tsm_shadow_observation.v1"
TSM_SET_ID = "crypto-1d-tsm-shadow-v1"
TSM_SET_VERSION = 1
SUPPORTED_SYMBOLS = ("BTCUSDT", "ETHUSDT")
DAILY_BAR_MS = 24 * 60 * 60 * 1000
BASE_POSITION_FRACTION = 0.10
TARGET_ANNUAL_VOL = 0.30
VOL_WINDOW_DAYS = 20
MAX_SCALE = 3.0
MAX_LEDGER_BYTES = 32 * 1024 * 1024
LEDGER_NAME = "tsm_shadow.ledger.jsonl"


class TsmShadowObserverError(RuntimeError):
    """Stable fail-closed error for TSM shadow inputs or writes."""


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
        raise TsmShadowObserverError("tsm_shadow_payload_invalid") from exc


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _non_authority_fields() -> dict[str, Any]:
    return {
        "authority": "none",
        "research_only": True,
        "execution_eligible": False,
        "execution_authority": False,
        "durable_execution_receipt": False,
        "production_eligible": False,
        "real_trading_enabled": False,
        "network_used": False,
        "model_network_used": False,
        "testnet_used": False,
        "live_broker_used": False,
        "promotion_authorized": False,
        "automatic_champion_replacement": False,
        "automatic_risk_expansion_enabled": False,
        "outbox_id": None,
        "capital_commit_id": None,
    }


def _regular_file(path: Path, *, reason: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise TsmShadowObserverError(reason) from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_size > MAX_LEDGER_BYTES
    ):
        raise TsmShadowObserverError(reason)


def _write_all(descriptor: int, encoded: bytes) -> None:
    offset = 0
    while offset < len(encoded):
        written = os.write(descriptor, encoded[offset:])
        if written <= 0:
            raise TsmShadowObserverError("tsm_shadow_short_write")
        offset += written


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _append_ledger_rows(
    ledger_path: Path,
    rows: list[Mapping[str, Any]],
) -> None:
    """Append immutable ledger rows atomically under an exclusive lock."""

    encoded = b"".join(
        (_canonical_json(row) + "\n").encode("utf-8") for row in rows
    )
    if not encoded or len(encoded) > MAX_LEDGER_BYTES:
        raise TsmShadowObserverError("tsm_shadow_ledger_size_invalid")
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = ledger_path.parent / f".{ledger_path.name}.lock"
    with open(lock_path, "a+b") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            if ledger_path.exists():
                _regular_file(ledger_path, reason="tsm_shadow_ledger_invalid")
            temporary = ledger_path.parent / (
                f".{ledger_path.name}.{uuid.uuid4().hex}.tmp"
            )
            descriptor: int | None = None
            try:
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                flags |= getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(temporary, flags, 0o600)
                if ledger_path.exists():
                    with open(ledger_path, "rb") as handle:
                        _write_all(descriptor, handle.read())
                _write_all(descriptor, encoded)
                os.fsync(descriptor)
                os.close(descriptor)
                descriptor = None
                os.replace(temporary, ledger_path)
                _fsync_directory(ledger_path.parent)
            finally:
                if descriptor is not None:
                    os.close(descriptor)
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def _parse_bar(value: Mapping[str, Any]) -> tuple[int, float]:
    open_ms = value.get("open_time_ms")
    close = value.get("close")
    if not isinstance(open_ms, int) or open_ms <= 0:
        raise TsmShadowObserverError("tsm_shadow_bar_open_time_invalid")
    try:
        parsed_close = float(str(close))
    except (TypeError, ValueError) as exc:
        raise TsmShadowObserverError("tsm_shadow_bar_close_invalid") from exc
    if not parsed_close > 0:
        raise TsmShadowObserverError("tsm_shadow_bar_close_invalid")
    return open_ms, parsed_close


def aggregate_daily_bars(
    five_minute_bars: Sequence[Mapping[str, Any]],
    *,
    symbol: str,
) -> list[dict[str, Any]]:
    """Aggregate closed 5m bars into UTC daily bars (open/high/low/close)."""

    if symbol not in SUPPORTED_SYMBOLS:
        raise TsmShadowObserverError("tsm_shadow_symbol_unsupported")
    if not five_minute_bars:
        raise TsmShadowObserverError("tsm_shadow_empty_input")
    daily: dict[int, dict[str, Any]] = {}
    for raw in five_minute_bars:
        open_ms, close = _parse_bar(raw)
        day = open_ms - (open_ms % DAILY_BAR_MS)
        bucket = daily.setdefault(
            day,
            {
                "day_open_ms": day,
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "n_bars": 0,
            },
        )
        try:
            open_price = float(str(raw.get("open")))
            high_price = float(str(raw.get("high")))
            low_price = float(str(raw.get("low")))
        except (TypeError, ValueError) as exc:
            raise TsmShadowObserverError("tsm_shadow_bar_ohlc_invalid") from exc
        if bucket["n_bars"] == 0:
            bucket["open"] = open_price
        bucket["high"] = max(bucket["high"], high_price)
        bucket["low"] = min(bucket["low"], low_price)
        bucket["close"] = close
        bucket["n_bars"] += 1
    result = [daily[key] for key in sorted(daily)]
    for item in result:
        if item["n_bars"] < 100:
            item["complete"] = False
        else:
            item["complete"] = True
    return result


def build_shadow_rows(
    daily_bars: Sequence[Mapping[str, Any]],
    *,
    symbol: str,
    input_sha256: str,
) -> list[dict[str, Any]]:
    """Build immutable shadow rows for closed daily bars (PIT, no forward info).

    TSM signal uses the trailing 20d/90d return of *closed* bars.  The
    volatility scale uses the trailing 20d realized vol of closed bars.  A row
    is emitted only for bars that have enough history; earlier bars are warmup.
    """

    closes = [float(item["close"]) for item in daily_bars]
    n = len(closes)
    rows: list[dict[str, Any]] = []
    for i in range(n):
        item = daily_bars[i]
        if not item.get("complete"):
            continue
        day_open_ms = int(item["day_open_ms"])
        day_key = (
            datetime.fromtimestamp(day_open_ms / 1000, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
        row: dict[str, Any] = {
            "contract": TSM_SHADOW_CONTRACT,
            "set_id": TSM_SET_ID,
            "set_version": TSM_SET_VERSION,
            "symbol": symbol,
            "day_open_ms": day_open_ms,
            "day_open": day_key,
            "close": closes[i],
            "input_sha256": input_sha256,
            "tsm_20d": None,
            "tsm_90d": None,
            "suggested_fraction_20d": None,
            "suggested_fraction_90d": None,
            "realized_vol_20d_ann": None,
            "warmup": True,
            **_non_authority_fields(),
        }
        for lookback, suffix in ((20, "20d"), (90, "90d")):
            if i < lookback:
                continue
            trailing_return = closes[i] / closes[i - lookback] - 1.0
            signal = 1.0 if trailing_return > 0 else 0.0
            row[f"tsm_{suffix}"] = round(trailing_return, 8)
            row["warmup"] = False
            # ex-ante vol scale from closed bars i-lookback..i-1
            window = closes[i - lookback : i]
            if len(window) >= 2:
                rets = [
                    window[j] / window[j - 1] - 1.0
                    for j in range(1, len(window))
                ]
                mean = sum(rets) / len(rets)
                variance = sum((x - mean) ** 2 for x in rets) / (len(rets) - 1)
                daily_vol = variance**0.5
                ann_vol = daily_vol * (365**0.5)
                row[f"realized_vol_{suffix}_ann"] = round(ann_vol, 8)
                if daily_vol > 0:
                    scale = min(TARGET_ANNUAL_VOL / ann_vol, MAX_SCALE)
                    row[f"suggested_fraction_{suffix}"] = round(
                        signal * BASE_POSITION_FRACTION * scale, 8
                    )
        row["row_sha256"] = _sha256(
            {key: value for key, value in row.items() if key != "row_sha256"}
        )
        rows.append(row)
    return rows


def append_tsm_shadow_rows(
    *,
    ledger_root: Path | str,
    rows: list[Mapping[str, Any]],
) -> int:
    """Append shadow rows to the per-symbol ledger; return rows written.

    Idempotent per (symbol, day_open_ms): rows whose day already exists in the
    ledger are skipped, so repeated cron runs never duplicate a daily row.
    """

    root = Path(ledger_root)
    if not rows:
        return 0
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        symbol = row.get("symbol")
        if not isinstance(symbol, str) or symbol not in SUPPORTED_SYMBOLS:
            raise TsmShadowObserverError("tsm_shadow_ledger_symbol_invalid")
        grouped.setdefault(symbol, []).append(row)
    written = 0
    for symbol, symbol_rows in sorted(grouped.items()):
        ledger_path = root / symbol / LEDGER_NAME
        existing: set[int] = set()
        if ledger_path.exists():
            for row in read_ledger(root, symbol=symbol):
                day = row.get("day_open_ms")
                if isinstance(day, int):
                    existing.add(day)
        fresh = [
            row
            for row in symbol_rows
            if not isinstance(row.get("day_open_ms"), int)
            or row["day_open_ms"] not in existing
        ]
        if fresh:
            _append_ledger_rows(ledger_path, fresh)
            written += len(fresh)
    return written


def read_ledger(
    ledger_root: Path | str,
    *,
    symbol: str,
) -> list[dict[str, Any]]:
    """Read back a symbol's shadow ledger rows in file order."""

    if symbol not in SUPPORTED_SYMBOLS:
        raise TsmShadowObserverError("tsm_shadow_symbol_unsupported")
    ledger_path = Path(ledger_root) / symbol / LEDGER_NAME
    if not ledger_path.exists():
        return []
    _regular_file(ledger_path, reason="tsm_shadow_ledger_invalid")
    result: list[dict[str, Any]] = []
    with open(ledger_path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError as exc:
                raise TsmShadowObserverError("tsm_shadow_ledger_corrupt") from exc
            if parsed.get("row_sha256") != _sha256(
                {key: value for key, value in parsed.items() if key != "row_sha256"}
            ):
                raise TsmShadowObserverError("tsm_shadow_ledger_checksum_invalid")
            result.append(parsed)
    return result


def run_tsm_shadow_once(
    *,
    five_minute_bars: Mapping[str, Sequence[Mapping[str, Any]]],
    ledger_root: Path | str,
) -> dict[str, Any]:
    """Run one read-only TSM shadow pass for all supported symbols."""

    if not isinstance(five_minute_bars, Mapping):
        raise TsmShadowObserverError("tsm_shadow_input_invalid")
    all_rows: list[dict[str, Any]] = []
    per_symbol = {}
    for symbol in SUPPORTED_SYMBOLS:
        bars = five_minute_bars.get(symbol)
        if not bars:
            continue
        input_sha = _sha256([_canonical_json(b) for b in bars])
        daily = aggregate_daily_bars(bars, symbol=symbol)
        rows = build_shadow_rows(daily, symbol=symbol, input_sha256=input_sha)
        all_rows.extend(rows)
        per_symbol[symbol] = {
            "input_bars": len(bars),
            "daily_bars": len(daily),
            "shadow_rows": len(rows),
            "last_row": rows[-1] if rows else None,
        }
    written = append_tsm_shadow_rows(ledger_root=ledger_root, rows=all_rows)
    return {
        "contract": TSM_SHADOW_CONTRACT,
        "status": "completed",
        "written": written,
        "per_symbol": per_symbol,
        **_non_authority_fields(),
    }


__all__ = [
    "TSM_SHADOW_CONTRACT",
    "TsmShadowObserverError",
    "aggregate_daily_bars",
    "append_tsm_shadow_rows",
    "build_shadow_rows",
    "read_ledger",
    "run_tsm_shadow_once",
]
