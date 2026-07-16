"""Single authority for A-share instrument eligibility.

Phase 0-3 permits only Shanghai/Shenzhen mainboard common stocks to acquire an
order identity.  ChiNext/STAR indices and sector aggregates may be consumed as
market context, but can never become candidates, intents or orders.  ChiNext
and STAR individual securities are outside both the tradable and context
universes while account permissions are unavailable.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Mapping

if TYPE_CHECKING:
    from shared.runtime.run_bundle import ComponentIdentity


POLICY_ID = "tradingagent.universe_scope.v1"


class InstrumentRole(str, Enum):
    MAINBOARD_COMMON_STOCK = "mainboard_common_stock"
    CHINEXT_COMMON_STOCK = "chinext_common_stock"
    STAR_COMMON_STOCK = "star_common_stock"
    CHINEXT_INDEX = "chinext_index"
    STAR_INDEX = "star_index"
    MARKET_CONTEXT_INDEX = "market_context_index"
    SECTOR_AGGREGATE = "sector_aggregate"
    BEIJING_SECURITY = "beijing_security"
    B_SHARE = "b_share"
    FUND_OR_ETF = "fund_or_etf"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class InstrumentEligibility:
    policy_id: str
    source_symbol: str
    normalized_symbol: str
    exchange: str
    role: InstrumentRole
    board: str
    tradable: bool
    context_allowed: bool
    context_only: bool
    order_identity_allowed: bool
    reason_code: str


_EXCHANGE_ALIASES = {
    "SH": "SH",
    "SSE": "SH",
    "XSHG": "SH",
    "SHSE": "SH",
    "SZ": "SZ",
    "SZSE": "SZ",
    "XSHE": "SZ",
    "BJ": "BJ",
    "BSE": "BJ",
    "NORTH": "BJ",
}

_KNOWN_CONTEXT_INDICES = {
    "399006.SZ": (InstrumentRole.CHINEXT_INDEX, "chinext"),
    "399102.SZ": (InstrumentRole.CHINEXT_INDEX, "chinext"),
    "399673.SZ": (InstrumentRole.CHINEXT_INDEX, "chinext"),
    "000688.SH": (InstrumentRole.STAR_INDEX, "star"),
}

_CANONICAL_MAINBOARD_SCOPE_POLICY_MANIFEST: Mapping[str, object] = MappingProxyType(
    {
        "schema_version": "tradingagent.canonical_mainboard_scope_policy.v1",
        "policy_id": POLICY_ID,
        "component_id": "mainboard-scope-policy",
        "component_version": "1",
        "order_identity_role": InstrumentRole.MAINBOARD_COMMON_STOCK.value,
        "context_only_roles": tuple(
            sorted(
                {
                    InstrumentRole.CHINEXT_INDEX.value,
                    InstrumentRole.MARKET_CONTEXT_INDEX.value,
                    InstrumentRole.SECTOR_AGGREGATE.value,
                    InstrumentRole.STAR_INDEX.value,
                }
            )
        ),
        "excluded_individual_roles": tuple(
            sorted(
                {
                    InstrumentRole.CHINEXT_COMMON_STOCK.value,
                    InstrumentRole.STAR_COMMON_STOCK.value,
                }
            )
        ),
    }
)
CANONICAL_MAINBOARD_SCOPE_POLICY_SHA256 = hashlib.sha256(
    json.dumps(
        dict(_CANONICAL_MAINBOARD_SCOPE_POLICY_MANIFEST),
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
).hexdigest()


class CanonicalMainboardScopePolicy:
    """Stateless, immutable port for the one frozen Phase-1 scope contract."""

    __slots__ = ()

    @property
    def manifest(self) -> Mapping[str, object]:
        return _CANONICAL_MAINBOARD_SCOPE_POLICY_MANIFEST

    @property
    def policy_sha256(self) -> str:
        return CANONICAL_MAINBOARD_SCOPE_POLICY_SHA256

    @property
    def identity(self) -> ComponentIdentity:
        # Lazy import keeps the universe classifier independent at import time.
        from shared.runtime.run_bundle import ComponentIdentity

        return ComponentIdentity(
            stage=None,
            component_id="mainboard-scope-policy",
            version="1",
            artifact_sha256=CANONICAL_MAINBOARD_SCOPE_POLICY_SHA256,
        )

    def order_identity_allowed(self, symbol: str) -> bool:
        return (
            classify_instrument(
                symbol,
                instrument_type="common_stock",
            ).role
            is InstrumentRole.MAINBOARD_COMMON_STOCK
        )

    def context_identity_allowed(
        self,
        symbol: str,
        *,
        instrument_type: str,
    ) -> bool:
        eligibility = classify_instrument(
            symbol,
            instrument_type=instrument_type,
        )
        return (
            eligibility.context_only
            and eligibility.role.value in self.manifest["context_only_roles"]
        )


def _normalize_exchange(value: Any) -> str:
    return _EXCHANGE_ALIASES.get(str(value or "").strip().upper(), "")


def _split_symbol(symbol: Any, exchange: Any = "") -> tuple[str, str, str]:
    source = str(symbol or "").strip().upper()
    raw_exchange = str(exchange or "").strip().upper()
    supplied_exchange = _normalize_exchange(exchange)
    if raw_exchange and not supplied_exchange:
        return source, source.rsplit(".", 1)[0], "INVALID"
    if "." in source:
        digits, suffix = source.rsplit(".", 1)
        suffix_exchange = _normalize_exchange(suffix)
        if not suffix_exchange:
            return source, digits, "INVALID"
        if supplied_exchange and supplied_exchange != suffix_exchange:
            return source, digits, "INVALID"
        return source, digits, suffix_exchange
    return source, source, supplied_exchange


def _eligibility(
    *,
    source_symbol: str,
    normalized_symbol: str,
    exchange: str,
    role: InstrumentRole,
    board: str,
    tradable: bool = False,
    context_allowed: bool = False,
    reason_code: str,
) -> InstrumentEligibility:
    context_only = bool(context_allowed and not tradable)
    return InstrumentEligibility(
        policy_id=POLICY_ID,
        source_symbol=source_symbol,
        normalized_symbol=normalized_symbol,
        exchange=exchange,
        role=role,
        board=board,
        tradable=tradable,
        context_allowed=context_allowed,
        context_only=context_only,
        order_identity_allowed=tradable,
        reason_code=reason_code,
    )


def classify_instrument(
    symbol: Any,
    *,
    exchange: Any = "",
    instrument_type: Any = "common_stock",
) -> InstrumentEligibility:
    """Classify one instrument without granting implicit execution authority."""

    native_symbol = (
        isinstance(symbol, str) and bool(symbol) and symbol == symbol.strip()
    )
    source, digits, resolved_exchange = _split_symbol(symbol, exchange)
    normalized_type = str(instrument_type or "common_stock").strip().lower()

    if not native_symbol:
        return _eligibility(
            source_symbol=source,
            normalized_symbol=digits,
            exchange=resolved_exchange,
            role=InstrumentRole.UNKNOWN,
            board="unknown",
            reason_code="symbol_must_be_native_canonical_string",
        )

    if normalized_type in {
        "sector",
        "industry",
        "sector_aggregate",
        "industry_aggregate",
    }:
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9._:-]{0,127}", source):
            return _eligibility(
                source_symbol=source,
                normalized_symbol=source,
                exchange=resolved_exchange,
                role=InstrumentRole.UNKNOWN,
                board="unknown",
                reason_code="invalid_context_aggregate_identity",
            )
        return _eligibility(
            source_symbol=source,
            normalized_symbol=source,
            exchange=resolved_exchange,
            role=InstrumentRole.SECTOR_AGGREGATE,
            board="aggregate",
            context_allowed=True,
            reason_code="sector_aggregate_context_only",
        )

    normalized_symbol = f"{digits}.{resolved_exchange}" if resolved_exchange else digits
    known_index = _KNOWN_CONTEXT_INDICES.get(normalized_symbol)
    if known_index is not None:
        role, board = known_index
        return _eligibility(
            source_symbol=source,
            normalized_symbol=normalized_symbol,
            exchange=resolved_exchange,
            role=role,
            board=board,
            context_allowed=True,
            reason_code=f"{board}_index_context_only",
        )

    if normalized_type in {"index", "market_index", "benchmark"}:
        if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", normalized_symbol):
            return _eligibility(
                source_symbol=source,
                normalized_symbol=normalized_symbol,
                exchange=resolved_exchange,
                role=InstrumentRole.UNKNOWN,
                board="unknown",
                reason_code="invalid_market_index_identity",
            )
        return _eligibility(
            source_symbol=source,
            normalized_symbol=normalized_symbol,
            exchange=resolved_exchange,
            role=InstrumentRole.MARKET_CONTEXT_INDEX,
            board="market",
            context_allowed=True,
            reason_code="market_index_context_only",
        )

    if normalized_type in {"fund", "etf", "lof", "closed_end_fund"}:
        return _eligibility(
            source_symbol=source,
            normalized_symbol=normalized_symbol,
            exchange=resolved_exchange,
            role=InstrumentRole.FUND_OR_ETF,
            board="fund",
            reason_code="fund_or_etf_not_in_phase_scope",
        )

    if normalized_type not in {
        "common_stock",
        "stock",
        "equity",
        "a_share",
        "ashare",
        "a_stock",
        "cn_equity",
    }:
        return _eligibility(
            source_symbol=source,
            normalized_symbol=normalized_symbol,
            exchange=resolved_exchange,
            role=InstrumentRole.UNKNOWN,
            board="unknown",
            reason_code="instrument_type_not_in_phase_scope",
        )

    if not re.fullmatch(r"\d{6}", digits):
        return _eligibility(
            source_symbol=source,
            normalized_symbol=normalized_symbol,
            exchange=resolved_exchange,
            role=InstrumentRole.UNKNOWN,
            board="unknown",
            reason_code="invalid_or_unknown_symbol",
        )

    if resolved_exchange == "BJ" or digits.startswith(("4", "8", "92")):
        return _eligibility(
            source_symbol=source,
            normalized_symbol=normalized_symbol,
            exchange=resolved_exchange or "BJ",
            role=InstrumentRole.BEIJING_SECURITY,
            board="beijing",
            reason_code="beijing_security_not_in_phase_scope",
        )

    if (digits.startswith("200") and resolved_exchange in {"", "SZ"}) or (
        digits.startswith("900") and resolved_exchange in {"", "SH"}
    ):
        return _eligibility(
            source_symbol=source,
            normalized_symbol=normalized_symbol,
            exchange=resolved_exchange,
            role=InstrumentRole.B_SHARE,
            board="b_share",
            reason_code="b_share_not_in_phase_scope",
        )

    if digits.startswith(("300", "301")) and resolved_exchange in {"", "SZ"}:
        return _eligibility(
            source_symbol=source,
            normalized_symbol=normalized_symbol,
            exchange=resolved_exchange or "SZ",
            role=InstrumentRole.CHINEXT_COMMON_STOCK,
            board="chinext",
            reason_code="chinext_individual_permission_unavailable",
        )

    if digits.startswith(("688", "689")) and resolved_exchange in {"", "SH"}:
        return _eligibility(
            source_symbol=source,
            normalized_symbol=normalized_symbol,
            exchange=resolved_exchange or "SH",
            role=InstrumentRole.STAR_COMMON_STOCK,
            board="star",
            reason_code="star_individual_permission_unavailable",
        )

    sz_mainboard = digits.startswith(
        ("000", "001", "002", "003")
    ) and resolved_exchange in {"", "SZ"}
    sh_mainboard = digits.startswith(
        ("600", "601", "603", "605")
    ) and resolved_exchange in {"", "SH"}
    if sz_mainboard or sh_mainboard:
        inferred_exchange = resolved_exchange or ("SZ" if sz_mainboard else "SH")
        return _eligibility(
            source_symbol=source,
            normalized_symbol=f"{digits}.{inferred_exchange}",
            exchange=inferred_exchange,
            role=InstrumentRole.MAINBOARD_COMMON_STOCK,
            board="mainboard",
            tradable=True,
            reason_code="mainboard_common_stock_tradable",
        )

    return _eligibility(
        source_symbol=source,
        normalized_symbol=normalized_symbol,
        exchange=resolved_exchange,
        role=InstrumentRole.UNKNOWN,
        board="unknown",
        reason_code="instrument_not_in_phase_scope",
    )


def is_mainboard_tradable(
    symbol: Any,
    *,
    exchange: Any = "",
    instrument_type: Any = "common_stock",
) -> bool:
    """Return true only for order-eligible mainboard common stocks."""

    return classify_instrument(
        symbol,
        exchange=exchange,
        instrument_type=instrument_type,
    ).order_identity_allowed
