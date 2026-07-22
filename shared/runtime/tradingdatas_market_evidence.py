"""Pure read adapter from a frozen TradingDatas observation to A-share marks.

The adapter has no endpoint, client, provider, socket, broker, or implicit
fallback.  A daily close may become a valuation mark only for an explicitly
named prior valuation session.  Daily rows can never become execution
evidence.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import NoReturn

from shared.data.research_snapshot import ResearchDataSnapshot, ResearchDatasetSnapshot
from shared.runtime.ashare_observation_ledger import (
    OBSERVED_REASON_CODE,
    AshareObservationMembershipArtifact,
)
from shared.runtime.ashare_runtime_ports import (
    AshareRuntimeAuthorityLoadBlocked,
    load_verified_ashare_runtime_authority_bundle,
)
from shared.runtime.market_evidence_authority import (
    AShareMarkEvidence,
    MarketEvidenceAuthorityError,
    MarketEvidenceContext,
    MarketSourceBinding,
)
from shared.universe.policy import is_mainboard_tradable


_SHA256_CHARS = frozenset("0123456789abcdef")


class TradingDatasMarketEvidenceBlocked(RuntimeError):
    """Controlled refusal to promote frozen data into market evidence."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


def _blocked(reason_code: str) -> NoReturn:
    raise TradingDatasMarketEvidenceBlocked(reason_code)


def _canonical_sha256(value: object) -> str:
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise TradingDatasMarketEvidenceBlocked(
            "frozen_observation_receipt_invalid"
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


def _sha256(value: object, *, reason_code: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in _SHA256_CHARS for character in value)
    ):
        _blocked(reason_code)
    return value


def _text(value: object, *, reason_code: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        _blocked(reason_code)
    return value


def _instant(value: object, *, reason_code: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value and value == value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise TradingDatasMarketEvidenceBlocked(reason_code) from exc
    else:
        _blocked(reason_code)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _blocked(reason_code)
    return parsed.astimezone(timezone.utc)


def _instant_text(value: object, *, reason_code: str) -> str:
    return _instant(value, reason_code=reason_code).isoformat()


def _absolute_path(value: object, *, reason_code: str) -> Path:
    if not isinstance(value, (str, os.PathLike)) or not os.fspath(value):
        _blocked(reason_code)
    candidate = Path(os.fspath(value))
    if not candidate.is_absolute() or ".." in candidate.parts:
        _blocked(reason_code)
    return Path(os.path.abspath(os.fspath(candidate)))


def _daily_dataset(
    snapshot: ResearchDataSnapshot,
    *,
    dataset_id: str,
) -> ResearchDatasetSnapshot:
    matches = [item for item in snapshot.datasets if item.dataset_id == dataset_id]
    if len(matches) != 1:
        _blocked("daily_market_dataset_unavailable")
    daily = matches[0]
    if (
        daily.catalog_version != snapshot.catalog_version
        or daily.role != "required_execution"
        or daily.evidence_action != "accept"
        or daily.eligible is not True
        or daily.weight != 1.0
        or daily.evidence_state not in {"ready", "healthy", "ok", "available"}
        or daily.reasons
        or daily.next_cursor is not None
        or daily.observation_mode != "current_observation"
        or daily.historical_pit_eligible is not False
    ):
        _blocked("daily_market_dataset_ineligible")
    if (
        daily.source_proof_complete is not True
        or not isinstance(daily.receipt_id, str)
        or not daily.receipt_id
        or daily.data_through is None
        or daily.observed_at is None
    ):
        _blocked("daily_market_source_proof_incomplete")
    _sha256(
        daily.source_proof_sha256,
        reason_code="daily_market_source_proof_incomplete",
    )
    _sha256(
        daily.lineage_sha256,
        reason_code="daily_market_source_proof_incomplete",
    )
    _sha256(
        daily.response_sha256,
        reason_code="daily_market_source_proof_incomplete",
    )
    if (
        daily.row_event_time_field != "trade_date"
        or daily.row_event_time_format != "yyyymmdd"
        or daily.row_event_time_semantic != "session"
        or not {"ts_code", "trade_date"}.issubset(daily.identity_fields)
    ):
        _blocked("daily_market_dataset_contract_invalid")
    data_through = _instant(
        daily.data_through,
        reason_code="daily_market_source_proof_incomplete",
    )
    observed_at = _instant(
        daily.observed_at,
        reason_code="daily_market_source_proof_incomplete",
    )
    if data_through > observed_at:
        _blocked("daily_market_source_proof_incomplete")
    return daily


class TradingDatasDailyMarketEvidenceAdapter:
    """Load one immutable observation and expose valuation-only daily marks."""

    def __init__(
        self,
        *,
        state_root: Path | str,
        expected_profile_id: str,
        expected_catalog_version: str,
        expected_observation_as_of: datetime | str,
        manifest_as_of: str,
        manifest_sha256: str,
        schema_major: int,
        daily_dataset_id: str,
    ) -> None:
        expected_profile = _text(
            expected_profile_id,
            reason_code="expected_observation_profile_invalid",
        )
        expected_catalog = _text(
            expected_catalog_version,
            reason_code="expected_observation_catalog_invalid",
        )
        expected_as_of = _instant_text(
            expected_observation_as_of,
            reason_code="expected_observation_as_of_invalid",
        )
        manifest_as_of_text = _text(
            manifest_as_of,
            reason_code="manifest_as_of_invalid",
        )
        root = _absolute_path(
            state_root,
            reason_code="frozen_observation_snapshot_root_invalid",
        )
        try:
            runtime_authorities = load_verified_ashare_runtime_authority_bundle(
                state_root=root,
                profile_id=expected_profile,
                catalog_version=expected_catalog,
                decision_as_of=expected_as_of,
                manifest_as_of=manifest_as_of_text,
                manifest_sha256=manifest_sha256,
                schema_major=schema_major,
            )
        except AshareRuntimeAuthorityLoadBlocked as exc:
            raise TradingDatasMarketEvidenceBlocked(exc.reason_code) from exc
        if (
            _instant_text(
                runtime_authorities.decision_as_of,
                reason_code="runtime_authority_decision_as_of_invalid",
            )
            != expected_as_of
        ):
            _blocked("expected_observation_as_of_mismatch")
        snapshot = runtime_authorities.research_snapshot
        membership = runtime_authorities.observation_membership
        if type(snapshot) is not ResearchDataSnapshot:
            _blocked("runtime_authority_snapshot_mismatch")
        if type(membership) is not AshareObservationMembershipArtifact:
            _blocked("runtime_authority_observation_membership_invalid")
        if (
            snapshot.execution_eligible is not True
            or snapshot.historical_pit_eligible is not False
            or snapshot.blocking_reasons
        ):
            _blocked("frozen_observation_snapshot_ineligible")

        dataset_id = _text(
            daily_dataset_id,
            reason_code="daily_market_dataset_id_invalid",
        )
        daily = _daily_dataset(snapshot, dataset_id=dataset_id)
        transaction_identity = _canonical_sha256(
            {
                "profile_id": expected_profile,
                "catalog_version": expected_catalog,
                "as_of": manifest_as_of_text,
                "manifest_sha256": manifest_sha256,
            }
        )
        self.snapshot_root = root
        self.observation_receipt_path = root / (
            f"observation-{transaction_identity}.json"
        )
        self.runtime_authorities = runtime_authorities
        self.snapshot = snapshot
        self.observation_membership_artifact = membership
        self.daily_dataset_id = dataset_id
        self._daily = daily
        self._membership_by_symbol = {
            record.symbol: record for record in membership.records
        }
        self._observation_available_at = _instant(
            snapshot.decision_as_of,
            reason_code="frozen_observation_snapshot_invalid",
        )

    def previous_session_mark(
        self,
        *,
        symbol: str,
        valuation_session: date,
        context: MarketEvidenceContext,
        session_calendar_receipt_sha256: str,
    ) -> AShareMarkEvidence:
        """Return a prior-session close mark, never an execution quote."""

        if type(context) is not MarketEvidenceContext:
            _blocked("market_evidence_context_required")
        if not is_mainboard_tradable(
            symbol,
            instrument_type="common_stock",
        ):
            _blocked("previous_session_symbol_out_of_scope")
        membership = self._membership_by_symbol.get(symbol)
        if (
            membership is None
            or membership.disposition != "observed"
            or membership.reason_code != OBSERVED_REASON_CODE
        ):
            _blocked("previous_session_symbol_not_observed")
        if not isinstance(valuation_session, date) or isinstance(
            valuation_session,
            datetime,
        ):
            _blocked("valuation_session_invalid")
        if valuation_session >= context.trade_date:
            _blocked("previous_valuation_session_must_precede_trade_date")
        _sha256(
            session_calendar_receipt_sha256,
            reason_code="session_calendar_receipt_sha256_invalid",
        )

        expected_trade_date = valuation_session.strftime("%Y%m%d")
        matches = [
            row
            for row in self._daily.decoded_rows()
            if row.get("ts_code") == symbol
            and row.get("trade_date") == expected_trade_date
        ]
        if len(matches) != 1:
            _blocked("previous_session_daily_mark_unavailable")
        row = matches[0]
        close = row.get("close")
        volume = row.get("vol")
        if (
            isinstance(close, bool)
            or not isinstance(close, (int, float))
            or not math.isfinite(float(close))
            or float(close) <= 0.0
            or isinstance(volume, bool)
            or not isinstance(volume, (int, float))
            or not math.isfinite(float(volume))
            or float(volume) <= 0.0
        ):
            _blocked("previous_session_daily_mark_invalid")

        data_through = _instant(
            self._daily.data_through,
            reason_code="daily_market_source_proof_incomplete",
        )
        observed_at = _instant(
            self._daily.observed_at,
            reason_code="daily_market_source_proof_incomplete",
        )
        committed_lineage_sha256 = _canonical_sha256(
            {
                "schema_id": (
                    "tradingagent.ashare.committed-observation-market-source.v1"
                ),
                "daily_lineage_sha256": self._daily.lineage_sha256,
                "observation_membership_sha256": (
                    self.runtime_authorities.observation_membership_sha256
                ),
                "observation_transaction_complete_sha256": (
                    self.runtime_authorities.observation_transaction_complete_sha256
                ),
            }
        )
        try:
            source = MarketSourceBinding(
                dataset_id=self._daily.dataset_id,
                catalog_version=self._daily.catalog_version,
                source_receipt_id=str(self._daily.receipt_id),
                source_receipt_sha256=self._daily.response_sha256,
                source_lineage_sha256=committed_lineage_sha256,
                data_through=data_through,
                observed_at=observed_at,
                available_at=self._observation_available_at,
            )
            return AShareMarkEvidence(
                symbol=symbol,
                price_cny=float(close),
                market_session="close",
                source=source,
                session_calendar_receipt_sha256=session_calendar_receipt_sha256,
                context=context,
            )
        except MarketEvidenceAuthorityError as exc:
            raise TradingDatasMarketEvidenceBlocked(
                "previous_session_daily_mark_invalid"
            ) from exc

    def execution_quote_evidence(
        self,
        *_args: object,
        **_kwargs: object,
    ) -> NoReturn:
        """Daily data has no executable quote authority."""

        _blocked("minute_execution_evidence_unavailable")

    def execution_bar_evidence(
        self,
        *_args: object,
        **_kwargs: object,
    ) -> NoReturn:
        """No minute bar has been frozen by this daily-only adapter."""

        _blocked("minute_execution_evidence_unavailable")


__all__ = [
    "TradingDatasDailyMarketEvidenceAdapter",
    "TradingDatasMarketEvidenceBlocked",
]
