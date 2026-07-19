"""Immutable style-sleeve receipts with no capital or execution authority."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Tuple

from shared.universe.policy import InstrumentRole, classify_instrument


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class StrategyRouterContractError(ValueError):
    """Raised when a style receipt attempts to acquire decision authority."""


class StyleId(str, Enum):
    INDUSTRY_TREND = "industry_trend"
    EVENT_SURPRISE = "event_surprise"
    CROSS_MARKET_DISLOCATION = "cross_market_dislocation"


class StyleStance(str, Enum):
    SUPPORT = "support"
    OPPOSE = "oppose"
    ABSTAIN = "abstain"


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise StrategyRouterContractError(f"{field_name}_invalid")
    return value


def _aware(value: object, field_name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise StrategyRouterContractError(f"{field_name}_timezone_required")
    return value.astimezone(timezone.utc)


def _sha_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise StrategyRouterContractError(f"{field_name}_invalid")
    return value


def _canonical_sha(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class EvidenceGroupRef:
    """One deduplication group backed by one immutable source receipt."""

    group_id: str
    source_receipt_sha256: str

    def __post_init__(self) -> None:
        _text(self.group_id, "evidence_group_id")
        _sha_text(self.source_receipt_sha256, "source_receipt_sha256")

    def canonical_payload(self) -> dict[str, str]:
        return {
            "group_id": self.group_id,
            "source_receipt_sha256": self.source_receipt_sha256,
        }


@dataclass(frozen=True)
class StyleSleeveReceipt:
    """One style's evidence opinion on the same frozen Champion candidate."""

    style_id: StyleId
    style_version: str
    lifecycle: str
    symbol: str
    decision_time: datetime
    base_snapshot_sha256: str
    champion_score_receipt_sha256: str
    stance: StyleStance
    raw_style_score: float
    calibrated_probability: None
    evidence_groups: Tuple[EvidenceGroupRef, ...]
    reason_codes: Tuple[str, ...]
    score_semantics: str = "uncalibrated_heuristic"
    schema_version: str = "tradingagent.style_sleeve_receipt.v1"
    shadow_only: bool = True
    decision_eligible: bool = False
    position_effect_allowed: bool = False
    order_effect_allowed: bool = False
    promotion_eligible: bool = False
    receipt_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.style_id, StyleId):
            raise StrategyRouterContractError("style_id_invalid")
        if not isinstance(self.stance, StyleStance):
            raise StrategyRouterContractError("style_stance_invalid")
        _text(self.style_version, "style_version")
        if self.lifecycle != "shadow":
            raise StrategyRouterContractError("style_lifecycle_must_be_shadow")
        _text(self.symbol, "symbol")
        eligibility = classify_instrument(self.symbol, instrument_type="common_stock")
        if eligibility.role is not InstrumentRole.MAINBOARD_COMMON_STOCK:
            raise StrategyRouterContractError("style_symbol_not_mainboard")
        decision_time = _aware(self.decision_time, "decision_time")
        object.__setattr__(self, "decision_time", decision_time)
        _sha_text(self.base_snapshot_sha256, "base_snapshot_sha256")
        _sha_text(
            self.champion_score_receipt_sha256,
            "champion_score_receipt_sha256",
        )
        if (
            isinstance(self.raw_style_score, bool)
            or not isinstance(self.raw_style_score, (int, float))
            or not math.isfinite(float(self.raw_style_score))
            or not 0.0 <= float(self.raw_style_score) <= 1.0
        ):
            raise StrategyRouterContractError("raw_style_score_invalid")
        object.__setattr__(self, "raw_style_score", float(self.raw_style_score))
        if self.calibrated_probability is not None:
            raise StrategyRouterContractError("calibrated_probability_forbidden")
        if not isinstance(self.evidence_groups, tuple) or not self.evidence_groups:
            raise StrategyRouterContractError("evidence_groups_invalid")
        if any(not isinstance(item, EvidenceGroupRef) for item in self.evidence_groups):
            raise StrategyRouterContractError("evidence_groups_invalid")
        group_ids = tuple(item.group_id for item in self.evidence_groups)
        if len(group_ids) != len(set(group_ids)):
            raise StrategyRouterContractError("evidence_group_duplicate_in_sleeve")
        object.__setattr__(
            self,
            "evidence_groups",
            tuple(sorted(self.evidence_groups, key=lambda item: item.group_id)),
        )
        if not isinstance(self.reason_codes, tuple) or not self.reason_codes:
            raise StrategyRouterContractError("reason_codes_invalid")
        if len(self.reason_codes) != len(set(self.reason_codes)):
            raise StrategyRouterContractError("reason_codes_invalid")
        for reason in self.reason_codes:
            _text(reason, "reason_code")
        if (
            self.score_semantics != "uncalibrated_heuristic"
            or self.schema_version != "tradingagent.style_sleeve_receipt.v1"
            or self.shadow_only is not True
            or self.decision_eligible is not False
            or self.position_effect_allowed is not False
            or self.order_effect_allowed is not False
            or self.promotion_eligible is not False
        ):
            raise StrategyRouterContractError("style_shadow_boundary_invalid")
        object.__setattr__(
            self, "receipt_sha256", _canonical_sha(self.canonical_payload())
        )

    def canonical_payload(self) -> dict[str, object]:
        return {
            "base_snapshot_sha256": self.base_snapshot_sha256,
            "calibrated_probability": None,
            "champion_score_receipt_sha256": self.champion_score_receipt_sha256,
            "decision_eligible": False,
            "decision_time": self.decision_time.isoformat(),
            "evidence_groups": [
                item.canonical_payload() for item in self.evidence_groups
            ],
            "lifecycle": self.lifecycle,
            "order_effect_allowed": False,
            "position_effect_allowed": False,
            "promotion_eligible": False,
            "raw_style_score": self.raw_style_score,
            "reason_codes": list(self.reason_codes),
            "schema_version": self.schema_version,
            "score_semantics": self.score_semantics,
            "shadow_only": True,
            "stance": self.stance.value,
            "style_id": self.style_id.value,
            "style_version": self.style_version,
            "symbol": self.symbol,
        }


__all__ = [
    "EvidenceGroupRef",
    "StyleId",
    "StyleSleeveReceipt",
    "StyleStance",
    "StrategyRouterContractError",
]
