from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Iterable, Protocol, runtime_checkable

from gpucall.domain import ArtifactManifest, CompiledPlan, JobRecord, TenantSpec


def postgres_state_dsn_from_env() -> str | None:
    value = os.getenv("GPUCALL_DATABASE_URL") or os.getenv("DATABASE_URL")
    if value is None or not value.strip():
        return None
    dsn = value.strip()
    if dsn.startswith(("postgres://", "postgresql://")):
        return dsn
    return None


@runtime_checkable
class JobStateStore(Protocol):
    async def create(self, plan: CompiledPlan, *, owner_identity: str | None = None) -> JobRecord: ...

    async def get(self, job_id: str) -> JobRecord | None: ...

    async def update(self, job_id: str, **changes: object) -> JobRecord: ...

    async def all(self) -> list[JobRecord]: ...


@runtime_checkable
class IdempotencyStateStore(Protocol):
    def get(
        self,
        key: str,
        *,
        ttl_seconds: float,
        max_entries: int,
    ) -> tuple[str, int, dict[str, Any], dict[str, str], str] | None: ...

    def reserve(self, key: str, *, request_hash: str, max_entries: int) -> bool: ...

    def set(
        self,
        key: str,
        *,
        request_hash: str,
        status: int,
        content: dict[str, Any],
        headers: dict[str, str],
        max_entries: int,
    ) -> None: ...

    def release(self, key: str, *, request_hash: str) -> None: ...


@runtime_checkable
class TenantUsageState(Protocol):
    def reserve(self, tenant_id: str, estimated_cost_usd: float, *, tuple: str | None, recipe: str | None, plan_id: str | None) -> None: ...

    def reserve_with_budget(
        self,
        tenant_id: str,
        estimated_cost_usd: float,
        *,
        tenant: TenantSpec | None,
        tuple: str | None,
        recipe: str | None,
        plan_id: str | None,
    ) -> None: ...

    def release_plan(self, plan_id: str | None) -> None: ...

    def commit_plan(self, plan_id: str | None) -> None: ...

    def spend_since(self, tenant_id: str, since: datetime) -> float: ...

    def summary(self, tenants: dict[str, TenantSpec]) -> dict[str, Any]: ...


@runtime_checkable
class ArtifactStateRegistry(Protocol):
    def append(self, manifest: ArtifactManifest) -> ArtifactManifest: ...

    def compare_and_set_latest(self, artifact_chain_id: str, *, expected_version: str | None, new_version: str) -> bool: ...

    def latest_version(self, artifact_chain_id: str) -> str | None: ...

    def get(self, artifact_id: str) -> ArtifactManifest | None: ...

    def list_chain(self, artifact_chain_id: str) -> Iterable[ArtifactManifest]: ...


@runtime_checkable
class AdmissionStateController(Protocol):
    async def acquire(self, tuple_name: str, *, workload_scope: str | None = None) -> Any: ...

    async def acquire_with_wait(
        self,
        tuple_name: str,
        *,
        workload_scope: str | None = None,
        wait_seconds: float = 0.0,
    ) -> Any: ...

    async def release(self, lease: Any | None) -> None: ...

    async def suppress(self, tuple_name: str, *, code: str | None = None, suppress_family: bool = False) -> None: ...

    def family_for(self, tuple_name: str) -> str: ...

    def snapshot(self) -> dict[str, object]: ...
