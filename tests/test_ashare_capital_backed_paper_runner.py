from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import pytest

from Ashare.capital_backed_paper_runner import (
    CALENDAR_DATASET_ID,
    CANONICAL_DATA_ROOT,
    INVENTED_FILL_SOURCES,
    QUOTE_DATASET_ID,
    CapitalBackedPaperConfig,
    CapitalBackedPaperError,
    FillAttemptRequest,
    FillAttemptResult,
    bind_cash_session_windows,
    close_or_touch_is_not_a_fill,
    count_coverage_is_not_a_fill,
    make_missing_window,
    make_observation_window,
    query_windows_from_tradingdatas,
    reject_invented_fill_source,
    run_capital_backed_paper_session,
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
    drift_ok=False,
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


CASH_SESSION_TRADE_DATE = "2026-09-01"
LAST_COMPLETE_DAILY = "20260831"
CASH_SESSION_SYMBOL = "000063.SZ"


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
    ) -> None:
        self.calendar_rows = calendar_rows
        self.daily_by_date = daily_by_date
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
            return HTTPResponse(200, _td_catalog_payload())
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
            trade_date = str((filters.get("trade_date") or {}).get("eq") or "")
            return HTTPResponse(
                200,
                _td_query_payload(
                    QUOTE_DATASET_ID,
                    self.daily_by_date.get(trade_date, ()),
                ),
            )
        raise AssertionError(f"unexpected dataset {dataset_id}")


def _td_catalog_payload() -> dict[str, object]:
    return {
        "api_version": "v1",
        "catalog_version": "td-catalog-live",
        "request_id": "catalog-cash-session",
        "data": [
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
        ],
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


def _td_client(transport: _RecordingTDTransport) -> SharedSignalsV1Client:
    return SharedSignalsV1Client(
        SharedSignalsV1Config(
            base_url="http://127.0.0.1:18082",
            expected_catalog_version="evidence-only",
            dataset_ids=frozenset({CALENDAR_DATASET_ID, QUOTE_DATASET_ID}),
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
    assert daily_filters == [{"trade_date": {"eq": LAST_COMPLETE_DAILY}}]
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
