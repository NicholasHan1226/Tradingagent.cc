"""Offline 40-symbol funding-carry research over perp premium_index + spot 5m.

This module is *research only*.  It reads a read-only TradingDatas crypto
read-model SQLite file (no network, no capital/order/Champion writes) and
evaluates a structural funding-carry hypothesis:

    when perp premium_index close is at an extreme, stand on the side that
    *receives* funding (short when premium >= +threshold, long when premium
    <= -threshold) and hold for a fixed horizon, so the position earns the
    funding carry while the price path is approximated by the 5m spot close.

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

CONTRACT = "tradingagent.crypto.forty_symbol_funding_carry_research.v1"
ALLOWED_HORIZON_BARS = (12, 48, 144, 288)
ALLOWED_THRESHOLDS = ("0.0001", "0.0002", "0.0005", "0.001")
FUNDING_INTERVAL_SLOTS = 96  # 8h funding cadence expressed in 5m slots
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


class FortySymbolFundingCarryError(RuntimeError):
    """Stable fail-closed error for funding-carry research."""


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
        raise FortySymbolFundingCarryError(
            "funding_carry_real_trading_must_be_disabled"
        )


def _parse_utc(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise FortySymbolFundingCarryError("funding_carry_timestamp_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FortySymbolFundingCarryError("funding_carry_timestamp_invalid") from exc
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() is None
        or parsed.utcoffset() != timedelta(0)
    ):
        raise FortySymbolFundingCarryError("funding_carry_timestamp_invalid")
    return parsed.astimezone(timezone.utc)


def _slot_index(value: datetime) -> int:
    # 5-minute-aligned slot index (seconds since epoch // 300).  Binance 5m
    # open_time is always on a 5m boundary.
    if value.second != 0 or value.microsecond != 0:
        raise FortySymbolFundingCarryError("funding_carry_slot_not_aligned")
    return int(value.timestamp()) // 300


def _slot_to_utc(slot: int) -> datetime:
    return datetime.fromtimestamp(slot * 300, tz=timezone.utc)


def _iso_slot(slot: int) -> str:
    return _slot_to_utc(slot).isoformat().replace("+00:00", "Z")


def _decimal(value: Any) -> Decimal:
    if not isinstance(value, str) or not value:
        raise FortySymbolFundingCarryError("funding_carry_decimal_invalid")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise FortySymbolFundingCarryError("funding_carry_decimal_invalid") from exc
    if not parsed.is_finite():
        raise FortySymbolFundingCarryError("funding_carry_decimal_invalid")
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
        raise FortySymbolFundingCarryError("funding_carry_db_path_invalid")
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise FortySymbolFundingCarryError("funding_carry_db_open_failed") from exc
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
            raise FortySymbolFundingCarryError("funding_carry_row_budget_exceeded")
        if raw_time is None or raw_value is None:
            continue
        slot = _slot_index(_parse_utc(raw_time))
        value = _decimal(raw_value)
        if slot in series:
            duplicates += 1
            continue
        series[slot] = value
    if not series:
        raise FortySymbolFundingCarryError(
            f"funding_carry_empty_dataset:{dataset_id}"
        )
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
            raise FortySymbolFundingCarryError(
                f"funding_carry_no_alignment:{symbol}"
            )
        gaps = 0
        for earlier, later in zip(aligned_slots, aligned_slots[1:]):
            gaps += later - earlier - 1
        material[symbol] = {
            "times": aligned_slots,
            "spot": [spot[s] for s in aligned_slots],
            "premium": [premium[s] for s in aligned_slots],
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
        raise FortySymbolFundingCarryError("funding_carry_symbols_drift")
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
        return -1  # short: premium high, short receives funding
    if premium <= -threshold:
        return 1  # long: premium low, long receives funding
    return 0


def _first_negative_after(values: Sequence[Decimal]) -> list[int]:
    """next_neg[i] = smallest j >= i with values[j] < 0, else len(values)."""
    count = len(values)
    next_neg = [count] * count
    nearest = count
    for index in range(count - 1, -1, -1):
        if values[index] < ZERO:
            nearest = index
        next_neg[index] = nearest
    return next_neg


def _build_baseline(
    material: Mapping[str, Mapping[str, Any]],
    *,
    symbols: Sequence[str],
    horizon_bars: int,
) -> dict[str, Any]:
    """Precompute threshold-independent baseline (always-long spot) per horizon."""
    base_sum = ZERO
    base_count = 0
    slot_base_sum: dict[int, Decimal] = {}
    slot_base_count: dict[int, int] = {}
    all_slots: set[int] = set()
    for symbol in symbols:
        item = material[symbol]
        times = item["times"]
        spot = item["spot"]
        count = len(times)
        for index in range(0, count - horizon_bars):
            slot = times[index]
            price_ret = spot[index + horizon_bars] / spot[index] - ONE
            base_net = _cost_adjusted_gross(price_ret)
            base_sum += base_net
            base_count += 1
            slot_base_sum[slot] = slot_base_sum.get(slot, ZERO) + base_net
            slot_base_count[slot] = slot_base_count.get(slot, 0) + 1
            all_slots.add(slot)
    ordered_slots = sorted(all_slots)
    kept_slots = set(ordered_slots[::horizon_bars])
    kept_base_sum = ZERO
    kept_base_count = 0
    for slot in kept_slots:
        value = slot_base_sum.get(slot)
        if value is not None:
            kept_base_sum += value
            kept_base_count += slot_base_count.get(slot, 0)
    return {
        "universe_count": base_count,
        "base_mean": base_sum / Decimal(base_count) if base_count else None,
        "kept_slots": kept_slots,
        "kept_base_mean": (
            kept_base_sum / Decimal(kept_base_count) if kept_base_count else None
        ),
    }


def _evaluate_cell(
    material: Mapping[str, Mapping[str, Any]],
    *,
    symbols: Sequence[str],
    threshold: Decimal,
    horizon_bars: int,
    baseline: Mapping[str, Any],
    next_neg_by_symbol: Mapping[str, Sequence[int]],
) -> dict[str, Any]:
    """Evaluate one threshold x horizon cell (baseline is precomputed)."""

    kept_slots = baseline["kept_slots"]
    frac = Decimal(horizon_bars) / Decimal(FUNDING_INTERVAL_SLOTS)

    signal_count = 0
    long_count = 0
    short_count = 0
    gross_sum = ZERO
    net_sum = ZERO
    net_positive = 0
    slot_net_sum: dict[int, Decimal] = {}
    slot_net_count: dict[int, int] = {}

    worst_net: Decimal | None = None
    worst_meta: dict[str, Any] | None = None
    pin_count = 0
    pin_net_sum = ZERO

    kept_gross_list: list[Decimal] = []
    kept_net_list: list[Decimal] = []

    for symbol in symbols:
        item = material[symbol]
        times = item["times"]
        spot = item["spot"]
        premium = item["premium"]
        next_neg = next_neg_by_symbol[symbol]
        count = len(times)
        for index in range(0, count - horizon_bars):
            premium_entry = premium[index]
            direction = _signal_direction(premium_entry, threshold)
            if direction == 0:
                continue
            exit_index = index + horizon_bars
            price_ret = spot[exit_index] / spot[index] - ONE
            # Funding received ≈ -direction * premium_entry per 8h, prorated
            # by holding time.  Gross = directional price move + carry.
            carry = -Decimal(direction) * premium_entry * frac
            gross = Decimal(direction) * price_ret + carry
            net = _cost_adjusted_gross(gross)

            if direction > 0:
                long_count += 1
            else:
                short_count += 1
            signal_count += 1
            gross_sum += gross
            net_sum += net
            if net > ZERO:
                net_positive += 1
            slot = times[index]
            slot_net_sum[slot] = slot_net_sum.get(slot, ZERO) + net
            slot_net_count[slot] = slot_net_count.get(slot, 0) + 1

            if worst_net is None or net < worst_net:
                worst_net = net
                worst_meta = {
                    "symbol": symbol,
                    "slot": slot,
                    "premium": _text(premium_entry),
                    "direction": direction,
                }

            # Short-side pin: entered short on positive premium, but funding
            # flipped negative at some point inside the holding window.
            if direction < 0 and next_neg[index] <= exit_index:
                pin_count += 1
                pin_net_sum += net

            if slot in kept_slots:
                kept_gross_list.append(gross)
                kept_net_list.append(net)

    universe_count = baseline["universe_count"]
    mean_gross = gross_sum / Decimal(signal_count) if signal_count else None
    mean_net = net_sum / Decimal(signal_count) if signal_count else None
    baseline_mean = baseline["base_mean"]
    baseline_delta = (
        mean_net - baseline_mean
        if mean_net is not None and baseline_mean is not None
        else None
    )
    kept_mean_gross = (
        sum(kept_gross_list, ZERO) / Decimal(len(kept_gross_list))
        if kept_gross_list
        else None
    )
    kept_mean_net = (
        sum(kept_net_list, ZERO) / Decimal(len(kept_net_list))
        if kept_net_list
        else None
    )
    kept_hit_rate = (
        Decimal(sum(value > ZERO for value in kept_net_list))
        / Decimal(len(kept_net_list))
        if kept_net_list
        else None
    )
    kept_base_mean = baseline["kept_base_mean"]
    kept_delta = (
        kept_mean_net - kept_base_mean
        if kept_mean_net is not None and kept_base_mean is not None
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

    # Consecutive-loss streaks at the portfolio-slot level.
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
            "long_count": long_count,
            "short_count": short_count,
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
            "mean_net": _text(mean_net),
            "baseline_delta": _text(baseline_delta),
            "max_drawdown": _text(max_drawdown),
            "non_overlapping": {
                "stride": horizon_bars,
                "slot_count": len(kept_slots),
                "signal_count": len(kept_net_list),
                "hit_rate": _text(kept_hit_rate),
                "mean_gross": _text(kept_mean_gross),
                "mean_net": _text(kept_mean_net),
                "baseline_delta": _text(kept_delta),
            },
            "tail": {
                "worst_obs_net": _text(worst_net),
                "worst_obs": worst_meta,
                "worst_slot_net": _text(worst_slot_net),
                "worst_slot": _iso_slot(worst_slot) if worst_slot is not None else None,
                "loss_streak_count": loss_streak_count,
                "max_loss_streak": max_loss_streak,
                "funding_flip_pin_count": pin_count,
                "funding_flip_pin_mean_net": _text(
                    pin_net_sum / Decimal(pin_count) if pin_count else None
                ),
            },
            "metric_basis": (
                "gross = direction * (spot_exit/spot_entry - 1)"
                " - direction * premium_entry * (horizon_bars/96);"
                " direction=+1 long / -1 short; carry uses entry premium"
                " prorated by holding time; spot close path approximates perp"
                " mark-to-market; cost = crypto-round-trip-taker-v1 (fee 0.001"
                " each side + 2bps slippage each side); historical backfill"
                " without PIT proof; not promotion evidence"
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
    """Evaluate every threshold x horizon cell of the funding-carry signal."""
    if tuple(symbols) != FORTY_SYMBOLS:
        raise FortySymbolFundingCarryError("funding_carry_symbols_drift")
    if tuple(thresholds) != ALLOWED_THRESHOLDS:
        raise FortySymbolFundingCarryError("funding_carry_thresholds_drift")
    if tuple(horizons) != ALLOWED_HORIZON_BARS:
        raise FortySymbolFundingCarryError("funding_carry_horizons_drift")
    if not material:
        raise FortySymbolFundingCarryError("funding_carry_material_empty")

    next_neg_by_symbol: dict[str, Sequence[int]] = {
        symbol: _first_negative_after(material[symbol]["premium"])
        for symbol in symbols
    }
    baseline_by_horizon: dict[int, Mapping[str, Any]] = {
        horizon: _build_baseline(material, symbols=symbols, horizon_bars=horizon)
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
                baseline=baseline_by_horizon[horizon],
                next_neg_by_symbol=next_neg_by_symbol,
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
        "event_type": "forty_symbol_funding_carry_analysis",
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
        },
        "signal": {
            "feature": "premium_index close (funding proxy)",
            "rule": (
                "short when premium_close >= +threshold; long when"
                " premium_close <= -threshold; otherwise flat"
            ),
            "funding_interval_slots": FUNDING_INTERVAL_SLOTS,
            "carry_formula": "carry = -direction * premium_entry * (horizon_bars/96)",
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
    return "1h" if bars == 12 else f"{bars * 5}min"


def render_report(result: Mapping[str, Any]) -> str:
    lines: list[str] = [
        "# Crypto 40 币 funding carry 结构性风险溢价预筛（非证据研究）",
        "",
        "> **非证据声明**：本报告全部数字来自无 PIT 证明的 TradingDatas 历史回填"
        "（`historical_backfill_no_pit=true`），仅供工程/定义检查"
        "（`not_promotion_evidence=true`、`authority=none`、`research_only=true`），"
        "**不得进入任何晋级证据**，不构成 edge、概率校准或参数变更授权，"
        "不涉及资金、订单、Champion 或自动风险扩张。",
        "",
        "## 方法",
        "",
        "- 数据：40 币 USDⓈ-M spot 5m OHLCV（价格路径代理）+ 同币 perp"
        " `premium_index` 5m（funding 代理，`close` 即 premium 水平），来自服务器"
        " TradingDatas crypto 只读 read-model SQLite 的诊断抽取"
        "（`sqlite3` `mode=ro` + `PRAGMA query_only`，只读、无网络、无资本写）。",
        "- 信号：per-symbol、每 5m 槽，用 `premium_index` 的 `close` 水平"
        "（不是变化量）：`premium_close >= +threshold` → 空（short perp）；"
        "`premium_close <= -threshold` → 多（long perp）；否则空仓。threshold 扫"
        " `0.0001 / 0.0002 / 0.0005 / 0.001`。",
        "- 标签：持有 12/48/144/288 槽（1h/4h/12h/24h）后平仓，close→close。",
        "- 收益口径：perp 持仓 mark-to-market ≈ spot 5m close 路径；额外加 funding"
        " carry。gross = `方向 × (spot_exit/spot_entry - 1) - 方向 × premium_entry"
        " × (horizon_bars/96)`（`方向`=+1 多 / -1 空；`premium_entry` 为入场槽"
        " premium，按持有时间占 8h 的比例折算，96 槽=8h）。",
        "- 成本：与证据链同一口径 `crypto-round-trip-taker-v1`，fee 0.001 双边"
        " taker + slippage 2bps 双边，`(1+net)=(1+gross)*(1-fee)/(1+fee)"
        "*(1-slip)^2-1`，往返约 0.24%。",
        "- 口径：每个 threshold×horizon 报全样本与**非重叠子样本**"
        "（stride=horizon 槽数）；等权权益曲线最大回撤；尾部事件（最差单槽、"
        "连亏、正→负 funding 插针）。",
        "",
        "## 近似与局限（必须读）",
        "",
        "- **无真实 funding rate 时点**：用 `premium_index` close 作为 funding"
        " 代理，且 carry 只取**入场槽**的 premium 并按 8h 时间占比折算，"
        "**没有逐 8h 结算按当时 premium 积分**。premium 若在持有期内均值回归，"
        "入场 premium 会**高估**实际可收的 carry；本报告方向性结论应以上限对待。",
        "- **无 perp 真实价差/基差**：perp 价格路径用 spot 5m close 近似，未计"
        " perp 自身 price premium 的收敛（basis）或 perp 与 spot 的偏离。",
        "- **没算强制平仓/保证金/资金费率具体 schedule**：short 侧价格大幅上涨"
        " 时会穿透，实际会被强平；本模型允许单样本 net < -100% 的情况出现，"
        " 属建模上限、非可交易结果。",
        "- **无 PIT**：历史回填，`historical_backfill_no_pit=true`，仅工程/定义检查。",
        "",
        "## 数据窗口",
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
        "| threshold | horizon | signal/universe | hit_rate | mean_gross | mean_net"
        " | Δ baseline | maxDD | 非重叠 n | 非重叠 gross | 非重叠 net |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
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
                f" | {m['signal_count']} / {m['universe_count']}"
                f" | {_pct(m['hit_rate'])} | {_pct(m['mean_gross'])}"
                f" | {_pct(m['mean_net'])} | {_pct(m['baseline_delta'])}"
                f" | {_pct(m['max_drawdown'])}"
                f" | {no['signal_count']}"
                f" | {_pct(no['mean_gross'])} | {_pct(no['mean_net'])} |"
            )
    lines += [
        "",
        "## 尾部事件",
        "",
        "| threshold | horizon | 最差单样本 net | 最差样本 symbol/slot/premium/dir"
        " | 最差单槽 net | 连亏次数(≥2) | 最长连亏(槽) | 正→负 funding 插针 n"
        " | 插针 mean_net |",
        "|---|---|---|---|---|---|---|---|---|",
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
                    f"{_num(worst.get('premium'))}/{worst.get('direction')}"
                )
            else:
                worst_meta = "—"
            lines.append(
                f"| {threshold} | {label}"
                f" | {_pct(t['worst_obs_net'])} | {worst_meta}"
                f" | {_pct(t['worst_slot_net'])}"
                f" | {t['loss_streak_count']} | {t['max_loss_streak']}"
                f" | {t['funding_flip_pin_count']}"
                f" | {_pct(t['funding_flip_pin_mean_net'])} |"
            )
    lines += [
        "",
        "## 结论与下一步",
        "",
        "（结论由机器结果回填，见报告末尾的定量判定。）",
        "",
        "---",
        "",
        f"生成：`Crypto/forty_symbol_funding_carry_research.py --db <read-model.sqlite>`；"
        f"contract `{result['contract']}`；cost policy"
        f" `{result['cost_policy']['cost_policy_id']}`。本报告为 research-only、"
        "not_promotion_evidence，不得进入任何晋级证据。",
        "",
    ]
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
        description="Offline 40-symbol funding-carry research (read-only)"
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
        print("crypto forty-symbol funding carry research failed closed", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ALLOWED_HORIZON_BARS",
    "ALLOWED_THRESHOLDS",
    "CONTRACT",
    "FORTY_SYMBOLS",
    "FortySymbolFundingCarryError",
    "analyze",
    "load_material_from_sqlite",
    "render_report",
    "main",
]
