#!/usr/bin/env python3
"""Fail-closed tombstone for the retired A-share health email command."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from shared.governance.retirement import retired_cli


def main(argv: list[str] | None = None) -> int:
    """Stop before old health readers, output paths, or email transports."""

    del argv
    return retired_cli("shared.runtime_test.ashare_health_alert")


if __name__ == "__main__":
    raise SystemExit(main())
