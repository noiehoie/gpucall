from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from gpucall.config import GpucallConfig


class SQLiteCapabilityCatalog:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def replace_from_config(self, config: GpucallConfig) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute("DELETE FROM recipes")
            conn.execute("DELETE FROM models")
            conn.execute("DELETE FROM engines")
            conn.execute("DELETE FROM providers")
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
                    INSERT INTO providers(name, adapter, model_ref, engine_ref, payload)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (provider.name, provider.adapter, provider.model_ref, provider.engine_ref, provider.model_dump_json()),
                )

    def snapshot(self) -> dict[str, Any]:
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            return {
                "path": str(self.path),
                "recipes": _rows(conn.execute("SELECT name, task FROM recipes ORDER BY name")),
                "models": _rows(conn.execute("SELECT name, provider_model_id FROM models ORDER BY name")),
                "engines": _rows(conn.execute("SELECT name, kind FROM engines ORDER BY name")),
                "providers": _rows(conn.execute("SELECT name, adapter, model_ref, engine_ref FROM providers ORDER BY name")),
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
                    model_ref TEXT,
                    engine_ref TEXT,
                    payload TEXT NOT NULL
                )
                """
            )


def _rows(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    return [dict(row) for row in cursor.fetchall()]


def dumps_snapshot(snapshot: dict[str, Any]) -> str:
    return json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
