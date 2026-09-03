from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from Ashare.capital_backed_paper_runner import (
    CALENDAR_DATASET_ID,
    CANONICAL_DATA_ROOT,
    CASH_SESSION_DAILY_TS_CODE_CHUNK,
    INVENTED_FILL_SOURCES,
    QUOTE_CLOCK_DATASET_ID,
    QUOTE_DATASET_ID,
    CapitalBackedPaperConfig,
    CapitalBackedPaperError,
    FillAttemptRequest,
    FillAttemptResult,
    QuoteClockQueryProof,
    attempt_capital_backed_simulation_fill,
    bind_cash_session_windows,
    bind_market_snapshots,
    bind_quote_clocks,
    cash_session_daily_ts_codes,
    close_or_touch_is_not_a_fill,
    count_coverage_is_not_a_fill,
    make_missing_window,
    in_session_quote_clock_slot,
    last_complete_in_session_quote_slot,
    make_observation_window,
    paper_session_drift_allows_new_risk,
    query_windows_from_tradingdatas,
    reject_invented_fill_source,
    run_capital_backed_paper_session,
    _bar_evidence_bid_ask,
    _bar_evidence_fill_gate,
    _envelope_query_proof,
    _looks_like_daily_close_row,
)
from Ashare.capital_backed_paper_universe import (
    EXCLUSION_PROBES,
    EXPLICIT_ADD_LIST,
    FROZEN_UNIVERSE_SHA256,
    PHARMA_MAINBOARD_SYMBOLS,
    TECH_MAINBOARD_SYMBOLS,
    classify_session_symbol,
    session_candidate_symbols,
)
from Ashare.minute_paper import MinuteFixturePaperBook
from shared.data.sharedsignals_v1 import (
    HTTPResponse,
    SharedSignalsV1Client,
    SharedSignalsV1Config,
)
from shared.review.decision_ledger import ExposureDisposition, SampleJournalDecisionLedger
from shared.review.sample_journal import SampleJournal
from shared.capital.market_ledger import MarketCapitalLedger
from shared.capital.market_policy import MarketPolicy
from shared.runtime.capital_stages import PaperCapitalAccount
from shared.runtime.composition import (
    PaperRuntimeConfig,
    PaperRuntimeConfigurationError,
    compose_capital_backed_paper_runtime,
    compose_paper_runtime,
)
from shared.universe.policy import CanonicalMainboardScopePolicy
from tests.test_capital_backed_paper_stages import (
    DECISION_AS_OF,
    LINEAGE,
    TRADE_DATE,
    _execute_buy,
    _init_ledger,
    _mark,
)
from tests.test_capital_runtime_composition import _manual_registry


DECISION = datetime.fromisoformat(DECISION_AS_OF)


def _config(tmp_path: Path, **overrides: object) -> CapitalBackedPaperConfig:
    values = {
        "trade_date": TRADE_DATE,
        "decision_as_of": DECISION,
        "ledger_root": tmp_path / "capital",
        "journal_path": tmp_path / "sample_journal.jsonl",
        "latest_path": tmp_path / "latest.json",
        "include_exclusion_probes": True,
    }
    values.update(overrides)
    return CapitalBackedPaperConfig(**values)  # type: ignore[arg-type]


def _prepare_ledger(tmp_path: Path):
    return _init_ledger(tmp_path)


def test_frozen_universe_is_hashed_mainboard_tech_pharma_plus_add_list() -> None:
    assert len(FROZEN_UNIVERSE_SHA256) == 64
    assert FROZEN_UNIVERSE_SHA256 == FROZEN_UNIVERSE_SHA256.lower()
    policy = CanonicalMainboardScopePolicy()
    for symbol in (*TECH_MAINBOARD_SYMBOLS, *PHARMA_MAINBOARD_SYMBOLS, *EXPLICIT_ADD_LIST):
        assert policy.order_identity_allowed(symbol)
        assert classify_session_symbol(symbol).order_identity_allowed
    symbols = session_candidate_symbols()
    assert "000063.SZ" in symbols
    assert "600276.SH" in symbols
    assert "000001.SZ" in symbols
    for probe in EXCLUSION_PROBES:
        assert probe in symbols
        assert classify_session_symbol(probe).order_identity_allowed is False


def test_chinext_and_star_are_exclusions_with_reason_codes() -> None:
    chinext = classify_session_symbol("300750.SZ")
    chinext_301 = classify_session_symbol("301269.SZ")
    star = classify_session_symbol("688981.SH")
    star_689 = classify_session_symbol("689009.SH")
    assert chinext.reason_code == "chinext_individual_permission_unavailable"
    assert chinext_301.reason_code == "chinext_individual_permission_unavailable"
    assert star.reason_code == "star_individual_permission_unavailable"
    assert star_689.reason_code == "star_individual_permission_unavailable"
    assert chinext.eligibility.board == "chinext"
    assert star.eligibility.board == "star"
    assert chinext.order_identity_allowed is False
    assert star_689.order_identity_allowed is False


def test_industry_shadow_name_is_not_order_identity() -> None:
    row = classify_session_symbol("电子")
    assert row.order_identity_allowed is False
    assert row.reason_code == "industry_shadow_not_order_identity"
    assert row.sleeve == "industry_shadow"


def test_real_trading_enabled_cannot_be_true(tmp_path: Path) -> None:
    _prepare_ledger(tmp_path)
    with pytest.raises(CapitalBackedPaperError, match="real_trading_enabled_must_be_native_false"):
        _config(tmp_path, real_trading_enabled=True)
    previous = os.environ.get("REAL_TRADING_ENABLED")
    os.environ["REAL_TRADING_ENABLED"] = "true"
    try:
        with pytest.raises(CapitalBackedPaperError, match="real_trading_must_remain_disabled"):
            _config(tmp_path)
    finally:
        if previous is None:
            os.environ.pop("REAL_TRADING_ENABLED", None)
        else:
            os.environ["REAL_TRADING_ENABLED"] = previous


def test_tests_cannot_mutate_production_canonical_root(tmp_path: Path) -> None:
    _prepare_ledger(tmp_path)
    with pytest.raises(CapitalBackedPaperError, match="canonical_ledger_mutation_forbidden"):
        _config(
            tmp_path,
            ledger_root=CANONICAL_DATA_ROOT / "shared" / "logs" / "capital" / "ashare",
        )


def test_invented_fill_checklist_fail_closed() -> None:
    for source in INVENTED_FILL_SOURCES:
        with pytest.raises(CapitalBackedPaperError, match="invented_fill_forbidden"):
            reject_invented_fill_source(source)
    with pytest.raises(CapitalBackedPaperError, match="wrong_capital_authority"):
        reject_invented_fill_source(compose_paper_runtime)
    with pytest.raises(CapitalBackedPaperError, match="minute_fixture_paper_book"):
        reject_invented_fill_source(MinuteFixturePaperBook())
    assert count_coverage_is_not_a_fill(1197) == 0
    assert close_or_touch_is_not_a_fill() == 0


def test_fixture_composer_stays_locked_to_frozen_transport() -> None:
    with pytest.raises(PaperRuntimeConfigurationError):
        PaperRuntimeConfig(  # type: ignore[misc]
            trade_date=TRADE_DATE,
            decision_as_of=DECISION,
            tradingdatas_v1_base_url="http://127.0.0.1:18082",
            tradingdatas_catalog_version="v1",
            tradingdatas_access_policy_id="policy",
            dataset_profile=None,
            dataset_requests={},
            evidence_policies={},
            capital_authority_id="ashare-capital-v1",
            authority_generation=1,
            execution_lineage=LINEAGE,
            champion_manifest_sha256="c" * 64,
            network_enabled=True,
        )
    assert compose_capital_backed_paper_runtime is not run_capital_backed_paper_session


_SESSION_SEQ = 0


def _run_session(
    tmp_path: Path,
    *,
    windows=None,
    extra_symbols=(),
    champion_registry=None,
    drift_ok=None,
    fill_attempt=None,
    fill_source=None,
    coverage_accepted_count=None,
):
    global _SESSION_SEQ
    _SESSION_SEQ += 1
    workspace = tmp_path / f"session-{_SESSION_SEQ}"
    workspace.mkdir()
    _prepare_ledger(workspace)
    config = _config(workspace, extra_symbols=extra_symbols)
    return run_capital_backed_paper_session(
        config,
        windows=windows,
        champion_registry=champion_registry,
        drift_ok=drift_ok,
        fill_attempt=fill_attempt,
        fill_source=fill_source,
        coverage_accepted_count=coverage_accepted_count,
    )


def test_reject_without_invented_fill_and_persist_dispositions(tmp_path: Path) -> None:
    result = _run_session(
        tmp_path,
        extra_symbols=("电子",),
        coverage_accepted_count=3188,
    )
    assert result.fill_count == 0
    assert result.canonical_account_connected is False
    assert result.capital_authority_id == "ashare-capital-v1"
    assert result.opening_cash_cny == 50_000.0
    assert result.universe_sha256 == FROZEN_UNIVERSE_SHA256
    latest = json.loads(result.latest_path.read_text(encoding="utf-8"))
    assert latest["_projection"]["record_type"] == "capital_backed_paper_session"
    assert latest["fill_count"] == 0
    assert latest["canonical_account_connected"] is False
    assert latest["real_trading_enabled"] is False
    assert latest["capital_authority_id"] == "ashare-capital-v1"
    chinext = result.disposition_for("300750.SZ")
    star = result.disposition_for("688981.SH")
    industry = result.disposition_for("电子")
    missing = result.disposition_for("000063.SZ")
    assert chinext.disposition is ExposureDisposition.REJECTED
    assert chinext.rejection_reason == "chinext_individual_permission_unavailable"
    assert star.rejection_reason == "star_individual_permission_unavailable"
    assert industry.rejection_reason == "industry_shadow_not_order_identity"
    assert missing.disposition is ExposureDisposition.OBSERVATION_ONLY
    assert missing.reason_code == "missing_dataset_catalog_or_session_window"
    events = SampleJournal(result.journal_path).read_events()
    assert {event["disposition"] for event in events} <= {
        "paper_filled",
        "paper_not_filled",
        "rejected",
        "observation_only",
    }
    assert all(event["audit_event_type"] == "decision_exposure_disposition" for event in events)
    assert not any(event["decision_exposure"]["filled_quantity"] for event in events)
    ledger = SampleJournalDecisionLedger(
        journal=SampleJournal(result.journal_path),
        source_run_id=result.run_id,
        input_bundle_sha256=result.input_bundle_sha256,
        capital_authority_id=result.capital_authority_id,
        authority_generation=result.authority_generation,
        execution_lineage_id=result.execution_lineage_id,
    )
    assert len(ledger.records()) == len(result.dispositions)


def test_missing_window_is_observation_not_fill(tmp_path: Path) -> None:
    windows = {
        "000001.SZ": make_missing_window("000001.SZ"),
        "600276.SH": make_observation_window("600276.SH", quote_clocks_ok=False),
    }
    result = _run_session(tmp_path, windows=windows)
    assert result.fill_count == 0
    assert result.disposition_for("000001.SZ").disposition is ExposureDisposition.OBSERVATION_ONLY
    assert result.disposition_for("600276.SH").disposition is ExposureDisposition.REJECTED
    assert result.disposition_for("600276.SH").rejection_reason == "champion_current_unavailable"


def test_zero_fill_unless_all_gates_pass(tmp_path: Path) -> None:
    registry, _manifest = _manual_registry(tmp_path)
    windows = {
        "000001.SZ": make_observation_window(
            "000001.SZ",
            prior_close_cny=10.0,
            quote_clocks_ok=True,
        )
    }
    result = _run_session(
        tmp_path,
        windows=windows,
        champion_registry=registry,
        drift_ok=False,
    )
    assert result.fill_count == 0
    assert result.disposition_for("000001.SZ").rejection_reason == (
        "drift_constraint_blocks_new_risk"
    )
    result = _run_session(
        tmp_path,
        windows={
            "000001.SZ": make_observation_window(
                "000001.SZ",
                prior_close_cny=80.0,
                quote_clocks_ok=True,
            )
        },
        champion_registry=registry,
        drift_ok=True,
    )
    assert result.fill_count == 0
    assert result.disposition_for("000001.SZ").rejection_reason == (
        "lot_or_single_name_cap_blocked"
    )
    result = _run_session(
        tmp_path,
        windows={
            "000001.SZ": make_observation_window(
                "000001.SZ",
                prior_close_cny=10.0,
                quote_clocks_ok=False,
            )
        },
        champion_registry=registry,
        drift_ok=True,
    )
    assert result.fill_count == 0
    assert result.disposition_for("000001.SZ").disposition is (
        ExposureDisposition.PAPER_NOT_FILLED
    )
    assert result.disposition_for("000001.SZ").nonfill_reason == "quote_clocks_unavailable"


def test_empty_sim_book_with_sim_only_current_does_not_reject_as_drift(
    tmp_path: Path,
) -> None:
    registry, _manifest = _manual_registry(tmp_path)
    windows = {
        "000001.SZ": make_observation_window(
            "000001.SZ",
            prior_close_cny=10.0,
            quote_clocks_ok=False,
        )
    }
    result = _run_session(
        tmp_path,
        windows=windows,
        champion_registry=registry,
    )
    assert result.opening_cash_cny == 50_000.0
    assert result.fill_count == 0
    assert result.canonical_account_connected is False
    row = result.disposition_for("000001.SZ")
    assert row.rejection_reason != "drift_constraint_blocks_new_risk"
    assert row.reason_code != "drift_constraint_blocks_new_risk"
    assert row.disposition is ExposureDisposition.PAPER_NOT_FILLED
    assert row.nonfill_reason == "quote_clocks_unavailable"
    chinext = result.disposition_for("300750.SZ")
    star = result.disposition_for("688981.SH")
    assert chinext.disposition is ExposureDisposition.REJECTED
    assert chinext.rejection_reason == "chinext_individual_permission_unavailable"
    assert star.rejection_reason == "star_individual_permission_unavailable"


def test_live_risk_flags_still_block_new_risk_as_drift(tmp_path: Path) -> None:
    registry, _manifest = _manual_registry(tmp_path)
    current = registry.load_current()
    windows = {
        "000001.SZ": make_observation_window(
            "000001.SZ",
            prior_close_cny=10.0,
            quote_clocks_ok=True,
        )
    }
    for field_name in (
        "live_transition_authorized",
        "automatic_risk_expansion_enabled",
        "real_trading_enabled",
    ):
        live = replace(current, **{field_name: True})
        with patch(
            "Ashare.capital_backed_paper_runner._load_champion",
            return_value=live,
        ):
            result = _run_session(
                tmp_path,
                windows=windows,
                champion_registry=registry,
                drift_ok=True,
            )
        row = result.disposition_for("000001.SZ")
        assert row.disposition is ExposureDisposition.REJECTED
        assert row.rejection_reason == "drift_constraint_blocks_new_risk"
        assert result.fill_count == 0
        assert result.disposition_for("300750.SZ").rejection_reason == (
            "chinext_individual_permission_unavailable"
        )
        assert result.disposition_for("688981.SH").rejection_reason == (
            "star_individual_permission_unavailable"
        )


def test_paper_session_drift_helper_fail_closes_live_risk(tmp_path: Path) -> None:
    ledger = _prepare_ledger(tmp_path)
    snapshot = ledger.snapshot()
    registry, _manifest = _manual_registry(tmp_path)
    champion = registry.load_current()
    assert snapshot.cash_balance_cny == 50_000.0
    assert snapshot.positions_market_value_cny == 0.0
    assert paper_session_drift_allows_new_risk(
        champion=champion,
        snapshot=snapshot,
        requested_drift_ok=None,
    )
    assert not paper_session_drift_allows_new_risk(
        champion=champion,
        snapshot=snapshot,
        requested_drift_ok=False,
    )
    for field_name in (
        "live_transition_authorized",
        "automatic_risk_expansion_enabled",
        "real_trading_enabled",
    ):
        live = replace(champion, **{field_name: True})
        assert not paper_session_drift_allows_new_risk(
            champion=live,
            snapshot=snapshot,
            requested_drift_ok=True,
        )
        assert not paper_session_drift_allows_new_risk(
            champion=live,
            snapshot=snapshot,
            requested_drift_ok=None,
        )
    previous = os.environ.get("REAL_TRADING_ENABLED")
    os.environ["REAL_TRADING_ENABLED"] = "true"
    try:
        assert not paper_session_drift_allows_new_risk(
            champion=champion,
            snapshot=snapshot,
            requested_drift_ok=True,
        )
    finally:
        if previous is None:
            os.environ.pop("REAL_TRADING_ENABLED", None)
        else:
            os.environ["REAL_TRADING_ENABLED"] = previous


def test_default_fill_attempt_does_not_invent_fill_from_daily_close(
    tmp_path: Path,
) -> None:
    registry, _manifest = _manual_registry(tmp_path)
    windows = {
        "000001.SZ": make_observation_window(
            "000001.SZ",
            prior_close_cny=10.0,
            quote_clocks_ok=True,
        )
    }
    result = _run_session(
        tmp_path,
        windows=windows,
        champion_registry=registry,
    )
    row = result.disposition_for("000001.SZ")
    assert row.rejection_reason != "drift_constraint_blocks_new_risk"
    assert row.disposition is ExposureDisposition.PAPER_NOT_FILLED
    assert row.nonfill_reason == "capital_fill_market_snapshot_unavailable"
    assert result.fill_count == 0
    request = FillAttemptRequest(
        symbol="000001.SZ",
        quantity=100,
        prior_close_cny=10.0,
        champion=registry.load_current(),
        window=windows["000001.SZ"],
        snapshot_before=_prepare_ledger(tmp_path / "fill-probe").snapshot(),
    )
    attempt = attempt_capital_backed_simulation_fill(
        request,
        ledger=_prepare_ledger(tmp_path / "fill-probe-ledger"),
    )
    assert attempt.committed is False
    assert attempt.filled_quantity == 0
    assert attempt.fill_id is None


def test_fingerprint_without_ledger_commit_is_not_a_fill(tmp_path: Path) -> None:
    registry, _manifest = _manual_registry(tmp_path)
    windows = {
        "000001.SZ": make_observation_window(
            "000001.SZ",
            prior_close_cny=10.0,
            quote_clocks_ok=True,
        )
    }

    def _fake_fill(request: FillAttemptRequest) -> FillAttemptResult:
        del request
        return FillAttemptResult(
            committed=True,
            fill_id="INVENTED-FILL",
            filled_quantity=100,
            filled_notional_cny=1_000.0,
            actual_cost_cny=5.0,
            ledger_event_id="fake",
            reason_code="invented",
        )

    with pytest.raises(CapitalBackedPaperError, match="invented_fill_without_ledger_commit"):
        _run_session(
            tmp_path,
            windows=windows,
            champion_registry=registry,
            drift_ok=True,
            fill_attempt=_fake_fill,
            fill_source=None,
        )


def test_kpi_forced_and_coverage_cannot_become_fills(tmp_path: Path) -> None:
    with pytest.raises(CapitalBackedPaperError, match="invented_fill_forbidden"):
        _run_session(tmp_path, fill_source="kpi_forced_order")
    result = _run_session(tmp_path, coverage_accepted_count=1197)
    assert result.fill_count == 0
    assert all(
        item.disposition is not ExposureDisposition.PAPER_FILLED
        for item in result.dispositions
    )


def test_capital_ledger_fill_is_the_only_paper_filled_path(tmp_path: Path) -> None:
    ledger = _init_ledger(tmp_path)
    account = PaperCapitalAccount(
        ledger=ledger,
        artifact_root=tmp_path / "capital-artifacts",
        mark_prices={"000001.SZ": _mark(10.0)},
    )
    registry, _manifest = _manual_registry(tmp_path)
    windows = {
        "000001.SZ": make_observation_window(
            "000001.SZ",
            prior_close_cny=10.0,
            quote_clocks_ok=True,
        )
    }

    def _real_fill(request: FillAttemptRequest) -> FillAttemptResult:
        payload = _execute_buy(
            account=account,
            order_id=f"ORDER-{request.symbol}",
            run_id="ashare-paper-test-run",
            trade_date=TRADE_DATE,
            decision_as_of=DECISION_AS_OF,
            execution_time="2026-07-16T09:35:00+08:00",
        )
        receipts = payload.get("order_receipts") or []
        receipt = receipts[0] if receipts else {}
        filled_quantity = int(receipt.get("filled_quantity") or 0)
        filled_price = float(receipt.get("filled_price_cny") or 0.0)
        filled_notional = float(
            receipt.get("filled_notional_cny") or (filled_quantity * filled_price)
        )
        after = account.ledger.snapshot()
        committed = receipt.get("capital_commit_status") == "committed"
        return FillAttemptResult(
            committed=bool(committed),
            fill_id=str(receipt.get("simulated_fill_id") or "") or None,
            filled_quantity=filled_quantity,
            filled_notional_cny=filled_notional,
            actual_cost_cny=float(receipt.get("fee_cny") or 0.0),
            ledger_event_id=after.event_id,
            reason_code=(
                "simulated_fill_recorded"
                if committed
                else str(receipt.get("execution_reason") or "order_not_filled_by_simulator")
            ),
        )

    result = run_capital_backed_paper_session(
        _config(tmp_path),
        windows=windows,
        champion_registry=registry,
        drift_ok=True,
        fill_attempt=_real_fill,
    )
    filled = result.disposition_for("000001.SZ")
    assert filled.disposition is ExposureDisposition.PAPER_FILLED
    assert result.fill_count == 1
    assert filled.simulated_fill_id
    assert filled.filled_quantity == 100
    assert filled.filled_notional_cny > 0
    assert filled.rejection_reason is None
    assert filled.nonfill_reason is None
    snapshot = account.ledger.snapshot()
    assert snapshot.cash_balance_cny < 50_000.0
    assert snapshot.positions_quantity_by_risk_unit.get("000001.SZ", 0) >= 100
    latest = json.loads(result.latest_path.read_text(encoding="utf-8"))
    assert latest["fill_count"] == 1
    assert latest["canonical_account_connected"] is False


def test_systemd_candidate_is_sim_only_and_points_at_sibling_runner() -> None:
    root = Path(__file__).resolve().parents[1] / "Ashare" / "systemd"
    service = (root / "tradingagent-ashare-capital-backed-paper.service").read_text(
        encoding="utf-8"
    )
    env = (root / "tradingagent-ashare-capital-backed-paper.env.example").read_text(
        encoding="utf-8"
    )
    timer = (root / "tradingagent-ashare-capital-backed-paper.timer").read_text(
        encoding="utf-8"
    )
    assert "Type=oneshot" in service
    assert "REAL_TRADING_ENABLED=false" in service
    assert "-m Ashare.capital_backed_paper_runner" in service
    assert "compose_capital_backed_paper_runtime" not in service
    assert "compose_paper_runtime" not in service
    assert "minute_scale500_runtime" not in service
    assert "同花顺" not in service
    assert "live" not in service.lower() or "REAL_TRADING_ENABLED=false" in service
    assert "/var/lib/tradingagent/ashare-canonical" in service
    assert "After=network-online.target tradingdatas-v1-internal.service" in service
    assert "IPAddressAllow=localhost" in service
    assert "[Install]" not in service
    assert "REAL_TRADING_ENABLED=false" in env
    assert "tradingagent-ashare-capital-backed-paper.service" in timer
    runner_source = (
        Path(__file__).resolve().parents[1] / "Ashare" / "capital_backed_paper_runner.py"
    ).read_text(encoding="utf-8")
    main_source = runner_source.split("def main(", 1)[1]
    assert "drift_ok=False" not in main_source
    assert "paper_session_drift_allows_new_risk" in runner_source
    assert "attempt_capital_backed_simulation_fill" in runner_source
    assert "bind_quote_clocks" in runner_source
    assert "bind_market_snapshots" in runner_source
    assert "cn.dataset.rt_min" in runner_source
    assert "decision_as_of=config.decision_as_of" in main_source


CASH_SESSION_TRADE_DATE = "2026-09-01"
LAST_COMPLETE_DAILY = "20260831"
CASH_SESSION_SYMBOL = "000063.SZ"
CASH_SESSION_UNIVERSE = (
    *TECH_MAINBOARD_SYMBOLS,
    *PHARMA_MAINBOARD_SYMBOLS,
    *EXPLICIT_ADD_LIST,
)


def _open_calendar_row(*, is_open: object = 1) -> dict[str, object]:
    return {
        "exchange": "SSE",
        "cal_date": "20260901",
        "is_open": is_open,
        "pretrade_date": LAST_COMPLETE_DAILY,
    }


def _daily_row(
    symbol: str,
    *,
    trade_date: str,
    close: float,
    pre_close: float | None = None,
) -> dict[str, object]:
    row: dict[str, object] = {
        "ts_code": symbol,
        "trade_date": trade_date,
        "close": close,
    }
    if pre_close is not None:
        row["pre_close"] = pre_close
    return row


def test_open_calendar_uses_last_complete_daily_without_todays_partition() -> None:
    windows = bind_cash_session_windows(
        (CASH_SESSION_SYMBOL, "300750.SZ"),
        trade_date=CASH_SESSION_TRADE_DATE,
        catalog_version="td-catalog-live",
        calendar_rows=(_open_calendar_row(),),
        daily_rows=(
            _daily_row(CASH_SESSION_SYMBOL, trade_date=LAST_COMPLETE_DAILY, close=12.5),
            _daily_row(
                CASH_SESSION_SYMBOL,
                trade_date="20260901",
                close=99.0,
                pre_close=12.5,
            ),
        ),
    )
    window = windows[CASH_SESSION_SYMBOL]
    assert window.session_calendar_ok is True
    assert window.observation_ready is True
    assert window.dataset_id == QUOTE_DATASET_ID
    assert window.quote_trade_date == LAST_COMPLETE_DAILY
    assert window.prior_close_cny == 12.5
    assert window.reason_code != "missing_dataset_catalog_or_session_window"
    assert window.quote_clocks_ok is False


def test_closed_calendar_does_not_pretend_there_is_a_session_window() -> None:
    windows = bind_cash_session_windows(
        (CASH_SESSION_SYMBOL,),
        trade_date=CASH_SESSION_TRADE_DATE,
        catalog_version="td-catalog-live",
        calendar_rows=(_open_calendar_row(is_open=0),),
        daily_rows=(
            _daily_row(CASH_SESSION_SYMBOL, trade_date=LAST_COMPLETE_DAILY, close=12.5),
        ),
    )
    window = windows[CASH_SESSION_SYMBOL]
    assert window.session_calendar_ok is False
    assert window.observation_ready is False
    assert window.reason_code == "missing_dataset_catalog_or_session_window"
    assert window.prior_close_cny is None


class _RecordingTDTransport:
    def __init__(
        self,
        *,
        calendar_rows: tuple[dict[str, object], ...],
        daily_by_date: dict[str, tuple[dict[str, object], ...]],
        quote_clock_rows: tuple[dict[str, object], ...] = (),
        include_quote_clock: bool = False,
    ) -> None:
        self.calendar_rows = calendar_rows
        self.daily_by_date = daily_by_date
        self.quote_clock_rows = quote_clock_rows
        self.include_quote_clock = include_quote_clock or bool(quote_clock_rows)
        self.calls: list[dict[str, object]] = []

    def __call__(
        self,
        *,
        method: str,
        url: str,
        headers,
        json_body,
        timeout_seconds: float,
    ) -> HTTPResponse:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "json_body": dict(json_body) if json_body is not None else None,
            }
        )
        if method == "GET":
            return HTTPResponse(
                200,
                _td_catalog_payload(include_quote_clock=self.include_quote_clock),
            )
        dataset_id = str((json_body or {}).get("dataset_id") or "")
        filters = (json_body or {}).get("filters") or {}
        if dataset_id == CALENDAR_DATASET_ID:
            cal_date = str((filters.get("cal_date") or {}).get("eq") or "")
            rows = [
                row
                for row in self.calendar_rows
                if str(row.get("cal_date") or "").replace("-", "")
                == cal_date.replace("-", "")
            ]
            return HTTPResponse(
                200,
                _td_query_payload(CALENDAR_DATASET_ID, tuple(rows)),
            )
        if dataset_id == QUOTE_DATASET_ID:
            ts_codes = (filters.get("ts_code") or {}).get("in")
            if not ts_codes:
                return HTTPResponse(
                    413,
                    {
                        "error": "budget_exceeded",
                        "reason": "budget_exceeded",
                    },
                )
            trade_date = str((filters.get("trade_date") or {}).get("eq") or "")
            wanted = {str(code).upper() for code in ts_codes}
            rows = tuple(
                row
                for row in self.daily_by_date.get(trade_date, ())
                if str(row.get("ts_code") or row.get("symbol") or "").upper()
                in wanted
            )
            return HTTPResponse(
                200,
                _td_query_payload(QUOTE_DATASET_ID, rows),
            )
        if dataset_id == QUOTE_CLOCK_DATASET_ID:
            ts_codes = (filters.get("ts_code") or {}).get("in")
            if not ts_codes:
                return HTTPResponse(
                    413,
                    {
                        "error": "budget_exceeded",
                        "reason": "budget_exceeded",
                    },
                )
            slot = str((filters.get("time") or {}).get("eq") or "")
            wanted = {str(code).upper() for code in ts_codes}
            rows = tuple(
                row
                for row in self.quote_clock_rows
                if str(row.get("ts_code") or row.get("symbol") or "").upper()
                in wanted
                and str(row.get("time") or "") == slot
            )
            return HTTPResponse(
                200,
                _td_query_payload(QUOTE_CLOCK_DATASET_ID, rows),
            )
        raise AssertionError(f"unexpected dataset {dataset_id}")


def _td_catalog_payload(*, include_quote_clock: bool = False) -> dict[str, object]:
    data: list[dict[str, object]] = [
        {
            "dataset_id": CALENDAR_DATASET_ID,
            "schema_major": 1,
            "fields": ["exchange", "cal_date", "is_open", "pretrade_date"],
        },
        {
            "dataset_id": QUOTE_DATASET_ID,
            "schema_major": 1,
            "fields": ["ts_code", "trade_date", "close", "pre_close"],
        },
    ]
    if include_quote_clock:
        data.append(
            {
                "dataset_id": QUOTE_CLOCK_DATASET_ID,
                "schema_major": 2,
                "fields": ["ts_code", "freq", "time", "open", "close", "high", "low"],
            }
        )
    return {
        "api_version": "v1",
        "catalog_version": "td-catalog-live",
        "request_id": "catalog-cash-session",
        "data": data,
    }


def _td_query_payload(
    dataset_id: str,
    rows: tuple[dict[str, object], ...],
) -> dict[str, object]:
    return {
        "api_version": "v1",
        "catalog_version": "td-catalog-live",
        "request_id": f"query-{dataset_id}",
        "dataset_id": dataset_id,
        "data": list(rows),
        "next_cursor": None,
        "metadata": {
            "state": "ready",
            "degraded": False,
            "freshness": {"state": "fresh"},
            "quality": {"state": "valid"},
            "lineage": {"complete": True, "provider_neutral": True},
            "receipt_id": f"receipt-{dataset_id}",
            "data_through": "2026-08-31T16:00:00+08:00",
            "observed_at": "2026-09-01T09:30:00+08:00",
            "reasons": [],
        },
    }


def _td_client(
    transport: _RecordingTDTransport,
    *,
    include_quote_clock: bool = False,
) -> SharedSignalsV1Client:
    dataset_ids = {CALENDAR_DATASET_ID, QUOTE_DATASET_ID}
    if include_quote_clock:
        dataset_ids.add(QUOTE_CLOCK_DATASET_ID)
    return SharedSignalsV1Client(
        SharedSignalsV1Config(
            base_url="http://127.0.0.1:18082",
            expected_catalog_version="evidence-only",
            dataset_ids=frozenset(dataset_ids),
            access_policy_id="tradingagent-read-v1",
            catalog_version_policy="evidence_only",
            timeout_seconds=5.0,
            max_limit=10_000,
            cache_ttl_seconds=0.0,
        ),
        transport=transport,
    )


def test_query_windows_open_session_does_not_require_todays_daily() -> None:
    transport = _RecordingTDTransport(
        calendar_rows=(_open_calendar_row(),),
        daily_by_date={
            LAST_COMPLETE_DAILY: (
                _daily_row(
                    CASH_SESSION_SYMBOL,
                    trade_date=LAST_COMPLETE_DAILY,
                    close=12.5,
                ),
            ),
        },
    )
    windows = query_windows_from_tradingdatas(
        (CASH_SESSION_SYMBOL,),
        trade_date=CASH_SESSION_TRADE_DATE,
        client=_td_client(transport),
    )
    window = windows[CASH_SESSION_SYMBOL]
    assert window.observation_ready is True
    assert window.session_calendar_ok is True
    assert window.prior_close_cny == 12.5
    assert window.quote_trade_date == LAST_COMPLETE_DAILY
    assert window.reason_code != "missing_dataset_catalog_or_session_window"
    daily_filters = [
        call["json_body"]["filters"]
        for call in transport.calls
        if call["method"] == "POST"
        and (call["json_body"] or {}).get("dataset_id") == QUOTE_DATASET_ID
    ]
    assert daily_filters
    for filters in daily_filters:
        assert set(filters) == {"trade_date", "ts_code"}
        assert filters["trade_date"] == {"eq": LAST_COMPLETE_DAILY}
        assert filters["ts_code"]["in"]
        assert CASH_SESSION_SYMBOL in filters["ts_code"]["in"]
        assert len(filters["ts_code"]["in"]) <= CASH_SESSION_DAILY_TS_CODE_CHUNK
    assert "20260901" not in json.dumps(daily_filters)


def test_query_windows_closed_calendar_skips_daily_and_stays_fail_closed() -> None:
    transport = _RecordingTDTransport(
        calendar_rows=(_open_calendar_row(is_open=0),),
        daily_by_date={
            LAST_COMPLETE_DAILY: (
                _daily_row(
                    CASH_SESSION_SYMBOL,
                    trade_date=LAST_COMPLETE_DAILY,
                    close=12.5,
                ),
            ),
        },
    )
    windows = query_windows_from_tradingdatas(
        (CASH_SESSION_SYMBOL,),
        trade_date=CASH_SESSION_TRADE_DATE,
        client=_td_client(transport),
    )
    window = windows[CASH_SESSION_SYMBOL]
    assert window.session_calendar_ok is False
    assert window.observation_ready is False
    assert window.reason_code == "missing_dataset_catalog_or_session_window"
    assert not any(
        (call["json_body"] or {}).get("dataset_id") == QUOTE_DATASET_ID
        for call in transport.calls
        if call["method"] == "POST"
    )


def _daily_query_filters(transport: _RecordingTDTransport) -> list[dict[str, object]]:
    return [
        call["json_body"]["filters"]
        for call in transport.calls
        if call["method"] == "POST"
        and (call["json_body"] or {}).get("dataset_id") == QUOTE_DATASET_ID
    ]


def _universe_daily_rows() -> tuple[dict[str, object], ...]:
    assert len(CASH_SESSION_UNIVERSE) == 43
    return tuple(
        _daily_row(symbol, trade_date=LAST_COMPLETE_DAILY, close=10.0 + index * 0.1)
        for index, symbol in enumerate(CASH_SESSION_UNIVERSE)
    )


def test_unfiltered_daily_413_does_not_mark_present_names_missing_prior_close() -> None:
    transport = _RecordingTDTransport(
        calendar_rows=(_open_calendar_row(),),
        daily_by_date={LAST_COMPLETE_DAILY: _universe_daily_rows()},
    )
    windows = query_windows_from_tradingdatas(
        CASH_SESSION_UNIVERSE,
        trade_date=CASH_SESSION_TRADE_DATE,
        client=_td_client(transport),
    )
    daily_filters = _daily_query_filters(transport)
    assert daily_filters
    requested: list[str] = []
    for filters in daily_filters:
        assert set(filters) == {"trade_date", "ts_code"}
        assert filters["trade_date"] == {"eq": LAST_COMPLETE_DAILY}
        chunk = list(filters["ts_code"]["in"])
        assert chunk
        assert len(chunk) <= CASH_SESSION_DAILY_TS_CODE_CHUNK
        requested.extend(str(code) for code in chunk)
    assert requested == list(cash_session_daily_ts_codes(CASH_SESSION_UNIVERSE))
    assert "20260901" not in json.dumps(daily_filters)
    for index, symbol in enumerate(CASH_SESSION_UNIVERSE):
        window = windows[symbol]
        assert window.reason_code != "missing_prior_close"
        assert window.observation_ready is True
        assert window.prior_close_cny == pytest.approx(10.0 + index * 0.1)
        assert window.quote_trade_date == LAST_COMPLETE_DAILY


def test_chunk_split_on_budget_exceeded_still_binds_prior_close() -> None:
    class _SplitOnLargeIn(_RecordingTDTransport):
        def __call__(self, *, method, url, headers, json_body, timeout_seconds):
            if method == "POST" and (json_body or {}).get("dataset_id") == QUOTE_DATASET_ID:
                filters = (json_body or {}).get("filters") or {}
                ts_codes = (filters.get("ts_code") or {}).get("in") or []
                if len(ts_codes) > 1:
                    return HTTPResponse(
                        413,
                        {"error": "budget_exceeded", "reason": "budget_exceeded"},
                    )
            return super().__call__(
                method=method,
                url=url,
                headers=headers,
                json_body=json_body,
                timeout_seconds=timeout_seconds,
            )

    transport = _SplitOnLargeIn(
        calendar_rows=(_open_calendar_row(),),
        daily_by_date={LAST_COMPLETE_DAILY: _universe_daily_rows()},
    )
    windows = query_windows_from_tradingdatas(
        CASH_SESSION_UNIVERSE,
        trade_date=CASH_SESSION_TRADE_DATE,
        client=_td_client(transport),
    )
    daily_filters = _daily_query_filters(transport)
    assert daily_filters
    assert all(len(filters["ts_code"]["in"]) == 1 for filters in daily_filters)
    assert {filters["ts_code"]["in"][0] for filters in daily_filters} == set(
        CASH_SESSION_UNIVERSE
    )
    for symbol in CASH_SESSION_UNIVERSE:
        assert windows[symbol].prior_close_cny > 0
        assert windows[symbol].reason_code != "missing_prior_close"


def test_open_window_without_champion_is_not_missing_catalog(tmp_path: Path) -> None:
    windows = {
        CASH_SESSION_SYMBOL: bind_cash_session_windows(
            (CASH_SESSION_SYMBOL,),
            trade_date=CASH_SESSION_TRADE_DATE,
            catalog_version="td-catalog-live",
            calendar_rows=(_open_calendar_row(),),
            daily_rows=(
                _daily_row(
                    CASH_SESSION_SYMBOL,
                    trade_date=LAST_COMPLETE_DAILY,
                    close=12.5,
                ),
            ),
        )[CASH_SESSION_SYMBOL]
    }
    result = _run_session(tmp_path, windows=windows)
    row = result.disposition_for(CASH_SESSION_SYMBOL)
    assert row.disposition is ExposureDisposition.REJECTED
    assert row.reason_code == "champion_current_unavailable"
    assert row.reason_code != "missing_dataset_catalog_or_session_window"
    assert result.fill_count == 0
    assert result.canonical_account_connected is False


SHANGHAI = ZoneInfo("Asia/Shanghai")
CASH_SESSION_DECISION = datetime(2026, 9, 1, 12, 17, tzinfo=SHANGHAI)
CASH_SESSION_QUOTE_SLOT = "2026-09-01 11:30:00"


def _quote_clock_row(
    symbol: str,
    *,
    time_text: str = CASH_SESSION_QUOTE_SLOT,
    freq: str = "5MIN",
    close: float = 12.8,
    vol: float | None = None,
    high: float | None = None,
    low: float | None = None,
) -> dict[str, object]:
    row: dict[str, object] = {
        "ts_code": symbol,
        "freq": freq,
        "time": time_text,
        "open": close,
        "close": close,
        "high": close if high is None else high,
        "low": close if low is None else low,
    }
    if vol is not None:
        row["vol"] = vol
    return row


def _production_rt_min_row(
    symbol: str,
    *,
    time_text: str = CASH_SESSION_QUOTE_SLOT,
    close: float = 12.8,
    vol: float | None = 8_800.0,
    high: float = 13.0,
    low: float = 12.4,
    trade_date: str = "20260901",
) -> dict[str, object]:
    """Live ``cn.dataset.rt_min`` shape: trade_date + close + time, no freq."""

    row: dict[str, object] = {
        "ts_code": symbol,
        "trade_date": trade_date,
        "time": time_text,
        "open": close,
        "close": close,
        "high": high,
        "low": low,
    }
    if vol is not None:
        row["vol"] = vol
    return row


def _quote_clock_proof() -> QuoteClockQueryProof:
    return QuoteClockQueryProof(
        receipt_id="receipt-cn.dataset.rt_min",
        catalog_version="td-catalog-live",
        data_through="2026-08-31T16:00:00+08:00",
        observed_at="2026-09-01T09:30:00+08:00",
        source_sha256="a" * 64,
        source_lineage_sha256="b" * 64,
    )


def _snapshot_ready_window(
    symbol: str,
    *,
    prior_close_cny: float = 10.0,
    last_cny: float = 10.0,
    clock_at: str = "2026-07-16 09:35:00",
    high_cny: float | None = None,
    low_cny: float | None = None,
) -> object:
    clock = datetime.strptime(clock_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=SHANGHAI)
    iso = clock.isoformat()
    high = last_cny + 0.05 if high_cny is None else high_cny
    low = last_cny - 0.05 if low_cny is None else low_cny
    return make_observation_window(
        symbol,
        prior_close_cny=prior_close_cny,
        quote_clocks_ok=True,
        quote_clock_at=clock_at,
        quote_clock_dataset_id=QUOTE_CLOCK_DATASET_ID,
        snapshot_last_cny=last_cny,
        snapshot_high_cny=high,
        snapshot_low_cny=low,
        snapshot_open_cny=last_cny,
        snapshot_volume=12_345.0,
        snapshot_receipt_id="receipt-cn.dataset.rt_min",
        snapshot_source_sha256="a" * 64,
        snapshot_lineage_sha256="b" * 64,
        snapshot_data_through=iso,
        snapshot_observed_at=iso,
        snapshot_available_at=iso,
        snapshot_catalog_version="td-catalog-v1",
    )


def _cash_session_daily_windows(
    *symbols: str,
) -> dict[str, object]:
    return bind_cash_session_windows(
        symbols,
        trade_date=CASH_SESSION_TRADE_DATE,
        catalog_version="td-catalog-live",
        calendar_rows=(_open_calendar_row(),),
        daily_rows=tuple(
            _daily_row(symbol, trade_date=LAST_COMPLETE_DAILY, close=12.5)
            for symbol in symbols
        ),
    )


def test_last_complete_in_session_quote_slot_is_not_daily_close() -> None:
    lunch = last_complete_in_session_quote_slot(
        trade_date=CASH_SESSION_TRADE_DATE,
        decision_as_of=CASH_SESSION_DECISION,
    )
    assert lunch is not None
    assert lunch.strftime("%Y-%m-%d %H:%M:%S") == CASH_SESSION_QUOTE_SLOT
    preopen = last_complete_in_session_quote_slot(
        trade_date=CASH_SESSION_TRADE_DATE,
        decision_as_of=datetime(2026, 9, 1, 9, 30, tzinfo=SHANGHAI),
    )
    assert preopen is None
    first_bar = last_complete_in_session_quote_slot(
        trade_date=CASH_SESSION_TRADE_DATE,
        decision_as_of=datetime(2026, 9, 1, 9, 37, tzinfo=SHANGHAI),
    )
    assert first_bar is not None
    assert first_bar.strftime("%H:%M:%S") == "09:35:00"
    other_day = last_complete_in_session_quote_slot(
        trade_date=CASH_SESSION_TRADE_DATE,
        decision_as_of=datetime(2026, 9, 2, 12, 17, tzinfo=SHANGHAI),
    )
    assert other_day is None


def test_in_session_quote_clock_slot_uses_open_print_before_first_complete_bar() -> None:
    open_oneshot = in_session_quote_clock_slot(
        trade_date=CASH_SESSION_TRADE_DATE,
        decision_as_of=datetime(2026, 9, 1, 9, 30, 29, tzinfo=SHANGHAI),
    )
    assert open_oneshot is not None
    assert open_oneshot.strftime("%Y-%m-%d %H:%M:%S") == "2026-09-01 09:30:00"
    assert last_complete_in_session_quote_slot(
        trade_date=CASH_SESSION_TRADE_DATE,
        decision_as_of=datetime(2026, 9, 1, 9, 30, 29, tzinfo=SHANGHAI),
    ) is None
    after_first_bar = in_session_quote_clock_slot(
        trade_date=CASH_SESSION_TRADE_DATE,
        decision_as_of=datetime(2026, 9, 1, 9, 37, tzinfo=SHANGHAI),
    )
    assert after_first_bar is not None
    assert after_first_bar.strftime("%H:%M:%S") == "09:35:00"
    lunch = in_session_quote_clock_slot(
        trade_date=CASH_SESSION_TRADE_DATE,
        decision_as_of=CASH_SESSION_DECISION,
    )
    assert lunch is not None
    assert lunch.strftime("%Y-%m-%d %H:%M:%S") == CASH_SESSION_QUOTE_SLOT
    afternoon_open = in_session_quote_clock_slot(
        trade_date=CASH_SESSION_TRADE_DATE,
        decision_as_of=datetime(2026, 9, 1, 13, 2, tzinfo=SHANGHAI),
    )
    assert afternoon_open is not None
    assert afternoon_open.strftime("%H:%M:%S") == "13:00:00"
    closing = in_session_quote_clock_slot(
        trade_date=CASH_SESSION_TRADE_DATE,
        decision_as_of=datetime(2026, 9, 1, 14, 59, tzinfo=SHANGHAI),
    )
    assert closing is not None
    assert closing.strftime("%H:%M:%S") == "14:55:00"
    preopen = in_session_quote_clock_slot(
        trade_date=CASH_SESSION_TRADE_DATE,
        decision_as_of=datetime(2026, 9, 1, 9, 15, tzinfo=SHANGHAI),
    )
    assert preopen is None


def test_cash_session_daily_close_does_not_set_clock_or_mint_fill(
    tmp_path: Path,
) -> None:
    windows = _cash_session_daily_windows(CASH_SESSION_SYMBOL)
    window = windows[CASH_SESSION_SYMBOL]
    assert window.observation_ready is True
    assert window.prior_close_cny == 12.5
    assert window.quote_clocks_ok is False
    assert window.quote_clock_at == ""
    overlay = bind_quote_clocks(
        windows,
        trade_date=CASH_SESSION_TRADE_DATE,
        decision_as_of=CASH_SESSION_DECISION,
        quote_clock_rows=(
            _daily_row(
                CASH_SESSION_SYMBOL,
                trade_date=LAST_COMPLETE_DAILY,
                close=12.5,
            ),
            _daily_row(
                CASH_SESSION_SYMBOL,
                trade_date="20260901",
                close=99.0,
                pre_close=12.5,
            ),
        ),
    )
    assert overlay[CASH_SESSION_SYMBOL].quote_clocks_ok is False
    assert overlay[CASH_SESSION_SYMBOL].quote_clock_at == ""
    registry, _manifest = _manual_registry(tmp_path)
    result = _run_session(
        tmp_path,
        windows=windows,
        champion_registry=registry,
    )
    row = result.disposition_for(CASH_SESSION_SYMBOL)
    assert row.disposition is ExposureDisposition.PAPER_NOT_FILLED
    assert row.nonfill_reason == "quote_clocks_unavailable"
    assert result.fill_count == 0
    assert close_or_touch_is_not_a_fill() == 0


def test_missing_and_present_quote_clocks_are_distinguished(tmp_path: Path) -> None:
    present = CASH_SESSION_SYMBOL
    missing = "600276.SH"
    windows = bind_quote_clocks(
        _cash_session_daily_windows(present, missing),
        trade_date=CASH_SESSION_TRADE_DATE,
        decision_as_of=CASH_SESSION_DECISION,
        quote_clock_rows=(_quote_clock_row(present),),
    )
    assert windows[present].quote_clocks_ok is True
    assert windows[present].quote_clock_at == CASH_SESSION_QUOTE_SLOT
    assert windows[present].quote_clock_dataset_id == QUOTE_CLOCK_DATASET_ID
    assert windows[present].prior_close_cny == 12.5
    assert windows[missing].quote_clocks_ok is False
    assert windows[missing].quote_clock_at == ""
    assert windows[missing].observation_ready is True
    registry, _manifest = _manual_registry(tmp_path)
    result = _run_session(
        tmp_path,
        windows=windows,
        champion_registry=registry,
    )
    filled_or_later = result.disposition_for(present)
    still_missing = result.disposition_for(missing)
    assert filled_or_later.disposition is ExposureDisposition.PAPER_NOT_FILLED
    assert filled_or_later.nonfill_reason != "quote_clocks_unavailable"
    assert filled_or_later.nonfill_reason == "capital_fill_market_snapshot_unavailable"
    assert still_missing.disposition is ExposureDisposition.PAPER_NOT_FILLED
    assert still_missing.nonfill_reason == "quote_clocks_unavailable"
    assert result.fill_count == 0
    assert result.canonical_account_connected is False
    assert result.disposition_for("300750.SZ").rejection_reason == (
        "chinext_individual_permission_unavailable"
    )
    assert result.disposition_for("688981.SH").rejection_reason == (
        "star_individual_permission_unavailable"
    )


def test_production_rt_min_row_with_trade_date_is_not_daily_close() -> None:
    daily = _daily_row(
        CASH_SESSION_SYMBOL,
        trade_date=LAST_COMPLETE_DAILY,
        close=12.5,
    )
    live_bar = _production_rt_min_row(CASH_SESSION_SYMBOL)
    assert _looks_like_daily_close_row(daily) is True
    assert _looks_like_daily_close_row(live_bar) is False
    windows = bind_quote_clocks(
        _cash_session_daily_windows(CASH_SESSION_SYMBOL),
        trade_date=CASH_SESSION_TRADE_DATE,
        decision_as_of=CASH_SESSION_DECISION,
        quote_clock_rows=(live_bar,),
    )
    window = windows[CASH_SESSION_SYMBOL]
    assert window.quote_clocks_ok is True
    assert window.quote_clock_at == CASH_SESSION_QUOTE_SLOT
    assert window.fill_quote_ready is True
    overlay = bind_market_snapshots(
        windows,
        trade_date=CASH_SESSION_TRADE_DATE,
        decision_as_of=CASH_SESSION_DECISION,
        snapshot_rows=(live_bar,),
        snapshot_proof=_quote_clock_proof(),
    )
    assert overlay[CASH_SESSION_SYMBOL].fill_snapshot_ready is True
    assert overlay[CASH_SESSION_SYMBOL].snapshot_last_cny == 12.8
    assert overlay[CASH_SESSION_SYMBOL].snapshot_volume == 8_800.0


def test_production_rt_min_clock_without_volume_leaves_later_reason(
    tmp_path: Path,
) -> None:
    present = CASH_SESSION_SYMBOL
    missing = "600276.SH"
    windows = bind_quote_clocks(
        _cash_session_daily_windows(present, missing),
        trade_date=CASH_SESSION_TRADE_DATE,
        decision_as_of=CASH_SESSION_DECISION,
        quote_clock_rows=(_production_rt_min_row(present, vol=None),),
    )
    overlay = bind_market_snapshots(
        windows,
        trade_date=CASH_SESSION_TRADE_DATE,
        decision_as_of=CASH_SESSION_DECISION,
        snapshot_rows=(_production_rt_min_row(present, vol=None),),
        snapshot_proof=_quote_clock_proof(),
    )
    assert overlay[present].quote_clocks_ok is True
    assert overlay[present].fill_snapshot_ready is False
    assert overlay[missing].quote_clocks_ok is False
    registry, _manifest = _manual_registry(tmp_path)
    result = _run_session(
        tmp_path,
        windows=overlay,
        champion_registry=registry,
    )
    later = result.disposition_for(present)
    still_missing = result.disposition_for(missing)
    assert later.disposition is ExposureDisposition.PAPER_NOT_FILLED
    assert later.nonfill_reason != "quote_clocks_unavailable"
    assert later.nonfill_reason == "capital_fill_market_snapshot_unavailable"
    assert still_missing.nonfill_reason == "quote_clocks_unavailable"
    assert result.fill_count == 0


def test_query_windows_present_rt_min_clock_is_not_daily_close() -> None:
    transport = _RecordingTDTransport(
        calendar_rows=(_open_calendar_row(),),
        daily_by_date={
            LAST_COMPLETE_DAILY: (
                _daily_row(
                    CASH_SESSION_SYMBOL,
                    trade_date=LAST_COMPLETE_DAILY,
                    close=12.5,
                ),
            ),
        },
        quote_clock_rows=(_quote_clock_row(CASH_SESSION_SYMBOL),),
    )
    windows = query_windows_from_tradingdatas(
        (CASH_SESSION_SYMBOL,),
        trade_date=CASH_SESSION_TRADE_DATE,
        decision_as_of=CASH_SESSION_DECISION,
        client=_td_client(transport, include_quote_clock=True),
    )
    window = windows[CASH_SESSION_SYMBOL]
    assert window.observation_ready is True
    assert window.prior_close_cny == 12.5
    assert window.quote_trade_date == LAST_COMPLETE_DAILY
    assert window.quote_clocks_ok is True
    assert window.quote_clock_at == CASH_SESSION_QUOTE_SLOT
    assert window.fill_quote_ready is True
    assert window.fill_snapshot_ready is False
    clock_filters = [
        call["json_body"]["filters"]
        for call in transport.calls
        if call["method"] == "POST"
        and (call["json_body"] or {}).get("dataset_id") == QUOTE_CLOCK_DATASET_ID
    ]
    assert clock_filters
    for filters in clock_filters:
        assert filters["time"] == {"eq": CASH_SESSION_QUOTE_SLOT}
        assert CASH_SESSION_SYMBOL in filters["ts_code"]["in"]
        assert "trade_date" not in filters
    daily_filters = _daily_query_filters(transport)
    assert daily_filters
    for filters in daily_filters:
        assert filters["trade_date"] == {"eq": LAST_COMPLETE_DAILY}


def test_query_windows_missing_rt_min_stays_quote_clocks_unavailable() -> None:
    transport = _RecordingTDTransport(
        calendar_rows=(_open_calendar_row(),),
        daily_by_date={
            LAST_COMPLETE_DAILY: (
                _daily_row(
                    CASH_SESSION_SYMBOL,
                    trade_date=LAST_COMPLETE_DAILY,
                    close=12.5,
                ),
            ),
        },
        include_quote_clock=True,
    )
    windows = query_windows_from_tradingdatas(
        (CASH_SESSION_SYMBOL,),
        trade_date=CASH_SESSION_TRADE_DATE,
        decision_as_of=CASH_SESSION_DECISION,
        client=_td_client(transport, include_quote_clock=True),
    )
    window = windows[CASH_SESSION_SYMBOL]
    assert window.observation_ready is True
    assert window.quote_clocks_ok is False
    assert window.quote_clock_at == ""
    assert window.fill_quote_ready is False
    without_clock_dataset = query_windows_from_tradingdatas(
        (CASH_SESSION_SYMBOL,),
        trade_date=CASH_SESSION_TRADE_DATE,
        decision_as_of=CASH_SESSION_DECISION,
        client=_td_client(
            _RecordingTDTransport(
                calendar_rows=(_open_calendar_row(),),
                daily_by_date={
                    LAST_COMPLETE_DAILY: (
                        _daily_row(
                            CASH_SESSION_SYMBOL,
                            trade_date=LAST_COMPLETE_DAILY,
                            close=12.5,
                        ),
                    ),
                },
            )
        ),
    )
    assert without_clock_dataset[CASH_SESSION_SYMBOL].quote_clocks_ok is False
    assert without_clock_dataset[CASH_SESSION_SYMBOL].fill_snapshot_ready is False


def test_daily_close_does_not_bind_market_snapshot_or_mint_fill(tmp_path: Path) -> None:
    windows = bind_quote_clocks(
        _cash_session_daily_windows(CASH_SESSION_SYMBOL),
        trade_date=CASH_SESSION_TRADE_DATE,
        decision_as_of=CASH_SESSION_DECISION,
        quote_clock_rows=(_quote_clock_row(CASH_SESSION_SYMBOL),),
    )
    overlay = bind_market_snapshots(
        windows,
        trade_date=CASH_SESSION_TRADE_DATE,
        decision_as_of=CASH_SESSION_DECISION,
        snapshot_rows=(
            _daily_row(
                CASH_SESSION_SYMBOL,
                trade_date=LAST_COMPLETE_DAILY,
                close=12.5,
            ),
            _daily_row(
                CASH_SESSION_SYMBOL,
                trade_date="20260901",
                close=99.0,
                pre_close=12.5,
            ),
        ),
        snapshot_proof=_quote_clock_proof(),
    )
    window = overlay[CASH_SESSION_SYMBOL]
    assert window.quote_clocks_ok is True
    assert window.fill_snapshot_ready is False
    assert window.snapshot_last_cny is None
    _prepare_ledger(tmp_path)
    registry, _manifest = _manual_registry(tmp_path)
    result = run_capital_backed_paper_session(
        _config(
            tmp_path,
            trade_date=CASH_SESSION_TRADE_DATE,
            decision_as_of=CASH_SESSION_DECISION,
        ),
        windows=overlay,
        champion_registry=registry,
    )
    row = result.disposition_for(CASH_SESSION_SYMBOL)
    assert row.disposition is ExposureDisposition.PAPER_NOT_FILLED
    assert row.nonfill_reason == "capital_fill_market_snapshot_unavailable"
    assert result.fill_count == 0
    assert close_or_touch_is_not_a_fill() == 0


def test_rt_min_bar_without_volume_stays_snapshot_unready() -> None:
    windows = bind_quote_clocks(
        _cash_session_daily_windows(CASH_SESSION_SYMBOL),
        trade_date=CASH_SESSION_TRADE_DATE,
        decision_as_of=CASH_SESSION_DECISION,
        quote_clock_rows=(_quote_clock_row(CASH_SESSION_SYMBOL),),
    )
    overlay = bind_market_snapshots(
        windows,
        trade_date=CASH_SESSION_TRADE_DATE,
        decision_as_of=CASH_SESSION_DECISION,
        snapshot_rows=(_quote_clock_row(CASH_SESSION_SYMBOL),),
        snapshot_proof=_quote_clock_proof(),
    )
    assert overlay[CASH_SESSION_SYMBOL].fill_quote_ready is True
    assert overlay[CASH_SESSION_SYMBOL].fill_snapshot_ready is False


def test_query_windows_rt_min_bar_binds_snapshot_not_daily_through() -> None:
    transport = _RecordingTDTransport(
        calendar_rows=(_open_calendar_row(),),
        daily_by_date={
            LAST_COMPLETE_DAILY: (
                _daily_row(
                    CASH_SESSION_SYMBOL,
                    trade_date=LAST_COMPLETE_DAILY,
                    close=12.5,
                ),
            ),
        },
        quote_clock_rows=(
            _quote_clock_row(
                CASH_SESSION_SYMBOL,
                vol=8_800.0,
                high=13.0,
                low=12.4,
            ),
        ),
    )
    windows = query_windows_from_tradingdatas(
        (CASH_SESSION_SYMBOL,),
        trade_date=CASH_SESSION_TRADE_DATE,
        decision_as_of=CASH_SESSION_DECISION,
        client=_td_client(transport, include_quote_clock=True),
    )
    window = windows[CASH_SESSION_SYMBOL]
    assert window.fill_quote_ready is True
    assert window.fill_snapshot_ready is True
    assert window.snapshot_last_cny == 12.8
    assert window.snapshot_volume == 8_800.0
    assert window.snapshot_receipt_id == "receipt-cn.dataset.rt_min"
    assert window.snapshot_data_through.startswith("2026-09-01T11:30:00")
    assert "16:00:00" not in window.snapshot_data_through
    assert window.snapshot_data_through != "2026-08-31T16:00:00+08:00"


def test_query_windows_production_rt_min_row_binds_clock_and_snapshot() -> None:
    transport = _RecordingTDTransport(
        calendar_rows=(_open_calendar_row(),),
        daily_by_date={
            LAST_COMPLETE_DAILY: (
                _daily_row(
                    CASH_SESSION_SYMBOL,
                    trade_date=LAST_COMPLETE_DAILY,
                    close=12.5,
                ),
            ),
        },
        quote_clock_rows=(_production_rt_min_row(CASH_SESSION_SYMBOL),),
    )
    windows = query_windows_from_tradingdatas(
        (CASH_SESSION_SYMBOL,),
        trade_date=CASH_SESSION_TRADE_DATE,
        decision_as_of=CASH_SESSION_DECISION,
        client=_td_client(transport, include_quote_clock=True),
    )
    window = windows[CASH_SESSION_SYMBOL]
    assert window.quote_clocks_ok is True
    assert window.fill_quote_ready is True
    assert window.fill_snapshot_ready is True
    assert window.quote_clock_at == CASH_SESSION_QUOTE_SLOT
    assert window.snapshot_last_cny == 12.8
    assert window.reason_code == "window_ready"


def test_envelope_proof_failure_does_not_discard_clock_rows() -> None:
    class _BrokenEnvelope:
        catalog_version = "td-catalog-live"
        request_id = "query-cn.dataset.rt_min"

        @property
        def metadata(self) -> object:
            raise RuntimeError("receipt_proof_boom")

    proof = _envelope_query_proof(_BrokenEnvelope())
    assert proof.receipt_id == ""
    assert proof.source_sha256 == ""
    windows = bind_quote_clocks(
        _cash_session_daily_windows(CASH_SESSION_SYMBOL),
        trade_date=CASH_SESSION_TRADE_DATE,
        decision_as_of=CASH_SESSION_DECISION,
        quote_clock_rows=(_production_rt_min_row(CASH_SESSION_SYMBOL),),
    )
    overlay = bind_market_snapshots(
        windows,
        trade_date=CASH_SESSION_TRADE_DATE,
        decision_as_of=CASH_SESSION_DECISION,
        snapshot_rows=(_production_rt_min_row(CASH_SESSION_SYMBOL),),
        snapshot_proof=proof,
    )
    assert overlay[CASH_SESSION_SYMBOL].quote_clocks_ok is True
    assert overlay[CASH_SESSION_SYMBOL].fill_snapshot_ready is False


def test_fresh_production_rt_min_bind_is_neither_banned_reason(tmp_path: Path) -> None:
    _prepare_ledger(tmp_path)
    registry, _manifest = _manual_registry(tmp_path)
    decision = datetime(2026, 7, 16, 9, 35, 10, tzinfo=SHANGHAI)
    symbol = "000063.SZ"
    daily = bind_cash_session_windows(
        (symbol,),
        trade_date=TRADE_DATE,
        catalog_version="td-catalog-live",
        calendar_rows=(
            {
                "exchange": "SSE",
                "cal_date": "20260716",
                "is_open": 1,
                "pretrade_date": "20260715",
            },
        ),
        daily_rows=(_daily_row(symbol, trade_date="20260715", close=10.0),),
    )
    clocked = bind_quote_clocks(
        daily,
        trade_date=TRADE_DATE,
        decision_as_of=decision,
        quote_clock_rows=(
            _production_rt_min_row(
                symbol,
                time_text="2026-07-16 09:35:00",
                trade_date="20260716",
                close=10.0,
                high=10.05,
                low=9.95,
            ),
        ),
    )
    overlay = bind_market_snapshots(
        clocked,
        trade_date=TRADE_DATE,
        decision_as_of=decision,
        snapshot_rows=(
            _production_rt_min_row(
                symbol,
                time_text="2026-07-16 09:35:00",
                trade_date="20260716",
                close=10.0,
                high=10.05,
                low=9.95,
            ),
        ),
        snapshot_proof=_quote_clock_proof(),
    )
    assert overlay[symbol].quote_clocks_ok is True
    assert overlay[symbol].fill_snapshot_ready is True
    result = run_capital_backed_paper_session(
        _config(tmp_path, decision_as_of=decision),
        windows=overlay,
        champion_registry=registry,
    )
    row = result.disposition_for(symbol)
    assert row.nonfill_reason not in IN_SESSION_BANNED_NONFILL
    assert row.disposition is ExposureDisposition.PAPER_FILLED
    assert result.fill_count == 1
    assert result.canonical_account_connected is False
    assert result.disposition_for("300750.SZ").rejection_reason == (
        "chinext_individual_permission_unavailable"
    )
    assert result.disposition_for("688981.SH").rejection_reason == (
        "star_individual_permission_unavailable"
    )


IN_SESSION_BANNED_NONFILL = frozenset(
    {
        "quote_clocks_unavailable",
        "capital_fill_market_snapshot_unavailable",
        "capital_fill_bar_evidence_invalid",
        "paper_market_snapshot_stale",
    }
)


def test_lunch_bound_snapshot_is_session_unavailable_not_a_fill(
    tmp_path: Path,
) -> None:
    _prepare_ledger(tmp_path)
    windows = bind_quote_clocks(
        _cash_session_daily_windows(CASH_SESSION_SYMBOL),
        trade_date=CASH_SESSION_TRADE_DATE,
        decision_as_of=CASH_SESSION_DECISION,
        quote_clock_rows=(_quote_clock_row(CASH_SESSION_SYMBOL),),
    )
    overlay = bind_market_snapshots(
        windows,
        trade_date=CASH_SESSION_TRADE_DATE,
        decision_as_of=CASH_SESSION_DECISION,
        snapshot_rows=(
            _quote_clock_row(
                CASH_SESSION_SYMBOL,
                vol=8_800.0,
                high=13.0,
                low=12.4,
            ),
        ),
        snapshot_proof=_quote_clock_proof(),
    )
    assert overlay[CASH_SESSION_SYMBOL].fill_snapshot_ready is True
    registry, _manifest = _manual_registry(tmp_path)
    result = run_capital_backed_paper_session(
        _config(
            tmp_path,
            trade_date=CASH_SESSION_TRADE_DATE,
            decision_as_of=CASH_SESSION_DECISION,
        ),
        windows=overlay,
        champion_registry=registry,
    )
    row = result.disposition_for(CASH_SESSION_SYMBOL)
    assert row.disposition is ExposureDisposition.PAPER_NOT_FILLED
    assert row.nonfill_reason == "paper_continuous_session_unavailable"
    assert row.nonfill_reason not in IN_SESSION_BANNED_NONFILL
    assert result.fill_count == 0
    assert close_or_touch_is_not_a_fill() == 0
    assert result.disposition_for("300750.SZ").rejection_reason == (
        "chinext_individual_permission_unavailable"
    )
    assert result.disposition_for("688981.SH").rejection_reason == (
        "star_individual_permission_unavailable"
    )


def test_default_fill_path_paper_filled_requires_ledger_fill_commit(
    tmp_path: Path,
) -> None:
    _prepare_ledger(tmp_path)
    registry, _manifest = _manual_registry(tmp_path)
    decision = datetime(2026, 7, 16, 9, 35, 10, tzinfo=SHANGHAI)
    symbol = "000063.SZ"
    windows = {symbol: _snapshot_ready_window(symbol)}
    result = run_capital_backed_paper_session(
        _config(tmp_path, decision_as_of=decision),
        windows=windows,
        champion_registry=registry,
    )
    filled = result.disposition_for(symbol)
    assert filled.disposition is ExposureDisposition.PAPER_FILLED
    assert filled.simulated_fill_id
    assert filled.filled_quantity == 100
    assert filled.filled_notional_cny > 0
    assert filled.rejection_reason is None
    assert filled.nonfill_reason is None
    assert result.fill_count == 1
    ledger = MarketCapitalLedger(
        _config(tmp_path).ledger_root,
        policy=MarketPolicy.load("ashare"),
    )
    snapshot = ledger.snapshot()
    assert snapshot.cash_balance_cny < 50_000.0
    assert snapshot.positions_quantity_by_risk_unit.get(symbol, 0) >= 100
    assert snapshot.unreconciled_fill_commit_ids
    events_path = Path(ledger.root) / ledger.events_filename
    events = [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert any(row.get("event_type") == "fill_commit" for row in events)
    latest = json.loads(result.latest_path.read_text(encoding="utf-8"))
    assert latest["fill_count"] == 1
    assert latest["canonical_account_connected"] is False
    assert latest["real_trading_enabled"] is False
    assert result.disposition_for("300750.SZ").rejection_reason == (
        "chinext_individual_permission_unavailable"
    )
    assert result.disposition_for("688981.SH").rejection_reason == (
        "star_individual_permission_unavailable"
    )


def test_doji_and_close_at_high_still_model_a_one_tick_book() -> None:
    doji = _bar_evidence_bid_ask(last_cny=10.0, high_cny=10.0, low_cny=10.0)
    assert doji is not None
    bid, ask = doji
    assert ask > bid
    assert ask != 10.0 or bid != 10.0
    close_at_high = _bar_evidence_bid_ask(last_cny=12.8, high_cny=12.8, low_cny=12.76)
    assert close_at_high is not None
    assert close_at_high[1] > close_at_high[0]


def test_bar_evidence_fill_gate_keeps_session_and_cross_session_honest() -> None:
    bar_0935 = datetime(2026, 9, 3, 9, 35, tzinfo=SHANGHAI)
    assert (
        _bar_evidence_fill_gate(
            decision=datetime(2026, 9, 3, 9, 30, 29, tzinfo=SHANGHAI),
            bar_slot=datetime(2026, 9, 3, 9, 30, tzinfo=SHANGHAI),
        )
        is None
    )
    assert (
        _bar_evidence_fill_gate(
            decision=datetime(2026, 9, 3, 9, 37, tzinfo=SHANGHAI),
            bar_slot=bar_0935,
        )
        is None
    )
    assert (
        _bar_evidence_fill_gate(
            decision=datetime(2026, 9, 3, 12, 17, tzinfo=SHANGHAI),
            bar_slot=datetime(2026, 9, 3, 11, 30, tzinfo=SHANGHAI),
        )
        == "paper_continuous_session_unavailable"
    )
    assert (
        _bar_evidence_fill_gate(
            decision=datetime(2026, 9, 3, 14, 59, tzinfo=SHANGHAI),
            bar_slot=datetime(2026, 9, 3, 14, 55, tzinfo=SHANGHAI),
        )
        == "paper_continuous_session_unavailable"
    )
    assert (
        _bar_evidence_fill_gate(
            decision=datetime(2026, 9, 3, 13, 2, tzinfo=SHANGHAI),
            bar_slot=datetime(2026, 9, 3, 11, 30, tzinfo=SHANGHAI),
        )
        == "paper_market_snapshot_stale"
    )


def test_in_session_doji_bar_outside_30s_is_paper_filled(
    tmp_path: Path,
) -> None:
    _prepare_ledger(tmp_path)
    registry, _manifest = _manual_registry(tmp_path)
    decision = datetime(2026, 7, 16, 9, 38, tzinfo=SHANGHAI)
    symbol = "000063.SZ"
    windows = {
        symbol: _snapshot_ready_window(
            symbol,
            last_cny=10.0,
            high_cny=10.0,
            low_cny=10.0,
            clock_at="2026-07-16 09:35:00",
        )
    }
    result = run_capital_backed_paper_session(
        _config(tmp_path, decision_as_of=decision),
        windows=windows,
        champion_registry=registry,
    )
    filled = result.disposition_for(symbol)
    assert filled.nonfill_reason not in IN_SESSION_BANNED_NONFILL
    assert filled.disposition is ExposureDisposition.PAPER_FILLED
    assert filled.simulated_fill_id
    assert result.fill_count == 1
    ledger = MarketCapitalLedger(
        _config(tmp_path).ledger_root,
        policy=MarketPolicy.load("ashare"),
    )
    events = [
        json.loads(line)
        for line in (Path(ledger.root) / ledger.events_filename)
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    assert any(row.get("event_type") == "fill_commit" for row in events)
    assert result.canonical_account_connected is False
    assert result.disposition_for("300750.SZ").rejection_reason == (
        "chinext_individual_permission_unavailable"
    )
    assert result.disposition_for("688981.SH").rejection_reason == (
        "star_individual_permission_unavailable"
    )


def test_open_session_rt_min_binds_clock_and_snapshot_without_complete_5min_bar() -> None:
    trade_date = "2026-09-03"
    decision = datetime(2026, 9, 3, 9, 30, 29, tzinfo=SHANGHAI)
    symbol = CASH_SESSION_SYMBOL
    open_print = _production_rt_min_row(
        symbol,
        time_text="2026-09-03 09:30:00",
        trade_date="20260903",
        close=12.8,
        high=12.8,
        low=12.74,
    )
    daily = bind_cash_session_windows(
        (symbol,),
        trade_date=trade_date,
        catalog_version="td-catalog-live",
        calendar_rows=(
            {
                "exchange": "SSE",
                "cal_date": "20260903",
                "is_open": 1,
                "pretrade_date": "20260902",
            },
        ),
        daily_rows=(_daily_row(symbol, trade_date="20260902", close=12.8),),
    )
    clocked = bind_quote_clocks(
        daily,
        trade_date=trade_date,
        decision_as_of=decision,
        quote_clock_rows=(open_print,),
    )
    overlay = bind_market_snapshots(
        clocked,
        trade_date=trade_date,
        decision_as_of=decision,
        snapshot_rows=(open_print,),
        snapshot_proof=_quote_clock_proof(),
    )
    window = overlay[symbol]
    assert window.quote_clocks_ok is True
    assert window.quote_clock_at == "2026-09-03 09:30:00"
    assert window.fill_snapshot_ready is True
    assert window.snapshot_last_cny == 12.8
    assert window.snapshot_volume == 8_800.0


def test_open_session_daily_close_is_not_a_clock_or_snapshot() -> None:
    trade_date = "2026-09-03"
    decision = datetime(2026, 9, 3, 9, 30, 29, tzinfo=SHANGHAI)
    symbol = CASH_SESSION_SYMBOL
    daily = bind_cash_session_windows(
        (symbol,),
        trade_date=trade_date,
        catalog_version="td-catalog-live",
        calendar_rows=(
            {
                "exchange": "SSE",
                "cal_date": "20260903",
                "is_open": 1,
                "pretrade_date": "20260902",
            },
        ),
        daily_rows=(_daily_row(symbol, trade_date="20260902", close=12.8),),
    )
    clocked = bind_quote_clocks(
        daily,
        trade_date=trade_date,
        decision_as_of=decision,
        quote_clock_rows=(
            _daily_row(symbol, trade_date="20260902", close=12.8),
            _daily_row(symbol, trade_date="20260903", close=99.0, pre_close=12.8),
        ),
    )
    overlay = bind_market_snapshots(
        clocked,
        trade_date=trade_date,
        decision_as_of=decision,
        snapshot_rows=(
            _daily_row(symbol, trade_date="20260902", close=12.8),
        ),
        snapshot_proof=_quote_clock_proof(),
    )
    window = overlay[symbol]
    assert window.quote_clocks_ok is False
    assert window.quote_clock_at == ""
    assert window.fill_snapshot_ready is False


def test_open_session_query_windows_requests_0930_and_binds_snapshot() -> None:
    trade_date = "2026-09-03"
    decision = datetime(2026, 9, 3, 9, 30, 29, tzinfo=SHANGHAI)
    symbol = CASH_SESSION_SYMBOL
    transport = _RecordingTDTransport(
        calendar_rows=(
            {
                "exchange": "SSE",
                "cal_date": "20260903",
                "is_open": 1,
                "pretrade_date": "20260902",
            },
        ),
        daily_by_date={
            "20260902": (_daily_row(symbol, trade_date="20260902", close=12.8),),
        },
        quote_clock_rows=(
            _production_rt_min_row(
                symbol,
                time_text="2026-09-03 09:30:00",
                trade_date="20260903",
                close=12.8,
                high=12.8,
                low=12.74,
            ),
        ),
    )
    windows = query_windows_from_tradingdatas(
        (symbol,),
        trade_date=trade_date,
        decision_as_of=decision,
        client=_td_client(transport, include_quote_clock=True),
    )
    window = windows[symbol]
    assert window.quote_clocks_ok is True
    assert window.quote_clock_at == "2026-09-03 09:30:00"
    assert window.fill_snapshot_ready is True
    assert window.snapshot_last_cny == 12.8
    clock_filters = [
        call["json_body"]["filters"]
        for call in transport.calls
        if call["method"] == "POST"
        and (call["json_body"] or {}).get("dataset_id") == QUOTE_CLOCK_DATASET_ID
    ]
    assert clock_filters
    for filters in clock_filters:
        assert filters["time"] == {"eq": "2026-09-03 09:30:00"}
        assert symbol in filters["ts_code"]["in"]
        assert "trade_date" not in filters


def test_open_session_missing_rt_min_stays_quote_clocks_unavailable() -> None:
    trade_date = "2026-09-03"
    decision = datetime(2026, 9, 3, 9, 30, 29, tzinfo=SHANGHAI)
    symbol = CASH_SESSION_SYMBOL
    transport = _RecordingTDTransport(
        calendar_rows=(
            {
                "exchange": "SSE",
                "cal_date": "20260903",
                "is_open": 1,
                "pretrade_date": "20260902",
            },
        ),
        daily_by_date={
            "20260902": (_daily_row(symbol, trade_date="20260902", close=12.8),),
        },
        include_quote_clock=True,
    )
    windows = query_windows_from_tradingdatas(
        (symbol,),
        trade_date=trade_date,
        decision_as_of=decision,
        client=_td_client(transport, include_quote_clock=True),
    )
    window = windows[symbol]
    assert window.observation_ready is True
    assert window.quote_clocks_ok is False
    assert window.fill_snapshot_ready is False


def test_thursday_open_oneshot_without_complete_5min_bar_is_paper_filled(
    tmp_path: Path,
) -> None:
    """Proof window: Thu 2026-09-03 09:30:29 CST, first 09:35 bar not complete."""

    _prepare_ledger(tmp_path)
    registry, _manifest = _manual_registry(tmp_path)
    trade_date = "2026-09-03"
    decision = datetime(2026, 9, 3, 9, 30, 29, tzinfo=SHANGHAI)
    symbol = "000063.SZ"
    live_bar = _production_rt_min_row(
        symbol,
        time_text="2026-09-03 09:30:00",
        trade_date="20260903",
        close=12.8,
        high=12.8,
        low=12.74,
    )
    daily = bind_cash_session_windows(
        (symbol,),
        trade_date=trade_date,
        catalog_version="td-catalog-live",
        calendar_rows=(
            {
                "exchange": "SSE",
                "cal_date": "20260903",
                "is_open": 1,
                "pretrade_date": "20260902",
            },
        ),
        daily_rows=(_daily_row(symbol, trade_date="20260902", close=12.8),),
    )
    clocked = bind_quote_clocks(
        daily,
        trade_date=trade_date,
        decision_as_of=decision,
        quote_clock_rows=(live_bar,),
    )
    overlay = bind_market_snapshots(
        clocked,
        trade_date=trade_date,
        decision_as_of=decision,
        snapshot_rows=(live_bar,),
        snapshot_proof=_quote_clock_proof(),
    )
    assert overlay[symbol].quote_clocks_ok is True
    assert overlay[symbol].fill_snapshot_ready is True
    result = run_capital_backed_paper_session(
        _config(tmp_path, trade_date=trade_date, decision_as_of=decision),
        windows=overlay,
        champion_registry=registry,
    )
    filled = result.disposition_for(symbol)
    assert filled.nonfill_reason not in IN_SESSION_BANNED_NONFILL
    assert filled.disposition is ExposureDisposition.PAPER_FILLED
    assert filled.simulated_fill_id
    assert filled.filled_quantity == 100
    assert result.fill_count == 1
    ledger = MarketCapitalLedger(
        _config(tmp_path, trade_date=trade_date, decision_as_of=decision).ledger_root,
        policy=MarketPolicy.load("ashare"),
    )
    snapshot = ledger.snapshot()
    assert snapshot.cash_balance_cny < 50_000.0
    assert snapshot.unreconciled_fill_commit_ids
    events = [
        json.loads(line)
        for line in (Path(ledger.root) / ledger.events_filename)
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    assert any(row.get("event_type") == "fill_commit" for row in events)
    latest = json.loads(result.latest_path.read_text(encoding="utf-8"))
    assert latest["fill_count"] == 1
    assert latest["canonical_account_connected"] is False
    assert latest["real_trading_enabled"] is False
    assert result.disposition_for("300750.SZ").rejection_reason == (
        "chinext_individual_permission_unavailable"
    )
    assert result.disposition_for("688981.SH").rejection_reason == (
        "star_individual_permission_unavailable"
    )


def test_thursday_open_oneshot_close_at_high_is_paper_filled(
    tmp_path: Path,
) -> None:
    """Proof window: Thu 2026-09-03 ~09:30 CST after the first 09:35 bar."""

    _prepare_ledger(tmp_path)
    registry, _manifest = _manual_registry(tmp_path)
    trade_date = "2026-09-03"
    decision = datetime(2026, 9, 3, 9, 37, tzinfo=SHANGHAI)
    symbol = "000063.SZ"
    live_bar = _production_rt_min_row(
        symbol,
        time_text="2026-09-03 09:35:00",
        trade_date="20260903",
        close=12.8,
        high=12.8,
        low=12.74,
    )
    daily = bind_cash_session_windows(
        (symbol,),
        trade_date=trade_date,
        catalog_version="td-catalog-live",
        calendar_rows=(
            {
                "exchange": "SSE",
                "cal_date": "20260903",
                "is_open": 1,
                "pretrade_date": "20260902",
            },
        ),
        daily_rows=(_daily_row(symbol, trade_date="20260902", close=12.8),),
    )
    clocked = bind_quote_clocks(
        daily,
        trade_date=trade_date,
        decision_as_of=decision,
        quote_clock_rows=(live_bar,),
    )
    overlay = bind_market_snapshots(
        clocked,
        trade_date=trade_date,
        decision_as_of=decision,
        snapshot_rows=(live_bar,),
        snapshot_proof=_quote_clock_proof(),
    )
    assert overlay[symbol].quote_clocks_ok is True
    assert overlay[symbol].fill_snapshot_ready is True
    result = run_capital_backed_paper_session(
        _config(tmp_path, trade_date=trade_date, decision_as_of=decision),
        windows=overlay,
        champion_registry=registry,
    )
    filled = result.disposition_for(symbol)
    assert filled.nonfill_reason not in IN_SESSION_BANNED_NONFILL
    assert filled.disposition is ExposureDisposition.PAPER_FILLED
    assert filled.simulated_fill_id
    assert filled.filled_quantity == 100
    assert result.fill_count == 1
    ledger = MarketCapitalLedger(
        _config(tmp_path, trade_date=trade_date, decision_as_of=decision).ledger_root,
        policy=MarketPolicy.load("ashare"),
    )
    snapshot = ledger.snapshot()
    assert snapshot.cash_balance_cny < 50_000.0
    assert snapshot.unreconciled_fill_commit_ids
    events = [
        json.loads(line)
        for line in (Path(ledger.root) / ledger.events_filename)
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    assert any(row.get("event_type") == "fill_commit" for row in events)
    latest = json.loads(result.latest_path.read_text(encoding="utf-8"))
    assert latest["fill_count"] == 1
    assert latest["canonical_account_connected"] is False
    assert latest["real_trading_enabled"] is False
    assert result.disposition_for("300750.SZ").rejection_reason == (
        "chinext_individual_permission_unavailable"
    )
    assert result.disposition_for("688981.SH").rejection_reason == (
        "star_individual_permission_unavailable"
    )


def test_closed_session_does_not_invent_a_fill_from_daily_close(
    tmp_path: Path,
) -> None:
    _prepare_ledger(tmp_path)
    registry, _manifest = _manual_registry(tmp_path)
    decision = datetime(2026, 9, 1, 14, 59, tzinfo=SHANGHAI)
    symbol = CASH_SESSION_SYMBOL
    windows = {
        symbol: _snapshot_ready_window(
            symbol,
            last_cny=12.8,
            high_cny=12.8,
            low_cny=12.8,
            clock_at="2026-09-01 14:55:00",
        )
    }
    result = run_capital_backed_paper_session(
        _config(
            tmp_path,
            trade_date=CASH_SESSION_TRADE_DATE,
            decision_as_of=decision,
        ),
        windows=windows,
        champion_registry=registry,
    )
    row = result.disposition_for(symbol)
    assert row.disposition is ExposureDisposition.PAPER_NOT_FILLED
    assert row.nonfill_reason == "paper_continuous_session_unavailable"
    assert row.nonfill_reason not in IN_SESSION_BANNED_NONFILL
    assert result.fill_count == 0
    assert close_or_touch_is_not_a_fill() == 0
    assert result.canonical_account_connected is False
    assert result.disposition_for("300750.SZ").rejection_reason == (
        "chinext_individual_permission_unavailable"
    )
    assert result.disposition_for("688981.SH").rejection_reason == (
        "star_individual_permission_unavailable"
    )


def test_morning_bar_in_afternoon_session_stays_stale_not_a_fill(
    tmp_path: Path,
) -> None:
    _prepare_ledger(tmp_path)
    registry, _manifest = _manual_registry(tmp_path)
    decision = datetime(2026, 9, 1, 13, 2, tzinfo=SHANGHAI)
    symbol = CASH_SESSION_SYMBOL
    windows = {
        symbol: _snapshot_ready_window(
            symbol,
            last_cny=12.8,
            clock_at="2026-09-01 11:30:00",
        )
    }
    result = run_capital_backed_paper_session(
        _config(
            tmp_path,
            trade_date=CASH_SESSION_TRADE_DATE,
            decision_as_of=decision,
        ),
        windows=windows,
        champion_registry=registry,
    )
    row = result.disposition_for(symbol)
    assert row.disposition is ExposureDisposition.PAPER_NOT_FILLED
    assert row.nonfill_reason == "paper_market_snapshot_stale"
    assert result.fill_count == 0
    assert close_or_touch_is_not_a_fill() == 0

