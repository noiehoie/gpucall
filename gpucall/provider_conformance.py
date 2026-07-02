"""Provider plugin conformance checks (v5 Provider Platform groundwork).

A provider adapter — built-in or entry-point plugin — must satisfy the same
registry-level contract before gpucall will treat it as a governed execution
device. These checks are deterministic and non-generation: they never call a
provider API and never spend money.

Local adapters additionally support an execution-cycle conformance run against
an owned local tuple (echo), proving the start/wait/cancel/lease contract
end to end without provider involvement.
"""

from __future__ import annotations

import inspect
from typing import Any

from gpucall.execution.base import ResourceLease, TupleAdapter
from gpucall.execution.registry import (
    TupleAdapterDescriptor,
    adapter_descriptor,
    registered_adapter_descriptors,
    registered_adapter_names,
    vendor_family_for_adapter,
)

CONFORMANCE_SCHEMA_VERSION = 1

_KNOWN_OUTPUT_CONTRACTS = {
    "openai-chat",
    "openai-compatible",
    "vllm-generate",
    "raw-json",
    "text",
    "echo",
    "ollama",
    "modal-worker",
    "none",
}


def run_provider_conformance(adapter: str | None = None) -> dict[str, Any]:
    """Run registry-level conformance for one adapter or every registered adapter."""
    names = [adapter] if adapter else registered_adapter_names()
    reports: dict[str, Any] = {}
    for name in names:
        reports[name] = _adapter_report(name)
    passed = all(item["passed"] for item in reports.values()) if reports else False
    return {
        "schema_version": CONFORMANCE_SCHEMA_VERSION,
        "phase": "provider-conformance",
        "non_generation_probe_only": True,
        "adapter_count": len(reports),
        "adapters": reports,
        "passed": passed,
    }


def _adapter_report(name: str) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    descriptor = adapter_descriptor(name)
    checks.append(_check("descriptor_registered", descriptor is not None, "adapter must register a TupleAdapterDescriptor"))
    if descriptor is not None:
        checks.append(
            _check(
                "execution_surface_declared",
                descriptor.execution_surface is not None,
                "descriptor must declare its execution surface",
            )
        )
        if descriptor.production_eligible:
            checks.append(
                _check(
                    "production_contracts_declared",
                    not descriptor.requires_contracts
                    or (descriptor.endpoint_contract is not None and descriptor.output_contract is not None),
                    "production-eligible adapters that require contracts must declare endpoint and output contracts",
                )
            )
        else:
            checks.append(
                _check(
                    "production_rejection_reason_declared",
                    bool(descriptor.production_rejection_reason),
                    "non-production adapters must explain why they are rejected from production routing",
                )
            )
        # descriptor.stream_contract semantics: "none" = streaming unsupported,
        # a concrete value = adapter-pinned contract enforced by config
        # validation, None = tuple-defined (each tuple declares its own).
        stream_scope = "tuple-defined" if descriptor.stream_contract is None else descriptor.stream_contract
        checks.append(
            _check(
                "stream_contract_scope_resolvable",
                stream_scope in _KNOWN_OUTPUT_CONTRACTS or stream_scope in {"tuple-defined", "sse"},
                "stream contract must be 'none', a known contract, or tuple-defined",
            )
        )
    checks.append(
        _check(
            "vendor_family_resolvable",
            bool(vendor_family_for_adapter(name)),
            "adapter must resolve to a vendor family for provider-family suppression",
        )
    )
    passed = all(item["ok"] for item in checks)
    return {
        "adapter": name,
        "vendor_family": vendor_family_for_adapter(name),
        "descriptor": _descriptor_summary(descriptor),
        "checks": checks,
        "passed": passed,
    }


async def run_execution_cycle_conformance(adapter_instance: TupleAdapter, plan: Any) -> dict[str, Any]:
    """Prove the start/wait/cancel/lease contract against an owned local adapter.

    The caller supplies a built adapter and compiled plan for a local,
    non-billable tuple (echo or another local runtime). Cloud adapters must not
    be passed here; conformance for cloud adapters stays registry-level and
    non-generation.
    """
    checks: list[dict[str, Any]] = []
    handle = await adapter_instance.start(plan)
    checks.append(_check("start_returns_resource_lease", isinstance(handle, ResourceLease), "start() must return a ResourceLease"))
    if isinstance(handle, ResourceLease):
        checks.append(_check("lease_tuple_named", bool(handle.tuple), "lease must carry the tuple name"))
        checks.append(_check("lease_remote_id", bool(handle.remote_id), "lease must carry a remote id"))
        checks.append(_check("lease_expiry", handle.expires_at is not None, "lease must carry an expiry"))
    result = await adapter_instance.wait(handle, plan)
    checks.append(
        _check(
            "wait_returns_tuple_result",
            hasattr(result, "kind") or isinstance(result, dict),
            "wait() must return a TupleResult-compatible value",
        )
    )
    cancel = adapter_instance.cancel_remote(handle)
    if inspect.isawaitable(cancel):
        await cancel
    checks.append(_check("cancel_remote_callable", True, "cancel_remote() must complete without raising"))
    return {
        "phase": "provider-conformance-execution-cycle",
        "adapter": getattr(adapter_instance, "name", type(adapter_instance).__name__),
        "checks": checks,
        "passed": all(item["ok"] for item in checks),
    }


def _descriptor_summary(descriptor: TupleAdapterDescriptor | None) -> dict[str, Any] | None:
    if descriptor is None:
        return None
    return {
        "execution_surface": getattr(descriptor.execution_surface, "value", None),
        "endpoint_contract": descriptor.endpoint_contract,
        "output_contract": descriptor.output_contract,
        "stream_contract": descriptor.stream_contract,
        "requires_contracts": descriptor.requires_contracts,
        "production_eligible": descriptor.production_eligible,
        "production_rejection_reason": descriptor.production_rejection_reason,
        "local_execution": descriptor.local_execution,
        "official_sources": list(descriptor.official_sources),
    }


def _check(name: str, ok: bool, requirement: str) -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "requirement": requirement}


def registered_conformance_matrix() -> dict[str, Any]:
    """Compact adapter/descriptor matrix used by reports and docs."""
    return {
        name: _descriptor_summary(descriptor)
        for name, descriptor in registered_adapter_descriptors().items()
    }
