"""Shared fail-closed semantics for simulated execution receipts."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any, Mapping
from zoneinfo import ZoneInfo


_SHANGHAI = ZoneInfo("Asia/Shanghai")
_LOWER_HEX = frozenset("0123456789abcdef")
ASHARE_EXECUTION_QUOTE_MAX_AGE = timedelta(seconds=30)

CAPITAL_COMMIT_MARKET_FAILURE_REASONS = frozenset(
    {
        "paper_market_clock_regressed_before_capital_commit",
        "paper_market_clock_trade_date_mismatch_before_capital_commit",
        "paper_market_clock_session_mismatch_before_capital_commit",
        "paper_market_snapshot_stale_before_capital_commit",
    }
)


def _aware(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value or value != value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _capital_event_id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 37
        and value.startswith("MCAP-")
        and all(character in _LOWER_HEX for character in value[5:])
    )


def ashare_continuous_session(value: datetime) -> str | None:
    """Return the one continuous-auction session containing an aware instant."""

    if value.tzinfo is None or value.utcoffset() is None:
        return None
    local_time = value.astimezone(_SHANGHAI).time()
    if time(9, 30) <= local_time <= time(11, 30):
        return "continuous_auction_am"
    if time(13, 0) <= local_time < time(14, 57):
        return "continuous_auction_pm"
    return None


def is_reconcilable_not_committed_market_failure(
    receipt: Mapping[str, Any],
    *,
    expected_trade_date: str,
    reconciled_at: datetime | None = None,
) -> bool:
    """Accept only an auditable zero-fill failure before capital commit.

    A bad commit clock reading is retained as evidence.  For a clock regression
    or trade-date mismatch, ``terminal_at`` stays at the last valid submit
    instant so the safe release can still close the paper-day audit loop.
    """

    if not isinstance(receipt, Mapping):
        return False
    requested = receipt.get("requested_quantity")
    filled = receipt.get("filled_quantity")
    residual = receipt.get("residual_quantity")
    intent = receipt.get("intent")
    reason = receipt.get("execution_reason")
    if (
        receipt.get("status") != "not_filled"
        or receipt.get("capital_commit_status") != "not_committed"
        or isinstance(requested, bool)
        or not isinstance(requested, int)
        or requested <= 0
        or filled != 0
        or residual != requested
        or intent not in {"open", "increase", "reduce", "exit"}
        or reason not in CAPITAL_COMMIT_MARKET_FAILURE_REASONS
        or receipt.get("capital_commit_receipt_id") is not None
        or receipt.get("simulated_fill_id") is not None
        or receipt.get("filled_at") is not None
        or receipt.get("fill_fingerprint") is not None
    ):
        return False

    release_status = receipt.get("capital_release_status")
    release_id = receipt.get("capital_release_receipt_id")
    if intent in {"open", "increase"}:
        if release_status != "released" or not _capital_event_id(release_id):
            return False
    elif release_status not in {None, "not_applicable"} or release_id is not None:
        return False

    market_execution = _aware(receipt.get("market_execution_time"))
    market_available = _aware(receipt.get("market_available_at"))
    market_data_through = _aware(receipt.get("market_data_through"))
    submit = _aware(receipt.get("sim_submit_checked_at"))
    commit = _aware(receipt.get("capital_commit_checked_at"))
    terminal = _aware(receipt.get("terminal_at"))
    market_session = receipt.get("market_session")
    if (
        market_execution is None
        or market_available is None
        or market_data_through is None
        or submit is None
        or commit is None
        or terminal is None
        or market_session not in {"continuous_auction_am", "continuous_auction_pm"}
        or any(
            value.astimezone(_SHANGHAI).date().isoformat() != expected_trade_date
            for value in (
                market_execution,
                market_available,
                market_data_through,
                submit,
            )
        )
        or ashare_continuous_session(submit) != market_session
        or ashare_continuous_session(market_execution) != market_session
        or not (market_data_through <= market_available <= market_execution <= submit)
        or submit - market_data_through > ASHARE_EXECUTION_QUOTE_MAX_AGE
        or terminal.astimezone(_SHANGHAI).date().isoformat() != expected_trade_date
        or (
            reconciled_at is not None
            and (
                reconciled_at.tzinfo is None
                or reconciled_at.utcoffset() is None
                or terminal > reconciled_at
            )
        )
    ):
        return False

    if reason == "paper_market_clock_regressed_before_capital_commit":
        return commit < submit and terminal == submit
    if commit < submit:
        return False
    if reason == "paper_market_clock_trade_date_mismatch_before_capital_commit":
        return (
            commit.astimezone(_SHANGHAI).date().isoformat() != expected_trade_date
            and terminal == submit
        )
    if commit.astimezone(_SHANGHAI).date().isoformat() != expected_trade_date:
        return False
    if reason == "paper_market_clock_session_mismatch_before_capital_commit":
        return (
            ashare_continuous_session(commit) != market_session and terminal == commit
        )
    if ashare_continuous_session(commit) != market_session:
        return False
    if reason == "paper_market_snapshot_stale_before_capital_commit":
        return (
            commit - market_data_through > ASHARE_EXECUTION_QUOTE_MAX_AGE
            and terminal == commit
        )
    return False


__all__ = [
    "ASHARE_EXECUTION_QUOTE_MAX_AGE",
    "CAPITAL_COMMIT_MARKET_FAILURE_REASONS",
    "ashare_continuous_session",
    "is_reconcilable_not_committed_market_failure",
]
