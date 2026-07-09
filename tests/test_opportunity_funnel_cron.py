from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_opportunity_funnel_sync_wrapper_uses_governed_cron_entrypoint() -> None:
    script = ROOT / "shared" / "wrappers" / "job_opportunity_funnel_sync.sh"

    text = script.read_text(encoding="utf-8")

    assert 'source "${SHARED_DIR}/env_loader.sh"' in text
    assert 'source "${WRAPPER_DIR}/_common.sh"' in text
    assert 'JOB_NAME="job_opportunity_funnel_sync"' in text
    assert 'PHASE="review"' in text
    assert 'LEVEL3_TARGET="opportunity_funnel_sync"' in text
    assert "shared.runtime_test.sync_opportunity_funnel_events" in text
    assert "--apply" in text
