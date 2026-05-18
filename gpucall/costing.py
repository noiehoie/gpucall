from __future__ import annotations

from math import ceil
from typing import Any, Mapping

from gpucall.domain import ExecutionSurface, ExecutionTupleSpec, Recipe
from gpucall.price_freshness import tuple_configured_price_freshness


def estimate_tuple_cost(
    tuple_spec: ExecutionTupleSpec,
    recipe: Recipe,
    *,
    timeout_seconds: int,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> dict[str, float | int | str]:
    """Estimate provider cost without charging fixed/warm capacity to each request budget."""
    billing_model = _billing_model(tuple_spec)
    cold_start_seconds = float(tuple_spec.expected_cold_start_seconds or recipe.expected_cold_start_seconds or 0)
    runtime_seconds, runtime_source = _runtime_seconds(
        tuple_spec,
        recipe,
        timeout_seconds=timeout_seconds,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )
    idle_seconds = _idle_seconds(tuple_spec, billing_model)
    raw_billable_seconds = cold_start_seconds + runtime_seconds + idle_seconds
    billable_seconds = _rounded_billable_seconds(tuple_spec, raw_billable_seconds)

    cost_per_second = float(tuple_spec.cost_per_second)
    standing_cost_per_second = float(tuple_spec.standing_cost_per_second or 0)
    endpoint_cost_per_second = float(tuple_spec.endpoint_cost_per_second or 0)
    standing_cost_seconds = float(tuple_spec.standing_cost_window_seconds or 0)
    endpoint_cost_seconds = float(tuple_spec.endpoint_cost_window_seconds or 0)
    standing_cost_usd = standing_cost_per_second * standing_cost_seconds
    endpoint_cost_usd = endpoint_cost_per_second * endpoint_cost_seconds

    if billing_model == "iaas_vm_lease":
        execution_cost_usd = 0.0
        lease_cost_usd = cost_per_second * billable_seconds
        marginal_cost_usd = lease_cost_usd
        fixed_cost_usd = standing_cost_usd + endpoint_cost_usd
        budget_reservation_usd = marginal_cost_usd
        cost_scope = "request_lease_marginal" if fixed_cost_usd == 0 else "request_lease_marginal_with_separate_fixed_cost"
    else:
        lease_cost_usd = 0.0
        execution_cost_usd = cost_per_second * billable_seconds
        marginal_cost_usd = execution_cost_usd
        fixed_cost_usd = standing_cost_usd + endpoint_cost_usd
        budget_reservation_usd = marginal_cost_usd
        cost_scope = "request_marginal" if fixed_cost_usd == 0 else "request_marginal_with_separate_fixed_cost"

    price_freshness = tuple_configured_price_freshness(tuple_spec)
    estimated_cost_usd = marginal_cost_usd + fixed_cost_usd
    return {
        "method": "provider_billing_model_with_request_marginal_and_fixed_cost_split",
        "tuple": tuple_spec.name,
        "provider_billing_model": billing_model,
        "cost_scope": cost_scope,
        "cost_per_second": cost_per_second,
        "configured_price_source": tuple_spec.configured_price_source or "",
        "configured_price_observed_at": tuple_spec.configured_price_observed_at or "",
        "configured_price_ttl_seconds": float(tuple_spec.configured_price_ttl_seconds or 0),
        "price_freshness": price_freshness.value,
        "cold_start_seconds": cold_start_seconds,
        "timeout_seconds": int(timeout_seconds),
        "input_tokens": int(input_tokens or 0),
        "output_tokens": int(output_tokens or 0),
        "runtime_seconds": runtime_seconds,
        "runtime_seconds_source": runtime_source,
        "estimated_prefill_tokens_per_second": float(tuple_spec.estimated_prefill_tokens_per_second or 0),
        "estimated_decode_tokens_per_second": float(tuple_spec.estimated_decode_tokens_per_second or 0),
        "estimated_runtime_overhead_seconds": float(tuple_spec.estimated_runtime_overhead_seconds or 0),
        "runtime_estimate_safety_multiplier": float(tuple_spec.runtime_estimate_safety_multiplier or 1.0),
        "idle_seconds": idle_seconds,
        "raw_billable_seconds": raw_billable_seconds,
        "billable_seconds": billable_seconds,
        "standing_cost_per_second": standing_cost_per_second,
        "standing_cost_seconds": standing_cost_seconds,
        "endpoint_cost_per_second": endpoint_cost_per_second,
        "endpoint_cost_seconds": endpoint_cost_seconds,
        "cold_start_cost_usd": cost_per_second * cold_start_seconds,
        "runtime_cost_usd": cost_per_second * runtime_seconds,
        "idle_cost_usd": cost_per_second * idle_seconds,
        "lease_cost_usd": lease_cost_usd,
        "standing_cost_usd": standing_cost_usd,
        "endpoint_cost_usd": endpoint_cost_usd,
        "execution_cost_usd": execution_cost_usd,
        "marginal_cost_usd": marginal_cost_usd,
        "fixed_cost_usd": fixed_cost_usd,
        "estimated_cost_usd": estimated_cost_usd,
        "budget_reservation_usd": budget_reservation_usd,
    }


def budget_reservation_usd(cost_estimate: Mapping[str, Any] | None) -> float:
    if not cost_estimate:
        return 0.0
    value = cost_estimate.get("budget_reservation_usd")
    if value is None:
        value = cost_estimate.get("estimated_cost_usd")
    return float(value or 0.0)


def _runtime_seconds(
    tuple_spec: ExecutionTupleSpec,
    recipe: Recipe,
    *,
    timeout_seconds: int,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> tuple[float, str]:
    token_estimate = _token_runtime_seconds(
        tuple_spec,
        timeout_seconds=timeout_seconds,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )
    if token_estimate is not None:
        return token_estimate
    for source, value in (
        ("tuple.expected_runtime_seconds", tuple_spec.expected_runtime_seconds),
        ("recipe.expected_runtime_seconds", recipe.expected_runtime_seconds),
    ):
        if value is None:
            continue
        seconds = min(float(value), float(timeout_seconds))
        return seconds, source if seconds == float(value) else f"{source}_clamped_to_timeout"
    return float(timeout_seconds), "timeout_seconds_fail_closed_fallback"


def _token_runtime_seconds(
    tuple_spec: ExecutionTupleSpec,
    *,
    timeout_seconds: int,
    input_tokens: int | None,
    output_tokens: int | None,
) -> tuple[float, str] | None:
    if input_tokens is None and output_tokens is None:
        return None
    prefill_tps = float(tuple_spec.estimated_prefill_tokens_per_second or 0)
    decode_tps = float(tuple_spec.estimated_decode_tokens_per_second or 0)
    if prefill_tps <= 0 and decode_tps <= 0:
        return None
    if prefill_tps <= 0:
        prefill_tps = decode_tps
    if decode_tps <= 0:
        decode_tps = prefill_tps
    prefill_seconds = max(float(input_tokens or 0), 0.0) / prefill_tps
    decode_seconds = max(float(output_tokens or 0), 0.0) / decode_tps
    overhead_seconds = float(tuple_spec.estimated_runtime_overhead_seconds or 0)
    multiplier = float(tuple_spec.runtime_estimate_safety_multiplier or 1.0)
    raw_seconds = max(0.0, overhead_seconds + prefill_seconds + decode_seconds) * max(multiplier, 0.0)
    seconds = min(raw_seconds, float(timeout_seconds))
    source = "tuple.token_throughput_estimate"
    if seconds != raw_seconds:
        source += "_clamped_to_timeout"
    return seconds, source


def _idle_seconds(tuple_spec: ExecutionTupleSpec, billing_model: str) -> float:
    if billing_model in {"function_runtime", "runpod_serverless_function", "runpod_serverless_managed_endpoint"}:
        return 0.0
    return float(tuple_spec.scaledown_window_seconds or 0)


def _rounded_billable_seconds(tuple_spec: ExecutionTupleSpec, raw_seconds: float) -> float:
    billable_seconds = raw_seconds
    if tuple_spec.min_billable_seconds is not None:
        billable_seconds = max(billable_seconds, float(tuple_spec.min_billable_seconds))
    if tuple_spec.billing_granularity_seconds:
        granularity = float(tuple_spec.billing_granularity_seconds)
        billable_seconds = ceil(billable_seconds / granularity) * granularity
    return billable_seconds


def _billing_model(tuple_spec: ExecutionTupleSpec) -> str:
    adapter = str(tuple_spec.adapter or "").lower()
    surface = tuple_spec.execution_surface
    if surface is ExecutionSurface.IAAS_VM or adapter == "hyperstack":
        return "iaas_vm_lease"
    if adapter.startswith("runpod") and surface is ExecutionSurface.MANAGED_ENDPOINT:
        return "runpod_serverless_managed_endpoint"
    if adapter.startswith("runpod"):
        return "runpod_serverless_function"
    if adapter == "modal" or surface is ExecutionSurface.FUNCTION_RUNTIME:
        return "function_runtime"
    if surface is ExecutionSurface.LOCAL_RUNTIME:
        return "local_runtime"
    return "generic_request_runtime"
