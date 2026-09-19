from __future__ import annotations

from dataclasses import asdict

from alphaforge.core.ranking.score_rules import (
    _balance_metrics_definitions,
    _growth_metrics_definitions,
    _quality_metrics_definitions,
    _valuation_metrics_definitions,
)


def build_general_scoring_audit(
    financial,
    valuation,
    fundamental_kpis,
    category_results: dict[str, dict],
    category_weights: tuple[float, float, float, float],
) -> dict:
    """Reconstruct general-model scoring using the same definitions as production."""
    fundamental_kpis = fundamental_kpis or {}

    # Map category names to their metric definition functions
    categories_metrics = {
        "quality": _quality_metrics_definitions(financial, fundamental_kpis) if financial else [],
        "growth": _growth_metrics_definitions(financial) if financial else [],
        "valuation": _valuation_metrics_definitions(
            valuation,
            financial.debt_to_equity if financial else None,
            category_results["quality"]["score"],
            category_results["growth"]["score"],
        )
        if valuation
        else [],
        "balance_sheet": _balance_metrics_definitions(financial, fundamental_kpis)
        if financial
        else [],
    }

    audit = {}
    for (category, metrics), outer_weight in zip(
        categories_metrics.items(), category_weights, strict=False
    ):
        results = [m.evaluate() for m in metrics]
        available_weight = sum(r.weight for r in results if r.score is not None)

        components = []
        reconstructed = 0.0

        # Determine weighting logic (equal vs weighted)
        is_weighted = category == "valuation"

        for r in results:
            if is_weighted:
                effective_weight = (
                    r.weight / available_weight if r.score is not None and available_weight else 0.0
                )
            else:
                # Equal weighting for other categories
                scores_count = sum(1 for res in results if res.score is not None and res.weight > 0)
                effective_weight = (
                    1.0 / scores_count
                    if r.score is not None and r.weight > 0 and scores_count
                    else 0.0
                )

            contribution = (r.score or 0.0) * effective_weight
            reconstructed += contribution

            component = asdict(r)
            component.update(
                {
                    "available": r.score is not None,
                    "effective_weight": effective_weight,
                    "category_contribution": contribution,
                    "total_contribution": contribution * outer_weight,
                }
            )
            components.append(component)

        production_score = category_results[category]["score"]
        audit[category] = {
            "production_score": production_score,
            "reconstructed_score": reconstructed,
            "reconstruction_error": reconstructed - production_score,
            "configured_available_weight": available_weight,
            "total_category_weight": outer_weight,
            "components": components,
        }

    return audit
