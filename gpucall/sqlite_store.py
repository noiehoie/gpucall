from __future__ import annotations

import asyncio
import json
import time
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from gpucall.dispatcher import JobStore
from gpucall.domain import CompiledPlan, JobRecord, JobState
from gpucall.sqlite_utils import connect_sqlite


class SQLiteJobStore(JobStore):
    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._conn = connect_sqlite(self.path, check_same_thread=False)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
              job_id TEXT PRIMARY KEY,
              state TEXT NOT NULL,
              payload TEXT NOT NULL,
              updated_at TEXT NOT NULL
            )
            """
        )
        self._conn.commit()

    async def create(self, plan: CompiledPlan, *, owner_identity: str | None = None) -> JobRecord:
        job = JobRecord(job_id=uuid4().hex, state=JobState.QUEUED, plan=plan, owner_identity=owner_identity)
        async with self._lock:
            self._upsert(job)
        return job

    async def get(self, job_id: str) -> JobRecord | None:
        async with self._lock:
            row = self._conn.execute("SELECT payload FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
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
            rows = self._conn.execute("SELECT payload FROM jobs ORDER BY updated_at").fetchall()
        return [JobRecord.model_validate_json(row[0]) for row in rows]

    def _upsert(self, job: JobRecord) -> None:
        self._conn.execute(
            """
            INSERT INTO jobs(job_id, state, payload, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
              state=excluded.state,
              payload=excluded.payload,
              updated_at=excluded.updated_at
            """,
            (job.job_id, job.state.value, job.model_dump_json(), job.updated_at.isoformat()),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


class SQLiteIdempotencyStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = connect_sqlite(self.path, check_same_thread=False)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS idempotency_entries (
              key TEXT PRIMARY KEY,
              created_at REAL NOT NULL,
              request_hash TEXT NOT NULL,
              status INTEGER,
              content TEXT,
              headers TEXT,
              idempotency_status TEXT NOT NULL DEFAULT 'completed'
            )
            """
        )
        self._migrate_schema()
        self._conn.commit()

    def _migrate_schema(self) -> None:
        info = self._conn.execute("PRAGMA table_info(idempotency_entries)").fetchall()
        columns = {str(row[1]) for row in info}
        if "idempotency_status" not in columns:
            self._conn.execute("ALTER TABLE idempotency_entries ADD COLUMN idempotency_status TEXT NOT NULL DEFAULT 'completed'")
        nullable_required = {"status", "content", "headers"}
        not_null_columns = {str(row[1]) for row in info if str(row[1]) in nullable_required and int(row[3]) == 1}
        if not_null_columns:
            self._conn.execute("ALTER TABLE idempotency_entries RENAME TO idempotency_entries_old")
            self._conn.execute(
                """
                CREATE TABLE idempotency_entries (
                  key TEXT PRIMARY KEY,
                  created_at REAL NOT NULL,
                  request_hash TEXT NOT NULL,
                  status INTEGER,
                  content TEXT,
                  headers TEXT,
                  idempotency_status TEXT NOT NULL DEFAULT 'completed'
                )
                """
            )
            self._conn.execute(
                """
                INSERT INTO idempotency_entries(key, created_at, request_hash, status, content, headers, idempotency_status)
                SELECT key, created_at, request_hash, status, content, headers, COALESCE(idempotency_status, 'completed')
                FROM idempotency_entries_old
                """
            )
            self._conn.execute("DROP TABLE idempotency_entries_old")

    def get(
        self,
        key: str,
        *,
        ttl_seconds: float,
        max_entries: int,
    ) -> tuple[str, int, dict[str, Any], dict[str, str], str] | None:
        with self._lock:
            now = time.time()
            self.prune(now, ttl_seconds=ttl_seconds, max_entries=max_entries)
            row = self._conn.execute(
                "SELECT created_at, request_hash, status, content, headers, idempotency_status FROM idempotency_entries WHERE key = ?",
                (key,),
            ).fetchone()
            if row is None:
                return None
            created_at, request_hash, status, content, headers, idempotency_status = row
            if now - float(created_at) > ttl_seconds:
                self._conn.execute("DELETE FROM idempotency_entries WHERE key = ?", (key,))
                self._conn.commit()
                return None
            return (
                str(request_hash),
                int(status) if status is not None else 0,
                json.loads(content) if content else {},
                json.loads(headers) if headers else {},
                str(idempotency_status),
            )

    def reserve(self, key: str, *, request_hash: str, max_entries: int) -> bool:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            cursor = self._conn.execute(
                """
                INSERT OR IGNORE INTO idempotency_entries(key, created_at, request_hash, idempotency_status)
                VALUES (?, ?, ?, 'pending')
                """,
                (key, time.time(), request_hash),
            )
            self._conn.commit()
            self.prune(time.time(), ttl_seconds=float("inf"), max_entries=max_entries)
            return cursor.rowcount == 1

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
            # Only update if it's currently 'pending' or doesn't exist
            self._conn.execute(
                """
                INSERT INTO idempotency_entries(key, created_at, request_hash, status, content, headers, idempotency_status)
                VALUES (?, ?, ?, ?, ?, ?, 'completed')
                ON CONFLICT(key) DO UPDATE SET
                    status=excluded.status,
                    content=excluded.content,
                    headers=excluded.headers,
                    idempotency_status='completed',
                    created_at=excluded.created_at
                WHERE idempotency_status = 'pending' AND request_hash = excluded.request_hash
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

    def release(self, key: str, *, request_hash: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM idempotency_entries WHERE key = ? AND request_hash = ? AND idempotency_status = 'pending'",
                (key, request_hash),
            )
            self._conn.commit()

    def prune(self, now: float, *, ttl_seconds: float, max_entries: int) -> None:
        with self._lock:
            cutoff = now - ttl_seconds
            if ttl_seconds != float("inf"):
                self._conn.execute("DELETE FROM idempotency_entries WHERE created_at < ?", (cutoff,))
            rows = self._conn.execute(
                "SELECT key FROM idempotency_entries ORDER BY created_at DESC LIMIT -1 OFFSET ?",
                (max_entries,),
            ).fetchall()
            if rows:
                self._conn.executemany("DELETE FROM idempotency_entries WHERE key = ?", rows)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
