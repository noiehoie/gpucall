from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from fastapi import FastAPI

from gpucall.config import load_config
from gpucall.credentials import load_credentials
from gpucall.live_catalog_scope import live_catalog_scope
from gpucall.panopticon import default_panopticon_path, load_panopticon_evidence, store_panopticon_evidence
from gpucall.tuple_catalog import live_tuple_catalog_evidence


PANOPTICON_DEFAULT_HOST = "127.0.0.1"
PANOPTICON_DEFAULT_PORT = 18090
PANOPTICON_DEFAULT_REFRESH_INTERVAL_SECONDS = 300
PANOPTICON_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}


def snapshot_panopticon(
    *,
    panopticon_path: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    snapshot = load_panopticon_evidence(panopticon_path, now=now)
    path = panopticon_path or default_panopticon_path()
    return _report(
        phase="provider-panopticon-snapshot",
        config_dir=None,
        snapshot_path=path,
        observed=None,
        snapshot=snapshot,
        now=now,
    )


def refresh_panopticon(
    *,
    config_dir: Path,
    panopticon_path: Path | None = None,
    tuple_names: Iterable[str] | None = None,
    ttl_seconds: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    config = load_config(config_dir)
    scope = live_catalog_scope(config, config_dir)
    selected = _selected_scope(scope, tuple_names)
    observed = live_tuple_catalog_evidence(selected, load_credentials()) if selected else {}
    path = panopticon_path or default_panopticon_path()
    if observed:
        store_panopticon_evidence(observed, path, ttl_seconds=ttl_seconds, now=now)
    snapshot = load_panopticon_evidence(path, now=now)
    return _report(
        phase="provider-panopticon-refresh",
        config_dir=config_dir,
        snapshot_path=path,
        observed=observed,
        snapshot=snapshot,
        now=now,
        scope_tuple_count=len(scope),
        selected_tuple_count=len(selected),
    )


def create_panopticon_app(
    *,
    config_dir: Path,
    panopticon_path: Path | None = None,
    refresh_interval_seconds: int | None = None,
) -> FastAPI:
    path = panopticon_path or default_panopticon_path()
    refresh_lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.last_refresh_error = None
        app.state.last_refresh_report = None
        task: asyncio.Task[None] | None = None
        if refresh_interval_seconds is not None:
            task = asyncio.create_task(_refresh_loop(app, refresh_lock, config_dir, path, refresh_interval_seconds))
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(title="gpucall Provider Panopticon", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {
            "status": "ok",
            "service": "provider-panopticon",
            "snapshot_path": str(path),
            "refresh_interval_seconds": refresh_interval_seconds,
            "last_refresh_error": app.state.last_refresh_error,
        }

    @app.get("/v1/snapshot")
    async def snapshot() -> dict[str, Any]:
        return await asyncio.to_thread(snapshot_panopticon, panopticon_path=path)

    @app.post("/v1/refresh")
    async def refresh() -> dict[str, Any]:
        async with refresh_lock:
            report = await asyncio.to_thread(refresh_panopticon, config_dir=config_dir, panopticon_path=path)
            app.state.last_refresh_error = None
            app.state.last_refresh_report = report
            return report

    return app


def assert_safe_panopticon_host(host: str) -> None:
    if host not in PANOPTICON_ALLOWED_HOSTS:
        allowed = ", ".join(sorted(PANOPTICON_ALLOWED_HOSTS))
        raise ValueError(f"provider panopticon serve host must be local-only ({allowed})")


async def _refresh_loop(
    app: FastAPI,
    refresh_lock: asyncio.Lock,
    config_dir: Path,
    panopticon_path: Path,
    refresh_interval_seconds: int,
) -> None:
    if refresh_interval_seconds < 1:
        raise ValueError("refresh_interval_seconds must be >= 1")
    while True:
        try:
            async with refresh_lock:
                report = await asyncio.to_thread(refresh_panopticon, config_dir=config_dir, panopticon_path=panopticon_path)
                app.state.last_refresh_error = None
                app.state.last_refresh_report = report
        except Exception as exc:
            app.state.last_refresh_error = f"{type(exc).__name__}: {exc}"
        await asyncio.sleep(refresh_interval_seconds)


def _selected_scope(scope: dict[str, Any], tuple_names: Iterable[str] | None) -> dict[str, Any]:
    names = [name for name in (tuple_names or []) if name]
    if not names:
        return dict(scope)
    missing = sorted({name for name in names if name not in scope})
    if missing:
        raise ValueError(f"unknown tuple(s): {', '.join(missing)}")
    return {name: scope[name] for name in names}


def _report(
    *,
    phase: str,
    config_dir: Path | None,
    snapshot_path: Path,
    observed: dict[str, dict[str, Any]] | None,
    snapshot: dict[str, dict[str, Any]],
    now: datetime | None,
    scope_tuple_count: int | None = None,
    selected_tuple_count: int | None = None,
) -> dict[str, Any]:
    generated_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    status_counts: dict[str, int] = {}
    dimension_counts: dict[str, int] = {}
    stale_tuple_count = 0
    for row in snapshot.values():
        status = str(row.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
        if row.get("panopticon_stale") is True:
            stale_tuple_count += 1
        for dimension in row.get("dimensions") or []:
            key = str(dimension)
            dimension_counts[key] = dimension_counts.get(key, 0) + 1
    report: dict[str, Any] = {
        "schema_version": 1,
        "phase": phase,
        "generated_at": generated_at,
        "snapshot_path": str(snapshot_path),
        "non_generation_probe_only": True,
        "tuple_count": len(snapshot),
        "status_counts": dict(sorted(status_counts.items())),
        "dimension_counts": dict(sorted(dimension_counts.items())),
        "stale_tuple_count": stale_tuple_count,
        "snapshot": snapshot,
    }
    if config_dir is not None:
        report["config_dir"] = str(config_dir)
    if observed is not None:
        report["observed_tuple_count"] = len(observed)
    if scope_tuple_count is not None:
        report["scope_tuple_count"] = scope_tuple_count
    if selected_tuple_count is not None:
        report["selected_tuple_count"] = selected_tuple_count
    return report


def dumps_panopticon_report(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2, sort_keys=True) + "\n"
