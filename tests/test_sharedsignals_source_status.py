from __future__ import annotations

from urllib.error import URLError
from pathlib import Path

from shared.runtime_test import sharedsignals_source_status


ROOT = Path(__file__).resolve().parents[1]


def _http_payload(payload: dict):
    def _inner(_url: str, _timeout: float) -> tuple[int, dict]:
        return 200, payload

    return _inner


def _http_payloads_by_path(payloads: dict[str, dict]):
    def _inner(url: str, _timeout: float) -> tuple[int, dict]:
        for suffix, payload in payloads.items():
            if url.endswith(suffix):
                return 200, payload
        raise AssertionError(f"unexpected url {url}")

    return _inner


def test_green_source_status_is_ok() -> None:
    result = sharedsignals_source_status.check_source_status(
        "http://ss.local",
        http_json_func=_http_payload({"data": {"status": "green", "summary": {"red_checks": 0}}}),
    )

    assert result["status"] == "ok"
    assert result["source_status"] == "green"
    assert result["blocking"] is False


def test_yellow_source_status_degrades_but_does_not_block() -> None:
    result = sharedsignals_source_status.check_source_status(
        "http://ss.local",
        http_json_func=_http_payload({"data": {"status": "yellow", "summary": {"yellow_checks": 1}}}),
    )

    assert result["status"] == "degraded"
    assert result["source_status"] == "yellow"
    assert result["blocking"] is False


def test_red_source_status_blocks_trading() -> None:
    result = sharedsignals_source_status.check_source_status(
        "http://ss.local",
        http_json_func=_http_payload({"data": {"status": "red", "summary": {"red_checks": 1}}}),
    )

    assert result["status"] == "critical"
    assert result["source_status"] == "red"
    assert result["blocking"] is True


def test_market_unrelated_red_source_status_degrades_without_blocking() -> None:
    result = sharedsignals_source_status.check_source_status(
        "http://ss.local",
        market="ashare",
        http_json_func=_http_payload(
            {
                "data": {
                    "status": "red",
                    "summary": {"red_checks": 1},
                    "checks": [
                        {
                            "name": "health_sla_summary",
                            "status": "red",
                            "evidence": {
                                "violations": [
                                    {"market": "Crypto", "status": "breached"},
                                    {"market": "PM", "status": "breached"},
                                ]
                            },
                        }
                    ],
                }
            }
        ),
    )

    assert result["status"] == "degraded"
    assert result["blocking"] is False
    assert result["reason"] == "source_status_red_unrelated_to_market"


def test_health_sla_red_enriches_market_from_health_endpoint() -> None:
    result = sharedsignals_source_status.check_source_status(
        "http://ss.local",
        market="ashare",
        http_json_func=_http_payloads_by_path(
            {
                "/source_status": {
                    "data": {
                        "status": "red",
                        "checks": [
                            {
                                "name": "health_sla_summary",
                                "status": "red",
                                "evidence": {"summary": {"critical": 2}},
                            }
                        ],
                    }
                },
                "/health": {
                    "checks": {
                        "sla": {
                            "violations": [
                                {"market": "Crypto", "table": "market_bars_intraday"},
                                {"market": "PM", "table": "market_pm_prices"},
                            ]
                        }
                    }
                },
            }
        ),
    )

    assert result["status"] == "degraded"
    assert result["blocking"] is False
    assert result["health_enrichment"]["violation_markets"] == ["crypto", "pm"]


def test_market_related_red_source_status_blocks_trading() -> None:
    result = sharedsignals_source_status.check_source_status(
        "http://ss.local",
        market="pm",
        http_json_func=_http_payload(
            {
                "data": {
                    "status": "red",
                    "summary": {"red_checks": 1},
                    "checks": [
                        {
                            "name": "health_sla_summary",
                            "status": "red",
                            "evidence": {"violations": [{"market": "PM", "status": "breached"}]},
                        }
                    ],
                }
            }
        ),
    )

    assert result["status"] == "critical"
    assert result["blocking"] is True
    assert result["blocking_checks"] == ["health_sla_summary"]


def test_unreachable_source_status_blocks_trading() -> None:
    def fail(_url: str, _timeout: float) -> tuple[int, dict]:
        raise URLError("connection refused")

    result = sharedsignals_source_status.check_source_status("http://ss.local", http_json_func=fail)

    assert result["status"] == "critical"
    assert result["blocking"] is True
    assert "connection refused" in result["error"]


def test_health_check_reads_sharedsignals_source_status() -> None:
    text = (ROOT / "cron/health_check.sh").read_text(encoding="utf-8")

    assert "shared.runtime_test.sharedsignals_source_status import check_source_status" in text
    assert "source_status = check_source_status" in text
    assert 'source_status["status"] == "critical"' in text


def test_sim_wrappers_run_sharedsignals_source_gate_before_execution() -> None:
    wrappers = [
        "shared/wrappers/job_ashare_sim_exec.sh",
        "shared/wrappers/job_cn_futures_sim.sh",
        "shared/wrappers/job_crypto_sim.sh",
        "shared/wrappers/job_pm_sim.sh",
        "shared/wrappers/job_us_sim.sh",
    ]

    for wrapper in wrappers:
        text = (ROOT / wrapper).read_text(encoding="utf-8")
        assert "sharedsignals_source_gate" in text, wrapper
        assert "--market" in (ROOT / "shared/wrappers/_common.sh").read_text(encoding="utf-8")
