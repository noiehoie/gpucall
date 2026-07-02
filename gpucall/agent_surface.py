"""Machine-readable agent-facing surfaces for the v2.5 Agent-Native Execution Layer.

This module owns deterministic, secret-free payloads that AI agents and external
automation consume to decide their next action without human relay:

- the failure/retry taxonomy (provider, governance, tenant budget)
- the non-billable pre-execution estimate summary

It must not perform routing decisions, provider calls, or budget mutation.
"""

from __future__ import annotations

from typing import Any, Mapping

from gpucall.provider_errors import PROVIDER_TEMPORARY_UNAVAILABLE_ERRORS

FAILURE_TAXONOMY_SCHEMA_VERSION = 1

GOVERNANCE_FAILURE_KINDS: dict[str, dict[str, Any]] = {
    "no_recipe": {
        "meaning": "no auto-selectable recipe matched the declared workload",
        "http_status": 422,
        "retryable_without_change": False,
        "caller_action": "run_gpucall_recipe_draft_intake",
        "owner": "caller_then_gpucall_admin",
    },
    "no_tuple": {
        "meaning": "no eligible execution tuple after policy, recipe, and circuit constraints",
        "http_status": 503,
        "retryable_without_change": True,
        "caller_action": "check_tuple_health_or_retry_later",
        "owner": "gpucall_operator",
    },
    "policy_denied": {
        "meaning": "tenant policy, security gate, or eligibility policy rejected the request",
        "http_status": 403,
        "retryable_without_change": False,
        "caller_action": "contact_gpucall_admin",
        "owner": "gpucall_operator",
    },
    "input_contract": {
        "meaning": "request payload violates the recipe input contract",
        "http_status": 422,
        "retryable_without_change": False,
        "caller_action": "fix_request_or_run_gpucall_recipe_draft_intake",
        "owner": "caller",
    },
    "tenant_budget": {
        "meaning": "tenant budget cap would be exceeded by the estimated cost",
        "http_status": 429,
        "retryable_without_change": False,
        "caller_action": "wait_for_budget_window_or_contact_gpucall_admin",
        "owner": "gpucall_operator",
    },
}


def failure_taxonomy() -> dict[str, Any]:
    """Return the deterministic failure/retry taxonomy for agent callers."""
    provider_errors = {
        code: {
            "meaning": item.meaning,
            "typical_state": item.typical_state,
            "retryable": True,
            "fallback_eligible": item.fallback_eligible,
            "cancel_remote": item.cancel_remote,
            "caller_action": item.caller_action,
            "suppress_provider_family": item.suppress_provider_family,
        }
        for code, item in sorted(PROVIDER_TEMPORARY_UNAVAILABLE_ERRORS.items())
    }
    return {
        "schema_version": FAILURE_TAXONOMY_SCHEMA_VERSION,
        "phase": "failure-taxonomy",
        "retry_semantics": {
            "provider_temporary_unavailable": (
                "gpucall already attempted governed fallback before returning; the caller may retry "
                "the same request later without changing it"
            ),
            "governance": "retrying an unchanged request will fail again unless retryable_without_change is true",
            "idempotency": "retries must reuse the original idempotency_key to avoid duplicate billable execution",
            "circuit_scope": "caller-side breakers must scope to task:intent:mode:transport, never process-global",
        },
        "provider_errors": provider_errors,
        "governance_failures": GOVERNANCE_FAILURE_KINDS,
    }


def estimate_summary(
    plan: Any,
    *,
    plan_summary: Mapping[str, Any],
    budget_reservation: float,
) -> dict[str, Any]:
    """Build the non-billable estimate response for /v2/estimate.

    The cost estimate comes from the compiled plan attestations; no budget is
    reserved and no provider is contacted.
    """
    attestations = getattr(plan, "attestations", {}) or {}
    cost_estimate = attestations.get("cost_estimate") or {}
    return {
        "schema_version": 1,
        "phase": "estimate",
        "billable": False,
        "budget_reserved": False,
        "plan": dict(plan_summary),
        "mode": getattr(getattr(plan, "mode", None), "value", None),
        "estimated_cost_usd": cost_estimate.get("estimated_cost_usd"),
        "budget_reservation_usd": budget_reservation,
        "cost_estimate": dict(cost_estimate),
        "lease_ttl_seconds": getattr(plan, "lease_ttl_seconds", None),
        "next_action": "submit the same request to /v2/tasks/sync, /v2/tasks/async, or /v2/tasks/stream",
    }
