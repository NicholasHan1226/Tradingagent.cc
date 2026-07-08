#!/usr/bin/env python3
"""Quarantine legacy USD capital samples in US/Crypto/PM simulated ledgers.

Background: US/Crypto/PM starting capital changed from ~200,000 CNY
(≈27,778 USD) to 10,000 original-currency units (= 72,000 CNY).
Historical ``daily_mark_to_market.jsonl``, ``trade_journal.jsonl``,
``style_performance.jsonl``, and ``style_comparison.json`` may still
contain rows with the old capital_base, polluting dashboard PnL.

This tool detects those rows and adds quarantine markers:
  ``exclude_from_dashboard=true``,
  ``run_context=legacy_usd_capital_quarantine``,
  ``quarantine_reason`` (with old/new capital values).

- Default dry-run — no files modified.
- ``--apply`` backs up each file as ``<name>.bak`` before writing.
- Only adds markers; never deletes rows.
- Skips A-share, CNFutures, HK, and real_execution=true rows.
- Idempotent — already-quarantined rows are counted but not re-modified.
"""

from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LEDGER_ROOT = ROOT / "shared" / "logs" / "sim_ledger"
DEFAULT_REVIEW_ROOT = ROOT / "shared" / "review"

# Markets in scope — only USD-denominated simulated markets
TARGET_MARKETS: tuple[str, ...] = ("us", "crypto", "pm")

# Markets explicitly excluded (also skip hk, ashare, cn_futures etc.)
EXCLUDED_MARKETS: tuple[str, ...] = ("ashare", "a_share", "cn_futures", "cnfutures", "futures", "hk")

# Old capital thresholds: flag if capital_base exceeds these values.
# New standard = 10,000 original currency = 72,000 CNY
OLD_CAPITAL_THRESHOLD_ORIGINAL: float = 12_000.0   # well above 10,000
OLD_CAPITAL_THRESHOLD_CNY: float = 80_000.0         # well above 72,000

# Quarantine fields injected into matching rows
QUARANTINE_FIELDS: dict[str, Any] = {
    "exclude_from_dashboard": True,
    "run_context": "legacy_usd_capital_quarantine",
}

# JSONL file names to scan under each style directory
MTM_FILENAME = "daily_mark_to_market.jsonl"
TRADE_JOURNAL_FILENAME = "trade_journal.jsonl"
STYLE_PERFORMANCE_FILENAME = "style_performance.jsonl"
STYLE_COMPARISON_FILENAME = "style_comparison.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _row_timestamp(row: dict[str, Any]) -> datetime | None:
    for key in ("timestamp", "generated_at", "updated_at", "created_at", "date", "trade_date"):
        parsed = _parse_datetime(row.get(key))
        if parsed is not None:
            return parsed
    return None


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
        return parsed if parsed == parsed else default
    except (TypeError, ValueError):
        return default


def _normalize_market(market: str) -> str:
    return str(market or "").strip().lower().replace("-", "_")


def _is_target_market(market: str) -> bool:
    key = _normalize_market(market)
    if key in EXCLUDED_MARKETS:
        return False
    return key in TARGET_MARKETS


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        return []
    return rows


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _build_quarantine_reason(row: dict[str, Any], market: str, *, reason: str = "legacy_capital_base") -> str:
    """Build a human-readable reason string for the quarantine."""
    old_cap = _safe_float(row.get("capital_base"))
    old_cap_cny = _safe_float(row.get("capital_base_cny"))
    parts = [reason]
    if reason == "pre_cutover":
        ts = _row_timestamp(row)
        if ts is not None:
            parts.append(f"timestamp={ts.isoformat()}")
    if old_cap > 0:
        parts.append(f"capital_base={old_cap}")
    if old_cap_cny > 0:
        parts.append(f"capital_base_cny={old_cap_cny}")
    parts.append("current standard is 10000 original currency / 72000 CNY")
    parts.append("market=" + market)
    return "; ".join(parts)


def _detect_old_capital_row(row: dict[str, Any], market: str) -> bool:
    """Return True if *row* carries an old capital_base for a target market.

    Detection logic:
    - Market must be US/Crypto/PM (not ashare/cn_futures/hk).
    - real_execution must not be True.
    - capital_base (original currency) > OLD_CAPITAL_THRESHOLD_ORIGINAL OR
      capital_base_cny > OLD_CAPITAL_THRESHOLD_CNY.
    """
    if not _is_target_market(market):
        return False

    # Never touch real execution rows
    if row.get("real_execution") is True:
        return False

    capital_base = row.get("capital_base")
    capital_base_cny = row.get("capital_base_cny")

    # If neither field exists, can't detect old capital
    if capital_base is None and capital_base_cny is None:
        return False

    cb = _safe_float(capital_base)
    cb_cny = _safe_float(capital_base_cny)

    if cb > OLD_CAPITAL_THRESHOLD_ORIGINAL:
        return True
    if cb_cny > OLD_CAPITAL_THRESHOLD_CNY:
        return True

    return False


def _already_quarantined(row: dict[str, Any]) -> bool:
    """Check if row already has quarantine markers."""
    return (
        row.get("exclude_from_dashboard") is True
        and row.get("run_context") == "legacy_usd_capital_quarantine"
    )


def _quarantine_row(row: dict[str, Any], market: str, *, reason: str = "legacy_capital_base") -> tuple[dict[str, Any], bool]:
    """Add quarantine fields to a row. Returns (modified_row, was_modified)."""
    if _already_quarantined(row):
        return row, False

    modified = dict(row)
    modified.update(QUARANTINE_FIELDS)
    modified["quarantine_reason"] = _build_quarantine_reason(row, market, reason=reason)
    modified["quarantined_at"] = _now_iso()
    return modified, True


def _is_before_cutoff_row(row: dict[str, Any], cutoff: datetime | None) -> bool:
    if cutoff is None:
        return False
    timestamp = _row_timestamp(row)
    return timestamp is not None and timestamp < cutoff


def _backup_file(path: Path) -> Path:
    """Copy file to <name>.bak. Returns backup path."""
    bak = path.with_suffix(path.suffix + ".bak")
    shutil.copy2(str(path), str(bak))
    return bak


def _process_jsonl_file(
    path: Path,
    market: str,
    *,
    apply: bool,
    is_old_capital_dir: bool = False,
    before: datetime | None = None,
) -> dict[str, Any]:
    """Process a single JSONL file. Returns per-file summary.

    For daily_mark_to_market and style_performance: detect per-row via capital_base.
    For trade_journal: quarantine all rows if directory is flagged as old-capital.
    """
    rows = _read_jsonl(path)
    if not rows:
        return {"path": str(path), "total_rows": 0, "quarantined": 0, "already_quarantined": 0, "skipped": 0, "modified": False}

    modified_rows: list[dict[str, Any]] = []
    quarantined = 0
    already = 0
    skipped = 0
    file_changed = False

    for row in rows:
        # Determine market for this row
        row_market = str(row.get("market") or market).lower().strip()
        if not _is_target_market(row_market):
            modified_rows.append(row)
            skipped += 1
            continue

        # Skip real_execution rows
        if row.get("real_execution") is True:
            modified_rows.append(row)
            skipped += 1
            continue

        if _already_quarantined(row):
            modified_rows.append(row)
            already += 1
            continue

        # Detection logic depends on file type
        base_name = path.name
        should_quarantine = False

        if base_name in (MTM_FILENAME, STYLE_PERFORMANCE_FILENAME):
            should_quarantine = _detect_old_capital_row(row, row_market)
        elif base_name == TRADE_JOURNAL_FILENAME:
            # Quarantine all trade journal rows in old-capital directories
            should_quarantine = is_old_capital_dir
        elif base_name == STYLE_COMPARISON_FILENAME:
            # style_comparison.json is a single JSON object, not JSONL
            # handled separately
            should_quarantine = _detect_old_capital_row(row, row_market)

        reason = "legacy_capital_base"
        if not should_quarantine and _is_before_cutoff_row(row, before):
            should_quarantine = True
            reason = "pre_cutover"
        elif should_quarantine and not _detect_old_capital_row(row, row_market) and _is_before_cutoff_row(row, before):
            reason = "pre_cutover"

        if should_quarantine:
            new_row, _ = _quarantine_row(row, row_market, reason=reason)
            modified_rows.append(new_row)
            quarantined += 1
            file_changed = True
        else:
            modified_rows.append(row)
            skipped += 1

    if apply and file_changed:
        _backup_file(path)
        _write_jsonl(path, modified_rows)

    return {
        "path": str(path),
        "total_rows": len(rows),
        "quarantined": quarantined,
        "already_quarantined": already,
        "skipped": skipped,
        "modified": file_changed,
    }


def _process_style_comparison(
    path: Path,
    market: str,
    *,
    apply: bool,
    before: datetime | None = None,
) -> dict[str, Any]:
    """Process a style_comparison.json file (single JSON object, not JSONL).

    If the top-level object or nested entries have old capital_base, add quarantine.
    """
    data = _read_json(path)
    if not data:
        return {"path": str(path), "entries": 0, "quarantined": 0, "already_quarantined": 0, "skipped": 0, "modified": False}

    # style_comparison.json typically is a dict of style_name -> performance_data
    quarantined = 0
    already = 0
    skipped = 0
    file_changed = False

    modified_data = dict(data)
    for key, value in data.items():
        if not isinstance(value, dict):
            continue
        row_market = str(value.get("market") or market).lower().strip()
        if not _is_target_market(row_market):
            skipped += 1
            continue
        if value.get("real_execution") is True:
            skipped += 1
            continue
        if _already_quarantined(value):
            already += 1
            continue
        reason = "legacy_capital_base"
        should_quarantine = _detect_old_capital_row(value, row_market)
        if not should_quarantine and _is_before_cutoff_row(value, before):
            should_quarantine = True
            reason = "pre_cutover"
        if should_quarantine:
            new_val, _ = _quarantine_row(value, row_market, reason=reason)
            modified_data[key] = new_val
            quarantined += 1
            file_changed = True
        else:
            skipped += 1

    if apply and file_changed:
        _backup_file(path)
        _write_json(path, modified_data)

    return {
        "path": str(path),
        "entries": len(data),
        "quarantined": quarantined,
        "already_quarantined": already,
        "skipped": skipped,
        "modified": file_changed,
    }


def _detect_directory_old_capital(style_dir: Path, market: str, before: datetime | None = None) -> bool:
    """Check if a style directory has old capital by scanning its MTM file."""
    mtm_path = style_dir / MTM_FILENAME
    rows = _read_jsonl(mtm_path)
    for row in rows:
        if _detect_old_capital_row(row, market):
            return True
        if _is_before_cutoff_row(row, before):
            return True
    return False


def _scan_style_directories(ledger_root: Path, before: datetime | None = None) -> list[tuple[Path, str, bool]]:
    """Yield (style_dir, market, is_old_capital) for each target-market style directory."""
    results: list[tuple[Path, str, bool]] = []
    if not ledger_root.exists():
        return results
    for market in TARGET_MARKETS:
        market_dir = ledger_root / market
        if not market_dir.is_dir():
            continue
        for style_dir in sorted(market_dir.iterdir()):
            if not style_dir.is_dir():
                continue
            is_old = _detect_directory_old_capital(style_dir, market, before)
            results.append((style_dir, market, is_old))
    return results


def _scan_review_performance(review_root: Path) -> list[tuple[Path, str]]:
    """Yield (path, market) for style_performance.jsonl / style_comparison.json under review."""
    results: list[tuple[Path, str]] = []
    if not review_root.exists():
        return results
    for market in TARGET_MARKETS:
        market_dir = review_root / market
        if not market_dir.is_dir():
            continue
        for name in (STYLE_PERFORMANCE_FILENAME, STYLE_COMPARISON_FILENAME):
            path = market_dir / name
            if path.exists():
                results.append((path, market))
    return results


def quarantine_legacy_usd_capital(
    *,
    ledger_root: Path | str | None = None,
    review_root: Path | str | None = None,
    apply: bool = False,
    batch_id: str | None = None,
    before: str | datetime | None = None,
) -> dict[str, Any]:
    """Scan and quarantine legacy USD capital samples.

    Returns an operational summary suitable for cron logs and manual review.
    """
    lr = Path(ledger_root) if ledger_root is not None else DEFAULT_LEDGER_ROOT
    rr = Path(review_root) if review_root is not None else DEFAULT_REVIEW_ROOT
    batch = batch_id or _now_iso()
    before_dt = before if isinstance(before, datetime) else _parse_datetime(before)
    if before_dt is not None:
        before_dt = before_dt.astimezone(timezone.utc)

    totals = {
        "quarantined_count": 0,
        "already_quarantined_count": 0,
        "skipped_count": 0,
        "total_rows_scanned": 0,
    }
    files_modified: list[dict[str, Any]] = []
    all_file_details: list[dict[str, Any]] = []

    # --- Scan sim ledger style directories ---
    for style_dir, market, is_old_capital in _scan_style_directories(lr, before_dt):
        for jsonl_name in (MTM_FILENAME, TRADE_JOURNAL_FILENAME):
            path = style_dir / jsonl_name
            if not path.exists():
                continue
            detail = _process_jsonl_file(
                path,
                market,
                apply=apply,
                is_old_capital_dir=is_old_capital,
                before=before_dt,
            )
            all_file_details.append(detail)
            totals["quarantined_count"] += detail["quarantined"]
            totals["already_quarantined_count"] += detail["already_quarantined"]
            totals["skipped_count"] += detail["skipped"]
            totals["total_rows_scanned"] += detail["total_rows"]
            if detail["modified"]:
                files_modified.append(detail)

    # --- Scan review performance files ---
    for path, market in _scan_review_performance(rr):
        if path.suffix == ".jsonl":
            detail = _process_jsonl_file(path, market, apply=apply, before=before_dt)
        else:
            detail = _process_style_comparison(path, market, apply=apply, before=before_dt)
        all_file_details.append(detail)
        totals["quarantined_count"] += detail.get("quarantined", 0)
        totals["already_quarantined_count"] += detail.get("already_quarantined", 0)
        totals["skipped_count"] += detail.get("skipped", 0)
        totals["total_rows_scanned"] += detail.get("total_rows", detail.get("entries", 0))
        if detail.get("modified"):
            files_modified.append(detail)

    manifest = {
        "status": "pass",
        "batch_id": batch,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tool": "quarantine_legacy_usd_capital",
        "applied": apply,
        "dry_run": not apply,
        "target_markets": list(TARGET_MARKETS),
        "before": before_dt.isoformat() if before_dt is not None else None,
        "thresholds": {
            "capital_base_original_gt": OLD_CAPITAL_THRESHOLD_ORIGINAL,
            "capital_base_cny_gt": OLD_CAPITAL_THRESHOLD_CNY,
            "new_capital_original": 10000.0,
            "new_capital_cny": 72000.0,
        },
        "quarantine_fields": QUARANTINE_FIELDS,
        **totals,
        "files_scanned": len(all_file_details),
        "files_modified": files_modified,
        "all_file_details": all_file_details,
    }

    # Write manifest to review/ops
    ops_review = rr.parent / "review" / "ops" if "review" in str(rr) else rr / "ops"
    ops_review.mkdir(parents=True, exist_ok=True)
    manifest_path = ops_review / f"quarantine_legacy_usd_capital_{batch}.json"
    _write_json(manifest_path, manifest)
    manifest["manifest_path"] = str(manifest_path)

    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Quarantine legacy USD capital samples in US/Crypto/PM simulated ledgers.",
    )
    parser.add_argument(
        "--ledger-root",
        type=Path,
        default=DEFAULT_LEDGER_ROOT,
        help="Sim ledger root. Default: shared/logs/sim_ledger",
    )
    parser.add_argument(
        "--review-root",
        type=Path,
        default=DEFAULT_REVIEW_ROOT,
        help="Review output root. Default: shared/review",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually modify files (backed up as .bak). Default is dry-run.",
    )
    parser.add_argument(
        "--batch-id",
        default="",
        help="Batch identifier. Defaults to current UTC timestamp.",
    )
    parser.add_argument(
        "--before",
        default="",
        help="Quarantine target-market rows before this ISO timestamp in addition to old capital thresholds.",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON output.",
    )
    args = parser.parse_args(argv)

    result = quarantine_legacy_usd_capital(
        ledger_root=args.ledger_root,
        review_root=args.review_root,
        apply=args.apply,
        batch_id=args.batch_id or None,
        before=args.before or None,
    )

    indent = 2 if args.pretty else None
    print(json.dumps(result, ensure_ascii=False, indent=indent, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "OLD_CAPITAL_THRESHOLD_ORIGINAL",
    "OLD_CAPITAL_THRESHOLD_CNY",
    "QUARANTINE_FIELDS",
    "TARGET_MARKETS",
    "_detect_old_capital_row",
    "quarantine_legacy_usd_capital",
]
