"""Frozen, hashed 科技 + 医药 mainboard universe for one capital-backed paper session.

This module is order-identity only.  It never reads ``shared/industry/shadow_slice``
and never treats industry names as securities.  ChiNext ``300/301`` and STAR
``688/689`` remain exclusion probes with reason codes, not orders.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Iterable, Mapping

from shared.universe.policy import (
    CanonicalMainboardScopePolicy,
    InstrumentEligibility,
    classify_instrument,
    is_mainboard_tradable,
)


UNIVERSE_CONTRACT_ID = "tradingagent.ashare.capital_backed_paper_universe.v1"
_SECURITY_RE = re.compile(r"^[0-9]{6}\.(SZ|SH)$")

# Mainboard 科技 names only.  ChiNext/STAR tickers are not admitted here.
TECH_MAINBOARD_SYMBOLS: tuple[str, ...] = (
    "000063.SZ",
    "000066.SZ",
    "000725.SZ",
    "000938.SZ",
    "000977.SZ",
    "002008.SZ",
    "002049.SZ",
    "002156.SZ",
    "002230.SZ",
    "002236.SZ",
    "002241.SZ",
    "002371.SZ",
    "002415.SZ",
    "002475.SZ",
    "600460.SH",
    "600584.SH",
    "600588.SH",
    "600703.SH",
    "600845.SH",
    "603019.SH",
)

# Mainboard 医药 names only.
PHARMA_MAINBOARD_SYMBOLS: tuple[str, ...] = (
    "000028.SZ",
    "000423.SZ",
    "000513.SZ",
    "000538.SZ",
    "000623.SZ",
    "000661.SZ",
    "000999.SZ",
    "002001.SZ",
    "002007.SZ",
    "002262.SZ",
    "002422.SZ",
    "600079.SH",
    "600085.SH",
    "600196.SH",
    "600267.SH",
    "600276.SH",
    "600332.SH",
    "600380.SH",
    "600511.SH",
    "600521.SH",
)

# Small explicit add-list used by existing 50k capital fixtures and tests.
EXPLICIT_ADD_LIST: tuple[str, ...] = (
    "000001.SZ",
    "600000.SH",
    "601318.SH",
)

# Always classified so ChiNext/STAR reason codes persist.  Never order identity.
EXCLUSION_PROBES: tuple[str, ...] = (
    "300750.SZ",
    "301269.SZ",
    "688981.SH",
    "689009.SH",
)


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


FROZEN_UNIVERSE_MANIFEST: Mapping[str, object] = {
    "contract_id": UNIVERSE_CONTRACT_ID,
    "tech_mainboard": list(TECH_MAINBOARD_SYMBOLS),
    "pharma_mainboard": list(PHARMA_MAINBOARD_SYMBOLS),
    "explicit_add_list": list(EXPLICIT_ADD_LIST),
    "exclusion_probes": list(EXCLUSION_PROBES),
}
FROZEN_UNIVERSE_SHA256 = _sha256(FROZEN_UNIVERSE_MANIFEST)


class CapitalBackedPaperUniverseError(ValueError):
    """Raised when the frozen session universe cannot be proven."""


@dataclass(frozen=True)
class SessionSymbolClassification:
    """One symbol's scope decision for the capital-backed paper session."""

    symbol: str
    eligibility: InstrumentEligibility
    sleeve: str
    order_identity_allowed: bool
    reason_code: str

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "sleeve": self.sleeve,
            "role": self.eligibility.role.value,
            "board": self.eligibility.board,
            "order_identity_allowed": self.order_identity_allowed,
            "reason_code": self.reason_code,
        }


def looks_like_security_symbol(symbol: object) -> bool:
    """Return whether *symbol* is a concrete exchange security, not an industry name."""

    return isinstance(symbol, str) and _SECURITY_RE.fullmatch(symbol.strip().upper()) is not None


def _sleeve_for(symbol: str) -> str:
    if symbol in TECH_MAINBOARD_SYMBOLS:
        return "tech"
    if symbol in PHARMA_MAINBOARD_SYMBOLS:
        return "pharma"
    if symbol in EXPLICIT_ADD_LIST:
        return "explicit_add"
    if symbol in EXCLUSION_PROBES:
        return "exclusion_probe"
    return "extra"


def classify_session_symbol(
    symbol: object,
    *,
    scope_policy: CanonicalMainboardScopePolicy | None = None,
) -> SessionSymbolClassification:
    """Classify one candidate.  Industry names and ChiNext/STAR cannot become orders."""

    policy = scope_policy or CanonicalMainboardScopePolicy()
    if not looks_like_security_symbol(symbol):
        text = str(symbol or "").strip() or "unnamed"
        eligibility = classify_instrument(text, instrument_type="unknown")
        return SessionSymbolClassification(
            symbol=text,
            eligibility=eligibility,
            sleeve="industry_shadow",
            order_identity_allowed=False,
            reason_code="industry_shadow_not_order_identity",
        )
    normalized = str(symbol).strip().upper()
    eligibility = classify_instrument(normalized, instrument_type="common_stock")
    allowed = bool(
        policy.order_identity_allowed(normalized) and is_mainboard_tradable(normalized)
    )
    if allowed:
        reason = "mainboard_common_stock_tradable"
    else:
        reason = eligibility.reason_code
    return SessionSymbolClassification(
        symbol=normalized,
        eligibility=eligibility,
        sleeve=_sleeve_for(normalized),
        order_identity_allowed=allowed,
        reason_code=reason,
    )


def session_candidate_symbols(
    *,
    extra_symbols: Iterable[str] = (),
    include_exclusion_probes: bool = True,
) -> tuple[str, ...]:
    """Return the frozen hashed list plus extras, without inventing securities."""

    seen: list[str] = []
    for symbol in (
        *TECH_MAINBOARD_SYMBOLS,
        *PHARMA_MAINBOARD_SYMBOLS,
        *EXPLICIT_ADD_LIST,
        *(EXCLUSION_PROBES if include_exclusion_probes else ()),
        *tuple(extra_symbols),
    ):
        if symbol in seen:
            continue
        seen.append(symbol)
    if not seen:
        raise CapitalBackedPaperUniverseError("session_universe_empty")
    return tuple(seen)


def classify_session_universe(
    *,
    extra_symbols: Iterable[str] = (),
    include_exclusion_probes: bool = True,
    scope_policy: CanonicalMainboardScopePolicy | None = None,
) -> tuple[SessionSymbolClassification, ...]:
    """Classify every session candidate against the canonical mainboard policy."""

    policy = scope_policy or CanonicalMainboardScopePolicy()
    return tuple(
        classify_session_symbol(symbol, scope_policy=policy)
        for symbol in session_candidate_symbols(
            extra_symbols=extra_symbols,
            include_exclusion_probes=include_exclusion_probes,
        )
    )


__all__ = [
    "EXCLUSION_PROBES",
    "EXPLICIT_ADD_LIST",
    "FROZEN_UNIVERSE_MANIFEST",
    "FROZEN_UNIVERSE_SHA256",
    "PHARMA_MAINBOARD_SYMBOLS",
    "TECH_MAINBOARD_SYMBOLS",
    "UNIVERSE_CONTRACT_ID",
    "CapitalBackedPaperUniverseError",
    "SessionSymbolClassification",
    "classify_session_symbol",
    "classify_session_universe",
    "looks_like_security_symbol",
    "session_candidate_symbols",
]
