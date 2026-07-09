from __future__ import annotations

from shared.runtime_test import sharedsignals_evidence_contract as contract


def test_contract_passes_and_records_empty_evidence_debt(monkeypatch):
    def fake_fetch(base_url, endpoint, params, timeout):  # noqa: ANN001
        if endpoint == "/sentiment":
            return []
        if endpoint in {"/macro", "/capital_flow"}:
            return [{"factor_name": "repo_daily:close", "value": 1.4}]
        if endpoint == "/events":
            return [{"event_time": "20260709", "event_type": "announcement"}]
        raise AssertionError(endpoint)

    monkeypatch.setattr(contract, "_fetch", fake_fetch)

    report = contract.run_contract_check(api_url="http://ss", as_of="20260709")

    assert report["overall_status"] == "pass"
    assert report["evidence_debt_count"] == 1
    assert report["evidence_debts"][0]["name"] == "sentiment"


def test_contract_strict_empty_marks_warn(monkeypatch):
    monkeypatch.setattr(contract, "_fetch", lambda *args, **kwargs: [])

    report = contract.run_contract_check(api_url="http://ss", as_of="20260709", strict_empty=True)

    assert report["overall_status"] == "warn"
    assert report["evidence_debt_count"] == 4


def test_contract_fails_on_schema_gap(monkeypatch):
    def fake_fetch(base_url, endpoint, params, timeout):  # noqa: ANN001
        if endpoint == "/macro":
            return [{"factor_name": "repo_daily:close"}]
        return [{"event_time": "20260709", "event_type": "announcement"}]

    monkeypatch.setattr(contract, "_fetch", fake_fetch)

    report = contract.run_contract_check(api_url="http://ss", as_of="20260709")

    assert report["overall_status"] == "fail"
    macro = next(check for check in report["checks"] if check["name"] == "macro")
    assert macro["summary"] == "schema_missing_required_keys"
    assert macro["required_keys_missing"] == ["value"]
