"""Frozen daily trend versus cash/BTC; pure research, no trading authority.

One candidate only: 20/60-day SMA, 20-day volatility, long/flat, daily orders
at 00:05 UTC using only previous completed days. The exact close/execution
bars must exist; unused intraday bars are not imputed or required.
Historical diagnostics are never a clean holdout; forward returns stay sealed
until the predeclared end date. No K10 definitions or runtime are consumed.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
from typing import Any, Mapping

from Crypto.market_observation import OBSERVATION_SYMBOLS
from Crypto.capital_policy import CRYPTO_INITIAL_CAPITAL_USDT
from Crypto.research_accounting import number
import Crypto.ten_symbol_factor_prescreen as history

ZERO, ONE = Decimal(0), Decimal(1)
DAY = timedelta(days=1)
BAR = timedelta(minutes=5)
CONTRACT = "tradingagent.crypto.slow_trend_research.v1"


def frozen_plan() -> dict[str, Any]:
    return {
        "candidate_id": "slow_trend_sma20_60_vol20_v1",
        "symbols": list(OBSERVATION_SYMBOLS),
        "fast_days": 20, "slow_days": 60, "vol_days": 20,
        "annual_vol_ceiling": "0.40", "max_symbol_weight": "0.10",
        "rebalance_relative_band": "0.25", "starting_research_cash": str(CRYPTO_INITIAL_CAPITAL_USDT),
        "fee_each_side": "0.001", "slippage_each_side": "0.0002",
        "fee_source": "assumed_not_account_fee_tier",
        "trial_count": 1, "parameter_search": False,
        "forward_start": "2026-08-31T00:00:00Z",
        "forward_end": "2026-11-29T00:00:00Z",
        "minimum_forward_days": 90,
        "selection_rule": "one_fixed_candidate; no_best_cell_selection",
        "readout_rule": "one_fixed_window; no_interim_returns_or_deadline_extension",
        "comparison_rule": "positive_net_and_better_than_cash_and_BTC; descriptive_only",
    }


def _daily(rows: list[Mapping[str, Any]], *, as_of: datetime) -> tuple[dict, int]:
    buckets: dict[datetime, list] = {}
    previous = None
    for row in rows:
        time = row["open_time"]
        if not isinstance(time, datetime) or time.utcoffset() != timedelta(0):
            raise ValueError("slow_trend_timestamp_invalid")
        if previous is not None and time <= previous:
            raise ValueError("slow_trend_duplicate_or_unsorted")
        previous = time
        if time + BAR > as_of:
            continue
        day = time.replace(hour=0, minute=0, second=0, microsecond=0)
        buckets.setdefault(day, []).append(row)
    days = {}
    excluded = 0
    for day, bars in sorted(buckets.items()):
        by_time = {bar["open_time"]: bar for bar in bars}
        if day + BAR not in by_time or day + DAY - BAR not in by_time:
            excluded += 1
            continue
        days[day] = {
            "close": number(by_time[day + DAY - BAR]["close"], positive=True),
            "execution_open": number(by_time[day + BAR]["open"], positive=True),
        }
    return days, excluded


def _weights(days: Mapping[str, Mapping], day: datetime) -> dict[str, Decimal]:
    plan = frozen_plan()
    weights = {}
    for symbol in plan["symbols"]:
        # All 60 daily bars must exist, not just both endpoints.
        dates = [day - i * DAY for i in range(60, 0, -1)]
        if not all(d in days[symbol] for d in dates):
            weights[symbol] = ZERO
            continue
        closes = [days[symbol][d]["close"] for d in dates]
        fast, slow = sum(closes[-20:]) / 20, sum(closes) / 60
        changes = [b / a - ONE for a, b in zip(closes[-21:-1], closes[-20:])]
        mean = sum(changes) / 20
        vol = (sum((r - mean) ** 2 for r in changes) / 20 * 365).sqrt()
        scale = min(ONE, number(plan["annual_vol_ceiling"]) / vol) if vol else ONE
        weights[symbol] = number(plan["max_symbol_weight"]) * scale if fast > slow else ZERO
    return weights


def _simulate(days: Mapping[str, Mapping], dates: list[datetime], *, benchmark: bool) -> dict:
    plan = frozen_plan()
    fee, slip = number(plan["fee_each_side"]), number(plan["slippage_each_side"])
    initial = number(plan["starting_research_cash"])
    cash = initial
    quantities = {s: ZERO for s in plan["symbols"]}
    ledger, equity_curve = [], []
    fees = ZERO
    turnover = ZERO
    peak, max_drawdown = initial, ZERO
    for index, day in enumerate(dates):
        prices = {s: days[s][day]["execution_open"] for s in quantities}
        before = cash + sum(quantities[s] * prices[s] for s in quantities)
        weights = {s: ONE if s == "BTCUSDT" else ZERO for s in quantities} if benchmark else _weights(days, day)
        orders = {}
        if not benchmark or index == 0:
            for symbol, weight in weights.items():
                current = quantities[symbol] * prices[symbol]
                target = weight * before
                if target and current and abs(target - current) <= target * number(plan["rebalance_relative_band"]):
                    continue
                orders[symbol] = target / prices[symbol] - quantities[symbol]
        # Sell before buy. Purchases share available cash; fees cannot create debt.
        for symbol, change in sorted(orders.items()):
            if change >= 0:
                continue
            fill = prices[symbol] * (ONE - slip)
            notional = -change * fill
            cost = notional * fee
            cash += notional - cost
            quantities[symbol] += change
            fees += cost
            turnover += notional
            ledger.append([history._iso(day + BAR), symbol, "sell", str(-change), str(fill), str(cost)])
        required = sum(change * prices[s] * (ONE + slip) * (ONE + fee) for s, change in orders.items() if change > 0)
        scale = min(ONE, cash / required) if required else ZERO
        for symbol, change in sorted(orders.items()):
            if change <= 0 or scale <= 0:
                continue
            quantity = change * scale
            fill = prices[symbol] * (ONE + slip)
            notional = quantity * fill
            cost = notional * fee
            cash -= notional + cost
            quantities[symbol] += quantity
            fees += cost
            turnover += notional
            ledger.append([history._iso(day + BAR), symbol, "buy", str(quantity), str(fill), str(cost)])
        if cash < Decimal("-1e-18"):
            raise ValueError("slow_trend_cash_conservation_failed")
        equity = cash + sum(quantities[s] * days[s][day]["close"] for s in quantities)
        if index == len(dates) - 1:
            # Symmetric terminal liquidation costs for strategy and BTC baseline.
            for symbol, quantity in quantities.items():
                if not quantity:
                    continue
                fill = days[symbol][day]["close"] * (ONE - slip)
                notional = quantity * fill
                cost = notional * fee
                cash += notional - cost
                fees += cost
                turnover += notional
                ledger.append([history._iso(day + DAY), symbol, "liquidate", str(quantity), str(fill), str(cost)])
            equity = cash
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, (peak - equity) / peak)
        equity_curve.append([history._iso(day + DAY), str(equity)])
    return {
        "return": str(cash / initial - ONE), "final_equity": str(cash),
        "fees": str(fees), "turnover_over_initial_cash": str(turnover / initial),
        "max_drawdown_daily_close": str(max_drawdown), "trade_leg_count": len(ledger),
        "ledger": ledger, "ledger_sha256": history._sha256(ledger),
        "daily_equity": equity_curve,
    }


def analyze(rows: Mapping[str, list[dict[str, Any]]], *, as_of: datetime) -> dict[str, Any]:
    history._assert_simulation_only()
    if as_of.utcoffset() != timedelta(0):
        raise ValueError("slow_trend_as_of_invalid")
    plan = frozen_plan()
    if set(rows) != set(plan["symbols"]):
        raise ValueError("slow_trend_frozen_universe_mismatch")
    days, exclusions = {}, {}
    for symbol in plan["symbols"]:
        days[symbol], exclusions[symbol] = _daily(rows[symbol], as_of=as_of)
    common = sorted(set.intersection(*(set(d) for d in days.values())))
    eligible = [d for d in common if any(all(d - i * DAY in days[s] for i in range(1, 61)) for s in days)]
    start, end = history._parse_utc(plan["forward_start"]), history._parse_utc(plan["forward_end"])
    historical = [d for d in eligible if d + DAY <= start]
    forward = [d for d in eligible if start <= d and d + DAY <= end]
    # One contiguous historical segment, chosen by length then earliest time,
    # never by returns. Do not stitch across data gaps or reset losing equity.
    segments: list[list[datetime]] = []
    for day in historical:
        if not segments or day != segments[-1][-1] + DAY:
            segments.append([])
        segments[-1].append(day)
    selected = max(segments, key=len) if segments else []
    result = {
        "contract": CONTRACT, **history._non_evidence_fields(),
        "plan": plan, "plan_sha256": history._sha256(plan),
        "as_of": history._iso(as_of), "clean_holdout": False,
        "cost_model": "assumed_taker; fractional_next_day_0005_open; terminal_close_liquidation",
        "execution_warning": "counterfactual_not_actual_fills; no_depth_or_exchange_filter_model",
        "daily_sampling": "UTC_0005_open_and_2355_bar_close_only; not_intraday_path",
        "historical": {"status": "insufficient_complete_history", "required_lookback_days": 60},
        "data_quality": {"incomplete_days": exclusions, "common_complete_days": len(common),
                         "eligible_days": len(eligible), "historical_segment_count": len(segments)},
        "forward": {"status": "sealed_until_fixed_readout", "eligible_days": len(forward),
                    "net_returns": None, "promotion_authorized": False},
    }
    def compare(dates):
        trend = _simulate(days, dates, benchmark=False)
        btc = _simulate(days, dates, benchmark=True)
        return {"start": history._iso(dates[0]), "end": history._iso(dates[-1] + DAY),
                "days": len(dates), "trend": trend, "btc_buy_hold": btc, "cash_return": "0",
                "net_excess_vs_btc": str(number(trend["return"]) - number(btc["return"])),
                "net_excess_vs_cash": trend["return"],
                "risk_warning": "BTC_is_full_exposure; lower_drawdown_alone_is_not_alpha"}
    if selected:
        result["historical"] = {"status": "historical_diagnostic_not_holdout", **compare(selected)}
    if as_of >= end:
        # A fixed-window NAV cannot skip unknown days while holding positions.
        wanted = [start + i * DAY for i in range((end - start).days)]
        if forward != wanted:
            result["forward"]["status"] = "fixed_window_incomplete_no_return_claim"
        else:
            comparison = compare(forward)
            positive = number(comparison["net_excess_vs_cash"]) > 0 and number(comparison["net_excess_vs_btc"]) > 0
            result["forward"] = {"status": "positive_requires_independent_PIT_review" if positive else "fixed_candidate_rejected",
                                 "comparison": comparison, "promotion_authorized": False,
                                 "clean_holdout": False,
                                 "reason": "offline_rows_cannot_prove_first_seen_or_registry_authority"}
    result["report_sha256"] = history._sha256(result)
    return result


def fetch_daily_inputs(client, *, start: datetime, end: datetime) -> tuple[dict, dict]:
    """Bounded catalog/query consumption of exactly two closed 5m bars/day.

Uses existing TD `in` filter (100 values) and receipt/pagination validation.
Missing required points are counted, never replaced by a neighbouring price.
"""
    history._assert_simulation_only()
    if (start.utcoffset() != timedelta(0) or end.utcoffset() != timedelta(0)
            or any(t.hour or t.minute or t.second or t.microsecond for t in (start, end))
            or not 1 <= (end - start).days <= 400):
        raise ValueError("slow_trend_fetch_window_invalid")
    catalog = client.get_catalog()
    requested = [start + i * DAY + offset for i in range((end - start).days) for offset in (BAR, DAY - BAR)]
    rows, receipts, missing = {}, {}, {}
    for symbol in frozen_plan()["symbols"]:
        dataset = history._dataset_id(symbol)
        history._verify_catalog(catalog, dataset)
        wire_rows = []
        receipts[symbol] = []
        for offset in range(0, len(requested), 100):
            batch = requested[offset:offset + 100]
            run = history.collect_query_pages(
                client=client,
                request=history.QueryRequest(dataset_id=dataset, schema_major=1, fields=history.BAR_FIELDS,
                    filters={"symbol": {"eq": symbol}, "open_time": {"in": [history._wire_iso(t) for t in batch]}},
                    order=("symbol:asc", "open_time:asc"), limit=100),
                identity_fields=("symbol", "open_time"), max_pages=1, max_rows=100,
            )
            envelope = run.envelope
            meta = envelope.metadata
            if meta.state != "ready" or meta.degraded or meta.quality.get("state") != "valid" or not meta.receipt_id:
                raise ValueError("slow_trend_fetch_metadata_invalid")
            page = [dict(row) for row in envelope.data]
            if any(history._parse_utc(row["open_time"]) not in batch for row in page):
                raise ValueError("slow_trend_fetch_window_overflow")
            wire_rows.extend(page)
            receipts[symbol].append({"receipt_id": meta.receipt_id, "observed_at": meta.observed_at,
                "data_through": meta.data_through, "row_count": len(page), "rows_sha256": history._sha256(page)})
        rows[symbol], _ = history._validate_history_rows(wire_rows, symbol=symbol)
        missing[symbol] = len(requested) - len(wire_rows)
    return rows, {"kind": "tradingdatas_catalog_query", "catalog_version": catalog.catalog_version,
                  "historical_backfill_no_pit": True, "receipts": receipts, "missing_requested_points": missing,
                  "query_start": history._iso(start), "query_end_exclusive": history._iso(end)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--raw-dir", type=Path)
    inputs.add_argument("--runtime-manifest", type=Path)
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--report", type=Path, help="write a NEW research artifact; never overwrite")
    parser.add_argument("--as-of", required=True)
    args = parser.parse_args()
    history._assert_simulation_only()
    if args.report and args.report.exists():
        parser.error("--report must not already exist")
    if args.raw_dir:
        rows, meta = history.load_raw_dir(args.raw_dir)
        source = {"kind": "validated_TD_historical_raw", "metadata": meta}
    else:
        from Crypto.ten_symbol_observation_runtime import load_crypto_ten_symbol_observation_runtime_manifest, RUNTIME_TOKEN_FILE
        from Crypto.ten_symbol_health_watch import _probe_client
        from shared.data.tradingdatas_transport import build_runtime_transport
        if not args.start or not args.end:
            parser.error("--runtime-manifest requires --start and --end")
        manifest = load_crypto_ten_symbol_observation_runtime_manifest(args.runtime_manifest)
        transport = build_runtime_transport("http-json-v1", token_file=RUNTIME_TOKEN_FILE, base_url=manifest.base_url)
        client, _ = _probe_client(manifest=manifest, transport=transport, dataset_ids=manifest.dataset_ids, timeout_seconds=40)
        rows, source = fetch_daily_inputs(client, start=history._parse_utc(args.start), end=history._parse_utc(args.end))
    result = analyze(rows, as_of=history._parse_utc(args.as_of))
    result.pop("report_sha256")
    result["source"] = source
    result["report_sha256"] = history._sha256(result)
    if args.report:
        history._write_file_atomic(args.report, (history._canonical_json(result) + "\n").encode())
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
