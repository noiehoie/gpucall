from __future__ import annotations

import asyncio
import json
import threading
import time
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from gpucall.dispatcher import JobStore
from gpucall.domain import CompiledPlan, JobRecord, JobState


class PostgresJobStore(JobStore):
    def __init__(self, dsn: str) -> None:
        super().__init__()
        import psycopg

        self._psycopg = psycopg
        self._conn = psycopg.connect(dsn)
        self._lock = asyncio.Lock()
        self._init_schema()

    def _init_schema(self) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS gpucall_jobs (
                  job_id TEXT PRIMARY KEY,
                  owner_identity TEXT,
                  state TEXT NOT NULL,
                  payload JSONB NOT NULL,
                  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS gpucall_jobs_owner_state_idx ON gpucall_jobs(owner_identity, state)")
        self._conn.commit()

    async def create(self, plan: CompiledPlan, *, owner_identity: str | None = None) -> JobRecord:
        job = JobRecord(job_id=uuid4().hex, state=JobState.PENDING, plan=plan, owner_identity=owner_identity)
        async with self._lock:
            self._upsert(job)
        return job

    async def get(self, job_id: str) -> JobRecord | None:
        async with self._lock:
            with self._conn.cursor() as cur:
                cur.execute("SELECT payload::text FROM gpucall_jobs WHERE job_id = %s", (job_id,))
                row = cur.fetchone()
        if row is None:
            return None
        return JobRecord.model_validate_json(row[0])

    async def update(self, job_id: str, **changes: object) -> JobRecord:
        current = await self.get(job_id)
        if current is None:
            raise KeyError(job_id)
        job = current.model_copy(update={**changes, "updated_at": datetime.now(timezone.utc)})
        async with self._lock:
            self._upsert(job)
        return job

    async def all(self) -> list[JobRecord]:
        async with self._lock:
            with self._conn.cursor() as cur:
                cur.execute("SELECT payload::text FROM gpucall_jobs ORDER BY updated_at")
                rows = cur.fetchall()
        return [JobRecord.model_validate_json(row[0]) for row in rows]

    def _upsert(self, job: JobRecord) -> None:
        payload = job.model_dump_json()
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO gpucall_jobs(job_id, owner_identity, state, payload, updated_at)
                VALUES (%s, %s, %s, %s::jsonb, %s)
                ON CONFLICT(job_id) DO UPDATE SET
                  owner_identity=excluded.owner_identity,
                  state=excluded.state,
                  payload=excluded.payload,
                  updated_at=excluded.updated_at
                """,
                (job.job_id, job.owner_identity, job.state.value, payload, job.updated_at),
            )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


class PostgresIdempotencyStore:
    def __init__(self, dsn: str) -> None:
        import psycopg

        self._conn = psycopg.connect(dsn)
        self._lock = threading.RLock()
        self._init_schema()

    def _init_schema(self) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS gpucall_idempotency (
                  key TEXT PRIMARY KEY,
                  created_at DOUBLE PRECISION NOT NULL,
                  request_hash TEXT NOT NULL,
                  status INTEGER NOT NULL,
                  content JSONB NOT NULL,
                  headers JSONB NOT NULL
                )
                """
            )
            cur.execute("ALTER TABLE gpucall_idempotency ADD COLUMN IF NOT EXISTS status INTEGER")
            cur.execute("ALTER TABLE gpucall_idempotency ADD COLUMN IF NOT EXISTS content JSONB")
            cur.execute("ALTER TABLE gpucall_idempotency ADD COLUMN IF NOT EXISTS headers JSONB")
            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name = 'gpucall_idempotency'
                """
            )
            columns = {str(row[0]) for row in cur.fetchall()}
            if {"status_code", "response_json", "headers_json"} & columns:
                if "owner_identity" in columns:
                    cur.execute("ALTER TABLE gpucall_idempotency ALTER COLUMN owner_identity DROP NOT NULL")
                if "status_code" in columns:
                    cur.execute("ALTER TABLE gpucall_idempotency ALTER COLUMN status_code DROP NOT NULL")
                if "response_json" in columns:
                    cur.execute("ALTER TABLE gpucall_idempotency ALTER COLUMN response_json DROP NOT NULL")
                if "headers_json" in columns:
                    cur.execute("ALTER TABLE gpucall_idempotency ALTER COLUMN headers_json DROP NOT NULL")
                status_source = "status_code" if "status_code" in columns else "status"
                content_source = "response_json" if "response_json" in columns else "content"
                headers_source = "headers_json" if "headers_json" in columns else "headers"
                cur.execute(
                    f"""
                    UPDATE gpucall_idempotency
                    SET
                      status = COALESCE(status, {status_source}),
                      content = COALESCE(content, {content_source}),
                      headers = COALESCE(headers, {headers_source}, '{{}}'::jsonb)
                    WHERE status IS NULL OR content IS NULL OR headers IS NULL
                    """
                )
            cur.execute("ALTER TABLE gpucall_idempotency ALTER COLUMN headers SET DEFAULT '{}'::jsonb")
            cur.execute("ALTER TABLE gpucall_idempotency ALTER COLUMN status SET NOT NULL")
            cur.execute("ALTER TABLE gpucall_idempotency ALTER COLUMN content SET NOT NULL")
            cur.execute("ALTER TABLE gpucall_idempotency ALTER COLUMN headers SET NOT NULL")
            cur.execute("CREATE INDEX IF NOT EXISTS gpucall_idempotency_created_at_idx ON gpucall_idempotency(created_at)")
        self._conn.commit()

    def get(self, key: str, *, ttl_seconds: float, max_entries: int) -> tuple[str, int, dict[str, Any], dict[str, str]] | None:
        with self._lock:
            now = time.time()
            self.prune(now, ttl_seconds=ttl_seconds, max_entries=max_entries)
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT created_at, request_hash, status, content::text, headers::text FROM gpucall_idempotency WHERE key = %s",
                    (key,),
                )
                row = cur.fetchone()
            if row is None:
                return None
            created_at, request_hash, status, content, headers = row
            if now - float(created_at) > ttl_seconds:
                with self._conn.cursor() as cur:
                    cur.execute("DELETE FROM gpucall_idempotency WHERE key = %s", (key,))
                self._conn.commit()
                return None
            return str(request_hash), int(status), json.loads(content), json.loads(headers)

    def set(
        self,
        key: str,
        *,
        request_hash: str,
        status: int,
        content: dict[str, Any],
        headers: dict[str, str],
        max_entries: int,
    ) -> None:
        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO gpucall_idempotency(key, created_at, request_hash, status, content, headers)
                    VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb)
                    ON CONFLICT(key) DO UPDATE SET
                      created_at=excluded.created_at,
                      request_hash=excluded.request_hash,
                      status=excluded.status,
                      content=excluded.content,
                      headers=excluded.headers
                    """,
                    (
                        key,
                        time.time(),
                        request_hash,
                        int(status),
                        json.dumps(content, sort_keys=True, separators=(",", ":")),
                        json.dumps(headers, sort_keys=True, separators=(",", ":")),
                    ),
                )
            self._conn.commit()
            self.prune(time.time(), ttl_seconds=float("inf"), max_entries=max_entries)

    def prune(self, now: float, *, ttl_seconds: float, max_entries: int) -> None:
        with self._lock:
            with self._conn.cursor() as cur:
                if ttl_seconds != float("inf"):
                    cur.execute("DELETE FROM gpucall_idempotency WHERE created_at < %s", (now - ttl_seconds,))
                cur.execute(
                    """
                    DELETE FROM gpucall_idempotency
                    WHERE key IN (
                      SELECT key FROM gpucall_idempotency ORDER BY created_at DESC OFFSET %s
                    )
                    """,
                    (max_entries,),
                )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
