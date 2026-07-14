import pytest

from shared.screening.candidate_pool import _load_fundamental_pool
from shared.screening.condition_generator import _gen_rotation
from shared.screening.six_dimension_scorer import _score_capital, score_stock


class CapitalReader:
    def __init__(self, rows):
        self.rows = rows

    def get_factors(self, market, symbol):
        return []

    def get_capital_flow(self, ts_code, start, end):
        return list(self.rows)


def test_capital_score_uses_moneyflow_amount_not_volume() -> None:
    rows = [
        {"factor_name": "moneyflow:net_mf_amount", "event_time": "20260703", "value": -5733.26},
        {"factor_name": "moneyflow:net_mf_vol", "event_time": "20260703", "value": -56845},
    ]

    score = _score_capital(
        "000001.SZ",
        "20260707",
        {"_data_reader": CapitalReader(rows), "_market": "ashare", "dimensions": {"capital": {"window_days": 5}}},
    )

    assert score == pytest.approx(0.3426674)


def test_capital_score_prefers_capital_flow_rows_over_factor_duplicates() -> None:
    class DuplicateReader(CapitalReader):
        def get_factors(self, market, symbol):
            return [{"factor_name": "moneyflow:net_mf_amount", "event_time": "20260707", "value": 10000}]

    score = _score_capital(
        "000001.SZ",
        "20260707",
        {"_data_reader": DuplicateReader([{"factor_name": "moneyflow:net_mf_amount", "event_time": "20260707", "value": 10000}]), "_market": "ashare"},
    )

    assert score == pytest.approx(0.7)


def test_score_stock_exposes_moneyflow_alias_for_capital_dimension() -> None:
    score = score_stock(
        "ashare",
        "000001.SZ",
        reader=CapitalReader([{"factor_name": "moneyflow:net_mf_amount", "event_time": "20260707", "value": 10000}]),
        date="20260707",
    )

    assert score["moneyflow"] == pytest.approx(score["capital"])


def test_rotation_condition_accepts_prefixed_moneyflow_factor_names() -> None:
    class RotationReader:
        def get_asset(self, market, symbol):
            return {"sector": "bank"}

        def get_factors(self, market, symbol):
            return [
                {"factor_name": "moneyflow:net_mf_amount", "value": 200_000_000},
                {"factor_name": "sector_rotation", "value": 0.8},
            ]

    condition = _gen_rotation(
        "000001.SZ",
        {"combined": 0.8},
        "20260707",
        reader=RotationReader(),
        market="ashare",
    )

    assert condition is not None
    assert condition["type"] == "rotation"
    assert "板块净流入" not in condition["description"]
    assert "个股资金净流入" in condition["description"]
    assert condition["params"]["flow_scope"] == "individual_stock"
    assert condition["params"]["individual_net_inflow"] == pytest.approx(
        200_000_000
    )
    assert "net_inflow" not in condition["params"]


def test_fundamental_pool_accepts_prefixed_value_quality_factor_names() -> None:
    class FundamentalReader:
        def get_assets(self, market):
            return [{"symbol": "000001.SZ"}]

        def get_factors(self, market, symbol):
            return [
                {"factor_name": "stk_factor_pro:value", "value": 0.8},
                {"factor_name": "fina_indicator:quality", "value": 0.7},
            ]

    assert _load_fundamental_pool(reader=FundamentalReader(), market="ashare") == ["000001.SZ"]
