from __future__ import annotations

from gpucall.credentials import load_credentials
from gpucall.domain import ExecutionTupleSpec
from gpucall.execution.base import TupleAdapter
from gpucall.execution.registry import build_registered_adapter


def build_adapters(tuples: dict[str, ExecutionTupleSpec]) -> dict[str, TupleAdapter]:
    credentials = load_credentials()
    return {name: build_adapter(spec, credentials) for name, spec in tuples.items()}


def build_adapter(spec: ExecutionTupleSpec, credentials: dict[str, dict[str, str]] | None = None) -> TupleAdapter:
    return build_registered_adapter(spec, credentials)
