#!/usr/bin/env python3
"""Archive legacy A-share style ledgers that are no longer dashboard inputs."""

from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LEDGER_ROOT = ROOT / "shared" / "logs" / "sim_ledger" / "ashare"
DEFAULT_ARCHIVE_ROOT = ROOT / "shared" / "logs" / "archive" / "ashare_legacy_style_ledgers"
CANONICAL_STYLE = "ashare_sim"


def _file_count(path: Path) -> int:
    return sum(1 for item in path.rglob("*") if item.is_file())


def archive_legacy_ashare_ledgers(
    *,
    ledger_root: Path = DEFAULT_LEDGER_ROOT,
    archive_root: Path = DEFAULT_ARCHIVE_ROOT,
    apply: bool = False,
    batch_id: str | None = None,
) -> dict[str, Any]:
    batch = batch_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target_root = archive_root / batch
    candidates: list[dict[str, Any]] = []

    if not ledger_root.exists():
        return {
            "status": "pass",
            "applied": apply,
            "ledger_root": str(ledger_root),
            "archive_root": str(archive_root),
            "batch_id": batch,
            "moved_count": 0,
            "candidates": [],
            "message": "ledger_root_missing",
        }

    for item in sorted(ledger_root.iterdir()):
        if not item.is_dir() or item.name == CANONICAL_STYLE:
            continue
        if not any((item / name).exists() for name in ("positions.json", "trade_journal.jsonl", "cash_ledger.jsonl", "daily_mark_to_market.jsonl")):
            continue
        candidates.append(
            {
                "style": item.name,
                "source": str(item),
                "target": str(target_root / item.name),
                "file_count": _file_count(item),
            }
        )

    moved: list[dict[str, Any]] = []
    if apply and candidates:
        target_root.mkdir(parents=True, exist_ok=True)
        for candidate in candidates:
            source = Path(candidate["source"])
            target = Path(candidate["target"])
            if target.exists():
                raise FileExistsError(f"archive target already exists: {target}")
            shutil.move(str(source), str(target))
            moved.append(candidate)
        manifest = {
            "archive_type": "ashare_legacy_style_ledgers",
            "archived_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "canonical_style": CANONICAL_STYLE,
            "ledger_root": str(ledger_root),
            "moved": moved,
        }
        (target_root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    return {
        "status": "pass",
        "applied": apply,
        "ledger_root": str(ledger_root),
        "archive_root": str(archive_root),
        "batch_id": batch,
        "moved_count": len(moved),
        "candidate_count": len(candidates),
        "candidates": candidates,
        "manifest": str(target_root / "manifest.json") if apply and candidates else "",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Archive legacy A-share style sim ledgers.")
    parser.add_argument("--ledger-root", type=Path, default=DEFAULT_LEDGER_ROOT)
    parser.add_argument("--archive-root", type=Path, default=DEFAULT_ARCHIVE_ROOT)
    parser.add_argument("--batch-id", default="")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)
    result = archive_legacy_ashare_ledgers(
        ledger_root=args.ledger_root,
        archive_root=args.archive_root,
        batch_id=args.batch_id or None,
        apply=args.apply,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["archive_legacy_ashare_ledgers"]
