"""Project the approved TA SharedSignals probe into a compatibility expectation.

This helper only maps an explicit TradingAgent probe configuration into the
shape consumed by :mod:`shared.runtime_test.integration_readiness_gate`.  It does
not run the probe, authenticate its origin, authorize capital writes, or reach
SharedSignals.
"""

from __future__ import annotations

from shared.runtime_test.integration_readiness_gate import (
    DatasetReadinessExpectation,
    IntegrationReadinessError,
    IntegrationReadinessExpectation,
)
from shared.runtime_test.sharedsignals_v1_integration_probe import (
    SharedSignalsIntegrationProbeConfig,
)


def readiness_expectation_from_probe_config(
    config: SharedSignalsIntegrationProbeConfig,
) -> IntegrationReadinessExpectation:
    """Return the exact non-authoritative expectation for one probe config."""

    if type(config) is not SharedSignalsIntegrationProbeConfig:
        raise IntegrationReadinessError("probe_config_invalid")
    return IntegrationReadinessExpectation(
        profile_id=config.profile_id,
        as_of=config.as_of,
        base_url=config.to_client_config().base_url,
        access_policy_id=config.access_policy_id,
        catalog_version=config.catalog_version,
        transport_id=config.transport_id,
        manifest_sha256=config.manifest_sha256,
        authority_sha256=config.authority_sha256,
        datasets=tuple(
            DatasetReadinessExpectation(
                probe_role=spec.probe_role,
                dataset_id=spec.dataset_id,
                schema_major=spec.schema_major,
                requirement_role=spec.requirement_role,
                query_sha256=spec.query(as_of=config.as_of).sha256,
                degraded_action=spec.degraded_action.value,
                stale_action=spec.stale_action.value,
                degraded_weight=float(spec.degraded_weight),
                stale_weight=float(spec.stale_weight),
            )
            for spec in config.datasets
        ),
    )


__all__ = ["readiness_expectation_from_probe_config"]
