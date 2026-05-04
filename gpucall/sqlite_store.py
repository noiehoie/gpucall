from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from gpucall.dispatcher import JobStore
from gpucall.domain import CompiledPlan, JobRecord, JobState


class SQLiteJobStore(JobStore):
    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
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
        job = JobRecord(job_id=uuid4().hex, state=JobState.PENDING, plan=plan, owner_identity=owner_identity)
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
