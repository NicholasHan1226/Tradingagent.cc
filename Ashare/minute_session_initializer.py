"""Prepare one private A-share delayed-paper session from TradingDatas.

The initializer is intentionally small.  It reuses a previously reviewed
universe or an explicitly supplied reviewed universe artifact, proves that the
target day is open, reads the preceding session's closes through the fixed
TradingDatas catalog/query API, and writes the three immutable inputs consumed
by ``minute_auto_runner``.  It never creates capital, orders, fills, or a state
bundle.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sys
from typing import Any

from .minute_canary import (
    MinuteCanaryConfig,
    MinuteCanaryConfigurationError,
    load_minute_canary_config,
)
from .minute_paper_runner import load_minute_research_universe
from .minute_data import SHANGHAI
from .minute_auto_runner import session_bar_ends
from shared.data.evidence_gate import (
    DataEvidenceGate,
    DatasetEvidencePolicy,
    EvidenceAction,
)
from shared.data.sharedsignals_v1 import (
    CATALOG_PATH,
    HTTPTransport,
    QueryEnvelope,
    QueryRequest,
    SharedSignalsV1Client,
    SharedSignalsV1Config,
    SharedSignalsV1Error,
    parse_catalog_envelope,
)
from shared.data.tradingdatas_pagination import collect_query_pages
from shared.data.tradingdatas_transport import (
    RuntimeGateConfigurationError,
    build_runtime_transport,
)


CALENDAR_DATASET_ID = "cn.market.trade_calendar"
DAILY_DATASET_ID = "cn.equity.daily"
MINUTE_DATASET_ID = "cn.dataset.rt_min"
MAX_SESSION_QUERY_LIMIT = 500
MAX_QUERY_IN_VALUES = 100
MAX_DAILY_PAGES_PER_BATCH = 5
TRACKING_UNIVERSE_CONTRACT_ID = "tradingagent.trading_copilot_tracking_universe.v1"
SCALE500_COHORT_COUNT = 31
SCALE500_COHORT_SIZE = 103
SCALE500_REFERENCE_KEY = "scale500_reference"
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
TransportFactory = Callable[..., HTTPTransport]


class MinuteSessionInitializerError(ValueError):
    """Fail-closed minute-session preparation error."""


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
        raise MinuteSessionInitializerError(
            "minute_session_value_not_canonical"
        ) from exc


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _parse_scale500_bar_end(value: object, *, trading_date: date) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MinuteSessionInitializerError("minute_session_scale500_bar_end_invalid")
    raw = value.strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MinuteSessionInitializerError(
            "minute_session_scale500_bar_end_invalid"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=SHANGHAI)
    local = parsed.astimezone(SHANGHAI)
    if local.date() != trading_date or local not in session_bar_ends(trading_date):
        raise MinuteSessionInitializerError("minute_session_scale500_bar_end_invalid")
    return local.strftime("%Y-%m-%d %H:%M:%S")


def _load_scale500_receipt(path: Path) -> Mapping[str, Any]:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise MinuteSessionInitializerError(
            "minute_session_scale500_cohort_receipt_invalid"
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MinuteSessionInitializerError(
            "minute_session_scale500_cohort_receipt_invalid"
        ) from exc
    if not isinstance(raw, Mapping):
        raise MinuteSessionInitializerError(
            "minute_session_scale500_cohort_receipt_invalid"
        )
    return raw


def build_scale500_reference_envelope(
    *,
    universe_symbols: Sequence[str],
    universe_sha256: str,
    trading_date: date,
    target_bar_end: str,
    cohort_receipts: Sequence[Path | str],
) -> dict[str, Any]:
    """Bind five existing 100-symbol canary receipts to one exact 500 slot.

    This is an offline validator only.  It reads already-retained canary JSON,
    never calls TradingDatas, and stores only receipt/lineage/replay bindings
    in the manifest (not provider rows or payloads).
    """

    symbols = tuple(sorted(universe_symbols))
    if (
        len(symbols) != SCALE500_COHORT_COUNT * SCALE500_COHORT_SIZE
        or len(set(symbols)) != len(symbols)
        or not _SHA256_PATTERN.fullmatch(universe_sha256)
    ):
        raise MinuteSessionInitializerError("minute_session_scale500_universe_invalid")
    expected_bar_end = _parse_scale500_bar_end(
        target_bar_end, trading_date=trading_date
    )
    paths = tuple(Path(value) for value in cohort_receipts)
    if len(paths) != SCALE500_COHORT_COUNT:
        raise MinuteSessionInitializerError(
            "minute_session_scale500_cohort_count_invalid"
        )
    cohorts: list[dict[str, Any]] = []
    for index, path in enumerate(paths):
        receipt = _load_scale500_receipt(path)
        try:
            receipt_bar_end = _parse_scale500_bar_end(
                receipt.get("bar_end"), trading_date=trading_date
            )
        except MinuteSessionInitializerError as exc:
            raise MinuteSessionInitializerError(
                "minute_session_scale500_cohort_receipt_mismatch"
            ) from exc
        expected_symbols = list(
            symbols[index * SCALE500_COHORT_SIZE : (index + 1) * SCALE500_COHORT_SIZE]
        )
        if (
            receipt.get("status") != "pass"
            or receipt.get("authority_tier") != "observation_only"
            or receipt.get("evidence_use") != "delayed_paper"
            or receipt.get("real_trading_enabled") is not False
            or receipt_bar_end != expected_bar_end
            or receipt.get("row_count") != SCALE500_COHORT_SIZE
            or receipt.get("same_observation") is not True
            or receipt.get("lineage_complete") is not True
            or receipt.get("audit_rejections") != 0
            or receipt.get("reference_symbols") != expected_symbols
            or receipt.get("dataset_id") != MINUTE_DATASET_ID
        ):
            raise MinuteSessionInitializerError(
                "minute_session_scale500_cohort_receipt_mismatch"
            )
        receipt_ids = receipt.get("receipt_ids")
        lineages = receipt.get("source_lineage_sha256s")
        replay = receipt.get("replay")
        if (
            not isinstance(receipt.get("receipt_id"), str)
            or not receipt["receipt_id"].strip()
            or not isinstance(receipt_ids, list)
            or not receipt_ids
            or any(not isinstance(value, str) or not value.strip() for value in receipt_ids)
            or not isinstance(receipt.get("source_lineage_sha256"), str)
            or not _SHA256_PATTERN.fullmatch(receipt["source_lineage_sha256"])
            or not isinstance(lineages, list)
            or not lineages
            or any(
                not isinstance(value, str) or not _SHA256_PATTERN.fullmatch(value)
                for value in lineages
            )
            or not isinstance(receipt.get("snapshot_sha256"), str)
            or not _SHA256_PATTERN.fullmatch(receipt["snapshot_sha256"])
            or not isinstance(replay, Mapping)
            or replay.get("same_observation") is not True
            or any(
                not isinstance(replay.get(name), str)
                or not _SHA256_PATTERN.fullmatch(replay[name])
                for name in (
                    "pagination_trace_sha256",
                    "first_semantic_sha256",
                    "replay_semantic_sha256",
                )
            )
        ):
            raise MinuteSessionInitializerError(
                "minute_session_scale500_cohort_proof_invalid"
            )
        bars = receipt.get("bars")
        if not isinstance(bars, list) or len(bars) != SCALE500_COHORT_SIZE:
            raise MinuteSessionInitializerError(
                "minute_session_scale500_cohort_rows_invalid"
            )
        for bar in bars:
            try:
                bar_end = _parse_scale500_bar_end(
                    bar.get("bar_end") if isinstance(bar, Mapping) else None,
                    trading_date=trading_date,
                )
            except MinuteSessionInitializerError as exc:
                raise MinuteSessionInitializerError(
                    "minute_session_scale500_cohort_rows_invalid"
                ) from exc
            if (
                not isinstance(bar, Mapping)
                or bar_end != expected_bar_end
                or not isinstance(bar.get("symbol"), str)
                or not isinstance(bar.get("receipt_id"), str)
                or not bar["receipt_id"].strip()
                or not isinstance(bar.get("source_lineage_sha256"), str)
                or not _SHA256_PATTERN.fullmatch(bar["source_lineage_sha256"])
                or not isinstance(bar.get("envelope_proof_sha256"), str)
                or not _SHA256_PATTERN.fullmatch(bar["envelope_proof_sha256"])
            ):
                raise MinuteSessionInitializerError(
                    "minute_session_scale500_cohort_rows_invalid"
                )
        if [bar["symbol"] for bar in bars] != expected_symbols:
            raise MinuteSessionInitializerError(
                "minute_session_scale500_cohort_rows_invalid"
            )
        cohorts.append(
            {
                "cohort_id": f"scale500-{index:03d}",
                "symbols": expected_symbols,
                "row_count": SCALE500_COHORT_SIZE,
                "bar_end": expected_bar_end,
                "receipt_id": receipt["receipt_id"],
                "receipt_ids": list(receipt_ids),
                "source_lineage_sha256": receipt["source_lineage_sha256"],
                "source_lineage_sha256s": list(lineages),
                "snapshot_sha256": receipt["snapshot_sha256"],
                "replay": {
                    name: replay[name]
                    for name in (
                        "same_observation",
                        "pagination_trace_sha256",
                        "first_semantic_sha256",
                        "replay_semantic_sha256",
                    )
                },
            }
        )
    return {
        "target_bar_end": expected_bar_end,
        "universe_sha256": universe_sha256,
        "max_rows": SCALE500_COHORT_COUNT * SCALE500_COHORT_SIZE,
        "row_count": SCALE500_COHORT_COUNT * SCALE500_COHORT_SIZE,
        "cohort_count": SCALE500_COHORT_COUNT,
        "cohort_size": SCALE500_COHORT_SIZE,
        "cohorts": cohorts,
    }


def _runtime_failure_code(error: Exception) -> str:
    """Return a safe, actionable CLI failure category without exception details."""
    if isinstance(error, MinuteCanaryConfigurationError):
        return "minute_session_canary_configuration_invalid"
    if isinstance(error, RuntimeGateConfigurationError):
        return "minute_session_transport_configuration_invalid"
    if isinstance(error, (SharedSignalsV1Error, OSError)):
        return "minute_session_dependency_failed"
    return "minute_session_input_invalid"


def _metadata_payload(envelope: QueryEnvelope) -> dict[str, object]:
    return {
        "state": envelope.metadata.state,
        "degraded": envelope.metadata.degraded,
        "freshness": dict(envelope.metadata.freshness),
        "quality": dict(envelope.metadata.quality),
        "lineage": (
            None
            if envelope.metadata.lineage is None
            else dict(envelope.metadata.lineage)
        ),
        "receipt_id": envelope.metadata.receipt_id,
        "data_through": envelope.metadata.data_through,
        "observed_at": envelope.metadata.observed_at,
        "reasons": list(envelope.metadata.reasons),
    }


def _semantic_payload(envelope: QueryEnvelope) -> dict[str, object]:
    return {
        "api_version": envelope.api_version,
        "catalog_version": envelope.catalog_version,
        "dataset_id": envelope.dataset_id,
        "data": list(envelope.data),
        "next_cursor": envelope.next_cursor,
        "metadata": _metadata_payload(envelope),
    }


def _accept_ready(envelope: QueryEnvelope) -> None:
    gate = DataEvidenceGate(
        {envelope.dataset_id: DatasetEvidencePolicy(dataset_id=envelope.dataset_id)}
    )
    decision = gate.evaluate(envelope)
    if (
        decision.action is not EvidenceAction.ACCEPT
        or decision.eligible is not True
        or decision.weight != 1.0
    ):
        raise MinuteSessionInitializerError(
            f"minute_session_dataset_rejected:{envelope.dataset_id}"
        )


def _fetch_catalog(
    *,
    base_url: str,
    timeout_seconds: float,
    transport: HTTPTransport,
) -> tuple[str, tuple[dict[str, Any], ...]]:
    response = transport(
        method="GET",
        url=f"{base_url}{CATALOG_PATH}",
        headers={"Accept": "application/json"},
        json_body=None,
        timeout_seconds=timeout_seconds,
    )
    if response.status_code != 200:
        raise MinuteSessionInitializerError("minute_session_catalog_http_failed")
    try:
        catalog = parse_catalog_envelope(response.json_body)
    except SharedSignalsV1Error as exc:
        raise MinuteSessionInitializerError("minute_session_catalog_invalid") from exc
    return catalog.catalog_version, catalog.data


def _catalog_row(
    rows: tuple[dict[str, Any], ...],
    dataset_id: str,
) -> Mapping[str, Any]:
    matches = [row for row in rows if row.get("dataset_id") == dataset_id]
    if len(matches) != 1:
        raise MinuteSessionInitializerError(
            f"minute_session_catalog_dataset_invalid:{dataset_id}"
        )
    row = matches[0]
    availability = row.get("availability")
    if not isinstance(availability, Mapping) or availability.get(
        "activation_states"
    ) != ["active"]:
        raise MinuteSessionInitializerError(
            f"minute_session_catalog_dataset_inactive:{dataset_id}"
        )
    return row


def _query_contract(
    row: Mapping[str, Any],
    *,
    dataset_id: str,
    required_fields: tuple[str, ...],
    required_filters: tuple[str, ...],
    required_identity_fields: tuple[str, ...],
) -> tuple[int, tuple[str, ...], int]:
    schema_major = row.get("schema_major")
    if type(schema_major) is not int or schema_major <= 0:
        raise MinuteSessionInitializerError(
            f"minute_session_schema_invalid:{dataset_id}"
        )
    defaults = row.get("default_fields")
    if not isinstance(defaults, list) or not set(required_fields).issubset(defaults):
        raise MinuteSessionInitializerError(
            f"minute_session_fields_invalid:{dataset_id}"
        )
    raw_identity = row.get("identity_fields")
    if (
        not isinstance(raw_identity, list)
        or tuple(raw_identity) != required_identity_fields
        or not set(required_identity_fields).issubset(defaults)
    ):
        raise MinuteSessionInitializerError(
            f"minute_session_identity_invalid:{dataset_id}"
        )
    operators = row.get("filter_operators")
    if not isinstance(operators, Mapping):
        raise MinuteSessionInitializerError(
            f"minute_session_filters_invalid:{dataset_id}"
        )
    for field_name in required_filters:
        values = operators.get(field_name)
        if not isinstance(values, list) or "eq" not in values:
            raise MinuteSessionInitializerError(
                f"minute_session_filters_invalid:{dataset_id}"
            )
    limits = row.get("limits")
    page_size = limits.get("max_page_size") if isinstance(limits, Mapping) else None
    if type(page_size) is not int or page_size <= 0:
        raise MinuteSessionInitializerError(
            f"minute_session_limit_invalid:{dataset_id}"
        )
    return schema_major, required_fields, min(page_size, MAX_SESSION_QUERY_LIMIT)


def _scaled_minute_profile(
    template_profile: Mapping[str, Any],
    *,
    symbol_count: int,
    catalog_page_size: int,
) -> dict[str, Any]:
    if (
        isinstance(symbol_count, bool)
        or not isinstance(symbol_count, int)
        or symbol_count <= 0
        or isinstance(catalog_page_size, bool)
        or not isinstance(catalog_page_size, int)
        or catalog_page_size <= 0
    ):
        raise MinuteSessionInitializerError("minute_session_profile_scale_invalid")
    page_limit = min(symbol_count, catalog_page_size, 500)
    profile = dict(template_profile)
    profile["page_limit"] = page_limit
    profile["max_rows"] = symbol_count
    profile["max_pages"] = (symbol_count + page_limit - 1) // page_limit
    return profile


def _query_twice(
    *,
    client: SharedSignalsV1Client,
    request: QueryRequest,
    identity_fields: tuple[str, ...],
    max_pages: int,
    max_rows: int,
) -> QueryEnvelope:
    first = collect_query_pages(
        client=client,
        request=request,
        identity_fields=identity_fields,
        max_pages=max_pages,
        max_rows=max_rows,
    ).envelope
    second = collect_query_pages(
        client=client,
        request=request,
        identity_fields=identity_fields,
        max_pages=max_pages,
        max_rows=max_rows,
    ).envelope
    _accept_ready(first)
    _accept_ready(second)
    if _semantic_payload(first) != _semantic_payload(second):
        raise MinuteSessionInitializerError(
            f"minute_session_replay_mismatch:{request.dataset_id}"
        )
    return first


def _find_template_day(state_root: Path, target: date) -> Path:
    candidates = [
        path
        for path in state_root.iterdir()
        if path.is_dir()
        and len(path.name) == 8
        and path.name.isdigit()
        and path.name < target.strftime("%Y%m%d")
        and all(
            (path / name).is_file()
            for name in (
                "minute-manifest.json",
                "reference-facts.json",
                "universe.json",
            )
        )
    ]
    if not candidates:
        raise MinuteSessionInitializerError("minute_session_template_missing")
    return max(candidates, key=lambda path: path.name)


def _atomic_write(path: Path, payload: object) -> bytes:
    encoded = (_canonical_json(payload) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    os.chmod(path, 0o600)
    return encoded


def _publish_tracking_universe(
    *,
    output: Path,
    generated_at: datetime,
    universe: Any,
) -> int:
    """Atomically publish the session's named research universe for Copilot.

    This projection is deliberately not a quote, forecast, account, or trading
    authority.  It only lets the separately read-only Copilot surface discover
    the exact symbol/name set that this verified minute-session initializer used.
    """

    if not output.is_absolute() or output.is_symlink():
        raise MinuteSessionInitializerError("tracking_universe_output_invalid")
    parent = output.parent
    if not parent.is_dir() or parent.is_symlink():
        raise MinuteSessionInitializerError("tracking_universe_output_parent_invalid")
    if output.exists():
        metadata = output.stat(follow_symlinks=False)
        if not output.is_file() or metadata.st_nlink != 1:
            raise MinuteSessionInitializerError("tracking_universe_output_invalid")

    items = [
        {"symbol": symbol, "name": instrument.name}
        for symbol, instrument in sorted(universe.instruments.items())
        if not instrument.context_only
    ]
    if not items:
        raise MinuteSessionInitializerError("tracking_universe_empty")
    payload = {
        "contractId": TRACKING_UNIVERSE_CONTRACT_ID,
        "generatedAt": generated_at.isoformat(),
        "items": items,
    }
    encoded = (_canonical_json(payload) + "\n").encode("utf-8")
    temporary = output.with_name(
        f".{output.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    )
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
        os.chmod(output, 0o600)
        parent_descriptor = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return len(items)


def _publish_day(
    *,
    state_root: Path,
    target: date,
    manifest: Mapping[str, Any],
    references: list[dict[str, Any]],
    universe: list[dict[str, Any]],
) -> bool:
    target_root = state_root / target.strftime("%Y%m%d")
    payloads = {
        "minute-manifest.json": manifest,
        "reference-facts.json": references,
        "universe.json": universe,
    }
    if target_root.exists():
        if not target_root.is_dir() or (target_root / "state-bundle.json").exists():
            raise MinuteSessionInitializerError("minute_session_target_already_started")
        for name, payload in payloads.items():
            try:
                existing = (target_root / name).read_bytes()
            except OSError as exc:
                raise MinuteSessionInitializerError(
                    "minute_session_existing_inputs_invalid"
                ) from exc
            expected = (_canonical_json(payload) + "\n").encode("utf-8")
            if existing != expected:
                raise MinuteSessionInitializerError(
                    "minute_session_existing_inputs_conflict"
                )
        return True

    temporary = state_root / f".{target_root.name}.init.{os.getpid()}"
    try:
        temporary.mkdir(mode=0o700)
        os.chmod(temporary, 0o700)
        for name, payload in payloads.items():
            _atomic_write(temporary / name, payload)
        directory_fd = os.open(temporary, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        os.replace(temporary, target_root)
        root_fd = os.open(state_root, os.O_RDONLY)
        try:
            os.fsync(root_fd)
        finally:
            os.close(root_fd)
    except OSError as exc:
        raise MinuteSessionInitializerError("minute_session_publish_failed") from exc
    return False


def initialize_minute_session(
    *,
    state_root: Path | str,
    token_file: Path | str,
    now: datetime,
    base_url: str = "http://127.0.0.1:18082",
    access_policy_id: str = "tradingagent-read-v1",
    transport_id: str = "http-json-v1",
    timeout_seconds: float = 20.0,
    transport_factory: TransportFactory = build_runtime_transport,
    universe_source: Path | str | None = None,
    bootstrap_manifest: Path | str | None = None,
    tracking_universe_output: Path | str | None = None,
    target_bar_end: str | None = None,
    scale500_cohort_receipts: Sequence[Path | str] | None = None,
) -> dict[str, object]:
    """Create the current open day's minute inputs, or return a closed-day no-op."""

    if os.environ.get("REAL_TRADING_ENABLED", "false").strip().lower() != "false":
        raise MinuteSessionInitializerError("real_trading_must_remain_disabled")
    root = Path(state_root)
    token = Path(token_file)
    if (
        not root.is_absolute()
        or not token.is_absolute()
        or now.tzinfo is None
        or now.utcoffset() is None
    ):
        raise MinuteSessionInitializerError("minute_session_inputs_invalid")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    target = now.astimezone(SHANGHAI).date()
    bootstrap_path = None if bootstrap_manifest is None else Path(bootstrap_manifest)
    try:
        template_root = _find_template_day(root, target)
    except MinuteSessionInitializerError as exc:
        if exc.args != ("minute_session_template_missing",) or bootstrap_path is None:
            raise
        if (
            not bootstrap_path.is_absolute()
            or bootstrap_path.is_symlink()
            or not bootstrap_path.is_file()
        ):
            raise MinuteSessionInitializerError(
                "minute_session_bootstrap_manifest_invalid"
            ) from exc
        if universe_source is None:
            raise MinuteSessionInitializerError(
                "minute_session_bootstrap_universe_required"
            ) from exc
        template_root = None
        template_config = load_minute_canary_config(bootstrap_path)
    else:
        if bootstrap_path is not None:
            raise MinuteSessionInitializerError(
                "minute_session_bootstrap_not_permitted_after_initialization"
            )
        template_config = load_minute_canary_config(
            template_root / "minute-manifest.json"
        )
    if (
        template_config.base_url != base_url
        or template_config.access_policy_id != access_policy_id
        or template_config.transport_id != transport_id
        or template_config.dataset_id != MINUTE_DATASET_ID
    ):
        raise MinuteSessionInitializerError(
            "minute_session_template_authority_mismatch"
        )
    universe_path = (
        template_root / "universe.json"
        if universe_source is None and template_root is not None
        else Path(universe_source) if universe_source is not None else None
    )
    if universe_path is None:
        raise MinuteSessionInitializerError("minute_session_universe_source_invalid")
    if universe_source is not None and (
        not universe_path.is_absolute()
        or universe_path.is_symlink()
        or not universe_path.is_file()
    ):
        raise MinuteSessionInitializerError("minute_session_universe_source_invalid")
    universe_raw = json.loads(universe_path.read_text(encoding="utf-8"))
    if not isinstance(universe_raw, list) or not universe_raw:
        raise MinuteSessionInitializerError("minute_session_universe_invalid")
    universe = load_minute_research_universe(universe_path)
    allowed_observation_exclusions = {
        "risk_warning_excluded",
        "delisting_risk_excluded",
    }
    if any(
        (reason := instrument.eligibility_reason(trade_date=target)) is not None
        and reason not in allowed_observation_exclusions
        for instrument in universe.instruments.values()
    ):
        raise MinuteSessionInitializerError("minute_session_universe_ineligible")
    symbols = tuple(sorted(universe.instruments))

    scale500_reference = None
    if target_bar_end is not None or scale500_cohort_receipts is not None:
        if target_bar_end is None or scale500_cohort_receipts is None:
            raise MinuteSessionInitializerError(
                "minute_session_scale500_reference_incomplete"
            )
        if len(symbols) != SCALE500_COHORT_COUNT * SCALE500_COHORT_SIZE:
            raise MinuteSessionInitializerError(
                "minute_session_scale500_universe_invalid"
            )
        scale500_reference = build_scale500_reference_envelope(
            universe_symbols=symbols,
            universe_sha256=_sha256(universe_raw),
            trading_date=target,
            target_bar_end=target_bar_end,
            cohort_receipts=scale500_cohort_receipts,
        )

    transport = transport_factory(
        transport_id,
        token_file=token,
        base_url=base_url,
    )
    catalog_version, catalog_rows = _fetch_catalog(
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        transport=transport,
    )
    minute_row = _catalog_row(catalog_rows, MINUTE_DATASET_ID)
    calendar_row = _catalog_row(catalog_rows, CALENDAR_DATASET_ID)
    daily_row = _catalog_row(catalog_rows, DAILY_DATASET_ID)
    calendar_contract = _query_contract(
        calendar_row,
        dataset_id=CALENDAR_DATASET_ID,
        required_fields=("exchange", "cal_date", "is_open", "pretrade_date"),
        required_filters=("exchange", "cal_date"),
        required_identity_fields=("exchange", "cal_date"),
    )
    daily_contract = _query_contract(
        daily_row,
        dataset_id=DAILY_DATASET_ID,
        required_fields=("ts_code", "trade_date", "close"),
        required_filters=("trade_date",),
        required_identity_fields=("ts_code", "trade_date"),
    )
    daily_filter_operators = daily_row.get("filter_operators")
    if (
        not isinstance(daily_filter_operators, Mapping)
        or not isinstance(daily_filter_operators.get("ts_code"), list)
        or "in" not in daily_filter_operators["ts_code"]
    ):
        raise MinuteSessionInitializerError(
            f"minute_session_filters_invalid:{DAILY_DATASET_ID}"
        )
    minute_schema = minute_row.get("schema_major")
    if type(minute_schema) is not int or minute_schema <= 0:
        raise MinuteSessionInitializerError("minute_session_minute_schema_invalid")
    minute_limits = minute_row.get("limits")
    minute_page_size = (
        minute_limits.get("max_page_size")
        if isinstance(minute_limits, Mapping)
        else None
    )
    if type(minute_page_size) is not int or minute_page_size <= 0:
        raise MinuteSessionInitializerError("minute_session_minute_limit_invalid")
    scaled_profile = _scaled_minute_profile(
        template_config.profile,
        symbol_count=len(symbols),
        catalog_page_size=minute_page_size,
    )

    client = SharedSignalsV1Client(
        SharedSignalsV1Config(
            base_url=base_url,
            expected_catalog_version=template_config.expected_catalog_version,
            dataset_ids=frozenset(
                {CALENDAR_DATASET_ID, DAILY_DATASET_ID, MINUTE_DATASET_ID}
            ),
            access_policy_id=access_policy_id,
            catalog_version_policy="evidence_only",
            timeout_seconds=timeout_seconds,
            max_limit=MAX_SESSION_QUERY_LIMIT,
            cache_ttl_seconds=0,
        ),
        transport=transport,
    )
    # Rebuild the minute profile against the just-read catalog before publishing.
    current_manifest = MinuteCanaryConfig(
        base_url=base_url,
        expected_catalog_version=template_config.expected_catalog_version,
        dataset_id=MINUTE_DATASET_ID,
        access_policy_id=access_policy_id,
        transport_id=transport_id,
        timeout_seconds=timeout_seconds,
        filters={},
        profile=scaled_profile,
    )
    profile = current_manifest.build_profile(client, require_declared_bindings=False)
    if profile.schema_major != minute_schema:
        raise MinuteSessionInitializerError("minute_session_minute_schema_drift")

    compact_target = target.strftime("%Y%m%d")
    calendar = _query_twice(
        client=client,
        request=QueryRequest(
            dataset_id=CALENDAR_DATASET_ID,
            schema_major=calendar_contract[0],
            fields=calendar_contract[1],
            filters={
                "exchange": {"eq": "SSE"},
                "cal_date": {"eq": compact_target},
            },
            limit=calendar_contract[2],
        ),
        identity_fields=("exchange", "cal_date"),
        max_pages=1,
        max_rows=4,
    )
    if len(calendar.data) != 1:
        raise MinuteSessionInitializerError("minute_session_calendar_invalid")
    calendar_row_data = calendar.data[0]
    if str(calendar_row_data.get("is_open")) != "1":
        return {
            "status": "noop",
            "reason": "market_closed",
            "trading_date": target.isoformat(),
            "real_trading_enabled": False,
        }
    pretrade = calendar_row_data.get("pretrade_date")
    if not isinstance(pretrade, str) or len(pretrade.replace("-", "")) < 8:
        raise MinuteSessionInitializerError("minute_session_pretrade_invalid")
    compact_pretrade = pretrade.replace("-", "")[:8]
    try:
        previous_session = datetime.strptime(compact_pretrade, "%Y%m%d").date()
    except ValueError as exc:
        raise MinuteSessionInitializerError("minute_session_pretrade_invalid") from exc
    if previous_session >= target:
        raise MinuteSessionInitializerError("minute_session_pretrade_invalid")

    by_symbol: dict[str, Mapping[str, Any]] = {}
    metadata_by_symbol: dict[str, dict[str, object]] = {}
    daily_batch_size = min(
        len(symbols),
        daily_contract[2],
        MAX_QUERY_IN_VALUES,
    )
    for offset in range(0, len(symbols), daily_batch_size):
        symbol_batch = symbols[offset : offset + daily_batch_size]
        batch_max_pages = min(len(symbol_batch), MAX_DAILY_PAGES_PER_BATCH)
        daily = _query_twice(
            client=client,
            request=QueryRequest(
                dataset_id=DAILY_DATASET_ID,
                schema_major=daily_contract[0],
                fields=daily_contract[1],
                filters={
                    "trade_date": {"eq": compact_pretrade},
                    "ts_code": {"in": list(symbol_batch)},
                },
                limit=len(symbol_batch),
            ),
            identity_fields=("ts_code", "trade_date"),
            max_pages=batch_max_pages,
            max_rows=len(symbol_batch),
        )
        batch_metadata = _metadata_payload(daily)
        batch_rows: dict[str, Mapping[str, Any]] = {}
        for row in daily.data:
            symbol = row.get("ts_code")
            if (
                not isinstance(symbol, str)
                or symbol not in symbol_batch
                or row.get("trade_date") != compact_pretrade
            ):
                raise MinuteSessionInitializerError(
                    "minute_session_daily_identity_mismatch"
                )
            if symbol in batch_rows or symbol in by_symbol:
                raise MinuteSessionInitializerError("minute_session_daily_duplicate")
            batch_rows[symbol] = row
        if set(batch_rows) != set(symbol_batch):
            raise MinuteSessionInitializerError(
                "minute_session_daily_universe_incomplete"
            )
        for symbol, row in batch_rows.items():
            by_symbol[symbol] = row
            metadata_by_symbol[symbol] = batch_metadata
    if set(by_symbol) != set(symbols):
        raise MinuteSessionInitializerError("minute_session_daily_universe_incomplete")

    references: list[dict[str, Any]] = []
    for symbol in symbols:
        row = by_symbol[symbol]
        close = row.get("close")
        if isinstance(close, bool) or not isinstance(close, (int, float)) or close <= 0:
            raise MinuteSessionInitializerError("minute_session_previous_close_invalid")
        evidence_payload = {
            "schema": "tradingagent.ashare.minute-reference.v1",
            "symbol": symbol,
            "target_trading_date": target.isoformat(),
            "previous_session": previous_session.isoformat(),
            "daily_row": dict(row),
            "daily_envelope_metadata": metadata_by_symbol[symbol],
            "suspension_semantics": (
                "provisional_false_rechecked_by_completed_positive_volume_minute_bar"
            ),
            "execution_authority": False,
        }
        references.append(
            {
                "symbol": symbol,
                "trade_date": target.isoformat(),
                "previous_close_cny": float(close),
                "suspended": False,
                "evidence_sha256": _sha256(evidence_payload),
            }
        )

    bound_profile = {
        **scaled_profile,
        "dataset_contract_fingerprint": profile.dataset_contract_fingerprint,
        "consumer_profile_sha256": profile.consumer_profile_sha256,
    }
    manifest = {
        "base_url": base_url,
        "expected_catalog_version": template_config.expected_catalog_version,
        "observed_catalog_version": profile.observed_catalog_version,
        "catalog_version_drift": profile.catalog_version_drift,
        "dataset_id": MINUTE_DATASET_ID,
        "access_policy_id": access_policy_id,
        "transport_id": transport_id,
        "timeout_seconds": timeout_seconds,
        "filters": {},
        "profile": bound_profile,
        "universe_sha256": _sha256(universe_raw),
    }
    if scale500_reference is not None:
        manifest[SCALE500_REFERENCE_KEY] = scale500_reference
    reused = _publish_day(
        state_root=root,
        target=target,
        manifest=manifest,
        references=references,
        universe=[dict(row) for row in universe_raw],
    )
    tracking_universe_count = None
    if tracking_universe_output is not None:
        tracking_universe_count = _publish_tracking_universe(
            output=Path(tracking_universe_output),
            generated_at=now,
            universe=universe,
        )
    return {
        "status": "pass",
        "authority_tier": "non_production_fixture",
        "trading_date": target.isoformat(),
        "previous_session": previous_session.isoformat(),
        "expected_catalog_version": profile.expected_catalog_version,
        "observed_catalog_version": profile.observed_catalog_version,
        "catalog_version_drift": profile.catalog_version_drift,
        "dataset_id": MINUTE_DATASET_ID,
        "symbol_count": len(symbols),
        "profile_max_pages": bound_profile["max_pages"],
        "profile_max_rows": bound_profile["max_rows"],
        "profile_page_limit": bound_profile["page_limit"],
        "dataset_contract_fingerprint": profile.dataset_contract_fingerprint,
        "consumer_profile_sha256": profile.consumer_profile_sha256,
        "universe_sha256": _sha256(universe_raw),
        "bootstrap": template_root is None,
        "reused": reused,
        "state_bundle_created": not reused,
        "tracking_universe_published": tracking_universe_count is not None,
        "tracking_universe_symbol_count": tracking_universe_count,
        "target_bar_end": (
            None if scale500_reference is None else scale500_reference["target_bar_end"]
        ),
        "scale500_reference_sha256": (
            None if scale500_reference is None else _sha256(scale500_reference)
        ),
        "capital_authority": False,
        "execution_authority": False,
        "real_trading_enabled": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Prepare one private A-share delayed-paper minute session"
    )
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument(
        "--universe-source",
        type=Path,
        help="Optional absolute, reviewed universe artifact for a scale transition",
    )
    parser.add_argument(
        "--bootstrap-manifest",
        type=Path,
        help=(
            "One-time absolute reviewed minute manifest for an empty state root; "
            "requires --universe-source and is rejected after initialization"
        ),
    )
    parser.add_argument(
        "--tracking-universe-output",
        type=Path,
        help="Optional absolute TradingCopilot symbol/name projection output",
    )
    parser.add_argument(
        "--target-bar-end",
        help="Exact delayed-paper bar_end bound to five retained 100-symbol receipts",
    )
    parser.add_argument(
        "--scale500-cohort-receipt",
        action="append",
        type=Path,
        dest="scale500_cohort_receipts",
        help="One of exactly five existing 100-symbol canary receipts",
    )
    parser.add_argument("--now", help="Explicit aware ISO timestamp for tests")
    args = parser.parse_args(argv)
    try:
        configured_universe_source = args.universe_source
        if configured_universe_source is None:
            environment_source = os.environ.get(
                "ASHARE_MINUTE_UNIVERSE_SOURCE", ""
            ).strip()
            if environment_source:
                configured_universe_source = Path(environment_source)
        configured_tracking_universe_output = args.tracking_universe_output
        if configured_tracking_universe_output is None:
            environment_output = os.environ.get(
                "TRADING_COPILOT_TRACKING_UNIVERSE_PATH", ""
            ).strip()
            if environment_output:
                configured_tracking_universe_output = Path(environment_output)
        now = (
            datetime.now(tz=SHANGHAI)
            if args.now is None
            else datetime.fromisoformat(args.now)
        )
        result = initialize_minute_session(
            state_root=args.state_root,
            token_file=args.token_file,
            now=now,
            universe_source=configured_universe_source,
            bootstrap_manifest=args.bootstrap_manifest,
            tracking_universe_output=configured_tracking_universe_output,
            target_bar_end=args.target_bar_end,
            scale500_cohort_receipts=args.scale500_cohort_receipts,
        )
    except MinuteSessionInitializerError as exc:
        print(f"minute session initializer failed closed: {exc}", file=sys.stderr)
        return 2
    except (
        MinuteCanaryConfigurationError,
        RuntimeGateConfigurationError,
        SharedSignalsV1Error,
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
    ) as exc:
        print(
            "minute session initializer failed closed: "
            f"{_runtime_failure_code(exc)}",
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "MinuteSessionInitializerError",
    "build_scale500_reference_envelope",
    "initialize_minute_session",
    "main",
]
