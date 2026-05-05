from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path
from datetime import datetime, timezone
from importlib.resources import files

import httpx
import uvicorn

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ""):
    sys.path.insert(0, str(PROJECT_ROOT))

from gpucall.app import build_runtime, create_app, plan_with_worker_refs, worker_readable_request
from gpucall.catalog import SQLiteCapabilityCatalog, dumps_snapshot
from gpucall.compiler import GovernanceCompiler
from gpucall.config import ConfigError, default_config_dir, default_state_dir, load_config
from gpucall.configure import configure_command
from gpucall.credentials import configured_credentials, credentials_path, load_credentials
from gpucall.domain import ExecutionMode, JobState, PresignPutRequest, TaskRequest
from gpucall.provider_catalog import live_provider_catalog_findings
from gpucall.registry import ObservedRegistry
from gpucall.audit import AuditTrail
from gpucall.routing import provider_route_rejection_reason
from gpucall.sqlite_store import SQLiteJobStore


def main() -> None:
    parser = argparse.ArgumentParser(prog="gpucall")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--config-dir", type=Path, default=default_config_dir())
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    explain = sub.add_parser("explain-config")
    explain.add_argument("recipe_name")
    explain.add_argument("--config-dir", type=Path, default=default_config_dir())
    explain.add_argument("--mode", choices=[mode.value for mode in ExecutionMode], default=None)
    explain.add_argument("--provider", default=None)
    explain.add_argument("--max-tokens", type=int, default=None)
    explain.add_argument("--timeout-seconds", type=int, default=None)
    explain.add_argument("--lease-ttl-seconds", type=int, default=None)
    init = sub.add_parser("init")
    init.add_argument("--config-dir", type=Path, default=default_config_dir())
    init.add_argument("--force", action="store_true")
    doctor = sub.add_parser("doctor")
    doctor.add_argument("--config-dir", type=Path, default=default_config_dir())
    doctor.add_argument("--live-provider-catalog", action="store_true")
    validate = sub.add_parser("validate-config")
    validate.add_argument("--config-dir", type=Path, default=default_config_dir())
    seed = sub.add_parser("seed-liveness")
    seed.add_argument("recipe_name")
    seed.add_argument("--config-dir", type=Path, default=default_config_dir())
    seed.add_argument("--count", type=int, default=3)
    smoke = sub.add_parser("smoke")
    smoke.add_argument("--url", default="http://127.0.0.1:18088")
    smoke.add_argument("--api-key", default=None)
    smoke.add_argument("--recipe", default="text-infer-standard")
    provider_smoke = sub.add_parser("provider-smoke")
    provider_smoke.add_argument("provider")
    provider_smoke.add_argument("--config-dir", type=Path, default=default_config_dir())
    provider_smoke.add_argument("--recipe", default="text-infer-standard")
    provider_smoke.add_argument("--mode", choices=["sync", "async", "stream"], default="sync")
    provider_smoke.add_argument("--write-artifact", action="store_true")
    jobs = sub.add_parser("jobs")
    jobs.add_argument("job_id", nargs="?")
    jobs.add_argument("--limit", type=int, default=20)
    jobs.add_argument("--scrub-inputs", action="store_true")
    jobs.add_argument("--expire-stale", action="store_true")
    registry = sub.add_parser("registry")
    registry.add_argument("action", choices=["show"])
    catalog = sub.add_parser("catalog")
    catalog.add_argument("action", choices=["build", "show"])
    catalog.add_argument("--config-dir", type=Path, default=default_config_dir())
    catalog.add_argument("--db", type=Path, default=None)
    audit = sub.add_parser("audit")
    audit.add_argument("action", choices=["verify", "tail", "rotate"])
    audit.add_argument("--limit", type=int, default=20)
    audit.add_argument("--max-bytes", type=int, default=100 * 1024 * 1024)
    security = sub.add_parser("security")
    security.add_argument("action", choices=["scan-secrets"])
    security.add_argument("--config-dir", type=Path, default=default_config_dir())
    openapi = sub.add_parser("openapi")
    openapi.add_argument("--config-dir", type=Path, default=default_config_dir())
    launch_check = sub.add_parser("launch-check")
    launch_check.add_argument("--config-dir", type=Path, default=default_config_dir())
    launch_check.add_argument("--url", default=None)
    launch_check.add_argument("--api-key", default=None)
    launch_check.add_argument("--profile", choices=["static", "production"], default="production")
    post_launch = sub.add_parser("post-launch-report")
    post_launch.add_argument("--config-dir", type=Path, default=default_config_dir())
    configure = sub.add_parser("configure")
    configure.add_argument("--config-dir", type=Path, default=default_config_dir())
    args = parser.parse_args()

    if args.command == "serve":
        uvicorn.run(create_app(args.config_dir), host=args.host, port=args.port)
    elif args.command == "explain-config":
        try:
            config = load_config(args.config_dir)
            recipe = config.recipes.get(args.recipe_name)
            if recipe is None:
                raise ConfigError(f"unknown recipe: {args.recipe_name}")
            mode = ExecutionMode(args.mode) if args.mode else recipe.allowed_modes[0]
            request = TaskRequest(
                task=recipe.task,
                mode=mode,
                recipe=recipe.name,
                requested_provider=args.provider,
                max_tokens=args.max_tokens,
                timeout_seconds=args.timeout_seconds,
                lease_ttl_seconds=args.lease_ttl_seconds,
                webhook_url="https://example.invalid/gpucall/explain" if mode is ExecutionMode.ASYNC else None,
            )
            compiler = GovernanceCompiler(
                policy=config.policy,
                recipes=config.recipes,
                providers=config.providers,
                models=config.models,
                engines=config.engines,
                registry=ObservedRegistry(),
            )
            plan = compiler.compile(request)
        except ConfigError as exc:
            raise SystemExit(f"config error: {exc}") from exc
        print(
            json.dumps(
                {
                    "recipe": recipe.name,
                    "policy_ceiling": {
                        "version": config.policy.version,
                        "max_timeout_seconds": config.policy.max_timeout_seconds,
                        "max_lease_ttl_seconds": config.policy.max_lease_ttl_seconds,
                        "inline_bytes_limit": config.policy.inline_bytes_limit,
                        "max_data_classification": config.policy.providers.max_data_classification,
                    },
                    "recipe_standard": recipe.model_dump(mode="json"),
                    "execution_spec": plan.model_dump(mode="json", exclude={"inline_inputs", "input_refs"}),
                },
                indent=2,
                sort_keys=True,
            )
        )
    elif args.command == "init":
        init_config(args.config_dir, force=args.force)
    elif args.command == "doctor":
        doctor_config(args.config_dir, live_provider_catalog=args.live_provider_catalog)
    elif args.command == "validate-config":
        validate_config_command(args.config_dir)
    elif args.command == "seed-liveness":
        asyncio.run(seed_liveness(args.config_dir, args.recipe_name, args.count))
    elif args.command == "smoke":
        smoke_gateway(args.url, api_key=args.api_key, recipe=args.recipe)
    elif args.command == "provider-smoke":
        asyncio.run(
            provider_smoke_command(
                args.config_dir,
                args.provider,
                args.recipe,
                ExecutionMode(args.mode),
                write_artifact=args.write_artifact,
            )
        )
    elif args.command == "jobs":
        asyncio.run(jobs_command(args.job_id, args.limit, scrub_inputs=args.scrub_inputs, expire_stale=args.expire_stale))
    elif args.command == "audit":
        audit_command(args.action, args.limit, args.max_bytes)
    elif args.command == "registry":
        registry_command(args.action)
    elif args.command == "catalog":
        catalog_command(args.action, args.config_dir, args.db)
    elif args.command == "security":
        security_command(args.action, args.config_dir)
    elif args.command == "openapi":
        print(json.dumps(create_app(args.config_dir).openapi(), indent=2, sort_keys=True))
    elif args.command == "launch-check":
        launch_check_command(args.config_dir, url=args.url, api_key=args.api_key, profile=args.profile)
    elif args.command == "post-launch-report":
        asyncio.run(post_launch_report_command(args.config_dir))
    elif args.command == "configure":
        configure_command(args.config_dir)


def init_config(config_dir: Path, *, force: bool = False) -> None:
    package_source = files("gpucall").joinpath("config_templates")
    source = Path(str(package_source))
    if not source.exists():
        source = PROJECT_ROOT / "config"
    if not source.exists():
        raise ConfigError("gpucall config templates are not available in this installation")
    config_dir.mkdir(parents=True, exist_ok=True)
    for path in source.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(source)
        target = config_dir / relative
        if target.exists() and not force:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    print(f"initialized gpucall config at {config_dir}")


def doctor_config(config_dir: Path, *, live_provider_catalog: bool = False) -> None:
    try:
        config = load_config(config_dir)
    except ConfigError as exc:
        raise SystemExit(f"config error: {exc}") from exc
    creds = load_credentials()
    checks = {
        "config_dir": str(config_dir),
        "credentials_path": str(credentials_path()),
        "state_dir": str(default_state_dir()),
        "policy_version": config.policy.version,
        "recipes": sorted(config.recipes),
        "providers": sorted(config.providers),
        "models": sorted(config.models),
        "engines": sorted(config.engines),
        "object_store": config.object_store.model_dump(mode="json") if config.object_store else None,
        "registry": _configured_registry_snapshot(config),
        "routing": _routing_decision_summary(config),
        "secrets": _secret_presence_summary(creds),
    }
    if live_provider_catalog:
        catalog_findings = live_provider_catalog_findings(config.providers, creds)
        checks["live_provider_catalog"] = {
            "ok": not catalog_findings,
            "findings": catalog_findings,
        }
    print(json.dumps(checks, indent=2, sort_keys=True))


def validate_config_command(config_dir: Path) -> None:
    config = load_config(config_dir)
    print(json.dumps({"valid": True, "recipes": sorted(config.recipes), "providers": sorted(config.providers)}, indent=2, sort_keys=True))


def launch_check_command(config_dir: Path, *, url: str | None = None, api_key: str | None = None, profile: str = "production") -> None:
    report = build_launch_report(config_dir, url=url, api_key=api_key, profile=profile)
    path = default_state_dir() / "launch" / "launch-check.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    print(json.dumps({**report, "report_path": str(path)}, indent=2, sort_keys=True, default=str))


def build_launch_report(config_dir: Path, *, url: str | None = None, api_key: str | None = None, profile: str = "production") -> dict[str, object]:
    config = load_config(config_dir)
    creds = load_credentials()
    audit_path = default_state_dir() / "audit" / "trail.jsonl"
    audit = AuditTrail(audit_path)
    openapi_schema = create_app(config_dir).openapi()
    secret_scan = _scan_secret_like_yaml(config_dir)
    routing_hygiene = _routing_hygiene_findings(config)
    routing_summary = _routing_decision_summary(config)
    provider_samples = _configured_registry_snapshot(config)
    gateway_smoke: dict[str, object] | None = None
    if url:
        try:
            smoke_recipe = os.getenv("GPUCALL_LAUNCH_SMOKE_RECIPE") or None
            gateway_smoke = _gateway_smoke_summary(url, api_key=api_key, recipe=smoke_recipe)
        except Exception as exc:
            gateway_smoke = {"ok": False, "error": str(exc)}
    checks = {
        "config_valid": True,
        "secrets_present": _secret_presence_summary(creds),
        "secret_scan_ok": not secret_scan,
        "routing_hygiene_ok": not routing_hygiene,
        "routing": routing_summary,
        "audit_chain_valid": audit.verify() if audit_path.exists() else True,
        "openapi_paths": sorted(openapi_schema.get("paths", {}).keys()),
        "mvp_scope": {
            "tasks": ["infer", "vision"],
            "deferred": ["transcribe", "train", "convert", "fine-tune", "multi-file batch", "postgres", "helm", "systemd"],
            "control_plane_only": ["train", "fine-tune", "split-infer"],
        },
        "packaging": {
            "dockerfile": (PROJECT_ROOT / "Dockerfile").exists(),
            "docker_compose": (PROJECT_ROOT / "docker-compose.yml").exists(),
            "python_package": (PROJECT_ROOT / "pyproject.toml").exists(),
            "typescript_sdk": (PROJECT_ROOT / "sdk" / "typescript" / "package.json").exists(),
        },
        "docs": {
            "quickstart": (PROJECT_ROOT / "README.md").exists(),
            "launch_runbook": (PROJECT_ROOT / "LAUNCH_MVP.md").exists(),
            "security": (PROJECT_ROOT / "SECURITY.md").exists(),
            "observability": (PROJECT_ROOT / "docs" / "OBSERVABILITY.md").exists(),
            "provider_validation": (PROJECT_ROOT / "docs" / "PROVIDER_VALIDATION.md").exists(),
        },
        "launch_profile": profile,
    }
    required_paths = {
        "/healthz",
        "/readyz",
        "/metrics",
        "/v2/tasks/sync",
        "/v2/tasks/async",
        "/v2/tasks/stream",
        "/v2/objects/presign-put",
        "/v2/objects/presign-get",
        "/v2/results/presign-put",
    }
    blockers = []
    missing_paths = sorted(required_paths - set(checks["openapi_paths"]))
    if missing_paths:
        blockers.append({"check": "openapi_paths", "missing": missing_paths})
    if secret_scan:
        blockers.append({"check": "secret_scan", "findings": secret_scan})
    if routing_hygiene:
        blockers.append({"check": "routing_hygiene", "findings": routing_hygiene})
    if not checks["audit_chain_valid"]:
        blockers.append({"check": "audit_chain", "valid": False})
    secrets = checks["secrets_present"]
    live_artifact = _latest_live_validation_artifact(config_dir=config_dir)
    production = profile == "production"
    checks["launch_gates"] = {
        "static_config_valid": checks["config_valid"],
        "auth_required": bool(gateway_smoke and gateway_smoke.get("auth_required") is True),
        "object_store_required": production,
        "object_store_configured": config.object_store is not None and bool(secrets["object_store"]),
        "gateway_live_smoke_passed": bool(gateway_smoke and gateway_smoke.get("ok") is True),
        "provider_live_validation_passed": live_artifact is not None,
        "audit_chain_valid": checks["audit_chain_valid"],
    }
    if production:
        if not secrets["gateway_auth"]:
            blockers.append({"check": "gateway_auth", "configured": False})
        if config.object_store is None or not secrets["object_store"]:
            blockers.append({"check": "object_store", "configured": config.object_store is not None, "secrets": secrets["object_store"]})
        if not url:
            blockers.append({"check": "gateway_live_smoke", "url": None})
        elif not gateway_smoke or gateway_smoke.get("ok") is not True:
            blockers.append({"check": "gateway_live_smoke", "passed": False, "summary": gateway_smoke})
        elif gateway_smoke.get("auth_required") is not True:
            blockers.append({"check": "gateway_auth_enforced", "auth_required": gateway_smoke.get("auth_required")})
        if live_artifact is None:
            blockers.append({"check": "provider_live_validation", "artifact": None})
    report: dict[str, object] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "config_dir": str(config_dir),
        "state_dir": str(default_state_dir()),
        "policy_version": config.policy.version,
        "providers": sorted(config.providers),
        "recipes": sorted(config.recipes),
        "checks": checks,
        "registry": provider_samples,
        "gateway_smoke": gateway_smoke,
        "provider_live_validation": live_artifact,
        "blockers": blockers,
        "go": not blockers,
    }
    return report


async def post_launch_report_command(config_dir: Path) -> None:
    config = load_config(config_dir)
    jobs = SQLiteJobStore(default_state_dir() / "state.db")
    records = await jobs.all()
    state_counts: dict[str, int] = {}
    for job in records:
        state_counts[str(job.state)] = state_counts.get(str(job.state), 0) + 1
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "config_dir": str(config_dir),
        "policy_version": config.policy.version,
        "job_state_counts": state_counts,
        "registry": ObservedRegistry(path=default_state_dir() / "registry.db").snapshot(),
        "audit_valid": AuditTrail(default_state_dir() / "audit" / "trail.jsonl").verify(),
        "post_launch_actions": [
            "review incident log",
            "review provider cost dashboards",
            "review provider success rates",
            "review SDK feedback",
            "triage docs corrections",
            "groom v2.1 backlog",
        ],
    }
    path = default_state_dir() / "launch" / "post-launch-report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    print(json.dumps({**report, "report_path": str(path)}, indent=2, sort_keys=True, default=str))


async def seed_liveness(config_dir: Path, recipe_name: str, count: int) -> None:
    runtime = build_runtime(config_dir)
    recipe = runtime.compiler.recipes.get(recipe_name)
    if recipe is None:
        raise SystemExit(f"unknown recipe: {recipe_name}")
    completed = 0
    for _ in range(count):
        request = TaskRequest(
            task=recipe.task,
            mode=ExecutionMode.SYNC,
            recipe=recipe.name,
            inline_inputs={"prompt": {"value": "gpucall liveness seed"}},
        )
        plan = runtime.compiler.compile(request)
        await runtime.dispatcher.execute_sync(plan)
        completed += 1
    print(json.dumps({"recipe": recipe_name, "seed_jobs": completed}, indent=2, sort_keys=True))


async def provider_smoke_command(
    config_dir: Path,
    provider: str,
    recipe_name: str,
    mode: ExecutionMode,
    *,
    write_artifact: bool = False,
) -> None:
    runtime = build_runtime(config_dir)
    recipe = runtime.compiler.recipes.get(recipe_name)
    if recipe is None:
        raise SystemExit(f"unknown recipe: {recipe_name}")
    started_at = datetime.now(timezone.utc)
    request = _provider_smoke_request(runtime, recipe, mode, provider)
    plan = runtime.compiler.compile(request)
    if request.input_refs or request.split_learning is not None:
        worker_request = worker_readable_request(request, runtime)
        plan = plan_with_worker_refs(plan, worker_request.input_refs, split_learning=worker_request.split_learning)
    summary: dict[str, object]
    if mode is ExecutionMode.STREAM:
        chunks = []
        stream = runtime.dispatcher.execute_stream(plan)
        try:
            while len(chunks) < 2:
                try:
                    chunks.append(await asyncio.wait_for(stream.__anext__(), timeout=10.0))
                except StopAsyncIteration:
                    break
        finally:
            await stream.aclose()
        summary = _provider_smoke_base_summary(runtime, provider, recipe_name, mode)
        summary.update({"chunks": len(chunks), "sample": chunks[:2]})
        _finish_provider_smoke_summary(summary, started_at=started_at, config_dir=config_dir, plan=plan, write_artifact=write_artifact)
        return
    if mode is ExecutionMode.ASYNC:
        job = await runtime.dispatcher.submit_async(plan)
        deadline = asyncio.get_running_loop().time() + plan.timeout_seconds
        current = job
        while asyncio.get_running_loop().time() < deadline:
            loaded = await runtime.jobs.get(job.job_id)
            if loaded is not None:
                current = loaded
                if loaded.state in {JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED, JobState.EXPIRED}:
                    break
            await asyncio.sleep(1.0)
        summary = {
            **_provider_smoke_base_summary(runtime, provider, recipe_name, mode),
            "provider": provider,
            "recipe": recipe_name,
            "mode": mode.value,
            "job_id": job.job_id,
            "state": current.state.value,
            "completed": current.state is JobState.COMPLETED,
        }
        _finish_provider_smoke_summary(summary, started_at=started_at, config_dir=config_dir, plan=plan, write_artifact=write_artifact)
        return
    result = await runtime.dispatcher.execute_sync(plan)
    summary = _provider_smoke_base_summary(runtime, provider, recipe_name, mode)
    summary["result"] = result.model_dump(mode="json")
    _finish_provider_smoke_summary(summary, started_at=started_at, config_dir=config_dir, plan=plan, write_artifact=write_artifact)


def _provider_smoke_base_summary(runtime, provider: str, recipe_name: str, mode: ExecutionMode) -> dict[str, object]:
    spec = runtime.compiler.providers.get(provider)
    return {
        "provider": provider,
        "recipe": recipe_name,
        "mode": mode.value,
        "model_ref": getattr(spec, "model_ref", None),
        "engine_ref": getattr(spec, "engine_ref", None),
        "provider_model": getattr(spec, "model", None),
        "gpu": getattr(spec, "gpu", None),
    }


def _provider_smoke_request(runtime, recipe, mode: ExecutionMode, provider: str) -> TaskRequest:
    inline_inputs = {"prompt": {"value": "gpucall provider smoke", "content_type": "text/plain"}}
    input_refs = []
    if recipe.task == "vision":
        if runtime.object_store is None:
            raise SystemExit("provider-smoke vision requires object_store")
        image_body = _smoke_png()
        digest = hashlib.sha256(image_body).hexdigest()
        presigned = runtime.object_store.presign_put(
            PresignPutRequest(name="provider-smoke.png", bytes=len(image_body), sha256=digest, content_type="image/png")
        )
        upload = httpx.put(str(presigned.upload_url), content=image_body, headers={"content-type": "image/png"}, timeout=30.0)
        upload.raise_for_status()
        input_refs.append(presigned.data_ref)
    return TaskRequest(
        task=recipe.task,
        mode=mode,
        recipe=recipe.name,
        requested_provider=provider,
        inline_inputs=inline_inputs,
        input_refs=input_refs,
    )


def _finish_provider_smoke_summary(
    summary: dict[str, object],
    *,
    started_at: datetime,
    config_dir: Path,
    plan,
    write_artifact: bool,
) -> None:
    ended_at = datetime.now(timezone.utc)
    summary["started_at"] = started_at.isoformat()
    summary["ended_at"] = ended_at.isoformat()
    summary["validation_schema_version"] = 1
    summary["passed"] = _provider_smoke_passed(summary)
    summary.setdefault("cleanup", {"required": False, "completed": None})
    summary.setdefault("cost", {"observed": None, "estimated": None})
    summary.setdefault("audit", {"event_ids": []})
    summary["commit"] = _git_commit()
    summary["config_hash"] = _config_hash(config_dir)
    summary["governance_hash"] = getattr(plan, "attestations", {}).get("governance_hash")
    if write_artifact:
        artifact_path = _write_live_validation_artifact(summary)
        summary["artifact_path"] = str(artifact_path)
    print(json.dumps(summary, indent=2, sort_keys=True, default=str))


def _write_live_validation_artifact(summary: dict[str, object]) -> Path:
    root = default_state_dir() / "provider-validation"
    root.mkdir(parents=True, exist_ok=True)
    provider = str(summary.get("provider") or "provider").replace("/", "_")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = root / f"{stamp}-{provider}.json"
    path.write_text(json.dumps(summary, sort_keys=True, separators=(",", ":"), default=str) + "\n", encoding="utf-8")
    return path


def _provider_smoke_passed(summary: dict[str, object]) -> bool:
    mode = summary.get("mode")
    if mode == "stream":
        return int(summary.get("chunks") or 0) > 0
    if mode == "async":
        return summary.get("completed") is True
    result = summary.get("result")
    if isinstance(result, dict):
        return result.get("kind") in {"inline", "ref", "artifact_manifest"}
    return False


def _git_commit() -> str | None:
    head = PROJECT_ROOT / ".git" / "HEAD"
    try:
        value = head.read_text(encoding="utf-8").strip()
        if value.startswith("ref: "):
            ref = PROJECT_ROOT / ".git" / value.removeprefix("ref: ")
            return ref.read_text(encoding="utf-8").strip()
        return value
    except OSError:
        return None


def _config_hash(config_dir: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(config_dir.rglob("*.yml")):
        digest.update(str(path.relative_to(config_dir)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def registry_command(action: str) -> None:
    registry = ObservedRegistry(path=default_state_dir() / "registry.db")
    if action == "show":
        print(json.dumps(registry.snapshot(), indent=2, sort_keys=True))


def catalog_command(action: str, config_dir: Path, db: Path | None) -> None:
    path = db or (default_state_dir() / "capability-catalog.db")
    catalog = SQLiteCapabilityCatalog(path)
    if action == "build":
        catalog.replace_from_config(load_config(config_dir), config_dir=config_dir)
    print(dumps_snapshot(catalog.snapshot()), end="")


def smoke_gateway(url: str, *, api_key: str | None, recipe: str) -> None:
    print(json.dumps(_gateway_smoke_summary(url, api_key=api_key, recipe=recipe), indent=2, sort_keys=True))


def _gateway_smoke_summary(url: str, *, api_key: str | None, recipe: str | None = None) -> dict[str, object]:
    key = api_key or _gateway_api_key()
    headers = {"authorization": f"Bearer {key}"} if key else {}
    summary: dict[str, object] = {"ok": False, "recipe_hint": recipe}
    timeout = float(os.getenv("GPUCALL_GATEWAY_SMOKE_TIMEOUT_SECONDS", "900"))
    with httpx.Client(base_url=url.rstrip("/"), timeout=timeout, headers=headers) as client:
        health = client.get("/healthz")
        health.raise_for_status()
        summary["healthz"] = health.json()
        ready = client.get("/readyz")
        ready.raise_for_status()
        ready_payload = ready.json()
        summary["readyz"] = ready_payload
        unauth = httpx.post(
            f"{url.rstrip('/')}/v2/tasks/sync",
            json={"task": "infer", "mode": "sync"},
            timeout=30.0,
        )
        summary["auth_required"] = unauth.status_code == 401
        sync_payload = {
            "task": "infer",
            "mode": "sync",
            "messages": [{"role": "user", "content": "Reply with exactly: gpucall smoke"}],
            "max_tokens": 16,
            "metadata": {"smoke": "true"},
        }
        if recipe:
            sync_payload["recipe"] = recipe
        sync = client.post(
            "/v2/tasks/sync",
            json=sync_payload,
        )
        sync.raise_for_status()
        sync_body = sync.json()
        result = sync_body.get("result", {})
        value = result.get("value") if isinstance(result, dict) else None
        plan = sync_body.get("plan", {})
        summary["sync"] = {
            **sync_body,
            "output_non_empty": bool(str(value or "").strip()),
            "selected_provider": plan.get("selected_provider") if isinstance(plan, dict) else None,
            "recipe_name": plan.get("recipe_name") if isinstance(plan, dict) else None,
            "output_kind": result.get("kind") if isinstance(result, dict) else None,
        }
        if ready_payload.get("object_store"):
            body = b"gpucall smoke\n"
            digest = hashlib.sha256(body).hexdigest()
            presign = client.post(
                "/v2/objects/presign-put",
                json={"name": "smoke.txt", "bytes": len(body), "sha256": digest, "content_type": "text/plain"},
            )
            presign.raise_for_status()
            presigned = presign.json()
            upload = httpx.put(presigned["upload_url"], content=body, headers={"content-type": "text/plain"}, timeout=30.0)
            upload.raise_for_status()
            data_ref = presigned["data_ref"]
            summary["object_store_smoke"] = {
                "uploaded": True,
                "uri": data_ref.get("uri"),
                "bytes": data_ref.get("bytes"),
            }
            image_body = _smoke_png()
            image_digest = hashlib.sha256(image_body).hexdigest()
            image_presign = client.post(
                "/v2/objects/presign-put",
                json={"name": "smoke.png", "bytes": len(image_body), "sha256": image_digest, "content_type": "image/png"},
            )
            image_presign.raise_for_status()
            image_ref = image_presign.json()["data_ref"]
            image_upload = httpx.put(
                image_presign.json()["upload_url"], content=image_body, headers={"content-type": "image/png"}, timeout=30.0
            )
            image_upload.raise_for_status()
            vision = client.post(
                "/v2/tasks/sync",
                json={
                    "task": "vision",
                    "mode": "sync",
                    "input_refs": [image_ref],
                    "inline_inputs": {"prompt": {"value": "describe smoke image", "content_type": "text/plain"}},
                    "metadata": {"smoke": "true"},
                },
            )
            summary["vision"] = {"status_code": vision.status_code, "ok": vision.status_code < 400}
            if vision.status_code < 400:
                summary["vision"]["body"] = vision.json()
            else:
                summary["vision"]["required_for_gateway_ok"] = False
        else:
            summary["vision"] = {"skipped": "object_store is not configured"}
        summary["ok"] = bool(
            isinstance(summary.get("sync"), dict)
            and summary["sync"].get("output_non_empty") is True
            and (
                not isinstance(summary.get("object_store_smoke"), dict)
                or summary["object_store_smoke"].get("uploaded") is True
            )
        )
    return summary


def _smoke_png() -> bytes:
    return bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000100000001008060000001ff3ff610000001649444154789c63f84f2160183560d4805103868b01005d78fc2eaffde1690000000049454e44ae426082"
    )


def _latest_live_validation_artifact(config_dir: Path | None = None) -> dict[str, object] | None:
    root = default_state_dir() / "provider-validation"
    if not root.exists():
        return None
    expected_commit = _git_commit()
    expected_config_hash = _config_hash(config_dir) if config_dir is not None else None
    candidates = sorted(root.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        if expected_commit and data.get("commit") != expected_commit:
            continue
        if expected_config_hash and data.get("config_hash") != expected_config_hash:
            continue
        if not _live_validation_artifact_valid(data):
            continue
        return {"path": str(path), "mtime": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(), "data": data}
    return None


def _live_validation_artifact_valid(data: dict[str, object]) -> bool:
    required = {"provider", "recipe", "mode", "started_at", "ended_at", "commit", "config_hash", "governance_hash"}
    if not required.issubset(data):
        return False
    if data.get("validation_schema_version") != 1:
        return False
    if data.get("passed") is not True:
        return False
    if not isinstance(data.get("cleanup"), dict):
        return False
    if not isinstance(data.get("cost"), dict):
        return False
    if not isinstance(data.get("audit"), dict):
        return False
    return True


async def jobs_command(job_id: str | None, limit: int, *, scrub_inputs: bool = False, expire_stale: bool = False) -> None:
    store = SQLiteJobStore(default_state_dir() / "state.db")
    if scrub_inputs:
        scrubbed = 0
        for job in await store.all():
            safe_plan = job.plan.model_copy(update={"input_refs": [], "inline_inputs": {}})
            if safe_plan != job.plan:
                await store.update(job.job_id, plan=safe_plan)
                scrubbed += 1
        print(json.dumps({"scrubbed_jobs": scrubbed}, indent=2, sort_keys=True))
        return
    if expire_stale:
        expired = 0
        now = datetime.now(timezone.utc)
        for job in await store.all():
            if job.state not in {JobState.PENDING, JobState.RUNNING}:
                continue
            if job.created_at.timestamp() + job.plan.lease_ttl_seconds <= now.timestamp():
                await store.update(job.job_id, state=JobState.EXPIRED, error="lease expired")
                expired += 1
        print(json.dumps({"expired_jobs": expired}, indent=2, sort_keys=True))
        return
    if job_id:
        job = await store.get(job_id)
        if job is None:
            raise SystemExit(f"job not found: {job_id}")
        print(job.model_dump_json(indent=2))
        return
    jobs = await store.all()
    rows = [
        {
            "job_id": job.job_id,
            "state": job.state,
            "task": job.plan.task,
            "recipe": job.plan.recipe_name,
            "updated_at": job.updated_at.isoformat(),
            "error": job.error,
        }
        for job in jobs[-limit:]
    ]
    print(json.dumps(rows, indent=2, sort_keys=True, default=str))


def audit_command(action: str, limit: int, max_bytes: int) -> None:
    path = default_state_dir() / "audit" / "trail.jsonl"
    trail = AuditTrail(path)
    if action == "verify":
        print(json.dumps({"path": str(path), "valid": trail.verify()}, indent=2, sort_keys=True))
        return
    if action == "rotate":
        rotated = trail.rotate_if_needed(max_bytes)
        print(json.dumps({"path": str(path), "rotated": str(rotated) if rotated else None}, indent=2, sort_keys=True))
        return
    if not path.exists():
        print("[]")
        return
    lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
    print("[")
    for index, line in enumerate(lines):
        suffix = "," if index < len(lines) - 1 else ""
        print(f"  {line}{suffix}")
    print("]")


def security_command(action: str, config_dir: Path) -> None:
    if action != "scan-secrets":
        raise SystemExit(f"unknown security action: {action}")
    findings = _scan_secret_like_yaml(config_dir)
    print(json.dumps({"ok": not findings, "findings": findings}, indent=2, sort_keys=True))
    if findings:
        raise SystemExit(1)


def _scan_secret_like_yaml(config_dir: Path) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    secret_keys = {"api_key", "secret", "token", "authorization", "access_key", "access_key_id", "secret_access_key"}
    for path in config_dir.rglob("*.yml"):
        if path.name == "credentials.yml":
            continue
        text = path.read_text(encoding="utf-8")
        for line_no, line in enumerate(text.splitlines(), start=1):
            key = line.split(":", 1)[0].strip().lower()
            if key in secret_keys or key.endswith("_api_key") or key.endswith("_secret") or key.endswith("_token"):
                findings.append({"path": str(path), "line": str(line_no), "reason": "secret-like key in YAML"})
    return findings


def _routing_hygiene_findings(config) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    for recipe in config.recipes.values():
        if not recipe.auto_select:
            continue
        candidates: list[str] = []
        for provider in config.providers.values():
            if _provider_route_rejection_reason(config, recipe, provider) is not None:
                continue
            candidates.append(provider.name)
        if not candidates:
            findings.append(
                {
                    "recipe": recipe.name,
                    "provider": "",
                    "reason": "auto-selected recipe has no production provider satisfying its requirements",
                }
            )
    return findings


def _routing_decision_summary(config) -> dict[str, dict[str, object]]:
    summary: dict[str, dict[str, object]] = {}
    for recipe in config.recipes.values():
        candidates: list[str] = []
        excluded: dict[str, str] = {}
        for provider in config.providers.values():
            reason = _provider_route_rejection_reason(config, recipe, provider)
            if reason is None:
                candidates.append(provider.name)
            else:
                excluded[provider.name] = reason
        summary[recipe.name] = {
            "auto_select": recipe.auto_select,
            "task": recipe.task,
            "candidates": sorted(candidates) if recipe.auto_select else [],
            "excluded": dict(sorted(excluded.items())) if recipe.auto_select else {},
        }
    return summary


def _provider_route_rejection_reason(config, recipe, provider) -> str | None:
    return provider_route_rejection_reason(
        policy=config.policy,
        recipe=recipe,
        provider=provider,
        required_len=recipe.max_model_len,
        require_auto_select=True,
    )


def _gateway_api_key() -> str | None:
    configured = load_credentials().get("auth", {}).get("api_keys", "")
    first = configured.split(",", 1)[0].strip()
    return os.getenv("GPUCALL_API_KEY") or first or None


def _secret_presence_summary(creds: dict[str, dict[str, str]]) -> dict[str, bool | list[str]]:
    configured = sorted(set(configured_credentials()))
    return {
        "configured": configured,
        "gateway_auth": bool(creds.get("auth", {}).get("api_keys") or os.getenv("GPUCALL_API_KEYS")),
        "object_store": bool(creds.get("aws", {}).get("access_key_id") and creds.get("aws", {}).get("secret_access_key")),
    }


def _configured_registry_snapshot(config) -> dict[str, dict[str, object]]:
    snapshot = ObservedRegistry(path=default_state_dir() / "registry.db").snapshot()
    return {name: snapshot[name] for name in sorted(config.providers) if name in snapshot}


if __name__ == "__main__":
    main()
