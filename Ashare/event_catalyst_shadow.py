"""Fixture-first A-share event-catalyst shadow factor.

This module turns a frozen, externally supplied catalyst calendar plus injected
daily bars into deterministic, receipt-bound shadow observations.  It encodes
the front-running hypothesis under study:

* ``front_run`` — the symbol already moved materially *before* a hard-dated
  event.  Moderate runs (3%..10% pre-event) map to ``realize_on_event``;
  extreme runs (>=10%) map to ``reduce_on_event_confirmation``, because the
  offline sample showed extreme anticipation can extend after confirmation.
* ``sell_off`` — the symbol was marked down into the event, so event
  confirmation is treated as a hold-through candidate.
* ``quiet`` — no anticipatory move; no positioning signal.

Everything here is research-only.  The module performs no network, no
persistence, no scheduling and no TradingDatas transport.  All inputs are
explicitly injected, all timestamps are point-in-time checked against the
caller-supplied ``as_of``, and every output carries ``shadow_only=True`` with
no candidate, execution, training, promotion, risk, position, order, or
real-trading authority.  Post-event returns are labels for offline hypothesis
scoring only; they are computed only when the full post-event window is
already observable at ``as_of`` and fail closed otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import hashlib
import json
import math
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from shared.universe.policy import classify_instrument


EVENT_CATALYST_SHADOW_CONTRACT = (
    "tradingagent.ashare.event_catalyst_shadow.v2"
)

EVENT_TYPES = (
    "earnings_disclosure",
    "earnings_preannouncement_window",
    "policy_meeting",
    "industry_conference",
    "product_launch",
    "index_rebalance",
    "lockup_expiry",
    "macro_release",
)
DATE_CONFIDENCE_LEVELS = (
    "hard_date",
    "expected_window",
    "soft_narrative",
)
IMPACT_DIRECTIONS = ("positive", "negative", "unclear")
ANTICIPATION_CLASSES = ("front_run", "sell_off", "quiet")
ANTICIPATION_INTENSITIES = ("moderate", "extreme")
POSITIONING_HYPOTHESES = (
    "realize_on_event",
    "reduce_on_event_confirmation",
    "hold_through_event",
    "no_signal",
)
POST_LABEL_STATES = ("labeled", "pending")

DEFAULT_PRE_WINDOW_SESSIONS = 10
DEFAULT_POST_WINDOW_SESSIONS = 5
FRONT_RUN_THRESHOLD = 0.03
EXTREME_FRONT_RUN_THRESHOLD = 0.10
SELL_OFF_THRESHOLD = -0.03

_SHA256_HEX = frozenset("0123456789abcdef")


class EventCatalystShadowError(ValueError):
    """Fail-closed contract failure with a stable reason code."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise EventCatalystShadowError(
            "event_catalyst_payload_not_canonical"
        ) from exc


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _text(value: object, reason: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\x00" in value
    ):
        raise EventCatalystShadowError(reason)
    return value


def _aware(value: object, reason: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise EventCatalystShadowError(reason)
    return value


def _session_date(value: object, reason: str) -> date:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise EventCatalystShadowError(reason)
    return value


def _positive_price(value: object, reason: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) <= 0
    ):
        raise EventCatalystShadowError(reason)
    return float(value)


def _mainboard_symbol(value: object, reason: str) -> str:
    symbol = _text(value, reason).upper()
    eligibility = classify_instrument(symbol, instrument_type="common_stock")
    if (
        not eligibility.order_identity_allowed
        or eligibility.normalized_symbol != symbol
    ):
        raise EventCatalystShadowError(
            "event_catalyst_symbol_outside_mainboard_scope"
        )
    return symbol


@dataclass(frozen=True)
class DailyBar:
    """One injected daily close; provider rows remain caller-validated."""

    trade_date: date
    close: float

    def __post_init__(self) -> None:
        _session_date(self.trade_date, "event_catalyst_bar_date_invalid")
        _positive_price(self.close, "event_catalyst_bar_close_invalid")


@dataclass(frozen=True)
class CatalystEntry:
    """One frozen catalyst-calendar entry supplied by the caller.

    ``symbol`` is required for instrument-level events and must stay inside the
    mainboard-only research scope; market-wide events instead carry an
    ``entity`` (for example ``CN-MACRO``) and no symbol.
    """

    event_id: str
    event_type: str
    scheduled_date: date
    date_confidence: str
    impact_direction: str
    source_ref: str
    entity: str | None = None
    symbol: str | None = None
    event_cluster_id: str | None = None

    def __post_init__(self) -> None:
        _text(self.event_id, "event_catalyst_event_id_invalid")
        _text(self.source_ref, "event_catalyst_source_ref_invalid")
        if self.event_cluster_id is not None:
            _text(self.event_cluster_id, "event_catalyst_cluster_id_invalid")
        if self.event_type not in EVENT_TYPES:
            raise EventCatalystShadowError("event_catalyst_event_type_invalid")
        if self.date_confidence not in DATE_CONFIDENCE_LEVELS:
            raise EventCatalystShadowError(
                "event_catalyst_date_confidence_invalid"
            )
        if self.impact_direction not in IMPACT_DIRECTIONS:
            raise EventCatalystShadowError(
                "event_catalyst_impact_direction_invalid"
            )
        _session_date(
            self.scheduled_date, "event_catalyst_scheduled_date_invalid"
        )
        if self.symbol is not None:
            object.__setattr__(
                self, "symbol", _mainboard_symbol(self.symbol, "unused")
            )
        elif self.entity is None:
            raise EventCatalystShadowError(
                "event_catalyst_entity_or_symbol_required"
            )
        if self.entity is not None:
            _text(self.entity, "event_catalyst_entity_invalid")

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "scheduled_date": self.scheduled_date.isoformat(),
            "date_confidence": self.date_confidence,
            "impact_direction": self.impact_direction,
            "source_ref": self.source_ref,
            "entity": self.entity,
            "symbol": self.symbol,
            "event_cluster_id": self.event_cluster_id,
        }


def _sorted_bars(
    bars: Sequence[DailyBar],
    *,
    event_id: str,
) -> tuple[DailyBar, ...]:
    if not isinstance(bars, Sequence) or isinstance(bars, (str, bytes)):
        raise EventCatalystShadowError("event_catalyst_bars_invalid")
    ordered = tuple(sorted(bars, key=lambda bar: bar.trade_date))
    seen: set[date] = set()
    for bar in ordered:
        if not isinstance(bar, DailyBar):
            raise EventCatalystShadowError("event_catalyst_bars_invalid")
        if bar.trade_date in seen:
            raise EventCatalystShadowError(
                "event_catalyst_bars_duplicate_session"
            )
        seen.add(bar.trade_date)
    if not ordered:
        raise EventCatalystShadowError("event_catalyst_bars_missing")
    return ordered


def _classify_anticipation(pre_return: float) -> str:
    if pre_return >= FRONT_RUN_THRESHOLD:
        return "front_run"
    if pre_return <= SELL_OFF_THRESHOLD:
        return "sell_off"
    return "quiet"


def _anticipation_intensity(
    anticipation_class: str, pre_return: float
) -> str | None:
    """Split front-running into moderate vs extreme anticipation.

    Offline sample evidence (2025-01..2026-08 mainboard tech, 56 policy-event
    observations) showed moderate front-runs (3%..10% pre-event) reliably
    faded after the event while extreme runs (>=10%) often persisted, so the
    two regimes must not share one exit hypothesis.
    """

    if anticipation_class != "front_run":
        return None
    if pre_return >= EXTREME_FRONT_RUN_THRESHOLD:
        return "extreme"
    return "moderate"


_HYPOTHESIS_BY_CLASS = MappingProxyType(
    {
        "front_run": "realize_on_event",
        "sell_off": "hold_through_event",
        "quiet": "no_signal",
    }
)


def _positioning_hypothesis(
    anticipation_class: str, intensity: str | None
) -> str:
    if anticipation_class == "front_run":
        if intensity == "extreme":
            # Extreme anticipation marks a strong catalyst; confirmation can
            # extend the move, so the hypothesis is a confirmation-gated
            # reduction, never an automatic full exit.
            return "reduce_on_event_confirmation"
        return "realize_on_event"
    return _HYPOTHESIS_BY_CLASS[anticipation_class]


@dataclass(frozen=True)
class CatalystShadowObservation:
    """One deterministic shadow observation; research-only, no authority."""

    event_id: str
    event_type: str
    date_confidence: str
    impact_direction: str
    scheduled_date: date
    symbol: str | None
    entity: str | None
    as_of: datetime
    pre_window_sessions: int
    post_window_sessions: int
    pre_return: float | None
    anticipation_class: str | None
    anticipation_intensity: str | None
    positioning_hypothesis: str | None
    post_return: float | None
    post_label_state: str
    observation_status: str
    input_receipt_sha256: str
    observation_sha256: str
    event_cluster_id: str | None = None
    shadow_only: bool = True
    calibrated_probability: None = None
    candidate_eligible: bool = False
    execution_eligible: bool = False
    training_eligible: bool = False
    promotion_eligible: bool = False
    risk_authority: bool = False
    position_authority: bool = False
    order_authority: bool = False
    real_trading_enabled: bool = False

    def __post_init__(self) -> None:
        _text(self.event_id, "event_catalyst_obs_event_id_invalid")
        if self.event_type not in EVENT_TYPES:
            raise EventCatalystShadowError("event_catalyst_obs_type_invalid")
        if self.date_confidence not in DATE_CONFIDENCE_LEVELS:
            raise EventCatalystShadowError(
                "event_catalyst_obs_confidence_invalid"
            )
        if self.impact_direction not in IMPACT_DIRECTIONS:
            raise EventCatalystShadowError(
                "event_catalyst_obs_direction_invalid"
            )
        _session_date(
            self.scheduled_date, "event_catalyst_obs_scheduled_invalid"
        )
        if self.event_cluster_id is not None:
            _text(
                self.event_cluster_id, "event_catalyst_obs_cluster_id_invalid"
            )
        _aware(self.as_of, "event_catalyst_obs_as_of_invalid")
        for field_name in ("pre_window_sessions", "post_window_sessions"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise EventCatalystShadowError(
                    "event_catalyst_obs_window_invalid"
                )
        if self.observation_status == "observed":
            if (
                not isinstance(self.pre_return, (int, float))
                or isinstance(self.pre_return, bool)
                or not math.isfinite(float(self.pre_return))
            ):
                raise EventCatalystShadowError(
                    "event_catalyst_obs_pre_return_invalid"
                )
            if self.anticipation_class not in ANTICIPATION_CLASSES:
                raise EventCatalystShadowError(
                    "event_catalyst_obs_class_invalid"
                )
            expected_intensity = _anticipation_intensity(
                self.anticipation_class, float(self.pre_return)
            )
            if self.anticipation_intensity != expected_intensity:
                raise EventCatalystShadowError(
                    "event_catalyst_obs_intensity_mismatch"
                )
            if (
                _positioning_hypothesis(
                    self.anticipation_class, self.anticipation_intensity
                )
                != self.positioning_hypothesis
            ):
                raise EventCatalystShadowError(
                    "event_catalyst_obs_hypothesis_mismatch"
                )
        elif self.observation_status == "insufficient_history":
            if (
                self.pre_return is not None
                or self.anticipation_class is not None
                or self.anticipation_intensity is not None
                or self.positioning_hypothesis is not None
            ):
                raise EventCatalystShadowError(
                    "event_catalyst_obs_status_payload_mismatch"
                )
        else:
            raise EventCatalystShadowError(
                "event_catalyst_obs_status_invalid"
            )
        if self.post_label_state not in POST_LABEL_STATES:
            raise EventCatalystShadowError(
                "event_catalyst_obs_label_state_invalid"
            )
        if self.post_label_state == "labeled":
            if (
                not isinstance(self.post_return, (int, float))
                or isinstance(self.post_return, bool)
                or not math.isfinite(float(self.post_return))
            ):
                raise EventCatalystShadowError(
                    "event_catalyst_obs_post_return_invalid"
                )
        elif self.post_return is not None:
            raise EventCatalystShadowError(
                "event_catalyst_obs_label_payload_mismatch"
            )
        for field_name in ("input_receipt_sha256", "observation_sha256"):
            value = _text(
                getattr(self, field_name),
                "event_catalyst_obs_receipt_invalid",
            )
            if len(value) != 64 or any(c not in _SHA256_HEX for c in value):
                raise EventCatalystShadowError(
                    "event_catalyst_obs_receipt_invalid"
                )
        if (
            self.shadow_only is not True
            or self.calibrated_probability is not None
            or any(
                (
                    self.candidate_eligible,
                    self.execution_eligible,
                    self.training_eligible,
                    self.promotion_eligible,
                    self.risk_authority,
                    self.position_authority,
                    self.order_authority,
                    self.real_trading_enabled,
                )
            )
        ):
            raise EventCatalystShadowError(
                "event_catalyst_obs_authority_invalid"
            )


@dataclass(frozen=True)
class CatalystShadowBatch:
    """Content-addressed batch of shadow observations for one as-of."""

    contract: str
    as_of: datetime
    pre_window_sessions: int
    post_window_sessions: int
    observations: tuple[CatalystShadowObservation, ...]
    batch_receipt_sha256: str
    shadow_only: bool = True
    candidate_eligible: bool = False
    execution_eligible: bool = False
    training_eligible: bool = False
    promotion_eligible: bool = False
    real_trading_enabled: bool = False

    def __post_init__(self) -> None:
        if self.contract != EVENT_CATALYST_SHADOW_CONTRACT:
            raise EventCatalystShadowError("event_catalyst_batch_contract_invalid")
        _aware(self.as_of, "event_catalyst_batch_as_of_invalid")
        receipt = _text(
            self.batch_receipt_sha256, "event_catalyst_batch_receipt_invalid"
        )
        if len(receipt) != 64 or any(c not in _SHA256_HEX for c in receipt):
            raise EventCatalystShadowError(
                "event_catalyst_batch_receipt_invalid"
            )
        if (
            self.shadow_only is not True
            or any(
                (
                    self.candidate_eligible,
                    self.execution_eligible,
                    self.training_eligible,
                    self.promotion_eligible,
                    self.real_trading_enabled,
                )
            )
        ):
            raise EventCatalystShadowError(
                "event_catalyst_batch_authority_invalid"
            )


def _observe_one(
    entry: CatalystEntry,
    bars_by_symbol: Mapping[str, Sequence[DailyBar]],
    *,
    as_of: datetime,
    pre_window: int,
    post_window: int,
) -> CatalystShadowObservation:
    if entry.symbol is None:
        # Market-wide events have no instrument series here; they stay as
        # calendar context with insufficient instrument history.
        bars: tuple[DailyBar, ...] | None = None
    else:
        bars = (
            _sorted_bars(bars_by_symbol[entry.symbol], event_id=entry.event_id)
            if entry.symbol in bars_by_symbol
            else None
        )
    if bars is not None:
        for bar in bars:
            if bar.trade_date > as_of.date():
                raise EventCatalystShadowError(
                    "event_catalyst_pit_violation_future_bar"
                )

    input_receipt = _sha256(
        {
            "contract": EVENT_CATALYST_SHADOW_CONTRACT,
            "entry": entry.canonical_payload(),
            "as_of": as_of.isoformat(),
            "pre_window_sessions": pre_window,
            "post_window_sessions": post_window,
            "bars": (
                None
                if bars is None
                else [
                    [bar.trade_date.isoformat(), bar.close] for bar in bars
                ]
            ),
        }
    )

    pre_return: float | None = None
    anticipation_class: str | None = None
    intensity: str | None = None
    hypothesis: str | None = None
    status = "insufficient_history"
    post_return: float | None = None
    post_label_state = "pending"

    if bars is not None:
        event_index = next(
            (
                index
                for index, bar in enumerate(bars)
                if bar.trade_date >= entry.scheduled_date
            ),
            None,
        )
        if event_index is not None and event_index > pre_window:
            pre_return = (
                bars[event_index - 1].close
                / bars[event_index - pre_window - 1].close
                - 1.0
            )
            anticipation_class = _classify_anticipation(pre_return)
            intensity = _anticipation_intensity(anticipation_class, pre_return)
            hypothesis = _positioning_hypothesis(anticipation_class, intensity)
            status = "observed"
            post_end = event_index + post_window
            if post_end < len(bars):
                post_return = (
                    bars[post_end].close / bars[event_index].close - 1.0
                )
                post_label_state = "labeled"

    observation_material = {
        "input_receipt_sha256": input_receipt,
        "observation_status": status,
        "pre_return": pre_return,
        "anticipation_class": anticipation_class,
        "anticipation_intensity": intensity,
        "positioning_hypothesis": hypothesis,
        "post_return": post_return,
        "post_label_state": post_label_state,
        "event_cluster_id": entry.event_cluster_id,
    }
    return CatalystShadowObservation(
        event_id=entry.event_id,
        event_type=entry.event_type,
        date_confidence=entry.date_confidence,
        impact_direction=entry.impact_direction,
        scheduled_date=entry.scheduled_date,
        symbol=entry.symbol,
        entity=entry.entity,
        as_of=as_of,
        pre_window_sessions=pre_window,
        post_window_sessions=post_window,
        pre_return=pre_return,
        anticipation_class=anticipation_class,
        anticipation_intensity=intensity,
        positioning_hypothesis=hypothesis,
        post_return=post_return,
        post_label_state=post_label_state,
        observation_status=status,
        input_receipt_sha256=input_receipt,
        observation_sha256=_sha256(observation_material),
        event_cluster_id=entry.event_cluster_id,
    )


def build_catalyst_shadow_batch(
    entries: Sequence[CatalystEntry],
    bars_by_symbol: Mapping[str, Sequence[DailyBar]],
    *,
    as_of: datetime,
    pre_window_sessions: int = DEFAULT_PRE_WINDOW_SESSIONS,
    post_window_sessions: int = DEFAULT_POST_WINDOW_SESSIONS,
) -> CatalystShadowBatch:
    """Build one deterministic, PIT-checked shadow batch.

    Fail-closed rules: windows must be positive integers; entries must be
    unique by ``event_id``; injected bars may not postdate ``as_of``;
    post-event labels are only produced when the complete post window is
    observable at ``as_of``.
    """

    as_of = _aware(as_of, "event_catalyst_as_of_invalid")
    for field_name, value in (
        ("pre_window_sessions", pre_window_sessions),
        ("post_window_sessions", post_window_sessions),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise EventCatalystShadowError("event_catalyst_window_invalid")
    if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
        raise EventCatalystShadowError("event_catalyst_entries_invalid")
    if not isinstance(bars_by_symbol, Mapping):
        raise EventCatalystShadowError("event_catalyst_bars_map_invalid")
    seen_ids: set[str] = set()
    for entry in entries:
        if not isinstance(entry, CatalystEntry):
            raise EventCatalystShadowError("event_catalyst_entries_invalid")
        if entry.event_id in seen_ids:
            raise EventCatalystShadowError(
                "event_catalyst_event_id_duplicate"
            )
        seen_ids.add(entry.event_id)

    observations = tuple(
        _observe_one(
            entry,
            bars_by_symbol,
            as_of=as_of,
            pre_window=pre_window_sessions,
            post_window=post_window_sessions,
        )
        for entry in entries
    )
    batch_receipt = _sha256(
        {
            "contract": EVENT_CATALYST_SHADOW_CONTRACT,
            "as_of": as_of.isoformat(),
            "pre_window_sessions": pre_window_sessions,
            "post_window_sessions": post_window_sessions,
            "observations": [
                observation.observation_sha256 for observation in observations
            ],
        }
    )
    return CatalystShadowBatch(
        contract=EVENT_CATALYST_SHADOW_CONTRACT,
        as_of=as_of,
        pre_window_sessions=pre_window_sessions,
        post_window_sessions=post_window_sessions,
        observations=observations,
        batch_receipt_sha256=batch_receipt,
    )
