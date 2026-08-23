"""Offline quality stratification of the lockup sell_off signal.

Research-only.  Replays the real ``event_catalyst_shadow`` factor over the
historical lockup calendar exactly like ``event_calendar_shadow_replay``,
keeps only the tracked signal (lockup expiries classified ``sell_off``),
and splits the labelled outcomes across three conditioning dimensions the
rolling tracker can actually observe before the event:

* float-ratio buckets — how much of the float unlocks,
* industry — Tushare ``stock_basic`` classification (fetched once into
  ``stock_basic.csv`` when absent; requires TUSHARE_MCP_TOKEN),
* market regime — SSE index return over the 10 sessions ending at the
  event-day close (weak / sideways / strong).

Every stratum is additionally reported net of an explicit round-trip cost
model (``COST_BPS_ROUNDTRIP_DEFAULT``, override with ``--cost-bps``), so
the descriptive question "which subsets still clear costs" is answered on
the same numbers the rolling tracker journals.  The default models a
round trip as: commission ~2.5 bps per side + 5 bps stamp duty on the
sell + ~0.2 bps transfer fee + ~5 bps slippage allowance ≈ 15 bps.

One event per (symbol, expiry date): same-day rows from multiple holders
are collapsed to the largest ``float_ratio`` so correlated rows do not
inflate the counts.  Post-event returns are absolute (same measure the
tracker journals), so strata partly inherit market beta; the regime split
is the partial control.  Nothing here is promotion evidence.

Usage::

    python3 Ashare/event_calendar_lockup_strata.py \
        [--cache /tmp/ashare_event_research] [--expanded] [--cost-bps N]
"""

from __future__ import annotations

import csv
import json
import os
import statistics
import sys
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from Ashare.event_catalyst_adapter import (  # noqa: E402
    SHARE_FLOAT_DATASET_ID,
    EventCatalystAdapterError,
    catalyst_entries_from_calendar_document,
    catalyst_entry_from_lockup_row,
)
from Ashare.event_catalyst_shadow import (  # noqa: E402
    POSITIONING_PROFILE_MOMENTUM_EVIDENCE_V1,
    DailyBar,
    build_catalyst_shadow_batch,
)


SIGNAL_EVENT_TYPE = "lockup_expiry"
SIGNAL_ANTICIPATION_CLASS = "sell_off"
REGIME_BINS = ((-1.0, -0.02, "weak"), (-0.02, 0.02, "sideways"), (0.02, 1.0, "strong"))
RATIO_BINS = ((0.0, 1.0, "<1%"), (1.0, 3.0, "1-3%"), (3.0, 5.0, "3-5%"), (5.0, 1e9, ">=5%"))
MIN_INDUSTRY_N = 40

# Round-trip cost model (basis points): commission ~2.5bps/side, 5bps stamp
# duty on the sell, ~0.2bps transfer fee, ~5bps slippage allowance.
COST_BPS_ROUNDTRIP_DEFAULT = 15.0


class StrataError(RuntimeError):
    """Fail-closed stratification failure with a stable reason code."""


def _read_csv(cache: Path, name: str) -> list[dict[str, str]]:
    path = cache / f"{name}.csv"
    if not path.exists():
        raise StrataError(f"cache_missing:{path.name}")
    with path.open(encoding="utf-8") as handle:
        reader = csv.reader(handle)
        fields = next(reader)
        return [dict(zip(fields, row)) for row in reader]


def _parse_day(raw: str) -> date:
    return datetime.strptime(raw.strip(), "%Y%m%d").date()


def ensure_stock_basic(cache: Path) -> Path:
    """Fetch the ts_code -> industry mapping once into the cache."""

    path = cache / "stock_basic.csv"
    if path.exists():
        return path
    token = os.environ.get("TUSHARE_MCP_TOKEN", "").strip().strip('"').strip("'")
    if not token:
        raise StrataError("token_missing")
    request = urllib.request.Request(
        "https://api.tushare.pro",
        data=json.dumps(
            {
                "api_name": "stock_basic",
                "token": token,
                "params": {"list_status": "L"},
                "fields": "ts_code,industry",
            }
        ).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read())
    except Exception as exc:
        raise StrataError(f"stock_basic_fetch_failed:{exc}") from exc
    if payload.get("code") != 0:
        raise StrataError(f"api_error:{payload.get('code')}")
    data = payload.get("data") or {}
    fields = data.get("fields") or []
    items = data.get("items") or []
    if "ts_code" not in fields or "industry" not in fields or not items:
        raise StrataError("stock_basic_empty")
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["ts_code", "industry"])
        for item in items:
            record = dict(zip(fields, item))
            writer.writerow([record["ts_code"], record.get("industry", "")])
    return path


def collapse_lockup_rows(rows: list[dict[str, str]]) -> dict[tuple[str, str], dict]:
    """One row per (symbol, expiry date), keeping the largest float_ratio."""

    best: dict[tuple[str, str], dict] = {}
    for raw_row in rows:
        if _parse_day(raw_row["float_date"]) < _parse_day(raw_row["ann_date"]):
            continue
        key = (raw_row["ts_code"], raw_row["float_date"])
        try:
            ratio = float(raw_row["float_ratio"])
        except (TypeError, ValueError):
            ratio = -1.0
        if key not in best or ratio > best[key][1]:
            best[key] = (raw_row, ratio)
    return {key: value[0] for key, value in best.items()}


def build_signal_entries(
    collapsed: dict[tuple[str, str], dict],
) -> tuple[list, int]:
    """Mint one lockup entry per collapsed event via the validated row path."""

    entries: list = []
    skipped = 0
    for idx, (_, row) in enumerate(sorted(collapsed.items())):
        minted = dict(row)
        for field_name in ("float_share", "float_ratio"):
            try:
                minted[field_name] = float(minted[field_name])
            except (TypeError, ValueError):
                pass
        try:
            entry = catalyst_entry_from_lockup_row(
                minted,
                dataset_id=SHARE_FLOAT_DATASET_ID,
                receipt_id=f"strata-{idx:06d}",
            )
        except EventCatalystAdapterError:
            skipped += 1
            continue
        entries.append(entry)
    return entries, skipped


def load_bars(cache: Path, samples: set[str]) -> dict[str, list[DailyBar]]:
    bars_by_symbol: dict[str, list[DailyBar]] = {}
    for code in sorted(samples):
        stem = code.replace(".", "")
        bar_path = cache / f"daily_{stem}.csv"
        adj_path = cache / f"adjfactor_{stem}.csv"
        if not bar_path.exists() or not adj_path.exists():
            continue
        with bar_path.open(encoding="utf-8") as handle:
            reader = csv.reader(handle)
            fields = next(reader)
            close_i = fields.index("close")
            date_i = fields.index("trade_date")
            rows = [(r[date_i], float(r[close_i])) for r in reader]
        with adj_path.open(encoding="utf-8") as handle:
            reader = csv.reader(handle)
            fields = next(reader)
            ai = fields.index("adj_factor")
            di = fields.index("trade_date")
            factors = {r[di]: float(r[ai]) for r in reader}
        latest = max(factors.values())
        series = [
            DailyBar(trade_date=_parse_day(d), close=c * factors[d] / latest)
            for d, c in rows
            if d in factors and c > 0
        ]
        series.sort(key=lambda bar: bar.trade_date)
        if series:
            bars_by_symbol[code] = series
    return bars_by_symbol


def load_index_series(cache: Path) -> list[tuple[date, float]]:
    name = "index_000001SH"
    path = cache / f"{name}.csv"
    if not path.exists():
        raise StrataError(f"cache_missing:{name}.csv")
    with path.open(encoding="utf-8") as handle:
        reader = csv.reader(handle)
        fields = next(reader)
        date_i = fields.index("trade_date")
        close_i = fields.index("close")
        pairs = sorted(
            (_parse_day(r[date_i]), float(r[close_i])) for r in reader
        )
    return pairs


def regime_bucket(index_pairs: list[tuple[date, float]], event_day: date) -> str:
    """Index return over the 10 sessions ending AT the event-day close.

    Fully known when the hypothetical post-event position opens (the
    labelled window starts at the event-day close), so this stays a
    legitimately observable conditioning variable.
    """

    days = [d for d, _ in index_pairs]
    lo, hi = 0, len(days) - 1
    pos: int | None = None
    while lo <= hi:
        mid = (lo + hi) // 2
        if days[mid] == event_day:
            pos = mid
            break
        if days[mid] < event_day:
            lo = mid + 1
        else:
            hi = mid - 1
    if pos is None or pos < 10:
        return "unknown"
    ret = index_pairs[pos][1] / index_pairs[pos - 10][1] - 1.0
    for low, high, label in REGIME_BINS:
        if low <= ret < high:
            return label
    return "unknown"


def bucket_by_ratio(ratio: float | None) -> str:
    if ratio is None:
        return "unknown"
    for low, high, label in RATIO_BINS:
        if low <= ratio < high:
            return label
    return "unknown"


def group_stats(values: list[float], cost_bps: float = 0.0) -> dict:
    """Descriptive stats, gross and (when cost_bps > 0) net of one round trip."""

    n = len(values)
    if n == 0:
        return {"n": 0}
    ordered = sorted(values)
    stats = {
        "n": n,
        "mean_bps": round(statistics.fmean(values) * 1e4, 1),
        "median_bps": round(statistics.median(values) * 1e4, 1),
        "win_rate": round(sum(1 for v in values if v > 0) / n, 3),
        "p25_bps": round(ordered[n // 4] * 1e4, 1),
        "p75_bps": round(ordered[(3 * n) // 4] * 1e4, 1),
    }
    if cost_bps > 0:
        net = [v - cost_bps / 1e4 for v in values]
        stats["mean_net_bps"] = round(statistics.fmean(net) * 1e4, 1)
        stats["median_net_bps"] = round(statistics.median(net) * 1e4, 1)
        stats["win_rate_net"] = round(sum(1 for v in net if v > 0) / len(net), 3)
    return stats


def main() -> int:
    cache = (
        Path(sys.argv[sys.argv.index("--cache") + 1])
        if "--cache" in sys.argv
        else Path("/tmp/ashare_event_research")
    )
    expanded = "--expanded" in sys.argv
    symbols_file = "sample_symbols_expanded" if expanded else "sample_symbols"
    float_file = "share_float_expanded" if expanded else "share_float"
    cost_bps = (
        float(sys.argv[sys.argv.index("--cost-bps") + 1])
        if "--cost-bps" in sys.argv
        else COST_BPS_ROUNDTRIP_DEFAULT
    )

    sym_rows = _read_csv(cache, symbols_file)
    samples = {r["ts_code"] for r in sym_rows}

    stock_basic = ensure_stock_basic(cache)
    industry = {
        r["ts_code"]: r["industry"].strip()
        for r in _read_csv(cache, "stock_basic")
        if r["industry"].strip()
    }
    print(f"industry_map={len(industry)} ({stock_basic.name})", flush=True)

    collapsed = collapse_lockup_rows(
        [r for r in _read_csv(cache, float_file) if r["ts_code"] in samples]
    )
    entries, skipped = build_signal_entries(collapsed)
    print(
        f"events={len(entries)} adapter_skipped={skipped} universe={symbols_file}",
        flush=True,
    )

    ratio_lookup: dict[tuple[str, str], float] = {}
    for (code, float_date), row in collapsed.items():
        try:
            ratio_lookup[(code, float_date)] = float(row["float_ratio"])
        except (TypeError, ValueError):
            pass

    bars_by_symbol = load_bars(cache, samples)
    print(f"symbols_with_bars={len(bars_by_symbol)}", flush=True)
    index_pairs = load_index_series(cache)

    batch = build_catalyst_shadow_batch(
        catalyst_entries_from_calendar_document(
            {
                "calendar_id": "ashare-lockup-strata-v1",
                "entries": [
                    {
                        "event_id": e.event_id,
                        "event_type": e.event_type,
                        "scheduled_date": e.scheduled_date.isoformat(),
                        "date_confidence": e.date_confidence,
                        "impact_direction": e.impact_direction,
                        "source_ref": e.source_ref,
                        "entity": e.entity,
                        "symbol": e.symbol,
                    }
                    for e in entries
                ],
            }
        ),
        bars_by_symbol,
        as_of=datetime.now(timezone.utc),
        positioning_profile=POSITIONING_PROFILE_MOMENTUM_EVIDENCE_V1,
    )

    outcomes: list[dict] = []
    for obs in batch.observations:
        if obs.observation_status != "observed":
            continue
        if obs.post_label_state != "labeled" or obs.post_return is None:
            continue
        if obs.event_type != SIGNAL_EVENT_TYPE:
            continue
        if obs.anticipation_class != SIGNAL_ANTICIPATION_CLASS:
            continue
        symbol = obs.symbol or ""
        float_date = obs.scheduled_date.strftime("%Y%m%d")
        outcomes.append(
            {
                "post_return": float(obs.post_return),
                "ratio": ratio_lookup.get((symbol, float_date)),
                "industry": industry.get(symbol, ""),
                "regime": regime_bucket(index_pairs, obs.scheduled_date),
            }
        )
    print(f"signal_outcomes={len(outcomes)}", flush=True)

    def stratify(key_fn) -> dict[str, dict]:
        groups: dict[str, list[float]] = {}
        for item in outcomes:
            groups.setdefault(key_fn(item), []).append(item["post_return"])
        return {
            key: group_stats(values, cost_bps)
            for key, values in sorted(groups.items())
        }

    summary: dict = {
        "research_only": True,
        "universe": symbols_file,
        "signal": f"{SIGNAL_EVENT_TYPE}|{SIGNAL_ANTICIPATION_CLASS}",
        "cost_bps_roundtrip": cost_bps,
        "overall": group_stats([o["post_return"] for o in outcomes], cost_bps),
        "by_float_ratio": stratify(lambda o: bucket_by_ratio(o["ratio"])),
        "by_regime": stratify(lambda o: o["regime"]),
    }

    industry_groups: dict[str, list[float]] = {}
    for item in outcomes:
        if item["industry"]:
            industry_groups.setdefault(item["industry"], []).append(
                item["post_return"]
            )
    major = {
        name: group_stats(values, cost_bps)
        for name, values in sorted(
            industry_groups.items(), key=lambda kv: -len(kv[1])
        )
        if len(values) >= MIN_INDUSTRY_N
    }
    rest = [
        v
        for name, values in industry_groups.items()
        if len(values) < MIN_INDUSTRY_N
        for v in values
    ]
    summary["by_industry_major"] = major
    summary["by_industry_rest_aggregate"] = group_stats(rest, cost_bps)

    out_path = cache / "lockup_strata_summary.json"
    out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    # Console Markdown report.
    labels = {
        "by_float_ratio": "按解禁流通盘比例（float_ratio）",
        "by_regime": "按市场环境（上证截至事件日收盘的 10 日收益）",
    }
    print("# Lockup sell_off signal quality strata (research_only)\n")
    overall = summary["overall"]
    print(
        f"- universe: {summary['universe']}; signal outcomes n={overall['n']}"
        + (
            f", mean={overall['mean_bps']}bps, median={overall['median_bps']}bps"
            f", win_rate={overall['win_rate']}"
            if overall.get("n")
            else ""
        )
    )
    print(f"- round-trip cost model: {cost_bps}bps (net columns deduct it once)")
    for section, title in labels.items():
        print(f"\n## {title}\n")
        print(
            "| 分层 | n | mean_bps | median_bps | win_rate "
            "| mean_net | median_net | win_net |"
        )
        print("|---|---|---|---|---|---|---|---|")
        for key, stat in summary[section].items():
            if not stat.get("n"):
                print(f"| {key} | 0 | | | | | | |")
                continue
            print(
                f"| {key} | {stat['n']} | {stat['mean_bps']} | {stat['median_bps']} "
                f"| {stat['win_rate']} | {stat.get('mean_net_bps', '')} "
                f"| {stat.get('median_net_bps', '')} "
                f"| {stat.get('win_rate_net', '')} |"
            )
    print("\n## 按行业（样本数 ≥ %d 的行业）\n" % MIN_INDUSTRY_N)
    print("| 行业 | n | mean_bps | median_bps | win_rate | mean_net | win_net |")
    print("|---|---|---|---|---|---|---|")
    for name, stat in summary["by_industry_major"].items():
        print(
            f"| {name} | {stat['n']} | {stat['mean_bps']} | {stat['median_bps']} "
            f"| {stat['win_rate']} | {stat.get('mean_net_bps', '')} "
            f"| {stat.get('win_rate_net', '')} |"
        )
    rest_stat = summary["by_industry_rest_aggregate"]
    if rest_stat.get("n"):
        print(
            f"| 其余行业合计 | {rest_stat['n']} | {rest_stat['mean_bps']} "
            f"| {rest_stat['median_bps']} | {rest_stat['win_rate']} "
            f"| {rest_stat.get('mean_net_bps', '')} "
            f"| {rest_stat.get('win_rate_net', '')} |"
        )
    survivors = [
        (key, stat)
        for section in ("by_float_ratio", "by_regime")
        for key, stat in summary[section].items()
        if stat.get("n") and stat["mean_net_bps"] > 0 and stat["win_rate_net"] > 0.5
    ]
    print("\n## 成本后仍为正的分层（mean_net>0 且 win_net>50%）\n")
    if survivors:
        for key, stat in survivors:
            print(
                f"- {key}: n={stat['n']}, mean_net={stat['mean_net_bps']}bps, "
                f"win_net={stat['win_rate_net']}"
            )
    else:
        print("- (none)")
    print(f"\nsaved -> {out_path}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except StrataError as exc:
        print(f"STRATA_FAILED {exc}", file=sys.stderr)
        sys.exit(1)
