"""Contracts and deterministic primitives for the Crypto fixture simulator."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_UP
from typing import Any, Mapping, Sequence

from Crypto.capital_policy import (
    CRYPTO_CAPITAL_POLICY,
    CryptoCapitalPolicy,
)


FIXTURE_CONTRACT = "tradingagent.crypto.fixture_spot_cycle.v1"
QUALIFICATION_CONTRACT = "tradingagent.crypto.fixture_qualification.v1"
CHAMPION_CONTRACT = "tradingagent.crypto.frozen_champion_candidate.v1"
DECISION_CONTRACT = "tradingagent.crypto.timeframe_decision.v1"
ORDER_INTENT_CONTRACT = "tradingagent.crypto.order_intent.v1"
PAPER_RECEIPT_CONTRACT = "tradingagent.crypto.paper_receipt.v1"
PAPER_BROKER_CONTRACT = "tradingagent.crypto.paper_broker.v1"
CAPITAL_LEDGER_CONTRACT = "tradingagent.crypto.capital_ledger.v1"
CAPITAL_HEAD_CONTRACT = "tradingagent.crypto.capital_ledger_head.v1"
CYCLE_CLAIM_CONTRACT = "tradingagent.crypto.cycle_claim.v1"
RUN_BUNDLE_CONTRACT = "tradingagent.crypto.fixture_auto_sim_run.v1"
SAMPLE_REVIEW_CONTRACT = "tradingagent.crypto.sample_review.v1"
LLM_SIDECAR_CONTRACT = "tradingagent.crypto.llm_sidecar_journal.v1"

WIRE_CONTRACT = {
    "catalog": {"method": "GET", "path": "/v1/catalog"},
    "query": {"method": "POST", "path": "/v1/query"},
}
ALLOWED_SYMBOLS = frozenset({"BTCUSDT", "ETHUSDT"})
ALLOWED_SOURCE_KINDS = frozenset({"fixture", "mock"})
FORBIDDEN_KEYS = frozenset(
    {
        "api_key",
        "api_secret",
        "base_url",
        "broker_url",
        "dataset_id",
        "durable_execution_receipt",
        "endpoint",
        "execution_authority",
        "execution_eligible",
        "leverage",
        "live_broker",
        "margin",
        "private_key",
        "provider_route",
        "outbox_id",
        "capital_commit_id",
        "real_trading_enabled",
        "signature",
        "sqlite_path",
        "testnet",
        "transfer",
        "withdraw",
    }
)
FORBIDDEN_LLM_AUTHORITY_KEYS = frozenset(
    {
        "action",
        "capital_commit_id",
        "durable_execution_receipt",
        "execution_authority",
        "execution_eligible",
        "order",
        "order_intent",
        "outbox_id",
        "position_size",
        "promotion_authorized",
        "quantity",
        "risk_budget",
        "side",
        "target_weight",
    }
)
FORBIDDEN_ROUTE_TOKENS = (
    "/source_status",
    "/tushare",
    "api.binance",
    "sqlite://",
)
ZERO = Decimal("0")
MONEY_QUANTUM = Decimal("0.00000001")
FROZEN_SLIPPAGE_BPS = Decimal("2")
FROZEN_TAKER_FEE_RATE = Decimal("0.001")
JSON_TREE_MAX_DEPTH = 16
JSON_TREE_MAX_VALUES = 256
JSON_TREE_MAX_INTERNAL_VALUES = 4096
JSON_TREE_MAX_KEY_CHARS = 128
JSON_TREE_MAX_STRING_CHARS = 4096
DURABILITY_SCOPE = "local_fixture_fsync_only"
NON_AUTHORITY_FALSE_FIELDS = frozenset(
    {
        "direct_execution",
        "durable_execution_receipt",
        "execution_authority",
        "execution_eligible",
        "live_broker",
        "live_broker_used",
        "model_network_used",
        "network_used",
        "production_eligible",
        "promotion_authorized",
        "promotion_evidence_ready",
        "real_execution",
        "real_trading_enabled",
        "testnet",
        "testnet_used",
    }
)
NON_AUTHORITY_NONE_FIELDS = frozenset({"capital_commit_id", "outbox_id"})
NON_AUTHORITY_LITERAL_FIELDS = {
    "account_type": "simulated",
    "authority": "none",
    "capital_layer": "simulated",
    "durability_scope": DURABILITY_SCOPE,
}
FIXTURE_TOP_LEVEL_KEYS = frozenset(
    {
        "contract",
        "fixture_id",
        "source_kind",
        "wire_contract",
        "symbol",
        "as_of",
        "metadata",
        "instrument",
        "bars_5m",
        "next_executable_quote",
        "llm_evidence",
    }
)
METADATA_KEYS = frozenset(
    {
        "state",
        "degraded",
        "freshness",
        "quality",
        "receipt_id",
        "observed_at",
        "data_through",
        "lineage",
    }
)
INSTRUMENT_KEYS = frozenset(
    {
        "base_asset",
        "quote_asset",
        "price_tick",
        "quantity_step",
        "min_quantity",
        "min_notional",
    }
)
BAR_KEYS = frozenset(
    {
        "symbol",
        "open_time",
        "close_time",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "closed",
    }
)
EXECUTABLE_QUOTE_KEYS = frozenset({"symbol", "observed_at", "bid", "ask"})
LLM_EVIDENCE_KEYS = frozenset(
    {"mode", "authority", "network_used", "evidence_id", "summary"}
)


class CryptoFixtureAutoSimError(RuntimeError):
    """Base error for the local fixture cycle."""


class CryptoSafetyError(CryptoFixtureAutoSimError):
    """Raised before side effects when a non-simulated path is requested."""


class CryptoEvidenceError(CryptoFixtureAutoSimError):
    """Raised when fixture evidence is not decision-eligible."""


class CryptoLedgerError(CryptoFixtureAutoSimError):
    """Raised when the append-only capital chain cannot be trusted."""


@dataclass(frozen=True)
class _JsonTreeVisit:
    path: str
    value: Any
    key: str | None
    depth: int


def _container_items(value: Any) -> list[tuple[str | int, Any]] | None:
    if is_dataclass(value) and not isinstance(value, type):
        return [(field.name, getattr(value, field.name)) for field in fields(value)]
    if isinstance(value, Mapping):
        normalized = [(str(key), item) for key, item in value.items()]
        keys = [key for key, _ in normalized]
        if len(keys) != len(set(keys)):
            raise CryptoEvidenceError("json_tree_key_collision")
        return normalized
    if isinstance(value, (list, tuple)):
        return list(enumerate(value))
    return None


def _validate_json_tree(
    value: Any,
    *,
    path: str = "value",
    forbidden_keys: frozenset[str] = frozenset(),
    route_tokens: Sequence[str] = (),
    max_values: int = JSON_TREE_MAX_VALUES,
    external: bool = False,
) -> tuple[_JsonTreeVisit, ...]:
    """Iteratively validate one bounded JSON-like tree.

    The root container is not charged against ``JSON_TREE_MAX_VALUES`` so a
    flat list or mapping with exactly 256 values remains valid. Container
    identity is tracked only while it is on the active DFS path: real cycles
    fail deterministically while harmless shared DAG nodes are accepted.
    """

    visits: list[_JsonTreeVisit] = []
    active: set[int] = set()
    value_count = 0
    stack: list[tuple[str, Any, str, int, str | None, bool]] = [
        ("enter", value, path, 0, None, True)
    ]
    while stack:
        phase, current, current_path, depth, key, is_root = stack.pop()
        if phase == "exit":
            active.remove(id(current))
            continue
        if depth > JSON_TREE_MAX_DEPTH:
            raise CryptoEvidenceError(f"json_tree_depth_exceeded:{current_path}")
        if external and type(current) not in {dict, list, str, int, bool, type(None)}:
            raise CryptoEvidenceError(
                f"json_tree_external_type_unsupported:{current_path}:{type(current).__name__}"
            )
        if type(current) in {dict, list, tuple} and len(current) > max_values:
            raise CryptoEvidenceError(f"json_tree_values_exceeded:{current_path}")
        if not is_root:
            value_count += 1
            if value_count > max_values:
                raise CryptoEvidenceError(f"json_tree_values_exceeded:{current_path}")
        visits.append(
            _JsonTreeVisit(
                path=current_path,
                value=current,
                key=key,
                depth=depth,
            )
        )
        items = _container_items(current)
        if items is None:
            if (
                not isinstance(current, (Decimal, datetime, str, int, bool))
                and current is not None
            ):
                raise CryptoEvidenceError(
                    f"json_tree_type_unsupported:{current_path}:{type(current).__name__}"
                )
            if isinstance(current, str):
                if len(current) > JSON_TREE_MAX_STRING_CHARS:
                    raise CryptoEvidenceError(
                        f"json_tree_string_too_long:{current_path}"
                    )
                lowered = current.lower()
                if any(token in lowered for token in route_tokens):
                    raise CryptoSafetyError(f"forbidden_fixture_route:{current_path}")
            continue
        identity = id(current)
        if identity in active:
            raise CryptoEvidenceError(f"json_tree_cycle_detected:{current_path}")
        active.add(identity)
        stack.append(("exit", current, current_path, depth, key, is_root))
        ordered = sorted(items, key=lambda item: str(item[0]))
        for child_key, child in reversed(ordered):
            normalized_key = str(child_key).strip().lower()
            if len(str(child_key)) > JSON_TREE_MAX_KEY_CHARS:
                raise CryptoEvidenceError(f"json_tree_key_too_long:{current_path}")
            if isinstance(current, (Mapping,)) or (
                is_dataclass(current) and not isinstance(current, type)
            ):
                if normalized_key in forbidden_keys:
                    raise CryptoSafetyError(
                        f"forbidden_fixture_key:{current_path}.{normalized_key}"
                    )
                child_path = f"{current_path}.{normalized_key}"
                visit_key: str | None = normalized_key
            else:
                child_path = f"{current_path}[{child_key}]"
                visit_key = None
            stack.append(("enter", child, child_path, depth + 1, visit_key, False))
    return tuple(visits)


def _decimal(
    value: Any,
    *,
    field_name: str,
    positive: bool = False,
    nonnegative: bool = False,
) -> Decimal:
    if isinstance(value, bool) or value in (None, ""):
        raise CryptoEvidenceError(f"{field_name}_invalid_decimal")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise CryptoEvidenceError(f"{field_name}_invalid_decimal") from exc
    if not parsed.is_finite():
        raise CryptoEvidenceError(f"{field_name}_invalid_decimal")
    if positive and parsed <= ZERO:
        raise CryptoEvidenceError(f"{field_name}_must_be_positive")
    if nonnegative and parsed < ZERO:
        raise CryptoEvidenceError(f"{field_name}_must_be_nonnegative")
    return parsed


def _canonical_value(value: Any) -> Any:
    _validate_json_tree(value, max_values=JSON_TREE_MAX_INTERNAL_VALUES)
    holder: list[Any] = [None]
    stack: list[tuple[Any, Any, str | int]] = [(value, holder, 0)]
    while stack:
        current, parent, slot = stack.pop()
        if isinstance(current, Decimal):
            parent[slot] = format(current, "f")
            continue
        if isinstance(current, datetime):
            parent[slot] = (
                current.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            )
            continue
        items = _container_items(current)
        if items is None:
            parent[slot] = current
            continue
        if isinstance(current, (list, tuple)):
            target: Any = [None] * len(items)
            parent[slot] = target
            for index, item in reversed(items):
                stack.append((item, target, int(index)))
            continue
        target = {}
        parent[slot] = target
        for key, item in reversed(items):
            stack.append((item, target, str(key)))
    return holder[0]


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _aware_utc(
    value: Any,
    *,
    field_name: str,
    require_minute_alignment: bool = True,
) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        raise CryptoEvidenceError(f"{field_name}_timestamp_required")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CryptoEvidenceError(f"{field_name}_timestamp_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CryptoEvidenceError(f"{field_name}_timezone_required")
    parsed = parsed.astimezone(timezone.utc)
    if require_minute_alignment and (parsed.second != 0 or parsed.microsecond != 0):
        raise CryptoEvidenceError(f"{field_name}_must_align_to_minute")
    return parsed


def _assert_simulation_only() -> None:
    value = str(os.environ.get("REAL_TRADING_ENABLED", "false")).strip().lower()
    if value not in {"", "0", "false"}:
        raise CryptoSafetyError("real_trading_enabled_must_be_false")


def _scan_forbidden_payload(value: Any, *, path: str = "fixture") -> None:
    _validate_json_tree(
        value,
        path=path,
        forbidden_keys=FORBIDDEN_KEYS,
        route_tokens=FORBIDDEN_ROUTE_TOKENS,
        external=True,
    )


def _require_exact_keys(
    value: Any,
    expected: frozenset[str],
    *,
    scope: str,
) -> Mapping[str, Any]:
    _validate_json_tree(value, path=scope, external=True)
    if not isinstance(value, Mapping):
        raise CryptoEvidenceError(f"{scope}_must_be_object")
    actual = {str(key) for key in value}
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        detail = f"missing={missing};unknown={unknown}"
        raise CryptoEvidenceError(f"{scope}_schema_mismatch:{detail}")
    return value


def _lineage_has_content(value: Any) -> bool:
    visits = _validate_json_tree(value, path="lineage", external=True)
    for visit in visits:
        current = visit.value
        items = _container_items(current)
        if items is not None:
            if not items or any(not str(key).strip() for key, _ in items):
                return False
            continue
        if isinstance(current, bool) or current is None or not str(current).strip():
            return False
    return True


def _nested_forbidden_keys(value: Any, forbidden: frozenset[str]) -> list[str]:
    visits = _validate_json_tree(value, path="nested", external=True)
    return sorted(
        visit.key
        for visit in visits
        if visit.key is not None and visit.key in forbidden
    )


def _state(value: Any) -> str:
    if isinstance(value, Mapping):
        return str(value.get("state") or value.get("status") or "").strip().lower()
    return str(value or "").strip().lower()


def _is_step_aligned(value: Decimal, step: Decimal) -> bool:
    return value % step == ZERO


def _floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    units = (value / step).to_integral_value(rounding=ROUND_DOWN)
    return units * step


def _ceil_to_step(value: Decimal, step: Decimal) -> Decimal:
    units = (value / step).to_integral_value(rounding=ROUND_UP)
    return units * step


def _non_authority_fields() -> dict[str, Any]:
    return {
        "execution_eligible": False,
        "execution_authority": False,
        "durable_execution_receipt": False,
        "production_eligible": False,
        "outbox_id": None,
        "capital_commit_id": None,
        "durability_scope": DURABILITY_SCOPE,
    }


def _assert_recursive_non_authority(value: Any, *, path: str) -> None:
    expected = _non_authority_fields()
    for visit in _validate_json_tree(
        value, path=path, max_values=JSON_TREE_MAX_INTERNAL_VALUES
    ):
        if not isinstance(visit.value, Mapping):
            continue
        for key, expected_value in expected.items():
            if key in visit.value and visit.value.get(key) != expected_value:
                raise CryptoSafetyError(
                    f"non_authority_field_invalid:{visit.path}.{key}"
                )
        for key in NON_AUTHORITY_FALSE_FIELDS:
            if key in visit.value and visit.value.get(key) is not False:
                raise CryptoSafetyError(
                    f"non_authority_field_invalid:{visit.path}.{key}"
                )
        for key in NON_AUTHORITY_NONE_FIELDS:
            if key in visit.value and visit.value.get(key) is not None:
                raise CryptoSafetyError(
                    f"non_authority_field_invalid:{visit.path}.{key}"
                )
        for key, expected_value in NON_AUTHORITY_LITERAL_FIELDS.items():
            if key in visit.value and visit.value.get(key) != expected_value:
                raise CryptoSafetyError(
                    f"non_authority_field_invalid:{visit.path}.{key}"
                )


def _assert_canonical_policy(policy: CryptoCapitalPolicy) -> None:
    if type(policy) is not CryptoCapitalPolicy or policy != CRYPTO_CAPITAL_POLICY:
        raise CryptoSafetyError("capital_policy_not_canonical")


@dataclass(frozen=True)
class SpotInstrumentRules:
    symbol: str
    base_asset: str
    quote_asset: str
    price_tick: Decimal
    quantity_step: Decimal
    min_quantity: Decimal
    min_notional: Decimal

    def __post_init__(self) -> None:
        if self.symbol not in ALLOWED_SYMBOLS:
            raise CryptoEvidenceError("instrument_symbol_not_allowed")
        if not self.symbol.endswith(self.quote_asset) or self.quote_asset != "USDT":
            raise CryptoEvidenceError("instrument_quote_asset_must_be_usdt")
        if not self.base_asset or self.symbol != f"{self.base_asset}{self.quote_asset}":
            raise CryptoEvidenceError("instrument_asset_binding_invalid")
        for name, value in (
            ("price_tick", self.price_tick),
            ("quantity_step", self.quantity_step),
            ("min_quantity", self.min_quantity),
            ("min_notional", self.min_notional),
        ):
            if not value.is_finite() or value <= ZERO:
                raise CryptoEvidenceError(f"instrument_{name}_must_be_positive")

    def to_payload(self) -> dict[str, Any]:
        return _canonical_value(self)


@dataclass(frozen=True)
class SpotBar5m:
    symbol: str
    open_time: datetime
    close_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    closed: bool

    def to_payload(self) -> dict[str, Any]:
        return _canonical_value(self)


@dataclass(frozen=True)
class ExecutableSpotQuote:
    symbol: str
    observed_at: datetime
    bid: Decimal
    ask: Decimal

    def __post_init__(self) -> None:
        if self.symbol not in ALLOWED_SYMBOLS:
            raise CryptoEvidenceError("execution_quote_symbol_invalid")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise CryptoEvidenceError("execution_quote_timezone_required")
        if any(
            not isinstance(value, Decimal) or not value.is_finite() or value <= ZERO
            for value in (self.bid, self.ask)
        ):
            raise CryptoEvidenceError("execution_quote_price_invalid")
        if self.ask < self.bid:
            raise CryptoEvidenceError("execution_quote_crossed")

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid

    def to_payload(self) -> dict[str, Any]:
        payload = _canonical_value(self)
        payload["spread"] = _canonical_value(self.spread)
        return payload


@dataclass(frozen=True)
class QualifiedFixtureEvidence:
    fixture_id: str
    source_kind: str
    symbol: str
    as_of: datetime
    receipt_id: str
    observed_at: datetime
    data_through: datetime
    lineage: Mapping[str, Any]
    rules: SpotInstrumentRules
    bars_5m: tuple[SpotBar5m, ...]
    next_executable_quote: ExecutableSpotQuote
    market_evidence_sha256: str
    llm_evidence_sha256: str | None
    llm_evidence_present: bool
    llm_evidence_payload: Mapping[str, Any] | None

    def qualification_payload(self) -> dict[str, Any]:
        return {
            "contract": QUALIFICATION_CONTRACT,
            "status": "qualified",
            "fixture_id": self.fixture_id,
            "source_kind": self.source_kind,
            "symbol": self.symbol,
            "as_of": _canonical_value(self.as_of),
            "receipt_id": self.receipt_id,
            "observed_at": _canonical_value(self.observed_at),
            "data_through": _canonical_value(self.data_through),
            "lineage": _canonical_value(self.lineage),
            "next_executable_quote": self.next_executable_quote.to_payload(),
            "wire_contract": WIRE_CONTRACT,
            "market_evidence_sha256": self.market_evidence_sha256,
            "llm_sidecar": {
                "storage": "separate_non_authority_journal",
                "authority": "none",
                "used_for_decision": False,
                "network_used": False,
            },
            **_non_authority_fields(),
            "real_trading_enabled": False,
        }


@dataclass(frozen=True)
class FrozenChampionCandidate:
    contract: str = CHAMPION_CONTRACT
    champion_id: str = "crypto-spot-15m-momentum-candidate-v1"
    version: int = 1
    status: str = "frozen_candidate"
    regime_interval: str = "1h"
    decision_interval: str = "15m"
    execution_interval: str = "5m"
    minimum_regime_return: Decimal = Decimal("0")
    minimum_decision_return: Decimal = Decimal("0.001")
    target_capital_pct: Decimal = Decimal("0.10")
    maximum_position_pct: Decimal = Decimal("0.15")
    manual_promotion_required: bool = True
    promotion_authorized: bool = False
    real_trading_enabled: bool = False

    def __post_init__(self) -> None:
        if type(self.version) is not int:
            raise ValueError("frozen Champion version must be an integer")
        for field_name, value in (
            ("minimum_regime_return", self.minimum_regime_return),
            ("minimum_decision_return", self.minimum_decision_return),
            ("target_capital_pct", self.target_capital_pct),
            ("maximum_position_pct", self.maximum_position_pct),
        ):
            if not isinstance(value, Decimal):
                raise ValueError(f"frozen Champion {field_name} must be Decimal")
        if (
            self.manual_promotion_required is not True
            or self.promotion_authorized is not False
            or self.real_trading_enabled is not False
        ):
            raise ValueError("frozen Champion safety booleans are immutable")
        frozen_fields = (
            self.contract,
            self.champion_id,
            self.version,
            self.status,
            self.regime_interval,
            self.decision_interval,
            self.execution_interval,
            self.minimum_regime_return,
            self.minimum_decision_return,
            self.target_capital_pct,
            self.maximum_position_pct,
            self.manual_promotion_required,
            self.promotion_authorized,
            self.real_trading_enabled,
        )
        expected = (
            CHAMPION_CONTRACT,
            "crypto-spot-15m-momentum-candidate-v1",
            1,
            "frozen_candidate",
            "1h",
            "15m",
            "5m",
            Decimal("0"),
            Decimal("0.001"),
            Decimal("0.10"),
            Decimal("0.15"),
            True,
            False,
            False,
        )
        if frozen_fields != expected:
            raise ValueError("frozen Champion fields are immutable")

    @property
    def sha256(self) -> str:
        return _sha256(self)

    def to_payload(self) -> dict[str, Any]:
        payload = _canonical_value(self)
        payload["sha256"] = self.sha256
        return payload


FROZEN_CHAMPION = FrozenChampionCandidate()


def _assert_canonical_champion(champion: FrozenChampionCandidate) -> None:
    if type(champion) is not FrozenChampionCandidate or champion != FROZEN_CHAMPION:
        raise CryptoSafetyError("frozen_champion_not_canonical")


@dataclass(frozen=True)
class TimeframeDecision:
    contract: str
    decision_id: str
    champion_id: str
    champion_sha256: str
    symbol: str
    regime_interval: str
    decision_interval: str
    execution_interval: str
    execution_slot: datetime
    decision_observed_at: datetime
    regime_return: Decimal
    decision_return: Decimal
    regime: str
    action: str
    reason: str
    evidence_receipt_id: str
    market_evidence_sha256: str
    promotion_authorized: bool = False
    real_trading_enabled: bool = False

    def __post_init__(self) -> None:
        if self.contract != DECISION_CONTRACT:
            raise CryptoEvidenceError("timeframe_decision_contract_invalid")
        if self.symbol not in ALLOWED_SYMBOLS:
            raise CryptoEvidenceError("timeframe_decision_symbol_invalid")
        if (
            self.regime_interval,
            self.decision_interval,
            self.execution_interval,
        ) != ("1h", "15m", "5m"):
            raise CryptoEvidenceError("timeframe_decision_intervals_invalid")
        if (
            self.execution_slot.tzinfo is None
            or self.execution_slot.utcoffset() is None
            or self.decision_observed_at.tzinfo is None
            or self.decision_observed_at.utcoffset() is None
        ):
            raise CryptoEvidenceError("timeframe_decision_timezone_required")
        if self.execution_slot < self.decision_observed_at:
            raise CryptoEvidenceError(
                "timeframe_decision_execution_precedes_observation"
            )
        if self.regime not in {"risk_on", "defensive"}:
            raise CryptoEvidenceError("timeframe_decision_regime_invalid")
        if self.action not in {"buy", "observe"}:
            raise CryptoEvidenceError("timeframe_decision_action_invalid")
        if not self.decision_id or not self.evidence_receipt_id:
            raise CryptoEvidenceError("timeframe_decision_binding_required")
        if len(self.champion_sha256) != 64 or len(self.market_evidence_sha256) != 64:
            raise CryptoEvidenceError("timeframe_decision_digest_invalid")
        if (
            self.promotion_authorized is not False
            or self.real_trading_enabled is not False
        ):
            raise CryptoSafetyError("timeframe_decision_cannot_promote_or_trade_live")

    def to_payload(self) -> dict[str, Any]:
        return _canonical_value(self)


@dataclass(frozen=True)
class OrderIntent:
    contract: str
    intent_id: str
    broker_contract: str
    authority_id: str
    authority_generation: int
    account_id: str
    symbol: str
    side: str
    order_type: str
    quantity: Decimal
    quote_bid: Decimal
    quote_ask: Decimal
    spread: Decimal
    slippage_bps: Decimal
    slippage_amount: Decimal
    reference_price: Decimal
    notional: Decimal
    fee_rate: Decimal
    maximum_fee: Decimal
    execution_slot: datetime
    evidence_receipt_id: str
    market_evidence_sha256: str
    champion_id: str
    champion_sha256: str
    decision_id: str
    capital_layer: str = "simulated"
    account_type: str = "simulated"
    real_trading_enabled: bool = False
    execution_eligible: bool = False
    execution_authority: bool = False
    durable_execution_receipt: bool = False
    production_eligible: bool = False
    outbox_id: str | None = None
    capital_commit_id: str | None = None
    durability_scope: str = DURABILITY_SCOPE

    def __post_init__(self) -> None:
        if self.contract != ORDER_INTENT_CONTRACT:
            raise CryptoEvidenceError("order_intent_contract_invalid")
        if self.broker_contract != PAPER_BROKER_CONTRACT:
            raise CryptoSafetyError("order_intent_broker_contract_invalid")
        if (
            self.authority_id != CRYPTO_CAPITAL_POLICY.authority_id
            or type(self.authority_generation) is not int
            or self.authority_generation != CRYPTO_CAPITAL_POLICY.generation
            or self.account_id != CRYPTO_CAPITAL_POLICY.account_id
        ):
            raise CryptoSafetyError("order_intent_capital_authority_invalid")
        if self.symbol not in ALLOWED_SYMBOLS or self.side != "buy":
            raise CryptoEvidenceError("order_intent_spot_binding_invalid")
        if self.order_type != "fixture_market_at_next_quote":
            raise CryptoEvidenceError("order_intent_order_type_invalid")
        for field_name, value in (
            ("quantity", self.quantity),
            ("quote_bid", self.quote_bid),
            ("quote_ask", self.quote_ask),
            ("reference_price", self.reference_price),
            ("notional", self.notional),
        ):
            if not isinstance(value, Decimal) or not value.is_finite() or value <= ZERO:
                raise CryptoEvidenceError(f"order_intent_{field_name}_invalid")
        if (
            self.quote_ask < self.quote_bid
            or self.spread != self.quote_ask - self.quote_bid
        ):
            raise CryptoEvidenceError("order_intent_spread_mismatch")
        if self.slippage_bps != FROZEN_SLIPPAGE_BPS:
            raise CryptoEvidenceError("order_intent_slippage_bps_invalid")
        if self.slippage_amount != self.reference_price - self.quote_ask:
            raise CryptoEvidenceError("order_intent_slippage_amount_mismatch")
        if self.slippage_amount < ZERO:
            raise CryptoEvidenceError("order_intent_negative_slippage")
        if (
            not isinstance(self.fee_rate, Decimal)
            or not self.fee_rate.is_finite()
            or self.fee_rate < ZERO
            or self.fee_rate > Decimal("0.1")
        ):
            raise CryptoEvidenceError("order_intent_fee_rate_invalid")
        if self.notional != self.quantity * self.reference_price:
            raise CryptoEvidenceError("order_intent_notional_mismatch")
        expected_fee = (self.notional * self.fee_rate).quantize(
            MONEY_QUANTUM, rounding=ROUND_UP
        )
        if self.maximum_fee != expected_fee:
            raise CryptoEvidenceError("order_intent_fee_mismatch")
        if (
            self.execution_slot.tzinfo is None
            or self.execution_slot.utcoffset() is None
        ):
            raise CryptoEvidenceError("order_intent_execution_slot_timezone_required")
        if not self.intent_id or not self.evidence_receipt_id or not self.decision_id:
            raise CryptoEvidenceError("order_intent_binding_required")
        if len(self.market_evidence_sha256) != 64 or len(self.champion_sha256) != 64:
            raise CryptoEvidenceError("order_intent_digest_invalid")
        if (
            self.capital_layer != "simulated"
            or self.account_type != "simulated"
            or self.real_trading_enabled is not False
            or self.execution_eligible is not False
            or self.execution_authority is not False
            or self.durable_execution_receipt is not False
            or self.production_eligible is not False
            or self.outbox_id is not None
            or self.capital_commit_id is not None
            or self.durability_scope != DURABILITY_SCOPE
        ):
            raise CryptoSafetyError("order_intent_must_remain_simulated")

    def to_payload(self) -> dict[str, Any]:
        return _canonical_value(self)


@dataclass(frozen=True)
class PaperFillReceipt:
    contract: str
    receipt_id: str
    broker_contract: str
    authority_id: str
    authority_generation: int
    account_id: str
    intent_id: str
    symbol: str
    side: str
    status: str
    filled_quantity: Decimal
    average_price: Decimal
    notional: Decimal
    fee: Decimal
    fee_asset: str
    filled_at: datetime
    evidence_receipt_id: str
    market_evidence_sha256: str
    capital_layer: str = "simulated"
    account_type: str = "simulated"
    real_trading_enabled: bool = False
    execution_eligible: bool = False
    execution_authority: bool = False
    durable_execution_receipt: bool = False
    production_eligible: bool = False
    outbox_id: str | None = None
    capital_commit_id: str | None = None
    durability_scope: str = DURABILITY_SCOPE

    def __post_init__(self) -> None:
        if self.contract != PAPER_RECEIPT_CONTRACT:
            raise CryptoEvidenceError("paper_receipt_contract_invalid")
        if self.broker_contract != PAPER_BROKER_CONTRACT:
            raise CryptoSafetyError("paper_receipt_broker_contract_invalid")
        if (
            self.authority_id != CRYPTO_CAPITAL_POLICY.authority_id
            or type(self.authority_generation) is not int
            or self.authority_generation != CRYPTO_CAPITAL_POLICY.generation
            or self.account_id != CRYPTO_CAPITAL_POLICY.account_id
        ):
            raise CryptoSafetyError("paper_receipt_capital_authority_invalid")
        if (
            self.symbol not in ALLOWED_SYMBOLS
            or self.side != "buy"
            or self.status != "fixture_simulated"
        ):
            raise CryptoEvidenceError("paper_receipt_spot_binding_invalid")
        for field_name, value in (
            ("filled_quantity", self.filled_quantity),
            ("average_price", self.average_price),
            ("notional", self.notional),
        ):
            if not isinstance(value, Decimal) or not value.is_finite() or value <= ZERO:
                raise CryptoEvidenceError(f"paper_receipt_{field_name}_invalid")
        if (
            not isinstance(self.fee, Decimal)
            or not self.fee.is_finite()
            or self.fee < ZERO
        ):
            raise CryptoEvidenceError("paper_receipt_fee_invalid")
        if self.notional != self.filled_quantity * self.average_price:
            raise CryptoEvidenceError("paper_receipt_notional_mismatch")
        if self.fee_asset != "USDT":
            raise CryptoEvidenceError("paper_receipt_fee_asset_invalid")
        if self.filled_at.tzinfo is None or self.filled_at.utcoffset() is None:
            raise CryptoEvidenceError("paper_receipt_filled_at_timezone_required")
        if not self.receipt_id or not self.intent_id or not self.evidence_receipt_id:
            raise CryptoEvidenceError("paper_receipt_binding_required")
        if len(self.market_evidence_sha256) != 64:
            raise CryptoEvidenceError("paper_receipt_digest_invalid")
        if (
            self.capital_layer != "simulated"
            or self.account_type != "simulated"
            or self.real_trading_enabled is not False
            or self.execution_eligible is not False
            or self.execution_authority is not False
            or self.durable_execution_receipt is not False
            or self.production_eligible is not False
            or self.outbox_id is not None
            or self.capital_commit_id is not None
            or self.durability_scope != DURABILITY_SCOPE
        ):
            raise CryptoSafetyError("paper_receipt_must_remain_simulated")

    def to_payload(self) -> dict[str, Any]:
        return _canonical_value(self)
