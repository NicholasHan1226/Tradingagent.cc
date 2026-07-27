"""Automatic fixture-only five-minute research and paper loop.

The loop connects accepted minute evidence to transparent ranking, t+1-bar
paper settlement, reconciliation and counterfactual ledgers.  It is designed
to be restartable for tests and local fixture runs, but is intentionally not a
durable capital authority, scheduler, broker adapter or production worker.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Mapping

from shared.review.decision_ledger import (
    DecisionExposureRecord,
    ExposureDisposition,
    InMemoryDecisionLedger,
)

from .minute_data import MinuteBarEvidence, MinuteBarSnapshot
from .minute_paper import (
    MinuteDecisionOutcome,
    MinuteExecutionPair,
    MinuteFixtureOrderReceipt,
    MinuteFixturePaperBook,
    MinutePaperContractError,
    MinuteSmallAccountConstraints,
    minute_action_allowed_during_data_failure,
    minute_decision_record,
)
from .minute_research import (
    MODEL_ID,
    MODEL_VERSION,
    MinuteContextObservation,
    MinuteRankedCandidate,
    MinuteResearchContractError,
    MinuteResearchUniverse,
    MinuteRollingFeatureEngine,
    rank_minute_candidates,
)
from shared.universe.policy import is_mainboard_tradable


PRIMARY_SLEEVE = "baseline"
SLEEVE_IDS = ("baseline", "event", "flow", "dynamic_position")


class MinuteLoopContractError(ValueError):
    """Fail-closed fixture-loop error."""


def _canonical_sha256(value: object) -> str:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise MinuteLoopContractError("minute_loop_payload_not_canonical") from exc
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _text(value: object, reason: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise MinuteLoopContractError(reason)
    return value


def _aware(value: object, reason: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise MinuteLoopContractError(reason)
    return value


def _finite(value: object, reason: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise MinuteLoopContractError(reason)
    return float(value)


def _valid_sha256(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


@dataclass(frozen=True)
class MinuteAuxiliaryEvidence:
    """Optional event/flow score used only by a matching shadow sleeve."""

    symbol: str
    evidence_type: str
    normalized_score: float
    event_time: datetime
    available_at: datetime
    decision_time: datetime
    expires_at: datetime
    evidence_sha256: str
    execution_authority: bool = False

    def __post_init__(self) -> None:
        _text(self.symbol, "minute_auxiliary_symbol_invalid")
        if not is_mainboard_tradable(self.symbol):
            raise MinuteLoopContractError("minute_auxiliary_symbol_not_mainboard")
        if self.evidence_type not in {"event", "flow"}:
            raise MinuteLoopContractError("minute_auxiliary_type_invalid")
        score = _finite(self.normalized_score, "minute_auxiliary_score_invalid")
        if not -1.0 <= score <= 1.0:
            raise MinuteLoopContractError("minute_auxiliary_score_out_of_range")
        event = _aware(self.event_time, "minute_auxiliary_event_time_invalid")
        available = _aware(self.available_at, "minute_auxiliary_available_at_invalid")
        decision = _aware(self.decision_time, "minute_auxiliary_decision_time_invalid")
        expires = _aware(self.expires_at, "minute_auxiliary_expires_at_invalid")
        if not event <= available <= decision <= expires:
            raise MinuteLoopContractError("minute_auxiliary_time_order_invalid")
        if not _valid_sha256(self.evidence_sha256):
            raise MinuteLoopContractError("minute_auxiliary_evidence_invalid")
        if self.execution_authority is not False:
            raise MinuteLoopContractError(
                "minute_auxiliary_cannot_have_execution_authority"
            )


@dataclass(frozen=True)
class MinutePendingFixtureOrder:
    sleeve_id: str
    order_id: str
    decision_id: str
    decision_cluster_id: str
    manifest_sha256: str
    symbol: str
    side: str
    requested_quantity: int
    requested_notional_cny: float
    decision_bar: MinuteBarEvidence
    raw_rank_score: float
    created_at: datetime
    authority_tier: str = "non_production_fixture"
    real_trading_enabled: bool = False

    def __post_init__(self) -> None:
        if self.sleeve_id not in SLEEVE_IDS:
            raise MinuteLoopContractError("minute_pending_sleeve_invalid")
        for field_name in (
            "order_id",
            "decision_id",
            "decision_cluster_id",
            "symbol",
            "side",
        ):
            _text(getattr(self, field_name), f"minute_pending_{field_name}_invalid")
        if self.side not in {"buy", "sell"}:
            raise MinuteLoopContractError("minute_pending_side_invalid")
        if not _valid_sha256(self.manifest_sha256):
            raise MinuteLoopContractError("minute_pending_manifest_invalid")
        if (
            isinstance(self.requested_quantity, bool)
            or not isinstance(self.requested_quantity, int)
            or self.requested_quantity <= 0
            or self.requested_quantity % 100
        ):
            raise MinuteLoopContractError("minute_pending_quantity_invalid")
        notional = _finite(
            self.requested_notional_cny, "minute_pending_notional_invalid"
        )
        if notional <= 0:
            raise MinuteLoopContractError("minute_pending_notional_invalid")
        if not isinstance(self.decision_bar, MinuteBarEvidence):
            raise MinuteLoopContractError("minute_pending_decision_bar_invalid")
        if self.decision_bar.symbol != self.symbol:
            raise MinuteLoopContractError("minute_pending_symbol_mismatch")
        _finite(self.raw_rank_score, "minute_pending_score_invalid")
        created = _aware(self.created_at, "minute_pending_created_at_invalid")
        if created < self.decision_bar.available_at:
            raise MinuteLoopContractError("minute_pending_created_before_available")
        if (
            self.authority_tier != "non_production_fixture"
            or self.real_trading_enabled is not False
        ):
            raise MinuteLoopContractError("minute_pending_boundary_invalid")


@dataclass(frozen=True)
class MinuteSleeveStep:
    sleeve_id: str
    settled_receipt: MinuteFixtureOrderReceipt | None
    scheduled_order: MinutePendingFixtureOrder | None
    ranked_count: int
    eligible_count: int
    reconciliation: Mapping[str, Any] | None
    reconciliation_reason: str | None

    def __post_init__(self) -> None:
        if self.sleeve_id not in SLEEVE_IDS:
            raise MinuteLoopContractError("minute_step_sleeve_invalid")
        if self.ranked_count < 0 or self.eligible_count < 0:
            raise MinuteLoopContractError("minute_step_count_invalid")
        if self.eligible_count > self.ranked_count:
            raise MinuteLoopContractError("minute_step_count_invalid")
        if (self.reconciliation is None) == (self.reconciliation_reason is None):
            raise MinuteLoopContractError("minute_step_reconciliation_state_invalid")


@dataclass(frozen=True)
class MinuteClosedLoopStep:
    snapshot_sha256: str
    manifest_sha256: str
    decision_time: datetime
    feature_count: int
    candidate_count: int
    sleeves: tuple[MinuteSleeveStep, ...]
    primary_sleeve: str = PRIMARY_SLEEVE
    fixture_only: bool = True
    durable: bool = False
    real_trading_enabled: bool = False

    def __post_init__(self) -> None:
        if not _valid_sha256(self.snapshot_sha256) or not _valid_sha256(
            self.manifest_sha256
        ):
            raise MinuteLoopContractError("minute_step_hash_invalid")
        _aware(self.decision_time, "minute_step_decision_time_invalid")
        if self.feature_count < 0 or self.candidate_count < 0:
            raise MinuteLoopContractError("minute_step_count_invalid")
        if tuple(item.sleeve_id for item in self.sleeves) != SLEEVE_IDS:
            raise MinuteLoopContractError("minute_step_sleeves_invalid")
        if (
            self.primary_sleeve != PRIMARY_SLEEVE
            or self.fixture_only is not True
            or self.durable is not False
            or self.real_trading_enabled is not False
        ):
            raise MinuteLoopContractError("minute_step_boundary_invalid")


class MinuteCounterfactualBooks:
    """Four isolated 50k fixture books; none is a durable account authority."""

    def __init__(
        self, books: Mapping[str, MinuteFixturePaperBook] | None = None
    ) -> None:
        source = dict(books or {})
        if not source:
            source = {sleeve_id: MinuteFixturePaperBook() for sleeve_id in SLEEVE_IDS}
        if tuple(sorted(source)) != tuple(sorted(SLEEVE_IDS)):
            raise MinuteLoopContractError("minute_counterfactual_sleeves_invalid")
        if any(
            not isinstance(book, MinuteFixturePaperBook) for book in source.values()
        ):
            raise MinuteLoopContractError("minute_counterfactual_book_invalid")
        self._books = source

    def __getitem__(self, sleeve_id: str) -> MinuteFixturePaperBook:
        if sleeve_id not in self._books:
            raise MinuteLoopContractError("minute_counterfactual_sleeve_unknown")
        return self._books[sleeve_id]

    @property
    def books(self) -> Mapping[str, MinuteFixturePaperBook]:
        return dict(self._books)

    def export_state(self) -> dict[str, Any]:
        return {
            sleeve_id: book.export_state()
            for sleeve_id, book in sorted(self._books.items())
        }

    @classmethod
    def restore(cls, state: Mapping[str, Any]) -> "MinuteCounterfactualBooks":
        if not isinstance(state, Mapping):
            raise MinuteLoopContractError("minute_counterfactual_state_invalid")
        try:
            books = {
                str(sleeve_id): MinuteFixturePaperBook.restore(value)
                for sleeve_id, value in state.items()
            }
        except (MinutePaperContractError, TypeError, ValueError) as exc:
            raise MinuteLoopContractError(
                "minute_counterfactual_state_invalid"
            ) from exc
        return cls(books)


def _decision_payload(record: DecisionExposureRecord) -> dict[str, Any]:
    return {
        "decision_id": record.decision_id,
        "decision_cluster_id": record.decision_cluster_id,
        "decision_time": record.decision_time.astimezone(timezone.utc).isoformat(),
        "symbol": record.symbol,
        "model_id": record.model_id,
        "model_version": record.model_version,
        "manifest_sha256": record.manifest_sha256,
        "action": record.action,
        "disposition": record.disposition.value,
        "requested_notional_cny": record.requested_notional_cny,
        "filled_quantity": record.filled_quantity,
        "filled_notional_cny": record.filled_notional_cny,
        "actual_cost_cny": record.actual_cost_cny,
        "simulated_fill_id": record.simulated_fill_id,
        "rejection_reason": record.rejection_reason,
        "nonfill_reason": record.nonfill_reason,
        "capital_layer": record.capital_layer,
        "account_type": record.account_type,
        "real_trading_enabled": record.real_trading_enabled,
        "live_transition_authorized": record.live_transition_authorized,
        "broker_order_id": record.broker_order_id,
    }


def _restore_decision(value: object) -> DecisionExposureRecord:
    if not isinstance(value, Mapping):
        raise MinuteLoopContractError("minute_loop_decision_state_invalid")
    payload = dict(value)
    try:
        payload["decision_time"] = datetime.fromisoformat(
            str(payload["decision_time"]).replace("Z", "+00:00")
        )
        payload["disposition"] = ExposureDisposition(str(payload["disposition"]))
        return DecisionExposureRecord(**payload)
    except (KeyError, TypeError, ValueError) as exc:
        raise MinuteLoopContractError("minute_loop_decision_state_invalid") from exc


def _pending_payload(order: MinutePendingFixtureOrder) -> dict[str, Any]:
    return {
        "sleeve_id": order.sleeve_id,
        "order_id": order.order_id,
        "decision_id": order.decision_id,
        "decision_cluster_id": order.decision_cluster_id,
        "manifest_sha256": order.manifest_sha256,
        "symbol": order.symbol,
        "side": order.side,
        "requested_quantity": order.requested_quantity,
        "requested_notional_cny": order.requested_notional_cny,
        "decision_bar": order.decision_bar.canonical_payload(),
        "raw_rank_score": order.raw_rank_score,
        "created_at": order.created_at.astimezone(timezone.utc).isoformat(),
        "authority_tier": order.authority_tier,
        "real_trading_enabled": order.real_trading_enabled,
    }


def _restore_bar(value: object) -> MinuteBarEvidence:
    return MinuteRollingFeatureEngine._bar_from_payload(value)


def _restore_pending(value: object) -> MinutePendingFixtureOrder:
    if not isinstance(value, Mapping):
        raise MinuteLoopContractError("minute_loop_pending_state_invalid")
    payload = dict(value)
    try:
        payload["decision_bar"] = _restore_bar(payload["decision_bar"])
        payload["created_at"] = datetime.fromisoformat(
            str(payload["created_at"]).replace("Z", "+00:00")
        )
        return MinutePendingFixtureOrder(**payload)
    except (KeyError, TypeError, ValueError, MinuteResearchContractError) as exc:
        raise MinuteLoopContractError("minute_loop_pending_state_invalid") from exc


class MinuteFixtureClosedLoop:
    """Restartable fixture orchestrator with deterministic shadow sleeves."""

    state_schema = "tradingagent.ashare.minute_fixture_closed_loop.v1"

    def __init__(
        self,
        *,
        universe: MinuteResearchUniverse,
        feature_engine: MinuteRollingFeatureEngine | None = None,
        counterfactual_books: MinuteCounterfactualBooks | None = None,
        ledgers: Mapping[str, InMemoryDecisionLedger] | None = None,
        pending: Mapping[str, MinutePendingFixtureOrder] | None = None,
        processed_snapshot_hashes: tuple[str, ...] = (),
        minimum_raw_score: float = 0.0,
        exit_raw_score: float = -0.25,
    ) -> None:
        if not isinstance(universe, MinuteResearchUniverse):
            raise MinuteLoopContractError("minute_loop_universe_required")
        self.universe = universe
        self.feature_engine = feature_engine or MinuteRollingFeatureEngine()
        self.counterfactual_books = counterfactual_books or MinuteCounterfactualBooks()
        source_ledgers = dict(ledgers or {})
        if not source_ledgers:
            source_ledgers = {
                sleeve_id: InMemoryDecisionLedger() for sleeve_id in SLEEVE_IDS
            }
        if tuple(sorted(source_ledgers)) != tuple(sorted(SLEEVE_IDS)) or any(
            not isinstance(value, InMemoryDecisionLedger)
            for value in source_ledgers.values()
        ):
            raise MinuteLoopContractError("minute_loop_ledgers_invalid")
        self._ledgers = source_ledgers
        self._pending = dict(pending or {})
        if any(
            sleeve_id not in SLEEVE_IDS
            or not isinstance(order, MinutePendingFixtureOrder)
            or order.sleeve_id != sleeve_id
            for sleeve_id, order in self._pending.items()
        ):
            raise MinuteLoopContractError("minute_loop_pending_invalid")
        if any(not _valid_sha256(value) for value in processed_snapshot_hashes):
            raise MinuteLoopContractError("minute_loop_processed_hash_invalid")
        if len(set(processed_snapshot_hashes)) != len(processed_snapshot_hashes):
            raise MinuteLoopContractError("minute_loop_processed_hash_duplicate")
        self._processed_snapshot_hashes = list(processed_snapshot_hashes)
        self.minimum_raw_score = _finite(
            minimum_raw_score, "minute_loop_minimum_score_invalid"
        )
        self.exit_raw_score = _finite(exit_raw_score, "minute_loop_exit_score_invalid")
        if self.exit_raw_score >= self.minimum_raw_score:
            raise MinuteLoopContractError("minute_loop_score_band_invalid")

    @property
    def pending(self) -> Mapping[str, MinutePendingFixtureOrder]:
        return dict(self._pending)

    @property
    def ledgers(self) -> Mapping[str, InMemoryDecisionLedger]:
        return dict(self._ledgers)

    def _append_record(
        self,
        *,
        sleeve_id: str,
        decision_id: str,
        cluster_id: str,
        decision_time: datetime,
        symbol: str,
        manifest_sha256: str,
        action: str,
        outcome: MinuteDecisionOutcome,
        requested_notional_cny: float,
        reason_code: str | None = None,
        receipt: MinuteFixtureOrderReceipt | None = None,
    ) -> None:
        record = minute_decision_record(
            decision_id=decision_id,
            decision_cluster_id=cluster_id,
            decision_time=decision_time,
            symbol=symbol,
            model_id=f"{MODEL_ID}:{sleeve_id}",
            model_version=MODEL_VERSION,
            manifest_sha256=manifest_sha256,
            action=action,
            outcome=outcome,
            requested_notional_cny=requested_notional_cny,
            filled_quantity=0 if receipt is None else receipt.filled_quantity,
            filled_notional_cny=0.0 if receipt is None else receipt.notional_cny,
            actual_cost_cny=0.0 if receipt is None else receipt.fee_cny,
            simulated_fill_id=(
                None
                if receipt is None or receipt.filled_quantity == 0
                else f"minute-fixture-fill:{receipt.sha256}"
            ),
            reason_code=reason_code,
        )
        self._ledgers[sleeve_id].append(record)

    def _settle_pending(
        self,
        *,
        sleeve_id: str,
        bars_by_symbol: Mapping[str, MinuteBarEvidence],
    ) -> MinuteFixtureOrderReceipt | None:
        pending = self._pending.get(sleeve_id)
        if pending is None:
            return None
        execution_bar = bars_by_symbol.get(pending.symbol)
        if execution_bar is None:
            self._append_record(
                sleeve_id=sleeve_id,
                decision_id=pending.decision_id,
                cluster_id=pending.decision_cluster_id,
                decision_time=pending.created_at,
                symbol=pending.symbol,
                manifest_sha256=pending.manifest_sha256,
                action=pending.side,
                outcome=MinuteDecisionOutcome.PAPER_NOT_FILLED,
                requested_notional_cny=pending.requested_notional_cny,
                reason_code="minute_execution_bar_missing",
            )
            del self._pending[sleeve_id]
            return None
        try:
            pair = MinuteExecutionPair(pending.decision_bar, execution_bar)
        except MinutePaperContractError:
            self._append_record(
                sleeve_id=sleeve_id,
                decision_id=pending.decision_id,
                cluster_id=pending.decision_cluster_id,
                decision_time=pending.created_at,
                symbol=pending.symbol,
                manifest_sha256=pending.manifest_sha256,
                action=pending.side,
                outcome=MinuteDecisionOutcome.PAPER_NOT_FILLED,
                requested_notional_cny=pending.requested_notional_cny,
                reason_code="minute_execution_not_exact_next_bar",
            )
            del self._pending[sleeve_id]
            return None
        receipt = self.counterfactual_books[sleeve_id].execute(
            order_id=pending.order_id,
            pair=pair,
            side=pending.side,
            requested_quantity=pending.requested_quantity,
        )
        if receipt.status in {"filled", "partial"}:
            outcome = MinuteDecisionOutcome.PAPER_FILLED
            reason_code = None
        elif receipt.status in {"not_filled", "cancelled"}:
            outcome = MinuteDecisionOutcome.PAPER_NOT_FILLED
            reason_code = receipt.reason_code
        elif receipt.reason_code == "minute_insufficient_cash":
            outcome = MinuteDecisionOutcome.INSUFFICIENT_CAPITAL
            reason_code = receipt.reason_code
        else:
            outcome = MinuteDecisionOutcome.MODEL_REJECTED
            reason_code = receipt.reason_code
        self._append_record(
            sleeve_id=sleeve_id,
            decision_id=pending.decision_id,
            cluster_id=pending.decision_cluster_id,
            decision_time=pending.created_at,
            symbol=pending.symbol,
            manifest_sha256=pending.manifest_sha256,
            action=pending.side,
            outcome=outcome,
            requested_notional_cny=pending.requested_notional_cny,
            reason_code=reason_code,
            receipt=receipt if receipt.filled_quantity else None,
        )
        del self._pending[sleeve_id]
        return receipt

    @staticmethod
    def _auxiliary_by_key(
        values: tuple[MinuteAuxiliaryEvidence, ...],
        *,
        decision_time: datetime,
    ) -> Mapping[tuple[str, str], MinuteAuxiliaryEvidence]:
        result: dict[tuple[str, str], MinuteAuxiliaryEvidence] = {}
        for value in values:
            if not isinstance(value, MinuteAuxiliaryEvidence):
                raise MinuteLoopContractError("minute_auxiliary_invalid")
            if value.decision_time > decision_time:
                raise MinuteLoopContractError("minute_auxiliary_future")
            if value.expires_at < decision_time:
                raise MinuteLoopContractError("minute_auxiliary_expired")
            key = (value.evidence_type, value.symbol)
            if key in result:
                raise MinuteLoopContractError("minute_auxiliary_duplicate")
            result[key] = value
        return result

    @staticmethod
    def _sleeve_score(
        sleeve_id: str,
        candidate: MinuteRankedCandidate,
        auxiliary: Mapping[tuple[str, str], MinuteAuxiliaryEvidence],
    ) -> tuple[float | None, str | None]:
        base = candidate.forecast.raw_rank_score
        if sleeve_id in {"baseline", "dynamic_position"}:
            return base, None
        evidence_type = "event" if sleeve_id == "event" else "flow"
        evidence = auxiliary.get((evidence_type, candidate.instrument.symbol))
        if evidence is None:
            return None, f"minute_{evidence_type}_evidence_missing"
        return base + 0.25 * evidence.normalized_score, None

    @staticmethod
    def _quantity(
        *,
        price_cny: float,
        dynamic: bool,
        constraints: MinuteSmallAccountConstraints,
    ) -> int:
        lot_notional = price_cny * 100
        if lot_notional <= 0:
            raise MinuteLoopContractError("minute_quantity_price_invalid")
        lots = max(
            1,
            math.ceil(constraints.policy.minimum_economic_order_cny / lot_notional),
        )
        if dynamic:
            lots += 1
        cap_lots = int(constraints.single_name_cap_cny / lot_notional)
        quantity = min(lots, cap_lots) * 100
        if quantity <= 0:
            raise MinuteLoopContractError("minute_symbol_too_expensive_for_account")
        return quantity

    def _schedule_for_sleeve(
        self,
        *,
        sleeve_id: str,
        ranked: tuple[MinuteRankedCandidate, ...],
        auxiliary: Mapping[tuple[str, str], MinuteAuxiliaryEvidence],
        manifest_sha256: str,
        decision_time: datetime,
        bars_by_symbol: Mapping[str, MinuteBarEvidence],
    ) -> MinutePendingFixtureOrder | None:
        if sleeve_id in self._pending:
            return None
        book = self.counterfactual_books[sleeve_id]
        scored: list[tuple[float, MinuteRankedCandidate]] = []
        missing_auxiliary: list[tuple[MinuteRankedCandidate, str]] = []
        for candidate in ranked:
            score, reason = self._sleeve_score(sleeve_id, candidate, auxiliary)
            if score is None:
                assert reason is not None
                missing_auxiliary.append((candidate, reason))
            else:
                scored.append((score, candidate))
        scored.sort(key=lambda item: (-item[0], item[1].instrument.symbol))
        cluster_id = (
            f"minute-cluster:{manifest_sha256[:12]}:"
            f"{decision_time.astimezone(timezone.utc).isoformat()}:{sleeve_id}"
        )
        for candidate, reason in missing_auxiliary:
            decision_id = (
                f"minute-decision:{sleeve_id}:{candidate.instrument.symbol}:"
                f"{candidate.feature.current_bar_sha256[:16]}:aux-missing"
            )
            self._append_record(
                sleeve_id=sleeve_id,
                decision_id=decision_id,
                cluster_id=cluster_id,
                decision_time=decision_time,
                symbol=candidate.instrument.symbol,
                manifest_sha256=manifest_sha256,
                action="abstain",
                outcome=MinuteDecisionOutcome.MODEL_REJECTED,
                requested_notional_cny=0.0,
                reason_code=reason,
            )
        held_with_scores = [
            (score, candidate)
            for score, candidate in scored
            if candidate.instrument.symbol in book.positions
        ]
        selected: tuple[float, MinuteRankedCandidate, str] | None = None
        if held_with_scores:
            weakest_score, weakest = min(
                held_with_scores, key=lambda item: (item[0], item[1].instrument.symbol)
            )
            if weakest_score <= self.exit_raw_score:
                selected = (weakest_score, weakest, "sell")
        if selected is None:
            for score, candidate in scored:
                if (
                    candidate.eligible
                    and score >= self.minimum_raw_score
                    and candidate.instrument.symbol not in book.positions
                ):
                    selected = (score, candidate, "buy")
                    break
        selected_symbol = None if selected is None else selected[1].instrument.symbol
        for score, candidate in scored:
            if candidate.instrument.symbol == selected_symbol:
                continue
            reason = candidate.reason_code
            if reason is not None:
                outcome = MinuteDecisionOutcome.MODEL_REJECTED
                action = "abstain"
            else:
                outcome = MinuteDecisionOutcome.RANKED_NOT_TRADED
                action = (
                    "hold"
                    if candidate.instrument.symbol in book.positions
                    else "observe"
                )
            decision_id = (
                f"minute-decision:{sleeve_id}:{candidate.instrument.symbol}:"
                f"{candidate.feature.current_bar_sha256[:16]}:{action}"
            )
            self._append_record(
                sleeve_id=sleeve_id,
                decision_id=decision_id,
                cluster_id=cluster_id,
                decision_time=decision_time,
                symbol=candidate.instrument.symbol,
                manifest_sha256=manifest_sha256,
                action=action,
                outcome=outcome,
                requested_notional_cny=0.0,
                reason_code=reason,
            )
        if selected is None:
            return None
        score, candidate, side = selected
        bar = bars_by_symbol[candidate.instrument.symbol]
        if side == "sell":
            position = book.positions[candidate.instrument.symbol]
            quantity = position.quantity
        else:
            quantity = self._quantity(
                price_cny=bar.close_cny,
                dynamic=sleeve_id == "dynamic_position" and score >= 0.5,
                constraints=book.constraints,
            )
        requested_notional = round(quantity * bar.close_cny, 6)
        action = "open" if side == "buy" else "exit"
        decision_id = (
            f"minute-decision:{sleeve_id}:{candidate.instrument.symbol}:"
            f"{candidate.feature.current_bar_sha256[:16]}:{action}"
        )
        order_id = (
            f"minute-order:{_canonical_sha256({'decision_id': decision_id})[:24]}"
        )
        return MinutePendingFixtureOrder(
            sleeve_id=sleeve_id,
            order_id=order_id,
            decision_id=decision_id,
            decision_cluster_id=cluster_id,
            manifest_sha256=manifest_sha256,
            symbol=candidate.instrument.symbol,
            side=side,
            requested_quantity=quantity,
            requested_notional_cny=requested_notional,
            decision_bar=bar,
            raw_rank_score=score,
            created_at=decision_time,
        )

    def process_snapshot(
        self,
        *,
        snapshot: MinuteBarSnapshot,
        manifest_sha256: str,
        contexts: tuple[MinuteContextObservation, ...] = (),
        auxiliary_evidence: tuple[MinuteAuxiliaryEvidence, ...] = (),
    ) -> MinuteClosedLoopStep:
        if not isinstance(snapshot, MinuteBarSnapshot):
            raise MinuteLoopContractError("minute_loop_snapshot_required")
        if not _valid_sha256(manifest_sha256):
            raise MinuteLoopContractError("minute_loop_manifest_invalid")
        if snapshot.sha256 in self._processed_snapshot_hashes:
            raise MinuteLoopContractError("minute_loop_snapshot_already_processed")
        decision_time = max(bar.decision_time for bar in snapshot.bars)
        bars_by_symbol = {bar.symbol: bar for bar in snapshot.bars}
        settled = {
            sleeve_id: self._settle_pending(
                sleeve_id=sleeve_id, bars_by_symbol=bars_by_symbol
            )
            for sleeve_id in SLEEVE_IDS
        }
        auxiliary = self._auxiliary_by_key(
            auxiliary_evidence, decision_time=decision_time
        )
        try:
            features = self.feature_engine.ingest(snapshot, contexts=contexts)
            trade_date = max(bar.bar_end.date() for bar in snapshot.bars)
            ranked = rank_minute_candidates(
                universe=self.universe,
                features=features,
                trade_date=trade_date,
                minimum_raw_score=self.minimum_raw_score,
            )
        except MinuteResearchContractError as exc:
            self.record_data_failure(
                decision_time=decision_time,
                manifest_sha256=manifest_sha256,
                reason_code=str(exc),
            )
            raise MinuteLoopContractError("minute_loop_research_rejected") from exc
        scheduled: dict[str, MinutePendingFixtureOrder | None] = {}
        for sleeve_id in SLEEVE_IDS:
            order = self._schedule_for_sleeve(
                sleeve_id=sleeve_id,
                ranked=ranked,
                auxiliary=auxiliary,
                manifest_sha256=manifest_sha256,
                decision_time=decision_time,
                bars_by_symbol=bars_by_symbol,
            )
            if order is not None:
                self._pending[sleeve_id] = order
            scheduled[sleeve_id] = order
        marks = {
            symbol: bar.close_cny
            for symbol, bar in bars_by_symbol.items()
            if symbol in self.universe.trade_symbols
        }
        sleeve_steps: list[MinuteSleeveStep] = []
        for sleeve_id in SLEEVE_IDS:
            book = self.counterfactual_books[sleeve_id]
            position_marks = {
                symbol: marks[symbol] for symbol in book.positions if symbol in marks
            }
            if set(position_marks) == set(book.positions):
                reconciliation = book.reconcile(marks=position_marks)
                reconciliation_reason = None
            else:
                reconciliation = None
                reconciliation_reason = "minute_reconcile_mark_missing"
            sleeve_steps.append(
                MinuteSleeveStep(
                    sleeve_id=sleeve_id,
                    settled_receipt=settled[sleeve_id],
                    scheduled_order=scheduled[sleeve_id],
                    ranked_count=len(ranked),
                    eligible_count=sum(candidate.eligible for candidate in ranked),
                    reconciliation=reconciliation,
                    reconciliation_reason=reconciliation_reason,
                )
            )
        self._processed_snapshot_hashes.append(snapshot.sha256)
        return MinuteClosedLoopStep(
            snapshot_sha256=snapshot.sha256,
            manifest_sha256=manifest_sha256,
            decision_time=decision_time,
            feature_count=len(features),
            candidate_count=len(ranked),
            sleeves=tuple(sleeve_steps),
        )

    def record_data_failure(
        self,
        *,
        decision_time: datetime,
        manifest_sha256: str,
        reason_code: str,
    ) -> None:
        decision = _aware(decision_time, "minute_loop_decision_time_invalid")
        if not _valid_sha256(manifest_sha256):
            raise MinuteLoopContractError("minute_loop_manifest_invalid")
        reason = _text(reason_code, "minute_loop_data_failure_reason_invalid")
        if minute_action_allowed_during_data_failure("open"):
            raise MinuteLoopContractError("minute_loop_data_failure_boundary_broken")
        for sleeve_id, pending in tuple(self._pending.items()):
            self._append_record(
                sleeve_id=sleeve_id,
                decision_id=pending.decision_id,
                cluster_id=pending.decision_cluster_id,
                decision_time=pending.created_at,
                symbol=pending.symbol,
                manifest_sha256=pending.manifest_sha256,
                action=pending.side,
                outcome=MinuteDecisionOutcome.PAPER_NOT_FILLED,
                requested_notional_cny=pending.requested_notional_cny,
                reason_code=f"minute_data_failure_before_execution:{reason}",
            )
            del self._pending[sleeve_id]
        for sleeve_id in SLEEVE_IDS:
            cluster_id = (
                f"minute-data-failure:{sleeve_id}:"
                f"{decision.astimezone(timezone.utc).isoformat()}"
            )
            for symbol in self.universe.trade_symbols:
                self._append_record(
                    sleeve_id=sleeve_id,
                    decision_id=(
                        f"minute-data-reject:{sleeve_id}:{symbol}:"
                        f"{_canonical_sha256({'time': cluster_id, 'reason': reason})[:20]}"
                    ),
                    cluster_id=cluster_id,
                    decision_time=decision,
                    symbol=symbol,
                    manifest_sha256=manifest_sha256,
                    action="hold",
                    outcome=MinuteDecisionOutcome.DATA_REJECTED,
                    requested_notional_cny=0.0,
                    reason_code=reason,
                )

    def reject_pending_by_human(
        self,
        *,
        sleeve_id: str,
        reason_code: str,
    ) -> None:
        """Audit a manual fixture rejection without creating execution evidence."""

        if sleeve_id not in SLEEVE_IDS:
            raise MinuteLoopContractError("minute_human_reject_sleeve_invalid")
        pending = self._pending.get(sleeve_id)
        if pending is None:
            raise MinuteLoopContractError("minute_human_reject_pending_missing")
        reason = _text(reason_code, "minute_human_reject_reason_invalid")
        self._append_record(
            sleeve_id=sleeve_id,
            decision_id=pending.decision_id,
            cluster_id=pending.decision_cluster_id,
            decision_time=pending.created_at,
            symbol=pending.symbol,
            manifest_sha256=pending.manifest_sha256,
            action=pending.side,
            outcome=MinuteDecisionOutcome.HUMAN_REJECTED,
            requested_notional_cny=pending.requested_notional_cny,
            reason_code=reason,
        )
        del self._pending[sleeve_id]

    def attribution_snapshot(self, *, marks: Mapping[str, float]) -> dict[str, Any]:
        by_sleeve: dict[str, Any] = {}
        for sleeve_id in SLEEVE_IDS:
            book = self.counterfactual_books[sleeve_id]
            sleeve_marks = {
                symbol: marks[symbol] for symbol in book.positions if symbol in marks
            }
            if set(sleeve_marks) != set(book.positions):
                by_sleeve[sleeve_id] = {
                    "eligible": False,
                    "reason": "minute_reconcile_mark_missing",
                }
                continue
            reconciliation = book.reconcile(marks=sleeve_marks)
            by_sleeve[sleeve_id] = {
                "eligible": True,
                "equity_cny": reconciliation["equity_cny"],
                "realized_pnl_cny": reconciliation["realized_pnl_cny"],
                "unrealized_pnl_cny": reconciliation["unrealized_pnl_cny"],
                "position_count": reconciliation["position_count"],
                "decision_count": len(self._ledgers[sleeve_id].records()),
            }
        return {
            "authority_tier": "non_production_fixture",
            "durable": False,
            "real_trading_enabled": False,
            "primary_sleeve": PRIMARY_SLEEVE,
            "sleeves": by_sleeve,
        }

    def export_state(self) -> dict[str, Any]:
        instruments = []
        for instrument in self.universe.instruments.values():
            instruments.append(
                {
                    "symbol": instrument.symbol,
                    "name": instrument.name,
                    "industry": instrument.industry,
                    "research_theme": instrument.research_theme,
                    "list_date": (
                        None
                        if instrument.list_date is None
                        else instrument.list_date.isoformat()
                    ),
                    "risk_warning": instrument.risk_warning,
                    "delisting_risk": instrument.delisting_risk,
                    "context_only": instrument.context_only,
                }
            )
        payload = {
            "schema": self.state_schema,
            "universe": {
                "instruments": sorted(instruments, key=lambda item: item["symbol"]),
                "expanded": self.universe.expanded,
            },
            "feature_engine": self.feature_engine.export_state(),
            "books": self.counterfactual_books.export_state(),
            "ledgers": {
                sleeve_id: [
                    _decision_payload(record)
                    for record in self._ledgers[sleeve_id].records()
                ]
                for sleeve_id in SLEEVE_IDS
            },
            "pending": {
                sleeve_id: _pending_payload(order)
                for sleeve_id, order in sorted(self._pending.items())
            },
            "processed_snapshot_hashes": list(self._processed_snapshot_hashes),
            "minimum_raw_score": self.minimum_raw_score,
            "exit_raw_score": self.exit_raw_score,
            "real_trading_enabled": False,
        }
        return {**payload, "state_sha256": _canonical_sha256(payload)}

    @classmethod
    def restore(cls, state: Mapping[str, Any]) -> "MinuteFixtureClosedLoop":
        if not isinstance(state, Mapping):
            raise MinuteLoopContractError("minute_loop_state_invalid")
        payload = dict(state)
        state_sha = payload.pop("state_sha256", None)
        if (
            payload.get("schema") != cls.state_schema
            or payload.get("real_trading_enabled") is not False
            or state_sha != _canonical_sha256(payload)
        ):
            raise MinuteLoopContractError("minute_loop_state_integrity_failed")
        raw_universe = payload.get("universe")
        raw_ledgers = payload.get("ledgers")
        raw_pending = payload.get("pending")
        if (
            not isinstance(raw_universe, Mapping)
            or not isinstance(raw_ledgers, Mapping)
            or not isinstance(raw_pending, Mapping)
        ):
            raise MinuteLoopContractError("minute_loop_state_invalid")
        if not isinstance(raw_universe.get("expanded"), bool):
            raise MinuteLoopContractError("minute_loop_state_invalid")
        from .minute_research import MinuteUniverseInstrument

        instruments = []
        for value in raw_universe.get("instruments") or []:
            if not isinstance(value, Mapping):
                raise MinuteLoopContractError("minute_loop_state_invalid")
            item = dict(value)
            if item.get("list_date") is not None:
                try:
                    item["list_date"] = date.fromisoformat(str(item["list_date"]))
                except ValueError as exc:
                    raise MinuteLoopContractError("minute_loop_state_invalid") from exc
            instruments.append(MinuteUniverseInstrument(**item))
        universe = MinuteResearchUniverse(
            instruments=tuple(instruments),
            expanded=raw_universe["expanded"],
        )
        ledgers: dict[str, InMemoryDecisionLedger] = {}
        for sleeve_id in SLEEVE_IDS:
            raw_records = raw_ledgers.get(sleeve_id)
            if not isinstance(raw_records, list):
                raise MinuteLoopContractError("minute_loop_state_invalid")
            ledger = InMemoryDecisionLedger()
            for value in raw_records:
                ledger.append(_restore_decision(value))
            ledgers[sleeve_id] = ledger
        pending = {
            str(sleeve_id): _restore_pending(value)
            for sleeve_id, value in raw_pending.items()
        }
        return cls(
            universe=universe,
            feature_engine=MinuteRollingFeatureEngine.restore(
                payload["feature_engine"]
            ),
            counterfactual_books=MinuteCounterfactualBooks.restore(payload["books"]),
            ledgers=ledgers,
            pending=pending,
            processed_snapshot_hashes=tuple(
                payload.get("processed_snapshot_hashes") or ()
            ),
            minimum_raw_score=float(payload["minimum_raw_score"]),
            exit_raw_score=float(payload["exit_raw_score"]),
        )


__all__ = [
    "MinuteAuxiliaryEvidence",
    "MinuteClosedLoopStep",
    "MinuteCounterfactualBooks",
    "MinuteFixtureClosedLoop",
    "MinuteLoopContractError",
    "MinutePendingFixtureOrder",
    "MinuteSleeveStep",
    "PRIMARY_SLEEVE",
    "SLEEVE_IDS",
]
