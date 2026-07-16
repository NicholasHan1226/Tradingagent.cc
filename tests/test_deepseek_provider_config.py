from __future__ import annotations

import pytest

from shared.llm.deepseek_config import (
    DeepSeekProviderConfig,
    DeepSeekProviderConfigError,
)
from shared.llm.gateway import LLMEvidenceGateway


def _environment(**overrides: str) -> dict[str, str]:
    values = {
        "TRADINGAGENT_LLM_PROVIDER": "deepseek",
        "TRADINGAGENT_LLM_BASE_URL": "https://api.deepseek.com",
        "TRADINGAGENT_LLM_API_KEY_ENV": "DEEPSEEK_API_KEY",
        "TRADINGAGENT_LLM_FLASH_MODEL": "deepseek-v4-flash",
        "TRADINGAGENT_LLM_PRO_MODEL": "deepseek-v4-pro",
        "TRADINGAGENT_LLM_NETWORK_ENABLED": "false",
    }
    values.update(overrides)
    return values


def test_official_v4_configuration_routes_roles_without_reading_secret() -> None:
    environment = _environment(DEEPSEEK_API_KEY="must-never-enter-config")

    config = DeepSeekProviderConfig.from_environment(environment)

    assert config.base_url == "https://api.deepseek.com"
    assert config.chat_completions_url == ("https://api.deepseek.com/chat/completions")
    assert config.api_key_env == "DEEPSEEK_API_KEY"
    assert config.network_enabled is False
    assert config.router().resolve("bulk_extraction").model == "deepseek-v4-flash"
    assert config.router().resolve("slow_research").model == "deepseek-v4-pro"
    descriptor = config.to_public_descriptor()
    assert descriptor == {
        "provider": "deepseek",
        "base_url": "https://api.deepseek.com",
        "api_key_env": "DEEPSEEK_API_KEY",
        "flash_model": "deepseek-v4-flash",
        "pro_model": "deepseek-v4-pro",
        "network_enabled": False,
        "transport_state": "not_installed",
    }
    assert "must-never-enter-config" not in repr(config)
    assert "must-never-enter-config" not in repr(descriptor)


@pytest.mark.parametrize(
    "overrides,reason",
    (
        ({"TRADINGAGENT_LLM_BASE_URL": "http://api.deepseek.com"}, "base_url"),
        (
            {"TRADINGAGENT_LLM_BASE_URL": "https://key@api.deepseek.com"},
            "base_url",
        ),
        (
            {"TRADINGAGENT_LLM_BASE_URL": "https://api.deepseek.com/beta"},
            "base_url",
        ),
        ({"TRADINGAGENT_LLM_FLASH_MODEL": "deepseek-chat"}, "legacy_model"),
        ({"TRADINGAGENT_LLM_PRO_MODEL": "deepseek-reasoner"}, "legacy_model"),
        ({"TRADINGAGENT_LLM_NETWORK_ENABLED": "true"}, "network_transport"),
        ({"TRADINGAGENT_LLM_API_KEY_ENV": "sk-secret-value"}, "api_key_env"),
        (
            {"TRADINGAGENT_LLM_API_KEY_ENV": "AWS_SECRET_ACCESS_KEY"},
            "api_key_env",
        ),
    ),
)
def test_unsafe_or_legacy_configuration_fails_closed(
    overrides: dict[str, str],
    reason: str,
) -> None:
    with pytest.raises(DeepSeekProviderConfigError, match=reason):
        DeepSeekProviderConfig.from_environment(_environment(**overrides))


def test_model_roles_are_exact_and_not_interchangeable() -> None:
    with pytest.raises(DeepSeekProviderConfigError, match="flash_model"):
        DeepSeekProviderConfig.from_environment(
            _environment(TRADINGAGENT_LLM_FLASH_MODEL="deepseek-v4-pro")
        )
    with pytest.raises(DeepSeekProviderConfigError, match="pro_model"):
        DeepSeekProviderConfig.from_environment(
            _environment(TRADINGAGENT_LLM_PRO_MODEL="deepseek-v4-flash")
        )


def test_default_gateway_uses_strict_public_config_and_rejects_network_enablement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key, value in _environment().items():
        monkeypatch.setenv(key, value)

    gateway = LLMEvidenceGateway()

    assert gateway.router.resolve("bulk_extraction").model == "deepseek-v4-flash"
    assert gateway.router.resolve("slow_research").model == "deepseek-v4-pro"

    monkeypatch.setenv("TRADINGAGENT_LLM_NETWORK_ENABLED", "true")
    with pytest.raises(DeepSeekProviderConfigError, match="network_transport"):
        LLMEvidenceGateway()


def test_router_has_no_independent_environment_bypass() -> None:
    from types import MappingProxyType

    from shared.llm.router import LLMRouter, ModelRoute

    assert not hasattr(LLMRouter, "from_environment")
    with pytest.raises(ValueError, match="validated_deepseek_v4_routes_required"):
        LLMRouter(
            {
                "slow_research": ModelRoute(
                    route="slow_research",
                    provider="deepseek",
                    model="arbitrary-unvalidated-model",
                )
            },
            fixture_only=False,
        )

    strict_router = DeepSeekProviderConfig.from_environment(_environment()).router()
    with pytest.raises(AttributeError, match="immutable"):
        strict_router._routes = {}  # type: ignore[attr-defined]
    with pytest.raises(AttributeError, match="immutable"):
        del strict_router._sealed  # type: ignore[attr-defined]

    object.__setattr__(
        strict_router,
        "_routes",
        MappingProxyType(
            {
                "slow_research": ModelRoute(
                    route="slow_research",
                    provider="deepseek",
                    model="arbitrary-unvalidated-model",
                )
            }
        ),
    )
    with pytest.raises(TypeError, match="llm_router_policy_rejected"):
        LLMEvidenceGateway(router=strict_router)

    runtime_router = DeepSeekProviderConfig.from_environment(_environment()).router()
    gateway = LLMEvidenceGateway(router=runtime_router)
    object.__setattr__(
        runtime_router,
        "_routes",
        MappingProxyType(
            {
                "slow_research": ModelRoute(
                    route="slow_research",
                    provider="deepseek",
                    model="arbitrary-unvalidated-model",
                )
            }
        ),
    )
    assert gateway._router_policy_valid() is False
