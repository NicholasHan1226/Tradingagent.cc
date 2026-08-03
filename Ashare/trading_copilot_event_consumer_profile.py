"""Declarative, fail-closed TradingDatas event consumer selection for Copilot."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Mapping, Sequence


EVENT_CONSUMER_PROFILE_CONTRACT = (
    "tradingagent.trading_copilot_td_event_consumer_profile.v1"
)
FIXED_TRANSPORT_CONTRACT = "tradingdatas_v1_catalog_query"
REVIEWED_DATASET_IDS = frozenset({
    "cn.dataset.anns_d",
    "cn.dataset.cctv_news",
    "cn.dataset.irm_qa_sh",
    "cn.dataset.irm_qa_sz",
    "cn.dataset.research_report",
    "cn.dataset.major_news",
})
_DEFAULT_PROFILE_PATH = (
    Path(__file__).resolve().parents[1]
    / "TradingCopilot/contracts/td_event_consumer_profile.v1.json"
)


class TradingCopilotEventConsumerProfileError(ValueError):
    """Raised when the static consumer declaration cannot be trusted."""


def _text(value: object, reason: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value:
        raise TradingCopilotEventConsumerProfileError(reason)
    return value


@dataclass(frozen=True)
class TradingCopilotEventConsumerProfile:
    dataset_id: str
    category: str
    cadence: str
    symbol_binding: str
    explicit_request_required: bool
    requires_fresh_receipt_lineage: bool

    def __post_init__(self) -> None:
        _text(self.dataset_id, "copilot_event_consumer_dataset_invalid")
        if self.category not in {
            "announcement", "news", "interaction", "research", "macro_news"
        }:
            raise TradingCopilotEventConsumerProfileError(
                "copilot_event_consumer_category_invalid"
            )
        if self.cadence not in {"session_bounded", "on_demand"}:
            raise TradingCopilotEventConsumerProfileError(
                "copilot_event_consumer_cadence_invalid"
            )
        if self.symbol_binding not in {"required", "optional"}:
            raise TradingCopilotEventConsumerProfileError(
                "copilot_event_consumer_symbol_binding_invalid"
            )
        if (
            type(self.explicit_request_required) is not bool
            or type(self.requires_fresh_receipt_lineage) is not bool
            or not self.requires_fresh_receipt_lineage
            or (self.cadence == "on_demand") is not self.explicit_request_required
        ):
            raise TradingCopilotEventConsumerProfileError(
                "copilot_event_consumer_conditions_invalid"
            )


def load_event_consumer_profiles(
    path: Path | str = _DEFAULT_PROFILE_PATH,
) -> tuple[TradingCopilotEventConsumerProfile, ...]:
    profile_path = Path(path)
    if not profile_path.is_absolute() or profile_path.is_symlink() or not profile_path.is_file():
        raise TradingCopilotEventConsumerProfileError(
            "copilot_event_consumer_profile_path_invalid"
        )
    try:
        raw = json.loads(profile_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TradingCopilotEventConsumerProfileError(
            "copilot_event_consumer_profile_invalid"
        ) from exc
    if not isinstance(raw, Mapping) or set(raw) != {
        "contractId", "transportContract", "profiles"
    }:
        raise TradingCopilotEventConsumerProfileError(
            "copilot_event_consumer_profile_invalid"
        )
    if raw.get("contractId") != EVENT_CONSUMER_PROFILE_CONTRACT or raw.get(
        "transportContract"
    ) != FIXED_TRANSPORT_CONTRACT or not isinstance(raw.get("profiles"), list):
        raise TradingCopilotEventConsumerProfileError(
            "copilot_event_consumer_profile_contract_invalid"
        )
    profiles: list[TradingCopilotEventConsumerProfile] = []
    for raw_profile in raw["profiles"]:
        if not isinstance(raw_profile, Mapping) or set(raw_profile) != {
            "datasetId",
            "category",
            "cadence",
            "symbolBinding",
            "explicitRequestRequired",
            "requiresFreshReceiptLineage",
        }:
            raise TradingCopilotEventConsumerProfileError(
                "copilot_event_consumer_profile_item_invalid"
            )
        profiles.append(
            TradingCopilotEventConsumerProfile(
                dataset_id=_text(
                    raw_profile["datasetId"], "copilot_event_consumer_dataset_invalid"
                ),
                category=_text(
                    raw_profile["category"], "copilot_event_consumer_category_invalid"
                ),
                cadence=_text(
                    raw_profile["cadence"], "copilot_event_consumer_cadence_invalid"
                ),
                symbol_binding=_text(
                    raw_profile["symbolBinding"], "copilot_event_consumer_symbol_binding_invalid"
                ),
                explicit_request_required=raw_profile["explicitRequestRequired"],
                requires_fresh_receipt_lineage=raw_profile[
                    "requiresFreshReceiptLineage"
                ],
            )
        )
    profile_dataset_ids = {profile.dataset_id for profile in profiles}
    if not profiles or len(profile_dataset_ids) != len(profiles):
        raise TradingCopilotEventConsumerProfileError(
            "copilot_event_consumer_profile_duplicate"
        )
    if profile_dataset_ids != REVIEWED_DATASET_IDS:
        raise TradingCopilotEventConsumerProfileError(
            "copilot_event_consumer_profile_scope_invalid"
        )
    return tuple(profiles)


def automatic_event_dataset_ids(
    profiles: Sequence[TradingCopilotEventConsumerProfile],
) -> tuple[str, ...]:
    return tuple(
        profile.dataset_id
        for profile in profiles
        if not profile.explicit_request_required
    )


def select_event_consumer_profiles(
    profiles: Sequence[TradingCopilotEventConsumerProfile],
    *,
    requested_on_demand_dataset_ids: Sequence[str] = (),
) -> tuple[TradingCopilotEventConsumerProfile, ...]:
    by_id = {profile.dataset_id: profile for profile in profiles}
    requested = tuple(_text(value, "copilot_event_consumer_request_invalid") for value in requested_on_demand_dataset_ids)
    if len(set(requested)) != len(requested):
        raise TradingCopilotEventConsumerProfileError(
            "copilot_event_consumer_request_duplicate"
        )
    for dataset_id in requested:
        profile = by_id.get(dataset_id)
        if profile is None or not profile.explicit_request_required:
            raise TradingCopilotEventConsumerProfileError(
                "copilot_event_consumer_request_forbidden"
            )
    return tuple(
        profile
        for profile in profiles
        if not profile.explicit_request_required or profile.dataset_id in requested
    )
