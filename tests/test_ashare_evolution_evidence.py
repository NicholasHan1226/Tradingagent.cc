from shared.review.sample_quality import enrich_trade_sample


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


def test_evolution_requires_explicit_verified_5min_evidence_class():
    trade = _base_trade()

    enriched = enrich_trade_sample(trade)

    assert enriched["strategy_sample_valid"] is True
    assert enriched["evolution_sample_eligible"] is False
    assert enriched["evolution_sample_reason"] == "weak_fill_price_evidence"


def test_verified_5min_evidence_is_eligible_for_evolution():
    trade = _base_trade()
    trade["fill_evidence"] = {
        **trade["fill_evidence"],
        "execution_evidence_class": "verified_5min_market_data",
    }

    enriched = enrich_trade_sample(trade)

    assert enriched["evolution_sample_eligible"] is True
    assert enriched["evolution_sample_reason"] == "verified_market_data_execution"


def test_stale_verified_5min_label_is_not_eligible_for_evolution():
    trade = _base_trade()
    trade["fill_evidence"] = {
        **trade["fill_evidence"],
        "bar_time": "2026-07-10 09:35:00",
        "execution_evidence_class": "verified_5min_market_data",
    }

    enriched = enrich_trade_sample(trade)

    assert enriched["evolution_sample_eligible"] is False
    assert enriched["evolution_sample_reason"] == "weak_fill_price_evidence"
