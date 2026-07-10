"""Test A-share evolution evidence classification and rejection reasons."""

from shared.review.sample_quality import enrich_trade_sample, summarize_sample_quality


def _base_trade() -> dict[str, object]:
    return {
        "market": "ashare",
        "capital_layer": "simulated",
        "side": "buy",
        "execution_source": "ashare_candidate_layer",
        "candidate_pool_layer": "candidate",
        "filled_price": 10.5,
        "trade_timestamp_bj": "2026-07-10T10:05:00+08:00",
        "fill_price_source": "order.market_snapshot.ask_price",
        "fill_price_source_class": "market_data",
        "fill_evidence": {
            "bar_time": "2026-07-10 10:05:00",
            "bar_volume": 1800,
        },
    }


def _make_verified(trade: dict[str, object]) -> dict[str, object]:
    trade["fill_evidence"] = {
        **trade["fill_evidence"],
        "execution_evidence_class": "verified_5min_market_data",
    }
    return trade


# --- existing tests (updated for specific reasons) ---

def test_evolution_requires_explicit_verified_5min_evidence_class():
    """Base trade has no execution_evidence_class → rejected with specific reason."""
    trade = _base_trade()

    enriched = enrich_trade_sample(trade)

    assert enriched["strategy_sample_valid"] is True
    assert enriched["evolution_sample_eligible"] is False
    assert enriched["evolution_sample_reason"] == "missing_execution_evidence_class"


def test_verified_5min_evidence_is_eligible_for_evolution():
    """Fresh bar with verified_5min → eligible."""
    trade = _make_verified(_base_trade())

    enriched = enrich_trade_sample(trade)

    assert enriched["evolution_sample_eligible"] is True
    assert enriched["evolution_sample_reason"] == "verified_market_data_execution"


def test_stale_verified_5min_label_is_not_eligible_for_evolution():
    """Bar time 30 minutes before trade → bar_time_too_stale."""
    trade = _base_trade()
    trade["fill_evidence"] = {
        **trade["fill_evidence"],
        "bar_time": "2026-07-10 09:35:00",
        "execution_evidence_class": "verified_5min_market_data",
    }

    enriched = enrich_trade_sample(trade)

    assert enriched["evolution_sample_eligible"] is False
    assert enriched["evolution_sample_reason"] == "bar_time_too_stale"


# --- new rejection reason tests ---

def test_weak_price_only_evidence_class_rejected():
    """execution_evidence_class=weak_price_only → rejected."""
    trade = _make_verified(_base_trade())
    trade["fill_evidence"]["execution_evidence_class"] = "weak_price_only"

    enriched = enrich_trade_sample(trade)

    assert enriched["strategy_sample_valid"] is True
    assert enriched["evolution_sample_eligible"] is False
    assert enriched["evolution_sample_reason"] == "execution_evidence_class_not_verified"


def test_non_market_data_fill_price_rejected():
    """fill_price_source_class not market_data and no market source markers → rejected."""
    trade = _make_verified(_base_trade())
    trade["fill_price_source_class"] = "signal_card_price"
    trade["fill_price_source"] = "signal_card.price"

    enriched = enrich_trade_sample(trade)

    assert enriched["strategy_sample_valid"] is True
    assert enriched["evolution_sample_eligible"] is False
    assert enriched["evolution_sample_reason"] == "no_market_data_fill_price"


def test_missing_bar_time_rejected():
    """No bar_time in row or fill_evidence → rejected."""
    trade = _make_verified(_base_trade())
    trade["fill_evidence"].pop("bar_time", None)

    enriched = enrich_trade_sample(trade)

    assert enriched["evolution_sample_eligible"] is False
    assert enriched["evolution_sample_reason"] == "missing_bar_time"


def test_zero_bar_volume_rejected():
    """bar_volume=0 → rejected."""
    trade = _make_verified(_base_trade())
    trade["fill_evidence"]["bar_volume"] = 0

    enriched = enrich_trade_sample(trade)

    assert enriched["evolution_sample_eligible"] is False
    assert enriched["evolution_sample_reason"] == "bar_volume_zero_or_negative"


def test_bar_volume_not_numeric_rejected():
    """bar_volume is a non-numeric string → rejected."""
    trade = _make_verified(_base_trade())
    trade["fill_evidence"]["bar_volume"] = "N/A"

    enriched = enrich_trade_sample(trade)

    assert enriched["evolution_sample_eligible"] is False
    assert enriched["evolution_sample_reason"] == "bar_volume_not_numeric"


def test_missing_trade_timestamp_rejected():
    """No trade_timestamp_bj → rejected."""
    trade = _make_verified(_base_trade())
    trade["trade_timestamp_bj"] = ""

    enriched = enrich_trade_sample(trade)

    assert enriched["evolution_sample_eligible"] is False
    assert enriched["evolution_sample_reason"] == "missing_trade_timestamp"


def test_bar_time_unparseable_rejected():
    """Garbage bar_time → rejected."""
    trade = _make_verified(_base_trade())
    trade["fill_evidence"]["bar_time"] = "not-a-date"

    enriched = enrich_trade_sample(trade)

    assert enriched["evolution_sample_eligible"] is False
    assert enriched["evolution_sample_reason"] == "bar_time_unparseable"


def test_bar_time_too_future_rejected():
    """Bar time > 5 minutes in the future → rejected."""
    trade = _make_verified(_base_trade())
    trade["fill_evidence"]["bar_time"] = "2026-07-10 10:12:00"  # 7 min after trade

    enriched = enrich_trade_sample(trade)

    assert enriched["evolution_sample_eligible"] is False
    assert enriched["evolution_sample_reason"] == "bar_time_too_future"


def test_non_strategy_sample_has_not_strategy_sample_reason():
    """A chain_validation trade gets not_strategy_sample reason."""
    trade = {
        "market": "ashare",
        "capital_layer": "simulated",
        "side": "buy",
        "trade_timestamp_bj": "2026-07-10T10:05:00+08:00",
    }

    enriched = enrich_trade_sample(trade)

    assert enriched["strategy_sample_valid"] is False
    assert enriched["evolution_sample_eligible"] is False
    assert enriched["evolution_sample_reason"] == "not_strategy_sample"


# --- summarize_sample_quality rejection reason counter tests ---

def test_summarize_quality_counts_rejection_reasons():
    """Multiple trades with different rejection reasons are counted correctly."""
    base = _make_verified(_base_trade())

    # 1 eligible
    eligible = dict(base)

    # 1 stale
    stale = dict(base)
    stale["fill_evidence"] = {**stale["fill_evidence"], "bar_time": "2026-07-10 09:35:00"}
    stale["trade_id"] = "stale-1"

    # 1 missing evidence class
    missing_class = _base_trade()
    missing_class["trade_id"] = "missing-1"

    # 1 bar volume zero
    zero_vol = dict(base)
    zero_vol["fill_evidence"] = {**zero_vol["fill_evidence"], "bar_volume": 0}
    zero_vol["trade_id"] = "zero-1"

    quality = summarize_sample_quality([eligible, stale, missing_class, zero_vol])

    assert quality["strategy_sample_valid_count"] == 4
    assert quality["evolution_sample_eligible_count"] == 1
    assert quality["evolution_rejection_reasons"] == {
        "bar_time_too_stale": 1,
        "bar_volume_zero_or_negative": 1,
        "missing_execution_evidence_class": 1,
    }


def test_summarize_quality_empty_rejection_when_all_eligible():
    """When all strategy trades are evolution-eligible, rejection_reasons is empty."""
    base = _make_verified(_base_trade())
    trade2 = dict(base)
    trade2["trade_id"] = "t2"

    quality = summarize_sample_quality([base, trade2])

    assert quality["strategy_sample_valid_count"] == 2
    assert quality["evolution_sample_eligible_count"] == 2
    assert quality["evolution_rejection_reasons"] == {}
