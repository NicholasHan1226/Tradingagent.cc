from __future__ import annotations

import json

from CNFutures.adapter import CNFuturesAdapter
from CNFutures.signal_engine import generate_style_signal


def _bars() -> list[dict[str, object]]:
    return [
        {"bar_time": "2026-07-30T09:00:00+08:00", "open": 3000.0, "high": 3002.0, "low": 2998.0, "close": 3000.0, "volume": 100},
        {"bar_time": "2026-07-30T09:05:00+08:00", "open": 3000.0, "high": 3005.0, "low": 2999.0, "close": 3004.0, "volume": 110},
        {"bar_time": "2026-07-30T09:10:00+08:00", "open": 3004.0, "high": 3009.0, "low": 3003.0, "close": 3008.0, "volume": 120},
        {"bar_time": "2026-07-30T09:15:00+08:00", "open": 3008.0, "high": 3014.0, "low": 3007.0, "close": 3013.0, "volume": 140},
        {"bar_time": "2026-07-30T09:20:00+08:00", "open": 3013.0, "high": 3020.0, "low": 3012.0, "close": 3019.0, "volume": 180},
        {"bar_time": "2026-07-30T09:25:00+08:00", "open": 3019.0, "high": 3027.0, "low": 3018.0, "close": 3026.0, "volume": 240},
        {"bar_time": "2026-07-30T09:30:00+08:00", "open": 3026.0, "high": 3035.0, "low": 3025.0, "close": 3034.0, "volume": 320},
    ]


def test_commodity_intraday_trend_is_one_lot_day_session_candidate() -> None:
    config = CNFuturesAdapter().get_strategy_config()
    assert config["universe_filter"]["products"] == ("m",)
    assert config["universe_filter"]["min_distinct_products"] == 1
    assert config["shadow_research"] == {
        "products": ("rb",),
        "mode": "read_only_evaluation",
        "execution_eligible": False,
        "simulated_fill_allowed": False,
    }
    assert set(config["styles"]) == {"commodity_intraday_trend"}
    style = config["styles"]["commodity_intraday_trend"]

    assert style["products"] == ["m"]
    assert style["risk_per_trade"] == 0.0025
    assert style["max_margin_usage"] == 0.1
    assert style["no_overnight"] is True
    assert style["day_session_only"] is True


def test_legacy_json_cannot_rejoin_runnable_strategies(tmp_path) -> None:
    (tmp_path / "breakout.json").write_text(
        json.dumps({"name": "breakout", "products": ["rb"]}), encoding="utf-8"
    )
    config = CNFuturesAdapter(strategy_dir=tmp_path).get_strategy_config()

    assert set(config["styles"]) == {"commodity_intraday_trend"}
    assert config["styles"]["commodity_intraday_trend"]["products"] == ("m",)


def test_unapproved_style_is_fail_closed_instead_of_a_generic_fallback() -> None:
    signal = generate_style_signal(
        "RB2610.SHF", _bars(), {"name": "breakout", "products": ("rb",)}
    )

    assert signal["action"] == "hold"
    assert signal["reason"] == "unsupported_strategy"


def test_rb_is_excluded_from_the_executable_universe() -> None:
    class Reader:
        def get_assets(self, *, market: str):
            assert market == "Futures"
            return [
                {"symbol": "M2609.DCE", "active": True},
                {"symbol": "RB2610.SHF", "active": True},
            ]

    adapter = CNFuturesAdapter(reader=Reader())

    assert adapter.get_universe("20260730") == ["M2609.DCE"]


def test_commodity_intraday_trend_returns_a_day_session_signal_with_its_own_family() -> None:
    style = {
        "name": "commodity_intraday_trend",
        "style_family": "commodity_intraday_trend",
        "signal_threshold": 0.0015,
        "momentum_lookback_bars": 3,
        "moving_average_bars": 6,
        "min_volume_ratio": 1.2,
        "open_cooldown_minutes": 20,
        "min_recent_range_pct": 0.0015,
        "min_directional_consistency": 0.6,
        "min_consecutive_aligned_bars": 2,
        "max_bar_gap_minutes": 7,
        "no_overnight": True,
        "day_session_only": True,
    }

    signal = generate_style_signal("M2609.DCE", _bars(), style)

    assert signal["action"] == "buy"
    assert signal["style_family"] == "commodity_intraday_trend"
    assert signal["scenario_tags"]["style_family"] == "commodity_intraday_trend"
    assert signal["no_overnight"] is True
