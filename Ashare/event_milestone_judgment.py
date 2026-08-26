"""Offline milestone-judgment evaluator (frozen family criteria).

The criteria document fixes keep/fail/gray at milestone sample counts;
the first lockup judgment (#571) was computed with an ad-hoc throwaway
script.  Judgments recur every week as samples accumulate (lockup second
judgment toward ~200 weak samples, earnings_pos/neg first judgments off
the Aug 31 export), so this module replaces the throwaway pattern with
one tested evaluator over the tracker's per-sample state export (#570):

- ``lockup_rule``   : labeled_outcomes[lockup] filtered to the practice
  rule arm (regime == ``weak`` AND ratio tagged outside the avoided
  ``3-5%`` band) — identical predicate to the tracker's
  ``rule_subset_breakdown``.  Row-level continuity preset; see the
  sample-unit decision doc for why it is no longer the canonical basis.
- ``lockup_rule_events`` : canonical lockup basis (#588) — unique
  records collapsed to economic events (symbol × unlock day),
  intersection rule predicate over each event's tagged records.
- ``earnings_*``    : labeled_outcomes[signal] post-event gross bps
  (#582 fix; ``prewindow_samples`` is descriptive-only).
- ``raw``           : labeled_outcomes[key] unfiltered.

All presets collapse to one row per unique event before judging
(:func:`dedupe_samples`, #586): lockup event ids embed a per-run
discovery sequence, so exports re-carry already-seen events under fresh
ids and raw row counts overstate the sample.

Net series deducts one round trip at the cost model (default 15 bps),
matching the tracker's net columns.  Verdicts are descriptive of the
frozen rules only: nothing here promotes anything, watch-list and
deployment decisions stay governed by the criteria document.
research_only / not_promotion_evidence.

Usage::

    python3 Ashare/event_milestone_judgment.py --state signal_tracker_state.json \\
        --preset lockup_rule [--gate-n 30] [--cost-bps 15.0]
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

LOCKUP_SIGNAL = "lockup"
EARNINGS_POS_SIGNAL = "earnings_pos"
EARNINGS_NEG_SIGNAL = "earnings_neg"
RULE_REGIME = "weak"
RULE_EXCLUDED_RATIO_BAND = "3-5%"

KEEP_WIN = 0.52               # frozen family thresholds (criteria document)
FAIL_WIN = 0.45


class MilestoneJudgmentError(RuntimeError):
    """Fail-closed evaluation failure with a stable reason code."""


def rule_arm_sample(sample: dict) -> bool:
    """Practice-rule predicate mirroring the tracker's rule subset."""

    return (
        sample.get("regime") == RULE_REGIME
        and sample.get("ratio_bucket") is not None
        and sample.get("ratio_bucket") != RULE_EXCLUDED_RATIO_BAND
    )


_TRACKER_SEQ_SEGMENT = re.compile(r"tracker-\d+")


def stable_event_key(event_id: str) -> tuple:
    """Identity of an event with volatile discovery-sequence segments stripped.

    The lockup family's ``event_id`` embeds a per-run ``tracker-NNNNNN``
    discovery sequence, so the same underlying disclosure event is
    re-numbered on every tracker run — and the per-run export itself
    carries one row per rediscovery (#586 finding: the rehearsal export's
    197 lockup rows are 138 unique events; its 100-row rule arm is 69).
    Stripping every ``tracker-<digits>`` segment recovers the stable
    identity (dataset/symbol/date/holder/type).  Collapsing only ever
    shrinks n, so any mis-merge biases against passing gates (safe side).
    """

    return tuple(
        p for p in str(event_id).split(":") if not _TRACKER_SEQ_SEGMENT.fullmatch(p)
    )


def dedupe_samples(samples: list[dict]) -> list[dict]:
    """Collapse re-discovered duplicate events, keeping the first row.

    Duplicate rows carry identical values (verified across the full rule
    arm in #586), so one row per stable key preserves each observation
    exactly once while removing the cross-run rediscovery artifact.
    Scope note (#586): this does NOT resolve holder-granularity
    multiplicity — several disclosure records of one company unlocking
    on one day stay separate rows even though they share one price path.
    That sample-unit question is a frozen-methodology decision recorded
    separately, not silently folded here.  Order-preserving,
    deterministic; rows without an ``event_id`` are never merged.
    """

    seen: dict[tuple, dict] = {}
    for s in samples:
        eid = s.get("event_id")
        if eid:
            key = stable_event_key(eid)
        else:  # pragma: no cover - exercised via tests below
            key = ("no-event-id", id(s))
        seen.setdefault(key, s)
    return list(seen.values())


def descriptive_profile(
    samples: list[dict], cost_bps: float = 15.0
) -> list[dict]:
    """Month-bucket descriptive cells over the (unique) sample series.

    Interpretation aid for the frozen-criteria verdicts (#583 matrix:
    e.g. a KEEP verdict on earnings_pos should be checked for early-year
    concentration before being read as live drift).  Strictly
    non-gating: nothing here enters keep/fail/gray, and month cells are
    never promoted to decision inputs.
    """

    buckets: dict[str, list[float]] = {}
    for s in sorted(samples, key=lambda x: str(x.get("event_date", ""))):
        month = str(s.get("event_date", ""))[:7]
        buckets.setdefault(month, []).append(
            float(s["post_return_bps"]) - cost_bps
        )
    return [
        {
            "month": m,
            "n": len(v),
            "mean_net_bps": round(statistics.fmean(v), 1),
            "win_net": round(sum(1 for x in v if x > 0) / len(v), 3),
        }
        for m, v in sorted(buckets.items())
    ]


def rule_arm_event(records: list[dict]) -> bool:
    """Event-level practice-rule predicate (intersection semantics).

    Frozen in the sample-unit decision doc: an economic event qualifies
    only when every tagged disclosure record of the symbol×day carries
    the rule regime AND none of them falls in the excluded ratio band.
    Untagged records neither veto nor qualify; a fully untagged event is
    out.  Intersection (rather than any-record-passes) mirrors the
    avoidance reading of the band and biases against passing gates —
    the same conservative direction as duplicate removal.
    """

    tagged = [r for r in records if r.get("ratio_bucket") is not None]
    if not tagged:
        return False
    if any(r.get("regime") != RULE_REGIME for r in tagged):
        return False
    return all(r.get("ratio_bucket") != RULE_EXCLUDED_RATIO_BAND for r in tagged)


def aggregate_rule_arm_events(rows: list[dict]) -> list[dict]:
    """Collapse unique lockup records to economic events (symbol × day).

    Event identity comes from the stable key's symbol segment plus
    ``event_date`` (verified identical to the id-embedded unlock day).
    The representative record carries a synthesized ``event_id``
    (``event:<symbol>:<day>``) so :func:`judge` and reporting work
    unchanged.  Order-preserving by first appearance after dedupe.
    """

    by_event: dict[tuple, list[dict]] = {}
    order: list[tuple] = []
    for r in rows:
        key = stable_event_key(r.get("event_id", ""))
        sym = key[2] if len(key) >= 4 else str(key)
        ident = (sym, str(r.get("event_date")))
        if ident not in by_event:
            by_event[ident] = []
            order.append(ident)
        by_event[ident].append(r)

    picked = []
    for ident in order:
        grp = by_event[ident]
        if rule_arm_event(grp):
            rep = dict(grp[0])
            rep["event_id"] = f"event:{ident[0]}:{ident[1]}"
            picked.append(rep)
    return picked


def _half_stats(net: list[float]) -> dict:
    if not net:
        return {"n": 0, "mean_net_bps": None}
    return {
        "n": len(net),
        "mean_net_bps": round(statistics.fmean(net), 1),
    }


def judge(
    samples: list[dict],
    gate_n: int,
    cost_bps: float = 15.0,
    value_key: str = "post_return_bps",
) -> dict:
    """Frozen-criteria verdict over one sample series.

    fail   : mean_net <= 0 OR win_net <= FAIL_WIN
    keep   : mean_net > 0 AND win_net >= KEEP_WIN AND both time halves'
             mean_net share the same (positive) direction
    gray   : everything else (extend once; second gray == fail)
    """

    if not samples:
        raise MilestoneJudgmentError("samples_empty")
    ordered = sorted(samples, key=lambda s: str(s.get("event_date", "")))
    net = [float(s[value_key]) - cost_bps for s in ordered]
    n = len(net)
    mean_net = statistics.fmean(net)
    win_net = sum(1 for v in net if v > 0) / n

    mid = n // 2
    first = _half_stats(net[:mid])
    second = _half_stats(net[mid:])
    f_mean = first["mean_net_bps"]
    s_mean = second["mean_net_bps"]
    halves_consistent = (
        f_mean is not None
        and s_mean is not None
        and (f_mean > 0) == (s_mean > 0)
        and f_mean != 0.0
    )

    if n < gate_n:
        verdict = "insufficient"
    elif mean_net <= 0 or win_net <= FAIL_WIN:
        verdict = "fail"
    elif mean_net > 0 and win_net >= KEEP_WIN and halves_consistent:
        verdict = "keep"
    else:
        verdict = "gray"

    return {
        "verdict": verdict,
        "gate_n": gate_n,
        "n": n,
        "mean_net_bps": round(mean_net, 1),
        "win_net": round(win_net, 3),
        "first_half": first,
        "second_half": second,
        "halves_consistent": halves_consistent,
        "cost_bps_roundtrip": cost_bps,
    }


def samples_for_preset(state: dict, preset: str) -> list[dict]:
    """Pull the sample list for a frozen preset out of a state export.

    All presets read ``labeled_outcomes`` (post-event gross bps — the
    series the frozen family criteria and the cost deduction are defined
    on).  ``prewindow_samples`` is the tracker's separate descriptive
    anticipation-window export (``pre_return_bps``); it is NOT judgment
    input.  Rehearsal note (#582): wiring the earnings presets to it
    produced rows without ``post_return_bps`` and an empty ``earnings_neg``
    arm (the tracker fills prewindow for the positive signal only).

    Every preset passes through :func:`dedupe_samples` — the judgment
    input is one row per unique event (#586).
    """

    labeled = state.get("labeled_outcomes") or {}
    if preset == "lockup_rule":
        rows = labeled.get(LOCKUP_SIGNAL) or []
        picked = [r for r in rows if rule_arm_sample(r)]
    elif preset == "lockup_rule_events":
        # Sample-unit decision (#588): economic-event granularity,
        # intersection semantics over the event's tagged records.
        rows = dedupe_samples(labeled.get(LOCKUP_SIGNAL) or [])
        picked = aggregate_rule_arm_events(rows)
    elif preset == EARNINGS_POS_SIGNAL:
        picked = list(labeled.get(EARNINGS_POS_SIGNAL) or [])
    elif preset == EARNINGS_NEG_SIGNAL:
        picked = list(labeled.get(EARNINGS_NEG_SIGNAL) or [])
    elif preset.startswith("raw:"):
        picked = list(labeled.get(preset.split(":", 1)[1]) or [])
    else:
        raise MilestoneJudgmentError(f"unknown_preset {preset}")
    if not picked:
        raise MilestoneJudgmentError(f"samples_empty {preset}")
    return dedupe_samples(picked)


def _preset_raw_keys(preset: str) -> list[str]:
    """Labeled-export keys a preset reads, for raw-vs-unique reporting."""

    if preset in ("lockup_rule", "lockup_rule_events"):
        return [LOCKUP_SIGNAL]
    if preset in (EARNINGS_POS_SIGNAL, EARNINGS_NEG_SIGNAL):
        return [preset]
    if preset.startswith("raw:"):
        return [preset.split(":", 1)[1]]
    return []


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument(
        "--preset",
        required=True,
        help=(
            "lockup_rule | lockup_rule_events | earnings_pos | "
            "earnings_neg | raw:<bucket_key>"
        ),
    )
    parser.add_argument("--gate-n", type=int, default=None)
    parser.add_argument("--cost-bps", type=float, default=15.0)
    parser.add_argument(
        "--profile",
        action="store_true",
        help="append descriptive month-bucket cells (non-gating aid)",
    )
    args = parser.parse_args()

    state = json.loads(args.state.read_text(encoding="utf-8"))
    samples = samples_for_preset(state, args.preset)
    gate_n = args.gate_n if args.gate_n is not None else (
        30 if args.preset.startswith("lockup_rule") else 50
    )
    result = judge(samples, gate_n=gate_n, cost_bps=args.cost_bps)

    n_raw = sum(
        len(state.get("labeled_outcomes", {}).get(key) or [])
        for key in _preset_raw_keys(args.preset)
    )
    print("## 里程碑判定（离线复算，冻结家族标准；research_only 非晋级证据）")
    print(f"- preset={args.preset} 门柱 n≥{gate_n} 成本 {args.cost_bps}bps 往返")
    if args.preset == "lockup_rule_events":
        n_records = len(dedupe_samples(
            state.get("labeled_outcomes", {}).get(LOCKUP_SIGNAL) or []
        ))
        print(
            f"- 样本 {n_raw} 行 → 披露记录 {n_records} 条 → "
            f"经济事件 {len(samples)} 个（#586/#588 口径）"
        )
    else:
        print(
            f"- 样本 {n_raw} 行 → 独立事件 {len(samples)}"
            f"（剔除跨运行重发现重复 {n_raw - len(samples)} 行；#586）"
        )
    print(
        f"- 判定 **{result['verdict'].upper()}**：n={result['n']} "
        f"净均值 {result['mean_net_bps']:+.1f}bps 净胜率 {result['win_net']:.3f}"
    )
    fh, sh = result["first_half"], result["second_half"]
    f_txt = "—" if fh["mean_net_bps"] is None else f"{fh['mean_net_bps']:+.1f}"
    s_txt = "—" if sh["mean_net_bps"] is None else f"{sh['mean_net_bps']:+.1f}"
    print(
        f"- 两半（按事件日期对分）：早段 n={fh['n']} {f_txt}bps / "
        f"近段 n={sh['n']} {s_txt}bps 一致={result['halves_consistent']}"
    )
    print(json.dumps(result, ensure_ascii=False))
    if args.profile:
        print("- 月度画像（描述性解读辅助，不参与判定；#583 矩阵）:")
        for cell in descriptive_profile(samples, cost_bps=args.cost_bps):
            print(
                f"  · {cell['month']} n={cell['n']:>3} "
                f"净均 {cell['mean_net_bps']:+7.1f}bps 胜率 {cell['win_net']:.3f}"
            )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except MilestoneJudgmentError as exc:
        print(f"MILESTONE_JUDGMENT_FAILED {exc}", file=sys.stderr)
        sys.exit(1)
