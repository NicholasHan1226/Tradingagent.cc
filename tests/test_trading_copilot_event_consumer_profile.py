from __future__ import annotations

import pytest


def test_declares_the_reviewed_tradingdatas_event_consumers() -> None:
    from Ashare.trading_copilot_event_consumer_profile import (
        automatic_event_dataset_ids,
        load_event_consumer_profiles,
    )

    profiles = load_event_consumer_profiles()

    assert [(profile.dataset_id, profile.category, profile.cadence) for profile in profiles] == [
        ("cn.dataset.anns_d", "announcement", "session_bounded"),
        ("cn.dataset.cctv_news", "news", "session_bounded"),
        ("cn.dataset.irm_qa_sh", "interaction", "session_bounded"),
        ("cn.dataset.irm_qa_sz", "interaction", "session_bounded"),
        ("cn.dataset.research_report", "research", "session_bounded"),
        ("cn.dataset.major_news", "macro_news", "on_demand"),
    ]
    assert automatic_event_dataset_ids(profiles) == (
        "cn.dataset.anns_d",
        "cn.dataset.cctv_news",
        "cn.dataset.irm_qa_sh",
        "cn.dataset.irm_qa_sz",
        "cn.dataset.research_report",
    )
    major_news = profiles[-1]
    assert major_news.explicit_request_required is True
    assert major_news.requires_fresh_receipt_lineage is True


def test_rejects_unreviewed_or_automatic_on_demand_requests() -> None:
    from Ashare.trading_copilot_event_consumer_profile import (
        TradingCopilotEventConsumerProfileError,
        load_event_consumer_profiles,
        select_event_consumer_profiles,
    )

    with pytest.raises(
        TradingCopilotEventConsumerProfileError,
        match="copilot_event_consumer_request_forbidden",
    ):
        select_event_consumer_profiles(
            load_event_consumer_profiles(),
            requested_on_demand_dataset_ids=("cn.dataset.news",),
        )
