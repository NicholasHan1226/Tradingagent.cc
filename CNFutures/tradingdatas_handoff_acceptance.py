"""Offline acceptance of an injected TradingDatas M handoff projection.

The module intentionally has no HTTP client, endpoint configuration, database
access, scheduler, runner, or fill path.  A caller supplies the catalog/query
responses it read through TradingDatas V1, and this function emits only a
non-authoritative observation, hold, or risk-reject record.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import math
import re
from typing import Any, Mapping
from zoneinfo import ZoneInfo


PROFILE_ID = "cn-futures-m-5min-handoff-v1"
_ROLES = ("contract_master", "bars_5min", "calendar_session")
_M_SYMBOL = re.compile(r"^M\d{3,4}\.DCE$")
_SHANGHAI = ZoneInfo("Asia/Shanghai")


class HandoffAcceptanceError(ValueError):
    """Raised internally for an incomplete or unsafe injected handoff."""


class _RiskReject(HandoffAcceptanceError):
    """A valid response shape whose contract spec is not safe to use."""


def evaluate_handoff_fixture(fixture: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one injected M handoff and return observation/hold/risk-reject.

    A passing result proves only that the supplied projection is complete enough
    for future read-only observation.  It never enables delayed paper, a fill,
    a timer, or execution authority.
    """

    try:
        _validate_fixture_markers(fixture)
        decision_time = _timestamp(fixture.get("decision_time"), "decision_time")
        profile = _profile(_mapping(fixture, "profile"))
        catalog = _catalog(_mapping(fixture, "catalog"), profile)
        queries = _mapping(fixture, "queries")
        contract_query = _query(
            queries, "contract_master", profile, catalog, decision_time
        )
        calendar_query = _query(
            queries, "calendar_session", profile, catalog, decision_time
        )
        bars_query = _query(queries, "bars_5min", profile, catalog, decision_time)
        contract = _contract(contract_query["data"], contract_query["observed_at"])
        calendar = _calendar(
            calendar_query["data"], contract["symbol"], calendar_query["observed_at"]
        )
        if contract["tradeable_on"] != calendar["trade_date"]:
            raise HandoffAcceptanceError("contract_tradeability_trade_date_mismatch")
        bars = _bars(
            bars_query["data"],
            contract["symbol"],
            calendar,
            bars_query["data_through"],
            bars_query["observed_at"],
            decision_time,
        )
        lineage = _sha256(
            {
                "profile": profile,
                "catalog": catalog,
                "contract": contract,
                "calendar": calendar,
                "bars": bars,
                "query_identities": {
                    role: query["query_identity"]
                    for role, query in {
                        "contract_master": contract_query,
                        "calendar_session": calendar_query,
                        "bars_5min": bars_query,
                    }.items()
                },
                "query_receipt_watermarks": {
                    role: query["receipt_watermark"]
                    for role, query in {
                        "contract_master": contract_query,
                        "calendar_session": calendar_query,
                        "bars_5min": bars_query,
                    }.items()
                },
                "decision_time": decision_time.isoformat(),
            }
        )
        return _result(
            disposition="observation",
            reason="m_handoff_evidence_accepted_for_observation_only",
            profile_id=profile["profile_id"],
            lineage=lineage,
            evidence={
                "catalog_version": catalog["catalog_version"],
                "dataset_ids": {
                    role: profile["roles"][role]["dataset_id"] for role in _ROLES
                },
                "query_receipt_watermarks": {
                    role: query["receipt_watermark"]
                    for role, query in {
                        "contract_master": contract_query,
                        "calendar_session": calendar_query,
                        "bars_5min": bars_query,
                    }.items()
                },
                "query_identities": {
                    role: query["query_identity"]
                    for role, query in {
                        "contract_master": contract_query,
                        "calendar_session": calendar_query,
                        "bars_5min": bars_query,
                    }.items()
                },
                "availability_source": "query_envelope.metadata.observed_at",
                "symbol": contract["symbol"],
                "trade_date": calendar["trade_date"],
                "session_id": calendar["session_id"],
                "bar_ends": [bar["bar_time"] for bar in bars],
            },
        )
    except _RiskReject as exc:
        return _result("risk_reject", str(exc))
    except HandoffAcceptanceError as exc:
        return _result("hold", str(exc))


def _validate_fixture_markers(fixture: Mapping[str, Any]) -> None:
    if fixture.get("fixture_only") is not True:
        raise HandoffAcceptanceError("fixture_only_required")
    for key in (
        "real_trading_enabled",
        "network_enabled",
        "runner_enabled",
        "timer_enabled",
        "delayed_paper_enabled",
        "simulated_fill_enabled",
        "live_broker",
    ):
        if fixture.get(key) not in (None, False):
            raise HandoffAcceptanceError(f"forbidden_fixture_marker:{key}")


def _profile(raw: Mapping[str, Any]) -> dict[str, Any]:
    if _text(raw.get("profile_id"), "profile.profile_id") != PROFILE_ID:
        raise HandoffAcceptanceError("unexpected_profile_id")
    roles = _mapping(raw, "roles")
    if set(roles) != set(_ROLES):
        raise HandoffAcceptanceError("profile_roles_mismatch")
    projected: dict[str, dict[str, Any]] = {}
    for role in _ROLES:
        item = _mapping(roles, role)
        projected[role] = {
            "dataset_id": _text(item.get("dataset_id"), f"profile.{role}.dataset_id"),
            "schema_major": _whole_positive(
                item.get("schema_major"), f"profile.{role}.schema_major"
            ),
        }
    return {"profile_id": PROFILE_ID, "roles": projected}


def _catalog(raw: Mapping[str, Any], profile: Mapping[str, Any]) -> dict[str, Any]:
    if raw.get("route") != "GET /v1/catalog":
        raise HandoffAcceptanceError("catalog_route_required")
    if raw.get("api_version") != "v1":
        raise HandoffAcceptanceError("catalog_api_version_required")
    catalog_version = _text(raw.get("catalog_version"), "catalog.catalog_version")
    datasets = raw.get("datasets")
    if not isinstance(datasets, list):
        raise HandoffAcceptanceError("catalog.datasets_required")
    indexed: dict[str, dict[str, Any]] = {}
    for item in datasets:
        if not isinstance(item, Mapping):
            raise HandoffAcceptanceError("catalog_dataset_mapping_required")
        dataset_id = _text(item.get("dataset_id"), "catalog.dataset_id")
        if dataset_id in indexed:
            raise HandoffAcceptanceError("duplicate_catalog_dataset_id")
        indexed[dataset_id] = {
            "schema_major": _whole_positive(
                item.get("schema_major"), "catalog.schema_major"
            ),
            "state": _text(item.get("state"), "catalog.state"),
            "degraded": item.get("degraded"),
        }
    roles = _mapping(profile, "roles")
    for role in _ROLES:
        expected = _mapping(roles, role)
        item = indexed.get(str(expected["dataset_id"]))
        if item is None:
            raise HandoffAcceptanceError(f"catalog_missing_dataset:{role}")
        if item["schema_major"] != expected["schema_major"]:
            raise HandoffAcceptanceError(f"catalog_schema_mismatch:{role}")
        if item["state"] != "ready" or item["degraded"] is not False:
            raise HandoffAcceptanceError(f"catalog_not_ready:{role}")
    return {"catalog_version": catalog_version, "datasets": indexed}


def _query(
    queries: Mapping[str, Any],
    role: str,
    profile: Mapping[str, Any],
    catalog: Mapping[str, Any],
    decision_time: datetime,
) -> dict[str, Any]:
    raw = _mapping(queries, role)
    expected = _mapping(_mapping(profile, "roles"), role)
    if raw.get("route") != "POST /v1/query":
        raise HandoffAcceptanceError(f"query_route_required:{role}")
    if raw.get("api_version") != "v1":
        raise HandoffAcceptanceError(f"query_api_version_required:{role}")
    if raw.get("dataset_id") != expected["dataset_id"]:
        raise HandoffAcceptanceError(f"query_dataset_mismatch:{role}")
    if (
        _whole_positive(raw.get("schema_major"), f"query.schema_major:{role}")
        != expected["schema_major"]
    ):
        raise HandoffAcceptanceError(f"query_schema_mismatch:{role}")
    if raw.get("catalog_version") != catalog["catalog_version"]:
        raise HandoffAcceptanceError(f"query_catalog_version_mismatch:{role}")
    data = raw.get("data")
    if not isinstance(data, list):
        raise HandoffAcceptanceError(f"query_data_required:{role}")
    if raw.get("next_cursor") is not None:
        raise HandoffAcceptanceError(f"query_page_not_complete:{role}")
    query_identity = _query_identity(raw, role)
    metadata = _mapping(raw, "metadata")
    lineage = _mapping(metadata, "lineage")
    if (
        metadata.get("state") != "ready"
        or metadata.get("degraded") is not False
        or _mapping(metadata, "freshness").get("state") != "fresh"
        or _mapping(metadata, "freshness").get("stale") is not False
        or _mapping(metadata, "quality").get("state") != "valid"
        or _mapping(metadata, "quality").get("valid") is not True
        or lineage.get("complete") is not True
        or lineage.get("provider_neutral") is not True
    ):
        raise HandoffAcceptanceError(f"query_evidence_not_eligible:{role}")
    data_through = _timestamp(metadata.get("data_through"), f"{role}.data_through")
    observed_at = _timestamp(metadata.get("observed_at"), f"{role}.observed_at")
    if not (data_through <= observed_at <= decision_time):
        raise HandoffAcceptanceError(f"query_pit_order_invalid:{role}")
    receipt_id = _text(metadata.get("receipt_id"), f"{role}.receipt_id")
    return {
        "data": data,
        "data_through": data_through,
        "observed_at": observed_at,
        "query_identity": query_identity,
        "receipt_watermark": {
            "receipt_id": receipt_id,
            "data_through": data_through.isoformat(),
            "observed_at": observed_at.isoformat(),
            "lineage_sha256": _sha256(lineage),
        },
    }


def _contract(rows: list[Any], observed_at: datetime) -> dict[str, Any]:
    if len(rows) != 1 or not isinstance(rows[0], Mapping):
        raise HandoffAcceptanceError("contract_master_requires_one_explicit_m_contract")
    row = rows[0]
    symbol = _text(row.get("symbol"), "contract.symbol").upper()
    if not _M_SYMBOL.fullmatch(symbol):
        raise HandoffAcceptanceError("contract_m_dce_symbol_required")
    if _text(row.get("product"), "contract.product").lower() != "m":
        raise HandoffAcceptanceError("contract_product_m_required")
    if _text(row.get("exchange"), "contract.exchange").upper() != "DCE":
        raise HandoffAcceptanceError("contract_exchange_dce_required")
    tradeability = _mapping(row, "tradeability")
    if tradeability.get("state") != "tradeable":
        raise HandoffAcceptanceError("contract_tradeability_required")
    if _positive_or_none(row.get("multiplier")) is None:
        raise _RiskReject("contract_multiplier_missing_or_invalid")
    if _positive_or_none(row.get("tick_size")) is None:
        raise _RiskReject("contract_tick_size_missing_or_invalid")
    if _positive_or_none(row.get("price_limit")) is None:
        raise _RiskReject("contract_price_limit_missing_or_invalid")
    return {
        "symbol": symbol,
        "multiplier": _positive_or_none(row["multiplier"]),
        "tick_size": _positive_or_none(row["tick_size"]),
        "price_limit": _positive_or_none(row["price_limit"]),
        "tradeable_on": _trade_date(tradeability.get("trade_date")),
        "observed_at": observed_at.isoformat(),
    }


def _calendar(rows: list[Any], symbol: str, observed_at: datetime) -> dict[str, Any]:
    if len(rows) != 1 or not isinstance(rows[0], Mapping):
        raise HandoffAcceptanceError("calendar_requires_one_session_row")
    row = rows[0]
    if _text(row.get("symbol"), "calendar.symbol").upper() != symbol:
        raise HandoffAcceptanceError("calendar_symbol_mismatch")
    trade_date = _trade_date(row.get("trade_date"))
    if row.get("calendar_eligible") is not True:
        raise HandoffAcceptanceError("calendar_not_eligible")
    if _text(row.get("session_kind"), "calendar.session_kind") != "day":
        raise HandoffAcceptanceError("calendar_day_session_required")
    windows = _session_windows(row, trade_date)
    return {
        "trade_date": trade_date,
        "session_id": _text(row.get("session_id"), "calendar.session_id"),
        "session_windows": windows,
        "observed_at": observed_at.isoformat(),
    }


def _bars(
    rows: list[Any],
    symbol: str,
    calendar: Mapping[str, Any],
    data_through: datetime,
    observed_at: datetime,
    decision_time: datetime,
) -> list[dict[str, Any]]:
    if len(rows) != 2:
        raise HandoffAcceptanceError("two_adjacent_5min_bars_required")
    windows = list(calendar["session_windows"])
    normalized: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise HandoffAcceptanceError("bar_mapping_required")
        if _text(row.get("symbol"), f"bars[{index}].symbol").upper() != symbol:
            raise HandoffAcceptanceError("bar_symbol_mismatch")
        if _trade_date(row.get("trade_date")) != calendar["trade_date"]:
            raise HandoffAcceptanceError("bar_trade_date_mismatch")
        if (
            _text(row.get("session_id"), f"bars[{index}].session_id")
            != calendar["session_id"]
        ):
            raise HandoffAcceptanceError("bar_session_mismatch")
        if row.get("completed") is not True:
            raise HandoffAcceptanceError("completed_5min_bar_required")
        bar_time = _timestamp(row.get("bar_time"), f"bars[{index}].bar_time")
        local_bar_time = bar_time.astimezone(_SHANGHAI)
        if (
            local_bar_time.second != 0
            or local_bar_time.microsecond != 0
            or local_bar_time.minute % 5
        ):
            raise HandoffAcceptanceError("bar_not_on_5min_grid")
        if not (bar_time <= data_through <= observed_at <= decision_time):
            raise HandoffAcceptanceError("bar_pit_order_invalid")
        if not _in_session_windows(bar_time, windows):
            raise HandoffAcceptanceError("bar_outside_calendar_session")
        if local_bar_time.strftime("%Y%m%d") != calendar["trade_date"]:
            raise HandoffAcceptanceError("bar_shanghai_trade_date_mismatch")
        open_ = _positive(row.get("open"), f"bars[{index}].open")
        high = _positive(row.get("high"), f"bars[{index}].high")
        low = _positive(row.get("low"), f"bars[{index}].low")
        close = _positive(row.get("close"), f"bars[{index}].close")
        volume = _nonnegative(row.get("volume"), f"bars[{index}].volume")
        if not low <= min(open_, close) <= max(open_, close) <= high:
            raise HandoffAcceptanceError("bar_ohlc_invalid")
        normalized.append(
            {
                "bar_time": bar_time.isoformat(),
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
            }
        )
    first = _timestamp(normalized[0]["bar_time"], "bars[0].bar_time")
    second = _timestamp(normalized[1]["bar_time"], "bars[1].bar_time")
    if (second - first).total_seconds() != 300:
        raise HandoffAcceptanceError("bars_not_adjacent_5min")
    return normalized


def _result(
    disposition: str,
    reason: str,
    profile_id: str | None = None,
    lineage: str | None = None,
    evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "mode": "fixture_catalog_query_handoff_acceptance",
        "fixture_only": True,
        "real_trading_enabled": False,
        "execution_eligible": False,
        "execution_authority": False,
        "delayed_paper_eligible": False,
        "learning_evidence_eligible": False,
        "durable": False,
        "capital_commit_id": None,
        "outbox_id": None,
        "profile_id": profile_id or PROFILE_ID,
        "disposition": disposition,
        "reason": reason,
        "handoff_lineage_sha256": lineage,
        "evidence": dict(evidence or {}),
    }


def _mapping(raw: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = raw.get(key)
    if not isinstance(value, Mapping):
        raise HandoffAcceptanceError(f"mapping_required:{key}")
    return value


def _query_identity(raw: Mapping[str, Any], role: str) -> dict[str, Any]:
    identity = _mapping(raw, "query_identity")
    if set(identity) != {"filters", "sort", "cursor"}:
        raise HandoffAcceptanceError(f"query_identity_shape_invalid:{role}")
    filters = _mapping(identity, "filters")
    if not filters:
        raise HandoffAcceptanceError(f"query_identity_filters_required:{role}")
    sort = identity.get("sort")
    if not isinstance(sort, list) or not sort:
        raise HandoffAcceptanceError(f"query_identity_sort_required:{role}")
    normalized_sort: list[dict[str, str]] = []
    for index, item in enumerate(sort):
        if not isinstance(item, Mapping) or set(item) != {"field", "direction"}:
            raise HandoffAcceptanceError(f"query_identity_sort_invalid:{role}:{index}")
        direction = _text(
            item.get("direction"), f"query_identity.sort.direction:{role}"
        )
        if direction not in {"asc", "desc"}:
            raise HandoffAcceptanceError(f"query_identity_sort_invalid:{role}:{index}")
        normalized_sort.append(
            {
                "field": _text(item.get("field"), f"query_identity.sort.field:{role}"),
                "direction": direction,
            }
        )
    if identity.get("cursor") is not None:
        raise HandoffAcceptanceError(f"query_identity_cursor_required_null:{role}")
    return {
        "filters": _canonical_json_projection(
            filters, f"query_identity.filters:{role}"
        ),
        "sort": normalized_sort,
        "cursor": None,
    }


def _session_windows(raw: Mapping[str, Any], trade_date: str) -> list[dict[str, str]]:
    source = raw.get("session_windows")
    if not isinstance(source, list) or not source:
        raise HandoffAcceptanceError("calendar_session_windows_required")
    windows: list[dict[str, str]] = []
    previous_end: datetime | None = None
    for index, item in enumerate(source):
        if not isinstance(item, Mapping) or set(item) != {"start", "end"}:
            raise HandoffAcceptanceError("calendar_session_window_invalid")
        start = _timestamp(
            item.get("start"), f"calendar.session_windows[{index}].start"
        )
        end = _timestamp(item.get("end"), f"calendar.session_windows[{index}].end")
        if (
            start >= end
            or start.astimezone(_SHANGHAI).strftime("%Y%m%d") != trade_date
            or end.astimezone(_SHANGHAI).strftime("%Y%m%d") != trade_date
            or (previous_end is not None and start <= previous_end)
        ):
            raise HandoffAcceptanceError("calendar_session_window_invalid")
        windows.append({"start": start.isoformat(), "end": end.isoformat()})
        previous_end = end
    return windows


def _in_session_windows(value: datetime, windows: list[Any]) -> bool:
    for item in windows:
        if not isinstance(item, Mapping):
            raise HandoffAcceptanceError("calendar_session_window_invalid")
        start = _timestamp(item.get("start"), "calendar.session_window.start")
        end = _timestamp(item.get("end"), "calendar.session_window.end")
        if start <= value <= end:
            return True
    return False


def _canonical_json_projection(value: Any, name: str) -> Any:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise HandoffAcceptanceError(f"canonical_projection_required:{name}") from exc


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HandoffAcceptanceError(f"text_required:{name}")
    return value.strip()


def _timestamp(value: Any, name: str) -> datetime:
    raw = _text(value, name)
    if not (raw.endswith("Z") or re.search(r"[+-]\d{2}:\d{2}$", raw)):
        raise HandoffAcceptanceError(f"timezone_required:{name}")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HandoffAcceptanceError(f"timestamp_invalid:{name}") from exc
    if parsed.tzinfo is None:
        raise HandoffAcceptanceError(f"timezone_required:{name}")
    return parsed


def _trade_date(value: Any) -> str:
    raw = _text(value, "trade_date")
    if not re.fullmatch(r"\d{8}", raw):
        raise HandoffAcceptanceError("trade_date_required")
    try:
        datetime.strptime(raw, "%Y%m%d")
    except ValueError as exc:
        raise HandoffAcceptanceError("trade_date_invalid") from exc
    return raw


def _positive(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HandoffAcceptanceError(f"positive_number_required:{name}")
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise HandoffAcceptanceError(f"positive_number_required:{name}")
    return parsed


def _positive_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return _positive(value, "contract_spec")
    except HandoffAcceptanceError:
        return None


def _nonnegative(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HandoffAcceptanceError(f"nonnegative_number_required:{name}")
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise HandoffAcceptanceError(f"nonnegative_number_required:{name}")
    return parsed


def _whole_positive(value: Any, name: str) -> int:
    parsed = _positive(value, name)
    if not parsed.is_integer():
        raise HandoffAcceptanceError(f"whole_number_required:{name}")
    return int(parsed)


def _sha256(value: Any) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise HandoffAcceptanceError("canonical_projection_required") from exc
    return hashlib.sha256(encoded).hexdigest()


__all__ = ["HandoffAcceptanceError", "PROFILE_ID", "evaluate_handoff_fixture"]
