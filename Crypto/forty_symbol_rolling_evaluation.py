"""Receipt-bound rolling evaluation entry over forty-symbol observation bars.

This module is the MVP-2 *rolling evaluation* entry point for the crypto lane
(docs/BACKLOG.md P1): it consumes the recovered forty-symbol observer's own
receipts -- the append-only store events plus the per-slot immutable bars
sidecars they bind -- and produces one deterministic, reproducible resolved-
outcome artifact per run.  Unlike the historical research modules, the input
bars are not re-fetched ad hoc: every evaluated row must re-derive its sidecar's
``identity_sha256``/``market_data_sha256``, every sidecar must match a success
event in the checksum-chained append-only log, and the selected slots must form
a gap-free 5-minute segment.  Labels never cross a gap; any missing slot fails
closed.

On that segment it runs exactly one pre-registered configuration of the existing
frozen champion momentum strategy (mirrors ``Crypto.fixture_sim.contracts.
FrozenChampionCandidate``: decision lookback 3 bars, regime lookback 12 bars,
minimum decision return 0.001, minimum regime return 0) with the champion exit
ladder (take-profit +3%, stop-loss -2%, 24h max hold, momentum reversal), plus
a simple buy-and-hold baseline, both net of declared taker fees and slippage.
No threshold or horizon scanning happens here.

The output is shadow-only by construction (``authority=none``,
``capital_write_eligible=false``); the mechanical recommendation is limited to
retain/downweight/disable/keep-accumulating language and never touches capital,
orders, Testnet/Live, or automatic risk expansion.  Analysis is pure and
offline.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Sequence

from Crypto.market_observation import (
    OBSERVATION_SYMBOLS_V40 as FORTY_SYMBOLS,
)
from Crypto.ten_symbol_factor_research import (
    FORTY_SYMBOL_FACTOR_RESEARCH_CONFIG,
    CryptoTenSymbolFactorProjectionError,
    _attach_eligibility,
    _open_store,
    _terminal_events,
)
from Crypto.ten_symbol_observation_store import (
    CryptoTenSymbolObservationStore,
    CryptoTenSymbolObservationStoreError,
)

CONTRACT = "tradingagent.crypto.forty_symbol_rolling_evaluation.v1"
EVENT_CONTRACT = "tradingagent.crypto.forty_symbol_observation_event.v1"

# Frozen champion parameters (single pre-registered configuration; no scan).
ENTRY_THRESHOLD = Decimal("0.001")
MINIMUM_REGIME_RETURN = Decimal("0")
DECISION_LOOKBACK_BARS = 3  # 15 minutes expressed in 5m bars
REGIME_LOOKBACK_BARS = 12  # 1 hour expressed in 5m bars
TAKE_PROFIT_RETURN = Decimal("0.03")
STOP_LOSS_RETURN = Decimal("-0.02")
MAX_HOLD_BARS = 288  # 24h expressed in 5m bars

# Declared execution costs: taker both legs (crypto-round-trip-taker-v1).
ENTRY_FEE = Decimal("0.001")
EXIT_FEE = Decimal("0.001")
SLIPPAGE_RATE = Decimal("0.0002")  # 2 bps per leg

# A recommendation other than "keep accumulating" requires this many resolved
# trips; below it the entry records an abstention-heavy insufficient-evidence
# outcome instead of pretending a small sample is a decision.
MIN_RESOLVED_TRIPS_FOR_RECOMMENDATION = 30

FIVE_MINUTE_SECONDS = 300
ZERO = Decimal("0")
ONE = Decimal("1")


class FortySymbolRollingEvaluationError(RuntimeError):
    """Stable fail-closed error for receipt-bound rolling evaluation."""


# ---------------------------------------------------------------------------
# Shared helpers (mirror the sibling research modules)
# ---------------------------------------------------------------------------


def _assert_simulation_only() -> None:
    if os.environ.get("REAL_TRADING_ENABLED") != "false":
        raise FortySymbolRollingEvaluationError(
            "rolling_evaluation_real_trading_must_be_disabled"
        )


def _parse_utc(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise FortySymbolRollingEvaluationError(
            "rolling_evaluation_timestamp_invalid"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FortySymbolRollingEvaluationError(
            "rolling_evaluation_timestamp_invalid"
        ) from exc
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() is None
        or parsed.utcoffset() != timedelta(0)
    ):
        raise FortySymbolRollingEvaluationError(
            "rolling_evaluation_timestamp_invalid"
        )
    return parsed.astimezone(timezone.utc)


def _iso_slot(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _decimal(value: Any) -> Decimal:
    if not isinstance(value, str) and not isinstance(value, int):
        raise FortySymbolRollingEvaluationError("rolling_evaluation_decimal_invalid")
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as exc:
        raise FortySymbolRollingEvaluationError(
            "rolling_evaluation_decimal_invalid"
        ) from exc
    if not parsed.is_finite():
        raise FortySymbolRollingEvaluationError("rolling_evaluation_decimal_invalid")
    return parsed


def _text(value: Decimal | int | None) -> str | None:
    if value is None:
        return None
    return format(value, "f")


def _round_trip_net(gross: Decimal) -> Decimal:
    """Four frictions: entry fee/slippage and exit fee/slippage (taker)."""

    bought = (ONE + gross) / (ONE + ENTRY_FEE)
    entered = bought * (ONE - SLIPPAGE_RATE)
    return entered * (ONE - EXIT_FEE) * (ONE - SLIPPAGE_RATE) - ONE


def _mean(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    total = ZERO
    for value in values:
        total += value
    return total / len(values)


def _max_drawdown(nets: Sequence[Decimal]) -> Decimal:
    """Max peak-to-trough of the cumulative resolved-net equity curve."""

    equity = ZERO
    peak = ZERO
    drawdown = ZERO
    for net in nets:
        equity += net
        peak = max(peak, equity)
        drawdown = min(drawdown, equity - peak)
    return drawdown


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _non_evidence_fields() -> dict[str, Any]:
    return {
        "authority": "none",
        "shadow_only_recommendation": True,
        "not_promotion_evidence": True,
        # Receipt integrity proves the source history is attributable.  It does
        # not prove a bar-only counterfactual had a tradeable quote or fill.
        "source_receipt_integrity_verified": True,
        "source_receipt_integrity_scope": "selected_eligible_segment_only",
        "tradeable_pit_verified": False,
        "receipt_bound_pit": False,
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
    }


# ---------------------------------------------------------------------------
# Receipt loading and verification
# ---------------------------------------------------------------------------


def _read_head_bytes(store: CryptoTenSymbolObservationStore) -> bytes:
    """Read the immutable checkpoint without invoking its repair path."""

    try:
        metadata = store.head_path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise FortySymbolRollingEvaluationError(
                "rolling_evaluation_store_head_invalid"
            )
        return store.head_path.read_bytes()
    except FortySymbolRollingEvaluationError:
        raise
    except OSError as exc:
        raise FortySymbolRollingEvaluationError(
            "rolling_evaluation_store_head_unavailable"
        ) from exc


def _verified_store_units(
    store_root: Path,
) -> tuple[CryptoTenSymbolObservationStore, list[dict[str, Any]], dict[str, Any]]:
    """Read one stable, full-store snapshot with a read-only head anchor."""

    try:
        store = _open_store(store_root, FORTY_SYMBOL_FACTOR_RESEARCH_CONFIG)
        before = _read_head_bytes(store)
        events = store.events_read_only()
        after = _read_head_bytes(store)
        if before != after:
            raise FortySymbolRollingEvaluationError(
                "rolling_evaluation_store_advanced_retry"
            )
        head = json.loads(before)
        if not isinstance(head, dict):
            raise FortySymbolRollingEvaluationError("rolling_evaluation_store_head_invalid")
        # Reuse the store's exact head schema/hash validator but never call
        # head(), which is allowed to repair a stale checkpoint for a writer.
        store._verify_head_structure(head)
        if (
            not events
            or head.get("sequence") != len(events)
            or head.get("last_checksum") != events[-1].get("checksum")
            or head.get("latest_event_id") != events[-1].get("event_id")
            or head.get("latest_event_checksum") != events[-1].get("checksum")
            or head.get("segment_count") != len(store._segment_paths())
        ):
            raise FortySymbolRollingEvaluationError("rolling_evaluation_store_head_invalid")
        current_sha = (
            _sha256_file(store.events_path) if store.events_path.exists() else None
        )
        if head.get("current_file_sha256") != current_sha:
            raise FortySymbolRollingEvaluationError("rolling_evaluation_store_head_invalid")
        units = _terminal_events(store)
        for unit in units:
            _attach_eligibility(store, unit, FORTY_SYMBOL_FACTOR_RESEARCH_CONFIG)
        if _read_head_bytes(store) != before:
            raise FortySymbolRollingEvaluationError(
                "rolling_evaluation_store_advanced_retry"
            )
        return store, units, head
    except FortySymbolRollingEvaluationError:
        raise
    except (
        CryptoTenSymbolFactorProjectionError,
        CryptoTenSymbolObservationStoreError,
        OSError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        raise FortySymbolRollingEvaluationError(
            "rolling_evaluation_store_read_invalid"
        ) from exc


def _contiguous_eligible_observation_suffix(
    units: Sequence[Mapping[str, Any]],
) -> tuple[list[Mapping[str, Any]], list[str]]:
    """Return the newest contiguous suffix; gaps and ineligible slots cut it."""

    ordered = sorted(units, key=lambda unit: unit["window_end"])
    selected_reversed: list[Mapping[str, Any]] = []
    expected: datetime | None = None
    for unit in reversed(ordered):
        event = unit["event"]
        window_end = unit["window_end"]
        if (
            event.get("event_type") != "observation"
            or unit.get("eligible") is not True
            or (expected is not None and window_end != expected)
        ):
            break
        selected_reversed.append(unit)
        expected = window_end - timedelta(seconds=FIVE_MINUTE_SECONDS)
    if not selected_reversed:
        raise FortySymbolRollingEvaluationError(
            "rolling_evaluation_success_events_empty"
        )
    selected = list(reversed(selected_reversed))
    dropped = [
        str(item["window_end"])
        for item in ordered[: len(ordered) - len(selected)]
    ]
    return selected, dropped


def _assemble_segment(units: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Union chained slot rows into one contiguous per-symbol bar panel."""

    union: dict[str, dict[str, tuple[dict[str, Any], datetime]]] = {
        symbol: {} for symbol in FORTY_SYMBOLS
    }
    for unit in units:
        observation = unit["observation"]
        event = unit["event"]
        if (
            observation is None
            or _parse_utc(event.get("observation_cutoff"))
            != observation.window.observation_cutoff
        ):
            raise FortySymbolRollingEvaluationError(
                "rolling_evaluation_sidecar_event_binding_invalid"
            )
        rows = unit["rows_by_symbol"]
        availability = {source.symbol: source.observed_at for source in observation.sources}
        if any(
            seen_at < observation.window.window_end
            or seen_at > observation.window.observation_cutoff
            for seen_at in availability.values()
        ):
            raise FortySymbolRollingEvaluationError(
                "rolling_evaluation_observed_at_invalid"
            )
        for symbol, source_rows in rows.items():
            for row in source_rows:
                open_time = str(row["open_time"])
                prior = union[symbol].get(open_time)
                candidate = (row, availability[symbol])
                if prior is not None and prior[0] != row:
                    raise FortySymbolRollingEvaluationError(
                        "rolling_evaluation_overlap_conflict"
                    )
                if prior is None or candidate[1] < prior[1]:
                    union[symbol][open_time] = candidate

    reference_times: list[str] | None = None
    for symbol in FORTY_SYMBOLS:
        times = sorted(union[symbol])
        if reference_times is None:
            reference_times = times
        elif times != reference_times:
            raise FortySymbolRollingEvaluationError(
                "rolling_evaluation_segment_symbols_disagree"
            )
    assert reference_times is not None
    if len(reference_times) < REGIME_LOOKBACK_BARS + 2:
        raise FortySymbolRollingEvaluationError("rolling_evaluation_segment_too_short")

    first = _parse_utc(reference_times[0])
    expected = first
    for time_text in reference_times:
        current = _parse_utc(time_text)
        if current != expected:
            raise FortySymbolRollingEvaluationError("rolling_evaluation_segment_gap")
        expected += timedelta(seconds=FIVE_MINUTE_SECONDS)

    bars_by_symbol: dict[str, list[dict[str, Any]]] = {}
    available_at_by_symbol: dict[str, list[datetime]] = {}
    for symbol in FORTY_SYMBOLS:
        bars_by_symbol[symbol] = [union[symbol][time][0] for time in reference_times]
        available_at_by_symbol[symbol] = [
            union[symbol][time][1] for time in reference_times
        ]
    return {
        "open_times": reference_times,
        "bars_by_symbol": bars_by_symbol,
        "available_at_by_symbol": available_at_by_symbol,
        "first_open_time": reference_times[0],
        "last_open_time": reference_times[-1],
        "bar_count_per_symbol": len(reference_times),
    }


def _first_executable_entry_index(
    open_times: Sequence[str],
    available_at: Sequence[datetime],
    signal_index: int,
) -> tuple[int, datetime] | None:
    """Use the first bar open strictly after all signal inputs were observed."""

    if (
        len(open_times) != len(available_at)
        or signal_index < REGIME_LOOKBACK_BARS
        or signal_index >= len(open_times)
    ):
        raise FortySymbolRollingEvaluationError(
            "rolling_evaluation_entry_timing_invalid"
        )
    decision_available_at = max(
        available_at[signal_index - REGIME_LOOKBACK_BARS : signal_index + 1]
    )
    for index in range(signal_index + 1, len(open_times)):
        if _parse_utc(open_times[index]) > decision_available_at:
            return index, decision_available_at
    return None


# ---------------------------------------------------------------------------
# Frozen champion evaluation (single pre-registered configuration)
# ---------------------------------------------------------------------------


def _is_entry_signal(closes: Sequence[Decimal], index: int) -> bool:
    if index < REGIME_LOOKBACK_BARS:
        return False
    base_decision = closes[index - DECISION_LOOKBACK_BARS]
    base_regime = closes[index - REGIME_LOOKBACK_BARS]
    if base_decision <= ZERO or base_regime <= ZERO:
        return False
    decision_return = closes[index] / base_decision - ONE
    regime_return = closes[index] / base_regime - ONE
    return (
        regime_return >= MINIMUM_REGIME_RETURN
        and decision_return >= ENTRY_THRESHOLD
    )


def _is_reversal_exit(closes: Sequence[Decimal], index: int) -> bool:
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
    highs: Sequence[Decimal],
    lows: Sequence[Decimal],
    closes: Sequence[Decimal],
    entry_price: Decimal,
    entry_index: int,
) -> dict[str, Any]:
    """Champion exit ladder; pessimistic intrabar (stop before target)."""

    tp_level = entry_price * (ONE + TAKE_PROFIT_RETURN)
    sl_level = entry_price * (ONE + STOP_LOSS_RETURN)
    last_index = min(entry_index + MAX_HOLD_BARS, len(closes) - 1)
    mfe = ZERO
    mae = ZERO
    # Entry is now at the bar OPEN, so its own high/low are post-entry
    # exposure. Skipping offset zero would erase an immediate stop/target.
    for offset in range(0, last_index - entry_index + 1):
        index = entry_index + offset
        mfe = max(mfe, highs[index] / entry_price - ONE)
        mae = min(mae, lows[index] / entry_price - ONE)
        if lows[index] <= sl_level:
            gross = sl_level / entry_price - ONE
            reason = "stop_loss"
        elif highs[index] >= tp_level:
            gross = tp_level / entry_price - ONE
            reason = "take_profit"
        elif offset >= MAX_HOLD_BARS:
            gross = closes[index] / entry_price - ONE
            reason = "max_holding_period"
        elif _is_reversal_exit(closes, index):
            gross = closes[index] / entry_price - ONE
            reason = "momentum_reversal_observed"
        else:
            continue
        return {
            "exit_reason": reason,
            "exit_offset_bars": offset,
            "exit_open_time": None,
            "gross": gross,
            "mfe": mfe,
            "mae": mae,
            "resolved": True,
        }
    return {
        "exit_reason": "data_end",
        "exit_offset_bars": last_index - entry_index,
        "exit_open_time": None,
        "gross": closes[last_index] / entry_price - ONE,
        "mfe": mfe,
        "mae": mae,
        "resolved": False,
    }


def _recommendation(resolved_trips: int, mean_net: Decimal | None) -> dict[str, Any]:
    if resolved_trips < MIN_RESOLVED_TRIPS_FOR_RECOMMENDATION or mean_net is None:
        return {
            "action": "continue_accumulation",
            "detail": (
                "resolved trips below the pre-declared minimum sample; keep "
                "accumulating rolling entries before any retain/downweight/"
                "disable judgement"
            ),
        }
    if mean_net > ZERO:
        return {
            "action": "retain_shadow",
            "detail": "positive mean net over resolved trips; stay shadow-only",
        }
    if mean_net > -Decimal("0.002"):
        return {"action": "downweight", "detail": "negative but marginal mean net"}
    return {"action": "disable_candidate", "detail": "materially negative mean net"}


def _evaluate_segment(segment: Mapping[str, Any]) -> dict[str, Any]:
    trips: list[dict[str, Any]] = []
    abstentions_data_end = 0
    abstentions_unavailable_entry = 0
    observe_slots = 0
    excluded_symbols: list[str] = []

    for symbol in FORTY_SYMBOLS:
        rows = segment["bars_by_symbol"][symbol]
        closes = [_decimal(row["close"]) for row in rows]
        highs = [_decimal(row["high"]) for row in rows]
        lows = [_decimal(row["low"]) for row in rows]
        opens = [_decimal(row["open"]) for row in rows]
        available_at = segment["available_at_by_symbol"][symbol]
        index = REGIME_LOOKBACK_BARS
        while index < len(closes):
            if not _is_entry_signal(closes, index):
                index += 1
                observe_slots += 1
                continue
            executable = _first_executable_entry_index(
                segment["open_times"], available_at, index
            )
            if executable is None:
                abstentions_unavailable_entry += 1
                break
            entry_index, decision_available_at = executable
            trip = _simulate_path(
                highs, lows, closes, opens[entry_index], entry_index
            )
            trip["symbol"] = symbol
            trip["signal_index"] = index
            trip["signal_open_time"] = segment["open_times"][index]
            trip["decision_available_at"] = _iso_slot(decision_available_at)
            trip["entry_index"] = entry_index
            trip["entry_open_time"] = segment["open_times"][entry_index]
            trip["entry_price"] = _text(opens[entry_index])
            trip["exit_open_time"] = segment["open_times"][
                min(entry_index + trip["exit_offset_bars"], len(closes) - 1)
            ]
            trip["net"] = _round_trip_net(trip["gross"]) if trip["resolved"] else None
            trips.append(trip)
            if not trip["resolved"]:
                abstentions_data_end += 1
                break  # data_end: no further bars to decide or exit on
            # Resolved trip: scanning resumes after the exit bar so a symbol
            # may re-enter later inside the same segment.
            index = entry_index + trip["exit_offset_bars"] + 1

    resolved = [trip for trip in trips if trip["resolved"]]
    resolved_nets = [trip["net"] for trip in resolved]  # type: ignore[misc]
    hits = [net for net in resolved_nets if net > ZERO]  # type: ignore[operator]
    mean_net = _mean(resolved_nets)
    mean_gross = _mean([trip["gross"] for trip in resolved])

    baselines: dict[str, dict[str, str]] = {}
    for symbol in FORTY_SYMBOLS:
        rows = segment["bars_by_symbol"][symbol]
        closes = [_decimal(row["close"]) for row in rows]
        gross = closes[-1] / closes[0] - ONE
        baselines[symbol] = {
            "gross": format(gross, "f"),
            "net": format(_round_trip_net(gross), "f"),
        }
    baseline_mean_net = _mean([_decimal(v["net"]) for v in baselines.values()])

    result: dict[str, Any] = {
        "trips_total": len(trips),
        "trips_resolved": len(resolved),
        "abstentions_data_end": abstentions_data_end,
        "abstentions_no_later_observed_bar": abstentions_unavailable_entry,
        "observe_slots_total": observe_slots,
        "excluded_symbols": excluded_symbols,
        "hits": len(hits),
        "hit_rate": (
            format(Decimal(len(hits)) / Decimal(len(resolved)), "f")
            if resolved
            else None
        ),
        "mean_gross_resolved": _text(mean_gross),
        "mean_net_resolved": _text(mean_net),
        "max_drawdown_resolved": _text(_max_drawdown(resolved_nets)),
        "turnover_trips_per_symbol": (
            format(Decimal(len(trips)) / Decimal(len(FORTY_SYMBOLS)), "f")
        ),
        "baseline_buy_hold": {
            "basis": "descriptive_observed_close_to_close_not_tradeable_pit",
            "tradeable_pit_verified": False,
            "mean_net": _text(baseline_mean_net),
            "per_symbol_net": baselines,
        },
        "exit_reason_counts": {
            reason: sum(1 for trip in trips if trip["exit_reason"] == reason)
            for reason in (
                "take_profit",
                "stop_loss",
                "max_holding_period",
                "momentum_reversal_observed",
                "data_end",
            )
        },
    }
    result["recommendation"] = _recommendation(len(resolved), mean_net)
    return result


# ---------------------------------------------------------------------------
# Artifact assembly and CLI
# ---------------------------------------------------------------------------


def build_artifact(
    *,
    store_root: Path,
    replay_command: str,
) -> dict[str, Any]:
    _assert_simulation_only()
    _, units, head = _verified_store_units(store_root)
    selected, dropped = _contiguous_eligible_observation_suffix(units)
    receipts: list[dict[str, str]] = []
    for unit in selected:
        event = unit["event"]
        receipts.append(
            {
                "event_id": str(event["event_id"]),
                "window_end": str(event["window_end"]),
                "checksum": str(event["checksum"]),
            }
        )
    slot_ends = [_parse_utc(receipt["window_end"]) for receipt in receipts]
    for earlier, later in zip(slot_ends, slot_ends[1:]):
        if later - earlier != timedelta(seconds=FIVE_MINUTE_SECONDS):
            raise FortySymbolRollingEvaluationError("rolling_evaluation_slot_gap")

    segment = _assemble_segment(selected)
    evaluation = _evaluate_segment(segment)

    return {
        "contract": CONTRACT,
        **_non_evidence_fields(),
        "generated_from": {
            "store_read_mode": "events_read_only_with_immutable_head_anchor",
            "head_sequence": head["sequence"],
            "head_last_checksum": head["last_checksum"],
            "head_sha256": head["head_sha256"],
            "head_segment_count": head["segment_count"],
            "full_chain_event_count": head["event_count"],
            "replay_command": replay_command,
        },
        "champion_configuration": {
            "entry_threshold": format(ENTRY_THRESHOLD, "f"),
            "minimum_regime_return": format(MINIMUM_REGIME_RETURN, "f"),
            "decision_lookback_bars": DECISION_LOOKBACK_BARS,
            "regime_lookback_bars": REGIME_LOOKBACK_BARS,
            "take_profit_return": format(TAKE_PROFIT_RETURN, "f"),
            "stop_loss_return": format(STOP_LOSS_RETURN, "f"),
            "max_hold_bars": MAX_HOLD_BARS,
            "entry_fee": format(ENTRY_FEE, "f"),
            "exit_fee": format(EXIT_FEE, "f"),
            "slippage_rate_per_leg": format(SLIPPAGE_RATE, "f"),
            "scan_performed": False,
        },
        "segment": {
            "first_open_time": segment["first_open_time"],
            "last_open_time": segment["last_open_time"],
            "bar_count_per_symbol": segment["bar_count_per_symbol"],
            "slot_count": len(receipts),
            "symbols": len(FORTY_SYMBOLS),
            "gap_free": True,
            "dropped_prefix_receipts": dropped,
        },
        "receipts": receipts,
        "evaluation": evaluation,
    }


def render_markdown(result: Mapping[str, Any]) -> str:
    evaluation = result["evaluation"]
    segment = result["segment"]
    recommendation = evaluation["recommendation"]
    lines = [
        "# Crypto 首个滚动评估入口（MVP-2 receipt-bound）",
        "",
        f"- 契约：`{result['contract']}`；shadow-only，authority=none。",
        "- 输入只接受完整只读 observation store：head 锚定前后字节一致、"
        "全链校验及逐槽边车重建 observation/market-data 双哈希全部通过。",
        f"- 段：{segment['first_open_time']} → {segment['last_open_time']}"
        f"（每标的 {segment['bar_count_per_symbol']} 根 5m，"
        f"{segment['slot_count']} 个连续回执槽，无缺口）。",
        f"- 冻结冠军单配置（阈值 {result['champion_configuration']['entry_threshold']}，"
        f"3/12 根回看），零扫描；费用 taker 双边 0.1% + 每腿 2bps 滑点。",
        "",
        "## 结果",
        "",
        "| 指标 | 值 |",
        "|---|---|",
        f"| 已了结回合 | {evaluation['trips_resolved']} |",
        f"| 未了结（data_end，计为弃权）| {evaluation['abstentions_data_end']} |",
        f"| 无后续可执行 bar（计为弃权）| {evaluation['abstentions_no_later_observed_bar']} |",
        f"| 命中数 / 命中率 | {evaluation['hits']} / {evaluation['hit_rate']} |",
        f"| 平均毛收益（已了结）| {evaluation['mean_gross_resolved']} |",
        f"| 平均净收益（已了结）| {evaluation['mean_net_resolved']} |",
        f"| 最大回撤（净）| {evaluation['max_drawdown_resolved']} |",
        f"| 描述性收盘到收盘基线平均净 | {evaluation['baseline_buy_hold']['mean_net']} |",
        "",
        "## 建议（机械规则，shadow-only）",
        "",
        f"`{recommendation['action']}` — {recommendation['detail']}",
        "",
        "source receipt integrity 已验证；tradeable PIT 仍为 false：信号须在"
        "全部输入 source 的 observed_at 后、下一根可用 bar 开盘才计算入场，"
        "且 bar-only 资料不能证明真实可成交报价或成交。负结果与弃权照实保留，"
        "不构成任何晋级证据，也不触发任何资本或风险行为。",
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--store-root",
        type=Path,
        required=True,
        help="full immutable forty-symbol observation store root",
    )
    parser.add_argument("--out-json", type=Path, help="write machine artifact JSON here")
    parser.add_argument("--report", type=Path, help="write Markdown report here")
    args = parser.parse_args(argv)

    # A read-only store cannot also be a report destination (including aliases).
    # Refuse before building or writing anything, even for explicit CLI paths.
    source_root = args.store_root.resolve()
    for output in (args.out_json, args.report):
        if output is not None and output.resolve().is_relative_to(source_root):
            raise FortySymbolRollingEvaluationError(
                "rolling_evaluation_output_inside_source_store"
            )

    replay_command = (
        f"python3 -m Crypto.forty_symbol_rolling_evaluation "
        f"--store-root {args.store_root}"
    )
    result = build_artifact(
        store_root=args.store_root,
        replay_command=replay_command,
    )
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
