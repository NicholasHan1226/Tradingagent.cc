#!/usr/bin/env python3
"""Archive reviewed historical failed/expired signal cards out of active queues."""

from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shared.runtime_test import ops_report

ROOT = Path(__file__).resolve().parents[2]
SIGNALS = ROOT / "signals"
OPS_REVIEW = ROOT / "shared" / "review" / "ops"
ACTIVE_STATES = ("pending", "claimed", "running")
REVIEW_STATES = ("failed", "expired")


def now_batch() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def active_counts() -> dict[str, int]:
    return {state: len(list((SIGNALS / state).glob("*.json"))) for state in ACTIVE_STATES}


def candidate_files() -> list[tuple[str, Path]]:
    rows: list[tuple[str, Path]] = []
    for state in REVIEW_STATES:
        for path in sorted((SIGNALS / state).glob("*.json")):
            rows.append((state, path))
    return rows


def build_manifest(batch_id: str, reason: str, files: list[tuple[str, Path]]) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    for state, path in files:
        card = read_json(path)
        receipt = card.get("receipt") if isinstance(card.get("receipt"), dict) else {}
        records.append({
            "source_state": state,
            "source_path": str(path.relative_to(ROOT)),
            "target_path": str((SIGNALS / "reviewed" / batch_id / state / path.name).relative_to(ROOT)),
            "order_id": card.get("order_id") or card.get("execution_id") or receipt.get("execution_id") or path.stem,
            "code": card.get("ts_code") or card.get("code") or card.get("symbol") or receipt.get("code"),
            "category": ops_report.classify_failure({**card, "_path": str(path.relative_to(ROOT))}),
            "status": card.get("status") or receipt.get("status"),
            "message": str(card.get("message") or receipt.get("message") or card.get("reason") or "")[:240],
        })
    return {
        "batch_id": batch_id,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "reason": reason,
        "active_counts_before": active_counts(),
        "source_states": list(REVIEW_STATES),
        "record_count": len(records),
        "records": records,
        "rollback": "Move each target_path back to source_path from this manifest.",
    }


def archive_reviewed(batch_id: str, reason: str, apply: bool) -> dict[str, Any]:
    files = candidate_files()
    manifest = build_manifest(batch_id, reason, files)
    manifest["dry_run"] = not apply
    manifest["applied"] = False
    if apply:
        if any(active_counts().values()):
            raise SystemExit(f"active pending/claimed/running queue is not empty: {active_counts()}")
        for record in manifest["records"]:
            src = ROOT / record["source_path"]
            dst = ROOT / record["target_path"]
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.exists():
                shutil.move(str(src), str(dst))
        manifest["active_counts_after"] = active_counts()
        manifest["applied"] = True
    OPS_REVIEW.mkdir(parents=True, exist_ok=True)
    review_path = OPS_REVIEW / f"reviewed_signal_archive_{batch_id}.json"
    review_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if apply:
        reviewed_manifest = SIGNALS / "reviewed" / batch_id / "manifest.json"
        reviewed_manifest.parent.mkdir(parents=True, exist_ok=True)
        reviewed_manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest["manifest_path"] = str(review_path)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--batch-id", default=now_batch())
    parser.add_argument("--reason", default="reviewed historical failed/expired signals")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()
    payload = archive_reviewed(args.batch_id, args.reason, args.apply)
    print(json.dumps(payload, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
