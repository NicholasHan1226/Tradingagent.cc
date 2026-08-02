from __future__ import annotations

import ast
from datetime import datetime, timezone
import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from Ashare import evidence_shadow_parity as parity
from Ashare.event_evidence import AshareEvidenceContractError
from Ashare.moneyflow_evidence import AshareMoneyflowEvidenceError
from shared.data.sharedsignals_v1 import CatalogEnvelope


CATALOG_VERSION = "catalog-v1"
DATASET_IDS = (
    "cn.dataset.anns_d",
    "cn.dataset.major_news",
    "cn.dataset.moneyflow",
    "cn.dataset.moneyflow_ths",
)
EVENT_DATASET_IDS = DATASET_IDS[:2]
MONEYFLOW_DATASET_IDS = DATASET_IDS[2:]
NOW = datetime(2026, 8, 1, 5, 0, tzinfo=timezone.utc)


def _catalog(*, duplicate: str | None = None) -> CatalogEnvelope:
    rows = [{"dataset_id": dataset_id} for dataset_id in DATASET_IDS]
    if duplicate is not None:
        rows.append({"dataset_id": duplicate})
    return CatalogEnvelope(
        api_version="v1",
        catalog_version=CATALOG_VERSION,
        request_id="catalog-fixture",
        data=tuple(rows),
    )


def _plan() -> parity.ShadowParityPlan:
    return parity.ShadowParityPlan(
        expected_catalog_version="catalog-expected",
        decision_time=NOW,
        allowed_symbols=("600000.SH",),
        filters_by_dataset={dataset_id: {} for dataset_id in DATASET_IDS},
    )


def _client(catalog: CatalogEnvelope, dataset_ids: frozenset[str]) -> MagicMock:
    client = MagicMock()
    client.config.catalog_version_policy = "evidence_only"
    client.config.dataset_ids = dataset_ids
    client.config.expected_catalog_version = "catalog-expected"
    client.get_catalog.return_value = catalog
    return client


def _install_success_fixtures(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[MagicMock, MagicMock]:
    event_profiles = {
        "cn.dataset.anns_d": SimpleNamespace(
            dataset_id="cn.dataset.anns_d",
            expected_catalog_version="catalog-expected",
            dataset_contract_fingerprint="e" * 64,
            consumer_profile_sha256="f" * 64,
            identity_fields=("ts_code", "ann_date"),
        ),
        "cn.dataset.major_news": SimpleNamespace(
            dataset_id="cn.dataset.major_news",
            expected_catalog_version="catalog-expected",
            dataset_contract_fingerprint="m" * 64,
            consumer_profile_sha256="n" * 64,
            identity_fields=("src", "pub_time", "title"),
        ),
    }
    flow_profiles = {
        dataset_id: SimpleNamespace(
            dataset_id=dataset_id,
            expected_catalog_version="catalog-expected",
            dataset_contract_fingerprint=("a" if dataset_id.endswith("ths") else "b")
            * 64,
            consumer_profile_sha256=("c" if dataset_id.endswith("ths") else "d") * 64,
            identity_fields=("trade_date", "ts_code"),
        )
        for dataset_id in MONEYFLOW_DATASET_IDS
    }
    monkeypatch.setattr(
        parity.EvidenceDatasetProfile,
        "from_catalog_row",
        classmethod(
            lambda cls, _catalog, row, **kwargs: event_profiles[row["dataset_id"]]
        ),
    )
    monkeypatch.setattr(
        parity.MoneyflowDatasetProfile,
        "from_catalog_row",
        classmethod(
            lambda cls, _catalog, row, **kwargs: flow_profiles[row["dataset_id"]]
        ),
    )

    event_port = MagicMock()
    event_port.load_event_snapshot.side_effect = [
        SimpleNamespace(
            row_count=1,
            page_count=1,
            same_observation=True,
            observed_catalog_version=CATALOG_VERSION,
        ),
        SimpleNamespace(
            row_count=1,
            page_count=1,
            same_observation=True,
            observed_catalog_version=CATALOG_VERSION,
        ),
    ]
    moneyflow_port = MagicMock()
    moneyflow_port.load_shadow_snapshot.side_effect = [
        SimpleNamespace(
            row_count=1,
            page_count=1,
            same_observation=True,
            observed_catalog_version=CATALOG_VERSION,
        ),
        SimpleNamespace(
            row_count=2,
            page_count=1,
            same_observation=True,
            observed_catalog_version=CATALOG_VERSION,
        ),
    ]
    monkeypatch.setattr(parity, "TradingDatasAshareEvidencePort", lambda _: event_port)
    monkeypatch.setattr(
        parity, "TradingDatasAshareMoneyflowPort", lambda _: moneyflow_port
    )
    return event_port, moneyflow_port


def test_parity_receipt_is_zero_notional_and_non_authoritative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event_port, _ = _install_success_fixtures(monkeypatch)

    receipt = parity.run_shadow_parity(
        _client(_catalog(), frozenset(EVENT_DATASET_IDS)),
        moneyflow_client=_client(_catalog(), frozenset(MONEYFLOW_DATASET_IDS)),
        plan=_plan(),
    )

    assert receipt["status"] == "pass"
    assert receipt["dataset_ids"] == list(DATASET_IDS)
    assert receipt["zero_notional_cny"] == 0
    assert receipt["candidate_authority"] is False
    assert receipt["training_authority"] is False
    assert receipt["execution_authority"] is False
    assert receipt["llm_network_used"] is False
    assert all(source["status"] == "accepted" for source in receipt["sources"].values())
    assert receipt["sources"]["cn.dataset.moneyflow"]["identity_fields"] == [
        "trade_date",
        "ts_code",
    ]
    assert receipt["sources"]["cn.dataset.major_news"]["identity_fields"] == [
        "src",
        "pub_time",
        "title",
    ]
    assert event_port.load_event_snapshot.call_args_list[0].kwargs[
        "allowed_symbols"
    ] == ("600000.SH",)
    assert (
        event_port.load_event_snapshot.call_args_list[1].kwargs["allowed_symbols"]
        is None
    )


def test_one_source_rejection_blocks_only_that_source_and_the_overall_parity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, moneyflow_port = _install_success_fixtures(monkeypatch)
    moneyflow_port.load_shadow_snapshot.side_effect = [
        AshareMoneyflowEvidenceError("ashare_moneyflow_receipt_missing"),
        SimpleNamespace(
            row_count=1,
            page_count=1,
            same_observation=True,
            observed_catalog_version=CATALOG_VERSION,
        ),
    ]
    monkeypatch.setattr(
        parity, "TradingDatasAshareMoneyflowPort", lambda _: moneyflow_port
    )

    receipt = parity.run_shadow_parity(
        _client(_catalog(), frozenset(EVENT_DATASET_IDS)),
        moneyflow_client=_client(_catalog(), frozenset(MONEYFLOW_DATASET_IDS)),
        plan=_plan(),
    )

    assert receipt["status"] == "blocked"
    assert receipt["sources"]["cn.dataset.moneyflow"]["status"] == "rejected"
    assert receipt["sources"]["cn.dataset.moneyflow"]["reason_code"] == (
        "ashare_moneyflow_receipt_missing"
    )
    assert receipt["sources"]["cn.dataset.moneyflow_ths"]["status"] == "accepted"
    assert receipt["execution_eligible"] is False


def test_macro_event_rejection_blocks_shadow_parity_without_stock_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event_port, _ = _install_success_fixtures(monkeypatch)
    event_port.load_event_snapshot.side_effect = [
        SimpleNamespace(
            row_count=1,
            page_count=1,
            same_observation=True,
            observed_catalog_version=CATALOG_VERSION,
        ),
        AshareEvidenceContractError("ashare_evidence_receipt_missing"),
    ]

    receipt = parity.run_shadow_parity(
        _client(_catalog(), frozenset(EVENT_DATASET_IDS)),
        moneyflow_client=_client(_catalog(), frozenset(MONEYFLOW_DATASET_IDS)),
        plan=_plan(),
    )

    assert receipt["status"] == "blocked"
    assert receipt["sources"]["cn.dataset.major_news"]["status"] == "rejected"
    assert receipt["sources"]["cn.dataset.major_news"]["reason_code"] == (
        "ashare_evidence_receipt_missing"
    )
    assert receipt["candidate_authority"] is False
    assert receipt["execution_authority"] is False


@pytest.mark.parametrize(
    ("catalog", "reason"),
    [
        (_catalog(duplicate="cn.dataset.moneyflow"), "shadow_parity_dataset_duplicate"),
        (
            CatalogEnvelope(
                api_version="v1",
                catalog_version=CATALOG_VERSION,
                request_id="missing-fixture",
                data=({"dataset_id": "cn.dataset.anns_d"},),
            ),
            "shadow_parity_dataset_missing",
        ),
    ],
)
def test_catalog_target_row_failure_happens_before_any_query(
    catalog: CatalogEnvelope,
    reason: str,
) -> None:
    client = _client(catalog, frozenset(EVENT_DATASET_IDS))

    with pytest.raises(parity.ShadowParityError, match=reason):
        parity.run_shadow_parity(
            client,
            moneyflow_client=_client(_catalog(), frozenset(MONEYFLOW_DATASET_IDS)),
            plan=_plan(),
        )

    assert client.get_catalog.call_count == 1


def test_plan_requires_exact_targets_evidence_only_and_aware_decision() -> None:
    with pytest.raises(parity.ShadowParityError, match="shadow_parity_dataset_scope"):
        parity.ShadowParityPlan(
            expected_catalog_version="catalog-expected",
            decision_time=NOW,
            allowed_symbols=("600000.SH",),
            filters_by_dataset={"cn.dataset.anns_d": {}},
        )

    client = _client(_catalog(), frozenset(EVENT_DATASET_IDS))
    client.config.catalog_version_policy = "strict"
    with pytest.raises(parity.ShadowParityError, match="shadow_parity_catalog_policy"):
        parity.run_shadow_parity(
            client,
            moneyflow_client=_client(_catalog(), frozenset(MONEYFLOW_DATASET_IDS)),
            plan=_plan(),
        )

    client = _client(_catalog(), frozenset(EVENT_DATASET_IDS))
    client.config.expected_catalog_version = "different-catalog"
    with pytest.raises(
        parity.ShadowParityError, match="shadow_parity_client_catalog_scope"
    ):
        parity.run_shadow_parity(
            client,
            moneyflow_client=_client(_catalog(), frozenset(MONEYFLOW_DATASET_IDS)),
            plan=_plan(),
        )

    with pytest.raises(
        parity.ShadowParityError, match="shadow_parity_client_dataset_scope"
    ):
        parity.run_shadow_parity(
            _client(_catalog(), frozenset(DATASET_IDS)),
            moneyflow_client=_client(_catalog(), frozenset(MONEYFLOW_DATASET_IDS)),
            plan=_plan(),
        )


def test_secret_free_manifest_preflight_never_needs_a_token(tmp_path: Path) -> None:
    manifest = tmp_path / "shadow-parity.json"
    manifest.write_text(
        """{
  "base_url": "http://127.0.0.1:18082",
  "expected_catalog_version": "catalog-expected",
  "access_policy_id": "ashare-shadow-fixture",
  "transport_id": "tradingdatas_v1",
  "timeout_seconds": 20,
  "decision_time": "2026-08-01T13:00:00+08:00",
  "allowed_symbols": ["600000.SH"],
  "filters_by_dataset": {
      "cn.dataset.anns_d": {},
      "cn.dataset.major_news": {},
      "cn.dataset.moneyflow": {},
    "cn.dataset.moneyflow_ths": {}
  }
}
""",
        encoding="utf-8",
    )

    assert parity.main(["--manifest", str(manifest), "--preflight", "--json"]) == 0


def test_manifest_rejects_nonformal_endpoint_before_transport(tmp_path: Path) -> None:
    manifest = tmp_path / "shadow-parity.json"
    manifest.write_text(
        """{
  "base_url": "https://provider.invalid",
  "expected_catalog_version": "catalog-expected",
  "access_policy_id": "ashare-shadow-fixture",
  "transport_id": "tradingdatas_v1",
  "timeout_seconds": 20,
  "decision_time": "2026-08-01T13:00:00+08:00",
  "allowed_symbols": ["600000.SH"],
  "filters_by_dataset": {
      "cn.dataset.anns_d": {},
      "cn.dataset.major_news": {},
      "cn.dataset.moneyflow": {},
    "cn.dataset.moneyflow_ths": {}
  }
}
""",
        encoding="utf-8",
    )

    with pytest.raises(
        parity.ShadowParityError, match="shadow_parity_base_url_invalid"
    ):
        parity.load_shadow_parity_config(manifest)


def test_module_has_no_duplicate_literal_dictionary_keys() -> None:
    source = inspect.getsource(parity)
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Dict):
            continue
        keys = [
            key.value
            for key in node.keys
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        ]
        assert len(keys) == len(set(keys))
