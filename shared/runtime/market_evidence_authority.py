"""Provider-neutral authority contract for A-share mark and quote evidence.

This module deliberately contains no production verifier and no network path.
The only concrete verifier is an exact, non-subclassable fixture verifier that
accepts a frozen allowlist of complete evidence content hashes.  It can prove
local contract binding, but it can never become production-eligible.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Mapping


class MarketEvidenceAuthorityError(ValueError):
    """Raised when market evidence cannot prove its immutable authority."""


_SHA256_HEX = frozenset("0123456789abcdef")
_SYMBOL_PATTERN = re.compile(r"^[0-9]{6}\.(?:SH|SZ)$")
_MARK_SCHEMA = "tradingagent.ashare_mark_evidence.v1"
_QUOTE_SCHEMA = "tradingagent.ashare_execution_quote_evidence.v1"
_SOURCE_SCHEMA = "tradingagent.market_source_binding.v1"
_CONTEXT_SCHEMA = "tradingagent.market_evidence_context.v1"
_VERIFICATION_SCHEMA = "tradingagent.market_evidence_verification.v1"


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise MarketEvidenceAuthorityError(f"{field_name}_invalid")
    return value


def _sha256_text(value: object, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in _SHA256_HEX for character in value)
    ):
        raise MarketEvidenceAuthorityError(f"{field_name}_invalid")
    return value


def _aware(value: object, field_name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise MarketEvidenceAuthorityError(f"{field_name}_timezone_required")
    return value


def _utc_text(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise MarketEvidenceAuthorityError(
            "market_evidence_payload_not_canonical"
        ) from exc


def _payload_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _finite_price(
    value: object,
    field_name: str,
    *,
    strictly_positive: bool,
) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or (float(value) <= 0.0 if strictly_positive else float(value) < 0.0)
    ):
        qualifier = "positive" if strictly_positive else "nonnegative"
        raise MarketEvidenceAuthorityError(f"{field_name}_must_be_{qualifier}_finite")
    return float(value)


def _nonnegative_integer(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MarketEvidenceAuthorityError(f"{field_name}_must_be_nonnegative_integer")
    return value


def _symbol(value: object) -> str:
    symbol = _text(value, "symbol")
    if not _SYMBOL_PATTERN.fullmatch(symbol):
        raise MarketEvidenceAuthorityError("symbol_invalid")
    return symbol


@dataclass(frozen=True)
class MarketSourceBinding:
    """Provider-neutral source and point-in-time lineage binding."""

    dataset_id: str
    catalog_version: str
    source_receipt_id: str
    source_receipt_sha256: str
    source_lineage_sha256: str
    data_through: datetime
    observed_at: datetime
    available_at: datetime

    def __post_init__(self) -> None:
        for field_name in ("dataset_id", "catalog_version", "source_receipt_id"):
            _text(getattr(self, field_name), field_name)
        _sha256_text(self.source_receipt_sha256, "source_receipt_sha256")
        _sha256_text(self.source_lineage_sha256, "source_lineage_sha256")
        data_through = _aware(self.data_through, "data_through")
        observed = _aware(self.observed_at, "observed_at")
        available = _aware(self.available_at, "available_at")
        if data_through > observed or observed > available:
            raise MarketEvidenceAuthorityError("market_source_time_order_invalid")

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "schema_version": _SOURCE_SCHEMA,
            "available_at": _utc_text(self.available_at),
            "catalog_version": self.catalog_version,
            "data_through": _utc_text(self.data_through),
            "dataset_id": self.dataset_id,
            "observed_at": _utc_text(self.observed_at),
            "source_lineage_sha256": self.source_lineage_sha256,
            "source_receipt_id": self.source_receipt_id,
            "source_receipt_sha256": self.source_receipt_sha256,
        }

    def sha256(self) -> str:
        return _payload_sha256(self.canonical_payload())


@dataclass(frozen=True)
class MarketEvidenceContext:
    """TA account and run context bound into every mark or quote."""

    trade_date: date
    decision_as_of: datetime
    capital_authority_id: str
    authority_generation: int
    execution_lineage_id: str
    account_type: str
    real_trading_enabled: bool

    def __post_init__(self) -> None:
        if not isinstance(self.trade_date, date) or isinstance(
            self.trade_date,
            datetime,
        ):
            raise MarketEvidenceAuthorityError("trade_date_invalid")
        _aware(self.decision_as_of, "decision_as_of")
        _text(self.capital_authority_id, "capital_authority_id")
        _text(self.execution_lineage_id, "execution_lineage_id")
        if (
            isinstance(self.authority_generation, bool)
            or not isinstance(self.authority_generation, int)
            or self.authority_generation <= 0
        ):
            raise MarketEvidenceAuthorityError("authority_generation_invalid")
        if self.account_type != "simulated":
            raise MarketEvidenceAuthorityError("account_type_must_be_simulated")
        if self.real_trading_enabled is not False:
            raise MarketEvidenceAuthorityError("real_trading_enabled_must_be_false")

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "schema_version": _CONTEXT_SCHEMA,
            "account_type": "simulated",
            "authority_generation": self.authority_generation,
            "capital_authority_id": self.capital_authority_id,
            "decision_as_of": _utc_text(self.decision_as_of),
            "execution_lineage_id": self.execution_lineage_id,
            "real_trading_enabled": False,
            "trade_date": self.trade_date.isoformat(),
        }

    def sha256(self) -> str:
        return _payload_sha256(self.canonical_payload())


@dataclass(frozen=True)
class AShareMarkEvidence:
    """One immutable A-share account valuation mark candidate."""

    symbol: str
    price_cny: float
    market_session: str
    source: MarketSourceBinding
    session_calendar_receipt_sha256: str
    context: MarketEvidenceContext

    def __post_init__(self) -> None:
        _symbol(self.symbol)
        _finite_price(self.price_cny, "price_cny", strictly_positive=True)
        _text(self.market_session, "market_session")
        if type(self.source) is not MarketSourceBinding:
            raise MarketEvidenceAuthorityError("market_source_binding_required")
        if type(self.context) is not MarketEvidenceContext:
            raise MarketEvidenceAuthorityError("market_evidence_context_required")
        _sha256_text(
            self.session_calendar_receipt_sha256,
            "session_calendar_receipt_sha256",
        )
        if self.source.available_at > self.context.decision_as_of:
            raise MarketEvidenceAuthorityError(
                "market_evidence_available_after_decision"
            )

    def price_payload(self) -> dict[str, Any]:
        return {"price_cny": float(self.price_cny)}

    def price_payload_sha256(self) -> str:
        return _payload_sha256(self.price_payload())

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "schema_version": _MARK_SCHEMA,
            "context": self.context.canonical_payload(),
            "evidence_type": "mark",
            "market": "ashare",
            "market_session": self.market_session,
            "price": self.price_payload(),
            "session_calendar_receipt_sha256": (self.session_calendar_receipt_sha256),
            "source": self.source.canonical_payload(),
            "symbol": self.symbol,
        }

    def sha256(self) -> str:
        return _payload_sha256(self.canonical_payload())


@dataclass(frozen=True)
class AShareExecutionQuoteEvidence:
    """One immutable, order-scoped A-share execution quote candidate."""

    symbol: str
    order_id: str
    bid_price_cny: float
    ask_price_cny: float
    bid_size: int
    ask_size: int
    previous_close_cny: float
    market_session: str
    execution_time: datetime
    source: MarketSourceBinding
    session_calendar_receipt_sha256: str
    context: MarketEvidenceContext

    def __post_init__(self) -> None:
        _symbol(self.symbol)
        _text(self.order_id, "order_id")
        _finite_price(
            self.bid_price_cny,
            "bid_price_cny",
            strictly_positive=False,
        )
        _finite_price(
            self.ask_price_cny,
            "ask_price_cny",
            strictly_positive=False,
        )
        _finite_price(
            self.previous_close_cny,
            "previous_close_cny",
            strictly_positive=True,
        )
        _nonnegative_integer(self.bid_size, "bid_size")
        _nonnegative_integer(self.ask_size, "ask_size")
        execution = _aware(self.execution_time, "execution_time")
        _text(self.market_session, "market_session")
        if type(self.source) is not MarketSourceBinding:
            raise MarketEvidenceAuthorityError("market_source_binding_required")
        if type(self.context) is not MarketEvidenceContext:
            raise MarketEvidenceAuthorityError("market_evidence_context_required")
        _sha256_text(
            self.session_calendar_receipt_sha256,
            "session_calendar_receipt_sha256",
        )
        if (
            self.source.available_at > execution
            or self.context.decision_as_of > execution
        ):
            raise MarketEvidenceAuthorityError("market_evidence_time_order_invalid")

    def price_payload(self) -> dict[str, Any]:
        return {
            "ask_price_cny": float(self.ask_price_cny),
            "ask_size": self.ask_size,
            "bid_price_cny": float(self.bid_price_cny),
            "bid_size": self.bid_size,
            "previous_close_cny": float(self.previous_close_cny),
        }

    def price_payload_sha256(self) -> str:
        return _payload_sha256(self.price_payload())

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "schema_version": _QUOTE_SCHEMA,
            "context": self.context.canonical_payload(),
            "evidence_type": "execution_quote",
            "execution_time": _utc_text(self.execution_time),
            "market": "ashare",
            "market_session": self.market_session,
            "order_id": self.order_id,
            "price": self.price_payload(),
            "session_calendar_receipt_sha256": (self.session_calendar_receipt_sha256),
            "source": self.source.canonical_payload(),
            "symbol": self.symbol,
        }

    def sha256(self) -> str:
        return _payload_sha256(self.canonical_payload())


def _verification_identity_payload(values: Mapping[str, Any]) -> dict[str, Any]:
    execution_time = values["execution_time"]
    return {
        "schema_version": _VERIFICATION_SCHEMA,
        "accepted": values["accepted"],
        "authority_generation": values["authority_generation"],
        "authority_tier": values["authority_tier"],
        "available_at": _utc_text(values["available_at"]),
        "capital_authority_id": values["capital_authority_id"],
        "catalog_version": values["catalog_version"],
        "context_sha256": values["context_sha256"],
        "data_through": _utc_text(values["data_through"]),
        "dataset_id": values["dataset_id"],
        "decision_as_of": _utc_text(values["decision_as_of"]),
        "evidence_sha256": values["evidence_sha256"],
        "evidence_type": values["evidence_type"],
        "execution_lineage_id": values["execution_lineage_id"],
        "execution_time": (
            _utc_text(execution_time) if execution_time is not None else None
        ),
        "frozen_at": _utc_text(values["frozen_at"]),
        "market_session": values["market_session"],
        "observed_at": _utc_text(values["observed_at"]),
        "order_id": values["order_id"],
        "price_payload_sha256": values["price_payload_sha256"],
        "production_eligible": values["production_eligible"],
        "session_calendar_receipt_sha256": values["session_calendar_receipt_sha256"],
        "source_binding_sha256": values["source_binding_sha256"],
        "source_lineage_sha256": values["source_lineage_sha256"],
        "source_receipt_id": values["source_receipt_id"],
        "source_receipt_sha256": values["source_receipt_sha256"],
        "symbol": values["symbol"],
        "trade_date": values["trade_date"].isoformat(),
        "verified_at": _utc_text(values["verified_at"]),
        "verifier_id": values["verifier_id"],
        "verifier_implementation_sha256": values["verifier_implementation_sha256"],
        "verifier_version": values["verifier_version"],
    }


@dataclass(frozen=True)
class MarketEvidenceVerification:
    """Detached proof binding verifier identity to exact market evidence."""

    accepted: bool
    authority_tier: str
    production_eligible: bool
    verifier_id: str
    verifier_version: str
    verifier_implementation_sha256: str
    evidence_type: str
    evidence_sha256: str
    source_binding_sha256: str
    context_sha256: str
    dataset_id: str
    catalog_version: str
    source_receipt_id: str
    source_receipt_sha256: str
    source_lineage_sha256: str
    symbol: str
    price_payload_sha256: str
    market_session: str
    session_calendar_receipt_sha256: str
    trade_date: date
    data_through: datetime
    observed_at: datetime
    available_at: datetime
    decision_as_of: datetime
    execution_time: datetime | None
    order_id: str | None
    capital_authority_id: str
    authority_generation: int
    execution_lineage_id: str
    verified_at: datetime
    frozen_at: datetime
    proof_sha256: str

    def __post_init__(self) -> None:
        if self.accepted is not True:
            raise MarketEvidenceAuthorityError(
                "market_evidence_verification_must_be_accepted"
            )
        if not isinstance(self.production_eligible, bool):
            raise MarketEvidenceAuthorityError("production_eligible_invalid")
        for field_name in (
            "authority_tier",
            "verifier_id",
            "verifier_version",
            "evidence_type",
            "dataset_id",
            "catalog_version",
            "source_receipt_id",
            "market_session",
            "capital_authority_id",
            "execution_lineage_id",
        ):
            _text(getattr(self, field_name), field_name)
        fixture_verifier_id = (
            "tradingagent-non-production-fixture-market-evidence-verifier"
        )
        if (
            self.authority_tier == "non_production_fixture"
            or self.verifier_id == fixture_verifier_id
        ) and (
            self.authority_tier != "non_production_fixture"
            or self.production_eligible is not False
        ):
            raise MarketEvidenceAuthorityError(
                "non_production_fixture_cannot_be_production_eligible"
            )
        if self.evidence_type not in {"mark", "execution_quote"}:
            raise MarketEvidenceAuthorityError("evidence_type_invalid")
        _symbol(self.symbol)
        for field_name in (
            "verifier_implementation_sha256",
            "evidence_sha256",
            "source_binding_sha256",
            "context_sha256",
            "source_receipt_sha256",
            "source_lineage_sha256",
            "price_payload_sha256",
            "session_calendar_receipt_sha256",
            "proof_sha256",
        ):
            _sha256_text(getattr(self, field_name), field_name)
        if not isinstance(self.trade_date, date) or isinstance(
            self.trade_date,
            datetime,
        ):
            raise MarketEvidenceAuthorityError("trade_date_invalid")
        if (
            isinstance(self.authority_generation, bool)
            or not isinstance(self.authority_generation, int)
            or self.authority_generation <= 0
        ):
            raise MarketEvidenceAuthorityError("authority_generation_invalid")
        data_through = _aware(self.data_through, "data_through")
        observed = _aware(self.observed_at, "observed_at")
        available = _aware(self.available_at, "available_at")
        decision = _aware(self.decision_as_of, "decision_as_of")
        verified = _aware(self.verified_at, "verified_at")
        frozen = _aware(self.frozen_at, "frozen_at")
        if (
            data_through > observed
            or observed > available
            or available > verified
            or verified > frozen
        ):
            raise MarketEvidenceAuthorityError(
                "market_evidence_verification_time_order_invalid"
            )
        if self.evidence_type == "mark":
            if available > decision:
                raise MarketEvidenceAuthorityError(
                    "mark_verification_available_after_decision"
                )
            if self.order_id is not None or self.execution_time is not None:
                raise MarketEvidenceAuthorityError(
                    "mark_verification_order_or_execution_time_invalid"
                )
        else:
            _text(self.order_id, "order_id")
            execution = _aware(self.execution_time, "execution_time")
            if decision > execution or available > execution:
                raise MarketEvidenceAuthorityError(
                    "quote_verification_execution_time_invalid"
                )
        expected = self.recompute_proof_sha256()
        if not hmac.compare_digest(self.proof_sha256, expected):
            raise MarketEvidenceAuthorityError(
                "market_evidence_verification_proof_mismatch"
            )

    def identity_payload(self) -> dict[str, Any]:
        return _verification_identity_payload(self.__dict__)

    def recompute_proof_sha256(self) -> str:
        return _payload_sha256(self.identity_payload())

    def canonical_payload(self) -> dict[str, Any]:
        return {**self.identity_payload(), "proof_sha256": self.proof_sha256}

    def sha256(self) -> str:
        return _payload_sha256(self.canonical_payload())

    @classmethod
    def issue(
        cls,
        *,
        evidence: AShareMarkEvidence | AShareExecutionQuoteEvidence,
        verifier_id: str,
        verifier_version: str,
        verifier_implementation_sha256: str,
        authority_tier: str,
        production_eligible: bool,
        verified_at: datetime,
        frozen_at: datetime,
    ) -> "MarketEvidenceVerification":
        if type(evidence) is AShareMarkEvidence:
            evidence_type = "mark"
            order_id = None
            execution_time = None
        elif type(evidence) is AShareExecutionQuoteEvidence:
            evidence_type = "execution_quote"
            order_id = evidence.order_id
            execution_time = evidence.execution_time
        else:
            raise MarketEvidenceAuthorityError("market_evidence_type_untrusted")
        values: dict[str, Any] = {
            "accepted": True,
            "authority_tier": authority_tier,
            "production_eligible": production_eligible,
            "verifier_id": verifier_id,
            "verifier_version": verifier_version,
            "verifier_implementation_sha256": verifier_implementation_sha256,
            "evidence_type": evidence_type,
            "evidence_sha256": evidence.sha256(),
            "source_binding_sha256": evidence.source.sha256(),
            "context_sha256": evidence.context.sha256(),
            "dataset_id": evidence.source.dataset_id,
            "catalog_version": evidence.source.catalog_version,
            "source_receipt_id": evidence.source.source_receipt_id,
            "source_receipt_sha256": evidence.source.source_receipt_sha256,
            "source_lineage_sha256": evidence.source.source_lineage_sha256,
            "symbol": evidence.symbol,
            "price_payload_sha256": evidence.price_payload_sha256(),
            "market_session": evidence.market_session,
            "session_calendar_receipt_sha256": (
                evidence.session_calendar_receipt_sha256
            ),
            "trade_date": evidence.context.trade_date,
            "data_through": evidence.source.data_through,
            "observed_at": evidence.source.observed_at,
            "available_at": evidence.source.available_at,
            "decision_as_of": evidence.context.decision_as_of,
            "execution_time": execution_time,
            "order_id": order_id,
            "capital_authority_id": evidence.context.capital_authority_id,
            "authority_generation": evidence.context.authority_generation,
            "execution_lineage_id": evidence.context.execution_lineage_id,
            "verified_at": verified_at,
            "frozen_at": frozen_at,
        }
        return cls(
            **values,
            proof_sha256=_payload_sha256(_verification_identity_payload(values)),
        )


class MarketEvidenceAuthorityVerifier(ABC):
    """Required port; deliberately has no default verification behavior."""

    @abstractmethod
    def verify(
        self,
        evidence: AShareMarkEvidence | AShareExecutionQuoteEvidence,
        *,
        expected_dataset_id: str,
        frozen_at: datetime,
    ) -> MarketEvidenceVerification:
        """Return an exact detached verification or fail closed."""


class NonProductionFixtureMarketEvidenceVerifier(MarketEvidenceAuthorityVerifier):
    """Exact fixture-only verifier over a frozen evidence hash allowlist."""

    verifier_id = "tradingagent-non-production-fixture-market-evidence-verifier"
    verifier_version = "1"
    verifier_implementation_sha256 = (
        "2b1e6fb1bc72c17684bf4fd5948d70b8f55309e3592a9f70ed2fe6de77607864"
    )
    authority_tier = "non_production_fixture"
    production_eligible = False

    def __init_subclass__(cls, **kwargs: Any) -> None:
        raise TypeError("fixture_market_evidence_verifier_is_final")

    def __setattr__(self, name: str, value: object) -> None:
        if name == "_allowed_evidence_sha256s":
            if hasattr(self, name):
                raise AttributeError("fixture_market_evidence_allowlist_is_frozen")
            object.__setattr__(self, name, value)
            return
        raise AttributeError("fixture_market_evidence_verifier_is_frozen")

    def __init__(self, *, allowed_evidence_sha256s: frozenset[str]) -> None:
        if (
            type(allowed_evidence_sha256s) is not frozenset
            or not allowed_evidence_sha256s
        ):
            raise MarketEvidenceAuthorityError(
                "allowed_evidence_sha256s_must_be_nonempty_frozenset"
            )
        for evidence_sha256 in allowed_evidence_sha256s:
            _sha256_text(evidence_sha256, "allowed_evidence_sha256")
        self._allowed_evidence_sha256s = allowed_evidence_sha256s

    def verify(
        self,
        evidence: AShareMarkEvidence | AShareExecutionQuoteEvidence,
        *,
        expected_dataset_id: str,
        frozen_at: datetime,
    ) -> MarketEvidenceVerification:
        if type(evidence) not in {
            AShareMarkEvidence,
            AShareExecutionQuoteEvidence,
        }:
            raise MarketEvidenceAuthorityError("market_evidence_type_untrusted")
        expected_dataset = _text(expected_dataset_id, "expected_dataset_id")
        frozen = _aware(frozen_at, "frozen_at")
        if evidence.source.dataset_id != expected_dataset:
            raise MarketEvidenceAuthorityError("market_evidence_dataset_id_mismatch")
        if evidence.source.available_at > frozen:
            raise MarketEvidenceAuthorityError(
                "market_evidence_frozen_before_available"
            )
        evidence_sha256 = evidence.sha256()
        if evidence_sha256 not in self._allowed_evidence_sha256s:
            raise MarketEvidenceAuthorityError("market_evidence_sha256_not_frozen")
        return MarketEvidenceVerification.issue(
            evidence=evidence,
            verifier_id=self.verifier_id,
            verifier_version=self.verifier_version,
            verifier_implementation_sha256=(self.verifier_implementation_sha256),
            authority_tier=self.authority_tier,
            production_eligible=False,
            verified_at=frozen,
            frozen_at=frozen,
        )


@dataclass(frozen=True)
class NonProductionFixtureMarketEvidenceAuthority:
    """One exact fixture evidence object plus its detached local verification."""

    evidence: AShareMarkEvidence | AShareExecutionQuoteEvidence
    verification: MarketEvidenceVerification
    expected_dataset_id: str
    frozen_at: datetime

    authority_tier = "non_production_fixture"
    production_eligible = False

    def __init_subclass__(cls, **kwargs: Any) -> None:
        raise TypeError("fixture_market_evidence_authority_is_final")

    def __post_init__(self) -> None:
        if type(self.evidence) not in {
            AShareMarkEvidence,
            AShareExecutionQuoteEvidence,
        }:
            raise MarketEvidenceAuthorityError("market_evidence_type_untrusted")
        if type(self.verification) is not MarketEvidenceVerification:
            raise MarketEvidenceAuthorityError("market_evidence_verification_untrusted")
        expected_dataset = _text(
            self.expected_dataset_id,
            "expected_dataset_id",
        )
        frozen = _aware(self.frozen_at, "frozen_at")
        if (
            self.evidence.source.dataset_id != expected_dataset
            or self.verification.dataset_id != expected_dataset
            or self.verification.evidence_sha256 != self.evidence.sha256()
            or self.verification.proof_sha256
            != self.verification.recompute_proof_sha256()
            or self.verification.verifier_id
            != NonProductionFixtureMarketEvidenceVerifier.verifier_id
            or self.verification.verifier_version
            != NonProductionFixtureMarketEvidenceVerifier.verifier_version
            or self.verification.verifier_implementation_sha256
            != NonProductionFixtureMarketEvidenceVerifier.verifier_implementation_sha256
            or self.verification.authority_tier != "non_production_fixture"
            or self.verification.production_eligible is not False
            or self.verification.frozen_at != frozen
        ):
            raise MarketEvidenceAuthorityError(
                "fixture_market_evidence_authority_binding_invalid"
            )

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "schema_version": (
                "tradingagent.non_production_fixture_market_evidence_authority.v1"
            ),
            "authority_tier": "non_production_fixture",
            "evidence": self.evidence.canonical_payload(),
            "expected_dataset_id": self.expected_dataset_id,
            "frozen_at": _utc_text(self.frozen_at),
            "production_eligible": False,
            "verification": self.verification.canonical_payload(),
        }

    @property
    def authority_sha256(self) -> str:
        return _payload_sha256(self.canonical_payload())


def freeze_non_production_market_evidence(
    evidence: AShareMarkEvidence | AShareExecutionQuoteEvidence,
    *,
    expected_dataset_id: str,
    frozen_at: datetime,
) -> NonProductionFixtureMarketEvidenceAuthority:
    """Freeze one exact evidence candidate with the fixture-only verifier."""

    if type(evidence) not in {
        AShareMarkEvidence,
        AShareExecutionQuoteEvidence,
    }:
        raise MarketEvidenceAuthorityError("market_evidence_type_untrusted")
    verifier = NonProductionFixtureMarketEvidenceVerifier(
        allowed_evidence_sha256s=frozenset({evidence.sha256()}),
    )
    verification = verifier.verify(
        evidence,
        expected_dataset_id=expected_dataset_id,
        frozen_at=frozen_at,
    )
    return NonProductionFixtureMarketEvidenceAuthority(
        evidence=evidence,
        verification=verification,
        expected_dataset_id=expected_dataset_id,
        frozen_at=frozen_at,
    )


__all__ = [
    "AShareExecutionQuoteEvidence",
    "AShareMarkEvidence",
    "MarketEvidenceAuthorityError",
    "MarketEvidenceAuthorityVerifier",
    "MarketEvidenceContext",
    "MarketEvidenceVerification",
    "MarketSourceBinding",
    "NonProductionFixtureMarketEvidenceAuthority",
    "NonProductionFixtureMarketEvidenceVerifier",
    "freeze_non_production_market_evidence",
]
