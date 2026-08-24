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
from .minute_event_aux import (
    MinuteEventAuxError,
    build_event_evidence,
    cached_hits_document,
    fetch_lockup_hits,
    load_or_refresh_daily_hits,
    make_session_client,
)
from .minute_loop import MinuteFixtureClosedLoop, MinuteLoopContractError
from .minute_research import (
    INITIAL_MONITOR_LIMIT,
    MinuteResearchContractError,
    MinuteResearchUniverse,
    MinuteUniverseInstrument,
)
from shared.data.tradingdatas_transport import (
    RuntimeGateConfigurationError,
    build_runtime_transport,
)


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
            raise MinutePaperRunnerError("minute_paper_universe_row_invalid") from exc
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
) -> tuple[MinuteFixtureClosedLoop, list[dict[str, Any]]]:
    if not path.exists():
        return MinuteFixtureClosedLoop(universe=universe), []
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
    history = raw.get("receipt_history")
    if history is None:
        last_receipt = raw.get("last_receipt")
        if not isinstance(last_receipt, Mapping):
            raise MinutePaperRunnerError("minute_paper_state_invalid")
        history = [last_receipt]
    if not isinstance(history, list) or not history:
        raise MinutePaperRunnerError("minute_paper_state_invalid")
    receipts: list[dict[str, Any]] = []
    bars: set[str] = set()
    for item in history:
        if not isinstance(item, Mapping):
            raise MinutePaperRunnerError("minute_paper_state_invalid")
        bar_end = item.get("bar_end")
        snapshot_sha256 = item.get("snapshot_sha256")
        audit_rejections = item.get("audit_rejections")
        if (
            not isinstance(bar_end, str)
            or not bar_end
            or bar_end in bars
            or not isinstance(snapshot_sha256, str)
            or len(snapshot_sha256) != 64
            or isinstance(audit_rejections, bool)
            or not isinstance(audit_rejections, int)
            or audit_rejections < 0
        ):
            raise MinutePaperRunnerError("minute_paper_state_invalid")
        bars.add(bar_end)
        receipts.append(dict(item))
    if receipts[-1] != raw.get("last_receipt"):
        raise MinutePaperRunnerError("minute_paper_state_invalid")
    return loop, receipts


def _validated_gap_recovery(
    value: Mapping[str, object] | None,
) -> tuple[str, tuple[str, ...]] | None:
    if value is None:
        return None
    reason = value.get("reason_code")
    raw_slots = value.get("skipped_session_slots")
    if (
        reason
        not in {
            "minute_session_gap_detected",
            "incident_recovery_no_historical_pit",
        }
        or not isinstance(raw_slots, (list, tuple))
        or not raw_slots
        or any(
            not isinstance(slot, str) or not slot or slot != slot.strip()
            for slot in raw_slots
        )
    ):
        raise MinutePaperRunnerError("minute_paper_gap_recovery_invalid")
    slots = tuple(raw_slots)
    if len(set(slots)) != len(slots):
        raise MinutePaperRunnerError("minute_paper_gap_recovery_invalid")
    return str(reason), slots


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
    gap_recovery: Mapping[str, object] | None = None,
    pin_universe_filter: bool = False,
    partial_observation_minimum: int | None = None,
    event_aux_enabled: bool = False,
) -> dict[str, Any]:
    """Process one exact completed bar in the delayed, simulation-only tier.

    ``event_aux_enabled`` opts into the pre-registered lockup shadow trial:
    the ``event`` sleeve then receives daily lockup-hit auxiliary evidence
    while baseline/dynamic_position stay bit-for-bit unchanged. Feed faults
    degrade to the abstain status quo and are reported in the receipt.
    """

    if os.environ.get("REAL_TRADING_ENABLED", "false").strip().lower() != "false":
        raise MinutePaperRunnerError("real_trading_must_remain_disabled")
    config = load_minute_canary_config(manifest)
    timestamp_field = config.profile.get("timestamp_field")
    if not isinstance(timestamp_field, str) or not timestamp_field:
        raise MinutePaperRunnerError("minute_paper_timestamp_field_invalid")
    reference_facts = load_reference_facts(reference_facts_path)
    universe = load_minute_research_universe(universe_path)
    universe_symbols = set(universe.instruments)
    if universe_symbols != set(reference_facts):
        raise MinutePaperRunnerError("minute_paper_reference_universe_mismatch")
    filters: dict[str, object] = {timestamp_field: {"eq": bar_end}}
    if pin_universe_filter:
        symbol_field = config.profile.get("symbol_field")
        if not isinstance(symbol_field, str) or not symbol_field:
            raise MinutePaperRunnerError("minute_paper_symbol_field_invalid")
        filters[symbol_field] = {"in": tuple(sorted(universe_symbols))}
    config = replace(config, filters=filters)
    profile, snapshot, audit = load_minute_snapshot(
        config,
        token_file=token_file,
        decision_time=decision_time,
        trading_date=trading_date,
        reference_facts=reference_facts,
        evidence_use=MinuteEvidenceUse.DELAYED_PAPER,
        allow_symbol_rejections=partial_observation_minimum is None,
    )
    if partial_observation_minimum is not None and (
        isinstance(partial_observation_minimum, bool)
        or not isinstance(partial_observation_minimum, int)
        or partial_observation_minimum <= 0
        or partial_observation_minimum >= len(universe_symbols)
    ):
        raise MinutePaperRunnerError(
            "minute_paper_partial_observation_policy_invalid"
        )
    observed_symbols = {bar.symbol for bar in snapshot.bars}
    if audit.records():
        return {
            "status": "partial_observation_failed_closed",
            "reason_code": "minute_paper_partial_audit_rejections",
            "bar_end": bar_end,
            "capital_authority": False,
            "execution_authority": False,
            "execution_eligible": False,
            "training_eligible": False,
            "promotion_authorized": False,
            "real_trading_enabled": False,
        }
    row_rejections = audit.row_rejections()
    row_rejection_payload = [
        {
            "symbol": item.symbol,
            "reason_code": item.reason_code,
            "dataset_id": item.dataset_id,
            "catalog_version": item.catalog_version,
            "rejected_payload_sha256": item.rejected_payload_sha256,
        }
        for item in row_rejections
    ]
    # Keep the existing field reserved for batch-level audit failures.  Row
    # quality quarantine is reported separately so runtime gates do not turn a
    # valid partial snapshot back into a whole-line failure.
    audit_rejection_count = len(audit.records())
    partial_observation = observed_symbols != universe_symbols
    if not observed_symbols <= universe_symbols:
        missing_symbols = sorted(universe_symbols - observed_symbols)
        return {
            "status": "partial_observation_failed_closed",
            "reason_code": "minute_paper_partial_identity_replacement",
            "bar_end": bar_end,
            "requested_count": len(universe_symbols),
            "accepted_count": len(observed_symbols),
            "missing_count": len(missing_symbols),
            "accepted_symbols": sorted(observed_symbols),
            "missing_symbols": missing_symbols,
            "capital_authority": False,
            "execution_authority": False,
            "execution_eligible": False,
            "training_eligible": False,
            "promotion_authorized": False,
            "real_trading_enabled": False,
        }
    if partial_observation:
        missing_symbols = sorted(universe_symbols - observed_symbols)
        unsafe_reason = None
        if (
            partial_observation_minimum is not None
            and len(observed_symbols) < partial_observation_minimum
        ):
            unsafe_reason = "minute_paper_partial_coverage_insufficient"
        if unsafe_reason is not None:
            return {
                "status": "partial_observation_failed_closed",
                "reason_code": unsafe_reason,
                "bar_end": bar_end,
                "requested_count": len(universe_symbols),
                "accepted_count": len(observed_symbols),
                "missing_count": len(missing_symbols),
                "accepted_symbols": sorted(observed_symbols),
                "missing_symbols": missing_symbols,
                "capital_authority": False,
                "execution_authority": False,
                "execution_eligible": False,
                "training_eligible": False,
                "promotion_authorized": False,
                "real_trading_enabled": False,
            }
        if partial_observation_minimum is not None:
            evidence_rows = [
                {
                    "symbol": bar.symbol,
                    "bar_end": bar.bar_end.isoformat(),
                    "receipt_id": bar.receipt_id,
                    "data_through": bar.data_through.isoformat(),
                    "observed_at": bar.observed_at.isoformat(),
                    "source_lineage_sha256": bar.source_lineage_sha256,
                    "envelope_proof_sha256": bar.envelope_proof_sha256,
                    "source_row_sha256": bar.source_row_sha256,
                }
                for bar in sorted(snapshot.bars, key=lambda item: item.symbol)
            ]
            return {
                "status": "partial_observation",
                "bar_end": bar_end,
                "decision_time": decision_time.isoformat(),
                "observed_at": max(
                    bar.observed_at for bar in snapshot.bars
                ).isoformat(),
                "requested_count": len(universe_symbols),
                "accepted_count": len(observed_symbols),
                "missing_count": len(missing_symbols),
                "accepted_symbols": sorted(observed_symbols),
                "missing_symbols": missing_symbols,
                "same_observation": snapshot.same_observation,
                "fanout_failures": [dict(item) for item in snapshot.fanout_failures],
                "lineage_complete": True,
                "proof_complete": True,
                "audit_rejections": audit_rejection_count,
                "row_rejection_count": len(row_rejections),
                "row_rejections": row_rejection_payload,
                "per_row_evidence": evidence_rows,
                "capital_authority": False,
                "execution_authority": False,
                "execution_eligible": False,
                "training_eligible": False,
                "promotion_authorized": False,
                "real_trading_enabled": False,
            }
        # Rolling mode intentionally continues below with the accepted subset.
        # The closed loop can process a snapshot smaller than its reviewed
        # universe; missing symbols remain absent for this bar and are retried
        # on the next timer window.
    state_path = Path(state_bundle)
    recovery = _validated_gap_recovery(gap_recovery)
    loop, receipt_history = _load_loop_bundle(state_path, universe=universe)
    if recovery is not None:
        reason_code, skipped_slots = recovery
        loop.resume_after_gap(
            decision_time=decision_time,
            manifest_sha256=profile.consumer_profile_sha256,
            reason_code=reason_code,
            skipped_session_slots=skipped_slots,
        )
    auxiliary_evidence: tuple = ()
    event_aux_status = "disabled"
    if event_aux_enabled:
        # First bar of the day: no cache yet, so fetch once and persist.
        # Every remaining fault degrades to today's status quo (the event
        # sleeve abstains) instead of failing the bar.
        try:
            document = cached_hits_document(Path(manifest).parent)
            fetched_at = datetime.fromisoformat(document["fetched_at"])
            auxiliary_evidence = build_event_evidence(
                document["hits"],
                decision_time=decision_time,
                available_at=fetched_at,
            )
            event_aux_status = f"ok:{len(auxiliary_evidence)}"
        except MinuteEventAuxError:
            try:
                aux_client = make_session_client(
                    transport_id=config.transport_id,
                    token_file=token_file,
                    base_url=config.base_url,
                    transport_factory=build_runtime_transport,
                    expected_catalog_version=config.expected_catalog_version,
                    access_policy_id=config.access_policy_id,
                    timeout_seconds=float(config.timeout_seconds),
                )
                fresh_hits = fetch_lockup_hits(aux_client, session_date=trading_date)
                load_or_refresh_daily_hits(
                    Path(manifest).parent,
                    session_date=trading_date,
                    refresh=fresh_hits,
                )
                auxiliary_evidence = build_event_evidence(
                    fresh_hits,
                    decision_time=decision_time,
                    available_at=decision_time,
                )
                event_aux_status = f"ok_fetched:{len(auxiliary_evidence)}"
            except MinuteEventAuxError as fetch_exc:
                auxiliary_evidence = ()
                event_aux_status = f"degraded:{fetch_exc}"
    step = loop.process_snapshot(
        snapshot=snapshot,
        manifest_sha256=profile.consumer_profile_sha256,
        auxiliary_evidence=auxiliary_evidence,
    )
    marks = {bar.symbol: bar.close_cny for bar in snapshot.bars}
    attribution = loop.attribution_snapshot(marks=marks)
    receipt = {
        "status": "pass",
        "event_aux_status": event_aux_status,
        "coverage_status": "partial" if partial_observation else "complete",
        "authority_tier": "non_production_fixture",
        "capital_authority": False,
        "execution_authority": False,
        "durable_capital": False,
        "real_trading_enabled": False,
        "evidence_use": MinuteEvidenceUse.DELAYED_PAPER.value,
        "dataset_id": profile.dataset_id,
        "expected_catalog_version": profile.expected_catalog_version,
        "observed_catalog_version": snapshot.observed_catalog_version,
        "catalog_version_drift": snapshot.catalog_version_drift,
        "dataset_contract_fingerprint": profile.dataset_contract_fingerprint,
        "consumer_profile_sha256": profile.consumer_profile_sha256,
        "bar_end": bar_end,
        "snapshot_sha256": snapshot.sha256,
        "row_count": snapshot.row_count,
        "requested_count": len(universe_symbols),
        "accepted_count": len(observed_symbols),
        "missing_count": len(universe_symbols - observed_symbols),
        "accepted_symbols": sorted(observed_symbols),
        "missing_symbols": sorted(universe_symbols - observed_symbols),
        "fanout_failures": [dict(item) for item in snapshot.fanout_failures],
        "audit_rejections": audit_rejection_count,
        "row_rejection_count": len(row_rejections),
        "row_rejections": row_rejection_payload,
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
    if recovery is not None:
        receipt.update(
            {
                "gap_recovery": True,
                "gap_slots": list(recovery[1]),
                "full_session_complete": False,
                "learning_eligible": False,
                "gap_recovery_reason": recovery[0],
            }
        )
    receipt_history.append(receipt)
    bundle = {
        "schema": "tradingagent.ashare.delayed_minute_paper_bundle.v1",
        "authority_tier": "non_production_fixture",
        "real_trading_enabled": False,
        "loop_state": loop.export_state(),
        "last_receipt": receipt,
        "receipt_history": receipt_history,
    }
    _atomic_write_json(state_path, bundle)
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
