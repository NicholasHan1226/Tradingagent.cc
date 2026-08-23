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
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Sequence

from Crypto.market_observation import (
    BAR_FIELDS,
    FORTY_SYMBOL_BARS_SIDECAR_CONTRACT,
    OBSERVATION_SYMBOLS_V40 as FORTY_SYMBOLS,
    _recomputed_identity_sha256,
    _recomputed_market_data_sha256,
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
        "receipt_bound_pit": True,
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


def _load_event_log(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise FortySymbolRollingEvaluationError(
                    "rolling_evaluation_event_line_invalid"
                ) from exc
            if not isinstance(event, dict):
                raise FortySymbolRollingEvaluationError(
                    "rolling_evaluation_event_line_invalid"
                )
            events.append(event)
    if not events:
        raise FortySymbolRollingEvaluationError("rolling_evaluation_event_log_empty")
    return events


def _verify_event_chain(events: Sequence[Mapping[str, Any]]) -> None:
    previous: Mapping[str, Any] | None = None
    for event in events:
        if event.get("contract") != EVENT_CONTRACT:
            raise FortySymbolRollingEvaluationError(
                "rolling_evaluation_event_contract_invalid"
            )
        if previous is not None:
            if event.get("previous_checksum") != previous.get("checksum"):
                raise FortySymbolRollingEvaluationError(
                    "rolling_evaluation_event_chain_broken"
                )
            try:
                if int(event["sequence"]) != int(previous["sequence"]) + 1:
                    raise FortySymbolRollingEvaluationError(
                        "rolling_evaluation_event_sequence_broken"
                    )
            except (KeyError, TypeError, ValueError) as exc:
                raise FortySymbolRollingEvaluationError(
                    "rolling_evaluation_event_sequence_broken"
                ) from exc
        previous = event


def _success_events(events: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    selected = [
        event
        for event in events
        if str(event.get("event_id") or "").startswith("crypto-forty-observation-")
        and str(event.get("event_type") or "") == "observation"
    ]
    if not selected:
        raise FortySymbolRollingEvaluationError(
            "rolling_evaluation_success_events_empty"
        )
    return selected


def _sidecar_path(bars_dir: Path, window_end: datetime) -> Path:
    name = _iso_slot(window_end).replace(":", "-") + ".json"
    return bars_dir / name


def _load_and_verify_sidecar(
    bars_dir: Path,
    event: Mapping[str, Any],
) -> dict[tuple[str, str], dict[str, Any]]:
    observation = event.get("observation")
    if not isinstance(observation, Mapping):
        raise FortySymbolRollingEvaluationError(
            "rolling_evaluation_event_observation_missing"
        )
    window_end = _parse_utc(event.get("window_end"))
    path = _sidecar_path(bars_dir, window_end)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FortySymbolRollingEvaluationError(
            "rolling_evaluation_sidecar_unreadable"
        ) from exc

    if payload.get("contract") != FORTY_SYMBOL_BARS_SIDECAR_CONTRACT:
        raise FortySymbolRollingEvaluationError(
            "rolling_evaluation_sidecar_contract_invalid"
        )
    if payload.get("window_end") != event.get("window_end"):
        raise FortySymbolRollingEvaluationError(
            "rolling_evaluation_sidecar_window_mismatch"
        )
    if payload.get("observation_sha256") != observation.get("observation_sha256"):
        raise FortySymbolRollingEvaluationError(
            "rolling_evaluation_sidecar_event_binding_invalid"
        )
    if (
        payload.get("authority") != "none"
        or payload.get("execution_eligible") is not False
        or payload.get("capital_write_eligible") is not False
        or payload.get("model_authority") is not False
    ):
        raise FortySymbolRollingEvaluationError("rolling_evaluation_authority_invalid")

    sources = payload.get("sources")
    if not isinstance(sources, list) or tuple(
        source.get("symbol") for source in sources
    ) != FORTY_SYMBOLS:
        raise FortySymbolRollingEvaluationError(
            "rolling_evaluation_sidecar_symbols_drift"
        )

    rows_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for source in sources:
        rows = source.get("rows")
        symbol = source.get("symbol")
        if (
            source.get("row_count") != 13
            or source.get("page_count") != 1
            or not isinstance(rows, list)
            or len(rows) != 13
            or any(
                not isinstance(row, dict)
                or set(row) != set(BAR_FIELDS)
                or row.get("symbol") != symbol
                for row in rows
            )
            or _recomputed_identity_sha256(rows) != source.get("identity_sha256")
            or _recomputed_market_data_sha256(rows)
            != source.get("market_data_sha256")
        ):
            raise FortySymbolRollingEvaluationError(
                "rolling_evaluation_row_digest_invalid"
            )
        for row in rows:
            key = (str(symbol), str(row["open_time"]))
            prior = rows_by_key.get(key)
            if prior is not None and prior != row:
                raise FortySymbolRollingEvaluationError(
                    "rolling_evaluation_overlap_conflict"
                )
            rows_by_key[key] = row
    return rows_by_key


def _assemble_segment(
    slots: Sequence[dict[tuple[str, str], dict[str, Any]]],
) -> dict[str, Any]:
    """Union chained slot rows into one contiguous per-symbol bar panel."""

    union: dict[str, dict[str, dict[str, Any]]] = {symbol: {} for symbol in FORTY_SYMBOLS}
    for rows in slots:
        for (symbol, open_time), row in rows.items():
            prior = union[symbol].get(open_time)
            if prior is not None and prior != row:
                raise FortySymbolRollingEvaluationError(
                    "rolling_evaluation_overlap_conflict"
                )
            union[symbol][open_time] = row

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
    for symbol in FORTY_SYMBOLS:
        bars_by_symbol[symbol] = [union[symbol][t] for t in reference_times]
    return {
        "open_times": reference_times,
        "bars_by_symbol": bars_by_symbol,
        "first_open_time": reference_times[0],
        "last_open_time": reference_times[-1],
        "bar_count_per_symbol": len(reference_times),
    }


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
    entry_index: int,
) -> dict[str, Any]:
    """Champion exit ladder; pessimistic intrabar (stop before target)."""

    entry_price = closes[entry_index]
    tp_level = entry_price * (ONE + TAKE_PROFIT_RETURN)
    sl_level = entry_price * (ONE + STOP_LOSS_RETURN)
    last_index = min(entry_index + MAX_HOLD_BARS, len(closes) - 1)
    mfe = ZERO
    mae = ZERO
    for offset in range(1, last_index - entry_index + 1):
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
    abstentions = 0
    observe_slots = 0
    excluded_symbols: list[str] = []

    for symbol in FORTY_SYMBOLS:
        rows = segment["bars_by_symbol"][symbol]
        closes = [_decimal(row["close"]) for row in rows]
        highs = [_decimal(row["high"]) for row in rows]
        lows = [_decimal(row["low"]) for row in rows]
        index = REGIME_LOOKBACK_BARS
        while index < len(closes):
            if not _is_entry_signal(closes, index):
                index += 1
                observe_slots += 1
                continue
            trip = _simulate_path(highs, lows, closes, index)
            trip["symbol"] = symbol
            trip["entry_index"] = index
            trip["entry_open_time"] = segment["open_times"][index]
            trip["exit_open_time"] = segment["open_times"][
                min(index + trip["exit_offset_bars"], len(closes) - 1)
            ]
            trip["net"] = _round_trip_net(trip["gross"]) if trip["resolved"] else None
            trips.append(trip)
            if not trip["resolved"]:
                abstentions += 1
                break  # data_end: no further bars to decide or exit on
            # Resolved trip: scanning resumes after the exit bar so a symbol
            # may re-enter later inside the same segment.
            index = index + trip["exit_offset_bars"] + 1

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
        "abstentions_data_end": abstentions,
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
    events_path: Path,
    bars_dir: Path,
    replay_command: str,
) -> dict[str, Any]:
    _assert_simulation_only()
    events = _load_event_log(events_path)
    _verify_event_chain(events)
    selected = _success_events(events)

    slots: list[dict[tuple[str, str], dict[str, Any]]] = []
    receipts: list[dict[str, str]] = []
    # Deterministic selection: the longest contiguous 5-minute suffix of
    # success slots.  Earlier isolated receipts (e.g. the pre-cutover identity
    # era) are dropped from the segment but kept in the artifact as evidence.
    selected_sorted = sorted(selected, key=lambda event: str(event["window_end"]))
    ends = [_parse_utc(event["window_end"]) for event in selected_sorted]
    keep_from = len(ends) - 1
    while keep_from > 0:
        if ends[keep_from] - ends[keep_from - 1] != timedelta(
            seconds=FIVE_MINUTE_SECONDS
        ):
            break
        keep_from -= 1
    dropped = [str(event["window_end"]) for event in selected_sorted[:keep_from]]
    selected_sorted = selected_sorted[keep_from:]

    for event in selected_sorted:
        slots.append(_load_and_verify_sidecar(bars_dir, event))
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

    segment = _assemble_segment(slots)
    evaluation = _evaluate_segment(segment)

    return {
        "contract": CONTRACT,
        **_non_evidence_fields(),
        "generated_from": {
            "events_file_sha256": _sha256_file(events_path),
            "events_file_name": events_path.name,
            "bars_dir_file_count": len(list(bars_dir.glob("*.json"))),
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
        "- 输入为四十币观察器自身回执：append-only 成功事件（校验链完整）+"
        " 逐槽不可变 K 线边车，逐行复算 identity/market-data 双哈希全部通过。",
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
        f"| 命中数 / 命中率 | {evaluation['hits']} / {evaluation['hit_rate']} |",
        f"| 平均毛收益（已了结）| {evaluation['mean_gross_resolved']} |",
        f"| 平均净收益（已了结）| {evaluation['mean_net_resolved']} |",
        f"| 最大回撤（净）| {evaluation['max_drawdown_resolved']} |",
        f"| 买入持有基线平均净 | {evaluation['baseline_buy_hold']['mean_net']} |",
        "",
        "## 建议（机械规则，shadow-only）",
        "",
        f"`{recommendation['action']}` — {recommendation['detail']}",
        "",
        "本条目按序进入 MVP-2 滚动评估累积；负结果与弃权照实保留，"
        "不构成任何晋级证据，也不触发任何资本或风险行为。",
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=Path, required=True, help="full events.jsonl copy")
    parser.add_argument("--bars-dir", type=Path, required=True, help="bars sidecar directory")
    parser.add_argument("--out-json", type=Path, help="write machine artifact JSON here")
    parser.add_argument("--report", type=Path, help="write Markdown report here")
    args = parser.parse_args(argv)

    replay_command = (
        f"python3 -m Crypto.forty_symbol_rolling_evaluation "
        f"--events {args.events} --bars-dir {args.bars_dir}"
    )
    result = build_artifact(
        events_path=args.events,
        bars_dir=args.bars_dir,
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
