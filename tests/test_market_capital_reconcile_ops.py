from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from CNFutures import sim_runner as cn_sim_runner
from shared.capital import market_ledger as market_ledger_module
from shared.capital.market_ledger import (
    CN_FUTURES_CONTRACT_SPEC_VERSION,
    MarketCapitalFillCommitRequest,
    MarketCapitalLedger,
    MarketCapitalReservationRequest,
    OpeningStateManifest,
    cn_futures_contract_spec_sha256,
)
from shared.capital.market_policy import (
    PINNED_CUTOVER_DECISION_ID,
    PINNED_SOURCE_THREAD_ID,
    MarketPolicy,
)
from shared.execution.execution_lineage import (
    ASHARE_EXECUTION_LINEAGE_ID,
    build_execution_lineage,
)
from shared.execution import local_sim_ledger
from shared.review.sample_journal import SampleJournal
from shared.runtime_test import market_capital_reconcile_ops as reconcile_ops


TRADE_DATE = "20260712"
OPEN_PIT = "2026-07-12T15:00:00+08:00"
OPS_PIT = "2026-07-12T15:05:00+08:00"
CLOSE_PIT = "2026-07-12T15:32:00+08:00"
ASHARE_LINEAGE = ASHARE_EXECUTION_LINEAGE_ID
CN_LINEAGE = "cn-futures-sim-fresh-20260712-v1"


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json_sha(payload: dict, *, excluded: set[str] | None = None) -> str:
    canonical = {
        key: value for key, value in payload.items() if key not in (excluded or set())
    }
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _mark_evidence(price: float, symbol: str) -> dict:
    source_row = {
        "symbol": symbol,
        "close": price,
        "bar_time": "2026-07-12T14:59:00+08:00",
        "source": "SharedSignals/test_5min",
    }
    return {
        "price": price,
        "observed_at": source_row["bar_time"],
        "point_in_time_as_of": OPEN_PIT,
        "source": source_row["source"],
        "source_owner": "SharedSignals",
        "source_row": source_row,
        "source_row_sha256": _json_sha(source_row),
        "cadence": "intraday_5min",
        "real_trading_enabled": False,
    }


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _init_ledger(tmp_path: Path, market: str) -> MarketCapitalLedger:
    tmp_path.mkdir(parents=True, exist_ok=True)
    policy = MarketPolicy.load(market)
    lineage = ASHARE_LINEAGE if market == "ashare" else CN_LINEAGE
    root = tmp_path / f"{market}-capital"
    legacy_events = tmp_path / f"{market}-legacy-events.jsonl"
    legacy_events.write_text(
        json.dumps({"event_id": f"LEGACY-{market}"}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    legacy_archive = tmp_path / f"{market}-legacy-archive"
    legacy_archive.mkdir()
    ledger = MarketCapitalLedger(root, policy=policy)
    ledger.initialize(
        OpeningStateManifest(
            market=market,
            authority_id=policy.capital_authority_id,
            cutover_decision_id=PINNED_CUTOVER_DECISION_ID,
            mode="fresh_start",
            as_of=TRADE_DATE,
            cash_balance_cny=50_000.0,
            opening_equity_cny=50_000.0,
            active_reservations_cny=0.0,
            consecutive_losses=0,
            inherited_high_water_equity_cny=0.0,
            positions_by_risk_unit={},
            position_margin_by_risk_unit={},
            frozen_order_cash_cny=0.0,
            realized_pnl_cny=0.0,
            unrealized_pnl_cny=0.0,
            source="test-fresh-opening",
            source_sha256=_sha(f"opening:{market}"),
            execution_lineage_id=lineage,
            real=False,
        ),
        cutover_manifest={
            "cutover_decision_id": PINNED_CUTOVER_DECISION_ID,
            "source_thread_id": PINNED_SOURCE_THREAD_ID,
            "cutover_state": "fresh_start_approved",
            "authority_generation": 1,
        },
        legacy_freeze_manifest={
            "events_path": str(legacy_events.resolve()),
            "sha256": hashlib.sha256(legacy_events.read_bytes()).hexdigest(),
            "last_event_id": f"LEGACY-{market}",
            "row_count": 1,
            "frozen_at": "2026-07-12T00:00:00+08:00",
            "archive_path": str(legacy_archive.resolve()),
            "imported": False,
        },
    )
    return ledger


def _ashare_lineage() -> dict:
    return build_execution_lineage(
        lineage_started_at="2026-07-12T08:00:00+08:00",
        point_in_time_as_of="2026-07-12T08:55:00+08:00",
    )


def _write_ashare_source(
    root: Path,
    *,
    cash: float = 50_000.0,
    positions: dict | None = None,
    actions: list[dict] | None = None,
    source: str = "server_local_sim_backup",
    real_trading_enabled: bool = False,
    extra_snapshot: dict | None = None,
) -> Path:
    positions = dict(positions or {})
    actions = list(actions or [])
    lineage = _ashare_lineage()
    root.mkdir(parents=True, exist_ok=True)
    manifest = {
        **lineage,
        "source": "fresh_zero_import_bootstrap",
        "initial_cash_cny": 50_000.0,
        "imported_legacy_record_count": 0,
        "legacy_roots_read": [],
        "created_at": "2026-07-12T08:00:00+08:00",
        "real_trading_enabled": False,
    }
    _write_json(root / "execution_lineage_manifest.json", manifest)
    account = "ashare_sim"
    market_value = round(
        sum(float(row["market_value"]) for row in positions.values()), 6
    )
    unrealized = round(
        sum(float(row["unrealized_pnl"]) for row in positions.values()), 6
    )
    pnl = {
        **lineage,
        "account": account,
        "cash_available": cash,
        "market_value": market_value,
        "realized_pnl": round(
            cash
            + sum(float(row["cost_basis"]) for row in positions.values())
            - 50_000.0,
            6,
        ),
        "unrealized_pnl": unrealized,
        "total_pnl": 0.0,
        "positions": positions,
        "total_trades": len(actions),
        "real_trading_enabled": False,
    }
    flat = [
        {
            "account": account,
            "ts_code": symbol,
            "quantity": row["quantity"],
            "avg_price": row.get("avg_cost", 0.0),
            "last_price": row["last_price"],
            "mark_price": row["mark_price"],
            "market_value": row["market_value"],
            "unrealized_pnl": row["unrealized_pnl"],
            "capital_layer": "simulated",
            "account_type": "simulated",
            "source": "server_local_sim_backup",
        }
        for symbol, row in sorted(positions.items())
    ]
    snapshot = {
        **lineage,
        "snapshot_id": "simulated_ashare_positions",
        "market": "ashare",
        "account_type": "simulated",
        "capital_layer": "simulated",
        "source": source,
        "synced_at": "2026-07-12T06:59:00+00:00",
        "positions": flat,
        "positions_by_account": {account: positions},
        "pnl": {account: pnl},
        "account_view": "strategy_samples_only",
        "audit_positions_by_account": {account: positions},
        "audit_pnl": {account: pnl},
        "mark_evidence_by_symbol": {
            symbol.upper(): _mark_evidence(float(row["mark_price"]), symbol.upper())
            for symbol, row in positions.items()
        },
        "real_trading_enabled": real_trading_enabled,
        **(extra_snapshot or {}),
    }
    snapshot_path = root / "simulated_ashare_positions.json"
    _write_json(snapshot_path, snapshot)
    outbox = {
        **lineage,
        "schema_version": "2026-07-12.ashare-market-capital-outbox.v2",
        "actions": actions,
        "updated_at": "2026-07-12T06:59:00+00:00",
        "real_trading_enabled": False,
    }
    outbox["payload_sha256"] = _json_sha(
        outbox,
        excluded={
            "payload_sha256",
            "receipt_sha256",
            "trade_sha256",
            "checksum",
            "sha256",
        },
    )
    _write_json(root / "market_capital_outbox.json", outbox)
    return snapshot_path


def _write_cn_source(
    signals_root: Path,
    *,
    positions: list[dict] | None = None,
    history: list[dict] | None = None,
    actions: list[dict] | None = None,
    real_trading_enabled: bool = False,
    extra_snapshot: dict | None = None,
) -> Path:
    positions = list(positions or [])
    history = list(history or [])
    actions = list(actions or [])
    snapshot = {
        "schema_version": reconcile_ops.CN_POSITION_SCHEMA,
        "market": "cn_futures",
        "capital_layer": "simulated",
        "account_type": "simulated",
        "trade_date": TRADE_DATE,
        "position_count": len(positions),
        "total_margin_required": round(
            sum(float(row.get("margin_required", 0.0)) for row in positions), 6
        ),
        "positions": positions,
        "pending_capital_releases": [],
        "pending_capital_commits": [],
        "capital_commit_history": history,
        "mark_evidence_by_symbol": {
            str(row["symbol"]).upper(): _mark_evidence(
                float(row["mark_price"]), str(row["symbol"]).upper()
            )
            for row in positions
        },
        "updated_at": "2026-07-12T06:59:00+00:00",
        "real_trading_enabled": real_trading_enabled,
        **(extra_snapshot or {}),
    }
    snapshot["payload_sha256"] = _json_sha(snapshot, excluded={"payload_sha256"})
    snapshot_path = signals_root / "positions" / "cn_futures_sim_positions.json"
    _write_json(snapshot_path, snapshot)
    outbox = {
        "schema_version": "2026-07-12.cn-futures-capital-outbox.v3",
        "market": "cn_futures",
        "capital_layer": "simulated",
        "account_type": "simulated",
        "actions": actions,
        "updated_at": "2026-07-12T06:59:00+00:00",
        "real_trading_enabled": False,
    }
    outbox["payload_sha256"] = _json_sha(outbox, excluded={"payload_sha256"})
    _write_json(
        signals_root / "capital" / "cn_futures_capital_outbox.json",
        outbox,
    )
    return snapshot_path


@pytest.mark.parametrize("market", ["ashare", "cn_futures"])
def test_empty_fresh_authority_reconciles_and_reports_fresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    market: str,
) -> None:
    monkeypatch.setenv("REAL_TRADING_ENABLED", "false")
    ledger = _init_ledger(tmp_path / market, market)
    source_root = (
        tmp_path / market / (ASHARE_LINEAGE if market == "ashare" else "signals")
    )
    if market == "ashare":
        _write_ashare_source(source_root)
    else:
        _write_cn_source(source_root)

    result = reconcile_ops.reconcile_market_capital(
        market=market,
        capital_root=ledger.root,
        source_root=source_root,
        trade_date=TRADE_DATE,
        pit_timestamp=OPEN_PIT,
        phase="opening",
    )

    assert result["status"] == "reconciled"
    assert result["fresh"] is True
    assert result["reconciled"] is True
    assert result["authority_generation"] == 1
    assert result["cash_balance_cny"] == 50_000.0
    assert result["real_trading_enabled"] is False
    assert Path(result["canonical_snapshot_path"]).is_file()
    assert ledger.snapshot().unreconciled_fill_commit_ids == ()


def test_ashare_close_reconcile_appends_one_idempotent_daily_mtm_fact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REAL_TRADING_ENABLED", "false")
    ledger = _init_ledger(tmp_path / "capital", "ashare")
    execution_root = tmp_path / ASHARE_LINEAGE
    journal_path = tmp_path / "review" / "sample_journal.jsonl"
    _write_ashare_source(execution_root)

    first = reconcile_ops.reconcile_market_capital(
        market="ashare",
        capital_root=ledger.root,
        source_root=execution_root,
        trade_date=TRADE_DATE,
        pit_timestamp=CLOSE_PIT,
        phase="ops",
        ashare_sample_journal_path=journal_path,
    )
    second = reconcile_ops.reconcile_market_capital(
        market="ashare",
        capital_root=ledger.root,
        source_root=execution_root,
        trade_date=TRADE_DATE,
        pit_timestamp=CLOSE_PIT,
        phase="ops",
        ashare_sample_journal_path=journal_path,
    )

    assert first["sample_journal_mtm_evidence"]["status"] == "appended"
    assert second["sample_journal_mtm_evidence"]["status"] == "idempotent"
    rows = SampleJournal(journal_path).latest_sample_records()
    assert len(rows) == 1
    assert rows[0]["evidence_type"] == "account_daily_mtm_equity"
    assert rows[0]["trade_date"] == TRADE_DATE
    assert rows[0]["account_equity_cny"] == pytest.approx(50_000.0)
    assert rows[0]["equity_source"] == "ashare_market_capital_reconcile"
    assert rows[0]["capital_authority_id"] == "ashare-capital-v1"
    assert rows[0]["execution_lineage_id"] == ASHARE_LINEAGE


def test_ashare_intraday_reconcile_does_not_claim_daily_closing_mtm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REAL_TRADING_ENABLED", "false")
    ledger = _init_ledger(tmp_path / "capital", "ashare")
    execution_root = tmp_path / ASHARE_LINEAGE
    journal_path = tmp_path / "review" / "sample_journal.jsonl"
    _write_ashare_source(execution_root)

    result = reconcile_ops.reconcile_market_capital(
        market="ashare",
        capital_root=ledger.root,
        source_root=execution_root,
        trade_date=TRADE_DATE,
        pit_timestamp=OPS_PIT,
        phase="ops",
        ashare_sample_journal_path=journal_path,
    )

    assert result["sample_journal_mtm_evidence"] == {
        "status": "not_due",
        "reason": "close_of_day_mtm_not_due",
    }
    assert not journal_path.exists()


def test_active_reservation_without_durable_execution_pending_fact_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REAL_TRADING_ENABLED", "false")
    ledger = _init_ledger(tmp_path, "ashare")
    execution_root = tmp_path / ASHARE_LINEAGE
    _write_ashare_source(execution_root)
    reconcile_ops.reconcile_market_capital(
        market="ashare",
        capital_root=ledger.root,
        source_root=execution_root,
        trade_date=TRADE_DATE,
        pit_timestamp=OPEN_PIT,
        phase="opening",
    )
    reservation = ledger.reserve(
        MarketCapitalReservationRequest(
            market="ashare",
            reference_id="ASH-PENDING-WITHOUT-FACT",
            risk_unit_key="000001.XSHE",
            worst_case_amount_cny=1_005.0,
            authority_id="ashare-capital-v1",
            trade_date=TRADE_DATE,
            point_in_time_as_of="2026-07-12T15:01:00+08:00",
            lineage_sha256=_sha("pending-lineage"),
            authority_generation=1,
            execution_lineage_id=ASHARE_LINEAGE,
            worst_case_cash_cny=1_005.0,
            worst_case_exposure_cny=1_000.0,
        )
    )
    assert reservation.approved

    with pytest.raises(
        reconcile_ops.MarketCapitalReconcileError,
        match="active_reservation_execution_fact_mismatch",
    ):
        reconcile_ops.reconcile_market_capital(
            market="ashare",
            capital_root=ledger.root,
            source_root=execution_root,
            trade_date=TRADE_DATE,
            pit_timestamp=OPS_PIT,
            phase="ops",
        )


@pytest.mark.parametrize(
    ("market", "pit_timestamp", "expected"),
    [
        ("cn_futures", "2026-07-09T21:05:00+08:00", "20260710"),
        ("cn_futures", "2026-07-10T21:05:00+08:00", "20260713"),
        ("cn_futures", "2026-07-11T01:05:00+08:00", "20260713"),
        ("cn_futures", "2026-07-10T10:05:00+08:00", "20260710"),
        ("ashare", "2026-07-10T21:05:00+08:00", "20260710"),
    ],
)
def test_reconcile_trade_date_uses_cn_exchange_session_not_wall_clock(
    market: str,
    pit_timestamp: str,
    expected: str,
) -> None:
    pit = market_ledger_module._parse_timestamp(
        pit_timestamp,
        field="test_pit",
    )

    assert market_ledger_module._reconcile_trade_date_for_pit(market, pit) == expected


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ({"cash": 49_999.0}, "cash"),
        ({"source": "legacy_sim_projection"}, "source"),
        ({"extra": {"account_epoch": 2}}, "legacy_numeric_epoch"),
        ({"real": True}, "real_trading"),
    ],
)
def test_ashare_source_mismatch_fails_closed_before_reconcile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: dict,
    reason: str,
) -> None:
    monkeypatch.setenv("REAL_TRADING_ENABLED", "false")
    ledger = _init_ledger(tmp_path, "ashare")
    execution_root = tmp_path / ASHARE_LINEAGE
    _write_ashare_source(
        execution_root,
        cash=float(mutation.get("cash", 50_000.0)),
        source=str(mutation.get("source", "server_local_sim_backup")),
        real_trading_enabled=bool(mutation.get("real", False)),
        extra_snapshot=mutation.get("extra"),
    )
    before = ledger.snapshot().event_id

    with pytest.raises(reconcile_ops.MarketCapitalReconcileError, match=reason):
        reconcile_ops.reconcile_market_capital(
            market="ashare",
            capital_root=ledger.root,
            source_root=execution_root,
            trade_date=TRADE_DATE,
            pit_timestamp=OPEN_PIT,
            phase="preopen",
        )

    assert ledger.snapshot().event_id == before


def test_cn_inventory_mismatch_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REAL_TRADING_ENABLED", "false")
    ledger = _init_ledger(tmp_path, "cn_futures")
    signals_root = tmp_path / "signals"
    _write_cn_source(
        signals_root,
        positions=[
            {
                "style": "trend",
                "strategy_name": "trend",
                "symbol": "IF2607",
                "net_qty": 1,
                "side": "long",
                "avg_price": 3500.0,
                "mark_price": 3510.0,
                "contract_multiplier": 300,
                "margin_required": 5000.0,
                "updated_trade_date": TRADE_DATE,
                "updated_at": "2026-07-12T06:58:00+00:00",
                "capital_commit_status": "committed",
                "capital_commit_action_id": "missing-history",
            }
        ],
    )
    before = ledger.snapshot().event_id

    with pytest.raises(
        reconcile_ops.MarketCapitalReconcileError,
        match="inventory|commit_history",
    ):
        reconcile_ops.reconcile_market_capital(
            market="cn_futures",
            capital_root=ledger.root,
            source_root=signals_root,
            trade_date=TRADE_DATE,
            pit_timestamp=OPEN_PIT,
            phase="ops",
        )

    assert ledger.snapshot().event_id == before


def test_stable_reader_rejects_torn_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "source.json"
    path.write_text('{"version":1}\n', encoding="utf-8")
    real_reader = reconcile_ops._read_regular_file_once
    calls = 0

    def torn_reader(candidate: Path):
        nonlocal calls
        calls += 1
        result = real_reader(candidate)
        if calls == 1:
            path.write_text('{"version":2}\n', encoding="utf-8")
        return result

    monkeypatch.setattr(reconcile_ops, "_read_regular_file_once", torn_reader)

    with pytest.raises(
        reconcile_ops.MarketCapitalReconcileError,
        match="torn_read",
    ):
        reconcile_ops._stable_read_json(path)


def test_immutable_outbox_may_predate_current_trade_date() -> None:
    reconcile_ops._validate_immutable_source_time(
        "2026-07-12T15:00:00+08:00",
        pit=reconcile_ops._aware_time("2026-07-13T09:00:00+08:00", field="test_pit"),
        field="outbox_updated_at",
    )


def test_commit_timestamp_cannot_be_after_reconcile_pit() -> None:
    with pytest.raises(
        reconcile_ops.MarketCapitalReconcileError,
        match="after_reconcile_pit",
    ):
        reconcile_ops._validate_commit_times(
            {
                "point_in_time_as_of": "2026-07-12T15:01:00+08:00",
                "filled_at": "2026-07-12T15:06:00+08:00",
            },
            pit=reconcile_ops._aware_time(OPS_PIT, field="test_pit"),
            source="test_commit",
        )


def test_commit_request_must_match_the_committed_ledger_event() -> None:
    request = {
        "reference_id": "R",
        "source": "immutable_execution_outbox",
    }
    event = {
        "event_type": "fill_commit",
        "reference_id": "R",
        "source": "tampered_ledger_event",
    }

    with pytest.raises(
        reconcile_ops.MarketCapitalReconcileError,
        match="commit_fact_mismatch",
    ):
        reconcile_ops._validate_commit_identity(
            "fill_commit", request=request, event=event
        )


def test_real_trading_environment_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REAL_TRADING_ENABLED", "true")
    ledger = _init_ledger(tmp_path, "ashare")
    execution_root = tmp_path / ASHARE_LINEAGE
    _write_ashare_source(execution_root)

    with pytest.raises(
        reconcile_ops.MarketCapitalReconcileError,
        match="sim_only",
    ):
        reconcile_ops.reconcile_market_capital(
            market="ashare",
            capital_root=ledger.root,
            source_root=execution_root,
            trade_date=TRADE_DATE,
            pit_timestamp=OPEN_PIT,
            phase="opening",
        )


def test_wrapper_is_sim_only_and_does_not_apply_crontab() -> None:
    wrapper = (
        Path(__file__).resolve().parents[1]
        / "shared"
        / "wrappers"
        / "job_market_capital_reconcile.sh"
    )
    text = wrapper.read_text(encoding="utf-8")

    assert "REAL_TRADING_ENABLED=false" in text
    assert "market_capital_reconcile_ops" in text
    assert "_reconcile_trade_date_for_pit" in text
    assert "--prepare-source" in text
    assert "crontab" not in text
    assert "broker" not in text.lower()
    assert "email" not in text.lower()


def test_actual_ashare_fresh_bootstrap_writes_reconcile_source_schema(
    tmp_path: Path,
) -> None:
    root = tmp_path / ASHARE_LINEAGE
    ledger = _init_ledger(tmp_path / "capital", "ashare")

    local_sim_ledger.bootstrap_fresh_local_sim(
        root=root,
        lineage_started_at="2026-07-12T08:00:00+08:00",
        point_in_time_as_of="2026-07-12T08:55:00+08:00",
        account="ashare_sim",
    )

    snapshot = json.loads(
        (root / "simulated_ashare_positions.json").read_text(encoding="utf-8")
    )
    assert snapshot["market"] == "ashare"
    assert snapshot["source"] == "server_local_sim_backup"
    assert snapshot["account_view"] == "strategy_samples_only"
    assert snapshot["positions_by_account"] == {"ashare_sim": {}}
    assert snapshot["pnl"]["ashare_sim"]["real_trading_enabled"] is False
    assert snapshot["synced_at"] == "2026-07-12T08:55:00+08:00"
    assert snapshot["real_trading_enabled"] is False
    reconciled = reconcile_ops.reconcile_market_capital(
        market="ashare",
        capital_root=ledger.root,
        source_root=root,
        trade_date=TRADE_DATE,
        pit_timestamp=OPEN_PIT,
        phase="opening",
    )
    assert reconciled["status"] == "reconciled"


def test_actual_ashare_refresh_preserves_authority_and_reconcile_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / ASHARE_LINEAGE
    local_sim_ledger.bootstrap_fresh_local_sim(
        root=root,
        lineage_started_at="2026-07-12T08:00:00+08:00",
        point_in_time_as_of="2026-07-12T08:55:00+08:00",
        account="ashare_sim",
    )
    for name, value in (
        ("LOCAL_SIM_DIR", root),
        ("LOCAL_SIM_TRADES", root / "local_sim_trades.jsonl"),
        ("LOCAL_SIM_POSITIONS", root / "local_sim_positions.json"),
        ("LOCAL_SIM_PNL", root / "local_sim_pnl.json"),
        ("LOCAL_SIM_LOCK", root / ".local_sim.lock"),
        ("LOCAL_SIM_POSITIONS_SNAPSHOT", root / "simulated_ashare_positions.json"),
        ("LOCAL_SIM_RECEIPTS", root / "sim_execution_receipts.jsonl"),
    ):
        monkeypatch.setattr(local_sim_ledger, name, value)

    result = local_sim_ledger.refresh_local_sim_snapshot(mark_prices={})

    assert result["status"] == "refreshed"
    snapshot = json.loads(
        (root / "simulated_ashare_positions.json").read_text(encoding="utf-8")
    )
    assert snapshot["positions_by_account"] == {"ashare_sim": {}}
    assert snapshot["pnl"]["ashare_sim"]["real_trading_enabled"] is False
    assert snapshot["audit_pnl"]["ashare_sim"]["real_trading_enabled"] is False
    assert snapshot["mark_evidence_by_symbol"] == {}
    assert snapshot["real_trading_enabled"] is False


def test_prepare_cn_source_persists_empty_positions_and_outbox_before_first_order(
    tmp_path: Path,
) -> None:
    signals_root = tmp_path / "signals"
    ledger = _init_ledger(tmp_path / "capital", "cn_futures")

    result = reconcile_ops.prepare_reconcile_source(
        market="cn_futures",
        source_root=signals_root,
        trade_date=TRADE_DATE,
        pit_timestamp=OPEN_PIT,
        reader=None,
    )

    assert result["status"] == "prepared"
    position_path = signals_root / "positions" / "cn_futures_sim_positions.json"
    outbox_path = signals_root / "capital" / "cn_futures_capital_outbox.json"
    position = json.loads(position_path.read_text(encoding="utf-8"))
    outbox = json.loads(outbox_path.read_text(encoding="utf-8"))
    assert position["positions"] == []
    assert position["trade_date"] == TRADE_DATE
    assert position["real_trading_enabled"] is False
    assert outbox["actions"] == []
    assert outbox["real_trading_enabled"] is False
    reconciled = reconcile_ops.reconcile_market_capital(
        market="cn_futures",
        capital_root=ledger.root,
        source_root=signals_root,
        trade_date=TRADE_DATE,
        pit_timestamp=OPEN_PIT,
        phase="opening",
    )
    assert reconciled["status"] == "reconciled"


def test_actual_cn_position_writer_emits_complete_reconcile_schema(
    tmp_path: Path,
) -> None:
    signals_root = tmp_path / "signals"
    mark_evidence = {
        "RB2610": {
            "price": 3512.0,
            "observed_at": "2026-07-12T14:55:00+08:00",
            "source": "sharedsignals",
            "source_row_sha256": _sha("rb-mark"),
        }
    }

    cn_sim_runner._write_position_snapshot(
        signals_root,
        {
            "trade_date": TRADE_DATE,
            "positions": [],
            "pending_capital_releases": [],
            "pending_capital_commits": [],
            "capital_commit_history": [],
            "mark_evidence_by_symbol": mark_evidence,
        },
    )

    snapshot = json.loads(
        (signals_root / "positions" / "cn_futures_sim_positions.json").read_text(
            encoding="utf-8"
        )
    )
    assert snapshot["schema_version"] == reconcile_ops.CN_POSITION_SCHEMA
    assert snapshot["trade_date"] == TRADE_DATE
    assert snapshot["mark_evidence_by_symbol"] == mark_evidence
    assert snapshot["real_trading_enabled"] is False


class _SharedSignalsMarkReader:
    def __init__(self, rows_by_symbol: dict[str, list[dict]]) -> None:
        self.rows_by_symbol = rows_by_symbol

    def get_bars_intraday(
        self,
        market: str,
        symbol: str,
        interval: str,
        start: str,
        end: str,
    ) -> list[dict]:
        assert market in {"Ashare", "Futures"}
        assert interval in {"5m", "5min"}
        return list(self.rows_by_symbol.get(symbol, []))

    def get_bars_daily(
        self,
        market: str,
        symbol: str,
        start: str,
        end: str,
    ) -> list[dict]:
        return []


def test_prepare_ashare_source_refreshes_open_position_from_pit_mark(
    tmp_path: Path,
) -> None:
    root = tmp_path / ASHARE_LINEAGE
    position = {
        "quantity": 100,
        "cost_basis": 1_005.0,
        "principal_cost_basis": 1_000.0,
        "entry_fee_cost_basis": 5.0,
        "avg_cost": 10.05,
        "last_price": 10.0,
        "mark_price": 10.0,
        "market_value": 1_000.0,
        "unrealized_pnl": -5.0,
        "trades": 1,
    }
    _write_ashare_source(root, positions={"000001.SZ": position})
    reader = _SharedSignalsMarkReader(
        {
            "000001.SZ": [
                {
                    "close": 12.0,
                    "bar_time": "2026-07-12T14:55:00+08:00",
                    "source": "SharedSignals/realtime_5min",
                },
                {
                    "close": 99.0,
                    "bar_time": "2026-07-12T15:10:00+08:00",
                    "source": "SharedSignals/realtime_5min",
                },
            ]
        }
    )

    result = reconcile_ops.prepare_reconcile_source(
        market="ashare",
        source_root=root,
        trade_date=TRADE_DATE,
        pit_timestamp=OPEN_PIT,
        reader=reader,
    )

    snapshot = json.loads(
        (root / "simulated_ashare_positions.json").read_text(encoding="utf-8")
    )
    refreshed = snapshot["positions_by_account"]["ashare_sim"]["000001.SZ"]
    assert result["mark_count"] == 1
    assert refreshed["mark_price"] == 12.0
    assert refreshed["market_value"] == 1_200.0
    assert refreshed["unrealized_pnl"] == 195.0
    assert snapshot["pnl"]["ashare_sim"]["unrealized_pnl"] == 195.0
    evidence = snapshot["mark_evidence_by_symbol"]["000001.SZ"]
    assert evidence["observed_at"] == "2026-07-12T14:55:00+08:00"
    assert evidence["source_row_sha256"] == _json_sha(
        reader.rows_by_symbol["000001.SZ"][0]
    )


def test_prepare_ashare_source_preserves_audit_only_positions(
    tmp_path: Path,
) -> None:
    root = tmp_path / ASHARE_LINEAGE
    strategy_position = {
        "quantity": 100,
        "cost_basis": 1_005.0,
        "principal_cost_basis": 1_000.0,
        "entry_fee_cost_basis": 5.0,
        "mark_price": 10.0,
        "last_price": 10.0,
        "market_value": 1_000.0,
        "unrealized_pnl": -5.0,
    }
    audit_only = {
        "quantity": 100,
        "cost_basis": 2_005.0,
        "principal_cost_basis": 2_000.0,
        "entry_fee_cost_basis": 5.0,
        "mark_price": 20.0,
        "last_price": 20.0,
        "market_value": 2_000.0,
        "unrealized_pnl": -5.0,
    }
    path = _write_ashare_source(
        root,
        positions={"000001.SZ": strategy_position},
    )
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    snapshot["audit_positions_by_account"]["ashare_sim"]["000002.SZ"] = audit_only
    snapshot["audit_pnl"]["ashare_sim"]["positions"] = snapshot[
        "audit_positions_by_account"
    ]["ashare_sim"]
    _write_json(path, snapshot)
    reader = _SharedSignalsMarkReader(
        {
            "000001.SZ": [
                {
                    "close": 12.0,
                    "bar_time": "2026-07-12T14:55:00+08:00",
                    "source": "SharedSignals/realtime_5min",
                }
            ],
            "000002.SZ": [
                {
                    "close": 21.0,
                    "bar_time": "2026-07-12T14:55:00+08:00",
                    "source": "SharedSignals/realtime_5min",
                }
            ],
        }
    )

    reconcile_ops.prepare_reconcile_source(
        market="ashare",
        source_root=root,
        trade_date=TRADE_DATE,
        pit_timestamp=OPEN_PIT,
        reader=reader,
    )

    refreshed = json.loads(path.read_text(encoding="utf-8"))
    assert set(refreshed["positions_by_account"]["ashare_sim"]) == {"000001.SZ"}
    assert set(refreshed["audit_positions_by_account"]["ashare_sim"]) == {
        "000001.SZ",
        "000002.SZ",
    }
    assert (
        refreshed["audit_positions_by_account"]["ashare_sim"]["000002.SZ"]["mark_price"]
        == 21.0
    )


def test_prepare_ashare_preopen_uses_prior_sharedsignals_daily_close(
    tmp_path: Path,
) -> None:
    root = tmp_path / ASHARE_LINEAGE
    position = {
        "quantity": 100,
        "cost_basis": 1_005.0,
        "principal_cost_basis": 1_000.0,
        "entry_fee_cost_basis": 5.0,
        "mark_price": 10.0,
        "last_price": 10.0,
        "market_value": 1_000.0,
        "unrealized_pnl": -5.0,
    }
    _write_ashare_source(root, positions={"000001.SZ": position})

    class PreopenReader:
        daily_calls: list[tuple[str, str]] = []

        def get_bars_intraday(self, *args: object, **kwargs: object) -> list[dict]:
            return []

        def get_bars_daily(
            self,
            market: str,
            symbol: str,
            start: str,
            end: str,
        ) -> list[dict]:
            self.daily_calls.append((start, end))
            return [
                {
                    "trade_date": "20260710",
                    "close": 10.8,
                    "source": "SharedSignals/market_data",
                }
            ]

    reader = PreopenReader()
    result = reconcile_ops.prepare_reconcile_source(
        market="ashare",
        source_root=root,
        trade_date="20260713",
        pit_timestamp="2026-07-13T08:55:00+08:00",
        reader=reader,
    )

    snapshot = json.loads(
        (root / "simulated_ashare_positions.json").read_text(encoding="utf-8")
    )
    assert result["mark_count"] == 1
    assert reader.daily_calls == [("20260703", "20260713")]
    assert (
        snapshot["positions_by_account"]["ashare_sim"]["000001.SZ"]["mark_price"]
        == 10.8
    )


def test_prepare_cn_source_refreshes_carried_position_without_rewriting_entry_date(
    tmp_path: Path,
) -> None:
    signals_root = tmp_path / "signals"
    position = {
        "style": "trend_breakout",
        "strategy_name": "trend_breakout",
        "symbol": "RB2610",
        "net_qty": 1,
        "side": "long",
        "avg_price": 3_500.0,
        "last_price": 3_500.0,
        "mark_price": 3_500.0,
        "contract_multiplier": 10,
        "margin_required": 4_000.0,
        "updated_trade_date": "20260710",
        "capital_commit_status": "committed",
        "capital_commit_action_id": "CNF-CAP-OPEN-1",
    }
    _write_cn_source(signals_root, positions=[position])
    reader = _SharedSignalsMarkReader(
        {
            "RB2610": [
                {
                    "close": 3_512.0,
                    "bar_time": "2026-07-12T14:55:00+08:00",
                    "source": "SharedSignals/realtime_5min",
                }
            ]
        }
    )

    result = reconcile_ops.prepare_reconcile_source(
        market="cn_futures",
        source_root=signals_root,
        trade_date=TRADE_DATE,
        pit_timestamp=OPEN_PIT,
        reader=reader,
    )

    snapshot = json.loads(
        (signals_root / "positions" / "cn_futures_sim_positions.json").read_text(
            encoding="utf-8"
        )
    )
    assert result["mark_count"] == 1
    assert snapshot["positions"][0]["mark_price"] == 3_512.0
    assert snapshot["positions"][0]["updated_trade_date"] == "20260710"
    assert snapshot["trade_date"] == TRADE_DATE
    assert snapshot["mark_evidence_by_symbol"]["RB2610"]["price"] == 3_512.0


def test_cn_night_mark_query_uses_natural_bar_date_and_exchange_trade_date(
    tmp_path: Path,
) -> None:
    signals_root = tmp_path / "signals"
    position = {
        "style": "trend_breakout",
        "strategy_name": "trend_breakout",
        "symbol": "RB2610",
        "net_qty": 1,
        "side": "long",
        "avg_price": 3_500.0,
        "last_price": 3_500.0,
        "mark_price": 3_500.0,
        "contract_multiplier": 10,
        "margin_required": 4_000.0,
        "updated_trade_date": "20260710",
        "capital_commit_status": "committed",
        "capital_commit_action_id": "CNF-CAP-OPEN-1",
    }
    _write_cn_source(signals_root, positions=[position])

    class NightReader:
        calls: list[tuple[str, str]] = []

        def get_bars_intraday(
            self,
            market: str,
            symbol: str,
            interval: str,
            start: str,
            end: str,
        ) -> list[dict]:
            self.calls.append((start, end))
            return [
                {
                    "close": 3_515.0,
                    "bar_time": "2026-07-10T21:00:00+08:00",
                    "source": "SharedSignals/realtime_5min",
                }
            ]

        def get_bars_daily(self, *args: object, **kwargs: object) -> list[dict]:
            return []

    reader = NightReader()
    result = reconcile_ops.prepare_reconcile_source(
        market="cn_futures",
        source_root=signals_root,
        trade_date="20260713",
        pit_timestamp="2026-07-10T21:05:00+08:00",
        reader=reader,
    )

    assert result["trade_date"] == "20260713"
    assert reader.calls == [("20260710", "20260710")]


def test_ashare_committed_fill_inventory_and_watermark_are_reconciled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REAL_TRADING_ENABLED", "false")
    ledger = _init_ledger(tmp_path, "ashare")
    execution_root = tmp_path / ASHARE_LINEAGE
    _write_ashare_source(execution_root)
    reconcile_ops.reconcile_market_capital(
        market="ashare",
        capital_root=ledger.root,
        source_root=execution_root,
        trade_date=TRADE_DATE,
        pit_timestamp=OPEN_PIT,
        phase="opening",
    )
    reservation = ledger.reserve(
        MarketCapitalReservationRequest(
            market="ashare",
            reference_id="ASH-ORDER-1",
            risk_unit_key="000001.XSHE",
            worst_case_amount_cny=1_005.0,
            authority_id="ashare-capital-v1",
            trade_date=TRADE_DATE,
            point_in_time_as_of="2026-07-12T15:01:00+08:00",
            lineage_sha256=_sha("ashare-lineage"),
            authority_generation=1,
            execution_lineage_id=ASHARE_LINEAGE,
            worst_case_cash_cny=1_005.0,
            worst_case_exposure_cny=1_000.0,
        )
    )
    assert reservation.approved and reservation.snapshot is not None
    request = MarketCapitalFillCommitRequest(
        market="ashare",
        reference_id=f"MCAPFILL:1:{ASHARE_LINEAGE}:{reservation.reservation_id}:ASH-FILL-1",
        reservation_id=reservation.reservation_id,
        reservation_event_id=reservation.event_id,
        reservation_reference_id="ASH-ORDER-1",
        risk_unit_key="000001.XSHE",
        authority_id="ashare-capital-v1",
        authority_generation=1,
        execution_lineage_id=ASHARE_LINEAGE,
        lineage_sha256=_sha("ashare-lineage"),
        order_id="ASH-ORDER-1",
        idempotency_key="ashare:20260712:000001:buy:1",
        execution_fill_id="ASH-FILL-1",
        fill_sequence=1,
        side="buy",
        status="filled",
        terminal=True,
        actual_filled_quantity=100,
        actual_fill_price=10.0,
        actual_cash_debit_cny=1_005.0,
        actual_exposure_cny=1_000.0,
        actual_margin_cny=0.0,
        actual_fee_cash_cny=5.0,
        filled_at="2026-07-12T15:02:00+08:00",
        point_in_time_as_of="2026-07-12T15:01:00+08:00",
        source="local_sim_trade",
        source_sha256=_sha("ashare-source"),
        receipt_sha256=_sha("ashare-receipt"),
        local_trade_sha256=_sha("ashare-local-trade"),
        expected_ledger_event_id=reservation.event_id,
        expected_ledger_checksum=reservation.snapshot.event_checksum,
    )
    committed = ledger.commit_fill(request)
    assert committed.committed
    lineage = _ashare_lineage()
    action = {
        **lineage,
        "action": "fill_commit",
        "action_id": "ASH-CAP-1",
        "reference_id": request.reference_id,
        "reservation_id": reservation.reservation_id,
        "amount_cny": 1_005.0,
        "risk_unit_key": "000001.XSHE",
        "fill_commit_request": asdict(request),
        "fill_commit_request_sha256": _json_sha(asdict(request)),
        "status": "completed",
        "attempt_count": 1,
        "last_result": {
            "status": "committed",
            "committed": True,
            "event_id": committed.event_id,
            "snapshot": {"real_trading_enabled": False},
        },
        "real_trading_enabled": False,
    }
    position = {
        "quantity": 100,
        "cost_basis": 1_005.0,
        "principal_cost_basis": 1_000.0,
        "entry_fee_cost_basis": 5.0,
        "avg_cost": 10.05,
        "last_price": 10.0,
        "mark_price": 11.0,
        "market_value": 1_100.0,
        "unrealized_pnl": 95.0,
        "trades": 1,
        "market_reservations": [],
    }
    _write_ashare_source(
        execution_root,
        cash=48_995.0,
        positions={"000001.XSHE": position},
        actions=[action],
    )
    snapshot_path = execution_root / "simulated_ashare_positions.json"
    writer_input = json.loads(snapshot_path.read_text(encoding="utf-8"))
    for name, value in (
        ("LOCAL_SIM_DIR", execution_root),
        ("LOCAL_SIM_POSITIONS_SNAPSHOT", snapshot_path),
    ):
        monkeypatch.setattr(local_sim_ledger, name, value)
    lineage_projection = {
        field: writer_input[field]
        for field in local_sim_ledger.LINEAGE_PROJECTION_FIELDS
    }
    local_sim_ledger._write_positions_snapshot(
        writer_input["positions_by_account"],
        writer_input["pnl"],
        audit_positions=writer_input["audit_positions_by_account"],
        audit_pnl=writer_input["audit_pnl"],
        lineage_metadata=lineage_projection,
        mark_evidence_by_symbol=writer_input["mark_evidence_by_symbol"],
        synced_at="2026-07-12T15:04:00+08:00",
        trade_date=TRADE_DATE,
    )

    result = reconcile_ops.reconcile_market_capital(
        market="ashare",
        capital_root=ledger.root,
        source_root=execution_root,
        trade_date=TRADE_DATE,
        pit_timestamp=OPS_PIT,
        phase="ops",
    )

    assert result["status"] == "reconciled"
    assert result["included_fill_commit_ids"] == [committed.event_id]
    assert result["positions_quantity_by_risk_unit"] == {"000001.XSHE": 100}
    assert result["positions_cost_basis_cny_by_risk_unit"] == {"000001.XSHE": 1000.0}
    assert result["positions_entry_fee_cny_by_risk_unit"] == {"000001.XSHE": 5.0}
    assert ledger.snapshot().unreconciled_fill_commit_ids == ()


def test_cn_committed_fill_replays_cash_contract_inventory_and_watermark(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REAL_TRADING_ENABLED", "false")
    ledger = _init_ledger(tmp_path, "cn_futures")
    signals_root = tmp_path / "signals"
    _write_cn_source(signals_root)
    reconcile_ops.reconcile_market_capital(
        market="cn_futures",
        capital_root=ledger.root,
        source_root=signals_root,
        trade_date=TRADE_DATE,
        pit_timestamp=OPEN_PIT,
        phase="opening",
    )
    reservation = ledger.reserve(
        MarketCapitalReservationRequest(
            market="cn_futures",
            reference_id="CN-ORDER-1",
            risk_unit_key="IF2607",
            worst_case_amount_cny=5_000.0,
            authority_id="cn-futures-capital-v1",
            trade_date=TRADE_DATE,
            point_in_time_as_of="2026-07-12T15:01:00+08:00",
            lineage_sha256=_sha("cn-lineage"),
            authority_generation=1,
            execution_lineage_id=CN_LINEAGE,
            worst_case_cash_cny=10.0,
            worst_case_exposure_cny=0.0,
            worst_case_margin_cny=5_000.0,
        )
    )
    assert reservation.approved and reservation.snapshot is not None
    contract_spec_sha = cn_futures_contract_spec_sha256("IF2607", 300.0, 5_000.0)
    request = MarketCapitalFillCommitRequest(
        market="cn_futures",
        reference_id=f"MCAPFILL:1:{CN_LINEAGE}:{reservation.reservation_id}:CN-FILL-1",
        reservation_id=reservation.reservation_id,
        reservation_event_id=reservation.event_id,
        reservation_reference_id="CN-ORDER-1",
        risk_unit_key="IF2607",
        authority_id="cn-futures-capital-v1",
        authority_generation=1,
        execution_lineage_id=CN_LINEAGE,
        lineage_sha256=_sha("cn-lineage"),
        order_id="CN-ORDER-1",
        idempotency_key="cn:20260712:IF2607:buy:1",
        execution_fill_id="CN-FILL-1",
        fill_sequence=1,
        side="buy",
        status="filled",
        terminal=True,
        actual_filled_quantity=1,
        actual_fill_price=3_500.0,
        actual_cash_debit_cny=10.0,
        actual_exposure_cny=0.0,
        actual_margin_cny=5_000.0,
        actual_fee_cash_cny=10.0,
        contract_multiplier=300.0,
        contract_margin_per_lot_cny=5_000.0,
        contract_spec_version=CN_FUTURES_CONTRACT_SPEC_VERSION,
        contract_spec_sha256=contract_spec_sha,
        filled_at="2026-07-12T15:02:00+08:00",
        point_in_time_as_of="2026-07-12T15:01:00+08:00",
        source="cn_futures_sim_fill_outbox",
        source_sha256=_sha("cn-source"),
        receipt_sha256=_sha("cn-receipt"),
        local_trade_sha256=_sha("cn-local-position"),
        expected_ledger_event_id=reservation.event_id,
        expected_ledger_checksum=reservation.snapshot.event_checksum,
    )
    committed = ledger.commit_fill(request)
    assert committed.committed
    result_payload = {
        "status": "committed",
        "reason": "fill_committed",
        "committed": True,
        "event_id": committed.event_id,
        "idempotent": False,
        "real_trading_enabled": False,
    }
    action = {
        "action": "fill_commit",
        "action_id": "CNF-CAP-1",
        "reference_id": request.reference_id,
        "amount_cny": 5_000.0,
        "status": "completed",
        "request": asdict(request),
        "result": result_payload,
        "real_trading_enabled": False,
    }
    history = {
        "action": "fill_commit",
        "action_id": "CNF-CAP-1",
        "reference_id": request.reference_id,
        "amount_cny": 5_000.0,
        "status": "committed",
        "request": asdict(request),
        "result": result_payload,
        "real_trading_enabled": False,
    }
    position = {
        "style": "trend",
        "strategy_name": "trend",
        "symbol": "IF2607",
        "net_qty": 1,
        "side": "long",
        "avg_price": 3_500.0,
        "last_price": 3_500.0,
        "mark_price": 3_510.0,
        "contract_multiplier": 300,
        "margin_required": 5_000.0,
        "notional": 1_050_000.0,
        "updated_trade_date": "20260710",
        "updated_at": "2026-07-12T07:03:00+00:00",
        "capital_commit_status": "committed",
        "capital_commit_action_id": "CNF-CAP-1",
    }
    _write_cn_source(
        signals_root,
        positions=[position],
        history=[history],
        actions=[action],
    )
    snapshot_path = signals_root / "positions" / "cn_futures_sim_positions.json"
    writer_input = json.loads(snapshot_path.read_text(encoding="utf-8"))
    monkeypatch.setattr(
        cn_sim_runner,
        "_now_iso",
        lambda: "2026-07-12T07:04:00+00:00",
    )
    cn_sim_runner._write_position_snapshot(signals_root, writer_input)

    result = reconcile_ops.reconcile_market_capital(
        market="cn_futures",
        capital_root=ledger.root,
        source_root=signals_root,
        trade_date=TRADE_DATE,
        pit_timestamp=OPS_PIT,
        phase="ops",
    )

    assert result["status"] == "reconciled"
    assert result["cash_balance_cny"] == 49_990.0
    assert result["included_fill_commit_ids"] == [committed.event_id]
    assert result["positions_quantity_by_risk_unit"] == {"IF2607": 1}
    assert result["position_margin_by_risk_unit"] == {"IF2607": 5_000.0}
    assert ledger.snapshot().unreconciled_fill_commit_ids == ()


def test_cn_capital_comparison_rejects_wrong_margin_total() -> None:
    spec_sha = _sha("spec")
    capital = SimpleNamespace(
        execution_lineage_id=CN_LINEAGE,
        cash_balance_cny=49_990.0,
        positions_quantity_by_risk_unit={"IF2607": 1},
        margin_used_cny=4_000.0,
        position_entry_price_by_risk_unit={"IF2607": 3_500.0},
        position_side_by_risk_unit={"IF2607": "long"},
        position_contract_multiplier_by_risk_unit={"IF2607": 300.0},
        position_contract_spec_sha256_by_risk_unit={"IF2607": spec_sha},
    )
    execution = SimpleNamespace(
        execution_lineage_id=CN_LINEAGE,
        cash_balance_cny=49_990.0,
        positions_quantity_by_risk_unit={"IF2607": 1},
        position_margin_by_risk_unit={"IF2607": 5_000.0},
        position_entry_price_by_risk_unit={"IF2607": 3_500.0},
        position_side_by_risk_unit={"IF2607": "long"},
        position_contract_multiplier_by_risk_unit={"IF2607": 300.0},
        position_contract_spec_sha256_by_risk_unit={"IF2607": spec_sha},
    )

    with pytest.raises(
        reconcile_ops.MarketCapitalReconcileError,
        match="inventory",
    ):
        reconcile_ops._assert_capital_matches_execution(
            "cn_futures", capital, execution
        )


def test_cn_completed_reservation_release_is_valid_non_commit_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REAL_TRADING_ENABLED", "false")
    ledger = _init_ledger(tmp_path, "cn_futures")
    signals_root = tmp_path / "signals"
    _write_cn_source(signals_root)
    reconcile_ops.reconcile_market_capital(
        market="cn_futures",
        capital_root=ledger.root,
        source_root=signals_root,
        trade_date=TRADE_DATE,
        pit_timestamp=OPEN_PIT,
        phase="opening",
    )
    reservation = ledger.reserve(
        MarketCapitalReservationRequest(
            market="cn_futures",
            reference_id="CN-REJECTED-ORDER",
            risk_unit_key="IF2607",
            worst_case_amount_cny=5_000.0,
            authority_id="cn-futures-capital-v1",
            trade_date=TRADE_DATE,
            point_in_time_as_of="2026-07-12T15:01:00+08:00",
            lineage_sha256=_sha("cn-release-lineage"),
            authority_generation=1,
            execution_lineage_id=CN_LINEAGE,
            worst_case_cash_cny=10.0,
            worst_case_exposure_cny=0.0,
            worst_case_margin_cny=5_000.0,
        )
    )
    assert reservation.approved
    release = ledger.release(
        reservation.reservation_id,
        5_000.0,
        "executor_rejected",
        reference_id="CN-RELEASE-1",
    )
    action = {
        "action": "release",
        "action_id": "CNF-CAP-RELEASE-1",
        "reference_id": "CN-RELEASE-1",
        "reservation_id": reservation.reservation_id,
        "amount_cny": 5_000.0,
        "reason": "executor_rejected",
        "status": "completed",
        "result": {
            "status": "released",
            "event_id": release["event_id"],
            "real_trading_enabled": False,
        },
        "real_trading_enabled": False,
    }
    _write_cn_source(signals_root, actions=[action])

    result = reconcile_ops.reconcile_market_capital(
        market="cn_futures",
        capital_root=ledger.root,
        source_root=signals_root,
        trade_date=TRADE_DATE,
        pit_timestamp=OPS_PIT,
        phase="ops",
    )

    assert result["status"] == "reconciled"
    assert result["active_reservation_count"] == 0
    assert result["included_fill_commit_ids"] == []
