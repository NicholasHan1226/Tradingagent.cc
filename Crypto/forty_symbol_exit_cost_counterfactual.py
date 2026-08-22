"""Offline 40-symbol exit-cost counterfactual over spot 5m OHLC bars.

This module is *research only*.  It reads a read-only TradingDatas crypto
read-model SQLite file (no network, no capital/order/Champion writes) and
simulates the frozen champion round trip path-wise -- entry when the frozen
signal fires, then the champion exit ladder (take-profit +3%, stop-loss -2%,
24h max hold, momentum reversal) walked bar by bar over high/low/close --
and reports net outcomes under an *exit-leg execution sensitivity grid*:

* ``taker_exit``      -- exit fee 0.1%, exit slippage 2bps (the current
  ``crypto-round-trip-taker-v1`` baseline);
* ``usdm_maker_exit`` -- exit fee 0.02%, exit slippage 0 (a post-only limit
  resting at the TP/SL-adjacent level, USDⓈ-M maker base tier).

Every maker figure is an **upper bound**: it assumes a resting limit always
fills whenever its level is touched (``assumes_touch_equals_fill``) and
ignores queue position and missed-move costs documented in prior research.
Nothing here is promotion evidence; artifacts are sealed
``not_promotion_evidence=true`` / ``historical_backfill_no_pit=true``.
Analysis is pure and offline.
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

CONTRACT = "tradingagent.crypto.forty_symbol_exit_cost_counterfactual.v1"
ALLOWED_THRESHOLDS = ("0.001", "0.002", "0.003", "0.005")
DECISION_LOOKBACK_BARS = 3
REGIME_LOOKBACK_BARS = 12
TAKE_PROFIT_RETURN = Decimal("0.03")
STOP_LOSS_RETURN = Decimal("-0.02")
MAX_HOLD_BARS = 288  # 24h expressed in 5m bars
PATH_STRIDE_BARS = MAX_HOLD_BARS
EXIT_VARIANTS: tuple[tuple[str, str, str], ...] = (
    ("taker_exit", "0.001", "0.0002"),
    ("usdm_maker_exit", "0.0002", "0"),
)
BASELINE_VARIANT = "taker_exit"
MAX_RAW_ROWS_PER_DATASET = 400_000

# Entry leg is always taker under both variants: prior research showed pure
# maker entries suffer adverse selection plus missed-move cost.
ENTRY_FEE = Decimal("0.001")
ENTRY_SLIP = Decimal("0.0002")
ZERO = Decimal("0")
ONE = Decimal("1")

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


class FortySymbolExitCostCounterfactualError(RuntimeError):
    """Stable fail-closed error for exit-cost counterfactual research."""


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
        "assumes_touch_equals_fill": True,
        "counterfactual_only": True,
    }


def _assert_simulation_only() -> None:
    if os.environ.get("REAL_TRADING_ENABLED") != "false":
        raise FortySymbolExitCostCounterfactualError(
            "exit_cost_counterfactual_real_trading_must_be_disabled"
        )


def _parse_utc(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise FortySymbolExitCostCounterfactualError(
            "exit_cost_counterfactual_timestamp_invalid"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FortySymbolExitCostCounterfactualError(
            "exit_cost_counterfactual_timestamp_invalid"
        ) from exc
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() is None
        or parsed.utcoffset() != timedelta(0)
    ):
        raise FortySymbolExitCostCounterfactualError(
            "exit_cost_counterfactual_timestamp_invalid"
        )
    return parsed.astimezone(timezone.utc)


def _slot_index(value: datetime) -> int:
    if value.second != 0 or value.microsecond != 0:
        raise FortySymbolExitCostCounterfactualError(
            "exit_cost_counterfactual_slot_not_aligned"
        )
    return int(value.timestamp()) // 300


def _slot_to_utc(slot: int) -> datetime:
    return datetime.fromtimestamp(slot * 300, tz=timezone.utc)


def _iso_slot(slot: int) -> str:
    return _slot_to_utc(slot).isoformat().replace("+00:00", "Z")


def _decimal(value: Any) -> Decimal:
    if not isinstance(value, str) and not isinstance(value, int):
        raise FortySymbolExitCostCounterfactualError(
            "exit_cost_counterfactual_decimal_invalid"
        )
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as exc:
        raise FortySymbolExitCostCounterfactualError(
            "exit_cost_counterfactual_decimal_invalid"
        ) from exc
    if not parsed.is_finite():
        raise FortySymbolExitCostCounterfactualError(
            "exit_cost_counterfactual_decimal_invalid"
        )
    return parsed


def _text(value: Decimal | int | None) -> str | None:
    if value is None:
        return None
    return format(value, "f")


def _spot_dataset_id(symbol: str) -> str:
    return f"crypto.spot.binance.{symbol.lower()}.5m"


def _round_trip_net(
    gross: Decimal,
    *,
    exit_fee: Decimal,
    exit_slip: Decimal,
) -> Decimal:
    """Entry taker leg fixed; exit leg parameterised.

    Four independent fill frictions: entry taker fee (buy side, grossed
    up), entry slippage, exit fee (sell side), exit slippage.
    """

    bought = (ONE + gross) / (ONE + ENTRY_FEE)
    entered = bought * (ONE - ENTRY_SLIP)
    return entered * (ONE - exit_fee) * (ONE - exit_slip) - ONE


# ---------------------------------------------------------------------------
# Read-only SQLite extraction
# ---------------------------------------------------------------------------


def _connect_read_only(db_path: Path | str) -> sqlite3.Connection:
    path = Path(db_path)
    if not path.is_file() or path.is_symlink():
        raise FortySymbolExitCostCounterfactualError(
            "exit_cost_counterfactual_db_path_invalid"
        )
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise FortySymbolExitCostCounterfactualError(
            "exit_cost_counterfactual_db_open_failed"
        ) from exc
    conn.execute("PRAGMA query_only = ON")
    return conn


def _extract_ohlc_series(
    conn: sqlite3.Connection,
    *,
    dataset_id: str,
) -> tuple[dict[int, tuple[Decimal, Decimal, Decimal]], int]:
    """Return {slot: (close, high, low)} for one spot dataset."""

    cursor = conn.execute(
        "SELECT json_extract(payload_json, '$.open_time'),"
        " json_extract(payload_json, '$.close'),"
        " json_extract(payload_json, '$.high'),"
        " json_extract(payload_json, '$.low')"
        " FROM provider_dataset_rows"
        " WHERE dataset_id = ? AND quality_state = 'valid'",
        (dataset_id,),
    )
    series: dict[int, tuple[Decimal, Decimal, Decimal]] = {}
    duplicates = 0
    seen = 0
    for raw_time, raw_close, raw_high, raw_low in cursor:
        seen += 1
        if seen > MAX_RAW_ROWS_PER_DATASET:
            raise FortySymbolExitCostCounterfactualError(
                "exit_cost_counterfactual_row_budget_exceeded"
            )
        if None in (raw_time, raw_close, raw_high, raw_low):
            continue
        slot = _slot_index(_parse_utc(raw_time))
        close_v = _decimal(raw_close)
        high_v = _decimal(raw_high)
        low_v = _decimal(raw_low)
        if low_v > high_v or close_v <= ZERO or low_v <= ZERO:
            raise FortySymbolExitCostCounterfactualError(
                f"exit_cost_counterfactual_bar_shape_invalid:{dataset_id}"
            )
        if slot in series:
            duplicates += 1
            continue
        series[slot] = (close_v, high_v, low_v)
    if not series:
        raise FortySymbolExitCostCounterfactualError(
            f"exit_cost_counterfactual_empty_dataset:{dataset_id}"
        )
    return series, duplicates


def load_material_from_sqlite(
    db_path: Path | str,
    *,
    symbols: Sequence[str] = FORTY_SYMBOLS,
) -> dict[str, dict[str, Any]]:
    """Read-only extraction of per-symbol 5m OHLC series."""

    _assert_simulation_only()
    if tuple(symbols) != FORTY_SYMBOLS:
        raise FortySymbolExitCostCounterfactualError(
            "exit_cost_counterfactual_symbols_drift"
        )
    conn = _connect_read_only(db_path)
    try:
        material: dict[str, dict[str, Any]] = {}
        for symbol in symbols:
            series, duplicates = _extract_ohlc_series(
                conn,
                dataset_id=_spot_dataset_id(symbol),
            )
            slots = sorted(series)
            gaps = sum(later - earlier - 1 for earlier, later in zip(slots, slots[1:]))
            material[symbol] = {
                "slots": slots,
                # Aligned columns; gap semantics are positional (labels never
                # cross a gap because every lookback/forward below indexes
                # positions, and a gap shows up as a jump between consecutive
                # slots rather than a hole here).
                "bars": [series[slot] for slot in slots],
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


def _is_entry_signal(
    closes: Sequence[Decimal],
    index: int,
    threshold: Decimal,
) -> bool:
    """Frozen champion entry rule (mirrors Crypto.round_trip_capital)."""

    if index < REGIME_LOOKBACK_BARS:
        return False
    base_decision = closes[index - DECISION_LOOKBACK_BARS]
    base_regime = closes[index - REGIME_LOOKBACK_BARS]
    if base_decision <= ZERO or base_regime <= ZERO:
        return False
    decision_return = closes[index] / base_decision - ONE
    regime_return = closes[index] / base_regime - ONE
    return regime_return >= ZERO and decision_return >= threshold


def _is_reversal_exit(
    closes: Sequence[Decimal],
    index: int,
) -> bool:
    """Champion momentum-reversal rule at a closed bar boundary."""

    if index < REGIME_LOOKBACK_BARS:
        return False
    base_decision = closes[index - DECISION_LOOKBACK_BARS]
    base_regime = closes[index - REGIME_LOOKBACK_BARS]
    if base_decision <= ZERO or base_regime <= ZERO:
        return False
    decision_return = closes[index] / base_decision - ONE
    regime_return = closes[index] / base_regime - ONE
    return decision_return < ZERO and regime_return < ZERO


def _simulate_path(
    bars: Sequence[tuple[Decimal, Decimal, Decimal]],
    closes: Sequence[Decimal],
    entry_index: int,
) -> dict[str, Any]:
    """Walk one round trip from ``entry_index`` under the champion ladder.

    Intrabar ambiguity resolves pessimistically: stop-loss is evaluated
    before take-profit inside the same bar.  Fill levels equal the trigger
    prices (documented assumption; exit-side slippage lives in the cost
    grid, not here).
    """

    entry_price = bars[entry_index][0]
    tp_level = entry_price * (ONE + TAKE_PROFIT_RETURN)
    sl_level = entry_price * (ONE + STOP_LOSS_RETURN)
    last_index = min(entry_index + MAX_HOLD_BARS, len(bars) - 1)
    mfe = ZERO
    mae = ZERO
    for offset in range(1, last_index - entry_index + 1):
        _, high, low = bars[entry_index + offset]
        mfe = max(mfe, high / entry_price - ONE)
        mae = min(mae, low / entry_price - ONE)
        if low <= sl_level:
            return {
                "exit_reason": "stop_loss",
                "exit_offset_bars": offset,
                "gross": sl_level / entry_price - ONE,
                "mfe": mfe,
                "mae": mae,
            }
        if high >= tp_level:
            return {
                "exit_reason": "take_profit",
                "exit_offset_bars": offset,
                "gross": tp_level / entry_price - ONE,
                "mfe": mfe,
                "mae": mae,
            }
        close_v = bars[entry_index + offset][0]
        if offset >= MAX_HOLD_BARS:
            return {
                "exit_reason": "max_holding_period",
                "exit_offset_bars": offset,
                "gross": close_v / entry_price - ONE,
                "mfe": mfe,
                "mae": mae,
            }
        if _is_reversal_exit(closes, entry_index + offset):
            return {
                "exit_reason": "momentum_reversal_observed",
                "exit_offset_bars": offset,
                "gross": close_v / entry_price - ONE,
                "mfe": mfe,
                "mae": mae,
            }
    close_v = bars[last_index][0]
    return {
        "exit_reason": "data_end",
        "exit_offset_bars": last_index - entry_index,
        "gross": close_v / entry_price - ONE,
        "mfe": mfe,
        "mae": mae,
    }


def analyze(
    material: Mapping[str, Mapping[str, Any]],
    *,
    symbols: Sequence[str] = FORTY_SYMBOLS,
    thresholds: Sequence[str] = ALLOWED_THRESHOLDS,
    exit_variants: Sequence[tuple[str, str, str]] = EXIT_VARIANTS,
) -> dict[str, Any]:
    """Simulate every frozen-entry round trip and apply the exit-cost grid."""

    if tuple(symbols) != FORTY_SYMBOLS:
        raise FortySymbolExitCostCounterfactualError(
            "exit_cost_counterfactual_symbols_drift"
        )
    if tuple(thresholds) != ALLOWED_THRESHOLDS:
        raise FortySymbolExitCostCounterfactualError(
            "exit_cost_counterfactual_thresholds_drift"
        )
    if not material:
        raise FortySymbolExitCostCounterfactualError(
            "exit_cost_counterfactual_material_empty"
        )

    cells: list[dict[str, Any]] = []
    for threshold_text in thresholds:
        threshold = Decimal(threshold_text)
        trips_by_reason: dict[str, int] = {}
        gross_values: list[Decimal] = []
        mfe_values: list[Decimal] = []
        mae_values: list[Decimal] = []
        hold_bars_total = 0
        net_by_variant: dict[str, list[Decimal]] = {
            name: [] for name, _, _ in exit_variants
        }

        for symbol in symbols:
            bars = material[symbol]["bars"]
            closes = [bar[0] for bar in bars]
            entry_index = REGIME_LOOKBACK_BARS
            while entry_index < len(closes) - 1:
                if not _is_entry_signal(closes, entry_index, threshold):
                    entry_index += 1
                    continue
                trip = _simulate_path(bars, closes, entry_index)
                reason = trip["exit_reason"]
                trips_by_reason[reason] = trips_by_reason.get(reason, 0) + 1
                gross_values.append(trip["gross"])
                mfe_values.append(trip["mfe"])
                mae_values.append(trip["mae"])
                hold_bars_total += trip["exit_offset_bars"]
                for name, fee_text, slip_text in exit_variants:
                    net_by_variant[name].append(
                        _round_trip_net(
                            trip["gross"],
                            exit_fee=Decimal(fee_text),
                            exit_slip=Decimal(slip_text),
                        )
                    )
                entry_index += PATH_STRIDE_BARS

        count = len(gross_values)

        def _mean(values: Sequence[Decimal]) -> Decimal | None:
            if not values:
                return None
            return sum(values, ZERO) / Decimal(len(values))

        variant_stats: dict[str, Any] = {}
        for name, _, _ in exit_variants:
            nets = net_by_variant[name]
            stats: dict[str, Any] = {
                "mean_net": _text(_mean(nets)),
                "hit_positive_rate": _text(
                    sum(1 for value in nets if value > ZERO) / Decimal(len(nets))
                    if nets
                    else None
                ),
            }
            if name != BASELINE_VARIANT:
                baseline_nets = net_by_variant[BASELINE_VARIANT]
                if nets and baseline_nets:
                    delta = _mean(nets) - _mean(baseline_nets)
                    stats["mean_net_delta_vs_baseline"] = _text(delta)
            variant_stats[name] = stats

        cells.append(
            {
                "threshold": threshold_text,
                "trip_count": count,
                "exit_reasons": trips_by_reason,
                "hold_bars_mean": _text(
                    Decimal(hold_bars_total) / Decimal(count) if count else None
                ),
                "mfe_mean": _text(_mean(mfe_values)),
                "mae_mean": _text(_mean(mae_values)),
                "mean_gross": _text(_mean(gross_values)),
                "variants": variant_stats,
            }
        )

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
    names = [name for name, _, _ in EXIT_VARIANTS]
    header = (
        "| threshold | trips | mean_gross | "
        + " | ".join(f"{name}_net" for name in names)
        + " | "
        + " | ".join(f"{name}_delta" for name in names[1:])
        + " |"
    )
    separator = "|" + "---|" * (3 + 2 * len(names) - 1)
    rows = []
    for cell in result["cells"]:
        variants = cell["variants"]
        nets = " | ".join(str(variants[name]["mean_net"]) for name in names)
        deltas = " | ".join(
            str(variants[name].get("mean_net_delta_vs_baseline"))
            for name in names[1:]
        )
        rows.append(
            f"| {cell['threshold']} | {cell['trip_count']}"
            f" | {cell['mean_gross']} | {nets} | {deltas} |"
        )
    lines = [
        "# 40-symbol exit-cost counterfactual (path-level)",
        "",
        header,
        separator,
        *rows,
        "",
        "Maker figures assume touch equals fill (upper bound; ignores queue",
        "position and missed-move cost). Research only; historical backfill",
        "without PIT proof; not promotion evidence.",
    ]
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
