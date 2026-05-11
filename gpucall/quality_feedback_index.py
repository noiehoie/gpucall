from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class QualityFeedbackIndex:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS quality_feedback (
                  feedback_id TEXT PRIMARY KEY,
                  source TEXT,
                  task TEXT,
                  intent TEXT,
                  quality_kind TEXT,
                  observed_tuple TEXT,
                  status TEXT NOT NULL,
                  original_path TEXT NOT NULL,
                  report_path TEXT,
                  original_sha256 TEXT NOT NULL,
                  received_at TEXT NOT NULL,
                  processed_at TEXT,
                  error TEXT,
                  updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_quality_feedback_status ON quality_feedback(status, updated_at)")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def upsert_pending(self, path: str | Path, submission: Mapping[str, Any]) -> dict[str, str]:
        source_path = Path(path)
        metadata = feedback_metadata(source_path, submission)
        now = _now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO quality_feedback(
                  feedback_id, source, task, intent, quality_kind, observed_tuple, status,
                  original_path, original_sha256, received_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
                ON CONFLICT(feedback_id) DO UPDATE SET
                  source=excluded.source,
                  task=excluded.task,
                  intent=excluded.intent,
                  quality_kind=excluded.quality_kind,
                  observed_tuple=excluded.observed_tuple,
                  status='pending',
                  original_path=excluded.original_path,
                  original_sha256=excluded.original_sha256,
                  received_at=excluded.received_at,
                  report_path=NULL,
                  processed_at=NULL,
                  error=NULL,
                  updated_at=excluded.updated_at
                """,
                (
                    metadata["feedback_id"],
                    metadata["source"],
                    metadata["task"],
                    metadata["intent"],
                    metadata["quality_kind"],
                    metadata["observed_tuple"],
                    str(source_path),
                    file_sha256(source_path),
                    _file_time(source_path),
                    now,
                ),
            )
        return metadata

    def mark_processed(self, feedback_id: str, *, original_path: str | Path, report_path: str | Path) -> None:
        self._mark_terminal(feedback_id, status="processed", original_path=original_path, report_path=report_path, error=None)

    def mark_failed(self, feedback_id: str, *, original_path: str | Path, error: str, report_path: str | Path | None = None) -> None:
        self._mark_terminal(feedback_id, status="failed", original_path=original_path, report_path=report_path, error=error)

    def _mark_terminal(
        self,
        feedback_id: str,
        *,
        status: str,
        original_path: str | Path,
        report_path: str | Path | None,
        error: str | None,
    ) -> None:
        processed_at = _now()
        with self._connect() as conn:
            row = conn.execute("SELECT feedback_id FROM quality_feedback WHERE feedback_id = ?", (feedback_id,)).fetchone()
            if row is None:
                source = Path(original_path)
                conn.execute(
                    """
                    INSERT INTO quality_feedback(
                      feedback_id, source, task, intent, quality_kind, observed_tuple, status,
                      original_path, report_path, original_sha256, received_at, processed_at, error, updated_at
                    )
                    VALUES (?, '', '', '', '', '', ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        feedback_id,
                        status,
                        str(source),
                        str(report_path) if report_path else None,
                        file_sha256(source) if source.exists() else "",
                        _file_time(source) if source.exists() else processed_at,
                        processed_at,
                        error,
                        processed_at,
                    ),
                )
                return
            conn.execute(
                """
                UPDATE quality_feedback
                SET status = ?,
                    original_path = ?,
                    report_path = ?,
                    processed_at = ?,
                    error = ?,
                    updated_at = ?
                WHERE feedback_id = ?
                """,
                (
                    status,
                    str(original_path),
                    str(report_path) if report_path else None,
                    processed_at,
                    error,
                    processed_at,
                    feedback_id,
                ),
            )

    def get(self, feedback_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM quality_feedback WHERE feedback_id = ?", (feedback_id,)).fetchone()
        return dict(row) if row is not None else None

    def list(self, *, status: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if status is None:
                rows = conn.execute("SELECT * FROM quality_feedback ORDER BY updated_at DESC").fetchall()
            else:
                rows = conn.execute("SELECT * FROM quality_feedback WHERE status = ? ORDER BY updated_at DESC", (status,)).fetchall()
        return [dict(row) for row in rows]


def default_quality_feedback_index_path(inbox_dir: str | Path) -> Path:
    return Path(inbox_dir) / "quality_feedback.db"


def feedback_metadata(path: str | Path, submission: Mapping[str, Any]) -> dict[str, str]:
    source_path = Path(path)
    artifact = _artifact_from_submission(submission)
    sanitized = _mapping(artifact.get("sanitized_request"))
    quality = _mapping(sanitized.get("quality_feedback"))
    runtime = _mapping(sanitized.get("runtime_selection"))
    return {
        "feedback_id": str(submission.get("request_id") or source_path.stem),
        "source": str(submission.get("source") or artifact.get("source") or sanitized.get("source") or ""),
        "task": str(sanitized.get("task") or artifact.get("task") or ""),
        "intent": str(sanitized.get("intent") or artifact.get("intent") or ""),
        "quality_kind": str(quality.get("kind") or ""),
        "observed_tuple": str(runtime.get("observed_tuple") or ""),
    }


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_time(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _artifact_from_submission(data: Mapping[str, Any]) -> Mapping[str, Any]:
    if data.get("kind") == "gpucall.recipe_request_submission":
        intake = data.get("intake")
        return intake if isinstance(intake, Mapping) else {}
    return data


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}
