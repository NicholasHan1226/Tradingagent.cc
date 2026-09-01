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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Sequence
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from shared.capital.market_ledger import MarketCapitalLedger, MarketCapitalSnapshot
from shared.capital.market_policy import MarketPolicy
from shared.execution.execution_lineage import ASHARE_CAPITAL_AUTHORITY_ID
from shared.models.champion_registry import (
    ChampionRegistryError,
    ChampionSelectionReceipt,
    ChampionSelectionRegistry,
)
from shared.review.decision_ledger import (
    DecisionExposureRecord,
    ExposureDisposition,
    SampleJournalDecisionLedger,
)
from shared.review.sample_journal import SampleJournal
from shared.runtime.capital_stages import (
    CapitalBackedSimulationExecutionStagePort,
    PaperCapitalAccount,
)
from shared.universe.policy import CanonicalMainboardScopePolicy

from .capital_backed_paper_universe import (
    FROZEN_UNIVERSE_SHA256,
    SessionSymbolClassification,
    classify_session_universe,
    looks_like_security_symbol,
)
from .minute_paper import MinuteFixturePaperBook


SHANGHAI = ZoneInfo("Asia/Shanghai")
CANONICAL_DATA_ROOT = Path("/var/lib/tradingagent/ashare-canonical")
CANONICAL_LEDGER_ROOT = CANONICAL_DATA_ROOT / "shared" / "logs" / "capital" / "ashare"
RUNNER_CONTRACT_ID = "tradingagent.ashare.capital_backed_paper_session.v1"
QUOTE_DATASET_ID = "cn.equity.daily"
CALENDAR_DATASET_ID = "cn.market.trade_calendar"
FORMAL_BASE_URL = "http://127.0.0.1:18082"
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


@dataclass(frozen=True)
class FillAttemptRequest:
    """Evidence required before the runner will call a capital fill port."""

    symbol: str
    quantity: int
    prior_close_cny: float
    champion: ChampionSelectionReceipt
    window: SymbolWindow
    snapshot_before: MarketCapitalSnapshot


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


def _load_champion(
    registry: ChampionSelectionRegistry | None,
) -> ChampionSelectionReceipt | None:
    if registry is None:
        return None
    try:
        current = registry.load_current()
    except ChampionRegistryError:
        return None
    if (
        current.real_trading_enabled is not False
        or current.live_transition_authorized is not False
        or current.automatic_risk_expansion_enabled is not False
        or current.capital_layer != "simulated"
        or current.account_type != "simulated"
        or current.simulation_only is not True
    ):
        raise CapitalBackedPaperError("champion_current_not_simulation_only")
    return current


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
    drift_ok: bool = False,
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
            fill_attempt is not None
            and classification.order_identity_allowed
            and champion is not None
            and drift_ok
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
                fill_result = fill_attempt(
                    FillAttemptRequest(
                        symbol=classification.symbol,
                        quantity=BUY_LOT,
                        prior_close_cny=float(window.prior_close_cny or 0.0),
                        champion=champion,
                        window=window,
                        snapshot_before=ledger_cursor,
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
            drift_ok=drift_ok,
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
    calendar stays fail-closed.  Daily close/touch is prior-close evidence only.
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
            quote_clocks_ok=False,
            reason_code=(
                "window_ready" if prior_close and prior_close > 0 else "missing_prior_close"
            ),
            quote_trade_date=last_complete
            or _compact_session_date(row.get("trade_date")),
        )
    return windows


def query_windows_from_tradingdatas(
    symbols: tuple[str, ...],
    *,
    trade_date: str,
    token_file: Path | None = None,
    base_url: str = FORMAL_BASE_URL,
    timeout_seconds: float = 20.0,
    client: Any | None = None,
) -> dict[str, SymbolWindow]:
    """Gate symbols on live trade_calendar plus last complete daily.

    Cash-session paper observes ``cn.market.trade_calendar`` for the trade_date
    and ``cn.equity.daily`` for the preceding complete partition (calendar
    ``pretrade_date`` / last daily strictly before today).  Today's postclose
    daily partition is not an observation requirement.  Daily close/touch never
    becomes a fill.  A closed calendar stays fail-closed.
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
            client = SharedSignalsV1Client(
                SharedSignalsV1Config(
                    base_url=base_url,
                    expected_catalog_version="evidence-only",
                    dataset_ids=frozenset({CALENDAR_DATASET_ID, QUOTE_DATASET_ID}),
                    access_policy_id="tradingagent-read-v1",
                    catalog_version_policy="evidence_only",
                    timeout_seconds=timeout_seconds,
                    max_limit=10_000,
                    cache_ttl_seconds=0.0,
                ),
                transport=transport,
            )
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
        try:
            daily = client.query(
                QueryRequest(
                    dataset_id=QUOTE_DATASET_ID,
                    schema_major=daily_schema,
                    filters={"trade_date": {"eq": last_complete}},
                    limit=10_000,
                )
            )
            daily_rows = tuple(
                row for row in daily.data if isinstance(row, Mapping)
            )
        except Exception:
            daily_rows = ()

    return bind_cash_session_windows(
        symbols,
        trade_date=trade_date,
        catalog_version=catalog_version,
        calendar_rows=calendar_rows,
        daily_rows=daily_rows,
        dataset_id=QUOTE_DATASET_ID,
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
                base_url=base_url,
            )

        window_source = _td_windows
    result = run_capital_backed_paper_session(
        config,
        window_source=window_source,
        champion_registry=registry,
        drift_ok=False,
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
