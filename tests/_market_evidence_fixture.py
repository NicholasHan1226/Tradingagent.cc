"""Strict non-production market-evidence builders for paper-runtime tests."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from typing import Any, Mapping

from shared.runtime.market_evidence_authority import (
    AShareExecutionQuoteEvidence,
    AShareMarkEvidence,
    MarketEvidenceContext,
    MarketSourceBinding,
    NonProductionFixtureMarketEvidenceVerifier,
    freeze_non_production_market_evidence,
)


def _sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _source(raw: Mapping[str, Any]) -> MarketSourceBinding:
    return MarketSourceBinding(
        dataset_id=str(raw["dataset_id"]),
        catalog_version=str(raw["catalog_version"]),
        source_receipt_id=str(raw["source_receipt_id"]),
        source_receipt_sha256=str(raw["source_sha256"]),
        source_lineage_sha256=str(raw["source_lineage_sha256"]),
        data_through=_instant(str(raw["data_through"])),
        observed_at=_instant(str(raw["observed_at"])),
        available_at=_instant(str(raw["available_at"])),
    )


def attach_mark_authority(
    raw: Mapping[str, Any],
    *,
    symbol: str,
    decision_as_of: str,
    capital_authority_id: str,
    authority_generation: int,
    execution_lineage: str,
) -> dict[str, Any]:
    value = dict(raw)
    value.setdefault("dataset_id", "fixture.ashare.daily_close.v1")
    value.setdefault("catalog_version", "fixture-catalog-v1")
    value.setdefault(
        "source_lineage_sha256",
        _sha256(
            {
                "dataset_id": value["dataset_id"],
                "source_receipt_id": value["source_receipt_id"],
            }
        ),
    )
    value["data_authority_id"] = NonProductionFixtureMarketEvidenceVerifier.verifier_id
    context = MarketEvidenceContext(
        trade_date=date.fromisoformat(decision_as_of[:10]),
        decision_as_of=_instant(decision_as_of),
        capital_authority_id=capital_authority_id,
        authority_generation=authority_generation,
        execution_lineage_id=execution_lineage,
        account_type="simulated",
        real_trading_enabled=False,
    )
    evidence = AShareMarkEvidence(
        symbol=symbol,
        price_cny=float(value["price_cny"]),
        market_session=str(value["market_session"]),
        source=_source(value),
        session_calendar_receipt_sha256=_sha256(value["session_calendar_receipt"]),
        context=context,
    )
    value["market_evidence_authority"] = freeze_non_production_market_evidence(
        evidence,
        expected_dataset_id=evidence.source.dataset_id,
        frozen_at=context.decision_as_of,
    )
    return value


def attach_quote_authority(
    raw: Mapping[str, Any],
    *,
    symbol: str,
    decision_as_of: str,
) -> dict[str, Any]:
    value = dict(raw)
    value["symbol"] = symbol
    value["decision_as_of"] = decision_as_of
    value.setdefault("dataset_id", "fixture.ashare.execution_quotes.v1")
    value.setdefault("catalog_version", "fixture-catalog-v1")
    value.setdefault(
        "source_lineage_sha256",
        _sha256(
            {
                "dataset_id": value["dataset_id"],
                "source_receipt_id": value["source_receipt_id"],
            }
        ),
    )
    context = MarketEvidenceContext(
        trade_date=date.fromisoformat(str(value["trade_date"])),
        decision_as_of=_instant(decision_as_of),
        capital_authority_id=str(value["capital_authority_id"]),
        authority_generation=int(value["authority_generation"]),
        execution_lineage_id=str(value["execution_lineage"]),
        account_type=str(value["account_type"]),
        real_trading_enabled=bool(value["real_trading_enabled"]),
    )
    evidence = AShareExecutionQuoteEvidence(
        symbol=symbol,
        order_id=str(value["snapshot_id"])
        .removeprefix("SNAPSHOT-")
        .removeprefix("snapshot-"),
        bid_price_cny=float(value.get("bid_price", 0.0)),
        ask_price_cny=float(value.get("ask_price", 0.0)),
        bid_size=int(value.get("bid_size", 0)),
        ask_size=int(value.get("ask_size", 0)),
        previous_close_cny=float(value["previous_close"]),
        market_session=str(value["market_session"]),
        execution_time=_instant(str(value["execution_time"])),
        source=_source(value),
        session_calendar_receipt_sha256=_sha256(value["session_calendar_receipt"]),
        context=context,
    )
    value["market_evidence_authority"] = freeze_non_production_market_evidence(
        evidence,
        expected_dataset_id=evidence.source.dataset_id,
        frozen_at=evidence.execution_time,
    )
    return value
