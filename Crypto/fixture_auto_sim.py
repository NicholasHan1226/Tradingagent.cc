#!/usr/bin/env python3
"""Compatibility facade and CLI for the network-closed Crypto fixture simulator."""

from __future__ import annotations

import argparse
from typing import Sequence

from .fixture_sim.contracts import (
    ALLOWED_SYMBOLS,
    FROZEN_CHAMPION,
    WIRE_CONTRACT,
    CryptoEvidenceError,
    CryptoFixtureAutoSimError,
    CryptoLedgerError,
    CryptoSafetyError,
    FrozenChampionCandidate,
    OrderIntent,
    PaperFillReceipt,
    QualifiedFixtureEvidence,
    SpotBar5m,
    SpotInstrumentRules,
    TimeframeDecision,
    _canonical_json,
    _sha256,  # noqa: F401 - retained private compatibility for fixture tests
)
from .fixture_sim.evidence import qualify_fixture_evidence
from .fixture_sim.replay import _write_projection  # noqa: F401 - compatibility alias
from .fixture_sim.runtime import (
    build_order_intent,
    evaluate_frozen_champion,
    execute_fixture_paper_order,
    run_fixture_auto_sim,
    run_fixture_file,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the network-closed Crypto fixture paper cycle."
    )
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--output-root", required=True)
    args = parser.parse_args(argv)
    result = run_fixture_file(args.fixture, output_root=args.output_root)
    summary = {
        "run_id": result["bundle"]["run_id"],
        "status": result["bundle"]["status"],
        "idempotent_replay": result["idempotent_replay"],
        "real_trading_enabled": False,
    }
    print(_canonical_json(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ALLOWED_SYMBOLS",
    "CryptoEvidenceError",
    "CryptoFixtureAutoSimError",
    "CryptoLedgerError",
    "CryptoSafetyError",
    "FROZEN_CHAMPION",
    "FrozenChampionCandidate",
    "OrderIntent",
    "PaperFillReceipt",
    "QualifiedFixtureEvidence",
    "SpotBar5m",
    "SpotInstrumentRules",
    "TimeframeDecision",
    "WIRE_CONTRACT",
    "build_order_intent",
    "evaluate_frozen_champion",
    "execute_fixture_paper_order",
    "qualify_fixture_evidence",
    "run_fixture_auto_sim",
    "run_fixture_file",
]
