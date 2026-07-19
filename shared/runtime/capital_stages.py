"""Capital-authority adapters for the isolated A-share paper day loop.

These adapters do not create a second account ledger.  They translate stage
contracts into the existing :class:`MarketCapitalLedger`, persist immutable
reconcile intents before mutating that ledger, and return orchestration proof
only.  No broker, network, environment default, or real-trading path exists in
this module.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date, datetime, time
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterator, Mapping
from zoneinfo import ZoneInfo

from shared.capital.market_ledger import (
    RECONCILE_SOURCE_SCHEMA_VERSION,
    MarketCapitalAshareSellCommitRequest,
    MarketCapitalFillCommitRequest,
    MarketCapitalLedger,
    MarketCapitalLedgerError,
    MarketCapitalReservationRequest,
    ReconcileManifest,
)
from shared.execution.sim_engine import SimExecutionEngine, SimOrder, SimPosition
from shared.models.lifecycle import (
    LifecycleContractError,
    TradingSessionCalendarAuthority,
    TradingSessionCalendarAuthorityVerification,
)
from shared.universe.policy import is_mainboard_tradable

from .day_loop import DayStagePort, StageRequest, StageResult
from .execution_receipt_contract import (
    ASHARE_EXECUTION_QUOTE_MAX_AGE,
    ashare_continuous_session,
    is_reconcilable_not_committed_market_failure,
)
from .market_evidence_authority import (
    AShareExecutionQuoteEvidence,
    AShareMarkEvidence,
    NonProductionFixtureMarketEvidenceAuthority,
    NonProductionFixtureMarketEvidenceVerifier,
)
from .run_bundle import ComponentIdentity, RunStage
from .trusted_clock import (
    NonProductionFixtureExecutionClock,
    TrustedExecutionClock,
    TrustedExecutionClockError,
)


class PaperCapitalStageError(RuntimeError):
    """Raised when a paper-capital adapter cannot prove its boundary."""


@dataclass(frozen=True)
class CapitalEffectAuthorization:
    """One fresh authorization decision for one concrete local side effect."""

    allowed: bool
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.allowed, bool):
            raise PaperCapitalStageError("capital_effect_authorization_invalid")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise PaperCapitalStageError("capital_effect_authorization_invalid")


class CapitalEffectGuard:
    """Explicit hook re-evaluated immediately before each capital effect."""

    identity_sha256: str

    def authorize(
        self,
        *,
        effect: str,
        request: StageRequest,
        order: Mapping[str, Any],
    ) -> CapitalEffectAuthorization:
        raise NotImplementedError


def _authorize_capital_effect(
    guard: CapitalEffectGuard | None,
    *,
    effect: str,
    request: StageRequest,
    order: Mapping[str, Any],
) -> CapitalEffectAuthorization:
    if guard is None:
        return CapitalEffectAuthorization(allowed=True, reason="guard_not_configured")
    if not isinstance(guard, CapitalEffectGuard):
        raise PaperCapitalStageError("capital_effect_guard_invalid")
    result = guard.authorize(
        effect=effect,
        request=request,
        order=order,
    )
    if type(result) is not CapitalEffectAuthorization:
        raise PaperCapitalStageError("capital_effect_authorization_invalid")
    return result


_SHA256_HEX = frozenset("0123456789abcdef")
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_CAPITAL_RESERVATION_FIELDS = frozenset(
    {
        "market_capital_reference_id",
        "market_capital_reservation_id",
        "market_capital_reservation_event_id",
        "market_capital_lineage_sha256",
        "market_reserved_cash_cny",
        "market_reserved_exposure_cny",
        "market_capital_required",
    }
)
_LEGACY_CAPITAL_RESERVATION_FIELDS = frozenset(
    {
        "market_capital_event_id",
        "market_capital_expected_head_event_id",
        "market_capital_expected_head_checksum",
        "market_capital_risk_unit_key",
        "market_reserved_gross_cny",
    }
)


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise PaperCapitalStageError("paper_capital_payload_not_canonical") from exc


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _strict_sha256(value: object, *, reason: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in _SHA256_HEX for character in value)
    ):
        raise PaperCapitalStageError(reason)
    return value


def _aware_instant(value: object, *, reason: str) -> datetime:
    if not isinstance(value, str) or not value or value != value.strip():
        raise PaperCapitalStageError(reason)
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PaperCapitalStageError(reason) from exc
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise PaperCapitalStageError(f"{reason}_timezone_required")
    return instant


def _session_calendar_receipt(
    value: object,
    *,
    reason_prefix: str,
) -> tuple[dict[str, Any], TradingSessionCalendarAuthority]:
    if not isinstance(value, Mapping):
        raise PaperCapitalStageError(
            f"{reason_prefix}_session_calendar_receipt_required"
        )
    receipt = dict(value)
    calendar_payload = receipt.get("calendar")
    verification_payload = receipt.get("verification")
    if (
        receipt.get("authority_tier") != "non_production_fixture"
        or receipt.get("production_eligible") is not False
        or not isinstance(calendar_payload, Mapping)
        or not isinstance(verification_payload, Mapping)
    ):
        raise PaperCapitalStageError(
            f"{reason_prefix}_session_calendar_receipt_invalid"
        )
    try:
        calendar_values = dict(calendar_payload)
        calendar_values["available_at"] = _aware_instant(
            calendar_values.get("available_at"),
            reason=f"{reason_prefix}_session_calendar_receipt_invalid",
        )
        sessions = calendar_values.get("sessions")
        if not isinstance(sessions, (list, tuple)):
            raise PaperCapitalStageError(
                f"{reason_prefix}_session_calendar_receipt_invalid"
            )
        calendar_values["sessions"] = tuple(
            date.fromisoformat(str(session)) for session in sessions
        )
        calendar = TradingSessionCalendarAuthority(**calendar_values)

        verification_values = dict(verification_payload)
        verification_values["verified_at"] = _aware_instant(
            verification_values.get("verified_at"),
            reason=f"{reason_prefix}_session_calendar_receipt_invalid",
        )
        verification_values["frozen_at"] = _aware_instant(
            verification_values.get("frozen_at"),
            reason=f"{reason_prefix}_session_calendar_receipt_invalid",
        )
        verification = TradingSessionCalendarAuthorityVerification(
            **verification_values
        )
    except (LifecycleContractError, TypeError, ValueError) as exc:
        raise PaperCapitalStageError(
            f"{reason_prefix}_session_calendar_receipt_invalid"
        ) from exc
    if (
        calendar.market.strip().lower() != "ashare"
        or verification.accepted is not True
        or verification.calendar_sha256 != calendar.calendar_sha256
        or verification.source_receipt_id != calendar.source_receipt_id
        or verification.source_receipt_sha256 != calendar.source_receipt_sha256
        or calendar.available_at > verification.verified_at
    ):
        raise PaperCapitalStageError(
            f"{reason_prefix}_session_calendar_receipt_invalid"
        )
    normalized = {
        "authority_tier": "non_production_fixture",
        "production_eligible": False,
        "calendar": calendar.canonical_payload(),
        "verification": verification.canonical_payload(),
    }
    normalized["receipt_sha256"] = _sha256(normalized)
    return normalized, calendar


def _artifact_component(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or Path(value).name != value
        or "/" in value
        or "\\" in value
    ):
        raise PaperCapitalStageError("paper_capital_artifact_component_invalid")
    return value


def _prepare_artifact_root(path: Path) -> Path:
    root = Path(path).expanduser().absolute()
    try:
        root.mkdir(parents=True, exist_ok=True)
        root_stat = root.lstat()
    except OSError as exc:
        raise PaperCapitalStageError("paper_capital_artifact_root_unavailable") from exc
    if stat.S_ISLNK(root_stat.st_mode):
        raise PaperCapitalStageError("paper_capital_artifact_root_symlink")
    if not stat.S_ISDIR(root_stat.st_mode):
        raise PaperCapitalStageError("paper_capital_artifact_root_not_directory")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(root, flags)
    except OSError as exc:
        raise PaperCapitalStageError("paper_capital_artifact_root_symlink") from exc
    os.close(descriptor)
    try:
        return root.resolve(strict=True)
    except OSError as exc:
        raise PaperCapitalStageError("paper_capital_artifact_root_unavailable") from exc


@contextmanager
def _artifact_directory_fd(root: Path, directories: tuple[str, ...]) -> Iterator[int]:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptors: list[int] = []
    try:
        try:
            current = os.open(root, flags)
        except OSError as exc:
            raise PaperCapitalStageError("paper_capital_artifact_root_symlink") from exc
        descriptors.append(current)
        for raw_component in directories:
            component = _artifact_component(raw_component)
            try:
                os.mkdir(component, mode=0o700, dir_fd=current)
            except FileExistsError:
                pass
            except OSError as exc:
                raise PaperCapitalStageError(
                    "paper_capital_artifact_directory_unavailable"
                ) from exc
            try:
                child = os.open(component, flags, dir_fd=current)
            except OSError as exc:
                raise PaperCapitalStageError(
                    "paper_capital_artifact_directory_symlink"
                ) from exc
            descriptors.append(child)
            current = child
        yield current
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _read_at(directory_fd: int, filename: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(filename, flags, dir_fd=directory_fd)
    except OSError as exc:
        raise PaperCapitalStageError("paper_capital_artifact_symlink") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise PaperCapitalStageError("paper_capital_artifact_not_regular")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _write_immutable(
    root: Path,
    relative_parts: tuple[str, ...],
    payload: object,
) -> Path:
    if not relative_parts:
        raise PaperCapitalStageError("paper_capital_artifact_path_empty")
    components = tuple(_artifact_component(value) for value in relative_parts)
    filename = components[-1]
    data = _canonical_bytes(payload)
    with _artifact_directory_fd(root, components[:-1]) as directory_fd:
        temporary_name = f".{filename}.{uuid.uuid4().hex}.tmp"
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = os.open(temporary_name, flags, 0o600, dir_fd=directory_fd)
        except OSError as exc:
            raise PaperCapitalStageError(
                "paper_capital_artifact_temp_unavailable"
            ) from exc
        try:
            view = memoryview(data)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            try:
                os.link(
                    temporary_name,
                    filename,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except FileExistsError:
                if _read_at(directory_fd, filename) != data:
                    raise PaperCapitalStageError("paper_capital_artifact_conflict")
            os.fsync(directory_fd)
        finally:
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
    return root.joinpath(*components)


def _read_immutable_optional(
    root: Path,
    relative_parts: tuple[str, ...],
) -> object | None:
    if not relative_parts:
        raise PaperCapitalStageError("paper_capital_artifact_path_empty")
    components = tuple(_artifact_component(value) for value in relative_parts)
    with _artifact_directory_fd(root, components[:-1]) as directory_fd:
        try:
            raw = _read_at(directory_fd, components[-1])
        except PaperCapitalStageError as exc:
            cause = exc.__cause__
            if isinstance(cause, OSError) and cause.errno == 2:
                return None
            raise
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PaperCapitalStageError("paper_capital_artifact_invalid") from exc


def _manifest_payload(manifest: ReconcileManifest) -> dict[str, Any]:
    payload = asdict(manifest)
    payload["included_fill_commit_ids"] = list(manifest.included_fill_commit_ids)
    return payload


def _manifest_from_payload(payload: Mapping[str, Any]) -> ReconcileManifest:
    values = dict(payload)
    included = values.get("included_fill_commit_ids")
    if not isinstance(included, list) or not all(
        isinstance(value, str) and value for value in included
    ):
        raise PaperCapitalStageError("paper_capital_reconcile_intent_invalid")
    values["included_fill_commit_ids"] = tuple(included)
    try:
        return ReconcileManifest(**values)
    except (TypeError, ValueError) as exc:
        raise PaperCapitalStageError("paper_capital_reconcile_intent_invalid") from exc


class PaperCapitalAccount:
    """Explicit adapter around the one canonical A-share capital ledger."""

    contract_id = "tradingagent.paper_capital_account.v1"

    def __init__(
        self,
        *,
        ledger: MarketCapitalLedger,
        artifact_root: Path,
        mark_prices: Mapping[str, Any],
    ) -> None:
        if type(ledger) is not MarketCapitalLedger or ledger.policy.market != "ashare":
            raise PaperCapitalStageError("ashare_market_capital_ledger_required")
        capital_snapshot = ledger.snapshot()
        root = _prepare_artifact_root(Path(artifact_root))
        normalized_marks: dict[str, float] = {}
        normalized_evidence: dict[str, dict[str, Any]] = {}
        for symbol, raw_value in dict(mark_prices).items():
            if not isinstance(symbol, str) or not symbol.strip():
                raise PaperCapitalStageError("paper_capital_mark_evidence_invalid")
            if not isinstance(raw_value, Mapping):
                raise PaperCapitalStageError("paper_capital_mark_evidence_required")
            evidence = dict(raw_value)
            market_authority = evidence.pop("market_evidence_authority", None)
            price = evidence.get("price_cny")
            trade_date_value = evidence.get("trade_date")
            source_receipt_id = evidence.get("source_receipt_id")
            authority_id = evidence.get("data_authority_id")
            if (
                isinstance(price, bool)
                or not isinstance(price, (int, float))
                or not math.isfinite(float(price))
                or float(price) <= 0.0
                or evidence.get("market") != "ashare"
                or not isinstance(trade_date_value, str)
                or not isinstance(source_receipt_id, str)
                or not source_receipt_id.strip()
                or not isinstance(authority_id, str)
                or not authority_id.strip()
                or evidence.get("real_trading_enabled") is not False
            ):
                raise PaperCapitalStageError("paper_capital_mark_evidence_invalid")
            try:
                parsed_trade_date = date.fromisoformat(trade_date_value)
            except ValueError as exc:
                raise PaperCapitalStageError(
                    "paper_capital_mark_evidence_trade_date_invalid"
                ) from exc
            observed = _aware_instant(
                evidence.get("observed_at"),
                reason="paper_capital_mark_evidence_observed_at_invalid",
            )
            available = _aware_instant(
                evidence.get("available_at"),
                reason="paper_capital_mark_evidence_available_at_invalid",
            )
            session_calendar_receipt, _ = _session_calendar_receipt(
                evidence.get("session_calendar_receipt"),
                reason_prefix="paper_capital_mark",
            )
            data_through = _aware_instant(
                evidence.get("data_through"),
                reason="paper_capital_mark_evidence_data_through_invalid",
            )
            if data_through > observed or observed > available:
                raise PaperCapitalStageError(
                    "paper_capital_mark_evidence_time_order_invalid"
                )
            if evidence.get("market_session") != "close":
                raise PaperCapitalStageError(
                    "paper_capital_mark_not_previous_verified_session_close"
                )
            normalized_symbol = symbol.strip().upper()
            if not is_mainboard_tradable(normalized_symbol):
                raise PaperCapitalStageError(
                    "paper_capital_mark_evidence_instrument_invalid"
                )
            normalized = {
                "price_cny": float(price),
                "market": "ashare",
                "trade_date": parsed_trade_date.isoformat(),
                "observed_at": observed.isoformat(),
                "available_at": available.isoformat(),
                "data_through": data_through.isoformat(),
                "market_session": "close",
                "source_receipt_id": source_receipt_id.strip(),
                "source_sha256": _strict_sha256(
                    evidence.get("source_sha256"),
                    reason="paper_capital_mark_evidence_source_sha256_invalid",
                ),
                "data_authority_id": authority_id.strip(),
                "dataset_id": str(evidence.get("dataset_id") or "").strip(),
                "catalog_version": str(evidence.get("catalog_version") or "").strip(),
                "source_lineage_sha256": _strict_sha256(
                    evidence.get("source_lineage_sha256"),
                    reason=(
                        "paper_capital_mark_evidence_source_lineage_sha256_invalid"
                    ),
                ),
                "session_calendar_receipt": session_calendar_receipt,
                "real_trading_enabled": False,
            }
            if not normalized["dataset_id"] or not normalized["catalog_version"]:
                raise PaperCapitalStageError(
                    "paper_capital_mark_evidence_source_binding_invalid"
                )
            if type(market_authority) is not (
                NonProductionFixtureMarketEvidenceAuthority
            ):
                raise PaperCapitalStageError(
                    "paper_capital_mark_evidence_authority_required"
                )
            mark_evidence = market_authority.evidence
            verification = market_authority.verification
            if (
                type(mark_evidence) is not AShareMarkEvidence
                or mark_evidence.symbol != normalized_symbol
                or mark_evidence.price_cny != float(price)
                or mark_evidence.market_session != "close"
                or mark_evidence.source.dataset_id != normalized["dataset_id"]
                or mark_evidence.source.catalog_version != normalized["catalog_version"]
                or mark_evidence.source.source_receipt_id
                != normalized["source_receipt_id"]
                or mark_evidence.source.source_receipt_sha256
                != normalized["source_sha256"]
                or mark_evidence.source.source_lineage_sha256
                != normalized["source_lineage_sha256"]
                or mark_evidence.source.data_through != data_through
                or mark_evidence.source.observed_at != observed
                or mark_evidence.source.available_at != available
                or mark_evidence.session_calendar_receipt_sha256
                != session_calendar_receipt["receipt_sha256"]
                or mark_evidence.context.capital_authority_id
                != ledger.policy.capital_authority_id
                or mark_evidence.context.authority_generation
                != ledger.policy.authority_generation
                or mark_evidence.context.execution_lineage_id
                != capital_snapshot.execution_lineage_id
                or authority_id.strip()
                != NonProductionFixtureMarketEvidenceVerifier.verifier_id
                or verification.evidence_sha256 != mark_evidence.sha256()
                or verification.proof_sha256 != verification.recompute_proof_sha256()
            ):
                raise PaperCapitalStageError(
                    "paper_capital_mark_evidence_authority_invalid"
                )
            normalized["market_evidence_authority_sha256"] = (
                market_authority.authority_sha256
            )
            normalized["market_evidence_verification"] = (
                verification.canonical_payload()
            )
            normalized_marks[normalized_symbol] = float(price)
            normalized_evidence[normalized_symbol] = normalized
        self.ledger = ledger
        self.artifact_root = root
        self.mark_prices = MappingProxyType(normalized_marks)
        self._mark_evidence = MappingProxyType(normalized_evidence)
        self.identity_sha256 = _sha256(
            {
                "contract_id": self.contract_id,
                "authority_id": ledger.policy.capital_authority_id,
                "authority_generation": ledger.policy.authority_generation,
                "market": ledger.policy.market,
                "marks": normalized_evidence,
                "artifact_root": str(root),
                "ledger_root": str(ledger.root.resolve()),
            }
        )

    def _mark_evidence_sha256(
        self,
        *,
        quantities: Mapping[str, int],
        pit_timestamp: str,
        trade_date_value: str,
    ) -> str:
        pit = _aware_instant(
            pit_timestamp,
            reason="paper_capital_mark_pit_timestamp_invalid",
        )
        try:
            run_date = date.fromisoformat(trade_date_value)
        except ValueError as exc:
            raise PaperCapitalStageError("paper_capital_trade_date_invalid") from exc
        used: dict[str, Mapping[str, Any]] = {}
        for symbol in sorted(quantities):
            evidence = self._mark_evidence.get(symbol)
            if evidence is None:
                raise PaperCapitalStageError(
                    f"paper_capital_mark_price_missing:{symbol}"
                )
            available = _aware_instant(
                evidence["available_at"],
                reason="paper_capital_mark_evidence_available_at_invalid",
            )
            data_through = _aware_instant(
                evidence["data_through"],
                reason="paper_capital_mark_evidence_data_through_invalid",
            )
            _, calendar = _session_calendar_receipt(
                evidence["session_calendar_receipt"],
                reason_prefix="paper_capital_mark",
            )
            try:
                mark_date = date.fromisoformat(str(evidence["trade_date"]))
            except ValueError as exc:
                raise PaperCapitalStageError(
                    "paper_capital_mark_evidence_trade_date_invalid"
                ) from exc
            if available > pit:
                raise PaperCapitalStageError("paper_capital_mark_evidence_future")
            if mark_date > run_date:
                raise PaperCapitalStageError("paper_capital_mark_evidence_future_date")
            verification = evidence.get("market_evidence_verification")
            if (
                not isinstance(verification, Mapping)
                or verification.get("trade_date") != run_date.isoformat()
            ):
                raise PaperCapitalStageError(
                    "paper_capital_mark_evidence_context_invalid"
                )
            verified_decision = _aware_instant(
                verification.get("decision_as_of"),
                reason="paper_capital_mark_evidence_context_invalid",
            )
            if verified_decision > pit:
                raise PaperCapitalStageError(
                    "paper_capital_mark_evidence_context_from_future"
                )
            try:
                run_index = calendar.sessions.index(run_date)
                if run_index <= 0:
                    raise PaperCapitalStageError(
                        "paper_capital_mark_session_calendar_invalid"
                    )
                previous_session = calendar.sessions[run_index - 1]
            except (ValueError, IndexError) as exc:
                raise PaperCapitalStageError(
                    "paper_capital_mark_session_calendar_invalid"
                ) from exc
            local_data_through = data_through.astimezone(_SHANGHAI)
            local_observed = _aware_instant(
                evidence["observed_at"],
                reason="paper_capital_mark_evidence_observed_at_invalid",
            ).astimezone(_SHANGHAI)
            if (
                mark_date != previous_session
                or evidence.get("market_session") != "close"
                or local_data_through.date() != previous_session
                or local_data_through.time() != time(15, 0)
                or local_observed != local_data_through
            ):
                raise PaperCapitalStageError(
                    "paper_capital_mark_not_previous_verified_session_close"
                )
            if symbol in quantities:
                used[symbol] = evidence
        return _sha256(used)

    def verified_mark_evidence_binding(
        self,
        *,
        symbols: tuple[str, ...],
        pit_timestamp: str,
        trade_date_value: str,
    ) -> tuple[Mapping[str, datetime], str]:
        """Revalidate and expose only the immutable mark evidence binding."""

        quantities = {symbol: 1 for symbol in symbols}
        evidence_sha256 = self._mark_evidence_sha256(
            quantities=quantities,
            pit_timestamp=pit_timestamp,
            trade_date_value=trade_date_value,
        )
        observations = {
            symbol: _aware_instant(
                self._mark_evidence[symbol]["observed_at"],
                reason="paper_capital_mark_evidence_observed_at_invalid",
            )
            for symbol in symbols
        }
        return MappingProxyType(observations), evidence_sha256

    def _new_reconcile_manifest(
        self,
        *,
        request: StageRequest,
        phase: str,
        pit_timestamp: str,
    ) -> ReconcileManifest:
        snapshot = self.ledger.snapshot()
        quantities = dict(snapshot.positions_quantity_by_risk_unit)
        missing_marks = sorted(set(quantities) - set(self.mark_prices))
        if missing_marks:
            raise PaperCapitalStageError(
                f"paper_capital_mark_price_missing:{','.join(missing_marks)}"
            )
        mark_evidence_sha256 = self._mark_evidence_sha256(
            quantities=quantities,
            pit_timestamp=pit_timestamp,
            trade_date_value=request.bundle.context.trade_date,
        )
        positions_market_value = {
            symbol: round(quantity * self.mark_prices[symbol], 6)
            for symbol, quantity in sorted(quantities.items())
        }
        unrealized_pnl = round(
            sum(positions_market_value.values())
            - sum(snapshot.positions_cost_basis_cny_by_risk_unit.values())
            - sum(snapshot.positions_entry_fee_cny_by_risk_unit.values()),
            6,
        )
        reservations = self.ledger.active_reservation_manifest()
        canonical_payload = {
            "schema_version": RECONCILE_SOURCE_SCHEMA_VERSION,
            "market": "ashare",
            "trade_date": request.bundle.context.trade_date.replace("-", ""),
            "pit_timestamp": pit_timestamp,
            "execution_lineage_id": request.bundle.context.execution_lineage,
            "cash_balance_cny": snapshot.cash_balance_cny,
            "positions_market_value": positions_market_value,
            "unrealized_pnl_cny": unrealized_pnl,
            "position_margin_by_risk_unit": {},
            "active_reservations_cny": snapshot.active_reservations_cny,
            "active_reservations": reservations,
            "frozen_order_cash_cny": snapshot.frozen_order_cash_cny,
            "frozen_order_margin_cny": snapshot.frozen_order_margin_cny,
            "positions_quantity_by_risk_unit": quantities,
            "positions_cost_basis_cny_by_risk_unit": dict(
                snapshot.positions_cost_basis_cny_by_risk_unit
            ),
            "positions_entry_fee_cny_by_risk_unit": dict(
                snapshot.positions_entry_fee_cny_by_risk_unit
            ),
            "position_entry_price_by_risk_unit": {},
            "position_side_by_risk_unit": {},
            "position_contract_multiplier_by_risk_unit": {},
            "position_contract_spec_sha256_by_risk_unit": {},
            "position_mark_price_by_risk_unit": {},
            "expected_ledger_event_id": snapshot.event_id,
            "expected_ledger_checksum": snapshot.event_checksum,
            "included_fill_commit_ids": list(snapshot.unreconciled_fill_commit_ids),
            "real_trading_enabled": False,
        }
        canonical_sha256 = hashlib.sha256(
            _canonical_bytes(canonical_payload)
        ).hexdigest()
        canonical_path = _write_immutable(
            self.artifact_root,
            ("sources", f"{canonical_sha256}.json"),
            canonical_payload,
        )
        return ReconcileManifest(
            market="ashare",
            authority_id=request.bundle.context.authority_id,
            as_of=request.bundle.context.trade_date.replace("-", ""),
            cash_balance_cny=snapshot.cash_balance_cny,
            positions_market_value=positions_market_value,
            unrealized_pnl_cny=unrealized_pnl,
            position_margin_by_risk_unit={},
            active_reservations_cny=snapshot.active_reservations_cny,
            frozen_order_cash_cny=snapshot.frozen_order_cash_cny,
            frozen_order_margin_cny=snapshot.frozen_order_margin_cny,
            authority_generation=request.bundle.context.authority_generation,
            execution_lineage_id=request.bundle.context.execution_lineage,
            pit_timestamp=pit_timestamp,
            source=(
                f"paper_capital:{phase}:{request.run_id}:"
                f"{request.input_bundle_sha256}:{mark_evidence_sha256}"
            ),
            source_sha256=canonical_sha256,
            active_reservations=reservations,
            expected_ledger_event_id=snapshot.event_id,
            expected_ledger_checksum=snapshot.event_checksum,
            included_fill_commit_ids=tuple(snapshot.unreconciled_fill_commit_ids),
            positions_quantity_by_risk_unit=quantities,
            positions_cost_basis_cny_by_risk_unit=dict(
                snapshot.positions_cost_basis_cny_by_risk_unit
            ),
            positions_entry_fee_cny_by_risk_unit=dict(
                snapshot.positions_entry_fee_cny_by_risk_unit
            ),
            position_entry_price_by_risk_unit={},
            position_side_by_risk_unit={},
            position_contract_multiplier_by_risk_unit={},
            position_contract_spec_sha256_by_risk_unit={},
            position_mark_price_by_risk_unit={},
            canonical_snapshot_path=str(canonical_path),
            canonical_snapshot_sha256=canonical_sha256,
        )

    def reconcile(
        self,
        *,
        request: StageRequest,
        phase: str,
        pit_timestamp: str,
    ) -> dict[str, Any]:
        intent_identity = {
            "contract": "tradingagent.paper_capital_reconcile_intent_path.v1",
            "stage_idempotency_key": request.idempotency_key,
        }
        intent_parts = ("intents", f"{_sha256(intent_identity)}.json")
        raw = _read_immutable_optional(self.artifact_root, intent_parts)
        if raw is not None:
            if (
                not isinstance(raw, dict)
                or raw.get("contract_id") != self.contract_id
                or raw.get("idempotency_key") != request.idempotency_key
                or raw.get("run_id") != request.run_id
                or raw.get("input_bundle_sha256") != request.input_bundle_sha256
                or raw.get("phase") != phase
            ):
                raise PaperCapitalStageError("paper_capital_reconcile_intent_conflict")
            manifest_raw = raw.get("manifest")
            if not isinstance(manifest_raw, Mapping):
                raise PaperCapitalStageError("paper_capital_reconcile_intent_invalid")
            manifest = _manifest_from_payload(manifest_raw)
        else:
            manifest = self._new_reconcile_manifest(
                request=request,
                phase=phase,
                pit_timestamp=pit_timestamp,
            )
            intent = {
                "contract_id": self.contract_id,
                "idempotency_key": request.idempotency_key,
                "run_id": request.run_id,
                "input_bundle_sha256": request.input_bundle_sha256,
                "phase": phase,
                "manifest": _manifest_payload(manifest),
                "real_trading_enabled": False,
            }
            _write_immutable(self.artifact_root, intent_parts, intent)
        try:
            result = self.ledger.mtm_reconcile(manifest)
        except MarketCapitalLedgerError as exc:
            raise PaperCapitalStageError(
                f"paper_capital_reconcile_rejected:{exc}"
            ) from exc
        return {
            "status": "reconciled",
            "event_id": result["event_id"],
            "lineage_sha256": result["lineage_sha256"],
            "equity_cny": result["equity_cny"],
            "cash_balance_cny": result["cash_balance_cny"],
            "positions_market_value_cny": result["positions_market_value_cny"],
            "canonical_snapshot_sha256": manifest.canonical_snapshot_sha256,
            "canonical_snapshot_path": manifest.canonical_snapshot_path,
            "real_trading_enabled": False,
        }


def _wrapped_identity(
    *,
    stage: RunStage,
    component_id: str,
    base_port: DayStagePort,
    account: PaperCapitalAccount,
) -> ComponentIdentity:
    return ComponentIdentity(
        stage=stage,
        component_id=component_id,
        version="1",
        artifact_sha256=_sha256(
            {
                "contract": component_id,
                "base": base_port.identity.to_dict(),
                "capital_account": account.identity_sha256,
            }
        ),
    )


class CapitalBackedPreopenStagePort:
    """Reconcile the canonical simulated account before any new-risk stage."""

    def __init__(
        self,
        *,
        base_port: DayStagePort,
        account: PaperCapitalAccount,
    ) -> None:
        if base_port.identity.stage is not RunStage.PREOPEN:
            raise PaperCapitalStageError("preopen_base_stage_invalid")
        self._base_port = base_port
        self._account = account
        self.identity = _wrapped_identity(
            stage=RunStage.PREOPEN,
            component_id="capital-backed-preopen-stage",
            base_port=base_port,
            account=account,
        )

    def execute(self, request: StageRequest) -> StageResult:
        if request.stage is not RunStage.PREOPEN:
            raise PaperCapitalStageError("preopen_request_stage_invalid")
        payload = dict(self._base_port.execute(request).payload)
        proof = self._account.reconcile(
            request=request,
            phase="preopen",
            pit_timestamp=request.bundle.context.decision_as_of,
        )
        payload.update(
            {
                "account_authority_valid": True,
                "position_authority_valid": True,
                "capital_reconcile_status": proof["status"],
                "capital_ledger_event_id": proof["event_id"],
                "capital_ledger_head_sha256": proof["lineage_sha256"],
                "capital_reconcile_source_sha256": proof["canonical_snapshot_sha256"],
            }
        )
        return StageResult(payload=payload)


class CapitalBackedRiskStagePort:
    """Reserve worst-case A-share cash/exposure before approving new risk."""

    def __init__(
        self,
        *,
        base_port: DayStagePort,
        account: PaperCapitalAccount,
        effect_guard: CapitalEffectGuard | None = None,
    ) -> None:
        if base_port.identity.stage is not RunStage.RISK_CHECKED:
            raise PaperCapitalStageError("risk_base_stage_invalid")
        self._base_port = base_port
        self._account = account
        if effect_guard is not None and not isinstance(
            effect_guard,
            CapitalEffectGuard,
        ):
            raise PaperCapitalStageError("capital_effect_guard_invalid")
        self._effect_guard = effect_guard
        self.identity = _wrapped_identity(
            stage=RunStage.RISK_CHECKED,
            component_id="capital-backed-risk-stage",
            base_port=base_port,
            account=account,
        )

    @staticmethod
    def _rejection(order: Mapping[str, Any], reason: str) -> dict[str, str]:
        decision_id = order.get("decision_id")
        if not isinstance(decision_id, str) or not decision_id:
            raise PaperCapitalStageError("capital_risk_decision_id_invalid")
        return {
            "decision_id": decision_id,
            "reason": f"market_capital_reservation_rejected:{reason}",
        }

    @staticmethod
    def _preflight_order(
        order: Mapping[str, Any],
        *,
        request: StageRequest,
    ) -> dict[str, Any]:
        value = dict(order)
        order_id = value.get("order_id")
        decision_id = value.get("decision_id")
        symbol = value.get("symbol")
        intent = value.get("intent")
        side = value.get("side")
        quantity = value.get("quantity")
        price = value.get("reservation_price_cny")
        fee = value.get("expected_fee_cny")
        if (
            not isinstance(order_id, str)
            or not order_id
            or order_id != order_id.strip()
            or not isinstance(decision_id, str)
            or not decision_id
            or decision_id != decision_id.strip()
            or not isinstance(symbol, str)
            or symbol != symbol.strip().upper()
            or not is_mainboard_tradable(symbol)
        ):
            raise PaperCapitalStageError(
                "capital_risk_preflight_order_identity_invalid"
            )
        if (
            intent not in {"open", "increase", "reduce", "exit"}
            or side not in {"buy", "sell"}
            or (intent in {"open", "increase"} and side != "buy")
            or (intent in {"reduce", "exit"} and side != "sell")
            or isinstance(quantity, bool)
            or not isinstance(quantity, int)
            or quantity <= 0
            or (side == "buy" and quantity % 100 != 0)
            or isinstance(price, bool)
            or not isinstance(price, (int, float))
            or not math.isfinite(float(price))
            or float(price) <= 0.0
            or isinstance(fee, bool)
            or not isinstance(fee, (int, float))
            or not math.isfinite(float(fee))
            or float(fee) < 0.0
        ):
            raise PaperCapitalStageError(
                "capital_risk_preflight_order_economics_invalid"
            )
        reservation_fields = (
            _CAPITAL_RESERVATION_FIELDS | _LEGACY_CAPITAL_RESERVATION_FIELDS
        )
        if any(field_name in value for field_name in reservation_fields):
            reason = (
                "capital_risk_sell_reservation_fields_forbidden"
                if intent in {"reduce", "exit"}
                else "capital_risk_preexisting_reservation_fields_forbidden"
            )
            raise PaperCapitalStageError(reason)
        context = request.bundle.context
        if (
            value.get("capital_authority_id") != context.authority_id
            or value.get("authority_generation") != context.authority_generation
            or value.get("execution_lineage") != context.execution_lineage
            or getattr(context, "account_type", None) != "simulated"
            or getattr(context, "real_trading_enabled", None) is not False
            or value.get("real_trading_enabled", False) is not False
        ):
            raise PaperCapitalStageError("capital_risk_preflight_authority_invalid")
        return value

    def execute(self, request: StageRequest) -> StageResult:
        if request.stage is not RunStage.RISK_CHECKED:
            raise PaperCapitalStageError("risk_request_stage_invalid")
        payload = dict(self._base_port.execute(request).payload)
        raw_orders = payload.get("approved_orders")
        raw_rejections = payload.get("rejected_decisions")
        if not isinstance(raw_orders, list) or not isinstance(raw_rejections, list):
            raise PaperCapitalStageError("capital_risk_payload_invalid")
        preflight_orders: list[dict[str, Any]] = []
        seen_order_ids: set[str] = set()
        seen_decision_ids: set[str] = set()
        for raw_order in raw_orders:
            if not isinstance(raw_order, Mapping):
                raise PaperCapitalStageError("capital_risk_preflight_order_invalid")
            order = self._preflight_order(raw_order, request=request)
            order_id = str(order["order_id"])
            decision_id = str(order["decision_id"])
            if order_id in seen_order_ids or decision_id in seen_decision_ids:
                raise PaperCapitalStageError(
                    "capital_risk_preflight_duplicate_identity"
                )
            seen_order_ids.add(order_id)
            seen_decision_ids.add(decision_id)
            preflight_orders.append(order)
        approved: list[dict[str, Any]] = []
        if not all(isinstance(item, Mapping) for item in raw_rejections):
            raise PaperCapitalStageError("capital_risk_rejection_invalid")
        rejected = [dict(item) for item in raw_rejections]
        for order in preflight_orders:
            intent = str(order.get("intent") or "").strip().lower()
            if intent not in {"open", "increase"}:
                approved.append(order)
                continue
            quantity = order.get("quantity")
            price = order.get("reservation_price_cny")
            fee = order.get("expected_fee_cny")
            order_id = str(order.get("order_id") or "").strip()
            symbol = str(order.get("symbol") or "").strip().upper()
            if (
                isinstance(quantity, bool)
                or not isinstance(quantity, int)
                or quantity <= 0
                or isinstance(price, bool)
                or not isinstance(price, (int, float))
                or float(price) <= 0.0
                or isinstance(fee, bool)
                or not isinstance(fee, (int, float))
                or float(fee) < 0.0
                or not order_id
                or not symbol
            ):
                rejected.append(self._rejection(order, "invalid_order_economics"))
                continue
            exposure = round(quantity * float(price), 6)
            cash = round(exposure + float(fee), 6)
            lineage_sha256 = _sha256(
                {
                    "run_id": request.run_id,
                    "input_bundle_sha256": request.input_bundle_sha256,
                    "order_id": order_id,
                    "symbol": symbol,
                    "quantity": quantity,
                    "reservation_price_cny": float(price),
                    "expected_fee_cny": float(fee),
                    "authority_id": request.bundle.context.authority_id,
                    "authority_generation": (
                        request.bundle.context.authority_generation
                    ),
                    "execution_lineage": request.bundle.context.execution_lineage,
                }
            )
            reference_id = (
                f"TA-PAPER-RESERVE:{request.bundle.context.authority_generation}:"
                f"{request.bundle.context.execution_lineage}:{request.run_id}:"
                f"{order_id}"
            )
            authorization = _authorize_capital_effect(
                self._effect_guard,
                effect="reserve",
                request=request,
                order=order,
            )
            if not authorization.allowed:
                rejected.append(self._rejection(order, authorization.reason))
                continue
            decision = self._account.ledger.reserve(
                MarketCapitalReservationRequest(
                    market="ashare",
                    reference_id=reference_id,
                    risk_unit_key=symbol,
                    worst_case_amount_cny=cash,
                    authority_id=request.bundle.context.authority_id,
                    trade_date=request.bundle.context.trade_date.replace("-", ""),
                    point_in_time_as_of=request.bundle.context.decision_as_of,
                    lineage_sha256=lineage_sha256,
                    authority_generation=(request.bundle.context.authority_generation),
                    execution_lineage_id=(request.bundle.context.execution_lineage),
                    worst_case_cash_cny=cash,
                    worst_case_exposure_cny=exposure,
                    worst_case_margin_cny=0.0,
                )
            )
            if not decision.approved:
                rejected.append(self._rejection(order, decision.reason))
                continue
            order.update(
                {
                    "market_capital_reference_id": reference_id,
                    "market_capital_reservation_id": decision.reservation_id,
                    "market_capital_reservation_event_id": decision.event_id,
                    "market_capital_lineage_sha256": lineage_sha256,
                    "market_reserved_cash_cny": cash,
                    "market_reserved_exposure_cny": exposure,
                    "market_capital_required": True,
                    "real_trading_enabled": False,
                }
            )
            approved.append(order)
        payload["approved_orders"] = approved
        payload["rejected_decisions"] = rejected
        return StageResult(payload=payload)


class CapitalBackedSimulationExecutionStagePort:
    """Execute only permitted paper orders and atomically commit capital effects.

    The supplied market snapshots are frozen into the component identity.  A
    retry therefore recreates the same simulated fill and relies on the
    canonical capital ledger's idempotent fill/release identities.  This port
    has no broker or network path.
    """

    def __init__(
        self,
        *,
        account: PaperCapitalAccount,
        market_snapshots: Mapping[str, Mapping[str, Any]],
        effect_guard: CapitalEffectGuard | None = None,
        execution_clock: TrustedExecutionClock | None = None,
    ) -> None:
        if type(execution_clock) is not NonProductionFixtureExecutionClock:
            raise PaperCapitalStageError("execution_clock_required")
        normalized: dict[str, dict[str, Any]] = {}
        normalized_authorities: dict[
            str,
            NonProductionFixtureMarketEvidenceAuthority,
        ] = {}
        for raw_order_id, raw_snapshot in dict(market_snapshots).items():
            if (
                not isinstance(raw_order_id, str)
                or not raw_order_id
                or raw_order_id != raw_order_id.strip()
                or not isinstance(raw_snapshot, Mapping)
            ):
                raise PaperCapitalStageError("paper_market_snapshot_invalid")
            order_id = raw_order_id
            snapshot = dict(raw_snapshot)
            market_authority = snapshot.pop("market_evidence_authority", None)
            required_strings = (
                "snapshot_id",
                "source_receipt_id",
                "source_sha256",
                "source_lineage_sha256",
                "dataset_id",
                "catalog_version",
                "symbol",
                "market",
                "trade_date",
                "decision_as_of",
                "capital_authority_id",
                "execution_lineage",
                "account_type",
                "market_session",
                "observed_at",
                "available_at",
                "data_through",
                "execution_time",
            )
            if any(
                not isinstance(snapshot.get(field_name), str)
                or not snapshot[field_name]
                or snapshot[field_name] != snapshot[field_name].strip()
                for field_name in required_strings
            ):
                raise PaperCapitalStageError("paper_market_snapshot_contract_invalid")
            _strict_sha256(
                snapshot["source_sha256"],
                reason="paper_market_snapshot_source_sha256_invalid",
            )
            _strict_sha256(
                snapshot["source_lineage_sha256"],
                reason="paper_market_snapshot_source_lineage_sha256_invalid",
            )
            observed = _aware_instant(
                snapshot["observed_at"],
                reason="paper_market_snapshot_observed_at_invalid",
            )
            available = _aware_instant(
                snapshot["available_at"],
                reason="paper_market_snapshot_available_at_invalid",
            )
            execution = _aware_instant(
                snapshot["execution_time"],
                reason="paper_market_execution_time_invalid",
            )
            data_through = _aware_instant(
                snapshot["data_through"],
                reason="paper_market_snapshot_data_through_invalid",
            )
            decision = _aware_instant(
                snapshot["decision_as_of"],
                reason="paper_market_snapshot_decision_as_of_invalid",
            )
            session_calendar_receipt, calendar = _session_calendar_receipt(
                snapshot.get("session_calendar_receipt"),
                reason_prefix="paper_market_snapshot",
            )
            if (
                data_through > observed
                or observed > available
                or available > execution
                or decision > execution
            ):
                raise PaperCapitalStageError("paper_market_snapshot_time_order_invalid")
            if execution - data_through > ASHARE_EXECUTION_QUOTE_MAX_AGE:
                raise PaperCapitalStageError("paper_market_snapshot_stale")
            try:
                snapshot_trade_date = date.fromisoformat(snapshot["trade_date"])
            except ValueError as exc:
                raise PaperCapitalStageError(
                    "paper_market_snapshot_trade_date_invalid"
                ) from exc
            if snapshot_trade_date.isoformat() != snapshot["trade_date"]:
                raise PaperCapitalStageError("paper_market_snapshot_trade_date_invalid")
            expected_session = ashare_continuous_session(execution)
            if (
                snapshot_trade_date not in calendar.sessions
                or snapshot.get("market_session") != expected_session
                or data_through != observed
                or data_through.astimezone(_SHANGHAI).date() != snapshot_trade_date
            ):
                raise PaperCapitalStageError("paper_market_snapshot_session_invalid")
            generation = snapshot.get("authority_generation")
            if (
                snapshot["market"] != "ashare"
                or snapshot["account_type"] != "simulated"
                or snapshot.get("real_trading_enabled") is not False
                or isinstance(generation, bool)
                or not isinstance(generation, int)
                or generation <= 0
            ):
                raise PaperCapitalStageError("paper_market_snapshot_boundary_invalid")
            if type(market_authority) is not (
                NonProductionFixtureMarketEvidenceAuthority
            ):
                raise PaperCapitalStageError(
                    "paper_market_snapshot_evidence_authority_required"
                )
            quote_evidence = market_authority.evidence
            verification = market_authority.verification
            bid_price = float(snapshot.get("bid_price", 0.0))
            ask_price = float(snapshot.get("ask_price", 0.0))
            bid_size = int(snapshot.get("bid_size", 0))
            ask_size = int(snapshot.get("ask_size", 0))
            if (
                type(quote_evidence) is not AShareExecutionQuoteEvidence
                or quote_evidence.order_id != order_id
                or quote_evidence.symbol != snapshot["symbol"].upper()
                or quote_evidence.bid_price_cny != bid_price
                or quote_evidence.ask_price_cny != ask_price
                or quote_evidence.bid_size != bid_size
                or quote_evidence.ask_size != ask_size
                or quote_evidence.previous_close_cny
                != float(snapshot.get("previous_close", 0.0))
                or quote_evidence.market_session != snapshot["market_session"]
                or quote_evidence.execution_time != execution
                or quote_evidence.source.dataset_id != snapshot["dataset_id"]
                or quote_evidence.source.catalog_version != snapshot["catalog_version"]
                or quote_evidence.source.source_receipt_id
                != snapshot["source_receipt_id"]
                or quote_evidence.source.source_receipt_sha256
                != snapshot["source_sha256"]
                or quote_evidence.source.source_lineage_sha256
                != snapshot["source_lineage_sha256"]
                or quote_evidence.source.data_through != data_through
                or quote_evidence.source.observed_at != observed
                or quote_evidence.source.available_at != available
                or quote_evidence.session_calendar_receipt_sha256
                != session_calendar_receipt["receipt_sha256"]
                or quote_evidence.context.trade_date != snapshot_trade_date
                or quote_evidence.context.decision_as_of != decision
                or quote_evidence.context.capital_authority_id
                != snapshot["capital_authority_id"]
                or quote_evidence.context.authority_generation != generation
                or quote_evidence.context.execution_lineage_id
                != snapshot["execution_lineage"]
                or verification.evidence_sha256 != quote_evidence.sha256()
                or verification.proof_sha256 != verification.recompute_proof_sha256()
            ):
                raise PaperCapitalStageError(
                    "paper_market_snapshot_evidence_authority_invalid"
                )
            snapshot["observed_at"] = observed.isoformat()
            snapshot["available_at"] = available.isoformat()
            snapshot["data_through"] = data_through.isoformat()
            snapshot["execution_time"] = execution.isoformat()
            snapshot["decision_as_of"] = decision.isoformat()
            snapshot["session_calendar_receipt"] = session_calendar_receipt
            snapshot["market_evidence_authority_sha256"] = (
                market_authority.authority_sha256
            )
            snapshot["market_evidence_verification"] = verification.canonical_payload()
            normalized[order_id] = json.loads(_canonical_bytes(snapshot))
            normalized_authorities[order_id] = market_authority
        if effect_guard is not None and not isinstance(
            effect_guard,
            CapitalEffectGuard,
        ):
            raise PaperCapitalStageError("capital_effect_guard_invalid")
        self._account = account
        self._effect_guard = effect_guard
        self._execution_clock = execution_clock
        self._market_snapshots = MappingProxyType(normalized)
        self._market_authorities = MappingProxyType(normalized_authorities)
        self.identity = ComponentIdentity(
            stage=RunStage.ORDERS_SIMULATED,
            component_id="capital-backed-simulation-execution-stage",
            version="1",
            artifact_sha256=_sha256(
                {
                    "contract": "tradingagent.capital_backed_execution.v1",
                    "capital_account": account.identity_sha256,
                    "execution_clock": execution_clock.identity_sha256,
                    "market_snapshots": normalized,
                    "real_trading_enabled": False,
                }
            ),
        )

    @property
    def execution_clock(self) -> TrustedExecutionClock:
        """Expose the immutable clock so guarded wrappers preserve authority."""

        return self._execution_clock

    @property
    def market_snapshots(self) -> Mapping[str, Mapping[str, Any]]:
        """Expose only normalized, authority-bound snapshots."""

        return self._market_snapshots

    def with_effect_guard(
        self,
        effect_guard: CapitalEffectGuard,
    ) -> "CapitalBackedSimulationExecutionStagePort":
        """Rebuild the exact evidence-bound port with a fresh effect guard."""

        rebound = {
            order_id: {
                **dict(snapshot),
                "market_evidence_authority": self._market_authorities[order_id],
            }
            for order_id, snapshot in self._market_snapshots.items()
        }
        return CapitalBackedSimulationExecutionStagePort(
            account=self._account,
            market_snapshots=rebound,
            effect_guard=effect_guard,
            execution_clock=self._execution_clock,
        )

    def validated_market_snapshot(
        self,
        *,
        request: StageRequest,
        order_id: str,
        side: str,
    ) -> dict[str, Any]:
        stored = self._market_snapshots.get(order_id)
        if stored is None:
            raise PaperCapitalStageError(f"paper_market_snapshot_missing:{order_id}")
        snapshot = dict(stored)
        context = request.bundle.context
        if (
            snapshot.get("market") != "ashare"
            or snapshot.get("trade_date") != context.trade_date
            or snapshot.get("decision_as_of") != context.decision_as_of
            or snapshot.get("capital_authority_id") != context.authority_id
            or snapshot.get("authority_generation") != context.authority_generation
            or snapshot.get("execution_lineage") != context.execution_lineage
            or snapshot.get("account_type") != "simulated"
            or snapshot.get("real_trading_enabled") is not False
            or getattr(context, "account_type", None) != "simulated"
            or getattr(context, "real_trading_enabled", None) is not False
        ):
            raise PaperCapitalStageError("paper_market_snapshot_authority_invalid")
        decision = _aware_instant(
            context.decision_as_of,
            reason="paper_market_decision_as_of_invalid",
        )
        observed = _aware_instant(
            snapshot["observed_at"],
            reason="paper_market_snapshot_observed_at_invalid",
        )
        available = _aware_instant(
            snapshot["available_at"],
            reason="paper_market_snapshot_available_at_invalid",
        )
        execution = _aware_instant(
            snapshot["execution_time"],
            reason="paper_market_execution_time_invalid",
        )
        if observed > available or available > execution or decision > execution:
            raise PaperCapitalStageError("paper_market_snapshot_time_order_invalid")
        if execution.astimezone(_SHANGHAI).date().isoformat() != context.trade_date:
            raise PaperCapitalStageError("paper_market_execution_trade_date_mismatch")
        price_field = "ask_price" if side == "buy" else "bid_price"
        size_field = "ask_size" if side == "buy" else "bid_size"
        for field_name, minimum in (
            (price_field, 0.00000001),
            (size_field, 0.0),
            ("previous_close", 0.00000001),
        ):
            value = snapshot.get(field_name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or float(value) < minimum
            ):
                raise PaperCapitalStageError(
                    f"paper_market_snapshot_{field_name}_invalid"
                )
        if side == "buy":
            cash_available = snapshot.get("cash_available")
            if (
                isinstance(cash_available, bool)
                or not isinstance(cash_available, (int, float))
                or not math.isfinite(float(cash_available))
                or float(cash_available) < 0.0
            ):
                raise PaperCapitalStageError(
                    "paper_market_snapshot_cash_available_invalid"
                )
        return snapshot

    def _execution_clock_reading(
        self,
        *,
        order_id: str,
        effect: str,
    ) -> datetime:
        """Read one explicit side-effect instant without silently retrying."""

        try:
            current = self._execution_clock.now(
                effect=effect,
                order_id=order_id,
            )
        except TrustedExecutionClockError as exc:
            raise PaperCapitalStageError("execution_clock_unavailable") from exc
        if current.tzinfo is None or current.utcoffset() is None:
            raise PaperCapitalStageError("execution_clock_timezone_required")
        return current

    @staticmethod
    def _validate_fresh_clock_reading(
        *,
        request: StageRequest,
        snapshot: Mapping[str, Any],
        effect: str,
        current: datetime,
        not_before: datetime | None = None,
    ) -> None:
        """Validate quote freshness and monotonic side-effect causality."""

        if not_before is not None and current < not_before:
            raise PaperCapitalStageError(
                f"paper_market_clock_regressed_before_{effect}"
            )
        available = _aware_instant(
            snapshot["available_at"],
            reason="paper_market_snapshot_available_at_invalid",
        )
        data_through = _aware_instant(
            snapshot["data_through"],
            reason="paper_market_snapshot_data_through_invalid",
        )
        quoted_execution = _aware_instant(
            snapshot["execution_time"],
            reason="paper_market_execution_time_invalid",
        )
        current_local = current.astimezone(_SHANGHAI)
        if current_local.date().isoformat() != request.bundle.context.trade_date:
            raise PaperCapitalStageError(
                f"paper_market_clock_trade_date_mismatch_before_{effect}"
            )
        expected_session = ashare_continuous_session(current)
        if expected_session != snapshot.get("market_session"):
            raise PaperCapitalStageError(
                f"paper_market_clock_session_mismatch_before_{effect}"
            )
        if available > current or quoted_execution > current or data_through > current:
            raise PaperCapitalStageError(
                f"paper_market_snapshot_future_before_{effect}"
            )
        if current - data_through > ASHARE_EXECUTION_QUOTE_MAX_AGE:
            raise PaperCapitalStageError(f"paper_market_snapshot_stale_before_{effect}")

    @staticmethod
    def approved_order_map(request: StageRequest) -> dict[str, dict[str, Any]]:
        try:
            payload = request.bundle.receipt_for(RunStage.RISK_CHECKED).payload
        except (AttributeError, KeyError) as exc:
            raise PaperCapitalStageError("risk_receipt_missing") from exc
        rows = payload.get("approved_orders")
        if not isinstance(rows, list):
            raise PaperCapitalStageError("risk_approved_orders_invalid")
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, Mapping):
                raise PaperCapitalStageError("risk_approved_order_invalid")
            order = dict(row)
            order_id = str(order.get("order_id") or "").strip()
            if not order_id or order_id in result:
                raise PaperCapitalStageError("risk_order_identity_invalid")
            result[order_id] = order
        return result

    @staticmethod
    def validate_order(
        order: Mapping[str, Any],
        *,
        request: StageRequest,
    ) -> tuple[str, str, str, int, float]:
        order_id = str(order.get("order_id") or "").strip()
        symbol = str(order.get("symbol") or "").strip().upper()
        intent = str(order.get("intent") or "").strip().lower()
        side = str(order.get("side") or "").strip().lower()
        quantity = order.get("quantity")
        price = order.get("reservation_price_cny")
        if (
            not order_id
            or not symbol
            or not is_mainboard_tradable(symbol)
            or intent not in {"open", "increase", "reduce", "exit"}
            or side not in {"buy", "sell"}
            or (intent in {"open", "increase"} and side != "buy")
            or (intent in {"reduce", "exit"} and side != "sell")
            or isinstance(quantity, bool)
            or not isinstance(quantity, int)
            or quantity <= 0
            or (side == "buy" and quantity % 100 != 0)
            or isinstance(price, bool)
            or not isinstance(price, (int, float))
            or not math.isfinite(float(price))
            or float(price) <= 0.0
        ):
            raise PaperCapitalStageError("paper_execution_order_invalid")
        if any(
            field_name in order for field_name in _LEGACY_CAPITAL_RESERVATION_FIELDS
        ):
            reason = (
                "paper_execution_sell_reservation_fields_forbidden"
                if intent in {"reduce", "exit"}
                else "paper_execution_legacy_reservation_fields_forbidden"
            )
            raise PaperCapitalStageError(reason)
        if intent in {"reduce", "exit"} and any(
            field_name in order for field_name in _CAPITAL_RESERVATION_FIELDS
        ):
            raise PaperCapitalStageError(
                "paper_execution_sell_reservation_fields_forbidden"
            )
        context = request.bundle.context
        if (
            order.get("capital_authority_id") != context.authority_id
            or order.get("authority_generation") != context.authority_generation
            or order.get("execution_lineage") != context.execution_lineage
            or order.get("real_trading_enabled", False) is not False
        ):
            raise PaperCapitalStageError("paper_execution_order_authority_invalid")
        return order_id, symbol, intent, quantity, float(price)

    def _execution_seed(
        self,
        *,
        request: StageRequest,
        order_id: str,
        symbol: str,
        market_sha256: str,
    ) -> dict[str, Any]:
        identity = {
            "contract": "tradingagent.paper_execution_seed.v1",
            "stage_idempotency_key": request.idempotency_key,
            "run_id": request.run_id,
            "input_bundle_sha256": request.input_bundle_sha256,
            "order_id": order_id,
            "symbol": symbol,
            "market_snapshot_sha256": market_sha256,
        }
        path_parts = ("execution-seeds", f"{_sha256(identity)}.json")
        payload = _read_immutable_optional(
            self._account.artifact_root,
            path_parts,
        )
        if payload is not None:
            if not isinstance(payload, dict) or payload.get("identity") != identity:
                raise PaperCapitalStageError("paper_execution_seed_conflict")
            return payload
        snapshot = self._account.ledger.snapshot()
        payload = {
            "identity": identity,
            "capital_authority_id": snapshot.authority_id,
            "authority_generation": snapshot.authority_generation,
            "execution_lineage_id": snapshot.execution_lineage_id,
            "position_quantity": int(
                snapshot.positions_quantity_by_risk_unit.get(symbol, 0)
            ),
            "position_cost_basis_cny": float(
                snapshot.positions_cost_basis_cny_by_risk_unit.get(symbol, 0.0)
            ),
            "position_entry_fee_cny": float(
                snapshot.positions_entry_fee_cny_by_risk_unit.get(symbol, 0.0)
            ),
            "position_market_value_cny": round(
                float(snapshot.positions_market_value_cny), 6
            ),
            "ledger_event_id": snapshot.event_id,
            "ledger_event_checksum": snapshot.event_checksum,
            "real_trading_enabled": False,
        }
        _write_immutable(self._account.artifact_root, path_parts, payload)
        return payload

    def _load_or_create_outbox_intent(
        self,
        *,
        operation: str,
        stable_payload: Mapping[str, Any],
        commit_request: Mapping[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        outbox_id = _sha256(
            {
                "contract": "tradingagent.paper_execution_outbox_identity.v1",
                "operation": operation,
                "stable_payload": stable_payload,
            }
        )
        parts = ("execution-outbox", "pending", f"{outbox_id}.json")
        existing = _read_immutable_optional(self._account.artifact_root, parts)
        if existing is not None:
            stored_request_raw = (
                existing.get("commit_request") if isinstance(existing, dict) else None
            )
            if (
                not isinstance(existing, dict)
                or existing.get("contract_id")
                != "tradingagent.paper_execution_outbox_intent.v1"
                or existing.get("outbox_id") != outbox_id
                or existing.get("operation") != operation
                or existing.get("stable_payload") != dict(stable_payload)
                or not isinstance(stored_request_raw, Mapping)
                or existing.get("real_trading_enabled") is not False
            ):
                raise PaperCapitalStageError("paper_execution_outbox_conflict")
            stored_request = dict(stored_request_raw)
            expected_request = dict(commit_request)
            mutable_head_fields = {
                "expected_ledger_event_id",
                "expected_ledger_checksum",
            }
            if any(
                stored_request.get(key) != expected_request.get(key)
                for key in set(stored_request) | set(expected_request)
                if key not in mutable_head_fields
            ):
                raise PaperCapitalStageError("paper_execution_outbox_conflict")
            if not isinstance(stored_request.get("expected_ledger_event_id"), str):
                raise PaperCapitalStageError("paper_execution_outbox_head_invalid")
            _strict_sha256(
                stored_request.get("expected_ledger_checksum"),
                reason="paper_execution_outbox_head_invalid",
            )
            return outbox_id, stored_request
        intent = {
            "contract_id": "tradingagent.paper_execution_outbox_intent.v1",
            "outbox_id": outbox_id,
            "operation": operation,
            "stable_payload": dict(stable_payload),
            "commit_request": dict(commit_request),
            "real_trading_enabled": False,
        }
        _write_immutable(self._account.artifact_root, parts, intent)
        return outbox_id, dict(commit_request)

    def _write_outbox_settlement(
        self,
        *,
        outbox_id: str,
        settlement: Mapping[str, Any],
    ) -> None:
        expected = {
            **dict(settlement),
            "contract_id": "tradingagent.paper_execution_outbox_settlement.v1",
            "outbox_id": outbox_id,
            "status": "settled",
            "real_trading_enabled": False,
        }
        _write_immutable(
            self._account.artifact_root,
            ("execution-outbox", "settled", f"{outbox_id}.json"),
            expected,
        )

    def _pending_outbox_intents(self) -> list[tuple[str, dict[str, Any]]]:
        outbox_root = self._account.artifact_root / "execution-outbox"
        pending_root = outbox_root / "pending"
        for path in (outbox_root, pending_root):
            try:
                metadata = path.lstat()
            except FileNotFoundError:
                return []
            except OSError as exc:
                raise PaperCapitalStageError(
                    "paper_execution_outbox_directory_unavailable"
                ) from exc
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise PaperCapitalStageError("paper_execution_outbox_directory_invalid")

        intents: list[tuple[str, dict[str, Any]]] = []
        with _artifact_directory_fd(
            self._account.artifact_root,
            ("execution-outbox", "pending"),
        ) as directory_fd:
            for filename in sorted(os.listdir(directory_fd)):
                if filename.startswith("."):
                    continue
                if not filename.endswith(".json"):
                    raise PaperCapitalStageError("paper_execution_outbox_entry_invalid")
                outbox_id = _strict_sha256(
                    filename[:-5],
                    reason="paper_execution_outbox_entry_invalid",
                )
                raw = _read_at(directory_fd, filename)
                try:
                    intent = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise PaperCapitalStageError(
                        "paper_execution_outbox_entry_invalid"
                    ) from exc
                if not isinstance(intent, dict):
                    raise PaperCapitalStageError("paper_execution_outbox_entry_invalid")
                operation = intent.get("operation")
                stable_payload = intent.get("stable_payload")
                if (
                    intent.get("contract_id")
                    != "tradingagent.paper_execution_outbox_intent.v1"
                    or intent.get("outbox_id") != outbox_id
                    or operation not in {"buy_fill_commit", "ashare_sell_commit"}
                    or not isinstance(stable_payload, Mapping)
                    or intent.get("real_trading_enabled") is not False
                    or _sha256(
                        {
                            "contract": (
                                "tradingagent.paper_execution_outbox_identity.v1"
                            ),
                            "operation": operation,
                            "stable_payload": stable_payload,
                        }
                    )
                    != outbox_id
                ):
                    raise PaperCapitalStageError("paper_execution_outbox_entry_invalid")
                intents.append((outbox_id, intent))
        return intents

    def _recover_committed_outbox(
        self,
        *,
        request: StageRequest,
        order: Mapping[str, Any],
        order_id: str,
        market_sha256: str,
    ) -> dict[str, Any] | None:
        matches: list[tuple[str, dict[str, Any]]] = []
        for outbox_id, intent in self._pending_outbox_intents():
            stable_payload = intent["stable_payload"]
            if (
                stable_payload.get("stage_idempotency_key") == request.idempotency_key
                and stable_payload.get("run_id") == request.run_id
                and stable_payload.get("input_bundle_sha256")
                == request.input_bundle_sha256
                and stable_payload.get("order_id") == order_id
            ):
                matches.append((outbox_id, intent))
        if not matches:
            return None
        if len(matches) != 1:
            raise PaperCapitalStageError("paper_execution_outbox_recovery_ambiguous")

        outbox_id, intent = matches[0]
        operation = intent["operation"]
        stable_payload = intent["stable_payload"]
        commit_request_raw = intent.get("commit_request")
        receipt_seed_raw = stable_payload.get("receipt_seed")
        if not isinstance(commit_request_raw, Mapping) or not isinstance(
            receipt_seed_raw,
            Mapping,
        ):
            raise PaperCapitalStageError("paper_execution_outbox_recovery_invalid")
        commit_request = dict(commit_request_raw)
        receipt_seed = dict(receipt_seed_raw)
        receipt_sha256 = _sha256(receipt_seed)
        expected_operation = (
            "buy_fill_commit"
            if str(order.get("intent") or "") in {"open", "increase"}
            else "ashare_sell_commit"
        )
        if (
            operation != expected_operation
            or stable_payload.get("receipt_sha256") != receipt_sha256
            or commit_request.get("receipt_sha256") != receipt_sha256
            or stable_payload.get("fill_id") != receipt_seed.get("simulated_fill_id")
            or commit_request.get("execution_fill_id") != stable_payload.get("fill_id")
            or stable_payload.get("source_sha256") != market_sha256
            or commit_request.get("source_sha256") != market_sha256
            or (
                operation == "buy_fill_commit"
                and stable_payload.get("local_trade_sha256")
                != commit_request.get("local_trade_sha256")
            )
            or (
                operation == "ashare_sell_commit"
                and stable_payload.get("local_position_sha256")
                != commit_request.get("local_position_sha256")
            )
            or receipt_seed.get("market_snapshot_sha256") != market_sha256
            or receipt_seed.get("order_id") != order_id
            or receipt_seed.get("symbol") != order.get("symbol")
            or receipt_seed.get("intent") != order.get("intent")
            or receipt_seed.get("requested_quantity") != order.get("quantity")
            or receipt_seed.get("capital_authority_id")
            != request.bundle.context.authority_id
            or receipt_seed.get("authority_generation")
            != request.bundle.context.authority_generation
            or receipt_seed.get("execution_lineage")
            != request.bundle.context.execution_lineage
            or receipt_seed.get("execution_clock_sha256")
            != self._execution_clock.identity_sha256
            or receipt_seed.get("status") not in {"filled", "partial"}
            or receipt_seed.get("real_trading_enabled") is not False
            or receipt_seed.get("capital_commit_status") is not None
            or receipt_seed.get("fill_fingerprint") is not None
        ):
            raise PaperCapitalStageError("paper_execution_outbox_recovery_invalid")

        expected_head = commit_request.get("expected_ledger_event_id")
        if (
            not isinstance(expected_head, str)
            or not expected_head
            or self._account.ledger.snapshot().event_id == expected_head
        ):
            return None
        try:
            if operation == "buy_fill_commit":
                replay_request = MarketCapitalFillCommitRequest(**commit_request)
                decision = self._account.ledger.commit_fill(replay_request)
            else:
                replay_request = MarketCapitalAshareSellCommitRequest(**commit_request)
                decision = self._account.ledger.commit_ashare_sell(replay_request)
        except (TypeError, ValueError) as exc:
            raise PaperCapitalStageError(
                "paper_execution_outbox_request_invalid"
            ) from exc
        if not decision.committed:
            return None
        if not decision.idempotent or not decision.event_id:
            raise PaperCapitalStageError(
                "paper_execution_outbox_recovery_not_idempotent"
            )
        self._write_outbox_settlement(
            outbox_id=outbox_id,
            settlement={
                "operation": operation,
                "pending_intent_sha256": _sha256(intent),
                "capital_commit_event_id": decision.event_id,
            },
        )
        receipt = {
            **receipt_seed,
            "capital_commit_receipt_id": decision.event_id,
            "capital_commit_status": "committed",
        }
        receipt["fill_fingerprint"] = _sha256(receipt)
        return receipt

    def _commit_buy(
        self,
        *,
        request: StageRequest,
        order: Mapping[str, Any],
        receipt_seed: Mapping[str, Any],
        fill_id: str,
        filled_quantity: int,
        filled_price: float,
        fee_cny: float,
        filled_at: str,
        status: str,
        source_sha256: str,
        local_trade_sha256: str,
    ) -> str:
        reservation_id = str(order.get("market_capital_reservation_id") or "").strip()
        reservation_event_id = str(
            order.get("market_capital_reservation_event_id") or ""
        ).strip()
        reservation_reference_id = str(
            order.get("market_capital_reference_id") or ""
        ).strip()
        lineage_sha256 = str(order.get("market_capital_lineage_sha256") or "").strip()
        if not all(
            (
                reservation_id,
                reservation_event_id,
                reservation_reference_id,
                lineage_sha256,
            )
        ):
            raise PaperCapitalStageError("paper_execution_reservation_proof_missing")
        head = self._account.ledger.snapshot()
        notional = round(filled_quantity * filled_price, 6)
        cash_debit = round(notional + fee_cny, 6)
        reference_id = (
            f"MCAPFILL:{request.bundle.context.authority_generation}:"
            f"{request.bundle.context.execution_lineage}:{reservation_id}:"
            f"{fill_id}"
        )
        commit_request = MarketCapitalFillCommitRequest(
            market="ashare",
            reference_id=reference_id,
            reservation_id=reservation_id,
            reservation_event_id=reservation_event_id,
            reservation_reference_id=reservation_reference_id,
            risk_unit_key=str(order["symbol"]).strip().upper(),
            authority_id=request.bundle.context.authority_id,
            authority_generation=request.bundle.context.authority_generation,
            execution_lineage_id=request.bundle.context.execution_lineage,
            lineage_sha256=lineage_sha256,
            order_id=str(order["order_id"]),
            idempotency_key=_sha256(
                {
                    "stage_idempotency_key": request.idempotency_key,
                    "order_id": order["order_id"],
                    "fill_id": fill_id,
                }
            ),
            execution_fill_id=fill_id,
            fill_sequence=1,
            side="buy",
            status=status,
            terminal=True,
            actual_filled_quantity=filled_quantity,
            actual_fill_price=filled_price,
            actual_cash_debit_cny=cash_debit,
            actual_exposure_cny=notional,
            actual_margin_cny=0.0,
            actual_fee_cash_cny=fee_cny,
            filled_at=filled_at,
            point_in_time_as_of=request.bundle.context.decision_as_of,
            source=(
                f"tradingagent:paper_execution:{request.run_id}:{order['order_id']}"
            ),
            source_sha256=source_sha256,
            receipt_sha256=_sha256(receipt_seed),
            local_trade_sha256=local_trade_sha256,
            expected_ledger_event_id=head.event_id,
            expected_ledger_checksum=head.event_checksum,
        )
        stable_payload = {
            "stage_idempotency_key": request.idempotency_key,
            "run_id": request.run_id,
            "input_bundle_sha256": request.input_bundle_sha256,
            "order_id": str(order["order_id"]),
            "fill_id": fill_id,
            "receipt_sha256": _sha256(receipt_seed),
            "source_sha256": source_sha256,
            "local_trade_sha256": local_trade_sha256,
            "receipt_seed": dict(receipt_seed),
        }
        outbox_id, stored_request = self._load_or_create_outbox_intent(
            operation="buy_fill_commit",
            stable_payload=stable_payload,
            commit_request=asdict(commit_request),
        )
        try:
            replay_request = MarketCapitalFillCommitRequest(**stored_request)
        except (TypeError, ValueError) as exc:
            raise PaperCapitalStageError(
                "paper_execution_outbox_request_invalid"
            ) from exc
        decision = self._account.ledger.commit_fill(replay_request)
        if not decision.committed or not decision.event_id:
            raise PaperCapitalStageError(
                f"paper_capital_fill_rejected:{decision.reason}"
            )
        self._write_outbox_settlement(
            outbox_id=outbox_id,
            settlement={
                "operation": "buy_fill_commit",
                "pending_intent_sha256": _sha256(
                    {
                        "contract_id": "tradingagent.paper_execution_outbox_intent.v1",
                        "outbox_id": outbox_id,
                        "operation": "buy_fill_commit",
                        "stable_payload": stable_payload,
                        "commit_request": stored_request,
                        "real_trading_enabled": False,
                    }
                ),
                "capital_commit_event_id": decision.event_id,
            },
        )
        return decision.event_id

    def release_unfilled(
        self,
        *,
        request: StageRequest,
        order: Mapping[str, Any],
        reason: str,
    ) -> str | None:
        intent = str(order.get("intent") or "").strip().lower()
        if intent not in {"open", "increase"}:
            if any(
                field_name in order
                for field_name in (
                    _CAPITAL_RESERVATION_FIELDS | _LEGACY_CAPITAL_RESERVATION_FIELDS
                )
            ):
                raise PaperCapitalStageError(
                    "paper_execution_sell_reservation_fields_forbidden"
                )
            return None
        reservation_id = str(order.get("market_capital_reservation_id") or "").strip()
        reservation_event_id = str(
            order.get("market_capital_reservation_event_id") or ""
        ).strip()
        reservation_reference_id = str(
            order.get("market_capital_reference_id") or ""
        ).strip()
        lineage_sha256 = str(order.get("market_capital_lineage_sha256") or "").strip()
        amount = order.get("market_reserved_cash_cny")
        exposure = order.get("market_reserved_exposure_cny")
        if (
            order.get("market_capital_required") is not True
            or not reservation_id
            or not reservation_event_id
            or not reservation_reference_id
            or not lineage_sha256
            or isinstance(amount, bool)
            or not isinstance(amount, (int, float))
            or float(amount) <= 0.0
            or not math.isfinite(float(amount))
            or isinstance(exposure, bool)
            or not isinstance(exposure, (int, float))
            or float(exposure) <= 0.0
            or not math.isfinite(float(exposure))
        ):
            raise PaperCapitalStageError("paper_execution_release_proof_invalid")
        _strict_sha256(
            lineage_sha256,
            reason="paper_execution_release_proof_invalid",
        )
        expected_reference_id = (
            f"TA-PAPER-RESERVE:{request.bundle.context.authority_generation}:"
            f"{request.bundle.context.execution_lineage}:{request.run_id}:"
            f"{order['order_id']}"
        )
        if reservation_reference_id != expected_reference_id:
            raise PaperCapitalStageError("paper_execution_release_proof_invalid")
        verification = self._account.ledger.verify_reservation_identity(
            reservation_id=reservation_id,
            reference_id=reservation_reference_id,
            market="ashare",
            authority_id=request.bundle.context.authority_id,
            authority_generation=request.bundle.context.authority_generation,
            execution_lineage_id=request.bundle.context.execution_lineage,
            risk_unit_key=str(order.get("symbol") or "").strip().upper(),
            expected_event_id=reservation_event_id,
        )
        if (
            verification.get("verified") is not True
            or verification.get("lineage_sha256") != lineage_sha256
        ):
            raise PaperCapitalStageError("paper_execution_release_proof_invalid")
        terminal = verification.get("terminal")
        remaining_cash = verification.get("remaining_cash_cny")
        remaining_exposure = verification.get("remaining_exposure_cny")
        original_cash = verification.get("original_cash_cny")
        original_exposure = verification.get("original_exposure_cny")
        release_reference_id = (
            f"TA-PAPER-RELEASE:{request.run_id}:{order['order_id']}:{reason}"
        )
        if (
            terminal not in {True, False}
            or isinstance(remaining_cash, bool)
            or not isinstance(remaining_cash, (int, float))
            or isinstance(remaining_exposure, bool)
            or not isinstance(remaining_exposure, (int, float))
            or isinstance(original_cash, bool)
            or not isinstance(original_cash, (int, float))
            or float(original_cash) <= 0.0
            or isinstance(original_exposure, bool)
            or not isinstance(original_exposure, (int, float))
            or float(original_exposure) <= 0.0
        ):
            raise PaperCapitalStageError("paper_execution_release_proof_invalid")
        canonical_cash = float(original_cash if terminal else remaining_cash)
        canonical_exposure = float(
            original_exposure if terminal else remaining_exposure
        )
        if (
            (terminal is False and canonical_cash <= 0.0)
            or (terminal is False and canonical_exposure <= 0.0)
            or (terminal is True and abs(float(remaining_cash)) > 1e-9)
            or (terminal is True and abs(float(remaining_exposure)) > 1e-9)
            or not math.isclose(float(amount), canonical_cash, abs_tol=1e-9)
            or not math.isclose(
                float(exposure),
                canonical_exposure,
                abs_tol=1e-9,
            )
        ):
            raise PaperCapitalStageError("paper_execution_release_amount_mismatch")
        if terminal is False:
            authorization = _authorize_capital_effect(
                self._effect_guard,
                effect="reservation_release",
                request=request,
                order=order,
            )
            if not authorization.allowed:
                raise PaperCapitalStageError(
                    f"paper_capital_release_not_authorized:{authorization.reason}"
                )
        try:
            result = self._account.ledger.release(
                reservation_id,
                canonical_cash,
                reason,
                reference_id=release_reference_id,
            )
        except MarketCapitalLedgerError as exc:
            raise PaperCapitalStageError(
                f"paper_capital_release_rejected:{exc}"
            ) from exc
        event_id = str(result.get("event_id") or "").strip()
        release_verification = self._account.ledger.verify_release(
            reservation_id=reservation_id,
            amount_cny=canonical_cash,
            reason=reason,
            reference_id=release_reference_id,
            expected_event_id=event_id,
            authority_id=request.bundle.context.authority_id,
            authority_generation=request.bundle.context.authority_generation,
            execution_lineage_id=request.bundle.context.execution_lineage,
            risk_unit_key=str(order.get("symbol") or "").strip().upper(),
            require_terminal=True,
        )
        if release_verification.get("verified") is not True:
            raise PaperCapitalStageError(
                "paper_capital_release_terminal_verification_failed"
            )
        return event_id or None

    def _commit_sell(
        self,
        *,
        request: StageRequest,
        order: Mapping[str, Any],
        seed: Mapping[str, Any],
        receipt_seed: Mapping[str, Any],
        fill_id: str,
        filled_quantity: int,
        filled_price: float,
        fee_cny: float,
        filled_at: str,
        status: str,
        source_sha256: str,
    ) -> str:
        original_quantity = int(seed.get("position_quantity") or 0)
        original_cost_basis = float(seed.get("position_cost_basis_cny") or 0.0)
        if original_quantity <= 0 or filled_quantity > original_quantity:
            raise PaperCapitalStageError("paper_execution_sell_position_invalid")
        gross_proceeds = round(filled_quantity * filled_price, 6)
        cost_basis_released = round(
            original_cost_basis * filled_quantity / original_quantity,
            6,
        )
        gross_realized = round(gross_proceeds - cost_basis_released, 6)
        net_credit = round(gross_proceeds - fee_cny, 6)
        lineage_sha256 = _sha256(
            {
                "run_id": request.run_id,
                "order_id": order["order_id"],
                "execution_seed": seed,
                "market_snapshot_sha256": source_sha256,
            }
        )
        head = self._account.ledger.snapshot()
        reference_id = (
            f"MCAPSELL:{request.bundle.context.authority_generation}:"
            f"{request.bundle.context.execution_lineage}:"
            f"{str(order['symbol']).strip().upper()}:{fill_id}"
        )
        commit_request = MarketCapitalAshareSellCommitRequest(
            market="ashare",
            reference_id=reference_id,
            risk_unit_key=str(order["symbol"]).strip().upper(),
            authority_id=request.bundle.context.authority_id,
            authority_generation=request.bundle.context.authority_generation,
            execution_lineage_id=request.bundle.context.execution_lineage,
            lineage_sha256=lineage_sha256,
            order_id=str(order["order_id"]),
            idempotency_key=_sha256(
                {
                    "stage_idempotency_key": request.idempotency_key,
                    "order_id": order["order_id"],
                    "fill_id": fill_id,
                }
            ),
            execution_fill_id=fill_id,
            fill_sequence=1,
            side="sell",
            status=status,
            terminal=True,
            actual_closed_quantity=filled_quantity,
            actual_fill_price=filled_price,
            actual_gross_proceeds_cny=gross_proceeds,
            actual_fee_cash_cny=fee_cny,
            actual_net_cash_credit_cny=net_credit,
            actual_gross_realized_pnl_cny=gross_realized,
            filled_at=filled_at,
            point_in_time_as_of=request.bundle.context.decision_as_of,
            source=(
                f"tradingagent:paper_execution:{request.run_id}:{order['order_id']}"
            ),
            source_sha256=source_sha256,
            receipt_sha256=_sha256(receipt_seed),
            local_position_sha256=_sha256(seed),
            expected_ledger_event_id=head.event_id,
            expected_ledger_checksum=head.event_checksum,
        )
        stable_payload = {
            "stage_idempotency_key": request.idempotency_key,
            "run_id": request.run_id,
            "input_bundle_sha256": request.input_bundle_sha256,
            "order_id": str(order["order_id"]),
            "fill_id": fill_id,
            "receipt_sha256": _sha256(receipt_seed),
            "source_sha256": source_sha256,
            "local_position_sha256": _sha256(seed),
            "receipt_seed": dict(receipt_seed),
        }
        outbox_id, stored_request = self._load_or_create_outbox_intent(
            operation="ashare_sell_commit",
            stable_payload=stable_payload,
            commit_request=asdict(commit_request),
        )
        try:
            replay_request = MarketCapitalAshareSellCommitRequest(**stored_request)
        except (TypeError, ValueError) as exc:
            raise PaperCapitalStageError(
                "paper_execution_outbox_request_invalid"
            ) from exc
        decision = self._account.ledger.commit_ashare_sell(replay_request)
        if not decision.committed or not decision.event_id:
            raise PaperCapitalStageError(
                f"paper_capital_sell_rejected:{decision.reason}"
            )
        self._write_outbox_settlement(
            outbox_id=outbox_id,
            settlement={
                "operation": "ashare_sell_commit",
                "pending_intent_sha256": _sha256(
                    {
                        "contract_id": "tradingagent.paper_execution_outbox_intent.v1",
                        "outbox_id": outbox_id,
                        "operation": "ashare_sell_commit",
                        "stable_payload": stable_payload,
                        "commit_request": stored_request,
                        "real_trading_enabled": False,
                    }
                ),
                "capital_commit_event_id": decision.event_id,
            },
        )
        return decision.event_id

    def execute(self, request: StageRequest) -> StageResult:
        if request.stage is not RunStage.ORDERS_SIMULATED:
            raise PaperCapitalStageError("execution_request_stage_invalid")
        approved = self.approved_order_map(request)
        permitted = tuple(request.permitted_order_ids)
        if len(permitted) != len(set(permitted)) or any(
            not isinstance(order_id, str)
            or not order_id
            or order_id != order_id.strip()
            for order_id in permitted
        ):
            raise PaperCapitalStageError("permitted_order_ids_duplicate")
        if set(permitted) != set(approved):
            raise PaperCapitalStageError("permitted_risk_order_set_mismatch")
        if set(permitted) != set(self._market_snapshots):
            raise PaperCapitalStageError("paper_market_snapshot_set_mismatch")
        preflight: dict[
            str,
            tuple[dict[str, Any], str, str, int, float, dict[str, Any]],
        ] = {}
        for permitted_order_id in permitted:
            order = approved[permitted_order_id]
            order_id, symbol, intent, quantity, reservation_price = self.validate_order(
                order,
                request=request,
            )
            snapshot = self.validated_market_snapshot(
                request=request,
                order_id=order_id,
                side="buy" if intent in {"open", "increase"} else "sell",
            )
            preflight[order_id] = (
                order,
                symbol,
                intent,
                quantity,
                reservation_price,
                snapshot,
            )
        receipts: list[dict[str, Any]] = []
        for order_id in permitted:
            (
                order,
                symbol,
                intent,
                quantity,
                reservation_price,
                snapshot,
            ) = preflight[order_id]
            market_sha256 = _sha256(snapshot)
            seed = self._execution_seed(
                request=request,
                order_id=order_id,
                symbol=symbol,
                market_sha256=market_sha256,
            )
            engine = SimExecutionEngine("ashare")
            position_quantity = int(seed.get("position_quantity") or 0)
            if position_quantity > 0:
                position_cost = float(seed.get("position_cost_basis_cny") or 0.0)
                position_entry_fee = float(seed.get("position_entry_fee_cny") or 0.0)
                engine.positions[symbol] = SimPosition(
                    symbol=symbol,
                    current_holdings=float(position_quantity),
                    avg_cost=round(
                        (position_cost + position_entry_fee) / position_quantity,
                        8,
                    ),
                    mark_price=reservation_price,
                )
            sim_order = SimOrder(
                symbol=symbol,
                side=str(order.get("side") or "").strip().lower(),
                quantity=quantity,
                limit_price=reservation_price,
                order_type=str(order.get("order_type") or "market"),
                time_in_force=str(order.get("time_in_force") or "day"),
                market="ashare",
                order_id=order_id,
                submitted_at=request.bundle.context.decision_as_of,
                metadata={"intent": intent, "paper_only": True},
            )
            execution_snapshot = dict(snapshot)
            if sim_order.side == "sell" and "sellable_qty" not in execution_snapshot:
                execution_snapshot["sellable_qty"] = order.get("sellable_quantity", 0)
            terminal_at = str(snapshot["execution_time"])
            execution_receipt_id = _sha256(
                {
                    "contract": "tradingagent.paper_execution_receipt.v1",
                    "run_id": request.run_id,
                    "order_id": order_id,
                    "market_snapshot_sha256": market_sha256,
                }
            )
            base_receipt: dict[str, Any] = {
                "order_id": order_id,
                "symbol": symbol,
                "intent": intent,
                "requested_quantity": quantity,
                "capital_authority_id": request.bundle.context.authority_id,
                "authority_generation": request.bundle.context.authority_generation,
                "execution_lineage": request.bundle.context.execution_lineage,
                "execution_receipt_id": execution_receipt_id,
                "market_evidence_receipt_id": str(snapshot["snapshot_id"]),
                "market_evidence_authority_sha256": snapshot[
                    "market_evidence_authority_sha256"
                ],
                "market_evidence_verification_sha256": _sha256(
                    snapshot["market_evidence_verification"]
                ),
                "market_snapshot_sha256": market_sha256,
                "market_session": str(snapshot["market_session"]),
                "market_available_at": str(snapshot["available_at"]),
                "market_data_through": str(snapshot["data_through"]),
                "market_execution_time": str(snapshot["execution_time"]),
                "execution_clock_sha256": self._execution_clock.identity_sha256,
                "terminal_at": terminal_at,
                "real_trading_enabled": False,
            }
            if intent in {"open", "increase"}:
                base_receipt.update(
                    {
                        field_name: order.get(field_name)
                        for field_name in sorted(_CAPITAL_RESERVATION_FIELDS)
                    }
                )
            recovered_receipt = self._recover_committed_outbox(
                request=request,
                order=order,
                order_id=order_id,
                market_sha256=market_sha256,
            )
            if recovered_receipt is not None:
                receipts.append(recovered_receipt)
                continue
            submit_authorization = _authorize_capital_effect(
                self._effect_guard,
                effect="sim_submit",
                request=request,
                order=order,
            )
            if not submit_authorization.allowed:
                release_receipt_id = self.release_unfilled(
                    request=request,
                    order=order,
                    reason=submit_authorization.reason,
                )
                receipts.append(
                    {
                        **base_receipt,
                        "status": "not_filled",
                        "filled_quantity": 0,
                        "residual_quantity": quantity,
                        "capital_commit_status": "not_applicable",
                        "capital_release_receipt_id": release_receipt_id,
                        "capital_release_status": (
                            "released" if release_receipt_id else "not_applicable"
                        ),
                        "execution_reason": submit_authorization.reason,
                    }
                )
                continue
            submit_checked_at = self._execution_clock_reading(
                order_id=order_id,
                effect="sim_submit",
            )
            try:
                self._validate_fresh_clock_reading(
                    request=request,
                    snapshot=snapshot,
                    effect="sim_submit",
                    current=submit_checked_at,
                )
            except PaperCapitalStageError as exc:
                reason = str(exc)
                if not reason.startswith("paper_market_"):
                    raise
                release_receipt_id = self.release_unfilled(
                    request=request,
                    order=order,
                    reason=reason,
                )
                receipts.append(
                    {
                        **base_receipt,
                        "sim_submit_checked_at": submit_checked_at.isoformat(),
                        "terminal_at": submit_checked_at.isoformat(),
                        "status": "not_filled",
                        "filled_quantity": 0,
                        "residual_quantity": quantity,
                        "capital_commit_status": "not_applicable",
                        "capital_release_receipt_id": release_receipt_id,
                        "capital_release_status": (
                            "released" if release_receipt_id else "not_applicable"
                        ),
                        "execution_reason": reason,
                    }
                )
                continue
            base_receipt["sim_submit_checked_at"] = submit_checked_at.isoformat()
            base_receipt["terminal_at"] = submit_checked_at.isoformat()
            record = engine.submit_order(sim_order, execution_snapshot)
            if record.fills:
                fill = record.fills[0]
                filled_quantity = int(round(record.filled_qty))
                if (
                    filled_quantity <= 0
                    or abs(record.filled_qty - filled_quantity) > 1e-9
                ):
                    raise PaperCapitalStageError(
                        "paper_execution_fractional_ashare_fill"
                    )
                status = "filled" if filled_quantity == quantity else "partial"
                fee_cny = round(float(record.fees.get("total") or 0.0), 6)
                reference_price = float(
                    snapshot.get("ask_price")
                    if sim_order.side == "buy"
                    else snapshot.get("bid_price") or reservation_price
                )
                fill_id = (
                    "SIMFILL-"
                    + _sha256(
                        {
                            "run_id": request.run_id,
                            "order_id": order_id,
                            "market_snapshot_sha256": market_sha256,
                            "filled_quantity": filled_quantity,
                            "filled_price": record.avg_fill_price,
                        }
                    )[:24]
                )
                # SimExecutionEngine assigns a UUID by default.  Replace it
                # with the frozen fill identity before hashing the local trade
                # so a crash/retry cannot create a conflicting capital commit.
                fill.fill_id = fill_id
                # The quote timestamp remains market evidence.  A simulated fill
                # is a side effect and therefore cannot predate its trusted
                # submit check.
                fill.fill_time = submit_checked_at.isoformat()
                filled_at = str(fill.fill_time)
                receipt_seed = {
                    **base_receipt,
                    "status": status,
                    "filled_quantity": filled_quantity,
                    "residual_quantity": quantity - filled_quantity,
                    "filled_price_cny": round(record.avg_fill_price, 8),
                    "fee_cny": fee_cny,
                    "slippage_cny": round(
                        abs(record.avg_fill_price - reference_price) * filled_quantity,
                        6,
                    ),
                    "filled_at": filled_at,
                    "simulated_fill_id": fill_id,
                }
                commit_authorization = _authorize_capital_effect(
                    self._effect_guard,
                    effect="capital_commit",
                    request=request,
                    order=order,
                )
                if not commit_authorization.allowed:
                    release_receipt_id = self.release_unfilled(
                        request=request,
                        order=order,
                        reason=commit_authorization.reason,
                    )
                    receipts.append(
                        {
                            **base_receipt,
                            "status": "not_filled",
                            "filled_quantity": 0,
                            "residual_quantity": quantity,
                            "capital_commit_status": "not_applicable",
                            "capital_release_receipt_id": release_receipt_id,
                            "capital_release_status": (
                                "released" if release_receipt_id else "not_applicable"
                            ),
                            "execution_reason": commit_authorization.reason,
                        }
                    )
                    continue
                commit_checked_at = self._execution_clock_reading(
                    order_id=order_id,
                    effect="capital_commit",
                )
                try:
                    self._validate_fresh_clock_reading(
                        request=request,
                        snapshot=snapshot,
                        effect="capital_commit",
                        current=commit_checked_at,
                        not_before=submit_checked_at,
                    )
                except PaperCapitalStageError as exc:
                    reason = str(exc)
                    if not reason.startswith("paper_market_"):
                        raise
                    release_receipt_id = self.release_unfilled(
                        request=request,
                        order=order,
                        reason=reason,
                    )
                    receipts.append(
                        {
                            **base_receipt,
                            "capital_commit_checked_at": commit_checked_at.isoformat(),
                            "terminal_at": (
                                submit_checked_at.isoformat()
                                if reason
                                in {
                                    "paper_market_clock_regressed_before_capital_commit",
                                    "paper_market_clock_trade_date_mismatch_before_capital_commit",
                                }
                                else max(
                                    submit_checked_at,
                                    commit_checked_at,
                                ).isoformat()
                            ),
                            "status": "not_filled",
                            "filled_quantity": 0,
                            "residual_quantity": quantity,
                            "capital_commit_status": "not_committed",
                            "capital_release_receipt_id": release_receipt_id,
                            "capital_release_status": (
                                "released" if release_receipt_id else "not_applicable"
                            ),
                            "execution_reason": reason,
                        }
                    )
                    continue
                final_commit_authorization = _authorize_capital_effect(
                    self._effect_guard,
                    effect="capital_commit",
                    request=request,
                    order=order,
                )
                if not final_commit_authorization.allowed:
                    release_receipt_id = self.release_unfilled(
                        request=request,
                        order=order,
                        reason=final_commit_authorization.reason,
                    )
                    receipts.append(
                        {
                            **base_receipt,
                            "status": "not_filled",
                            "filled_quantity": 0,
                            "residual_quantity": quantity,
                            "capital_commit_status": "not_applicable",
                            "capital_release_receipt_id": release_receipt_id,
                            "capital_release_status": (
                                "released" if release_receipt_id else "not_applicable"
                            ),
                            "execution_reason": final_commit_authorization.reason,
                        }
                    )
                    continue
                receipt_seed["capital_commit_checked_at"] = (
                    commit_checked_at.isoformat()
                )
                if sim_order.side == "buy":
                    capital_receipt_id = self._commit_buy(
                        request=request,
                        order=order,
                        receipt_seed=receipt_seed,
                        fill_id=fill_id,
                        filled_quantity=filled_quantity,
                        filled_price=round(record.avg_fill_price, 8),
                        fee_cny=fee_cny,
                        filled_at=filled_at,
                        status=status,
                        source_sha256=market_sha256,
                        local_trade_sha256=_sha256(record.as_dict()),
                    )
                else:
                    capital_receipt_id = self._commit_sell(
                        request=request,
                        order=order,
                        seed=seed,
                        receipt_seed=receipt_seed,
                        fill_id=fill_id,
                        filled_quantity=filled_quantity,
                        filled_price=round(record.avg_fill_price, 8),
                        fee_cny=fee_cny,
                        filled_at=filled_at,
                        status=status,
                        source_sha256=market_sha256,
                    )
                receipt = {
                    **receipt_seed,
                    "capital_commit_receipt_id": capital_receipt_id,
                    "capital_commit_status": "committed",
                }
                receipt["fill_fingerprint"] = _sha256(receipt)
            else:
                status = "rejected" if record.state == "rejected" else "not_filled"
                execution_reason = str(record.reason or f"paper_execution_{status}")
                release_receipt_id = self.release_unfilled(
                    request=request,
                    order=order,
                    reason=execution_reason,
                )
                receipt = {
                    **base_receipt,
                    "status": status,
                    "filled_quantity": 0,
                    "residual_quantity": quantity,
                    "capital_commit_status": "not_applicable",
                    "capital_release_receipt_id": release_receipt_id,
                    "capital_release_status": (
                        "released" if release_receipt_id else "not_applicable"
                    ),
                    "execution_reason": execution_reason,
                }
            receipts.append(receipt)
        return StageResult(
            payload={
                "execution_lineage": request.bundle.context.execution_lineage,
                "account_type": "simulated",
                "real_trading_enabled": False,
                "order_receipts": receipts,
                "unknown_order_ids": [],
            }
        )


class CapitalBackedReconcileStagePort:
    """Close the simulated day against the canonical capital ledger."""

    def __init__(
        self,
        *,
        account: PaperCapitalAccount,
        reconciled_at: str,
    ) -> None:
        try:
            parsed = datetime.fromisoformat(
                str(reconciled_at or "").replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise PaperCapitalStageError("reconciled_at_invalid") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise PaperCapitalStageError("reconciled_at_timezone_required")
        self._account = account
        self._reconciled_at = parsed.isoformat()
        self.identity = ComponentIdentity(
            stage=RunStage.RECONCILED,
            component_id="capital-backed-reconcile-stage",
            version="1",
            artifact_sha256=_sha256(
                {
                    "contract": "tradingagent.capital_backed_reconcile.v1",
                    "capital_account": account.identity_sha256,
                    "reconciled_at": self._reconciled_at,
                    "real_trading_enabled": False,
                }
            ),
        )

    def _validate_execution_receipts(
        self,
        *,
        request: StageRequest,
        execution_payload: Mapping[str, Any],
        order_receipts: list[Any],
        unknown_order_ids: list[Any],
    ) -> None:
        context = request.bundle.context
        if (
            execution_payload.get("execution_lineage") != context.execution_lineage
            or execution_payload.get("account_type") != "simulated"
            or execution_payload.get("real_trading_enabled") is not False
            or unknown_order_ids != []
        ):
            reason = (
                "execution_unknown_order_ids_nonempty"
                if unknown_order_ids
                else "execution_receipt_authority_invalid"
            )
            raise PaperCapitalStageError(reason)
        permitted = tuple(request.bundle.permitted_order_ids)
        if len(permitted) != len(set(permitted)):
            raise PaperCapitalStageError("execution_permitted_order_ids_duplicate")
        rows: list[dict[str, Any]] = []
        for raw_receipt in order_receipts:
            if not isinstance(raw_receipt, Mapping):
                raise PaperCapitalStageError("execution_receipt_row_invalid")
            rows.append(dict(raw_receipt))
        receipt_order_ids = tuple(receipt.get("order_id") for receipt in rows)
        if receipt_order_ids != permitted or any(
            not isinstance(order_id, str) or not order_id
            for order_id in receipt_order_ids
        ):
            raise PaperCapitalStageError("execution_receipt_order_set_invalid")
        unique_fields: dict[str, set[str]] = {
            "execution_receipt_id": set(),
            "simulated_fill_id": set(),
        }
        decision = _aware_instant(
            context.decision_as_of,
            reason="execution_decision_as_of_invalid",
        )
        reconciled = datetime.fromisoformat(self._reconciled_at)
        for receipt in rows:
            fingerprint = receipt.get("fill_fingerprint")
            if fingerprint is not None:
                _strict_sha256(
                    fingerprint,
                    reason="execution_fill_fingerprint_invalid",
                )
                fingerprint_payload = dict(receipt)
                fingerprint_payload.pop("fill_fingerprint", None)
                if _sha256(fingerprint_payload) != fingerprint:
                    raise PaperCapitalStageError("execution_fill_fingerprint_mismatch")
            symbol = receipt.get("symbol")
            intent = receipt.get("intent")
            status = receipt.get("status")
            requested = receipt.get("requested_quantity")
            filled = receipt.get("filled_quantity")
            residual = receipt.get("residual_quantity")
            if (
                not isinstance(symbol, str)
                or symbol != symbol.strip().upper()
                or not is_mainboard_tradable(symbol)
                or intent not in {"open", "increase", "reduce", "exit"}
                or status not in {"filled", "partial", "not_filled", "rejected"}
                or isinstance(requested, bool)
                or not isinstance(requested, int)
                or requested <= 0
                or isinstance(filled, bool)
                or not isinstance(filled, int)
                or filled < 0
                or isinstance(residual, bool)
                or not isinstance(residual, int)
                or residual < 0
                or filled + residual != requested
                or receipt.get("capital_authority_id") != context.authority_id
                or receipt.get("authority_generation") != context.authority_generation
                or receipt.get("execution_lineage") != context.execution_lineage
                or receipt.get("real_trading_enabled") is not False
            ):
                raise PaperCapitalStageError("execution_receipt_contract_invalid")
            terminal = _aware_instant(
                receipt.get("terminal_at"),
                reason="execution_terminal_at_invalid",
            )
            if (
                terminal < decision
                or terminal > reconciled
                or terminal.astimezone(_SHANGHAI).date().isoformat()
                != context.trade_date
            ):
                raise PaperCapitalStageError("execution_terminal_at_out_of_bounds")
            receipt_id = _strict_sha256(
                receipt.get("execution_receipt_id"),
                reason="execution_receipt_id_invalid",
            )
            market_snapshot_sha256 = _strict_sha256(
                receipt.get("market_snapshot_sha256"),
                reason="execution_market_snapshot_sha256_invalid",
            )
            for field_name in (
                "execution_clock_sha256",
                "market_evidence_authority_sha256",
                "market_evidence_verification_sha256",
            ):
                _strict_sha256(
                    receipt.get(field_name),
                    reason=f"execution_{field_name}_invalid",
                )
            market_execution = _aware_instant(
                receipt.get("market_execution_time"),
                reason="execution_market_execution_time_invalid",
            )
            submit_checked = (
                _aware_instant(
                    receipt["sim_submit_checked_at"],
                    reason="execution_sim_submit_checked_at_invalid",
                )
                if receipt.get("sim_submit_checked_at") is not None
                else None
            )
            commit_checked = (
                _aware_instant(
                    receipt["capital_commit_checked_at"],
                    reason="execution_capital_commit_checked_at_invalid",
                )
                if receipt.get("capital_commit_checked_at") is not None
                else None
            )
            market_receipt_id = receipt.get("market_evidence_receipt_id")
            expected_receipt_id = _sha256(
                {
                    "contract": "tradingagent.paper_execution_receipt.v1",
                    "run_id": request.run_id,
                    "order_id": receipt["order_id"],
                    "market_snapshot_sha256": market_snapshot_sha256,
                }
            )
            if (
                receipt_id != expected_receipt_id
                or not isinstance(market_receipt_id, str)
                or not market_receipt_id
            ):
                raise PaperCapitalStageError("execution_receipt_evidence_invalid")
            if receipt_id in unique_fields["execution_receipt_id"]:
                raise PaperCapitalStageError("execution_receipt_id_duplicate")
            unique_fields["execution_receipt_id"].add(receipt_id)
            if status in {"filled", "partial"}:
                if (
                    (status == "filled" and filled != requested)
                    or (status == "partial" and not (0 < filled < requested))
                    or receipt.get("capital_commit_status") != "committed"
                    or not isinstance(receipt.get("capital_commit_receipt_id"), str)
                    or not receipt["capital_commit_receipt_id"]
                    or fingerprint is None
                ):
                    raise PaperCapitalStageError("execution_fill_status_invalid")
                fill_id = receipt.get("simulated_fill_id")
                if (
                    not isinstance(fill_id, str)
                    or not fill_id
                    or fill_id in unique_fields["simulated_fill_id"]
                ):
                    raise PaperCapitalStageError("execution_fill_id_invalid")
                unique_fields["simulated_fill_id"].add(fill_id)
                filled_at = _aware_instant(
                    receipt.get("filled_at"),
                    reason="execution_filled_at_invalid",
                )
                if filled_at != terminal:
                    raise PaperCapitalStageError("execution_fill_time_mismatch")
                if (
                    submit_checked is None
                    or commit_checked is None
                    or not (
                        market_execution
                        <= submit_checked
                        <= filled_at
                        <= commit_checked
                        <= reconciled
                    )
                ):
                    raise PaperCapitalStageError("execution_effect_time_order_invalid")
                for field_name, minimum in (
                    ("filled_price_cny", 0.00000001),
                    ("fee_cny", 0.0),
                    ("slippage_cny", 0.0),
                ):
                    value = receipt.get(field_name)
                    if (
                        isinstance(value, bool)
                        or not isinstance(value, (int, float))
                        or not math.isfinite(float(value))
                        or float(value) < minimum
                    ):
                        raise PaperCapitalStageError(
                            f"execution_fill_{field_name}_invalid"
                        )
            else:
                commit_status = receipt.get("capital_commit_status")
                if (
                    filled != 0
                    or residual != requested
                    or commit_status not in {"not_applicable", "not_committed"}
                    or fingerprint is not None
                ):
                    raise PaperCapitalStageError("execution_unfilled_status_invalid")
                if submit_checked is not None and terminal < submit_checked:
                    raise PaperCapitalStageError("execution_effect_time_order_invalid")
                execution_reason = receipt.get("execution_reason")
                if (
                    submit_checked is not None
                    and market_execution > submit_checked
                    and not (
                        commit_status == "not_applicable"
                        and execution_reason
                        == "paper_market_snapshot_future_before_sim_submit"
                    )
                ):
                    raise PaperCapitalStageError("execution_effect_time_order_invalid")
                if commit_status == "not_committed":
                    if not is_reconcilable_not_committed_market_failure(
                        receipt,
                        expected_trade_date=context.trade_date,
                        reconciled_at=reconciled,
                    ):
                        raise PaperCapitalStageError(
                            "execution_uncommitted_reason_invalid"
                        )
                elif (
                    receipt.get("capital_commit_receipt_id") is not None
                    or receipt.get("simulated_fill_id") is not None
                    or receipt.get("filled_at") is not None
                    or commit_checked is not None
                ):
                    raise PaperCapitalStageError("execution_unfilled_status_invalid")
                release_status = receipt.get("capital_release_status")
                release_id = receipt.get("capital_release_receipt_id")
                if intent in {"open", "increase"}:
                    if (
                        release_status != "released"
                        or not isinstance(release_id, str)
                        or not release_id
                    ):
                        raise PaperCapitalStageError(
                            "execution_unfilled_release_invalid"
                        )
                    reservation_id = str(
                        receipt.get("market_capital_reservation_id") or ""
                    ).strip()
                    release_amount = receipt.get("market_reserved_cash_cny")
                    execution_reason = receipt.get("execution_reason")
                    if (
                        not reservation_id
                        or isinstance(release_amount, bool)
                        or not isinstance(release_amount, (int, float))
                        or float(release_amount) <= 0.0
                        or not isinstance(execution_reason, str)
                        or not execution_reason
                    ):
                        raise PaperCapitalStageError(
                            "execution_unfilled_release_event_invalid"
                        )
                    release_reference_id = (
                        f"TA-PAPER-RELEASE:{request.run_id}:"
                        f"{receipt['order_id']}:{execution_reason}"
                    )
                    release_verification = self._account.ledger.verify_release(
                        reservation_id=reservation_id,
                        amount_cny=float(release_amount),
                        reason=execution_reason,
                        reference_id=release_reference_id,
                        expected_event_id=release_id,
                        authority_id=context.authority_id,
                        authority_generation=context.authority_generation,
                        execution_lineage_id=context.execution_lineage,
                        risk_unit_key=symbol,
                        require_terminal=True,
                    )
                    if release_verification.get("verified") is not True:
                        raise PaperCapitalStageError(
                            "execution_unfilled_release_event_invalid"
                        )
                elif (
                    release_status not in {None, "not_applicable"}
                    or release_id is not None
                ):
                    raise PaperCapitalStageError("execution_unfilled_release_invalid")

    def execute(self, request: StageRequest) -> StageResult:
        if request.stage is not RunStage.RECONCILED:
            raise PaperCapitalStageError("reconcile_request_stage_invalid")
        try:
            execution_payload = request.bundle.receipt_for(
                RunStage.ORDERS_SIMULATED
            ).payload
        except (AttributeError, KeyError) as exc:
            raise PaperCapitalStageError("execution_receipt_missing") from exc
        order_receipts = execution_payload.get("order_receipts")
        unknown_order_ids = execution_payload.get("unknown_order_ids")
        if not isinstance(order_receipts, list) or not isinstance(
            unknown_order_ids, list
        ):
            raise PaperCapitalStageError("execution_receipt_contract_invalid")
        self._validate_execution_receipts(
            request=request,
            execution_payload=execution_payload,
            order_receipts=order_receipts,
            unknown_order_ids=unknown_order_ids,
        )
        proof = self._account.reconcile(
            request=request,
            phase="close",
            pit_timestamp=self._reconciled_at,
        )
        snapshot = self._account.ledger.snapshot()
        position_fingerprint = _sha256(
            {
                "authority_id": snapshot.authority_id,
                "authority_generation": snapshot.authority_generation,
                "execution_lineage_id": snapshot.execution_lineage_id,
                "cash_balance_cny": snapshot.cash_balance_cny,
                "positions_quantity_by_risk_unit": (
                    snapshot.positions_quantity_by_risk_unit
                ),
                "positions_cost_basis_cny_by_risk_unit": (
                    snapshot.positions_cost_basis_cny_by_risk_unit
                ),
                "positions_entry_fee_cny_by_risk_unit": (
                    snapshot.positions_entry_fee_cny_by_risk_unit
                ),
                "event_id": snapshot.event_id,
                "event_checksum": snapshot.event_checksum,
            }
        )
        return StageResult(
            payload={
                "status": "reconciled",
                "account_authority_valid": True,
                "position_authority_valid": not bool(
                    snapshot.unreconciled_fill_commit_ids
                ),
                "execution_lineage": request.bundle.context.execution_lineage,
                "capital_authority_id": request.bundle.context.authority_id,
                "authority_generation": (request.bundle.context.authority_generation),
                "source_run_id": request.run_id,
                "source_input_bundle_sha256": request.input_bundle_sha256,
                "reconciled_at": self._reconciled_at,
                "reconciliation_receipt_id": proof["event_id"],
                "capital_ledger_head_sha256": proof["lineage_sha256"],
                "position_fingerprint": position_fingerprint,
                "order_receipts_sha256": _sha256(order_receipts),
                "account_equity_cny": snapshot.equity_cny,
                "cash_cny": snapshot.cash_balance_cny,
                "unknown_order_ids": list(unknown_order_ids),
                "unreconciled_order_ids": list(snapshot.unreconciled_fill_commit_ids),
                "real_trading_enabled": False,
            }
        )


__all__ = [
    "CapitalBackedPreopenStagePort",
    "CapitalBackedReconcileStagePort",
    "CapitalBackedRiskStagePort",
    "CapitalBackedSimulationExecutionStagePort",
    "CapitalEffectAuthorization",
    "CapitalEffectGuard",
    "PaperCapitalAccount",
    "PaperCapitalStageError",
]
