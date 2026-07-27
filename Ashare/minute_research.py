"""Transparent five-minute research baseline for the A-share fixture lane.

This module deliberately stops before claiming predictive probability or
expected return.  It turns a sequence of already accepted ``MinuteBarSnapshot``
objects into deterministic, auditable ranking features.  The score is useful
for exercising the paper loop and collecting labels; it is not calibrated and
is never eligible for model promotion by itself.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from shared.universe.policy import is_mainboard_tradable

from .minute_data import FIVE_MINUTES, MinuteBarEvidence, MinuteBarSnapshot


SCORE_SEMANTICS = "uncalibrated_deterministic_rank_score"
PROBABILITY_MODEL_STATE = "not_calibrated"
MODEL_ID = "ashare-five-minute-transparent-baseline"
MODEL_VERSION = "v1"
ALLOWED_RESEARCH_THEMES = frozenset(
    {
        "ai_semiconductor_infrastructure",
        "robotics_industrial_automation",
        "innovative_medicine",
        "broad_market_control",
    }
)
TRADE_RESEARCH_THEMES = ALLOWED_RESEARCH_THEMES - {"broad_market_control"}
SHANGHAI = ZoneInfo("Asia/Shanghai")


class MinuteResearchContractError(ValueError):
    """Fail-closed research-contract error with a stable reason code."""


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
        raise MinuteResearchContractError(
            "minute_research_payload_not_canonical"
        ) from exc
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _text(value: object, reason: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise MinuteResearchContractError(reason)
    return value


def _aware(value: object, reason: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise MinuteResearchContractError(reason)
    return value


def _finite(value: object, reason: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise MinuteResearchContractError(reason)
    return float(value)


def _expected_successor(value: datetime) -> datetime | None:
    """Return the next supported completed bar end, including the lunch gap."""

    local = value.astimezone(SHANGHAI)
    clock = local.timetz().replace(tzinfo=None)
    if time(9, 35) <= clock < time(11, 30):
        return local + FIVE_MINUTES
    if clock == time(11, 30):
        return local.replace(hour=13, minute=5)
    if time(13, 5) <= clock < time(15, 0):
        return local + FIVE_MINUTES
    return None


@dataclass(frozen=True)
class MinuteUniverseInstrument:
    """TA-owned research routing for one mainboard security or context series."""

    symbol: str
    name: str
    industry: str
    research_theme: str
    list_date: date | None
    risk_warning: bool = False
    delisting_risk: bool = False
    context_only: bool = False

    def __post_init__(self) -> None:
        for field_name in ("symbol", "name", "industry", "research_theme"):
            _text(getattr(self, field_name), f"minute_universe_{field_name}_invalid")
        if self.research_theme not in ALLOWED_RESEARCH_THEMES:
            raise MinuteResearchContractError("minute_universe_theme_not_allowed")
        if not isinstance(self.risk_warning, bool) or not isinstance(
            self.delisting_risk, bool
        ):
            raise MinuteResearchContractError("minute_universe_risk_flag_invalid")
        if not isinstance(self.context_only, bool):
            raise MinuteResearchContractError("minute_universe_context_flag_invalid")
        if self.context_only:
            if self.research_theme != "broad_market_control":
                raise MinuteResearchContractError(
                    "minute_context_must_use_broad_market_control"
                )
            return
        if not is_mainboard_tradable(self.symbol):
            raise MinuteResearchContractError("minute_universe_not_mainboard")
        if not isinstance(self.list_date, date):
            raise MinuteResearchContractError("minute_universe_list_date_required")
        if self.research_theme not in TRADE_RESEARCH_THEMES:
            raise MinuteResearchContractError("minute_trade_theme_not_allowed")

    def eligibility_reason(self, *, trade_date: date) -> str | None:
        if self.context_only:
            return "context_only_not_tradeable"
        if self.risk_warning:
            return "risk_warning_excluded"
        if self.delisting_risk:
            return "delisting_risk_excluded"
        assert self.list_date is not None
        if trade_date - self.list_date < timedelta(days=30):
            return "listed_less_than_30_days"
        return None


@dataclass(frozen=True)
class MinuteContextObservation:
    """Context-only market/industry value; it can adjust a score, never an order."""

    context_id: str
    event_time: datetime
    available_at: datetime
    decision_time: datetime
    expires_at: datetime
    normalized_value: float
    evidence_sha256: str
    context_only: bool = True

    def __post_init__(self) -> None:
        _text(self.context_id, "minute_context_id_invalid")
        event = _aware(self.event_time, "minute_context_event_time_invalid")
        available = _aware(self.available_at, "minute_context_available_at_invalid")
        decision = _aware(self.decision_time, "minute_context_decision_time_invalid")
        expires = _aware(self.expires_at, "minute_context_expires_at_invalid")
        value = _finite(self.normalized_value, "minute_context_value_invalid")
        if not -1.0 <= value <= 1.0:
            raise MinuteResearchContractError("minute_context_value_out_of_range")
        if not event <= available <= decision <= expires:
            raise MinuteResearchContractError("minute_context_time_order_invalid")
        if (
            not isinstance(self.evidence_sha256, str)
            or len(self.evidence_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in self.evidence_sha256
            )
        ):
            raise MinuteResearchContractError("minute_context_evidence_invalid")
        if self.context_only is not True:
            raise MinuteResearchContractError("minute_context_cannot_be_tradeable")


@dataclass(frozen=True)
class MinuteFeatureVector:
    symbol: str
    bar_end: datetime
    previous_bar_sha256: str
    current_bar_sha256: str
    close_to_close_return: float
    intrabar_return: float
    range_ratio: float
    volume_change: float
    amount_change: float
    context_adjustment: float
    raw_rank_score: float
    score_semantics: str = SCORE_SEMANTICS

    def __post_init__(self) -> None:
        if not is_mainboard_tradable(self.symbol):
            raise MinuteResearchContractError("minute_feature_symbol_invalid")
        _aware(self.bar_end, "minute_feature_bar_end_invalid")
        for field_name in ("previous_bar_sha256", "current_bar_sha256"):
            value = getattr(self, field_name)
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise MinuteResearchContractError(
                    f"minute_feature_{field_name}_invalid"
                )
        for field_name in (
            "close_to_close_return",
            "intrabar_return",
            "range_ratio",
            "volume_change",
            "amount_change",
            "context_adjustment",
            "raw_rank_score",
        ):
            _finite(getattr(self, field_name), f"minute_feature_{field_name}_invalid")
        if self.score_semantics != SCORE_SEMANTICS:
            raise MinuteResearchContractError("minute_feature_score_semantics_invalid")

    @property
    def sha256(self) -> str:
        return _canonical_sha256(
            {
                "symbol": self.symbol,
                "bar_end": self.bar_end.astimezone(timezone.utc).isoformat(),
                "previous_bar_sha256": self.previous_bar_sha256,
                "current_bar_sha256": self.current_bar_sha256,
                "close_to_close_return": self.close_to_close_return,
                "intrabar_return": self.intrabar_return,
                "range_ratio": self.range_ratio,
                "volume_change": self.volume_change,
                "amount_change": self.amount_change,
                "context_adjustment": self.context_adjustment,
                "raw_rank_score": self.raw_rank_score,
                "score_semantics": self.score_semantics,
            }
        )


@dataclass(frozen=True)
class MinuteRawForecast:
    """Explicitly uncalibrated research output."""

    symbol: str
    feature_sha256: str
    raw_rank_score: float
    rank: int
    model_id: str = MODEL_ID
    model_version: str = MODEL_VERSION
    score_semantics: str = SCORE_SEMANTICS
    calibrated_probability: None = None
    expected_return_bps: None = None
    probability_model_state: str = PROBABILITY_MODEL_STATE
    promotion_eligible: bool = False
    execution_authority: bool = False

    def __post_init__(self) -> None:
        if not is_mainboard_tradable(self.symbol):
            raise MinuteResearchContractError("minute_forecast_symbol_invalid")
        if (
            not isinstance(self.feature_sha256, str)
            or len(self.feature_sha256) != 64
            or any(
                character not in "0123456789abcdef" for character in self.feature_sha256
            )
        ):
            raise MinuteResearchContractError("minute_forecast_feature_invalid")
        _finite(self.raw_rank_score, "minute_forecast_score_invalid")
        if (
            isinstance(self.rank, bool)
            or not isinstance(self.rank, int)
            or self.rank <= 0
        ):
            raise MinuteResearchContractError("minute_forecast_rank_invalid")
        if (
            self.model_id != MODEL_ID
            or self.model_version != MODEL_VERSION
            or self.score_semantics != SCORE_SEMANTICS
            or self.calibrated_probability is not None
            or self.expected_return_bps is not None
            or self.probability_model_state != PROBABILITY_MODEL_STATE
            or self.promotion_eligible is not False
            or self.execution_authority is not False
        ):
            raise MinuteResearchContractError("minute_forecast_boundary_invalid")


@dataclass(frozen=True)
class MinuteRankedCandidate:
    instrument: MinuteUniverseInstrument
    feature: MinuteFeatureVector
    forecast: MinuteRawForecast
    eligible: bool
    reason_code: str | None

    def __post_init__(self) -> None:
        if self.instrument.context_only:
            raise MinuteResearchContractError("minute_context_cannot_be_candidate")
        if not (
            self.instrument.symbol == self.feature.symbol == self.forecast.symbol
            and self.forecast.feature_sha256 == self.feature.sha256
        ):
            raise MinuteResearchContractError("minute_candidate_binding_mismatch")
        if self.eligible and self.reason_code is not None:
            raise MinuteResearchContractError("minute_candidate_reason_unexpected")
        if not self.eligible and not self.reason_code:
            raise MinuteResearchContractError("minute_candidate_reason_required")


class MinuteResearchUniverse:
    """Small-account monitored universe with context series kept separate."""

    def __init__(
        self,
        *,
        instruments: tuple[MinuteUniverseInstrument, ...],
        initial_monitor_limit: int = 10,
        expanded_monitor_limit: int = 60,
        expanded: bool = False,
    ) -> None:
        if (
            initial_monitor_limit != 10
            or expanded_monitor_limit != 60
            or not isinstance(expanded, bool)
        ):
            raise MinuteResearchContractError("minute_universe_capacity_invalid")
        by_symbol: dict[str, MinuteUniverseInstrument] = {}
        for instrument in instruments:
            if not isinstance(instrument, MinuteUniverseInstrument):
                raise MinuteResearchContractError("minute_universe_instrument_invalid")
            if instrument.symbol in by_symbol:
                raise MinuteResearchContractError("minute_universe_duplicate_symbol")
            by_symbol[instrument.symbol] = instrument
        trade_count = sum(not item.context_only for item in by_symbol.values())
        limit = expanded_monitor_limit if expanded else initial_monitor_limit
        if trade_count > limit:
            raise MinuteResearchContractError("minute_universe_monitor_limit_exceeded")
        self._by_symbol = by_symbol
        self.initial_monitor_limit = initial_monitor_limit
        self.expanded_monitor_limit = expanded_monitor_limit
        self.expanded = expanded

    @property
    def instruments(self) -> Mapping[str, MinuteUniverseInstrument]:
        return dict(self._by_symbol)

    @property
    def trade_symbols(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                symbol
                for symbol, instrument in self._by_symbol.items()
                if not instrument.context_only
            )
        )

    @property
    def context_symbols(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                symbol
                for symbol, instrument in self._by_symbol.items()
                if instrument.context_only
            )
        )


class MinuteRollingFeatureEngine:
    """Process-local rolling state built only from accepted snapshot bars."""

    state_schema = "tradingagent.ashare.minute_feature_state.v1"

    def __init__(self) -> None:
        self._previous: dict[str, MinuteBarEvidence] = {}
        self._current: dict[str, MinuteBarEvidence] = {}
        self._snapshot_hashes: list[str] = []

    @property
    def current_bars(self) -> Mapping[str, MinuteBarEvidence]:
        return dict(self._current)

    def ingest(
        self,
        snapshot: MinuteBarSnapshot,
        *,
        contexts: tuple[MinuteContextObservation, ...] = (),
    ) -> tuple[MinuteFeatureVector, ...]:
        if not isinstance(snapshot, MinuteBarSnapshot):
            raise MinuteResearchContractError("minute_feature_snapshot_required")
        bar_ends = {bar.bar_end for bar in snapshot.bars}
        if len(bar_ends) != 1:
            raise MinuteResearchContractError("minute_snapshot_mixed_bar_end")
        context_adjustment = 0.0
        if contexts:
            decision_time = max(bar.decision_time for bar in snapshot.bars)
            for context in contexts:
                if not isinstance(context, MinuteContextObservation):
                    raise MinuteResearchContractError("minute_context_invalid")
                if context.decision_time > decision_time:
                    raise MinuteResearchContractError("minute_context_future")
                if context.expires_at < decision_time:
                    raise MinuteResearchContractError("minute_context_expired")
            context_adjustment = sum(item.normalized_value for item in contexts) / len(
                contexts
            )
        for bar in snapshot.bars:
            prior_current = self._current.get(bar.symbol)
            if prior_current is not None:
                expected = _expected_successor(prior_current.bar_end)
                if expected != bar.bar_end:
                    raise MinuteResearchContractError(
                        "minute_feature_nonconsecutive_bar"
                    )
        features: list[MinuteFeatureVector] = []
        for bar in sorted(snapshot.bars, key=lambda item: item.symbol):
            prior_current = self._current.get(bar.symbol)
            if prior_current is not None:
                close_return = bar.close_cny / prior_current.close_cny - 1.0
                intrabar_return = bar.close_cny / bar.open_cny - 1.0
                range_ratio = (bar.high_cny - bar.low_cny) / prior_current.close_cny
                volume_change = bar.volume_shares / prior_current.volume_shares - 1.0
                amount_change = bar.amount_cny / prior_current.amount_cny - 1.0
                bounded_volume = max(-2.0, min(2.0, volume_change))
                raw_score = 100.0 * (
                    0.45 * close_return
                    + 0.30 * intrabar_return
                    - 0.15 * range_ratio
                    + 0.05 * bounded_volume
                    + 0.05 * context_adjustment
                )
                features.append(
                    MinuteFeatureVector(
                        symbol=bar.symbol,
                        bar_end=bar.bar_end,
                        previous_bar_sha256=prior_current.sha256,
                        current_bar_sha256=bar.sha256,
                        close_to_close_return=round(close_return, 10),
                        intrabar_return=round(intrabar_return, 10),
                        range_ratio=round(range_ratio, 10),
                        volume_change=round(volume_change, 10),
                        amount_change=round(amount_change, 10),
                        context_adjustment=round(context_adjustment, 10),
                        raw_rank_score=round(raw_score, 10),
                    )
                )
            self._previous[bar.symbol] = prior_current or bar
            self._current[bar.symbol] = bar
        self._snapshot_hashes.append(snapshot.sha256)
        return tuple(features)

    def export_state(self) -> dict[str, Any]:
        payload = {
            "schema": self.state_schema,
            "current": {
                symbol: bar.canonical_payload()
                for symbol, bar in sorted(self._current.items())
            },
            "previous": {
                symbol: bar.canonical_payload()
                for symbol, bar in sorted(self._previous.items())
            },
            "snapshot_hashes": list(self._snapshot_hashes),
            "real_trading_enabled": False,
        }
        return {**payload, "state_sha256": _canonical_sha256(payload)}

    @staticmethod
    def _bar_from_payload(value: object) -> MinuteBarEvidence:
        if not isinstance(value, Mapping):
            raise MinuteResearchContractError("minute_feature_state_bar_invalid")
        payload = dict(value)
        for field_name in (
            "bar_start",
            "bar_end",
            "data_through",
            "observed_at",
            "available_at",
            "decision_time",
        ):
            raw = payload.get(field_name)
            if not isinstance(raw, str):
                raise MinuteResearchContractError("minute_feature_state_bar_invalid")
            try:
                payload[field_name] = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError as exc:
                raise MinuteResearchContractError(
                    "minute_feature_state_bar_invalid"
                ) from exc
        try:
            return MinuteBarEvidence(**payload)
        except (TypeError, ValueError) as exc:
            raise MinuteResearchContractError(
                "minute_feature_state_bar_invalid"
            ) from exc

    @classmethod
    def restore(cls, state: Mapping[str, Any]) -> "MinuteRollingFeatureEngine":
        if not isinstance(state, Mapping):
            raise MinuteResearchContractError("minute_feature_state_invalid")
        payload = dict(state)
        state_sha = payload.pop("state_sha256", None)
        if (
            payload.get("schema") != cls.state_schema
            or payload.get("real_trading_enabled") is not False
            or state_sha != _canonical_sha256(payload)
        ):
            raise MinuteResearchContractError("minute_feature_state_integrity_failed")
        raw_current = payload.get("current")
        raw_previous = payload.get("previous")
        raw_hashes = payload.get("snapshot_hashes")
        if (
            not isinstance(raw_current, Mapping)
            or not isinstance(raw_previous, Mapping)
            or not isinstance(raw_hashes, list)
        ):
            raise MinuteResearchContractError("minute_feature_state_invalid")
        restored = cls()
        restored._current = {
            str(symbol): cls._bar_from_payload(value)
            for symbol, value in raw_current.items()
        }
        restored._previous = {
            str(symbol): cls._bar_from_payload(value)
            for symbol, value in raw_previous.items()
        }
        if any(
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
            for value in raw_hashes
        ):
            raise MinuteResearchContractError("minute_feature_state_invalid")
        restored._snapshot_hashes = list(raw_hashes)
        if set(restored._current) != set(restored._previous):
            raise MinuteResearchContractError("minute_feature_state_symbol_mismatch")
        return restored


def rank_minute_candidates(
    *,
    universe: MinuteResearchUniverse,
    features: tuple[MinuteFeatureVector, ...],
    trade_date: date,
    minimum_raw_score: float = 0.0,
) -> tuple[MinuteRankedCandidate, ...]:
    """Rank eligible research candidates without manufacturing probability."""

    if not isinstance(universe, MinuteResearchUniverse):
        raise MinuteResearchContractError("minute_universe_required")
    threshold = _finite(minimum_raw_score, "minute_score_threshold_invalid")
    feature_by_symbol: dict[str, MinuteFeatureVector] = {}
    for feature in features:
        if not isinstance(feature, MinuteFeatureVector):
            raise MinuteResearchContractError("minute_feature_invalid")
        if feature.symbol in feature_by_symbol:
            raise MinuteResearchContractError("minute_duplicate_feature")
        feature_by_symbol[feature.symbol] = feature
    ranked_features = sorted(
        feature_by_symbol.values(),
        key=lambda item: (-item.raw_rank_score, item.symbol),
    )
    result: list[MinuteRankedCandidate] = []
    for rank, feature in enumerate(ranked_features, start=1):
        instrument = universe.instruments.get(feature.symbol)
        if instrument is None:
            continue
        reason = instrument.eligibility_reason(trade_date=trade_date)
        if reason is None and feature.raw_rank_score < threshold:
            reason = "raw_score_below_experimental_threshold"
        forecast = MinuteRawForecast(
            symbol=feature.symbol,
            feature_sha256=feature.sha256,
            raw_rank_score=feature.raw_rank_score,
            rank=rank,
        )
        result.append(
            MinuteRankedCandidate(
                instrument=instrument,
                feature=feature,
                forecast=forecast,
                eligible=reason is None,
                reason_code=reason,
            )
        )
    return tuple(result)


__all__ = [
    "ALLOWED_RESEARCH_THEMES",
    "MODEL_ID",
    "MODEL_VERSION",
    "MinuteContextObservation",
    "MinuteFeatureVector",
    "MinuteRankedCandidate",
    "MinuteRawForecast",
    "MinuteResearchContractError",
    "MinuteResearchUniverse",
    "MinuteRollingFeatureEngine",
    "MinuteUniverseInstrument",
    "PROBABILITY_MODEL_STATE",
    "SCORE_SEMANTICS",
    "TRADE_RESEARCH_THEMES",
    "rank_minute_candidates",
]
