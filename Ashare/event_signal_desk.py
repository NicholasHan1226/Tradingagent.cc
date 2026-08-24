"""Render the A-share event "signal desk" research page (draft v3).

Research-only front-end draft (2026-08-24 redesign pass 3): results first,
analysis backend.  Layout is regime board -> signal queue table (the
protagonist) -> strategy archive cards -> scheduling calendar (dot-matrix)
-> detail drawers.  Every queue row answers "what can I do with this" with
an honest executability label:

* executable means the strategy itself is cleared AND an action point is
  live.  Lockup repair clears only in weak markets (#412); earnings_pos is
  in decay-watch (#413/#416), so its exit points are counted separately as
  ``n_exit`` and never inflate ``n_exec``;
* dividends are scheduling-layer only (no signal claim yet); index
  rebalancing has no stable data source and is not integrated.

Reads the same research cache as ``event_calendar_doc`` plus a calendar
document, and emits a self-contained HTML page.  No network access.

The ``--doc`` document has two interchangeable flavors: the committed
research document, or the whole-market TD-sourced one produced by
``event_calendar_tradingdatas.py`` (its ``earnings_disclosure`` entries
then drive the appointment schedule and ``disclosure_all.csv`` becomes
optional).  Direction grouping, float ratios and the index regime stay on
the Tushare research cache either way (hybrid wiring decision 2026-08-25).

Usage::

    python3 Ashare/event_signal_desk.py \
        [--cache /tmp/ashare_event_research] \
        [--doc Ashare/reports/calendar_doc.json] \
        [--out /tmp/event_signal_desk.html] [--today YYYY-MM-DD]

TD-flavored variant (whole-mainboard appointments)::

    python3 Ashare/event_calendar_tradingdatas.py \
        --token-file ... --out-dir /tmp/ashare_event_research_td
    python3 Ashare/event_signal_desk.py \
        --doc /tmp/ashare_event_research_td/calendar_doc.json ...
"""

from __future__ import annotations

import argparse
import calendar as pycal
import csv
import json
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from Ashare.event_signal_lockup_tracker import make_regime_lookup  # noqa: E402

DESK_ID = "ashare-event-signal-desk-v0"
DEFAULT_CACHE = Path("/tmp/ashare_event_research")
DEFAULT_DOC = _REPO_ROOT / "Ashare" / "reports" / "calendar_doc.json"
DEFAULT_OUT = Path("/tmp/event_signal_desk.html")

POS_TYPES = {"预增", "略增", "续盈", "预盈", "扭亏"}
NEG_TYPES = {"预减", "略减", "首亏", "续亏", "预亏"}
WEEKDAY = "一二三四五六日"

REGIME_CN = {"weak": "弱市", "sideways": "震荡市", "strong": "强市", "unknown": "未知"}
# lockup strategy is only cleared for deployment in weak markets (#412)
LK_FIT = {"weak": ("ok", "弱市 · 策略启用"),
          "sideways": ("warn", "震荡市 · 历史信号消失"),
          "strong": ("warn", "强市 · 历史仅右尾不可靠"),
          "unknown": ("off", "环境未知")}


class DeskError(RuntimeError):
    """Fail-closed desk build failure with a stable reason code."""


def _read_csv(cache: Path, name: str) -> list[dict[str, str]]:
    path = cache / name
    if not path.exists():
        raise DeskError(f"cache_missing:{name}")
    with path.open(encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def iso(day: date) -> str:
    return day.isoformat()


def compact(day: date) -> str:
    return day.strftime("%Y%m%d")


def report_label(end_date: str) -> str:
    tail = end_date[4:]
    year = end_date[:4]
    m = {"0331": "一季报", "0630": "中报", "0930": "三季报", "1231": "年报"}.get(tail)
    return f"{year}{m}" if m else end_date


def ratio_bucket(r: float | None) -> tuple[str, str]:
    """float_ratio -> (bucket key, 中文标签), per #409 strata."""
    if r is None:
        return ("na", "占比未知")
    if r < 0.01:
        return ("lt1", "<1% 无信号")
    if r < 0.03:
        return ("13", "1–3%")
    if r < 0.05:
        return ("35", "3–5% 回避带")
    return ("ge5", "≥5% 最强")


def load_desk_data(cache: Path, doc_path: Path, today: date) -> dict:
    """Assemble every render input from the research cache (no network)."""
    names: dict[str, tuple[str, str]] = {}
    for r in _read_csv(cache, "stock_basic_named.csv"):
        names[r["ts_code"]] = (r["name"], r.get("industry", ""))

    if not doc_path.exists():
        raise DeskError(f"doc_missing:{doc_path.name}")
    doc = json.loads(doc_path.read_text(encoding="utf-8"))

    # ---- lockups (future schedule from validated doc) + float ratio -------
    ratio_by_key: dict[tuple[str, str], float] = {}
    for r in _read_csv(cache, "share_float_expanded.csv"):
        try:
            ratio_by_key[(r["ts_code"], r["float_date"])] = max(
                ratio_by_key.get((r["ts_code"], r["float_date"]), 0.0),
                float(r["float_ratio"]),
            )
        except (TypeError, ValueError):
            pass

    lockups: dict[str, list[dict]] = defaultdict(list)
    lpr_dates: list[str] = []
    for e in doc["entries"]:
        d = e["scheduled_date"]
        if e["event_type"] == "macro_release":
            lpr_dates.append(d)
            continue
        sym = e.get("symbol", "")
        name, industry = names.get(sym, (sym, ""))
        lockups[d].append({
            "code": sym, "name": name, "ind": industry,
            "holder": e.get("entity", ""),
            "ratio": ratio_by_key.get((sym, d.replace("-", ""))),
        })

    # ---- dividends (scheduling layer only; no signal claim) ----------------
    # Coverage = announced dividends whose ex-date is known and in the
    # future; the 45-day announcement window is refetched by the probe.
    # The cache file is optional: without it the page simply omits the layer.
    dividends: dict[str, list[dict]] = defaultdict(list)
    div_src = cache / "dividend_recent.csv"
    if div_src.exists():
        for r in _read_csv(cache, "dividend_recent.csv"):
            ex = r.get("ex_date") or ""
            if len(ex) != 8:
                continue
            try:
                d = iso(datetime.strptime(ex, "%Y%m%d").date())
            except ValueError:
                continue
            sym = r["ts_code"]
            name, industry = names.get(sym, (sym, ""))
            try:
                cash = float(r["cash_div"]) if r.get("cash_div") not in (None, "") else None
            except (TypeError, ValueError):
                cash = None
            dividends[d].append({
                "code": sym, "name": name, "ind": industry,
                "cash": cash, "proc": r.get("div_proc", ""),
            })

    # ---- disclosures (whole-market appointment schedule) ------------------
    # Appointment dates have two interchangeable sources: a TD-sourced
    # calendar document (``event_calendar_tradingdatas.py`` emits
    # ``earnings_disclosure`` entries from cn.dataset.disclosure_date) is
    # authoritative when present; otherwise the per-sample Tushare cache
    # file disclosure_all.csv is required.  Direction grouping always
    # matches against forecast.csv.
    earliest_fc: dict[tuple[str, str], dict] = {}
    for r in _read_csv(cache, "forecast.csv"):
        key = (r["ts_code"], r["end_date"])
        ann = r.get("first_ann_date") or r["ann_date"]
        cur = earliest_fc.get(key)
        if cur is None or ann < (cur.get("first_ann_date") or cur["ann_date"]):
            earliest_fc[key] = r

    month_start = compact(date(today.year, today.month, 1))
    appts: list[tuple[str, str, str]] = []  # (symbol, end_date, pre compact)
    doc_disc = [e for e in doc["entries"]
                if e.get("event_type") == "earnings_disclosure"]
    if doc_disc:
        seen_appt: set[tuple[str, str, str]] = set()
        for e in doc_disc:
            sym = e.get("symbol") or ""
            end = e.get("entity") or ""
            try:
                pre = compact(datetime.strptime(
                    e["scheduled_date"], "%Y-%m-%d").date())
            except (KeyError, ValueError):
                continue
            if not sym or len(end) != 8 or pre < month_start:
                continue
            appt_key = (sym, end, pre)
            if appt_key not in seen_appt:
                seen_appt.add(appt_key)
                appts.append(appt_key)
    else:
        for r in _read_csv(cache, "disclosure_all.csv"):
            pre = r["pre_date"] or r["actual_date"]
            if not pre or pre < month_start:
                continue
            appts.append((r["ts_code"], r["end_date"], pre))

    disc: dict[str, list[dict]] = defaultdict(list)
    for sym, end, pre in appts:
        pre_d = datetime.strptime(pre, "%Y%m%d").date()
        d = iso(pre_d)
        name, industry = names.get(sym, (sym, ""))
        row = {"code": sym, "name": name, "ind": industry,
               "rep": report_label(end), "grp": "", "rng": ""}
        fc = earliest_fc.get((sym, end))
        if fc is not None:
            ann = fc.get("first_ann_date") or fc["ann_date"]
            if ann and ann <= pre:
                if fc["type"] in POS_TYPES:
                    row["grp"] = "pos"
                    lo, hi = fc["p_change_min"], fc["p_change_max"]
                    row["rng"] = f"{float(lo):+.0f}%~{float(hi):+.0f}%" if lo and hi else ""
                elif fc["type"] in NEG_TYPES:
                    row["grp"] = "neg"
                    lo, hi = fc["p_change_min"], fc["p_change_max"]
                    row["rng"] = f"{float(lo):+.0f}%~{float(hi):+.0f}%" if lo and hi else ""
        disc[d].append(row)

    # ---- regime ------------------------------------------------------------
    idx: list[tuple[date, float]] = []
    for r in _read_csv(cache, "index_000001SH.csv"):
        try:
            idx.append((datetime.strptime(r["trade_date"], "%Y%m%d").date(), float(r["close"])))
        except (KeyError, TypeError, ValueError):
            pass
    idx.sort()
    regime_of = make_regime_lookup(idx)
    reg_key = regime_of(today)
    reg = REGIME_CN.get(reg_key, "未知")
    closes = [c for d, c in idx if d <= today]
    ret10 = closes[-1] / closes[-11] - 1
    lk_fit = LK_FIT.get(reg_key, ("off", "环境未知"))

    # ---- signal queue (the protagonist table) ------------------------------
    # phase: exit=披露日执行退出 / window=窗口内 / repair=事后修复 / avoid=事前回避
    sigs: list[dict] = []
    today_iso = iso(today)

    for d in sorted(disc):
        for r in disc[d]:
            if not r["grp"]:
                continue
            # grp is only set when the forecast was public before the
            # appointment date, so no look-ahead enters the window rows.
            base = {"code": r["code"], "name": r["name"], "ind": r["ind"],
                    "rep": r["rep"], "rng": r["rng"], "d": d}
            if r["grp"] == "pos":
                if d == today_iso:
                    sigs.append({**base, "sig": "pos", "phase": "exit",
                                 "act": "今日披露 · 执行退出", "act_cls": "now"})
                elif d > today_iso:
                    sigs.append({**base, "sig": "pos", "phase": "window",
                                 "act": "持有至披露日前退出", "act_cls": "hold"})
            else:  # neg
                if d == today_iso or (today_iso >= d and d >= iso(today - timedelta(days=5))):
                    sigs.append({**base, "sig": "neg", "phase": "repair",
                                 "act": "事后修复观察 · 仅研究", "act_cls": "watch"})
                elif d > today_iso:
                    sigs.append({**base, "sig": "neg", "phase": "window",
                                 "act": "披露前观望 · 无事前背书", "act_cls": "wait"})

    # lockups: future 14d avoid window + past 5d repair window
    for d in sorted(set(lockups)):
        dd = datetime.strptime(d, "%Y-%m-%d").date()
        if today <= dd <= today + timedelta(days=14):
            for e in lockups[d]:
                bk, bk_cn = ratio_bucket(e["ratio"])
                sigs.append({"code": e["code"], "name": e["name"], "ind": e["ind"],
                             "sig": "lk", "phase": "avoid", "d": d,
                             "ratio": e["ratio"], "bucket": bk, "bucket_cn": bk_cn,
                             "act": "事前回避窗口", "act_cls": "avoid"})
    for r in _read_csv(cache, "share_float_expanded.csv"):
        fd = r.get("float_date", "")
        if not fd or len(fd) != 8:
            continue
        try:
            fdd = datetime.strptime(fd, "%Y%m%d").date()
        except ValueError:
            continue
        if iso(today - timedelta(days=5)) <= iso(fdd) < today_iso:
            sym = r["ts_code"]
            name, industry = names.get(sym, (sym, ""))
            try:
                ratio = float(r["float_ratio"])
            except (TypeError, ValueError):
                ratio = None
            bk, bk_cn = ratio_bucket(ratio)
            sigs.append({"code": sym, "name": name, "ind": industry,
                         "sig": "lk", "phase": "repair",
                         "d": iso(fdd), "ratio": ratio, "bucket": bk, "bucket_cn": bk_cn,
                         "act": "落地后修复观察 · 弱市才可买", "act_cls": "watch"})

    sigs.sort(key=lambda s: (s["d"], s["sig"], s["code"]))

    n_exit = sum(1 for s in sigs if s["act_cls"] == "now")
    # "executable" means: the strategy itself is cleared for deployment AND
    # an action point is live.  Lockup clears only in weak markets (#412);
    # earnings_pos is in decay-watch, so its exit points do not count.
    n_exec = sum(1 for s in sigs
                 if s["sig"] == "lk" and s["phase"] == "repair" and lk_fit[0] == "ok")
    n_hold = sum(1 for s in sigs if s["phase"] == "window" and s["sig"] == "pos")
    n_repair = sum(1 for s in sigs if s["phase"] == "repair")
    n_avoid = sum(1 for s in sigs if s["phase"] == "avoid")
    n_ge5 = sum(1 for s in sigs if s.get("bucket") == "ge5")

    # ---- calendar payload (v2, demoted to scheduling view) -----------------
    all_dates = sorted(set(lockups) | set(disc) | set(lpr_dates) | set(dividends))
    if not all_dates:
        raise DeskError("empty_calendar")
    last_m = max(all_dates)[:7]
    months: list[str] = []
    y, m = today.year, today.month
    while f"{y:04d}-{m:02d}" <= last_m:
        months.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1

    payload: dict[str, dict] = {}
    for d in all_dates:
        lk = lockups.get(d, [])
        rows = disc.get(d, [])
        pos = [r for r in rows if r["grp"] == "pos"]
        neg = [r for r in rows if r["grp"] == "neg"]
        non = [r for r in rows if not r["grp"]]
        if not any([lk, pos, neg, non, d in lpr_dates, dividends.get(d)]):
            continue
        payload[d] = {"lk": lk, "pos": pos, "neg": neg,
                      "nonN": len(non),
                      "non": [r["code"] + " " + r["name"] for r in non[:40]],
                      "lpr": d in lpr_dates,
                      "div": dividends.get(d, [])}

    return {"desk_id": DESK_ID, "today": today, "months": months, "payload": payload,
            "sigs": sigs, "reg": reg, "reg_key": reg_key, "ret10": ret10,
            "lk_fit": lk_fit, "n_exec": n_exec, "n_exit": n_exit, "n_hold": n_hold,
            "n_repair": n_repair, "n_avoid": n_avoid, "n_ge5": n_ge5}


def render_desk(data: dict) -> str:
    months = data["months"]
    payload = data["payload"]
    sigs = data["sigs"]
    reg = data["reg"]
    reg_key = data["reg_key"]
    ret10 = data["ret10"]
    lk_fit = data["lk_fit"]
    n_exec = data["n_exec"]
    n_exit = data["n_exit"]
    n_hold = data["n_hold"]
    n_repair = data["n_repair"]
    n_avoid = data["n_avoid"]
    n_ge5 = data["n_ge5"]
    today = data["today"]
    months_js = json.dumps(months, ensure_ascii=False)
    data_js = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    sigs_js = json.dumps(sigs, ensure_ascii=False, separators=(",", ":"))
    today_iso = iso(today)

    rail = "".join(
        f'<a class="ry" href="#m{y}">{y}</a>' for y in sorted({int(mn[:4]) for mn in months})
    )

    # calendar month sections (v2 renderer, verbatim logic)
    month_sections = []
    for mn in months:
        y, m = int(mn[:4]), int(mn[5:7])
        cnt = defaultdict(int)
        for daystr, ev in payload.items():
            if daystr.startswith(mn):
                cnt["lk"] += len(ev["lk"])
                cnt["pos"] += len(ev["pos"])
                cnt["neg"] += len(ev["neg"])
                cnt["non"] += ev["nonN"]
                cnt["div"] += len(ev.get("div", []))
                cnt["lpr"] += 1 if ev["lpr"] else 0
        mstat = f'{cnt["lk"]} 笔解禁 · 预增 {cnt["pos"]} · 预减 {cnt["neg"]} · 无预告 {cnt["non"]} · 分红 {cnt["div"]}' + (f' · LPR {cnt["lpr"]}' if cnt["lpr"] else "")
        first_wd, ndays = pycal.monthrange(y, m)
        cells = []
        for _ in range(first_wd):
            cells.append('<div class="cell ghost" aria-hidden="true"></div>')
        for day in range(1, ndays + 1):
            d = f"{y:04d}-{m:02d}-{day:02d}"
            wd = pycal.weekday(y, m, day)
            cls = ["cell"]
            if wd >= 5:
                cls.append("wknd")
            if d == today_iso:
                cls.append("today")
            elif d < today_iso:
                cls.append("past")
            ev = payload.get(d)
            inner = f'<span class="dn">{day}</span>'
            if ev:
                dots, chip = dots_and_chips(ev)
                inner += f'<span class="dots">{dots}</span>{chip}'
            cells.append(f'<button class="{" ".join(cls)}" data-d="{d}" aria-expanded="false">{inner}</button>')
        month_sections.append(
            f'<section class="month" id="m{y}" data-m="{mn}">'
            f'<header class="mh"><h2>{y} 年 {m} 月</h2><span class="ms">{mstat}</span></header>'
            f'<div class="dow">' + "".join(f'<span>周{WEEKDAY[i]}</span>' for i in range(7)) + '</div>'
            f'<div class="grid">{"".join(cells)}</div></section>'
        )

    sig_rows = []
    for s in sigs:
        if s["sig"] == "lk":
            num = (f'{s["ratio"]*100:.2f}%' if s.get("ratio") is not None else "—")
            numsub = s.get("bucket_cn", "")
        else:
            num = s.get("rng") or "—"
            numsub = "预告幅度"
        badge = ""
        if s["sig"] == "pos":
            badge = '<span class="badge decay">结构衰减观察</span>'
        elif s["sig"] == "lk":
            badge = f'<span class="fit {lk_fit[0]}">{lk_fit[1]}</span>'
        code = s["code"]
        sig_key = s["sig"]
        act_cls = s["act_cls"]
        phase = s["phase"]
        rep = s.get("rep") or s.get("bucket_cn", "")
        ind = s.get("ind") or "—"
        sig_rows.append(
            f'<tr class="srow" data-sig="{sig_key}" data-code="{code}" data-d="{s["d"]}">'
            f'<td class="c-sym"><span class="nm">{s["name"]}</span>'
            f'<span class="sub">{code} · {ind}</span></td>'
            f'<td class="c-sig"><span class="stag {sig_key}">{SIG_CN[sig_key]}</span>'
            f'<span class="sub">{rep}</span></td>'
            f'<td class="c-act"><span class="act {act_cls}">{s["act"]}</span>'
            f'<span class="sub">{PHASE_CN[phase]}</span></td>'
            f'<td class="c-win"><span class="wd">{fmt_d(s["d"])}</span>'
            f'<span class="sub">{PHASE_SUB[phase]}</span></td>'
            f'<td class="c-num"><span class="wd">{num}</span><span class="sub">{numsub}</span></td>'
            f'<td class="c-badge">{badge}</td></tr>'
        )

    tabs = (
        f'<button class="tab on" data-f="all">全部 <b>{len(sigs)}</b></button>'
        f'<button class="tab" data-f="pos">预增 <b>{sum(1 for s in sigs if s["sig"]=="pos")}</b></button>'
        f'<button class="tab" data-f="neg">预减 <b>{sum(1 for s in sigs if s["sig"]=="neg")}</b></button>'
        f'<button class="tab" data-f="lk">解禁 <b>{sum(1 for s in sigs if s["sig"]=="lk")}</b></button>'
    )

    return (HTML
            .replace("__MONTHS__", months_js).replace("__DATA__", data_js)
            .replace("__SIGS__", sigs_js)
            .replace("__RAIL__", rail)
            .replace("__SECTIONS__", "".join(month_sections))
            .replace("__ROWS__", "".join(sig_rows)).replace("__TABS__", tabs)
            .replace("__TODAY__", today_iso)
            .replace("__REG__", reg).replace("__REGKEY__", reg_key)
            .replace("__RET10__", f"{ret10 * 100:+.1f}")
            .replace("__LKFIT__", lk_fit[1])
            .replace("__N_EXEC__", str(n_exec)).replace("__N_EXIT__", str(n_exit))
            .replace("__N_HOLD__", str(n_hold))
            .replace("__N_REPAIR__", str(n_repair)).replace("__N_AVOID__", str(n_avoid))
            .replace("__N_GE5__", str(n_ge5))
            .replace("__N_QUEUE__", str(len(sigs))))


SIG_CN = {"pos": "预增", "neg": "预减", "lk": "解禁"}
PHASE_CN = {"exit": "披露日", "window": "窗口内", "repair": "修复期", "avoid": "事前窗口"}
PHASE_SUB = {"exit": "信号终点", "window": "信号生效中", "repair": "事后 5 日", "avoid": "未来 14 日"}


def fmt_d(d: str) -> str:
    return d[5:].replace("-", "/")


def dots_and_chips(ev: dict) -> tuple[str, str]:
    parts = [("u", len(ev["lk"])), ("p", len(ev["pos"])), ("n", len(ev["neg"])),
             ("d", len(ev.get("div", []))), ("m", 1 if ev["lpr"] else 0)]
    total = sum(n for _, n in parts)
    dots = []
    for cls, n in parts:
        dots.extend(f'<i class="{cls}"></i>' for _ in range(min(n, 12)))
    shown = "".join(dots[:12])
    more = f'<i class="more">+{total - 12}</i>' if total > 12 else ""
    chips = []
    if ev["pos"]:
        chips.append(f'<span class="chip p">预增 {len(ev["pos"])}</span>')
    if ev["neg"]:
        chips.append(f'<span class="chip n">预减 {len(ev["neg"])}</span>')
    if ev["lk"]:
        chips.append(f'<span class="chip u">解禁 {len(ev["lk"])}</span>')
    if ev.get("div"):
        chips.append(f'<span class="chip d">分红 {len(ev["div"])}</span>')
    if ev["lpr"]:
        chips.append('<span class="chip m">LPR</span>')
    non = f'<span class="non">无预告 {ev["nonN"]}</span>' if ev["nonN"] else ""
    return shown + more, "".join(chips) + non


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Render the A-share event signal desk page.")
    p.add_argument("--cache", type=Path, default=DEFAULT_CACHE,
                   help="research cache directory (default: %(default)s)")
    p.add_argument("--doc", type=Path, default=DEFAULT_DOC,
                   help="committed calendar document (default: %(default)s)")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT,
                   help="output HTML path (default: %(default)s)")
    p.add_argument("--today", default=None, help="ISO date override (default: actual today)")
    args = p.parse_args(argv)
    today = date.fromisoformat(args.today) if args.today else date.today()
    data = load_desk_data(args.cache, args.doc, today)
    html = render_desk(data)
    args.out.write_text(html, encoding="utf-8")
    print(f"written {args.out} ({args.out.stat().st_size} bytes), signals={len(data['sigs'])} "
          f"(exec={data['n_exec']} hold={data['n_hold']} repair={data['n_repair']} "
          f"avoid={data['n_avoid']})")
HTML = r'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>A股事件信号台</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Noto+Serif+SC:wght@600;900&family=Noto+Sans+SC:wght@400;500;700&family=IBM+Plex+Mono:wght@500;600&display=swap">
<style>
:root{
  --paper:#F6F4EF; --surface:#FFFFFF; --ink:#1D2126; --ink2:#5B6470; --ink3:#9AA0AA;
  --line:#E4E1D8; --line2:#D0CCC0;
  --accent:#2E559B; --accent-soft:#EBF0F8;
  --up:#C23A2B; --up-soft:#F9E9E6; --down:#1E7A4E; --down-soft:#E3F1E9;
  --gold:#8A6A1F; --gold-soft:#F5EDD6; --mac:#6B6478; --mac-soft:#ECEAF2;
  --div:#1F6E6E; --div-soft:#E2F0EE;
  --warn:#9A6B15; --warn-soft:#F7EED9;
  --wknd:#FBFAF6; --ghost:#EDEBE4;
  --serif:'Noto Serif SC','Songti SC',serif;
  --sans:'Noto Sans SC','PingFang SC',sans-serif;
  --mono:'IBM Plex Mono',ui-monospace,monospace;
}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){
  --paper:#131519; --surface:#1B1E24; --ink:#E7E5E0; --ink2:#9AA2AC; --ink3:#676E78;
  --line:#2A2E36; --line2:#3A3F48;
  --accent:#8FA9D8; --accent-soft:#232B3A;
  --up:#E06455; --up-soft:#3A2622; --down:#4CAF80; --down-soft:#1E3329;
  --gold:#C9A75C; --gold-soft:#332B18; --mac:#A29BB5; --mac-soft:#282633;
  --div:#5FB8AE; --div-soft:#1A312E;
  --warn:#D0A44C; --warn-soft:#332B18;
  --wknd:#17191E; --ghost:#20232A;
}}
:root[data-theme="dark"]{
  --paper:#131519; --surface:#1B1E24; --ink:#E7E5E0; --ink2:#9AA2AC; --ink3:#676E78;
  --line:#2A2E36; --line2:#3A3F48;
  --accent:#8FA9D8; --accent-soft:#232B3A;
  --up:#E06455; --up-soft:#3A2622; --down:#4CAF80; --down-soft:#1E3329;
  --gold:#C9A75C; --gold-soft:#332B18; --mac:#A29BB5; --mac-soft:#282633;
  --div:#5FB8AE; --div-soft:#1A312E;
  --warn:#D0A44C; --warn-soft:#332B18;
  --wknd:#17191E; --ghost:#20232A;
}
*{box-sizing:border-box;margin:0;padding:0}
html{scroll-behavior:smooth}
@media (prefers-reduced-motion: reduce){html{scroll-behavior:auto} *{transition:none!important;animation:none!important}}
body{background:var(--paper);color:var(--ink);font:14px/1.6 var(--sans);
  font-variant-numeric:tabular-nums;-webkit-font-smoothing:antialiased}
.wrap{max-width:1240px;margin:0 auto;padding:0 28px 90px}
button{font:inherit;color:inherit;background:none;border:none;cursor:pointer}
.sub{display:block;color:var(--ink3);font-size:11.5px;line-height:1.5}
.wd{font-family:var(--mono);font-weight:600}

/* ---------- top bar ---------- */
.top{position:sticky;top:0;z-index:40;display:flex;align-items:center;gap:16px;
  padding:14px 28px;background:color-mix(in srgb, var(--paper) 82%, transparent);
  backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);
  border-bottom:1px solid var(--line)}
.brand{font:900 19px/1 var(--serif);letter-spacing:.02em;white-space:nowrap}
.brand small{font:500 11px/1 var(--sans);color:var(--ink3);margin-left:8px;letter-spacing:.14em}
.gauge{margin-left:auto;display:flex;align-items:center;gap:10px;font-size:12.5px;color:var(--ink2)}
.gauge .lamp{width:9px;height:9px;border-radius:50%;background:var(--warn);
  box-shadow:0 0 0 3px var(--warn-soft)}
.gauge b{color:var(--ink);font-size:13px}
.gauge .num{font-family:var(--mono)}
.gauge .asof{color:var(--ink3)}
.tbtn{border:1px solid var(--line2);border-radius:6px;padding:5px 10px;font-size:12px;color:var(--ink2)}
.tbtn:hover{color:var(--ink);border-color:var(--ink3)}

/* ---------- regime board ---------- */
.board{display:grid;grid-template-columns:1.15fr 1fr 1fr 1fr;gap:14px;margin:34px 0 0}
.bcard{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:18px 20px}
.bcard.env{grid-row:span 1;border-left:3px solid var(--warn)}
.bcard h3{font:700 12px/1 var(--sans);letter-spacing:.18em;color:var(--ink3);margin-bottom:10px}
.envbig{font:900 30px/1.15 var(--serif)}
.envbig small{font:500 13px/1 var(--sans);color:var(--ink2);display:block;margin-top:6px}
.envnote{margin-top:10px;font-size:12.5px;color:var(--ink2);line-height:1.6}
.envnote b{color:var(--ink)}
.sline{display:flex;align-items:center;gap:10px;padding:9px 0;border-bottom:1px dashed var(--line);
  font-size:13px}
.sline:last-child{border-bottom:none}
.sline .dot{width:8px;height:8px;border-radius:50%;flex:none}
.sline .dot.warn{background:var(--warn)}
.sline .dot.off{background:var(--ink3)}
.sline .dot.ok{background:var(--down)}
.sline b{font-weight:700}
.sline .st{margin-left:auto;font-size:11.5px;color:var(--ink2);text-align:right}
.bignum{font:600 26px/1 var(--mono)}
.bignum small{font:400 12px var(--sans);color:var(--ink3)}
.bcard .cap{font-size:12px;color:var(--ink2);margin-top:8px;line-height:1.55}

/* ---------- section headers ---------- */
.shead2{display:flex;align-items:baseline;gap:14px;margin:52px 0 4px}
.shead2 h2{font:900 22px/1.3 var(--serif)}
.shead2 .lede{font-size:12.5px;color:var(--ink3)}

/* ---------- KPI strip ---------- */
.kpis{display:grid;grid-template-columns:repeat(5,1fr);margin:16px 0 0;
  background:var(--surface);border:1px solid var(--line);border-radius:10px;overflow:hidden}
.kpi{padding:16px 18px;border-right:1px solid var(--line)}
.kpi:last-child{border-right:none}
.kpi .v{font:600 24px/1.1 var(--mono)}
.kpi .v em{font-style:normal;font-size:13px;color:var(--ink3);font-family:var(--sans)}
.kpi .k{font-size:12px;color:var(--ink2);margin-top:5px}
.kpi.hot .v{color:var(--accent)}

/* ---------- signal queue ---------- */
.tabs{display:flex;gap:8px;margin:16px 0 0;flex-wrap:wrap}
.tab{border:1px solid var(--line2);border-radius:999px;padding:6px 14px;font-size:12.5px;color:var(--ink2)}
.tab b{font-family:var(--mono);font-weight:600;margin-left:2px}
.tab.on{background:var(--ink);border-color:var(--ink);color:var(--paper)}
.qwrap{background:var(--surface);border:1px solid var(--line);border-radius:10px;
  margin-top:12px;overflow:hidden}
.qscroll{overflow-x:auto}
table.q{width:100%;border-collapse:collapse;min-width:920px}
table.q th{background:var(--surface);
  font:700 11px/1 var(--sans);letter-spacing:.14em;color:var(--ink3);text-align:left;
  padding:12px 14px;border-bottom:1px solid var(--line2);white-space:nowrap}
table.q td{padding:11px 14px;border-bottom:1px solid var(--line);vertical-align:top}
tr.srow{cursor:pointer}
tr.srow:hover{background:var(--accent-soft)}
tr.srow.hidden{display:none}
.c-sym{min-width:170px}
.c-sym .nm{font-weight:700;font-size:13.5px}
.c-sig{min-width:90px}
.stag{display:inline-block;font:700 11.5px/1 var(--sans);border-radius:4px;padding:4px 7px}
.stag.pos{background:var(--gold-soft);color:var(--gold)}
.stag.neg{background:var(--down-soft);color:var(--down)}
.stag.lk{background:var(--accent-soft);color:var(--accent)}
.act{display:inline-block;font:700 12.5px/1.3 var(--sans);border-radius:5px;padding:5px 9px}
.act.now{background:var(--up);color:#fff}
.act.hold{background:var(--accent-soft);color:var(--accent)}
.act.watch{background:var(--ghost);color:var(--ink2)}
.act.wait{background:var(--ghost);color:var(--ink3)}
.act.avoid{background:var(--warn-soft);color:var(--warn)}
.c-win,.c-num{white-space:nowrap}
.badge{display:inline-block;font-size:11px;border:1px solid var(--warn);color:var(--warn);
  border-radius:4px;padding:2.5px 6px;white-space:nowrap}
.fit{display:inline-block;font-size:11px;border-radius:4px;padding:2.5px 6px;white-space:nowrap}
.fit.warn{background:var(--warn-soft);color:var(--warn)}
.fit.ok{background:var(--down-soft);color:var(--down)}
.fit.off{background:var(--ghost);color:var(--ink3)}
.qfoot{padding:10px 14px;font-size:11.5px;color:var(--ink3);border-top:1px solid var(--line)}

/* ---------- strategy archive ---------- */
.arch{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:16px}
.acard{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:20px 22px}
.acard header{display:flex;align-items:center;gap:10px;margin-bottom:8px}
.acard h3{font:900 17px/1.3 var(--serif)}
.acard .tag{margin-left:auto;font-size:11px;border-radius:4px;padding:3px 7px;white-space:nowrap}
.tag.grey{background:var(--warn-soft);color:var(--warn)}
.tag.obs{background:var(--ghost);color:var(--ink2)}
.tag.cond{background:var(--accent-soft);color:var(--accent)}
.tag.base{background:var(--down-soft);color:var(--down)}
.acard .thesis{font-size:13px;line-height:1.7;color:var(--ink)}
.acard .thesis b{font-weight:700}
.acard .nums{display:flex;gap:0;margin:12px 0 10px;border-top:1px solid var(--line);
  border-bottom:1px solid var(--line)}
.acard .nums div{flex:1;padding:9px 4px 9px 0;border-right:1px solid var(--line);margin-right:12px}
.acard .nums div:last-child{border-right:none;margin-right:0}
.acard .nums .v{font:600 17px/1.2 var(--mono)}
.acard .nums .k{font-size:11px;color:var(--ink3);margin-top:2px}
.acard .note{font-size:12px;color:var(--ink2);line-height:1.65}

/* ---------- calendar (scheduling view, v2) ---------- */
.rail{position:sticky;top:110px;float:left;margin-left:-72px;display:flex;flex-direction:column;gap:6px}
.rail a{font:500 12px/1 var(--mono);color:var(--ink3);text-decoration:none;padding:2px 6px;
  border-left:2px solid transparent}
.rail a.on{color:var(--ink);border-left-color:var(--accent);font-weight:600}
.month{clear:none;margin-top:46px}
.mh{position:sticky;top:57px;z-index:20;display:flex;align-items:baseline;gap:14px;
  padding:10px 2px;background:color-mix(in srgb, var(--paper) 85%, transparent);
  backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);
  border-bottom:1px solid var(--line)}
.mh h2{font:900 19px/1.3 var(--serif)}
.mh .ms{font-size:11.5px;color:var(--ink3)}
.dow{display:grid;grid-template-columns:repeat(7,1fr);gap:6px;margin-top:8px}
.dow span{font:700 10.5px/1 var(--sans);letter-spacing:.12em;color:var(--ink3);text-align:right;padding:4px 6px}
.grid{display:grid;grid-template-columns:repeat(7,1fr);gap:6px;margin-top:2px}
.cell{position:relative;text-align:left;min-height:106px;border:1px solid var(--line);
  border-radius:8px;background:var(--surface);padding:7px 8px;display:flex;flex-direction:column;gap:5px;
  overflow:hidden}
.cell:hover{border-color:var(--ink3)}
.cell.ghost{background:transparent;border-color:transparent}
.cell.wknd{background:var(--wknd)}
.cell.past{opacity:.55}
.cell.past .chip,.cell.past .dots{opacity:.75}
.cell.today{border:1.5px solid var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
.dn{font:600 12px/1 var(--mono);color:var(--ink2)}
.cell.today .dn{color:var(--accent);font-weight:600}
.cell.today .dn::after{content:" · 今";font:700 10px var(--sans)}
.dots{display:flex;flex-wrap:wrap;gap:3px;min-height:6px}
.dots i{width:6px;height:6px;border-radius:50%;background:var(--ink3)}
.dots i.u{background:var(--accent)}
.dots i.p{background:var(--gold)}
.dots i.n{background:var(--down)}
.dots i.m{background:var(--mac)}
.dots i.d{background:var(--div)}
.dots i.more{background:transparent;color:var(--ink3);font:600 9px/1 var(--mono);width:auto;height:auto}
.chip{align-self:flex-start;font:700 10.5px/1 var(--sans);border-radius:4px;padding:3.5px 6px;white-space:nowrap}
.chip.p{background:var(--gold-soft);color:var(--gold)}
.chip.n{background:var(--down-soft);color:var(--down)}
.chip.u{background:var(--accent-soft);color:var(--accent)}
.chip.m{background:var(--mac-soft);color:var(--mac)}
.chip.d{background:var(--div-soft);color:var(--div)}
.non{font-size:10.5px;color:var(--ink3)}

/* ---------- drawer ---------- */
#scrim{position:fixed;inset:0;background:rgba(10,12,16,.45);opacity:0;pointer-events:none;transition:opacity .25s;z-index:90}
body.open #scrim{opacity:1;pointer-events:auto}
#drawer{position:fixed;top:0;right:0;bottom:0;width:min(520px,94vw);background:var(--surface);
  border-left:1px solid var(--line);z-index:100;transform:translateX(102%);transition:transform .28s ease;
  display:flex;flex-direction:column}
body.open #drawer{transform:none}
.dhead{display:flex;align-items:center;gap:12px;padding:18px 22px;border-bottom:1px solid var(--line)}
.dhead h2{font:900 20px/1.2 var(--serif)}
#dcnt{font-size:12px;color:var(--ink2);font-family:var(--mono)}
#dclose{margin-left:auto;border:1px solid var(--line2);border-radius:6px;width:30px;height:30px;
  font-size:15px;color:var(--ink2)}
#dclose:hover{color:var(--ink)}
#dconcl{padding:14px 22px;border-bottom:1px solid var(--line);background:var(--accent-soft);
  font-size:12.5px;line-height:1.7;color:var(--ink)}
#dconcl div+div{margin-top:6px}
#dbody{overflow-y:auto;padding:6px 22px 30px}
.sechead{font:700 12px/1 var(--sans);letter-spacing:.14em;color:var(--ink3);margin:20px 0 8px}
#dbody table{width:100%;border-collapse:collapse}
#dbody th{font:700 10.5px/1 var(--sans);letter-spacing:.12em;color:var(--ink3);text-align:left;
  padding:7px 8px;border-bottom:1px solid var(--line2)}
#dbody td{padding:8px;border-bottom:1px solid var(--line);font-size:12.5px;vertical-align:top}
#dbody td.num,#dbody th.num{text-align:right;font-family:var(--mono)}
.code{font-family:var(--mono);font-size:11.5px;color:var(--ink2)}
.nm{font-weight:600}
.ind{display:block;font-size:11px;color:var(--ink3)}
.grp{display:inline-block;font:700 10.5px/1 var(--sans);border-radius:4px;padding:3px 6px}
.grp.p{background:var(--gold-soft);color:var(--gold)}
.grp.n{background:var(--down-soft);color:var(--down)}
.grp.u{background:var(--accent-soft);color:var(--accent)}
.grp.x{background:var(--ghost);color:var(--ink3)}
.grp.m{background:var(--mac-soft);color:var(--mac)}
.grp.d{background:var(--div-soft);color:var(--div)}

/* ---------- legend / footer ---------- */
.legend{display:flex;flex-wrap:wrap;gap:16px;margin-top:18px;font-size:11.5px;color:var(--ink2)}
.legend i{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:5px}
.legend i.u{background:var(--accent)}.legend i.p{background:var(--gold)}
.legend i.n{background:var(--down)}.legend i.m{background:var(--mac)}
footer{margin-top:60px;padding-top:16px;border-top:1px solid var(--line);
  font-size:11.5px;color:var(--ink3);line-height:1.8}

@media (max-width:1000px){
  .board{grid-template-columns:1fr 1fr}
  .kpis{grid-template-columns:repeat(2,1fr)}
  .kpi{border-bottom:1px solid var(--line)}
  .arch{grid-template-columns:1fr}
  .rail{display:none}
}
</style>
</head>
<body>
<header class="top">
  <span class="brand">A股事件信号台<small>仅供研究</small></span>
  <span class="gauge"><span class="lamp"></span>市场环境 <b>__REG__</b> · 上证10日 <span class="num">__RET10__%</span><span class="asof">· 数据截至 __TODAY__</span></span>
  <button class="tbtn" id="themeBtn">深浅</button>
</header>

<div class="wrap">

  <section class="board">
    <div class="bcard env">
      <h3>当前环境判定</h3>
      <div class="envbig">__REG__<small>上证指数近10个交易日 __RET10__%</small></div>
      <div class="envnote">事件策略按环境分层启用：<b>解禁策略仅在弱市启用</b>（弱市净 +92bps / 胜率 55%，唯一成本后稳健分层）；当前环境<b>不满足启用条件</b>，信号队列仅供观察。</div>
    </div>
    <div class="bcard">
      <h3>策略状态</h3>
      <div class="sline"><span class="dot warn"></span><b>解禁</b><span class="st">__LKFIT__</span></div>
      <div class="sline"><span class="dot warn"></span><b>预增组</b><span class="st">结构衰减观察中</span></div>
      <div class="sline"><span class="dot off"></span><b>预减组</b><span class="st">仅观察 · 不入组合</span></div>
    </div>
    <div class="bcard">
      <h3>可执行信号</h3>
      <div class="bignum">__N_EXEC__<small> 个</small></div>
      <div class="cap">可执行 = 策略本身获启用 且 操作点生效。解禁策略仅弱市启用（当前__REG__）；预增组处于结构衰减观察期，今日 __N_EXIT__ 个规则退出点仅供参考，不计入可执行。</div>
    </div>
    <div class="bcard">
      <h3>观察队列</h3>
      <div class="bignum">__N_QUEUE__<small> 只</small></div>
      <div class="cap">窗口内 __N_HOLD__ · 修复期 __N_REPAIR__ · 事前回避 __N_AVOID__（≥5% 大额 __N_GE5__）· 今日退出点 __N_EXIT__</div>
    </div>
  </section>

  <div class="shead2" id="queue"><h2>信号队列</h2><span class="lede">按窗口截止日排序 · 点击任意行查看研究依据</span></div>
  <div class="tabs" id="tabs">__TABS__</div>
  <div class="qwrap"><div class="qscroll">
    <table class="q">
      <thead><tr><th>标的</th><th>信号</th><th>建议动作</th><th>关键日</th><th>关键数字</th><th>状态</th></tr></thead>
      <tbody>__ROWS__</tbody>
    </table>
  </div>
  <div class="qfoot">动作语义：执行（红）= 达到研究标准的操作点 · 持有（蓝）= 窗口内按规则持有 · 回避（黄）= 事前风险窗口 · 观察（灰）= 仅研究跟踪，不构成操作。历史统计均为超额收益口径，成本后仅弱市分层存活。</div>
  </div>

  <div class="shead2" id="archive"><h2>策略档案</h2><span class="lede">后端研究结论 · 每条均可回溯至预注册评估标准</span></div>
  <div class="arch">
    <div class="acard">
      <header><h3>解禁 · 躲预期，买事实</h3><span class="tag cond">弱市专用</span></header>
      <div class="thesis">解禁前 10 日超额 <b>−108bps</b>（t=−6.4），落地后修复至 <b>+27bps</b>（t=2.2）。弱市分层成本后仍净 <b>+92bps</b> / 胜率 55%，是唯一穿越交易成本的分层；震荡市信号消失，强市仅剩右尾不可靠。</div>
      <div class="nums">
        <div><div class="v">−108</div><div class="k">事前10日 bps</div></div>
        <div><div class="v">+92</div><div class="k">弱市净 bps</div></div>
        <div><div class="v">55%</div><div class="k">弱市净胜率</div></div>
        <div><div class="v">n=777</div><div class="k">分层样本</div></div>
      </div>
      <div class="note">规则：弱市入场；回避解禁占比 3–5% 带（一致偏弱 −21bps）；≥5% 最强但靠右尾；&lt;1% 无信号。当前环境：__LKFIT__。</div>
    </div>
    <div class="acard">
      <header><h3>预增组 · 买预期，卖事实</h3><span class="tag grey">结构衰减观察</span></header>
      <div class="thesis">历史教科书级结构：事前 10 日 <b>+150bps</b>（t=7.4），披露日 <b>−47bps</b>（t=−7.6）。但分年检验 2018–2022 全正、<b>2023–2026 转负</b>；预告后入场的窗口读数 n=20 为 <b>−365bps</b>——按预注册标准正走向 GREY/FAIL。</div>
      <div class="nums">
        <div><div class="v">+150</div><div class="k">历史事前 bps</div></div>
        <div><div class="v">−47</div><div class="k">披露日 bps</div></div>
        <div><div class="v">−365</div><div class="k">近期窗口读数</div></div>
        <div><div class="v">n=1535</div><div class="k">扩样检验</div></div>
      </div>
      <div class="note">队列中预增信号按规则展示窗口与退出点，但状态徽章提示结构衰减——样本积累至里程碑后由晋级通道自动判定。</div>
    </div>
    <div class="acard">
      <header><h3>预减组 · 利空出尽</h3><span class="tag obs">仅观察</span></header>
      <div class="thesis">披露后 5 日修复 <b>+85bps</b>（t=5.1）。但该口径为超额收益；并入组合后回撤从 −10.6% 恶化至 −23.3%，弱市条件不迁移（n=129 转负）——<b>跨族合并不自动产生分散</b>。</div>
      <div class="nums">
        <div><div class="v">+85</div><div class="k">事后5日 bps</div></div>
        <div><div class="v">t=5.1</div><div class="k">显著性</div></div>
        <div><div class="v">−23.3%</div><div class="k">并组合后回撤</div></div>
        <div><div class="v">n=129</div><div class="k">弱市子集转负</div></div>
      </div>
      <div class="note">修复窗口信号仅作研究跟踪；不作为组合部署条件。</div>
    </div>
    <div class="acard">
      <header><h3>组合基线 · 规则臂</h3><span class="tag base">参照基准</span></header>
      <div class="thesis">信号流过真实组合引擎（槽位制、15bps 往返成本、事件日收盘进 +5 日出）：规则臂（弱市×非3–5%带）总净 <b>+64.2%</b>、回撤 <b>−10.6%</b>、胜率 <b>58.4%</b>，同期上证 +16.6%。月均净 +0.5~0.7% 是 30%/月目标的首个真实刻度。</div>
      <div class="nums">
        <div><div class="v">+64.2%</div><div class="k">总净收益</div></div>
        <div><div class="v">−10.6%</div><div class="k">最大回撤</div></div>
        <div><div class="v">58.4%</div><div class="k">笔胜率</div></div>
        <div><div class="v">1035</div><div class="k">全宇宙信号</div></div>
      </div>
      <div class="note">2018-01 → 2026-08 全样本回放；只读缓存、不写账本。仓位规则锁定同一口径后结论稳健。</div>
    </div>
  </div>

  <div class="shead2" id="cal"><h2>事件排期</h2><span class="lede">全市场披露与解禁日历 · 点日期格看当日名单</span></div>
  <nav class="rail" aria-label="年份">__RAIL__</nav>
  __SECTIONS__

  <div class="legend">
    <span><i class="u"></i>解禁</span><span><i class="p"></i>财报 · 预增组</span>
    <span><i class="n"></i>财报 · 预减组</span><span><i class="d"></i>分红除息（排期）</span><span><i class="m"></i>LPR / 宏观</span>
    <span>格子色条 = 当日强度 · 点日期格看完整名单</span>
  </div>

  <footer>
    研究口径：全部统计为相对上证指数的超额收益，信号层未计交易成本，成本后仅弱市分层存活（15–40bps 各档稳健）。预注册评估标准与晋级通道自动判定升降级；本页为研究展示，不构成投资建议。数据源：Tushare（披露排期/解禁/预告/分红/指数），截至 __TODAY__。分红排期覆盖近 45 天内已公告且除息日确定的记录，随公告滚动补全；未来报告期的交易所排期提前约一个月公布，9 月后排期待更新；指数成分调整暂无稳定数据源，未接入。
  </footer>
</div>

<div id="scrim"></div>
<aside id="drawer" role="dialog" aria-modal="true" aria-label="详情">
  <div class="dhead"><h2 id="dtitle"></h2><span id="dcnt"></span><button id="dclose" aria-label="关闭">×</button></div>
  <div id="dconcl"></div>
  <div id="dbody"></div>
</aside>

<script>
const DATA = __DATA__;
const SIGS = __SIGS__;
const MONTHS = __MONTHS__;
const TODAY = "__TODAY__";
const REGKEY = "__REGKEY__";
const CONCL = {
  pos: "<div><b>预增组（买预期、卖事实）</b>事前10日 +150bps (t=7.4) · 披露日 −47bps (t=−7.6)。注意：2023–2026 分年转负、近期窗口读数 −365bps，结构衰减观察中。</div>",
  neg: "<div><b>预减组（利空出尽）</b>事后5日 +85bps (t=5.1) 修复 · 超额口径，组合层并回回撤恶化，仅观察不入组合。</div>",
  lk: "<div><b>解禁（躲预期、买事实）</b>事前10日 −108bps · 落地后修复 +27bps · 弱市分层成本后唯一稳健（净 +92bps/胜率 55%）· 回避 3–5% 带 · 当前环境：__LKFIT__。</div>",
  non: "<div><b>无预告披露</b>历史无可交易结构，仅作排期参考</div>",
  div: "<div><b>分红除息</b>排期层事件：除权除息日按公告确定，登记日在除息前一交易日。分红事件未做信号验证，仅作排期参考。</div>",
  lpr: "<div><b>LPR</b>指数层面历史不可交易，仅作宏观日历标注</div>"
};
const $ = s => document.querySelector(s);
const esc = s => String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

function row(code, name, ind, mid, num, grp, grpTxt){
  return `<tr><td><span class="code">${esc(code)}</span></td>`+
    `<td><span class="nm">${esc(name||"—")}</span><span class="ind">${esc(ind||"")}</span><div>${mid||""}</div></td>`+
    `<td class="num">${num?esc(num):"—"}</td>`+
    `<td><span class="grp ${grp}">${grpTxt}</span></td></tr>`;
}
function tbl(head, rows){ return `<table><thead><tr>${head}</tr></thead><tbody>${rows.join("")}</tbody></table>`; }

function openDay(d){
  const ev = DATA[d];
  if(!ev) return;
  const dt = new Date(d+"T00:00:00");
  const wd = "日一二三四五六"[dt.getDay()];
  $("#dtitle").textContent = d.slice(5).replace("-","/") + " 周" + wd;
  const parts = [];
  if(ev.pos.length) parts.push("预增 "+ev.pos.length);
  if(ev.neg.length) parts.push("预减 "+ev.neg.length);
  if(ev.lk.length) parts.push("解禁 "+ev.lk.length);
  if(ev.nonN) parts.push("无预告 "+ev.nonN);
  if(ev.div && ev.div.length) parts.push("分红 "+ev.div.length);
  if(ev.lpr) parts.push("LPR");
  $("#dcnt").textContent = parts.join(" · ");
  const concls = [];
  if(ev.pos.length) concls.push(CONCL.pos);
  if(ev.neg.length) concls.push(CONCL.neg);
  if(ev.lk.length) concls.push(CONCL.lk);
  if(ev.nonN) concls.push(CONCL.non);
  if(ev.div && ev.div.length) concls.push(CONCL.div);
  if(ev.lpr) concls.push(CONCL.lpr);
  $("#dconcl").innerHTML = concls.join("");
  let html = "";
  const thead = '<th>代码</th><th>公司</th><th class="num">幅度 / 占比</th><th>方向</th>';
  if(ev.pos.length){
    html += `<div class="sechead">预增组 ${ev.pos.length}</div>` + tbl(thead, ev.pos.map(r =>
      row(r.code, r.name, r.ind, esc(r.rep), r.rng, "p", "预增")));
  }
  if(ev.neg.length){
    html += `<div class="sechead">预减组 ${ev.neg.length}</div>` + tbl(thead, ev.neg.map(r =>
      row(r.code, r.name, r.ind, esc(r.rep), r.rng, "n", "预减")));
  }
  if(ev.lk.length){
    html += `<div class="sechead">解禁 ${ev.lk.length}</div>` + tbl('<th>代码</th><th>公司 · 解禁方</th><th class="num">占比</th><th>类型</th>',
      ev.lk.map(r => row(r.code, r.name, r.ind, esc(r.holder||""), r.ratio!=null ? (r.ratio*100).toFixed(2)+"%" : "", "u", "解禁")));
  }
  if(ev.nonN){
    html += `<div class="sechead">无预告披露 ${ev.nonN} 家（仅排期）</div>` + tbl('<th>代码</th><th>公司</th><th class="num"></th><th></th>',
      ev.non.map(s => { const [c, ...rest] = s.split(" "); return `<tr><td><span class="code">${esc(c)}</span></td><td><span class="nm">${esc(rest.join(" "))}</span></td><td class="num"></td><td></td></tr>`; })) +
      (ev.nonN > ev.non.length ? `<div class="sechead" style="font-weight:400;color:var(--ink3)">其余 ${ev.nonN - ev.non.length} 家从略</div>` : "");
  }
  if(ev.div && ev.div.length){
    html += `<div class="sechead">分红除息 ${ev.div.length}</div>` + tbl('<th>代码</th><th>公司</th><th class="num">每股派息</th><th>进度</th>',
      ev.div.map(r => row(r.code, r.name, r.ind, "", r.cash!=null ? r.cash.toFixed(3)+" 元" : "—", "d", esc(r.proc||"分红"))));
  }
  if(ev.lpr) html += `<div class="sechead">宏观</div>` + tbl('<th></th><th></th><th class="num"></th><th></th>',
    `<tr><td><span class="code">PBOC</span></td><td><span class="nm">LPR 报价发布</span></td><td class="num"></td><td><span class="grp m">宏观</span></td></tr>`);
  $("#dbody").innerHTML = html;
  $("#dbody").scrollTop = 0;
  document.body.classList.add("open");
  $("#dclose").focus();
}

function openSig(code, d){
  const s = SIGS.find(x => x.code === code && x.d === d);
  if(!s) return;
  const dt = new Date(s.d+"T00:00:00");
  const wd = "日一二三四五六"[dt.getDay()];
  $("#dtitle").textContent = s.name + " · " + s.d.slice(5).replace("-","/") + " 周" + wd;
  $("#dcnt").textContent = s.code + (s.rep ? " · " + s.rep : "");
  const key = s.sig;
  $("#dconcl").innerHTML = (CONCL[key]||"");
  const num = s.sig === "lk"
    ? (s.ratio!=null ? (s.ratio*100).toFixed(2)+"%" : "—")
    : (s.rng || "—");
  const numK = s.sig === "lk" ? "解禁占比" : "预告幅度";
  let rows = [
    row(s.code, s.name, s.ind, s.rep ? esc(s.rep) : "", num, key, key==="pos"?"预增":key==="neg"?"预减":"解禁")
  ];
  if(s.sig === "lk" && s.bucket) rows.push(`<tr><td></td><td><span class="nm">分层标签</span><span class="ind">${esc(s.bucket_cn||"")}</span></td><td class="num"></td><td></td></tr>`);
  $("#dbody").innerHTML =
    `<div class="sechead">信号</div>` +
    tbl('<th>代码</th><th>公司</th><th class="num">'+numK+'</th><th>方向</th>', rows) +
    `<div class="sechead">建议动作</div><div style="font-size:13.5px;line-height:1.8">${esc(s.act)}<br><span style="color:var(--ink3);font-size:12px">关键日 ${s.d} · ${esc(PHASE_CN[s.phase]||"")}</span></div>`;
  $("#dbody").scrollTop = 0;
  document.body.classList.add("open");
  $("#dclose").focus();
}
function closeDay(){ document.body.classList.remove("open"); }

/* tabs */
const PHASE_CN = {exit:"披露日", window:"窗口内", repair:"修复期", avoid:"事前窗口"};
document.getElementById("tabs").addEventListener("click", e => {
  const b = e.target.closest(".tab");
  if(!b) return;
  document.querySelectorAll(".tab").forEach(t => t.classList.toggle("on", t === b));
  const f = b.dataset.f;
  document.querySelectorAll("tr.srow").forEach(tr =>
    tr.classList.toggle("hidden", f !== "all" && tr.dataset.sig !== f));
});

document.addEventListener("click", e => {
  const tr = e.target.closest("tr.srow");
  if(tr){ openSig(tr.dataset.code, tr.dataset.d); return; }
  const cell = e.target.closest(".cell[data-d]");
  if(cell){ openDay(cell.dataset.d); return; }
  if(e.target.id === "scrim" || e.target.id === "dclose") closeDay();
});
document.addEventListener("keydown", e => { if(e.key === "Escape") closeDay(); });

/* theme */
const root = document.documentElement;
try{ const saved = localStorage.getItem("cal-theme"); if(saved) root.dataset.theme = saved; }catch(e){}
$("#themeBtn").addEventListener("click", () => {
  const dark = root.dataset.theme === "dark" || (!root.dataset.theme && matchMedia("(prefers-color-scheme: dark)").matches);
  root.dataset.theme = dark ? "light" : "dark";
  try{ localStorage.setItem("cal-theme", root.dataset.theme); }catch(e){}
});

/* year rail scrollspy */
const links = [...document.querySelectorAll(".rail a")];
const obs = new IntersectionObserver(es => {
  es.forEach(en => {
    if(en.isIntersecting){
      links.forEach(a => a.classList.toggle("on", a.getAttribute("href") === "#" + en.target.id));
    }
  });
}, {rootMargin: "-20% 0px -70% 0px"});
document.querySelectorAll(".month").forEach(m => obs.observe(m));
</script>
</body>
</html>'''

if __name__ == "__main__":
    main()
