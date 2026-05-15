from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Iterable

from gpucall.domain import ArtifactManifest


class SQLiteArtifactRegistry:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def append(self, manifest: ArtifactManifest) -> ArtifactManifest:
        if manifest.version.strip().lower() == "latest":
            raise ValueError("artifact version must be explicit; 'latest' is not allowed")
        payload = json.dumps(manifest.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        with sqlite3.connect(self.path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                INSERT INTO artifacts (artifact_id, artifact_chain_id, version, classification, payload)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    manifest.artifact_id,
                    manifest.artifact_chain_id,
                    manifest.version,
                    manifest.classification.value,
                    payload,
                ),
            )
        return manifest

    def compare_and_set_latest(self, artifact_chain_id: str, *, expected_version: str | None, new_version: str) -> bool:
        if new_version.strip().lower() == "latest":
            raise ValueError("latest pointer target must be an explicit version")
        with sqlite3.connect(self.path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("BEGIN IMMEDIATE")
            if expected_version is None:
                # Use standard INSERT OR IGNORE and check rowcount
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO artifact_latest (artifact_chain_id, version) VALUES (?, ?)",
                    (artifact_chain_id, new_version),
                )
                return cursor.rowcount == 1
            cursor = conn.execute(
                """
                UPDATE artifact_latest
                SET version = ?
                WHERE artifact_chain_id = ? AND version = ?
                """,
                (new_version, artifact_chain_id, expected_version),
            )
            return cursor.rowcount == 1

    def latest_version(self, artifact_chain_id: str) -> str | None:
        with sqlite3.connect(self.path) as conn:
            row = conn.execute(
                "SELECT version FROM artifact_latest WHERE artifact_chain_id = ?", (artifact_chain_id,)
            ).fetchone()
        return row[0] if row else None

    def get(self, artifact_id: str) -> ArtifactManifest | None:
        with sqlite3.connect(self.path) as conn:
            row = conn.execute("SELECT payload FROM artifacts WHERE artifact_id = ?", (artifact_id,)).fetchone()
        if row is None:
            return None
        return ArtifactManifest.model_validate_json(row[0])

    def list_chain(self, artifact_chain_id: str) -> Iterable[ArtifactManifest]:
        with sqlite3.connect(self.path) as conn:
            rows = conn.execute(
                """
                SELECT payload FROM artifacts
                WHERE artifact_chain_id = ?
                ORDER BY created_at, version
                """,
                (artifact_chain_id,),
            ).fetchall()
        for row in rows:
            yield ArtifactManifest.model_validate_json(row[0])

    def _init_db(self) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    artifact_chain_id TEXT NOT NULL,
                    version TEXT NOT NULL,
                    classification TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS artifact_latest (
                    artifact_chain_id TEXT PRIMARY KEY,
                    version TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_artifacts_chain ON artifacts(artifact_chain_id, created_at, version)"
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_artifacts_chain_version ON artifacts(artifact_chain_id, version)"
            )


class PostgresArtifactRegistry:
    def __init__(self, dsn: str) -> None:
        import psycopg

        self._conn = psycopg.connect(dsn)
        self._init_db()

    def append(self, manifest: ArtifactManifest) -> ArtifactManifest:
        if manifest.version.strip().lower() == "latest":
            raise ValueError("artifact version must be explicit; 'latest' is not allowed")
        payload = json.dumps(manifest.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO gpucall_artifacts (artifact_id, artifact_chain_id, version, classification, payload)
                VALUES (%s, %s, %s, %s, %s::jsonb)
                """,
                (
                    manifest.artifact_id,
                    manifest.artifact_chain_id,
                    manifest.version,
                    manifest.classification.value,
                    payload,
                ),
            )
        self._conn.commit()
        return manifest

    def compare_and_set_latest(self, artifact_chain_id: str, *, expected_version: str | None, new_version: str) -> bool:
        if new_version.strip().lower() == "latest":
            raise ValueError("latest pointer target must be an explicit version")
        with self._conn.cursor() as cur:
            if expected_version is None:
                cur.execute(
                    """
                    INSERT INTO gpucall_artifact_latest (artifact_chain_id, version)
                    VALUES (%s, %s)
                    ON CONFLICT(artifact_chain_id) DO NOTHING
                    """,
                    (artifact_chain_id, new_version),
                )
                changed = cur.rowcount == 1
            else:
                cur.execute(
                    """
                    UPDATE gpucall_artifact_latest
                    SET version = %s
                    WHERE artifact_chain_id = %s AND version = %s
                    """,
                    (new_version, artifact_chain_id, expected_version),
                )
                changed = cur.rowcount == 1
        self._conn.commit()
        return changed

    def latest_version(self, artifact_chain_id: str) -> str | None:
        with self._conn.cursor() as cur:
            cur.execute("SELECT version FROM gpucall_artifact_latest WHERE artifact_chain_id = %s", (artifact_chain_id,))
            row = cur.fetchone()
        return row[0] if row else None

    def get(self, artifact_id: str) -> ArtifactManifest | None:
        with self._conn.cursor() as cur:
            cur.execute("SELECT payload::text FROM gpucall_artifacts WHERE artifact_id = %s", (artifact_id,))
            row = cur.fetchone()
        if row is None:
            return None
        return ArtifactManifest.model_validate_json(row[0])

    def list_chain(self, artifact_chain_id: str) -> Iterable[ArtifactManifest]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT payload::text FROM gpucall_artifacts
                WHERE artifact_chain_id = %s
                ORDER BY created_at, version
                """,
                (artifact_chain_id,),
            )
            rows = cur.fetchall()
        for row in rows:
            yield ArtifactManifest.model_validate_json(row[0])

    def close(self) -> None:
        self._conn.close()

    def _init_db(self) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS gpucall_artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    artifact_chain_id TEXT NOT NULL,
                    version TEXT NOT NULL,
                    classification TEXT NOT NULL,
                    payload JSONB NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS gpucall_artifact_latest (
                    artifact_chain_id TEXT PRIMARY KEY,
                    version TEXT NOT NULL
                )
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS gpucall_artifacts_chain_idx ON gpucall_artifacts(artifact_chain_id, created_at, version)")
            cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS gpucall_artifacts_chain_version_idx ON gpucall_artifacts(artifact_chain_id, version)")
        self._conn.commit()


def build_artifact_registry(state_dir: Path) -> SQLiteArtifactRegistry | PostgresArtifactRegistry:
    database_url = os.getenv("GPUCALL_DATABASE_URL") or os.getenv("DATABASE_URL")
    if database_url and database_url.startswith(("postgres://", "postgresql://")):
        return PostgresArtifactRegistry(database_url)
    return SQLiteArtifactRegistry(state_dir / "artifacts.db")
