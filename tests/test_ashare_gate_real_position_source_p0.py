from __future__ import annotations

import json
from pathlib import Path

import pytest

import shared.capital as market_capital
from shared.capital.ashare_position_authority import (
    CAPITAL_POSITION_SOURCE_MISMATCH,
    build_ashare_capital_position_authority_view,
    canonical_sha256,
    normalize_ashare_positions,
    reconcile_ashare_position_sources,
)
from shared.execution import local_sim_ledger
from shared.wrappers import tradings_cron_entry


TRADE_DATE = "20260714"
LINEAGE = "ashare-sim-fresh-20260712-v1"


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
        "authority_id": "ashare-capital-v1",
        "authority_generation": 1,
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
            0.0,
            min(50_000.0 - market_value, 45_000.0 - market_value),
        ),
        "stock_gross_exposure_limit_cny": 45_000.0,
        "single_name_cap_cny": 7_500.0,
        "capital_utilization_rate": market_value / 50_000.0,
        "fresh": True,
        "reconciled": True,
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


@pytest.fixture
def isolated_local_sim(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Path:
    root = tmp_path / LINEAGE
    paths = {
        "LOCAL_SIM_DIR": root,
        "LOCAL_SIM_TRADES": root / "local_sim_trades.jsonl",
        "LOCAL_SIM_POSITIONS": root / "local_sim_positions.json",
        "LOCAL_SIM_PNL": root / "local_sim_pnl.json",
        "LOCAL_SIM_LOCK": root / ".local_sim.lock",
        "LOCAL_SIM_POSITIONS_SNAPSHOT": root / "simulated_ashare_positions.json",
        "LOCAL_SIM_RECEIPTS": root / "sim_execution_receipts.jsonl",
    }
    for name, value in paths.items():
        monkeypatch.setattr(local_sim_ledger, name, value)
    local_sim_ledger.bootstrap_fresh_local_sim(
        root=root,
        lineage_started_at="2026-07-12T00:00:00+08:00",
        point_in_time_as_of="2026-07-12T00:00:00+08:00",
    )
    return root


@pytest.fixture(autouse=True)
def offline_ashare_calendar(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "Ashare.t_plus_1._shared_calendar_is_trading_day",
        lambda *_: None,
    )
    monkeypatch.setattr(
        "Ashare.t_plus_1._shared_calendar_trading_days",
        lambda *_: None,
    )
    monkeypatch.setattr(
        "Ashare.t_plus_1._shared_calendar_next_trading_day",
        lambda *_: None,
    )


def _authority(state: dict[str, object]) -> dict[str, object]:
    result = build_ashare_capital_position_authority_view(state, TRADE_DATE)
    assert result["status"] == "verified"
    return result


def _record_real_local_position(
    monkeypatch: pytest.MonkeyPatch,
    *,
    position_trade_date: str = "20260713",
) -> None:
    position_iso_date = (
        f"{position_trade_date[:4]}-{position_trade_date[4:6]}-"
        f"{position_trade_date[6:8]}"
    )
    position_timestamp = f"{position_iso_date}T10:00:00+08:00"
    monkeypatch.setattr(
        local_sim_ledger,
        "_ashare_session_metadata",
        lambda *args, **kwargs: {
            "trade_timestamp_bj": position_timestamp,
            "ashare_session_valid": True,
            "ashare_session_rejection": "",
        },
    )
    monkeypatch.setattr(
        market_capital,
        "verify_market_capital_reservation",
        lambda market, **kwargs: {
            "verified": True,
            "reason": "reservation_verified",
            "reservation_id": kwargs["reservation_id"],
            "reference_id": kwargs["reference_id"],
            "market": market,
            "authority_id": kwargs["authority_id"],
            "authority_generation": kwargs["authority_generation"],
            "execution_lineage_id": kwargs["execution_lineage_id"],
            "risk_unit_key": kwargs["risk_unit_key"],
            "event_id": kwargs["expected_event_id"],
            "remaining_amount_cny": 10_000.0,
            "real_trading_enabled": False,
        },
    )
    lineage = local_sim_ledger.build_execution_lineage(
        lineage_started_at="2026-07-12T00:00:00+08:00",
        point_in_time_as_of=position_timestamp,
    )
    result = local_sim_ledger.record_local_sim_order(
        {
            **lineage,
            "order_id": f"REAL-PRODUCER-NONZERO-{position_trade_date}",
            "idempotency_key": (f"REAL:ashare:{position_trade_date}:600000.SH:buy:1"),
            "trade_date": position_trade_date,
            "ts_code": "600000.SH",
            "side": "buy",
            "quantity": 100,
            "price": 10.0,
            "candidate_pool_layer": "candidate",
            "execution_source": "ashare_candidate_layer",
            "sample_intent": "exploitation",
            "capital_scope": "strategy",
            "market_capital_required": True,
            "market_capital_reference_id": (
                f"ASHARE-CAP:REAL-PRODUCER-NONZERO-{position_trade_date}"
            ),
            "market_capital_reservation_id": "ares-real-producer-1",
            "market_capital_event_id": "aevt-real-producer-1",
            "market_capital_expected_head_event_id": "aevt-real-producer-1",
            "market_capital_expected_head_checksum": "a" * 64,
            "market_capital_risk_unit_key": "600000.SH",
            "market_reserved_gross_cny": 2_000.0,
            "fill_price_source": "sharedsignals_api_realtime_5min",
            "fill_price_source_class": "market_data",
            "fill_evidence": {
                "execution_evidence_class": "verified_5min_market_data",
                "fill_price_source": "sharedsignals_api_realtime_5min",
                "fill_price_source_class": "market_data",
                "bar_time": position_timestamp,
                "bar_volume": 100_000.0,
            },
        },
        "ashare",
        {"account": "ashare_sim"},
        {"local_sim_slippage_bps": 0},
    )
    assert result["status"] == "filled", result
    assert result["recorded"] is True


def _write_pending_order(
    root: Path,
    *,
    side: str = "buy",
    symbol: str = "601999.SH",
) -> None:
    pending = root / "signals" / "pending"
    pending.mkdir(parents=True)
    (pending / "order-1.json").write_text(
        json.dumps(
            {
                "order_id": "order-1",
                "market": "ashare",
                "ts_code": symbol,
                "side": side,
                "trade_date": TRADE_DATE,
                "quantity": 100,
                "weight": 0.0 if side in {"sell", "trim", "exit"} else 0.05,
                "sector": "bank",
                "turnover_wan": 10_000.0,
            }
        ),
        encoding="utf-8",
    )


PAUSE_CASES = (
    (
        {"daily_mtm_change": -1_500.0},
        "ashare_capital_daily_loss_pause",
    ),
    (
        {"consecutive_losses": 3},
        "ashare_capital_consecutive_loss_pause",
    ),
    (
        {
            "equity_cny": 46_500.0,
            "cash_balance_cny": 45_500.0,
            "realized_pnl_cny": -3_500.0,
            "high_water_equity": 50_000.0,
        },
        "ashare_capital_drawdown_halt",
    ),
)


def test_real_local_sim_zero_envelope_reconciles_without_mocked_producer(
    isolated_local_sim: Path,
) -> None:
    state = _capital_state()
    authority = build_ashare_capital_position_authority_view(state, TRADE_DATE)

    source = tradings_cron_entry._load_ashare_generic_position_source(
        TRADE_DATE,
        authority,
    )
    reconciled = reconcile_ashare_position_sources(
        state,
        TRADE_DATE,
        sources={"server_local_sim": source},
        preferred_source="server_local_sim",
        final_capital_state=state,
    )

    assert source["source"] == "server_local_sim_account_snapshot"
    assert source["position_source_status"] == "ready"
    assert source["positions"] == {}
    assert reconciled["status"] == "verified"
    assert reconciled["position_count"] == 0


def test_real_local_sim_nonzero_envelope_reconciles_without_mocked_producer(
    isolated_local_sim: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _record_real_local_position(monkeypatch)
    state = _capital_state({"600000.SH": 100})
    authority = _authority(state)

    source = tradings_cron_entry._load_ashare_generic_position_source(
        TRADE_DATE,
        authority,
    )
    reconciled = reconcile_ashare_position_sources(
        state,
        TRADE_DATE,
        sources={"server_local_sim": source},
        preferred_source="server_local_sim",
        final_capital_state=state,
    )

    assert source["position_source_status"] == "ready"
    assert source["positions"]["600000.SH"]["quantity"] == 100
    assert reconciled["status"] == "verified"
    assert reconciled["position_count"] == 1


def test_missing_or_stale_context_blocks_before_real_local_sim_read(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    absent_root = tmp_path / LINEAGE
    monkeypatch.setattr(local_sim_ledger, "LOCAL_SIM_DIR", absent_root)
    monkeypatch.setattr(
        local_sim_ledger,
        "LOCAL_SIM_LOCK",
        absent_root / ".local_sim.lock",
    )

    missing = tradings_cron_entry._load_ashare_generic_position_source(
        TRADE_DATE,
        None,
    )
    stale = tradings_cron_entry._load_ashare_generic_position_source(
        TRADE_DATE,
        {**_authority(_capital_state()), "trade_date": "20260713"},
    )

    assert missing["position_source_status"] == "blocked"
    assert stale["position_source_status"] == "blocked"
    assert missing["position_source_reason"] == "position_authority_context_invalid"
    assert stale["position_source_reason"] == "position_authority_context_invalid"
    assert not absent_root.exists()


def test_real_local_sim_position_mismatch_fails_closed(
    isolated_local_sim: Path,
) -> None:
    state = _capital_state({"600000.SH": 100})
    authority = _authority(state)

    source = tradings_cron_entry._load_ashare_generic_position_source(
        TRADE_DATE,
        authority,
    )
    reconciled = reconcile_ashare_position_sources(
        state,
        TRADE_DATE,
        sources={"server_local_sim": source},
        preferred_source="server_local_sim",
        final_capital_state=state,
    )

    assert source["position_source_status"] == "ready"
    assert source["position_count"] == 0
    assert reconciled["status"] == "blocked"
    assert reconciled["reason"] == CAPITAL_POSITION_SOURCE_MISMATCH
    assert {"position_count", "positions_fingerprint"}.issubset(
        reconciled["mismatches"][0]["fields"]
    )


def test_real_pending_order_zero_position_path_reaches_ordinary_risk(
    isolated_local_sim: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_pending_order(tmp_path)
    state = _capital_state()
    monkeypatch.setattr(tradings_cron_entry, "ROOT", tmp_path)
    monkeypatch.setattr(tradings_cron_entry, "SHARED", tmp_path / "shared")
    monkeypatch.setattr(tradings_cron_entry, "trade_date", lambda: TRADE_DATE)
    monkeypatch.setattr(
        tradings_cron_entry.market_capital,
        "load_market_capital_provider_state",
        lambda market, date: state,
    )

    result = tradings_cron_entry.run_gate_review(
        "job_gate_review_day",
        "risk/gate/test.jsonl",
        "day",
    )

    assert result["state"] == "ok"
    assert result["pending_order_count"] == 1
    assert result["ashare_position_authority"]["status"] == "verified"
    assert result["decisions"][0]["decision"]["approved"] is True


@pytest.mark.parametrize("pause_overrides, expected_reason", PAUSE_CASES)
@pytest.mark.parametrize("side", ("sell", "trim", "exit"))
def test_real_position_pause_keeps_authority_for_risk_reducing_gate(
    isolated_local_sim: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    pause_overrides: dict[str, object],
    expected_reason: str,
    side: str,
) -> None:
    _record_real_local_position(monkeypatch)
    _write_pending_order(tmp_path, side=side, symbol="600000.SH")
    state = _capital_state({"600000.SH": 100}, **pause_overrides)
    monkeypatch.setattr(tradings_cron_entry, "ROOT", tmp_path)
    monkeypatch.setattr(tradings_cron_entry, "SHARED", tmp_path / "shared")
    monkeypatch.setattr(tradings_cron_entry, "trade_date", lambda: TRADE_DATE)
    monkeypatch.setattr(
        tradings_cron_entry.market_capital,
        "load_market_capital_provider_state",
        lambda market, date: state,
    )

    result = tradings_cron_entry.run_gate_review(
        "job_gate_review_day",
        "risk/gate/test.jsonl",
        "day",
    )

    authority = result["ashare_position_authority"]
    decision = result["decisions"][0]["decision"]
    assert result["state"] == "ok"
    assert authority["status"] == "verified"
    assert authority["position_count"] == 1
    assert authority["positions"][0]["ts_code"] == "600000.SH"
    assert authority["positions"][0]["quantity"] == 100
    assert authority["positions"][0]["entry_date"] == "2026-07-13"
    assert authority["new_risk_allowed"] is False
    assert authority["new_risk_reason"] == expected_reason
    assert decision["approved"] is True, json.dumps(decision, ensure_ascii=False)
    assert not any("持仓数" in reason for reason in decision.get("reasons", []))


@pytest.mark.parametrize("side", ("sell", "trim", "exit"))
def test_real_position_pause_still_enforces_t_plus_one(
    isolated_local_sim: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    side: str,
) -> None:
    _record_real_local_position(monkeypatch, position_trade_date=TRADE_DATE)
    _write_pending_order(tmp_path, side=side, symbol="600000.SH")
    state = _capital_state(
        {"600000.SH": 100},
        daily_mtm_change=-1_500.0,
    )
    monkeypatch.setattr(tradings_cron_entry, "ROOT", tmp_path)
    monkeypatch.setattr(tradings_cron_entry, "SHARED", tmp_path / "shared")
    monkeypatch.setattr(tradings_cron_entry, "trade_date", lambda: TRADE_DATE)
    monkeypatch.setattr(
        tradings_cron_entry.market_capital,
        "load_market_capital_provider_state",
        lambda market, date: state,
    )

    result = tradings_cron_entry.run_gate_review(
        "job_gate_review_day",
        "risk/gate/test.jsonl",
        "day",
    )

    authority = result["ashare_position_authority"]
    decision = result["decisions"][0]["decision"]
    assert authority["status"] == "verified"
    assert authority["position_count"] == 1
    assert authority["positions"][0]["entry_date"] == "2026-07-14"
    assert authority["new_risk_allowed"] is False
    assert decision["approved"] is False
    assert any("T+1" in reason for reason in decision["reasons"])


@pytest.mark.parametrize("pause_overrides, expected_reason", PAUSE_CASES)
@pytest.mark.parametrize(
    ("side", "symbol"),
    (
        ("buy", "601999.SH"),
        ("open", "601999.SH"),
        ("add", "600000.SH"),
    ),
)
def test_real_position_pause_blocks_only_new_risk_before_ordinary_risk(
    isolated_local_sim: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    pause_overrides: dict[str, object],
    expected_reason: str,
    side: str,
    symbol: str,
) -> None:
    _record_real_local_position(monkeypatch)
    _write_pending_order(tmp_path, side=side, symbol=symbol)
    state = _capital_state({"600000.SH": 100}, **pause_overrides)
    monkeypatch.setattr(tradings_cron_entry, "ROOT", tmp_path)
    monkeypatch.setattr(tradings_cron_entry, "SHARED", tmp_path / "shared")
    monkeypatch.setattr(tradings_cron_entry, "trade_date", lambda: TRADE_DATE)
    monkeypatch.setattr(
        tradings_cron_entry.market_capital,
        "load_market_capital_provider_state",
        lambda market, date: state,
    )

    result = tradings_cron_entry.run_gate_review(
        "job_gate_review_day",
        "risk/gate/test.jsonl",
        "day",
    )

    authority = result["ashare_position_authority"]
    decision = result["decisions"][0]["decision"]
    assert result["state"] == "ok"
    assert authority["status"] == "verified"
    assert authority["position_count"] == 1
    assert authority["new_risk_allowed"] is False
    assert authority["new_risk_reason"] == expected_reason
    assert decision["approved"] is False
    assert decision["reason_code"] == expected_reason
    assert decision["position_authority_status"] == "verified"
    assert not any("持仓数" in reason for reason in decision["reasons"])


def test_real_position_source_mismatch_blocks_risk_reducing_order(
    isolated_local_sim: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_pending_order(tmp_path, side="sell", symbol="600000.SH")
    state = _capital_state({"600000.SH": 100})
    monkeypatch.setattr(tradings_cron_entry, "ROOT", tmp_path)
    monkeypatch.setattr(tradings_cron_entry, "SHARED", tmp_path / "shared")
    monkeypatch.setattr(tradings_cron_entry, "trade_date", lambda: TRADE_DATE)
    monkeypatch.setattr(
        tradings_cron_entry.market_capital,
        "load_market_capital_provider_state",
        lambda market, date: state,
    )

    result = tradings_cron_entry.run_gate_review(
        "job_gate_review_day",
        "risk/gate/test.jsonl",
        "day",
    )

    authority = result["ashare_position_authority"]
    decision = result["decisions"][0]["decision"]
    assert result["state"] == "degraded"
    assert authority["status"] == "blocked"
    assert authority["positions"] == []
    assert decision["approved"] is False
    assert decision["reason_code"] == CAPITAL_POSITION_SOURCE_MISMATCH


def test_real_pending_order_concurrent_capital_change_blocks_before_risk(
    isolated_local_sim: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_pending_order(tmp_path)
    before = _capital_state()
    after = _capital_state(
        event_checksum="b" * 64,
        checksum_last="b" * 64,
        event_id="MCAP-CONCURRENT",
    )
    states = iter((before, after))
    monkeypatch.setattr(tradings_cron_entry, "ROOT", tmp_path)
    monkeypatch.setattr(tradings_cron_entry, "SHARED", tmp_path / "shared")
    monkeypatch.setattr(tradings_cron_entry, "trade_date", lambda: TRADE_DATE)
    monkeypatch.setattr(
        tradings_cron_entry.market_capital,
        "load_market_capital_provider_state",
        lambda market, date: next(states),
    )

    result = tradings_cron_entry.run_gate_review(
        "job_gate_review_day",
        "risk/gate/test.jsonl",
        "day",
    )

    decision = result["decisions"][0]["decision"]
    assert result["state"] == "degraded"
    assert decision["approved"] is False
    assert decision["reason_code"] == CAPITAL_POSITION_SOURCE_MISMATCH
    assert not any("持仓数" in reason for reason in decision["reasons"])
    assert result["ashare_position_authority"]["mismatches"][0]["fields"] == [
        "concurrent_authority_read_binding"
    ]
