from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from shared.execution.execution_lineage import ASHARE_EXECUTION_LINEAGE_ID


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "tools"
    / "ashare_fresh_authority_bootstrap.py"
)
DECISION_ID = "nicholas-fresh-start-019f5040-20260712"


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _inputs(tmp_path: Path, *, trade_date: str = "20260713") -> tuple[Path, Path]:
    source = tmp_path / "opening_source.json"
    source.write_text('{"cash":50000,"positions":{}}\n', encoding="utf-8")
    opening = tmp_path / "opening_manifest.json"
    opening.write_text(
        json.dumps(
            {
                "market": "ashare",
                "authority_id": "ashare-capital-v1",
                "cutover_decision_id": DECISION_ID,
                "mode": "fresh_start",
                "as_of": trade_date,
                "cash_balance_cny": 50000.0,
                "opening_equity_cny": 50000.0,
                "active_reservations_cny": 0.0,
                "consecutive_losses": 0,
                "inherited_high_water_equity_cny": 0.0,
                "positions_by_risk_unit": {},
                "position_margin_by_risk_unit": {},
                "frozen_order_cash_cny": 0.0,
                "realized_pnl_cny": 0.0,
                "unrealized_pnl_cny": 0.0,
                "source": str(source),
                "source_sha256": _sha(source.read_bytes()),
                "execution_lineage_id": "old-random-lineage",
                "real": False,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    archive = tmp_path / "legacy-archive"
    archive.mkdir()
    events = tmp_path / "legacy.jsonl"
    events.write_text('{"event_id":"OLD-1"}\n', encoding="utf-8")
    legacy = tmp_path / "legacy_freeze.json"
    legacy.write_text(
        json.dumps(
            {
                "events_path": str(events),
                "sha256": _sha(events.read_bytes()),
                "last_event_id": "OLD-1",
                "row_count": 1,
                "frozen_at": "2026-07-12T20:00:00+08:00",
                "archive_path": str(archive),
                "imported": False,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return opening, legacy


def _run(
    tmp_path: Path, *extra: str, env: dict[str, str] | None = None
) -> tuple[subprocess.CompletedProcess[str], dict]:
    now_at = datetime.now(timezone(timedelta(hours=8)))
    opening, legacy = _inputs(tmp_path, trade_date=now_at.strftime("%Y%m%d"))
    now = now_at.isoformat(timespec="seconds")
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--capital-root",
            str(tmp_path / "capital"),
            "--execution-root",
            str(tmp_path / "execution" / ASHARE_EXECUTION_LINEAGE_ID),
            "--source-opening-manifest",
            str(opening),
            "--legacy-freeze-manifest",
            str(legacy),
            "--output-opening-manifest",
            str(tmp_path / "evidence" / "opening_manifest.json"),
            "--lineage-started-at",
            now,
            "--point-in-time-as-of",
            now,
            "--confirm-zero-import",
            *extra,
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    return result, json.loads(result.stdout)


def test_dry_run_validates_without_writing(tmp_path: Path) -> None:
    result, payload = _run(tmp_path)

    assert result.returncode == 0
    assert payload["status"] == "validated_dry_run"
    assert payload["execution_lineage_id"] == ASHARE_EXECUTION_LINEAGE_ID
    assert not (tmp_path / "capital").exists()
    assert not (tmp_path / "execution").exists()
    assert not (tmp_path / "evidence").exists()


def test_apply_builds_two_zero_import_fixed_lineage_authorities(tmp_path: Path) -> None:
    result, payload = _run(tmp_path, "--apply")

    assert result.returncode == 0, result.stderr
    assert payload["status"] == "staged"
    assert payload["capital_checksum"]["status"] == "valid"
    assert payload["execution_manifest_status"] == "ready"
    assert payload["fresh"] is False
    assert payload["reconciled"] is False
    assert payload["real_trading_enabled"] is False

    opening = json.loads(
        (tmp_path / "evidence" / "opening_manifest.json").read_text("utf-8")
    )
    event = json.loads(
        (tmp_path / "capital" / "ashare_sim_capital_events.jsonl")
        .read_text("utf-8")
        .splitlines()[0]
    )
    execution = json.loads(
        (
            tmp_path
            / "execution"
            / ASHARE_EXECUTION_LINEAGE_ID
            / "execution_lineage_manifest.json"
        ).read_text("utf-8")
    )
    assert opening["execution_lineage_id"] == ASHARE_EXECUTION_LINEAGE_ID
    assert event["execution_lineage_id"] == ASHARE_EXECUTION_LINEAGE_ID
    assert event["cash_balance_cny"] == 50_000.0
    assert event["positions_by_risk_unit"] == {}
    assert execution["execution_lineage_id"] == ASHARE_EXECUTION_LINEAGE_ID
    assert execution["imported_legacy_record_count"] == 0
    assert execution["legacy_roots_read"] == []
    assert execution["real_trading_enabled"] is False


def test_rejects_nonzero_opening_state_before_writing(tmp_path: Path) -> None:
    now_at = datetime.now(timezone(timedelta(hours=8)))
    opening, _ = _inputs(tmp_path, trade_date=now_at.strftime("%Y%m%d"))
    payload = json.loads(opening.read_text("utf-8"))
    payload["positions_by_risk_unit"] = {"600000.SH": 100.0}
    opening.write_text(json.dumps(payload), encoding="utf-8")
    now = now_at.isoformat(timespec="seconds")
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--capital-root",
            str(tmp_path / "capital"),
            "--execution-root",
            str(tmp_path / "execution" / ASHARE_EXECUTION_LINEAGE_ID),
            "--source-opening-manifest",
            str(opening),
            "--legacy-freeze-manifest",
            str(tmp_path / "legacy_freeze.json"),
            "--output-opening-manifest",
            str(tmp_path / "evidence" / "opening_manifest.json"),
            "--lineage-started-at",
            now,
            "--point-in-time-as-of",
            now,
            "--confirm-zero-import",
            "--apply",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    output = json.loads(result.stdout)
    assert result.returncode == 2
    assert "opening_manifest_not_zero_import:positions" in output["blockers"]
    assert not (tmp_path / "capital").exists()
    assert not (tmp_path / "execution").exists()


def test_rejects_real_trading_environment(tmp_path: Path) -> None:
    env = dict(os.environ)
    env["REAL_TRADING_ENABLED"] = "true"
    result, payload = _run(tmp_path, "--apply", env=env)

    assert result.returncode == 2
    assert payload["blockers"] == ["environment_real_trading_requested"]
    assert not (tmp_path / "capital").exists()
