from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import yaml

from gpucall.config import GpucallConfig
from gpucall.execution.registry import adapter_descriptor


class SQLiteCapabilityCatalog:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def replace_from_config(self, config: GpucallConfig, *, config_dir: str | Path | None = None) -> None:
        root = Path(config_dir) if config_dir else None
        with sqlite3.connect(self.path) as conn:
            conn.execute("DELETE FROM recipes")
            conn.execute("DELETE FROM models")
            conn.execute("DELETE FROM engines")
            conn.execute("DELETE FROM providers")
            conn.execute("DELETE FROM provider_candidates")
            for recipe in config.recipes.values():
                conn.execute(
                    "INSERT INTO recipes(name, task, payload) VALUES (?, ?, ?)",
                    (recipe.name, recipe.task, recipe.model_dump_json()),
                )
            for model in config.models.values():
                conn.execute(
                    "INSERT INTO models(name, provider_model_id, payload) VALUES (?, ?, ?)",
                    (model.name, model.provider_model_id, model.model_dump_json()),
                )
            for engine in config.engines.values():
                conn.execute(
                    "INSERT INTO engines(name, kind, payload) VALUES (?, ?, ?)",
                    (engine.name, engine.kind, engine.model_dump_json()),
                )
            for provider in config.providers.values():
                conn.execute(
                    """
                    INSERT INTO providers(name, adapter, execution_surface, model_ref, engine_ref, payload)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        provider.name,
                        provider.adapter,
                        provider.execution_surface.value if provider.execution_surface else None,
                        provider.model_ref,
                        provider.engine_ref,
                        provider.model_dump_json(),
                    ),
                )
            if root is not None:
                for candidate in _load_candidate_payloads(root / "provider_candidates"):
                    conn.execute(
                        """
                        INSERT INTO provider_candidates(name, adapter, execution_surface, model_ref, engine_ref, status, payload)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            candidate.get("name"),
                            candidate.get("adapter"),
                            candidate.get("execution_surface") or _surface_for_adapter(str(candidate.get("adapter") or "")),
                            candidate.get("model_ref"),
                            candidate.get("engine_ref"),
                            candidate.get("status", "candidate"),
                            json.dumps(candidate, ensure_ascii=False, sort_keys=True),
                        ),
                    )

    def snapshot(self) -> dict[str, Any]:
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            return {
                "path": str(self.path),
                "recipes": _rows(conn.execute("SELECT name, task FROM recipes ORDER BY name")),
                "models": _rows(conn.execute("SELECT name, provider_model_id FROM models ORDER BY name")),
                "engines": _rows(conn.execute("SELECT name, kind FROM engines ORDER BY name")),
                "providers": _rows(
                    conn.execute("SELECT name, adapter, execution_surface, model_ref, engine_ref FROM providers ORDER BY name")
                ),
                "provider_candidates": _rows(
                    conn.execute(
                        "SELECT name, adapter, execution_surface, model_ref, engine_ref, status FROM provider_candidates ORDER BY name"
                    )
                ),
            }

    def _init(self) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS recipes (name TEXT PRIMARY KEY, task TEXT NOT NULL, payload TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS models (name TEXT PRIMARY KEY, provider_model_id TEXT NOT NULL, payload TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS engines (name TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS providers (
                    name TEXT PRIMARY KEY,
                    adapter TEXT NOT NULL,
                    execution_surface TEXT,
                    model_ref TEXT,
                    engine_ref TEXT,
                    payload TEXT NOT NULL
                )
                """
            )
            _ensure_column(conn, "providers", "execution_surface", "TEXT")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS provider_candidates (
                    name TEXT PRIMARY KEY,
                    adapter TEXT NOT NULL,
                    execution_surface TEXT,
                    model_ref TEXT,
                    engine_ref TEXT,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL
                )
                """
            )
            _ensure_column(conn, "provider_candidates", "execution_surface", "TEXT")


def _rows(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    return [dict(row) for row in cursor.fetchall()]


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, declaration: str) -> None:
    existing = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def _surface_for_adapter(adapter: str) -> str | None:
    descriptor = adapter_descriptor(adapter)
    if descriptor is None or descriptor.execution_surface is None:
        return None
    return descriptor.execution_surface.value


def _load_candidate_payloads(root: Path) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    if not root.exists():
        return payloads
    for path in sorted(root.glob("*.yml")):
        with path.open("r", encoding="utf-8") as handle:
            payload = yaml.safe_load(handle) or {}
        if not isinstance(payload, dict):
            raise ValueError(f"invalid candidate payload in {path}")
        if not payload.get("name") or not payload.get("adapter"):
            raise ValueError(f"candidate provider must define name and adapter: {path}")
        payloads.append(payload)
    return payloads


def dumps_snapshot(snapshot: dict[str, Any]) -> str:
    return json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
