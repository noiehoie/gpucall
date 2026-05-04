from __future__ import annotations

import json
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
            current = conn.execute(
                "SELECT version FROM artifact_latest WHERE artifact_chain_id = ?", (artifact_chain_id,)
            ).fetchone()
            current_version = current[0] if current else None
            if current_version != expected_version:
                return False
            conn.execute(
                """
                INSERT INTO artifact_latest (artifact_chain_id, version)
                VALUES (?, ?)
                ON CONFLICT(artifact_chain_id) DO UPDATE SET version = excluded.version
                """,
                (artifact_chain_id, new_version),
            )
            return True

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
