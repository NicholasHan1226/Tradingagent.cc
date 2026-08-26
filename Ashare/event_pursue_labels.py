"""Shared Pursue-pair label attachment for portfolio-layer overlays.

Single source of truth for the #47 forward program's two Pursue
conditions and their label wiring: both the hard-filter scan
(``event_valholdtype_portfolio_overlay.py``) and the priority-tilt scan
(``event_priority_tilt_overlay.py``) attach identical bucket labels, so
the side-table calls live here instead of being copied per overlay.
Labels key on (ts_code, float_date) identity exactly like the study
engines and tracker; missing shard or unknown batch lands as
``unlabeled`` (excluded downstream — unlabeled is honest).
research_only / not_promotion_evidence.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from Ashare.event_unlock_holdertype_study import (  # noqa: E402
    holdertype_buckets_for_entries,
)
from Ashare.event_valuation_prelockup_study import (  # noqa: E402
    valuation_buckets_for_entries,
)

PURSUE_VALUATION = "low_le25"
PURSUE_HOLDERTYPE = "incentive"


def attach_pursue_labels(
    signals: list[dict[str, object]], cache: Path
) -> None:
    """Fill ``valuation_bucket`` / ``holdertype_bucket`` in place."""

    entries = [(str(s["ts_code"]), str(s["float_date"])) for s in signals]
    val_labels = valuation_buckets_for_entries(cache, entries)
    hold_labels = holdertype_buckets_for_entries(cache, entries)
    for s in signals:
        key = (str(s["ts_code"]), str(s["float_date"]))
        s["valuation_bucket"] = val_labels.get(key, "unlabeled")
        s["holdertype_bucket"] = hold_labels.get(key, "unlabeled")
