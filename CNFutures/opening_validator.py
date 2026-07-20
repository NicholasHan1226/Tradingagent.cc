#!/usr/bin/env python3
"""Fail-closed tombstone for the retired CNFutures opening validator.

The former module mixed opening checks with the legacy SharedSignals HTTP and
SQLite read models.  Keeping those readers behind an environment flag left a
second data authority and a direct-Python bypass.  Until the CNFutures lane is
rebuilt around an explicitly injected TradingDatas catalog/query port, every
library call and the CLI return a structured non-authoritative failure without
reading configuration, network, SQLite, or local sample files.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# A direct module or file-path invocation is a retired runtime entry. Stop
# before parsing arguments or touching data/output paths.
if __name__ == "__main__":
    from shared.governance.retirement import retired_cli

    raise SystemExit(retired_cli("CNFutures.opening_validator"))

from shared.governance.retirement import retired_cli


CN_TZ = timezone(timedelta(hours=8))
DEFAULT_SQLITE_DB = Path("/nonexistent/retired-cn-futures-sqlite-authority")
DEFAULT_REVIEW_PATH = Path("/nonexistent/retired-cn-futures-review")
DEFAULT_SIGNALS_DIR = Path("/nonexistent/retired-cn-futures-signals")
DEFAULT_RECEIPT_PATH = Path("/nonexistent/retired-cn-futures-receipts")


def _retired_report(report_type: str, now: datetime | None = None) -> dict[str, Any]:
    current = now or datetime.now(CN_TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=CN_TZ)
    else:
        current = current.astimezone(CN_TZ)
    return {
        "market": "cn_futures",
        "report_type": report_type,
        "checked_at": current.isoformat(timespec="seconds"),
        "status": "fail",
        "state": "retired",
        "reason": "legacy_opening_validator_retired",
        "replacement": "explicit_tradingdatas_catalog_query_port_after_fresh_handoff",
        "data_source": "none",
        "read_only": True,
        "production_verified": False,
        "real_trading_enabled": False,
    }


def validate_pre_open(
    *,
    sqlite_db: Path = DEFAULT_SQLITE_DB,
    now: datetime | None = None,
    min_symbols: int = 4,
) -> dict[str, Any]:
    """Retired compatibility signature; never opens ``sqlite_db``."""

    del sqlite_db, min_symbols
    return _retired_report("pre_open_acceptance", now)


def first_sample_alerts(
    *,
    sqlite_db: Path = DEFAULT_SQLITE_DB,
    review_path: Path = DEFAULT_REVIEW_PATH,
    signals_dir: Path = DEFAULT_SIGNALS_DIR,
    receipt_path: Path = DEFAULT_RECEIPT_PATH,
    now: datetime | None = None,
    min_symbols: int = 4,
    wait_minutes: int = 10,
) -> dict[str, Any]:
    """Retired compatibility signature; never reads legacy local artifacts."""

    del sqlite_db, review_path, signals_dir, receipt_path, min_symbols, wait_minutes
    return _retired_report("first_sample_alert", now)


def validate_opening(
    *,
    sqlite_db: Path = DEFAULT_SQLITE_DB,
    now: datetime | None = None,
    min_symbols: int = 4,
) -> dict[str, Any]:
    """Retired compatibility signature; never opens ``sqlite_db``."""

    del sqlite_db, min_symbols
    return _retired_report("opening_validation", now)


def main(argv: list[str] | None = None) -> int:
    """Fail closed before arguments, environment, data, or output are read."""

    del argv
    return retired_cli("CNFutures.opening_validator")


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["first_sample_alerts", "main", "validate_opening", "validate_pre_open"]
