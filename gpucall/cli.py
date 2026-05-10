from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
from pathlib import Path
from datetime import datetime, timezone
from importlib.resources import files

import httpx
import uvicorn
import yaml
from pydantic import ValidationError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ""):
    sys.path.insert(0, str(PROJECT_ROOT))

from gpucall.app import build_runtime, create_app, plan_with_worker_refs, worker_readable_request
from gpucall.admin_automation import admin_automation_summary, configure_admin_automation
from gpucall.catalog import SQLiteCapabilityCatalog, dumps_snapshot
from gpucall.handoff import handoff_payload as _handoff_payload, render_handoff as _render_handoff
from gpucall.cli_commands.readiness import add_readiness_parser, run_readiness_command
from gpucall.cli_commands.setup import add_setup_parser, run_setup_command
from gpucall.compiler import GovernanceCompiler
from gpucall.config import ConfigError, default_config_dir, default_state_dir, load_config
from gpucall.configure import configure_command
from gpucall.credentials import configured_credentials, credentials_path, load_credentials
from gpucall.domain import ExecutionMode, JobState, PresignPutRequest, TupleError, TaskRequest, recipe_requirements
from gpucall.domain import ApiKeyHandoffMode
from gpucall.execution_catalog import build_resource_catalog_snapshot, dumps_candidates, dumps_snapshot as dumps_execution_snapshot, generate_tuple_candidates
from gpucall.execution.contracts import (
    artifact_tuple_evidence_key,
    official_contract,
    official_contract_hash,
    tuple_evidence_key,
    tuple_evidence_label,
)
from gpucall.lease_reaper import active_manifest_leases, lease_reaper_report
from gpucall.price_cache import load_cached_price_evidence, merge_price_evidence, store_live_price_evidence
from gpucall.tuple_audit import _tuple_from_candidate, tuple_audit_report
from gpucall.tuple_catalog import live_tuple_catalog_evidence, live_tuple_catalog_findings
from gpucall.execution.registry import adapter_descriptor, vendor_family_for_adapter
from gpucall.registry import ObservedRegistry
from gpucall.audit import AuditTrail
from gpucall.routing import tuple_route_rejection_reason
from gpucall.targeting import has_configured_endpoint_or_target, is_configured_cidr, is_configured_target
from gpucall.sqlite_store import SQLiteJobStore
from gpucall.tenant import TenantUsageLedger
from gpucall.validator_plan import build_validator_plan, dumps_validator_plan


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
    explain.add_argument("--tuple", dest="tuple_name", default=None)
    explain.add_argument("--max-tokens", type=int, default=None)
    explain.add_argument("--timeout-seconds", type=int, default=None)
    explain.add_argument("--lease-ttl-seconds", type=int, default=None)
    init = sub.add_parser("init")
    init.add_argument("--config-dir", type=Path, default=default_config_dir())
    init.add_argument("--force", action="store_true")
    doctor = sub.add_parser("doctor")
    doctor.add_argument("--config-dir", type=Path, default=default_config_dir())
    doctor.add_argument("--live-tuple-catalog", action="store_true")
    validate = sub.add_parser("validate-config")
    validate.add_argument("--config-dir", type=Path, default=default_config_dir())
    runtime = sub.add_parser("runtime")
    runtime.add_argument("action", choices=["add-openai", "validate", "show"])
    runtime.add_argument("--config-dir", type=Path, default=default_config_dir())
    runtime.add_argument("--name", default=None)
    runtime.add_argument("--endpoint", default=None)
    runtime.add_argument("--model", default="deepseek-v4-flash")
    runtime.add_argument("--model-ref", default="deepseek-v4-flash-local")
    runtime.add_argument("--dataref-worker", action="store_true")
    runtime.add_argument("--runtime-boundary", choices=["gateway_host", "private_network", "site_network"], default="private_network")
    runtime.add_argument("--network-scope", choices=["localhost", "lan", "vpn", "tailscale", "private_subnet", "manual"], default="tailscale")
    runtime.add_argument("--max-model-len", type=int, default=1000000)
    runtime.add_argument("--max-data-classification", choices=["public", "internal", "confidential", "restricted"], default="restricted")
    runtime.add_argument("--force", action="store_true")
    seed = sub.add_parser("seed-liveness")
    seed.add_argument("recipe_name")
    seed.add_argument("--config-dir", type=Path, default=default_config_dir())
    seed.add_argument("--count", type=int, default=3)
    seed.add_argument("--interval", type=float, default=0.0, help="seconds to sleep between seeds; with --count 0, run until interrupted")
    seed.add_argument("--budget-usd", type=float, required=True, help="hard cost ceiling for all seed executions")
    seed.add_argument("--allow-zero-estimate", action="store_true", help="allow seed execution when the compiled tuple cost estimate is zero")
    smoke = sub.add_parser("smoke")
    smoke.add_argument("--url", default="http://127.0.0.1:18088")
    smoke.add_argument("--api-key", default=None)
    smoke.add_argument("--recipe", default=None, help="Deprecated recipe hint for reports only; not sent to the gateway")
    tuple_smoke = sub.add_parser("tuple-smoke")
    tuple_smoke.add_argument("tuple")
    tuple_smoke.add_argument("--config-dir", type=Path, default=default_config_dir())
    tuple_smoke.add_argument("--recipe", default="text-infer-standard")
    tuple_smoke.add_argument("--mode", choices=["sync", "async", "stream"], default="sync")
    tuple_smoke.add_argument("--write-artifact", action="store_true")
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
    execution_catalog = sub.add_parser("execution-catalog")
    execution_catalog.add_argument("action", choices=["snapshot", "candidates"])
    execution_catalog.add_argument("--config-dir", type=Path, default=default_config_dir())
    execution_catalog.add_argument("--recipe", default=None)
    execution_catalog.add_argument("--live", action="store_true", help="revalidate active tuple catalog metadata with live provider catalogs")
    validator_plan = sub.add_parser("validator-plan")
    validator_plan.add_argument("--config-dir", type=Path, default=default_config_dir())
    validator_plan.add_argument("--budget-usd", type=float, required=True)
    validator_plan.add_argument("--max-items", type=int, default=None)
    validator_plan.add_argument("--include-candidates", action="store_true")
    validator_plan.add_argument("--live", action="store_true", help="include live catalog overlay before planning validation work")
    audit = sub.add_parser("audit")
    audit.add_argument("action", choices=["verify", "tail", "rotate"])
    audit.add_argument("--limit", type=int, default=20)
    audit.add_argument("--max-bytes", type=int, default=100 * 1024 * 1024)
    cost_audit = sub.add_parser("cost-audit")
    cost_audit.add_argument("--config-dir", type=Path, default=default_config_dir())
    cost_audit.add_argument("--live", action="store_true")
    tuple_audit = sub.add_parser("tuple-audit")
    tuple_audit.add_argument("--config-dir", type=Path, default=default_config_dir())
    tuple_audit.add_argument("--recipe", default=None)
    tuple_audit.add_argument("--live", action="store_true")
    cleanup_audit = sub.add_parser("cleanup-audit")
    cleanup_audit.add_argument("--config-dir", type=Path, default=default_config_dir())
    lease_reaper = sub.add_parser("lease-reaper")
    lease_reaper.add_argument("--manifest", type=Path, default=None)
    lease_reaper.add_argument("--apply", action="store_true")
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
    release_check = sub.add_parser("release-check")
    release_check.add_argument("--config-dir", type=Path, default=default_config_dir())
    release_check.add_argument("--output-dir", type=Path, default=default_state_dir() / "release")
    add_readiness_parser(sub)
    add_setup_parser(sub)
    configure = sub.add_parser("configure")
    configure.add_argument("--config-dir", type=Path, default=default_config_dir())
    admin = sub.add_parser("admin")
    admin.add_argument(
        "action",
        choices=[
            "status",
            "tenant-list",
            "tenant-create",
            "tenant-key-create",
            "tenant-key-list",
            "tenant-onboard",
            "tenant-onboard-batch",
            "tenant-usage",
            "automation-status",
            "automation-configure",
        ],
    )
    admin.add_argument("--config-dir", type=Path, default=default_config_dir())
    admin.add_argument("--name", default=None)
    admin.add_argument("--manifest", type=Path, default=None)
    admin.add_argument("--gateway-url", default=None)
    admin.add_argument("--recipe-inbox", default=None)
    admin.add_argument("--output", type=Path, default=None)
    admin.add_argument("--format", choices=["json", "env"], default="json")
    admin.add_argument("--requests-per-minute", type=int, default=None)
    admin.add_argument("--daily-budget-usd", type=float, default=None)
    admin.add_argument("--monthly-budget-usd", type=float, default=None)
    admin.add_argument("--max-request-estimated-cost-usd", type=float, default=None)
    admin.add_argument("--object-prefix", default=None)
    admin.add_argument("--handoff-mode", choices=[mode.value for mode in ApiKeyHandoffMode], default=None)
    admin.add_argument("--bootstrap-allowed-cidr", action="append", default=None)
    admin.add_argument("--bootstrap-allowed-host", action="append", default=None)
    admin.add_argument("--bootstrap-gateway-url", default=None)
    admin.add_argument("--bootstrap-recipe-inbox", default=None)
    admin.add_argument("--clear-bootstrap-allowlist", action="store_true")
    admin.add_argument("--enable-recipe-auto-materialize", action="store_true")
    admin.add_argument("--disable-recipe-auto-materialize", action="store_true")
    admin.add_argument("--enable-recipe-auto-validate-existing-tuples", action="store_true")
    admin.add_argument("--disable-recipe-auto-validate-existing-tuples", action="store_true")
    admin.add_argument("--enable-recipe-auto-activate-existing", action="store_true")
    admin.add_argument("--disable-recipe-auto-activate-existing", action="store_true")
    admin.add_argument("--enable-recipe-auto-promote", action="store_true")
    admin.add_argument("--disable-recipe-auto-promote", action="store_true")
    admin.add_argument("--enable-recipe-auto-billable-validation", action="store_true")
    admin.add_argument("--disable-recipe-auto-billable-validation", action="store_true")
    admin.add_argument("--enable-recipe-auto-activate", action="store_true")
    admin.add_argument("--disable-recipe-auto-activate", action="store_true")
    admin.add_argument("--enable-recipe-auto-require-auto-select-safe", action="store_true")
    admin.add_argument("--disable-recipe-auto-require-auto-select-safe", action="store_true")
    admin.add_argument("--enable-recipe-auto-set-auto-select", action="store_true")
    admin.add_argument("--disable-recipe-auto-set-auto-select", action="store_true")
    admin.add_argument("--enable-recipe-auto-run-validate-config", action="store_true")
    admin.add_argument("--disable-recipe-auto-run-validate-config", action="store_true")
    admin.add_argument("--enable-recipe-auto-run-launch-check", action="store_true")
    admin.add_argument("--disable-recipe-auto-run-launch-check", action="store_true")
    admin.add_argument("--recipe-promotion-work-dir", default=None)
    admin.add_argument("--onboarding-prompt-url", default=None)
    admin.add_argument("--onboarding-manual-url", default=None)
    admin.add_argument("--caller-sdk-wheel-url", default=None)
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
                requested_tuple=args.tuple_name,
                max_tokens=args.max_tokens,
                timeout_seconds=args.timeout_seconds,
                lease_ttl_seconds=args.lease_ttl_seconds,
                webhook_url="https://example.invalid/gpucall/explain" if mode is ExecutionMode.ASYNC else None,
            )
            compiler = GovernanceCompiler(
                policy=config.policy,
                recipes=config.recipes,
                tuples=config.tuples,
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
                        "max_data_classification": config.policy.tuples.max_data_classification,
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
        doctor_config(args.config_dir, live_tuple_catalog=args.live_tuple_catalog)
    elif args.command == "validate-config":
        validate_config_command(args.config_dir)
    elif args.command == "runtime":
        runtime_command(args)
    elif args.command == "seed-liveness":
        asyncio.run(
            seed_liveness(
                args.config_dir,
                args.recipe_name,
                args.count,
                interval=args.interval,
                budget_usd=args.budget_usd,
                allow_zero_estimate=args.allow_zero_estimate,
            )
        )
    elif args.command == "smoke":
        smoke_gateway(args.url, api_key=args.api_key, recipe=args.recipe)
    elif args.command == "tuple-smoke":
        asyncio.run(
            provider_smoke_command(
                args.config_dir,
                args.tuple,
                args.recipe,
                ExecutionMode(args.mode),
                write_artifact=args.write_artifact,
            )
        )
    elif args.command == "jobs":
        asyncio.run(jobs_command(args.job_id, args.limit, scrub_inputs=args.scrub_inputs, expire_stale=args.expire_stale))
    elif args.command == "audit":
        audit_command(args.action, args.limit, args.max_bytes)
    elif args.command == "cost-audit":
        cost_audit_command(args.config_dir, live=args.live)
    elif args.command == "tuple-audit":
        tuple_audit_command(args.config_dir, recipe=args.recipe, live=args.live)
    elif args.command == "cleanup-audit":
        cleanup_audit_command(args.config_dir)
    elif args.command == "lease-reaper":
        lease_reaper_command(args.manifest, apply=args.apply)
    elif args.command == "registry":
        registry_command(args.action)
    elif args.command == "catalog":
        catalog_command(args.action, args.config_dir, args.db)
    elif args.command == "execution-catalog":
        execution_catalog_command(args.action, args.config_dir, recipe=args.recipe, live=args.live)
    elif args.command == "validator-plan":
        validator_plan_command(
            args.config_dir,
            budget_usd=args.budget_usd,
            max_items=args.max_items,
            include_candidates=args.include_candidates,
            live=args.live,
        )
    elif args.command == "security":
        security_command(args.action, args.config_dir)
    elif args.command == "openapi":
        print(json.dumps(create_app(args.config_dir).openapi(), indent=2, sort_keys=True))
    elif args.command == "launch-check":
        launch_check_command(args.config_dir, url=args.url, api_key=args.api_key, profile=args.profile)
    elif args.command == "post-launch-report":
        asyncio.run(post_launch_report_command(args.config_dir))
    elif args.command == "release-check":
        release_check_command(args.config_dir, args.output_dir)
    elif args.command == "readiness":
        run_readiness_command(args)
    elif args.command == "setup":
        run_setup_command(args)
    elif args.command == "configure":
        configure_command(args.config_dir)
    elif args.command == "admin":
        admin_command(
            args.action,
            args.config_dir,
            name=args.name,
            manifest=args.manifest,
            gateway_url=args.gateway_url,
            recipe_inbox=args.recipe_inbox,
            output=args.output,
            output_format=args.format,
            requests_per_minute=args.requests_per_minute,
            daily_budget_usd=args.daily_budget_usd,
            monthly_budget_usd=args.monthly_budget_usd,
            max_request_estimated_cost_usd=args.max_request_estimated_cost_usd,
            object_prefix=args.object_prefix,
            handoff_mode=args.handoff_mode,
            bootstrap_allowed_cidrs=args.bootstrap_allowed_cidr,
            bootstrap_allowed_hosts=args.bootstrap_allowed_host,
            bootstrap_gateway_url=args.bootstrap_gateway_url,
            bootstrap_recipe_inbox=args.bootstrap_recipe_inbox,
            clear_bootstrap_allowlist=args.clear_bootstrap_allowlist,
            enable_recipe_auto_materialize=args.enable_recipe_auto_materialize,
            disable_recipe_auto_materialize=args.disable_recipe_auto_materialize,
            enable_recipe_auto_validate_existing_tuples=args.enable_recipe_auto_validate_existing_tuples,
            disable_recipe_auto_validate_existing_tuples=args.disable_recipe_auto_validate_existing_tuples,
            enable_recipe_auto_activate_existing=args.enable_recipe_auto_activate_existing,
            disable_recipe_auto_activate_existing=args.disable_recipe_auto_activate_existing,
            enable_recipe_auto_promote=args.enable_recipe_auto_promote,
            disable_recipe_auto_promote=args.disable_recipe_auto_promote,
            enable_recipe_auto_billable_validation=args.enable_recipe_auto_billable_validation,
            disable_recipe_auto_billable_validation=args.disable_recipe_auto_billable_validation,
            enable_recipe_auto_activate=args.enable_recipe_auto_activate,
            disable_recipe_auto_activate=args.disable_recipe_auto_activate,
            enable_recipe_auto_require_auto_select_safe=args.enable_recipe_auto_require_auto_select_safe,
            disable_recipe_auto_require_auto_select_safe=args.disable_recipe_auto_require_auto_select_safe,
            enable_recipe_auto_set_auto_select=args.enable_recipe_auto_set_auto_select,
            disable_recipe_auto_set_auto_select=args.disable_recipe_auto_set_auto_select,
            enable_recipe_auto_run_validate_config=args.enable_recipe_auto_run_validate_config,
            disable_recipe_auto_run_validate_config=args.disable_recipe_auto_run_validate_config,
            enable_recipe_auto_run_launch_check=args.enable_recipe_auto_run_launch_check,
            disable_recipe_auto_run_launch_check=args.disable_recipe_auto_run_launch_check,
            recipe_promotion_work_dir=args.recipe_promotion_work_dir,
            onboarding_prompt_url=args.onboarding_prompt_url,
            onboarding_manual_url=args.onboarding_manual_url,
            caller_sdk_wheel_url=args.caller_sdk_wheel_url,
        )


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


def doctor_config(config_dir: Path, *, live_tuple_catalog: bool = False) -> None:
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
        "tuples": sorted(config.tuples),
        "runtimes": sorted(config.runtimes),
        "models": sorted(config.models),
        "engines": sorted(config.engines),
        "object_store": config.object_store.model_dump(mode="json") if config.object_store else None,
        "registry": _configured_registry_snapshot(config),
        "routing": _routing_decision_summary(config),
        "secrets": _secret_presence_summary(creds),
    }
    if live_tuple_catalog:
        catalog_findings = live_tuple_catalog_findings(config.tuples, creds)
        checks["live_tuple_catalog"] = {
            "ok": not catalog_findings,
            "findings": catalog_findings,
        }
    print(json.dumps(checks, indent=2, sort_keys=True))


def validate_config_command(config_dir: Path) -> None:
    config = load_config(config_dir)
    print(
        json.dumps(
            {
                "valid": True,
                "recipes": sorted(config.recipes),
                "runtimes": sorted(config.runtimes),
                "tuples": sorted(config.tuples),
                "tenants": sorted(config.tenants),
            },
            indent=2,
            sort_keys=True,
        )
    )


def runtime_command(args) -> None:
    if args.action == "show":
        config = load_config(args.config_dir)
        print(json.dumps({name: runtime.model_dump(mode="json") for name, runtime in sorted(config.runtimes.items())}, indent=2, sort_keys=True))
        return
    if args.action == "add-openai":
        if not args.name or not args.endpoint:
            raise SystemExit("runtime add-openai requires --name and --endpoint")
        result = add_openai_runtime(
            config_dir=args.config_dir,
            name=args.name,
            endpoint=args.endpoint,
            model=args.model,
            model_ref=args.model_ref,
            max_model_len=args.max_model_len,
            dataref_worker=bool(args.dataref_worker),
            runtime_boundary=args.runtime_boundary,
            network_scope=args.network_scope,
            max_data_classification=args.max_data_classification,
            force=bool(args.force),
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    if args.action == "validate":
        if not args.name:
            raise SystemExit("runtime validate requires --name")
        print(json.dumps(validate_runtime(args.config_dir, args.name), indent=2, sort_keys=True))
        return
    raise SystemExit(f"unknown runtime action: {args.action}")


def add_openai_runtime(
    *,
    config_dir: Path,
    name: str,
    endpoint: str,
    model: str,
    model_ref: str,
    max_model_len: int,
    dataref_worker: bool,
    runtime_boundary: str,
    network_scope: str,
    max_data_classification: str,
    force: bool,
) -> dict[str, object]:
    adapter = "local-dataref-openai-worker" if dataref_worker else "local-openai-compatible"
    engine_ref = "local-dataref-openai-worker-chat" if dataref_worker else "local-openai-compatible-chat"
    input_contracts = ["text", "chat_messages", "data_refs"] if dataref_worker else ["text", "chat_messages"]
    output_contract = "gpucall-tuple-result" if dataref_worker else "openai-chat-completions"
    stream_contract = "none" if dataref_worker else "sse"
    endpoint_contract = "local-dataref-openai-worker" if dataref_worker else "openai-chat-completions"
    modes = ["async"] if dataref_worker else ["async", "stream"]
    health_url = f"{endpoint.rstrip('/')}/healthz" if dataref_worker else f"{endpoint.rstrip('/')}/models"
    trust_profile = {
        "security_tier": "local",
        "sovereign_jurisdiction": "local",
        "dedicated_gpu": True,
        "requires_attestation": False,
        "supports_key_release": False,
        "allows_worker_s3_credentials": False,
    }
    now = datetime.now(timezone.utc).isoformat()
    runtime_payload = {
        "name": name,
        "kind": "controlled_runtime",
        "runtime_boundary": runtime_boundary,
        "network_scope": network_scope,
        "operator_controlled": True,
        "endpoint": endpoint,
        "adapter": adapter,
        "model": model,
        "max_model_len": max_model_len,
        "input_contracts": input_contracts,
        "max_data_classification": max_data_classification,
        "trust_profile": trust_profile,
        "routing": {
            "enabled": True,
            "preference": "prefer_when_eligible",
            "allowed_tasks": ["infer"],
            "allowed_modes": modes,
            "require_validation_evidence": True,
        },
        "health": {
            "check_url": health_url,
            "timeout_seconds": 2,
            "failure_policy": "disable_runtime",
        },
        "discovery": {
            "source": "manual",
            "last_verified_at": now,
        },
    }
    surface_payload = {
        "surface_ref": name,
        "worker_ref": name,
        "controlled_runtime_ref": name,
        "account_ref": "local",
        "adapter": adapter,
        "execution_surface": "local_runtime",
        "gpu": "local",
        "vram_gb": 1,
        "max_model_len": max_model_len,
        "region": "local",
        "zone": "local",
        "cost_per_second": 0,
        "configured_price_source": "local-free",
        "configured_price_observed_at": now,
        "configured_price_ttl_seconds": 315360000,
        "stock_state": "configured",
        "expected_cold_start_seconds": 10,
        "billing_granularity_seconds": 0,
        "max_data_classification": max_data_classification,
        "scaledown_window_seconds": 0,
        "min_billable_seconds": 0,
        "trust_profile": trust_profile,
        "endpoint": endpoint,
    }
    worker_payload = {
        "worker_ref": name,
        "account_ref": "local",
        "adapter": adapter,
        "execution_surface": "local_runtime",
        "model_ref": model_ref,
        "engine_ref": engine_ref,
        "modes": modes,
        "input_contracts": input_contracts,
        "output_contract": output_contract,
        "stream_contract": stream_contract,
        "target": None,
        "stream_target": None,
        "endpoint_contract": endpoint_contract,
        "model": model,
    }
    if dataref_worker:
        worker_payload["provider_params"] = {"worker_api_key_env": "GPUCALL_LOCAL_DATAREF_WORKER_API_KEY"}
    paths = {
        "runtime": config_dir / "runtimes" / f"{name}.yml",
        "surface": config_dir / "surfaces" / f"{name}.yml",
        "worker": config_dir / "workers" / f"{name}.yml",
    }
    for path, payload in (
        (paths["runtime"], runtime_payload),
        (paths["surface"], surface_payload),
        (paths["worker"], worker_payload),
    ):
        _write_yaml(path, payload, force=force)
    config = load_config(config_dir)
    return {"created": {key: str(path) for key, path in paths.items()}, "valid": name in config.runtimes and name in config.tuples}


def validate_runtime(config_dir: Path, name: str) -> dict[str, object]:
    config = load_config(config_dir)
    runtime = config.runtimes.get(name)
    if runtime is None:
        raise SystemExit(f"unknown controlled runtime: {name}")
    health_url = str(runtime.health.check_url or "").strip()
    if not health_url and runtime.endpoint:
        health_url = f"{str(runtime.endpoint).rstrip('/')}/healthz"
    if not health_url:
        raise SystemExit(f"controlled runtime {name!r} has no endpoint or health check")
    try:
        with httpx.Client(timeout=runtime.health.timeout_seconds) as client:
            response = client.get(health_url)
        ok = response.status_code < 500 and response.status_code != 404
    except httpx.HTTPError as exc:
        return {"name": name, "ok": False, "health_url": health_url, "error": type(exc).__name__}
    return {
        "name": name,
        "ok": ok,
        "health_url": health_url,
        "status_code": response.status_code,
        "routing_enabled": runtime.routing.enabled,
        "preference": runtime.routing.preference,
    }


def _write_yaml(path: Path, payload: dict[str, object], *, force: bool) -> None:
    if path.exists() and not force:
        raise SystemExit(f"refusing to overwrite existing file without --force: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def admin_command(
    action: str,
    config_dir: Path,
    *,
    name: str | None,
    manifest: Path | None = None,
    gateway_url: str | None = None,
    recipe_inbox: str | None = None,
    output: Path | None = None,
    output_format: str = "json",
    requests_per_minute: int | None,
    daily_budget_usd: float | None,
    monthly_budget_usd: float | None,
    max_request_estimated_cost_usd: float | None,
    object_prefix: str | None,
    handoff_mode: str | None = None,
    bootstrap_allowed_cidrs: list[str] | None = None,
    bootstrap_allowed_hosts: list[str] | None = None,
    bootstrap_gateway_url: str | None = None,
    bootstrap_recipe_inbox: str | None = None,
    clear_bootstrap_allowlist: bool = False,
    enable_recipe_auto_materialize: bool = False,
    disable_recipe_auto_materialize: bool = False,
    enable_recipe_auto_validate_existing_tuples: bool = False,
    disable_recipe_auto_validate_existing_tuples: bool = False,
    enable_recipe_auto_activate_existing: bool = False,
    disable_recipe_auto_activate_existing: bool = False,
    enable_recipe_auto_promote: bool = False,
    disable_recipe_auto_promote: bool = False,
    enable_recipe_auto_billable_validation: bool = False,
    disable_recipe_auto_billable_validation: bool = False,
    enable_recipe_auto_activate: bool = False,
    disable_recipe_auto_activate: bool = False,
    enable_recipe_auto_require_auto_select_safe: bool = False,
    disable_recipe_auto_require_auto_select_safe: bool = False,
    enable_recipe_auto_set_auto_select: bool = False,
    disable_recipe_auto_set_auto_select: bool = False,
    enable_recipe_auto_run_validate_config: bool = False,
    disable_recipe_auto_run_validate_config: bool = False,
    enable_recipe_auto_run_launch_check: bool = False,
    disable_recipe_auto_run_launch_check: bool = False,
    recipe_promotion_work_dir: str | None = None,
    onboarding_prompt_url: str | None = None,
    onboarding_manual_url: str | None = None,
    caller_sdk_wheel_url: str | None = None,
) -> None:
    if action == "tenant-create":
        if not name:
            raise SystemExit("admin tenant-create requires --name")
        path = config_dir / "tenants" / f"{name}.yml"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            raise SystemExit(f"tenant already exists: {name}")
        payload = {
            "name": name,
            "requests_per_minute": requests_per_minute or 120,
            "daily_budget_usd": daily_budget_usd if daily_budget_usd is not None else 25.0,
            "monthly_budget_usd": monthly_budget_usd if monthly_budget_usd is not None else 500.0,
            "max_request_estimated_cost_usd": max_request_estimated_cost_usd if max_request_estimated_cost_usd is not None else 10.0,
            "object_prefix": object_prefix or name,
        }
        path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
        print(json.dumps({"created": name, "path": str(path), "credential_action": "add auth.tenant_keys entry outside YAML"}, indent=2))
        return
    config = load_config(config_dir)
    if action == "automation-status":
        print(json.dumps(admin_automation_summary(config_dir), indent=2, sort_keys=True))
        return
    if action == "automation-configure":
        if enable_recipe_auto_materialize and disable_recipe_auto_materialize:
            raise SystemExit("choose only one of --enable-recipe-auto-materialize or --disable-recipe-auto-materialize")
        if enable_recipe_auto_validate_existing_tuples and disable_recipe_auto_validate_existing_tuples:
            raise SystemExit("choose only one of --enable-recipe-auto-validate-existing-tuples or --disable-recipe-auto-validate-existing-tuples")
        if enable_recipe_auto_activate_existing and disable_recipe_auto_activate_existing:
            raise SystemExit("choose only one of --enable-recipe-auto-activate-existing or --disable-recipe-auto-activate-existing")
        if enable_recipe_auto_promote and disable_recipe_auto_promote:
            raise SystemExit("choose only one of --enable-recipe-auto-promote or --disable-recipe-auto-promote")
        if enable_recipe_auto_billable_validation and disable_recipe_auto_billable_validation:
            raise SystemExit("choose only one of --enable-recipe-auto-billable-validation or --disable-recipe-auto-billable-validation")
        if enable_recipe_auto_activate and disable_recipe_auto_activate:
            raise SystemExit("choose only one of --enable-recipe-auto-activate or --disable-recipe-auto-activate")
        if enable_recipe_auto_require_auto_select_safe and disable_recipe_auto_require_auto_select_safe:
            raise SystemExit("choose only one of --enable-recipe-auto-require-auto-select-safe or --disable-recipe-auto-require-auto-select-safe")
        if enable_recipe_auto_set_auto_select and disable_recipe_auto_set_auto_select:
            raise SystemExit("choose only one of --enable-recipe-auto-set-auto-select or --disable-recipe-auto-set-auto-select")
        if enable_recipe_auto_run_validate_config and disable_recipe_auto_run_validate_config:
            raise SystemExit("choose only one of --enable-recipe-auto-run-validate-config or --disable-recipe-auto-run-validate-config")
        if enable_recipe_auto_run_launch_check and disable_recipe_auto_run_launch_check:
            raise SystemExit("choose only one of --enable-recipe-auto-run-launch-check or --disable-recipe-auto-run-launch-check")
        auto_materialize = None
        if enable_recipe_auto_materialize:
            auto_materialize = True
        if disable_recipe_auto_materialize:
            auto_materialize = False
        auto_validate_existing = None
        if enable_recipe_auto_validate_existing_tuples:
            auto_validate_existing = True
        if disable_recipe_auto_validate_existing_tuples:
            auto_validate_existing = False
        auto_activate_existing = None
        if enable_recipe_auto_activate_existing:
            auto_activate_existing = True
        if disable_recipe_auto_activate_existing:
            auto_activate_existing = False
        auto_promote = None
        if enable_recipe_auto_promote:
            auto_promote = True
        if disable_recipe_auto_promote:
            auto_promote = False
        auto_billable_validation = None
        if enable_recipe_auto_billable_validation:
            auto_billable_validation = True
        if disable_recipe_auto_billable_validation:
            auto_billable_validation = False
        auto_activate = None
        if enable_recipe_auto_activate:
            auto_activate = True
        if disable_recipe_auto_activate:
            auto_activate = False
        require_auto_select_safe = None
        if enable_recipe_auto_require_auto_select_safe:
            require_auto_select_safe = True
        if disable_recipe_auto_require_auto_select_safe:
            require_auto_select_safe = False
        auto_set_auto_select = None
        if enable_recipe_auto_set_auto_select:
            auto_set_auto_select = True
        if disable_recipe_auto_set_auto_select:
            auto_set_auto_select = False
        auto_run_validate_config = None
        if enable_recipe_auto_run_validate_config:
            auto_run_validate_config = True
        if disable_recipe_auto_run_validate_config:
            auto_run_validate_config = False
        auto_run_launch_check = None
        if enable_recipe_auto_run_launch_check:
            auto_run_launch_check = True
        if disable_recipe_auto_run_launch_check:
            auto_run_launch_check = False
        try:
            updated = configure_admin_automation(
                config_dir,
                handoff_mode=ApiKeyHandoffMode(handoff_mode) if handoff_mode else None,
                recipe_inbox_auto_materialize=auto_materialize,
                recipe_inbox_auto_validate_existing_tuples=auto_validate_existing,
                recipe_inbox_auto_activate_existing_validated_recipe=auto_activate_existing,
                recipe_inbox_auto_promote_candidates=auto_promote,
                recipe_inbox_auto_billable_validation=auto_billable_validation,
                recipe_inbox_auto_activate_validated=auto_activate,
                recipe_inbox_auto_require_auto_select_safe=require_auto_select_safe,
                recipe_inbox_auto_set_auto_select=auto_set_auto_select,
                recipe_inbox_auto_run_validate_config=auto_run_validate_config,
                recipe_inbox_auto_run_launch_check=auto_run_launch_check,
                recipe_inbox_promotion_work_dir=recipe_promotion_work_dir,
                bootstrap_allowed_cidrs=bootstrap_allowed_cidrs,
                bootstrap_allowed_hosts=bootstrap_allowed_hosts,
                bootstrap_gateway_url=bootstrap_gateway_url,
                bootstrap_recipe_inbox=bootstrap_recipe_inbox,
                onboarding_prompt_url=onboarding_prompt_url,
                onboarding_manual_url=onboarding_manual_url,
                caller_sdk_wheel_url=caller_sdk_wheel_url,
                clear_bootstrap_allowlist=clear_bootstrap_allowlist,
            )
        except (ValueError, ValidationError) as exc:
            raise SystemExit(str(exc)) from exc
        print(
            json.dumps(
                {
                    "updated": str(config_dir / "admin.yml"),
                    "admin_automation": admin_automation_summary(config_dir),
                    "safe_default": updated.api_key_handoff_mode is ApiKeyHandoffMode.MANUAL,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return
    if action == "tenant-onboard":
        if not name:
            raise SystemExit("admin tenant-onboard requires --name")
        if not gateway_url:
            raise SystemExit("admin tenant-onboard requires --gateway-url")
        if not recipe_inbox:
            raise SystemExit("admin tenant-onboard requires --recipe-inbox")
        if config.admin_automation.api_key_handoff_mode is not ApiKeyHandoffMode.HANDOFF_FILE:
            raise SystemExit("admin tenant-onboard requires admin.yml api_key_handoff_mode: handoff_file")
        if output is None:
            raise SystemExit("admin tenant-onboard requires --output when api_key_handoff_mode is handoff_file")
        tenant_path = _ensure_tenant_file(
            config_dir,
            name=name,
            requests_per_minute=requests_per_minute,
            daily_budget_usd=daily_budget_usd,
            monthly_budget_usd=monthly_budget_usd,
            max_request_estimated_cost_usd=max_request_estimated_cost_usd,
            object_prefix=object_prefix,
        )
        token = _create_tenant_key(name)
        handoff = _handoff_payload(
            tenant=name,
            token=token,
            gateway_url=gateway_url,
            recipe_inbox=recipe_inbox,
        )
        rendered = _render_handoff(handoff, output_format)
        _write_secret_file(output, rendered)
        print(
            json.dumps(
                {
                    "tenant": name,
                    "tenant_path": str(tenant_path),
                    "handoff_path": str(output),
                    "handoff_format": output_format,
                    "api_key_fingerprint": _fingerprint_secret(token),
                    "credentials_path": str(credentials_path()),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return
    if action == "tenant-onboard-batch":
        if not gateway_url:
            raise SystemExit("admin tenant-onboard-batch requires --gateway-url")
        if not recipe_inbox:
            raise SystemExit("admin tenant-onboard-batch requires --recipe-inbox")
        if output is None:
            raise SystemExit("admin tenant-onboard-batch requires --output directory")
        if config.admin_automation.api_key_handoff_mode is not ApiKeyHandoffMode.HANDOFF_FILE:
            raise SystemExit("admin tenant-onboard-batch requires admin.yml api_key_handoff_mode: handoff_file")
        specs = _batch_onboard_specs(manifest)
        _validate_batch_specs(specs, output, output_format)
        output.mkdir(parents=True, exist_ok=True)
        os.chmod(output, 0o700)
        results: list[dict[str, str]] = []
        for spec in specs:
            tenant_name = spec["name"]
            handoff_path = output / f"{tenant_name}.gpucall.{output_format}"
            tenant_path = _ensure_tenant_file(
                config_dir,
                name=tenant_name,
                requests_per_minute=_optional_int(spec.get("requests_per_minute")),
                daily_budget_usd=_optional_float(spec.get("daily_budget_usd")),
                monthly_budget_usd=_optional_float(spec.get("monthly_budget_usd")),
                max_request_estimated_cost_usd=_optional_float(spec.get("max_request_estimated_cost_usd")),
                object_prefix=str(spec.get("object_prefix") or tenant_name),
            )
            token = _create_tenant_key(tenant_name)
            rendered = _render_handoff(
                _handoff_payload(tenant=tenant_name, token=token, gateway_url=gateway_url, recipe_inbox=recipe_inbox),
                output_format,
            )
            _write_secret_file(handoff_path, rendered)
            results.append(
                {
                    "tenant": tenant_name,
                    "tenant_path": str(tenant_path),
                    "handoff_path": str(handoff_path),
                    "api_key_fingerprint": _fingerprint_secret(token),
                }
            )
        print(
            json.dumps(
                {
                    "created": results,
                    "count": len(results),
                    "handoff_dir": str(output),
                    "handoff_format": output_format,
                    "credentials_path": str(credentials_path()),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return
    if action == "tenant-key-create":
        if not name:
            raise SystemExit("admin tenant-key-create requires --name")
        if name not in config.tenants:
            raise SystemExit(f"unknown tenant: {name}; run admin tenant-create first")
        token = _create_tenant_key(name)
        print(
            json.dumps(
                {
                    "tenant": name,
                    "api_key": token,
                    "api_key_fingerprint": _fingerprint_secret(token),
                    "credentials_path": str(credentials_path()),
                    "handoff": {
                        "GPUCALL_API_KEY": token,
                        "secret_handling": "show this value only once; store it in the caller system secret manager or process environment",
                    },
                },
                indent=2,
                sort_keys=True,
            )
        )
        return
    if action == "tenant-key-list":
        creds = load_credentials()
        auth = dict(creds.get("auth", {}))
        tenant_keys = _parse_tenant_key_pairs(auth.get("tenant_keys", ""))
        print(
            json.dumps(
                {
                    "credentials_path": str(credentials_path()),
                    "tenant_keys": {
                        tenant: {"configured": True, "api_key_fingerprint": _fingerprint_secret(key)}
                        for tenant, key in sorted(tenant_keys.items())
                    },
                },
                indent=2,
                sort_keys=True,
            )
        )
        return
    if action == "tenant-list":
        print(json.dumps({"tenants": {name: tenant.model_dump(mode="json") for name, tenant in sorted(config.tenants.items())}}, indent=2, sort_keys=True))
        return
    if action == "tenant-usage":
        ledger = TenantUsageLedger(default_state_dir() / "tenant_usage.db")
        print(json.dumps({"tenants": ledger.summary(config.tenants)}, indent=2, sort_keys=True))
        return
    if action == "status":
        report = {
            "config_valid": True,
            "tenant_count": len(config.tenants),
            "tenants": sorted(config.tenants),
            "state_dir": str(default_state_dir()),
            "credentials_path": str(credentials_path()),
            "tenant_usage_db": str(default_state_dir() / "tenant_usage.db"),
            "admin_automation": admin_automation_summary(config_dir),
        }
        print(json.dumps(report, indent=2, sort_keys=True))
        return
    raise SystemExit(f"unknown admin action: {action}")


def _parse_tenant_key_pairs(raw: str) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for item in raw.split(","):
        tenant, sep, key = item.partition(":")
        tenant = tenant.strip()
        key = key.strip()
        if sep and tenant and key:
            pairs[tenant] = key
    return pairs


def _format_tenant_key_pairs(pairs: dict[str, str]) -> str:
    return ",".join(f"{tenant}:{key}" for tenant, key in sorted(pairs.items()))


def _fingerprint_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _ensure_tenant_file(
    config_dir: Path,
    *,
    name: str,
    requests_per_minute: int | None,
    daily_budget_usd: float | None,
    monthly_budget_usd: float | None,
    max_request_estimated_cost_usd: float | None,
    object_prefix: str | None,
) -> Path:
    path = config_dir / "tenants" / f"{name}.yml"
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "name": name,
        "requests_per_minute": requests_per_minute or 120,
        "daily_budget_usd": daily_budget_usd if daily_budget_usd is not None else 25.0,
        "monthly_budget_usd": monthly_budget_usd if monthly_budget_usd is not None else 500.0,
        "max_request_estimated_cost_usd": max_request_estimated_cost_usd if max_request_estimated_cost_usd is not None else 10.0,
        "object_prefix": object_prefix or name,
    }
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def _create_tenant_key(name: str) -> str:
    creds = load_credentials()
    auth = dict(creds.get("auth", {}))
    tenant_keys = _parse_tenant_key_pairs(auth.get("tenant_keys", ""))
    if name in tenant_keys:
        raise SystemExit(f"tenant key already exists for {name}; rotate by removing or replacing auth.tenant_keys outside this command")
    token = "gpk_" + secrets.token_urlsafe(32)
    tenant_keys[name] = token
    auth["tenant_keys"] = _format_tenant_key_pairs(tenant_keys)
    from gpucall.credentials import save_credentials

    save_credentials("auth", auth)
    return token


def _write_secret_file(path: Path, payload: str) -> None:
    if path.exists():
        raise SystemExit(f"refusing to overwrite existing handoff file: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise


_TENANT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,62}$")


def _batch_onboard_specs(manifest: Path | None) -> list[dict[str, object]]:
    if manifest is None:
        raise SystemExit("admin tenant-onboard-batch requires --manifest")
    try:
        payload = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise SystemExit(f"invalid tenant onboard manifest YAML: {exc}") from exc
    if isinstance(payload, list):
        raw_items = payload
    elif isinstance(payload, dict) and isinstance(payload.get("systems"), list):
        raw_items = payload["systems"]
    else:
        raise SystemExit("tenant onboard manifest must be a list or a mapping with systems: [...]")
    specs: list[dict[str, object]] = []
    for index, item in enumerate(raw_items):
        if not isinstance(item, dict):
            raise SystemExit(f"tenant onboard manifest item {index} must be a mapping")
        specs.append(dict(item))
    if not specs:
        raise SystemExit("tenant onboard manifest is empty")
    return specs


def _validate_batch_specs(specs: list[dict[str, object]], output_dir: Path, output_format: str) -> None:
    names: set[str] = set()
    outputs: set[Path] = set()
    existing_keys = _parse_tenant_key_pairs(load_credentials().get("auth", {}).get("tenant_keys", ""))
    for index, spec in enumerate(specs):
        name = str(spec.get("name") or "").strip()
        if not _TENANT_NAME_RE.fullmatch(name):
            raise SystemExit(f"tenant onboard manifest item {index} has invalid name: {name!r}")
        if name in names:
            raise SystemExit(f"tenant onboard manifest contains duplicate tenant name: {name}")
        if name in existing_keys:
            raise SystemExit(f"tenant key already exists for {name}; refusing batch onboarding")
        handoff_path = output_dir / f"{name}.gpucall.{output_format}"
        if handoff_path in outputs:
            raise SystemExit(f"tenant onboard manifest contains duplicate handoff path: {handoff_path}")
        if handoff_path.exists():
            raise SystemExit(f"handoff file already exists: {handoff_path}")
        names.add(name)
        outputs.add(handoff_path)


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    return int(value)


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    return float(value)


def launch_check_command(config_dir: Path, *, url: str | None = None, api_key: str | None = None, profile: str = "production") -> None:
    report = build_launch_report(config_dir, url=url, api_key=api_key, profile=profile)
    path = default_state_dir() / "launch" / "launch-check.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    print(json.dumps({**report, "report_path": str(path)}, indent=2, sort_keys=True, default=str))


def release_check_command(config_dir: Path, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    config = load_config(config_dir)
    openapi_path = output_dir / "openapi.json"
    manifest_path = output_dir / "release-manifest.json"
    launch_report = build_launch_report(config_dir, profile="static")
    openapi_path.write_text(json.dumps(create_app(config_dir).openapi(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    manifest = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "commit": _git_commit(),
        "config_hash": _config_hash(config_dir),
        "policy_version": config.policy.version,
        "tuples": sorted(config.tuples),
        "recipes": sorted(config.recipes),
        "tenants": sorted(config.tenants),
        "static_launch_go": launch_report["go"],
        "static_launch_blockers": launch_report["blockers"],
        "artifacts": {"openapi": str(openapi_path)},
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    print(json.dumps({**manifest, "manifest_path": str(manifest_path)}, indent=2, sort_keys=True, default=str))


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
    cost_audit = _cost_audit_report(config, creds, config_dir=config_dir, live=profile == "production")
    cleanup_audit = _cleanup_audit_report(config)
    tuple_audit = tuple_audit_report(config, config_dir=config_dir, live=False)
    tuple_validation_gaps = _tuple_validation_gaps(tuple_audit) if profile == "production" else []
    live_cost_findings = _live_cost_audit_findings(cost_audit.get("live")) if profile == "production" else []
    gateway_smoke: dict[str, object] | None = None
    if url:
        try:
            smoke_recipe = os.getenv("GPUCALL_LAUNCH_SMOKE_RECIPE") or None
            gateway_smoke = _gateway_smoke_summary(url, api_key=api_key, recipe=smoke_recipe)
        except Exception as exc:
            gateway_smoke = {"ok": False, "error": str(exc)}
    gateway_live_tuples = _gateway_smoke_live_tuples(gateway_smoke, config)
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
            "draft_control_plane_tasks": ["transcribe", "convert", "train", "fine-tune", "split-infer"],
            "deferred": ["high-confidential provider live connections"],
            "control_plane_only": ["transcribe", "convert", "train", "fine-tune", "split-infer"],
        },
        "packaging": {
            "dockerfile": (PROJECT_ROOT / "Dockerfile").exists(),
            "docker_compose": (PROJECT_ROOT / "docker-compose.yml").exists(),
            "helm": (PROJECT_ROOT / "deploy" / "helm" / "gpucall" / "Chart.yaml").exists(),
            "systemd": (PROJECT_ROOT / "deploy" / "systemd" / "gpucall.service").exists(),
            "postgres_schema": (PROJECT_ROOT / "deploy" / "postgres" / "001_init.sql").exists(),
            "prometheus_alerts": (PROJECT_ROOT / "deploy" / "prometheus" / "gpucall-alerts.yml").exists(),
            "python_package": (PROJECT_ROOT / "pyproject.toml").exists(),
            "typescript_sdk": (PROJECT_ROOT / "sdk" / "typescript" / "package.json").exists(),
        },
        "docs": {
            "quickstart": (PROJECT_ROOT / "README.md").exists(),
            "launch_runbook": (PROJECT_ROOT / "LAUNCH_MVP.md").exists(),
            "security": (PROJECT_ROOT / "SECURITY.md").exists(),
            "observability": (PROJECT_ROOT / "docs" / "OBSERVABILITY.md").exists(),
            "tuple_validation": (PROJECT_ROOT / "docs" / "TUPLE_VALIDATION.md").exists(),
        },
        "launch_profile": profile,
        "tenant_governance": {
            "tenant_count": len(config.tenants),
            "tenants": sorted(config.tenants),
            "usage_db": str(default_state_dir() / "tenant_usage.db"),
        },
        "cost_audit": cost_audit,
        "cost_audit_live_ok": not live_cost_findings,
        "cost_audit_live_findings": live_cost_findings,
        "cleanup_audit": cleanup_audit,
        "tuple_audit": _tuple_audit_launch_summary(tuple_audit),
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
    incomplete_cost_metadata = [
        row for row in cost_audit["tuples"] if isinstance(row, dict) and row.get("metadata_complete") is not True
    ]
    if incomplete_cost_metadata:
        blockers.append({"check": "cost_metadata", "tuples": incomplete_cost_metadata})
    if cleanup_audit.get("ok") is not True:
        blockers.append({"check": "cleanup_audit", "summary": cleanup_audit})
    secrets = checks["secrets_present"]
    required_live_tuples = _required_live_validation_tuples(config)
    live_artifacts = _live_validation_artifacts_by_tuple(config, config_dir=config_dir)
    capacity_unavailable_tuples = _capacity_unavailable_validation_tuples(config, config_dir=config_dir)
    missing_live_tuples = [
        item
        for item in required_live_tuples
        if item["tuple_key"] not in live_artifacts
        and item["tuple_key"] not in gateway_live_tuples
        and item["tuple_key"] not in capacity_unavailable_tuples
    ]
    production = profile == "production"
    checks["launch_gates"] = {
        "static_config_valid": checks["config_valid"],
        "auth_required": bool(gateway_smoke and gateway_smoke.get("auth_required") is True),
        "object_store_required": production,
        "object_store_configured": config.object_store is not None and bool(secrets["object_store"]),
        "gateway_live_smoke_passed": bool(gateway_smoke and gateway_smoke.get("ok") is True),
        "tuple_live_validation_passed": not missing_live_tuples,
        "audit_chain_valid": checks["audit_chain_valid"],
    }
    if production:
        if live_cost_findings:
            blockers.append({"check": "tuple_live_cost_audit", "findings": live_cost_findings})
        if not config.tenants:
            blockers.append({"check": "tenant_governance", "configured": False})
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
        if missing_live_tuples:
            blockers.append(
                {
                    "check": "tuple_live_validation",
                    "missing_tuples": missing_live_tuples,
                    "required_tuples": required_live_tuples,
                }
            )
        if tuple_validation_gaps:
            blockers.append({"check": "tuple_validation", "recipes": tuple_validation_gaps})
    report: dict[str, object] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "config_dir": str(config_dir),
        "state_dir": str(default_state_dir()),
        "policy_version": config.policy.version,
        "tuples": sorted(config.tuples),
        "recipes": sorted(config.recipes),
        "checks": checks,
        "registry": provider_samples,
        "gateway_smoke": gateway_smoke,
        "tuple_live_validation": {
            "required_tuples": required_live_tuples,
            "missing_tuples": missing_live_tuples,
            "gateway_live_tuples": gateway_live_tuples,
            "capacity_unavailable_tuples": capacity_unavailable_tuples,
            "artifacts_by_tuple": live_artifacts,
        },
        "blockers": blockers,
        "go": not blockers,
    }
    return report


def _tuple_audit_launch_summary(tuple_audit: dict[str, object]) -> dict[str, object]:
    recipes = tuple_audit.get("recipes") if isinstance(tuple_audit, dict) else {}
    summary: dict[str, object] = {"phase": tuple_audit.get("phase") if isinstance(tuple_audit, dict) else None, "recipes": {}}
    if not isinstance(recipes, dict):
        return summary
    for name, row in sorted(recipes.items()):
        if not isinstance(row, dict):
            continue
        summary["recipes"][name] = {
            "routing_decision": (row.get("routing_decision") or {}).get("decision") if isinstance(row.get("routing_decision"), dict) else None,
            "active_fit_count": row.get("active_fit_count"),
            "production_ready_count": row.get("production_ready_count"),
            "ready_for_validation_count": row.get("ready_for_validation_count"),
            "candidate_fit_count": row.get("candidate_fit_count"),
        }
    return summary


def _tuple_validation_gaps(tuple_audit: dict[str, object]) -> list[dict[str, object]]:
    recipes = tuple_audit.get("recipes") if isinstance(tuple_audit, dict) else {}
    if not isinstance(recipes, dict):
        return [{"recipe": None, "decision": None, "reason": "tuple audit report is malformed"}]
    gaps: list[dict[str, object]] = []
    for name, row in sorted(recipes.items()):
        if not isinstance(row, dict):
            gaps.append({"recipe": name, "decision": None, "reason": "recipe audit row is malformed"})
            continue
        decision = row.get("routing_decision") if isinstance(row.get("routing_decision"), dict) else {}
        if decision.get("decision") == "ROUTABLE":
            continue
        gaps.append(
            {
                "recipe": name,
                "decision": decision.get("decision"),
                "reason": decision.get("reason"),
                "active_fit_count": row.get("active_fit_count"),
                "production_ready_count": row.get("production_ready_count"),
                "ready_for_validation_count": row.get("ready_for_validation_count"),
                "candidate_fit_count": row.get("candidate_fit_count"),
            }
        )
    return gaps


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
            "review tuple cost dashboards",
            "review tuple success rates",
            "review SDK feedback",
            "triage docs corrections",
            "groom v2.1 backlog",
        ],
    }
    path = default_state_dir() / "launch" / "post-launch-report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    print(json.dumps({**report, "report_path": str(path)}, indent=2, sort_keys=True, default=str))


async def seed_liveness(
    config_dir: Path,
    recipe_name: str,
    count: int,
    *,
    interval: float = 0.0,
    budget_usd: float,
    allow_zero_estimate: bool = False,
) -> None:
    if budget_usd < 0:
        raise SystemExit("--budget-usd must be non-negative")
    runtime = build_runtime(config_dir)
    recipe = runtime.compiler.recipes.get(recipe_name)
    if recipe is None:
        raise SystemExit(f"unknown recipe: {recipe_name}")
    completed = 0
    reserved_cost = 0.0
    while count <= 0 or completed < count:
        request = TaskRequest(
            task=recipe.task,
            mode=ExecutionMode.SYNC,
            recipe=recipe.name,
            inline_inputs={"prompt": {"value": "gpucall liveness seed"}},
        )
        plan = runtime.compiler.compile(request)
        estimated = float((plan.attestations.get("cost_estimate") or {}).get("estimated_cost_usd") or 0.0)
        if estimated <= 0.0 and not allow_zero_estimate:
            raise SystemExit("seed-liveness refuses zero-cost estimates unless --allow-zero-estimate is set")
        if reserved_cost + estimated > budget_usd:
            raise SystemExit(
                f"seed-liveness budget exceeded before execution: reserved={reserved_cost:.6f}, next={estimated:.6f}, budget={budget_usd:.6f}"
            )
        reserved_cost += estimated
        await runtime.dispatcher.execute_sync(plan)
        completed += 1
        if interval > 0 and (count <= 0 or completed < count):
            await asyncio.sleep(interval)
    print(json.dumps({"recipe": recipe_name, "seed_jobs": completed, "reserved_cost_usd": reserved_cost}, indent=2, sort_keys=True))


async def provider_smoke_command(
    config_dir: Path,
    tuple: str,
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
    request = _provider_smoke_request(runtime, recipe, mode, tuple)
    plan = runtime.compiler.compile(request)
    if request.input_refs or request.split_learning is not None:
        worker_request = worker_readable_request(request, runtime)
        plan = plan_with_worker_refs(plan, worker_request.input_refs, split_learning=worker_request.split_learning)
    try:
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
            summary = _provider_smoke_base_summary(runtime, tuple, recipe_name, mode)
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
                **_provider_smoke_base_summary(runtime, tuple, recipe_name, mode),
                "tuple": tuple,
                "recipe": recipe_name,
                "mode": mode.value,
                "job_id": job.job_id,
                "state": current.state.value,
                "completed": current.state is JobState.COMPLETED,
            }
            if current.result is not None:
                summary["result"] = current.result.model_dump(mode="json")
            if current.error:
                summary["error"] = {
                    "message": current.error,
                    "code": "PROVIDER_ERROR" if current.state is JobState.FAILED else current.state.value,
                    "status_code": 502 if current.state is JobState.FAILED else None,
                    "retryable": current.state in {JobState.FAILED, JobState.EXPIRED},
                }
            _finish_provider_smoke_summary(summary, started_at=started_at, config_dir=config_dir, plan=plan, write_artifact=write_artifact)
            return
        result = await runtime.dispatcher.execute_sync(plan)
        summary = _provider_smoke_base_summary(runtime, tuple, recipe_name, mode)
        summary["result"] = result.model_dump(mode="json")
        _finish_provider_smoke_summary(summary, started_at=started_at, config_dir=config_dir, plan=plan, write_artifact=write_artifact)
    except TupleError as exc:
        summary = _provider_smoke_base_summary(runtime, tuple, recipe_name, mode)
        summary["error"] = _provider_smoke_error(exc)
        _finish_provider_smoke_summary(summary, started_at=started_at, config_dir=config_dir, plan=plan, write_artifact=write_artifact)
        raise SystemExit(1) from exc


def _provider_smoke_base_summary(runtime, tuple: str, recipe_name: str, mode: ExecutionMode) -> dict[str, object]:
    spec = runtime.compiler.tuples.get(tuple)
    provider_contract = official_contract(spec)
    return {
        "tuple": tuple,
        "recipe": recipe_name,
        "mode": mode.value,
        "model_ref": getattr(spec, "model_ref", None),
        "engine_ref": getattr(spec, "engine_ref", None),
        "provider_model": getattr(spec, "model", None),
        "gpu": getattr(spec, "gpu", None),
        "tuple_evidence_key": tuple_evidence_key(spec) if spec is not None else None,
        "tuple_evidence_label": tuple_evidence_label(spec) if spec is not None else None,
        "official_contract": provider_contract,
        "official_contract_hash": official_contract_hash(provider_contract),
    }


def _provider_official_contract(spec) -> dict[str, object]:
    return official_contract(spec)


def _provider_smoke_error(exc: TupleError) -> dict[str, object]:
    error: dict[str, object] = {
        "message": str(exc),
        "code": exc.code or "PROVIDER_ERROR",
        "status_code": exc.status_code,
        "retryable": exc.retryable,
    }
    if exc.raw_output is not None:
        error["tuple_error_body_redacted"] = exc.raw_output
        error["tuple_error_body_sha256"] = hashlib.sha256(exc.raw_output.encode("utf-8")).hexdigest()
    return error


def _provider_smoke_request(runtime, recipe, mode: ExecutionMode, tuple: str) -> TaskRequest:
    spec = runtime.compiler.tuples.get(tuple)
    input_contracts = set(getattr(spec, "input_contracts", []) or [])
    inline_inputs = {}
    messages = []
    if "chat_messages" in input_contracts and "text" not in input_contracts:
        messages = [{"role": "user", "content": "gpucall tuple smoke"}]
    else:
        inline_inputs = {"prompt": {"value": "gpucall tuple smoke", "content_type": "text/plain"}}
    input_refs = []
    if recipe.task == "vision":
        if runtime.object_store is None:
            raise SystemExit("tuple-smoke vision requires object_store")
        image_body = _smoke_png()
        digest = hashlib.sha256(image_body).hexdigest()
        presigned = runtime.object_store.presign_put(
            PresignPutRequest(name="tuple-smoke.png", bytes=len(image_body), sha256=digest, content_type="image/png")
        )
        upload = httpx.put(str(presigned.upload_url), content=image_body, headers={"content-type": "image/png"}, timeout=30.0)
        upload.raise_for_status()
        input_refs.append(presigned.data_ref)
    return TaskRequest(
        task=recipe.task,
        mode=mode,
        recipe=recipe.name,
        requested_tuple=tuple,
        bypass_circuit_for_validation=True,
        messages=messages,
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
    summary["observed_wall_seconds"] = (ended_at - started_at).total_seconds()
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
    root = default_state_dir() / "tuple-validation"
    root.mkdir(parents=True, exist_ok=True)
    tuple = str(summary.get("tuple") or "tuple").replace("/", "_")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = root / f"{stamp}-{tuple}.json"
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
    env_commit = os.getenv("GPUCALL_GIT_COMMIT")
    if env_commit:
        return env_commit
    build_commit = PROJECT_ROOT / "BUILD_COMMIT"
    try:
        value = build_commit.read_text(encoding="utf-8").strip()
        if value:
            return value
    except OSError:
        pass
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


def execution_catalog_command(action: str, config_dir: Path, *, recipe: str | None = None, live: bool = False) -> None:
    config = load_config(config_dir)
    evidence = _catalog_live_evidence(config, config_dir, live=live)
    snapshot = build_resource_catalog_snapshot(config, config_dir=config_dir, live_catalog_evidence=evidence)
    if action == "snapshot":
        print(dumps_execution_snapshot(snapshot), end="")
        return
    selected_recipe = None
    if recipe is not None:
        selected_recipe = config.recipes.get(recipe)
        if selected_recipe is None:
            raise SystemExit(f"unknown recipe: {recipe}")
    print(dumps_candidates(generate_tuple_candidates(snapshot, recipe=selected_recipe)), end="")


def validator_plan_command(
    config_dir: Path,
    *,
    budget_usd: float,
    max_items: int | None = None,
    include_candidates: bool = False,
    live: bool = False,
) -> None:
    config = load_config(config_dir)
    evidence = _catalog_live_evidence(config, config_dir, live=live)
    snapshot = build_resource_catalog_snapshot(config, config_dir=config_dir, live_catalog_evidence=evidence)
    plan = build_validator_plan(
        snapshot,
        budget_usd=max(0.0, budget_usd),
        max_items=max_items,
        include_candidates=include_candidates,
    )
    print(dumps_validator_plan(plan), end="")


def _catalog_live_evidence(config, config_dir: Path, *, live: bool) -> dict[str, dict[str, object]] | None:
    cached = load_cached_price_evidence()
    if not live:
        return cached or None
    observed = live_tuple_catalog_evidence(_live_catalog_scope(config, config_dir), load_credentials())
    store_live_price_evidence(observed)
    return merge_price_evidence(observed, cached)


def _live_catalog_scope(config, config_dir: Path) -> dict[str, object]:
    scope: dict[str, object] = dict(config.tuples)
    from gpucall.candidate_sources import load_tuple_candidate_payloads

    for candidate in load_tuple_candidate_payloads(config_dir):
        try:
            tuple_spec = _tuple_from_candidate(candidate, config)
        except Exception:
            continue
        scope[tuple_spec.name] = tuple_spec
    return scope


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
            "inline_inputs": {
                "prompt": {
                    "value": "Reply with exactly: gpucall smoke",
                    "content_type": "text/plain",
                }
            },
            "max_tokens": 16,
            "metadata": {"smoke": "true"},
        }
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
            "selected_tuple": plan.get("selected_tuple") if isinstance(plan, dict) else None,
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
    root = default_state_dir() / "tuple-validation"
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


def _required_live_validation_tuples(config) -> list[dict[str, object]]:
    tuples: dict[str, dict[str, object]] = {}
    allowed = set(config.policy.tuples.allow)
    denied = set(config.policy.tuples.deny)
    for tuple in config.tuples.values():
        if allowed and tuple.name not in allowed:
            continue
        if tuple.name in denied:
            continue
        descriptor = adapter_descriptor(tuple)
        if descriptor is None:
            continue
        if descriptor.local_execution or not descriptor.production_eligible:
            continue
        if not has_configured_endpoint_or_target(tuple.endpoint, tuple.target):
            continue
        if descriptor.execution_surface and descriptor.execution_surface.value == "iaas_vm" and tuple.ssh_remote_cidr is not None and not is_configured_cidr(tuple.ssh_remote_cidr):
            continue
        key = tuple_evidence_key(tuple)
        tuples[key] = {
            "tuple_key": key,
            "label": tuple_evidence_label(tuple),
            "tuple": tuple.name,
            "adapter": tuple.adapter,
        }
    return [tuples[key] for key in sorted(tuples)]


def _live_validation_artifacts_by_tuple(config, config_dir: Path | None = None) -> dict[str, object]:
    root = default_state_dir() / "tuple-validation"
    if not root.exists():
        return {}
    expected_commit = _git_commit()
    expected_config_hash = _config_hash(config_dir) if config_dir is not None else None
    providers_by_name = {tuple.name: tuple for tuple in config.tuples.values()}
    required_keys = {str(item["tuple_key"]) for item in _required_live_validation_tuples(config)}
    artifacts: dict[str, object] = {}
    candidates = sorted(root.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        if expected_commit and data.get("commit") != expected_commit:
            continue
        if _strict_validation_config_hash_enabled() and expected_config_hash and data.get("config_hash") != expected_config_hash:
            continue
        if not _live_validation_artifact_valid(data):
            continue
        tuple = providers_by_name.get(str(data.get("tuple") or ""))
        if tuple is None:
            continue
        contract = data.get("official_contract") if isinstance(data.get("official_contract"), dict) else {}
        tuple_key = artifact_tuple_evidence_key(data, tuple)
        if tuple_key is None:
            continue
        if tuple_key not in required_keys or tuple_key in artifacts:
            continue
        artifacts[tuple_key] = {
            "path": str(path),
            "mtime": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
            "label": tuple_evidence_label(tuple),
            "tuple": tuple.name,
            "data": data,
        }
    return artifacts


def _gateway_smoke_live_tuples(gateway_smoke: dict[str, object] | None, config) -> list[str]:
    if not isinstance(gateway_smoke, dict) or gateway_smoke.get("ok") is not True:
        return []
    providers_by_name = config.tuples
    tuple_keys: set[str] = set()
    sync = gateway_smoke.get("sync")
    if isinstance(sync, dict):
        tuple_name = sync.get("selected_tuple")
        tuple = providers_by_name.get(str(tuple_name or ""))
        if tuple is not None and sync.get("output_non_empty") is True:
            tuple_keys.add(tuple_evidence_key(tuple))
    vision = gateway_smoke.get("vision")
    if isinstance(vision, dict) and vision.get("ok") is True:
        body = vision.get("body")
        plan = body.get("plan") if isinstance(body, dict) else None
        tuple_name = plan.get("selected_tuple") if isinstance(plan, dict) else None
        tuple = providers_by_name.get(str(tuple_name or ""))
        if tuple is not None:
            tuple_keys.add(tuple_evidence_key(tuple))
    return sorted(tuple_keys)


def _capacity_unavailable_validation_tuples(config, config_dir: Path | None = None) -> list[str]:
    root = default_state_dir() / "tuple-validation"
    if not root.exists():
        return []
    expected_commit = _git_commit()
    expected_config_hash = _config_hash(config_dir) if config_dir is not None else None
    providers_by_name = {tuple.name: tuple for tuple in config.tuples.values()}
    tuple_keys: set[str] = set()
    for path in sorted(root.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if expected_commit and data.get("commit") != expected_commit:
            continue
        if _strict_validation_config_hash_enabled() and expected_config_hash and data.get("config_hash") != expected_config_hash:
            continue
        if data.get("validation_schema_version") != 1 or data.get("passed") is not False:
            continue
        error = data.get("error") if isinstance(data.get("error"), dict) else {}
        if error.get("code") != "PROVIDER_PROVISION_UNAVAILABLE" and not (
            error.get("retryable") is True and error.get("status_code") == 503
        ):
            continue
        cleanup = data.get("cleanup") if isinstance(data.get("cleanup"), dict) else {}
        if cleanup.get("required") is True and cleanup.get("completed") is not True:
            continue
        tuple = providers_by_name.get(str(data.get("tuple") or ""))
        contract = data.get("official_contract") if isinstance(data.get("official_contract"), dict) else {}
        if tuple is None or not _official_contract_hash_valid(data, contract):
            continue
        tuple_key = artifact_tuple_evidence_key(data, tuple)
        if tuple_key is not None:
            tuple_keys.add(tuple_key)
    return sorted(tuple_keys)


def _strict_validation_config_hash_enabled() -> bool:
    return os.getenv("GPUCALL_STRICT_VALIDATION_CONFIG_HASH") == "1"


def _live_validation_artifact_valid(data: dict[str, object]) -> bool:
    required = {
        "tuple",
        "recipe",
        "mode",
        "started_at",
        "ended_at",
        "commit",
        "config_hash",
        "governance_hash",
        "official_contract",
        "official_contract_hash",
    }
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
    contract = data.get("official_contract")
    if not isinstance(contract, dict):
        return False
    if not contract.get("adapter"):
        return False
    if not contract.get("endpoint_contract") or contract.get("endpoint_contract") != contract.get("expected_endpoint_contract"):
        return False
    if not contract.get("output_contract") or contract.get("output_contract") != contract.get("expected_output_contract"):
        return False
    expected_stream_contract = contract.get("expected_stream_contract")
    if expected_stream_contract is not None and contract.get("stream_contract") != expected_stream_contract:
        return False
    if not contract.get("official_sources"):
        return False
    if not _official_contract_hash_valid(data, contract):
        return False
    return True


def _official_contract_hash_valid(data: dict[str, object], contract: dict[str, object]) -> bool:
    return data.get("official_contract_hash") == official_contract_hash(contract)


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


def cost_audit_command(config_dir: Path, *, live: bool = False) -> None:
    config = load_config(config_dir)
    creds = load_credentials()
    print(json.dumps(_cost_audit_report(config, creds, config_dir=config_dir, live=live), indent=2, sort_keys=True, default=str))


def tuple_audit_command(config_dir: Path, *, recipe: str | None = None, live: bool = False) -> None:
    config = load_config(config_dir)
    print(json.dumps(tuple_audit_report(config, config_dir=config_dir, recipe_name=recipe, live=live), indent=2, sort_keys=True, default=str))


def _cost_audit_report(config, creds: dict[str, dict[str, str]], *, config_dir: Path, live: bool = False) -> dict[str, object]:
    report: dict[str, object] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "config_dir": str(config_dir),
        "credentials_path": str(credentials_path()),
        "tuples": [_provider_cost_audit_row(tuple) for tuple in sorted(config.tuples.values(), key=lambda item: item.name)],
    }
    if live:
        report["live"] = _live_cost_audit(config.tuples, creds)
    return report


def cleanup_audit_command(config_dir: Path) -> None:
    config = load_config(config_dir)
    print(json.dumps(_cleanup_audit_report(config), indent=2, sort_keys=True, default=str))


def lease_reaper_command(manifest: Path | None, *, apply: bool) -> None:
    path = manifest or Path(os.getenv("GPUCALL_HYPERSTACK_LEASE_MANIFEST", str(default_state_dir() / "hyperstack_leases.jsonl"))).expanduser()
    print(json.dumps(lease_reaper_report(manifest_path=path, apply=apply), indent=2, sort_keys=True, default=str))


def _cleanup_audit_report(config) -> dict[str, object]:
    lease_path = Path(os.getenv("GPUCALL_HYPERSTACK_LEASE_MANIFEST", str(default_state_dir() / "hyperstack_leases.jsonl"))).expanduser()
    active_leases = _active_resource_leases_from_manifest(lease_path)
    validation = _tuple_validation_cleanup_summary(config)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ok": not active_leases and validation["invalid_cleanup_artifacts"] == [],
        "resource_leases": [
            {
                "lifecycle_contract": "iaas_vm_lease",
                "lease_manifest_path": str(lease_path),
                "active_manifest_leases": active_leases,
            }
        ],
        "tuple_validation": validation,
    }


def _active_resource_leases_from_manifest(path: Path) -> list[dict[str, object]]:
    return active_manifest_leases(path)


def _tuple_validation_cleanup_summary(config) -> dict[str, object]:
    root = default_state_dir() / "tuple-validation"
    if not root.exists():
        return {"artifact_count": 0, "invalid_cleanup_artifacts": []}
    invalid: list[dict[str, object]] = []
    count = 0
    for path in sorted(root.glob("*.json")):
        count += 1
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            invalid.append({"path": str(path), "reason": f"invalid json: {type(exc).__name__}"})
            continue
        cleanup = data.get("cleanup")
        if not isinstance(cleanup, dict):
            invalid.append({"path": str(path), "tuple": data.get("tuple"), "reason": "missing cleanup object"})
            continue
        if cleanup.get("required") is True and cleanup.get("completed") is not True:
            invalid.append({"path": str(path), "tuple": data.get("tuple"), "cleanup": cleanup})
    return {"artifact_count": count, "invalid_cleanup_artifacts": invalid}


def _provider_cost_audit_row(tuple) -> dict[str, object]:
    cost_fields = {
        "cost_per_second": float(tuple.cost_per_second),
        "expected_cold_start_seconds": tuple.expected_cold_start_seconds,
        "scaledown_window_seconds": tuple.scaledown_window_seconds,
        "min_billable_seconds": tuple.min_billable_seconds,
        "billing_granularity_seconds": tuple.billing_granularity_seconds,
        "standing_cost_per_second": tuple.standing_cost_per_second,
        "standing_cost_window_seconds": tuple.standing_cost_window_seconds,
        "endpoint_cost_per_second": tuple.endpoint_cost_per_second,
        "endpoint_cost_window_seconds": tuple.endpoint_cost_window_seconds,
    }
    required = ["scaledown_window_seconds", "min_billable_seconds", "billing_granularity_seconds"]
    missing = [key for key in required if cost_fields[key] is None and float(tuple.cost_per_second) > 0]
    return {
        "name": tuple.name,
        "adapter": tuple.adapter,
        "execution_surface": tuple.execution_surface.value if tuple.execution_surface else None,
        "target": tuple.target,
        "gpu": tuple.gpu,
        "cost": cost_fields,
        "metadata_complete": not missing,
        "missing_metadata": missing,
    }


def _live_cost_audit(tuples: dict[str, object], creds: dict[str, dict[str, str]]) -> dict[str, object]:
    return {
        "function_runtime": _function_runtime_live_cost_audit(tuples),
        "managed_endpoint": _managed_endpoint_live_cost_audit(tuples, creds),
        "iaas_vm_lease": _iaas_vm_live_cost_audit(tuples, creds),
    }


def _live_cost_audit_findings(live: object) -> list[dict[str, object]]:
    if not isinstance(live, dict):
        return [{"surface_contract": "tuple-cost", "reason": "missing live cost audit"}]
    findings: list[dict[str, object]] = []
    for surface_contract, section in sorted(live.items()):
        if not isinstance(section, dict) or section.get("configured") is not True:
            continue
        if section.get("ok") is False:
            findings.append({"surface_contract": surface_contract, "reason": section.get("error") or "live cost audit failed"})
        for command_name in ("app_list", "billing_today", "virtual_machines"):
            command_result = section.get(command_name)
            if isinstance(command_result, dict) and command_result.get("ok") is False:
                findings.append(
                    {
                        "surface_contract": surface_contract,
                        "check": command_name,
                        "status_code": command_result.get("status_code"),
                        "returncode": command_result.get("returncode"),
                        "reason": command_result.get("error") or command_result.get("stderr") or "live cost audit failed",
                    }
                )
        endpoints = section.get("endpoints")
        if isinstance(endpoints, list):
            for endpoint in endpoints:
                if not isinstance(endpoint, dict):
                    continue
                health = endpoint.get("health")
                if isinstance(health, dict) and health.get("ok") is False:
                    findings.append(
                        {
                            "surface_contract": surface_contract,
                            "endpoint_id": endpoint.get("endpoint_id"),
                            "check": "endpoint_health",
                            "status_code": health.get("status_code"),
                            "reason": health.get("error") or "endpoint health audit failed",
                        }
                    )
    return findings


def _function_runtime_live_cost_audit(tuples: dict[str, object]) -> dict[str, object]:
    function_providers = [
        tuple for tuple in tuples.values() if str(getattr(getattr(tuple, "execution_surface", None), "value", "")) == "function_runtime"
    ]
    if not function_providers:
        return {"configured": False}
    family_names = sorted({vendor_family_for_adapter(str(getattr(tuple, "adapter", "") or "")) for tuple in function_providers})
    modal = shutil.which("modal") if "modal" in family_names else None
    if "modal" in family_names and modal is None:
        return {"configured": True, "ok": False, "credential_families": family_names, "error": "modal CLI not found"}
    result: dict[str, object] = {"configured": True, "credential_families": family_names}
    if modal is None:
        return result
    result.update(
        {
            "app_list": _run_jsonish_command([modal, "app", "list"], timeout=30),
            "billing_today": _run_jsonish_command(
                [modal, "billing", "report", "--for", "today", "--resolution", "h", "--tz", "Asia/Tokyo", "--json"],
                timeout=60,
            ),
        }
    )
    return {
        **result,
    }


def _managed_endpoint_live_cost_audit(tuples: dict[str, object], creds: dict[str, dict[str, str]]) -> dict[str, object]:
    endpoint_tuples = [
        tuple_spec
        for tuple_spec in tuples.values()
        if str(getattr(getattr(tuple_spec, "execution_surface", None), "value", "")) == "managed_endpoint"
        and is_configured_target(getattr(tuple_spec, "target", None))
    ]
    if not endpoint_tuples:
        return {"configured": False}
    families = sorted({vendor_family_for_adapter(str(getattr(tuple_spec, "adapter", "") or "")) for tuple_spec in endpoint_tuples})
    if families != ["runpod"]:
        return {"configured": True, "ok": False, "credential_families": families, "error": "managed endpoint live cost probe supports RunPod credentials only"}
    api_key = creds.get(families[0], {}).get("api_key")
    if not api_key:
        return {"configured": True, "ok": False, "credential_families": families, "error": "managed endpoint api_key is not configured"}
    rows: list[dict[str, object]] = []
    for tuple_spec in endpoint_tuples:
        base_url = str(getattr(tuple_spec, "endpoint", None) or "https://api.runpod.ai/v2").rstrip("/")
        endpoint_id = str(getattr(tuple_spec, "target"))
        rows.append(
            {
                "tuple": getattr(tuple_spec, "name", ""),
                "endpoint_id": endpoint_id,
                "health": _http_json(
                    f"{base_url}/{endpoint_id}/health",
                    headers={"authorization": f"Bearer {api_key}", "accept": "application/json"},
                ),
            }
        )
    return {"configured": True, "credential_families": families, "endpoints": rows}


def _iaas_vm_live_cost_audit(tuples: dict[str, object], creds: dict[str, dict[str, str]]) -> dict[str, object]:
    vm_tuples = [
        tuple_spec for tuple_spec in tuples.values() if str(getattr(getattr(tuple_spec, "execution_surface", None), "value", "")) == "iaas_vm"
    ]
    if not vm_tuples:
        return {"configured": False}
    families = sorted({vendor_family_for_adapter(str(getattr(tuple_spec, "adapter", "") or "")) for tuple_spec in vm_tuples})
    if families != ["hyperstack"]:
        return {"configured": True, "ok": False, "credential_families": families, "error": "iaas_vm live cost probe supports Hyperstack credentials only"}
    api_key = creds.get(families[0], {}).get("api_key")
    if not api_key:
        return {"configured": True, "ok": False, "credential_families": families, "error": "iaas_vm api_key is not configured"}
    base_url = str(getattr(vm_tuples[0], "endpoint", None) or "https://infrahub-api.nexgencloud.com/v1").rstrip("/")
    return {
        "configured": True,
        "credential_families": families,
        "virtual_machines": _http_json(
            f"{base_url}/core/virtual-machines",
            headers={"api_key": api_key, "accept": "application/json", "content-type": "application/json"},
        ),
    }


def _run_jsonish_command(command: list[str], *, timeout: int) -> dict[str, object]:
    try:
        completed = subprocess.run(command, check=False, capture_output=True, text=True, timeout=timeout)
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    stdout = completed.stdout.strip()
    payload: object = stdout
    if stdout:
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            payload = stdout
    return {
        "ok": completed.returncode == 0,
        "returncode": completed.returncode,
        "stdout": payload,
        "stderr": completed.stderr.strip(),
    }


def _http_json(url: str, *, headers: dict[str, str]) -> dict[str, object]:
    try:
        response = httpx.get(url, headers=headers, timeout=30)
        payload: object
        try:
            payload = response.json()
        except ValueError:
            payload = response.text
        return {"ok": 200 <= response.status_code < 300, "status_code": response.status_code, "body": payload}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


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
        for tuple in config.tuples.values():
            if _provider_route_rejection_reason(config, recipe, tuple) is not None:
                continue
            candidates.append(tuple.name)
        if not candidates:
            findings.append(
                {
                    "recipe": recipe.name,
                    "tuple": "",
                    "reason": "auto-selected recipe has no production tuple satisfying its requirements",
                }
            )
    return findings


def _routing_decision_summary(config) -> dict[str, dict[str, object]]:
    summary: dict[str, dict[str, object]] = {}
    for recipe in config.recipes.values():
        candidates: list[str] = []
        excluded: dict[str, str] = {}
        for tuple in config.tuples.values():
            reason = _provider_route_rejection_reason(config, recipe, tuple)
            if reason is None:
                candidates.append(tuple.name)
            else:
                excluded[tuple.name] = reason
        summary[recipe.name] = {
            "auto_select": recipe.auto_select,
            "task": recipe.task,
            "candidates": sorted(candidates) if recipe.auto_select else [],
            "excluded": dict(sorted(excluded.items())) if recipe.auto_select else {},
        }
    return summary


def _provider_route_rejection_reason(config, recipe, tuple) -> str | None:
    return tuple_route_rejection_reason(
        policy=config.policy,
        recipe=recipe,
        tuple=tuple,
        required_len=recipe_requirements(recipe).context_budget_tokens,
        require_auto_select=True,
    )


def _gateway_api_key() -> str | None:
    configured = load_credentials().get("auth", {}).get("api_keys", "")
    first = configured.split(",", 1)[0].strip()
    return os.getenv("GPUCALL_API_KEY") or first or None


def _secret_presence_summary(creds: dict[str, dict[str, str]]) -> dict[str, bool | list[str]]:
    configured = sorted(set(configured_credentials()))
    auth = creds.get("auth", {})
    return {
        "configured": configured,
        "gateway_auth": bool(auth.get("api_keys") or auth.get("tenant_keys") or os.getenv("GPUCALL_API_KEYS") or os.getenv("GPUCALL_TENANT_API_KEYS")),
        "object_store": bool(creds.get("aws", {}).get("access_key_id") and creds.get("aws", {}).get("secret_access_key")),
    }


def _configured_registry_snapshot(config) -> dict[str, dict[str, object]]:
    snapshot = ObservedRegistry(path=default_state_dir() / "registry.db").snapshot()
    return {name: snapshot[name] for name in sorted(config.tuples) if name in snapshot}


if __name__ == "__main__":
    main()
