"""Receipt-bound current-observation contract for ``rt_min_daily``.

This adapter deliberately consumes an already validated TradingDatas V1
``PagedQueryRun``.  It does not perform transport, persistence, ranking,
probability, learning, promotion, execution, or live-trading work.  The
provider dataset is an intraday cumulative observation, not a historical PIT
source; every projection therefore keeps ``historical_pit_eligible=False``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any
from zoneinfo import ZoneInfo

from shared.data.evidence_gate import (
    DataEvidenceGate,
    DatasetEvidencePolicy,
    EvidenceAction,
)
from shared.data.research_snapshot import (
    DatasetRequirement,
    ResearchDataProfile,
    ResearchDataSnapshot,
    build_research_data_snapshot,
)
from shared.data.tradingdatas_pagination import PagedQueryRun


RT_MIN_DAILY_DATASET_ID = "cn.dataset.rt_min_daily"
RT_MIN_DAILY_SCHEMA_MAJOR = 1
RT_MIN_DAILY_FIELDS = (
    "ts_code",
    "freq",
    "time",
    "open",
    "close",
    "high",
    "low",
    "vol",
    "amount",
)
RT_MIN_DAILY_IDENTITY_FIELDS = ("ts_code", "freq", "time")
RT_MIN_DAILY_CONTRACT_ID = "tradingagent.ashare.rt_min_daily.pit.v1"
_SYMBOL_RE = re.compile(r"^[0-9]{6}\.(?:SH|SZ|BJ)$")
_SHANGHAI = ZoneInfo("Asia/Shanghai")


class RtMinDailyPITContractError(ValueError):
    """Raised when receipt-bound current-observation evidence is unsafe."""


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise RtMinDailyPITContractError("rt_min_daily_feature_payload_invalid") from exc


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _symbol(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or _SYMBOL_RE.fullmatch(value) is None:
        raise RtMinDailyPITContractError(f"{field_name}_invalid")
    return value


def _timestamp(value: object, *, field_name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise RtMinDailyPITContractError(f"{field_name}_invalid") from exc
    else:
        raise RtMinDailyPITContractError(f"{field_name}_invalid")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RtMinDailyPITContractError(f"{field_name}_must_be_timezone_aware")
    return parsed.astimezone(timezone.utc)


def _row_time(value: object, *, field_name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise RtMinDailyPITContractError(f"{field_name}_invalid")
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=_SHANGHAI
            )
        except ValueError as exc:
            raise RtMinDailyPITContractError(f"{field_name}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=_SHANGHAI)
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class RtMinDailyPITFeatureContract:
    """Immutable, receipt-bound current observation and non-model features."""

    contract_id: str
    dataset_id: str
    schema_major: int
    catalog_version: str
    decision_as_of: str
    requested_symbols: tuple[str, ...]
    accepted_symbols: tuple[str, ...]
    missing_symbols: tuple[str, ...]
    quality_status: str
    receipt_id: str
    data_through: str
    observed_at: str
    observation_mode: str
    lineage_complete: bool
    source_lineage_sha256: str
    source_proof_sha256: str
    snapshot_sha256: str
    row_count: int
    historical_pit_eligible: bool
    learning_eligible: bool
    promotion_eligible: bool
    execution_authority: bool
    features: tuple[dict[str, Any], ...]
    content_sha256: str

    @property
    def requested_count(self) -> int:
        return len(self.requested_symbols)

    @property
    def accepted_count(self) -> int:
        return len(self.accepted_symbols)

    @property
    def missing_count(self) -> int:
        return len(self.missing_symbols)

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_id": self.contract_id,
            "dataset_id": self.dataset_id,
            "schema_major": self.schema_major,
            "catalog_version": self.catalog_version,
            "decision_as_of": self.decision_as_of,
            "requested_symbols": list(self.requested_symbols),
            "accepted_symbols": list(self.accepted_symbols),
            "missing_symbols": list(self.missing_symbols),
            "requested_count": self.requested_count,
            "accepted_count": self.accepted_count,
            "missing_count": self.missing_count,
            "quality_status": self.quality_status,
            "receipt_id": self.receipt_id,
            "data_through": self.data_through,
            "observed_at": self.observed_at,
            "observation_mode": self.observation_mode,
            "lineage_complete": self.lineage_complete,
            "source_lineage_sha256": self.source_lineage_sha256,
            "source_proof_sha256": self.source_proof_sha256,
            "snapshot_sha256": self.snapshot_sha256,
            "row_count": self.row_count,
            "historical_pit_eligible": self.historical_pit_eligible,
            "learning_eligible": self.learning_eligible,
            "promotion_eligible": self.promotion_eligible,
            "execution_authority": self.execution_authority,
            "features": [dict(item) for item in self.features],
            "content_sha256": self.content_sha256,
        }


def _profile() -> ResearchDataProfile:
    return ResearchDataProfile(
        profile_id=RT_MIN_DAILY_CONTRACT_ID,
        catalog_version="runtime-bound",
        requirements=(
            DatasetRequirement(
                RT_MIN_DAILY_DATASET_ID,
                role="required_execution",
                identity_fields=RT_MIN_DAILY_IDENTITY_FIELDS,
                query_as_of_mode="omit",
                minimum_row_count=0,
                max_pages=1_000,
                max_rows=5_000_000,
            ),
        ),
    )


def _normalize_symbols(values: Sequence[str], *, field_name: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise RtMinDailyPITContractError(f"{field_name}_must_be_a_sequence")
    raw_values = tuple(values)
    normalized = tuple(sorted({_symbol(value, field_name=f"{field_name}_item") for value in raw_values}))
    if not normalized:
        raise RtMinDailyPITContractError(f"{field_name}_empty")
    if len(normalized) != len(raw_values):
        raise RtMinDailyPITContractError(f"{field_name}_duplicate")
    return normalized


def _feature_rows(
    rows: Sequence[dict[str, Any]],
    requested: set[str],
    *,
    observed: datetime,
    decision: datetime,
) -> tuple[dict[str, Any], ...]:
    result: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise RtMinDailyPITContractError(f"row_{index}_not_mapping")
        missing = [field for field in RT_MIN_DAILY_FIELDS if field not in row]
        if missing:
            raise RtMinDailyPITContractError(f"row_{index}_field_missing")
        symbol = _symbol(row["ts_code"], field_name=f"row_{index}_ts_code")
        if symbol not in requested:
            raise RtMinDailyPITContractError("row_symbol_out_of_requested_scope")
        if not isinstance(row["freq"], str) or row["freq"] != "1MIN":
            raise RtMinDailyPITContractError(f"row_{index}_freq_invalid")
        if not isinstance(row["time"], str) or not row["time"].strip():
            raise RtMinDailyPITContractError(f"row_{index}_time_invalid")
        row_time = _row_time(row["time"], field_name=f"row_{index}_time")
        if row_time > observed:
            raise RtMinDailyPITContractError(f"row_{index}_time_after_observed_at")
        if row_time > decision:
            raise RtMinDailyPITContractError(f"row_{index}_time_after_decision_as_of")
        result.append(
            {
                "symbol": symbol,
                "freq": row["freq"],
                "time": row["time"],
                "open": row["open"],
                "close": row["close"],
                "high": row["high"],
                "low": row["low"],
                "vol": row["vol"],
                "amount": row["amount"],
                "feature_kind": "receipt_bound_current_observation",
                "calibrated_probability": None,
                "expected_return_bps": None,
                "promotion_eligible": False,
                "execution_authority": False,
            }
        )
    return tuple(result)


def build_rt_min_daily_pit_feature_contract(
    *,
    page_run: PagedQueryRun,
    requested_symbols: Sequence[str],
    decision_as_of: datetime,
) -> RtMinDailyPITFeatureContract:
    """Build a current-observation contract from an existing complete page run.

    The caller is responsible for obtaining the page run through the existing
    ``SharedSignalsV1Client`` and ``collect_query_pages`` path.  This function
    never calls that client and never writes a durable artifact.
    """

    if not isinstance(page_run, PagedQueryRun):
        raise RtMinDailyPITContractError("page_run_invalid")
    if page_run.dataset_id != RT_MIN_DAILY_DATASET_ID:
        raise RtMinDailyPITContractError("dataset_id_mismatch")
    requested = _normalize_symbols(requested_symbols, field_name="requested_symbols")
    decision = _timestamp(decision_as_of, field_name="decision_as_of")
    envelope = page_run.envelope
    metadata = envelope.metadata
    if not isinstance(metadata.receipt_id, str) or not metadata.receipt_id.strip():
        raise RtMinDailyPITContractError("receipt_id_missing")
    if not isinstance(metadata.lineage, dict) or metadata.lineage.get("complete") is not True:
        raise RtMinDailyPITContractError("lineage_incomplete")
    if metadata.lineage.get("provider_neutral") is not True:
        raise RtMinDailyPITContractError("lineage_not_provider_neutral")
    through = _timestamp(metadata.data_through, field_name="data_through")
    observed = _timestamp(metadata.observed_at, field_name="observed_at")
    if through > observed:
        raise RtMinDailyPITContractError("data_through_after_observed_at")
    if observed > decision:
        raise RtMinDailyPITContractError("observed_at_after_decision_as_of")
    catalog_version = envelope.catalog_version
    profile = ResearchDataProfile(
        profile_id=RT_MIN_DAILY_CONTRACT_ID,
        catalog_version=catalog_version,
        requirements=(
            DatasetRequirement(
                RT_MIN_DAILY_DATASET_ID,
                role="required_execution",
                identity_fields=RT_MIN_DAILY_IDENTITY_FIELDS,
                query_as_of_mode="omit",
                minimum_row_count=0,
                max_pages=1_000,
                max_rows=5_000_000,
            ),
        ),
    )
    policy = DatasetEvidencePolicy(
        RT_MIN_DAILY_DATASET_ID,
        degraded_action=EvidenceAction.DEWEIGHT,
        stale_action=EvidenceAction.DEWEIGHT,
        degraded_weight=0.25,
        stale_weight=0.10,
    )
    decision_evidence = DataEvidenceGate({RT_MIN_DAILY_DATASET_ID: policy}).evaluate(envelope)
    if decision_evidence.action is EvidenceAction.REJECT:
        raise RtMinDailyPITContractError("evidence_rejected")
    snapshot: ResearchDataSnapshot = build_research_data_snapshot(
        profile=profile,
        page_runs=(page_run,),
        decisions=(decision_evidence,),
        decision_as_of=decision,
    )
    dataset = snapshot.datasets[0]
    rows = _feature_rows(
        dataset.decoded_rows(),
        set(requested),
        observed=observed,
        decision=decision,
    )
    accepted = tuple(sorted({row["symbol"] for row in rows}))
    missing = tuple(symbol for symbol in requested if symbol not in accepted)
    quality_status = "usable" if not missing else "usable_degraded"
    source_lineage = dataset.lineage_sha256
    if source_lineage is None:
        raise RtMinDailyPITContractError("source_lineage_missing")
    payload = {
        "contract_id": RT_MIN_DAILY_CONTRACT_ID,
        "dataset_id": RT_MIN_DAILY_DATASET_ID,
        "schema_major": RT_MIN_DAILY_SCHEMA_MAJOR,
        "catalog_version": catalog_version,
        "decision_as_of": decision.isoformat(),
        "requested_symbols": list(requested),
        "accepted_symbols": list(accepted),
        "missing_symbols": list(missing),
        "quality_status": quality_status,
        "receipt_id": dataset.receipt_id,
        "data_through": through.isoformat(),
        "observed_at": observed.isoformat(),
        "observation_mode": "current_observation",
        "lineage_complete": dataset.source_proof_complete,
        "source_lineage_sha256": source_lineage,
        "source_proof_sha256": dataset.source_proof_sha256,
        "snapshot_sha256": snapshot.snapshot_sha256,
        "row_count": len(rows),
        "historical_pit_eligible": False,
        "learning_eligible": False,
        "promotion_eligible": False,
        "execution_authority": False,
        "features": list(rows),
    }
    return RtMinDailyPITFeatureContract(
        contract_id=RT_MIN_DAILY_CONTRACT_ID,
        dataset_id=RT_MIN_DAILY_DATASET_ID,
        schema_major=RT_MIN_DAILY_SCHEMA_MAJOR,
        catalog_version=catalog_version,
        decision_as_of=decision.isoformat(),
        requested_symbols=requested,
        accepted_symbols=accepted,
        missing_symbols=missing,
        quality_status=quality_status,
        receipt_id=dataset.receipt_id or "",
        data_through=through.isoformat(),
        observed_at=observed.isoformat(),
        observation_mode="current_observation",
        lineage_complete=dataset.source_proof_complete,
        source_lineage_sha256=source_lineage,
        source_proof_sha256=dataset.source_proof_sha256 or "",
        snapshot_sha256=snapshot.snapshot_sha256,
        row_count=len(rows),
        historical_pit_eligible=False,
        learning_eligible=False,
        promotion_eligible=False,
        execution_authority=False,
        features=rows,
        content_sha256=_sha256(payload),
    )


__all__ = [
    "RT_MIN_DAILY_CONTRACT_ID",
    "RT_MIN_DAILY_DATASET_ID",
    "RT_MIN_DAILY_FIELDS",
    "RT_MIN_DAILY_IDENTITY_FIELDS",
    "RtMinDailyPITContractError",
    "RtMinDailyPITFeatureContract",
    "build_rt_min_daily_pit_feature_contract",
]
