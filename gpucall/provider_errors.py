from __future__ import annotations

from dataclasses import dataclass

from gpucall.domain import ProviderErrorCode


@dataclass(frozen=True)
class ProviderErrorClass:
    code: str
    meaning: str
    typical_state: str
    fallback_eligible: bool = True
    cancel_remote: bool = True
    caller_action: str = "retry_later_or_wait_for_gpucall_fallback"
    suppress_provider_family: bool = False


PROVIDER_TEMPORARY_UNAVAILABLE_ERRORS: dict[str, ProviderErrorClass] = {
    item.code: item
    for item in (
        ProviderErrorClass(
            "PROVIDER_RESOURCE_EXHAUSTED",
            "provider GPU, queue, quota, or rate capacity is exhausted",
            "429/503",
            suppress_provider_family=True,
        ),
        ProviderErrorClass("PROVIDER_CAPACITY_UNAVAILABLE", "endpoint exists but no ready worker is available", "503"),
        ProviderErrorClass("PROVIDER_PROVISION_UNAVAILABLE", "VM, worker, or container cannot be started now", "409/423/429/503/504"),
        ProviderErrorClass("PROVIDER_QUEUE_SATURATED", "accepted job remains queued beyond the gateway tolerance", "IN_QUEUE"),
        ProviderErrorClass("PROVIDER_WORKER_INITIALIZING", "worker, container, or model is still initializing", "initializing/cold_start"),
        ProviderErrorClass(
            "PROVIDER_WORKER_THROTTLED",
            "worker is throttled and no execution slot is available",
            "throttled",
            suppress_provider_family=True,
        ),
        ProviderErrorClass("PROVIDER_TIMEOUT", "provider-side job timeout", "TIMED_OUT/504"),
        ProviderErrorClass("PROVIDER_POLL_TIMEOUT", "gateway timed out polling a provider job", "504"),
        ProviderErrorClass("PROVIDER_JOB_FAILED", "provider job failed with no finer temporary classification", "FAILED"),
        ProviderErrorClass(
            "PROVIDER_CANCELLED",
            "provider job was cancelled before usable output",
            "CANCELLED",
            fallback_eligible=False,
            cancel_remote=False,
            caller_action="check_caller_or_admin_cancellation_before_retry",
        ),
        ProviderErrorClass("PROVIDER_UNHEALTHY", "provider worker health is unhealthy", "unhealthy"),
        ProviderErrorClass("PROVIDER_BOOTING", "endpoint, pod, or container is booting", "starting/booting"),
        ProviderErrorClass("PROVIDER_PREEMPTED", "spot or provider host preempted the job", "preempted/terminated"),
        ProviderErrorClass(
            "PROVIDER_MAINTENANCE",
            "provider surface is in maintenance",
            "503/maintenance",
            suppress_provider_family=True,
        ),
        ProviderErrorClass(
            "PROVIDER_UPSTREAM_UNAVAILABLE",
            "provider API, queue engine, or control plane is unavailable",
            "502/503",
            suppress_provider_family=True,
        ),
        ProviderErrorClass("PROVIDER_RATE_LIMITED", "provider API rate limit was reached", "429", suppress_provider_family=True),
        ProviderErrorClass(
            "PROVIDER_QUOTA_EXCEEDED",
            "provider account quota, spend, or service limit was reached",
            "403/429",
            fallback_eligible=False,
            caller_action="contact_gpucall_admin_or_use_a_different_provider_account",
            suppress_provider_family=True,
        ),
        ProviderErrorClass("PROVIDER_REGION_UNAVAILABLE", "requested provider region or zone has no usable GPU", "409/503"),
        ProviderErrorClass("PROVIDER_IMAGE_PULL_DELAY", "image or container pull is delaying startup", "initializing/timeout"),
        ProviderErrorClass("PROVIDER_MODEL_LOADING", "model load is still in progress", "health_initializing"),
        ProviderErrorClass(
            "PROVIDER_CONCURRENCY_LIMIT",
            "provider max workers or max concurrency was reached",
            "429/503/throttled",
            suppress_provider_family=True,
        ),
        ProviderErrorClass("PROVIDER_LEASE_EXPIRED", "lease or TTL expired before execution completed", "expired/504"),
        ProviderErrorClass("PROVIDER_STALE_JOB", "accepted job stopped reporting progress", "no_heartbeat"),
        ProviderErrorClass("PROVIDER_ERROR", "unclassified retryable provider runtime failure", "502/503"),
    )
}


PROVIDER_TEMPORARY_UNAVAILABLE_CODES = frozenset(PROVIDER_TEMPORARY_UNAVAILABLE_ERRORS)

assert PROVIDER_TEMPORARY_UNAVAILABLE_CODES == {item.value for item in ProviderErrorCode}


def is_provider_temporary_unavailable(code: str | None) -> bool:
    if not code:
        return False
    return code in PROVIDER_TEMPORARY_UNAVAILABLE_CODES


def provider_error_class(code: str | None) -> ProviderErrorClass | None:
    if not code:
        return None
    return PROVIDER_TEMPORARY_UNAVAILABLE_ERRORS.get(code)


def should_suppress_provider_family(code: str | None) -> bool:
    provider_class = provider_error_class(code)
    return bool(provider_class and provider_class.suppress_provider_family)
