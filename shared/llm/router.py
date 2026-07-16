"""Configuration-only LLM model routing."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping


DEEPSEEK_PROVIDER = "deepseek"
DEEPSEEK_V4_FLASH_MODEL = "deepseek-v4-flash"
DEEPSEEK_V4_PRO_MODEL = "deepseek-v4-pro"


@dataclass(frozen=True)
class ModelRoute:
    route: str
    provider: str
    model: str


class LLMRouter:
    """Route table only; fixture mappings never authorise provider egress."""

    __slots__ = ("_fixture_only", "_routes", "_sealed")

    def __init__(
        self,
        routes: Mapping[str, ModelRoute] | None = None,
        *,
        fixture_only: bool,
    ) -> None:
        canonical_routes = dict(routes or {})
        if not fixture_only and not self._is_validated_deepseek_v4(canonical_routes):
            raise ValueError("validated_deepseek_v4_routes_required")
        object.__setattr__(self, "_routes", MappingProxyType(canonical_routes))
        object.__setattr__(self, "_fixture_only", fixture_only)
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

        return cls._from_mapping(config, fixture_only=True)

    @classmethod
    def _from_validated_deepseek_v4_mapping(
        cls,
        config: Mapping[str, Mapping[str, Any]],
    ) -> "LLMRouter":
        """Build the exact validated V4 route set, never raw environment."""

        return cls._from_mapping(config, fixture_only=False)

    @classmethod
    def _from_mapping(
        cls,
        config: Mapping[str, Mapping[str, Any]],
        *,
        fixture_only: bool,
    ) -> "LLMRouter":
        routes: dict[str, ModelRoute] = {}
        for route_name, raw in config.items():
            if not isinstance(raw, Mapping):
                continue
            provider = str(raw.get("provider") or "").strip()
            model = str(raw.get("model") or "").strip()
            route = str(route_name or "").strip()
            if route and provider and model:
                routes[route] = ModelRoute(route=route, provider=provider, model=model)
        return cls(routes, fixture_only=fixture_only)

    @property
    def fixture_only(self) -> bool:
        return self._fixture_only

    @property
    def validated_deepseek_v4(self) -> bool:
        return not self.fixture_only and self._is_validated_deepseek_v4(self._routes)

    def resolve(self, route: str) -> ModelRoute | None:
        return self._routes.get(str(route or "").strip())
