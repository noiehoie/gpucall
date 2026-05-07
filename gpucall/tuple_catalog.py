from __future__ import annotations

from typing import Any

from gpucall.domain import ExecutionTupleSpec
from gpucall.execution.registry import adapter_descriptor


def live_tuple_catalog_findings(tuples: dict[str, ExecutionTupleSpec], credentials: dict[str, dict[str, str]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[ExecutionTupleSpec]] = {}
    for tuple_spec in tuples.values():
        grouped.setdefault(tuple_spec.adapter.strip().lower(), []).append(tuple_spec)

    findings: list[dict[str, Any]] = []
    for providers_for_adapter in grouped.values():
        descriptor = adapter_descriptor(providers_for_adapter[0])
        if descriptor is None or descriptor.catalog_validator is None:
            continue
        findings.extend(descriptor.catalog_validator(providers_for_adapter, credentials))
    return findings
