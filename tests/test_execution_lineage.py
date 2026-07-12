from __future__ import annotations

import pytest

from shared.execution.execution_lineage import (
    ASHARE_AUTHORITY_GENERATION,
    ASHARE_CAPITAL_AUTHORITY_ID,
    ASHARE_EXECUTION_LINEAGE_ID,
    ExecutionLineageError,
    build_execution_lineage,
    require_execution_lineage,
)


def test_fresh_ashare_lineage_is_namespaced_and_rejects_numeric_epoch_fields() -> None:
    lineage = build_execution_lineage(
        lineage_started_at="2026-07-12T00:00:00+08:00",
        point_in_time_as_of="2026-07-13T10:00:00+08:00",
    )

    assert lineage["capital_authority_id"] == ASHARE_CAPITAL_AUTHORITY_ID
    assert lineage["authority_generation"] == ASHARE_AUTHORITY_GENERATION
    assert lineage["execution_lineage_id"] == ASHARE_EXECUTION_LINEAGE_ID
    assert lineage["point_in_time_as_of"] == "2026-07-13T10:00:00+08:00"
    assert "capital_epoch" not in lineage

    assert require_execution_lineage(lineage) == lineage
    with pytest.raises(ExecutionLineageError, match="legacy_numeric_epoch_forbidden"):
        require_execution_lineage({**lineage, "capital_epoch": 2})
    with pytest.raises(ExecutionLineageError, match="authority_generation_mismatch"):
        require_execution_lineage({**lineage, "authority_generation": True})
