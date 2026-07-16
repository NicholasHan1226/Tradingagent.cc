"""Shadow-only multi-style evidence routing.

The package never owns cash, sub-accounts, positions or orders.  It only
normalizes independent research sleeves into one net candidate-level opinion.
"""

from .contracts import (
    EvidenceGroupRef,
    StyleId,
    StyleSleeveReceipt,
    StyleStance,
    StrategyRouterContractError,
)
from .shadow_router import (
    NetCandidateIntent,
    StyleRouterRunReceipt,
    route_shadow_styles,
)

__all__ = [
    "EvidenceGroupRef",
    "NetCandidateIntent",
    "StyleId",
    "StyleRouterRunReceipt",
    "StyleSleeveReceipt",
    "StyleStance",
    "StrategyRouterContractError",
    "route_shadow_styles",
]
