"""Shadow-only industry research contracts for the A-share Phase 1.5 slice."""

from .shadow_slice import (
    IndustryScoreAuthorityVerification,
    IndustryScoreAuthorityVerifier,
    IndustryShadowBasket,
    IndustryShadowContractError,
    IndustryShadowInput,
    build_industry_shadow_basket,
)

__all__ = [
    "IndustryScoreAuthorityVerification",
    "IndustryScoreAuthorityVerifier",
    "IndustryShadowBasket",
    "IndustryShadowContractError",
    "IndustryShadowInput",
    "build_industry_shadow_basket",
]
