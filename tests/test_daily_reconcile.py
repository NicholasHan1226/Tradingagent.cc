from __future__ import annotations

import pytest

from Crypto.capital_policy import CRYPTO_CAPITAL_AUTHORITY_ID
from shared.accounting.daily_reconcile import (
    ReconcileSnapshotIdentity,
    reconcile,
)


def _identity(
    *,
    market: str = "ashare",
    account_id: str = "ashare-paper-001",
    authority_id: str = "ashare-capital-v1",
    broker_contract: str = "tradingagent.ashare.paper_broker.v1",
    receipt_id: str,
    observed_at: str = "2026-07-20T01:00:00Z",
    generation: int = 1,
) -> ReconcileSnapshotIdentity:
    return ReconcileSnapshotIdentity(
        market=market,
        account_id=account_id,
        authority_id=authority_id,
        broker_contract=broker_contract,
        receipt_id=receipt_id,
        observed_at=observed_at,
        generation=generation,
    )


def test_reconcile_uses_provider_neutral_broker_fields() -> None:
    result = reconcile(
        [{"instrument_id": "600000.SH", "quantity": 100, "cost_basis": 1_000.0}],
        [{"instrument_id": "600000.SH", "quantity": 80, "market_value": 840.0}],
        system_identity=_identity(receipt_id="system-001"),
        broker_identity=_identity(receipt_id="broker-001"),
        quantity_step="1",
        log=False,
    )

    mismatch = result["mismatches"][0]
    assert mismatch["broker_qty"] == 80
    assert mismatch["broker_value"] == 840
    assert "hermes_qty" not in mismatch
    assert result["summary"]["total_broker"] == 1
    assert result["actions"][0]["action"] == "investigate"


def test_reconcile_never_treats_missing_adapter_position_as_automatic_sync() -> None:
    result = reconcile(
        [{"instrument_id": "000001.SZ", "quantity": 100, "cost_basis": 900.0}],
        [],
        system_identity=_identity(receipt_id="system-002"),
        broker_identity=_identity(receipt_id="broker-002"),
        quantity_step="1",
        log=False,
    )

    assert result["mismatches"][0]["type"] == "missing_in_broker"
    assert result["actions"] == [
        {
            "instrument_id": "000001.SZ",
            "position_side": "long",
            "position_bucket": "settled",
            "action": "investigate",
            "detail": (
                "Quantity mismatch: system=100, broker=0, diff=100. "
                "Investigate before selecting either side as authority."
            ),
        }
    ]


def test_reconcile_preserves_crypto_fractional_quantity() -> None:
    system_identity = _identity(
        market="crypto",
        account_id="crypto-paper-001",
        authority_id=CRYPTO_CAPITAL_AUTHORITY_ID,
        broker_contract="tradingagent.crypto.paper_broker.v1",
        receipt_id="system-crypto-001",
    )
    broker_identity = _identity(
        market="crypto",
        account_id="crypto-paper-001",
        authority_id=CRYPTO_CAPITAL_AUTHORITY_ID,
        broker_contract="tradingagent.crypto.paper_broker.v1",
        receipt_id="broker-crypto-001",
    )

    result = reconcile(
        [{"instrument_id": "BTCUSDT", "quantity": "0.001", "market_value": "70"}],
        [{"instrument_id": "BTCUSDT", "quantity": "0.001", "market_value": "70"}],
        system_identity=system_identity,
        broker_identity=broker_identity,
        quantity_step="0.000001",
        log=False,
    )

    assert result["matched"] == ["BTCUSDT|long|settled"]
    assert result["summary"]["passed"] is True


def test_reconcile_supports_explicit_cnfutures_short_positions() -> None:
    system_identity = _identity(
        market="cn_futures",
        account_id="cnfutures-paper-001",
        authority_id="cn-futures-capital-v1",
        broker_contract="tradingagent.cnfutures.paper_broker.v1",
        receipt_id="system-cnf-001",
    )
    broker_identity = _identity(
        market="cn_futures",
        account_id="cnfutures-paper-001",
        authority_id="cn-futures-capital-v1",
        broker_contract="tradingagent.cnfutures.paper_broker.v1",
        receipt_id="broker-cnf-001",
    )

    result = reconcile(
        [
            {
                "instrument_id": "rb2601",
                "position_side": "short",
                "position_bucket": "today",
                "quantity": 2,
                "market_value": 70000,
            }
        ],
        [
            {
                "instrument_id": "rb2601",
                "position_side": "short",
                "position_bucket": "today",
                "quantity": 2,
                "market_value": 70000,
            }
        ],
        system_identity=system_identity,
        broker_identity=broker_identity,
        quantity_step="1",
        allow_short=True,
        log=False,
    )

    assert result["matched"] == ["rb2601|short|today"]
    assert result["identity"]["allow_short"] is True


def test_reconcile_rejects_cross_market_or_account_binding() -> None:
    with pytest.raises(ValueError, match="identity mismatch"):
        reconcile(
            [],
            [],
            system_identity=_identity(receipt_id="system-003"),
            broker_identity=_identity(
                market="cnfutures",
                account_id="cnfutures-paper-001",
                authority_id="cn-futures-capital-v1",
                broker_contract="tradingagent.cnfutures.paper_broker.v1",
                receipt_id="broker-003",
            ),
            quantity_step="1",
            log=False,
        )


def test_reconcile_rejects_wrong_authority_for_the_declared_market() -> None:
    with pytest.raises(ValueError, match="market governance"):
        reconcile(
            [],
            [],
            system_identity=_identity(
                authority_id="forged-authority", receipt_id="system-005"
            ),
            broker_identity=_identity(
                authority_id="forged-authority", receipt_id="broker-005"
            ),
            quantity_step="1",
            log=False,
        )


def test_cnfutures_long_short_and_today_yesterday_are_not_netted() -> None:
    identity_kwargs = {
        "market": "cn_futures",
        "account_id": "cnfutures-paper-001",
        "authority_id": "cn-futures-capital-v1",
        "broker_contract": "tradingagent.cnfutures.paper_broker.v1",
    }
    system = [
        {
            "instrument_id": "rb2601",
            "position_side": "long",
            "position_bucket": "today",
            "quantity": 2,
            "market_value": 70000,
        },
        {
            "instrument_id": "rb2601",
            "position_side": "short",
            "position_bucket": "yesterday",
            "quantity": 1,
            "market_value": 35000,
        },
    ]
    result = reconcile(
        system,
        list(system),
        system_identity=_identity(receipt_id="system-cnf-dims", **identity_kwargs),
        broker_identity=_identity(receipt_id="broker-cnf-dims", **identity_kwargs),
        quantity_step="1",
        allow_short=True,
        log=False,
    )

    assert result["matched"] == [
        "rb2601|long|today",
        "rb2601|short|yesterday",
    ]


def test_reconcile_rejects_stale_or_cross_generation_snapshots() -> None:
    with pytest.raises(ValueError, match="observation-time skew"):
        reconcile(
            [],
            [],
            system_identity=_identity(
                receipt_id="system-stale", observed_at="2026-07-20T01:00:00Z"
            ),
            broker_identity=_identity(
                receipt_id="broker-stale", observed_at="2026-07-20T01:05:00Z"
            ),
            quantity_step="1",
            log=False,
        )

    with pytest.raises(ValueError, match="identity mismatch"):
        reconcile(
            [],
            [],
            system_identity=_identity(receipt_id="system-gen", generation=1),
            broker_identity=_identity(receipt_id="broker-gen", generation=2),
            quantity_step="1",
            log=False,
        )


def test_reconcile_market_value_difference_is_not_silently_matched() -> None:
    result = reconcile(
        [{"instrument_id": "600000.SH", "quantity": 100, "market_value": 1000}],
        [{"instrument_id": "600000.SH", "quantity": 100, "market_value": 1000.02}],
        system_identity=_identity(receipt_id="system-006"),
        broker_identity=_identity(receipt_id="broker-006"),
        quantity_step="1",
        value_tolerance="0.01",
        log=False,
    )

    assert result["summary"]["passed"] is False
    assert result["mismatches"][0]["type"] == "market_value_diff"


@pytest.mark.parametrize("quantity", ["0.0015", "-0.001", True])
def test_reconcile_rejects_invalid_quantity_for_declared_step(quantity: object) -> None:
    with pytest.raises(ValueError, match="quantity"):
        reconcile(
            [{"instrument_id": "BTCUSDT", "quantity": quantity, "market_value": 1}],
            [],
            system_identity=_identity(
                market="crypto",
                account_id="crypto-paper-001",
                authority_id=CRYPTO_CAPITAL_AUTHORITY_ID,
                broker_contract="tradingagent.crypto.paper_broker.v1",
                receipt_id="system-004",
            ),
            broker_identity=_identity(
                market="crypto",
                account_id="crypto-paper-001",
                authority_id=CRYPTO_CAPITAL_AUTHORITY_ID,
                broker_contract="tradingagent.crypto.paper_broker.v1",
                receipt_id="broker-004",
            ),
            quantity_step="0.001",
            log=False,
        )
