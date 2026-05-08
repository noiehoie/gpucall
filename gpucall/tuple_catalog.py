from __future__ import annotations

from typing import Any

from gpucall.domain import ExecutionTupleSpec
from gpucall.execution.registry import adapter_descriptor


def live_tuple_catalog_evidence(
    tuples: dict[str, ExecutionTupleSpec],
    credentials: dict[str, dict[str, str]],
) -> dict[str, dict[str, Any]]:
    evidence = {
        tuple.name: {
            "tuple": tuple.name,
            "adapter": tuple.adapter,
            "status": "unknown",
            "checked": False,
            "findings": [],
        }
        for tuple in tuples.values()
    }
    grouped: dict[str, list[ExecutionTupleSpec]] = {}
    for tuple_spec in tuples.values():
        grouped.setdefault(tuple_spec.adapter.strip().lower(), []).append(tuple_spec)

    for tuples_for_adapter in grouped.values():
        descriptor = adapter_descriptor(tuples_for_adapter[0])
        for tuple_spec in tuples_for_adapter:
            evidence[tuple_spec.name]["catalog_validator"] = descriptor.catalog_validator is not None if descriptor else False
        if descriptor is None or descriptor.catalog_validator is None:
            continue
        findings = descriptor.catalog_validator(tuples_for_adapter, credentials)
        finding_names = {str(item.get("tuple") or "") for item in findings if isinstance(item, dict)}
        for tuple_spec in tuples_for_adapter:
            tuple_findings = [item for item in findings if item.get("tuple") == tuple_spec.name]
            blocking_findings = [item for item in tuple_findings if item.get("severity", "error") == "error"]
            unavailable_stock = any(item.get("live_stock_state") == "unavailable" for item in tuple_findings)
            evidence[tuple_spec.name].update(
                {
                    "status": "blocked" if blocking_findings or unavailable_stock else "live_revalidated",
                    "checked": True,
                    "findings": tuple_findings,
                }
            )
        for item in findings:
            if not isinstance(item, dict):
                continue
            name = str(item.get("tuple") or "")
            if name and name not in evidence:
                evidence[name] = {"tuple": name, "status": "blocked", "checked": True, "findings": [item]}
        for name in finding_names:
            tuple_findings = [item for item in findings if item.get("tuple") == name]
            if name in evidence and (
                any(item.get("severity", "error") == "error" for item in tuple_findings)
                or any(item.get("live_stock_state") == "unavailable" for item in tuple_findings)
            ):
                evidence[name]["status"] = "blocked"
    return evidence


def live_tuple_catalog_findings(tuples: dict[str, ExecutionTupleSpec], credentials: dict[str, dict[str, str]]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for item in live_tuple_catalog_evidence(tuples, credentials).values():
        findings.extend(item.get("findings") or [])
    return findings
