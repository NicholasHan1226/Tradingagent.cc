from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from Ashare import adapter as ashare_adapter_module
from Ashare.adapter import AshareAdapter
from shared.accounting import trade_audit_trail
import shared.capital as market_capital
from shared.capital.ashare_position_authority import (
    canonical_sha256,
    normalize_ashare_positions,
)
from shared.execution import local_sim_ledger, shadow_broker, sim_executor_registry
from shared.execution.execution_lineage import (
    ASHARE_AUTHORITY_GENERATION,
    ASHARE_EXECUTION_LINEAGE_ID,
    build_execution_lineage,
)
from shared.execution.signal_state_machine import read_json
from shared.markets.base import MarketAdapter
from shared.review.sample_journal import SampleJournal
from shared import orchestrator as orchestrator_module
from shared.orchestrator import (
    OrchestratorDeps,
    _apply_position_budgets,
    _ashare_authoritative_account_view,
    _build_signal_card,
    _estimate_ashare_market_reservation,
    run_sim_loop,
)


def test_ashare_position_budget_cannot_expand_risk_approved_weight():
    portfolio = {
        "positions": [
            {"ts_code": "600000.SH", "price": 10.0, "shares": 700, "weight": 0.14}
        ]
    }
    _apply_position_budgets(
        market="ashare",
        portfolio=portfolio,
        order_meta={"600000.SH": {"price": 10.0, "weight": 0.14}},
        capital_plan={
            "enabled": True,
            "position_budget_by_symbol": {"600000.SH": 15_000.0},
        },
        capital=50_000.0,
    )
    position = portfolio["positions"][0]
    assert position["weight"] <= 0.14
    assert position["amount"] <= 7_000.0
    assert position["requested_budget"] == 15_000.0
    assert position["budget_cap_reason"] == "risk_adjusted_weight_cap"


def test_ashare_position_budget_rounding_cannot_cross_fifteen_percent():
    portfolio = {
        "positions": [
            {"ts_code": "600000.SH", "price": 7.31, "shares": 100, "weight": 0.01}
        ]
    }
    _apply_position_budgets(
        market="ashare",
        portfolio=portfolio,
        order_meta={"600000.SH": {"price": 7.31, "weight": 0.15}},
        capital_plan={
            "enabled": True,
            "position_budget_by_symbol": {"600000.SH": 17_500.0},
        },
        capital=50_000.0,
    )
    assert portfolio["positions"][0]["amount"] <= 7_500.0
    assert portfolio["positions"][0]["weight"] <= 0.15


def test_ashare_reservation_uses_versioned_buy_transfer_fee() -> None:
    reserved = _estimate_ashare_market_reservation(
        {
            "ts_code": "600000.SH",
            "quantity": 100,
            "price": 10.0,
            "market_snapshot": {"upper_limit": 10.0},
        }
    )

    assert reserved == 1_005.01


def test_ashare_position_budget_includes_worst_limit_and_fees_before_one_lot():
    blocked = {
        "positions": [
            {"ts_code": "600000.SH", "price": 75.0, "shares": 100, "weight": 0.15}
        ]
    }
    _apply_position_budgets(
        market="ashare",
        portfolio=blocked,
        order_meta={"600000.SH": {"price": 75.0, "weight": 0.15}},
        capital_plan={
            "enabled": True,
            "position_budget_by_symbol": {"600000.SH": 7_500.0},
        },
        capital=50_000.0,
    )
    assert blocked["positions"][0]["shares"] == 0
    assert blocked["positions"][0]["worst_case_gross_cny"] == 0.0

    allowed = {
        "positions": [
            {"ts_code": "600001.SH", "price": 68.0, "shares": 100, "weight": 0.15}
        ]
    }
    _apply_position_budgets(
        market="ashare",
        portfolio=allowed,
        order_meta={"600001.SH": {"price": 68.0, "weight": 0.15}},
        capital_plan={
            "enabled": True,
            "position_budget_by_symbol": {"600001.SH": 7_500.0},
        },
        capital=50_000.0,
    )
    assert allowed["positions"][0]["shares"] == 100
    assert allowed["positions"][0]["worst_case_gross_cny"] <= 7_500.0


class StubSimAdapter(MarketAdapter):
    def get_universe(self, date: str) -> list[str]:
        return ["AAA"]

    def get_market(self) -> str:
        return "unit"

    def map_symbol_to_reader(self, symbol: str) -> tuple[str, str]:
        return "unit", symbol

    def get_strategy_config(self) -> dict[str, object]:
        return {
            "portfolio_method": "conviction_weighted",
            "regime": "growth",
            "max_candidates": 1,
            "default_price": 10.0,
            "default_volatility": 0.20,
        }

    def get_shadow_account(self) -> str:
        return "unit_shadow"

    def get_sim_account(self) -> dict[str, object]:
        return {
            "account": "unit_sim",
            "sim_capital": 50000.0,
            "positions": [
                {"ts_code": "HELD", "weight": 0.03, "sector": "unit"},
            ],
        }


class StubReader:
    def get_bars_daily(
        self, market: str, symbol: str, start: object = None, end: object = None
    ) -> list[dict[str, float]]:
        return [{"close": 9.8}, {"close": 10.0}, {"close": 10.2}]


def _authority_position_evidence(
    trade_date: str,
    positions: list[dict[str, object]] | dict[str, object] | None = None,
) -> dict[str, object]:
    raw_positions: object = positions or []
    normalized, _, reason = normalize_ashare_positions(raw_positions)
    if normalized is None:
        raise AssertionError(reason)
    quantity_map = {str(row["ts_code"]): int(row["quantity"]) for row in normalized}
    fingerprint = canonical_sha256(normalized)
    checksum = canonical_sha256(
        {
            "authority_id": "ashare-capital-v1",
            "authority_generation": 1,
            "execution_lineage_id": "ashare-sim-fresh-20260712-v1",
            "positions": normalized,
            "trade_date": str(trade_date).replace("-", ""),
        }
    )
    return {
        "authority_id": "ashare-capital-v1",
        "authority_generation": 1,
        "execution_lineage_id": "ashare-sim-fresh-20260712-v1",
        "authority_checksum": checksum,
        "trade_date": str(trade_date).replace("-", ""),
        "position_count": len(normalized),
        "positions_fingerprint": fingerprint,
        "positions_quantity_by_risk_unit": quantity_map,
        "position_source_status": "ready",
    }


def _ashare_market_state(
    trade_date: str,
    *,
    positions: list[dict[str, object]] | dict[str, object] | None = None,
    **overrides: object,
) -> dict[str, object]:
    position_evidence = _authority_position_evidence(trade_date, positions)
    normalized_positions, _, _ = normalize_ashare_positions(positions or [])
    market_value = 0.0
    raw_rows = positions if isinstance(positions, list) else []
    for row in raw_rows:
        if not isinstance(row, dict):
            continue
        row_value = row.get("market_value")
        if isinstance(row_value, (int, float)) and not isinstance(row_value, bool):
            market_value += float(row_value)
        else:
            market_value += float(row.get("quantity") or 0) * float(
                row.get("last_price") or row.get("avg_price") or 0
            )
    market_value = round(market_value, 2)
    cash_balance = round(50_000.0 - market_value, 2)
    event_checksum = str(position_evidence["authority_checksum"])
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
        "cash_balance_cny": cash_balance,
        "positions_market_value_cny": market_value,
        "frozen_order_cash_cny": 0.0,
        "realized_pnl_cny": 0.0,
        "unrealized_pnl_cny": 0.0,
        "reserved_capital_cny": 0.0,
        "active_reservations_cny": 0.0,
        "available_to_reserve_cny": max(
            0.0, min(cash_balance, 45_000.0 - market_value)
        ),
        "stock_gross_exposure_limit_cny": 45_000.0,
        "single_name_cap_cny": 7_500.0,
        "capital_utilization_rate": round(market_value / 50_000.0, 8),
        "reconciled": True,
        "fresh": True,
        "trade_date": str(trade_date).replace("-", ""),
        "event_id": f"MCAP-{str(trade_date).replace('-', '')}-RECONCILED",
        "event_checksum": event_checksum,
        "checksum_status": "valid",
        "checksum_event_count": 2,
        "checksum_last": event_checksum,
        "execution_lineage_id": "ashare-sim-fresh-20260712-v1",
        "daily_mtm_change": 0.0,
        "daily_realized_pnl": 0.0,
        "max_daily_loss": 1_500.0,
        "consecutive_losses": 0,
        "max_consecutive_losses": 3,
        "high_water_equity": 50_000.0,
        "max_drawdown": 3_500.0,
        "real_trading_enabled": False,
        "positions_quantity_by_risk_unit": position_evidence[
            "positions_quantity_by_risk_unit"
        ],
        "position_count": len(normalized_positions or []),
        "positions_fingerprint": position_evidence["positions_fingerprint"],
    }
    state.update(overrides)
    return state


def _order_lineage() -> dict[str, object]:
    return build_execution_lineage(
        lineage_started_at="2026-07-12T00:00:00+08:00",
        point_in_time_as_of="2026-07-13T10:01:00+08:00",
    )


class SignalCardTPlusOneTest(unittest.TestCase):
    def test_ashare_sim_buy_signal_card_uses_next_trading_day_sellable_date(
        self,
    ) -> None:
        card = _build_signal_card(
            market="ashare",
            symbol="600030.SH",
            account="ashare_sim",
            date="20260709",
            order={
                "side": "buy",
                "quantity": 100,
                "price": 28.0,
                "candidate_pool_layer": "candidate",
                "execution_source": "ashare_candidate_layer",
            },
            risk={"approved": True},
            trade={"trade_id": "T1"},
            audit_id="AUDIT-T1",
            order_id="SIM-ashare-600030.SH-20260709-test",
            order_id_prefix="SIM-",
            capital_layer="simulated",
            account_type="simulated",
            direct_execution=True,
        )

        self.assertEqual(card["t_plus_1"]["sellable_from"], "2026-07-10")
        self.assertEqual(card["t_plus_1"]["sellable_date"], "2026-07-10")


class MultiCandidateSimAdapter(StubSimAdapter):
    def __init__(
        self,
        symbols: list[str],
        *,
        max_candidates: int = 3,
        score_universe_limit: int | None = None,
        max_portfolio_positions: int = 3,
        positions: list[dict[str, object]] | None = None,
        cash_available: float | None = None,
        strategy_positions: list[dict[str, object]] | None = None,
        strategy_cash_available: float | None = None,
        sample_adjustment: dict[str, object] | None = None,
        trade_date: str = "20260713",
    ) -> None:
        self.symbols = symbols
        self.max_candidates = max_candidates
        self.score_universe_limit = score_universe_limit or max_candidates
        self.max_portfolio_positions = max_portfolio_positions
        self.positions = positions or []
        self.cash_available = cash_available
        self.strategy_positions = strategy_positions
        self.strategy_cash_available = strategy_cash_available
        self.sample_adjustment = sample_adjustment
        self.trade_date = trade_date

    def get_universe(self, date: str) -> list[str]:
        return list(self.symbols)

    def get_market(self) -> str:
        return "ashare"

    def get_strategy_config(self) -> dict[str, object]:
        return {
            "portfolio_method": "conviction_weighted",
            "regime": "ashare_default",
            "max_candidates": self.max_candidates,
            "score_universe_limit": self.score_universe_limit,
            "max_portfolio_positions": self.max_portfolio_positions,
            "default_price": 0.0,
            "default_volatility": 0.20,
        }

    def get_sim_account(self) -> dict[str, object]:
        envelope = _authority_position_evidence(self.trade_date, self.positions)
        payload: dict[str, object] = {
            "account": "ashare_sim",
            "sim_capital": 50_000.0,
            "positions": list(self.positions),
            "source": "test_strategy_adapter",
            **envelope,
        }
        if self.cash_available is not None:
            payload["cash_available"] = self.cash_available
        if self.strategy_positions is not None:
            payload["strategy_positions"] = list(self.strategy_positions)
            strategy_evidence = _authority_position_evidence(
                self.trade_date, self.strategy_positions
            )
            payload["strategy_position_envelope"] = {
                "source": "test_strategy_position_snapshot",
                "positions": list(self.strategy_positions),
                **strategy_evidence,
            }
        if self.strategy_cash_available is not None:
            payload["strategy_cash_available"] = self.strategy_cash_available
        if self.sample_adjustment is not None:
            payload["capital_plan_sample_adjustment"] = dict(self.sample_adjustment)
        return payload


def _patch_shadow_paths(testcase: unittest.TestCase, tmp_path: Path) -> None:
    shadow_dir = tmp_path / "shadow"
    for name, value in (
        ("SHADOW_DIR", shadow_dir),
        ("SHADOW_TRADES", shadow_dir / "shadow_trades.jsonl"),
        ("SHADOW_POSITIONS", shadow_dir / "shadow_positions.json"),
        ("SHADOW_PNL", shadow_dir / "shadow_pnl.json"),
        ("SHADOW_LOCK", shadow_dir / ".shadow.lock"),
    ):
        patcher = patch.object(shadow_broker, name, value)
        patcher.start()
        testcase.addCleanup(patcher.stop)


def _patch_audit_paths(testcase: unittest.TestCase, tmp_path: Path) -> None:
    ledger_dir = tmp_path / "logs"
    for name, value in (
        ("LEDGER_DIR", ledger_dir),
        ("AUDIT_TRAIL", ledger_dir / "trade_audit_trail.jsonl"),
    ):
        patcher = patch.object(trade_audit_trail, name, value)
        patcher.start()
        testcase.addCleanup(patcher.stop)


class SimLoopTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        # macOS exposes /var as a symlink to /private/var.  Use the physical
        # temporary path so the production sample journal's parent-symlink
        # fail-closed guard is exercised without weakening it for tests.
        self.tmp_path = Path(self.tmpdir.name).resolve()
        _patch_shadow_paths(self, self.tmp_path)
        _patch_audit_paths(self, self.tmp_path)
        self._calendar_day_patcher = patch(
            "Ashare.t_plus_1._shared_calendar_is_trading_day",
            return_value=None,
        )
        self._calendar_day_patcher.start()
        self.addCleanup(self._calendar_day_patcher.stop)
        self._calendar_next_patcher = patch(
            "Ashare.t_plus_1._shared_calendar_next_trading_day",
            return_value=None,
        )
        self._calendar_next_patcher.start()
        self.addCleanup(self._calendar_next_patcher.stop)
        self._sim_executor_snapshot = dict(sim_executor_registry._SIM_EXECUTORS)
        self.addCleanup(self._restore_sim_executors)
        self.calls: list[str] = []
        self.risk_portfolios: list[dict[str, object]] = []
        self.review_requests: list[dict[str, object]] = []
        self.executed_orders: list[dict[str, object]] = []
        self.market_releases: list[dict[str, object]] = []
        self.market_realized_pnl: list[dict[str, object]] = []
        self._master_state_patcher = patch.object(
            market_capital,
            "load_market_capital_provider_state",
            side_effect=lambda market, trade_date: _ashare_market_state(trade_date),
        )
        self.master_state_loader = self._master_state_patcher.start()
        self.addCleanup(self._master_state_patcher.stop)
        self._master_verification_patcher = patch.object(
            market_capital,
            "verify_market_capital_reservation",
            side_effect=lambda market, **kwargs: {
                "verified": True,
                "reason": "reservation_verified",
                "reservation_id": kwargs["reservation_id"],
                "reference_id": kwargs["reference_id"],
                "market": market,
                "authority_id": kwargs["authority_id"],
                "authority_generation": kwargs["authority_generation"],
                "execution_lineage_id": kwargs["execution_lineage_id"],
                "risk_unit_key": kwargs["risk_unit_key"],
                "event_id": str(kwargs["reservation_id"]).replace("mres-", "mevt-", 1),
                "remaining_amount_cny": 100_000.0,
                "real_trading_enabled": False,
            },
        )
        self._master_verification_patcher.start()
        self.addCleanup(self._master_verification_patcher.stop)

        def reserve(market, request):
            self.assertEqual(market, "ashare")
            return market_capital.MarketCapitalReservationDecision(
                approved=True,
                reason="reserved",
                reservation_id=f"mres-{request.reference_id}",
                event_id=f"mevt-{request.reference_id}",
            )

        self._master_reserver_patcher = patch.object(
            market_capital,
            "reserve_market_capital",
            side_effect=reserve,
        )
        self.master_reserver = self._master_reserver_patcher.start()
        self.addCleanup(self._master_reserver_patcher.stop)

        def release(market, reservation_id, amount, reason, *, reference_id):
            self.assertEqual(market, "ashare")
            row = {
                "reservation_id": reservation_id,
                "amount_cny": amount,
                "reason": reason,
                "reference_id": reference_id,
            }
            self.market_releases.append(row)
            return {"status": "released", **row, "real_trading_enabled": False}

        self._market_releaser_patcher = patch.object(
            market_capital,
            "release_market_capital",
            side_effect=release,
        )
        self.market_releaser = self._market_releaser_patcher.start()
        self.addCleanup(self._market_releaser_patcher.stop)

        def record_pnl(market, **kwargs):
            self.assertEqual(market, "ashare")
            self.market_realized_pnl.append(dict(kwargs))
            return {"status": "recorded", **kwargs, "real_trading_enabled": False}

        self._master_pnl_patcher = patch.object(
            market_capital,
            "record_market_capital_realized_pnl",
            side_effect=record_pnl,
        )
        self.master_pnl_recorder = self._master_pnl_patcher.start()
        self.addCleanup(self._master_pnl_patcher.stop)
        self._master_outbox_patcher = patch.object(
            local_sim_ledger,
            "list_local_sim_market_capital_actions",
            return_value=[],
        )
        self.master_outbox = self._master_outbox_patcher.start()
        self.addCleanup(self._master_outbox_patcher.stop)
        self._capital_head_patcher = patch.object(
            orchestrator_module,
            "_capture_ashare_market_capital_head",
            return_value={
                "event_id": "captured-market-head",
                "checksum": "c" * 64,
            },
            create=True,
        )
        self.capital_head = self._capital_head_patcher.start()
        self.addCleanup(self._capital_head_patcher.stop)

        def replay_market_outbox():
            if not self.executed_orders:
                return {
                    "status": "replayed",
                    "pending_count": 0,
                    "action_count": 0,
                    "actions": [],
                    "real_trading_enabled": False,
                }
            order = self.executed_orders[-1]
            action_type = (
                "ashare_sell_commit"
                if str(order.get("side") or "").lower() == "sell"
                else "fill_commit"
            )
            return {
                "status": "replayed",
                "pending_count": 0,
                "action_count": 1,
                "actions": [
                    {
                        "action": action_type,
                        "reservation_id": order.get(
                            "market_capital_reservation_id", ""
                        ),
                        "idempotency_key": order.get("idempotency_key", ""),
                        "risk_unit_key": order.get("risk_unit_key", ""),
                        "status": "completed",
                        "last_result": {
                            "committed": True,
                            "status": "committed",
                            "reason": "fill_committed",
                        },
                    }
                ],
                "real_trading_enabled": False,
            }

        self._market_outbox_replay_patcher = patch.object(
            local_sim_ledger,
            "replay_local_sim_market_capital_outbox",
            side_effect=replay_market_outbox,
        )
        self.market_outbox_replay = self._market_outbox_replay_patcher.start()
        self.addCleanup(self._market_outbox_replay_patcher.stop)
        self._lineage_patcher = patch.object(
            local_sim_ledger,
            "build_local_sim_order_lineage",
            side_effect=lambda **_: _order_lineage(),
        )
        self.order_lineage = self._lineage_patcher.start()
        self.addCleanup(self._lineage_patcher.stop)
        self._lineage_manifest_patcher = patch.object(
            local_sim_ledger,
            "get_local_sim_execution_lineage_manifest",
            return_value={"status": "ready", **_order_lineage()},
        )
        self.lineage_manifest = self._lineage_manifest_patcher.start()
        self.addCleanup(self._lineage_manifest_patcher.stop)
        self._exploration_state_patcher = patch.object(
            local_sim_ledger,
            "get_local_sim_exploration_state",
            return_value={
                "new_position_count": 0,
                "open_exposure_cny": 0.0,
                "daily_realized_pnl_cny": 0.0,
                "daily_loss_cny": 0.0,
                "real_trading_enabled": False,
                "status": "ready",
            },
        )
        self.exploration_state = self._exploration_state_patcher.start()
        self.addCleanup(self._exploration_state_patcher.stop)
        self._local_trade_lookup_patcher = patch.object(
            local_sim_ledger,
            "get_local_sim_trade_by_idempotency",
            return_value=None,
        )
        self.local_trade_lookup = self._local_trade_lookup_patcher.start()
        self.addCleanup(self._local_trade_lookup_patcher.stop)

        def authoritative_account_view(account, trade_date, *, position_authority=None):
            payload = account if isinstance(account, dict) else {}
            positions = payload.get("strategy_positions")
            if not isinstance(positions, list):
                positions = payload.get("positions")
            if not isinstance(positions, list):
                positions = []
            raw_cash = payload.get(
                "strategy_cash_available",
                payload.get("cash_available", 50_000.0),
            )
            cash = min(50_000.0, max(0.0, float(raw_cash)))
            view = {
                "account": str(payload.get("account") or "ashare_sim"),
                "capital_cny": 50_000.0,
                "cash_available": cash,
                "positions": [dict(row) for row in positions if isinstance(row, dict)],
                "source": "test_server_local_authority",
                "trade_date": str(trade_date).replace("-", ""),
            }
            evidence = _authority_position_evidence(trade_date, positions)
            return {**view, **evidence}

        self._authoritative_account_patcher = patch.object(
            orchestrator_module,
            "_ashare_authoritative_account_view",
            side_effect=authoritative_account_view,
            create=True,
        )
        self.authoritative_account_view = self._authoritative_account_patcher.start()
        self.addCleanup(self._authoritative_account_patcher.stop)

    def _restore_sim_executors(self) -> None:
        sim_executor_registry._SIM_EXECUTORS.clear()
        sim_executor_registry._SIM_EXECUTORS.update(self._sim_executor_snapshot)

    def _use_authority_positions(
        self,
        positions: list[dict[str, object]],
        *,
        trade_date: str = "20260713",
    ) -> None:
        self.master_state_loader.side_effect = lambda market, requested_date: (
            _ashare_market_state(requested_date, positions=positions)
        )

    def _deps(self) -> OrchestratorDeps:
        def score_stock(
            market: str,
            symbol: str,
            data_reader: object = None,
            date: str | None = None,
        ) -> dict[str, object]:
            self.calls.append("screening")
            return {
                "combined": 0.72,
                "sector": "unit",
                "turnover_wan": 10000,
                "capital_layer": "simulated",
            }

        def build_pool(
            date: str,
            universe: list[str],
            market: str | None = None,
            reader: object | None = None,
        ) -> dict[str, list[str]]:
            self.calls.append("candidate_pool")
            return {
                "candidate": list(universe),
                "watch": [],
                "holdings": [],
                "universe": list(universe),
            }

        def debate(symbol: str, scores: dict[str, object]) -> dict[str, object]:
            self.calls.append("adversarial")
            self.assertEqual(scores["capital_layer"], "simulated")
            return {
                "ts_code": symbol,
                "belief_score": 0.70,
                "bull_case": "ok",
                "bear_case": "risk",
            }

        def risk_check(
            order: dict[str, object], portfolio: dict[str, object]
        ) -> dict[str, object]:
            self.calls.append("risk")
            self.risk_portfolios.append(portfolio)
            self.assertEqual(order["capital_layer"], "simulated")
            self.assertEqual(order["account_type"], "simulated")
            self.assertEqual(portfolio["capital_layer"], "simulated")
            self.assertEqual(portfolio["account_type"], "simulated")
            self.assertEqual(portfolio["positions"][0]["ts_code"], "HELD")
            return {
                "approved": True,
                "adjusted_weight": order["weight"],
                "adjustments": ["ok"],
                "reasons": [],
            }

        def construct(
            orders: list[dict[str, object]], capital: float, method: str, regime: str
        ) -> dict[str, object]:
            self.calls.append("portfolio")
            self.assertEqual(capital, 50000.0)
            return {
                "method": method,
                "capital": capital,
                "positions": [
                    {
                        "ts_code": order["ts_code"],
                        "weight": order["weight"],
                        "shares": 10,
                        "amount": 100.0,
                        "sector": "unit",
                        "price": 10.0,
                    }
                    for order in orders
                ],
                "total_weight": sum(float(order["weight"]) for order in orders),
                "cash_weight": 0.95,
            }

        def size_position(belief_score: float, volatility: float, regime: str) -> float:
            self.calls.append("position_sizer")
            return 0.05

        def record_shadow(order: dict[str, object], account: str) -> dict[str, object]:
            raise AssertionError("run_sim_loop must not call record_shadow")

        def review(
            date: str, session: str = "close", capital_layer: str = "shadow"
        ) -> dict[str, object]:
            self.calls.append("review")
            self.review_requests.append(
                {"date": date, "session": session, "capital_layer": capital_layer}
            )
            return {
                "session": session,
                "trade_date": date,
                "capital_layer": capital_layer,
            }

        def execute_sim_order(
            order: dict[str, object], account: object = None
        ) -> dict[str, object]:
            self.calls.append("sim_broker")
            self.executed_orders.append(order)
            self.assertTrue(str(order["order_id"]).startswith("SIM-"))
            self.assertEqual(order["capital_layer"], "simulated")
            self.assertEqual(order["account_type"], "simulated")
            filled_price = 10.05
            filled_quantity = int(order["quantity"])
            amount = round(filled_price * filled_quantity, 2)
            retained = round(amount + max(amount * 0.00025, 5.0), 2)
            return {
                "order_id": order["order_id"],
                "status": "filled",
                "filled_price": filled_price,
                "filled_quantity": filled_quantity,
                "fill_time": "2026-07-13T10:00:00+08:00",
                "raw_response": {
                    "local_sim_backup": {
                        "status": "filled",
                        "recorded": True,
                        "capital_scope": order.get("capital_scope", "strategy"),
                        "capital_authority_id": order.get("capital_authority_id"),
                        "authority_generation": order.get("authority_generation"),
                        "execution_lineage_id": order.get("execution_lineage_id"),
                        "execution_lineage_sha256": order.get(
                            "execution_lineage_sha256"
                        ),
                        "point_in_time_as_of": order.get("point_in_time_as_of"),
                        "filled_qty": filled_quantity,
                        "avg_price": filled_price,
                        "net_amount": retained,
                        "market_capital_reference_id": order.get(
                            "market_capital_reference_id", ""
                        ),
                        "market_capital_reservation_id": order.get(
                            "market_capital_reservation_id", ""
                        ),
                        "market_capital_event_id": order.get(
                            "market_capital_event_id", ""
                        ),
                        "market_capital_risk_unit_key": order.get(
                            "market_capital_risk_unit_key", ""
                        ),
                        "market_capital_required": order.get(
                            "market_capital_required", False
                        ),
                        "market_capital_expected_head_event_id": order.get(
                            "market_capital_expected_head_event_id", ""
                        ),
                        "market_capital_expected_head_checksum": order.get(
                            "market_capital_expected_head_checksum", ""
                        ),
                        "market_reserved_gross_cny": order.get(
                            "market_reserved_gross_cny", 0.0
                        ),
                        "market_retained_gross_cny": retained,
                        "market_release_allocations": [],
                        "realized_pnl_cny": 0.0,
                        "fill_price_source_class": "market_data",
                        "fill_evidence": {
                            "execution_evidence_class": "verified_5min_market_data",
                            "fill_price_source": "sharedsignals_api_realtime_5min",
                            "fill_price_source_class": "market_data",
                            "bar_time": "2026-07-13T10:00:00+08:00",
                            "bar_volume": 100_000.0,
                        },
                    }
                },
            }

        return OrchestratorDeps(
            score_stock=score_stock,
            build_pool=build_pool,
            debate=debate,
            risk_check=risk_check,
            construct=construct,
            size_position=size_position,
            record_shadow=record_shadow,
            run_review=review,
            record_audit_event=trade_audit_trail.record_event,
            execute_sim_order=execute_sim_order,
        )

    def _multi_candidate_deps(self) -> OrchestratorDeps:
        deps = self._deps()

        def risk_check(
            order: dict[str, object], portfolio: dict[str, object]
        ) -> dict[str, object]:
            self.calls.append("risk")
            return {
                "approved": True,
                "adjusted_weight": order["weight"],
                "adjustments": ["ok"],
                "reasons": [],
            }

        def construct(
            orders: list[dict[str, object]], capital: float, method: str, regime: str
        ) -> dict[str, object]:
            self.calls.append("portfolio")
            return {
                "method": method,
                "capital": capital,
                "positions": [
                    {
                        "ts_code": order["ts_code"],
                        "weight": order["weight"],
                        "shares": 100,
                        "amount": 1000.0,
                        "sector": "unit",
                        "price": 10.0,
                    }
                    for order in orders
                ],
                "total_weight": sum(float(order["weight"]) for order in orders),
                "cash_weight": 0.95,
            }

        deps.risk_check = risk_check
        deps.construct = construct
        return deps

    def _ordered_sample_deps(self) -> OrchestratorDeps:
        deps = self._multi_candidate_deps()
        execute = deps.execute_sim_order
        self.assertIsNotNone(execute)

        def execute_after_prediction(
            order: dict[str, object], account: object = None
        ) -> dict[str, object]:
            receipt = dict(execute(order, account))  # type: ignore[misc]
            # The broker fixture represents a provider-confirmed execution,
            # so persist its real event and receipt clocks explicitly.  The
            # sample pipeline must never infer these from prediction/as-of.
            execution_at = "2026-07-13T10:02:00+08:00"
            receipt.update(
                {
                    "fill_time": execution_at,
                    "filled_at": execution_at,
                    "available_at": execution_at,
                    "ingested_at": execution_at,
                    "retrieved_as_of": execution_at,
                }
            )
            return receipt

        deps.execute_sim_order = execute_after_prediction
        return deps

    def _high_score_deps(self) -> OrchestratorDeps:
        deps = self._multi_candidate_deps()

        def score_universe(
            date: str,
            universe: list[str],
            data_reader: object = None,
            market: str = "ashare",
        ) -> list[tuple[str, dict[str, object]]]:
            return [
                (
                    symbol,
                    {
                        "combined": 0.82,
                        "sector": "unit",
                        "turnover_wan": 10000,
                        "capital_layer": "simulated",
                    },
                )
                for symbol in universe
            ]

        deps.score_universe = score_universe
        return deps

    @contextmanager
    def _approved_expansion_evidence(self, trade_date: str):
        decision = {
            "report_type": "ashare_evolution_decision_v2",
            "evidence_source": "sample_journal_kpi",
            "evidence_trade_date": trade_date,
            "trade_date": trade_date,
            "authority_scope": {
                "capital_authority_id": "ashare-capital-v1",
                "authority_generation": 1,
                "execution_lineage_id": "ashare-sim-fresh-20260712-v1",
            },
            "evidence_usable": True,
            "state": "manual_review_candidate",
            "recommended_action": "manual_review_only",
            "reasons": ["automatic_risk_expansion_disabled"],
            "policy": {
                "observation_enabled": True,
                "safe_exploration_enabled": True,
                "automatic_promotion_enabled": False,
                "automatic_risk_expansion_enabled": False,
            },
            "metrics": {"completed_round_trip_count": 20},
        }
        with patch(
            "Ashare.evolution_controller.load_latest_decision",
            return_value=decision,
        ):
            yield

    def _run_one_ashare_buy(
        self,
        *,
        deps: OrchestratorDeps | None = None,
        signals_name: str = "signals_market_buy",
    ) -> dict[str, object]:
        with self._approved_expansion_evidence("20260713"):
            return run_sim_loop(
                MultiCandidateSimAdapter(
                    ["300418.SZ"],
                    max_candidates=1,
                    score_universe_limit=1,
                    max_portfolio_positions=1,
                ),
                "20260713",
                StubReader(),
                deps=deps or self._multi_candidate_deps(),
                signals_dir=self.tmp_path / signals_name,
            )

    def test_ashare_strategy_buy_reserves_market_capital_before_executor_and_propagates_lineage(
        self,
    ) -> None:
        result = self._run_one_ashare_buy()

        self.assertEqual(result["filled_count"], 1, result)
        self.assertEqual(result["authoritative_account_view"]["capital_cny"], 50_000.0)
        self.assertEqual(result["adapter_account_diagnostics"]["capital_cny"], 50_000.0)
        self.assertFalse(result["adapter_account_diagnostics"]["authoritative"])
        self.assertEqual(self.master_reserver.call_count, 1)
        request = self.master_reserver.call_args.args[1]
        order = self.executed_orders[0]
        self.assertEqual(request.market, "ashare")
        self.assertEqual(request.authority_id, "ashare-capital-v1")
        self.assertEqual(request.authority_generation, 1)
        self.assertEqual(request.execution_lineage_id, order["execution_lineage_id"])
        self.assertEqual(request.risk_unit_key, order["ts_code"])
        self.assertEqual(request.lineage_sha256, order["execution_lineage_sha256"])
        self.assertEqual(
            request.reference_id,
            f"AMCAP:1:{order['execution_lineage_id']}:{order['idempotency_key']}",
        )
        self.assertEqual(order["capital_scope"], "strategy")
        self.assertIs(order["market_capital_required"], True)
        self.assertEqual(order["market_capital_reference_id"], request.reference_id)
        self.assertEqual(
            order["market_capital_reservation_id"],
            f"mres-{request.reference_id}",
        )
        self.assertEqual(
            order["market_capital_expected_head_event_id"],
            "captured-market-head",
        )
        self.assertEqual(order["market_capital_expected_head_checksum"], "c" * 64)
        self.assertGreater(float(order["market_reserved_gross_cny"]), 1_005.0)
        self.assertEqual(self.market_releases, [])
        self.assertGreaterEqual(self.market_outbox_replay.call_count, 1)
        filled_card = next(
            (self.tmp_path / "signals_market_buy" / "filled").glob("SIM-*.json")
        )
        payload = read_json(filled_card)
        self.assertEqual(
            payload["market_capital_reservation_id"],
            order["market_capital_reservation_id"],
        )
        self.assertEqual(payload["capital_scope"], "strategy")
        self.assertTrue(payload["execution_eligible"])
        self.assertEqual(
            payload["market_capital_settlement"]["status"],
            "fill_committed",
        )
        self.assertEqual(
            result["post_execution_capital_plan_refresh"]["status"], "written"
        )
        for stage in (
            "capital.ashare_post_execution_position_authority.market_capital_before",
            "capital.ashare_post_execution_position_authority.adapter_position_source",
            "capital.ashare_post_execution_position_authority.server_local_position_source",
            "capital.ashare_post_execution_position_authority.market_capital_after",
        ):
            self.assertIn(stage, result["stage_calls"])
        post_refresh_rows = [
            json.loads(line)
            for line in Path(result["post_execution_capital_plan_refresh"]["path"])
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]
        self.assertEqual(
            post_refresh_rows[-1]["capital_plan"]["cash_source"],
            "market_capital_authority_post_execution",
        )
        self.assertEqual(
            post_refresh_rows[-1]["capital_plan"]["capital_authority_checksum"],
            result["ashare_position_authority"]["authority_checksum"],
        )

    def test_ashare_fill_commit_pending_preserves_observation_but_blocks_execution_eligibility(
        self,
    ) -> None:
        def replay_pending_market_outbox():
            order = self.executed_orders[-1]
            return {
                "status": "pending",
                "pending_count": 1,
                "action_count": 1,
                "actions": [
                    {
                        "action": "fill_commit",
                        "reservation_id": order["market_capital_reservation_id"],
                        "idempotency_key": order["idempotency_key"],
                        "status": "error",
                        "last_result": {
                            "committed": False,
                            "status": "rejected",
                            "reason": "ledger_head_cas_mismatch",
                        },
                    }
                ],
                "real_trading_enabled": False,
            }

        self.market_outbox_replay.side_effect = replay_pending_market_outbox

        result = self._run_one_ashare_buy(signals_name="signals_fill_commit_pending")

        receipt = result["records"][0]["receipt"]
        self.assertEqual(receipt["status"], "filled")
        self.assertFalse(receipt["execution_eligible"])
        self.assertEqual(receipt["reason"], "market_capital_fill_commit_pending")
        self.assertEqual(
            receipt["market_capital_settlement"]["status"],
            "fill_commit_pending",
        )
        self.assertTrue(receipt["market_capital_settlement"]["reservation_retained"])
        self.assertEqual(
            receipt["market_capital_settlement"]["commit_result"]["reason"],
            "ledger_head_cas_mismatch",
        )
        self.assertEqual(self.market_releases, [])

    def test_ashare_open_partial_commit_keeps_residual_reservation_active(self) -> None:
        from shared.orchestrator import _settle_ashare_market_receipt

        order = {
            **_order_lineage(),
            "order_id": "SIM-OPEN-PARTIAL",
            "idempotency_key": "SIM:ashare:ashare_sim:20260713:300418.SZ:buy:partial",
            "ts_code": "300418.SZ",
            "risk_unit_key": "300418.SZ",
            "price": 10.0,
            "market_capital_reference_id": "AMCAP:1:partial",
            "market_capital_reservation_id": "mres-partial",
            "market_capital_event_id": "mevt-partial",
            "market_reserved_gross_cny": 1_105.0,
        }
        receipt = {
            "order_id": order["order_id"],
            "status": "partial",
            "filled_qty": 100,
            "avg_price": 10.0,
            "raw_response": {
                "local_sim_backup": {
                    "recorded": True,
                    "status": "partial",
                    "quantity": 100,
                    "filled_price": 10.0,
                    "net_amount": 1_005.0,
                    "commission": 5.0,
                    "stamp_duty": 0.0,
                    "partial_terminal": False,
                    "capital_authority_id": order["capital_authority_id"],
                    "authority_generation": order["authority_generation"],
                    "execution_lineage_id": order["execution_lineage_id"],
                    "market_capital_reference_id": order["market_capital_reference_id"],
                    "market_capital_reservation_id": order[
                        "market_capital_reservation_id"
                    ],
                    "market_capital_event_id": order["market_capital_event_id"],
                    "fill_price_source_class": "market_data",
                    "fill_evidence": {
                        "execution_evidence_class": "verified_5min_market_data",
                        "fill_price_source": "sharedsignals_api_realtime_5min",
                        "fill_price_source_class": "market_data",
                        "quote_price": 10.0,
                        "bar_time": "2026-07-13T10:00:00+08:00",
                        "bar_volume": 100_000.0,
                    },
                }
            },
        }
        self.market_outbox_replay.side_effect = None
        self.market_outbox_replay.return_value = {
            "status": "replayed",
            "pending_count": 0,
            "action_count": 1,
            "actions": [
                {
                    "action": "fill_commit",
                    "reservation_id": "mres-partial",
                    "idempotency_key": order["idempotency_key"],
                    "status": "completed",
                    "last_result": {
                        "committed": True,
                        "status": "committed",
                        "reason": "fill_committed",
                    },
                }
            ],
            "real_trading_enabled": False,
        }

        settled_receipt, settlement = _settle_ashare_market_receipt(
            order,
            receipt,
            "ashare_sim",
        )

        self.assertEqual(settlement["status"], "fill_committed")
        self.assertTrue(settlement["reservation_retained"])
        self.assertFalse(settlement["terminal"])
        self.assertFalse(settled_receipt["execution_eligible"])

        deps = self._multi_candidate_deps()
        base_construct = deps.construct
        base_execute = deps.execute_sim_order
        self.assertIsNotNone(base_construct)
        self.assertIsNotNone(base_execute)

        def construct_partial_order(orders, capital, method, regime):
            portfolio = base_construct(orders, capital, method, regime)
            portfolio["positions"][0]["shares"] = 200
            portfolio["positions"][0]["amount"] = 2_000.0
            return portfolio

        def execute_partial_order(order, account=None):
            partial_receipt = base_execute(order, account)
            backup = partial_receipt["raw_response"]["local_sim_backup"]
            partial_receipt.update(
                {
                    "status": "partial",
                    "filled_quantity": 100,
                    "filled_qty": 100,
                }
            )
            backup.update(
                {
                    "status": "partial",
                    "quantity": 100,
                    "filled_qty": 100,
                    "net_amount": 1_010.0,
                    "market_retained_gross_cny": 1_010.0,
                    "partial_terminal": False,
                }
            )
            return partial_receipt

        def replay_partial_outbox():
            executed_order = self.executed_orders[-1]
            return {
                "status": "replayed",
                "pending_count": 0,
                "action_count": 1,
                "actions": [
                    {
                        "action": "fill_commit",
                        "reservation_id": executed_order[
                            "market_capital_reservation_id"
                        ],
                        "idempotency_key": executed_order["idempotency_key"],
                        "status": "completed",
                        "last_result": {
                            "committed": True,
                            "status": "committed",
                            "reason": "fill_committed",
                        },
                    }
                ],
                "real_trading_enabled": False,
            }

        deps.construct = construct_partial_order
        deps.execute_sim_order = execute_partial_order
        self.market_outbox_replay.side_effect = replay_partial_outbox
        authority_resolver = (
            orchestrator_module._resolve_ashare_position_authority_for_entry
        )
        with patch.object(
            orchestrator_module,
            "_resolve_ashare_position_authority_for_entry",
            wraps=authority_resolver,
        ) as resolve_authority:
            result = self._run_one_ashare_buy(
                deps=deps,
                signals_name="signals_partial_post_execution_authority",
            )

        self.assertEqual(result["filled_count"], 0, result)
        self.assertEqual(result["partial_count"], 1, result)
        self.assertEqual(resolve_authority.call_count, 1)
        self.assertEqual(
            result["post_execution_capital_plan_refresh"]["status"], "written"
        )
        self.assertIn(
            "capital.ashare_post_execution_position_authority.market_capital_before",
            result["stage_calls"],
        )

    def test_five_percent_drawdown_derisks_but_still_allows_small_sim_sample(
        self,
    ) -> None:
        self.master_state_loader.side_effect = lambda market, trade_date: (
            _ashare_market_state(
                trade_date,
                equity_cny=47_500.0,
                cash_balance_cny=47_500.0,
                realized_pnl_cny=-2_500.0,
                high_water_equity=50_000.0,
            )
        )

        result = self._run_one_ashare_buy(signals_name="signals_master_drawdown_derisk")

        self.assertEqual(result["filled_count"], 1, result)
        self.assertTrue(result["capital_plan"]["risk_tightening_active"])
        self.assertEqual(result["capital_plan"]["risk_multiplier"], 0.75)
        self.assertTrue(result["capital_plan"]["new_risk_allowed"])
        request = self.master_reserver.call_args.args[1]
        self.assertLessEqual(request.worst_case_amount_cny, 5_625.0)

    def test_ashare_buy_fails_closed_when_market_capital_unavailable_without_executor(
        self,
    ) -> None:
        self.master_state_loader.side_effect = lambda market, trade_date: None

        result = self._run_one_ashare_buy(signals_name="signals_master_missing")

        self.assertEqual(self.executed_orders, [])
        self.assertEqual(self.master_reserver.call_count, 0)
        self.assertEqual(result["filled_count"], 0)
        self.assertEqual(result["failed_count"], 0)
        self.assertEqual(result["records"], [])
        self.assertEqual(
            result["ashare_capital_state_reason"],
            "ashare_capital_unavailable",
        )
        self.assertGreaterEqual(
            result["sample_pipeline"]["observation"]["prediction_count"], 1
        )

    def test_ashare_terminal_reject_without_local_trade_releases_full_reservation(
        self,
    ) -> None:
        deps = self._multi_candidate_deps()

        def reject(order, account=None):
            self.executed_orders.append(dict(order))
            return {
                "order_id": order["order_id"],
                "status": "rejected",
                "reason": "liquidity_gate",
                "filled_qty": 0,
                "avg_price": 0.0,
            }

        deps.execute_sim_order = reject
        result = self._run_one_ashare_buy(
            deps=deps,
            signals_name="signals_master_terminal_reject",
        )

        order = self.executed_orders[0]
        self.assertEqual(result["failed_count"], 1)
        self.assertEqual(len(self.market_releases), 1)
        self.assertEqual(
            self.market_releases[0]["amount_cny"],
            order["market_reserved_gross_cny"],
        )
        self.assertEqual(
            self.market_releases[0]["reason"],
            "ashare_terminal_without_fill",
        )

    def test_ashare_pending_keeps_reservation_and_persists_lineage(self) -> None:
        deps = self._multi_candidate_deps()

        def pending(order, account=None):
            self.executed_orders.append(dict(order))
            return {
                "order_id": order["order_id"],
                "status": "pending",
                "filled_qty": 0,
                "avg_price": 0.0,
                "raw_response": {
                    "mode": "mini_webhook_sent",
                    "webhook": {"success": True},
                },
            }

        deps.execute_sim_order = pending
        result = self._run_one_ashare_buy(
            deps=deps,
            signals_name="signals_master_pending",
        )

        self.assertEqual(result["pending_count"], 1)
        self.assertEqual(self.market_releases, [])
        order = self.executed_orders[0]
        pending_card = result["records"][0]["signal_result"]["pending_signal"][
            "signal_card"
        ]
        self.assertEqual(
            pending_card["market_capital_reservation_id"],
            order["market_capital_reservation_id"],
        )

    def test_closed_market_reservation_recovers_committed_local_trade_without_executor(
        self,
    ) -> None:
        def closed(market, request):
            return market_capital.MarketCapitalReservationDecision(
                approved=False,
                reason="reservation_closed",
                reservation_id="mres-existing",
                event_id="mevt-existing",
            )

        self.master_reserver.side_effect = closed
        self.local_trade_lookup.return_value = {
            "trade_id": "LSIM-existing",
            "idempotency_key": "SIM:ashare:ashare_sim:20260713:300418.SZ:buy",
            "status": "filled",
            "quantity": 100,
            "filled_price": 10.0,
            "net_amount": 1_005.0,
            "capital_scope": "strategy",
            "capital_authority_id": "ashare-capital-v1",
            "authority_generation": 1,
            "execution_lineage_id": "ashare-sim-fresh-20260712-v1",
            "execution_lineage_sha256": _order_lineage()["execution_lineage_sha256"],
            "market_capital_required": True,
            "market_capital_reference_id": "AMCAP:1:ashare-sim-fresh-20260712-v1:SIM:ashare:ashare_sim:20260713:300418.SZ:buy",
            "market_capital_reservation_id": "mres-existing",
            "market_capital_event_id": "mevt-existing",
            "market_capital_risk_unit_key": "300418.SZ",
            "market_reserved_gross_cny": 1_105.0,
            "market_retained_gross_cny": 0.0,
            "market_release_allocations": [],
            "market_capital_expected_head_event_id": "captured-market-head",
            "market_capital_expected_head_checksum": "c" * 64,
            "market_capital_source_sha256": "a" * 64,
            "market_capital_receipt_sha256": "b" * 64,
            "trade_sha256": "d" * 64,
            "realized_pnl_cny": 0.0,
            "fill_price_source_class": "market_data",
            "fill_evidence": {
                "execution_evidence_class": "verified_5min_market_data",
                "fill_price_source": "sharedsignals_api_realtime_5min",
                "fill_price_source_class": "market_data",
                "bar_time": "2026-07-13T10:00:00+08:00",
                "bar_volume": 100_000.0,
            },
        }

        def replay_recovered_fill_commit():
            return {
                "status": "replayed",
                "pending_count": 0,
                "action_count": 1,
                "actions": [
                    {
                        "action": "fill_commit",
                        "reservation_id": "mres-existing",
                        "idempotency_key": "SIM:ashare:ashare_sim:20260713:300418.SZ:buy",
                        "status": "completed",
                        "last_result": {
                            "committed": True,
                            "status": "idempotent",
                            "reason": "fill_already_committed",
                        },
                    }
                ],
                "real_trading_enabled": False,
            }

        self.market_outbox_replay.side_effect = replay_recovered_fill_commit
        deps = self._multi_candidate_deps()

        def must_not_execute(order, account=None):
            raise AssertionError(
                "idempotent reservation with local fill must not execute"
            )

        deps.execute_sim_order = must_not_execute
        result = self._run_one_ashare_buy(
            deps=deps,
            signals_name="signals_master_recovery",
        )

        self.assertEqual(result["filled_count"], 1)
        self.assertEqual(self.executed_orders, [])
        self.assertEqual(
            result["records"][0]["receipt"]["recovery_source"],
            "server_local_trade_log",
        )
        self.assertEqual(
            result["records"][0]["receipt"]["market_capital_settlement"]["status"],
            "fill_committed",
        )

    def test_idempotent_pending_reservation_does_not_require_original_event_as_current_head(
        self,
    ) -> None:
        def idempotent(market, request):
            return market_capital.MarketCapitalReservationDecision(
                approved=True,
                reason="idempotent_reservation",
                reservation_id="mres-existing",
                event_id="mevt-existing",
            )

        self.master_reserver.side_effect = idempotent
        self.capital_head.side_effect = AssertionError(
            "idempotent reservation must not recapture the original event as head"
        )

        result = self._run_one_ashare_buy(
            signals_name="signals_idempotent_pending_reservation",
        )

        self.assertEqual(result["pending_count"], 1)
        self.assertEqual(self.executed_orders, [])
        receipt = result["records"][0]["receipt"]
        self.assertEqual(receipt["status"], "pending")
        self.assertEqual(
            receipt["reason"],
            "idempotent_reservation_reconciliation_required",
        )
        self.assertEqual(
            receipt["market_capital_gate"]["reason"],
            "idempotent_reservation",
        )

    def test_ashare_rebalance_does_not_read_positions_without_market_authority(
        self,
    ) -> None:
        self.master_state_loader.side_effect = lambda market, trade_date: None
        deps = self._multi_candidate_deps()
        positions = [
            {
                "ts_code": "000010.SZ",
                "quantity": 100,
                "sellable_quantity": 100,
                "avg_price": 12.0,
                "last_price": 10.0,
                "weight": 0.02,
            }
        ]

        adapter = MultiCandidateSimAdapter(
            ["AAA"],
            max_candidates=1,
            score_universe_limit=1,
            max_portfolio_positions=3,
            positions=positions,
        )
        with patch.object(
            adapter, "get_sim_account", wraps=adapter.get_sim_account
        ) as account_loader:
            result = run_sim_loop(
                adapter,
                "20260713",
                StubReader(),
                deps=deps,
                signals_dir=self.tmp_path / "signals_sell_without_master_state",
            )

        sell_orders = [row for row in self.executed_orders if row["side"] == "sell"]
        buy_orders = [row for row in self.executed_orders if row["side"] == "buy"]
        account_loader.assert_not_called()
        self.assertEqual(sell_orders, [])
        self.assertEqual(buy_orders, [])
        self.assertEqual(self.master_reserver.call_count, 0)
        self.assertEqual(
            result["ashare_capital_state_reason"], "ashare_capital_unavailable"
        )
        self.assertEqual(result["filled_count"], 0)
        self.assertEqual(result["rebalance"]["status"], "blocked")
        self.assertIsNone(result["capital_plan"]["existing_position_count"])

    def test_ashare_market_outbox_replays_release_and_realized_pnl_idempotently(
        self,
    ) -> None:
        from shared.orchestrator import _dispatch_ashare_market_outbox

        replay_result = {
            "status": "replayed",
            "pending_count": 0,
            "action_count": 2,
            "actions": [
                {
                    "action": "release",
                    "status": "completed",
                    "last_result": {"status": "idempotent_release"},
                },
                {
                    "action": "realized_pnl",
                    "status": "completed",
                    "last_result": {"status": "idempotent_realized_pnl"},
                },
            ],
            "real_trading_enabled": False,
        }
        self.market_outbox_replay.side_effect = None
        self.market_outbox_replay.return_value = replay_result

        first = _dispatch_ashare_market_outbox("ashare_sim")
        second = _dispatch_ashare_market_outbox("ashare_sim")

        self.assertEqual(first["status"], "replayed")
        self.assertEqual(second["status"], "replayed")
        self.assertEqual(first["account"], "ashare_sim")
        self.assertEqual(second["account"], "ashare_sim")
        self.assertEqual(self.market_outbox_replay.call_count, 2)
        self.assertEqual(self.market_releases, [])
        self.assertEqual(self.market_realized_pnl, [])

    def test_ashare_market_validator_never_relaxes_loss_streak_or_drawdown_gates(
        self,
    ) -> None:
        from shared.orchestrator import _validate_ashare_market_capital_state

        base = _ashare_market_state("20260712")
        cases = (
            ({"max_daily_loss": 50_000.0}, "ashare_capital_policy_mismatch"),
            ({"max_consecutive_losses": 999}, "ashare_capital_policy_mismatch"),
            ({"max_drawdown": 50_000.0}, "ashare_capital_policy_mismatch"),
            ({"daily_mtm_change": -1_500.0}, "ashare_capital_daily_loss_pause"),
            ({"consecutive_losses": 3}, "ashare_capital_consecutive_loss_pause"),
            (
                {
                    "equity_cny": 46_500.0,
                    "cash_balance_cny": 46_500.0,
                    "realized_pnl_cny": -3_500.0,
                    "high_water_equity": 50_000.0,
                },
                "ashare_capital_drawdown_halt",
            ),
        )

        for changes, expected in cases:
            with self.subTest(expected=expected):
                state, reason = _validate_ashare_market_capital_state(
                    {**base, **changes},
                    "20260712",
                )
                self.assertIsNone(state)
                self.assertEqual(reason, expected)

        tightened, reason = _validate_ashare_market_capital_state(
            {
                **base,
                "equity_cny": 47_500.0,
                "cash_balance_cny": 47_500.0,
                "realized_pnl_cny": -2_500.0,
            },
            "20260712",
        )
        self.assertIsNotNone(tightened)
        self.assertEqual(reason, "approved_drawdown_tightened")
        self.assertTrue(tightened["drawdown_tightened"])
        self.assertTrue(tightened["new_risk_allowed"])
        self.assertEqual(tightened["risk_multiplier"], 0.75)

    def test_authoritative_ashare_account_view_rejects_parallel_account_name(
        self,
    ) -> None:
        with self.assertRaisesRegex(
            RuntimeError,
            "ashare_authoritative_account_must_be_ashare_sim",
        ):
            _ashare_authoritative_account_view(
                {"account": "aggressive_parallel_50k"},
                "20260713",
            )

    def test_authoritative_ashare_account_view_reads_local_ledger_not_adapter_balances(
        self,
    ) -> None:
        source_positions = [
            {
                "ts_code": "600000.SH",
                "quantity": 600,
                "market_value": 6_480.0,
                "last_price": 10.80,
            }
        ]
        source_evidence = _authority_position_evidence("20260713", source_positions)
        with (
            patch.object(
                local_sim_ledger,
                "get_local_sim_account_snapshot",
                return_value={
                    "status": "ready",
                    "source": "test_local_snapshot",
                    "capital_authority_id": "ashare-capital-v1",
                    "authority_generation": 1,
                    "execution_lineage_id": "ashare-sim-fresh-20260712-v1",
                    "real_trading_enabled": False,
                    "cash_available": 43_520.0,
                    "positions": {
                        "600000.SH": {
                            "quantity": 600,
                            "sellable_quantity": 500,
                        }
                    },
                    **source_evidence,
                },
            ),
            patch.object(
                local_sim_ledger,
                "get_local_sim_pnl",
                return_value={
                    "status": "ready",
                    "source": "test_local_pnl",
                    "capital_authority_id": "ashare-capital-v1",
                    "authority_generation": 1,
                    "execution_lineage_id": "ashare-sim-fresh-20260712-v1",
                    "real_trading_enabled": False,
                    "positions": {
                        "600000.SH": {
                            "quantity": 600,
                            "avg_cost": 10.72,
                            "last_price": 10.80,
                            "mark_price": 10.80,
                            "market_value": 6_480.0,
                        }
                    },
                    **source_evidence,
                },
            ),
        ):
            view = _ashare_authoritative_account_view(
                {
                    "account": "ashare_sim",
                    "sim_capital": 200_000.0,
                    "cash_available": 150_000.0,
                    "positions": [{"ts_code": "FAKE", "quantity": 1}],
                },
                "20260713",
            )

        self.assertEqual(view["capital_cny"], 50_000.0)
        self.assertEqual(view["cash_available"], 43_520.0)
        self.assertEqual([row["ts_code"] for row in view["positions"]], ["600000.SH"])
        self.assertEqual(view["positions"][0]["sellable_quantity"], 500)
        self.assertEqual(view["source"], "server_local_sim_ledger")
        self.assertEqual(view["capital_authority_id"], "ashare-capital-v1")
        self.assertEqual(view["authority_generation"], 1)
        self.assertEqual(
            view["execution_lineage_id"],
            "ashare-sim-fresh-20260712-v1",
        )

    def test_authoritative_ashare_account_view_rejects_missing_fresh_lineage(
        self,
    ) -> None:
        with (
            patch.object(
                local_sim_ledger,
                "get_local_sim_account_snapshot",
                return_value={
                    "status": "execution_lineage_unavailable",
                    "cash_available": None,
                    "positions": {},
                    "real_trading_enabled": False,
                },
            ),
            patch.object(
                local_sim_ledger,
                "get_local_sim_pnl",
                return_value={
                    "status": "execution_lineage_unavailable",
                    "cash_available": None,
                    "positions": {},
                    "real_trading_enabled": False,
                },
            ),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "ashare_fresh_execution_lineage_unavailable",
            ):
                _ashare_authoritative_account_view(
                    {"account": "ashare_sim"},
                    "20260713",
                )

    def test_run_sim_loop_fills_signal_audit_and_review_as_simulated(self) -> None:
        result = run_sim_loop(
            StubSimAdapter(),
            "20260713",
            StubReader(),
            deps=self._deps(),
            signals_dir=self.tmp_path / "signals",
        )

        self.assertEqual(result["state"], "ok")
        self.assertEqual(result["capital_layer"], "simulated")
        self.assertEqual(result["account_type"], "simulated")
        self.assertEqual(result["account"], "unit_sim")
        self.assertEqual(result["filled_count"], 1)
        self.assertEqual(result["failed_count"], 0)
        self.assertEqual(result["pending_count"], 0)
        self.assertEqual(
            result["portfolio"]["existing_positions"][0]["ts_code"], "HELD"
        )
        for expected in (
            "screening",
            "candidate_pool",
            "adversarial",
            "risk",
            "position_sizer",
            "portfolio",
            "sim_broker",
            "review",
        ):
            self.assertIn(expected, self.calls)

        filled_files = list((self.tmp_path / "signals" / "filled").glob("SIM-*.json"))
        self.assertEqual(len(filled_files), 1)
        self.assertFalse(
            list((self.tmp_path / "signals" / "pending").glob("SIM-*.json"))
        )
        filled = read_json(filled_files[0])
        self.assertEqual(filled["capital_layer"], "simulated")
        self.assertEqual(filled["account_type"], "simulated")
        self.assertEqual(filled["status"], "filled")
        self.assertEqual(filled["filled_price"], 10.05)
        self.assertEqual(filled["filled_quantity"], 10)

        audit_rows = [
            json.loads(line)
            for line in trade_audit_trail.AUDIT_TRAIL.read_text(
                encoding="utf-8"
            ).splitlines()
            if line.strip()
        ]
        self.assertEqual(
            {row["stage"] for row in audit_rows},
            {"signal", "decision", "risk", "execution", "result"},
        )
        self.assertTrue(
            all(
                row.get("metadata", {}).get("capital_layer") == "simulated"
                for row in audit_rows
            )
        )
        self.assertEqual(result["review"]["capital_layer"], "simulated")
        self.assertEqual(
            self.review_requests,
            [{"date": "20260713", "session": "close", "capital_layer": "simulated"}],
        )
        self.assertFalse(shadow_broker.SHADOW_TRADES.exists())

    def test_write_execution_signal_does_not_duplicate_successful_mini_webhook(
        self,
    ) -> None:
        from shared.orchestrator import _write_execution_signal

        signals_dir = self.tmp_path / "signals"
        card = {
            "order_id": "SIM-ASHARE-WEBHOOK-NODUP",
            "ts_code": "600000.SH",
            "market": "ashare",
            "direction": "buy",
            "quantity": 100,
            "price": 10.0,
            "capital_layer": "simulated",
            "account_type": "simulated",
        }
        receipt = {
            "status": "pending",
            "raw_response": {
                "mode": "mini_webhook_sent",
                "webhook": {"success": True, "http_status": 200},
                "signal_card": card,
            },
        }

        result = _write_execution_signal(card, receipt, signals_dir=signals_dir)

        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["pending_signal"]["source"], "mini_webhook")
        self.assertEqual(list((signals_dir / "pending").glob("*.json")), [])

    def test_write_execution_signal_persists_failure_details(self) -> None:
        from shared.execution.signal_state_machine import read_json
        from shared.orchestrator import _write_execution_signal

        signals_dir = self.tmp_path / "signals_failure_details"
        card = {
            "order_id": "SIM-ASHARE-FAIL-DETAILS",
            "ts_code": "600000.SH",
            "market": "ashare",
            "direction": "buy",
            "quantity": 100,
            "price": 10.0,
            "capital_layer": "simulated",
            "account_type": "simulated",
        }
        receipt = {
            "order_id": "SIM-ASHARE-FAIL-DETAILS",
            "status": "rejected",
            "message": "Server-local A-share simulated fill via matching engine: rejected: insufficient_cash",
            "filled_qty": 0,
            "avg_price": 0.0,
            "raw_response": {
                "mode": "server_local_sim_engine",
                "engine_record": {"state": "rejected", "reason": "insufficient_cash"},
            },
        }

        result = _write_execution_signal(card, receipt, signals_dir=signals_dir)

        self.assertEqual(result["status"], "failed")
        failed_card = read_json(signals_dir / "failed" / "SIM-ASHARE-FAIL-DETAILS.json")
        self.assertIn("insufficient_cash", failed_card["failure_reason"])
        self.assertEqual(
            failed_card["failure_details"]["engine_reason"], "insufficient_cash"
        )
        self.assertEqual(failed_card["failure_details"]["receipt_status"], "rejected")

    def test_write_execution_signal_rejects_unknown_status_without_fake_fill(
        self,
    ) -> None:
        from shared.orchestrator import _write_execution_signal

        signals_dir = self.tmp_path / "signals_unknown_receipt"
        card = {
            "order_id": "SIM-ASHARE-UNKNOWN-RECEIPT",
            "ts_code": "600000.SH",
            "market": "ashare",
            "direction": "buy",
            "quantity": 100,
            "price": 10.0,
            "capital_layer": "simulated",
            "account_type": "simulated",
        }

        result = _write_execution_signal(
            card,
            {"status": "unknown"},
            signals_dir=signals_dir,
        )

        self.assertEqual(result["status"], "failed")
        self.assertFalse(list((signals_dir / "filled").glob("*.json")))
        failed = read_json(signals_dir / "failed" / "SIM-ASHARE-UNKNOWN-RECEIPT.json")
        self.assertIn("unsupported", failed["failure_reason"])

    def test_write_execution_signal_rejects_ashare_fill_without_authoritative_eligibility(
        self,
    ) -> None:
        from shared.orchestrator import _write_execution_signal

        signals_dir = self.tmp_path / "signals_ineligible_fill"
        card = {
            "order_id": "SIM-ASHARE-INELIGIBLE-FILL",
            "ts_code": "600000.SH",
            "market": "ashare",
            "direction": "buy",
            "quantity": 100,
            "price": 10.0,
            "capital_layer": "simulated",
            "account_type": "simulated",
        }
        receipt = {
            "status": "filled",
            "filled_qty": 100,
            "avg_price": 10.0,
            "execution_eligible": False,
        }

        result = _write_execution_signal(card, receipt, signals_dir=signals_dir)

        self.assertEqual(result["status"], "failed")
        self.assertFalse(list((signals_dir / "filled").glob("*.json")))

    def test_write_execution_signal_keeps_partial_separate_from_filled(self) -> None:
        from shared.orchestrator import _write_execution_signal

        signals_dir = self.tmp_path / "signals_partial_fill"
        card = {
            "order_id": "SIM-ASHARE-PARTIAL-FILL",
            "ts_code": "600000.SH",
            "market": "ashare",
            "direction": "buy",
            "quantity": 200,
            "price": 10.0,
            "capital_layer": "simulated",
            "account_type": "simulated",
        }
        receipt = {
            "status": "partial",
            "filled_qty": 100,
            "avg_price": 10.01,
            "execution_eligible": False,
        }

        result = _write_execution_signal(card, receipt, signals_dir=signals_dir)

        self.assertEqual(result["status"], "partial")
        self.assertTrue(
            (signals_dir / "partial" / "SIM-ASHARE-PARTIAL-FILL.json").exists()
        )
        self.assertFalse(list((signals_dir / "filled").glob("*.json")))

    def test_run_sim_loop_skips_existing_same_day_sim_signal(self) -> None:
        signals_dir = self.tmp_path / "signals"
        filled_dir = signals_dir / "filled"
        filled_dir.mkdir(parents=True, exist_ok=True)
        existing = {
            "order_id": "SIM-unit-AAA-20260713-existing",
            "ts_code": "AAA",
            "market": "unit",
            "direction": "buy",
            "quantity": 10,
            "price": 10.0,
            "capital_layer": "simulated",
            "account_type": "simulated",
            "valid_until": "2026-07-13",
            "status": "filled",
        }
        (filled_dir / "SIM-unit-AAA-20260713-existing.json").write_text(
            json.dumps(existing), encoding="utf-8"
        )

        result = run_sim_loop(
            StubSimAdapter(),
            "20260713",
            StubReader(),
            deps=self._deps(),
            signals_dir=signals_dir,
        )

        self.assertEqual(result["state"], "ok")
        self.assertEqual(result["filled_count"], 0)
        self.assertEqual(result["failed_count"], 0)
        self.assertEqual(result["pending_count"], 0)
        self.assertEqual(self.executed_orders, [])
        self.assertEqual(result["records"][0]["signal_result"]["status"], "duplicate")
        self.assertIn("signals.sim_dedup", result["stage_calls"])
        self.assertEqual(len(list(filled_dir.glob("SIM-*.json"))), 1)

    def test_run_sim_loop_retries_recoverable_ashare_cash_failure_once(self) -> None:
        signals_dir = self.tmp_path / "signals_retryable_cash"
        failed_dir = signals_dir / "failed"
        failed_dir.mkdir(parents=True, exist_ok=True)
        failed = {
            "order_id": "SIM-ashare-300418.SZ-20260713-original",
            "idempotency_key": "SIM:ashare:ashare_sim:20260713:300418.SZ:buy",
            "ts_code": "300418.SZ",
            "market": "ashare",
            "direction": "buy",
            "quantity": 100,
            "price": 10.0,
            "capital_layer": "simulated",
            "account_type": "simulated",
            "valid_until": "2026-07-13",
            "status": "failed",
            "retry_attempt": 0,
            "failure_reason": "A-share server-local simulated fill rejected by ledger: insufficient cash",
            "failure_details": {
                "raw_mode": "server_local_sim_engine",
                "receipt_status": "rejected",
                "receipt_message": "A-share server-local simulated fill rejected by ledger: insufficient cash",
            },
        }
        (failed_dir / "SIM-ashare-300418.SZ-20260713-original.json").write_text(
            json.dumps(failed), encoding="utf-8"
        )

        result = run_sim_loop(
            MultiCandidateSimAdapter(
                ["300418.SZ"],
                max_candidates=1,
                score_universe_limit=1,
                max_portfolio_positions=1,
            ),
            "20260713",
            StubReader(),
            deps=self._multi_candidate_deps(),
            signals_dir=signals_dir,
        )

        self.assertEqual(result["filled_count"], 1)
        self.assertEqual(len(self.executed_orders), 1)
        self.assertEqual(self.executed_orders[0]["retry_of"], failed["order_id"])
        self.assertEqual(self.executed_orders[0]["retry_attempt"], 1)
        self.assertTrue(self.executed_orders[0]["idempotency_key"].endswith(":retry1"))
        self.assertTrue(
            (failed_dir / "SIM-ashare-300418.SZ-20260713-original.json").exists()
        )

    def test_run_sim_loop_stops_recoverable_ashare_retry_after_limit(self) -> None:
        signals_dir = self.tmp_path / "signals_retry_limit"
        failed_dir = signals_dir / "failed"
        failed_dir.mkdir(parents=True, exist_ok=True)
        failed = {
            "order_id": "SIM-ashare-300418.SZ-20260713-retry2",
            "idempotency_key": "SIM:ashare:ashare_sim:20260713:300418.SZ:buy:retry2",
            "ts_code": "300418.SZ",
            "market": "ashare",
            "direction": "buy",
            "quantity": 100,
            "price": 10.0,
            "capital_layer": "simulated",
            "account_type": "simulated",
            "valid_until": "2026-07-13",
            "status": "failed",
            "retry_attempt": 2,
            "failure_reason": "A-share server-local simulated fill rejected by ledger: insufficient cash",
            "failure_details": {
                "raw_mode": "server_local_sim_engine",
                "receipt_status": "rejected",
                "receipt_message": "A-share server-local simulated fill rejected by ledger: insufficient cash",
            },
        }
        (failed_dir / "SIM-ashare-300418.SZ-20260713-retry2.json").write_text(
            json.dumps(failed), encoding="utf-8"
        )

        result = run_sim_loop(
            MultiCandidateSimAdapter(
                ["300418.SZ"],
                max_candidates=1,
                score_universe_limit=1,
                max_portfolio_positions=1,
            ),
            "20260713",
            StubReader(),
            deps=self._multi_candidate_deps(),
            signals_dir=signals_dir,
        )

        self.assertEqual(result["filled_count"], 0)
        self.assertEqual(self.executed_orders, [])
        self.assertEqual(result["records"][0]["signal_result"]["status"], "duplicate")

    def test_run_sim_loop_with_real_ashare_sim_broker_fills_locally_by_default(
        self,
    ) -> None:
        from shared.execution import sim_broker

        received_markets: list[str] = []

        def execute_sim_order(
            order: dict[str, object], market: str, account: object = None
        ) -> object:
            self.executed_orders.append(dict(order))
            received_markets.append(market)
            return sim_broker.execute_sim_order(
                order,
                market=market,
                account=account,
                config={
                    "signals_dir": self.tmp_path / "signals",
                    "bypass_market_hours": True,
                },
            )

        class VerifiedReader(StubReader):
            def get_bars_intraday(self, market, symbol, interval, start, end):
                return [
                    {
                        "close": 10.0,
                        "bar_time": "2026-07-13T10:00:00+08:00",
                        "volume": 100_000.0,
                        "provider": "sharedsignals_api_realtime_5min",
                    }
                ]

        base = self.tmp_path / "ashare-sim-fresh-20260712-v1"
        path_patches = [
            patch.object(local_sim_ledger, "LOCAL_SIM_DIR", base),
            patch.object(
                local_sim_ledger, "LOCAL_SIM_TRADES", base / "local_sim_trades.jsonl"
            ),
            patch.object(
                local_sim_ledger,
                "LOCAL_SIM_POSITIONS",
                base / "local_sim_positions.json",
            ),
            patch.object(
                local_sim_ledger, "LOCAL_SIM_PNL", base / "local_sim_pnl.json"
            ),
            patch.object(local_sim_ledger, "LOCAL_SIM_LOCK", base / ".local_sim.lock"),
            patch.object(
                local_sim_ledger,
                "LOCAL_SIM_POSITIONS_SNAPSHOT",
                base / "simulated_ashare_positions.json",
            ),
            patch.object(
                local_sim_ledger,
                "LOCAL_SIM_RECEIPTS",
                base / "sim_execution_receipts.jsonl",
            ),
        ]
        deps = self._multi_candidate_deps()
        deps.execute_sim_order = execute_sim_order
        with ExitStack() as stack:
            for path_patch in path_patches:
                stack.enter_context(path_patch)
            local_sim_ledger.bootstrap_fresh_local_sim(
                root=base,
                lineage_started_at="2026-07-12T00:00:00+08:00",
                point_in_time_as_of="2026-07-12T09:00:00+08:00",
                account="ashare_sim",
            )
            stack.enter_context(
                patch("Ashare.sim_executor._fresh_5min_bar", return_value=(True, 0.0))
            )
            stack.enter_context(
                patch.object(
                    local_sim_ledger,
                    "_ashare_session_metadata",
                    return_value={
                        "trade_timestamp_bj": "2026-07-13T10:00:00+08:00",
                        "ashare_session_valid": True,
                        "ashare_session_rejection": "",
                    },
                )
            )
            stack.enter_context(self._approved_expansion_evidence("20260713"))
            result = run_sim_loop(
                MultiCandidateSimAdapter(
                    ["600000.SH"],
                    max_candidates=1,
                    score_universe_limit=1,
                    max_portfolio_positions=1,
                ),
                "20260713",
                VerifiedReader(),
                deps=deps,
                signals_dir=self.tmp_path / "signals",
            )

        self.assertEqual(result["state"], "ok")
        self.assertEqual(result["filled_count"], 1, result["records"])
        self.assertEqual(result["pending_count"], 0)
        self.assertEqual(result["failed_count"], 0)
        pending_files = list((self.tmp_path / "signals" / "pending").glob("SIM-*.json"))
        filled_files = list((self.tmp_path / "signals" / "filled").glob("SIM-*.json"))
        self.assertEqual(len(pending_files), 0)
        self.assertEqual(len(filled_files), 1)
        filled = read_json(filled_files[0])
        self.assertEqual(filled["capital_layer"], "simulated")
        self.assertEqual(filled["account_type"], "simulated")
        self.assertEqual(filled["commission"], 5.0)
        self.assertEqual(filled["slippage_cny"], 2.0)
        self.assertTrue(filled["filled_at"])
        self.assertEqual(received_markets, ["ashare"])
        self.assertEqual(
            result["records"][0]["receipt"]["raw_response"]["mode"],
            "server_local_sim_engine",
        )
        self.assertTrue(
            result["records"][0]["receipt"]["raw_response"]["local_sim_backup"][
                "recorded"
            ]
        )
        self.assertTrue(result["records"][0]["receipt"]["execution_eligible"])
        filled_quantity = result["records"][0]["order"]["quantity"]
        self.assertEqual(filled["filled_quantity"], filled_quantity)
        self.assertEqual(filled_quantity % 100, 0)
        self.assertLessEqual(
            filled_quantity * result["records"][0]["order"]["price"],
            7_500.0,
        )
        self.assertTrue((base / "local_sim_trades.jsonl").exists())

    def test_run_sim_loop_reports_no_trade_risk_rejections(self) -> None:
        deps = self._deps()

        def reject_risk(
            order: dict[str, object], portfolio: dict[str, object]
        ) -> dict[str, object]:
            return {
                "approved": False,
                "adjusted_weight": 0.0,
                "reasons": ["unit risk rejection"],
            }

        deps.risk_check = reject_risk

        result = run_sim_loop(
            StubSimAdapter(),
            "20260713",
            StubReader(),
            deps=deps,
            signals_dir=self.tmp_path / "signals",
        )

        self.assertEqual(result["filled_count"], 0)
        self.assertEqual(result["order_count"], 0)
        self.assertEqual(result["risk_rejection_count"], 1)
        self.assertEqual(
            result["no_trade_explanation"]["category"], "all_rejected_by_risk"
        )
        self.assertEqual(result["no_trade_explanation"]["counts"]["risk_rejections"], 1)
        self.assertEqual(
            result["risk_rejections"][0]["reasons"], ["unit risk rejection"]
        )

    def test_run_sim_loop_ranks_candidates_by_combined_score_before_limit(self) -> None:
        scores = {"AAA": 0.10, "BBB": 0.92, "CCC": 0.81}
        deps = self._multi_candidate_deps()

        def score_universe(
            date: str,
            universe: list[str],
            data_reader: object = None,
            market: str = "ashare",
        ) -> list[tuple[str, dict[str, object]]]:
            return [
                (
                    symbol,
                    {
                        "combined": scores[symbol],
                        "sector": "unit",
                        "turnover_wan": 10000,
                        "capital_layer": "simulated",
                    },
                )
                for symbol in universe
            ]

        deps.score_universe = score_universe

        with self._approved_expansion_evidence("20260713"):
            result = run_sim_loop(
                MultiCandidateSimAdapter(
                    ["AAA", "BBB", "CCC"],
                    max_candidates=2,
                    score_universe_limit=3,
                    max_portfolio_positions=2,
                ),
                "20260713",
                StubReader(),
                deps=deps,
                signals_dir=self.tmp_path / "signals_ranked",
            )

        self.assertEqual(result["candidate_count"], 2)
        self.assertEqual(
            [record["symbol"] for record in result["records"]], ["BBB", "CCC"]
        )
        self.assertEqual(
            [order["ts_code"] for order in self.executed_orders], ["BBB", "CCC"]
        )
        self.assertTrue(
            all(
                order["candidate_pool_layer"] == "candidate"
                for order in self.executed_orders
            )
        )
        self.assertTrue(
            all(
                order["execution_source"] == "ashare_candidate_layer"
                for order in self.executed_orders
            )
        )

    def test_run_sim_loop_passes_precomputed_scores_to_candidate_pool(self) -> None:
        deps = self._multi_candidate_deps()
        received_scores: dict[str, dict[str, object]] = {}

        def score_universe(
            date: str,
            universe: list[str],
            data_reader: object = None,
            market: str = "ashare",
        ) -> list[tuple[str, dict[str, object]]]:
            return [
                (
                    "AAA",
                    {
                        "combined": 0.54,
                        "sector": "unit",
                        "turnover_wan": 10000,
                        "capital_layer": "simulated",
                    },
                ),
                (
                    "BBB",
                    {
                        "combined": 0.92,
                        "sector": "unit",
                        "turnover_wan": 10000,
                        "capital_layer": "simulated",
                    },
                ),
            ]

        def build_pool(
            date: str,
            universe: list[str],
            market: str | None = None,
            reader: object | None = None,
            scores_by_symbol: dict[str, dict[str, object]] | None = None,
        ) -> dict[str, list[str]]:
            self.calls.append("candidate_pool")
            received_scores.update(scores_by_symbol or {})
            return {
                "candidate": [
                    symbol
                    for symbol in universe
                    if float(
                        (scores_by_symbol or {}).get(symbol, {}).get("combined", 0.0)
                    )
                    >= 0.55
                ],
                "watch": [],
                "holdings": [],
                "universe": list(universe),
            }

        deps.score_universe = score_universe
        deps.build_pool = build_pool

        result = run_sim_loop(
            MultiCandidateSimAdapter(
                ["AAA", "BBB"],
                max_candidates=2,
                score_universe_limit=2,
                max_portfolio_positions=2,
            ),
            "20260713",
            StubReader(),
            deps=deps,
            signals_dir=self.tmp_path / "signals_precomputed_scores",
        )

        self.assertEqual(received_scores["AAA"]["combined"], 0.54)
        self.assertEqual(received_scores["BBB"]["combined"], 0.92)
        self.assertEqual(result["candidate_count"], 1)
        self.assertEqual([record["symbol"] for record in result["records"]], ["BBB"])

    def test_run_sim_loop_caps_ashare_new_positions_to_configured_target(self) -> None:
        deps = self._multi_candidate_deps()

        def score_universe(
            date: str,
            universe: list[str],
            data_reader: object = None,
            market: str = "ashare",
        ) -> list[tuple[str, dict[str, object]]]:
            return [
                (
                    symbol,
                    {
                        "combined": 1.0 - index * 0.01,
                        "sector": "unit",
                        "turnover_wan": 10000,
                        "capital_layer": "simulated",
                    },
                )
                for index, symbol in enumerate(universe)
            ]

        deps.score_universe = score_universe

        with self._approved_expansion_evidence("20260713"):
            result = run_sim_loop(
                MultiCandidateSimAdapter(
                    ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"],
                    max_candidates=6,
                    score_universe_limit=6,
                    max_portfolio_positions=3,
                ),
                "20260713",
                StubReader(),
                deps=deps,
                signals_dir=self.tmp_path / "signals_capped",
            )

        self.assertEqual(result["order_count"], 3)
        self.assertEqual(result["filled_count"], 3)
        self.assertEqual(
            [order["ts_code"] for order in self.executed_orders], ["AAA", "BBB", "CCC"]
        )

    def test_sample_debt_does_not_force_zero_orders_for_risk_approved_candidates(
        self,
    ) -> None:
        deps = self._multi_candidate_deps()

        def score_universe(
            date: str,
            universe: list[str],
            data_reader: object = None,
            market: str = "ashare",
        ) -> list[tuple[str, dict[str, object]]]:
            scores = {"AAA": 0.52, "BBB": 0.50, "CCC": 0.48}
            return [
                (
                    symbol,
                    {
                        "combined": scores[symbol],
                        "sector": "unit",
                        "turnover_wan": 10000,
                        "capital_layer": "simulated",
                    },
                )
                for symbol in universe
            ]

        deps.score_universe = score_universe

        result = run_sim_loop(
            MultiCandidateSimAdapter(
                ["AAA", "BBB", "CCC"],
                max_candidates=3,
                score_universe_limit=3,
                max_portfolio_positions=3,
            ),
            "20260713",
            StubReader(),
            deps=deps,
            signals_dir=self.tmp_path / "signals_dynamic_capital",
        )

        self.assertEqual(result["capital_plan"]["risk_mode"], "sample_collection")
        self.assertEqual(result["capital_plan"]["max_new_positions"], 1)
        self.assertEqual(result["order_count"], 1)
        self.assertEqual(result["filled_count"], 1)
        self.assertEqual(len(self.executed_orders), 1)
        self.assertEqual(self.executed_orders[0]["sample_intent"], "exploitation")
        self.assertLessEqual(
            self.executed_orders[0]["quantity"] * self.executed_orders[0]["price"],
            7_500.0,
        )
        self.assertEqual(result["candidate_layer_breakdown"]["candidate"], 3)
        self.assertEqual(
            result["capital_plan_decision"]["risk_mode"],
            "sample_collection",
        )
        self.assertEqual(result["capital_plan_decision"]["position_capacity"], 1)
        self.assertEqual(
            result["portfolio_decision"]["ranked_risk_approved_candidates"], 3
        )
        self.assertEqual(result["portfolio_decision"]["allowed_buy_count"], 1)

    def test_run_sim_loop_ashare_does_not_trade_watch_layer(self) -> None:
        deps = self._multi_candidate_deps()

        def build_pool(
            date: str,
            universe: list[str],
            market: str | None = None,
            reader: object | None = None,
        ) -> dict[str, list[str]]:
            return {
                "candidate": [],
                "watch": list(universe),
                "holdings": [],
                "universe": list(universe),
            }

        deps.build_pool = build_pool

        def score_universe(
            date: str,
            universe: list[str],
            data_reader: object = None,
            market: str = "ashare",
        ) -> list[tuple[str, dict[str, object]]]:
            return [
                (
                    symbol,
                    {
                        "combined": 0.92,
                        "sector": "unit",
                        "turnover_wan": 10000,
                        "capital_layer": "simulated",
                    },
                )
                for symbol in universe
            ]

        deps.score_universe = score_universe

        result = run_sim_loop(
            MultiCandidateSimAdapter(
                ["000001.SZ", "000002.SZ"],
                max_candidates=2,
                score_universe_limit=2,
                max_portfolio_positions=2,
            ),
            "20260713",
            StubReader(),
            deps=deps,
            signals_dir=self.tmp_path / "signals_watch_only",
        )

        self.assertEqual(result["candidate_count"], 0)
        self.assertEqual(result["order_count"], 0)
        self.assertEqual(result["filled_count"], 0)
        self.assertEqual(result["no_trade_explanation"]["category"], "no_candidates")
        self.assertEqual(self.executed_orders, [])

    def test_run_sim_loop_ashare_does_not_fallback_to_universe_when_pool_empty(
        self,
    ) -> None:
        deps = self._multi_candidate_deps()

        def build_pool(
            date: str,
            universe: list[str],
            market: str | None = None,
            reader: object | None = None,
        ) -> dict[str, list[str]]:
            return {
                "candidate": [],
                "watch": [],
                "holdings": [],
                "universe": list(universe),
            }

        deps.build_pool = build_pool

        def score_universe(
            date: str,
            universe: list[str],
            data_reader: object = None,
            market: str = "ashare",
        ) -> list[tuple[str, dict[str, object]]]:
            return [
                (
                    symbol,
                    {
                        "combined": 0.95,
                        "sector": "unit",
                        "turnover_wan": 10000,
                        "capital_layer": "simulated",
                    },
                )
                for symbol in universe
            ]

        deps.score_universe = score_universe

        result = run_sim_loop(
            MultiCandidateSimAdapter(
                ["000001.SZ", "000002.SZ"],
                max_candidates=2,
                score_universe_limit=2,
                max_portfolio_positions=2,
            ),
            "20260713",
            StubReader(),
            deps=deps,
            signals_dir=self.tmp_path / "signals_empty_pool",
        )

        self.assertEqual(result["candidate_count"], 0)
        self.assertEqual(result["filled_count"], 0)
        diagnostics = result["no_trade_explanation"]["score_diagnostics"]
        self.assertEqual(diagnostics["scored_count"], 2)
        self.assertEqual(diagnostics["candidate_threshold"], 0.55)
        self.assertEqual(diagnostics["top_scores"][0]["combined"], 0.95)
        self.assertEqual(diagnostics["candidate_above_threshold_count"], 2)
        self.assertEqual(
            diagnostics["candidate_pool_status"], "pool_empty_despite_threshold_scores"
        )
        self.assertEqual(self.executed_orders, [])
        observation = result["sample_pipeline"]["observation"]
        self.assertEqual(
            observation["candidate_observation_count"], 2, result["errors"]
        )
        self.assertEqual(observation["prediction_count"], 8)
        self.assertEqual(observation["data_quality_rejected_count"], 8)
        self.assertTrue(Path(observation["journal_path"]).exists())

    def test_sample_debt_selects_one_relative_rank_exploration_below_mature_threshold(
        self,
    ) -> None:
        deps = self._ordered_sample_deps()

        def build_pool(
            date: str,
            universe: list[str],
            market: str | None = None,
            reader: object | None = None,
        ) -> dict[str, list[str]]:
            return {
                "candidate": [],
                "watch": list(universe),
                "holdings": [],
                "universe": list(universe),
            }

        deps.build_pool = build_pool

        def score_universe(
            date: str,
            universe: list[str],
            data_reader: object = None,
            market: str = "ashare",
        ) -> list[tuple[str, dict[str, object]]]:
            scores = [0.41, 0.44, 0.39]
            return [
                (
                    symbol,
                    {
                        "combined": scores[index],
                        "macro": scores[index],
                        "event": scores[index],
                        "fundamental": scores[index],
                        "capital": scores[index],
                        "technical": scores[index],
                        "sentiment": scores[index],
                        "sector": "unit",
                        "turnover_wan": 20_000,
                        "evidence_coverage": 1.0,
                        "missing_evidence_dimensions": [],
                    },
                )
                for index, symbol in enumerate(universe)
            ]

        deps.score_universe = score_universe

        class VerifiedReader(StubReader):
            def get_bars_intraday(self, market, symbol, interval, start, end):
                return [
                    {
                        "close": 10.0,
                        "bar_time": "2026-07-13T10:00:00+08:00",
                        "available_at": "2026-07-13T10:00:00+08:00",
                        "ingested_at": "2026-07-13T10:00:00+08:00",
                        "retrieved_as_of": "2026-07-13T10:00:00+08:00",
                        "volume": 100_000,
                        "provider": "sharedsignals_api_realtime_5min",
                    }
                ]

        with patch.object(
            orchestrator_module,
            "_now_iso",
            return_value="2026-07-13T10:01:00+08:00",
        ):
            result = run_sim_loop(
                MultiCandidateSimAdapter(
                    ["600001.SH", "600002.SH", "600003.SH"],
                    max_candidates=3,
                    score_universe_limit=3,
                    max_portfolio_positions=3,
                    sample_adjustment={
                        "strategy_sample_valid_count": 0,
                        "min_strategy_samples": 5,
                    },
                ),
                "20260713",
                VerifiedReader(),
                deps=deps,
                signals_dir=self.tmp_path / "signals_relative_exploration",
            )

        selection = result["sample_pipeline"]["exploration_selection"]
        self.assertEqual(selection["status"], "selected", result)
        self.assertIn(selection["symbol"], selection["eligible_top_k_symbols"])
        self.assertIn(selection["symbol"], {"600001.SH", "600002.SH", "600003.SH"})
        self.assertGreater(float(selection["propensity"]), 0.0)
        self.assertLessEqual(float(selection["propensity"]), 1.0)
        self.assertFalse(selection["absolute_mature_threshold_required"])
        self.assertEqual(result["candidate_count"], 1)
        self.assertEqual(result["order_count"], 1)
        self.assertEqual(result["filled_count"], 1)
        order = self.executed_orders[0]
        self.assertEqual(order["ts_code"], selection["symbol"])
        self.assertEqual(order["sample_intent"], "exploration")
        self.assertTrue(order["primary_style"])
        self.assertIn("style_scores", order)
        self.assertIn("style_versions", order)
        self.assertEqual(
            result["sample_pipeline"]["outcomes"]["exploration_fill_count"], 1
        )
        self.assertEqual(
            result["sample_pipeline"]["outcomes"]["exploitation_fill_count"], 0
        )

    def test_missing_journal_overrides_fake_repaid_sample_adjustment_for_exploration(
        self,
    ) -> None:
        deps = self._multi_candidate_deps()

        deps.build_pool = lambda date, universe, market=None, reader=None: {
            "candidate": [],
            "watch": list(universe),
            "holdings": [],
            "universe": list(universe),
        }
        deps.score_universe = lambda date, universe, data_reader=None, market="ashare": [
            (
                symbol,
                {
                    "combined": 0.44 - index * 0.01,
                    "macro": 0.44,
                    "event": 0.44,
                    "fundamental": 0.44,
                    "capital": 0.44,
                    "technical": 0.44,
                    "sentiment": 0.44,
                    "sector": "unit",
                    "turnover_wan": 20_000,
                    "evidence_coverage": 1.0,
                    "missing_evidence_dimensions": [],
                },
            )
            for index, symbol in enumerate(universe)
        ]

        class VerifiedReader(StubReader):
            def get_bars_intraday(self, market, symbol, interval, start, end):
                return [
                    {
                        "close": 10.0,
                        "bar_time": "2026-07-13T10:00:00+08:00",
                        "volume": 100_000,
                        "provider": "sharedsignals_api_realtime_5min",
                        "available_at": "2026-07-13T10:00:00+08:00",
                        "ingested_at": "2026-07-13T10:00:00+08:00",
                        "retrieved_as_of": "2026-07-13T10:00:00+08:00",
                    }
                ]

        result = run_sim_loop(
            MultiCandidateSimAdapter(
                ["600001.SH", "600002.SH"],
                max_candidates=2,
                score_universe_limit=2,
                max_portfolio_positions=2,
                sample_adjustment={
                    "strategy_sample_valid_count": 999,
                    "min_strategy_samples": 5,
                    "sample_debt": False,
                    "source": "fake_adapter_injection",
                },
            ),
            "20260713",
            VerifiedReader(),
            deps=deps,
            signals_dir=self.tmp_path / "signals_missing_journal_authority",
        )

        adjustment = result["capital_plan"]["sample_adjustment"]
        self.assertEqual(
            adjustment["sample_authority_status"], "fresh_start_journal_missing"
        )
        self.assertEqual(adjustment["strategy_sample_valid_count"], 0)
        self.assertIs(adjustment["sample_debt"], True)
        self.assertEqual(result["filled_count"], 1, result)
        self.assertEqual(self.executed_orders[0]["sample_intent"], "exploration")

    def test_wrong_authority_exploration_fill_does_not_consume_daily_limit(
        self,
    ) -> None:
        signals_dir = self.tmp_path / "signals_wrong_authority_daily_limit"
        journal_path = (
            signals_dir.parent / "shared" / "review" / "ashare" / "sample_journal.jsonl"
        )
        SampleJournal(journal_path).append_sample(
            {
                "journal_event_id": "legacy-exploration-fill",
                "record_type": "fill",
                "sample_intent": "exploration",
                "sample_layer": "exploration_fill",
                "execution_eligible": True,
                "trade_date": "20260713",
                "capital_authority_id": "legacy-shared-capital",
                "authority_generation": ASHARE_AUTHORITY_GENERATION,
                "execution_lineage_id": ASHARE_EXECUTION_LINEAGE_ID,
                "real_trading_enabled": False,
            }
        )
        deps = self._ordered_sample_deps()
        deps.build_pool = lambda date, universe, market=None, reader=None: {
            "candidate": [],
            "watch": list(universe),
            "holdings": [],
            "universe": list(universe),
        }
        deps.score_universe = lambda date, universe, data_reader=None, market="ashare": [
            (
                symbol,
                {
                    "combined": 0.44,
                    "macro": 0.44,
                    "event": 0.44,
                    "fundamental": 0.44,
                    "capital": 0.44,
                    "technical": 0.44,
                    "sentiment": 0.44,
                    "sector": "unit",
                    "turnover_wan": 20_000,
                    "evidence_coverage": 1.0,
                    "missing_evidence_dimensions": [],
                },
            )
            for symbol in universe
        ]

        class VerifiedReader(StubReader):
            def get_bars_intraday(self, market, symbol, interval, start, end):
                return [
                    {
                        "close": 10.0,
                        "bar_time": "2026-07-13T10:00:00+08:00",
                        "available_at": "2026-07-13T10:00:00+08:00",
                        "ingested_at": "2026-07-13T10:00:00+08:00",
                        "retrieved_as_of": "2026-07-13T10:00:00+08:00",
                        "volume": 100_000,
                        "provider": "sharedsignals_api_realtime_5min",
                    }
                ]

        adapter = MultiCandidateSimAdapter(
            ["600001.SH"],
            max_candidates=1,
            score_universe_limit=1,
            max_portfolio_positions=1,
        )
        with patch.object(
            orchestrator_module,
            "_now_iso",
            return_value="2026-07-13T10:01:00+08:00",
        ):
            result = run_sim_loop(
                adapter,
                "20260713",
                VerifiedReader(),
                deps=deps,
                signals_dir=signals_dir,
            )

        self.assertEqual(
            result["capital_plan"]["sample_adjustment"][
                "excluded_wrong_authority_count"
            ],
            1,
        )
        self.assertEqual(result["filled_count"], 1, result)
        self.assertEqual(self.executed_orders[0]["sample_intent"], "exploration")

        with patch.object(
            orchestrator_module,
            "_now_iso",
            return_value="2026-07-13T10:01:00+08:00",
        ):
            second = run_sim_loop(
                adapter,
                "20260713",
                VerifiedReader(),
                deps=deps,
                signals_dir=signals_dir,
            )
        self.assertEqual(second["filled_count"], 0)
        self.assertEqual(len(self.executed_orders), 1)
        self.assertEqual(
            second["sample_pipeline"]["exploration_selection"]["reason"],
            "exploration_daily_position_limit_reached",
        )

    def test_unavailable_sample_authority_blocks_exploration_with_concrete_gate(
        self,
    ) -> None:
        deps = self._multi_candidate_deps()
        deps.build_pool = lambda date, universe, market=None, reader=None: {
            "candidate": [],
            "watch": list(universe),
            "holdings": [],
            "universe": list(universe),
        }
        deps.score_universe = lambda date, universe, data_reader=None, market="ashare": [
            (
                symbol,
                {
                    "combined": 0.44,
                    "macro": 0.44,
                    "event": 0.44,
                    "fundamental": 0.44,
                    "capital": 0.44,
                    "technical": 0.44,
                    "sentiment": 0.44,
                    "sector": "unit",
                    "turnover_wan": 20_000,
                    "evidence_coverage": 1.0,
                    "missing_evidence_dimensions": [],
                },
            )
            for symbol in universe
        ]

        class VerifiedReader(StubReader):
            def get_bars_intraday(self, market, symbol, interval, start, end):
                return [
                    {
                        "close": 10.0,
                        "bar_time": "2026-07-13T10:00:00+08:00",
                        "volume": 100_000,
                        "provider": "sharedsignals_api_realtime_5min",
                    }
                ]

        unavailable = {
            "sample_authority_status": "sample_journal_unavailable",
            "sample_authority_reliable": False,
            "strategy_sample_valid_count": 0,
            "min_strategy_samples": 5,
            "sample_debt": True,
            "reason": "sample_journal_unavailable",
            "real_trading_enabled": False,
        }
        with patch(
            "Ashare.adapter.build_current_sample_adjustment",
            return_value=unavailable,
        ):
            result = run_sim_loop(
                MultiCandidateSimAdapter(
                    ["600001.SH"],
                    max_candidates=1,
                    score_universe_limit=1,
                    max_portfolio_positions=1,
                ),
                "20260713",
                VerifiedReader(),
                deps=deps,
                signals_dir=self.tmp_path / "signals_unavailable_sample_authority",
            )

        selection = result["sample_pipeline"]["exploration_selection"]
        self.assertEqual(selection["reason"], "safety_gate_blocked")
        self.assertIn(
            "sample_journal_authority_unavailable", selection["safety_blockers"]
        )
        self.assertEqual(result["filled_count"], 0)
        self.assertEqual(self.executed_orders, [])

    def test_real_adapter_missing_position_snapshot_blocks_exploration_risk(
        self,
    ) -> None:
        signals_dir = self.tmp_path / "signals_real_adapter_opening"
        journal_path = (
            signals_dir.parent / "shared" / "review" / "ashare" / "sample_journal.jsonl"
        )

        class OpeningReader(StubReader):
            def get_assets(self, market):
                return [
                    {
                        "symbol": "600001.SH",
                        "name": "Opening Candidate",
                        "exchange": "SH",
                        "list_date": "20000101",
                        "status": "active",
                    }
                ]

            def get_coverage(self, market, date):
                return [{"symbol": "600001.SH", "coverage_status": "normal"}]

            def get_bars_daily(self, market, symbol, start=None, end=None):
                return [
                    {
                        "trade_date": "20260713",
                        "close": 10.0,
                        "amount": 100_000,
                        "provider": "sharedsignals_api_daily",
                    }
                ]

            def get_bars_intraday(self, market, symbol, interval, start, end):
                return [
                    {
                        "close": 10.0,
                        "bar_time": "2026-07-13T10:00:00+08:00",
                        "volume": 100_000,
                        "provider": "sharedsignals_api_realtime_5min",
                        "available_at": "2026-07-13T10:00:00+08:00",
                        "ingested_at": "2026-07-13T10:00:00+08:00",
                        "retrieved_as_of": "2026-07-13T10:00:00+08:00",
                    }
                ]

        reader = OpeningReader()
        adapter = AshareAdapter(reader=reader)
        deps = self._multi_candidate_deps()
        deps.build_pool = lambda date, universe, market=None, reader=None: {
            "candidate": [],
            "watch": list(universe),
            "holdings": [],
            "universe": list(universe),
        }
        deps.score_universe = lambda date, universe, data_reader=None, market="ashare": [
            (
                symbol,
                {
                    "combined": 0.44,
                    "macro": 0.44,
                    "event": 0.44,
                    "fundamental": 0.44,
                    "capital": 0.44,
                    "technical": 0.44,
                    "sentiment": 0.44,
                    "sector": "unit",
                    "turnover_wan": 20_000,
                    "evidence_coverage": 1.0,
                    "missing_evidence_dimensions": [],
                },
            )
            for symbol in universe
        ]
        missing_snapshot = self.tmp_path / "missing_positions.json"
        missing_trades = self.tmp_path / "missing_trades.jsonl"
        with (
            patch.object(
                ashare_adapter_module,
                "DEFAULT_SAMPLE_JOURNAL_PATH",
                journal_path,
            ),
            patch.object(
                local_sim_ledger,
                "LOCAL_SIM_POSITIONS_SNAPSHOT",
                missing_snapshot,
            ),
            patch.object(local_sim_ledger, "LOCAL_SIM_TRADES", missing_trades),
        ):
            account = adapter.get_sim_account()
            result = run_sim_loop(
                adapter,
                "20260713",
                reader,
                deps=deps,
                signals_dir=signals_dir,
            )

        self.assertIs(account["capital_plan_sample_adjustment"]["sample_debt"], True)
        self.assertEqual(result["candidate_count"], 0)
        self.assertEqual(result["filled_count"], 0, result)
        self.assertEqual(self.executed_orders, [])
        self.assertEqual(
            result["ashare_position_authority_reason"],
            "capital_position_source_mismatch",
        )

    def test_sample_debt_activates_safe_exploration_when_normal_candidate_has_no_order(
        self,
    ) -> None:
        deps = self._ordered_sample_deps()

        def build_pool(
            date: str,
            universe: list[str],
            market: str | None = None,
            reader: object | None = None,
        ) -> dict[str, list[str]]:
            return {
                "candidate": [universe[0]],
                "watch": list(universe[1:]),
                "holdings": [],
                "universe": list(universe),
            }

        deps.build_pool = build_pool

        def score_universe(
            date: str,
            universe: list[str],
            data_reader: object = None,
            market: str = "ashare",
        ) -> list[tuple[str, dict[str, object]]]:
            scores = [0.80, 0.44, 0.41]
            return [
                (
                    symbol,
                    {
                        "combined": scores[index],
                        "macro": scores[index],
                        "event": scores[index],
                        "fundamental": scores[index],
                        "capital": scores[index],
                        "technical": scores[index],
                        "sentiment": scores[index],
                        "sector": "unit",
                        "turnover_wan": 20_000,
                        "evidence_coverage": 1.0,
                        "missing_evidence_dimensions": [],
                    },
                )
                for index, symbol in enumerate(universe)
            ]

        deps.score_universe = score_universe

        def risk_check(
            order: dict[str, object], portfolio: dict[str, object]
        ) -> dict[str, object]:
            if order["ts_code"] == "600001.SH":
                return {
                    "approved": False,
                    "adjusted_weight": 0.0,
                    "adjustments": [],
                    "reasons": ["mature_strategy_edge_not_met"],
                }
            return {
                "approved": True,
                "adjusted_weight": order["weight"],
                "adjustments": [],
                "reasons": [],
            }

        deps.risk_check = risk_check

        class VerifiedReader(StubReader):
            def get_bars_intraday(self, market, symbol, interval, start, end):
                return [
                    {
                        "close": 10.0,
                        "bar_time": "2026-07-13T10:00:00+08:00",
                        "available_at": "2026-07-13T10:00:00+08:00",
                        "ingested_at": "2026-07-13T10:00:00+08:00",
                        "retrieved_as_of": "2026-07-13T10:00:00+08:00",
                        "volume": 100_000,
                        "provider": "sharedsignals_api_realtime_5min",
                    }
                ]

        with patch.object(
            orchestrator_module,
            "_now_iso",
            return_value="2026-07-13T10:01:00+08:00",
        ):
            result = run_sim_loop(
                MultiCandidateSimAdapter(
                    ["600001.SH", "600002.SH", "600003.SH"],
                    max_candidates=3,
                    score_universe_limit=3,
                    max_portfolio_positions=3,
                    sample_adjustment={
                        "strategy_sample_valid_count": 0,
                        "min_strategy_samples": 5,
                    },
                ),
                "20260713",
                VerifiedReader(),
                deps=deps,
                signals_dir=self.tmp_path / "signals_exploration_fallback",
            )

        selection = result["sample_pipeline"]["exploration_selection"]
        self.assertIn(selection["symbol"], {"600002.SH", "600003.SH"})
        self.assertEqual(selection["activation_status"], "activated")
        self.assertEqual(
            selection["activation_reason"],
            "normal_strategy_no_risk_approved_order",
        )
        self.assertEqual(result["filled_count"], 1, result)
        self.assertEqual(len(self.executed_orders), 1)
        self.assertEqual(self.executed_orders[0]["ts_code"], selection["symbol"])
        self.assertEqual(self.executed_orders[0]["sample_intent"], "exploration")
        self.assertEqual(
            result["sample_pipeline"]["outcomes"]["exploration_fill_count"], 1
        )
        self.assertEqual(
            result["sample_pipeline"]["outcomes"]["exploitation_fill_count"], 0
        )
        rejected = {row["symbol"]: row for row in result["risk_rejections"]}
        self.assertEqual(
            rejected["600001.SH"]["reasons"],
            ["mature_strategy_edge_not_met"],
        )

    def test_normal_risk_approved_order_keeps_exploration_as_unfunded_standby(
        self,
    ) -> None:
        deps = self._ordered_sample_deps()

        def build_pool(
            date: str,
            universe: list[str],
            market: str | None = None,
            reader: object | None = None,
        ) -> dict[str, list[str]]:
            return {
                "candidate": [universe[0]],
                "watch": list(universe[1:]),
                "holdings": [],
                "universe": list(universe),
            }

        deps.build_pool = build_pool

        def score_universe(
            date: str,
            universe: list[str],
            data_reader: object = None,
            market: str = "ashare",
        ) -> list[tuple[str, dict[str, object]]]:
            scores = [0.80, 0.44]
            return [
                (
                    symbol,
                    {
                        "combined": scores[index],
                        "macro": scores[index],
                        "event": scores[index],
                        "fundamental": scores[index],
                        "capital": scores[index],
                        "technical": scores[index],
                        "sentiment": scores[index],
                        "sector": "unit",
                        "turnover_wan": 20_000,
                        "evidence_coverage": 1.0,
                        "missing_evidence_dimensions": [],
                    },
                )
                for index, symbol in enumerate(universe)
            ]

        deps.score_universe = score_universe

        class VerifiedReader(StubReader):
            def get_bars_intraday(self, market, symbol, interval, start, end):
                return [
                    {
                        "close": 10.0,
                        "bar_time": "2026-07-13T10:00:00+08:00",
                        "available_at": "2026-07-13T10:00:00+08:00",
                        "ingested_at": "2026-07-13T10:00:00+08:00",
                        "retrieved_as_of": "2026-07-13T10:00:00+08:00",
                        "volume": 100_000,
                        "provider": "sharedsignals_api_realtime_5min",
                    }
                ]

        with patch.object(
            orchestrator_module,
            "_now_iso",
            return_value="2026-07-13T10:01:00+08:00",
        ):
            result = run_sim_loop(
                MultiCandidateSimAdapter(
                    ["600001.SH", "600002.SH"],
                    max_candidates=2,
                    score_universe_limit=2,
                    max_portfolio_positions=2,
                    sample_adjustment={
                        "strategy_sample_valid_count": 0,
                        "min_strategy_samples": 5,
                    },
                ),
                "20260713",
                VerifiedReader(),
                deps=deps,
                signals_dir=self.tmp_path / "signals_exploration_standby",
            )

        selection = result["sample_pipeline"]["exploration_selection"]
        self.assertEqual(selection["status"], "not_activated")
        self.assertEqual(selection["standby_selected_count"], 1)
        self.assertEqual(
            selection["activation_reason"],
            "normal_strategy_risk_approved_order_available",
        )
        self.assertEqual(result["filled_count"], 1, result)
        self.assertEqual(len(self.executed_orders), 1)
        self.assertEqual(self.executed_orders[0]["ts_code"], "600001.SH")
        self.assertEqual(self.executed_orders[0]["sample_intent"], "exploitation")
        self.assertEqual(
            result["sample_pipeline"]["outcomes"]["exploration_fill_count"], 0
        )
        self.assertEqual(
            result["sample_pipeline"]["outcomes"]["exploitation_fill_count"],
            1,
            result["sample_pipeline"]["outcomes"],
        )

    def test_run_sim_loop_ashare_diagnoses_all_neutral_scores_as_missing_evidence(
        self,
    ) -> None:
        deps = self._multi_candidate_deps()

        def build_pool(
            date: str,
            universe: list[str],
            market: str | None = None,
            reader: object | None = None,
        ) -> dict[str, list[str]]:
            return {
                "candidate": [],
                "watch": list(universe),
                "holdings": [],
                "universe": list(universe),
            }

        deps.build_pool = build_pool

        def score_universe(
            date: str,
            universe: list[str],
            data_reader: object = None,
            market: str = "ashare",
        ) -> list[tuple[str, dict[str, object]]]:
            evidence_sources = {
                "macro": {
                    "has_evidence": False,
                    "source": "MarketGraph regime",
                    "reason": "missing_regime",
                },
                "event": {
                    "has_evidence": False,
                    "source": "SharedSignals events + MarketGraph candidates",
                    "reason": "no_matched_event_evidence",
                },
                "fundamental": {
                    "has_evidence": False,
                    "source": "SharedSignals fundamentals/factors",
                    "reason": "missing_fundamental_rows",
                },
                "capital": {
                    "has_evidence": False,
                    "source": "SharedSignals capital flow/factors",
                    "reason": "missing_capital_flow_rows",
                },
                "technical": {
                    "has_evidence": False,
                    "source": "SharedSignals daily bars",
                    "reason": "insufficient_daily_bars",
                },
                "sentiment": {
                    "has_evidence": False,
                    "source": "SharedSignals/MarketGraph sentiment",
                    "reason": "missing_sentiment_rows",
                },
            }
            return [
                (
                    symbol,
                    {
                        "combined": 0.5,
                        "macro": 0.5,
                        "event": 0.5,
                        "fundamental": 0.5,
                        "capital": 0.5,
                        "technical": 0.5,
                        "sentiment": 0.5,
                        "evidence_coverage": 0.0,
                        "missing_evidence_dimensions": [
                            "macro",
                            "event",
                            "fundamental",
                            "capital",
                            "technical",
                            "sentiment",
                        ],
                        "evidence_sources": evidence_sources,
                    },
                )
                for symbol in universe
            ]

        deps.score_universe = score_universe

        result = run_sim_loop(
            MultiCandidateSimAdapter(
                ["000001.SZ", "000002.SZ"],
                max_candidates=2,
                score_universe_limit=2,
                max_portfolio_positions=2,
            ),
            "20260713",
            StubReader(),
            deps=deps,
            signals_dir=self.tmp_path / "signals_neutral_pool",
        )

        diagnostics = result["no_trade_explanation"]["score_diagnostics"]
        self.assertEqual(diagnostics["scored_count"], 2)
        self.assertEqual(diagnostics["all_neutral_symbol_count"], 2)
        self.assertEqual(
            diagnostics["data_quality_status"], "missing_evidence_default_like"
        )
        self.assertEqual(
            diagnostics["all_neutral_symbol_sample"], ["000001.SZ", "000002.SZ"]
        )
        self.assertEqual(
            diagnostics["missing_and_default_like_dimension_counts"]["capital"], 2
        )
        self.assertEqual(
            diagnostics["evidence_reason_summary"]["capital"][
                "missing_capital_flow_rows"
            ],
            2,
        )
        self.assertEqual(diagnostics["evidence_coverage_distribution"]["zero"], 2)
        self.assertEqual(
            diagnostics["all_missing_evidence_symbol_reason_sample"][0]["reasons"][
                "technical"
            ],
            "insufficient_daily_bars",
        )

    def test_run_sim_loop_ashare_fails_closed_when_candidate_pool_errors(self) -> None:
        deps = self._multi_candidate_deps()

        def build_pool(
            date: str,
            universe: list[str],
            market: str | None = None,
            reader: object | None = None,
        ) -> dict[str, list[str]]:
            raise RuntimeError("candidate pool unavailable")

        deps.build_pool = build_pool

        def score_universe(
            date: str,
            universe: list[str],
            data_reader: object = None,
            market: str = "ashare",
        ) -> list[tuple[str, dict[str, object]]]:
            return [
                (
                    symbol,
                    {
                        "combined": 0.95,
                        "sector": "unit",
                        "turnover_wan": 10000,
                        "capital_layer": "simulated",
                    },
                )
                for symbol in universe
            ]

        deps.score_universe = score_universe

        result = run_sim_loop(
            MultiCandidateSimAdapter(
                ["000001.SZ", "000002.SZ"],
                max_candidates=2,
                score_universe_limit=2,
                max_portfolio_positions=2,
            ),
            "20260713",
            StubReader(),
            deps=deps,
            signals_dir=self.tmp_path / "signals_pool_error",
        )

        self.assertEqual(result["candidate_count"], 0)
        self.assertEqual(result["filled_count"], 0)
        self.assertEqual(result["no_trade_explanation"]["category"], "no_candidates")
        self.assertTrue(
            any(
                error.get("stage") == "screening.candidate_pool"
                for error in result["errors"]
            )
        )
        self.assertEqual(self.executed_orders, [])

    def test_run_sim_loop_uses_market_capital_cash_for_ashare_capital_plan(
        self,
    ) -> None:
        deps = self._multi_candidate_deps()

        def score_universe(
            date: str,
            universe: list[str],
            data_reader: object = None,
            market: str = "ashare",
        ) -> list[tuple[str, dict[str, object]]]:
            return [
                (
                    symbol,
                    {
                        "combined": 0.86 - index * 0.02,
                        "sector": "unit",
                        "turnover_wan": 10000,
                        "capital_layer": "simulated",
                    },
                )
                for index, symbol in enumerate(universe)
            ]

        deps.score_universe = score_universe

        result = run_sim_loop(
            MultiCandidateSimAdapter(
                ["AAA", "BBB", "CCC"],
                max_candidates=3,
                score_universe_limit=3,
                max_portfolio_positions=3,
                cash_available=12_000.0,
            ),
            "20260713",
            StubReader(),
            deps=deps,
            signals_dir=self.tmp_path / "signals_cash_snapshot",
        )

        self.assertEqual(result["capital_plan"]["available_cash"], 45_000.0)
        self.assertEqual(
            result["capital_plan"]["cash_source"], "market_capital_authority"
        )
        self.assertEqual(result["capital_plan"]["max_new_positions"], 1)
        self.assertEqual(result["filled_count"], 1)
        self.assertLessEqual(
            self.executed_orders[0]["quantity"] * self.executed_orders[0]["price"],
            7_500.0,
        )
        self.assertLessEqual(
            self.executed_orders[0]["quantity"] * self.executed_orders[0]["price"],
            12_000.0,
        )

    def test_run_sim_loop_uses_unified_positions_for_ashare_capital_plan(
        self,
    ) -> None:
        deps = self._multi_candidate_deps()

        def score_universe(
            date: str,
            universe: list[str],
            data_reader: object = None,
            market: str = "ashare",
        ) -> list[tuple[str, dict[str, object]]]:
            return [
                (
                    symbol,
                    {
                        "combined": 0.82 - index * 0.02,
                        "sector": "unit",
                        "turnover_wan": 10000,
                        "capital_layer": "simulated",
                    },
                )
                for index, symbol in enumerate(universe)
            ]

        deps.score_universe = score_universe
        validation_positions = [
            {
                "ts_code": "000101.SZ",
                "quantity": 100,
                "sellable_quantity": 100,
                "avg_price": 10.0,
                "last_price": 10.0,
                "market_value": 1_000.0,
            },
            {
                "ts_code": "000102.SZ",
                "quantity": 100,
                "sellable_quantity": 100,
                "avg_price": 10.0,
                "last_price": 10.0,
                "market_value": 1_000.0,
            },
            {
                "ts_code": "000103.SZ",
                "quantity": 100,
                "sellable_quantity": 100,
                "avg_price": 10.0,
                "last_price": 10.0,
                "market_value": 1_000.0,
            },
        ]
        self._use_authority_positions(validation_positions)

        result = run_sim_loop(
            MultiCandidateSimAdapter(
                ["AAA", "BBB", "CCC"],
                max_candidates=3,
                score_universe_limit=3,
                max_portfolio_positions=3,
                positions=validation_positions,
                cash_available=47_000.0,
                strategy_positions=validation_positions,
                strategy_cash_available=47_000.0,
                sample_adjustment={
                    "view": "strategy_valid_samples_only",
                    "ignored_validation_sample_count": 3,
                    "reason": "chain_validation_samples_do_not_consume_strategy_capital",
                },
            ),
            "20260713",
            StubReader(),
            deps=deps,
            signals_dir=self.tmp_path / "signals_validation_capital_view",
        )

        self.assertEqual(result["capital_plan"]["available_cash"], 42_000.0)
        self.assertEqual(result["capital_plan"]["existing_position_count"], 3)
        self.assertEqual(
            result["capital_plan"]["sample_adjustment"][
                "ignored_validation_sample_count"
            ],
            3,
        )
        self.assertNotIn(
            "original_strategy_cash_available",
            result["capital_plan"]["sample_adjustment"],
        )
        self.assertEqual(
            result["capital_plan_decision"]["account_cash_available"], 42_000.0
        )
        self.assertEqual(
            result["adapter_account_diagnostics"]["cash_available"], 47_000.0
        )
        self.assertFalse(result["adapter_account_diagnostics"]["authoritative"])
        self.assertGreater(
            result["sample_pipeline"]["observation"]["prediction_count"],
            0,
        )

    def test_run_sim_loop_uses_sample_count_for_ashare_probe_position(self) -> None:
        deps = self._multi_candidate_deps()

        def score_universe(
            date: str,
            universe: list[str],
            data_reader: object = None,
            market: str = "ashare",
        ) -> list[tuple[str, dict[str, object]]]:
            return [
                (
                    symbol,
                    {
                        "combined": 0.60,
                        "sector": "unit",
                        "turnover_wan": 10000,
                        "capital_layer": "simulated",
                    },
                )
                for symbol in universe
            ]

        deps.score_universe = score_universe
        strategy_positions = [
            {
                "ts_code": "300759.SZ",
                "quantity": 500,
                "sellable_quantity": 0,
                "avg_price": 10.0,
                "last_price": 10.0,
                "market_value": 5_000.0,
            },
            {
                "ts_code": "600030.SH",
                "quantity": 500,
                "sellable_quantity": 0,
                "avg_price": 10.0,
                "last_price": 10.0,
                "market_value": 5_000.0,
            },
        ]
        self._use_authority_positions(strategy_positions)

        result = run_sim_loop(
            MultiCandidateSimAdapter(
                ["300418.SZ"],
                max_candidates=1,
                score_universe_limit=1,
                max_portfolio_positions=3,
                positions=strategy_positions,
                cash_available=40_000.0,
                strategy_positions=strategy_positions,
                strategy_cash_available=40_000.0,
                sample_adjustment={
                    "strategy_sample_valid_count": 2,
                    "min_strategy_samples": 5,
                },
            ),
            "20260713",
            StubReader(),
            deps=deps,
            signals_dir=self.tmp_path / "signals_sample_collection",
        )

        self.assertEqual(result["capital_plan"]["risk_mode"], "sample_collection")
        self.assertEqual(result["capital_plan"]["max_new_positions"], 1)
        self.assertEqual(result["capital_plan_decision"]["position_capacity"], 1)
        self.assertEqual(result["order_count"], 1)
        self.assertEqual(result["filled_count"], 1)
        self.assertEqual(self.executed_orders[0]["ts_code"], "300418.SZ")

    def test_manual_review_candidate_never_expands_runtime_risk(self) -> None:
        with self._approved_expansion_evidence("20260713"):
            result = run_sim_loop(
                MultiCandidateSimAdapter(
                    ["000001.SZ", "600000.SH", "300418.SZ"],
                    max_candidates=3,
                    score_universe_limit=3,
                    max_portfolio_positions=8,
                ),
                "20260713",
                StubReader(),
                deps=self._high_score_deps(),
                signals_dir=self.tmp_path / "signals_manual_review_only",
            )

        plan = result["capital_plan"]
        self.assertTrue(plan["evolution_decision"]["evidence_usable"])
        self.assertEqual(
            plan["evolution_decision"]["recommended_action"],
            "manual_review_only",
        )
        self.assertFalse(plan["automatic_promotion_enabled"])
        self.assertFalse(plan["automatic_risk_expansion_enabled"])
        self.assertFalse(plan["real_trading_enabled"])
        self.assertLessEqual(plan["planned_stock_exposure_cny"], 45_000.0)
        self.assertTrue(
            all(
                float(amount) <= 7_500.0
                for amount in plan["position_budget_by_symbol"].values()
            )
        )

    def test_run_sim_loop_attaches_latest_5min_bar_to_ashare_order(self) -> None:
        class IntradayReader(StubReader):
            def get_bars_intraday(
                self, market, symbol, interval="5m", start=None, end=None
            ):
                return [
                    {
                        "close": 10.6,
                        "bar_time": "2026-07-13 10:05:00",
                        "volume": 1800,
                        "provider": "sharedsignals_api_realtime_5min",
                    }
                ]

        deps = self._multi_candidate_deps()

        result = run_sim_loop(
            MultiCandidateSimAdapter(
                ["600000.SH"],
                max_candidates=1,
                score_universe_limit=1,
                max_portfolio_positions=1,
            ),
            "20260713",
            IntradayReader(),
            deps=deps,
            signals_dir=self.tmp_path / "signals_5min_evidence",
        )

        self.assertEqual(result["filled_count"], 1)
        snapshot = self.executed_orders[0]["market_snapshot"]
        self.assertEqual(snapshot["bar_time"], "2026-07-13 10:05:00")
        self.assertEqual(snapshot["bar_volume"], 1800)
        self.assertEqual(snapshot["provider"], "sharedsignals_api_realtime_5min")

    def test_run_sim_loop_uses_each_execution_symbol_for_ashare_5min_evidence(
        self,
    ) -> None:
        class IntradayReader(StubReader):
            def get_bars_intraday(
                self, market, symbol, interval="5m", start=None, end=None
            ):
                if symbol == "000001.SZ":
                    return [
                        {
                            "close": 10.1,
                            "bar_time": "2026-07-13 10:05:00",
                            "volume": 1800,
                        }
                    ]
                if symbol == "600000.SH":
                    return [
                        {
                            "close": 20.2,
                            "bar_time": "2026-07-13 10:10:00",
                            "volume": 1900,
                        }
                    ]
                return []

        deps = self._multi_candidate_deps()

        def score_universe(date, universe, data_reader=None, market="ashare"):
            return [
                (
                    "000001.SZ",
                    {
                        "combined": 0.80,
                        "sector": "unit",
                        "turnover_wan": 10000,
                        "capital_layer": "simulated",
                    },
                ),
                (
                    "600000.SH",
                    {
                        "combined": 0.70,
                        "sector": "unit",
                        "turnover_wan": 10000,
                        "capital_layer": "simulated",
                    },
                ),
            ]

        deps.score_universe = score_universe
        result = run_sim_loop(
            MultiCandidateSimAdapter(
                ["000001.SZ", "600000.SH"],
                max_candidates=2,
                score_universe_limit=2,
                max_portfolio_positions=1,
            ),
            "20260713",
            IntradayReader(),
            deps=deps,
            signals_dir=self.tmp_path / "signals_5min_execution_symbol",
        )

        self.assertEqual(result["filled_count"], 1)
        self.assertEqual(self.executed_orders[0]["ts_code"], "000001.SZ")
        self.assertEqual(self.executed_orders[0]["market_snapshot"]["last_price"], 10.1)
        self.assertEqual(
            self.executed_orders[0]["market_snapshot"]["bar_time"],
            "2026-07-13 10:05:00",
        )

    def test_execution_snapshot_rejects_stale_current_ashare_bar(self) -> None:
        from shared.orchestrator import _latest_execution_market_snapshot

        class IntradayReader:
            def get_bars_intraday(
                self, market, symbol, interval="5m", start=None, end=None
            ):
                return [
                    {"close": 10.1, "bar_time": "2026-07-10 10:10:00", "volume": 1800}
                ]

        snapshot = _latest_execution_market_snapshot(
            IntradayReader(),
            "ashare",
            "000001.SZ",
            "20260710",
            "buy",
            now=datetime.fromisoformat("2026-07-10T10:54:00+08:00"),
        )

        self.assertEqual(snapshot, {})

    def test_run_sim_loop_does_not_let_retired_daily_sample_gate_override_capacity(
        self,
    ) -> None:
        deps = self._multi_candidate_deps()

        def score_universe(
            date: str,
            universe: list[str],
            data_reader: object = None,
            market: str = "ashare",
        ) -> list[tuple[str, dict[str, object]]]:
            return [
                (
                    symbol,
                    {
                        "combined": 0.60,
                        "sector": "unit",
                        "turnover_wan": 10000,
                        "capital_layer": "simulated",
                    },
                )
                for symbol in universe
            ]

        deps.score_universe = score_universe
        strategy_positions = [
            {
                "ts_code": f"{index:06d}.SZ",
                "quantity": 500,
                "sellable_quantity": 500,
                "avg_price": 10.0,
                "last_price": 10.0,
                "market_value": 5_000.0,
            }
            for index in range(1, 9)
        ]
        self._use_authority_positions(strategy_positions)
        decision = {
            "report_type": "ashare_evolution_decision_v2",
            "evidence_source": "sample_journal_kpi",
            "evidence_trade_date": "20260713",
            "trade_date": "20260713",
            "authority_scope": {
                "capital_authority_id": "ashare-capital-v1",
                "authority_generation": 1,
                "execution_lineage_id": "ashare-sim-fresh-20260712-v1",
            },
            "evidence_usable": True,
            "state": "manual_review_candidate",
            "recommended_action": "force_sample_collection",
            "policy": {
                "daily_sample_hard_gate": True,
                "daily_strategy_sample_target": 1,
                "today_strategy_sample_count": 0,
                "strategy_sample_count": 8,
                "min_strategy_samples": 5,
                "sample_collection_min_score": 0.55,
                "automatic_promotion_enabled": False,
                "automatic_risk_expansion_enabled": False,
            },
            "metrics": {"completed_round_trip_count": 20},
        }

        with patch(
            "Ashare.evolution_controller.load_latest_decision", return_value=decision
        ):
            result = run_sim_loop(
                MultiCandidateSimAdapter(
                    ["300418.SZ"],
                    max_candidates=1,
                    score_universe_limit=1,
                    max_portfolio_positions=8,
                    positions=strategy_positions,
                    cash_available=10_000.0,
                    strategy_positions=strategy_positions,
                    strategy_cash_available=10_000.0,
                    sample_adjustment={
                        "strategy_sample_valid_count": 8,
                        "min_strategy_samples": 5,
                    },
                ),
                "20260713",
                StubReader(),
                deps=deps,
                signals_dir=self.tmp_path / "signals_daily_sample_gate",
            )

        self.assertNotEqual(result["capital_plan"]["risk_mode"], "sample_collection")
        self.assertNotIn(
            "daily_strategy_sample_target_not_met", result["capital_plan"]["reasons"]
        )
        self.assertEqual(result["order_count"], 0)
        self.assertEqual(result["filled_count"], 0)
        self.assertIn(
            result["capital_plan"]["capacity_reason"],
            {
                "position_capacity_reached",
                "target_positions_reached",
                "no_execution_eligible_candidates",
            },
        )
        self.assertIn(
            result["candidate_decision_trace"][0]["drop_reason"],
            {
                "position_capacity_reached",
                "target_positions_reached",
                "no_execution_eligible_candidates",
                "position_capacity_limit",
            },
        )
        self.assertEqual(self.executed_orders, [])

    def test_stale_three_position_adapter_limit_does_not_force_liquidation(
        self,
    ) -> None:
        deps = self._multi_candidate_deps()

        def score_universe(
            date: str,
            universe: list[str],
            data_reader: object = None,
            market: str = "ashare",
        ) -> list[tuple[str, dict[str, object]]]:
            return [
                (
                    symbol,
                    {
                        "combined": 0.86 - index * 0.03,
                        "sector": "unit",
                        "turnover_wan": 10000,
                        "capital_layer": "simulated",
                    },
                )
                for index, symbol in enumerate(universe)
            ]

        deps.score_universe = score_universe
        positions = [
            {
                "ts_code": f"{i + 1:06d}.SZ",
                "quantity": 100,
                "sellable_quantity": 100,
                "avg_price": 10.0,
                "last_price": 10.0,
                "weight": 0.08,
            }
            for i in range(5)
        ]
        self._use_authority_positions(positions)

        with self._approved_expansion_evidence("20260713"):
            result = run_sim_loop(
                MultiCandidateSimAdapter(
                    ["AAA", "BBB", "CCC"],
                    max_candidates=3,
                    score_universe_limit=3,
                    max_portfolio_positions=3,
                    positions=positions,
                ),
                "20260713",
                StubReader(),
                deps=deps,
                signals_dir=self.tmp_path / "signals_rebalance",
            )

        sell_orders = [
            order for order in self.executed_orders if order["side"] == "sell"
        ]
        self.assertEqual(result["capital_plan"]["target_positions"], 8)
        self.assertEqual(result["rebalance"]["planned_sell_count"], 0)
        self.assertEqual(len(sell_orders), 0)
        self.assertEqual(result["order_count"], 0)
        self.assertEqual(result["capital_plan_log"]["status"], "written")
        self.assertEqual(
            result["post_execution_capital_plan_refresh"]["status"], "skipped"
        )
        log_path = Path(result["capital_plan_log"]["path"])
        rows = [
            json.loads(line)
            for line in log_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(rows[0]["rebalance"]["planned_sell_count"], 0)
        self.assertEqual(rows[0]["capital_plan"]["target_positions"], 8)

    def test_run_sim_loop_sells_stop_loss_ashare_position_even_within_target_count(
        self,
    ) -> None:
        deps = self._multi_candidate_deps()

        def score_universe(
            date: str,
            universe: list[str],
            data_reader: object = None,
            market: str = "ashare",
        ) -> list[tuple[str, dict[str, object]]]:
            return [
                (
                    symbol,
                    {
                        "combined": 0.86,
                        "sector": "unit",
                        "turnover_wan": 10000,
                        "capital_layer": "simulated",
                    },
                )
                for symbol in universe
            ]

        deps.score_universe = score_universe
        positions = [
            {
                "ts_code": "000010.SZ",
                "quantity": 100,
                "sellable_quantity": 100,
                "avg_price": 12.0,
                "last_price": 10.0,
                "weight": 0.08,
            }
        ]
        self._use_authority_positions(positions)

        result = run_sim_loop(
            MultiCandidateSimAdapter(
                ["AAA", "BBB", "CCC"],
                max_candidates=3,
                score_universe_limit=3,
                max_portfolio_positions=3,
                positions=positions,
            ),
            "20260713",
            StubReader(),
            deps=deps,
            signals_dir=self.tmp_path / "signals_stop_loss",
        )

        sell_orders = [
            order for order in self.executed_orders if order["side"] == "sell"
        ]
        self.assertEqual(result["rebalance"]["planned_sell_count"], 1)
        self.assertEqual(sell_orders[0]["ts_code"], "000010.SZ")
        self.assertIn("stop_loss", sell_orders[0]["note"])

    def test_run_sim_loop_pause_preserves_sell_and_blocks_new_buy(self) -> None:
        deps = self._multi_candidate_deps()

        def score_universe(
            date: str,
            universe: list[str],
            data_reader: object = None,
            market: str = "ashare",
        ) -> list[tuple[str, dict[str, object]]]:
            return [
                (
                    symbol,
                    {
                        "combined": 0.86,
                        "sector": "unit",
                        "turnover_wan": 10_000,
                        "capital_layer": "simulated",
                    },
                )
                for symbol in universe
            ]

        deps.score_universe = score_universe
        positions = [
            {
                "ts_code": "000010.SZ",
                "quantity": 100,
                "sellable_quantity": 100,
                "avg_price": 12.0,
                "last_price": 10.0,
                "weight": 0.02,
            }
        ]
        self.master_state_loader.side_effect = lambda market, requested_date: (
            _ashare_market_state(
                requested_date,
                positions=positions,
                daily_mtm_change=-1_500.0,
            )
        )

        result = run_sim_loop(
            MultiCandidateSimAdapter(
                ["000020.SZ"],
                max_candidates=1,
                score_universe_limit=1,
                max_portfolio_positions=3,
                positions=positions,
            ),
            "20260713",
            StubReader(),
            deps=deps,
            signals_dir=self.tmp_path / "signals_pause_preserves_sell",
        )

        sell_orders = [
            order for order in self.executed_orders if order["side"] == "sell"
        ]
        buy_orders = [order for order in self.executed_orders if order["side"] == "buy"]
        self.assertEqual(result["ashare_position_authority"]["status"], "verified")
        self.assertEqual(result["ashare_position_authority"]["position_count"], 1)
        self.assertFalse(result["ashare_capital_state"]["new_risk_allowed"])
        self.assertEqual(
            result["ashare_capital_state_reason"],
            "ashare_capital_daily_loss_pause",
        )
        self.assertFalse(result["capital_plan"]["new_risk_allowed"])
        self.assertEqual(result["capital_plan"]["max_new_positions"], 0)
        self.assertEqual(result["rebalance"]["planned_sell_count"], 1)
        self.assertEqual(len(sell_orders), 1)
        self.assertEqual(sell_orders[0]["ts_code"], "000010.SZ")
        self.assertIn("stop_loss", sell_orders[0]["note"])
        self.assertEqual(buy_orders, [])
        self.assertNotIn("risk", self.calls)
        self.assertEqual(self.master_reserver.call_count, 0)

    def test_run_sim_loop_uses_authority_aggregated_position_for_sell_order(
        self,
    ) -> None:
        deps = self._multi_candidate_deps()

        def score_universe(
            date: str,
            universe: list[str],
            data_reader: object = None,
            market: str = "ashare",
        ) -> list[tuple[str, dict[str, object]]]:
            return [
                (
                    symbol,
                    {
                        "combined": 0.86,
                        "sector": "unit",
                        "turnover_wan": 10000,
                        "capital_layer": "simulated",
                    },
                )
                for symbol in universe
            ]

        deps.score_universe = score_universe
        positions = [
            {
                "ts_code": "000010.SZ",
                "quantity": 500,
                "sellable_quantity": 500,
                "avg_price": 12.0,
                "last_price": 10.0,
                "weight": 0.10,
            },
        ]
        self._use_authority_positions(positions)

        result = run_sim_loop(
            MultiCandidateSimAdapter(
                ["AAA"],
                max_candidates=1,
                score_universe_limit=1,
                max_portfolio_positions=3,
                positions=positions,
            ),
            "20260713",
            StubReader(),
            deps=deps,
            signals_dir=self.tmp_path / "signals_duplicate_lots",
        )

        sell_orders = [
            order for order in self.executed_orders if order["side"] == "sell"
        ]
        self.assertEqual(result["rebalance"]["planned_sell_count"], 1)
        self.assertEqual(len(sell_orders), 1)
        self.assertEqual(sell_orders[0]["ts_code"], "000010.SZ")
        self.assertEqual(sell_orders[0]["quantity"], 500)

    def test_run_sim_loop_does_not_liquidate_normal_positions_without_exit_trigger(
        self,
    ) -> None:
        deps = self._multi_candidate_deps()

        def score_universe(
            date: str,
            universe: list[str],
            data_reader: object = None,
            market: str = "ashare",
        ) -> list[tuple[str, dict[str, object]]]:
            return [
                (
                    symbol,
                    {
                        "combined": 0.50,
                        "sector": "unit",
                        "turnover_wan": 10000,
                        "capital_layer": "simulated",
                    },
                )
                for symbol in universe
            ]

        deps.score_universe = score_universe
        positions = [
            {
                "ts_code": "000010.SZ",
                "quantity": 100,
                "sellable_quantity": 100,
                "avg_price": 10.0,
                "last_price": 10.0,
                "weight": 0.08,
            },
            {
                "ts_code": "000011.SZ",
                "quantity": 100,
                "sellable_quantity": 100,
                "avg_price": 10.0,
                "last_price": 10.0,
                "weight": 0.08,
            },
        ]
        self._use_authority_positions(positions)

        result = run_sim_loop(
            MultiCandidateSimAdapter(
                ["AAA", "BBB"],
                max_candidates=2,
                score_universe_limit=2,
                max_portfolio_positions=3,
                positions=positions,
            ),
            "20260713",
            StubReader(),
            deps=deps,
            signals_dir=self.tmp_path / "signals_defensive_hold",
        )

        self.assertEqual(result["rebalance"]["planned_sell_count"], 0)
        self.assertEqual(
            [order for order in self.executed_orders if order["side"] == "sell"],
            [],
        )

    def test_run_sim_loop_does_not_buy_same_symbol_planned_for_rebalance_sell(
        self,
    ) -> None:
        deps = self._multi_candidate_deps()

        def score_universe(
            date: str,
            universe: list[str],
            data_reader: object = None,
            market: str = "ashare",
        ) -> list[tuple[str, dict[str, object]]]:
            scores = {"000010.SZ": 0.92, "000011.SZ": 0.88, "000012.SZ": 0.84}
            return [
                (
                    symbol,
                    {
                        "combined": scores[symbol],
                        "sector": "unit",
                        "turnover_wan": 10000,
                        "capital_layer": "simulated",
                    },
                )
                for symbol in universe
            ]

        deps.score_universe = score_universe
        positions = [
            {
                "ts_code": "000010.SZ",
                "quantity": 100,
                "sellable_quantity": 100,
                "avg_price": 12.0,
                "last_price": 10.0,
                "weight": 0.08,
            }
        ]
        self._use_authority_positions(positions)

        with self._approved_expansion_evidence("20260713"):
            result = run_sim_loop(
                MultiCandidateSimAdapter(
                    ["000010.SZ", "000011.SZ", "000012.SZ"],
                    max_candidates=3,
                    score_universe_limit=3,
                    max_portfolio_positions=3,
                    positions=positions,
                ),
                "20260713",
                StubReader(),
                deps=deps,
                signals_dir=self.tmp_path / "signals_no_round_trip",
            )

        sell_orders = [
            order for order in self.executed_orders if order["side"] == "sell"
        ]
        buy_orders = [order for order in self.executed_orders if order["side"] == "buy"]
        self.assertEqual(result["rebalance"]["planned_sell_count"], 1)
        self.assertEqual(sell_orders[0]["ts_code"], "000010.SZ")
        self.assertNotIn("000010.SZ", [order["ts_code"] for order in buy_orders])
        self.assertEqual(
            [order["ts_code"] for order in buy_orders], ["000011.SZ", "000012.SZ"]
        )

    def test_run_sim_loop_replaces_full_position_after_stop_loss_sell(self) -> None:
        deps = self._multi_candidate_deps()

        def score_universe(
            date: str,
            universe: list[str],
            data_reader: object = None,
            market: str = "ashare",
        ) -> list[tuple[str, dict[str, object]]]:
            scores = {"000013.SZ": 0.91, "000014.SZ": 0.82}
            return [
                (
                    symbol,
                    {
                        "combined": scores[symbol],
                        "sector": "unit",
                        "turnover_wan": 10000,
                        "capital_layer": "simulated",
                    },
                )
                for symbol in universe
            ]

        deps.score_universe = score_universe
        positions = [
            {
                "ts_code": "000010.SZ",
                "quantity": 500,
                "sellable_quantity": 500,
                "avg_price": 12.0,
                "last_price": 10.0,
                "weight": 0.10,
            },
            {
                "ts_code": "000011.SZ",
                "quantity": 500,
                "sellable_quantity": 500,
                "avg_price": 10.0,
                "last_price": 10.0,
                "weight": 0.10,
            },
            {
                "ts_code": "000012.SZ",
                "quantity": 500,
                "sellable_quantity": 500,
                "avg_price": 10.0,
                "last_price": 10.0,
                "weight": 0.10,
            },
        ]
        self._use_authority_positions(positions)

        with self._approved_expansion_evidence("20260713"):
            result = run_sim_loop(
                MultiCandidateSimAdapter(
                    ["000013.SZ", "000014.SZ"],
                    max_candidates=2,
                    score_universe_limit=2,
                    max_portfolio_positions=3,
                    positions=positions,
                ),
                "20260713",
                StubReader(),
                deps=deps,
                signals_dir=self.tmp_path / "signals_replacement_after_sell",
            )

        sell_orders = [
            order for order in self.executed_orders if order["side"] == "sell"
        ]
        buy_orders = [order for order in self.executed_orders if order["side"] == "buy"]
        self.assertEqual(result["rebalance"]["planned_sell_count"], 1)
        self.assertEqual(sell_orders[0]["ts_code"], "000010.SZ")
        self.assertEqual([order["ts_code"] for order in buy_orders], ["000013.SZ"])
        self.assertLessEqual(
            buy_orders[0]["quantity"] * buy_orders[0]["price"],
            sell_orders[0]["quantity"] * sell_orders[0]["price"],
        )
        self.assertLessEqual(
            buy_orders[0]["quantity"] * buy_orders[0]["price"],
            7_500.0,
        )

    def test_run_sim_loop_replaces_full_position_for_opportunity_cost_gap(self) -> None:
        deps = self._multi_candidate_deps()

        def score_universe(
            date: str,
            universe: list[str],
            data_reader: object = None,
            market: str = "ashare",
        ) -> list[tuple[str, dict[str, object]]]:
            scores = {"000010.SZ": 0.60, "000013.SZ": 0.84, "000014.SZ": 0.76}
            return [
                (
                    symbol,
                    {
                        "combined": scores[symbol],
                        "sector": "unit",
                        "turnover_wan": 10000,
                        "capital_layer": "simulated",
                    },
                )
                for symbol in universe
            ]

        deps.score_universe = score_universe
        positions = [
            {
                "ts_code": "000010.SZ",
                "quantity": 500,
                "sellable_quantity": 500,
                "avg_price": 10.0,
                "last_price": 10.0,
                "market_value": 5_000.0,
            }
        ]
        self._use_authority_positions(positions)

        result = run_sim_loop(
            MultiCandidateSimAdapter(
                ["000010.SZ", "000013.SZ", "000014.SZ"],
                max_candidates=3,
                score_universe_limit=3,
                max_portfolio_positions=1,
                positions=positions,
            ),
            "20260713",
            StubReader(),
            deps=deps,
            signals_dir=self.tmp_path / "signals_opportunity_cost",
        )

        sell_orders = [
            order for order in self.executed_orders if order["side"] == "sell"
        ]
        buy_orders = [order for order in self.executed_orders if order["side"] == "buy"]
        self.assertEqual(result["rebalance"]["planned_sell_count"], 1)
        self.assertEqual(sell_orders[0]["ts_code"], "000010.SZ")
        self.assertIn("opportunity_cost", sell_orders[0]["note"])
        self.assertEqual([order["ts_code"] for order in buy_orders], ["000013.SZ"])
        self.assertLessEqual(
            buy_orders[0]["quantity"] * buy_orders[0]["price"], 7_500.0
        )
        self.assertLessEqual(
            result["capital_plan"]["planned_stock_exposure_cny"], 45_000.0
        )

    def test_run_sim_loop_keeps_full_position_when_opportunity_gap_is_small(
        self,
    ) -> None:
        deps = self._multi_candidate_deps()

        def score_universe(
            date: str,
            universe: list[str],
            data_reader: object = None,
            market: str = "ashare",
        ) -> list[tuple[str, dict[str, object]]]:
            scores = {"000010.SZ": 0.70, "000013.SZ": 0.82}
            return [
                (
                    symbol,
                    {
                        "combined": scores[symbol],
                        "sector": "unit",
                        "turnover_wan": 10000,
                        "capital_layer": "simulated",
                    },
                )
                for symbol in universe
            ]

        deps.score_universe = score_universe
        positions = [
            {
                "ts_code": "000010.SZ",
                "quantity": 500,
                "sellable_quantity": 500,
                "avg_price": 10.0,
                "last_price": 10.0,
                "weight": 0.10,
            }
        ]
        self._use_authority_positions(positions)

        result = run_sim_loop(
            MultiCandidateSimAdapter(
                ["000010.SZ", "000013.SZ"],
                max_candidates=2,
                score_universe_limit=2,
                max_portfolio_positions=1,
                positions=positions,
            ),
            "20260713",
            StubReader(),
            deps=deps,
            signals_dir=self.tmp_path / "signals_opportunity_gap_small",
        )

        self.assertEqual(result["rebalance"]["planned_sell_count"], 0)
        self.assertEqual(self.executed_orders, [])

    def test_run_sim_loop_persists_exclusions_even_when_some_orders_fill(self) -> None:
        class SelectiveReader:
            def get_bars_daily(
                self, market: str, symbol: str, start: object = None, end: object = None
            ) -> list[dict[str, float]]:
                if symbol == "BAD":
                    return []
                return [{"close": 10.0}]

        deps = self._multi_candidate_deps()

        def score_universe(
            date: str,
            universe: list[str],
            data_reader: object = None,
            market: str = "ashare",
        ) -> list[tuple[str, dict[str, object]]]:
            return [
                (
                    symbol,
                    {
                        "combined": 0.9 if symbol == "GOOD" else 0.8,
                        "sector": "unit",
                        "turnover_wan": 10000,
                        "capital_layer": "simulated",
                    },
                )
                for symbol in universe
            ]

        deps.score_universe = score_universe

        result = run_sim_loop(
            MultiCandidateSimAdapter(
                ["GOOD", "BAD"],
                max_candidates=2,
                score_universe_limit=2,
                max_portfolio_positions=2,
            ),
            "20260713",
            SelectiveReader(),
            deps=deps,
            signals_dir=self.tmp_path / "signals_exclusions",
        )

        self.assertEqual(result["filled_count"], 1)
        self.assertEqual(result["skipped_candidate_count"], 1)
        self.assertEqual(result["execution_exclusion_log"]["rows"], 1)
        exclusion_path = Path(result["execution_exclusion_log"]["path"])
        rows = [
            json.loads(line)
            for line in exclusion_path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertIn(
            rows[0]["kind"], {"skipped_candidate", "risk_rejection", "execution_skip"}
        )
        self.assertEqual(rows[0]["symbol"], "BAD")

    def test_adapter_strategy_cash_cannot_override_authoritative_50k_account(
        self,
    ) -> None:
        """Adapter cash is diagnostic only for the one server-local account."""
        deps = self._multi_candidate_deps()

        def score_universe(
            date: str,
            universe: list[str],
            data_reader: object = None,
            market: str = "ashare",
        ) -> list[tuple[str, dict[str, object]]]:
            return [
                (
                    "300418.SZ",
                    {
                        "combined": 0.80,
                        "sector": "unit",
                        "turnover_wan": 10000,
                        "capital_layer": "simulated",
                    },
                )
            ]

        deps.score_universe = score_universe

        positions = [
            {
                "ts_code": "300759.SZ",
                "quantity": 100,
                "sellable_quantity": 0,
                "avg_price": 30.34,
                "last_price": 30.31,
                "market_value": 3_031.0,
            },
            {
                "ts_code": "600030.SH",
                "quantity": 100,
                "sellable_quantity": 0,
                "avg_price": 28.03,
                "last_price": 28.00,
                "market_value": 2_800.0,
            },
        ]
        self._use_authority_positions(positions)
        # Every source replays the same sub-50k account; adapter cash remains
        # diagnostic and cannot fabricate the retired 200k strategy account.
        result = run_sim_loop(
            MultiCandidateSimAdapter(
                ["300418.SZ"],
                max_candidates=1,
                score_universe_limit=1,
                max_portfolio_positions=3,
                positions=positions,
                cash_available=44_169.0,
                strategy_positions=positions,
                strategy_cash_available=44_169.0,
                sample_adjustment={
                    "view": "strategy_valid_samples_only",
                    "ignored_validation_sample_count": 0,
                    "reason": "validation_samples_do_not_consume_strategy_capital",
                },
            ),
            "20260713",
            StubReader(),
            deps=deps,
            signals_dir=self.tmp_path / "signals_strategy_cash_capped",
        )

        self.assertEqual(result["capital_plan"]["available_cash"], 39_169.0)
        self.assertEqual(
            result["capital_plan"]["cash_source"], "market_capital_authority"
        )
        self.assertGreaterEqual(result["capital_plan"]["max_new_positions"], 0)
        sample_adj = result["capital_plan"].get("sample_adjustment", {})
        self.assertNotIn("original_strategy_cash_available", sample_adj)
        self.assertEqual(
            result["adapter_account_diagnostics"]["cash_available"], 44_169.0
        )
        self.assertFalse(result["adapter_account_diagnostics"]["authoritative"])

    def test_ashare_sell_orders_use_ashare_rebalance_sell_candidate_pool_layer(
        self,
    ) -> None:
        """Risk-3 regression: A-share sell orders must carry
        candidate_pool_layer='ashare_rebalance_sell'."""
        deps = self._multi_candidate_deps()

        def score_universe(
            date: str,
            universe: list[str],
            data_reader: object = None,
            market: str = "ashare",
        ) -> list[tuple[str, dict[str, object]]]:
            scores = {"000010.SZ": 0.60, "000013.SZ": 0.84}
            return [
                (
                    symbol,
                    {
                        "combined": scores[symbol],
                        "sector": "unit",
                        "turnover_wan": 10000,
                        "capital_layer": "simulated",
                    },
                )
                for symbol in universe
            ]

        deps.score_universe = score_universe
        positions = [
            {
                "ts_code": "000010.SZ",
                "quantity": 500,
                "sellable_quantity": 500,
                "avg_price": 10.0,
                "last_price": 10.0,
                "market_value": 5_000.0,
            }
        ]
        self._use_authority_positions(positions)

        run_sim_loop(
            MultiCandidateSimAdapter(
                ["000010.SZ", "000013.SZ"],
                max_candidates=2,
                score_universe_limit=2,
                max_portfolio_positions=1,
                positions=positions,
            ),
            "20260713",
            StubReader(),
            deps=deps,
            signals_dir=self.tmp_path / "signals_rebalance_naming",
        )

        sell_orders = [
            order for order in self.executed_orders if order["side"] == "sell"
        ]
        self.assertEqual(len(sell_orders), 1)
        sell = sell_orders[0]
        self.assertEqual(sell["candidate_pool_layer"], "ashare_rebalance_sell")
        self.assertEqual(sell["execution_source"], "ashare_rebalance_sell")
        self.assertIn("opportunity_cost", sell["note"])


if __name__ == "__main__":
    unittest.main()
