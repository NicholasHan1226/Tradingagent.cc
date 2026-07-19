from __future__ import annotations

import hashlib
import json
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from Ashare.adapter import AshareAdapter
from Ashare.sim_executor import ashare_sim_execute
from shared.screening.candidate_pool import build_pool
from shared.screening.universe_filter import filter_universe
from shared.universe.policy import (
    InstrumentRole,
    classify_instrument,
    is_mainboard_tradable,
)


def test_canonical_mainboard_scope_policy_is_immutable_and_content_addressed() -> None:
    from shared.universe import policy as policy_module

    policy = policy_module.CanonicalMainboardScopePolicy()
    manifest = dict(policy.manifest)
    expected_sha256 = hashlib.sha256(
        json.dumps(
            manifest,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()

    assert type(policy) is policy_module.CanonicalMainboardScopePolicy
    assert policy.policy_sha256 == expected_sha256
    assert policy.identity.artifact_sha256 == expected_sha256
    assert policy.identity.stage is None
    assert policy.order_identity_allowed("000001.SZ") is True
    assert policy.order_identity_allowed("300001.SZ") is False
    assert policy.order_identity_allowed("688001.SH") is False
    assert (
        policy.context_identity_allowed(
            "399006.SZ",
            instrument_type="index",
        )
        is True
    )
    assert (
        policy.context_identity_allowed(
            "000688.SH",
            instrument_type="index",
        )
        is True
    )
    assert (
        policy.context_identity_allowed(
            "sector:sw801080",
            instrument_type="sector_aggregate",
        )
        is True
    )
    with pytest.raises(TypeError):
        policy.manifest["policy_id"] = "forged"
    with pytest.raises(AttributeError):
        policy.policy_sha256 = "f" * 64


MAINBOARD_SYMBOLS = (
    "000001.SZ",
    "001286.SZ",
    "002415.SZ",
    "003816.SZ",
    "600000.SH",
    "601318.SH",
    "603259.SH",
    "605499.SH",
)
NON_TRADABLE_GROWTH_SYMBOLS = (
    "300750.SZ",
    "301269.SZ",
    "688981.SH",
    "689009.SH",
)


@pytest.mark.parametrize("symbol", MAINBOARD_SYMBOLS)
def test_mainboard_common_stocks_are_tradable(symbol: str) -> None:
    eligibility = classify_instrument(symbol)

    assert eligibility.policy_id == "tradingagent.universe_scope.v1"
    assert eligibility.role is InstrumentRole.MAINBOARD_COMMON_STOCK
    assert eligibility.tradable is True
    assert eligibility.context_only is False
    assert eligibility.order_identity_allowed is True
    assert is_mainboard_tradable(symbol) is True


@pytest.mark.parametrize("symbol", NON_TRADABLE_GROWTH_SYMBOLS)
def test_growth_board_common_stocks_are_not_tradable(symbol: str) -> None:
    eligibility = classify_instrument(symbol)

    assert eligibility.role in {
        InstrumentRole.CHINEXT_COMMON_STOCK,
        InstrumentRole.STAR_COMMON_STOCK,
    }
    assert eligibility.tradable is False
    assert eligibility.context_allowed is False
    assert eligibility.order_identity_allowed is False
    assert is_mainboard_tradable(symbol) is False


def test_growth_indices_and_sector_aggregates_remain_context_only() -> None:
    chinext = classify_instrument("399006.SZ", instrument_type="index")
    star = classify_instrument("000688.SH", instrument_type="index")
    sector = classify_instrument(
        "sector:sw801080",
        instrument_type="sector_aggregate",
    )

    assert chinext.role is InstrumentRole.CHINEXT_INDEX
    assert star.role is InstrumentRole.STAR_INDEX
    assert sector.role is InstrumentRole.SECTOR_AGGREGATE
    for eligibility in (chinext, star, sector):
        assert eligibility.context_allowed is True
        assert eligibility.context_only is True
        assert eligibility.tradable is False
        assert eligibility.order_identity_allowed is False


@pytest.mark.parametrize(
    ("symbol", "exchange", "instrument_type"),
    (
        ("600000.FOO", "", "common_stock"),
        ("600000.SH", "SZ", "common_stock"),
        ("600000", "UNKNOWN", "common_stock"),
        ("600000.SH", "", "future"),
        ("510300.SH", "", "etf"),
    ),
)
def test_unknown_exchange_type_and_funds_fail_closed(
    symbol: str,
    exchange: str,
    instrument_type: str,
) -> None:
    eligibility = classify_instrument(
        symbol,
        exchange=exchange,
        instrument_type=instrument_type,
    )

    assert eligibility.tradable is False
    assert eligibility.order_identity_allowed is False


@pytest.mark.parametrize(
    ("symbol", "instrument_type"),
    (
        ("", "sector_aggregate"),
        (True, "index"),
        (600000, "common_stock"),
        ("not-an-index", "index"),
        (" sector:sw801080", "sector_aggregate"),
    ),
)
def test_context_and_order_identity_require_native_canonical_symbol(
    symbol: object,
    instrument_type: str,
) -> None:
    eligibility = classify_instrument(symbol, instrument_type=instrument_type)

    assert eligibility.tradable is False
    assert eligibility.context_allowed is False
    assert eligibility.order_identity_allowed is False


class _Reader:
    def __init__(self) -> None:
        self.symbols = [*MAINBOARD_SYMBOLS, *NON_TRADABLE_GROWTH_SYMBOLS]

    def get_assets(self, market: str):
        del market
        return [
            {
                "symbol": symbol,
                "name": symbol,
                "list_date": "20000101",
                "status": "active",
                "exchange": symbol.rsplit(".", 1)[-1],
            }
            for symbol in self.symbols
        ]

    def get_coverage(self, market: str, date: str):
        del market, date
        return [
            {"symbol": symbol, "coverage_status": "normal"} for symbol in self.symbols
        ]

    def get_bars_daily(self, market: str, symbol: str, start=None, end=None):
        del market, start, end
        if (
            symbol not in self.symbols
            and f"{symbol}.SZ" not in self.symbols
            and f"{symbol}.SH" not in self.symbols
        ):
            return []
        return [
            {
                "trade_date": "20260716",
                "close": 10.0,
                "vol": 100_000,
                "amount": 100_000.0,
            }
        ]


def test_filter_candidate_and_adapter_share_mainboard_policy() -> None:
    reader = _Reader()

    filtered = filter_universe(
        "20260716",
        list(reader.symbols),
        config={"exclude_non_a_share": False},
        reader=reader,
        market="ashare",
    )
    pool = build_pool(
        date="20260716",
        holdings=["300750.SZ", "600000.SH"],
        universe=list(reader.symbols),
        market="ashare",
        reader=reader,
        scores_by_symbol={symbol: {"combined": 0.8} for symbol in reader.symbols},
    )
    adapter_universe = AshareAdapter(
        reader=reader,
        universe_filter={"exclude_non_a_share": False},
    ).get_universe("20260716")

    assert set(filtered) == set(MAINBOARD_SYMBOLS)
    assert set(pool["universe"]) == set(MAINBOARD_SYMBOLS)
    assert pool["holdings"] == ["600000.SH"]
    assert set(pool["candidate"]) == set(MAINBOARD_SYMBOLS) - {"600000.SH"}
    assert set(adapter_universe) == set(MAINBOARD_SYMBOLS)


@pytest.mark.parametrize("symbol", NON_TRADABLE_GROWTH_SYMBOLS)
def test_sim_execution_rejects_growth_board_before_any_order_path(symbol: str) -> None:
    now = datetime(2026, 7, 16, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    with patch("Ashare.sim_executor._now_cn", return_value=now):
        result = ashare_sim_execute(
            order={
                "order_id": f"REJECT-{symbol}",
                "ts_code": symbol,
                "quantity": 100,
                "price": 10.0,
                "side": "buy",
            },
            account="ashare_sim",
            config={"mock_filled": True},
        )

    assert result.status == "rejected"
    assert result.filled_qty == 0
    assert result.raw_response["reason_code"] == "instrument_not_mainboard_tradable"
    assert result.raw_response["instrument_role"] in {
        InstrumentRole.CHINEXT_COMMON_STOCK.value,
        InstrumentRole.STAR_COMMON_STOCK.value,
    }


@pytest.mark.parametrize(
    ("symbol", "instrument_type", "expected_role"),
    (
        ("399006.SZ", "index", InstrumentRole.CHINEXT_INDEX),
        ("000688.SH", "index", InstrumentRole.STAR_INDEX),
        ("sector:sw801080", "sector_aggregate", InstrumentRole.SECTOR_AGGREGATE),
    ),
)
def test_context_only_identity_cannot_cross_sim_order_boundary(
    symbol: str,
    instrument_type: str,
    expected_role: InstrumentRole,
) -> None:
    now = datetime(2026, 7, 16, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    with patch("Ashare.sim_executor._now_cn", return_value=now):
        result = ashare_sim_execute(
            order={
                "order_id": f"CONTEXT-ONLY-{expected_role.value}",
                "ts_code": symbol,
                "instrument_type": instrument_type,
                "quantity": 100,
                "price": 10.0,
                "side": "buy",
            },
            account="ashare_sim",
            config={"mock_filled": True},
        )

    assert result.status == "rejected"
    assert result.raw_response["reason_code"] == "instrument_not_mainboard_tradable"
    assert result.raw_response["instrument_role"] == expected_role.value
    assert result.raw_response["context_only"] is True
    assert result.raw_response["order_identity_allowed"] is False


def test_disallowed_instrument_is_rejected_before_signal_card_construction() -> None:
    with patch(
        "Ashare.sim_executor._signal_card",
        side_effect=AssertionError("signal card must not be constructed"),
    ):
        result = ashare_sim_execute(
            order={
                "order_id": "PRE-CARD-REJECT",
                "ts_code": "300750.SZ",
                "quantity": 100,
                "price": 10.0,
                "side": "buy",
            },
            account="ashare_sim",
            config={"mock_filled": True},
        )

    assert result.status == "rejected"
    assert result.order_id == "PRE-CARD-REJECT"
