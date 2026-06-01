from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from fastapi import FastAPI

from gpucall.config import load_config
from gpucall.credentials import configured_credentials, load_credentials
from gpucall.execution.registry import adapter_descriptor, vendor_family_for_adapter
from gpucall.live_catalog_scope import live_catalog_scope
from gpucall.panopticon import default_panopticon_path, load_panopticon_evidence, store_panopticon_evidence
from gpucall.provider_registry import provider_registry_configured_contracts
from gpucall.targeting import is_configured_target
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
    credentials = load_credentials()
    configured = _configured_contracts_from_credentials(credentials)
    configured.update(configured_credentials())
    configured.update(provider_registry_configured_contracts())
    preflight = _provider_refresh_preflight(selected, configured)
    probe_scope = {name: selected[name] for name in selected if name not in preflight["skipped_tuples"]}
    observed = live_tuple_catalog_evidence(probe_scope, credentials) if probe_scope else {}
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
        preflight=preflight,
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


def _provider_refresh_preflight(selected: dict[str, Any], configured: set[str]) -> dict[str, Any]:
    provider_counts: dict[str, int] = {}
    skipped_tuples: set[str] = set()
    credential_skipped_counts: dict[str, int] = {}
    target_skipped_counts: dict[str, int] = {}
    target_missing_fields: dict[str, set[str]] = {}
    blockers: list[dict[str, Any]] = []
    for tuple_name, tuple_spec in selected.items():
        provider = vendor_family_for_adapter(str(getattr(tuple_spec, "adapter", "") or ""))
        if provider == "local":
            continue
        provider_counts[provider] = provider_counts.get(provider, 0) + 1
        missing = _missing_provider_contracts(provider, configured)
        if missing:
            skipped_tuples.add(tuple_name)
            credential_skipped_counts[provider] = credential_skipped_counts.get(provider, 0) + 1
            continue
        missing_fields = _missing_provider_target_fields(tuple_spec)
        if not missing_fields:
            continue
        skipped_tuples.add(tuple_name)
        target_skipped_counts[provider] = target_skipped_counts.get(provider, 0) + 1
        target_missing_fields.setdefault(provider, set()).update(missing_fields)

    skipped_provider_counts: dict[str, int] = {}
    for tuple_name in skipped_tuples:
        provider = vendor_family_for_adapter(str(getattr(selected[tuple_name], "adapter", "") or ""))
        skipped_provider_counts[provider] = skipped_provider_counts.get(provider, 0) + 1

    for provider, count in sorted(credential_skipped_counts.items()):
        missing = sorted(_missing_provider_contracts(provider, configured))
        blockers.append(
            {
                "code": "PROVIDER_CREDENTIALS_MISSING",
                "owner": "gpucall-admin",
                "provider": provider,
                "tuple_count": count,
                "missing_contracts": missing,
                "next_action": _provider_missing_credentials_next_action(provider),
            }
        )
    for provider, count in sorted(target_skipped_counts.items()):
        blockers.append(
            {
                "code": "PROVIDER_ENDPOINT_TARGET_MISSING",
                "owner": "provider-ops",
                "provider": provider,
                "tuple_count": count,
                "missing_fields": sorted(target_missing_fields.get(provider, set())),
                "next_action": _provider_missing_target_next_action(provider),
            }
        )

    probe_tuple_count = max(len(selected) - len(skipped_tuples), 0)
    if blockers and probe_tuple_count == 0:
        status = "blocked"
    elif blockers:
        status = "partial"
    else:
        status = "ok"

    return {
        "status": status,
        "provider_counts": dict(sorted(provider_counts.items())),
        "skipped_provider_counts": dict(sorted(skipped_provider_counts.items())),
        "skipped_tuple_count": len(skipped_tuples),
        "skipped_tuples": skipped_tuples,
        "probe_tuple_count": probe_tuple_count,
        "blockers": blockers,
    }


def _missing_provider_contracts(provider: str, configured: set[str]) -> set[str]:
    if provider == "runpod":
        return set() if "api_key:runpod" in configured else {"api_key:runpod"}
    if provider == "modal":
        return set() if {"token_pair:modal", "sdk_profile:modal"}.intersection(configured) else {"token_pair:modal"}
    if provider == "hyperstack":
        required = {"api_key:hyperstack", "ssh_key:hyperstack"}
        return required.difference(configured)
    if provider == "azure":
        return set() if "cloud_subscription:azure" in configured else {"cloud_subscription:azure"}
    if provider == "gcp":
        return set() if "cloud_project:gcp" in configured else {"cloud_project:gcp"}
    if provider == "scaleway":
        return set() if "api_key:scaleway" in configured else {"api_key:scaleway"}
    if provider == "ovhcloud":
        return set() if "cloud_project:ovhcloud" in configured else {"cloud_project:ovhcloud"}
    return set()


def _missing_provider_target_fields(tuple_spec: Any) -> set[str]:
    descriptor = adapter_descriptor(tuple_spec)
    required = descriptor.required_auto_fields if descriptor is not None else {}
    missing: set[str] = set()
    for field in required:
        value = getattr(tuple_spec, field, None)
        if not is_configured_target(value):
            missing.add(field)
    return missing


def _configured_contracts_from_credentials(credentials: dict[str, dict[str, str]]) -> set[str]:
    configured: set[str] = set()
    runpod = credentials.get("runpod", {})
    if runpod.get("api_key"):
        configured.add("api_key:runpod")
    modal = credentials.get("modal", {})
    if modal.get("token_id") and modal.get("token_secret"):
        configured.add("token_pair:modal")
    hyperstack = credentials.get("hyperstack", {})
    if hyperstack.get("api_key"):
        configured.add("api_key:hyperstack")
    if credentials.get("azure"):
        configured.add("cloud_subscription:azure")
    if credentials.get("gcp"):
        configured.add("cloud_project:gcp")
    if credentials.get("scaleway"):
        configured.add("api_key:scaleway")
    if credentials.get("ovhcloud"):
        configured.add("cloud_project:ovhcloud")
    return configured


def _provider_missing_credentials_next_action(provider: str) -> str:
    if provider == "runpod":
        return "Run `gpucall configure runpod-serverless` or add providers.runpod.api_key to the gpucall credentials store."
    if provider == "modal":
        return "Run `gpucall configure modal` or add providers.modal.token_id/token_secret to the gpucall credentials store."
    if provider == "hyperstack":
        return "Run `gpucall configure hyperstack` or add providers.hyperstack api_key and ssh_key_path to the gpucall credentials store."
    return f"Configure {provider} credentials in the gpucall credentials store before refreshing provider evidence."


def _provider_missing_target_next_action(provider: str) -> str:
    if provider == "runpod":
        return "Run provider supply provisioning for RunPod, or set tuple target to a live RunPod endpoint before refreshing endpoint evidence."
    if provider == "modal":
        return "Deploy the Modal function and set tuple target to app:function before refreshing endpoint evidence."
    return f"Provision {provider} supply and set required tuple target fields before refreshing endpoint evidence."


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
    preflight: dict[str, Any] | None = None,
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
    if preflight is not None:
        report["status"] = preflight["status"]
        report["probe_tuple_count"] = preflight["probe_tuple_count"]
        report["skipped_tuple_count"] = preflight["skipped_tuple_count"]
        report["skipped_provider_counts"] = preflight["skipped_provider_counts"]
        report["provider_counts"] = preflight["provider_counts"]
        report["blockers"] = preflight["blockers"]
    return report


def dumps_panopticon_report(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2, sort_keys=True) + "\n"
