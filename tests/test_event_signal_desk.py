"""Contract tests for the event signal desk page generator.

The desk is a research-only rendering layer over the event-calendar study.
These tests pin the honesty contracts that make the page trustworthy:

* executability: ``n_exec`` counts a lockup repair point only when the
  market regime clears the strategy (weak market, #412) — in any other
  regime identical signals must yield zero, with exits reported separately;
* scheduling vs signal: dividend and no-forecast rows render as schedule
  context only, and the page keeps its research disclaimer;
* fail-closed assembly: missing cache/doc inputs raise ``DeskError`` with
  stable reason codes instead of silently rendering an empty page.

Fixtures are tiny synthetic caches; no network access anywhere.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from Ashare.event_signal_desk import (
    DeskError,
    load_desk_data,
    ratio_bucket,
    render_desk,
)

TODAY = date(2026, 8, 24)

INDEX_SESSIONS = [
    ("20260805", 3000.0), ("20260806", 3000.0), ("20260807", 3000.0),
    ("20260810", 3000.0), ("20260811", 3000.0), ("20260812", 3000.0),
    ("20260813", 3000.0), ("20260814", 3000.0), ("20260817", 3000.0),
    ("20260818", 3000.0), ("20260819", 3000.0), ("20260820", 3000.0),
    ("20260821", 3000.0),
]


def _write_csv(cache: Path, name: str, header: str, rows: list[str]) -> None:
    (cache / name).write_text(header + "\n" + "\n".join(rows) + "\n", encoding="utf-8")


def build_cache(tmp_path: Path, *, weak: bool = False, dividends: bool = False) -> tuple[Path, Path]:
    cache = tmp_path / "cache"
    cache.mkdir()
    sessions = list(INDEX_SESSIONS)
    if weak:
        # last session −3% vs session[-11] → regime "weak"
        sessions[-1] = ("20260821", 2910.0)
    _write_csv(cache, "index_000001SH.csv", "trade_date,close",
               [f"{d},{c}" for d, c in sessions])
    _write_csv(cache, "stock_basic_named.csv", "ts_code,name,industry",
               ["600000.SH,浦发银行,银行",
                "000721.SZ,西安饮食,餐饮"])
    _write_csv(cache, "share_float_expanded.csv", "ts_code,float_date,float_ratio",
               ["600000.SH,20260901,0.062",   # future: inside 14d avoid window
                "000721.SZ,20260821,0.041"])  # past: inside 5d repair window
    _write_csv(cache, "forecast.csv",
               "ts_code,end_date,type,p_change_min,p_change_max,ann_date,first_ann_date",
               ["600000.SH,20260630,预增,50,60,20260710,20260710"])
    _write_csv(cache, "disclosure_all.csv", "ts_code,end_date,pre_date,actual_date",
               ["600000.SH,20260630,20260826,",  # future appointment, forecast public
                "000721.SZ,20260630,20260822,"])  # no forecast group -> schedule only
    if dividends:
        _write_csv(cache, "dividend_recent.csv", "ts_code,ex_date,cash_div,div_proc",
                   ["600000.SH,20260827,0.53,实施分红"])
    doc = {"calendar_id": "test-desk-doc", "entries": [
        {"event_id": "t1", "event_type": "lockup_expiry",
         "scheduled_date": "2026-09-01", "symbol": "600000.SH",
         "entity": "控股股东", "date_confidence": "hard_date"},
        {"event_id": "lpr", "event_type": "macro_release",
         "scheduled_date": "2026-09-20", "date_confidence": "expected_window"},
    ]}
    doc_path = tmp_path / "calendar_doc.json"
    doc_path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return cache, doc_path


def test_ratio_bucket_boundaries_match_preregistered_strata() -> None:
    assert ratio_bucket(None) == ("na", "占比未知")
    assert ratio_bucket(0.005)[0] == "lt1"
    assert "无信号" in ratio_bucket(0.005)[1]
    assert ratio_bucket(0.02)[0] == "13"
    assert ratio_bucket(0.04)[0] == "35"
    assert "回避带" in ratio_bucket(0.04)[1]
    assert ratio_bucket(0.062)[0] == "ge5"
    assert "最强" in ratio_bucket(0.062)[1]


def test_sideways_regime_hides_executable_despite_live_repair_points(tmp_path) -> None:
    cache, doc_path = build_cache(tmp_path)
    data = load_desk_data(cache, doc_path, TODAY)
    assert data["reg_key"] == "sideways"
    phases = {(s["sig"], s["phase"]) for s in data["sigs"]}
    # all three action families are present...
    assert ("pos", "window") in phases          # hold-until-disclosure row
    assert ("lk", "avoid") in phases            # pre-event avoidance window
    assert ("lk", "repair") in phases           # post-event repair observation
    # ...but nothing is executable outside a weak market (#412)
    assert data["n_exec"] == 0
    assert data["n_avoid"] >= 1 and data["n_repair"] >= 1


def test_weak_regime_is_the_only_unlock_for_n_exec(tmp_path) -> None:
    cache, doc_path = build_cache(tmp_path, weak=True)
    data = load_desk_data(cache, doc_path, TODAY)
    assert data["reg_key"] == "weak"
    repairs = [s for s in data["sigs"]
               if s["sig"] == "lk" and s["phase"] == "repair"]
    assert len(repairs) >= 1
    assert data["n_exec"] == len(repairs)


def test_pos_exit_counts_as_exit_not_executable(tmp_path) -> None:
    cache, doc_path = build_cache(tmp_path, weak=True)
    # appointment lands exactly on today -> pos exit point
    disc = cache / "disclosure_all.csv"
    disc.write_text(
        "ts_code,end_date,pre_date,actual_date\n"
        "600000.SH,20260630,20260824,\n"
        "000721.SZ,20260630,20260822,\n",
        encoding="utf-8")
    data = load_desk_data(cache, doc_path, TODAY)
    pos_exits = [s for s in data["sigs"]
                 if s["sig"] == "pos" and s["phase"] == "exit"]
    assert len(pos_exits) == 1 and pos_exits[0]["act_cls"] == "now"
    # decay-watch (#413/#416): earnings_pos exit points never inflate n_exec,
    # they are reported separately as n_exit
    assert data["n_exit"] == 1


def test_dividends_are_schedule_only_and_optional(tmp_path) -> None:
    cache, doc_path = build_cache(tmp_path, dividends=True)
    data = load_desk_data(cache, doc_path, TODAY)
    div_rows = data["payload"]["2026-08-27"]["div"]
    assert len(div_rows) == 1
    assert div_rows[0]["cash"] == 0.53
    html = render_desk(data)
    assert "分红" in html                      # rendered as schedule chip
    assert "不构成投资建议" in html             # research disclaimer stays

    # the layer is optional: without the probe file the page still builds
    # and the dividend-only day simply drops out of the schedule
    (cache / "dividend_recent.csv").unlink()
    data2 = load_desk_data(cache, doc_path, TODAY)
    assert "2026-08-27" not in data2["payload"]


def test_render_replaces_every_placeholder_and_keeps_honest_labels(tmp_path) -> None:
    cache, doc_path = build_cache(tmp_path, dividends=True)
    data = load_desk_data(cache, doc_path, TODAY)
    html = render_desk(data)
    for token in ("__MONTHS__", "__DATA__", "__SIGS__", "__RAIL__",
                  "__SECTIONS__", "__ROWS__", "__TABS__", "__TODAY__",
                  "__REG__", "__REGKEY__", "__RET10__", "__LKFIT__",
                  "__N_EXEC__", "__N_EXIT__", "__N_HOLD__", "__N_REPAIR__",
                  "__N_AVOID__", "__N_GE5__", "__N_QUEUE__"):
        assert token not in html, f"placeholder left behind: {token}"
    assert "仅排期" in html                     # no-forecast rows are schedule-only
    assert html.count('class="srow"') == len(data["sigs"])


def test_fail_closed_on_missing_inputs(tmp_path) -> None:
    cache, doc_path = build_cache(tmp_path)

    # missing calendar document fails closed before any rendering
    try:
        load_desk_data(cache, tmp_path / "absent_doc.json", TODAY)
    except DeskError as exc:
        assert "doc_missing" in str(exc)
    else:
        raise AssertionError("expected DeskError for missing calendar document")

    # missing cache file fails closed with the stable reason code
    (cache / "stock_basic_named.csv").unlink()
    try:
        load_desk_data(cache, doc_path, TODAY)
    except DeskError as exc:
        assert "cache_missing:stock_basic_named.csv" in str(exc)
    else:
        raise AssertionError("expected DeskError for missing cache file")
