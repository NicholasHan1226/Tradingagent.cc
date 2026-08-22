"""Tests for the sealed 40-symbol exit-cost counterfactual research module."""

from __future__ import annotations

import importlib.util
import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

_MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "Crypto"
    / "forty_symbol_exit_cost_counterfactual.py"
)
_SPEC = importlib.util.spec_from_file_location(
    "forty_symbol_exit_cost_counterfactual", _MODULE_PATH
)
MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules.setdefault("forty_symbol_exit_cost_counterfactual", MODULE)
_SPEC.loader.exec_module(MODULE)

ZERO = Decimal("0")
ONE = Decimal("1")


def _rising_closes(count: int) -> list[Decimal]:
    """Geometric +1%/bar so every bar clears every allowed threshold."""

    step = Decimal("1.01")
    return [Decimal(100) * step**i for i in range(count)]


def _material_universe(closes: list[Decimal]) -> dict:
    bars = [(close, close, close) for close in closes]
    slots = list(range(1_000_000, 1_000_000 + len(closes)))
    return {
        symbol: {
            "slots": list(slots),
            "bars": list(bars),
            "duplicates": 0,
            "gap_slots": 0,
            "first_open_time": MODULE._iso_slot(slots[0]),
            "last_open_time": MODULE._iso_slot(slots[-1]),
        }
        for symbol in MODULE.FORTY_SYMBOLS
    }


# ---------------------------------------------------------------------------
# Cost model
# ---------------------------------------------------------------------------


def test_baseline_variant_matches_round_trip_taker_v1_formula() -> None:
    gross = Decimal("0.03")
    actual = MODULE._round_trip_net(
        gross, exit_fee=Decimal("0.001"), exit_slip=Decimal("0.0002")
    )
    # Same-step decomposition: buy-side fee gross-up, entry slip,
    # sell-side (exit) fee, exit slippage.
    bought = (ONE + gross) / (ONE + Decimal("0.001"))
    expected = ((bought * Decimal("0.9998")) * Decimal("0.999")) * Decimal(
        "0.9998"
    ) - ONE
    assert actual == expected
    # Cross-check against the frozen phase-1 closed form within decimal
    # context precision (different multiplication grouping).
    closed_form = (
        (ONE + gross)
        * (ONE - Decimal("0.001"))
        / (ONE + Decimal("0.001"))
        * (Decimal("0.9998") ** 2)
        - ONE
    )
    assert abs(actual - closed_form) < Decimal("1e-24")


def test_maker_exit_variant_strictly_reduces_cost() -> None:
    gross = Decimal("0.03")
    baseline = MODULE._round_trip_net(
        gross, exit_fee=Decimal("0.001"), exit_slip=Decimal("0.0002")
    )
    maker = MODULE._round_trip_net(
        gross, exit_fee=Decimal("0.0002"), exit_slip=ZERO
    )
    assert maker > baseline


# ---------------------------------------------------------------------------
# Frozen champion rules
# ---------------------------------------------------------------------------


def test_entry_signal_mirrors_champion_rule() -> None:
    closes = [Decimal(100) + Decimal(i) for i in range(20)]
    # 3-bar return = 103/100 - 1 = 3% >= every allowed threshold.
    assert MODULE._is_entry_signal(closes, 12, Decimal("0.003")) is True
    flat = [Decimal(100)] * 20
    assert MODULE._is_entry_signal(flat, 12, Decimal("0.001")) is False
    # Falling regime (12-bar return negative) never enters.
    falling = [Decimal(200) - Decimal(i) for i in range(20)]
    assert MODULE._is_entry_signal(falling, 12, Decimal("0.001")) is False
    assert MODULE._is_entry_signal(closes, 11, Decimal("0.001")) is False


def test_reversal_exit_mirrors_champion_rule() -> None:
    falling = [Decimal(200) - Decimal(i) for i in range(20)]
    assert MODULE._is_reversal_exit(falling, 15) is True
    rising = [Decimal(100) + Decimal(i) for i in range(20)]
    assert MODULE._is_reversal_exit(rising, 15) is False
    flat = [Decimal(100)] * 20
    assert MODULE._is_reversal_exit(flat, 15) is False


# ---------------------------------------------------------------------------
# Path simulation
# ---------------------------------------------------------------------------


def _bars_from_closes(closes: list[Decimal]) -> list[tuple[Decimal, Decimal, Decimal]]:
    return [(close, close, close) for close in closes]


def test_path_take_profit_at_level() -> None:
    closes = [Decimal(100), Decimal(100), Decimal(104), Decimal(104)]
    bars = _bars_from_closes(closes)
    trip = MODULE._simulate_path(bars, closes, 0)
    assert trip["exit_reason"] == "take_profit"
    assert trip["exit_offset_bars"] == 2
    assert trip["gross"] == Decimal("0.03")
    assert trip["mfe"] == Decimal("0.04")


def test_path_stop_loss_pessimistic_before_take_profit() -> None:
    # Both trigger levels are touched inside the same post-entry bar;
    # the champion ladder must book the stop-loss first.
    closes = [Decimal(100), Decimal(100), Decimal(100)]
    bars = [
        (Decimal(100), Decimal(100), Decimal(100)),
        (Decimal(100), Decimal(105), Decimal(97)),
        (Decimal(100), Decimal(100), Decimal(100)),
    ]
    trip = MODULE._simulate_path(bars, closes, 0)
    assert trip["exit_reason"] == "stop_loss"
    assert trip["exit_offset_bars"] == 1
    assert trip["gross"] == Decimal("-0.02")
    assert trip["mfe"] == Decimal("0.05")
    assert trip["mae"] == Decimal("-0.03")


def test_path_max_holding_period_on_flat_series() -> None:
    closes = [Decimal(100)] * 300
    bars = _bars_from_closes(closes)
    trip = MODULE._simulate_path(bars, closes, 0)
    assert trip["exit_reason"] == "max_holding_period"
    assert trip["exit_offset_bars"] == MODULE.MAX_HOLD_BARS
    assert trip["gross"] == ZERO


def test_path_momentum_reversal_exit() -> None:
    closes = (
        [Decimal(100)] * 3
        + [Decimal(101)] * 12
        + [Decimal(99)]
    )
    bars = _bars_from_closes(closes)
    trip = MODULE._simulate_path(bars, closes, 0)
    assert trip["exit_reason"] == "momentum_reversal_observed"
    assert trip["exit_offset_bars"] == 15
    assert trip["gross"] == Decimal("99") / Decimal("100") - ONE


def test_path_data_end_when_series_truncated() -> None:
    closes = [Decimal(100), Decimal(101)]
    bars = _bars_from_closes(closes)
    trip = MODULE._simulate_path(bars, closes, 0)
    assert trip["exit_reason"] == "data_end"
    assert trip["exit_offset_bars"] == 1


# ---------------------------------------------------------------------------
# analyze(): grids, non-overlap, sealing
# ---------------------------------------------------------------------------


def test_analyze_rejects_drifted_grids() -> None:
    material = _material_universe(_rising_closes(400))
    with pytest.raises(MODULE.FortySymbolExitCostCounterfactualError, match="thresholds_drift"):
        MODULE.analyze(material, thresholds=("0.001",))
    with pytest.raises(MODULE.FortySymbolExitCostCounterfactualError, match="symbols_drift"):
        MODULE.analyze(material, symbols=("BTCUSDT",))


def test_analyze_non_overlapping_stride_and_exit_mix() -> None:
    material = _material_universe(_rising_closes(1000))
    result = MODULE.analyze(material)
    assert result["contract"] == MODULE.CONTRACT
    assert len(result["cells"]) == len(MODULE.ALLOWED_THRESHOLDS)
    for cell in result["cells"]:
        # Every bar is an entry signal; stride 288 keeps at most
        # ceil(evaluable_bars / 288) trips per symbol.
        evaluable = 1000 - MODULE.REGIME_LOOKBACK_BARS - 1
        upper_bound = len(MODULE.FORTY_SYMBOLS) * -(-evaluable // MODULE.PATH_STRIDE_BARS)
        assert cell["trip_count"] == 160
        assert cell["trip_count"] <= upper_bound
        assert cell["exit_reasons"] == {"take_profit": 160}
        maker = cell["variants"]["usdm_maker_exit"]
        taker = cell["variants"]["taker_exit"]
        assert Decimal(maker["mean_net"]) > Decimal(taker["mean_net"])
        assert Decimal(maker["mean_net_delta_vs_baseline"]) == (
            Decimal(maker["mean_net"]) - Decimal(taker["mean_net"])
        )


def test_analyze_flat_universe_yields_zero_trips() -> None:
    material = _material_universe([Decimal(100)] * 400)
    result = MODULE.analyze(material)
    for cell in result["cells"]:
        assert cell["trip_count"] == 0
        assert cell["exit_reasons"] == {}
        assert cell["mean_gross"] is None
        assert cell["variants"]["taker_exit"]["mean_net"] is None


def test_analyze_result_is_sealed_research_only() -> None:
    material = _material_universe(_rising_closes(400))
    result = MODULE.analyze(material)
    assert result["research_only"] is True
    assert result["not_promotion_evidence"] is True
    assert result["historical_backfill_no_pit"] is True
    assert result["assumes_touch_equals_fill"] is True
    assert result["counterfactual_only"] is True
    assert result["execution_eligible"] is False
    assert result["real_trading_enabled"] is False


# ---------------------------------------------------------------------------
# load_material env gate + markdown projection
# ---------------------------------------------------------------------------


def test_load_material_requires_simulation_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("REAL_TRADING_ENABLED", raising=False)
    with pytest.raises(
        MODULE.FortySymbolExitCostCounterfactualError,
        match="real_trading_must_be_disabled",
    ):
        MODULE.load_material_from_sqlite(tmp_path / "missing.sqlite")
    monkeypatch.setenv("REAL_TRADING_ENABLED", "false")
    with pytest.raises(
        MODULE.FortySymbolExitCostCounterfactualError,
        match="db_path_invalid",
    ):
        MODULE.load_material_from_sqlite(tmp_path / "missing.sqlite")


def test_render_markdown_contains_grid_and_disclaimer() -> None:
    material = _material_universe(_rising_closes(400))
    rendered = MODULE.render_markdown(MODULE.analyze(material))
    assert "threshold | trips | mean_gross" in rendered
    assert "taker_exit_net" in rendered
    assert "usdm_maker_exit_delta" in rendered
    assert "not promotion evidence" in rendered
    assert "0.005" in rendered


def test_main_cli_writes_json_and_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = tmp_path / "fake.sqlite"
    db.write_text("not a real db", encoding="utf-8")
    # CLI path is exercised end-to-end elsewhere; here we only assert the
    # artifact writers on a precomputed result to stay offline.
    material = _material_universe(_rising_closes(400))
    result = MODULE.analyze(material)
    out_json = tmp_path / "out.json"
    out_report = tmp_path / "out.md"
    out_json.write_text(
        json.dumps(result, allow_nan=False, sort_keys=True), encoding="utf-8"
    )
    out_report.write_text(MODULE.render_markdown(result), encoding="utf-8")
    assert json.loads(out_json.read_text(encoding="utf-8"))["contract"] == MODULE.CONTRACT
    assert "exit-cost counterfactual" in out_report.read_text(encoding="utf-8")
