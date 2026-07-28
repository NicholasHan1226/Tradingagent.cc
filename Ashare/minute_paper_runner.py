"""One-shot delayed five-minute A-share research-paper runner.

The runner deliberately persists one fixture research bundle, not a capital or
broker authority.  It consumes only the formal TradingDatas catalog/query
transport through ``minute_canary.load_minute_snapshot`` and keeps
``REAL_TRADING_ENABLED=false``.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import date, datetime
import json
import os
from pathlib import Path
import sys
from typing import Any, Mapping

from .minute_canary import (
    MinuteCanaryConfigurationError,
    load_minute_canary_config,
    load_minute_snapshot,
    load_reference_facts,
)
from .minute_data import MinuteDataContractError, MinuteEvidenceUse
from .minute_loop import MinuteFixtureClosedLoop, MinuteLoopContractError
from .minute_research import (
    INITIAL_MONITOR_LIMIT,
    MinuteResearchContractError,
    MinuteResearchUniverse,
    MinuteUniverseInstrument,
)
from shared.data.tradingdatas_transport import RuntimeGateConfigurationError


class MinutePaperRunnerError(ValueError):
    """Fail-closed one-shot runner configuration or persistence error."""


def _mapping(value: object, reason: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise MinutePaperRunnerError(reason)
    return value


def _load_json(path: Path, reason: str) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MinutePaperRunnerError(reason) from exc


def load_minute_research_universe(path: Path | str) -> MinuteResearchUniverse:
    raw = _load_json(Path(path), "minute_paper_universe_invalid")
    if not isinstance(raw, list) or not raw:
        raise MinutePaperRunnerError("minute_paper_universe_invalid")
    instruments: list[MinuteUniverseInstrument] = []
    for value in raw:
        row = dict(_mapping(value, "minute_paper_universe_row_invalid"))
        raw_list_date = row.get("list_date")
        if raw_list_date is not None:
            try:
                row["list_date"] = date.fromisoformat(str(raw_list_date))
            except ValueError as exc:
                raise MinutePaperRunnerError(
                    "minute_paper_universe_row_invalid"
                ) from exc
        try:
            instruments.append(MinuteUniverseInstrument(**row))
        except (TypeError, ValueError, MinuteResearchContractError) as exc:
            raise MinutePaperRunnerError(
                "minute_paper_universe_row_invalid"
            ) from exc
    return MinuteResearchUniverse(
        instruments=tuple(instruments),
        expanded=(
            len([item for item in instruments if not item.context_only])
            > INITIAL_MONITOR_LIMIT
        ),
    )


def _load_loop_bundle(
    path: Path,
    *,
    universe: MinuteResearchUniverse,
) -> MinuteFixtureClosedLoop:
    if not path.exists():
        return MinuteFixtureClosedLoop(universe=universe)
    raw = dict(
        _mapping(
            _load_json(path, "minute_paper_state_invalid"),
            "minute_paper_state_invalid",
        )
    )
    if (
        raw.get("schema") != "tradingagent.ashare.delayed_minute_paper_bundle.v1"
        or raw.get("authority_tier") != "non_production_fixture"
        or raw.get("real_trading_enabled") is not False
    ):
        raise MinutePaperRunnerError("minute_paper_state_invalid")
    try:
        loop = MinuteFixtureClosedLoop.restore(
            _mapping(raw.get("loop_state"), "minute_paper_state_invalid")
        )
    except (MinuteLoopContractError, ValueError) as exc:
        raise MinutePaperRunnerError("minute_paper_state_invalid") from exc
    if (
        loop.universe.instruments != universe.instruments
        or loop.universe.expanded != universe.expanded
    ):
        raise MinutePaperRunnerError("minute_paper_universe_drift")
    return loop


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    encoded = (
        json.dumps(
            dict(value),
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except OSError as exc:
        raise MinutePaperRunnerError("minute_paper_state_persist_failed") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def run_delayed_minute_paper_once(
    *,
    manifest: Path | str,
    reference_facts_path: Path | str,
    universe_path: Path | str,
    token_file: Path | str,
    state_bundle: Path | str,
    decision_time: datetime,
    trading_date: date,
    bar_end: str,
) -> dict[str, Any]:
    """Process one exact completed bar in the delayed, simulation-only tier."""

    if os.environ.get("REAL_TRADING_ENABLED", "false").strip().lower() != "false":
        raise MinutePaperRunnerError("real_trading_must_remain_disabled")
    config = load_minute_canary_config(manifest)
    timestamp_field = config.profile.get("timestamp_field")
    if not isinstance(timestamp_field, str) or not timestamp_field:
        raise MinutePaperRunnerError("minute_paper_timestamp_field_invalid")
    config = replace(config, filters={timestamp_field: {"eq": bar_end}})
    reference_facts = load_reference_facts(reference_facts_path)
    universe = load_minute_research_universe(universe_path)
    universe_symbols = set(universe.instruments)
    if universe_symbols != set(reference_facts):
        raise MinutePaperRunnerError("minute_paper_reference_universe_mismatch")
    profile, snapshot, audit = load_minute_snapshot(
        config,
        token_file=token_file,
        decision_time=decision_time,
        trading_date=trading_date,
        reference_facts=reference_facts,
        evidence_use=MinuteEvidenceUse.DELAYED_PAPER,
    )
    if set(bar.symbol for bar in snapshot.bars) != universe_symbols:
        raise MinutePaperRunnerError("minute_paper_snapshot_universe_incomplete")
    loop = _load_loop_bundle(Path(state_bundle), universe=universe)
    step = loop.process_snapshot(
        snapshot=snapshot,
        manifest_sha256=profile.catalog_contract_sha256,
    )
    marks = {bar.symbol: bar.close_cny for bar in snapshot.bars}
    attribution = loop.attribution_snapshot(marks=marks)
    receipt = {
        "status": "pass",
        "authority_tier": "non_production_fixture",
        "capital_authority": False,
        "execution_authority": False,
        "durable_capital": False,
        "real_trading_enabled": False,
        "evidence_use": MinuteEvidenceUse.DELAYED_PAPER.value,
        "dataset_id": profile.dataset_id,
        "catalog_version": profile.catalog_version,
        "bar_end": bar_end,
        "snapshot_sha256": snapshot.sha256,
        "row_count": snapshot.row_count,
        "audit_rejections": len(audit.records()),
        "decision_time": step.decision_time.isoformat(),
        "feature_count": step.feature_count,
        "candidate_count": step.candidate_count,
        "pending_sleeves": sorted(loop.pending),
        "sleeves": [
            {
                "sleeve_id": sleeve.sleeve_id,
                "settled_status": (
                    None
                    if sleeve.settled_receipt is None
                    else sleeve.settled_receipt.status
                ),
                "scheduled": sleeve.scheduled_order is not None,
                "ranked_count": sleeve.ranked_count,
                "eligible_count": sleeve.eligible_count,
                "reconciled": bool(
                    sleeve.reconciliation
                    and sleeve.reconciliation.get("reconciled") is True
                ),
                "reconciliation_reason": sleeve.reconciliation_reason,
            }
            for sleeve in step.sleeves
        ],
        "attribution": attribution,
    }
    bundle = {
        "schema": "tradingagent.ashare.delayed_minute_paper_bundle.v1",
        "authority_tier": "non_production_fixture",
        "real_trading_enabled": False,
        "loop_state": loop.export_state(),
        "last_receipt": receipt,
    }
    _atomic_write_json(Path(state_bundle), bundle)
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="One-shot delayed A-share five-minute fixture paper runner"
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--reference-facts", type=Path, required=True)
    parser.add_argument("--universe", type=Path, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--state-bundle", type=Path, required=True)
    parser.add_argument("--decision-time", required=True)
    parser.add_argument("--trading-date", required=True)
    parser.add_argument("--bar-end", required=True)
    args = parser.parse_args(argv)
    try:
        if not args.token_file.is_absolute():
            raise MinutePaperRunnerError("token_file_must_be_absolute")
        receipt = run_delayed_minute_paper_once(
            manifest=args.manifest,
            reference_facts_path=args.reference_facts,
            universe_path=args.universe,
            token_file=args.token_file,
            state_bundle=args.state_bundle,
            decision_time=datetime.fromisoformat(args.decision_time),
            trading_date=date.fromisoformat(args.trading_date),
            bar_end=args.bar_end,
        )
    except (
        MinuteCanaryConfigurationError,
        MinuteDataContractError,
        MinuteLoopContractError,
        MinutePaperRunnerError,
        RuntimeGateConfigurationError,
        OSError,
        ValueError,
    ):
        print("delayed minute paper runner failed closed", file=sys.stderr)
        return 2
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "MinutePaperRunnerError",
    "load_minute_research_universe",
    "main",
    "run_delayed_minute_paper_once",
]
