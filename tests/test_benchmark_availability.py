from __future__ import annotations

from shared.review import benchmark
from shared.review.benchmark import compare_to_benchmark


def test_unavailable_benchmark_propagates_null_instead_of_fake_zero_alpha() -> None:
    result = compare_to_benchmark(0.01, None)

    assert result["status"] == "unavailable"
    assert result["benchmark_return"] is None
    assert result["alpha"] is None
    assert result["excess_return"] is None
    assert result["beat_benchmark"] is None


def test_explicit_flat_benchmark_remains_a_real_zero_observation() -> None:
    result = compare_to_benchmark(0.01, 0.0)

    assert result["status"] == "available"
    assert result["benchmark_return"] == 0.0
    assert result["alpha"] == 0.01
    assert result["beat_benchmark"] is True


def test_get_benchmark_keeps_unavailable_index_return_null(monkeypatch) -> None:
    monkeypatch.setattr(
        benchmark,
        "_read_index_return",
        lambda date, symbols, label: (None, "sharedsignals_unavailable:%s" % label),
    )

    result = benchmark.get_benchmark("20260713")

    assert result["csi300_return"] is None
    assert result["csi300_status"] == "unavailable"
    assert result["benchmark_status"] == "unavailable_csi300_evidence"
