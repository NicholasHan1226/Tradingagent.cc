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
from collections.abc import Callable, Mapping
from datetime import date, datetime
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any

from .minute_canary import (
    MinuteCanaryConfig,
    MinuteCanaryConfigurationError,
    load_minute_canary_config,
)
from .minute_paper_runner import load_minute_research_universe
from .minute_data import SHANGHAI
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
    template_root = _find_template_day(root, target)
    template_config = load_minute_canary_config(template_root / "minute-manifest.json")
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
        if universe_source is None
        else Path(universe_source)
    )
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
    )
    daily_contract = _query_contract(
        daily_row,
        dataset_id=DAILY_DATASET_ID,
        required_fields=("ts_code", "trade_date", "close"),
        required_filters=("trade_date",),
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
    reused = _publish_day(
        state_root=root,
        target=target,
        manifest=manifest,
        references=references,
        universe=[dict(row) for row in universe_raw],
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
        "reused": reused,
        "state_bundle_created": False,
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
        )
    except (
        MinuteSessionInitializerError,
        MinuteCanaryConfigurationError,
        RuntimeGateConfigurationError,
        SharedSignalsV1Error,
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
    ):
        print("minute session initializer failed closed", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "MinuteSessionInitializerError",
    "initialize_minute_session",
    "main",
]
