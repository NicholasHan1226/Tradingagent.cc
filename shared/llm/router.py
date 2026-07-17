"""Configuration-only LLM model routing."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping


DEEPSEEK_PROVIDER = "deepseek"
DEEPSEEK_V4_FLASH_MODEL = "deepseek-v4-flash"
DEEPSEEK_V4_PRO_MODEL = "deepseek-v4-pro"
_NETWORK_AUTHORITY_SEAL = object()


@dataclass(frozen=True)
class ModelRoute:
    route: str
    provider: str
    model: str


class LLMRouter:
    """Route table only; fixture mappings never authorise provider egress."""

    __slots__ = (
        "_fixture_only",
        "_network_authority_seal",
        "_routes",
        "_sealed",
    )

    def __init__(
        self,
        routes: Mapping[str, ModelRoute] | None = None,
        *,
        fixture_only: bool,
    ) -> None:
        self._initialize(
            routes,
            fixture_only=fixture_only,
            network_authority_seal=None,
        )

    def _initialize(
        self,
        routes: Mapping[str, ModelRoute] | None,
        *,
        fixture_only: bool,
        network_authority_seal: object | None,
    ) -> None:
        if type(fixture_only) is not bool:
            raise TypeError("llm_router_authority_invalid")
        if fixture_only and network_authority_seal is not None:
            raise ValueError("fixture_router_cannot_authorize_network")
        if (
            network_authority_seal is not None
            and network_authority_seal is not _NETWORK_AUTHORITY_SEAL
        ):
            raise ValueError("llm_router_network_authority_invalid")
        canonical_routes = dict(routes or {})
        if not fixture_only and not self._is_validated_deepseek_v4(canonical_routes):
            raise ValueError("validated_deepseek_v4_routes_required")
        object.__setattr__(self, "_routes", MappingProxyType(canonical_routes))
        object.__setattr__(self, "_fixture_only", fixture_only)
        object.__setattr__(self, "_network_authority_seal", network_authority_seal)
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError("llm_router_immutable")
        object.__setattr__(self, name, value)

    def __delattr__(self, name: str) -> None:
        raise AttributeError("llm_router_immutable")

    @staticmethod
    def _is_validated_deepseek_v4(routes: Mapping[str, ModelRoute]) -> bool:
        expected = {
            "bulk_extraction": ModelRoute(
                route="bulk_extraction",
                provider=DEEPSEEK_PROVIDER,
                model=DEEPSEEK_V4_FLASH_MODEL,
            ),
            "slow_research": ModelRoute(
                route="slow_research",
                provider=DEEPSEEK_PROVIDER,
                model=DEEPSEEK_V4_PRO_MODEL,
            ),
        }
        return dict(routes) == expected

    @classmethod
    def from_offline_fixture_mapping(
        cls,
        config: Mapping[str, Mapping[str, Any]],
    ) -> "LLMRouter":
        """Build a non-authoritative router for frozen offline response tests."""

        return cls._from_mapping(
            config,
            fixture_only=True,
        )

    @classmethod
    def _from_validated_deepseek_v4_mapping(
        cls,
        config: Mapping[str, Mapping[str, Any]],
        *,
        network_authority_seal: object | None,
    ) -> "LLMRouter":
        """Build exact V4 routes bound to explicit provider network authority."""

        if (
            network_authority_seal is not None
            and network_authority_seal is not _NETWORK_AUTHORITY_SEAL
        ):
            raise ValueError("llm_router_network_authority_invalid")
        routes = cls._routes_from_mapping(config)
        candidate = object.__new__(cls)
        candidate._initialize(
            routes,
            fixture_only=False,
            network_authority_seal=network_authority_seal,
        )
        return candidate

    @classmethod
    def _from_mapping(
        cls,
        config: Mapping[str, Mapping[str, Any]],
        *,
        fixture_only: bool,
    ) -> "LLMRouter":
        return cls(cls._routes_from_mapping(config), fixture_only=fixture_only)

    @staticmethod
    def _routes_from_mapping(
        config: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, ModelRoute]:
        routes: dict[str, ModelRoute] = {}
        for route_name, raw in config.items():
            if not isinstance(raw, Mapping):
                continue
            provider = str(raw.get("provider") or "").strip()
            model = str(raw.get("model") or "").strip()
            route = str(route_name or "").strip()
            if route and provider and model:
                routes[route] = ModelRoute(route=route, provider=provider, model=model)
        return routes

    @property
    def fixture_only(self) -> bool:
        return self._fixture_only

    @property
    def validated_deepseek_v4(self) -> bool:
        return not self.fixture_only and self._is_validated_deepseek_v4(self._routes)

    @property
    def network_authorized(self) -> bool:
        return self._network_authority_seal is _NETWORK_AUTHORITY_SEAL

    def resolve(self, route: str) -> ModelRoute | None:
        return self._routes.get(str(route or "").strip())
