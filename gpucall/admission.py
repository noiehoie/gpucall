from __future__ import annotations

import os
from dataclasses import dataclass
from time import monotonic
from typing import Mapping
import asyncio

from gpucall.domain import ExecutionTupleSpec


@dataclass(frozen=True)
class AdmissionLease:
    tuple: str
    family: str
    workload_scope: str | None = None


@dataclass(frozen=True)
class AdmissionDecision:
    allowed: bool
    lease: AdmissionLease | None = None
    reason: str | None = None
    suppressed_until_seconds: float | None = None


class AdmissionController:
    """In-process runtime admission control for tuple execution.

    Static routing tells us that a tuple is compatible. Admission tells us that
    the gateway should actually start work on it now. The implementation is
    deliberately local and deterministic; multi-gateway deployments should put a
    shared implementation behind the same interface.
    """

    def __init__(
        self,
        tuples: Mapping[str, ExecutionTupleSpec] | None = None,
        *,
        tuple_limit: int | None = None,
        family_limit: int | None = None,
        workload_scope_limit: int | None = None,
        cooldown_seconds: float | None = None,
    ) -> None:
        self.tuple_families = {
            name: _tuple_family(spec)
            for name, spec in (tuples or {}).items()
        }
        self.tuple_limit = tuple_limit if tuple_limit is not None else _env_int("GPUCALL_TUPLE_CONCURRENCY_LIMIT", 1)
        self.family_limit = family_limit if family_limit is not None else _env_int("GPUCALL_PROVIDER_FAMILY_CONCURRENCY_LIMIT", 2)
        self.workload_scope_limit = (
            workload_scope_limit
            if workload_scope_limit is not None
            else _env_int("GPUCALL_WORKLOAD_SCOPE_CONCURRENCY_LIMIT", 4)
        )
        self.cooldown_seconds = (
            cooldown_seconds
            if cooldown_seconds is not None
            else _env_float("GPUCALL_PROVIDER_TEMPORARY_COOLDOWN_SECONDS", 60.0)
        )
        self._tuple_inflight: dict[str, int] = {}
        self._family_inflight: dict[str, int] = {}
        self._workload_scope_inflight: dict[str, int] = {}
        self._tuple_suppressed_until: dict[str, float] = {}
        self._family_suppressed_until: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, tuple_name: str, *, workload_scope: str | None = None) -> AdmissionDecision:
        family = self.family_for(tuple_name)
        async with self._lock:
            now = monotonic()
            tuple_until = self._tuple_suppressed_until.get(tuple_name, 0.0)
            family_until = self._family_suppressed_until.get(family, 0.0)
            if tuple_until > now:
                return AdmissionDecision(False, reason="tuple_suppressed", suppressed_until_seconds=tuple_until)
            if family_until > now:
                return AdmissionDecision(False, reason="provider_family_suppressed", suppressed_until_seconds=family_until)
            if self.tuple_limit > 0 and self._tuple_inflight.get(tuple_name, 0) >= self.tuple_limit:
                return AdmissionDecision(False, reason="tuple_inflight_limit")
            if self.family_limit > 0 and self._family_inflight.get(family, 0) >= self.family_limit:
                return AdmissionDecision(False, reason="provider_family_inflight_limit")
            if (
                workload_scope
                and self.workload_scope_limit > 0
                and self._workload_scope_inflight.get(workload_scope, 0) >= self.workload_scope_limit
            ):
                return AdmissionDecision(False, reason="workload_scope_inflight_limit")
            self._tuple_inflight[tuple_name] = self._tuple_inflight.get(tuple_name, 0) + 1
            self._family_inflight[family] = self._family_inflight.get(family, 0) + 1
            if workload_scope:
                self._workload_scope_inflight[workload_scope] = self._workload_scope_inflight.get(workload_scope, 0) + 1
            return AdmissionDecision(True, lease=AdmissionLease(tuple=tuple_name, family=family, workload_scope=workload_scope))

    async def release(self, lease: AdmissionLease | None) -> None:
        if lease is None:
            return
        async with self._lock:
            _decrement(self._tuple_inflight, lease.tuple)
            _decrement(self._family_inflight, lease.family)
            if lease.workload_scope:
                _decrement(self._workload_scope_inflight, lease.workload_scope)

    async def suppress(self, tuple_name: str, *, code: str | None = None) -> None:
        family = self.family_for(tuple_name)
        until = monotonic() + self.cooldown_seconds
        async with self._lock:
            self._tuple_suppressed_until[tuple_name] = max(self._tuple_suppressed_until.get(tuple_name, 0.0), until)
            self._family_suppressed_until[family] = max(self._family_suppressed_until.get(family, 0.0), until)

    def family_for(self, tuple_name: str) -> str:
        return self.tuple_families.get(tuple_name, tuple_name)

    def snapshot(self) -> dict[str, object]:
        now = monotonic()
        return {
            "tuple_limit": self.tuple_limit,
            "provider_family_limit": self.family_limit,
            "workload_scope_limit": self.workload_scope_limit,
            "cooldown_seconds": self.cooldown_seconds,
            "tuple_inflight": dict(sorted(self._tuple_inflight.items())),
            "provider_family_inflight": dict(sorted(self._family_inflight.items())),
            "workload_scope_inflight": dict(sorted(self._workload_scope_inflight.items())),
            "suppressed_tuples": {
                name: round(until - now, 3)
                for name, until in sorted(self._tuple_suppressed_until.items())
                if until > now
            },
            "suppressed_provider_families": {
                name: round(until - now, 3)
                for name, until in sorted(self._family_suppressed_until.items())
                if until > now
            },
        }


def _tuple_family(spec: ExecutionTupleSpec) -> str:
    account = spec.account_ref or spec.adapter
    surface = spec.execution_surface.value if spec.execution_surface is not None else spec.adapter
    region = spec.region or spec.zone or ""
    return ":".join(part for part in (account, surface, region) if part)


def _decrement(values: dict[str, int], key: str) -> None:
    current = values.get(key, 0)
    if current <= 1:
        values.pop(key, None)
        return
    values[key] = current - 1


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return max(int(raw), 0)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return max(float(raw), 0.0)
    except ValueError:
        return default
