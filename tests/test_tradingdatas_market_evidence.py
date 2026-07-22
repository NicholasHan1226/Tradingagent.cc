from __future__ import annotations

import hashlib
import inspect
import json
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from shared.data.evidence_gate import EvidenceAction, EvidenceDecision
from shared.data.research_snapshot import (
    DatasetRequirement,
    ResearchDataProfile,
    build_research_data_snapshot,
)
from shared.data.research_snapshot_store import FileResearchSnapshotStore
from shared.data.sharedsignals_v1 import QueryRequest, parse_query_envelope
from shared.data.tradingdatas_pagination import bind_complete_page
from shared.runtime.market_evidence_authority import (
    AShareMarkEvidence,
    MarketEvidenceContext,
)
from shared.runtime.ashare_observation_ledger import (
    OBSERVED_REASON_CODE,
    AshareObservationMembershipArtifact,
    AshareObservationMembershipRecord,
    FileAshareObservationMembershipLedger,
    build_ashare_observation_membership_artifact,
)
from shared.runtime.tradingdatas_market_evidence import (
    TradingDatasDailyMarketEvidenceAdapter,
    TradingDatasMarketEvidenceBlocked,
)


CATALOG_VERSION = "v1-fixture-catalog"
PROFILE_ID = "ashare-observation-fixture"
DAILY_DATASET_ID = "cn.equity.daily"
OBSERVATION_AS_OF = datetime(2026, 7, 22, 14, 30, tzinfo=timezone.utc)
MANIFEST_AS_OF = "2026-07-22T22:30:00+08:00"
CALENDAR_RECEIPT_SHA256 = "c" * 64


def _sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _context(*, trade_date: date = date(2026, 7, 23)) -> MarketEvidenceContext:
    return MarketEvidenceContext(
        trade_date=trade_date,
        decision_as_of=datetime(2026, 7, 23, 1, 0, tzinfo=timezone.utc),
        capital_authority_id="ashare-capital-v1",
        authority_generation=2,
        execution_lineage_id="ashare-sim-20260723-v2",
        account_type="simulated",
        real_trading_enabled=False,
    )


def _frozen_observation(
    tmp_path: Path,
    *,
    close: float = 12.34,
    volume: float = 1_234_500.0,
    trade_date: str = "20260722",
    daily_rows: tuple[dict[str, object], ...] | None = None,
    membership_records: tuple[AshareObservationMembershipRecord, ...] | None = None,
) -> tuple[
    TradingDatasDailyMarketEvidenceAdapter,
    object,
    AshareObservationMembershipArtifact,
]:
    if daily_rows is None:
        daily_rows = (
            {
                "ts_code": "600000.SH",
                "trade_date": trade_date,
                "open": 12.10,
                "high": 12.50,
                "low": 12.00,
                "close": close,
                "vol": volume,
                "amount": 15_000_000.0,
            },
        )
    if membership_records is None:
        membership_records = (
            AshareObservationMembershipRecord(
                symbol="600000.SH",
                disposition="observed",
                reason_code=OBSERVED_REASON_CODE,
            ),
        )
    profile = ResearchDataProfile(
        profile_id=PROFILE_ID,
        catalog_version=CATALOG_VERSION,
        requirements=(
            DatasetRequirement(
                DAILY_DATASET_ID,
                role="required_execution",
                identity_fields=("ts_code", "trade_date"),
                row_event_time_field="trade_date",
                row_event_time_format="yyyymmdd",
                row_event_timezone="Asia/Shanghai",
                row_event_time_semantic="session",
                max_pages=2,
                max_rows=10,
            ),
        ),
    )
    envelope = parse_query_envelope(
        {
            "api_version": "v1",
            "catalog_version": CATALOG_VERSION,
            "request_id": "query-daily-page-1",
            "dataset_id": DAILY_DATASET_ID,
            "data": list(daily_rows),
            "next_cursor": None,
            "metadata": {
                "state": "ready",
                "degraded": False,
                "freshness": {"state": "fresh", "stale": False},
                "quality": {"state": "valid", "valid": True},
                "lineage": {
                    "state": "complete",
                    "complete": True,
                    "provider_neutral": True,
                    "provider": "tushare",
                    "transport_service": "quicksync",
                },
                "receipt_id": "td-receipt-cn-equity-daily-20260722",
                "data_through": "2026-07-22T07:00:00+00:00",
                "observed_at": "2026-07-22T14:00:00+00:00",
                "reasons": [],
            },
        }
    )
    page_run = bind_complete_page(
        request=QueryRequest(
            dataset_id=DAILY_DATASET_ID,
            schema_major=2,
            fields=("ts_code", "trade_date", "close", "vol"),
            filters={"trade_date": {"eq": trade_date}},
            as_of=OBSERVATION_AS_OF.isoformat(),
            limit=10,
        ),
        envelope=envelope,
        identity_fields=("ts_code", "trade_date"),
    )
    snapshot = build_research_data_snapshot(
        profile=profile,
        page_runs=(page_run,),
        decisions=(
            EvidenceDecision(
                dataset_id=DAILY_DATASET_ID,
                receipt_id="td-receipt-cn-equity-daily-20260722",
                effective_state="ready",
                action=EvidenceAction.ACCEPT,
                eligible=True,
                weight=1.0,
                reasons=(),
            ),
        ),
        decision_as_of=OBSERVATION_AS_OF,
    )
    snapshot_root = tmp_path / "research-snapshots"
    FileResearchSnapshotStore(snapshot_root).compare_and_swap(
        snapshot=snapshot,
        expected_snapshot_sha256=None,
    )
    daily = snapshot.datasets[0]
    probe: dict[str, object] = {
        "schema_id": "tradingagent.tradingdatas.integration-readiness.v2",
        "probe_version": 2,
        "authority": "non_authority",
        "production_verified": False,
        "real_trading_enabled": False,
        "profile_id": PROFILE_ID,
        "as_of": MANIFEST_AS_OF,
        "catalog_version": CATALOG_VERSION,
        "manifest_sha256": "a" * 64,
        "status": "pass",
        "blocking": False,
        "reason_codes": [],
        "same_as_of_match": True,
        "semantic_snapshot_sha256": "d" * 64,
        "snapshot_runs": [
            {
                "snapshot_sha256": snapshot.snapshot_sha256,
                "execution_eligible": True,
                "historical_pit_eligible": False,
                "profile_contract_sha256": snapshot.profile_contract_sha256,
                "blocking_reasons": [],
            },
            {
                "snapshot_sha256": snapshot.snapshot_sha256,
                "execution_eligible": True,
                "historical_pit_eligible": False,
                "profile_contract_sha256": snapshot.profile_contract_sha256,
                "blocking_reasons": [],
            },
        ],
        "datasets": [
            {
                "probe_role": "daily_bars",
                "dataset_id": daily.dataset_id,
                "schema_major": 2,
                "requirement_role": daily.role,
                "observation_mode": "current_observation",
                "historical_pit_eligible": False,
                "source_proof_complete": True,
                "eligible": True,
                "pagination_complete": True,
                "same_as_of_match": True,
                "identity_sha256": daily.identity_sha256,
                "pagination_semantic_sha256": daily.pagination_semantic_sha256,
                "row_count": daily.row_count,
                "page_count": daily.page_count,
                "receipt_id": daily.receipt_id,
                "lineage_sha256": daily.lineage_sha256,
                "source_proof_sha256": daily.source_proof_sha256,
            }
        ],
    }
    probe["receipt_sha256"] = _sha256(probe)
    observed_symbols = sorted(
        record.symbol
        for record in membership_records
        if record.disposition == "observed"
    )
    excluded_reason_counts = dict(
        sorted(
            Counter(
                record.reason_code
                for record in membership_records
                if record.disposition == "excluded"
            ).items()
        )
    )
    receipt: dict[str, object] = {
        "schema_id": "tradingagent.ashare.observation-receipt.v1",
        "profile_id": snapshot.profile_id,
        "catalog_version": snapshot.catalog_version,
        "decision_as_of": snapshot.decision_as_of,
        "manifest_sha256": "a" * 64,
        "snapshot_sha256": snapshot.snapshot_sha256,
        "probe_receipt_sha256": probe["receipt_sha256"],
        "tradable_universe_count": len(observed_symbols),
        "tradable_universe_sha256": _sha256(observed_symbols),
        "excluded_reason_counts": excluded_reason_counts,
        "context_probe_roles": [],
        "mode": "observation_only",
        "marketgraph_mode": "mg_off",
        "real_trading_enabled": False,
        "historical_pit_eligible": False,
        "execution_authority": False,
    }
    receipt["receipt_sha256"] = _sha256(receipt)
    membership_artifact = build_ashare_observation_membership_artifact(
        observation_session=trade_date,
        research_snapshot=snapshot,
        observation_receipt=receipt,
        records=membership_records,
    )
    FileAshareObservationMembershipLedger(
        snapshot_root / "observation-membership"
    ).compare_and_swap(
        observation_session=trade_date,
        research_snapshot=snapshot,
        observation_receipt=receipt,
        records=membership_records,
        expected_content_sha256=None,
    )
    complete: dict[str, object] = {
        "schema_id": "tradingagent.ashare.observation-transaction-complete.v1",
        "profile_id": snapshot.profile_id,
        "catalog_version": snapshot.catalog_version,
        "decision_as_of": snapshot.decision_as_of,
        "observation_session": trade_date,
        "manifest_sha256": "a" * 64,
        "snapshot_sha256": snapshot.snapshot_sha256,
        "probe_receipt_sha256": probe["receipt_sha256"],
        "observation_receipt_sha256": receipt["receipt_sha256"],
        "observation_membership_sha256": membership_artifact.content_sha256,
        "required_artifacts": [
            "integration_probe_receipt",
            "research_snapshot",
            "observation_receipt",
            "observation_membership",
        ],
        "mode": "observation_only",
        "marketgraph_mode": "mg_off",
        "historical_pit_eligible": False,
        "real_trading_enabled": False,
        "execution_authority": False,
    }
    complete["content_sha256"] = _sha256(complete)
    transaction_identity = _sha256(
        {
            "profile_id": snapshot.profile_id,
            "catalog_version": snapshot.catalog_version,
            "as_of": MANIFEST_AS_OF,
            "manifest_sha256": "a" * 64,
        }
    )
    for name, payload in (
        (f"integration-{transaction_identity}.json", probe),
        (f"observation-{transaction_identity}.json", receipt),
        (f"observation-complete-{transaction_identity}.json", complete),
    ):
        path = snapshot_root / name
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        path.chmod(0o600)
    lock = snapshot_root / f"observation-session-lock-{trade_date}.lock"
    lock.touch(mode=0o600)
    lock.chmod(0o600)
    return (
        TradingDatasDailyMarketEvidenceAdapter(
            state_root=snapshot_root,
            expected_profile_id=PROFILE_ID,
            expected_catalog_version=CATALOG_VERSION,
            expected_observation_as_of=OBSERVATION_AS_OF,
            manifest_as_of=MANIFEST_AS_OF,
            manifest_sha256="a" * 64,
            schema_major=2,
            daily_dataset_id=DAILY_DATASET_ID,
        ),
        snapshot,
        membership_artifact,
    )


def test_adapter_requires_durable_transaction_complete(tmp_path: Path) -> None:
    adapter, _, _ = _frozen_observation(tmp_path)
    next(adapter.snapshot_root.glob("observation-complete-*.json")).unlink()

    with pytest.raises(
        TradingDatasMarketEvidenceBlocked,
        match="observation_transaction_complete_missing",
    ):
        TradingDatasDailyMarketEvidenceAdapter(
            state_root=adapter.snapshot_root,
            expected_profile_id=PROFILE_ID,
            expected_catalog_version=CATALOG_VERSION,
            expected_observation_as_of=OBSERVATION_AS_OF,
            manifest_as_of=MANIFEST_AS_OF,
            manifest_sha256="a" * 64,
            schema_major=2,
            daily_dataset_id=DAILY_DATASET_ID,
        )


def test_previous_session_daily_row_issues_existing_mark_type(
    tmp_path: Path,
) -> None:
    adapter, snapshot, _ = _frozen_observation(tmp_path)

    mark = adapter.previous_session_mark(
        symbol="600000.SH",
        valuation_session=date(2026, 7, 22),
        context=_context(),
        session_calendar_receipt_sha256=CALENDAR_RECEIPT_SHA256,
    )

    daily = snapshot.datasets[0]  # type: ignore[attr-defined]
    assert type(mark) is AShareMarkEvidence
    assert mark.symbol == "600000.SH"
    assert mark.price_cny == 12.34
    assert mark.market_session == "close"
    assert mark.context == _context()
    assert mark.session_calendar_receipt_sha256 == CALENDAR_RECEIPT_SHA256
    assert mark.source.dataset_id == DAILY_DATASET_ID
    assert mark.source.catalog_version == CATALOG_VERSION
    assert mark.source.source_receipt_id == daily.receipt_id
    assert mark.source.source_receipt_sha256 == daily.response_sha256
    assert mark.source.source_lineage_sha256 == _sha256(
        {
            "schema_id": "tradingagent.ashare.committed-observation-market-source.v1",
            "daily_lineage_sha256": daily.lineage_sha256,
            "observation_membership_sha256": (
                adapter.runtime_authorities.observation_membership_sha256
            ),
            "observation_transaction_complete_sha256": (
                adapter.runtime_authorities.observation_transaction_complete_sha256
            ),
        }
    )
    assert mark.source.data_through.isoformat() == "2026-07-22T07:00:00+00:00"
    assert mark.source.observed_at.isoformat() == "2026-07-22T14:00:00+00:00"
    assert mark.source.available_at == OBSERVATION_AS_OF
    assert not hasattr(mark, "bid_price_cny")
    assert not hasattr(mark, "ask_price_cny")


@pytest.mark.parametrize("symbol", ("300750.SZ", "688981.SH", "832000.BJ"))
def test_previous_session_mark_rejects_non_mainboard_individual_symbols(
    tmp_path: Path,
    symbol: str,
) -> None:
    adapter, _, _ = _frozen_observation(tmp_path)

    with pytest.raises(
        TradingDatasMarketEvidenceBlocked,
        match="^previous_session_symbol_out_of_scope$",
    ) as caught:
        adapter.previous_session_mark(
            symbol=symbol,
            valuation_session=date(2026, 7, 22),
            context=_context(),
            session_calendar_receipt_sha256=CALENDAR_RECEIPT_SHA256,
        )

    assert caught.value.reason_code == "previous_session_symbol_out_of_scope"


def test_previous_session_mark_requires_observed_membership_for_mainboard_symbol(
    tmp_path: Path,
) -> None:
    observed_row = {
        "ts_code": "600000.SH",
        "trade_date": "20260722",
        "open": 12.10,
        "high": 12.50,
        "low": 12.00,
        "close": 12.34,
        "vol": 1_234_500.0,
        "amount": 15_000_000.0,
    }
    excluded_row = {
        "ts_code": "600001.SH",
        "trade_date": "20260722",
        "open": 8.10,
        "high": 8.50,
        "low": 8.00,
        "close": 8.34,
        "vol": 934_500.0,
        "amount": 7_500_000.0,
    }
    adapter, _, _ = _frozen_observation(
        tmp_path,
        daily_rows=(observed_row, excluded_row),
        membership_records=(
            AshareObservationMembershipRecord(
                symbol="600000.SH",
                disposition="observed",
                reason_code=OBSERVED_REASON_CODE,
            ),
            AshareObservationMembershipRecord(
                symbol="600001.SH",
                disposition="excluded",
                reason_code="st_or_risk_warning",
            ),
        ),
    )

    with pytest.raises(
        TradingDatasMarketEvidenceBlocked,
        match="^previous_session_symbol_not_observed$",
    ) as caught:
        adapter.previous_session_mark(
            symbol="600001.SH",
            valuation_session=date(2026, 7, 22),
            context=_context(),
            session_calendar_receipt_sha256=CALENDAR_RECEIPT_SHA256,
        )

    assert caught.value.reason_code == "previous_session_symbol_not_observed"


def test_same_day_observation_cannot_masquerade_as_previous_session_mark(
    tmp_path: Path,
) -> None:
    adapter, _, _ = _frozen_observation(tmp_path)

    with pytest.raises(
        TradingDatasMarketEvidenceBlocked,
        match="previous_valuation_session_must_precede_trade_date",
    ) as caught:
        adapter.previous_session_mark(
            symbol="600000.SH",
            valuation_session=date(2026, 7, 22),
            context=_context(trade_date=date(2026, 7, 22)),
            session_calendar_receipt_sha256=CALENDAR_RECEIPT_SHA256,
        )

    assert caught.value.reason_code == (
        "previous_valuation_session_must_precede_trade_date"
    )


def test_daily_row_must_match_explicit_previous_valuation_session(
    tmp_path: Path,
) -> None:
    adapter, _, _ = _frozen_observation(tmp_path)

    with pytest.raises(
        TradingDatasMarketEvidenceBlocked,
        match="previous_session_daily_mark_unavailable",
    ):
        adapter.previous_session_mark(
            symbol="600000.SH",
            valuation_session=date(2026, 7, 21),
            context=_context(),
            session_calendar_receipt_sha256=CALENDAR_RECEIPT_SHA256,
        )


@pytest.mark.parametrize(
    ("close", "volume"),
    ((0.0, 100.0), (12.34, 0.0)),
)
def test_daily_mark_requires_positive_close_and_volume(
    tmp_path: Path,
    close: float,
    volume: float,
) -> None:
    adapter, _, _ = _frozen_observation(tmp_path, close=close, volume=volume)

    with pytest.raises(
        TradingDatasMarketEvidenceBlocked,
        match="previous_session_daily_mark_invalid",
    ):
        adapter.previous_session_mark(
            symbol="600000.SH",
            valuation_session=date(2026, 7, 22),
            context=_context(),
            session_calendar_receipt_sha256=CALENDAR_RECEIPT_SHA256,
        )


def test_observation_receipt_must_bind_exact_frozen_snapshot(tmp_path: Path) -> None:
    adapter, _, _ = _frozen_observation(tmp_path)
    receipt_path = adapter.observation_receipt_path
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["snapshot_sha256"] = "f" * 64
    unsigned = dict(receipt)
    unsigned.pop("receipt_sha256")
    receipt["receipt_sha256"] = _sha256(unsigned)
    receipt_path.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    receipt_path.chmod(0o600)

    with pytest.raises(
        TradingDatasMarketEvidenceBlocked,
        match="observation_snapshot_binding_invalid",
    ):
        TradingDatasDailyMarketEvidenceAdapter(
            state_root=adapter.snapshot_root,
            expected_profile_id=PROFILE_ID,
            expected_catalog_version=CATALOG_VERSION,
            expected_observation_as_of=OBSERVATION_AS_OF,
            manifest_as_of=MANIFEST_AS_OF,
            manifest_sha256="a" * 64,
            schema_major=2,
            daily_dataset_id=DAILY_DATASET_ID,
        )


def test_minute_execution_quote_and_bar_evidence_fail_closed_without_transport(
    tmp_path: Path,
) -> None:
    adapter, _, _ = _frozen_observation(tmp_path)

    for request in (
        lambda: adapter.execution_quote_evidence(
            symbol="600000.SH",
            order_id="paper-order-1",
        ),
        lambda: adapter.execution_bar_evidence(
            symbol="600000.SH",
            valuation_session=date(2026, 7, 22),
        ),
    ):
        with pytest.raises(
            TradingDatasMarketEvidenceBlocked,
            match="^minute_execution_evidence_unavailable$",
        ) as caught:
            request()
        assert caught.value.reason_code == "minute_execution_evidence_unavailable"

    module_source = inspect.getsource(
        __import__(
            "shared.runtime.tradingdatas_market_evidence",
            fromlist=["TradingDatasDailyMarketEvidenceAdapter"],
        )
    )
    assert "SharedSignalsV1Client" not in module_source
    assert "tradingdatas_transport" not in module_source
    assert "bid_price_cny=" not in module_source
    assert "ask_price_cny=" not in module_source
    assert "fill" not in module_source.lower()
