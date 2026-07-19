"""Public, secret-free DeepSeek V4 routing configuration.

The network implementation lives in :mod:`shared.llm.providers.deepseek_http`.
This object never reads or stores credential material.  An ambient environment
flag is deliberately insufficient to enable egress: the caller must also pass
an explicit in-process authority when constructing a network candidate.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping

from .router import (
    DEEPSEEK_PROVIDER,
    DEEPSEEK_V4_FLASH_MODEL,
    DEEPSEEK_V4_PRO_MODEL,
    LLMRouter,
    _NETWORK_AUTHORITY_SEAL,
)


OFFICIAL_DEEPSEEK_OPENAI_BASE_URL = "https://api.deepseek.com"
_LEGACY_MODELS = frozenset({"deepseek-chat", "deepseek-reasoner"})
_API_KEY_ENV = "DEEPSEEK_API_KEY"
_PROVIDER_NETWORK_AUTHORITY_SEAL = object()


class DeepSeekProviderConfigError(ValueError):
    """Raised when provider configuration could create an ambiguous egress."""


def _strict_text(value: object, *, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\x00" in value
    ):
        raise DeepSeekProviderConfigError(f"{field_name}_invalid")
    return value


def _network_flag(value: object, *, allow_network_transport: bool) -> bool:
    normalized = str(value or "false").strip().casefold()
    if normalized in {"false", "0", "no", "off"}:
        return False
    if normalized in {"true", "1", "yes", "on"}:
        if allow_network_transport is not True:
            raise DeepSeekProviderConfigError(
                "network_transport_requires_explicit_authorization"
            )
        return True
    raise DeepSeekProviderConfigError("network_enabled_invalid")


@dataclass(frozen=True)
class DeepSeekProviderConfig:
    """Validated V4 route configuration with no credential material."""

    provider: str
    base_url: str
    api_key_env: str
    flash_model: str
    pro_model: str
    network_enabled: bool = False
    transport_state: str = "candidate_installed_network_disabled"
    _network_authority_seal: object | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if _strict_text(self.provider, field_name="provider") != DEEPSEEK_PROVIDER:
            raise DeepSeekProviderConfigError("provider_must_be_deepseek")
        if (
            _strict_text(self.base_url, field_name="base_url")
            != OFFICIAL_DEEPSEEK_OPENAI_BASE_URL
        ):
            raise DeepSeekProviderConfigError("base_url_not_official_openai_endpoint")
        if _strict_text(self.api_key_env, field_name="api_key_env") != _API_KEY_ENV:
            raise DeepSeekProviderConfigError("api_key_env_invalid")
        flash = _strict_text(self.flash_model, field_name="flash_model")
        pro = _strict_text(self.pro_model, field_name="pro_model")
        if flash in _LEGACY_MODELS or pro in _LEGACY_MODELS:
            raise DeepSeekProviderConfigError("legacy_model_forbidden")
        if flash != DEEPSEEK_V4_FLASH_MODEL:
            raise DeepSeekProviderConfigError("flash_model_role_invalid")
        if pro != DEEPSEEK_V4_PRO_MODEL:
            raise DeepSeekProviderConfigError("pro_model_role_invalid")
        if type(self.network_enabled) is not bool:
            raise DeepSeekProviderConfigError("network_enabled_invalid")
        if self.network_enabled:
            if self._network_authority_seal is not _PROVIDER_NETWORK_AUTHORITY_SEAL:
                raise DeepSeekProviderConfigError(
                    "network_transport_requires_explicit_authorization"
                )
        elif self._network_authority_seal is not None:
            raise DeepSeekProviderConfigError("network_authority_state_invalid")
        expected_transport_state = (
            "candidate_enabled"
            if self.network_enabled
            else "candidate_installed_network_disabled"
        )
        if self.transport_state != expected_transport_state:
            raise DeepSeekProviderConfigError("transport_state_invalid")

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        allow_network_transport: bool = False,
    ) -> "DeepSeekProviderConfig":
        """Read public routing values only; never read the credential value."""

        source = os.environ if environ is None else environ
        network_enabled = _network_flag(
            source.get("TRADINGAGENT_LLM_NETWORK_ENABLED", "false"),
            allow_network_transport=allow_network_transport,
        )
        return cls(
            provider=str(source.get("TRADINGAGENT_LLM_PROVIDER", DEEPSEEK_PROVIDER)),
            base_url=str(
                source.get(
                    "TRADINGAGENT_LLM_BASE_URL",
                    OFFICIAL_DEEPSEEK_OPENAI_BASE_URL,
                )
            ),
            api_key_env=str(
                source.get("TRADINGAGENT_LLM_API_KEY_ENV", "DEEPSEEK_API_KEY")
            ),
            flash_model=str(
                source.get(
                    "TRADINGAGENT_LLM_FLASH_MODEL",
                    DEEPSEEK_V4_FLASH_MODEL,
                )
            ),
            pro_model=str(
                source.get(
                    "TRADINGAGENT_LLM_PRO_MODEL",
                    DEEPSEEK_V4_PRO_MODEL,
                )
            ),
            network_enabled=network_enabled,
            transport_state=(
                "candidate_enabled"
                if network_enabled
                else "candidate_installed_network_disabled"
            ),
            _network_authority_seal=(
                _PROVIDER_NETWORK_AUTHORITY_SEAL if network_enabled else None
            ),
        )

    @property
    def chat_completions_url(self) -> str:
        return f"{self.base_url}/chat/completions"

    def router(self) -> LLMRouter:
        if self.network_enabled:
            if (
                self.transport_state != "candidate_enabled"
                or self._network_authority_seal is not _PROVIDER_NETWORK_AUTHORITY_SEAL
            ):
                raise DeepSeekProviderConfigError("network_authority_state_invalid")
            router_authority_seal: object | None = _NETWORK_AUTHORITY_SEAL
        else:
            if (
                self.transport_state != "candidate_installed_network_disabled"
                or self._network_authority_seal is not None
            ):
                raise DeepSeekProviderConfigError("network_authority_state_invalid")
            router_authority_seal = None
        return LLMRouter._from_validated_deepseek_v4_mapping(
            {
                "bulk_extraction": {
                    "provider": self.provider,
                    "model": self.flash_model,
                },
                "slow_research": {
                    "provider": self.provider,
                    "model": self.pro_model,
                },
            },
            network_authority_seal=router_authority_seal,
        )

    def to_public_descriptor(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "base_url": self.base_url,
            "api_key_env": self.api_key_env,
            "flash_model": self.flash_model,
            "pro_model": self.pro_model,
            "network_enabled": self.network_enabled,
            "transport_state": self.transport_state,
        }


__all__ = [
    "DEEPSEEK_V4_FLASH_MODEL",
    "DEEPSEEK_V4_PRO_MODEL",
    "DeepSeekProviderConfig",
    "DeepSeekProviderConfigError",
    "OFFICIAL_DEEPSEEK_OPENAI_BASE_URL",
]
