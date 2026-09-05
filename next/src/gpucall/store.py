"""Private, durable owner state; SQLite transactions serialize budget reservations."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .kernel import KERNEL_VERSION, Rejected, compile_plan
from .models import Bundle, Evidence, Profile, Request, canonical, digest

MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
TERMINAL = {"succeeded", "failed", "cancelled"}


def private_dir(path: Path) -> None:
    if path.is_symlink():
        raise ValueError("state directories must not be symlinks")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def check_space(path: Path, bytes_needed: int = 0) -> None:
    total, used, free = shutil.disk_usage(path)
    if free - bytes_needed < total // 20:
        raise Rejected("disk_capacity_limit")


class Artifacts:
    def __init__(self, root: Path):
        self.root = root
        private_dir(root)

    def path(self, sha: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise Rejected("invalid_artifact_id")
        return self.root / sha

    def put(self, data: bytes) -> str:
        if len(data) > MAX_ARTIFACT_BYTES:
            raise Rejected("artifact_too_large")
        check_space(self.root, len(data))
        sha = hashlib.sha256(data).hexdigest()
        target = self.path(sha)
        if target.exists():
            self.get(sha)
            return sha
        fd, temporary = tempfile.mkstemp(dir=self.root, prefix=".write-")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            directory_fd = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return sha

    def get(self, sha: str) -> bytes:
        path = self.path(sha)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as handle:
            data = handle.read(MAX_ARTIFACT_BYTES + 1)
        if len(data) > MAX_ARTIFACT_BYTES or hashlib.sha256(data).hexdigest() != sha:
            raise Rejected("artifact_integrity_failed")
        return data


class Store:
    def __init__(self, root: Path, limit_microusd: int):
        self.root = root
        private_dir(root)
        self.path = root / "jobs.sqlite3"
        if self.path.is_symlink():
            raise ValueError("database must not be a symlink")
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        self.path.chmod(0o600)
        self.artifacts = Artifacts(root / "artifacts")
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS account(id INTEGER PRIMARY KEY CHECK(id=1), ceiling INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs(
                    id TEXT PRIMARY KEY, key TEXT UNIQUE NOT NULL, request_hash TEXT NOT NULL,
                    status TEXT NOT NULL, reserved INTEGER NOT NULL CHECK(reserved>=0),
                    charged INTEGER NOT NULL CHECK(charged>=0), version INTEGER NOT NULL,
                    cancelled INTEGER NOT NULL DEFAULT 0, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events(
                    seq INTEGER PRIMARY KEY, job TEXT NOT NULL, at_ms INTEGER NOT NULL,
                    kind TEXT NOT NULL, body TEXT NOT NULL, previous TEXT NOT NULL, hash TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS evidence(job TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
                    BEGIN SELECT RAISE(ABORT,'append-only events'); END;
                CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
                    BEGIN SELECT RAISE(ABORT,'append-only events'); END;
            """)
            db.execute("INSERT INTO account VALUES(1,?) ON CONFLICT(id) DO UPDATE SET ceiling=excluded.ceiling", (limit_microusd,))

    @contextmanager
    def connection(self):
        check_space(self.root)
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        try:
            yield db
        finally:
            db.close()

    @staticmethod
    def event(db, job: str, at_ms: int, kind: str, payload: dict):
        last = db.execute("SELECT hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        previous = last[0] if last else "0" * 64
        body = canonical(payload)
        hashed = digest({"job": job, "at_ms": at_ms, "kind": kind, "body": body, "previous": previous})
        db.execute("INSERT INTO events(job,at_ms,kind,body,previous,hash) VALUES(?,?,?,?,?,?)",
                   (job, at_ms, kind, body, previous, hashed))

    def submit(self, request: Request, bundle: Bundle, now_ms: int, *, validation_execution=None) -> dict:
        identity = digest({"request": request.model_dump(mode="json"), "validation_execution": validation_execution})
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                existing = db.execute("SELECT * FROM jobs WHERE key=?", (request.key,)).fetchone()
                if existing:
                    if existing["request_hash"] != identity:
                        raise Rejected("idempotency_conflict")
                    db.commit()
                    return self.decode(existing)
                profile = next((p for p in bundle.profiles if p.id == request.profile), None)
                if profile is None:
                    raise Rejected("unknown_profile")
                for item in request.inputs:
                    if len(self.artifacts.get(item.sha256)) != item.size:
                        raise Rejected("input_size_mismatch")
                evidence = tuple(Evidence.model_validate_json(r[0]) for r in db.execute("SELECT body FROM evidence ORDER BY job"))
                snapshot = bundle.snapshot.model_copy(update={"at_ms": now_ms, "evidence": (*bundle.snapshot.evidence, *evidence)})
                plan = compile_plan(request, profile, snapshot, allow_fixture=bundle.allow_fixture,
                                    validation_execution=validation_execution)
                committed = db.execute("SELECT COALESCE(SUM(reserved+charged),0) FROM jobs").fetchone()[0]
                ceiling = db.execute("SELECT ceiling FROM account WHERE id=1").fetchone()[0]
                if committed + plan.reservation_microusd > ceiling:
                    raise Rejected("account_budget_exceeded")
                job_id = hashlib.sha256(request.key.encode()).hexdigest()
                job = {"id": job_id, "kernel_version": KERNEL_VERSION, "request": request.model_dump(mode="json"),
                       "profile": profile.model_dump(mode="json"), "snapshot": snapshot.model_dump(mode="json"),
                       "plan": plan.model_dump(mode="json"), "allow_fixture": bundle.allow_fixture,
                       "status": "queued", "attempt": 0, "resource": None, "start_sent": False,
                       "cancel_requested": False, "version": 0, "reserved": plan.reservation_microusd,
                       "charged": 0, "output": None, "error": None, "outcome": None,
                       "actual_microusd": None, "cost_basis": "unsettled", "receipts": [],
                       "created_ms": now_ms, "updated_ms": now_ms}
                db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)", (job_id, request.key, identity,
                           "queued", job["reserved"], 0, 0, 0, canonical(job)))
                self.event(db, job_id, now_ms, "submitted", {"request_hash": digest(request), "plan_hash": digest(plan)})
                db.commit()
                return job
            except BaseException:
                db.rollback()
                raise

    @staticmethod
    def decode(row) -> dict:
        job = json.loads(row["body"])
        job.update(version=row["version"], cancel_requested=bool(row["cancelled"]))
        return job

    def get(self, job_id: str) -> dict:
        with self.connection() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return self.decode(row)

    def jobs(self, active_only=False) -> list[dict]:
        with self.connection() as db:
            rows = db.execute("SELECT * FROM jobs ORDER BY rowid").fetchall()
        return [self.decode(r) for r in rows if not active_only or r["status"] not in TERMINAL]

    def save(self, job: dict, now_ms: int, kind: str) -> dict:
        job = dict(job, updated_ms=now_ms)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job["id"],)).fetchone()
            if row["version"] != job["version"]:
                db.rollback()
                raise Rejected("state_conflict")
            # Cancellation is an independent durable flag; never overwrite a concurrent request.
            job["cancel_requested"] = bool(row["cancelled"])
            job["version"] += 1
            db.execute("UPDATE jobs SET status=?,reserved=?,charged=?,version=?,body=? WHERE id=?",
                       (job["status"], job["reserved"], job["charged"], job["version"], canonical(job), job["id"]))
            self.event(db, job["id"], now_ms, kind, {k: job[k] for k in
                       ("status", "attempt", "resource", "reserved", "charged", "output", "error")})
            db.commit()
        return job

    def cancel(self, job_id: str, now_ms: int) -> dict:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                db.rollback()
                raise KeyError(job_id)
            if row["status"] not in TERMINAL and not row["cancelled"]:
                db.execute("UPDATE jobs SET cancelled=1 WHERE id=?", (job_id,))
                self.event(db, job_id, now_ms, "cancel_requested", {})
            db.commit()
        return self.get(job_id)

    def account(self) -> dict:
        with self.connection() as db:
            reserved, charged = db.execute("SELECT COALESCE(SUM(reserved),0),COALESCE(SUM(charged),0) FROM jobs").fetchone()
            ceiling = db.execute("SELECT ceiling FROM account WHERE id=1").fetchone()[0]
        return {"ceiling_microusd": ceiling, "reserved_microusd": reserved, "charged_microusd": charged,
                "available_microusd": max(0, ceiling - reserved - charged), "overrun": reserved + charged > ceiling}

    def replay(self, job_id: str) -> dict:
        from .models import Snapshot
        job = self.get(job_id)
        if job.get("kernel_version", 1) != KERNEL_VERSION:
            raise Rejected("kernel_version_unavailable")
        plan = compile_plan(Request.model_validate(job["request"]), Profile.model_validate(job["profile"]),
                            Snapshot.model_validate(job["snapshot"]), allow_fixture=job["allow_fixture"],
                            validation_execution=job["plan"].get("validation_execution"))
        return {"job": job_id, "matches": canonical(plan) == canonical(job["plan"]), "plan_hash": digest(plan)}

    def audit(self) -> dict:
        with self.connection() as db:
            rows = db.execute("SELECT * FROM events ORDER BY seq").fetchall()
        previous = "0" * 64
        for row in rows:
            material = {k: row[k] for k in ("job", "at_ms", "kind", "body", "previous")}
            if row["previous"] != previous or digest(material) != row["hash"]:
                return {"valid": False, "at_sequence": row["seq"]}
            previous = row["hash"]
        return {"valid": True, "events": len(rows), "head": previous}

    def enrich(self, bundle: Bundle) -> Bundle:
        with self.connection() as db:
            evidence = tuple(Evidence.model_validate_json(r[0]) for r in db.execute("SELECT body FROM evidence ORDER BY job"))
        return bundle.model_copy(update={"snapshot": bundle.snapshot.model_copy(update={
            "evidence": (*bundle.snapshot.evidence, *evidence)})})

    def certify(self, job_id: str, valid_for_ms: int) -> Evidence:
        job = self.get(job_id)
        if not job["plan"].get("validation_execution") or job["status"] not in TERMINAL or not job["receipts"]:
            raise Rejected("completed_validation_required")
        if not 0 < valid_for_ms <= 7 * 86400_000:
            raise Rejected("invalid_evidence_lifetime")
        if not self.audit()["valid"]:
            raise Rejected("audit_integrity_failed")
        report = {"job": job_id, "request_hash": digest(job["request"]), "plan": job["plan"],
                  "result": job["output"], "status": job["status"], "receipts": job["receipts"]}
        artifact = self.artifacts.put(canonical(report).encode())
        evidence = Evidence(execution_hash=digest(job["plan"]["attempts"][0]["offer"]["execution"]),
                            profile_hash=job["plan"]["profile_hash"], tested_ms=job["updated_ms"],
                            expires_ms=job["updated_ms"]+valid_for_ms, passed=job["status"] == "succeeded", artifact_hash=artifact)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT body FROM evidence WHERE job=?", (job_id,)).fetchone()
            if existing:
                db.commit()
                return Evidence.model_validate_json(existing[0])
            db.execute("INSERT INTO evidence VALUES(?,?)", (job_id, canonical(evidence)))
            self.event(db, job_id, job["updated_ms"], "validation_recorded", {"evidence_hash": digest(evidence)})
            db.commit()
        return evidence
