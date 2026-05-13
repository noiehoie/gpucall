from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from time import monotonic
from typing import Any, Mapping
from uuid import uuid4
import asyncio

from gpucall.domain import ExecutionTupleSpec


@dataclass(frozen=True)
class AdmissionLease:
    tuple: str
    family: str
    workload_scope: str | None = None
    lease_id: str | None = None


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
                return AdmissionDecision(False, reason="tuple_suppressed", suppressed_until_seconds=tuple_until - now)
            if family_until > now:
                return AdmissionDecision(False, reason="provider_family_suppressed", suppressed_until_seconds=family_until - now)
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

    async def acquire_with_wait(
        self,
        tuple_name: str,
        *,
        workload_scope: str | None = None,
        wait_seconds: float = 0.0,
    ) -> AdmissionDecision:
        deadline = monotonic() + max(float(wait_seconds), 0.0)
        while True:
            decision = await self.acquire(tuple_name, workload_scope=workload_scope)
            if decision.allowed or decision.reason not in _INFLIGHT_LIMIT_REASONS or monotonic() >= deadline:
                return decision
            await asyncio.sleep(min(0.1, max(deadline - monotonic(), 0.0)))

    async def release(self, lease: AdmissionLease | None) -> None:
        if lease is None:
            return
        async with self._lock:
            _decrement(self._tuple_inflight, lease.tuple)
            _decrement(self._family_inflight, lease.family)
            if lease.workload_scope:
                _decrement(self._workload_scope_inflight, lease.workload_scope)

    async def suppress(self, tuple_name: str, *, code: str | None = None, suppress_family: bool = False) -> None:
        family = self.family_for(tuple_name)
        until = monotonic() + self.cooldown_seconds
        async with self._lock:
            self._tuple_suppressed_until[tuple_name] = max(self._tuple_suppressed_until.get(tuple_name, 0.0), until)
            if suppress_family:
                self._family_suppressed_until[family] = max(self._family_suppressed_until.get(family, 0.0), until)

    def family_for(self, tuple_name: str) -> str:
        return self.tuple_families.get(tuple_name, tuple_name)

    def snapshot(self) -> dict[str, object]:
        now = monotonic()
        return {
            "backend": "memory",
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


class PostgresAdmissionController(AdmissionController):
    """Postgres-backed admission control for multi-gateway deployments."""

    def __init__(self, dsn: str, tuples: Mapping[str, ExecutionTupleSpec] | None = None, **kwargs: Any) -> None:
        super().__init__(tuples, **kwargs)
        import psycopg

        self._psycopg = psycopg
        self._conn = psycopg.connect(dsn)
        self._thread_lock = threading.RLock()
        self.lease_ttl_seconds = _env_float("GPUCALL_ADMISSION_LEASE_TTL_SECONDS", 3600.0)
        self._init_schema()

    def _init_schema(self) -> None:
        with self._thread_lock, self._conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS gpucall_admission_leases (
                  lease_id TEXT NOT NULL,
                  kind TEXT NOT NULL,
                  key TEXT NOT NULL,
                  expires_at TIMESTAMPTZ NOT NULL,
                  PRIMARY KEY(lease_id, kind, key)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS gpucall_admission_suppression (
                  kind TEXT NOT NULL,
                  key TEXT NOT NULL,
                  suppressed_until TIMESTAMPTZ NOT NULL,
                  code TEXT,
                  PRIMARY KEY(kind, key)
                )
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS gpucall_admission_leases_kind_key_idx ON gpucall_admission_leases(kind, key, expires_at)")
        self._conn.commit()

    async def acquire(self, tuple_name: str, *, workload_scope: str | None = None) -> AdmissionDecision:
        return await asyncio.to_thread(self._acquire_sync, tuple_name, workload_scope)

    def _acquire_sync(self, tuple_name: str, workload_scope: str | None) -> AdmissionDecision:
        family = self.family_for(tuple_name)
        now = datetime.now(timezone.utc)
        with self._thread_lock:
            try:
                with self._conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_xact_lock(917380642)")
                    self._cleanup_expired(cur, now)
                    tuple_until = self._suppressed_until(cur, "tuple", tuple_name)
                    family_until = self._suppressed_until(cur, "family", family)
                    if tuple_until and tuple_until > now:
                        self._conn.commit()
                        return AdmissionDecision(False, reason="tuple_suppressed", suppressed_until_seconds=(tuple_until - now).total_seconds())
                    if family_until and family_until > now:
                        self._conn.commit()
                        return AdmissionDecision(False, reason="provider_family_suppressed", suppressed_until_seconds=(family_until - now).total_seconds())
                    if self.tuple_limit > 0 and self._lease_count(cur, "tuple", tuple_name) >= self.tuple_limit:
                        self._conn.commit()
                        return AdmissionDecision(False, reason="tuple_inflight_limit")
                    if self.family_limit > 0 and self._lease_count(cur, "family", family) >= self.family_limit:
                        self._conn.commit()
                        return AdmissionDecision(False, reason="provider_family_inflight_limit")
                    if workload_scope and self.workload_scope_limit > 0 and self._lease_count(cur, "workload_scope", workload_scope) >= self.workload_scope_limit:
                        self._conn.commit()
                        return AdmissionDecision(False, reason="workload_scope_inflight_limit")
                    lease_id = uuid4().hex
                    expires_at = now + timedelta(seconds=self.lease_ttl_seconds)
                    rows = [("tuple", tuple_name), ("family", family)]
                    if workload_scope:
                        rows.append(("workload_scope", workload_scope))
                    for kind, key in rows:
                        cur.execute(
                            "INSERT INTO gpucall_admission_leases(lease_id, kind, key, expires_at) VALUES (%s, %s, %s, %s)",
                            (lease_id, kind, key, expires_at),
                        )
                self._conn.commit()
                return AdmissionDecision(True, lease=AdmissionLease(tuple=tuple_name, family=family, workload_scope=workload_scope, lease_id=lease_id))
            except Exception:
                self._conn.rollback()
                raise

    async def release(self, lease: AdmissionLease | None) -> None:
        if lease is None:
            return
        if not lease.lease_id:
            await super().release(lease)
            return
        await asyncio.to_thread(self._release_sync, lease.lease_id)

    def _release_sync(self, lease_id: str) -> None:
        with self._thread_lock, self._conn.cursor() as cur:
            cur.execute("DELETE FROM gpucall_admission_leases WHERE lease_id = %s", (lease_id,))
        self._conn.commit()

    async def suppress(self, tuple_name: str, *, code: str | None = None, suppress_family: bool = False) -> None:
        await asyncio.to_thread(self._suppress_sync, tuple_name, code, suppress_family)

    def _suppress_sync(self, tuple_name: str, code: str | None, suppress_family: bool) -> None:
        family = self.family_for(tuple_name)
        until = datetime.now(timezone.utc) + timedelta(seconds=self.cooldown_seconds)
        rows = [("tuple", tuple_name)]
        if suppress_family:
            rows.append(("family", family))
        with self._thread_lock, self._conn.cursor() as cur:
            for kind, key in rows:
                cur.execute(
                    """
                    INSERT INTO gpucall_admission_suppression(kind, key, suppressed_until, code)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT(kind, key) DO UPDATE SET
                      suppressed_until = GREATEST(gpucall_admission_suppression.suppressed_until, excluded.suppressed_until),
                      code = excluded.code
                    """,
                    (kind, key, until, code),
                )
        self._conn.commit()

    def snapshot(self) -> dict[str, object]:
        return self._snapshot_sync()

    def _snapshot_sync(self) -> dict[str, object]:
        now = datetime.now(timezone.utc)
        with self._thread_lock, self._conn.cursor() as cur:
            self._cleanup_expired(cur, now)
            cur.execute("SELECT kind, key, count(*) FROM gpucall_admission_leases GROUP BY kind, key ORDER BY kind, key")
            lease_rows = cur.fetchall()
            cur.execute(
                "SELECT kind, key, suppressed_until FROM gpucall_admission_suppression WHERE suppressed_until > %s ORDER BY kind, key",
                (now,),
            )
            suppressed_rows = cur.fetchall()
        self._conn.commit()
        tuple_inflight: dict[str, int] = {}
        family_inflight: dict[str, int] = {}
        workload_scope_inflight: dict[str, int] = {}
        for kind, key, count in lease_rows:
            target = {
                "tuple": tuple_inflight,
                "family": family_inflight,
                "workload_scope": workload_scope_inflight,
            }.get(str(kind))
            if target is not None:
                target[str(key)] = int(count)
        suppressed_tuples: dict[str, float] = {}
        suppressed_families: dict[str, float] = {}
        for kind, key, until in suppressed_rows:
            remaining = round((until - now).total_seconds(), 3)
            if kind == "tuple":
                suppressed_tuples[str(key)] = remaining
            elif kind == "family":
                suppressed_families[str(key)] = remaining
        return {
            "backend": "postgres",
            "tuple_limit": self.tuple_limit,
            "provider_family_limit": self.family_limit,
            "workload_scope_limit": self.workload_scope_limit,
            "cooldown_seconds": self.cooldown_seconds,
            "lease_ttl_seconds": self.lease_ttl_seconds,
            "tuple_inflight": tuple_inflight,
            "provider_family_inflight": family_inflight,
            "workload_scope_inflight": workload_scope_inflight,
            "suppressed_tuples": suppressed_tuples,
            "suppressed_provider_families": suppressed_families,
        }

    def close(self) -> None:
        self._conn.close()

    @staticmethod
    def _cleanup_expired(cur: Any, now: datetime) -> None:
        cur.execute("DELETE FROM gpucall_admission_leases WHERE expires_at <= %s", (now,))
        cur.execute("DELETE FROM gpucall_admission_suppression WHERE suppressed_until <= %s", (now,))

    @staticmethod
    def _lease_count(cur: Any, kind: str, key: str) -> int:
        cur.execute("SELECT count(*) FROM gpucall_admission_leases WHERE kind = %s AND key = %s", (kind, key))
        row = cur.fetchone()
        return int(row[0] if row else 0)

    @staticmethod
    def _suppressed_until(cur: Any, kind: str, key: str) -> datetime | None:
        cur.execute("SELECT suppressed_until FROM gpucall_admission_suppression WHERE kind = %s AND key = %s", (kind, key))
        row = cur.fetchone()
        return row[0] if row else None


def _tuple_family(spec: ExecutionTupleSpec) -> str:
    account = spec.account_ref or spec.adapter
    surface = spec.execution_surface.value if spec.execution_surface is not None else spec.adapter
    region = spec.region or spec.zone or ""
    return ":".join(part for part in (account, surface, region) if part)


_INFLIGHT_LIMIT_REASONS = {
    "tuple_inflight_limit",
    "provider_family_inflight_limit",
    "workload_scope_inflight_limit",
}


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
