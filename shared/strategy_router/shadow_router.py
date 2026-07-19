"""Net shadow style opinions without allocating the 50k capital authority."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Tuple

from .contracts import (
    StyleId,
    StyleSleeveReceipt,
    StyleStance,
    StrategyRouterContractError,
)


class NetCandidateIntent(str, Enum):
    OPEN_CANDIDATE = "open_candidate"
    ABSTAIN = "abstain"


def _sha(value: object) -> str:
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
class StyleRouterRunReceipt:
    symbol: str
    decision_time: datetime
    base_snapshot_sha256: str
    champion_score_receipt_sha256: str
    sleeve_receipts: Tuple[StyleSleeveReceipt, ...]
    intent: NetCandidateIntent
    primary_style: StyleId | None
    supporting_styles: Tuple[StyleId, ...]
    disagreement: bool
    unique_evidence_group_count: int
    router_mode: str = "shadow_only"
    schema_version: str = "tradingagent.style_router_run_receipt.v1"
    decision_eligible: bool = False
    position_effect_allowed: bool = False
    order_effect_allowed: bool = False
    automatic_promotion_enabled: bool = False
    automatic_risk_expansion_enabled: bool = False
    live_transition_authorized: bool = False
    run_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.sleeve_receipts, tuple) or not self.sleeve_receipts:
            raise StrategyRouterContractError("style_receipt_set_invalid")
        if any(
            not isinstance(item, StyleSleeveReceipt) for item in self.sleeve_receipts
        ):
            raise StrategyRouterContractError("style_receipt_set_invalid")
        styles = tuple(item.style_id for item in self.sleeve_receipts)
        if len(styles) != len(set(styles)):
            raise StrategyRouterContractError("style_receipt_duplicate")
        ordered = tuple(
            sorted(self.sleeve_receipts, key=lambda item: item.style_id.value)
        )
        object.__setattr__(self, "sleeve_receipts", ordered)

        bindings = {
            (
                item.symbol,
                item.decision_time,
                item.base_snapshot_sha256,
                item.champion_score_receipt_sha256,
            )
            for item in ordered
        }
        supplied_binding = (
            self.symbol,
            self.decision_time,
            self.base_snapshot_sha256,
            self.champion_score_receipt_sha256,
        )
        if len(bindings) != 1 or supplied_binding not in bindings:
            raise StrategyRouterContractError("style_router_binding_mismatch")
        if (
            not isinstance(self.decision_time, datetime)
            or self.decision_time.tzinfo is None
            or self.decision_time.utcoffset() is None
        ):
            raise StrategyRouterContractError("style_router_binding_mismatch")
        object.__setattr__(
            self,
            "decision_time",
            self.decision_time.astimezone(timezone.utc),
        )

        evidence_groups: dict[str, str] = {}
        for sleeve in ordered:
            for evidence in sleeve.evidence_groups:
                previous = evidence_groups.get(evidence.group_id)
                if previous is not None and previous != evidence.source_receipt_sha256:
                    raise StrategyRouterContractError("evidence_group_receipt_conflict")
                evidence_groups[evidence.group_id] = evidence.source_receipt_sha256
        if (
            isinstance(self.unique_evidence_group_count, bool)
            or not isinstance(self.unique_evidence_group_count, int)
            or self.unique_evidence_group_count != len(evidence_groups)
        ):
            raise StrategyRouterContractError("style_router_binding_mismatch")

        supporting = tuple(
            sorted(
                (item for item in ordered if item.stance is StyleStance.SUPPORT),
                key=lambda item: (-item.raw_style_score, item.style_id.value),
            )
        )
        opposing = tuple(item for item in ordered if item.stance is StyleStance.OPPOSE)
        expected_disagreement = bool(supporting and opposing)
        if supporting and not opposing:
            expected_intent = NetCandidateIntent.OPEN_CANDIDATE
            expected_primary = supporting[0].style_id
            expected_supporting = tuple(item.style_id for item in supporting[1:])
        else:
            expected_intent = NetCandidateIntent.ABSTAIN
            expected_primary = None
            expected_supporting = ()
        if (
            self.intent is not expected_intent
            or self.primary_style is not expected_primary
            or self.supporting_styles != expected_supporting
            or self.disagreement is not expected_disagreement
        ):
            raise StrategyRouterContractError("style_router_derived_fields_invalid")
        if (
            self.router_mode != "shadow_only"
            or self.schema_version != "tradingagent.style_router_run_receipt.v1"
            or self.decision_eligible is not False
            or self.position_effect_allowed is not False
            or self.order_effect_allowed is not False
            or self.automatic_promotion_enabled is not False
            or self.automatic_risk_expansion_enabled is not False
            or self.live_transition_authorized is not False
        ):
            raise StrategyRouterContractError("style_router_boundary_invalid")
        object.__setattr__(self, "run_sha256", _sha(self.canonical_payload()))

    def canonical_payload(self) -> dict[str, object]:
        return {
            "automatic_promotion_enabled": False,
            "automatic_risk_expansion_enabled": False,
            "base_snapshot_sha256": self.base_snapshot_sha256,
            "champion_score_receipt_sha256": self.champion_score_receipt_sha256,
            "decision_eligible": False,
            "decision_time": self.decision_time.isoformat(),
            "disagreement": self.disagreement,
            "intent": self.intent.value,
            "live_transition_authorized": False,
            "order_effect_allowed": False,
            "position_effect_allowed": False,
            "primary_style": (
                self.primary_style.value if self.primary_style is not None else None
            ),
            "router_mode": self.router_mode,
            "schema_version": self.schema_version,
            "sleeve_receipts": [
                item.canonical_payload() for item in self.sleeve_receipts
            ],
            "supporting_styles": [item.value for item in self.supporting_styles],
            "symbol": self.symbol,
            "unique_evidence_group_count": self.unique_evidence_group_count,
        }


def route_shadow_styles(
    sleeves: Tuple[StyleSleeveReceipt, ...],
) -> StyleRouterRunReceipt:
    """Produce exactly one non-capital intent for one symbol and snapshot."""

    if not isinstance(sleeves, tuple) or not sleeves:
        raise StrategyRouterContractError("style_receipt_set_invalid")
    if any(not isinstance(item, StyleSleeveReceipt) for item in sleeves):
        raise StrategyRouterContractError("style_receipt_set_invalid")
    styles = tuple(item.style_id for item in sleeves)
    if len(styles) != len(set(styles)):
        raise StrategyRouterContractError("style_receipt_duplicate")
    bindings = {
        (
            item.symbol,
            item.decision_time,
            item.base_snapshot_sha256,
            item.champion_score_receipt_sha256,
        )
        for item in sleeves
    }
    if len(bindings) != 1:
        raise StrategyRouterContractError("style_receipt_binding_mismatch")
    ordered = tuple(sorted(sleeves, key=lambda item: item.style_id.value))
    evidence_groups: dict[str, str] = {}
    for sleeve in ordered:
        for evidence in sleeve.evidence_groups:
            previous = evidence_groups.get(evidence.group_id)
            if previous is not None and previous != evidence.source_receipt_sha256:
                raise StrategyRouterContractError("evidence_group_receipt_conflict")
            evidence_groups[evidence.group_id] = evidence.source_receipt_sha256

    supporting = tuple(
        sorted(
            (item for item in ordered if item.stance is StyleStance.SUPPORT),
            key=lambda item: (-item.raw_style_score, item.style_id.value),
        )
    )
    opposing = tuple(item for item in ordered if item.stance is StyleStance.OPPOSE)
    disagreement = bool(supporting and opposing)
    if supporting and not opposing:
        intent = NetCandidateIntent.OPEN_CANDIDATE
        primary_style = supporting[0].style_id
        supporting_styles = tuple(item.style_id for item in supporting[1:])
    else:
        intent = NetCandidateIntent.ABSTAIN
        primary_style = None
        supporting_styles = ()
    symbol, decision_time, snapshot_sha, champion_sha = next(iter(bindings))
    return StyleRouterRunReceipt(
        symbol=symbol,
        decision_time=decision_time,
        base_snapshot_sha256=snapshot_sha,
        champion_score_receipt_sha256=champion_sha,
        sleeve_receipts=ordered,
        intent=intent,
        primary_style=primary_style,
        supporting_styles=supporting_styles,
        disagreement=disagreement,
        unique_evidence_group_count=len(evidence_groups),
    )


__all__ = [
    "NetCandidateIntent",
    "StyleRouterRunReceipt",
    "route_shadow_styles",
]
