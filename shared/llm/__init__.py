"""Optional, evidence-only LLM sidecar.

This package has no authority over portfolio, risk, TradeIntent, orders, or
broker operations.  Provider output must first become a versioned
``LLMEvidenceObservation``.
"""

from .deepseek_config import DeepSeekProviderConfig, DeepSeekProviderConfigError
from .gateway import (
    DeepSeekAdapter,
    GatewayAnalysisResult,
    LLMEvidenceGateway,
    OfflineDeepSeekFixtureTransport,
    ProviderEvidenceBindingError,
    ProviderOutputSensitiveError,
    ProviderTransportReceipt,
    ProviderTransportReceiptError,
)
from .evidence_journal import (
    EMPTY_LLM_EVIDENCE_JOURNAL_HEAD_SHA256,
    LLMEvidenceEnvelope,
    LLMEvidenceEnvelopeError,
    LLMEvidenceJournal,
    LLMEvidenceJournalError,
    LLMEvidenceJournalReadback,
)
from .router import LLMRouter, ModelRoute
from .schema import LLMEvidenceRequest

__all__ = [
    "DeepSeekAdapter",
    "DeepSeekProviderConfig",
    "DeepSeekProviderConfigError",
    "EMPTY_LLM_EVIDENCE_JOURNAL_HEAD_SHA256",
    "GatewayAnalysisResult",
    "LLMEvidenceEnvelope",
    "LLMEvidenceEnvelopeError",
    "LLMEvidenceGateway",
    "LLMEvidenceJournal",
    "LLMEvidenceJournalError",
    "LLMEvidenceJournalReadback",
    "LLMEvidenceRequest",
    "LLMRouter",
    "ModelRoute",
    "OfflineDeepSeekFixtureTransport",
    "ProviderEvidenceBindingError",
    "ProviderOutputSensitiveError",
    "ProviderTransportReceipt",
    "ProviderTransportReceiptError",
]
