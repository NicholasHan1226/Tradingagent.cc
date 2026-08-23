"""Cross-sectional prescreen unit tests over deterministic synthetic panels.

The frozen pre-registered grid itself is asserted so any widening shows up as
a test failure rather than a silent research-scope change.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import gzip

import pytest

from Crypto.forty_symbol_cross_sectional_prescreen import (
    DISPERSION_GATE_LOOKBACK_MEDIAN,
    ENTRY_FEE,
    EXIT_FEE,
    HORIZONS,
    LOOKBACKS,
    SLIP,
    TOP_K,
    CrossSectionalPrescreenError,
    _forward_returns,
    _median,
    _mean,
    _portfolio_net,
    _rank_symbols,
    _slot_index,
    _slot_to_iso,
    _t_stat,
    _trailing_returns,
    analyze,
    build_panel,
    load_closes,
    pre_registered_candidates,
)


# ---------------------------------------------------------------------------
# Frozen grid
# ---------------------------------------------------------------------------


def test_pre_registered_grid_is_frozen() -> None:
    assert LOOKBACKS == (288, 576)
    assert TOP_K == (5, 10)
    assert HORIZONS == (48, 288)
    assert DISPERSION_GATE_LOOKBACK_MEDIAN == 1440
    assert ENTRY_FEE == EXIT_FEE == Decimal("0.001")
    assert SLIP == Decimal("0.0002")
    names = [c["name"] for c in pre_registered_candidates()]
    # 2 families x 2x2 grid + 4 gated variants of the l288 long-top family
    assert len(names) == 20
    assert len(set(names)) == 20


def test_slot_index_roundtrip() -> None:
    iso = "2026-02-18T04:45:00Z"
    slot = _slot_index(iso)
    assert slot == int(
        datetime(2026, 2, 18, 4, 45, tzinfo=timezone.utc).timestamp()
    ) // 300
    assert _slot_to_iso(slot) == iso


# ---------------------------------------------------------------------------
# Loading and panel
# ---------------------------------------------------------------------------


def _gzip_csv(tmp_path, rows):
    path = tmp_path / "closes.csv.gz"
    body = "".join(",".join(r) + "\n" for r in rows)
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        handle.write(body)
    return path


def test_load_closes_dedups_first_seen(tmp_path) -> None:
    rows = [
        ["AAAUSDT", "2026-02-18T04:45:00Z", "10"],
        ["AAAUSDT", "2026-02-18T04:45:00Z", "999"],  # duplicate: keep first
        ["AAAUSDT", "2026-02-18T04:50:00Z", "11"],
    ]
    closes = load_closes(_gzip_csv(tmp_path, rows))
    assert closes["AAAUSDT"] == {
        _slot_index("2026-02-18T04:45:00Z"): Decimal("10"),
        _slot_index("2026-02-18T04:50:00Z"): Decimal("11"),
    }


def test_load_closes_rejects_bad_shape_and_empty(tmp_path) -> None:
    with pytest.raises(CrossSectionalPrescreenError):
        load_closes(_gzip_csv(tmp_path, [["AAAUSDT", "2026-02-18T04:45:00Z"]]))
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(CrossSectionalPrescreenError):
        load_closes(_gzip_csv(empty, []))


def _synthetic_panel(symbols: int = 40, bars: int = 30) -> dict:
    """Half the symbols trend up, half stay flat, on a shared dense grid."""

    universe = [f"S{i:02d}USDT" for i in range(symbols)]
    grid = list(range(bars))
    closes = {}
    for i, symbol in enumerate(universe):
        if i % 2 == 0:
            closes[symbol] = [Decimal(100 + j + i) for j in range(bars)]
        else:
            closes[symbol] = [Decimal(100) for _ in range(bars)]
    return {"universe": universe, "grid": grid, "closes": closes}


def test_build_panel_requires_full_universe(tmp_path) -> None:
    base = _slot_index("2026-02-18T04:45:00Z")
    rows = []
    for s in range(39):  # one short of the required 40
        for j in range(60):
            rows.append([f"S{s:02d}USDT", _slot_to_iso(base + j), "10"])
    with pytest.raises(CrossSectionalPrescreenError):
        build_panel(load_closes(_gzip_csv(tmp_path, rows)))


def test_forward_and_trailing_abstain_across_gaps() -> None:
    panel = _synthetic_panel(bars=30)
    grid = panel["grid"]
    # Dense stretch: windows resolve.
    assert _forward_returns(panel["closes"], grid, 0, 4) is not None
    assert _trailing_returns(panel["closes"], grid, 10, 5) is not None
    # A hole makes every window crossing it abstain.
    holed = grid[:12] + grid[13:]
    assert _forward_returns(panel["closes"], holed, 10, 4) is None
    assert _trailing_returns(panel["closes"], holed, 13, 5) is None
    assert _trailing_returns(panel["closes"], grid, 4, 5) is None  # warm-up


# ---------------------------------------------------------------------------
# Ranking and costing
# ---------------------------------------------------------------------------


def test_rank_symbols_deterministic_tiebreak() -> None:
    returns = {"BBB": Decimal("0.01"), "AAA": Decimal("0.01"), "CCC": Decimal("0.02")}
    ranked = _rank_symbols(returns, descending=True)
    assert ranked[:1] == ["CCC"]
    # Equal-return ties resolve deterministically (reverse-sorts the names).
    assert ranked[1:] == sorted(["AAA", "BBB"], reverse=True)
    assert _rank_symbols(returns, descending=False)[:2] == ["AAA", "BBB"]


def test_portfolio_net_cost_model() -> None:
    forward = {"A": Decimal("0.05"), "B": Decimal("0.03")}
    # First entry: full round-trip cost on 100% of weight.
    first = _portfolio_net(["A"], forward, None)
    one_leg = (Decimal(1) - ENTRY_FEE) * (Decimal(1) - SLIP)
    expected_first = (Decimal("1.05") * one_leg**2) - 1
    assert first["net"] == expected_first
    assert first["replaced"] == 1
    # Full overlap: positions persist, no cost charged.
    kept = _portfolio_net(["A"], forward, ["A"])
    assert kept["net"] == Decimal("0.05")
    assert kept["replaced"] == 0
    # Half turnover pays half the round trip.
    half = _portfolio_net(["A", "B"], forward, ["A", "C"])
    assert half["replaced"] == Decimal("0.5")


def test_mean_median_t_stat() -> None:
    values = [Decimal("1"), Decimal("2"), Decimal("3"), Decimal("-6")]
    assert _mean(values) == Decimal("0")
    assert _median(values) == Decimal("1.5") or _median(values) == Decimal("-2")
    ordered = sorted(values)
    assert _median(values) == (ordered[1] + ordered[2]) / 2
    # Constant series has no defined t statistic.
    assert _t_stat([Decimal(1), Decimal(1)]) is None


# ---------------------------------------------------------------------------
# End-to-end candidate evaluation on a known panel
# ---------------------------------------------------------------------------


def _run_candidate(panel, family, lookback=4, k=1, horizon=2, gated=False):
    from Crypto.forty_symbol_cross_sectional_prescreen import _evaluate_candidate

    return _evaluate_candidate(
        name="test",
        family=family,
        panel=panel,
        lookback=lookback,
        k=k,
        horizon=horizon,
        gated=gated,
    )


def test_long_top_picks_the_trending_half() -> None:
    panel = _synthetic_panel()
    result = _run_candidate(panel, "long_top")
    assert result["invested_slots"] > 0
    assert result["abstain_slots"] >= 4  # trailing warm-up
    assert Decimal(result["mean_net"]) > 0
    assert Decimal(result["hit_rate"]) > Decimal("0.9")
    # Baseline (all-symbol equal weight) also earns the trend plus flat legs.
    assert Decimal(result["baseline_mean_net"]) > 0


def test_long_bottom_earns_less_than_long_top() -> None:
    panel = _synthetic_panel()
    top = _run_candidate(panel, "long_top")
    bottom = _run_candidate(panel, "long_bottom")
    assert Decimal(bottom["mean_net"]) < Decimal(top["mean_net"])
    assert Decimal(bottom["mean_net"]) < 0  # flat leg still pays entry costs


def test_gated_variant_never_diverges_before_gate_arms(monkeypatch) -> None:
    monkeypatch.setenv("REAL_TRADING_ENABLED", "false")
    panel = _synthetic_panel(bars=30)
    result = _run_candidate(panel, "long_top", gated=True)
    assert result["dispersion_gate_armed"] is False
    assert result["gated_flat_slots"] == 0


def test_invalid_family_fails_closed() -> None:
    with pytest.raises(CrossSectionalPrescreenError):
        _run_candidate(_synthetic_panel(), "market_neutral")


def test_analyze_seals_non_evidence_flags() -> None:
    monkeypatch_env = __import__("os").environ
    previous = monkeypatch_env.get("REAL_TRADING_ENABLED")
    monkeypatch_env["REAL_TRADING_ENABLED"] = "false"
    try:
        result = analyze(_synthetic_panel())
    finally:
        if previous is None:
            monkeypatch_env.pop("REAL_TRADING_ENABLED", None)
        else:
            monkeypatch_env["REAL_TRADING_ENABLED"] = previous
    assert result["not_promotion_evidence"] is True
    assert result["historical_backfill_no_pit"] is True
    assert result["pre_registered_grid"] is True
    assert result["execution_eligible"] is False
    assert result["data_source"] == "sqlite_readonly_diagnostic"
    assert len(result["candidates"]) == 20
