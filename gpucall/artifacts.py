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
                "CREATE INDEX IF NOT EXISTS idx_artifacts_chain ON artifacts(artifact_chain_id, created_at, version)"
            )
