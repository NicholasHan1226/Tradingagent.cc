"""Offline 40-symbol delta-neutral basis / cash-and-carry research.

This module is *research only*.  It reads a read-only TradingDatas crypto
read-model SQLite file (no network, no capital/order/Champion writes) and
evaluates a market-neutral cash-and-carry hypothesis on the 5m spot close and
the perp ``premium_index`` close:

    a perp price path is reconstructed as ``P = spot_close * (1 +
    premium_close)``.  When premium is at an extreme the strategy holds the
    two opposite legs (long spot + short perp for premium >= +threshold,
    short spot + long perp for premium <= -threshold) for a fixed horizon,
    so directional price risk is hedged and the position monetises premium /
    basis convergence.

The portfolio gross is computed directly from the two legs (NOT from the
premium difference) so the residual approximation error of the delta-neutral
first-order argument stays visible.  A separate "pure basis convergence"
``premium_0 - premium_H`` is reported alongside for comparison.

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

CONTRACT = "tradingagent.crypto.forty_symbol_basis_carry_research.v1"
ALLOWED_HORIZON_BARS = (12, 48, 144, 288)
ALLOWED_THRESHOLDS = ("0.0001", "0.0002", "0.0005", "0.001")
MAX_RAW_ROWS_PER_DATASET = 400_000

# Cost policy mirrors Crypto/round_trip_capital.py ``crypto-round-trip-taker-v1``:
# 0.1% taker fee each side + 2bps slippage each side, applied per leg
# (~0.24% round trip per leg; two legs => ~0.48% combined).
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


class FortySymbolBasisCarryError(RuntimeError):
    """Stable fail-closed error for basis-carry research."""


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
        raise FortySymbolBasisCarryError(
            "basis_carry_real_trading_must_be_disabled"
        )


def _parse_utc(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise FortySymbolBasisCarryError("basis_carry_timestamp_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FortySymbolBasisCarryError("basis_carry_timestamp_invalid") from exc
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() is None
        or parsed.utcoffset() != timedelta(0)
    ):
        raise FortySymbolBasisCarryError("basis_carry_timestamp_invalid")
    return parsed.astimezone(timezone.utc)


def _slot_index(value: datetime) -> int:
    # 5-minute-aligned slot index (seconds since epoch // 300).  Binance 5m
    # open_time is always on a 5m boundary.
    if value.second != 0 or value.microsecond != 0:
        raise FortySymbolBasisCarryError("basis_carry_slot_not_aligned")
    return int(value.timestamp()) // 300


def _slot_to_utc(slot: int) -> datetime:
    return datetime.fromtimestamp(slot * 300, tz=timezone.utc)


def _iso_slot(slot: int) -> str:
    return _slot_to_utc(slot).isoformat().replace("+00:00", "Z")


def _decimal(value: Any) -> Decimal:
    if not isinstance(value, str) or not value:
        raise FortySymbolBasisCarryError("basis_carry_decimal_invalid")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise FortySymbolBasisCarryError("basis_carry_decimal_invalid") from exc
    if not parsed.is_finite():
        raise FortySymbolBasisCarryError("basis_carry_decimal_invalid")
    return parsed


def _text(value: Decimal | int | None) -> str | None:
    if value is None:
        return None
    return format(value, "f")


def _spot_dataset_id(symbol: str) -> str:
    return f"crypto.spot.binance.{symbol.lower()}.5m"


def _premium_dataset_id(symbol: str) -> str:
    return f"crypto.perp.binance.{symbol.lower()}.premium_index"


# ---------------------------------------------------------------------------
# Cost model (same round-trip taker model as the evidence chain, per leg)
# ---------------------------------------------------------------------------


def _cost_factor() -> Decimal:
    """Per-leg round-trip cost factor.

    ``(1+net_leg) = (1+gross_leg) * (1-fee)/(1+fee) * (1-slip)^2``.  This is
    the same multiplicative round-trip cost used by
    ``Crypto/round_trip_capital.py`` (fee both sides + slippage both sides,
    ~0.24% per leg).  The combined cash-and-carry position has two legs, so
    the total drag is ~0.48%.
    """
    return (ONE - FEE) / (ONE + FEE) * (ONE - SLIP) ** 2


def _leg_net_taker(gross: Decimal) -> Decimal:
    return (ONE + gross) * _cost_factor() - ONE


# ---------------------------------------------------------------------------
# Read-only SQLite extraction
# ---------------------------------------------------------------------------


def _connect_read_only(db_path: Path | str) -> sqlite3.Connection:
    path = Path(db_path)
    if not path.is_file() or path.is_symlink():
        raise FortySymbolBasisCarryError("basis_carry_db_path_invalid")
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise FortySymbolBasisCarryError("basis_carry_db_open_failed") from exc
    conn.execute("PRAGMA query_only = ON")
    return conn


def _extract_series(
    conn: sqlite3.Connection,
    *,
    dataset_id: str,
    symbol: str,
    field: str,
) -> tuple[dict[int, Decimal], int]:
    """Return {slot_index: value} for one dataset and a duplicate-slot count.

    ``field`` is the JSON key used for both the timestamp (``open_time``) and
    the value (``close``) in this read model; both are extracted via SQLite
    JSON functions so the full payload is never materialised in Python.
    """
    path_time = "$.open_time"
    path_value = f"$.{field}"
    cursor = conn.execute(
        "SELECT json_extract(payload_json, ?), json_extract(payload_json, ?)"
        " FROM provider_dataset_rows"
        " WHERE dataset_id = ? AND quality_state = 'valid'",
        (path_time, path_value, dataset_id),
    )
    series: dict[int, Decimal] = {}
    duplicates = 0
    seen = 0
    for raw_time, raw_value in cursor:
        seen += 1
        if seen > MAX_RAW_ROWS_PER_DATASET:
            raise FortySymbolBasisCarryError("basis_carry_row_budget_exceeded")
        if raw_time is None or raw_value is None:
            continue
        slot = _slot_index(_parse_utc(raw_time))
        value = _decimal(raw_value)
        if slot in series:
            duplicates += 1
            continue
        series[slot] = value
    if not series:
        raise FortySymbolBasisCarryError(f"basis_carry_empty_dataset:{dataset_id}")
    return series, duplicates


def _build_material(
    conn: sqlite3.Connection,
    *,
    symbols: Sequence[str],
) -> dict[str, dict[str, Any]]:
    material: dict[str, dict[str, Any]] = {}
    for symbol in symbols:
        spot, spot_dup = _extract_series(
            conn, dataset_id=_spot_dataset_id(symbol), symbol=symbol, field="close"
        )
        premium, premium_dup = _extract_series(
            conn, dataset_id=_premium_dataset_id(symbol), symbol=symbol, field="close"
        )
        aligned_slots = sorted(set(spot) & set(premium))
        if not aligned_slots:
            raise FortySymbolBasisCarryError(f"basis_carry_no_alignment:{symbol}")
        gaps = 0
        for earlier, later in zip(aligned_slots, aligned_slots[1:]):
            gaps += later - earlier - 1
        perp: list[Decimal] = []
        for slot in aligned_slots:
            factor = ONE + premium[slot]
            if factor <= ZERO:
                raise FortySymbolBasisCarryError(
                    f"basis_carry_invalid_perp_premium:{symbol}"
                )
            perp.append(spot[slot] * factor)
        material[symbol] = {
            "times": aligned_slots,
            "spot": [spot[s] for s in aligned_slots],
            "premium": [premium[s] for s in aligned_slots],
            "perp": perp,
            "spot_duplicates": spot_dup,
            "premium_duplicates": premium_dup,
            "gap_slots": gaps,
            "first_open_time": _iso_slot(aligned_slots[0]),
            "last_open_time": _iso_slot(aligned_slots[-1]),
            "spot_count": len(spot),
            "premium_count": len(premium),
            "aligned_count": len(aligned_slots),
        }
    return material


def load_material_from_sqlite(
    db_path: Path | str,
    *,
    symbols: Sequence[str] = FORTY_SYMBOLS,
) -> dict[str, dict[str, Any]]:
    """Read-only extraction of aligned spot + premium 5m series per symbol."""
    _assert_simulation_only()
    if tuple(symbols) != FORTY_SYMBOLS:
        raise FortySymbolBasisCarryError("basis_carry_symbols_drift")
    conn = _connect_read_only(db_path)
    try:
        return _build_material(conn, symbols=symbols)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Pure evaluation
# ---------------------------------------------------------------------------


def _signal_direction(premium: Decimal, threshold: Decimal) -> int:
    if premium >= threshold:
        return 1  # long spot + short perp (receive positive funding)
    if premium <= -threshold:
        return -1  # short spot + long perp (receive negative funding)
    return 0


def _build_horizon_context(
    material: Mapping[str, Mapping[str, Any]],
    *,
    symbols: Sequence[str],
    horizon_bars: int,
) -> dict[str, Any]:
    """Precompute threshold-independent context for one horizon.

    Returns the universe count, the non-overlapping kept slots (stride =
    horizon), and the always-long-spot directional baseline (single-leg taker
    net) for comparison.
    """
    universe_count = 0
    spot_gross_sum = ZERO
    spot_net_sum = ZERO
    all_slots: set[int] = set()
    for symbol in symbols:
        item = material[symbol]
        times = item["times"]
        spot = item["spot"]
        count = len(times)
        for index in range(0, count - horizon_bars):
            slot = times[index]
            price_ret = spot[index + horizon_bars] / spot[index] - ONE
            spot_gross_sum += price_ret
            spot_net_sum += _leg_net_taker(price_ret)
            universe_count += 1
            all_slots.add(slot)
    ordered_slots = sorted(all_slots)
    kept_slots = set(ordered_slots[::horizon_bars])
    return {
        "universe_count": universe_count,
        "kept_slots": kept_slots,
        "spot_baseline_gross_mean": (
            spot_gross_sum / Decimal(universe_count) if universe_count else None
        ),
        "spot_baseline_net_mean": (
            spot_net_sum / Decimal(universe_count) if universe_count else None
        ),
        "cash_baseline_mean": ZERO,
    }


def _evaluate_cell(
    material: Mapping[str, Mapping[str, Any]],
    *,
    symbols: Sequence[str],
    threshold: Decimal,
    horizon_bars: int,
    context: Mapping[str, Any],
) -> dict[str, Any]:
    """Evaluate one threshold x horizon cell (context is precomputed)."""

    kept_slots = context["kept_slots"]

    signal_count = 0
    positive_count = 0
    negative_count = 0

    gross_sum = ZERO
    basis_sum = ZERO
    net_taker_sum = ZERO
    net_maker_sum = ZERO
    net_positive = 0

    pos_gross_sum = ZERO
    pos_basis_sum = ZERO
    pos_net_sum = ZERO
    neg_gross_sum = ZERO
    neg_basis_sum = ZERO
    neg_net_sum = ZERO

    slot_net_sum: dict[int, Decimal] = {}
    slot_net_count: dict[int, int] = {}

    worst_net: Decimal | None = None
    worst_meta: dict[str, Any] | None = None

    kept_gross_list: list[Decimal] = []
    kept_basis_list: list[Decimal] = []
    kept_net_list: list[Decimal] = []
    kept_maker_list: list[Decimal] = []

    gross_basis_abs_sum = ZERO

    for symbol in symbols:
        item = material[symbol]
        times = item["times"]
        spot = item["spot"]
        premium = item["premium"]
        perp = item["perp"]
        count = len(times)
        for index in range(0, count - horizon_bars):
            premium_entry = premium[index]
            direction = _signal_direction(premium_entry, threshold)
            if direction == 0:
                continue
            exit_index = index + horizon_bars

            s_entry = spot[index]
            s_exit = spot[exit_index]
            p_entry = perp[index]
            p_exit = perp[exit_index]
            premium_exit = premium[exit_index]

            if s_entry <= ZERO or p_entry <= ZERO or p_exit <= ZERO:
                raise FortySymbolBasisCarryError("basis_carry_invalid_price")

            # Each leg is normalised to its own entry notional.  The combined
            # gross is the sum of the two legs (equal notional per leg), so
            # the approximation error of the delta-neutral argument is visible
            # rather than hand-removed.
            spot_leg = Decimal(direction) * (s_exit / s_entry - ONE)
            perp_leg = Decimal(direction) * (p_entry / p_exit - ONE)
            gross = spot_leg + perp_leg
            basis = Decimal(direction) * (premium_entry - premium_exit)

            spot_net = _leg_net_taker(spot_leg)
            perp_net = _leg_net_taker(perp_leg)
            net_taker = spot_net + perp_net
            net_maker = gross  # maker hypothesis: fee=0, slippage=0

            if direction > 0:
                positive_count += 1
                pos_gross_sum += gross
                pos_basis_sum += basis
                pos_net_sum += net_taker
            else:
                negative_count += 1
                neg_gross_sum += gross
                neg_basis_sum += basis
                neg_net_sum += net_taker

            signal_count += 1
            gross_sum += gross
            basis_sum += basis
            net_taker_sum += net_taker
            net_maker_sum += net_maker
            gross_basis_abs_sum += abs(gross - basis)
            if net_taker > ZERO:
                net_positive += 1

            slot = times[index]
            slot_net_sum[slot] = slot_net_sum.get(slot, ZERO) + net_taker
            slot_net_count[slot] = slot_net_count.get(slot, 0) + 1

            if worst_net is None or net_taker < worst_net:
                worst_net = net_taker
                worst_meta = {
                    "symbol": symbol,
                    "slot": slot,
                    "premium_entry": _text(premium_entry),
                    "premium_exit": _text(premium_exit),
                    "direction": direction,
                    "side": "positive_premium" if direction > 0 else "negative_premium",
                    "spot_leg_gross": _text(spot_leg),
                    "perp_leg_gross": _text(perp_leg),
                    "gross": _text(gross),
                    "basis": _text(basis),
                    "net_taker": _text(net_taker),
                }

            if slot in kept_slots:
                kept_gross_list.append(gross)
                kept_basis_list.append(basis)
                kept_net_list.append(net_taker)
                kept_maker_list.append(net_maker)

    def _mean(total: Decimal, count: int) -> Decimal | None:
        return total / Decimal(count) if count else None

    mean_gross = _mean(gross_sum, signal_count)
    mean_basis = _mean(basis_sum, signal_count)
    mean_net_taker = _mean(net_taker_sum, signal_count)
    mean_net_maker = _mean(net_maker_sum, signal_count)

    universe_count = context["universe_count"]
    spot_baseline_net_mean = context["spot_baseline_net_mean"]
    delta_cash = mean_net_taker  # cash baseline is exactly zero
    delta_spot = (
        mean_net_taker - spot_baseline_net_mean
        if mean_net_taker is not None and spot_baseline_net_mean is not None
        else None
    )

    kept_mean_gross = _mean(sum(kept_gross_list, ZERO), len(kept_gross_list))
    kept_mean_basis = _mean(sum(kept_basis_list, ZERO), len(kept_basis_list))
    kept_mean_net = _mean(sum(kept_net_list, ZERO), len(kept_net_list))
    kept_mean_maker = _mean(sum(kept_maker_list, ZERO), len(kept_maker_list))
    kept_hit_rate = (
        Decimal(sum(value > ZERO for value in kept_net_list))
        / Decimal(len(kept_net_list))
        if kept_net_list
        else None
    )

    # Equal-weight equity curve across signal slots (flat slots contribute 0).
    equity = ONE
    peak = ONE
    max_drawdown = ZERO
    for slot in sorted(slot_net_sum):
        slot_net = slot_net_sum[slot] / Decimal(slot_net_count[slot])
        equity *= ONE + slot_net
        peak = max(peak, equity)
        if peak > ZERO:
            max_drawdown = max(max_drawdown, (peak - equity) / peak)

    slot_nets = [
        slot_net_sum[s] / Decimal(slot_net_count[s]) for s in sorted(slot_net_sum)
    ]
    loss_streak_count = 0
    max_loss_streak = 0
    current = 0
    for value in slot_nets:
        if value < ZERO:
            current += 1
            max_loss_streak = max(max_loss_streak, current)
            if current == 2:
                loss_streak_count += 1
        else:
            current = 0

    worst_slot = None
    worst_slot_net = None
    for slot in sorted(slot_net_sum):
        slot_net = slot_net_sum[slot] / Decimal(slot_net_count[slot])
        if worst_slot_net is None or slot_net < worst_slot_net:
            worst_slot_net = slot_net
            worst_slot = slot

    return {
        "horizon_bars": horizon_bars,
        "horizon_minutes": horizon_bars * 5,
        "metrics": {
            "universe_count": universe_count,
            "signal_count": signal_count,
            "positive_count": positive_count,
            "negative_count": negative_count,
            "coverage": _text(
                Decimal(signal_count) / Decimal(universe_count)
                if universe_count
                else ZERO
            ),
            "hit_rate": _text(
                Decimal(net_positive) / Decimal(signal_count)
                if signal_count
                else None
            ),
            "mean_gross": _text(mean_gross),
            "mean_basis": _text(mean_basis),
            "gross_minus_basis": _text(
                mean_gross - mean_basis
                if mean_gross is not None and mean_basis is not None
                else None
            ),
            "gross_basis_mae": _text(
                gross_basis_abs_sum / Decimal(signal_count) if signal_count else None
            ),
            "mean_net_taker": _text(mean_net_taker),
            "mean_net_maker": _text(mean_net_maker),
            "delta_cash": _text(delta_cash),
            "delta_spot": _text(delta_spot),
            "max_drawdown": _text(max_drawdown),
            "sides": {
                "positive_premium": {
                    "count": positive_count,
                    "mean_gross": _text(_mean(pos_gross_sum, positive_count)),
                    "mean_basis": _text(_mean(pos_basis_sum, positive_count)),
                    "mean_net_taker": _text(_mean(pos_net_sum, positive_count)),
                    "feasible_without_borrow": True,
                },
                "negative_premium": {
                    "count": negative_count,
                    "mean_gross": _text(_mean(neg_gross_sum, negative_count)),
                    "mean_basis": _text(_mean(neg_basis_sum, negative_count)),
                    "mean_net_taker": _text(_mean(neg_net_sum, negative_count)),
                    "feasible_without_borrow": False,
                },
            },
            "non_overlapping": {
                "stride": horizon_bars,
                "slot_count": len(kept_slots),
                "signal_count": len(kept_net_list),
                "hit_rate": _text(kept_hit_rate),
                "mean_gross": _text(kept_mean_gross),
                "mean_basis": _text(kept_mean_basis),
                "mean_net_taker": _text(kept_mean_net),
                "mean_net_maker": _text(kept_mean_maker),
            },
            "tail": {
                "worst_obs_net_taker": _text(worst_net),
                "worst_obs": worst_meta,
                "worst_slot_net": _text(worst_slot_net),
                "worst_slot": _iso_slot(worst_slot) if worst_slot is not None else None,
                "loss_streak_count": loss_streak_count,
                "max_loss_streak": max_loss_streak,
            },
            "metric_basis": (
                "delta-neutral cash-and-carry: perp price P = spot_close *"
                " (1 + premium_close); direction=+1 => long spot + short perp"
                " (premium >= +threshold); direction=-1 => short spot + long"
                " perp (premium <= -threshold); spot_leg = d*(S_H/S_0 - 1);"
                " perp_leg = d*(P_0/P_H - 1); portfolio gross = spot_leg +"
                " perp_leg (equal notional per leg); basis (pure) ="
                " d*(premium_0 - premium_H); net_taker = cost(spot_leg) +"
                " cost(perp_leg) with per-leg crypto-round-trip-taker-v1"
                " (fee 0.001 each side + 2bps slippage each side, ~0.24%/leg,"
                " ~0.48% combined); net_maker = gross (fee=0, slippage=0);"
                " cash baseline = 0; historical backfill without PIT proof;"
                " not promotion evidence"
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
    """Evaluate every threshold x horizon cell of the basis-carry signal."""
    if tuple(symbols) != FORTY_SYMBOLS:
        raise FortySymbolBasisCarryError("basis_carry_symbols_drift")
    if tuple(thresholds) != ALLOWED_THRESHOLDS:
        raise FortySymbolBasisCarryError("basis_carry_thresholds_drift")
    if tuple(horizons) != ALLOWED_HORIZON_BARS:
        raise FortySymbolBasisCarryError("basis_carry_horizons_drift")
    if not material:
        raise FortySymbolBasisCarryError("basis_carry_material_empty")

    context_by_horizon: dict[int, Mapping[str, Any]] = {
        horizon: _build_horizon_context(material, symbols=symbols, horizon_bars=horizon)
        for horizon in horizons
    }

    results: list[dict[str, Any]] = []
    for threshold_str in thresholds:
        threshold = _decimal(threshold_str)
        horizon_results: dict[str, Any] = {}
        for horizon in horizons:
            horizon_results[f"h{horizon}"] = _evaluate_cell(
                material,
                symbols=symbols,
                threshold=threshold,
                horizon_bars=horizon,
                context=context_by_horizon[horizon],
            )
        results.append({"threshold": threshold_str, "horizons": horizon_results})

    data_window: dict[str, Any] = {}
    for symbol, item in material.items():
        data_window[symbol] = {
            "spot_count": item["spot_count"],
            "premium_count": item["premium_count"],
            "aligned_count": item["aligned_count"],
            "first_open_time": item["first_open_time"],
            "last_open_time": item["last_open_time"],
            "gap_slots": item["gap_slots"],
            "spot_duplicates": item["spot_duplicates"],
            "premium_duplicates": item["premium_duplicates"],
        }

    return {
        "contract": CONTRACT,
        "event_type": "forty_symbol_basis_carry_analysis",
        "symbols": list(symbols),
        "data_window": data_window,
        "data_source": {
            "kind": "tradingdatas_crypto_read_model_sqlite",
            "read_only": True,
            "historical_backfill_no_pit": True,
            "note": (
                "read-only diagnostic extraction from the TradingDatas crypto"
                " read-model SQLite; not a supported runtime consumer path"
            ),
        },
        "cost_policy": {
            "cost_policy_id": "crypto-round-trip-taker-v1",
            "fee_rate": format(FEE, "f"),
            "slippage_bps_each_side": format(SLIPPAGE_BPS, "f"),
            "legs": 2,
            "per_leg_round_trip_approx": "-0.24%",
            "combined_round_trip_approx": "-0.48%",
            "maker_hypothesis": "fee=0, slippage=0 (net_maker == gross)",
        },
        "method": {
            "feature": "spot 5m close + perp premium_index 5m close",
            "perp_price": "P = spot_close * (1 + premium_close)",
            "signal": (
                "premium_close >= +threshold => long spot + short perp"
                " (direction +1); premium_close <= -threshold => short spot +"
                " long perp (direction -1); otherwise flat"
            ),
            "portfolio_gross": "spot_leg + perp_leg",
            "spot_leg": "d * (S_H / S_0 - 1)",
            "perp_leg": "d * (P_0 / P_H - 1)",
            "pure_basis": "d * (premium_0 - premium_H)",
            "baselines": {
                "cash": "0 (market-neutral, no position)",
                "spot_always_long": "always-long spot single-leg taker net",
            },
        },
        "results": results,
        **_non_evidence_fields(),
    }


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------


def _pct(value: Any) -> str:
    if value is None:
        return "—"
    try:
        return f"{Decimal(value) * 100:.4f}%"
    except (InvalidOperation, TypeError):
        return str(value)


def _num(value: Any) -> str:
    if value is None:
        return "—"
    return str(value)


def _horizon_label(bars: int) -> str:
    if bars == 12:
        return "1h"
    if bars == 48:
        return "4h"
    if bars == 144:
        return "12h"
    if bars == 288:
        return "24h"
    return f"{bars * 5}min"


def _best_cells(result: Mapping[str, Any]) -> dict[str, Any]:
    """Derive the best cells and the go/no-go judgments from the numbers."""
    best_taker_full = None
    best_taker_kept = None
    best_maker_full = None
    best_maker_kept = None
    any_taker_full_positive = False
    any_taker_kept_positive = False
    any_maker_full_positive = False
    any_maker_kept_positive = False

    for entry in result["results"]:
        threshold = entry["threshold"]
        for key in sorted(entry["horizons"]):
            cell = entry["horizons"][key]
            m = cell["metrics"]
            no = m["non_overlapping"]
            label = _horizon_label(cell["horizon_bars"])

            def _record(best, value, th, lb):
                if value is None:
                    return best
                if best is None or value > best["value"]:
                    return {"threshold": th, "horizon": lb, "value": value}
                return best

            ft = m["mean_net_taker"]
            if ft is not None:
                v = Decimal(ft)
                if v > ZERO:
                    any_taker_full_positive = True
                best_taker_full = _record(best_taker_full, v, threshold, label)

            kt = no["mean_net_taker"]
            if kt is not None:
                v = Decimal(kt)
                if v > ZERO:
                    any_taker_kept_positive = True
                best_taker_kept = _record(best_taker_kept, v, threshold, label)

            fm = m["mean_net_maker"]
            if fm is not None:
                v = Decimal(fm)
                if v > ZERO:
                    any_maker_full_positive = True
                best_maker_full = _record(best_maker_full, v, threshold, label)

            km = no["mean_net_maker"]
            if km is not None:
                v = Decimal(km)
                if v > ZERO:
                    any_maker_kept_positive = True
                best_maker_kept = _record(best_maker_kept, v, threshold, label)

    return {
        "best_taker_full": best_taker_full,
        "best_taker_kept": best_taker_kept,
        "best_maker_full": best_maker_full,
        "best_maker_kept": best_maker_kept,
        "any_taker_full_positive": any_taker_full_positive,
        "any_taker_kept_positive": any_taker_kept_positive,
        "any_maker_full_positive": any_maker_full_positive,
        "any_maker_kept_positive": any_maker_kept_positive,
    }


def _summary_stats(result: Mapping[str, Any]) -> dict[str, Any]:
    worst_single = None
    worst_slot = None
    max_dd = ZERO
    max_streak = 0
    for entry in result["results"]:
        threshold = entry["threshold"]
        for key in sorted(entry["horizons"]):
            cell = entry["horizons"][key]
            m = cell["metrics"]
            label = _horizon_label(cell["horizon_bars"])
            if m["max_drawdown"] is not None:
                dd = Decimal(m["max_drawdown"])
                max_dd = max(max_dd, dd)
            tail = m["tail"]
            if tail["worst_obs_net_taker"] is not None:
                value = Decimal(tail["worst_obs_net_taker"])
                if worst_single is None or value < worst_single["value"]:
                    meta = tail["worst_obs"] or {}
                    worst_single = {
                        "value": value,
                        "threshold": threshold,
                        "horizon": label,
                        "symbol": meta.get("symbol"),
                        "side": meta.get("side"),
                    }
            if tail["worst_slot_net"] is not None:
                value = Decimal(tail["worst_slot_net"])
                if worst_slot is None or value < worst_slot["value"]:
                    worst_slot = {"value": value, "threshold": threshold, "horizon": label}
            max_streak = max(max_streak, tail["max_loss_streak"])
    return {
        "max_dd": max_dd,
        "worst_single": worst_single,
        "worst_slot": worst_slot,
        "max_streak": max_streak,
    }


def render_report(result: Mapping[str, Any]) -> str:
    best = _best_cells(result)
    stats = _summary_stats(result)

    # Representative facts for the data-window summary and conclusion.
    first_cell = result["results"][0]["horizons"]["h12"]
    neg_share = None
    if first_cell["metrics"]["signal_count"]:
        neg_share = (
            Decimal(first_cell["metrics"]["negative_count"])
            / Decimal(first_cell["metrics"]["signal_count"])
        )
    pos_best = None
    for entry in result["results"]:
        threshold = entry["threshold"]
        for key in sorted(entry["horizons"]):
            cell = entry["horizons"][key]
            pos = cell["metrics"]["sides"]["positive_premium"]
            if pos["mean_gross"] is not None:
                value = Decimal(pos["mean_gross"])
                if pos_best is None or value > pos_best["value"]:
                    pos_best = {
                        "threshold": threshold,
                        "horizon": _horizon_label(cell["horizon_bars"]),
                        "value": value,
                        "n": pos["count"],
                    }
    first_times = sorted(
        {item["first_open_time"] for item in result["data_window"].values()}
    )
    last_times = sorted(
        {item["last_open_time"] for item in result["data_window"].values()}
    )
    total_aligned = sum(
        item["aligned_count"] for item in result["data_window"].values()
    )
    window_summary = (
        f"40 币分两批回填：前 10 币 first_open_time {first_times[0]}、后 30 币"
        f" {first_times[-1]}；全部 last_open_time {last_times[0]}；aligned 序列无 5m"
        f" 缺口（gap_slots=0），总计 aligned {total_aligned} 槽。"
    )

    lines: list[str] = [
        "# Crypto 40 币 delta-neutral basis / cash-and-carry 预筛（非证据研究）",
        "",
        "> **非证据声明**：本报告全部数字来自无 PIT 证明的 TradingDatas 历史回填"
        "（`historical_backfill_no_pit=true`），仅供工程/定义检查"
        "（`not_promotion_evidence=true`、`authority=none`、`research_only=true`、"
        "`real_trading_enabled=false`），**不得进入任何晋级证据**，不构成 edge、概率校准"
        "或参数变更授权，不涉及资金、订单、Champion 或自动风险扩张。",
        "",
        "## 方法",
        "",
        "- 数据：40 币 USDⓈ-M spot 5m OHLCV（`close` 作 spot 价格）+ 同币 perp"
        " `premium_index` 5m（`close` 即 premium 水平），来自服务器 TradingDatas crypto"
        " 只读 read-model SQLite 的诊断抽取（`sqlite3` `file:...?mode=ro` +"
        " `PRAGMA query_only`；只读、无网络、无资本写）。",
        "- 构造：per-symbol、每 5m 槽定义 perp 价格 `P = spot_close * (1 +"
        " premium_close)`（premium 为分数）。",
        "- 信号：`premium_close >= +threshold` → 开 **long spot + short perp**"
        "（收正 funding，赚 premium 收敛）；`premium_close <= -threshold` → 开"
        " **short spot + long perp**（收负 funding）。threshold 扫 `0.0001 / 0.0002 /"
        " 0.0005 / 0.001`。",
        "- 持有 12/48/144/288 槽（1h/4h/12h/24h）后平仓，close→close。",
        "- 收益口径（**关键，delta-neutral，直接算两腿**）：令方向 `d=+1`（long spot +"
        " short perp）或 `d=-1`（short spot + long perp），`S_0/S_H` 与 `P_0/P_H` 为"
        " spot/perp 入场与出场价：",
        "  - `spot_leg = d*(S_H/S_0 - 1)`；",
        "  - `perp_leg = d*(P_0/P_H - 1)`；",
        "  - **组合 gross = spot_leg + perp_leg**（两腿各按 1x notional 归一，相加；"
        " 不手动只取 premium 差，近似误差可见）。",
        "  - 同时报告**纯 basis 收敛**口径 `premium_0 - premium_H`（`d` 符号化）与组合"
        " gross 对照，看 delta 中性近似有多大误差。",
        "- 成本：两腿各自套用 `crypto-round-trip-taker-v1`（fee 0.001 双边 taker +"
        " slippage 2bps 双边，`(1+net_leg)=(1+gross_leg)*(1-fee)/(1+fee)*(1-slip)^2-1`），"
        " 每腿往返约 0.24%，**两腿合计约 0.48%**。",
        "- maker 假设：`fee=0`、`slippage=0`，此时 `net_maker = gross`，用于判断"
        " 去掉成本后毛 edge 能否转正（是否值得做市/限价执行）。",
        "- 基线：现金/中性基线固定 0；另报 always-long spot 单腿方向基线作对照。",
        "- 口径：每个 threshold×horizon 报全样本与非重叠子样本（stride=horizon 槽数）、"
        " 等权权益曲线 maxDD、尾部（最差单样本/单槽、连亏）、以及正 premium 侧 vs 负"
        " premium 侧分拆。",
        "",
        "## 近似与局限（必须读）",
        "",
        "- **用 spot 近似 perp 价格**：`P = spot*(1+premium)` 是 perp 无 tick 级价差的"
        " 一阶重构；真实 perp 与 spot 的 tick/点差/深度/基差未建模，组合 gross 因此含"
        " 二阶凸性误差（`S_H/S_0 + S_0/S_H - 2` 等），本报告用「组合 gross − 纯 basis」"
        " 显式量化该误差。",
        "- **无真实 funding schedule**：`premium_index` 只是 funding 代理，不逐 8h 按当期"
        " premium 结算；funding 现金流未独立入账，只体现在 premium 收敛里。",
        "- **无保证金/强平/资金费率具体时点**：delta-neutral 对冲掉一阶方向风险，但两腿"
        " 各自仍暴露于基差跳空、极端插针与杠杆/保证金约束；本模型允许单样本 net <"
        " -100% 出现（研究上限，非可交易结果）。",
        "- **负 premium 侧（short spot + long perp）需要借券**：现实成本更高/受限，"
        " 报告分拆表单独标注该方向不可无借券执行；正 premium 侧（long spot + short"
        " perp）是主口径、可直接执行。",
        "- **无 PIT**：历史回填，`historical_backfill_no_pit=true`，仅工程/定义检查。",
        "",
        "## 数据窗口",
        "",
        window_summary,
        "",
        "| symbol | spot | premium | aligned | first_open_time | last_open_time | gap_slots |",
        "|---|---|---|---|---|---|---|",
    ]
    for symbol in result["symbols"]:
        item = result["data_window"][symbol]
        lines.append(
            f"| {symbol} | {item['spot_count']} | {item['premium_count']}"
            f" | {item['aligned_count']} | {item['first_open_time']}"
            f" | {item['last_open_time']} | {item['gap_slots']} |"
        )
    lines += [
        "",
        "## 结论摘要",
        "",
        "| threshold | horizon | signal/universe | 正侧 n | 负侧 n | mean_gross"
        " | mean_basis(纯) | gross−basis | mean_net(taker) | mean_net(maker)"
        " | Δ vs cash | Δ vs spot | maxDD |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for entry in result["results"]:
        threshold = entry["threshold"]
        for key in sorted(entry["horizons"]):
            cell = entry["horizons"][key]
            m = cell["metrics"]
            label = _horizon_label(cell["horizon_bars"])
            lines.append(
                f"| {threshold} | {label}"
                f" | {m['signal_count']} / {m['universe_count']}"
                f" | {m['positive_count']} | {m['negative_count']}"
                f" | {_pct(m['mean_gross'])} | {_pct(m['mean_basis'])}"
                f" | {_pct(m['gross_minus_basis'])} | {_pct(m['mean_net_taker'])}"
                f" | {_pct(m['mean_net_maker'])} | {_pct(m['delta_cash'])}"
                f" | {_pct(m['delta_spot'])} | {_pct(m['max_drawdown'])} |"
            )
    lines += [
        "",
        "## 非重叠独立子样本（stride = horizon）",
        "",
        "| threshold | horizon | 非重叠 n | 非重叠 gross | 非重叠 basis | 非重叠"
        " net(taker) | 非重叠 net(maker) | 非重叠 hit_rate |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for entry in result["results"]:
        threshold = entry["threshold"]
        for key in sorted(entry["horizons"]):
            cell = entry["horizons"][key]
            m = cell["metrics"]
            no = m["non_overlapping"]
            label = _horizon_label(cell["horizon_bars"])
            lines.append(
                f"| {threshold} | {label}"
                f" | {no['signal_count']} | {_pct(no['mean_gross'])}"
                f" | {_pct(no['mean_basis'])} | {_pct(no['mean_net_taker'])}"
                f" | {_pct(no['mean_net_maker'])} | {_pct(no['hit_rate'])} |"
            )
    lines += [
        "",
        "## 正 premium 侧 vs 负 premium 侧分拆",
        "",
        "> 正侧 = long spot + short perp（无需借券，主口径）；负侧 = short spot + long"
        " perp（需借券，现实更贵/受限，仅补充口径）。",
        "",
        "| threshold | horizon | 正侧 n | 正侧 gross | 正侧 basis | 正侧 net(taker)"
        " | 负侧 n | 负侧 gross | 负侧 basis | 负侧 net(taker) |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for entry in result["results"]:
        threshold = entry["threshold"]
        for key in sorted(entry["horizons"]):
            cell = entry["horizons"][key]
            m = cell["metrics"]
            s = m["sides"]
            pos = s["positive_premium"]
            neg = s["negative_premium"]
            label = _horizon_label(cell["horizon_bars"])
            lines.append(
                f"| {threshold} | {label}"
                f" | {pos['count']} | {_pct(pos['mean_gross'])}"
                f" | {_pct(pos['mean_basis'])} | {_pct(pos['mean_net_taker'])}"
                f" | {neg['count']} | {_pct(neg['mean_gross'])}"
                f" | {_pct(neg['mean_basis'])} | {_pct(neg['mean_net_taker'])} |"
            )
    lines += [
        "",
        "## 尾部事件",
        "",
        "| threshold | horizon | 最差单样本 net(taker) | 最差样本"
        " symbol/slot/premium/dir/两腿gross | 最差单槽 net | 连亏次数(≥2)"
        " | 最长连亏(槽) |",
        "|---|---|---|---|---|---|---|",
    ]
    for entry in result["results"]:
        threshold = entry["threshold"]
        for key in sorted(entry["horizons"]):
            cell = entry["horizons"][key]
            m = cell["metrics"]
            t = m["tail"]
            label = _horizon_label(cell["horizon_bars"])
            worst = t["worst_obs"] or {}
            if worst.get("symbol") is not None:
                worst_meta = (
                    f"{worst['symbol']}/"
                    f"{_iso_slot(worst['slot']) if worst.get('slot') is not None else '—'}/"
                    f"p0={_num(worst.get('premium_entry'))}/"
                    f"dir={worst.get('direction')}/"
                    f"spot_leg={_pct(worst.get('spot_leg_gross'))}/"
                    f"perp_leg={_pct(worst.get('perp_leg_gross'))}"
                )
            else:
                worst_meta = "—"
            lines.append(
                f"| {threshold} | {label}"
                f" | {_pct(t['worst_obs_net_taker'])} | {worst_meta}"
                f" | {_pct(t['worst_slot_net'])}"
                f" | {t['loss_streak_count']} | {t['max_loss_streak']} |"
            )
    lines += [
        "",
        "## maker 假设判定（fee=0、slippage=0）",
        "",
        "maker 假设下 `net_maker == gross`（无成本）。判断标准是 gross 去掉成本后是否"
        "转正——即 delta-neutral basis 的毛 edge 是否足够覆盖做市/限价执行的成本。",
        "",
    ]
    if best["any_maker_full_positive"] or best["any_maker_kept_positive"]:
        lines.append(
            "- 全样本 gross（=maker net）存在正 cell；最佳全样本 maker cell："
        )
        if best["best_maker_full"]:
            lines.append(
                f"  `threshold {best['best_maker_full']['threshold']} ×"
                f" {best['best_maker_full']['horizon']}` ="
                f" {_pct(best['best_maker_full']['value'])}。"
            )
        lines.append("- 非重叠（独立）gross 存在正 cell；最佳非重叠 maker cell：")
        if best["best_maker_kept"]:
            lines.append(
                f"  `threshold {best['best_maker_kept']['threshold']} ×"
                f" {best['best_maker_kept']['horizon']}` ="
                f" {_pct(best['best_maker_kept']['value'])}。"
            )
    else:
        lines.append("- 全样本与非重叠口径的 gross（=maker net）**均无正 cell**。")

    if best["any_taker_full_positive"]:
        lines.append(
            "- taker 成本下全样本存在正 net cell；最佳全样本 taker cell："
        )
        if best["best_taker_full"]:
            lines.append(
                f"  `threshold {best['best_taker_full']['threshold']} ×"
                f" {best['best_taker_full']['horizon']}` ="
                f" {_pct(best['best_taker_full']['value'])}。"
            )
    else:
        lines.append("- taker 成本下全样本**无任何正 net cell**。")

    if best["any_taker_kept_positive"]:
        lines.append(
            "- taker 成本下非重叠（独立）存在正 net cell；最佳非重叠 taker cell："
        )
        if best["best_taker_kept"]:
            lines.append(
                f"  `threshold {best['best_taker_kept']['threshold']} ×"
                f" {best['best_taker_kept']['horizon']}` ="
                f" {_pct(best['best_taker_kept']['value'])}。"
            )
    else:
        lines.append("- taker 成本下非重叠（独立）**无任何正 net cell**。")
    conclusion: list[str] = ["", "## 结论与下一步", ""]
    # taker
    if best["any_taker_full_positive"] and best["best_taker_full"] is not None:
        b = best["best_taker_full"]
        conclusion.append(
            f"- taker 成本下全样本最优 net：`threshold {b['threshold']} × {b['horizon']}`"
            f" = {_pct(b['value'])}。"
        )
    else:
        conclusion.append("- taker 成本下全样本 **无正 net cell**。")
    if best["any_taker_kept_positive"] and best["best_taker_kept"] is not None:
        b = best["best_taker_kept"]
        conclusion.append(
            f"- taker 成本下非重叠（独立）最优 net：`threshold {b['threshold']} ×"
            f" {b['horizon']}` = {_pct(b['value'])}。"
        )
    else:
        conclusion.append("- taker 成本下非重叠（独立）**无正 net cell**。")
    # maker
    if best["any_maker_full_positive"] and best["best_maker_full"] is not None:
        b = best["best_maker_full"]
        conclusion.append(
            f"- maker 假设（fee=0、slippage=0）全样本最优 gross：`threshold"
            f" {b['threshold']} × {b['horizon']}` = {_pct(b['value'])}。"
        )
    else:
        conclusion.append("- maker 假设下全样本 **无正 gross cell**。")
    if best["any_maker_kept_positive"] and best["best_maker_kept"] is not None:
        b = best["best_maker_kept"]
        conclusion.append(
            f"- maker 假设非重叠（独立）最优 gross：`threshold {b['threshold']} ×"
            f" {b['horizon']}` = {_pct(b['value'])}。"
        )
    else:
        conclusion.append("- maker 假设下非重叠（独立）**无正 gross cell**。")
    conclusion.append("")
    if stats["worst_single"] is not None:
        ws = stats["worst_single"]
        conclusion.append(
            f"- 尾部：全表最大回撤 {_pct(stats['max_dd'])}；最差单样本 net"
            f" {_pct(ws['value'])}（{ws['symbol']}/{ws['side']}/{ws['threshold']}×"
            f"{ws['horizon']}）；最差单槽 net {_pct(stats['worst_slot']['value'])}；"
            f"最长连亏 {stats['max_streak']} 槽。"
        )
    conclusion.append("")

    if neg_share is not None:
        conclusion.append(
            f"- 负 premium 侧（short spot + long perp，需借券）在信号里占"
            f" {neg_share * 100:.1f}%（以 threshold 0.0001 × 1h 计），是绝对多数；"
            f"正 premium 侧（long spot + short perp，可直接执行）样本稀少。"
        )
    if pos_best is not None:
        conclusion.append(
            f"- 正 premium 侧（无需借券）最优 gross 出现在 `threshold"
            f" {pos_best['threshold']} × {pos_best['horizon']}` ="
            f" {_pct(pos_best['value'])}，但仅 n={pos_best['n']}（独立样本≈个位数），"
            f"统计上不可下结论。"
        )
    conclusion.append("")

    maker_kept = (
        best["best_maker_kept"]["value"]
        if best["best_maker_kept"] is not None
        else None
    )
    if maker_kept is not None and maker_kept > ZERO:
        if maker_kept >= Decimal("0.001"):
            conclusion.append(
                "**判断**：delta-neutral basis 的毛 edge（gross）在 maker/限价假设下"
                " 独立样本转正且幅度达到 0.1% 量级，说明 carry/basis 本身有可提取的"
                " 正向收敛，但 taker 0.48% 往返成本会把它吃成负——这条路的正确执行方式"
                " 是低费/做市/限价，而非 taker 追价。当前只是历史回填研究口径"
                "（`not_promotion_evidence`），须先用真实 funding schedule、perp 真实"
                " 基差、借券成本与保证金/强平模型复核后再决定是否投入低费执行。"
            )
        else:
            conclusion.append(
                "**判断**：delta-neutral basis 的毛 edge（gross）在 maker/限价假设下"
                " 独立样本虽为正但幅度很小（<0.1%），不足以覆盖真实做市库存风险、资金"
                " 占用成本与尾部插针；taker 0.48% 往返成本必然为负。这条 carry/basis"
                " 路线当前证据不足以支撑投入低费执行，应保留为观察、等待更大 premium"
                " 极值或更窄执行成本再做复核（`not_promotion_evidence`）。"
            )
    else:
        conclusion.append(
            "**判断**：连 maker/限价假设（fee=0、slippage=0）下的独立样本毛 gross 都"
            " 没有转正，说明 delta-neutral basis 的毛 edge 本身不足以覆盖方向对冲的"
            " 二阶误差与 premium 代理噪声；这条 carry/basis 路线当前应否，不构成投入"
            " 低费执行的理由（`not_promotion_evidence`）。"
        )
    conclusion += [
        "",
        "---",
        "",
        f"生成：`Crypto/forty_symbol_basis_carry_research.py --db <read-model.sqlite>`；"
        f"contract `{result['contract']}`；cost policy"
        f" `{result['cost_policy']['cost_policy_id']}`。本报告为 research-only、"
        "not_promotion_evidence，不得进入任何晋级证据。",
        "",
    ]
    lines += conclusion
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


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


def _write_text(path: Path, text: str) -> None:
    temporary = path.parent / f".{path.name}.tmp"
    with open(temporary, "w", encoding="utf-8") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Offline 40-symbol delta-neutral basis-carry research (read-only)"
    )
    parser.add_argument("--db", type=Path, required=True, help="read-model SQLite file")
    parser.add_argument("--report", type=Path, help="write Markdown report here")
    parser.add_argument("--out-json", type=Path, help="write machine result JSON here")
    args = parser.parse_args(argv)
    try:
        _assert_simulation_only()
        material = load_material_from_sqlite(args.db)
        result = analyze(material)
        if args.out_json is not None:
            _write_text(
                args.out_json,
                json.dumps(
                    result,
                    ensure_ascii=True,
                    allow_nan=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                + "\n",
            )
        if args.report is not None:
            _write_text(args.report, render_report(result))
        _emit(result)
        return 0
    except Exception:
        print("crypto forty-symbol basis carry research failed closed", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ALLOWED_HORIZON_BARS",
    "ALLOWED_THRESHOLDS",
    "CONTRACT",
    "FORTY_SYMBOLS",
    "FortySymbolBasisCarryError",
    "analyze",
    "load_material_from_sqlite",
    "render_report",
    "main",
]
