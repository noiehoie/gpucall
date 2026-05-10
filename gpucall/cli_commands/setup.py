from __future__ import annotations

import argparse
import getpass
import secrets
import shutil
import sys
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from gpucall.admin_automation import admin_automation_summary, configure_admin_automation
from gpucall.config import ConfigError, default_config_dir, load_admin_automation, load_config, load_object_store
from gpucall.credentials import configured_credentials, credentials_path, load_credentials, save_credentials
from gpucall.domain import ApiKeyHandoffMode
from gpucall.handoff import handoff_payload
from gpucall.release import ONBOARDING_MANUAL_URL, ONBOARDING_PROMPT_URL, SDK_WHEEL_URL


SetupProfile = Literal["local-trial", "internal-team", "production-multitenant", "hardened-regulated"]
CredentialSource = Literal["official_cli", "prompt", "gpucall_credentials"]


class SetupCredentials(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: CredentialSource


class SetupGatewayAuth(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["generated_gateway_key", "existing_gpucall_credentials", "none"] = "none"


class SetupGateway(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str | None = None
    caller_auth: SetupGatewayAuth = Field(default_factory=SetupGatewayAuth)


class SetupProvider(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    credentials: SetupCredentials | None = None
    endpoint_id: str | None = None
    ssh_key_path: str | None = None


class SetupObjectStore(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["cloudflare_r2", "s3"] | None = None
    bucket: str | None = None
    region: str = "auto"
    endpoint_url: str | None = None
    prefix: str = "gpucall"
    credentials: SetupCredentials | None = None


class SetupTenantOnboarding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: ApiKeyHandoffMode = ApiKeyHandoffMode.MANUAL
    allowed_cidrs: tuple[str, ...] = ()
    allowed_hosts: tuple[str, ...] = ()
    recipe_inbox: str | None = None


class SetupRecipeAutomation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    auto_materialize: bool = False
    auto_validate_existing_tuples: bool = False
    auto_activate_existing_validated_recipe: bool = False
    auto_promote_candidates: bool = False
    auto_billable_validation: bool = False
    auto_activate_validated: bool = False
    auto_require_auto_select_safe: bool = True
    auto_set_auto_select: bool = False
    auto_run_validate_config: bool = True
    auto_run_launch_check: bool = False
    promotion_work_dir: str | None = None

    @model_validator(mode="after")
    def validate_recipe_automation_chain(self) -> "SetupRecipeAutomation":
        if self.auto_promote_candidates and not self.auto_materialize:
            raise ValueError("auto_promote_candidates requires auto_materialize")
        if self.auto_validate_existing_tuples and not self.auto_materialize:
            raise ValueError("auto_validate_existing_tuples requires auto_materialize")
        if self.auto_activate_existing_validated_recipe and not self.auto_validate_existing_tuples:
            raise ValueError("auto_activate_existing_validated_recipe requires auto_validate_existing_tuples")
        if self.auto_billable_validation and not (self.auto_promote_candidates or self.auto_validate_existing_tuples):
            raise ValueError("auto_billable_validation requires auto_promote_candidates or auto_validate_existing_tuples")
        if self.auto_activate_validated and not self.auto_billable_validation:
            raise ValueError("auto_activate_validated requires auto_billable_validation")
        if self.auto_set_auto_select and not (self.auto_activate_existing_validated_recipe or self.auto_activate_validated):
            raise ValueError("auto_set_auto_select requires an auto-activation path")
        if self.auto_run_launch_check and not self.auto_run_validate_config:
            raise ValueError("auto_run_launch_check requires auto_run_validate_config")
        return self


class SetupHandoffAssets(BaseModel):
    model_config = ConfigDict(extra="forbid")

    onboarding_prompt_url: str | None = None
    onboarding_manual_url: str | None = None
    caller_sdk_wheel_url: str | None = None


class SetupExternalSystem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    expected_workloads: tuple[str, ...] = ()


class SetupLaunch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_static_check: bool = True
    require_object_store: bool = False
    require_gateway_auth: bool = False


class SetupPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    setup_schema_version: Literal[1]
    profile: SetupProfile
    gateway: SetupGateway = Field(default_factory=SetupGateway)
    providers: dict[str, SetupProvider] = Field(default_factory=dict)
    object_store: SetupObjectStore | None = None
    tenant_onboarding: SetupTenantOnboarding = Field(default_factory=SetupTenantOnboarding)
    recipe_automation: SetupRecipeAutomation = Field(default_factory=SetupRecipeAutomation)
    handoff_assets: SetupHandoffAssets = Field(default_factory=SetupHandoffAssets)
    external_systems: tuple[SetupExternalSystem, ...] = ()
    launch: SetupLaunch = Field(default_factory=SetupLaunch)

    @model_validator(mode="after")
    def validate_plan(self) -> "SetupPlan":
        for name in self.providers:
            if name not in {"modal", "runpod", "hyperstack"}:
                raise ValueError(f"unsupported provider in setup plan: {name}")
        for name, provider in self.providers.items():
            if not provider.enabled:
                continue
            if provider.credentials is None:
                raise ValueError(f"provider {name} requires credentials.source")
            if name == "modal" and provider.credentials.source != "official_cli":
                raise ValueError("modal requires credentials.source: official_cli")
            if name == "runpod" and not provider.endpoint_id:
                raise ValueError("runpod requires endpoint_id")
            if name == "hyperstack" and not provider.ssh_key_path and provider.credentials.source == "prompt":
                raise ValueError("hyperstack prompt setup requires ssh_key_path")
        if self.object_store is not None:
            if not self.object_store.provider:
                raise ValueError("object_store requires provider")
            if not self.object_store.bucket:
                raise ValueError("object_store requires bucket")
            if self.object_store.credentials is None:
                raise ValueError("object_store requires credentials.source")
        if self.tenant_onboarding.mode is ApiKeyHandoffMode.TRUSTED_BOOTSTRAP and not (
            self.tenant_onboarding.allowed_cidrs or self.tenant_onboarding.allowed_hosts
        ):
            raise ValueError("trusted_bootstrap requires at least one allowed CIDR or host")
        return self


def add_setup_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "setup",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="""First-run operator setup journey.

Common commands:
  gpucall setup
  gpucall setup status
  gpucall setup next
  gpucall setup section providers
  gpucall setup apply --file gpucall.setup.yml --dry-run
  gpucall setup apply --file gpucall.setup.yml --yes
  gpucall setup export-handoff-prompt --system-name example-system
""",
    )
    parser.add_argument(
        "action",
        nargs="?",
        metavar="action",
        choices=["status", "next", "section", "apply", "export-handoff-prompt"],
        default=None,
        help="status, next, section, apply, or export-handoff-prompt",
    )
    parser.add_argument(
        "section_name",
        nargs="?",
        metavar="section",
        choices=["profile", "gateway", "providers", "object-store", "tenant-handoff", "recipe-inbox", "external-system", "launch"],
        help="section name for 'gpucall setup section'",
    )
    parser.add_argument("--config-dir", type=Path, default=default_config_dir())
    parser.add_argument("--file", type=Path, default=None)
    parser.add_argument("--profile", choices=["local-trial", "internal-team", "production-multitenant", "hardened-regulated"], default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--system-name", default=None)
    return parser


def run_setup_command(args: argparse.Namespace) -> None:
    action = args.action
    if action is None:
        if sys.stdin.isatty() and sys.stdout.isatty():
            interactive_setup(args.config_dir, profile=args.profile)
        else:
            setup_dashboard(args.config_dir, profile=args.profile)
        return
    if action == "status":
        print(setup_status_text(args.config_dir, profile=args.profile))
        return
    if action == "next":
        print(setup_next_text(args.config_dir, profile=args.profile))
        return
    if action == "section":
        if args.section_name is None:
            raise SystemExit("setup section requires a section name")
        print(setup_section_text(args.config_dir, args.section_name, profile=args.profile))
        return
    if action == "apply":
        if args.file is None:
            raise SystemExit("setup apply requires --file")
        print(apply_setup_plan(args.config_dir, args.file, dry_run=args.dry_run, yes=args.yes))
        return
    if action == "export-handoff-prompt":
        if not args.system_name:
            raise SystemExit("setup export-handoff-prompt requires --system-name")
        print(export_handoff_prompt(args.config_dir, args.system_name))
        return
    raise SystemExit(f"unknown setup action: {action}")


def setup_dashboard(config_dir: Path, *, profile: str | None = None) -> None:
    print(
        f"""gpucall setup

gpucall is a deterministic GPU governance gateway.
It sits between your internal systems and leased GPU execution surfaces.

Before creating config files, choose the operating shape you want.

What are you setting up?

  1. Local trial
     Try gpucall on one machine with a local/smoke runtime.
     No external systems, no tenant onboarding, no production keys.

  2. Internal team gateway
     One gateway for trusted internal systems.
     Tenant-scoped API keys, recipe request inbox, and cloud GPU providers.

  3. Production multi-tenant gateway
     Multiple teams or services, strict budgets, object-store DataRefs,
     audit logs, explicit onboarding, and launch gates.

  4. Hardened / regulated deployment
     Strict auth, external object store, private handoff, audit evidence,
     and no unauthenticated shortcuts.

  0. Explain these options

Current status:
{setup_status_text(config_dir, profile=profile)}
"""
    )


def interactive_setup(config_dir: Path, *, profile: str | None = None) -> None:
    selected_profile = profile or _load_setup_profile(config_dir)
    if selected_profile is None:
        selected_profile = _prompt_profile()
        if selected_profile is None:
            return
        _write_setup_profile(config_dir, selected_profile)
    while True:
        print()
        print(setup_status_text(config_dir, profile=selected_profile))
        try:
            raw = input("\nSelect section, b for back, q to quit: ").strip().lower()
        except EOFError:
            return
        if raw in {"q", "quit", "exit", ""}:
            return
        if raw in {"b", "back"}:
            selected_profile = _prompt_profile() or selected_profile
            _write_setup_profile(config_dir, selected_profile)
            continue
        section = _section_from_choice(raw)
        if section is None:
            print("Unknown section.")
            continue
        print()
        print(setup_section_text(config_dir, section, profile=selected_profile))
        try:
            input("\nPress Enter to return to setup overview.")
        except EOFError:
            return


def setup_status_text(config_dir: Path, *, profile: str | None = None) -> str:
    status = _setup_status(config_dir, profile=profile)
    required = "\n".join(f"  [{item['state']}] {item['label']}" for item in status["required"])
    recommended = "\n".join(f"  [{item['state']}] {item['label']}" for item in status["recommended"])
    return (
        f"Profile: {status['profile']}\n\n"
        f"Required:\n{required}\n\n"
        f"Recommended:\n{recommended}\n\n"
        "Choose section:\n\n"
        "  1. Operating profile\n"
        "  2. Gateway URL and caller auth\n"
        "  3. GPU execution surfaces\n"
        "  4. Object store / DataRef storage\n"
        "  5. Tenant and API key handoff\n"
        "  6. Recipe request inbox\n"
        "  7. Launch checks\n"
        "  8. External-system onboarding prompt\n\n"
        "  b. Back\n"
        "  q. Quit"
    )


def setup_next_text(config_dir: Path, *, profile: str | None = None) -> str:
    status = _setup_status(config_dir, profile=profile)
    for item in status["required"]:
        if item["state"] in {"missing", "partial", "warn"}:
            return f"Next required step: {item['label']}\n\nRun:\n  gpucall setup section {item['section']}"
    return "All required setup checks are satisfied.\n\nRun:\n  gpucall launch-check --profile static"


def setup_section_text(config_dir: Path, section: str, *, profile: str | None = None) -> str:
    status = _setup_status(config_dir, profile=profile)
    if section == "profile":
        return (
            "Operating profile\n\n"
            "  1. local-trial\n"
            "  2. internal-team\n"
            "  3. production-multitenant\n"
            "  4. hardened-regulated\n\n"
            "Use setup plan YAML or --profile to make this persistent."
        )
    if section == "gateway":
        return (
            "Gateway URL and caller auth\n\n"
            f"Current gateway URL: {status['gateway_url'] or '<unset>'}\n"
            f"Gateway auth: {status['gateway_auth_state']}\n\n"
            "For setup-as-code, set gateway.base_url and gateway.caller_auth in gpucall.setup.yml."
        )
    if section == "providers":
        providers = status["providers"]
        return (
            "GPU execution surfaces\n\n"
            "gpucall needs at least one execution surface.\n\n"
            "Configured:\n"
            f"  [{providers['local']}] Local smoke runtime\n"
            f"  [{providers['modal']}] Modal serverless GPU\n"
            f"  [{providers['runpod']}] RunPod managed endpoint\n"
            f"  [{providers['hyperstack']}] Hyperstack VM\n\n"
            "Choose action:\n"
            "  1. Configure Modal\n"
            "  2. Configure RunPod\n"
            "  3. Configure Hyperstack\n"
            "  4. Register controlled runtime / local GPU endpoint\n"
            "  5. Back to setup overview\n"
            "  q. Quit"
        )
    if section == "object-store":
        return (
            "Object store / DataRef storage\n\n"
            "Required for image and file workflows.\n"
            "Provider workers read DataRefs; gateway does not carry payload bytes.\n\n"
            f"Current state: {status['object_store_state']}\n\n"
            "Choose object store:\n"
            "  1. Cloudflare R2\n"
            "  2. S3-compatible\n"
            "  3. Skip for now\n"
            "  b. Back"
        )
    if section == "tenant-handoff":
        automation = admin_automation_summary(config_dir)
        return (
            "Tenant and API key handoff\n\n"
            "Choose how external systems receive gpucall API keys:\n\n"
            "  1. Manual\n"
            "     Administrator creates keys and passes them through an external process.\n\n"
            "  2. Handoff file\n"
            "     gpucall writes one 0600 handoff file per system.\n\n"
            "  3. Trusted bootstrap\n"
            "     Systems inside configured CIDRs/hosts can request their own tenant key.\n\n"
            f"Current mode: {automation['api_key_handoff_mode']}"
        )
    if section == "recipe-inbox":
        automation = admin_automation_summary(config_dir)
        return (
            "Recipe request inbox\n\n"
            "This controls what happens after an external system submits sanitized\n"
            "preflight or quality-feedback intake. It is gateway-side automation,\n"
            "not caller-side routing.\n\n"
            f"Current inbox: {automation['trusted_bootstrap']['recipe_inbox'] or '<unset>'}\n"
            f"Auto materialize recipes: {automation['recipe_inbox_auto_materialize']}\n"
            f"Auto validate existing tuples: {automation['recipe_inbox_auto_validate_existing_tuples']}\n"
            f"Auto activate existing validated recipes: {automation['recipe_inbox_auto_activate_existing_validated_recipe']}\n"
            f"Auto prepare tuple promotion workspace: {automation['recipe_inbox_auto_promote_candidates']}\n"
            f"Auto run billable validation: {automation['recipe_inbox_auto_billable_validation']}\n"
            f"Auto activate validated tuples: {automation['recipe_inbox_auto_activate_validated']}\n"
            f"Auto set recipe auto_select: {automation['recipe_inbox_auto_set_auto_select']}\n"
            f"Promotion work dir: {automation['recipe_inbox_promotion_work_dir'] or '<default inbox/promotions>'}\n\n"
            "For setup-as-code, set recipe_automation in gpucall.setup.yml.\n"
            "For one-shot operation, run:\n"
            "  gpucall-recipe-admin process-inbox --inbox-dir <inbox> --output-dir <config>/recipes --config-dir <config>\n"
        )
    if section == "external-system":
        return (
            "External-system onboarding prompt\n\n"
            "After the gateway is configured, export a system-specific prompt that\n"
            "contains the gateway URL, bootstrap endpoint, recipe inbox, and SDK\n"
            "helper location without embedding any API key.\n\n"
            "Run:\n"
            "  gpucall setup export-handoff-prompt --system-name <external-system>\n"
        )
    if section == "launch":
        return (
            "Launch checks\n\n"
            "Required before production use:\n"
            "  gpucall validate-config\n"
            "  gpucall security scan-secrets\n"
            "  gpucall launch-check --profile static\n"
            "  gpucall launch-check --profile production --url <gateway-url>\n"
        )
    raise SystemExit(f"unknown setup section: {section}")


def apply_setup_plan(config_dir: Path, plan_path: Path, *, dry_run: bool, yes: bool) -> str:
    plan = _load_setup_plan(plan_path)
    changes = _planned_changes(config_dir, plan)
    warnings = _setup_plan_warnings(config_dir, plan)
    report = _setup_apply_report(plan, changes, warnings, dry_run=dry_run)
    if dry_run:
        return report + "\nNo changes written because --dry-run is set."
    prompt_targets = _setup_plan_prompt_targets(plan)
    if yes and prompt_targets:
        raise SystemExit(
            "setup apply --yes cannot use credentials.source: prompt "
            f"({', '.join(prompt_targets)}). Use credentials.source: gpucall_credentials for unattended apply."
        )
    if not yes and not _confirm("Apply setup plan?"):
        return report + "\nAborted."
    _ensure_config_initialized(config_dir)
    _apply_gateway(config_dir, plan)
    _apply_providers(config_dir, plan)
    _apply_object_store(config_dir, plan)
    _apply_tenant_onboarding(config_dir, plan)
    _write_setup_state(config_dir, plan)
    post_checks = _post_apply_checks(config_dir, plan)
    return report + "\nApplied setup plan.\n\n" + post_checks + "\n\n" + setup_status_text(config_dir, profile=plan.profile)


def export_handoff_prompt(config_dir: Path, system_name: str) -> str:
    automation = load_admin_automation(config_dir)
    gateway_url = automation.api_key_bootstrap_gateway_url or "<GPUCALL_BASE_URL>"
    recipe_inbox = automation.api_key_bootstrap_recipe_inbox or "<GPUCALL_RECIPE_INBOX>"
    onboarding_prompt_url = automation.onboarding_prompt_url or ONBOARDING_PROMPT_URL
    onboarding_manual_url = automation.onboarding_manual_url or ONBOARDING_MANUAL_URL
    sdk_wheel_url = automation.caller_sdk_wheel_url or SDK_WHEEL_URL
    return f"""You are adapting this system to gpucall.

System name: {system_name}

Use the gpucall onboarding documents:
  {onboarding_prompt_url}
  {onboarding_manual_url}

Gateway:
  GPUCALL_BASE_URL={gateway_url}
  Bootstrap endpoint={gateway_url}/v2/bootstrap/tenant-key
  Recipe inbox={recipe_inbox}
  SDK helper wheel={sdk_wheel_url}

Rules:
  - Do not clone, install, modify, or vendor the gpucall gateway repository.
  - Work only in this external system's repository.
  - Obtain a tenant key through trusted bootstrap if enabled.
  - Do not ask for provider credentials.
  - Do not choose providers, GPUs, models, recipes, or tuples in application code.
  - Submit preflight intake for unknown workloads before live canary.
  - Final status must be Go or No-Go; skipped canary is No-Go.
"""


def _setup_status(config_dir: Path, *, profile: str | None) -> dict[str, Any]:
    config_exists = (config_dir / "policy.yml").exists()
    selected_profile = profile or _load_setup_profile(config_dir) or "unselected"
    creds = load_credentials()
    configured = set(configured_credentials())
    config = None
    config_error = None
    if config_exists:
        try:
            config = load_config(config_dir)
        except ConfigError as exc:
            config_error = str(exc)
    automation = admin_automation_summary(config_dir)
    gateway_url = automation["trusted_bootstrap"]["gateway_url"]
    gateway_auth = "ok" if ("gateway_auth:api_keys" in configured or "auth" in creds and creds["auth"].get("api_keys")) else "missing"
    if config is None:
        providers = {"local": "missing", "modal": "missing", "runpod": "missing", "hyperstack": "missing"}
    else:
        providers = {
            "local": "ok" if "local-echo" in config.tuples else "missing",
            "modal": "ok" if "sdk_profile:modal" in configured else "missing",
            "runpod": "ok" if "api_key:runpod" in configured else "missing",
            "hyperstack": "ok" if {"api_key:hyperstack", "ssh_key:hyperstack"}.issubset(configured) else "missing",
        }
    cloud_provider_count = sum(1 for name in ("modal", "runpod", "hyperstack") if providers[name] == "ok")
    provider_state = "ok" if cloud_provider_count else ("partial" if providers["local"] == "ok" else "missing")
    object_store = load_object_store(config_dir) if config_exists else None
    object_store_state = "ok" if object_store else "missing"
    handoff_state = "ok" if automation["api_key_handoff_mode"] != "manual" else "missing"
    recipe_inbox_state = "ok" if automation["trusted_bootstrap"]["recipe_inbox"] else "missing"
    launch_state = "ok" if config is not None and config_error is None else "missing"
    required = [
        {"state": "ok" if config_exists and config_error is None else "missing", "label": "config initialized", "section": "profile"},
        {"state": "ok" if gateway_url and gateway_auth == "ok" else "missing", "label": "gateway URL and caller auth", "section": "gateway"},
        {"state": provider_state, "label": _provider_label(providers), "section": "providers"},
        {"state": object_store_state, "label": "object store / DataRef storage", "section": "object-store"},
        {"state": handoff_state, "label": "tenant API key handoff", "section": "tenant-handoff"},
        {"state": recipe_inbox_state, "label": "recipe request inbox", "section": "tenant-handoff"},
        {"state": launch_state, "label": "launch check readiness", "section": "launch"},
    ]
    recommended = [
        {"state": "ok" if automation["recipe_inbox_auto_materialize"] else "warn", "label": "recipe inbox auto-materialize policy reviewed"},
        {"state": "ok" if gateway_url else "warn", "label": "external-system onboarding prompt has concrete gateway URL"},
    ]
    return {
        "profile": selected_profile,
        "required": required,
        "recommended": recommended,
        "providers": providers,
        "object_store_state": object_store_state,
        "gateway_url": gateway_url,
        "gateway_auth_state": gateway_auth,
    }


def _provider_label(providers: dict[str, str]) -> str:
    configured = [name for name in ("Modal", "RunPod", "Hyperstack") if providers[name.lower()] == "ok"]
    if configured:
        return f"GPU execution surfaces: {', '.join(configured)} configured"
    if providers["local"] == "ok":
        return "GPU execution surfaces: local smoke only, cloud provider missing"
    return "GPU execution surfaces"


def _load_setup_profile(config_dir: Path) -> str | None:
    path = config_dir / "setup.yml"
    if not path.exists():
        return None
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if isinstance(payload, dict):
        profile = payload.get("profile")
        if isinstance(profile, str):
            return profile
    return None


def _write_setup_profile(config_dir: Path, profile: str) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "setup.yml"
    payload: dict[str, object] = {}
    if path.exists():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if isinstance(loaded, dict):
            payload = loaded
    payload["setup_schema_version"] = 1
    payload["profile"] = profile
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _prompt_profile() -> str | None:
    print(
        """gpucall setup

gpucall is a deterministic GPU governance gateway.
It sits between your internal systems and leased GPU execution surfaces.

Before creating config files, choose the operating shape you want.

What are you setting up?

  1. Local trial
  2. Internal team gateway
  3. Production multi-tenant gateway
  4. Hardened / regulated deployment
  0. Explain these options
"""
    )
    profiles = {
        "1": "local-trial",
        "local-trial": "local-trial",
        "2": "internal-team",
        "internal-team": "internal-team",
        "3": "production-multitenant",
        "production-multitenant": "production-multitenant",
        "4": "hardened-regulated",
        "hardened-regulated": "hardened-regulated",
    }
    while True:
        try:
            raw = input("Select profile: ").strip().lower()
        except EOFError:
            return None
        if raw in {"q", "quit", "exit", ""}:
            return None
        if raw == "0":
            print("local-trial is for installation validation; internal-team is for trusted internal callers; production profiles require stricter auth, object storage, and launch gates.")
            continue
        profile = profiles.get(raw)
        if profile is not None:
            return profile
        print("Unknown profile.")


def _section_from_choice(raw: str) -> str | None:
    return {
        "1": "profile",
        "profile": "profile",
        "2": "gateway",
        "gateway": "gateway",
        "3": "providers",
        "providers": "providers",
        "4": "object-store",
        "object-store": "object-store",
        "5": "tenant-handoff",
        "tenant-handoff": "tenant-handoff",
        "6": "recipe-inbox",
        "recipe-inbox": "recipe-inbox",
        "recipe-request-inbox": "recipe-inbox",
        "7": "launch",
        "launch": "launch",
        "8": "external-system",
        "external-system": "external-system",
        "external-system-onboarding": "external-system",
    }.get(raw)


def _load_setup_plan(path: Path) -> SetupPlan:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise SystemExit(f"invalid setup plan YAML: {exc}") from exc
    try:
        return SetupPlan.model_validate(payload)
    except ValidationError as exc:
        raise SystemExit(f"invalid setup plan: {_validation_error_summary(exc)}") from exc


def _validation_error_summary(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(part) for part in error.get('loc', ())) or '<root>'}: {error.get('msg')}"
        for error in exc.errors(include_input=False)
    )


def _planned_changes(config_dir: Path, plan: SetupPlan) -> list[str]:
    changes = [str(config_dir / "setup.yml"), str(config_dir / "admin.yml")]
    if not (config_dir / "policy.yml").exists():
        changes.append(f"{config_dir}/*")
    if plan.object_store is not None:
        changes.append(str(config_dir / "object_store.yml"))
    if plan.recipe_automation != SetupRecipeAutomation():
        changes.append(str(config_dir / "admin.yml") + " recipe_automation")
    if plan.handoff_assets != SetupHandoffAssets():
        changes.append(str(config_dir / "admin.yml") + " handoff_assets")
    if plan.providers.get("runpod", SetupProvider()).enabled:
        changes.append(str(config_dir / "surfaces" / "runpod-vllm-serverless.yml"))
    credential_targets: list[str] = []
    if plan.gateway.caller_auth.mode == "generated_gateway_key":
        credential_targets.append("auth")
    for name, provider in sorted(plan.providers.items()):
        if provider.enabled and provider.credentials and provider.credentials.source == "prompt":
            credential_targets.append(name)
    if plan.object_store and plan.object_store.credentials and plan.object_store.credentials.source == "prompt":
        credential_targets.append("aws")
    if credential_targets:
        changes.append(f"credentials: {', '.join(sorted(set(credential_targets)))}")
    return changes


def _setup_plan_warnings(config_dir: Path, plan: SetupPlan) -> list[str]:
    warnings: list[str] = []
    configured = set(configured_credentials())
    for name, provider in sorted(plan.providers.items()):
        if not provider.enabled or provider.credentials is None:
            continue
        if provider.credentials.source == "official_cli":
            cli = "modal" if name == "modal" else "flash"
            if shutil.which(cli) is None:
                warnings.append(f"{name} uses official CLI credentials, but {cli!r} is not on PATH")
        if provider.credentials.source == "gpucall_credentials":
            required = _provider_contracts(name)
            missing = sorted(required.difference(configured))
            if missing:
                warnings.append(f"{name} credentials.source=gpucall_credentials but missing: {', '.join(missing)}")
    if plan.object_store and plan.object_store.credentials and plan.object_store.credentials.source == "gpucall_credentials":
        if "object_store:s3" not in configured:
            warnings.append("object_store credentials.source=gpucall_credentials but object_store:s3 is missing")
    return warnings


def _setup_plan_prompt_targets(plan: SetupPlan) -> list[str]:
    targets: list[str] = []
    for name, provider in sorted(plan.providers.items()):
        if provider.enabled and provider.credentials and provider.credentials.source == "prompt":
            targets.append(f"provider:{name}")
    if plan.object_store and plan.object_store.credentials and plan.object_store.credentials.source == "prompt":
        targets.append("object_store")
    return targets


def _setup_apply_report(plan: SetupPlan, changes: list[str], warnings: list[str], *, dry_run: bool) -> str:
    checks = ["[ok] setup schema valid"]
    if plan.tenant_onboarding.mode is ApiKeyHandoffMode.TRUSTED_BOOTSTRAP:
        checks.append("[ok] trusted bootstrap has allowlist")
    if plan.object_store is not None:
        checks.append("[ok] object store config has bucket")
    checks.extend(f"[warn] {warning}" for warning in warnings)
    return (
        f"Setup plan: {plan.profile}\n\n"
        "Will update:\n"
        + "\n".join(f"  - {item}" for item in changes)
        + "\n\nWill not store:\n"
        "  - raw provider secrets in YAML\n"
        "  - external system API keys in repository files\n\n"
        "Checks:\n"
        + "\n".join(f"  {item}" for item in checks)
    )


def _ensure_config_initialized(config_dir: Path) -> None:
    if (config_dir / "policy.yml").exists():
        return
    source = Path(str(files("gpucall").joinpath("config_templates")))
    if not source.exists():
        raise SystemExit("gpucall config templates are not available")
    config_dir.mkdir(parents=True, exist_ok=True)
    for path in source.rglob("*"):
        if not path.is_file():
            continue
        target = config_dir / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            shutil.copyfile(path, target)


def _apply_gateway(config_dir: Path, plan: SetupPlan) -> None:
    if plan.gateway.caller_auth.mode == "generated_gateway_key":
        token = "gpk_" + secrets.token_urlsafe(32)
        save_credentials("auth", {"api_keys": token})


def _apply_providers(config_dir: Path, plan: SetupPlan) -> None:
    for name, provider in plan.providers.items():
        if not provider.enabled or provider.credentials is None:
            continue
        if provider.credentials.source == "prompt":
            if name == "runpod":
                save_credentials("runpod", {"api_key": getpass.getpass("RunPod API key: ").strip()})
            if name == "hyperstack":
                save_credentials("hyperstack", {"api_key": getpass.getpass("Hyperstack API key: ").strip(), "ssh_key_path": provider.ssh_key_path or ""})
        if name == "runpod" and provider.endpoint_id:
            _update_yaml(config_dir / "surfaces" / "runpod-vllm-serverless.yml", {"target": provider.endpoint_id})


def _apply_object_store(config_dir: Path, plan: SetupPlan) -> None:
    object_store = plan.object_store
    if object_store is None:
        return
    if object_store.credentials and object_store.credentials.source == "prompt":
        access_key = getpass.getpass("Object store access key: ").strip()
        secret_key = getpass.getpass("Object store secret key: ").strip()
        save_credentials("aws", {"access_key_id": access_key, "secret_access_key": secret_key, "region": object_store.region, "endpoint_url": object_store.endpoint_url or ""})
    payload = {
        "provider": "s3",
        "bucket": object_store.bucket,
        "region": object_store.region,
        "endpoint": object_store.endpoint_url,
        "prefix": object_store.prefix,
        "presign_ttl_seconds": 900,
    }
    path = config_dir / "object_store.yml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _apply_tenant_onboarding(config_dir: Path, plan: SetupPlan) -> None:
    configure_admin_automation(
        config_dir,
        handoff_mode=plan.tenant_onboarding.mode,
        bootstrap_allowed_cidrs=plan.tenant_onboarding.allowed_cidrs,
        bootstrap_allowed_hosts=plan.tenant_onboarding.allowed_hosts,
        bootstrap_gateway_url=plan.gateway.base_url,
        bootstrap_recipe_inbox=plan.tenant_onboarding.recipe_inbox,
        recipe_inbox_auto_materialize=plan.recipe_automation.auto_materialize,
        recipe_inbox_auto_validate_existing_tuples=plan.recipe_automation.auto_validate_existing_tuples,
        recipe_inbox_auto_activate_existing_validated_recipe=plan.recipe_automation.auto_activate_existing_validated_recipe,
        recipe_inbox_auto_promote_candidates=plan.recipe_automation.auto_promote_candidates,
        recipe_inbox_auto_billable_validation=plan.recipe_automation.auto_billable_validation,
        recipe_inbox_auto_activate_validated=plan.recipe_automation.auto_activate_validated,
        recipe_inbox_auto_require_auto_select_safe=plan.recipe_automation.auto_require_auto_select_safe,
        recipe_inbox_auto_set_auto_select=plan.recipe_automation.auto_set_auto_select,
        recipe_inbox_auto_run_validate_config=plan.recipe_automation.auto_run_validate_config,
        recipe_inbox_auto_run_launch_check=plan.recipe_automation.auto_run_launch_check,
        recipe_inbox_promotion_work_dir=plan.recipe_automation.promotion_work_dir,
        onboarding_prompt_url=plan.handoff_assets.onboarding_prompt_url,
        onboarding_manual_url=plan.handoff_assets.onboarding_manual_url,
        caller_sdk_wheel_url=plan.handoff_assets.caller_sdk_wheel_url,
        clear_bootstrap_allowlist=plan.tenant_onboarding.mode is not ApiKeyHandoffMode.TRUSTED_BOOTSTRAP,
    )


def _write_setup_state(config_dir: Path, plan: SetupPlan) -> None:
    payload = {
        "setup_schema_version": plan.setup_schema_version,
        "profile": plan.profile,
        "gateway_base_url": plan.gateway.base_url,
        "recipe_automation": plan.recipe_automation.model_dump(mode="json"),
        "handoff_assets": plan.handoff_assets.model_dump(mode="json"),
        "external_systems": [system.model_dump(mode="json") for system in plan.external_systems],
        "launch": plan.launch.model_dump(mode="json"),
    }
    (config_dir / "setup.yml").write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _post_apply_checks(config_dir: Path, plan: SetupPlan) -> str:
    checks: list[tuple[str, bool, str]] = []
    try:
        config = load_config(config_dir)
        checks.append(("validate-config", True, f"{len(config.recipes)} recipes, {len(config.tuples)} tuples"))
    except Exception as exc:
        checks.append(("validate-config", False, f"{type(exc).__name__}: {exc}"))
    secret_findings = _scan_secret_like_yaml(config_dir)
    checks.append(("security scan-secrets", not secret_findings, f"{len(secret_findings)} findings"))
    if plan.launch.run_static_check:
        checks.append(
            (
                "launch-check static",
                checks[0][1] and checks[1][1],
                "run `gpucall launch-check --profile static` for the full report",
            )
        )
    lines = ["Post-apply checks:"]
    for name, ok, detail in checks:
        state = "ok" if ok else "fail"
        lines.append(f"  [{state}] {name}: {detail}")
    if any(not ok for _, ok, _ in checks):
        lines.append("  Next: fix failed checks before starting the gateway.")
    return "\n".join(lines)


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


def _update_yaml(path: Path, updates: dict[str, object]) -> None:
    if not path.exists():
        return
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        return
    payload.update(updates)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _provider_contracts(name: str) -> set[str]:
    if name == "runpod":
        return {"api_key:runpod"}
    if name == "hyperstack":
        return {"api_key:hyperstack", "ssh_key:hyperstack"}
    if name == "modal":
        return {"sdk_profile:modal"}
    return set()


def _confirm(message: str) -> bool:
    raw = input(message + " [y/N]: ").strip().lower()
    return raw in {"y", "yes"}
