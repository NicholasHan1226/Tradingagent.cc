"""Shadow-only opportunity intelligence contracts.

This package deliberately has no portfolio, capital, risk, execution or order
dependency.  It can discover and track research opportunities, but it cannot
turn them into TradingAgent V1 candidates.
"""

from .contracts import (
    OpportunityContractError,
    OpportunityEvidenceRef,
    OpportunityScope,
    OpportunitySnapshot,
    OpportunityState,
    transition_opportunity,
)
from .radar import (
    FrozenOpportunityRadar,
    OpportunityBatch,
    OpportunityCoverageVerification,
    OpportunityScanRow,
)

__all__ = [
    "FrozenOpportunityRadar",
    "OpportunityBatch",
    "OpportunityContractError",
    "OpportunityCoverageVerification",
    "OpportunityEvidenceRef",
    "OpportunityScanRow",
    "OpportunityScope",
    "OpportunitySnapshot",
    "OpportunityState",
    "transition_opportunity",
]
