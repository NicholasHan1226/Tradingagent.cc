"""Contract tests for the event-catalyst adapters."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from Ashare.event_catalyst_adapter import (
    EVENT_CATALYST_ADAPTER_CONTRACT,
    EventCatalystAdapterError,
    catalyst_entries_from_calendar_document,
    catalyst_entry_from_disclosure_snapshot,
    catalyst_entry_from_lockup_row,
)
from Ashare.event_catalyst_shadow import CatalystEntry
from Ashare.event_evidence import EventEvidenceSnapshot


AS_OF = datetime.fromisoformat("2026-08-30T10:00:00+08:00")
EARLIER = datetime.fromisoformat("2026-08-29T09:00:00+08:00")
SYMBOL = "002475.SZ"


def _snapshot(**overrides) -> EventEvidenceSnapshot:
    payload = {
        "dataset_id": "cn.dataset.disclosure_date",
        "catalog_version": "fixture-catalog-v1",
        "event_time": "2026-08-28",
        "event_time_precision": "date",
        "as_of": AS_OF,
        "data_through": EARLIER,
        "available_at": AS_OF,
        "available_at_source": "query_envelope.metadata.observed_at",
        "entity": "立讯精密",
        "symbol": SYMBOL,
        "title": "2026年半年报披露",
        "content": None,
        "url": "https://example.invalid/ann/1",
        "source": "fixture",
        "receipt_id": "receipt-1",
        "source_lineage_sha256": "a" * 64,
        "source_row_sha256": "b" * 64,
        "envelope_proof_sha256": "c" * 64,
        "evidence_ref": "td-v1:cn.dataset.disclosure_date:receipt-1:" + "b" * 16,
        "evidence_confidence": 0.9,
        "event_time_instant_proven": False,
        "historical_known_time_proven": False,
        "pit_feature_eligible": False,
    }
    payload.update(overrides)
    return EventEvidenceSnapshot(**payload)


class TestDisclosureSnapshotAdapter:
    def test_mints_hard_dated_entry(self):
        entry = catalyst_entry_from_disclosure_snapshot(_snapshot())
        assert isinstance(entry, CatalystEntry)
        assert entry.event_type == "earnings_disclosure"
        assert entry.date_confidence == "hard_date"
        assert entry.impact_direction == "unclear"
        assert entry.scheduled_date == date(2026, 8, 28)
        assert entry.symbol == SYMBOL
        assert entry.source_ref.startswith("td-v1:")
        assert entry.event_id.endswith(_snapshot().evidence_ref)

    def test_accepts_datetime_event_time(self):
        entry = catalyst_entry_from_disclosure_snapshot(
            _snapshot(
                event_time="2026-08-28T19:00:00+08:00",
                event_time_precision="instant",
                event_time_instant_proven=True,
            )
        )
        assert entry.scheduled_date == date(2026, 8, 28)

    def test_rejects_non_appointment_dataset(self):
        with pytest.raises(EventCatalystAdapterError) as excinfo:
            catalyst_entry_from_disclosure_snapshot(
                _snapshot(
                    dataset_id="cn.dataset.anns_d",
                    evidence_ref="td-v1:cn.dataset.anns_d:receipt-1:" + "b" * 16,
                )
            )
        assert (
            excinfo.value.reason_code
            == "event_catalyst_adapter_dataset_not_appointment"
        )

    def test_rejects_missing_symbol(self):
        with pytest.raises(EventCatalystAdapterError) as excinfo:
            catalyst_entry_from_disclosure_snapshot(
                _snapshot(
                    symbol=None,
                    evidence_ref=(
                        "td-v1:cn.dataset.disclosure_date:receipt-1:" + "b" * 16
                    ),
                )
            )
        assert (
            excinfo.value.reason_code
            == "event_catalyst_adapter_symbol_missing"
        )

    def test_rejects_mainboard_out_of_scope_symbol(self):
        # The evidence layer itself fail-closes before the adapter runs.
        from Ashare.event_evidence import AshareEvidenceContractError

        with pytest.raises(AshareEvidenceContractError) as excinfo:
            _snapshot(symbol="688981.SH")
        assert (
            excinfo.value.reason_code
            == "ashare_evidence_symbol_outside_mainboard_scope"
        )


def _doc(**overrides):
    document = {
        "calendar_id": "tech-policy-calendar-2026h2",
        "entries": [
            {
                "event_id": "politburo-2026-10",
                "event_type": "policy_meeting",
                "scheduled_date": "2026-10-26",
                "date_confidence": "expected_window",
                "impact_direction": "unclear",
                "source_ref": "manual:convention-inference",
                "entity": "CN-MACRO",
                "symbol": None,
            },
            {
                "event_id": "apple-sept-2026",
                "event_type": "product_launch",
                "scheduled_date": "2026-09-08",
                "date_confidence": "expected_window",
                "impact_direction": "positive",
                "source_ref": "manual:annual-convention",
                "entity": "AAPL",
                "symbol": "002475.SZ",
            },
        ],
    }
    document.update(overrides)
    return document


class TestCalendarDocumentAdapter:
    def test_valid_document_mints_entries(self):
        entries = catalyst_entries_from_calendar_document(_doc())
        assert len(entries) == 2
        first, second = entries
        assert first.event_id == "tech-policy-calendar-2026h2:politburo-2026-10"
        assert first.symbol is None and first.entity == "CN-MACRO"
        assert second.symbol == "002475.SZ"
        assert second.scheduled_date == date(2026, 9, 8)

    def test_duplicate_event_id_fails_closed(self):
        doc = _doc()
        doc["entries"].append(dict(doc["entries"][0]))
        with pytest.raises(EventCatalystAdapterError) as excinfo:
            catalyst_entries_from_calendar_document(doc)
        assert (
            excinfo.value.reason_code
            == "event_catalyst_calendar_event_id_duplicate"
        )

    def test_unknown_event_type_fails_closed(self):
        doc = _doc()
        doc["entries"][0]["event_type"] = "insider_tip"
        with pytest.raises(EventCatalystAdapterError) as excinfo:
            catalyst_entries_from_calendar_document(doc)
        assert (
            excinfo.value.reason_code
            == "event_catalyst_calendar_event_type_invalid"
        )

    def test_unknown_confidence_fails_closed(self):
        doc = _doc()
        doc["entries"][0]["date_confidence"] = "certain"
        with pytest.raises(EventCatalystAdapterError) as excinfo:
            catalyst_entries_from_calendar_document(doc)
        assert (
            excinfo.value.reason_code
            == "event_catalyst_calendar_confidence_invalid"
        )

    def test_bad_date_fails_closed(self):
        doc = _doc()
        doc["entries"][0]["scheduled_date"] = "next-tuesday"
        with pytest.raises(EventCatalystAdapterError) as excinfo:
            catalyst_entries_from_calendar_document(doc)
        assert (
            excinfo.value.reason_code
            == "event_catalyst_calendar_scheduled_date_invalid"
        )

    def test_empty_entries_fail_closed(self):
        with pytest.raises(EventCatalystAdapterError):
            catalyst_entries_from_calendar_document(_doc(entries=[]))

    def test_non_mapping_fails_closed(self):
        with pytest.raises(EventCatalystAdapterError):
            catalyst_entries_from_calendar_document(["not", "a", "mapping"])


class TestLockupRowAdapter:
    def _row(self, **overrides):
        row = {
            "ts_code": "002475.SZ",
            "ann_date": "20260810",
            "float_date": "20260915",
            "float_share": 12500000.0,
            "float_ratio": 1.73,
            "holder_name": "示例控股股东",
            "share_type": "首发原股东限售股份",
        }
        row.update(overrides)
        return row

    def test_mints_hard_dated_negative_entry(self):
        from Ashare.event_catalyst_adapter import (
            catalyst_entry_from_lockup_row,
        )

        entry = catalyst_entry_from_lockup_row(
            self._row(),
            dataset_id="cn.dataset.share_float",
            receipt_id="receipt-9",
        )
        assert entry.event_type == "lockup_expiry"
        assert entry.date_confidence == "hard_date"
        assert entry.impact_direction == "negative"
        assert entry.scheduled_date == date(2026, 9, 15)
        assert entry.symbol == "002475.SZ"
        assert entry.source_ref == "td-v1:cn.dataset.share_float:receipt-9"
        assert "receipt-9" in entry.event_id

    def test_rejects_wrong_dataset(self):
        from Ashare.event_catalyst_adapter import (
            catalyst_entry_from_lockup_row,
        )

        with pytest.raises(EventCatalystAdapterError) as excinfo:
            catalyst_entry_from_lockup_row(
                self._row(),
                dataset_id="cn.dataset.disclosure_date",
                receipt_id="receipt-9",
            )
        assert (
            excinfo.value.reason_code
            == "event_catalyst_adapter_lockup_dataset_invalid"
        )

    def test_rejects_missing_field(self):
        from Ashare.event_catalyst_adapter import (
            catalyst_entry_from_lockup_row,
        )

        with pytest.raises(EventCatalystAdapterError) as excinfo:
            catalyst_entry_from_lockup_row(
                self._row(float_ratio=None),
                dataset_id="cn.dataset.share_float",
                receipt_id="receipt-9",
            )
        assert (
            excinfo.value.reason_code
            == "event_catalyst_adapter_lockup_field_missing"
        )

    def test_rejects_bad_float_date(self):
        from Ashare.event_catalyst_adapter import (
            catalyst_entry_from_lockup_row,
        )

        with pytest.raises(EventCatalystAdapterError) as excinfo:
            catalyst_entry_from_lockup_row(
                self._row(float_date="mid-september"),
                dataset_id="cn.dataset.share_float",
                receipt_id="receipt-9",
            )
        assert (
            excinfo.value.reason_code
            == "event_catalyst_adapter_lockup_float_date_invalid"
        )

    def test_rejects_nonpositive_share(self):
        from Ashare.event_catalyst_adapter import (
            catalyst_entry_from_lockup_row,
        )

        with pytest.raises(EventCatalystAdapterError) as excinfo:
            catalyst_entry_from_lockup_row(
                self._row(float_share=0),
                dataset_id="cn.dataset.share_float",
                receipt_id="receipt-9",
            )
        assert (
            excinfo.value.reason_code
            == "event_catalyst_adapter_lockup_share_invalid"
        )

    def test_rejects_star_market_symbol(self):
        from Ashare.event_catalyst_adapter import (
            catalyst_entry_from_lockup_row,
        )

        with pytest.raises(EventCatalystAdapterError) as excinfo:
            catalyst_entry_from_lockup_row(
                self._row(ts_code="688981.SH"),
                dataset_id="cn.dataset.share_float",
                receipt_id="receipt-9",
            )
        assert (
            excinfo.value.reason_code
            == "event_catalyst_symbol_outside_mainboard_scope"
        )


class TestExplicitEventClusterIds:
    def test_disclosure_cluster_id_matches_event_id(self):
        entry = catalyst_entry_from_disclosure_snapshot(_snapshot())
        assert entry.event_cluster_id == entry.event_id
        assert entry.event_cluster_id.startswith("disclosure:")

    def test_calendar_cluster_id_shares_calendar_scope(self):
        entries = catalyst_entries_from_calendar_document(_doc())
        for entry in entries:
            assert entry.event_cluster_id == entry.event_id

    def test_lockup_cluster_drops_row_identity(self):
        row = {
            "ts_code": "002475.SZ",
            "ann_date": "20260810",
            "float_date": "20260915",
            "float_share": 12500000.0,
            "float_ratio": 1.73,
            "holder_name": "示例控股股东",
            "share_type": "首发原股东限售股份",
        }
        other_holder = dict(row, holder_name="另一股东")
        first = catalyst_entry_from_lockup_row(
            row, dataset_id="cn.dataset.share_float", receipt_id="r1"
        )
        second = catalyst_entry_from_lockup_row(
            other_holder, dataset_id="cn.dataset.share_float", receipt_id="r2"
        )
        # Same symbol + same float_date = one underlying unlock event,
        # regardless of holder row or ingestion receipt.
        assert first.event_cluster_id == second.event_cluster_id
        assert first.event_id != second.event_id
