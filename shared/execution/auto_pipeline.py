#!/usr/bin/env python3
"""Fail-closed tombstone for the retired mixed-market auto pipeline.

Market-specific compositions own simulation and review.  This module remains
only so stale imports and installed cron entries fail explicitly without
loading data readers, strategy mutation, notification, or execution code.
"""

from __future__ import annotations

from typing import Any

from shared.governance.retirement import RetiredRuntimeError, retired_cli


def run_auto_pipeline(*, reader: Any | None = None, **_: Any) -> dict[str, Any]:
    if reader is None:
        raise RetiredRuntimeError(
            "AutoPipeline.reader:tradingdatas_fixture_or_v1_port_required"
        )
    raise RetiredRuntimeError("AutoPipeline:legacy_runtime_retired")


def main(argv: list[str] | None = None) -> int:
    del argv
    return retired_cli("shared.execution.auto_pipeline")


if __name__ == "__main__":
    raise SystemExit(main())
