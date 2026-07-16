"""Non-production A-share validation-plan fixture for authority-bound tests."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path

from shared.models.lifecycle import (
    TradingSessionCalendarAuthority,
    TradingSessionCalendarAuthorityVerification,
    ValidationPlan,
    build_validation_plan,
)


UTC = timezone.utc


def _fixture_sessions() -> tuple[date, ...]:
    current = date(2024, 12, 2)
    end = date(2027, 12, 31)
    closed_dates = {date(2025, 1, 1)}
    sessions: list[date] = []
    while current <= end:
        if current.weekday() < 5 and current not in closed_dates:
            sessions.append(current)
        current += timedelta(days=1)
    return tuple(sessions)


class NonProductionFixtureCalendarAuthorityVerifier:
    """Independent deterministic verifier used only by tests/fixtures."""

    verifier_id = "non-production-fixture-calendar-verifier"
    verifier_version = "1.0.0"

    def verify(
        self,
        calendar: TradingSessionCalendarAuthority,
        *,
        frozen_at: datetime,
    ) -> TradingSessionCalendarAuthorityVerification:
        return TradingSessionCalendarAuthorityVerification(
            accepted=True,
            verifier_id=self.verifier_id,
            verifier_version=self.verifier_version,
            proof_sha256="f" * 64,
            verified_at=frozen_at - timedelta(minutes=1),
            frozen_at=frozen_at,
            calendar_sha256=calendar.calendar_sha256,
            source_receipt_id=calendar.source_receipt_id,
            source_receipt_sha256=calendar.source_receipt_sha256,
        )


def build_non_production_ashare_validation_plan() -> ValidationPlan:
    """Return one frozen, authority-bound fixture plan; never use in production."""

    calendar = TradingSessionCalendarAuthority(
        market="ashare",
        calendar_id="fixture-sse-szse-joint-trading-sessions",
        calendar_version="non-production-fixture-through-20271231-v1",
        source_dataset_id="fixture.ashare.trade_calendar",
        source_receipt_id="receipt-non-production-fixture-calendar-001",
        source_receipt_sha256="e" * 64,
        available_at=datetime(2025, 4, 1, tzinfo=UTC),
        sessions=_fixture_sessions(),
    )
    return build_validation_plan(
        train_start=date(2024, 12, 2),
        train_end=date(2024, 12, 31),
        validation_start=date(2025, 1, 9),
        validation_end=date(2025, 2, 28),
        test_start=date(2025, 3, 10),
        test_end=date(2025, 3, 31),
        purge_days=5,
        embargo_days=5,
        label_horizon_days=5,
        max_feature_lookback_days=5,
        event_cluster_embargo_days=5,
        decision_cluster_key="decision_cluster_id",
        decision_cluster_deduplicated=True,
        registered_trial_count=1,
        multiple_testing_trial_budget=20,
        pbo_required=True,
        deflated_sharpe_required=True,
        oos_reuse_count=0,
        max_oos_reuse_count=1,
        oos_used_for_tuning=False,
        oos_authority_receipt_sha256="d" * 64,
        experiment_family_id="ashare-forward-label-fixture-v1",
        experiment_id="ashare-forward-label-fixture-001",
        frozen_test_set_id="ashare-forward-label-oos-fixture-v1",
        frozen_at=datetime(2025, 4, 2, tzinfo=UTC),
        market="ashare",
        trading_session_calendar=calendar,
        calendar_authority_verifier=NonProductionFixtureCalendarAuthorityVerifier(),
    )


def write_non_production_validation_plan_artifact(path: Path) -> Path:
    """Persist a test-only frozen plan artifact consumable by runtime CLIs."""

    plan = build_non_production_ashare_validation_plan()
    payload = {
        "artifact_type": "ashare_validation_plan_v1",
        "authority_tier": "non_production_fixture",
        "production_eligible": False,
        "validation_plan": plan.canonical_payload(),
        "validation_plan_sha256": plan.sha256(),
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    return path
