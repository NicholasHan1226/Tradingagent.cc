#!/usr/bin/env python3
"""Read-only data access adapters for TradingAgent.

The TradingDatas V1 catalog/query contract is the only current architecture
surface. The reader exports below are a time-boxed compatibility inventory;
new code must not import them.
"""

from .evidence_gate import (
    DataEvidenceGate,
    DatasetEvidencePolicy,
    EvidenceAction,
    EvidenceDecision,
)

from .reader import (
    MarketGraphCSVReader,
    SharedSignalsReader,
    TradingagentDataReader,
)
from .sharedsignals_v1 import (
    CatalogEnvelope,
    QueryEnvelope,
    QueryRequest,
    SharedSignalsV1Client,
    SharedSignalsV1Config,
)

__all__ = [
    "CatalogEnvelope",
    "DataEvidenceGate",
    "DatasetEvidencePolicy",
    "EvidenceAction",
    "EvidenceDecision",
    "MarketGraphCSVReader",
    "QueryEnvelope",
    "QueryRequest",
    "SharedSignalsReader",
    "SharedSignalsV1Client",
    "SharedSignalsV1Config",
    "TradingagentDataReader",
]
