"""Independent fixed-window confirmation research; no runtime or order authority.

The CLI clock gate avoids accidental interim readout, not a hostile-user
security boundary. Local hashes are not an external timestamp or PIT proof.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from decimal import Context, Decimal, localcontext
import hashlib
import json
from pathlib import Path
from typing import Mapping

import Crypto.cost_aware_trend_research as candidate

original, risk, history = candidate.original, candidate.risk, candidate.history
CONTRACT = "tradingagent.crypto.confirmation_forward_research.v1"
PARENT_COMMIT = "6a798cad199b0889ee524634b10fba7759fd1494"
START = datetime(2026, 9, 1, tzinfo=timezone.utc)
END = datetime(2026, 11, 30, tzinfo=timezone.utc)
WARMUP = START - 61 * original.DAY
CALCULATION_FILES = (
    "Crypto/confirmation_forward_research.py",
    "Crypto/cost_aware_trend_research.py",
    "Crypto/slow_trend_risk_replay.py",
    "Crypto/slow_trend_research.py",
    "Crypto/research_accounting.py",
    "Crypto/ten_symbol_factor_prescreen.py",
    "Crypto/capital_policy.py",
    "Crypto/market_observation.py",
    "shared/capital/market_policy.py",
)


def frozen_plan() -> dict:
    signal = original.frozen_plan()
    return {
        "candidate_id": "slow_trend_confirmation2_forward_v1",
        "parent_candidate_commit": PARENT_COMMIT,
        "variant": "confirmation_only", "confirmation_days": 2,
        "start": history._iso(START), "end_exclusive": history._iso(END),
        "days": 90, "warmup_start": history._iso(WARMUP), "warmup_days": 61,
        "signal": {k: signal[k] for k in (
            "symbols", "fast_days", "slow_days", "vol_days", "annual_vol_ceiling",
            "max_symbol_weight", "rebalance_relative_band", "starting_research_cash",
            "fee_each_side", "slippage_each_side", "fee_source")},
        "signal_definition_sha256": history._sha256(signal),
        "risk_definition_sha256": history._sha256(risk.frozen_plan()),
        "cost_multipliers": ["1", "2"],
        "controls": ["baseline_risk", "cash", "btc_original_signal_budget_cash"],
        "comparison": "positive_net_and_excess_vs_baseline_risk_at_both_costs; descriptive_only",
        "btc_context": "not_risk_matched; not_the_acceptance_threshold",
        "initial_state": "cash_only; unadmitted; no_warmup_trades; fresh_research_risk_latch",
        "risk": "daily_sampled; sticky_pauses_no_resume; not_a_7pct_loss_guarantee",
        "prices": "actual_0005_open_and_2355_bar_close; no_intraday_interpolation",
        "terminal": "last_day_close_liquidation_with_costs",
        "coverage": "all_10_symbols_all_61_warmup_and_90_window_days; no_skip_or_extension",
        "readout": "no_input_read_or_returns_before_end; no_parameter_search_or_deadline_extension",
        "candidate_count": 1, "historical_selection_trial_cells": 12,
        "parameter_search": False, "decimal_context": "Context(prec=28), ROUND_HALF_EVEN",
        "source_semantics": "normalized_formal_TD_inputs; no_first_seen_or_PIT_proof",
        "data_revisions": "retain_all_input_hashes_and_readouts; no_best_revision_selection",
        "authority": "none", "promotion_authorized": False,
    }


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() != timedelta(0):
        raise ValueError("confirmation_forward_UTC_required")
    return value


def calculation_hashes() -> dict:
    root = Path(__file__).resolve().parents[1]
    return {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
            for name in CALCULATION_FILES}


def _seal(payload: dict, key: str) -> dict:
    return {**payload, key: history._sha256(payload)}


def register(*, registered_at: datetime) -> dict:
    history._assert_simulation_only()
    registered_at = _utc(registered_at)
    if registered_at >= START:
        raise ValueError("confirmation_forward_registration_too_late")
    plan = frozen_plan()
    return _seal({
        "contract": CONTRACT, "registered_at": history._iso(registered_at),
        "plan": plan, "plan_sha256": history._sha256(plan),
        "calculation_sources": calculation_hashes(),
        "timestamp_authority": "local_clock_only; verify_remote_commit_before_start_separately",
        "runtime_installed": False, "research_only": True,
    }, "registration_sha256")


def validate_registration(registration: Mapping, *, now: datetime) -> None:
    history._assert_simulation_only()
    now = _utc(now)
    try:
        registered = history._parse_utc(registration["registered_at"])
    except (KeyError, RuntimeError) as exc:
        raise ValueError("confirmation_forward_registration_timestamp_invalid") from exc
    if registered > now:
        raise ValueError("confirmation_forward_registration_in_future")
    # Exact regeneration checks schema, frozen definition, sources and digest,
    # not merely a self-consistent (and freely recomputable) supplied checksum.
    if registration != register(registered_at=registered):
        raise ValueError("confirmation_forward_registration_or_source_drift")


def status(registration: Mapping, *, now: datetime) -> dict:
    validate_registration(registration, now=now)
    return _seal({
        "contract": CONTRACT, **history._non_evidence_fields(), "clean_holdout": False,
        "as_of": history._iso(now), "registration_sha256": registration["registration_sha256"],
        "plan_sha256": registration["plan_sha256"],
        "window": {"start": history._iso(START), "end_exclusive": history._iso(END), "days": 90},
        "status": ("registered_not_started" if now < START else
                   "sealed_until_fixed_readout" if now < END else "fixed_readout_due_inputs_required"),
        "results": None, "runtime_installed": False, "data_collection_installed": False,
    }, "report_sha256")


def evaluate(registration: Mapping, rows: Mapping, *, now: datetime,
             source: Mapping | None = None) -> dict:
    result = status(registration, now=now)
    if now < END:
        return result  # Do not even inspect rows or source while sealed.
    result.pop("report_sha256")
    symbols = frozen_plan()["signal"]["symbols"]
    if set(rows) != set(symbols):
        raise ValueError("confirmation_forward_frozen_universe_mismatch")
    # Only this fixed window and its warmup affect signals or valuation. Validate
    # timestamps/order before excluding extra rows; no historical analyze call.
    days, selected = {}, {}
    for symbol in symbols:
        previous = None
        selected[symbol] = []
        for bar in rows[symbol]:
            if "symbol" in bar and bar["symbol"] != symbol:
                raise ValueError("confirmation_forward_row_symbol_mismatch")
            at = _utc(bar["open_time"])
            if previous is not None and at <= previous:
                raise ValueError("confirmation_forward_duplicate_or_unsorted")
            previous = at
            if WARMUP <= at < END:
                selected[symbol].append(bar)
        days[symbol] = original._daily(selected[symbol], as_of=END)[0]
    required = [WARMUP + i * original.DAY for i in range(151)]
    missing = {s: [history._iso(d) for d in required if d not in days[s]] for s in symbols}
    normalized = {s: [{k: history._iso(v) if isinstance(v, datetime) else
                       str(v) if isinstance(v, Decimal) else v for k, v in bar.items()}
                      for bar in selected[s]] for s in symbols}
    result.update(input_rows_sha256=history._sha256(normalized),
                  source_metadata_sha256=history._sha256(dict(source or {})),
                  source_verification="not_performed; normalized_rows_not_PIT_or_wire_receipt_proof",
                  coverage={"required_days_per_symbol": 151, "missing_days": missing},
                  status="fixed_window_incomplete_no_return_claim")
    if any(missing.values()):
        return _seal(result, "report_sha256")
    dates = [START + i * original.DAY for i in range(90)]
    # Pin arithmetic independently of caller context. No estimation/fitting,
    # candidate selection or risk-state carry-in occurs during this readout.
    with localcontext(Context(prec=28)):
        fs = {d: candidate.features(days, d) for d in [START - original.DAY, *dates]}
        targets = candidate.target_path(fs, dates, variant="confirmation_only")
        scenarios = {}
        for multiplier in (Decimal(1), Decimal(2)):
            arms = {
                "confirmation_only": risk._simulate(days, dates, mode="risk_trend",
                    target_provider=targets.__getitem__, cost_multiplier=multiplier),
                "baseline_risk": risk._simulate(days, dates, mode="risk_trend", cost_multiplier=multiplier),
                "btc_original_signal_budget_cash": risk._simulate(days, dates, mode="btc_cash",
                                                                  cost_multiplier=multiplier),
            }
            summaries = {name: candidate.summarize(arm) for name, arm in arms.items()}
            excess = (Decimal(summaries["confirmation_only"]["return"])
                      - Decimal(summaries["baseline_risk"]["return"]))
            scenarios[str(multiplier)] = {"arms": arms, "summary": summaries, "cash_return": "0",
                "excess_vs_baseline_risk": str(excess), "descriptive_criterion_met":
                Decimal(summaries["confirmation_only"]["return"]) > 0 and excess > 0}
        result.update(status="fixed_window_offline_readout_not_PIT", results=scenarios,
            descriptive_criterion_met=all(s["descriptive_criterion_met"] for s in scenarios.values()),
            recommendation="retain_fixed_result_no_promotion",
            limitations=["single_90_day_regime_not_statistical_edge", "prior_selection_from_12_historical_cells",
                         "offline_rows_no_first_seen_or_revision_authority", "sampled_counterfactual_not_actual_fills",
                         "BTC_context_not_strict_risk_match", "no_intraday_path_or_guaranteed_stop_loss"])
    return _seal(result, "report_sha256")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("register", "readout"))
    parser.add_argument("--registration", type=Path)
    parser.add_argument("--input", type=Path, help="Previously collected normalized formal TD rows; read only after end")
    parser.add_argument("--output", type=Path, required=True, help="New local research file; existing path never overwritten")
    args = parser.parse_args(argv)
    now = _now()  # Intentionally no --as-of / clock override on the CLI.
    if args.action == "register":
        if args.registration or args.input:
            parser.error("register does not consume an existing registration or price input")
        result = register(registered_at=now)
    else:
        if args.registration is None:
            parser.error("readout requires --registration")
        registration = json.loads(args.registration.read_text())
        result = status(registration, now=now)
        if now >= END and args.input is not None:
            raw = json.loads(args.input.read_text())
            rows = {s: [{**bar, "open_time": history._parse_utc(bar["open_time"])} for bar in bars]
                    for s, bars in raw["rows"].items()}
            result = evaluate(registration, rows, now=now, source=raw.get("source"))
    with args.output.open("x") as stream:
        json.dump(result, stream, ensure_ascii=True, sort_keys=True, indent=2, allow_nan=False)
        stream.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
