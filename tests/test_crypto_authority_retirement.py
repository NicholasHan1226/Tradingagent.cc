from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from Crypto.capital_policy import (
    CRYPTO_CAPITAL_AUTHORITY_ID,
    CRYPTO_CAPITAL_POLICY,
)
from Crypto.common import CryptoConfig
from Crypto.promotion import CryptoStrategyPromotion
from Crypto.shadow_runner import CryptoShadowRunner
from Crypto.sim_executor import CryptoLegacyExecutionRetired, crypto_sim_execute
from Crypto.simulator import CryptoSimulator
from Crypto.workflow import CryptoWorkflow, run_crypto_shadow_cycle
from shared.execution import execution_router, shadow_broker, sim_executor_registry
from shared.execution.sim_broker import execute_sim_order
from shared.governance.market_lanes import load_market_lanes
from shared.governance.retirement import RetiredRuntimeError
from shared.markets.config_schema import DataConfig
from shared.markets.safety import looks_like_crypto_payload


ROOT = Path(__file__).resolve().parents[1]
RETIRED_AUTHORITY_ID = "crypto-shadow-sim-v1"


class _NoPriceData:
    reader = None

    def get_latest_price(self, symbol: str, date: str) -> None:
        del symbol, date
        return None


def test_legacy_authority_is_absent_from_active_python_runtime() -> None:
    matches = [
        path.relative_to(ROOT).as_posix()
        for package in (ROOT / "Crypto", ROOT / "shared")
        for path in package.rglob("*.py")
        if RETIRED_AUTHORITY_ID in path.read_text(encoding="utf-8")
    ]

    assert matches == []


def test_governance_has_one_current_crypto_authority_and_read_only_history() -> None:
    registry = load_market_lanes()
    lane = registry.get("crypto")
    retired = registry.get_retired_authority(RETIRED_AUTHORITY_ID)

    assert lane.authority_id == CRYPTO_CAPITAL_AUTHORITY_ID
    assert lane.authority_state == "local_fixture_simulated_candidate"
    assert lane.broker_boundary.live_enabled is False
    assert retired.lane_id == lane.lane_id
    assert retired.successor_authority_id == lane.authority_id
    assert retired.state == "historical_evidence_only"
    assert retired.read_only is True
    assert CRYPTO_CAPITAL_POLICY.real_trading_enabled is False

    raw = yaml.safe_load(
        (ROOT / "shared/governance/market_lanes.yaml").read_text(encoding="utf-8")
    )
    assert [item["authority_id"] for item in raw["retired_authorities"]] == [
        RETIRED_AUTHORITY_ID
    ]


def test_legacy_authority_cannot_register_as_current_crypto_executor() -> None:
    with pytest.raises(ValueError, match="registration disabled"):
        sim_executor_registry.register_sim_executor(
            "crypto",
            lambda order, account, config: None,
            simulation_contract="tradingagent.crypto.paper_broker.v1",
            authority_id=RETIRED_AUTHORITY_ID,
        )


def test_current_fixture_candidate_cannot_register_generic_crypto_executor() -> None:
    with pytest.raises(ValueError, match="registration disabled"):
        sim_executor_registry.register_sim_executor(
            "crypto",
            lambda order, account, config: None,
            simulation_contract="tradingagent.crypto.paper_broker.v1",
            authority_id=CRYPTO_CAPITAL_AUTHORITY_ID,
        )
    assert sim_executor_registry.get_sim_executor("crypto") is None
    assert sim_executor_registry.get_sim_executor_binding("crypto") is None


def test_all_legacy_crypto_writable_entrypoints_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(CryptoLegacyExecutionRetired):
        crypto_sim_execute({}, {}, {})
    with pytest.raises(CryptoLegacyExecutionRetired):
        CryptoSimulator(market_data=_NoPriceData()).simulate({}, {})
    with pytest.raises(CryptoLegacyExecutionRetired):
        CryptoWorkflow(reader=object(), signals_dir=tmp_path / "workflow-signals")
    with pytest.raises(CryptoLegacyExecutionRetired):
        run_crypto_shadow_cycle("20260721", reader=object())
    with pytest.raises(CryptoLegacyExecutionRetired):
        CryptoShadowRunner(signals_dir=tmp_path / "shadow-signals")

    assert list(tmp_path.iterdir()) == []


def test_general_dispatch_never_returns_crypto_fill_without_capital_runtime() -> None:
    result = execute_sim_order(
        order={"order_id": "NO-SECOND-AUTHORITY", "authority_generation": 1},
        market="crypto",
        account={
            "account_id": "crypto_sim",
            "market": "crypto",
            "broker_contract": "tradingagent.crypto.paper_broker.v1",
            "authority_id": CRYPTO_CAPITAL_AUTHORITY_ID,
            "authority_generation": 1,
        },
        config={},
    )

    assert result.status == "failed"
    assert result.filled_qty == 0
    assert result.raw_response["reason"] == "crypto_general_executor_retired"


@pytest.mark.parametrize(
    "payload",
    [
        {"market": "crypto", "symbol": "BTCUSDT"},
        {"symbol": "BTCUSD"},
        {"market": "ashare", "ts_code": "BTC/USDT"},
        {"market": "cn_futures", "pair": "ETH-USDT"},
        {"base_asset": "BTC", "quote_asset": "USDT"},
    ],
)
def test_shared_crypto_identity_detection_cannot_be_masked(
    payload: dict[str, object],
) -> None:
    assert looks_like_crypto_payload(payload) is True


@pytest.mark.parametrize(
    "identity",
    [
        {"market": "crypto", "symbol": "BTCUSDT"},
        {"symbol": "BTCUSD"},
        {"market": "ashare", "ts_code": "BTC/USDT"},
        {"base_asset": "BTC", "quote_asset": "USDT"},
    ],
)
def test_shared_execution_router_rejects_crypto_before_logging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    identity: dict[str, object],
) -> None:
    router_log = tmp_path / "router" / "decisions.jsonl"
    shadow_log = tmp_path / "shadow" / "trades.jsonl"
    monkeypatch.setattr(execution_router, "ROUTER_LOG", router_log)
    monkeypatch.setattr(execution_router, "SHADOW_EXECUTION_LOG", shadow_log)
    order = {
        "order_id": "NO-SHARED-CRYPTO-WRITE",
        "side": "buy",
        "quantity": 1,
        "price": 10,
        **identity,
    }

    with pytest.raises(RetiredRuntimeError, match="legacy_runtime_retired"):
        execution_router.route(order, "shadow")

    assert not router_log.exists()
    assert not shadow_log.parent.exists()


@pytest.mark.parametrize(
    "market,identity",
    [
        ("crypto", {"ts_code": "BTCUSDT"}),
        (None, {"symbol": "BTCUSD"}),
        ("ashare", {"ts_code": "BTC-USDT"}),
        ("cn_futures", {"base_asset": "BTC", "quote_asset": "USDT"}),
    ],
)
def test_shared_shadow_broker_rejects_crypto_before_any_file_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    market: str | None,
    identity: dict[str, object],
) -> None:
    root = tmp_path / "shadow"
    monkeypatch.setattr(shadow_broker, "SHADOW_DIR", root)
    monkeypatch.setattr(shadow_broker, "SHADOW_TRADES", root / "trades.jsonl")
    monkeypatch.setattr(shadow_broker, "SHADOW_POSITIONS", root / "positions.json")
    monkeypatch.setattr(shadow_broker, "SHADOW_PNL", root / "pnl.json")
    monkeypatch.setattr(shadow_broker, "SHADOW_LOCK", root / ".lock")

    with pytest.raises(RetiredRuntimeError, match="legacy_runtime_retired"):
        shadow_broker.record_shadow(
            {
                "side": "buy",
                "quantity": 1,
                "price": 10,
                "capital_layer": "shadow",
                **identity,
            },
            "retired-crypto-writer",
            market=market,
        )

    assert not root.exists()


def test_promotion_scorecard_is_read_only_and_never_eligible() -> None:
    records = [
        {
            "trade_date": "2026-07-20",
            "symbol": symbol,
            "strategy_name": "candidate",
            "pnl": 1,
            "direction_hit": True,
            "triggered": True,
            "capital_layer": "shadow",
            "account_type": "shadow",
        }
        for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT")
    ]
    result = CryptoStrategyPromotion(
        CryptoConfig(promotion={"min_shadow_trades": 1, "min_positive_days_pct": 0.5}),
        records=records,
        train_end="2026-07-19",
    ).score("candidate", as_of="2026-07-21")

    assert result["eligible_for_sim"] is False
    assert result["automatic_promotion_enabled"] is False
    assert result["promotion_authority"] is False
    assert result["manual_review_required"] is True
    assert result["target_layer"] == "shadow"
    assert result["tier"] != "sim"


def test_shared_data_schema_is_unconfigured_until_explicit_fixture_binding() -> None:
    data = DataConfig()

    assert data.binding_scope == "unconfigured"
    assert data.daily_table is None
    assert data.intraday_table is None
    assert data.events_table is None
