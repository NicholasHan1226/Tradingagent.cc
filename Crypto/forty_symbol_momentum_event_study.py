"""Offline 40-symbol momentum event study over spot 5m closes.

This module is *research only*.  It reads a read-only TradingDatas crypto
read-model SQLite file (no network, no capital/order/Champion writes) and
evaluates the frozen champion entry signal and its threshold variants as an
*event study*, replacing trade-count waiting with bar-level observations:

    event at slot i when regime_return(close, 12 bars) >= 0 AND
    decision_return(close, 3 bars) >= threshold;

then measures the conditional forward return of holding ``horizon`` bars,
against an unconditional stride-sampled baseline, net of the same
``crypto-round-trip-taker-v1`` cost model as the evidence chain.

The history is a *backfill without PIT proof*: every artifact this module
produces is fixed ``not_promotion_evidence=true`` and may only ever feed
engineering/definition checks, never promotion evidence.  Analysis is pure
and offline; there is no exchange, broker, execution, capital, order or model
write path in this module.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Sequence

CONTRACT = "tradingagent.crypto.forty_symbol_momentum_event_study.v1"
ALLOWED_THRESHOLDS = ("0.001", "0.002", "0.003", "0.005")
ALLOWED_HORIZON_BARS = (12, 48, 144, 288)
DECISION_LOOKBACK_BARS = 3  # 15 minutes expressed in 5m bars
REGIME_LOOKBACK_BARS = 12  # 1 hour expressed in 5m bars
MIN_T_STAT_SAMPLES = 2
MAX_RAW_ROWS_PER_DATASET = 400_000

# Cost policy mirrors Crypto/round_trip_capital.py ``crypto-round-trip-taker-v1``:
# 0.1% taker fee each side + 2bps slippage each side (~0.24% round trip).
FEE = Decimal("0.001")
SLIPPAGE_BPS = Decimal("2")
SLIP = SLIPPAGE_BPS / Decimal("10000")
ZERO = Decimal("0")
ONE = Decimal("1")

# Frozen 40-symbol USDⓈ-M spot universe (selection_policy
# ``liquid_usdt_spot_history_v1``, selected_at 2026-08-16T14:04:05Z).  Kept
# verbatim here so the research is reproducible without reading the release
# config at runtime; the server run asserts equality with the release file.
FORTY_SYMBOLS: tuple[str, ...] = (
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "BNBUSDT",
    "DOGEUSDT",
    "ADAUSDT",
    "TRXUSDT",
    "LINKUSDT",
    "AVAXUSDT",
    "BCHUSDT",
    "LTCUSDT",
    "DOTUSDT",
    "NEARUSDT",
    "SUIUSDT",
    "APTUSDT",
    "UNIUSDT",
    "ATOMUSDT",
    "XLMUSDT",
    "HBARUSDT",
    "ETCUSDT",
    "FILUSDT",
    "INJUSDT",
    "ARBUSDT",
    "OPUSDT",
    "AAVEUSDT",
    "GRTUSDT",
    "TIAUSDT",
    "SEIUSDT",
    "ONDOUSDT",
    "LDOUSDT",
    "CRVUSDT",
    "ENAUSDT",
    "WLDUSDT",
    "STRKUSDT",
    "JUPUSDT",
    "PYTHUSDT",
    "FETUSDT",
    "RENDERUSDT",
    "POLUSDT",
)


class FortySymbolMomentumEventStudyError(RuntimeError):
    """Stable fail-closed error for momentum event-study research."""


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
        raise FortySymbolMomentumEventStudyError(
            "momentum_event_study_real_trading_must_be_disabled"
        )


def _parse_utc(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise FortySymbolMomentumEventStudyError(
            "momentum_event_study_timestamp_invalid"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FortySymbolMomentumEventStudyError(
            "momentum_event_study_timestamp_invalid"
        ) from exc
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() is None
        or parsed.utcoffset() != timedelta(0)
    ):
        raise FortySymbolMomentumEventStudyError(
            "momentum_event_study_timestamp_invalid"
        )
    return parsed.astimezone(timezone.utc)


def _slot_index(value: datetime) -> int:
    # 5-minute-aligned slot index (seconds since epoch // 300).  Binance 5m
    # open_time is always on a 5m boundary.
    if value.second != 0 or value.microsecond != 0:
        raise FortySymbolMomentumEventStudyError(
            "momentum_event_study_slot_not_aligned"
        )
    return int(value.timestamp()) // 300


def _slot_to_utc(slot: int) -> datetime:
    return datetime.fromtimestamp(slot * 300, tz=timezone.utc)


def _iso_slot(slot: int) -> str:
    return _slot_to_utc(slot).isoformat().replace("+00:00", "Z")


def _decimal(value: Any) -> Decimal:
    if not isinstance(value, str) and not isinstance(value, int):
        raise FortySymbolMomentumEventStudyError(
            "momentum_event_study_decimal_invalid"
        )
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as exc:
        raise FortySymbolMomentumEventStudyError(
            "momentum_event_study_decimal_invalid"
        ) from exc
    if not parsed.is_finite():
        raise FortySymbolMomentumEventStudyError(
            "momentum_event_study_decimal_invalid"
        )
    return parsed


def _text(value: Decimal | int | None) -> str | None:
    if value is None:
        return None
    return format(value, "f")


def _spot_dataset_id(symbol: str) -> str:
    return f"crypto.spot.binance.{symbol.lower()}.5m"


# ---------------------------------------------------------------------------
# Cost model (same round-trip taker model as the evidence chain)
# ---------------------------------------------------------------------------


def _cost_adjusted_gross(gross: Decimal) -> Decimal:
    """Apply crypto-round-trip-taker-v1 cost to a gross return on notional.

    ``(1+net) = (1+gross) * (1-fee)/(1+fee) * (1-slip)^2 - 1``; this is the
    same multiplicative round-trip cost used by
    ``Crypto/ten_symbol_*_prescreen.py`` (fee both sides + slippage both
    sides, ~0.24%).
    """
    net = (ONE + gross) * (ONE - FEE) / (ONE + FEE) - ONE
    return (ONE + net) * (ONE - SLIP) ** 2 - ONE


# ---------------------------------------------------------------------------
# Read-only SQLite extraction
# ---------------------------------------------------------------------------


def _connect_read_only(db_path: Path | str) -> sqlite3.Connection:
    path = Path(db_path)
    if not path.is_file() or path.is_symlink():
        raise FortySymbolMomentumEventStudyError(
            "momentum_event_study_db_path_invalid"
        )
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise FortySymbolMomentumEventStudyError(
            "momentum_event_study_db_open_failed"
        ) from exc
    conn.execute("PRAGMA query_only = ON")
    return conn


def _extract_close_series(
    conn: sqlite3.Connection,
    *,
    dataset_id: str,
) -> tuple[dict[int, Decimal], int]:
    """Return {slot_index: close} for one spot dataset and a duplicate count."""

    cursor = conn.execute(
        "SELECT json_extract(payload_json, '$.open_time'),"
        " json_extract(payload_json, '$.close')"
        " FROM provider_dataset_rows"
        " WHERE dataset_id = ? AND quality_state = 'valid'",
        (dataset_id,),
    )
    series: dict[int, Decimal] = {}
    duplicates = 0
    seen = 0
    for raw_time, raw_value in cursor:
        seen += 1
        if seen > MAX_RAW_ROWS_PER_DATASET:
            raise FortySymbolMomentumEventStudyError(
                "momentum_event_study_row_budget_exceeded"
            )
        if raw_time is None or raw_value is None:
            continue
        slot = _slot_index(_parse_utc(raw_time))
        value = _decimal(raw_value)
        if slot in series:
            duplicates += 1
            continue
        series[slot] = value
    if not series:
        raise FortySymbolMomentumEventStudyError(
            f"momentum_event_study_empty_dataset:{dataset_id}"
        )
    return series, duplicates


def load_material_from_sqlite(
    db_path: Path | str,
    *,
    symbols: Sequence[str] = FORTY_SYMBOLS,
) -> dict[str, dict[str, Any]]:
    """Read-only extraction of per-symbol 5m close series."""

    _assert_simulation_only()
    if tuple(symbols) != FORTY_SYMBOLS:
        raise FortySymbolMomentumEventStudyError(
            "momentum_event_study_symbols_drift"
        )
    conn = _connect_read_only(db_path)
    try:
        material: dict[str, dict[str, Any]] = {}
        for symbol in symbols:
            closes, duplicates = _extract_close_series(
                conn,
                dataset_id=_spot_dataset_id(symbol),
            )
            slots = sorted(closes)
            gaps = sum(later - earlier - 1 for earlier, later in zip(slots, slots[1:]))
            material[symbol] = {
                "slots": slots,
                "close": [closes[slot] for slot in slots],
                "duplicates": duplicates,
                "gap_slots": gaps,
                "first_open_time": _iso_slot(slots[0]),
                "last_open_time": _iso_slot(slots[-1]),
            }
        return material
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Pure evaluation
# ---------------------------------------------------------------------------


def _forward_returns(
    close: Sequence[Decimal],
    *,
    horizon_bars: int,
) -> list[tuple[int, Decimal]]:
    """Return [(entry_index, gross_forward_return)] for every evaluable slot."""

    out: list[tuple[int, Decimal]] = []
    for i in range(len(close) - horizon_bars):
        entry = close[i]
        if entry <= ZERO:
            raise FortySymbolMomentumEventStudyError(
                "momentum_event_study_nonpositive_close"
            )
        out.append((i, close[i + horizon_bars] / entry - ONE))
    return out


def _is_event(
    close: Sequence[Decimal],
    index: int,
    threshold: Decimal,
) -> bool:
    """Frozen champion entry rule at one closed-bar boundary.

    Mirrors Crypto/fixture_sim/runtime.py: buy when the 1h regime return is
    non-negative AND the 15m decision return reaches the threshold.
    """

    if index < REGIME_LOOKBACK_BARS:
        return False
    base_decision = close[index - DECISION_LOOKBACK_BARS]
    base_regime = close[index - REGIME_LOOKBACK_BARS]
    if base_decision <= ZERO or base_regime <= ZERO:
        return False
    decision_return = close[index] / base_decision - ONE
    regime_return = close[index] / base_regime - ONE
    return regime_return >= ZERO and decision_return >= threshold


def _t_stat(values: Sequence[Decimal]) -> Decimal | None:
    """Sample t-statistic of the mean against zero (None when undefined)."""

    n = len(values)
    if n < MIN_T_STAT_SAMPLES:
        return None
    mean = sum(values, ZERO) / Decimal(n)
    variance = sum(((v - mean) ** 2 for v in values), ZERO) / Decimal(n - 1)
    if variance <= ZERO:
        return None
    std = variance.sqrt()
    return mean / std * Decimal(n).sqrt()


def _evaluate_cell(
    material: Mapping[str, Mapping[str, Any]],
    *,
    symbols: Sequence[str],
    threshold: Decimal,
    horizon_bars: int,
) -> dict[str, Any]:
    event_gross: list[Decimal] = []
    kept_net: list[Decimal] = []
    total_events = 0

    for symbol in symbols:
        close = material[symbol]["close"]
        forwards = _forward_returns(close, horizon_bars=horizon_bars)
        # Non-overlap stride is tracked per symbol in bar-index space so a
        # calendar gap cannot silently widen or shrink the stride.
        previous_kept: int | None = None
        for index, gross in forwards:
            if not _is_event(close, index, threshold):
                continue
            total_events += 1
            if previous_kept is not None and index - previous_kept < horizon_bars:
                continue
            previous_kept = index
            event_gross.append(gross)
            kept_net.append(_cost_adjusted_gross(gross))

    # Unconditional stride-sampled baseline over every evaluable slot.
    baseline_gross: list[Decimal] = []
    for symbol in symbols:
        close = material[symbol]["close"]
        forwards = _forward_returns(close, horizon_bars=horizon_bars)
        for position in range(0, len(forwards), horizon_bars):
            baseline_gross.append(forwards[position][1])

    def _mean(values: Sequence[Decimal]) -> Decimal | None:
        if not values:
            return None
        return sum(values, ZERO) / Decimal(len(values))

    mean_event_gross = _mean(event_gross)
    mean_kept_net = _mean(kept_net)
    mean_baseline = _mean(baseline_gross)
    hit_positive = sum(1 for value in kept_net if value > ZERO)

    return {
        "threshold": _text(threshold),
        "decision_lookback_bars": DECISION_LOOKBACK_BARS,
        "regime_lookback_bars": REGIME_LOOKBACK_BARS,
        "metrics": {
            "event_count_all_overlapping": total_events,
            "baseline": {
                "count": len(baseline_gross),
                "mean_gross": _text(mean_baseline),
            },
            "non_overlapping": {
                "stride": horizon_bars,
                "count": len(kept_net),
                "hit_rate": _text(
                    Decimal(hit_positive) / Decimal(len(kept_net))
                    if kept_net
                    else None
                ),
                "mean_gross": _text(mean_event_gross),
                "mean_net": _text(mean_kept_net),
                "baseline_delta": _text(
                    mean_event_gross - mean_baseline
                    if mean_event_gross is not None and mean_baseline is not None
                    else None
                ),
                "t_stat_net": _text(_t_stat(kept_net)),
            },
            "metric_basis": (
                "event = frozen champion entry (1h regime >= 0 and 15m"
                " decision >= threshold); gross = close[t+horizon]/close[t]-1"
                " on spot 5m close; net applies crypto-round-trip-taker-v1"
                " (fee 0.001 each side + 2bps slippage each side);"
                " non-overlapping stride equals the horizon;"
                " historical backfill without PIT proof; not promotion"
                " evidence"
            ),
        },
    }


def analyze(
    material: Mapping[str, Mapping[str, Any]],
    *,
    symbols: Sequence[str] = FORTY_SYMBOLS,
    thresholds: Sequence[str] = ALLOWED_THRESHOLDS,
    horizons: Sequence[int] = ALLOWED_HORIZON_BARS,
) -> dict[str, Any]:
    """Evaluate every threshold x horizon cell of the momentum entry signal."""

    if tuple(symbols) != FORTY_SYMBOLS:
        raise FortySymbolMomentumEventStudyError(
            "momentum_event_study_symbols_drift"
        )
    if tuple(thresholds) != ALLOWED_THRESHOLDS:
        raise FortySymbolMomentumEventStudyError(
            "momentum_event_study_thresholds_drift"
        )
    if tuple(horizons) != ALLOWED_HORIZON_BARS:
        raise FortySymbolMomentumEventStudyError(
            "momentum_event_study_horizons_drift"
        )
    if not material:
        raise FortySymbolMomentumEventStudyError(
            "momentum_event_study_material_empty"
        )

    cells: list[dict[str, Any]] = []
    for threshold_text in thresholds:
        threshold = Decimal(threshold_text)
        for horizon_bars in horizons:
            cell = _evaluate_cell(
                material,
                symbols=symbols,
                threshold=threshold,
                horizon_bars=horizon_bars,
            )
            cell["horizon_bars"] = horizon_bars
            cells.append(cell)

    coverage_gap_slots = sum(item["gap_slots"] for item in material.values())
    return {
        "contract": CONTRACT,
        **_non_evidence_fields(),
        "universe_size": len(symbols),
        "coverage": {
            "first_open_time": min(
                item["first_open_time"] for item in material.values()
            ),
            "last_open_time": max(
                item["last_open_time"] for item in material.values()
            ),
            "duplicate_slots_total": sum(
                item["duplicates"] for item in material.values()
            ),
            "gap_slots_total": coverage_gap_slots,
        },
        "cells": cells,
    }


# ---------------------------------------------------------------------------
# Markdown projection
# ---------------------------------------------------------------------------


def render_markdown(result: Mapping[str, Any]) -> str:
    lines = [
        "# 40-symbol momentum entry event study",
        "",
        "| threshold | horizon_min | events | kept | hit_rate | mean_gross"
        " | mean_net | baseline_delta | t_stat_net |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for cell in result["cells"]:
        metrics = cell["metrics"]
        overlap = metrics["non_overlapping"]
        lines.append(
            f"| {cell['threshold']} | {cell['horizon_bars'] * 5}"
            f" | {metrics['event_count_all_overlapping']}"
            f" | {overlap['count']} | {overlap['hit_rate']}"
            f" | {overlap['mean_gross']} | {overlap['mean_net']}"
            f" | {overlap['baseline_delta']} | {overlap['t_stat_net']} |"
        )
    lines.append("")
    lines.append(
        "Research only; historical backfill without PIT proof; not promotion"
        " evidence."
    )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True, help="read-model SQLite file")
    parser.add_argument("--out-json", type=Path, help="write machine result JSON here")
    parser.add_argument("--report", type=Path, help="write Markdown report here")
    args = parser.parse_args(argv)

    material = load_material_from_sqlite(args.db)
    result = analyze(material)
    payload = json.dumps(
        result, ensure_ascii=True, allow_nan=False, sort_keys=True, indent=2
    ) + "\n"

    if args.out_json:
        args.out_json.write_text(payload, encoding="utf-8")
    if args.report:
        args.report.write_text(render_markdown(result), encoding="utf-8")
    if args.out_json is None and args.report is None:
        sys.stdout.write(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
