"""Read-only chronological diagnostic; never independent promotion evidence.

Reuses the registered prescreen definitions on hourly decision clusters. The
70/30 split is a diagnostic of already inspected history, not a fresh holdout.
No store artifact, checkpoint, capital, registry, or scheduler is written.
"""

from __future__ import annotations

import argparse
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
from typing import Any, Mapping

import Crypto.ten_symbol_factor_prescreen as prescreen
import Crypto.ten_symbol_factor_research as projection
import Crypto.ten_symbol_research_loop as loop


CONTRACT = "tradingagent.crypto.ten_symbol_time_split_diagnostic.v1"
HORIZON_BARS = 12
BAR = prescreen.FIVE_MINUTES


def partition(
    universe: Mapping[str, Mapping[datetime, Any]],
    rows: Mapping[str, list[dict[str, Any]]],
    *,
    split: datetime,
    as_of: datetime,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Purge label crossing, embargo the full feature window, reject gaps.

    Keys are bar OPEN times: both the decision close and label close occur
    one bar later. Every future bar must exist; endpoint presence is not enough.
    """
    train: dict[str, Any] = {}
    test: dict[str, Any] = {}
    for symbol, samples in universe.items():
        available = {row["open_time"] for row in rows[symbol]}
        train[symbol], test[symbol] = {}, {}
        for slot, sample in samples.items():
            if slot + (HORIZON_BARS + 1) * BAR > as_of:
                continue
            if any(slot + step * BAR not in available for step in range(13)):
                continue
            if slot + (HORIZON_BARS + 1) * BAR <= split:
                train[symbol][slot] = sample
            elif slot - HORIZON_BARS * BAR >= split:
                test[symbol][slot] = sample
    return train, test


def _hour_grid(universe: Mapping[str, Mapping[datetime, Any]]) -> dict[str, Any]:
    # Absolute UTC clock grid: a missing slot must not shift future sampling.
    return {
        symbol: {slot: row for slot, row in samples.items() if slot.minute == 0}
        for symbol, samples in universe.items()
    }


def _variants(universe: Mapping[str, Any], threshold: Decimal | None) -> dict[str, Any]:
    symbols = tuple(universe)
    candidates = [
        evaluator(universe, symbols=symbols, horizon_bars=HORIZON_BARS)
        for evaluator in (
            prescreen._evaluate_xs_rs,
            prescreen._evaluate_short_reversal,
            prescreen._evaluate_amihud,
        )
    ]
    # The old descriptive prescreen estimates median vol over its entire
    # input. Here the threshold is fitted once using TRAIN only, then frozen.
    rows = [(slot, row) for samples in universe.values() for slot, row in samples.items()]
    baseline = sum((row["forward_return"] for _, row in rows), Decimal(0)) / len(rows) if rows else None
    vol_variants = {}
    for name, high in (("high_vol_half", True), ("low_vol_half", False)):
        half = [
            (slot, row) for slot, row in rows
            if threshold is not None
            and (row["features"]["realized_volatility_1h"] >= threshold) == high
        ]
        selected = [
            (slot, row["forward_return"], row["forward_gross"])
            for slot, row in half
            if prescreen._signal(
                "time_series_momentum_v1", prescreen._signal_snapshot(row["features"])
            )
        ]
        vol_variants[name] = prescreen._sample_metrics(
            selected, universe_count=len(half), baseline_mean=baseline,
            universe_slots=sorted({slot for slot, _ in rows}),
            horizon_bars=HORIZON_BARS,
        )
    candidates.append({"candidate_id": "momentum_vol_regime", "variants": vol_variants})
    # The input was already clock-sampled; ignore the evaluators' second
    # stride-based subset. Main metrics now describe non-overlapping horizons.
    fields = ("signal_count", "mean_gross", "mean_net", "hit_rate", "max_drawdown")
    return {
        candidate["candidate_id"]: {
            variant: {key: metrics[key] for key in fields}
            for variant, metrics in candidate["variants"].items()
        }
        for candidate in candidates
    }


def analyze(rows: Mapping[str, list[dict[str, Any]]], *, split: datetime, as_of: datetime) -> dict[str, Any]:
    prescreen._assert_simulation_only()
    if split >= as_of or not rows:
        raise ValueError("time_split_invalid")
    universe = prescreen._symbol_evaluation_rows(rows, horizon_bars=HORIZON_BARS)
    train, test = partition(universe, rows, split=split, as_of=as_of)
    threshold = prescreen._median(sorted(
        row["features"]["realized_volatility_1h"]
        for samples in train.values() for row in samples.values()
    ))
    train_grid, test_grid = _hour_grid(train), _hour_grid(test)
    train_metrics, test_metrics = _variants(train_grid, threshold), _variants(test_grid, threshold)
    comparisons = {}
    for candidate, variants in train_metrics.items():
        eligible = [name for name, values in variants.items() if values["mean_net"] is not None]
        selected = min(eligible, key=lambda name: (-Decimal(variants[name]["mean_net"]), name)) if eligible else None
        train_cell = variants[selected] if selected else None
        test_cell = test_metrics[candidate][selected] if selected else None
        if train_cell is None or test_cell["mean_net"] is None:
            diagnostic = "insufficient_samples"
        elif Decimal(train_cell["mean_net"]) <= 0:
            diagnostic = "train_reject"
        elif Decimal(test_cell["mean_net"]) <= 0:
            diagnostic = "held_out_period_negative"
        else:
            diagnostic = "positive_diagnostic_requires_fresh_forward_evidence"
        comparisons[candidate] = {
            "train_selected_variant": selected,
            "train": train_cell, "test": test_cell, "diagnostic": diagnostic,
        }
    clusters = lambda grid: len({slot for samples in grid.values() for slot in samples})
    return {
        "contract": CONTRACT,
        **projection._non_authority_fields(),
        "not_promotion_evidence": True,
        "clean_holdout": False,
        "reason": "history_already_inspected; chronological_diagnostic_only",
        "split": prescreen._iso(split), "as_of": prescreen._iso(as_of),
        "horizon_minutes": 60, "selection": "train_mean_net_only; name_tie_break",
        "volatility_threshold_train_only": prescreen._text(threshold),
        "grid": "bar_open_at_UTC_hour; decision_at_bar_close; no_grid_shift_on_gaps",
        "metric_basis": "prescreen_close_counterfactual; hourly_nonoverlap; cross_symbol_dependence_not_removed",
        "independence_warning": "signal_count_is_not_independent_sample_count; no_significance_claim",
        "cost_policy": {"fee_rate_each_side": str(prescreen.TAKER_FEE_RATE), "slippage_bps_each_side": str(prescreen.SLIPPAGE_BPS), "source": "assumed"},
        "train_time_clusters": clusters(train_grid), "test_time_clusters": clusters(test_grid),
        "candidate_family_count": len(comparisons),
        "variant_count": sum(len(values) for values in train_metrics.values()),
        "comparisons": comparisons,
    }


def run(store_root: Path, *, as_of: datetime) -> dict[str, Any]:
    prescreen._assert_simulation_only()
    store = loop._open_store(store_root)
    if store.pending_record_read_only() is not None:
        raise ValueError("time_split_core_pending")
    events = store.events_read_only()
    units = projection._build_units(store)
    eligible = [unit for unit in units if unit["eligible"] and unit["slot"] <= as_of]
    if not eligible:
        raise ValueError("time_split_no_eligible_data")
    rows, _ = loop._merge_eligible_bars(eligible)
    rows = {symbol: [row for row in bars if row["open_time"] + BAR <= as_of] for symbol, bars in rows.items()}
    if not all(rows.values()):
        raise ValueError("time_split_empty_symbol")
    meta = {
        symbol: {
            "row_count": len(bars),
            "first_open_time": prescreen._iso(bars[0]["open_time"]),
            "last_open_time": prescreen._iso(bars[-1]["open_time"]),
            "gap_count": sum(
                later["open_time"] - earlier["open_time"] != BAR
                for earlier, later in zip(bars, bars[1:])
            ),
        }
        for symbol, bars in rows.items()
    }
    first = max(bars[0]["open_time"] for bars in rows.values())
    last = min(bars[-1]["open_time"] for bars in rows.values())
    split = first + ((last - first) // BAR * 7 // 10) * BAR
    result = analyze(rows, split=split, as_of=as_of)
    after = store.events_read_only()
    if not events or after[-1]["checksum"] != events[-1]["checksum"]:
        raise ValueError("time_split_store_changed_retry")
    result["source"] = {
        "store_head_checksum": events[-1]["checksum"],
        "selected_units_sha256": loop._sha256(loop._terminal_unit_material(eligible)),
        "data_window": meta, "eligible_units": len(eligible),
        "split_rule": "70_percent_elapsed_common_history",
    }
    result["report_sha256"] = loop._sha256(result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store-root", type=Path, required=True)
    parser.add_argument("--as-of", required=True)
    args = parser.parse_args()
    result = run(args.store_root, as_of=prescreen._parse_utc(args.as_of))
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
