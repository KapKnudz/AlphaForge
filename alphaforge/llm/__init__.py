"""Optional model sidecars; deterministic outputs remain outside this boundary."""

from alphaforge.llm.jev_shadow import (
    CITATION_QUESTION_VERSION,
    CITATION_RELATION_CLASSES,
    CRITERIA_VERSION,
    MISSING_INFORMATION_CLASSES,
    MISSING_INFORMATION_QUESTION_VERSION,
    PINNED_MODEL_VERSION,
    VARIANT_RELATION_CLASSES,
    VARIANT_RELATION_QUESTION_VERSION,
    JevShadowBudget,
    JevShadowConfig,
    JevShadowResult,
    JevShadowSidecar,
    accept_variant_relation_decision,
    run_shadow_signals,
)

__all__ = [
    "CITATION_RELATION_CLASSES",
    "CITATION_QUESTION_VERSION",
    "CRITERIA_VERSION",
    "JevShadowBudget",
    "JevShadowConfig",
    "JevShadowResult",
    "JevShadowSidecar",
    "MISSING_INFORMATION_CLASSES",
    "MISSING_INFORMATION_QUESTION_VERSION",
    "PINNED_MODEL_VERSION",
    "VARIANT_RELATION_CLASSES",
    "VARIANT_RELATION_QUESTION_VERSION",
    "accept_variant_relation_decision",
    "run_shadow_signals",
]
