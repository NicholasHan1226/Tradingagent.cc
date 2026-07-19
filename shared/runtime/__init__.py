"""Simulation-only runtime composition contracts."""

from .day_loop import (
    ASharePaperDayLoop,
    DayLoopError,
    FaultPoint,
    MemoryRunBundleStore,
    StageRequest,
    StageResult,
)
from .file_store import FileRunBundleStore, RunBundleStoreCorruption
from .run_bundle import (
    ComponentIdentity,
    RunBundle,
    RunContext,
    RunStage,
    STAGE_ORDER,
    StageReceipt,
)
from .stage_ports import (
    SampleJournalLearningPort,
    SharedSignalsResearchEvidencePort,
    StagePortContractError,
)

__all__ = [
    "ASharePaperDayLoop",
    "ComponentIdentity",
    "DayLoopError",
    "FaultPoint",
    "FileRunBundleStore",
    "MemoryRunBundleStore",
    "RunBundle",
    "RunBundleStoreCorruption",
    "RunContext",
    "RunStage",
    "STAGE_ORDER",
    "StageReceipt",
    "StageRequest",
    "StageResult",
    "SampleJournalLearningPort",
    "SharedSignalsResearchEvidencePort",
    "StagePortContractError",
]
