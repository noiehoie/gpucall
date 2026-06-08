from __future__ import annotations

from gpucall.costing import estimate_tuple_cost
from gpucall.domain import CostPolicy, ExecutionTupleSpec, Policy, Recipe


def effective_cost_policy(policy: Policy, recipe: Recipe) -> CostPolicy:
    base = policy.cost_policy
    override = recipe.cost_policy
    if override is None:
        return base
    return CostPolicy(
        max_estimated_cost_usd=override.max_estimated_cost_usd
        if override.max_estimated_cost_usd is not None
        else base.max_estimated_cost_usd,
        max_cold_start_cost_usd=override.max_cold_start_cost_usd
        if override.max_cold_start_cost_usd is not None
        else base.max_cold_start_cost_usd,
        max_idle_cost_usd=override.max_idle_cost_usd
        if override.max_idle_cost_usd is not None
        else base.max_idle_cost_usd,
        require_budget_for_high_cost_tuple=override.require_budget_for_high_cost_tuple
        if override.require_budget_for_high_cost_tuple is not None
        else base.require_budget_for_high_cost_tuple,
        high_cost_threshold_usd=override.high_cost_threshold_usd
        if override.high_cost_threshold_usd is not None
        else base.high_cost_threshold_usd,
        require_fresh_price_for_budget=override.require_fresh_price_for_budget
        if override.require_fresh_price_for_budget is not None
        else base.require_fresh_price_for_budget,
    )


def tuple_cost_policy_rejection_reason(
    *,
    policy: Policy,
    tuple: ExecutionTupleSpec,
    recipe: Recipe,
    timeout_seconds: int,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> tuple[str | None, dict[str, float | int | str]]:
    estimate = estimate_tuple_cost(
        tuple,
        recipe,
        timeout_seconds=timeout_seconds,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )
    cost_policy = effective_cost_policy(policy, recipe)
    explicit_budget = any(
        value is not None
        for value in (
            cost_policy.max_estimated_cost_usd,
            cost_policy.max_cold_start_cost_usd,
            cost_policy.max_idle_cost_usd,
        )
    )
    if cost_policy.max_cold_start_cost_usd is not None and estimate["cold_start_cost_usd"] > float(cost_policy.max_cold_start_cost_usd):
        return (
            f"estimated cold start cost {estimate['cold_start_cost_usd']:.4f} exceeds "
            f"max_cold_start_cost_usd {float(cost_policy.max_cold_start_cost_usd):.4f}",
            estimate,
        )
    if cost_policy.max_idle_cost_usd is not None and estimate["idle_cost_usd"] > float(cost_policy.max_idle_cost_usd):
        return (
            f"estimated idle cost {estimate['idle_cost_usd']:.4f} exceeds "
            f"max_idle_cost_usd {float(cost_policy.max_idle_cost_usd):.4f}",
            estimate,
        )
    if cost_policy.max_estimated_cost_usd is not None and estimate["estimated_cost_usd"] > float(cost_policy.max_estimated_cost_usd):
        return (
            f"estimated cost {estimate['estimated_cost_usd']:.4f} exceeds "
            f"max_estimated_cost_usd {float(cost_policy.max_estimated_cost_usd):.4f}",
            estimate,
        )
    require_budget = cost_policy.require_budget_for_high_cost_tuple
    if require_budget is None:
        require_budget = True
    threshold = cost_policy.high_cost_threshold_usd
    if threshold is None:
        threshold = 5.0
    high_cost_without_explicit_budget = require_budget and not explicit_budget and estimate["estimated_cost_usd"] > float(threshold)
    if high_cost_without_explicit_budget:
        return (
            f"estimated cost {estimate['estimated_cost_usd']:.4f} exceeds high_cost_threshold_usd "
            f"{float(threshold):.4f} without explicit budget",
            estimate,
        )
    if cost_policy.require_fresh_price_for_budget and estimate.get("price_freshness") != "fresh" and explicit_budget:
        return f"tuple price is {estimate.get('price_freshness')} under strict budget policy", estimate
    return None, estimate
