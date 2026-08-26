"""Offline milestone-judgment evaluator (frozen family criteria).

The criteria document fixes keep/fail/gray at milestone sample counts;
the first lockup judgment (#571) was computed with an ad-hoc throwaway
script.  Judgments recur every week as samples accumulate (lockup second
judgment toward ~200 weak samples, earnings_pos/neg first judgments off
the Aug 31 export), so this module replaces the throwaway pattern with
one tested evaluator over the tracker's per-sample state export (#570):

- ``lockup_rule``   : labeled_outcomes[lockup_expiry] filtered to the
  practice rule arm (regime == ``weak`` AND ratio tagged outside the
  avoided ``3-5%`` band) — identical predicate to the tracker's
  ``rule_subset_breakdown``.
- ``earnings_*``    : prewindow_samples[signal] as exported.
- ``raw``           : labeled_outcomes[key] unfiltered.

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
    """Pull the sample list for a frozen preset out of a state export."""

    labeled = state.get("labeled_outcomes") or {}
    prewindow = state.get("prewindow_samples") or {}
    if preset == "lockup_rule":
        rows = labeled.get(LOCKUP_SIGNAL) or []
        picked = [r for r in rows if rule_arm_sample(r)]
    elif preset == EARNINGS_POS_SIGNAL:
        picked = list(prewindow.get(EARNINGS_POS_SIGNAL) or [])
    elif preset == EARNINGS_NEG_SIGNAL:
        picked = list(prewindow.get(EARNINGS_NEG_SIGNAL) or [])
    elif preset.startswith("raw:"):
        picked = list(labeled.get(preset.split(":", 1)[1]) or [])
    else:
        raise MilestoneJudgmentError(f"unknown_preset {preset}")
    if not picked:
        raise MilestoneJudgmentError(f"samples_empty {preset}")
    return picked


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument(
        "--preset",
        required=True,
        help="lockup_rule | earnings_pos | earnings_neg | raw:<bucket_key>",
    )
    parser.add_argument("--gate-n", type=int, default=None)
    parser.add_argument("--cost-bps", type=float, default=15.0)
    args = parser.parse_args()

    state = json.loads(args.state.read_text(encoding="utf-8"))
    samples = samples_for_preset(state, args.preset)
    gate_n = args.gate_n if args.gate_n is not None else (
        30 if args.preset == "lockup_rule" else 50
    )
    result = judge(samples, gate_n=gate_n, cost_bps=args.cost_bps)

    print("## 里程碑判定（离线复算，冻结家族标准；research_only 非晋级证据）")
    print(f"- preset={args.preset} 门柱 n≥{gate_n} 成本 {args.cost_bps}bps 往返")
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
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except MilestoneJudgmentError as exc:
        print(f"MILESTONE_JUDGMENT_FAILED {exc}", file=sys.stderr)
        sys.exit(1)
