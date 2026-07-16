from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_legacy_opportunity_funnel_writer_is_unscheduled_and_hard_blocked() -> None:
    script = ROOT / "shared" / "wrappers" / "job_opportunity_funnel_sync.sh"

    text = script.read_text(encoding="utf-8")

    assert 'source "${SHARED_DIR}/env_loader.sh"' in text
    assert 'source "${WRAPPER_DIR}/_common.sh"' in text
    assert 'JOB_NAME="job_opportunity_funnel_sync"' in text
    assert "legacy_opportunity_funnel_writer_retired" in text
    assert "exit 78" in text
    assert "shared.runtime_test.sync_opportunity_funnel_events" not in text
    assert "--apply" not in text

    for crontab in (ROOT / "crontab.txt", ROOT / "shared" / "crontab.txt"):
        crontab_text = crontab.read_text(encoding="utf-8")
        assert "job_opportunity_funnel_sync.sh" not in crontab_text
