"""Fixed cost/confirmation/component ablations, historical diagnostics only.

No optimizer, capital writer, network client, scheduler or forward-window read.
The pooled ridge is an uncalibrated return estimate, never an execution signal.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
from typing import Mapping

import Crypto.slow_trend_risk_replay as risk

original = risk.original
history = original.history
ZERO, ONE = Decimal(0), Decimal(1)
CONTRACT = "tradingagent.crypto.cost_aware_trend_diagnostic.v1"
GRID_ORIGIN = datetime(2026, 2, 1, tzinfo=timezone.utc)
# cost, confirmation, hard trend admission, volatility signal sizing
VARIANTS = {
    "baseline_risk": (False, False, True, True),
    "cost_only": (True, False, True, True),
    "confirmation_only": (False, True, True, True),
    "combined": (True, True, True, True),
    "combined_no_trend": (True, False, False, True),
    "combined_no_vol": (True, True, True, False),
}


def frozen_plan() -> dict:
    return {
        "contract": CONTRACT, "variants": {k: list(v) for k, v in VARIANTS.items()},
        "historical_cutoff": history._iso(risk.HISTORY_END),
        "signal_plan_sha256": history._sha256(original.frozen_plan()),
        "risk_plan_sha256": history._sha256(risk.frozen_plan()),
        "training_days": 60, "label_days": 5, "grid_origin": history._iso(GRID_ORIGIN),
        "minimum_asset_windows": 30, "minimum_time_clusters": 5, "ridge": "0.01",
        "cost_buffer": "2", "cost_multipliers": ["1", "2"],
        "confirmation_days": 2, "trial_cells": 12, "context_cells": 4,
        "parameter_search": False, "new_forward_window": None,
        "forecast_kind": "uncalibrated_pooled_ridge_point_estimate",
        "no_trend_ablation": "hard_admission_and_confirmation_only; model_strength_feature_retained",
        "no_vol_ablation": "signal_sizing_only; account_risk_and_symbol_cap_retained",
    }


def features(days: Mapping, day: datetime) -> dict:
    result = {}
    plan = original.frozen_plan()
    for symbol, series in sorted(days.items()):
        dates = [day - i * original.DAY for i in range(60, 0, -1)]
        if not all(d in series for d in dates):
            continue
        closes = [series[d]["close"] for d in dates]
        strength = (sum(closes[-20:]) / 20) / (sum(closes) / 60) - ONE
        returns = [b / a - ONE for a, b in zip(closes[-21:-1], closes[-20:])]
        mean = sum(returns) / 20
        vol = (sum((r - mean) ** 2 for r in returns) / 20 * 365).sqrt()
        scale = min(ONE, Decimal(plan["annual_vol_ceiling"]) / vol) if vol else ONE
        result[symbol] = {"strength": strength, "trend": strength > ZERO,
                          "vol_weight": Decimal(plan["max_symbol_weight"]) * scale}
    return result


def fit_model(days: Mapping, feature_map: Mapping, day: datetime) -> dict:
    samples = []
    for entry in sorted(feature_map):
        if not day - 60 * original.DAY <= entry < day:
            continue
        if (entry - GRID_ORIGIN).days % 5:
            continue
        end = entry + 5 * original.DAY
        # An open at 00:05 of the decision day is not known at 00:00.
        if end + original.BAR >= day:
            continue
        for symbol, f in sorted(feature_map[entry].items()):
            series = days[symbol]
            if not all(entry + i * original.DAY in series for i in range(6)):
                continue
            label = series[end]["execution_open"] / series[entry]["execution_open"] - ONE
            samples.append({"entry": history._iso(entry), "symbol": symbol,
                            "exit_open_at": history._iso(end + original.BAR),
                            "x": str(f["strength"]), "y": str(label)})
    clusters = len({s["entry"] for s in samples})
    result = {"asset_windows": len(samples), "time_clusters": clusters,
              "samples_sha256": history._sha256(samples), "decision_at": history._iso(day),
              "latest_label_open": max((s["exit_open_at"] for s in samples), default=None),
              "status": "insufficient_training", "intercept": None, "slope": None}
    if len(samples) < 30 or clusters < 5:
        return result
    xs, ys = [Decimal(s["x"]) for s in samples], [Decimal(s["y"]) for s in samples]
    xm, ym = sum(xs) / len(xs), sum(ys) / len(ys)
    slope = sum((x - xm) * (y - ym) for x, y in zip(xs, ys)) / (
        sum((x - xm) ** 2 for x in xs) + Decimal("0.01"))
    result.update(status="fitted_uncalibrated", slope=str(slope), intercept=str(ym - slope * xm))
    return result


def cost_threshold(multiplier: Decimal) -> Decimal:
    if multiplier not in (ONE, Decimal(2)):
        raise ValueError("cost_diagnostic_multiplier_invalid")
    plan = original.frozen_plan()
    fee, slip = Decimal(plan["fee_each_side"]) * multiplier, Decimal(plan["slippage_each_side"]) * multiplier
    return 2 * ((ONE + fee) * (ONE + slip) / ((ONE - fee) * (ONE - slip)) - ONE)


def target_path(feature_map: Mapping, dates: list[datetime], *, variant: str) -> dict:
    _, confirm, trend_gate, vol_sizing = VARIANTS[variant]
    symbols = original.frozen_plan()["symbols"]
    admitted = {s: False for s in symbols}
    result = {}
    for day in dates:
        weights = {}
        for symbol in symbols:
            f = feature_map.get(day, {}).get(symbol)
            previous = feature_map.get(day - original.DAY, {}).get(symbol)
            if f is None:
                admitted[symbol] = False
                weights[symbol] = ZERO
                continue
            if not trend_gate:
                admitted[symbol] = True
            elif not confirm:
                admitted[symbol] = f["trend"]
            elif previous is None:
                admitted[symbol] = False
            elif f["trend"] and previous["trend"]:
                admitted[symbol] = True
            elif not f["trend"] and not previous["trend"]:
                admitted[symbol] = False
            weights[symbol] = (f["vol_weight"] if vol_sizing else Decimal("0.10")) if admitted[symbol] else ZERO
        result[day] = weights
    return result


def summarize(arm: dict) -> dict:
    keys = ("return", "fees", "slippage_cost", "turnover_over_initial_cash", "trade_leg_count",
            "max_drawdown_daily_close", "max_drawdown_sampled", "final_equity", "final_pause_reasons")
    result = {k: arm[k] for k in keys}
    result.update(
        buy_legs=sum(l["side"] == "buy" for l in arm["ledger"]),
        completed_episodes=len(arm["completed_position_episodes"]),
        filtered_buy_legs=len(arm.get("filtered_buys", [])),
        exposure_days=sum(Decimal(e["gross_exposure"]) > ZERO for e in arm["sampled_equity"]
                          if e["phase"] == "day_close_before_terminal"),
        first_pause=next((e["at"] for e in arm["risk_events"] if e["reason"] != "drawdown_tighten"), None),
    )
    return result


def analyze(rows: Mapping, *, as_of: datetime, source: Mapping | None = None) -> dict:
    history._assert_simulation_only()
    # Reuse original validation/window selection and preserve its exact baseline.
    baseline = risk.analyze(rows, as_of=as_of)
    cutoff = min(as_of, risk.HISTORY_END)
    days = {s: original._daily(bars, as_of=cutoff)[0] for s, bars in rows.items()}
    plan = frozen_plan()
    result = {"contract": CONTRACT, **history._non_evidence_fields(), "research_only": True,
              "clean_holdout": False, "as_of": history._iso(as_of), "plan": plan,
              "plan_sha256": history._sha256(plan), "input_rows_sha256": history._sha256({
                  s: [{k: history._iso(v) if isinstance(v, datetime) else str(v) if isinstance(v, Decimal) else v
                       for k, v in row.items()} for row in bars] for s, bars in rows.items()}),
              "source": dict(source or {}), "status": "insufficient_complete_history",
              "forward": {"status": "not_started_original_window_untouched", "returns": None},
              "limitations": ["already_viewed_historical_backfill_not_PIT", "daily_sampled_not_5m_risk",
                               "pooled_assets_not_independent", "uncalibrated_forecast_not_proven_edge",
                               "BTC_context_not_strict_risk_match", "all_trials_retained_no_promotion"]}
    if baseline["historical"]["status"] != "historical_diagnostic_not_holdout":
        result["report_sha256"] = history._sha256(result)
        return result
    hist = baseline["historical"]
    start, end = history._parse_utc(hist["start"]), history._parse_utc(hist["end"])
    dates = [start + i * original.DAY for i in range((end - start).days)]
    all_dates = sorted(set.union(*(set(s) for s in days.values())))
    fs = {d: features(days, d) for d in all_dates}
    models = {d: fit_model(days, fs, d) for d in dates}
    predictions = {
        d: {s: (Decimal(models[d]["intercept"]) + Decimal(models[d]["slope"]) * f["strength"])
            if models[d]["status"] == "fitted_uncalibrated" else None for s, f in fs[d].items()}
        for d in dates
    }
    result.update(status="historical_diagnostic_only", window={"start": hist["start"], "end": hist["end"], "days": len(dates)},
                  training=[{**models[d], "predicted_five_day_gross": {s: None if p is None else str(p)
                             for s, p in predictions[d].items()}} for d in dates], scenarios={})
    for multiplier in (ONE, Decimal(2)):
        arms, summaries = {}, {}
        for variant, config in VARIANTS.items():
            targets = target_path(fs, dates, variant=variant)
            def allowed(day, symbol):
                p = predictions[day].get(symbol)
                return p is not None and p > cost_threshold(multiplier)
            arm = risk._simulate(days, dates, mode="risk_trend", target_provider=targets.__getitem__,
                                 buy_allowed=allowed if config[0] else None, cost_multiplier=multiplier)
            arms[variant] = arm
            summaries[variant] = summarize(arm)
        if multiplier == ONE:
            # Callback-free old simulator must be bit-identical for all legacy fields.
            old = hist["risk_trend"]
            assert all(arms["baseline_risk"][k] == v for k, v in old.items()), "baseline_parity_failed"
        base_return = Decimal(summaries["baseline_risk"]["return"])
        for summary in summaries.values():
            summary["return_delta_vs_baseline"] = str(Decimal(summary["return"]) - base_return)
        btc = risk._simulate(days, dates, mode="btc_cash", cost_multiplier=multiplier)
        result["scenarios"][str(multiplier)] = {
            "gate_threshold_five_day_gross": str(cost_threshold(multiplier)), "arms": arms,
            "summary": summaries, "cash_return": "0", "btc_original_signal_budget_cash": btc,
            "paired_removal_return_deltas": {
                name: str(Decimal(summaries[name]["return"]) - Decimal(summaries["combined"]["return"]))
                for name in ("cost_only", "confirmation_only", "combined_no_trend", "combined_no_vol")},
        }
    result["recommendation"] = "no_promotion_historical_diagnostic_only"
    result["report_sha256"] = history._sha256(result)
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="Previously collected normalized TD input JSON")
    parser.add_argument("--reference-report", type=Path, required=True, help="Original verified risk replay report")
    parser.add_argument("--output", type=Path, required=True, help="New research file; existing path is never overwritten")
    args = parser.parse_args(argv)
    try:
        history._assert_simulation_only()
        payload = json.loads(args.input.read_text())
        reference = json.loads(args.reference_report.read_text())
        rows = payload["rows"]
        for bars in rows.values():
            for bar in bars:
                bar["open_time"] = history._parse_utc(bar["open_time"])
        at = history._parse_utc(reference["as_of"])
        replay = risk.analyze(rows, as_of=at)
        replay.pop("report_sha256")
        replay["source"] = payload["source"]
        replay["report_sha256"] = history._sha256(replay)
        if replay != reference:
            raise ValueError("reference_replay_mismatch")
        source = {"provenance": payload["source"], "reference_report_sha256": replay["report_sha256"],
                  "binding": "normalized_input_hash_and_reference_replay; not_original_wire_row_hash_reconstruction"}
        result = analyze(rows, as_of=at, source=source)
        with args.output.open("x") as stream:
            json.dump(result, stream, sort_keys=True, ensure_ascii=False, default=str)
            stream.write("\n")
        print(json.dumps({"status": result["status"], "report_sha256": result["report_sha256"],
                          "plan_sha256": result["plan_sha256"], "output": str(args.output)}))
        return 0
    except (ValueError, KeyError, TypeError, OSError, AssertionError, history.CryptoTenSymbolFactorPrescreenError):
        print("cost-aware diagnostic failed closed; no production action")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
