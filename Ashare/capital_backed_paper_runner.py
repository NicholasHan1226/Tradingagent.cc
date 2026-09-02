"""Sibling capital-backed paper session for one natural A-share day.

This runner does **not** wrap ``compose_capital_backed_paper_runtime``.  That
composer stays network-closed and accepts only ``FrozenFixtureHTTPTransport``.
This sibling reuses the existing capital ports and Champion/clock contracts
while consuming already-working TradingDatas ``GET /v1/catalog`` +
``POST /v1/query``.  It never invents a fill.

Every session candidate is persisted as ``paper_filled``, ``paper_not_filled``,
``rejected``, or ``observation_only`` with an explicit reason code.
Coverage/observation counts are not fills.  ``REAL_TRADING_ENABLED`` must stay
false.  Production data root is ``/var/lib/tradingagent/ashare-canonical``;
tests must pass an isolated ledger and never set ``allow_canonical_root``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import dataclass, fields
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from shared.capital.market_ledger import MarketCapitalLedger, MarketCapitalSnapshot
from shared.capital.market_policy import MarketPolicy
from shared.execution.execution_lineage import ASHARE_CAPITAL_AUTHORITY_ID
from shared.execution.execution_reality import ashare_execution_reality
from shared.models.champion_registry import (
    ChampionRegistryError,
    ChampionSelectionReceipt,
    ChampionSelectionRegistry,
)
from shared.models.lifecycle import (
    TradingSessionCalendarAuthority,
    TradingSessionCalendarAuthorityVerification,
)
from shared.review.decision_ledger import (
    DecisionExposureRecord,
    ExposureDisposition,
    SampleJournalDecisionLedger,
)
from shared.review.sample_journal import SampleJournal
from shared.runtime.capital_stages import (
    CapitalBackedPreopenStagePort,
    CapitalBackedRiskStagePort,
    CapitalBackedSimulationExecutionStagePort,
    PaperCapitalAccount,
    PaperCapitalStageError,
)
from shared.runtime.day_loop import StageRequest, StageResult
from shared.runtime.execution_receipt_contract import ashare_continuous_session
from shared.runtime.market_evidence_authority import (
    AShareExecutionQuoteEvidence,
    MarketEvidenceContext,
    MarketSourceBinding,
    freeze_non_production_market_evidence,
)
from shared.runtime.run_bundle import ComponentIdentity, RunStage
from shared.runtime.trusted_clock import NonProductionFixtureExecutionClock
from shared.universe.policy import CanonicalMainboardScopePolicy

from .capital_backed_paper_universe import (
    FROZEN_UNIVERSE_SHA256,
    SessionSymbolClassification,
    classify_session_universe,
    looks_like_security_symbol,
)
from .minute_auto_runner import session_bar_ends
from .minute_paper import MinuteFixturePaperBook


SHANGHAI = ZoneInfo("Asia/Shanghai")
CANONICAL_DATA_ROOT = Path("/var/lib/tradingagent/ashare-canonical")
CANONICAL_LEDGER_ROOT = CANONICAL_DATA_ROOT / "shared" / "logs" / "capital" / "ashare"
RUNNER_CONTRACT_ID = "tradingagent.ashare.capital_backed_paper_session.v1"
QUOTE_DATASET_ID = "cn.equity.daily"
QUOTE_CLOCK_DATASET_ID = "cn.dataset.rt_min"
CALENDAR_DATASET_ID = "cn.market.trade_calendar"
FORMAL_BASE_URL = "http://127.0.0.1:18082"
# Bounded ``ts_code in`` shards for last-complete daily.  Aligns with the
# documented A-share daily query cap; unfiltered partition pulls 413.
CASH_SESSION_DAILY_TS_CODE_CHUNK = 10
CASH_SESSION_QUOTE_CLOCK_CHUNK = 10
QUOTE_CLOCK_FREQ = "5MIN"
QUOTE_CLOCK_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
_FIVE_MINUTE_FREQS = frozenset({"5", "5m", "5min"})
BUY_LOT = 100
SINGLE_NAME_CAP_CNY = 7_500.0
GROSS_CAP_CNY = 45_000.0
OPENING_CASH_CNY = 50_000.0
CHAMPION_UNAVAILABLE_MANIFEST_SHA256 = hashlib.sha256(
    b"tradingagent.ashare.capital_backed_paper.champion_unavailable"
).hexdigest()

INVENTED_FILL_SOURCES = frozenset(
    {
        "frozen_fixture_stage_port",
        "compose_paper_runtime",
        "minute_fixture_paper_book",
        "coverage_accepted_count",
        "close_touch",
        "kpi_forced_order",
        "industry_shadow_name",
        "chinext_or_star",
    }
)

WindowSource = Callable[[tuple[str, ...]], Mapping[str, "SymbolWindow"]]
FillAttempt = Callable[["FillAttemptRequest"], "FillAttemptResult"]


class CapitalBackedPaperError(ValueError):
    """Fail-closed capital-backed paper session error."""


@dataclass(frozen=True)
class SymbolWindow:
    """One symbol's dataset/catalog/session window.  Missing fields block fills."""

    symbol: str
    dataset_id: str
    catalog_version: str
    quote_fresh: bool
    prior_close_cny: float | None
    session_calendar_ok: bool
    quote_clocks_ok: bool
    reason_code: str = "window_ready"
    quote_trade_date: str = ""
    quote_clock_at: str = ""
    quote_clock_dataset_id: str = ""
    snapshot_last_cny: float | None = None
    snapshot_high_cny: float | None = None
    snapshot_low_cny: float | None = None
    snapshot_open_cny: float | None = None
    snapshot_volume: float | None = None
    snapshot_receipt_id: str = ""
    snapshot_source_sha256: str = ""
    snapshot_lineage_sha256: str = ""
    snapshot_data_through: str = ""
    snapshot_observed_at: str = ""
    snapshot_available_at: str = ""
    snapshot_catalog_version: str = ""

    @property
    def observation_ready(self) -> bool:
        return bool(
            self.dataset_id
            and self.catalog_version
            and self.session_calendar_ok
            and self.prior_close_cny is not None
            and self.prior_close_cny > 0
        )

    @property
    def fill_quote_ready(self) -> bool:
        return self.observation_ready and self.quote_fresh and self.quote_clocks_ok

    @property
    def fill_snapshot_ready(self) -> bool:
        """True only when an in-session rt_min bar snapshot is bound.

        Daily close/touch never sets these fields.  Last/close is a bar
        mid, not a bid/ask.
        """

        return bool(
            self.fill_quote_ready
            and self.snapshot_last_cny is not None
            and self.snapshot_last_cny > 0
            and self.snapshot_volume is not None
            and self.snapshot_volume > 0
            and self.snapshot_receipt_id
            and self.snapshot_source_sha256
            and self.snapshot_lineage_sha256
            and self.snapshot_data_through
            and self.snapshot_observed_at
            and self.snapshot_available_at
            and self.quote_clock_at
            and self.quote_clock_dataset_id == QUOTE_CLOCK_DATASET_ID
        )


@dataclass(frozen=True)
class FillAttemptRequest:
    """Evidence required before the runner will call a capital fill port."""

    symbol: str
    quantity: int
    prior_close_cny: float
    champion: ChampionSelectionReceipt
    window: SymbolWindow
    snapshot_before: MarketCapitalSnapshot
    trade_date: str = ""
    decision_as_of: datetime | None = None
    run_id: str = ""
    artifact_root: Path | None = None


@dataclass(frozen=True)
class FillAttemptResult:
    """Outcome of one optional capital commit.  Fingerprints alone are not fills."""

    committed: bool
    fill_id: str | None
    filled_quantity: int
    filled_notional_cny: float
    actual_cost_cny: float
    ledger_event_id: str | None
    reason_code: str


@dataclass(frozen=True)
class CapitalBackedPaperConfig:
    """Explicit session identity.  No production path is implied by defaults."""

    trade_date: str
    decision_as_of: datetime
    ledger_root: Path
    journal_path: Path
    latest_path: Path
    real_trading_enabled: bool = False
    live_execution_enabled: bool = False
    allow_canonical_root: bool = False
    extra_symbols: tuple[str, ...] = ()
    include_exclusion_probes: bool = True

    def __post_init__(self) -> None:
        if type(self.real_trading_enabled) is not bool or self.real_trading_enabled:
            raise CapitalBackedPaperError("real_trading_enabled_must_be_native_false")
        if type(self.live_execution_enabled) is not bool or self.live_execution_enabled:
            raise CapitalBackedPaperError("live_execution_enabled_must_be_native_false")
        if os.environ.get("REAL_TRADING_ENABLED", "false").strip().lower() != "false":
            raise CapitalBackedPaperError("real_trading_must_remain_disabled")
        if self.decision_as_of.tzinfo is None or self.decision_as_of.utcoffset() is None:
            raise CapitalBackedPaperError("decision_as_of_timezone_required")
        local = self.decision_as_of.astimezone(SHANGHAI)
        if local.date().isoformat() != self.trade_date:
            raise CapitalBackedPaperError("decision_as_of_trade_date_mismatch")
        for path in (self.ledger_root, self.journal_path, self.latest_path):
            _assert_writable_path(Path(path), allow_canonical=self.allow_canonical_root)


@dataclass(frozen=True)
class CandidateDisposition:
    """One persisted session candidate."""

    symbol: str
    disposition: ExposureDisposition
    action: str
    reason_code: str
    rejection_reason: str | None
    nonfill_reason: str | None
    requested_notional_cny: float
    filled_quantity: int
    filled_notional_cny: float
    actual_cost_cny: float
    simulated_fill_id: str | None
    order_identity_allowed: bool
    sleeve: str

    def to_latest_row(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "disposition": self.disposition.value,
            "action": self.action,
            "reason_code": self.reason_code,
            "rejection_reason": self.rejection_reason,
            "nonfill_reason": self.nonfill_reason,
            "requested_notional_cny": self.requested_notional_cny,
            "filled_quantity": self.filled_quantity,
            "filled_notional_cny": self.filled_notional_cny,
            "actual_cost_cny": self.actual_cost_cny,
            "simulated_fill_id": self.simulated_fill_id,
            "order_identity_allowed": self.order_identity_allowed,
            "sleeve": self.sleeve,
        }


@dataclass(frozen=True)
class CapitalBackedPaperSessionResult:
    """Readback of one isolated paper session."""

    run_id: str
    input_bundle_sha256: str
    capital_authority_id: str
    authority_generation: int
    execution_lineage_id: str
    opening_cash_cny: float
    fill_count: int
    ledger_fill_commit_count: int
    canonical_account_connected: bool
    champion_manifest_sha256: str
    universe_sha256: str
    dispositions: tuple[CandidateDisposition, ...]
    latest_path: Path
    journal_path: Path

    def disposition_for(self, symbol: str) -> CandidateDisposition:
        matches = [item for item in self.dispositions if item.symbol == symbol]
        if len(matches) != 1:
            raise CapitalBackedPaperError(f"disposition_missing:{symbol}")
        return matches[0]


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _looks_like_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _assert_writable_path(path: Path, *, allow_canonical: bool) -> None:
    if not path.is_absolute() or ".." in path.parts:
        raise CapitalBackedPaperError("session_path_must_be_absolute")
    if _is_under(path, CANONICAL_DATA_ROOT) and not allow_canonical:
        raise CapitalBackedPaperError("canonical_ledger_mutation_forbidden")


def reject_invented_fill_source(source: object) -> None:
    """Fail closed for every invented-fill checklist item."""

    if source in INVENTED_FILL_SOURCES or source == "compose_paper_runtime":
        raise CapitalBackedPaperError(f"invented_fill_forbidden:{source}")
    if getattr(source, "__name__", "") == "compose_paper_runtime":
        raise CapitalBackedPaperError("wrong_capital_authority_compose_paper_runtime")
    if isinstance(source, MinuteFixturePaperBook) or source is MinuteFixturePaperBook:
        raise CapitalBackedPaperError("invented_fill_forbidden:minute_fixture_paper_book")


def count_coverage_is_not_a_fill(accepted_count: object) -> int:
    """Coverage accepted_count is an observation statistic, never a fill count."""

    if isinstance(accepted_count, bool) or not isinstance(accepted_count, int):
        raise CapitalBackedPaperError("coverage_accepted_count_invalid")
    if accepted_count < 0:
        raise CapitalBackedPaperError("coverage_accepted_count_invalid")
    return 0


def close_or_touch_is_not_a_fill() -> int:
    """A daily close or last touch is valuation evidence, not a fill."""

    return 0


def _ledger_has_new_fill(
    before: MarketCapitalSnapshot,
    after: MarketCapitalSnapshot,
) -> bool:
    return (
        after.authority_id == before.authority_id
        and after.event_id != before.event_id
        and after.cash_balance_cny < before.cash_balance_cny
        and (
            after.positions_market_value_cny > before.positions_market_value_cny
            or len(after.unreconciled_fill_commit_ids)
            > len(before.unreconciled_fill_commit_ids)
        )
    )


def _bind_ledger(config: CapitalBackedPaperConfig) -> tuple[MarketCapitalLedger, MarketCapitalSnapshot]:
    policy = MarketPolicy.load("ashare")
    ledger = MarketCapitalLedger(config.ledger_root, policy=policy)
    try:
        snapshot = ledger.snapshot()
    except Exception as exc:
        raise CapitalBackedPaperError("canonical_capital_snapshot_unavailable") from exc
    if (
        snapshot.authority_id != ASHARE_CAPITAL_AUTHORITY_ID
        or policy.capital_authority_id != ASHARE_CAPITAL_AUTHORITY_ID
        or snapshot.market != "ashare"
        or policy.market != "ashare"
        or snapshot.real_trading_enabled is not False
        or snapshot.initial_equity_cny != OPENING_CASH_CNY
        or snapshot.authority_generation <= 0
    ):
        raise CapitalBackedPaperError("canonical_capital_identity_mismatch")
    return ledger, snapshot


def _live_risk_flags_present(
    *,
    champion: ChampionSelectionReceipt | None = None,
    snapshot: MarketCapitalSnapshot | None = None,
) -> bool:
    """True when any live / real-trading / risk-expansion flag is not native false."""

    env = os.environ.get("REAL_TRADING_ENABLED", "false").strip().lower()
    if env != "false":
        return True
    flags: list[object] = []
    if champion is not None:
        flags.extend(
            (
                champion.real_trading_enabled,
                champion.live_transition_authorized,
                champion.automatic_risk_expansion_enabled,
            )
        )
    if snapshot is not None:
        flags.append(snapshot.real_trading_enabled)
    return any(flag is not False for flag in flags)


def _simulation_only_champion(champion: ChampionSelectionReceipt | None) -> bool:
    return (
        champion is not None
        and champion.simulation_only is True
        and champion.capital_layer == "simulated"
        and champion.account_type == "simulated"
        and champion.real_trading_enabled is False
        and champion.live_transition_authorized is False
        and champion.automatic_risk_expansion_enabled is False
    )


def paper_session_drift_allows_new_risk(
    *,
    champion: ChampionSelectionReceipt | None,
    snapshot: MarketCapitalSnapshot,
    requested_drift_ok: bool | None = None,
) -> bool:
    """Resolve the paper-session live-risk drift latch.

    Simulation-only Champion current is not a live-risk latch.  The oneshot
    default used to hardcode ``False``, which rejected every windowed
    order-identity name as ``drift_constraint_blocks_new_risk`` before lot,
    cash, T+1, quote clocks, or any fill attempt.

    Live / real_trading / live_transition_authorized / automatic_risk_expansion
    stay fail-closed even if a caller requests ``drift_ok=True``.  An explicit
    ``requested_drift_ok=False`` still rejects (tests and a real latch).
    """

    if _live_risk_flags_present(champion=champion, snapshot=snapshot):
        return False
    if snapshot.real_trading_enabled is not False:
        return False
    if requested_drift_ok is False:
        return False
    if requested_drift_ok is True:
        return True
    return _simulation_only_champion(champion)


def _load_champion(
    registry: ChampionSelectionRegistry | None,
) -> ChampionSelectionReceipt | None:
    if registry is None:
        return None
    try:
        current = registry.load_current()
    except ChampionRegistryError:
        return None
    if not _simulation_only_champion(current):
        raise CapitalBackedPaperError("champion_current_not_simulation_only")
    return current


def _unfilled_attempt(reason_code: str) -> FillAttemptResult:
    return FillAttemptResult(
        committed=False,
        fill_id=None,
        filled_quantity=0,
        filled_notional_cny=0.0,
        actual_cost_cny=0.0,
        ledger_event_id=None,
        reason_code=reason_code,
    )


def _replace_window(window: SymbolWindow, **updates: object) -> SymbolWindow:
    payload = {item.name: getattr(window, item.name) for item in fields(SymbolWindow)}
    payload.update(updates)
    return SymbolWindow(**payload)  # type: ignore[arg-type]


def _paper_weekday_sessions() -> tuple[date, ...]:
    current = date(2024, 12, 2)
    end = date(2027, 12, 31)
    closed = {date(2025, 1, 1)}
    sessions: list[date] = []
    while current <= end:
        if current.weekday() < 5 and current not in closed:
            sessions.append(current)
        current += timedelta(days=1)
    return tuple(sessions)


def paper_session_calendar_receipt() -> dict[str, Any]:
    """Sim-only calendar receipt required by the capital fill port.

    This is the same non-production fixture tier the port already requires.
    It is not a production exchange calendar and does not mint a fill.
    """

    calendar = TradingSessionCalendarAuthority(
        market="ashare",
        calendar_id="fixture-sse-szse-joint-trading-sessions",
        calendar_version="non-production-fixture-through-20271231-v1",
        source_dataset_id="fixture.ashare.trade_calendar",
        source_receipt_id="receipt-non-production-fixture-calendar-001",
        source_receipt_sha256="e" * 64,
        available_at=datetime(2025, 4, 1, tzinfo=timezone.utc),
        sessions=_paper_weekday_sessions(),
    )
    frozen_at = datetime(2025, 4, 2, tzinfo=timezone.utc)
    verification = TradingSessionCalendarAuthorityVerification(
        accepted=True,
        verifier_id="non-production-fixture-calendar-verifier",
        verifier_version="1.0.0",
        proof_sha256="f" * 64,
        verified_at=frozen_at - timedelta(minutes=1),
        frozen_at=frozen_at,
        calendar_sha256=calendar.calendar_sha256,
        source_receipt_id=calendar.source_receipt_id,
        source_receipt_sha256=calendar.source_receipt_sha256,
    )
    return {
        "authority_tier": "non_production_fixture",
        "production_eligible": False,
        "calendar": calendar.canonical_payload(),
        "verification": verification.canonical_payload(),
    }


def _bar_evidence_bid_ask(
    *,
    last_cny: float,
    high_cny: float | None,
    low_cny: float | None,
) -> tuple[float, float] | None:
    """Model bid/ask from a completed rt_min last.  Never copy last as both."""

    model = ashare_execution_reality()
    if last_cny <= 0:
        return None
    slippage = model.conservative_label_slippage_bps_per_side / 10_000.0
    ask = model._round_to_tick(float(last_cny) * (1.0 + slippage))
    bid = model._round_to_tick(float(last_cny) * (1.0 - slippage))
    tick = model.price_tick_cny
    if ask <= last_cny:
        ask = model._round_to_tick(last_cny + tick)
    if bid >= last_cny:
        bid = model._round_to_tick(max(tick, last_cny - tick))
    if high_cny is not None and high_cny > 0 and ask > high_cny:
        return None
    if low_cny is not None and low_cny > 0 and bid < low_cny:
        return None
    if bid <= 0 or ask <= bid:
        return None
    return bid, ask


def _reservation_price_cny(*, ask: float, high_cny: float | None) -> float:
    """Worst-case buy reservation.  The engine may slip through the ask."""

    model = ashare_execution_reality()
    pad = model.conservative_label_slippage_bps_per_side / 10_000.0
    padded = model._round_to_tick(ask * (1.0 + pad) + model.price_tick_cny)
    if high_cny is not None and high_cny >= padded:
        return model._round_to_tick(float(high_cny))
    return padded


class _StaticFillStagePort:
    """Minimal in-process stage payload.  Not a second capital authority."""

    def __init__(self, stage: RunStage, payload: Mapping[str, Any]) -> None:
        self.identity = ComponentIdentity(
            stage=stage,
            component_id=f"ashare-paper-fill-{stage.value}",
            version="1",
            artifact_sha256=_sha256({"stage": stage.value, "payload": dict(payload)}),
        )
        self._payload = dict(payload)

    def execute(self, request: StageRequest) -> StageResult:
        if request.stage is not self.identity.stage:
            raise CapitalBackedPaperError("fill_stage_mismatch")
        return StageResult(payload=self._payload)


def _fill_bundle(
    *,
    run_id: str,
    trade_date: str,
    decision_as_of: str,
    snapshot: MarketCapitalSnapshot,
    permitted_order_ids: tuple[str, ...],
    stage_payloads: Mapping[RunStage, Mapping[str, Any]],
) -> SimpleNamespace:
    return SimpleNamespace(
        context=SimpleNamespace(
            authority_id=snapshot.authority_id,
            authority_generation=snapshot.authority_generation,
            execution_lineage=snapshot.execution_lineage_id,
            trade_date=trade_date,
            decision_as_of=decision_as_of,
            account_type="simulated",
            real_trading_enabled=False,
        ),
        run_id=run_id,
        permitted_order_ids=permitted_order_ids,
        stage_payloads=dict(stage_payloads),
        receipt_for=lambda stage: SimpleNamespace(payload=dict(stage_payloads[stage])),
    )


def _build_bar_evidence_snapshot(
    request: FillAttemptRequest,
    *,
    order_id: str,
    snapshot: MarketCapitalSnapshot,
    decision_text: str,
    execution_text: str,
    calendar_receipt: Mapping[str, Any],
) -> dict[str, Any] | None:
    window = request.window
    last = window.snapshot_last_cny
    if last is None or last <= 0:
        return None
    spread = _bar_evidence_bid_ask(
        last_cny=last,
        high_cny=window.snapshot_high_cny,
        low_cny=window.snapshot_low_cny,
    )
    if spread is None:
        return None
    bid, ask = spread
    prior_close = float(window.prior_close_cny or 0.0)
    if prior_close <= 0:
        return None
    execution = _parse_quote_clock_time(execution_text)
    if execution is None:
        return None
    session = ashare_continuous_session(execution)
    if session is None:
        return None
    data_through = window.snapshot_data_through
    observed = window.snapshot_observed_at
    available = window.snapshot_available_at
    catalog_version = window.snapshot_catalog_version or window.catalog_version
    source = MarketSourceBinding(
        dataset_id=QUOTE_CLOCK_DATASET_ID,
        catalog_version=catalog_version,
        source_receipt_id=window.snapshot_receipt_id,
        source_receipt_sha256=window.snapshot_source_sha256,
        source_lineage_sha256=window.snapshot_lineage_sha256,
        data_through=_parse_quote_clock_time(data_through) or execution,
        observed_at=_parse_quote_clock_time(observed) or execution,
        available_at=_parse_quote_clock_time(available) or execution,
    )
    if not request.trade_date:
        return None
    context = MarketEvidenceContext(
        trade_date=date.fromisoformat(request.trade_date),
        decision_as_of=datetime.fromisoformat(decision_text.replace("Z", "+00:00")),
        capital_authority_id=snapshot.authority_id,
        authority_generation=snapshot.authority_generation,
        execution_lineage_id=snapshot.execution_lineage_id,
        account_type="simulated",
        real_trading_enabled=False,
    )
    receipt_sha256 = _sha256(
        {
            "authority_tier": calendar_receipt.get("authority_tier"),
            "calendar": calendar_receipt.get("calendar"),
            "production_eligible": calendar_receipt.get("production_eligible"),
            "verification": calendar_receipt.get("verification"),
        }
    )
    quote = AShareExecutionQuoteEvidence(
        symbol=request.symbol,
        order_id=order_id,
        bid_price_cny=bid,
        ask_price_cny=ask,
        bid_size=BUY_LOT,
        ask_size=BUY_LOT,
        previous_close_cny=prior_close,
        market_session=session,
        execution_time=execution,
        source=source,
        session_calendar_receipt_sha256=str(
            calendar_receipt.get("receipt_sha256") or receipt_sha256
        ),
        context=context,
    )
    authority = freeze_non_production_market_evidence(
        quote,
        expected_dataset_id=QUOTE_CLOCK_DATASET_ID,
        frozen_at=execution,
    )
    return {
        "snapshot_id": f"SNAPSHOT-{order_id}",
        "source_receipt_id": window.snapshot_receipt_id,
        "source_sha256": window.snapshot_source_sha256,
        "source_lineage_sha256": window.snapshot_lineage_sha256,
        "dataset_id": QUOTE_CLOCK_DATASET_ID,
        "catalog_version": catalog_version,
        "symbol": request.symbol,
        "market": "ashare",
        "trade_date": request.trade_date,
        "decision_as_of": decision_text,
        "capital_authority_id": snapshot.authority_id,
        "authority_generation": snapshot.authority_generation,
        "execution_lineage": snapshot.execution_lineage_id,
        "account_type": "simulated",
        "real_trading_enabled": False,
        "observed_at": source.observed_at.isoformat(),
        "available_at": source.available_at.isoformat(),
        "data_through": source.data_through.isoformat(),
        "execution_time": execution.isoformat(),
        "bid_price": bid,
        "ask_price": ask,
        "bid_size": BUY_LOT,
        "ask_size": BUY_LOT,
        "previous_close": prior_close,
        "market_session": session,
        "session_calendar_receipt": dict(calendar_receipt),
        "cash_available": float(snapshot.cash_balance_cny),
        "market_evidence_authority": authority,
        "bar_evidence_fill": True,
        "bar_last_cny": last,
        "bar_volume": float(window.snapshot_volume or 0.0),
        "volume": float(window.snapshot_volume or 0.0),
    }


def attempt_capital_backed_simulation_fill(
    request: FillAttemptRequest,
    *,
    ledger: MarketCapitalLedger,
    account: PaperCapitalAccount | None = None,
) -> FillAttemptResult:
    """The only fill path is ``CapitalBackedSimulationExecutionStagePort``.

    Daily close/touch is valuation, not a fill snapshot.  Missing quote clocks
    stay ``PAPER_NOT_FILLED``.  This function never mints a commit from
    ``prior_close_cny``.  A bound in-session ``rt_min`` bar may become
    bar-evidence bid/ask (last ± conservative slippage), never a copied close.
    """

    if type(request) is not FillAttemptRequest:
        raise CapitalBackedPaperError("fill_attempt_request_invalid")
    if type(ledger) is not MarketCapitalLedger:
        raise CapitalBackedPaperError("capital_ledger_required")
    if not request.window.fill_quote_ready:
        return _unfilled_attempt("quote_clocks_unavailable")
    if (
        _CAPITAL_EXECUTION_PORT is not CapitalBackedSimulationExecutionStagePort
        or _PAPER_CAPITAL_ACCOUNT is not PaperCapitalAccount
    ):
        raise CapitalBackedPaperError("wrong_capital_fill_port")
    if not request.window.fill_snapshot_ready:
        return _unfilled_attempt("capital_fill_market_snapshot_unavailable")
    if (
        not request.trade_date
        or request.decision_as_of is None
        or request.decision_as_of.tzinfo is None
        or request.decision_as_of.utcoffset() is None
        or not request.run_id
    ):
        return _unfilled_attempt("capital_fill_market_snapshot_unavailable")
    spread = _bar_evidence_bid_ask(
        last_cny=float(request.window.snapshot_last_cny or 0.0),
        high_cny=request.window.snapshot_high_cny,
        low_cny=request.window.snapshot_low_cny,
    )
    if spread is None:
        return _unfilled_attempt("capital_fill_bar_evidence_invalid")
    decision_text = request.decision_as_of.isoformat()
    available = _parse_quote_clock_time(request.window.snapshot_available_at)
    data_through = _parse_quote_clock_time(request.window.snapshot_data_through)
    if available is None or data_through is None:
        return _unfilled_attempt("capital_fill_market_snapshot_unavailable")
    execution = request.decision_as_of
    if execution < available:
        execution = available
    execution_text = execution.isoformat()
    if execution - data_through > timedelta(seconds=30):
        return _unfilled_attempt("paper_market_snapshot_stale")
    calendar_receipt = paper_session_calendar_receipt()
    snapshot_before = ledger.snapshot()
    market_snapshot = _build_bar_evidence_snapshot(
        request,
        order_id=f"ORDER-{request.symbol}",
        snapshot=snapshot_before,
        decision_text=decision_text,
        execution_text=execution_text,
        calendar_receipt=calendar_receipt,
    )
    if market_snapshot is None:
        return _unfilled_attempt("capital_fill_bar_evidence_invalid")
    artifact_root = request.artifact_root
    if artifact_root is None:
        artifact_root = Path(ledger.root) / "paper-capital-artifacts"
    bound_account = account
    if bound_account is None:
        bound_account = PaperCapitalAccount(
            ledger=ledger,
            artifact_root=artifact_root,
            mark_prices={},
        )
    elif type(bound_account) is not PaperCapitalAccount:
        raise CapitalBackedPaperError("paper_capital_account_invalid")
    reservation_price = _reservation_price_cny(
        ask=float(spread[1]),
        high_cny=request.window.snapshot_high_cny,
    )
    order = {
        "order_id": f"ORDER-{request.symbol}",
        "decision_id": f"DECISION-{request.symbol}",
        "symbol": request.symbol,
        "intent": "open",
        "side": "buy",
        "quantity": int(request.quantity),
        "reservation_price_cny": reservation_price,
        "expected_fee_cny": float(
            ashare_execution_reality()
            .calculate_fees("buy", request.quantity * reservation_price)
            .get("total")
            or 5.0
        ),
        "capital_authority_id": snapshot_before.authority_id,
        "authority_generation": snapshot_before.authority_generation,
        "execution_lineage": snapshot_before.execution_lineage_id,
    }
    context = {
        "run_id": request.run_id,
        "trade_date": request.trade_date,
        "decision_as_of": decision_text,
    }
    try:
        if not bound_account.ledger.provider_state(
            request.trade_date.replace("-", "")
        ).get("fresh"):
            preopen_bundle = _fill_bundle(
                **context,
                snapshot=snapshot_before,
                permitted_order_ids=(),
                stage_payloads={
                    RunStage.PREOPEN: {
                        "market": "ashare",
                        "account_type": "simulated",
                        "real_trading_enabled": False,
                        "account_authority_valid": True,
                        "position_authority_valid": True,
                    }
                },
            )
            CapitalBackedPreopenStagePort(
                base_port=_StaticFillStagePort(
                    RunStage.PREOPEN,
                    {
                        "market": "ashare",
                        "account_type": "simulated",
                        "real_trading_enabled": False,
                        "account_authority_valid": True,
                        "position_authority_valid": True,
                    },
                ),
                account=bound_account,
            ).execute(
                StageRequest(
                    run_id=request.run_id,
                    stage=RunStage.PREOPEN,
                    idempotency_key=_sha256(f"{request.run_id}:preopen"),
                    input_bundle_sha256=_sha256(context),
                    bundle=preopen_bundle,  # type: ignore[arg-type]
                    allowed_actions=("open", "increase", "reduce", "exit", "hold"),
                    permitted_order_ids=(),
                )
            )
        risk_bundle = _fill_bundle(
            **context,
            snapshot=snapshot_before,
            permitted_order_ids=(order["order_id"],),
            stage_payloads={
                RunStage.RISK_CHECKED: {
                    "risk_policy_version": "ashare-paper-bar-evidence-v1",
                    "oms_plan_id": f"PLAN-{request.symbol}",
                    "approved_orders": [order],
                    "rejected_decisions": [],
                }
            },
        )
        risk_payload = (
            CapitalBackedRiskStagePort(
                base_port=_StaticFillStagePort(
                    RunStage.RISK_CHECKED,
                    {
                        "risk_policy_version": "ashare-paper-bar-evidence-v1",
                        "oms_plan_id": f"PLAN-{request.symbol}",
                        "approved_orders": [order],
                        "rejected_decisions": [],
                    },
                ),
                account=bound_account,
            )
            .execute(
                StageRequest(
                    run_id=request.run_id,
                    stage=RunStage.RISK_CHECKED,
                    idempotency_key=_sha256(f"{request.run_id}:risk:{request.symbol}"),
                    input_bundle_sha256=_sha256(context),
                    bundle=risk_bundle,  # type: ignore[arg-type]
                    allowed_actions=("open", "increase", "reduce", "exit", "hold"),
                    permitted_order_ids=(order["order_id"],),
                )
            )
            .payload
        )
        approved = risk_payload.get("approved_orders") or []
        if not approved:
            rejected = risk_payload.get("rejected_decisions") or []
            reason = "capital_reservation_rejected"
            if rejected and isinstance(rejected[0], Mapping):
                reason = str(rejected[0].get("reason") or reason)
            return _unfilled_attempt(reason)
        execution_bundle = _fill_bundle(
            **context,
            snapshot=snapshot_before,
            permitted_order_ids=(order["order_id"],),
            stage_payloads={RunStage.RISK_CHECKED: risk_payload},
        )
        payload = CapitalBackedSimulationExecutionStagePort(
            account=bound_account,
            market_snapshots={order["order_id"]: market_snapshot},
            execution_clock=NonProductionFixtureExecutionClock.from_isoformat(
                default_instant=execution_text,
                effect_overrides={},
            ),
        ).execute(
            StageRequest(
                run_id=request.run_id,
                stage=RunStage.ORDERS_SIMULATED,
                idempotency_key=_sha256(f"{request.run_id}:exec:{request.symbol}"),
                input_bundle_sha256=_sha256(context),
                bundle=execution_bundle,  # type: ignore[arg-type]
                allowed_actions=("open", "increase", "reduce", "exit", "hold"),
                permitted_order_ids=(order["order_id"],),
            )
        ).payload
    except PaperCapitalStageError as exc:
        reason = str(exc).split(":", 1)[0]
        if not reason:
            reason = "capital_fill_port_rejected"
        return _unfilled_attempt(reason)
    except (TypeError, ValueError) as exc:
        reason = str(exc).split(":", 1)[0] or "capital_fill_port_rejected"
        if reason.startswith("paper_") or reason.startswith("capital_"):
            return _unfilled_attempt(reason)
        return _unfilled_attempt("capital_fill_port_rejected")
    receipts = payload.get("order_receipts") or []
    receipt = receipts[0] if receipts else {}
    if not isinstance(receipt, Mapping):
        return _unfilled_attempt("order_not_filled_by_simulator")
    filled_quantity = int(receipt.get("filled_quantity") or 0)
    filled_price = float(receipt.get("filled_price_cny") or 0.0)
    filled_notional = float(
        receipt.get("filled_notional_cny") or (filled_quantity * filled_price)
    )
    after = ledger.snapshot()
    committed = receipt.get("capital_commit_status") == "committed"
    if not committed or filled_quantity <= 0:
        return _unfilled_attempt(
            str(receipt.get("execution_reason") or "order_not_filled_by_simulator")
        )
    return FillAttemptResult(
        committed=True,
        fill_id=str(receipt.get("simulated_fill_id") or "") or None,
        filled_quantity=filled_quantity,
        filled_notional_cny=filled_notional,
        actual_cost_cny=float(receipt.get("fee_cny") or 0.0),
        ledger_event_id=after.event_id,
        reason_code="simulated_fill_recorded",
    )


def _requested_notional(window: SymbolWindow | None) -> float:
    if window is None or window.prior_close_cny is None:
        return 0.0
    return float(BUY_LOT * window.prior_close_cny)


def _lot_cash_t1_ok(
    *,
    snapshot: MarketCapitalSnapshot,
    window: SymbolWindow,
    action: str,
    symbol: str,
) -> tuple[bool, bool, bool, str]:
    price = float(window.prior_close_cny or 0.0)
    notional = BUY_LOT * price
    lot_ok = BUY_LOT % 100 == 0 and BUY_LOT >= 100 and 0 < notional <= SINGLE_NAME_CAP_CNY
    cash_ok = (
        snapshot.cash_balance_cny >= notional
        and snapshot.stock_gross_exposure_limit_cny >= GROSS_CAP_CNY
        and snapshot.available_to_reserve_cny >= notional
    )
    if action == "buy":
        t_plus_1_ok = True
    else:
        held = int(snapshot.positions_quantity_by_risk_unit.get(symbol, 0) or 0)
        t_plus_1_ok = held >= BUY_LOT
    if not lot_ok:
        return lot_ok, cash_ok, t_plus_1_ok, "lot_or_single_name_cap_blocked"
    if not cash_ok:
        return lot_ok, cash_ok, t_plus_1_ok, "cash_or_gross_cap_blocked"
    if not t_plus_1_ok:
        return lot_ok, cash_ok, t_plus_1_ok, "t_plus_1_not_sellable"
    return lot_ok, cash_ok, t_plus_1_ok, "capital_gates_ready"


def _decide_disposition(
    *,
    classification: SessionSymbolClassification,
    window: SymbolWindow | None,
    champion: ChampionSelectionReceipt | None,
    snapshot: MarketCapitalSnapshot,
    drift_ok: bool,
    fill_result: FillAttemptResult | None,
    ledger_after: MarketCapitalSnapshot,
    ledger_before: MarketCapitalSnapshot,
) -> CandidateDisposition:
    symbol = classification.symbol
    if not classification.order_identity_allowed:
        return CandidateDisposition(
            symbol=symbol,
            disposition=ExposureDisposition.REJECTED,
            action="hold",
            reason_code=classification.reason_code,
            rejection_reason=classification.reason_code,
            nonfill_reason=None,
            requested_notional_cny=0.0,
            filled_quantity=0,
            filled_notional_cny=0.0,
            actual_cost_cny=0.0,
            simulated_fill_id=None,
            order_identity_allowed=False,
            sleeve=classification.sleeve,
        )
    if window is None or not window.observation_ready:
        reason = (
            window.reason_code
            if window is not None
            else "missing_dataset_catalog_or_session_window"
        )
        return CandidateDisposition(
            symbol=symbol,
            disposition=ExposureDisposition.OBSERVATION_ONLY,
            action="hold",
            reason_code=reason,
            rejection_reason=None,
            nonfill_reason=None,
            requested_notional_cny=_requested_notional(window),
            filled_quantity=0,
            filled_notional_cny=0.0,
            actual_cost_cny=0.0,
            simulated_fill_id=None,
            order_identity_allowed=True,
            sleeve=classification.sleeve,
        )
    if champion is None:
        return CandidateDisposition(
            symbol=symbol,
            disposition=ExposureDisposition.REJECTED,
            action="hold",
            reason_code="champion_current_unavailable",
            rejection_reason="champion_current_unavailable",
            nonfill_reason=None,
            requested_notional_cny=_requested_notional(window),
            filled_quantity=0,
            filled_notional_cny=0.0,
            actual_cost_cny=0.0,
            simulated_fill_id=None,
            order_identity_allowed=True,
            sleeve=classification.sleeve,
        )
    if not drift_ok:
        return CandidateDisposition(
            symbol=symbol,
            disposition=ExposureDisposition.REJECTED,
            action="hold",
            reason_code="drift_constraint_blocks_new_risk",
            rejection_reason="drift_constraint_blocks_new_risk",
            nonfill_reason=None,
            requested_notional_cny=_requested_notional(window),
            filled_quantity=0,
            filled_notional_cny=0.0,
            actual_cost_cny=0.0,
            simulated_fill_id=None,
            order_identity_allowed=True,
            sleeve=classification.sleeve,
        )
    lot_ok, cash_ok, t_plus_1_ok, capital_reason = _lot_cash_t1_ok(
        snapshot=snapshot,
        window=window,
        action="buy",
        symbol=symbol,
    )
    if not (lot_ok and cash_ok and t_plus_1_ok):
        return CandidateDisposition(
            symbol=symbol,
            disposition=ExposureDisposition.REJECTED,
            action="hold",
            reason_code=capital_reason,
            rejection_reason=capital_reason,
            nonfill_reason=None,
            requested_notional_cny=_requested_notional(window),
            filled_quantity=0,
            filled_notional_cny=0.0,
            actual_cost_cny=0.0,
            simulated_fill_id=None,
            order_identity_allowed=True,
            sleeve=classification.sleeve,
        )
    if not window.fill_quote_ready:
        return CandidateDisposition(
            symbol=symbol,
            disposition=ExposureDisposition.PAPER_NOT_FILLED,
            action="buy",
            reason_code="quote_clocks_unavailable",
            rejection_reason=None,
            nonfill_reason="quote_clocks_unavailable",
            requested_notional_cny=_requested_notional(window),
            filled_quantity=0,
            filled_notional_cny=0.0,
            actual_cost_cny=0.0,
            simulated_fill_id=None,
            order_identity_allowed=True,
            sleeve=classification.sleeve,
        )
    if fill_result is None:
        return CandidateDisposition(
            symbol=symbol,
            disposition=ExposureDisposition.PAPER_NOT_FILLED,
            action="buy",
            reason_code="capital_fill_attempt_not_provided",
            rejection_reason=None,
            nonfill_reason="capital_fill_attempt_not_provided",
            requested_notional_cny=_requested_notional(window),
            filled_quantity=0,
            filled_notional_cny=0.0,
            actual_cost_cny=0.0,
            simulated_fill_id=None,
            order_identity_allowed=True,
            sleeve=classification.sleeve,
        )
    if fill_result.committed:
        if not (
            fill_result.fill_id
            and fill_result.filled_quantity > 0
            and fill_result.filled_notional_cny > 0
            and _ledger_has_new_fill(ledger_before, ledger_after)
        ):
            raise CapitalBackedPaperError("invented_fill_without_ledger_commit")
        return CandidateDisposition(
            symbol=symbol,
            disposition=ExposureDisposition.PAPER_FILLED,
            action="buy",
            reason_code="simulated_fill_recorded",
            rejection_reason=None,
            nonfill_reason=None,
            requested_notional_cny=_requested_notional(window),
            filled_quantity=fill_result.filled_quantity,
            filled_notional_cny=fill_result.filled_notional_cny,
            actual_cost_cny=fill_result.actual_cost_cny,
            simulated_fill_id=fill_result.fill_id,
            order_identity_allowed=True,
            sleeve=classification.sleeve,
        )
    return CandidateDisposition(
        symbol=symbol,
        disposition=ExposureDisposition.PAPER_NOT_FILLED,
        action="buy",
        reason_code=fill_result.reason_code or "order_not_filled_by_simulator",
        rejection_reason=None,
        nonfill_reason=fill_result.reason_code or "order_not_filled_by_simulator",
        requested_notional_cny=_requested_notional(window),
        filled_quantity=0,
        filled_notional_cny=0.0,
        actual_cost_cny=0.0,
        simulated_fill_id=None,
        order_identity_allowed=True,
        sleeve=classification.sleeve,
    )


def _write_latest(
    *,
    config: CapitalBackedPaperConfig,
    run_id: str,
    input_bundle_sha256: str,
    snapshot: MarketCapitalSnapshot,
    champion: ChampionSelectionReceipt | None,
    dispositions: tuple[CandidateDisposition, ...],
    fill_count: int,
    ledger_fill_commit_count: int,
) -> None:
    payload = {
        "authority_generation": snapshot.authority_generation,
        "candidates": [item.to_latest_row() for item in dispositions],
        "canonical_account_connected": False,
        "capital_authority_id": snapshot.authority_id,
        "champion_manifest_sha256": (
            champion.selected_manifest_sha256
            if champion is not None
            else CHAMPION_UNAVAILABLE_MANIFEST_SHA256
        ),
        "champion_model_id": (
            champion.selected_model_id if champion is not None else "champion_unavailable"
        ),
        "contract_id": RUNNER_CONTRACT_ID,
        "decision_as_of": config.decision_as_of.astimezone(timezone.utc).isoformat(),
        "execution_lineage_id": snapshot.execution_lineage_id,
        "fill_count": fill_count,
        "input_bundle_sha256": input_bundle_sha256,
        "ledger_fill_commit_count": ledger_fill_commit_count,
        "opening_cash_cny": snapshot.initial_equity_cny,
        "real_trading_enabled": False,
        "run_id": run_id,
        "trade_date": config.trade_date,
        "universe_sha256": FROZEN_UNIVERSE_SHA256,
    }
    encoded = (
        _canonical_json(
            {
                **payload,
                "_projection": {
                    "authority": "non_authority",
                    "bundle_sha256": _sha256(payload),
                    "environment": "local_candidate",
                    "production_verified": False,
                    "record_type": "capital_backed_paper_session",
                    "schema_version": 1,
                },
            }
        )
        + "\n"
    ).encode("utf-8")
    config.latest_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = config.latest_path.with_name(
        f".{config.latest_path.name}.{os.getpid()}.tmp"
    )
    temporary.write_bytes(encoded)
    os.replace(temporary, config.latest_path)


def run_capital_backed_paper_session(
    config: CapitalBackedPaperConfig,
    *,
    windows: Mapping[str, SymbolWindow] | None = None,
    window_source: WindowSource | None = None,
    champion_registry: ChampionSelectionRegistry | None = None,
    drift_ok: bool | None = None,
    fill_attempt: FillAttempt | None = None,
    fill_source: str | None = None,
    coverage_accepted_count: int | None = None,
    scope_policy: CanonicalMainboardScopePolicy | None = None,
) -> CapitalBackedPaperSessionResult:
    """Run one isolated session and persist every candidate disposition."""

    if not isinstance(config, CapitalBackedPaperConfig):
        raise CapitalBackedPaperError("session_config_missing")
    if fill_source is not None:
        reject_invented_fill_source(fill_source)
    if coverage_accepted_count is not None:
        count_coverage_is_not_a_fill(coverage_accepted_count)

    ledger, snapshot_before = _bind_ledger(config)
    champion = _load_champion(champion_registry)
    resolved_drift_ok = paper_session_drift_allows_new_risk(
        champion=champion,
        snapshot=snapshot_before,
        requested_drift_ok=drift_ok,
    )
    bound_fill_attempt = fill_attempt
    if bound_fill_attempt is None:
        def _default_fill(request: FillAttemptRequest) -> FillAttemptResult:
            return attempt_capital_backed_simulation_fill(request, ledger=ledger)

        bound_fill_attempt = _default_fill
    classifications = classify_session_universe(
        extra_symbols=config.extra_symbols,
        include_exclusion_probes=config.include_exclusion_probes,
        scope_policy=scope_policy or CanonicalMainboardScopePolicy(),
    )
    symbols = tuple(item.symbol for item in classifications)
    resolved_windows: dict[str, SymbolWindow] = dict(windows or {})
    if window_source is not None:
        fetched = window_source(symbols)
        for symbol, window in dict(fetched).items():
            if not isinstance(window, SymbolWindow):
                raise CapitalBackedPaperError("window_source_invalid")
            resolved_windows[symbol] = window

    valid_subset = tuple(
        item.symbol
        for item in classifications
        if item.order_identity_allowed
        and resolved_windows.get(item.symbol) is not None
        and resolved_windows[item.symbol].observation_ready
    )
    # First nonempty valid subset is enough; missing windows stay observations.
    del valid_subset

    identity = {
        "contract_id": RUNNER_CONTRACT_ID,
        "trade_date": config.trade_date,
        "decision_as_of": config.decision_as_of.astimezone(timezone.utc).isoformat(),
        "universe_sha256": FROZEN_UNIVERSE_SHA256,
        "capital_authority_id": snapshot_before.authority_id,
        "authority_generation": snapshot_before.authority_generation,
        "execution_lineage_id": snapshot_before.execution_lineage_id,
    }
    input_bundle_sha256 = _sha256(identity)
    run_id = f"ashare-paper-day-{input_bundle_sha256[:32]}"
    journal = SampleJournal(config.journal_path)
    decision_ledger = SampleJournalDecisionLedger(
        journal=journal,
        source_run_id=run_id,
        input_bundle_sha256=input_bundle_sha256,
        capital_authority_id=snapshot_before.authority_id,
        authority_generation=snapshot_before.authority_generation,
        execution_lineage_id=snapshot_before.execution_lineage_id,
    )
    model_id = (
        champion.selected_model_id
        if champion is not None
        else "uncalibrated_deterministic_rank_score"
    )
    model_version = champion.selected_model_version if champion is not None else "1"
    manifest_sha256 = (
        champion.selected_manifest_sha256
        if champion is not None
        else CHAMPION_UNAVAILABLE_MANIFEST_SHA256
    )

    dispositions: list[CandidateDisposition] = []
    ledger_cursor = snapshot_before
    fill_count = 0
    for index, classification in enumerate(classifications):
        window = resolved_windows.get(classification.symbol)
        fill_result = None
        snapshot_after = ledger_cursor
        if (
            bound_fill_attempt is not None
            and classification.order_identity_allowed
            and champion is not None
            and resolved_drift_ok
            and window is not None
            and window.fill_quote_ready
        ):
            lot_ok, cash_ok, t_plus_1_ok, _reason = _lot_cash_t1_ok(
                snapshot=ledger_cursor,
                window=window,
                action="buy",
                symbol=classification.symbol,
            )
            if lot_ok and cash_ok and t_plus_1_ok:
                fill_result = bound_fill_attempt(
                    FillAttemptRequest(
                        symbol=classification.symbol,
                        quantity=BUY_LOT,
                        prior_close_cny=float(window.prior_close_cny or 0.0),
                        champion=champion,
                        window=window,
                        snapshot_before=ledger_cursor,
                        trade_date=config.trade_date,
                        decision_as_of=config.decision_as_of,
                        run_id=run_id,
                        artifact_root=config.latest_path.parent / "capital-artifacts",
                    )
                )
                if type(fill_result) is not FillAttemptResult:
                    raise CapitalBackedPaperError("fill_attempt_result_invalid")
                snapshot_after = ledger.snapshot()
        disposition = _decide_disposition(
            classification=classification,
            window=window,
            champion=champion,
            snapshot=ledger_cursor,
            drift_ok=resolved_drift_ok,
            fill_result=fill_result,
            ledger_after=snapshot_after,
            ledger_before=ledger_cursor,
        )
        if disposition.disposition is ExposureDisposition.PAPER_FILLED:
            if not _ledger_has_new_fill(ledger_cursor, snapshot_after):
                raise CapitalBackedPaperError("invented_fill_without_ledger_commit")
            fill_count += 1
            ledger_cursor = snapshot_after
        record = DecisionExposureRecord(
            decision_id=f"cbp-{config.trade_date}-{index:04d}-{classification.symbol}",
            decision_cluster_id=f"cbp-cluster-{config.trade_date}",
            decision_time=config.decision_as_of,
            symbol=classification.symbol,
            model_id=model_id,
            model_version=model_version,
            manifest_sha256=manifest_sha256,
            action=disposition.action,
            disposition=disposition.disposition,
            requested_notional_cny=disposition.requested_notional_cny,
            filled_quantity=disposition.filled_quantity,
            filled_notional_cny=disposition.filled_notional_cny,
            actual_cost_cny=disposition.actual_cost_cny,
            simulated_fill_id=disposition.simulated_fill_id,
            rejection_reason=disposition.rejection_reason,
            nonfill_reason=disposition.nonfill_reason,
        )
        decision_ledger.append(
            record,
            receipt_time=config.decision_as_of.astimezone(timezone.utc),
        )
        dispositions.append(disposition)

    snapshot_end = ledger.snapshot()
    ledger_fill_commit_count = len(snapshot_end.unreconciled_fill_commit_ids)
    _write_latest(
        config=config,
        run_id=run_id,
        input_bundle_sha256=input_bundle_sha256,
        snapshot=snapshot_end,
        champion=champion,
        dispositions=tuple(dispositions),
        fill_count=fill_count,
        ledger_fill_commit_count=ledger_fill_commit_count,
    )
    return CapitalBackedPaperSessionResult(
        run_id=run_id,
        input_bundle_sha256=input_bundle_sha256,
        capital_authority_id=snapshot_end.authority_id,
        authority_generation=snapshot_end.authority_generation,
        execution_lineage_id=snapshot_end.execution_lineage_id,
        opening_cash_cny=snapshot_end.initial_equity_cny,
        fill_count=fill_count,
        ledger_fill_commit_count=ledger_fill_commit_count,
        canonical_account_connected=False,
        champion_manifest_sha256=manifest_sha256,
        universe_sha256=FROZEN_UNIVERSE_SHA256,
        dispositions=tuple(dispositions),
        latest_path=config.latest_path,
        journal_path=config.journal_path,
    )


def make_missing_window(symbol: str, *, reason_code: str = "missing_dataset_catalog_or_session_window") -> SymbolWindow:
    return SymbolWindow(
        symbol=symbol,
        dataset_id="",
        catalog_version="",
        quote_fresh=False,
        prior_close_cny=None,
        session_calendar_ok=False,
        quote_clocks_ok=False,
        reason_code=reason_code,
    )


def make_observation_window(
    symbol: str,
    *,
    dataset_id: str = QUOTE_DATASET_ID,
    catalog_version: str = "td-catalog-v1",
    prior_close_cny: float = 10.0,
    quote_clocks_ok: bool = False,
    quote_fresh: bool = True,
    quote_trade_date: str = "",
    quote_clock_at: str = "",
    quote_clock_dataset_id: str = "",
    snapshot_last_cny: float | None = None,
    snapshot_high_cny: float | None = None,
    snapshot_low_cny: float | None = None,
    snapshot_open_cny: float | None = None,
    snapshot_volume: float | None = None,
    snapshot_receipt_id: str = "",
    snapshot_source_sha256: str = "",
    snapshot_lineage_sha256: str = "",
    snapshot_data_through: str = "",
    snapshot_observed_at: str = "",
    snapshot_available_at: str = "",
    snapshot_catalog_version: str = "",
) -> SymbolWindow:
    return SymbolWindow(
        symbol=symbol,
        dataset_id=dataset_id,
        catalog_version=catalog_version,
        quote_fresh=quote_fresh,
        prior_close_cny=prior_close_cny,
        session_calendar_ok=True,
        quote_clocks_ok=quote_clocks_ok,
        reason_code="window_ready" if quote_clocks_ok else "quote_clocks_unavailable",
        quote_trade_date=quote_trade_date,
        quote_clock_at=quote_clock_at,
        quote_clock_dataset_id=quote_clock_dataset_id,
        snapshot_last_cny=snapshot_last_cny,
        snapshot_high_cny=snapshot_high_cny,
        snapshot_low_cny=snapshot_low_cny,
        snapshot_open_cny=snapshot_open_cny,
        snapshot_volume=snapshot_volume,
        snapshot_receipt_id=snapshot_receipt_id,
        snapshot_source_sha256=snapshot_source_sha256,
        snapshot_lineage_sha256=snapshot_lineage_sha256,
        snapshot_data_through=snapshot_data_through,
        snapshot_observed_at=snapshot_observed_at,
        snapshot_available_at=snapshot_available_at,
        snapshot_catalog_version=snapshot_catalog_version,
    )


def _compact_session_date(value: object) -> str:
    raw = str(value or "").replace("-", "").strip()
    if len(raw) < 8 or not raw[:8].isdigit():
        return ""
    return raw[:8]


def _calendar_session_open(row: Mapping[str, Any]) -> bool:
    if "is_open" in row:
        value = row.get("is_open")
    elif "open" in row:
        value = row.get("open")
    else:
        return False
    if type(value) is bool:
        return value
    if type(value) is int:
        return value == 1
    return str(value).strip().lower() in {"1", "true"}


def _calendar_row_for_trade_date(
    calendar_rows: Sequence[Mapping[str, Any]],
    trade_date: str,
) -> Mapping[str, Any] | None:
    expected = _compact_session_date(trade_date)
    if not expected:
        return None
    for row in calendar_rows:
        if not isinstance(row, Mapping):
            continue
        raw = row.get("cal_date") if row.get("cal_date") not in (None, "") else row.get(
            "trade_date"
        )
        if _compact_session_date(raw) == expected:
            return row
    return None


def last_complete_daily_date(
    *,
    trade_date: str,
    calendar_row: Mapping[str, Any] | None,
    daily_rows: Sequence[Mapping[str, Any]] = (),
) -> str:
    """Compact yyyymmdd of the last complete daily strictly before ``trade_date``."""

    session = _compact_session_date(trade_date)
    if not session:
        return ""
    if calendar_row is not None:
        pretrade = _compact_session_date(
            calendar_row.get("pretrade_date") or calendar_row.get("pretrade")
        )
        if pretrade and pretrade < session:
            return pretrade
    prior = [
        compact
        for row in daily_rows
        if isinstance(row, Mapping)
        for compact in (_compact_session_date(row.get("trade_date")),)
        if compact and compact < session
    ]
    return max(prior) if prior else ""


def bind_cash_session_windows(
    symbols: tuple[str, ...],
    *,
    trade_date: str,
    catalog_version: str,
    calendar_rows: Sequence[Mapping[str, Any]],
    daily_rows: Sequence[Mapping[str, Any]],
    dataset_id: str = QUOTE_DATASET_ID,
) -> dict[str, SymbolWindow]:
    """Bind an in-session window from live calendar + last complete daily.

    Today's ``cn.equity.daily`` postclose partition is never required.  A closed
    calendar stays fail-closed.  Daily close/touch is prior-close evidence only
    and never sets ``quote_clocks_ok``.  In-session quote clocks come from
    ``bind_quote_clocks`` / ``cn.dataset.rt_min``, not from this daily bind.
    """

    calendar_row = _calendar_row_for_trade_date(calendar_rows, trade_date)
    session_ok = calendar_row is not None and _calendar_session_open(calendar_row)
    if not session_ok:
        return {
            symbol: make_missing_window(
                symbol,
                reason_code="missing_dataset_catalog_or_session_window",
            )
            for symbol in symbols
        }

    last_complete = last_complete_daily_date(
        trade_date=trade_date,
        calendar_row=calendar_row,
        daily_rows=daily_rows,
    )
    by_symbol: dict[str, Mapping[str, Any]] = {}
    for row in daily_rows:
        if not isinstance(row, Mapping):
            continue
        symbol = str(row.get("ts_code") or row.get("symbol") or "").upper()
        row_date = _compact_session_date(row.get("trade_date"))
        if symbol not in symbols:
            continue
        if last_complete:
            if row_date != last_complete:
                continue
        elif row_date == _compact_session_date(trade_date):
            continue
        by_symbol[symbol] = row

    windows: dict[str, SymbolWindow] = {}
    for symbol in symbols:
        if not looks_like_security_symbol(symbol):
            windows[symbol] = make_missing_window(
                symbol,
                reason_code="industry_shadow_not_order_identity",
            )
            continue
        row = by_symbol.get(symbol)
        if row is None:
            windows[symbol] = SymbolWindow(
                symbol=symbol,
                dataset_id=dataset_id,
                catalog_version=catalog_version,
                quote_fresh=False,
                prior_close_cny=None,
                session_calendar_ok=True,
                quote_clocks_ok=False,
                reason_code="missing_prior_close",
                quote_trade_date=last_complete,
            )
            continue
        close = row.get("close") or row.get("pre_close") or row.get("prior_close")
        try:
            prior_close = float(close)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            prior_close = None
        windows[symbol] = SymbolWindow(
            symbol=symbol,
            dataset_id=dataset_id,
            catalog_version=catalog_version,
            quote_fresh=True,
            prior_close_cny=prior_close,
            session_calendar_ok=True,
            # Daily close/touch is prior-close evidence, not a fill quote clock.
            quote_clocks_ok=False,
            reason_code=(
                "window_ready" if prior_close and prior_close > 0 else "missing_prior_close"
            ),
            quote_trade_date=last_complete
            or _compact_session_date(row.get("trade_date")),
        )
    return windows


def last_complete_in_session_quote_slot(
    *,
    trade_date: str,
    decision_as_of: datetime,
) -> datetime | None:
    """Last completed 5-minute session slot at or before ``decision_as_of``.

    This is an in-session quote clock, not delayed-paper freshness.  Lunch or
    a later in-session oneshot may use the last morning bar.  Daily close is
    never a slot.  Before the first 09:35 bar there is no clock.
    """

    if decision_as_of.tzinfo is None or decision_as_of.utcoffset() is None:
        return None
    session = _compact_session_date(trade_date)
    if not session:
        return None
    local = decision_as_of.astimezone(SHANGHAI)
    if local.date().isoformat() != trade_date:
        return None
    eligible = [slot for slot in session_bar_ends(local.date()) if slot <= local]
    return max(eligible) if eligible else None


def _parse_quote_clock_time(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = datetime.strptime(text, QUOTE_CLOCK_TIME_FORMAT).replace(
                    tzinfo=SHANGHAI
                )
            except ValueError:
                return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=SHANGHAI)
    return parsed.astimezone(SHANGHAI)


def _five_minute_freq(value: object) -> bool:
    if value in (None, ""):
        return False
    return str(value).strip().lower().replace(" ", "") in _FIVE_MINUTE_FREQS


def _looks_like_daily_close_row(row: Mapping[str, Any]) -> bool:
    """Daily close/touch identity: trade_date + close without a 5-min freq."""

    return bool(
        _compact_session_date(row.get("trade_date"))
        and row.get("close") not in (None, "")
        and not _five_minute_freq(row.get("freq"))
    )


def bind_quote_clocks(
    windows: Mapping[str, SymbolWindow],
    *,
    trade_date: str,
    decision_as_of: datetime,
    quote_clock_rows: Sequence[Mapping[str, Any]],
    quote_clock_slot: datetime | None = None,
    source_dataset_id: str = QUOTE_CLOCK_DATASET_ID,
) -> dict[str, SymbolWindow]:
    """Overlay in-session ``rt_min`` clocks onto cash-session windows.

    Daily close/touch rows never set ``quote_clocks_ok``.  A present clock is
    an exact completed session slot at or before ``decision_as_of``.  Missing
    clocks stay ``quote_clocks_ok=False``.  This function never mints a fill.
    """

    slot = quote_clock_slot or last_complete_in_session_quote_slot(
        trade_date=trade_date,
        decision_as_of=decision_as_of,
    )
    from_rt_min = source_dataset_id == QUOTE_CLOCK_DATASET_ID
    clocks: dict[str, datetime] = {}
    if slot is not None:
        for row in quote_clock_rows:
            if not isinstance(row, Mapping) or _looks_like_daily_close_row(row):
                continue
            symbol = str(row.get("ts_code") or row.get("symbol") or "").upper()
            if not looks_like_security_symbol(symbol):
                continue
            freq = row.get("freq")
            if freq not in (None, "") and not _five_minute_freq(freq):
                continue
            if not from_rt_min and not _five_minute_freq(freq):
                continue
            if from_rt_min and freq in (None, "") and _compact_session_date(
                row.get("trade_date")
            ):
                # Daily identity even if handed in as an rt_min query result.
                continue
            instant = _parse_quote_clock_time(
                row.get("time") if row.get("time") not in (None, "") else row.get(
                    "bar_end"
                )
            )
            if instant is None or instant != slot:
                continue
            clocks[symbol] = instant

    bound: dict[str, SymbolWindow] = {}
    for symbol, window in dict(windows).items():
        clock = clocks.get(symbol)
        if clock is None or not window.observation_ready:
            bound[symbol] = _replace_window(
                window,
                quote_clocks_ok=False,
                reason_code=(
                    window.reason_code
                    if not window.observation_ready
                    else "quote_clocks_unavailable"
                ),
                quote_clock_at="",
                quote_clock_dataset_id="",
            )
            continue
        bound[symbol] = _replace_window(
            window,
            quote_clocks_ok=True,
            reason_code="window_ready",
            quote_clock_at=clock.strftime(QUOTE_CLOCK_TIME_FORMAT),
            quote_clock_dataset_id=QUOTE_CLOCK_DATASET_ID,
        )
    return bound


@dataclass(frozen=True)
class QuoteClockQueryProof:
    """Receipt/lineage proof for one rt_min clock query.  Not a fill."""

    receipt_id: str
    catalog_version: str
    data_through: str
    observed_at: str
    source_sha256: str
    source_lineage_sha256: str


def _finite_price(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        try:
            parsed = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
    else:
        parsed = float(value)
    if parsed != parsed or parsed <= 0:
        return None
    return parsed


def bind_market_snapshots(
    windows: Mapping[str, SymbolWindow],
    *,
    trade_date: str,
    decision_as_of: datetime,
    snapshot_rows: Sequence[Mapping[str, Any]],
    snapshot_proof: QuoteClockQueryProof | None = None,
    source_dataset_id: str = QUOTE_CLOCK_DATASET_ID,
) -> dict[str, SymbolWindow]:
    """Bind in-session rt_min bar-evidence onto clocked windows.

    Daily close/touch rows never become a snapshot.  Last/close is the bar
    mid, not bid/ask.  Missing volume or receipt proof leaves the window
    clocked but snapshot-unready.  This function never mints a fill.
    """

    slot = last_complete_in_session_quote_slot(
        trade_date=trade_date,
        decision_as_of=decision_as_of,
    )
    def _proof_ready(proof: QuoteClockQueryProof | None) -> bool:
        return bool(
            proof is not None
            and proof.receipt_id
            and (proof.catalog_version or "")
            and _looks_like_sha256(proof.source_sha256)
            and _looks_like_sha256(proof.source_lineage_sha256)
        )

    bars: dict[str, Mapping[str, Any]] = {}
    if slot is not None and _proof_ready(snapshot_proof):
        for row in snapshot_rows:
            if not isinstance(row, Mapping) or _looks_like_daily_close_row(row):
                continue
            symbol = str(row.get("ts_code") or row.get("symbol") or "").upper()
            if not looks_like_security_symbol(symbol):
                continue
            freq = row.get("freq")
            if freq not in (None, "") and not _five_minute_freq(freq):
                continue
            if source_dataset_id != QUOTE_CLOCK_DATASET_ID:
                continue
            instant = _parse_quote_clock_time(
                row.get("time") if row.get("time") not in (None, "") else row.get(
                    "bar_end"
                )
            )
            if instant is None or instant != slot:
                continue
            last = _finite_price(row.get("close") or row.get("last") or row.get("open"))
            volume = _finite_price(row.get("vol") or row.get("volume") or row.get("vol_shares"))
            if last is None or volume is None:
                continue
            bars[symbol] = row

    bound: dict[str, SymbolWindow] = {}
    for symbol, window in dict(windows).items():
        row = bars.get(symbol)
        if (
            row is None
            or not _proof_ready(snapshot_proof)
            or snapshot_proof is None
            or not window.fill_quote_ready
            or window.quote_clock_at == ""
        ):
            bound[symbol] = _replace_window(
                window,
                snapshot_last_cny=None,
                snapshot_high_cny=None,
                snapshot_low_cny=None,
                snapshot_open_cny=None,
                snapshot_volume=None,
                snapshot_receipt_id="",
                snapshot_source_sha256="",
                snapshot_lineage_sha256="",
                snapshot_data_through="",
                snapshot_observed_at="",
                snapshot_available_at="",
                snapshot_catalog_version="",
            )
            continue
        last = _finite_price(row.get("close") or row.get("last") or row.get("open"))
        volume = _finite_price(row.get("vol") or row.get("volume") or row.get("vol_shares"))
        clock = _parse_quote_clock_time(window.quote_clock_at)
        if last is None or volume is None or clock is None:
            bound[symbol] = _replace_window(
                window,
                snapshot_last_cny=None,
                snapshot_volume=None,
                snapshot_receipt_id="",
            )
            continue
        data_through = clock.isoformat()
        observed = clock.isoformat()
        available = clock.isoformat()
        bound[symbol] = _replace_window(
            window,
            snapshot_last_cny=last,
            snapshot_high_cny=_finite_price(row.get("high")),
            snapshot_low_cny=_finite_price(row.get("low")),
            snapshot_open_cny=_finite_price(row.get("open")),
            snapshot_volume=volume,
            snapshot_receipt_id=snapshot_proof.receipt_id,
            snapshot_source_sha256=snapshot_proof.source_sha256,
            snapshot_lineage_sha256=snapshot_proof.source_lineage_sha256,
            snapshot_data_through=data_through,
            snapshot_observed_at=observed,
            snapshot_available_at=available,
            snapshot_catalog_version=snapshot_proof.catalog_version,
        )
    return bound


def cash_session_daily_ts_codes(symbols: Sequence[str]) -> tuple[str, ...]:
    """Order-preserving security identities eligible for a daily ``ts_code`` filter."""

    seen: list[str] = []
    for symbol in symbols:
        if not looks_like_security_symbol(symbol):
            continue
        normalized = str(symbol).strip().upper()
        if normalized not in seen:
            seen.append(normalized)
    return tuple(seen)


def _daily_query_budget_exceeded(exc: BaseException) -> bool:
    text = str(exc).replace("-", "_").lower()
    return "413" in text or "budget_exceeded" in text


def _query_last_complete_daily_chunk(
    client: Any,
    *,
    schema_major: int,
    last_complete: str,
    codes: Sequence[str],
) -> tuple[Mapping[str, Any], ...]:
    """Query one ``trade_date`` + ``ts_code in`` shard.  Split on 413, never empty-all."""

    from shared.data.sharedsignals_v1 import QueryRequest

    if not codes:
        return ()
    try:
        daily = client.query(
            QueryRequest(
                dataset_id=QUOTE_DATASET_ID,
                schema_major=schema_major,
                filters={
                    "trade_date": {"eq": last_complete},
                    "ts_code": {"in": list(codes)},
                },
                limit=max(len(codes), 1),
            )
        )
    except Exception as exc:
        if _daily_query_budget_exceeded(exc) and len(codes) > 1:
            mid = max(1, len(codes) // 2)
            return _query_last_complete_daily_chunk(
                client,
                schema_major=schema_major,
                last_complete=last_complete,
                codes=codes[:mid],
            ) + _query_last_complete_daily_chunk(
                client,
                schema_major=schema_major,
                last_complete=last_complete,
                codes=codes[mid:],
            )
        raise
    return tuple(row for row in daily.data if isinstance(row, Mapping))


def query_last_complete_daily_rows(
    client: Any,
    *,
    schema_major: int,
    last_complete: str,
    symbols: Sequence[str],
    chunk_size: int = CASH_SESSION_DAILY_TS_CODE_CHUNK,
) -> tuple[Mapping[str, Any], ...]:
    """Fetch last-complete daily rows for candidate codes only.

    An unfiltered ``cn.equity.daily`` partition 413s.  Empty/413 on that
    unfiltered pull must not be rewritten as ``missing_prior_close``.
    """

    if (
        isinstance(chunk_size, bool)
        or not isinstance(chunk_size, int)
        or chunk_size <= 0
    ):
        raise CapitalBackedPaperError("daily_ts_code_chunk_size_invalid")
    codes = cash_session_daily_ts_codes(symbols)
    collected: list[Mapping[str, Any]] = []
    for offset in range(0, len(codes), chunk_size):
        collected.extend(
            _query_last_complete_daily_chunk(
                client,
                schema_major=schema_major,
                last_complete=last_complete,
                codes=codes[offset : offset + chunk_size],
            )
        )
    return tuple(collected)


def _envelope_attr(envelope: Any, name: str) -> object:
    metadata = getattr(envelope, "metadata", None)
    value = getattr(envelope, name, None)
    if value not in (None, ""):
        return value
    if metadata is None:
        return None
    return getattr(metadata, name, None)


def _envelope_query_proof(envelope: Any) -> QuoteClockQueryProof:
    """Bind query-envelope receipt proof.  Missing receipt/SHA stays empty."""

    receipt_id = str(_envelope_attr(envelope, "receipt_id") or "").strip()
    catalog_version = str(getattr(envelope, "catalog_version", "") or "").strip()
    data_through = str(_envelope_attr(envelope, "data_through") or "").strip()
    observed_at = str(_envelope_attr(envelope, "observed_at") or "").strip()
    source = str(_envelope_attr(envelope, "source_sha256") or "").strip().lower()
    lineage_sha = str(
        _envelope_attr(envelope, "source_lineage_sha256")
        or _envelope_attr(envelope, "lineage_sha256")
        or ""
    ).strip().lower()
    lineage = _envelope_attr(envelope, "lineage")
    if not _looks_like_sha256(source):
        source = (
            _sha256(
                {
                    "catalog_version": catalog_version,
                    "dataset_id": QUOTE_CLOCK_DATASET_ID,
                    "receipt_id": receipt_id,
                    "request_id": str(getattr(envelope, "request_id", "") or ""),
                }
            )
            if receipt_id and catalog_version
            else ""
        )
    if not _looks_like_sha256(lineage_sha):
        lineage_sha = (
            _sha256(
                {
                    "dataset_id": QUOTE_CLOCK_DATASET_ID,
                    "lineage": lineage if isinstance(lineage, Mapping) else {},
                    "receipt_id": receipt_id,
                }
            )
            if receipt_id
            else ""
        )
    return QuoteClockQueryProof(
        receipt_id=receipt_id,
        catalog_version=catalog_version,
        data_through=data_through,
        observed_at=observed_at,
        source_sha256=source,
        source_lineage_sha256=lineage_sha,
    )


def _query_quote_clock_chunk(
    client: Any,
    *,
    schema_major: int,
    slot_text: str,
    codes: Sequence[str],
    include_freq: bool,
) -> tuple[tuple[Mapping[str, Any], ...], QuoteClockQueryProof | None]:
    """Query one ``time`` + ``ts_code in`` rt_min shard.  Split on 413."""

    from shared.data.sharedsignals_v1 import QueryRequest

    if not codes:
        return (), None
    filters: dict[str, object] = {
        "time": {"eq": slot_text},
        "ts_code": {"in": list(codes)},
    }
    if include_freq:
        filters["freq"] = {"eq": QUOTE_CLOCK_FREQ}
    try:
        clock = client.query(
            QueryRequest(
                dataset_id=QUOTE_CLOCK_DATASET_ID,
                schema_major=schema_major,
                filters=filters,
                limit=max(len(codes), 1),
            )
        )
    except Exception as exc:
        if _daily_query_budget_exceeded(exc) and len(codes) > 1:
            mid = max(1, len(codes) // 2)
            left_rows, left_proof = _query_quote_clock_chunk(
                client,
                schema_major=schema_major,
                slot_text=slot_text,
                codes=codes[:mid],
                include_freq=include_freq,
            )
            right_rows, right_proof = _query_quote_clock_chunk(
                client,
                schema_major=schema_major,
                slot_text=slot_text,
                codes=codes[mid:],
                include_freq=include_freq,
            )
            return left_rows + right_rows, left_proof or right_proof
        raise
    rows = tuple(row for row in clock.data if isinstance(row, Mapping))
    return rows, _envelope_query_proof(clock)


def query_last_complete_quote_clock_bundle(
    client: Any,
    *,
    schema_major: int,
    slot: datetime,
    symbols: Sequence[str],
    chunk_size: int = CASH_SESSION_QUOTE_CLOCK_CHUNK,
    include_freq: bool = False,
) -> tuple[tuple[Mapping[str, Any], ...], QuoteClockQueryProof | None]:
    """Fetch last-complete rt_min rows plus the query envelope proof."""

    if (
        isinstance(chunk_size, bool)
        or not isinstance(chunk_size, int)
        or chunk_size <= 0
    ):
        raise CapitalBackedPaperError("quote_clock_chunk_size_invalid")
    if slot.tzinfo is None or slot.utcoffset() is None:
        raise CapitalBackedPaperError("quote_clock_slot_timezone_required")
    codes = cash_session_daily_ts_codes(symbols)
    slot_text = slot.astimezone(SHANGHAI).strftime(QUOTE_CLOCK_TIME_FORMAT)
    collected: list[Mapping[str, Any]] = []
    proof: QuoteClockQueryProof | None = None
    for offset in range(0, len(codes), chunk_size):
        rows, chunk_proof = _query_quote_clock_chunk(
            client,
            schema_major=schema_major,
            slot_text=slot_text,
            codes=codes[offset : offset + chunk_size],
            include_freq=include_freq,
        )
        collected.extend(rows)
        if chunk_proof is not None:
            proof = chunk_proof
    return tuple(collected), proof


def query_last_complete_quote_clock_rows(
    client: Any,
    *,
    schema_major: int,
    slot: datetime,
    symbols: Sequence[str],
    chunk_size: int = CASH_SESSION_QUOTE_CLOCK_CHUNK,
    include_freq: bool = False,
) -> tuple[Mapping[str, Any], ...]:
    """Fetch the last complete in-session rt_min slot for candidate codes.

    An unfiltered ``cn.dataset.rt_min`` pull 413s.  Empty/413 must not be
    rewritten as a present clock or as a fill.
    """

    rows, _proof = query_last_complete_quote_clock_bundle(
        client,
        schema_major=schema_major,
        slot=slot,
        symbols=symbols,
        chunk_size=chunk_size,
        include_freq=include_freq,
    )
    return rows


def query_windows_from_tradingdatas(
    symbols: tuple[str, ...],
    *,
    trade_date: str,
    token_file: Path | None = None,
    base_url: str = FORMAL_BASE_URL,
    timeout_seconds: float = 20.0,
    client: Any | None = None,
    decision_as_of: datetime | None = None,
) -> dict[str, SymbolWindow]:
    """Gate symbols on live trade_calendar, last complete daily, and rt_min clocks.

    Cash-session paper observes ``cn.market.trade_calendar`` for the trade_date
    and ``cn.equity.daily`` for the preceding complete partition (calendar
    ``pretrade_date`` / last daily strictly before today).  Daily queries always
    include a ``ts_code`` filter (chunked).  An unfiltered partition pull 413s
    and must not be swallowed into ``missing_prior_close``.  Today's postclose
    daily partition is not an observation requirement.      Daily close/touch never
    becomes a quote clock, a bid/ask snapshot, or a fill.

    In-session quote clocks come from ``cn.dataset.rt_min`` at the last
    completed five-minute session slot at or before ``decision_as_of``.  The
    same last-complete bar may bind volume and query-receipt proof as a
    bar-evidence snapshot; last/close is the bar mid, not bid/ask.  Missing
    clocks stay ``quote_clocks_unavailable``.  Present clocks without a
    snapshot stay ``capital_fill_market_snapshot_unavailable``.  A closed
    calendar stays fail-closed.
    """

    from shared.data.sharedsignals_v1 import (
        QueryRequest,
        SharedSignalsV1Client,
        SharedSignalsV1Config,
    )
    from shared.data.tradingdatas_transport import build_runtime_transport

    if os.environ.get("REAL_TRADING_ENABLED", "false").strip().lower() != "false":
        raise CapitalBackedPaperError("real_trading_must_remain_disabled")

    def _missing(reason_code: str) -> dict[str, SymbolWindow]:
        return {
            symbol: make_missing_window(symbol, reason_code=reason_code)
            for symbol in symbols
        }

    try:
        if client is None:
            if token_file is None:
                raise CapitalBackedPaperError("tradingdatas_token_file_required")
            transport = build_runtime_transport(
                "http-json-v1",
                token_file=token_file,
                base_url=base_url,
            )

            def _make_client(dataset_ids: frozenset[str]) -> Any:
                return SharedSignalsV1Client(
                    SharedSignalsV1Config(
                        base_url=base_url,
                        expected_catalog_version="evidence-only",
                        dataset_ids=dataset_ids,
                        access_policy_id="tradingagent-read-v1",
                        catalog_version_policy="evidence_only",
                        timeout_seconds=timeout_seconds,
                        max_limit=10_000,
                        cache_ttl_seconds=0.0,
                    ),
                    transport=transport,
                )

            clock_capable_ids = frozenset(
                {CALENDAR_DATASET_ID, QUOTE_DATASET_ID, QUOTE_CLOCK_DATASET_ID}
            )
            try:
                client = _make_client(clock_capable_ids)
                catalog = client.get_catalog()
            except Exception:
                client = _make_client(
                    frozenset({CALENDAR_DATASET_ID, QUOTE_DATASET_ID})
                )
                catalog = client.get_catalog()
        else:
            catalog = client.get_catalog()
    except Exception:
        return _missing("tradingdatas_catalog_window_missing")

    catalog_version = str(catalog.catalog_version or "")
    rows_by_id = {
        str(row.get("dataset_id") or ""): row
        for row in catalog.data
        if isinstance(row, Mapping)
    }
    calendar_catalog = rows_by_id.get(CALENDAR_DATASET_ID)
    daily_catalog = rows_by_id.get(QUOTE_DATASET_ID)
    if not catalog_version or calendar_catalog is None or daily_catalog is None:
        return _missing("tradingdatas_catalog_window_missing")

    def _schema_major(row: Mapping[str, Any]) -> int | None:
        value = row.get("schema_major")
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            return None
        return value

    calendar_schema = _schema_major(calendar_catalog)
    daily_schema = _schema_major(daily_catalog)
    if calendar_schema is None or daily_schema is None:
        return _missing("tradingdatas_catalog_window_missing")

    compact_session = _compact_session_date(trade_date)
    try:
        calendar = client.query(
            QueryRequest(
                dataset_id=CALENDAR_DATASET_ID,
                schema_major=calendar_schema,
                filters={
                    "exchange": {"eq": "SSE"},
                    "cal_date": {"eq": compact_session},
                },
                limit=500,
            )
        )
    except Exception:
        return _missing("missing_dataset_catalog_or_session_window")

    calendar_rows = tuple(
        row for row in calendar.data if isinstance(row, Mapping)
    )
    session_row = _calendar_row_for_trade_date(calendar_rows, trade_date)
    if session_row is None or not _calendar_session_open(session_row):
        return bind_cash_session_windows(
            symbols,
            trade_date=trade_date,
            catalog_version=catalog_version,
            calendar_rows=calendar_rows,
            daily_rows=(),
        )

    last_complete = last_complete_daily_date(
        trade_date=trade_date,
        calendar_row=session_row,
    )
    daily_rows: tuple[Mapping[str, Any], ...] = ()
    if last_complete:
        daily_rows = query_last_complete_daily_rows(
            client,
            schema_major=daily_schema,
            last_complete=last_complete,
            symbols=symbols,
        )

    windows = bind_cash_session_windows(
        symbols,
        trade_date=trade_date,
        catalog_version=catalog_version,
        calendar_rows=calendar_rows,
        daily_rows=daily_rows,
        dataset_id=QUOTE_DATASET_ID,
    )
    if decision_as_of is None:
        return windows

    clock_catalog = rows_by_id.get(QUOTE_CLOCK_DATASET_ID)
    clock_schema = _schema_major(clock_catalog) if clock_catalog is not None else None
    slot = last_complete_in_session_quote_slot(
        trade_date=trade_date,
        decision_as_of=decision_as_of,
    )
    configured_ids = getattr(getattr(client, "config", None), "dataset_ids", frozenset())
    if (
        clock_schema is None
        or slot is None
        or QUOTE_CLOCK_DATASET_ID not in configured_ids
    ):
        return bind_quote_clocks(
            windows,
            trade_date=trade_date,
            decision_as_of=decision_as_of,
            quote_clock_rows=(),
            quote_clock_slot=slot,
        )

    clock_rows: tuple[Mapping[str, Any], ...] = ()
    clock_proof: QuoteClockQueryProof | None = None
    try:
        clock_rows, clock_proof = query_last_complete_quote_clock_bundle(
            client,
            schema_major=clock_schema,
            slot=slot,
            symbols=symbols,
        )
    except Exception:
        clock_rows, clock_proof = (), None
    clocked = bind_quote_clocks(
        windows,
        trade_date=trade_date,
        decision_as_of=decision_as_of,
        quote_clock_rows=clock_rows,
        quote_clock_slot=slot,
    )
    return bind_market_snapshots(
        clocked,
        trade_date=trade_date,
        decision_as_of=decision_as_of,
        snapshot_rows=clock_rows,
        snapshot_proof=clock_proof,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="One-shot capital-backed A-share paper session (simulation only)."
    )
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--decision-as-of", required=True)
    parser.add_argument("--ledger-root", required=True, type=Path)
    parser.add_argument("--journal-path", required=True, type=Path)
    parser.add_argument("--latest-path", required=True, type=Path)
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--champion-registry-root", type=Path)
    parser.add_argument("--allow-canonical-root", action="store_true")
    parser.add_argument("--base-url", default=FORMAL_BASE_URL)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    decision_as_of = datetime.fromisoformat(args.decision_as_of.replace("Z", "+00:00"))
    config = CapitalBackedPaperConfig(
        trade_date=args.trade_date,
        decision_as_of=decision_as_of,
        ledger_root=args.ledger_root,
        journal_path=args.journal_path,
        latest_path=args.latest_path,
        allow_canonical_root=bool(args.allow_canonical_root),
    )
    registry = (
        ChampionSelectionRegistry(args.champion_registry_root)
        if args.champion_registry_root is not None
        else None
    )
    window_source = None
    if args.token_file is not None:
        token_file = args.token_file
        base_url = args.base_url

        def _td_windows(symbols: tuple[str, ...]) -> dict[str, SymbolWindow]:
            return query_windows_from_tradingdatas(
                symbols,
                token_file=token_file,
                trade_date=config.trade_date,
                decision_as_of=config.decision_as_of,
                base_url=base_url,
            )

        window_source = _td_windows
    result = run_capital_backed_paper_session(
        config,
        window_source=window_source,
        champion_registry=registry,
    )
    print(
        _canonical_json(
            {
                "run_id": result.run_id,
                "fill_count": result.fill_count,
                "candidate_count": len(result.dispositions),
                "canonical_account_connected": False,
                "real_trading_enabled": False,
            }
        )
    )
    return 0


# Imported by tests that prove the fixture composer stays locked.
_CAPITAL_EXECUTION_PORT = CapitalBackedSimulationExecutionStagePort
_PAPER_CAPITAL_ACCOUNT = PaperCapitalAccount


if __name__ == "__main__":
    raise SystemExit(main())
