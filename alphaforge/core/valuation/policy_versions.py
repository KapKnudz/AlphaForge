"""Canonical policy version composition — pure."""

FORWARD_SCENARIO_POLICY_VERSION = "forward-scenario-v6-market-cap-hurdle"
FORWARD_RANKING_POLICY_VERSION = "forward-ranking-v3-market-cap-hurdle"
COMPARATIVE_CONFIDENCE_POLICY_VERSION = "comparative-confidence-v1"
THESIS_CALIBRATION_POLICY_VERSION = "thesis-calibration-v2"
VERDICT_COHERENCE_POLICY_VERSION = "verdict-coherence-v1"
SCENARIO_DRIVER_ATTRIBUTION_POLICY_VERSION = "scenario-driver-attribution-shapley-v1"


def compose_policy_version(*versions: str) -> str:
    return "+".join(version for version in versions if version)


def comparative_ranking_policy_version() -> str:
    return compose_policy_version(
        FORWARD_RANKING_POLICY_VERSION,
        COMPARATIVE_CONFIDENCE_POLICY_VERSION,
    )
