from __future__ import annotations

import json
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock

import pytest

from Ashare.adapter import AshareAdapter
from shared import orchestrator as orchestrator_module
from shared.capital.ashare_position_authority import (
    CAPITAL_POSITION_SOURCE_MISMATCH,
    build_ashare_capital_position_authority_view,
    canonical_sha256,
    normalize_ashare_positions,
    reconcile_ashare_position_sources,
)
from shared.markets.base import MarketAdapter
from shared.orchestrator import OrchestratorDeps, run_shadow_loop, run_sim_loop
from shared.wrappers import tradings_cron_entry


TRADE_DATE = "20260714"
LINEAGE = "ashare-sim-fresh-20260712-v1"


def _position_rows(count: int) -> list[dict[str, object]]:
    return [
        {
            "ts_code": f"600{index:03d}.SH",
            "quantity": 100,
            "sellable_quantity": 100,
            "avg_price": 10.0,
            "last_price": 10.0,
            "market_value": 1_000.0,
        }
        for index in range(count)
    ]


def _capital_state(
    positions: dict[str, object] | None = None,
    **overrides: object,
) -> dict[str, object]:
    quantity_map = dict(positions or {})
    normalized, _, reason = normalize_ashare_positions(quantity_map)
    assert normalized is not None, reason
    market_value = round(sum(int(row["quantity"]) * 10.0 for row in normalized), 2)
    checksum = canonical_sha256(
        {
            "authority_id": "ashare-capital-v1",
            "authority_generation": 1,
            "execution_lineage_id": LINEAGE,
            "positions": normalized,
            "trade_date": TRADE_DATE,
        }
    )
    state: dict[str, object] = {
        "source": "market_capital_ledger",
        "schema_version": "market-capital-snapshot.v2",
        "authority_id": "ashare-capital-v1",
        "authority_generation": 1,
        "account_name": "ashare_sim",
        "market": "ashare",
        "currency": "CNY",
        "initial_equity_cny": 50_000.0,
        "equity_cny": 50_000.0,
        "cash_balance_cny": 50_000.0 - market_value,
        "positions_market_value_cny": market_value,
        "positions_quantity_by_risk_unit": quantity_map,
        "position_count": len(normalized),
        "positions_fingerprint": canonical_sha256(normalized),
        "frozen_order_cash_cny": 0.0,
        "realized_pnl_cny": 0.0,
        "unrealized_pnl_cny": 0.0,
        "reserved_capital_cny": 0.0,
        "active_reservations_cny": 0.0,
        "available_to_reserve_cny": max(
            0.0, min(50_000.0 - market_value, 45_000.0 - market_value)
        ),
        "stock_gross_exposure_limit_cny": 45_000.0,
        "single_name_cap_cny": 7_500.0,
        "capital_utilization_rate": market_value / 50_000.0,
        "reconciled": True,
        "fresh": True,
        "trade_date": TRADE_DATE,
        "event_id": "MCAP-RECONCILED",
        "event_checksum": checksum,
        "checksum_status": "valid",
        "checksum_event_count": 2,
        "checksum_last": checksum,
        "execution_lineage_id": LINEAGE,
        "daily_mtm_change": 0.0,
        "daily_realized_pnl": 0.0,
        "max_daily_loss": 1_500.0,
        "consecutive_losses": 0,
        "max_consecutive_losses": 3,
        "high_water_equity": 50_000.0,
        "max_drawdown": 3_500.0,
        "real_trading_enabled": False,
    }
    state.update(overrides)
    return state


def _source(
    positions: list[dict[str, object]] | dict[str, object],
    *,
    state: dict[str, object] | None = None,
    source: str = "test_position_source",
) -> dict[str, object]:
    authority = build_ashare_capital_position_authority_view(
        state or _capital_state(), TRADE_DATE
    )
    assert authority["status"] == "verified"
    normalized, _, reason = normalize_ashare_positions(positions)
    assert normalized is not None, reason
    return {
        "source": source,
        "position_source_status": "ready",
        "positions": positions,
        "authority_id": authority["authority_id"],
        "authority_generation": authority["authority_generation"],
        "execution_lineage_id": authority["execution_lineage_id"],
        "authority_checksum": authority["authority_checksum"],
        "trade_date": authority["trade_date"],
        "position_count": len(normalized),
        "positions_fingerprint": canonical_sha256(normalized),
    }


def test_authority_zero_vs_legacy_eight_is_source_mismatch() -> None:
    result = reconcile_ashare_position_sources(
        _capital_state(),
        TRADE_DATE,
        sources={"legacy": _source(_position_rows(8), source="legacy")},
        preferred_source="legacy",
    )

    assert result["status"] == "blocked"
    assert result["reason"] == CAPITAL_POSITION_SOURCE_MISMATCH
    assert result["position_count"] == 0
    assert result["positions"] == []
    assert {"position_count", "positions_fingerprint"}.issubset(
        result["mismatches"][0]["fields"]
    )
    assert len(result["source_audit"][0]["source_sha256"]) == 64


def test_authority_zero_vs_strategy_zero_passes_position_count_gate() -> None:
    result = reconcile_ashare_position_sources(
        _capital_state(),
        TRADE_DATE,
        sources={"strategy": _source([], source="strategy")},
        preferred_source="strategy",
    )

    assert result["status"] == "verified"
    assert result["position_count"] == 0
    assert result["source_audit"][0]["status"] == "verified"


def test_checksum_envelope_fields_are_all_mandatory() -> None:
    for field in (
        "event_checksum",
        "checksum_status",
        "checksum_last",
        "checksum_event_count",
    ):
        state = _capital_state()
        state.pop(field)
        result = build_ashare_capital_position_authority_view(state, TRADE_DATE)
        assert result == {
            "status": "blocked",
            "reason": "ashare_capital_checksum_invalid",
        }


def test_explicit_current_authority_accepts_rotated_generation_and_lineage() -> None:
    rotated_lineage = "ashare-sim-rotated-generation-2"
    state = _capital_state(
        authority_generation=2,
        execution_lineage_id=rotated_lineage,
    )
    checksum = canonical_sha256(
        {
            "authority_id": "ashare-capital-v1",
            "authority_generation": 2,
            "execution_lineage_id": rotated_lineage,
            "positions": [],
            "trade_date": TRADE_DATE,
        }
    )
    state.update(event_checksum=checksum, checksum_last=checksum)

    result = build_ashare_capital_position_authority_view(
        state,
        TRADE_DATE,
        current_authority_scope={
            "capital_authority_id": "ashare-capital-v1",
            "authority_generation": 2,
            "execution_lineage_id": rotated_lineage,
        },
    )

    assert result["status"] == "verified"
    assert result["authority_generation"] == 2
    assert result["execution_lineage_id"] == rotated_lineage


def test_explicit_current_authority_scope_never_fills_missing_lineage() -> None:
    result = build_ashare_capital_position_authority_view(
        _capital_state(),
        TRADE_DATE,
        current_authority_scope={
            "capital_authority_id": "ashare-capital-v1",
            "authority_generation": 1,
        },
    )

    assert result == {
        "status": "blocked",
        "reason": "ashare_current_authority_scope_invalid",
    }


def test_checksum_status_last_and_event_count_must_be_consistent() -> None:
    cases = (
        {"checksum_status": "unknown"},
        {"checksum_last": "b" * 64},
        {"event_checksum": "not-a-checksum"},
        {"checksum_event_count": 0},
        {"checksum_event_count": -1},
        {"checksum_event_count": 1.5},
        {"checksum_event_count": 2.0},
        {"checksum_event_count": True},
    )
    for overrides in cases:
        result = build_ashare_capital_position_authority_view(
            _capital_state(**overrides), TRADE_DATE
        )
        assert result["reason"] == "ashare_capital_checksum_invalid"


def test_missing_position_map_never_infers_empty_from_zero_market_value() -> None:
    state = _capital_state(positions_market_value_cny=0.0)
    state.pop("positions_quantity_by_risk_unit")

    result = build_ashare_capital_position_authority_view(state, TRADE_DATE)

    assert result["status"] == "blocked"
    assert result["reason"] == "ashare_capital_position_state_incomplete"

    source = _source([], source="server_local")
    source.pop("positions")
    source_result = reconcile_ashare_position_sources(
        _capital_state(),
        TRADE_DATE,
        sources={"server_local": source},
        preferred_source="server_local",
    )
    assert source_result["reason"] == CAPITAL_POSITION_SOURCE_MISMATCH
    assert "positions" in source_result["mismatches"][0]["fields"]


def test_every_source_identity_field_is_mandatory() -> None:
    for field in (
        "authority_id",
        "authority_generation",
        "execution_lineage_id",
        "authority_checksum",
        "trade_date",
    ):
        source = _source([], source="strategy")
        source.pop(field)
        if field == "authority_id":
            source["capital_authority_id"] = "ashare-capital-v1"
        if field == "authority_checksum":
            source["capital_authority_checksum"] = _capital_state()["event_checksum"]
        result = reconcile_ashare_position_sources(
            _capital_state(),
            TRADE_DATE,
            sources={"strategy": source},
            preferred_source="strategy",
        )
        assert result["reason"] == CAPITAL_POSITION_SOURCE_MISMATCH
        assert f"{field}_missing" in result["mismatches"][0]["fields"]


def test_source_identity_values_must_match_authority() -> None:
    mutations = (
        ("authority_id", "retired-capital"),
        ("authority_generation", 2),
        ("authority_generation", 1.0),
        ("execution_lineage_id", "retired-lineage"),
        ("authority_checksum", "b" * 64),
        ("trade_date", "20260713"),
    )
    for field, value in mutations:
        source = _source([], source="strategy")
        source[field] = value
        result = reconcile_ashare_position_sources(
            _capital_state(),
            TRADE_DATE,
            sources={"strategy": source},
            preferred_source="strategy",
        )
        assert result["reason"] == CAPITAL_POSITION_SOURCE_MISMATCH
        assert field in result["mismatches"][0]["fields"]


def test_symbols_are_strict_and_normalized_duplicates_are_rejected() -> None:
    invalid_cases = (
        [{"ts_code": "600000", "quantity": 100}],
        [{"ts_code": "ABC.SH", "quantity": 100}],
        [
            {"ts_code": "600000.SH", "quantity": 100},
            {"ts_code": "600000.XSHG", "quantity": 100},
        ],
    )
    for rows in invalid_cases:
        normalized, _, reason = normalize_ashare_positions(rows)
        assert normalized is None
        assert reason.startswith(
            ("position_symbol_invalid", "duplicate_position_symbol")
        )


def test_negative_fractional_and_boolean_quantities_fail_closed() -> None:
    for quantity in (-1, 1.5, True, float("nan")):
        normalized, _, reason = normalize_ashare_positions(
            [{"ts_code": "600000.SH", "quantity": quantity}]
        )
        assert normalized is None
        assert reason == "position_quantity_invalid:600000.SH"


def test_declared_position_count_and_fingerprint_conflicts_fail_closed() -> None:
    state = _capital_state({"600000.SH": 100})
    for overrides, expected_reason in (
        ({"position_count": 0}, "ashare_capital_position_count_invalid"),
        (
            {"positions_fingerprint": "b" * 64},
            "ashare_capital_positions_fingerprint_invalid",
        ),
    ):
        result = build_ashare_capital_position_authority_view(
            {**state, **overrides}, TRADE_DATE
        )
        assert result["status"] == "blocked"
        assert result["reason"] == expected_reason


def test_stale_authority_trade_date_fails_closed() -> None:
    result = build_ashare_capital_position_authority_view(
        _capital_state(trade_date="20260713"), TRADE_DATE
    )

    assert result["status"] == "blocked"
    assert result["reason"] == "ashare_capital_trade_date_mismatch"


def test_concurrent_authority_double_read_drift_is_blocked() -> None:
    result = reconcile_ashare_position_sources(
        _capital_state(),
        TRADE_DATE,
        sources={"strategy": _source([], source="strategy")},
        preferred_source="strategy",
        final_capital_state=_capital_state(checksum_event_count=3),
    )

    assert result["status"] == "blocked"
    assert result["reason"] == CAPITAL_POSITION_SOURCE_MISMATCH
    assert result["mismatches"][0]["fields"] == ["concurrent_authority_read_binding"]
    assert [row["source_name"] for row in result["source_audit"]] == [
        "market_capital_before",
        "market_capital_after",
    ]


def test_matching_nonzero_positions_preserve_authority_and_source_details(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    state = _capital_state({"000001.XSHE": 100})
    rows = [
        {
            "ts_code": "000001.SZ",
            "quantity": 100,
            "sellable_quantity": 100,
            "avg_price": 10.0,
            "last_price": 10.5,
            "market_value": 1_050.0,
        }
    ]
    result = reconcile_ashare_position_sources(
        state,
        TRADE_DATE,
        sources={"server_local": _source(rows, state=state, source="server_local")},
        preferred_source="server_local",
        final_capital_state=state,
    )

    assert result["status"] == "verified"
    assert result["position_count"] == 1
    assert result["positions"][0]["ts_code"] == "000001.SZ"
    assert result["positions"][0]["sellable_quantity"] == 100
    assert result["positions"][0]["position_authority_verified"] is True
    assert result["positions"][0]["execution_lineage_id"] == LINEAGE

    blocked_refresh = (
        orchestrator_module._write_ashare_post_execution_capital_plan_refresh(
            market="ashare",
            date=TRADE_DATE,
            account="ashare_sim",
            capital_plan={"target_positions": 8},
            position_authority={
                "status": "blocked",
                "reason": CAPITAL_POSITION_SOURCE_MISMATCH,
            },
            capital_layer="simulated",
            account_type="simulated",
            position_change_count=1,
            review_root=tmp_path / "blocked-review",
        )
    )
    assert blocked_refresh["status"] == "blocked"
    verified_refresh = (
        orchestrator_module._write_ashare_post_execution_capital_plan_refresh(
            market="ashare",
            date=TRADE_DATE,
            account="ashare_sim",
            capital_plan={"target_positions": 8},
            position_authority={**result, "capital_cash_available": 44_000.0},
            capital_layer="simulated",
            account_type="simulated",
            position_change_count=1,
            review_root=tmp_path / "verified-review",
        )
    )
    assert verified_refresh["status"] == "written"
    refresh_row = json.loads(
        Path(verified_refresh["path"]).read_text(encoding="utf-8").splitlines()[-1]
    )
    assert refresh_row["capital_plan"]["cash_source"] == (
        "market_capital_authority_post_execution"
    )
    assert refresh_row["capital_plan"]["available_cash"] == 44_000.0

    local_ledger = orchestrator_module.local_sim_ledger
    lineage = local_ledger.build_execution_lineage(
        lineage_started_at="2026-07-12T00:00:00+08:00",
        point_in_time_as_of="2026-07-14T10:00:00+08:00",
    )
    manifest = {**lineage, "source": "fresh_zero_import_bootstrap"}
    native_trades: list[dict[str, object]] = []
    monkeypatch.setattr(
        local_ledger, "_read_fresh_lineage_manifest", lambda *args: manifest
    )
    monkeypatch.setattr(local_ledger, "_lock", lambda: nullcontext())
    monkeypatch.setattr(
        local_ledger, "_load_trades_unlocked", lambda: list(native_trades)
    )
    monkeypatch.setattr(local_ledger, "_strategy_trades_only", lambda rows: list(rows))
    monkeypatch.setattr(
        "Ashare.adapter.build_current_sample_adjustment",
        lambda: {
            "sample_authority_status": "ready",
            "sample_authority_reliable": True,
            "strategy_sample_valid_count": 5,
            "min_strategy_samples": 5,
            "sample_debt": False,
            "reason": "ready",
            "real_trading_enabled": False,
        },
    )

    def assert_native_envelope(
        authority: dict[str, object], expected_count: int
    ) -> None:
        snapshot = local_ledger.get_local_sim_account_snapshot(
            "ashare_sim",
            trade_date=TRADE_DATE,
            position_authority=authority,
        )
        pnl = local_ledger.get_local_sim_pnl(
            "ashare_sim",
            trade_date=TRADE_DATE,
            position_authority=authority,
        )
        for source in (snapshot, pnl):
            assert source["position_source_status"] == "ready"
            assert source["authority_id"] == authority["authority_id"]
            assert source["authority_generation"] == authority["authority_generation"]
            assert source["execution_lineage_id"] == authority["execution_lineage_id"]
            assert source["authority_checksum"] == authority["authority_checksum"]
            assert source["trade_date"] == TRADE_DATE
            assert source["position_count"] == expected_count
            assert source["positions_fingerprint"] == authority["positions_fingerprint"]
        adapter_source = AshareAdapter(reader=object()).get_sim_account(
            trade_date=TRADE_DATE,
            position_authority=authority,
        )
        assert adapter_source["position_source_status"] == "ready"
        assert adapter_source["authority_checksum"] == authority["authority_checksum"]
        assert adapter_source["position_count"] == expected_count
        assert (
            adapter_source["strategy_position_envelope"]["position_source_status"]
            == "ready"
        )
        assert (
            adapter_source["strategy_position_envelope"]["positions_fingerprint"]
            == authority["positions_fingerprint"]
        )

    assert_native_envelope(
        build_ashare_capital_position_authority_view(_capital_state(), TRADE_DATE),
        0,
    )
    native_trades.append(
        {
            **lineage,
            "account": "ashare_sim",
            "status": "filled",
            "ts_code": "600000.SH",
            "side": "buy",
            "quantity": 100,
            "amount": 1_000.0,
            "net_amount": 1_000.0,
            "filled_price": 10.0,
            "commission": 0.0,
            "stamp_duty": 0.0,
            "transfer_fee": 0.0,
            "trade_date": "2026-07-14",
            "sample_intent": "exploitation",
            "real_trading_enabled": False,
        }
    )
    assert_native_envelope(
        build_ashare_capital_position_authority_view(
            _capital_state({"600000.SH": 100}), TRADE_DATE
        ),
        1,
    )


class _MismatchAdapter(MarketAdapter):
    def get_market(self) -> str:
        return "ashare"

    def get_universe(self, date: str) -> list[str]:
        return ["601999.SH"]

    def map_symbol_to_reader(self, symbol: str) -> tuple[str, str]:
        return "ashare", symbol

    def get_strategy_config(self) -> dict[str, object]:
        return {
            "market": "ashare",
            "sim_capital": 50_000.0,
            "portfolio_method": "conviction_weighted",
            "regime": "unit",
            "max_candidates": 1,
            "score_universe_limit": 1,
            "max_portfolio_positions": 8,
            "default_price": 10.0,
            "default_volatility": 0.2,
            "sample_collection_policy": {},
        }

    def get_shadow_account(self) -> str:
        return "ashare_shadow"

    def get_sim_account(self, *, trade_date: str = "") -> dict[str, object]:
        return {
            "account": "ashare_sim",
            "sim_capital": 50_000.0,
            "cash_available": 42_000.0,
            **_source(_position_rows(8), source="legacy_strategy_snapshot"),
        }


class _ZeroAdapter(_MismatchAdapter):
    def get_sim_account(self, *, trade_date: str = "") -> dict[str, object]:
        return {
            "account": "ashare_sim",
            "sim_capital": 50_000.0,
            "cash_available": 50_000.0,
            **_source([], source="zero_strategy_snapshot"),
        }


class _Reader:
    def get_bars_daily(
        self,
        market: str,
        symbol: str,
        start: object = None,
        end: object = None,
    ) -> list[dict[str, float]]:
        return [{"close": 10.0}, {"close": 10.0}]


def _deps(risk_check: Mock) -> OrchestratorDeps:
    counter = {"value": 0}

    def audit(**kwargs: object) -> dict[str, object]:
        counter["value"] += 1
        return {
            "audit_id": f"audit-{counter['value']}",
            "stage": kwargs.get("stage", ""),
            "ts_code": kwargs.get("ts_code", ""),
        }

    return OrchestratorDeps(
        score_stock=lambda *args, **kwargs: {},
        build_pool=lambda *args, **kwargs: {
            "candidate": ["601999.SH"],
            "watch": [],
            "holdings": [],
            "universe": ["601999.SH"],
        },
        debate=lambda *args, **kwargs: {"belief_score": 0.8},
        risk_check=risk_check,
        construct=lambda *args, **kwargs: {
            "positions": [],
            "total_weight": 0.0,
            "cash_weight": 1.0,
        },
        size_position=lambda *args, **kwargs: 0.05,
        record_shadow=lambda *args, **kwargs: {},
        run_review=lambda *args, **kwargs: {"status": "ok"},
        record_audit_event=audit,
        execute_sim_order=lambda *args, **kwargs: {
            "status": "rejected",
            "filled_qty": 0,
        },
        send_email=None,
        score_universe=None,
    )


def test_entrypoints_block_before_ordinary_risk_and_pass_only_verified_zero(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    local_ledger = orchestrator_module.local_sim_ledger
    local_root = tmp_path / LINEAGE
    for name, value in {
        "LOCAL_SIM_DIR": local_root,
        "LOCAL_SIM_TRADES": local_root / "local_sim_trades.jsonl",
        "LOCAL_SIM_POSITIONS": local_root / "local_sim_positions.json",
        "LOCAL_SIM_PNL": local_root / "local_sim_pnl.json",
        "LOCAL_SIM_LOCK": local_root / ".local_sim.lock",
        "LOCAL_SIM_POSITIONS_SNAPSHOT": (
            local_root / "simulated_ashare_positions.json"
        ),
        "LOCAL_SIM_RECEIPTS": local_root / "sim_execution_receipts.jsonl",
    }.items():
        monkeypatch.setattr(local_ledger, name, value)
    local_ledger.bootstrap_fresh_local_sim(
        root=local_root,
        lineage_started_at="2026-07-12T00:00:00+08:00",
        point_in_time_as_of="2026-07-12T00:00:00+08:00",
    )

    generic_loader = tradings_cron_entry._load_ashare_generic_position_source
    risk_check = Mock(
        return_value={
            "approved": True,
            "adjusted_weight": 0.05,
            "reasons": [],
            "portfolio_position_count": 0,
        }
    )
    monkeypatch.setattr(
        tradings_cron_entry,
        "_pending_signal_orders",
        lambda: [
            {
                "order_id": "order-1",
                "market": "ashare",
                "ts_code": "601999.SH",
                "weight": 0.05,
            }
        ],
    )
    monkeypatch.setattr(tradings_cron_entry, "trade_date", lambda: TRADE_DATE)
    monkeypatch.setattr(tradings_cron_entry, "append_jsonl", lambda *_: None)
    monkeypatch.setattr(
        tradings_cron_entry.market_capital,
        "load_market_capital_provider_state",
        lambda market, date: _capital_state(),
    )
    monkeypatch.setattr("shared.risk.pre_trade_check.check", risk_check)
    monkeypatch.setattr(
        tradings_cron_entry,
        "_load_ashare_generic_position_source",
        lambda date, authority: _source(_position_rows(8), source="legacy_generic"),
    )

    blocked_gate = tradings_cron_entry.run_gate_review(
        "job_gate_review_day", "risk/gate/test.jsonl", "day"
    )
    risk_check.assert_not_called()
    assert blocked_gate["decisions"][0]["decision"]["reason_code"] == (
        CAPITAL_POSITION_SOURCE_MISMATCH
    )
    assert not any(
        "持仓数" in reason
        for reason in blocked_gate["decisions"][0]["decision"]["reasons"]
    )

    monkeypatch.setattr(
        tradings_cron_entry, "_load_ashare_generic_position_source", generic_loader
    )
    passed_gate = tradings_cron_entry.run_gate_review(
        "job_gate_review_day", "risk/gate/test.jsonl", "day"
    )
    risk_check.assert_called_once()
    assert passed_gate["decisions"][0]["decision"]["approved"] is True

    risk_check.reset_mock()
    monkeypatch.setattr(
        tradings_cron_entry,
        "_load_ashare_generic_position_source",
        generic_loader,
    )
    monkeypatch.setattr(
        tradings_cron_entry.market_capital,
        "load_market_capital_provider_state",
        lambda market, date: _capital_state(daily_mtm_change=-1_500.0),
    )
    capital_blocked_gate = tradings_cron_entry.run_gate_review(
        "job_gate_review_day", "risk/gate/test.jsonl", "day"
    )
    risk_check.assert_not_called()
    assert capital_blocked_gate["state"] == "ok"
    assert capital_blocked_gate["ashare_position_authority"]["status"] == "verified"
    assert (
        capital_blocked_gate["ashare_position_authority"]["new_risk_allowed"] is False
    )
    assert capital_blocked_gate["decisions"][0]["decision"]["reason_code"] == (
        "ashare_capital_daily_loss_pause"
    )

    dynamic_plan = Mock(side_effect=AssertionError("dynamic plan must not run"))
    rebalance = Mock(side_effect=AssertionError("rebalance must not run"))
    monkeypatch.setattr(
        orchestrator_module,
        "_dispatch_ashare_market_outbox",
        lambda account: {"status": "replayed", "action_count": 0, "pending_count": 0},
    )
    monkeypatch.setattr(
        orchestrator_module,
        "_ashare_authoritative_account_view",
        lambda account, date, **kwargs: _source([], source="server_local_sim_ledger"),
    )
    monkeypatch.setattr(
        orchestrator_module.local_sim_ledger,
        "get_local_sim_exploration_state",
        lambda *args, **kwargs: {
            "status": "ready",
            "new_position_count": 0,
            "open_exposure_cny": 0.0,
            "daily_realized_pnl_cny": 0.0,
            "daily_loss_cny": 0.0,
            "real_trading_enabled": False,
        },
    )
    monkeypatch.setattr(
        orchestrator_module.market_capital,
        "load_market_capital_provider_state",
        lambda market, date: _capital_state(),
    )
    monkeypatch.setattr(
        "Ashare.adapter.build_current_sample_adjustment",
        lambda **kwargs: {
            "sample_authority_status": "ready",
            "sample_authority_reliable": True,
            "strategy_sample_valid_count": 5,
            "min_strategy_samples": 5,
            "sample_debt": False,
            "reason": "ready",
            "real_trading_enabled": False,
        },
    )
    monkeypatch.setattr(
        orchestrator_module,
        "_score_symbols_with_batch",
        lambda *args, **kwargs: {
            "601999.SH": {"combined": 0.8, "sector": "unit", "turnover_wan": 20_000}
        },
    )
    monkeypatch.setattr(
        orchestrator_module,
        "_build_and_persist_ashare_observations",
        lambda **kwargs: {
            "observations": [],
            "observation_by_symbol": {},
            "persistence": {
                "status": "recorded",
                "candidate_observation_count": 1,
                "prediction_count": 1,
                "real_trading_enabled": False,
            },
            "real_trading_enabled": False,
        },
    )
    monkeypatch.setattr(
        orchestrator_module,
        "_run_condition_lifecycle",
        lambda *args, **kwargs: {
            "condition_count": 0,
            "trigger_replay_count": 0,
            "filled_replay_count": 0,
            "conditions": [],
            "trigger_replay": [],
        },
    )
    monkeypatch.setattr(
        orchestrator_module, "_ashare_exploration_fill_count", lambda *a, **k: 0
    )
    monkeypatch.setattr(
        orchestrator_module, "_ashare_dynamic_capital_plan", dynamic_plan
    )
    monkeypatch.setattr(orchestrator_module, "_ashare_rebalance_plan", rebalance)
    monkeypatch.setattr(
        orchestrator_module,
        "_persist_ashare_sample_outcomes",
        lambda **kwargs: {"status": "recorded", "real_trading_enabled": False},
    )
    monkeypatch.setattr(
        orchestrator_module,
        "_write_ashare_capital_plan_log",
        lambda **kwargs: {"status": "skipped", "rows": 0},
    )
    monkeypatch.setattr(
        orchestrator_module,
        "_write_sim_execution_exclusions",
        lambda **kwargs: {"status": "skipped", "rows": 0},
    )
    monkeypatch.setattr(
        orchestrator_module,
        "_write_ashare_post_execution_capital_plan_refresh",
        lambda **kwargs: {"status": "skipped", "rows": 0},
    )

    result = run_sim_loop(
        _MismatchAdapter(),
        TRADE_DATE,
        _Reader(),
        deps=_deps(risk_check),
        signals_dir=tmp_path / "signals",
    )

    risk_check.assert_not_called()
    dynamic_plan.assert_not_called()
    rebalance.assert_not_called()
    assert result["ashare_position_authority_reason"] == (
        CAPITAL_POSITION_SOURCE_MISMATCH
    )
    assert result["capital_plan"]["existing_position_count"] is None
    assert result["capital_plan_decision"]["position_capacity"] is None
    assert result["rebalance"]["reason"] == CAPITAL_POSITION_SOURCE_MISMATCH

    risk_check.reset_mock()
    blocked_shadow = run_shadow_loop(
        _MismatchAdapter(),
        TRADE_DATE,
        _Reader(),
        deps=_deps(risk_check),
        signals_dir=tmp_path / "shadow-signals-blocked",
    )
    risk_check.assert_not_called()
    assert blocked_shadow["order_count"] == 0
    assert blocked_shadow["ashare_position_authority_reason"] == (
        CAPITAL_POSITION_SOURCE_MISMATCH
    )

    passed_shadow = run_shadow_loop(
        _ZeroAdapter(),
        TRADE_DATE,
        _Reader(),
        deps=_deps(risk_check),
        signals_dir=tmp_path / "shadow-signals-zero",
    )
    risk_check.assert_called_once()
    assert passed_shadow["ashare_position_authority"]["status"] == "verified"
    assert passed_shadow["ashare_position_authority"]["capital_cash_available"] == (
        45_000.0
    )

    risk_check.reset_mock()
    capital_blocked_adapter = _ZeroAdapter()
    source_getter = Mock(wraps=capital_blocked_adapter.get_sim_account)
    monkeypatch.setattr(capital_blocked_adapter, "get_sim_account", source_getter)
    monkeypatch.setattr(
        orchestrator_module.market_capital,
        "load_market_capital_provider_state",
        lambda market, date: _capital_state(daily_mtm_change=-1_500.0),
    )
    dynamic_plan.side_effect = None
    dynamic_plan.return_value = {
        "enabled": True,
        "status": "approved",
        "risk_mode": "normal",
        "target_positions": 3,
        "existing_position_count": 0,
        "max_new_positions": 1,
        "position_budget_by_symbol": {"601999.SH": 1_000.0},
        "available_cash": 45_000.0,
        "cash_reserve": 5_000.0,
        "reasons": [],
        "notes": [],
    }
    rebalance.side_effect = None
    rebalance.return_value = {
        "enabled": True,
        "status": "approved",
        "target_positions": 3,
        "existing_position_count": 0,
        "planned_sell_count": 0,
        "sells": [],
        "dynamic_thresholds": {},
    }
    capital_blocked_sim = run_sim_loop(
        capital_blocked_adapter,
        TRADE_DATE,
        _Reader(),
        deps=_deps(risk_check),
        signals_dir=tmp_path / "signals-capital-blocked",
    )
    capital_blocked_shadow = run_shadow_loop(
        capital_blocked_adapter,
        TRADE_DATE,
        _Reader(),
        deps=_deps(risk_check),
        signals_dir=tmp_path / "shadow-signals-capital-blocked",
    )
    assert source_getter.call_count >= 2
    risk_check.assert_not_called()
    dynamic_plan.assert_called_once()
    rebalance.assert_called_once()
    assert capital_blocked_sim["ashare_capital_state_reason"] == (
        "ashare_capital_daily_loss_pause"
    )
    assert capital_blocked_sim["ashare_position_authority"]["status"] == "verified"
    assert capital_blocked_sim["capital_plan"]["existing_position_count"] == 0
    assert capital_blocked_sim["capital_plan"]["max_new_positions"] == 0
    assert capital_blocked_sim["capital_plan"]["new_risk_allowed"] is False
    assert capital_blocked_shadow["ashare_position_authority"]["status"] == ("verified")
    assert capital_blocked_shadow["ashare_position_authority"]["new_risk_reason"] == (
        "ashare_capital_daily_loss_pause"
    )

    risk_check.reset_mock()
    nonzero_state = _capital_state({"600000.SH": 100})
    nonzero_rows = _position_rows(1)
    matching_nonzero_adapter = _ZeroAdapter()
    monkeypatch.setattr(
        matching_nonzero_adapter,
        "get_sim_account",
        lambda **kwargs: {
            "account": "ashare_sim",
            "sim_capital": 50_000.0,
            "cash_available": 49_000.0,
            **_source(
                nonzero_rows,
                state=nonzero_state,
                source="matching_nonzero_adapter",
            ),
        },
    )
    monkeypatch.setattr(
        orchestrator_module,
        "_ashare_authoritative_account_view",
        lambda account, date, **kwargs: _source(
            nonzero_rows,
            state=nonzero_state,
            source="matching_nonzero_server_local",
        ),
    )
    monkeypatch.setattr(
        orchestrator_module.market_capital,
        "load_market_capital_provider_state",
        lambda market, date: nonzero_state,
    )
    nonzero_shadow = run_shadow_loop(
        matching_nonzero_adapter,
        TRADE_DATE,
        _Reader(),
        deps=_deps(risk_check),
        signals_dir=tmp_path / "shadow-signals-nonzero",
    )
    risk_check.assert_called_once()
    nonzero_risk_portfolio = risk_check.call_args.args[1]
    assert nonzero_risk_portfolio["total_exposure"] == pytest.approx(0.02)
    assert nonzero_shadow["ashare_position_authority"]["position_count"] == 1
    assert (
        nonzero_shadow["ashare_position_authority"][
            "capital_positions_market_value_cny"
        ]
        == 1_000.0
    )
