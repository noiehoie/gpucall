from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import plistlib
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal
from urllib.error import URLError
from urllib.request import urlopen

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from gpucall.admin_automation import (
    ADMIN_SYNTHETIC_DRY_RUN_TTL_SECONDS,
    admin_automation_summary,
    configure_admin_automation,
    run_admin_automation_synthetic_dry_run,
)
from gpucall.caller_auth_registry import caller_auth_status_summary, record_caller_auth
from gpucall.config import ConfigError, default_config_dir, default_state_dir, load_admin_automation, load_config, load_object_store
from gpucall.credentials import configured_credentials, credentials_path, load_credentials, save_credentials
from gpucall.domain import ApiKeyHandoffMode
from gpucall.handoff import _default_quality_feedback_inbox
from gpucall.handoff_package import caller_ai_onboarding_prompt, build_handoff_contract, write_handoff_package
from gpucall.panopticon import PANOPTICON_TTL_BY_DIMENSION, PANOPTICON_SCHEMA_VERSION, default_panopticon_path
from gpucall.panopticon_service import refresh_panopticon
from gpucall.provider_registry import (
    load_provider_registry,
    provider_registry_configured_contracts,
    provider_registry_path,
    provider_registry_snapshot_hash,
    save_provider_metadata,
)
from gpucall.provider_contracts import CLOUD_PROVIDER_FAMILIES, PROVIDER_SETUP_CONTRACTS
from gpucall.release import GITHUB_RELEASE_TAG, ONBOARDING_MANUAL_URL, ONBOARDING_PROMPT_URL, SDK_WHEEL_URL


SetupProfile = Literal["local-trial", "internal-team", "production-multitenant", "hardened-regulated"]
CredentialSource = Literal["official_cli", "prompt", "gpucall_credentials"]
PROVIDER_MUTATION_CONSENT_TTL_SECONDS = 3600


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
    deploy_worker: bool = False


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
    auto_provision_supply: bool = False
    auto_apply_supply: bool = False
    auto_billable_validation: bool = False
    auto_validation_budget_usd: float = 0.10
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
        if self.auto_provision_supply and not self.auto_promote_candidates:
            raise ValueError("auto_provision_supply requires auto_promote_candidates")
        if self.auto_apply_supply and not self.auto_provision_supply:
            raise ValueError("auto_apply_supply requires auto_provision_supply")
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
            if name not in PROVIDER_SETUP_CONTRACTS:
                raise ValueError(f"unsupported provider in setup plan: {name}")
        for name, provider in self.providers.items():
            if not provider.enabled:
                continue
            if provider.credentials is None:
                raise ValueError(f"provider {name} requires credentials.source")
            contract = PROVIDER_SETUP_CONTRACTS[name]
            if contract.prompt_requires_ssh_key and not provider.ssh_key_path and provider.credentials.source == "prompt":
                raise ValueError("hyperstack prompt setup requires ssh_key_path")
            if provider.deploy_worker and name != "modal":
                raise ValueError("deploy_worker is supported only for modal")
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
  gpucall setup starter-plan --profile local-trial
  gpucall setup starter-plan --profile internal-team --provider modal --output gpucall.modal.setup.yml
  gpucall setup section providers
  gpucall setup apply --file gpucall.setup.yml --dry-run
  gpucall setup apply --file gpucall.setup.yml --yes
  gpucall setup export-handoff-prompt --system-name example-system
  gpucall setup export-handoff-package --system-name example-system --output-dir handoff/example-system
""",
    )
    parser.add_argument(
        "action",
        nargs="?",
        metavar="action",
        choices=["status", "next", "section", "starter-plan", "apply", "export-handoff-prompt", "export-handoff-package"],
        default=None,
        help="status, next, section, starter-plan, apply, export-handoff-prompt, or export-handoff-package",
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
    parser.add_argument("--provider", choices=["none", "modal", "runpod", "hyperstack"], default=None)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument(
        "--accept-plan-hash",
        default=None,
        help="accept the exact provider-mutation plan hash printed by setup apply --dry-run",
    )
    parser.add_argument("--system-name", default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument(
        "--allow-placeholders",
        action="store_true",
        help="allow template placeholders in export-handoff-prompt output; default requires concrete handoff values",
    )
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
    if action == "starter-plan":
        print(write_starter_plan(args.output, profile=args.profile, provider=args.provider))
        return
    if action == "apply":
        if args.file is None:
            raise SystemExit("setup apply requires --file")
        print(apply_setup_plan(args.config_dir, args.file, dry_run=args.dry_run, yes=args.yes, accept_plan_hash=args.accept_plan_hash))
        return
    if action == "export-handoff-prompt":
        if not args.system_name:
            raise SystemExit("setup export-handoff-prompt requires --system-name")
        print(export_handoff_prompt(args.config_dir, args.system_name, require_concrete=not args.allow_placeholders))
        return
    if action == "export-handoff-package":
        if not args.system_name:
            raise SystemExit("setup export-handoff-package requires --system-name")
        if args.output_dir is None:
            raise SystemExit("setup export-handoff-package requires --output-dir")
        print(yaml.safe_dump(export_handoff_package(args.config_dir, args.system_name, args.output_dir), sort_keys=False))
        return
    raise SystemExit(f"unknown setup action: {action}")


def setup_dashboard(config_dir: Path, *, profile: str | None = None) -> None:
    print(
        f"""gpucall setup

gpucall is a deterministic GPU governance gateway.
It sits between your internal systems and leased GPU execution surfaces.

Start with one path. If you are unsure, choose local-trial first; it verifies
the install without provider credentials or billable generation.

Fast path:

  1. Create a starter plan:
     gpucall setup starter-plan --profile local-trial

  2. Review what it will change:
     gpucall setup apply --file gpucall.setup.yml --dry-run

  3. Apply it:
     gpucall setup apply --file gpucall.setup.yml --yes

Cloud path after local trial:

  gpucall setup starter-plan --profile internal-team --provider modal --output gpucall.modal.setup.yml
  gpucall setup apply --file gpucall.modal.setup.yml --dry-run
  gpucall setup apply --file gpucall.modal.setup.yml

Profiles:

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
        print(setup_status_text(config_dir, profile=selected_profile, include_menu=True))
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


def setup_status_text(config_dir: Path, *, profile: str | None = None, include_menu: bool = False) -> str:
    status = _setup_status(config_dir, profile=profile)
    required = "\n".join(f"  [{item['state']}] {item['label']}" for item in status["required"])
    recommended = "\n".join(f"  [{item['state']}] {item['label']}" for item in status["recommended"])
    control_plane = status["control_plane"]
    caller_auth = status["caller_auth"]
    caller_auth_line = _caller_auth_status_line(caller_auth)
    text = (
        f"Profile: {status['profile']}\n\n"
        f"OOB readiness: {status['oob_readiness']}\n\n"
        f"Required:\n{required}\n\n"
        f"Recommended:\n{recommended}\n\n"
        "Control-plane:\n"
        f"  Panopticon: {control_plane['panopticon_service_state']} ({control_plane['panopticon_service_mode']})\n"
        f"  Panopticon bootstrap: {control_plane['panopticon_bootstrap_state']}\n"
        f"  Panopticon evidence: {control_plane['panopticon_evidence_state']} ({control_plane['panopticon_path']})\n"
        f"  Admin automation: {control_plane['admin_automation_service_state']} ({control_plane['admin_automation_service_mode']})\n"
        f"  Admin synthetic dry-run: {control_plane['admin_synthetic_state']}\n"
        f"  Caller auth lifecycle: {caller_auth_line}\n"
        f"  TTL defaults: hot={PANOPTICON_TTL_BY_DIMENSION['health']}s price={PANOPTICON_TTL_BY_DIMENSION['price']}s "
        f"contract={PANOPTICON_TTL_BY_DIMENSION['contract']}s validation=604800s"
    )
    if not include_menu:
        return (
            text
            + "\n\nNext command:\n  gpucall setup next\n\n"
            "If you have not created a setup plan yet:\n"
            "  gpucall setup starter-plan --profile local-trial\n"
            "  gpucall setup apply --file gpucall.setup.yml --dry-run\n"
            "  gpucall setup apply --file gpucall.setup.yml --yes"
        )
    return (
        text
        + "\n\n"
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
    if status["profile"] == "unselected" and status["required"] and status["required"][0]["state"] == "missing":
        return (
            "Next required step: choose a starter plan\n\n"
            "Run:\n"
            "  gpucall setup starter-plan --profile local-trial\n"
            "  gpucall setup apply --file gpucall.setup.yml --dry-run\n"
            "  gpucall setup apply --file gpucall.setup.yml --yes\n\n"
            "After local trial, switch to the Modal happy-path cloud plan:\n"
            "  gpucall setup starter-plan --profile internal-team --provider modal --output gpucall.modal.setup.yml"
        )
    for item in status["required"]:
        if item["state"] in {"missing", "partial", "warn", "service-error", "service-uninitialized"}:
            return f"Next required step: {item['label']}\n\nRun:\n  gpucall setup section {item['section']}"
    if status["profile"] == "local-trial":
        blockers = [item for item in status["recommended"] if item["state"] in {"missing", "partial", "warn"}]
        if blockers:
            return _local_trial_cloud_next_text()
    if status["profile"] == "internal-team":
        if status["oob_readiness"] == "onboarding-ready-provisional":
            return (
                "Next required step: verify runtime services and caller reachability\n\n"
                "Current state: onboarding-ready-provisional.\n\n"
                "Run:\n"
                "  gpucall setup status\n"
                "  gpucall panopticon snapshot\n"
                "  gpucall setup export-handoff-package --system-name <external-system> --output-dir \"$XDG_DATA_HOME/gpucall/handoffs/<external-system>\"\n\n"
                "Production routing still requires fresh Panopticon evidence and accepted route validation evidence."
            )
        return (
            "Now you are good to go for an internal gateway setup.\n\n"
            "Next, generate a caller handoff package for each external system:\n"
            "  gpucall setup export-handoff-package --system-name <external-system> --output-dir \"$XDG_DATA_HOME/gpucall/handoffs/<external-system>\"\n\n"
            "Then start or restart the gateway and hand the generated caller-ai-onboarding-prompt.md to the caller-side AI CLI."
        )
    return "All required setup checks are satisfied.\n\nRun:\n  gpucall launch-check --profile static"


def setup_section_text(config_dir: Path, section: str, *, profile: str | None = None) -> str:
    status = _setup_status(config_dir, profile=profile)
    if section == "profile":
        return (
            "Operating profile\n\n"
            "Start here if you are unsure:\n"
            "  gpucall setup starter-plan --profile local-trial\n"
            "  gpucall setup apply --file gpucall.setup.yml --dry-run\n"
            "  gpucall setup apply --file gpucall.setup.yml --yes\n\n"
            "Profiles:\n"
            "  1. local-trial             no provider credentials, no external callers\n"
            "  2. internal-team           one trusted internal gateway\n"
            "  3. production-multitenant  strict budgets, DataRefs, launch gates\n"
            "  4. hardened-regulated      strict auth and audit evidence\n\n"
            "For cloud setup after the trial:\n"
            "  gpucall setup starter-plan --profile internal-team --provider modal --output gpucall.modal.setup.yml"
        )
    if section == "gateway":
        return (
            "Gateway URL and caller auth\n\n"
            f"Current gateway URL: {status['gateway_url'] or '<unset>'}\n"
            f"Gateway auth: {status['gateway_auth_state']}\n\n"
            "For local trial, you can skip this until an external caller needs to connect.\n"
            "For a real gateway, set gateway.base_url and gateway.caller_auth in gpucall.setup.yml.\n\n"
            "Beginner path:\n"
            "  gpucall setup starter-plan --profile internal-team --provider modal --output gpucall.modal.setup.yml"
        )
    if section == "providers":
        providers = status["providers"]
        provider_labels = status["provider_labels"]
        provider_lines = "\n".join(
            f"  [{providers[name]}] {provider_labels[name]}"
            for name in PROVIDER_SETUP_CONTRACTS
        )
        return (
            "GPU execution surfaces\n\n"
            "A local trial uses the bundled local smoke runtime.\n"
            "A real gateway also needs one cloud provider account.\n\n"
            "Configured:\n"
            f"  [{providers['local']}] Local smoke runtime\n"
            f"{provider_lines}\n\n"
            "Fast choices:\n"
            "  Local trial: gpucall setup starter-plan --profile local-trial\n"
            "  Modal happy path: gpucall setup starter-plan --profile internal-team --provider modal --output gpucall.modal.setup.yml\n"
            "  RunPod advanced:  gpucall setup starter-plan --profile internal-team --provider runpod --output gpucall.runpod.setup.yml\n"
            "  Hyperstack:  gpucall setup starter-plan --profile internal-team --provider hyperstack --output gpucall.hyperstack.setup.yml\n\n"
            "If you do not yet have any cloud GPU provider account, create a Modal account and token first.\n"
            "Without provider credentials, gpucall will keep cloud routing fail-closed.\n\n"
            "Advanced: Register controlled runtime / local GPU endpoint with gpucall runtime add-openai or add-ollama."
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
            f"Auto plan provider supply: {automation['recipe_inbox_auto_provision_supply']}\n"
            f"Auto apply provider supply: {automation['recipe_inbox_auto_apply_supply']}\n"
            f"Auto run billable validation: {automation['recipe_inbox_auto_billable_validation']}\n"
            f"Auto validation budget USD: {automation['recipe_inbox_auto_validation_budget_usd']}\n"
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


def write_starter_plan(output: Path | None, *, profile: str | None, provider: str | None) -> str:
    selected_profile = profile or "local-trial"
    selected_provider = provider or ("none" if selected_profile == "local-trial" else "modal")
    if selected_profile == "local-trial" and selected_provider != "none":
        raise SystemExit("local-trial starter plan does not use a cloud provider; omit --provider or use --provider none")
    if selected_profile != "local-trial" and selected_provider == "none":
        raise SystemExit(f"{selected_profile} starter plan requires --provider modal, runpod, or hyperstack")
    path = output or Path("gpucall.setup.yml")
    if path.exists():
        raise SystemExit(f"{path} already exists; pass --output with a new path or remove the existing file")
    path.parent.mkdir(parents=True, exist_ok=True)
    text = _starter_plan_text(selected_profile, selected_provider)
    path.write_text(text, encoding="utf-8")
    next_lines = [f"Wrote starter setup plan: {path}"]
    if selected_provider == "modal":
        next_lines.extend(
            [
                "",
                "Before apply:",
                "  - keep your Modal token ID and token secret ready",
                "  - set MODAL_TOKEN_ID and MODAL_TOKEN_SECRET for non-interactive apply",
                "  - run the dry-run first and copy the printed --accept-plan-hash value",
                "  - edit gateway.base_url if callers will connect from another machine",
                "  - optionally add external_systems to generate caller handoff packages automatically",
            ]
        )
    next_lines.extend(
        [
            "",
            "Next:",
            f"  gpucall setup apply --file {path} --dry-run",
            f"  gpucall setup apply --file {path}" + (" --yes" if selected_profile == "local-trial" else ""),
        ]
    )
    return "\n".join(next_lines)


def _starter_plan_text(profile: str, provider: str) -> str:
    if profile == "local-trial":
        return """# gpucall starter setup plan
# Use this first if you are new. It does not need provider credentials,
# endpoint IDs, object storage, tenant handoff, or billable generation.
setup_schema_version: 1
profile: local-trial
launch:
  run_static_check: true
"""
    recipe_inbox = default_state_dir() / "recipe_requests" / "inbox"
    lines = [
        "# gpucall starter setup plan",
        "# Edit gateway.base_url before exposing the gateway to another machine.",
        "# Provider credentials are prompted at apply time and are stored outside this YAML.",
        "setup_schema_version: 1",
        f"profile: {profile}",
        "gateway:",
        "  base_url: http://127.0.0.1:18088",
        "  caller_auth:",
        "    mode: generated_gateway_key",
        "providers:",
    ]
    if provider == "runpod":
        lines.extend(
            [
                "  runpod:",
                "    enabled: true",
                "    credentials:",
                "      source: prompt",
                "    # endpoint_id is optional on first install.",
                "    # gpucall will show endpoint provisioning pending until supply is created.",
            ]
        )
    elif provider == "modal":
        lines.extend(
            [
                "  modal:",
                "    enabled: true",
                "    credentials:",
                "      source: prompt",
                "    # Deploy the bundled gpucall Modal worker during setup apply.",
                "    deploy_worker: true",
            ]
        )
    elif provider == "hyperstack":
        lines.extend(
            [
                "  hyperstack:",
                "    enabled: true",
                "    credentials:",
                "      source: prompt",
                "    ssh_key_path: ~/.ssh/gpucall_hyperstack_ed25519",
            ]
        )
    else:
        raise SystemExit(f"unsupported starter provider: {provider}")
    if provider == "modal":
        automation_lines = [
            "recipe_automation:",
            "  auto_materialize: true",
            "  auto_validate_existing_tuples: true",
            "  auto_activate_existing_validated_recipe: true",
            "  auto_promote_candidates: true",
            "  auto_provision_supply: true",
            "  auto_apply_supply: false",
            "  # Billable validation is intentionally opt-in after setup.",
            "  auto_billable_validation: false",
            "  auto_validation_budget_usd: 0.10",
            "  auto_activate_validated: false",
            "  auto_require_auto_select_safe: false",
            "  auto_set_auto_select: true",
            "  auto_run_validate_config: true",
            "  auto_run_launch_check: true",
        ]
    else:
        automation_lines = [
            "recipe_automation:",
            "  auto_materialize: true",
            "  auto_promote_candidates: true",
            "  auto_provision_supply: true",
            "  auto_apply_supply: false",
        ]
    lines.extend(
        [
            "tenant_onboarding:",
            "  mode: trusted_bootstrap",
            "  allowed_hosts:",
            "    - localhost",
            f"  recipe_inbox: {recipe_inbox}",
            *automation_lines,
            "launch:",
            "  run_static_check: true",
            "# Optional: uncomment to generate caller handoff packages during setup apply.",
            "# external_systems:",
            "#   - name: example-system",
            "#     expected_workloads: [infer]",
            "",
        ]
    )
    return "\n".join(lines)


def apply_setup_plan(config_dir: Path, plan_path: Path, *, dry_run: bool, yes: bool, accept_plan_hash: str | None = None) -> str:
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
    _assert_provider_mutation_consent(plan, yes=yes, accept_plan_hash=accept_plan_hash)
    if not yes and not _confirm("Apply setup plan?"):
        return report + "\nAborted."
    _ensure_config_initialized(config_dir)
    _ensure_oob_control_plane_initialized(config_dir, plan)
    consent_artifact = _write_modal_deploy_consent_artifact(plan)
    _apply_gateway(config_dir, plan)
    provider_results = _apply_providers(config_dir, plan, modal_plan_hash=str(consent_artifact.get("plan_hash")) if consent_artifact else None)
    if consent_artifact is not None:
        _write_modal_cleanup_manifest(consent_artifact)
    _apply_object_store(config_dir, plan)
    _apply_tenant_onboarding(config_dir, plan)
    _maybe_create_local_inboxes(plan.tenant_onboarding.recipe_inbox)
    _write_setup_state(config_dir, plan)
    synthetic_result = run_admin_automation_synthetic_dry_run(plan.tenant_onboarding.recipe_inbox, config_dir=config_dir)
    panopticon_bootstrap = _run_panopticon_bootstrap_refresh(config_dir, plan)
    panopticon_service = _start_panopticon_background_service(config_dir, plan)
    admin_service = _start_admin_automation_service(config_dir, plan)
    _update_oob_control_plane_state(
        config_dir,
        plan,
        synthetic_result=synthetic_result,
        panopticon_bootstrap=panopticon_bootstrap,
        panopticon_service=panopticon_service,
        admin_service=admin_service,
    )
    handoff_results = _write_external_system_handoff_packages(config_dir, plan)
    post_checks = _post_apply_checks(config_dir, plan)
    provider_text = ("\n\nProvider setup actions:\n" + "\n".join(f"  {item}" for item in provider_results)) if provider_results else ""
    panopticon_text = _panopticon_bootstrap_report_text(panopticon_bootstrap)
    synthetic_text = _synthetic_dry_run_report_text(synthetic_result)
    admin_service_text = _admin_automation_service_report_text(admin_service)
    handoff_text = ("\n\nCaller handoff packages:\n" + "\n".join(f"  {item}" for item in handoff_results)) if handoff_results else ""
    completion_text = _setup_completion_text(config_dir, plan, handoff_results=handoff_results)
    return (
        report
        + "\nApplied setup plan."
        + provider_text
        + panopticon_text
        + synthetic_text
        + admin_service_text
        + handoff_text
        + "\n\n"
        + post_checks
        + "\n\n"
        + setup_status_text(config_dir, profile=plan.profile)
        + "\n\n"
        + completion_text
    )


def export_handoff_prompt(config_dir: Path, system_name: str, *, require_concrete: bool = True) -> str:
    try:
        contract = build_handoff_contract(config_dir, system_name, require_concrete=require_concrete)
    except ValueError as exc:
        raise SystemExit(
            str(exc)
            + "\nRun `gpucall setup starter-plan --profile internal-team --provider modal --output gpucall.modal.setup.yml`, "
            "`gpucall setup apply --file gpucall.modal.setup.yml --dry-run`, then apply a concrete gateway, trusted-bootstrap, and recipe-inbox setup. "
            "Use --allow-placeholders only when you intentionally want a template."
        ) from exc
    return caller_ai_onboarding_prompt(contract)


def export_handoff_package(config_dir: Path, system_name: str, output_dir: Path) -> dict[str, Any]:
    return write_handoff_package(config_dir, system_name, output_dir)


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
    configured.update(provider_registry_configured_contracts())
    gateway_auth = "ok" if ("gateway_auth:api_keys" in configured or "auth" in creds and creds["auth"].get("api_keys")) else "missing"
    if config is None:
        providers = {"local": "missing", **{name: "missing" for name in CLOUD_PROVIDER_FAMILIES}}
    else:
        providers = {
            "local": "ok" if "local-echo" in config.tuples else "missing",
            **{
                name: "ok" if contract.configured_by(configured) else "missing"
                for name, contract in PROVIDER_SETUP_CONTRACTS.items()
            },
        }
    providers, provider_labels = _provider_display_state(config, providers)
    cloud_provider_count = sum(1 for name in CLOUD_PROVIDER_FAMILIES if providers[name] == "ok")
    cloud_pending_count = sum(1 for name in CLOUD_PROVIDER_FAMILIES if providers[name] == "partial")
    provider_state = "ok" if cloud_provider_count else ("partial" if cloud_pending_count or providers["local"] == "ok" else "missing")
    object_store = load_object_store(config_dir) if config_exists else None
    object_store_state = "ok" if object_store else "missing"
    handoff_state = "ok" if automation["api_key_handoff_mode"] != "manual" else "missing"
    recipe_inbox_state = "ok" if automation["trusted_bootstrap"]["recipe_inbox"] else "missing"
    synthetic = automation["synthetic_dry_run"]
    admin_synthetic_state = "ok" if synthetic.get("fresh") is True and synthetic.get("status") == "processed" else "missing"
    launch_state = "ok" if config is not None and config_error is None else "missing"
    control_plane = _control_plane_status(config_dir, profile=selected_profile, admin_synthetic_state=admin_synthetic_state)
    caller_auth = caller_auth_status_summary()
    config_item = {"state": "ok" if config_exists and config_error is None else "missing", "label": "config initialized", "section": "profile"}
    gateway_item = {"state": "ok" if gateway_url and gateway_auth == "ok" else "missing", "label": "gateway URL and caller auth", "section": "gateway"}
    provider_item = {"state": provider_state, "label": _provider_label(providers, provider_labels), "section": "providers"}
    local_provider_item = {
        "state": "ok" if providers["local"] == "ok" else "missing",
        "label": "GPU execution surfaces: local smoke runtime",
        "section": "providers",
    }
    object_store_item = {"state": object_store_state, "label": "object store / DataRef storage", "section": "object-store"}
    handoff_item = {"state": handoff_state, "label": "tenant API key handoff", "section": "tenant-handoff"}
    recipe_inbox_item = {"state": recipe_inbox_state, "label": "recipe request inbox", "section": "recipe-inbox"}
    synthetic_item = {
        "state": admin_synthetic_state,
        "label": "admin automation synthetic intake dry-run",
        "section": "recipe-inbox",
    }
    panopticon_service_item = {
        "state": control_plane["panopticon_service_state"],
        "label": "Panopticon service lifecycle",
        "section": "launch",
    }
    admin_service_item = {
        "state": control_plane["admin_automation_service_state"],
        "label": "admin automation service lifecycle",
        "section": "recipe-inbox",
    }
    launch_item = {"state": launch_state, "label": "launch check readiness", "section": "launch"}
    if selected_profile == "local-trial":
        required = [config_item, local_provider_item, launch_item]
        recommended = [
            {**gateway_item, "label": "gateway URL and caller auth before external callers"},
            {**provider_item, "label": "cloud provider before external callers"},
            {**object_store_item, "label": "object store / DataRef storage before file or image workflows"},
            {**handoff_item, "label": "tenant API key handoff before external callers"},
            {**recipe_inbox_item, "label": "recipe request inbox before external callers"},
        ]
    elif selected_profile == "internal-team":
        required = [config_item, gateway_item, provider_item, handoff_item, recipe_inbox_item, synthetic_item, panopticon_service_item, admin_service_item, launch_item]
        recommended = [
            {**object_store_item, "label": "object store / DataRef storage before file or image workflows"},
            {"state": "ok" if automation["recipe_inbox_auto_materialize"] else "warn", "label": "recipe inbox auto-materialize policy reviewed"},
            _external_system_handoff_status_item(config_dir, gateway_url),
        ]
    else:
        required = [config_item, gateway_item, provider_item, object_store_item, handoff_item, recipe_inbox_item, synthetic_item, panopticon_service_item, admin_service_item, launch_item]
        recommended = [
            {"state": "ok" if automation["recipe_inbox_auto_materialize"] else "warn", "label": "recipe inbox auto-materialize policy reviewed"},
            _external_system_handoff_status_item(config_dir, gateway_url),
        ]
    oob_readiness = _oob_readiness_for(selected_profile, required, control_plane)
    return {
        "profile": selected_profile,
        "required": required,
        "recommended": recommended,
        "oob_readiness": oob_readiness,
        "control_plane": control_plane,
        "providers": providers,
        "provider_labels": provider_labels,
        "object_store_state": object_store_state,
        "gateway_url": gateway_url,
        "gateway_auth_state": gateway_auth,
        "caller_auth": caller_auth,
    }


def _local_trial_cloud_next_text() -> str:
    return (
        "Local trial is complete.\n\n"
        "To use gpucall with external systems, configure a cloud provider next.\n"
        "Recommended happy path: Modal.\n\n"
        "If you already have a Modal account and token, run:\n"
        "  gpucall setup starter-plan --profile internal-team --provider modal --output gpucall.modal.setup.yml\n"
        "  gpucall setup apply --file gpucall.modal.setup.yml --dry-run\n"
        "  gpucall setup apply --file gpucall.modal.setup.yml\n\n"
        "The apply step prompts for Modal token ID and token secret, stores them in the gpucall credentials store,\n"
        "deploys the bundled gpucall Modal worker, creates gateway caller auth, creates the recipe inbox,\n"
        "and enables the bounded demand-to-supply automation.\n\n"
        "If you do not yet have any cloud GPU provider account, create a Modal account and token first.\n"
        "Without provider credentials, gpucall will not start cloud routing; it remains fail-closed."
    )


def _external_system_handoff_status_item(config_dir: Path, gateway_url: str | None) -> dict[str, str]:
    names = _configured_external_system_names(config_dir)
    if not names:
        return {"state": "ok" if gateway_url else "warn", "label": "external-system onboarding prompt has concrete gateway URL"}
    missing = []
    root = _default_handoff_root()
    for name in names:
        directory = root / _safe_handoff_dir_name(name)
        if not (directory / "caller-ai-onboarding-prompt.md").exists() or not (directory / "CALLER_ENGINEER_README.md").exists():
            missing.append(name)
    if missing:
        return {"state": "warn", "label": "external-system handoff packages pending: " + ", ".join(missing)}
    return {"state": "ok", "label": "external-system handoff packages generated"}


def _configured_external_system_names(config_dir: Path) -> list[str]:
    path = config_dir / "setup.yml"
    if not path.exists():
        return []
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return []
    systems = payload.get("external_systems") if isinstance(payload, dict) else None
    if not isinstance(systems, list):
        return []
    names = []
    for system in systems:
        if isinstance(system, dict) and isinstance(system.get("name"), str) and system["name"].strip():
            names.append(system["name"].strip())
    return names


def _control_plane_status(config_dir: Path, *, profile: str, admin_synthetic_state: str) -> dict[str, str]:
    panopticon_path = default_panopticon_path()
    service_mode = _service_mode_for(profile)
    state = _load_control_plane_state()
    if panopticon_path.exists():
        panopticon_service_state = str(state.get("panopticon_service_state") or "service-initialized")
        panopticon_evidence_state = "evidence-missing" if _panopticon_snapshot_empty(panopticon_path) else "evidence-fresh"
    else:
        panopticon_service_state = "service-uninitialized"
        panopticon_evidence_state = "evidence-missing"
    return {
        "config_dir": str(config_dir),
        "panopticon_path": str(panopticon_path),
        "panopticon_service_mode": str(state.get("panopticon_service_mode") or service_mode),
        "panopticon_service_state": panopticon_service_state,
        "panopticon_bootstrap_state": str(state.get("panopticon_bootstrap_status") or "uninitialized"),
        "panopticon_evidence_state": panopticon_evidence_state,
        "admin_automation_service_mode": str(state.get("admin_automation_service_mode") or service_mode),
        "admin_automation_service_state": str(state.get("admin_automation_service_state") or "service-uninitialized"),
        "admin_synthetic_state": admin_synthetic_state,
    }


def _load_control_plane_state() -> dict[str, object]:
    path = default_state_dir() / "setup" / "control-plane-state.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _service_mode_for(profile: str) -> str:
    explicit = os.getenv("GPUCALL_SETUP_SERVICE_MODE")
    if explicit:
        return explicit
    if os.getenv("CI"):
        return "foreground-dry-run"
    if profile == "local-trial":
        return "foreground-status-probe"
    if sys.platform == "darwin":
        return "launchd-user-agent"
    if sys.platform.startswith("linux"):
        return "systemd-user-service" if shutil.which("systemctl") else "foreground-command"
    return "foreground-command"


def _panopticon_snapshot_empty(path: Path) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return True
    tuples = payload.get("tuples") if isinstance(payload, dict) else None
    return not bool(tuples)


def _oob_readiness_for(profile: str, required: list[dict[str, str]], control_plane: dict[str, str]) -> str:
    if any(item["state"] in {"missing", "partial", "warn", "service-error", "service-uninitialized"} for item in required):
        return "onboarding-blocked" if profile != "local-trial" else "provider-selection-required"
    if profile == "local-trial":
        return "local-trial-ready"
    if (
        control_plane["panopticon_service_state"] == "service-running"
        and control_plane["panopticon_evidence_state"] == "evidence-fresh"
        and control_plane["admin_automation_service_state"] == "service-running"
        and control_plane["admin_synthetic_state"] == "ok"
    ):
        return "onboarding-ready"
    return "onboarding-ready-provisional"


def _provider_display_state(config: Any | None, raw_providers: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    providers = dict(raw_providers)
    labels = {
        name: (f"{contract.display_name} configured" if providers.get(name) == "ok" else contract.setup_label)
        for name, contract in PROVIDER_SETUP_CONTRACTS.items()
    }
    if providers.get("runpod") == "ok" and not _runpod_endpoint_ready(config):
        providers["runpod"] = "partial"
        labels["runpod"] = "RunPod account connected; endpoint provisioning pending"
    elif providers.get("runpod") == "ok":
        labels["runpod"] = "RunPod managed endpoint ready"
    return providers, labels


def _runpod_endpoint_ready(config: Any | None) -> bool:
    if config is None:
        return False
    return any(
        (tuple_spec.account_ref == "runpod" or tuple_spec.adapter.startswith("runpod"))
        and _concrete_endpoint_target(tuple_spec.target)
        for tuple_spec in config.tuples.values()
    )


def _concrete_endpoint_target(target: str | None) -> bool:
    if not target:
        return False
    normalized = str(target).strip()
    if not normalized:
        return False
    return "PLACEHOLDER" not in normalized.upper()


def _caller_auth_status_line(summary: dict[str, Any]) -> str:
    if summary.get("state") != "ok":
        return "missing"
    records = summary.get("records")
    if not isinstance(records, list) or not records:
        return "missing"
    first = records[0] if isinstance(records[0], dict) else {}
    fingerprint = str(first.get("fingerprint") or "<unknown>")
    age = first.get("age_seconds")
    age_text = f"{age}s" if isinstance(age, int) else "unknown-age"
    expires = first.get("expires_at") or "non-expiring-policy"
    return f"ok count={summary.get('count')} fingerprint={fingerprint} age={age_text} expires={expires}"


def _provider_label(providers: dict[str, str], provider_labels: dict[str, str]) -> str:
    ready = [
        provider_labels[name]
        for name in PROVIDER_SETUP_CONTRACTS
        if providers[name] == "ok"
    ]
    pending = [
        provider_labels[name]
        for name in PROVIDER_SETUP_CONTRACTS
        if providers[name] == "partial"
    ]
    if ready and pending:
        return f"GPU execution surfaces: {', '.join(ready)}; {'; '.join(pending)}"
    if ready:
        return f"GPU execution surfaces: {', '.join(ready)}"
    if pending:
        prefix = "GPU execution surfaces"
        if providers["local"] == "ok":
            prefix += ": local smoke only"
        return prefix + "; " + "; ".join(pending)
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
    changes = [
        str(config_dir / "setup.yml"),
        str(config_dir / "admin.yml"),
        str(default_state_dir() / "setup" / "control-plane-state.json"),
        str(provider_registry_path()),
        str(default_panopticon_path()),
        str(default_state_dir() / "setup" / "admin-automation-synthetic-dry-run.json"),
    ]
    if not (config_dir / "policy.yml").exists():
        changes.append(f"{config_dir}/*")
    if plan.object_store is not None:
        changes.append(str(config_dir / "object_store.yml"))
    if plan.recipe_automation != SetupRecipeAutomation():
        changes.append(str(config_dir / "admin.yml") + " recipe_automation")
    if plan.handoff_assets != SetupHandoffAssets():
        changes.append(str(config_dir / "admin.yml") + " handoff_assets")
    runpod = plan.providers.get("runpod", SetupProvider())
    if runpod.enabled and runpod.endpoint_id:
        changes.append(str(config_dir / "surfaces" / "runpod-vllm-serverless.yml"))
        changes.append(str(config_dir / "workers" / "runpod-vllm-serverless.yml"))
    elif runpod.enabled:
        changes.append("provider account: runpod (endpoint provisioning pending)")
    modal = plan.providers.get("modal", SetupProvider())
    if modal.enabled and modal.deploy_worker:
        changes.append("provider worker deployment: modal gpucall-worker-json")
        changes.append(str(_provider_mutation_consent_path(_modal_deploy_plan_hash(plan) or "unknown")))
        changes.append(str(_modal_cleanup_manifest_path(_modal_deploy_plan_hash(plan) or "unknown")))
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
    configured.update(provider_registry_configured_contracts())
    for name, provider in sorted(plan.providers.items()):
        if not provider.enabled or provider.credentials is None:
            continue
        contract = PROVIDER_SETUP_CONTRACTS[name]
        if provider.credentials.source == "official_cli":
            cli = contract.official_cli
            if cli is None:
                warnings.append(f"{name} does not define official CLI credential discovery; use prompt or gpucall_credentials")
            elif shutil.which(cli) is None:
                warnings.append(f"{name} uses official CLI credentials, but {cli!r} is not on PATH")
        if provider.credentials.source == "gpucall_credentials":
            required = _provider_contracts(name)
            configured.update(_env_provider_contracts(name))
            missing = sorted(required.difference(configured))
            if missing:
                warnings.append(f"{name} credentials.source=gpucall_credentials but missing: {', '.join(missing)}")
        if contract.endpoint_id_supported and not provider.endpoint_id and contract.endpoint_pending_warning:
            warnings.append(contract.endpoint_pending_warning)
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


def _assert_provider_mutation_consent(plan: SetupPlan, *, yes: bool, accept_plan_hash: str | None) -> None:
    expected = _modal_deploy_plan_hash(plan)
    if expected is None:
        return
    if accept_plan_hash == expected:
        return
    if yes:
        raise SystemExit(
            "Modal worker deploy requires explicit provider-mutation consent. "
            f"Run `gpucall setup apply --file <plan> --dry-run`, review the plan, then re-run with --accept-plan-hash {expected}. "
            "--yes alone is not consent for provider mutation."
        )
    print(
        "\nModal worker deployment will create or update a provider-side resource.\n"
        f"Consent plan hash: {expected}\n"
        "Type the exact plan hash to allow this provider mutation, or press Enter to abort."
    )
    try:
        raw = input("Modal deploy plan hash: ").strip()
    except EOFError as exc:
        raise SystemExit(f"Modal worker deploy requires --accept-plan-hash {expected}") from exc
    if raw != expected:
        raise SystemExit(f"Modal worker deploy was not accepted; expected --accept-plan-hash {expected}")


def _modal_deploy_plan_hash(plan: SetupPlan) -> str | None:
    payload = _modal_deploy_plan_payload(plan)
    if payload is None:
        return None
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return digest[:16]


def _modal_deploy_plan_payload(plan: SetupPlan) -> dict[str, object] | None:
    provider = plan.providers.get("modal")
    if provider is None or not provider.enabled or not provider.deploy_worker:
        return None
    route_setup_config_hash = hashlib.sha256(
        json.dumps(plan.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    dry_run_basis = {
        "provider": "modal",
        "action": "deploy_worker",
        "profile": plan.profile,
        "gateway_base_url": plan.gateway.base_url,
        "worker_package_version": GITHUB_RELEASE_TAG,
    }
    dry_run_result_id = "modal-dry-run-" + hashlib.sha256(
        json.dumps(dry_run_basis, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    payload = {
        "action": "deploy_worker",
        "provider": "modal",
        "target": {
            "app": "gpucall",
            "function": "gpucall-worker-json",
            "environment": "credential-store-or-modal-default",
        },
        "worker": {
            "module": "gpucall.worker_contracts.modal",
            "package": "gpucall",
            "package_version": GITHUB_RELEASE_TAG,
            "package_hash": _modal_worker_package_hash(),
        },
        "setup": {
            "profile": plan.profile,
            "gateway_base_url": plan.gateway.base_url,
            "credential_source": provider.credentials.source if provider.credentials else None,
            "route_setup_config_hash": route_setup_config_hash,
        },
        "provider_registry_snapshot_hash": provider_registry_snapshot_hash(),
        "dry_run_result_id": dry_run_result_id,
        "estimated_cost_class": "provider-controlled-worker-deploy-no-generation",
    }
    return payload


def _modal_deploy_consent_artifact(plan: SetupPlan) -> dict[str, object] | None:
    payload = _modal_deploy_plan_payload(plan)
    plan_hash = _modal_deploy_plan_hash(plan)
    if payload is None or plan_hash is None:
        return None
    now = datetime.now(timezone.utc)
    cleanup_manifest = _modal_cleanup_manifest_path(plan_hash)
    return {
        "schema_version": 1,
        "phase": "setup-provider-mutation-consent",
        "provider": "modal",
        "action": "deploy_worker",
        "target": payload["target"],
        "worker": payload["worker"],
        "provider_registry_snapshot_hash": payload["provider_registry_snapshot_hash"],
        "route_setup_config_hash": payload["setup"]["route_setup_config_hash"],
        "dry_run_result_id": payload["dry_run_result_id"],
        "plan_hash": plan_hash,
        "estimated_cost_class": payload["estimated_cost_class"],
        "created_at": now.isoformat(),
        "expires_at": datetime.fromtimestamp(now.timestamp() + PROVIDER_MUTATION_CONSENT_TTL_SECONDS, timezone.utc).isoformat(),
        "ownership_tag": _modal_ownership_tag(plan_hash),
        "cleanup_manifest_path": str(cleanup_manifest),
    }


def _write_modal_deploy_consent_artifact(plan: SetupPlan) -> dict[str, object] | None:
    artifact = _modal_deploy_consent_artifact(plan)
    if artifact is None:
        return None
    artifact = {**artifact, "accepted_at": datetime.now(timezone.utc).isoformat()}
    _write_json_file(_provider_mutation_consent_path(str(artifact["plan_hash"])), artifact, mode=0o600)
    return artifact


def _provider_mutation_consent_path(plan_hash: str) -> Path:
    return default_state_dir() / "setup" / "provider-mutation-consents" / f"modal-deploy-{plan_hash}.json"


def _modal_cleanup_manifest_path(plan_hash: str) -> Path:
    return default_state_dir() / "setup" / "cleanup-manifests" / f"modal-deploy-{plan_hash}.json"


def _modal_ownership_tag(plan_hash: str) -> str:
    return f"gpucall-setup-{plan_hash}"


def _modal_worker_package_hash() -> str:
    path = Path(str(files("gpucall").joinpath("worker_contracts", "modal.py")))
    try:
        data = path.read_bytes()
    except OSError:
        data = b"gpucall.worker_contracts.modal"
    return hashlib.sha256(data).hexdigest()[:16]


def _setup_apply_report(plan: SetupPlan, changes: list[str], warnings: list[str], *, dry_run: bool) -> str:
    checks = ["[ok] setup schema valid"]
    if plan.tenant_onboarding.mode is ApiKeyHandoffMode.TRUSTED_BOOTSTRAP:
        checks.append("[ok] trusted bootstrap has allowlist")
    if plan.object_store is not None:
        checks.append("[ok] object store config has bucket")
    modal_hash = _modal_deploy_plan_hash(plan)
    if modal_hash is not None:
        checks.append(f"[warn] modal deploy requires provider-mutation consent plan_hash={modal_hash}")
    checks.extend(f"[warn] {warning}" for warning in warnings)
    text = (
        f"Setup plan: {plan.profile}\n\n"
        "Will update:\n"
        + "\n".join(f"  - {item}" for item in changes)
        + "\n\nWill not store:\n"
        "  - raw provider secrets in YAML\n"
        "  - external system API keys in repository files\n\n"
        "Checks:\n"
        + "\n".join(f"  {item}" for item in checks)
    )
    if modal_hash is not None:
        artifact = _modal_deploy_consent_artifact(plan)
        dry_run_id = artifact.get("dry_run_result_id") if artifact else "<unknown>"
        text += (
            "\n\nProvider mutation consent:\n"
            "  Modal worker deployment is a provider-side mutation.\n"
            f"  Dry-run result id: {dry_run_id}\n"
            f"  Cleanup manifest: {_modal_cleanup_manifest_path(modal_hash)}\n"
            f"  Re-run apply with: --accept-plan-hash {modal_hash}\n"
            "  Interactive apply still asks for final confirmation and any prompt-based credentials.\n"
            "  --yes alone is not provider mutation consent."
        )
    return text


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


def _ensure_oob_control_plane_initialized(config_dir: Path, plan: SetupPlan) -> None:
    state_dir = default_state_dir()
    for path in (
        state_dir,
        state_dir / "catalog",
        state_dir / "setup",
        state_dir / "setup" / "provider-mutation-consents",
        state_dir / "setup" / "cleanup-manifests",
        state_dir / "recipe_requests",
        state_dir / "quality_feedback",
    ):
        path.mkdir(parents=True, exist_ok=True)
    if not provider_registry_path().exists():
        _write_json_file(
            provider_registry_path(),
            {
                "schema_version": 1,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "providers": {},
            },
            mode=0o600,
        )
    panopticon_path = default_panopticon_path()
    if not panopticon_path.exists():
        _write_json_file(
            panopticon_path,
            {
                "schema_version": PANOPTICON_SCHEMA_VERSION,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "tuples": {},
            },
            mode=0o600,
        )
    service_state = {
        "schema_version": 1,
        "profile": plan.profile,
        "config_dir": str(config_dir),
        "panopticon_path": str(panopticon_path),
        "panopticon_service_mode": _service_mode_for(plan.profile),
        "panopticon_service_state": "service-initialized",
        "admin_automation_service_mode": _service_mode_for(plan.profile),
        "admin_automation_service_state": "service-initialized",
        "admin_synthetic_dry_run_ttl_seconds": ADMIN_SYNTHETIC_DRY_RUN_TTL_SECONDS,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_json_file(state_dir / "setup" / "control-plane-state.json", service_state, mode=0o600)


def _apply_gateway(config_dir: Path, plan: SetupPlan) -> None:
    if plan.gateway.caller_auth.mode == "generated_gateway_key":
        token = "gpk_" + secrets.token_urlsafe(32)
        save_credentials("auth", {"api_keys": token})
        record_caller_auth(
            "default",
            scope="gateway",
            token=token,
            non_expiring_policy_reason="setup-generated default gateway key; rotate or replace with tenant-scoped keys before broad production use",
        )


def _apply_providers(config_dir: Path, plan: SetupPlan, *, modal_plan_hash: str | None = None) -> list[str]:
    results: list[str] = []
    for name, provider in plan.providers.items():
        if not provider.enabled or provider.credentials is None:
            continue
        if provider.credentials.source == "prompt":
            if name == "runpod":
                api_key = _first_env_value("GPUCALL_RUNPOD_API_KEY", "RUNPOD_API_KEY")
                save_credentials("runpod", {"api_key": api_key or getpass.getpass("RunPod API key: ").strip()})
            if name == "modal":
                token_id = _first_env_value("MODAL_TOKEN_ID", "GPUCALL_MODAL_TOKEN_ID") or input("Modal token ID: ").strip()
                token_secret = _first_env_value("MODAL_TOKEN_SECRET", "GPUCALL_MODAL_TOKEN_SECRET") or getpass.getpass("Modal token secret: ").strip()
                environment = _first_env_value("MODAL_ENVIRONMENT", "GPUCALL_MODAL_ENVIRONMENT")
                if environment is None:
                    environment = input("Modal environment (optional, default main): ").strip()
                save_credentials("modal", {"token_id": token_id, "token_secret": token_secret})
                save_provider_metadata("modal", {"environment": environment or "main"}, state="credential-configured")
            if name == "hyperstack":
                api_key = _first_env_value("GPUCALL_HYPERSTACK_API_KEY", "HYPERSTACK_API_KEY")
                save_credentials("hyperstack", {"api_key": api_key or getpass.getpass("Hyperstack API key: ").strip()})
                save_provider_metadata("hyperstack", {"ssh_key_path": provider.ssh_key_path or ""}, state="provider-configured")
        elif provider.credentials.source == "gpucall_credentials":
            if name == "runpod" and "api_key" not in load_credentials().get("runpod", {}):
                api_key = _first_env_value("GPUCALL_RUNPOD_API_KEY", "RUNPOD_API_KEY")
                if api_key:
                    save_credentials("runpod", {"api_key": api_key})
            if name == "modal":
                modal_credentials = load_credentials().get("modal", {})
                token_id = str(modal_credentials.get("token_id") or _first_env_value("MODAL_TOKEN_ID", "GPUCALL_MODAL_TOKEN_ID") or "").strip()
                token_secret = str(modal_credentials.get("token_secret") or _first_env_value("MODAL_TOKEN_SECRET", "GPUCALL_MODAL_TOKEN_SECRET") or "").strip()
                environment = (
                    modal_credentials.get("environment")
                    or _first_env_value("MODAL_ENVIRONMENT", "GPUCALL_MODAL_ENVIRONMENT")
                    or _provider_registry_metadata("modal").get("environment")
                    or "main"
                )
                if token_id and token_secret:
                    save_credentials("modal", {"token_id": token_id, "token_secret": token_secret})
                save_provider_metadata("modal", {"environment": environment}, state="credential-configured")
            if name == "hyperstack" and "api_key" not in load_credentials().get("hyperstack", {}):
                api_key = _first_env_value("GPUCALL_HYPERSTACK_API_KEY", "HYPERSTACK_API_KEY")
                if api_key:
                    save_credentials("hyperstack", {"api_key": api_key})
            if name == "hyperstack" and provider.ssh_key_path:
                save_provider_metadata("hyperstack", {"ssh_key_path": provider.ssh_key_path}, state="provider-configured")
        if name == "runpod":
            state = "provider-configured" if provider.endpoint_id else "supply-pending"
            save_provider_metadata("runpod", {"endpoint_id": provider.endpoint_id or "", "endpoint_required": not bool(provider.endpoint_id)}, state=state)
        if name == "modal" and provider.deploy_worker:
            results.append(_deploy_modal_worker())
            plan_hash = modal_plan_hash or _modal_deploy_plan_hash(plan) or "unknown"
            save_provider_metadata(
                "modal",
                {
                    "environment": _provider_registry_metadata("modal").get("environment") or "main",
                    "deployment_id": f"modal:gpucall:gpucall-worker-json:{plan_hash}",
                    "ownership_tag": _modal_ownership_tag(plan_hash),
                    "cleanup_manifest_path": str(_modal_cleanup_manifest_path(plan_hash)),
                    "worker_contract_id": "gpucall-worker-json",
                    "function_name": "gpucall-worker-json",
                },
                state="provider-configured",
            )
        if name == "runpod" and provider.endpoint_id:
            _update_yaml(config_dir / "surfaces" / "runpod-vllm-serverless.yml", {"target": provider.endpoint_id})
            _update_yaml(config_dir / "workers" / "runpod-vllm-serverless.yml", {"target": provider.endpoint_id})
    return results


def _first_env_value(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value is not None and value.strip():
            return value.strip()
    return None


def _env_provider_contracts(name: str) -> set[str]:
    if name == "modal" and _first_env_value("MODAL_TOKEN_ID", "GPUCALL_MODAL_TOKEN_ID") and _first_env_value("MODAL_TOKEN_SECRET", "GPUCALL_MODAL_TOKEN_SECRET"):
        return {"token_pair:modal"}
    if name == "runpod" and _first_env_value("GPUCALL_RUNPOD_API_KEY", "RUNPOD_API_KEY"):
        return {"api_key:runpod"}
    if name == "hyperstack" and _first_env_value("GPUCALL_HYPERSTACK_API_KEY", "HYPERSTACK_API_KEY"):
        return {"api_key:hyperstack"}
    return set()


def _provider_registry_metadata(provider: str) -> dict[str, object]:
    registry = load_provider_registry()
    providers = registry.get("providers") if isinstance(registry, dict) else {}
    record = providers.get(provider) if isinstance(providers, dict) else None
    metadata = record.get("metadata") if isinstance(record, dict) else {}
    return dict(metadata) if isinstance(metadata, dict) else {}


def _deploy_modal_worker() -> str:
    credentials = load_credentials().get("modal", {})
    metadata = _provider_registry_metadata("modal")
    env = dict(os.environ)
    token_id = str(credentials.get("token_id") or "").strip()
    token_secret = str(credentials.get("token_secret") or "").strip()
    environment = str(metadata.get("environment") or "").strip()
    if token_id and token_secret:
        env["MODAL_TOKEN_ID"] = token_id
        env["MODAL_TOKEN_SECRET"] = token_secret
    if environment:
        env["MODAL_ENVIRONMENT"] = environment
    command = [sys.executable, "-m", "modal", "deploy", "-m", "gpucall.worker_contracts.modal"]
    completed = subprocess.run(command, capture_output=True, text=True, env=env, check=False, timeout=1800)
    if completed.returncode != 0:
        raise SystemExit("Modal worker deploy failed: " + _safe_subprocess_tail(completed.stderr or completed.stdout))
    return "[ok] Modal worker deployed: gpucall-worker-json"


def _write_modal_cleanup_manifest(consent_artifact: dict[str, object]) -> None:
    plan_hash = str(consent_artifact.get("plan_hash") or "unknown")
    manifest = {
        "schema_version": 1,
        "phase": "setup-provider-cleanup-manifest",
        "provider": "modal",
        "eligible_for_cleanup_only_when": {
            "ownership_tag": consent_artifact.get("ownership_tag"),
            "deployment_id": f"modal:gpucall:gpucall-worker-json:{plan_hash}",
            "plan_hash": plan_hash,
        },
        "resources": [
            {
                "kind": "modal_function",
                "app": "gpucall",
                "function": "gpucall-worker-json",
                "ownership_tag": consent_artifact.get("ownership_tag"),
            }
        ],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_json_file(_modal_cleanup_manifest_path(plan_hash), manifest, mode=0o600)


def _run_panopticon_bootstrap_refresh(config_dir: Path, plan: SetupPlan) -> dict[str, object]:
    path = default_state_dir() / "setup" / "panopticon-bootstrap-refresh.json"
    enabled_cloud_providers = [name for name, provider in plan.providers.items() if provider.enabled and name in CLOUD_PROVIDER_FAMILIES]
    if not enabled_cloud_providers:
        report = {
            "schema_version": 1,
            "phase": "provider-panopticon-bootstrap-refresh",
            "status": "skipped",
            "reason": "no cloud providers configured",
            "provider_registry_reloaded": True,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        _write_json_file(path, report, mode=0o600)
        return report
    try:
        if os.getenv("GPUCALL_SETUP_LIVE_PROVIDER_PROBES", "0") == "0":
            provider_registry = load_provider_registry()
            report = {
                "schema_version": 1,
                "phase": "provider-panopticon-bootstrap-refresh",
                "status": "processed",
                "mode": "preflight-only",
                "reason": "inline live probes skipped; Provider Panopticon service will refresh provider evidence in the background",
                "provider_registry_reloaded": True,
                "provider_registry_snapshot_hash": provider_registry_snapshot_hash(),
                "provider_count": len(provider_registry.get("providers") or {}),
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        else:
            refresh = refresh_panopticon(config_dir=config_dir, panopticon_path=default_panopticon_path())
            preflight = refresh.get("preflight") if isinstance(refresh, dict) else {}
            refresh_status = str(refresh.get("status") or "") if isinstance(refresh, dict) else ""
            preflight_status = str(preflight.get("status") or "") if isinstance(preflight, dict) else ""
            status = "processed" if (preflight_status in {"ok", "partial"} or refresh_status in {"ok", "processed", "partial"}) else "blocked"
            if status == "processed" and refresh_status == "partial":
                reason = "provider panopticon bootstrap refresh completed with provider-level warnings"
            else:
                reason = "provider panopticon bootstrap refresh completed" if status == "processed" else "provider panopticon bootstrap refresh blocked"
            report = {
                "schema_version": 1,
                "phase": "provider-panopticon-bootstrap-refresh",
                "status": status,
                "mode": "live-non-generation-probes",
                "reason": reason,
                "provider_registry_reloaded": True,
                "provider_registry_snapshot_hash": provider_registry_snapshot_hash(),
                "panopticon_report": _redacted_panopticon_report(refresh),
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
    except Exception as exc:
        report = {
            "schema_version": 1,
            "phase": "provider-panopticon-bootstrap-refresh",
            "status": "failed",
            "reason": f"provider panopticon bootstrap refresh failed: {type(exc).__name__}: {exc}",
            "provider_registry_reloaded": True,
            "provider_registry_snapshot_hash": provider_registry_snapshot_hash(),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    _write_json_file(path, report, mode=0o600)
    return report


def _start_panopticon_background_service(config_dir: Path, plan: SetupPlan) -> dict[str, object]:
    path = default_state_dir() / "setup" / "panopticon-service.json"
    enabled_cloud_providers = [name for name, provider in plan.providers.items() if provider.enabled and name in CLOUD_PROVIDER_FAMILIES]
    if not enabled_cloud_providers:
        report = {
            "schema_version": 1,
            "phase": "provider-panopticon-service-start",
            "status": "service-initialized",
            "reason": "no cloud providers configured",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        _write_json_file(path, report, mode=0o600)
        return report
    if os.getenv("GPUCALL_SETUP_START_SERVICES", "1") == "0":
        report = {
            "schema_version": 1,
            "phase": "provider-panopticon-service-start",
            "status": "service-initialized",
            "reason": "service start disabled by GPUCALL_SETUP_START_SERVICES=0",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        _write_json_file(path, report, mode=0o600)
        return report
    host = "127.0.0.1"
    port = int(os.getenv("GPUCALL_PANOPTICON_PORT", "18090"))
    health_url = f"http://{host}:{port}/healthz"
    if _http_health_ok(health_url):
        report = {
            "schema_version": 1,
            "phase": "provider-panopticon-service-start",
            "status": "service-running",
            "reason": "existing panopticon health endpoint is healthy",
            "health_url": health_url,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        _write_json_file(path, report, mode=0o600)
        return report
    if _tcp_port_open(host, port):
        report = {
            "schema_version": 1,
            "phase": "provider-panopticon-service-start",
            "status": "service-error",
            "reason": f"port {host}:{port} is already in use by a non-panopticon service",
            "health_url": health_url,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        _write_json_file(path, report, mode=0o600)
        return report
    service_mode = _service_mode_for(plan.profile)
    if _compose_service_mode(service_mode):
        report = _start_compose_panopticon_service(health_url=health_url)
        _write_json_file(path, report, mode=0o600)
        return report
    if service_mode == "systemd-user-service" and shutil.which("systemctl"):
        report = _start_systemd_user_panopticon_service(config_dir, host=host, port=port, health_url=health_url)
        _write_json_file(path, report, mode=0o600)
        return report
    if service_mode == "launchd-user-agent" and shutil.which("launchctl"):
        report = _start_launchd_user_panopticon_service(config_dir, host=host, port=port, health_url=health_url)
        _write_json_file(path, report, mode=0o600)
        return report
    if os.getenv("GPUCALL_SETUP_ALLOW_BACKGROUND_FALLBACK") != "1":
        report = {
            "schema_version": 1,
            "phase": "provider-panopticon-service-start",
            "status": "service-initialized",
            "reason": f"no safe supervisor available for service mode {service_mode}",
            "next_command": f"gpucall panopticon serve --config-dir {config_dir} --host {host} --port {port}",
            "health_url": health_url,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        _write_json_file(path, report, mode=0o600)
        return report
    log_path = default_state_dir() / "setup" / "panopticon-service.log"
    command = [
        sys.executable,
        "-m",
        "gpucall.cli",
        "panopticon",
        "serve",
        "--config-dir",
        str(config_dir),
        "--host",
        host,
        "--port",
        str(port),
        "--panopticon-path",
        str(default_panopticon_path()),
    ]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log:
        process = subprocess.Popen(command, stdout=log, stderr=log, start_new_session=True)
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        if process.poll() is not None:
            break
        if _http_health_ok(health_url):
            report = {
                "schema_version": 1,
                "phase": "provider-panopticon-service-start",
                "status": "service-running",
                "reason": "panopticon health endpoint is healthy",
                "pid": process.pid,
                "health_url": health_url,
                "log_path": str(log_path),
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            _write_json_file(path, report, mode=0o600)
            return report
        time.sleep(0.25)
    report = {
        "schema_version": 1,
        "phase": "provider-panopticon-service-start",
        "status": "service-error",
        "reason": "panopticon service did not become healthy within 8 seconds",
        "pid": process.pid,
        "returncode": process.poll(),
        "health_url": health_url,
        "log_path": str(log_path),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    if process.poll() is None:
        process.terminate()
    _write_json_file(path, report, mode=0o600)
    return report


def _compose_service_mode(service_mode: str) -> bool:
    return service_mode in {"docker-compose-service", "compose-service"}


def _compose_base_command() -> tuple[list[str] | None, str | None]:
    if not shutil.which("docker"):
        return None, "docker executable not found"
    compose_file = os.getenv("GPUCALL_SETUP_COMPOSE_FILE")
    if not compose_file:
        return None, "GPUCALL_SETUP_COMPOSE_FILE is required for docker compose service mode"
    return ["docker", "compose", "-f", compose_file], None


def _start_compose_panopticon_service(*, health_url: str) -> dict[str, object]:
    base_command, error = _compose_base_command()
    service = os.getenv("GPUCALL_SETUP_PANOPTICON_COMPOSE_SERVICE", "gpucall-panopticon")
    if base_command is None:
        return {
            "schema_version": 1,
            "phase": "provider-panopticon-service-start",
            "status": "service-initialized",
            "reason": error or "docker compose service mode is not configured",
            "service_mode": "docker-compose-service",
            "service_name": service,
            "health_url": health_url,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    completed = subprocess.run(base_command + ["up", "-d", service], capture_output=True, text=True, check=False, timeout=60)
    if completed.returncode != 0:
        return {
            "schema_version": 1,
            "phase": "provider-panopticon-service-start",
            "status": "service-error",
            "reason": "docker compose service start failed: " + _safe_subprocess_tail(completed.stderr or completed.stdout),
            "service_mode": "docker-compose-service",
            "service_name": service,
            "health_url": health_url,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    return _wait_for_panopticon_health(health_url, unit_path=Path(os.getenv("GPUCALL_SETUP_COMPOSE_FILE", "")), service_mode="docker-compose-service")


def _start_systemd_user_panopticon_service(config_dir: Path, *, host: str, port: int, health_url: str) -> dict[str, object]:
    unit_dir = Path(os.getenv("XDG_CONFIG_HOME", str(Path.home() / ".config"))).expanduser() / "systemd" / "user"
    unit_path = unit_dir / "gpucall-panopticon.service"
    unit_dir.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "gpucall.cli",
        "panopticon",
        "serve",
        "--config-dir",
        str(config_dir),
        "--host",
        host,
        "--port",
        str(port),
        "--panopticon-path",
        str(default_panopticon_path()),
    ]
    unit_text = "\n".join(
        [
            "[Unit]",
            "Description=gpucall Provider Panopticon",
            "",
            "[Service]",
            "Type=simple",
            "Restart=on-failure",
            f"ExecStart={shlex.join(command)}",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )
    unit_path.write_text(unit_text, encoding="utf-8")
    unit_path.chmod(0o644)
    commands = [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", "gpucall-panopticon.service"],
    ]
    for command in commands:
        completed = subprocess.run(command, capture_output=True, text=True, check=False, timeout=20)
        if completed.returncode != 0:
            return {
                "schema_version": 1,
                "phase": "provider-panopticon-service-start",
                "status": "service-error",
                "reason": "systemd user service start failed: " + _safe_subprocess_tail(completed.stderr or completed.stdout),
                "unit_path": str(unit_path),
                "health_url": health_url,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
    return _wait_for_panopticon_health(health_url, unit_path=unit_path, service_mode="systemd-user-service")


def _start_launchd_user_panopticon_service(config_dir: Path, *, host: str, port: int, health_url: str) -> dict[str, object]:
    label = "ai.tnmc.gpucall.panopticon"
    plist_dir = Path.home() / "Library" / "LaunchAgents"
    plist_path = plist_dir / f"{label}.plist"
    plist_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "Label": label,
        "ProgramArguments": [
            sys.executable,
            "-m",
            "gpucall.cli",
            "panopticon",
            "serve",
            "--config-dir",
            str(config_dir),
            "--host",
            host,
            "--port",
            str(port),
            "--panopticon-path",
            str(default_panopticon_path()),
        ],
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(default_state_dir() / "setup" / "panopticon-service.log"),
        "StandardErrorPath": str(default_state_dir() / "setup" / "panopticon-service.log"),
    }
    with plist_path.open("wb") as handle:
        plistlib.dump(payload, handle)
    plist_path.chmod(0o644)
    domain = f"gui/{os.getuid()}"
    bootstrap = subprocess.run(["launchctl", "bootstrap", domain, str(plist_path)], capture_output=True, text=True, check=False, timeout=20)
    if bootstrap.returncode != 0 and "already bootstrapped" not in (bootstrap.stderr or bootstrap.stdout):
        return {
            "schema_version": 1,
            "phase": "provider-panopticon-service-start",
            "status": "service-error",
            "reason": "launchd user agent bootstrap failed: " + _safe_subprocess_tail(bootstrap.stderr or bootstrap.stdout),
            "unit_path": str(plist_path),
            "health_url": health_url,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    kickstart = subprocess.run(["launchctl", "kickstart", "-k", f"{domain}/{label}"], capture_output=True, text=True, check=False, timeout=20)
    if kickstart.returncode != 0:
        return {
            "schema_version": 1,
            "phase": "provider-panopticon-service-start",
            "status": "service-error",
            "reason": "launchd user agent kickstart failed: " + _safe_subprocess_tail(kickstart.stderr or kickstart.stdout),
            "unit_path": str(plist_path),
            "health_url": health_url,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    return _wait_for_panopticon_health(health_url, unit_path=plist_path, service_mode="launchd-user-agent")


def _wait_for_panopticon_health(health_url: str, *, unit_path: Path, service_mode: str) -> dict[str, object]:
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        if _http_health_ok(health_url):
            return {
                "schema_version": 1,
                "phase": "provider-panopticon-service-start",
                "status": "service-running",
                "reason": "panopticon health endpoint is healthy",
                "service_mode": service_mode,
                "unit_path": str(unit_path),
                "health_url": health_url,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        time.sleep(0.25)
    return {
        "schema_version": 1,
        "phase": "provider-panopticon-service-start",
        "status": "service-error",
        "reason": "panopticon service did not become healthy within 8 seconds",
        "service_mode": service_mode,
        "unit_path": str(unit_path),
        "health_url": health_url,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def _http_health_ok(url: str) -> bool:
    try:
        with urlopen(url, timeout=1.0) as response:
            return 200 <= int(response.status) < 300
    except (OSError, URLError, TimeoutError):
        return False


def _tcp_port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def _start_admin_automation_service(config_dir: Path, plan: SetupPlan) -> dict[str, object]:
    path = default_state_dir() / "setup" / "admin-automation-service.json"
    recipe_inbox = _local_path_from_inbox_spec(plan.tenant_onboarding.recipe_inbox)
    if recipe_inbox is None:
        report = {
            "schema_version": 1,
            "phase": "admin-automation-service-start",
            "status": "service-initialized",
            "reason": "recipe inbox is missing or remote; admin watch service needs a local inbox path",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        _write_json_file(path, report, mode=0o600)
        return report
    if not plan.recipe_automation.auto_materialize:
        report = {
            "schema_version": 1,
            "phase": "admin-automation-service-start",
            "status": "service-initialized",
            "reason": "recipe automation auto_materialize is disabled",
            "inbox_dir": str(recipe_inbox),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        _write_json_file(path, report, mode=0o600)
        return report
    if os.getenv("GPUCALL_SETUP_START_SERVICES", "1") == "0":
        report = {
            "schema_version": 1,
            "phase": "admin-automation-service-start",
            "status": "service-initialized",
            "reason": "service start disabled by GPUCALL_SETUP_START_SERVICES=0",
            "inbox_dir": str(recipe_inbox),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        _write_json_file(path, report, mode=0o600)
        return report
    service_mode = _service_mode_for(plan.profile)
    if _compose_service_mode(service_mode):
        report = _start_compose_admin_automation_service()
        _write_json_file(path, report, mode=0o600)
        return report
    if service_mode == "systemd-user-service" and shutil.which("systemctl"):
        report = _start_systemd_user_admin_automation_service(config_dir, recipe_inbox)
        _write_json_file(path, report, mode=0o600)
        return report
    if service_mode == "launchd-user-agent" and shutil.which("launchctl"):
        report = _start_launchd_user_admin_automation_service(config_dir, recipe_inbox)
        _write_json_file(path, report, mode=0o600)
        return report
    report = {
        "schema_version": 1,
        "phase": "admin-automation-service-start",
        "status": "service-initialized",
        "reason": f"no safe supervisor available for service mode {service_mode}",
        "next_command": shlex.join(_admin_watch_command(config_dir, recipe_inbox)),
        "inbox_dir": str(recipe_inbox),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_json_file(path, report, mode=0o600)
    return report


def _admin_watch_command(config_dir: Path, recipe_inbox: Path) -> list[str]:
    return [
        sys.executable,
        "-m",
        "gpucall.recipe_admin",
        "watch",
        "--inbox-dir",
        str(recipe_inbox),
        "--output-dir",
        str(config_dir / "recipes"),
        "--config-dir",
        str(config_dir),
        "--accept-all",
    ]


def _start_systemd_user_admin_automation_service(config_dir: Path, recipe_inbox: Path) -> dict[str, object]:
    service_name = "gpucall-recipe-admin-watch.service"
    unit_dir = Path(os.getenv("XDG_CONFIG_HOME", str(Path.home() / ".config"))).expanduser() / "systemd" / "user"
    unit_path = unit_dir / service_name
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit_text = "\n".join(
        [
            "[Unit]",
            "Description=gpucall recipe admin automation",
            "",
            "[Service]",
            "Type=simple",
            "Restart=on-failure",
            f"ExecStart={shlex.join(_admin_watch_command(config_dir, recipe_inbox))}",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )
    unit_path.write_text(unit_text, encoding="utf-8")
    unit_path.chmod(0o644)
    commands = [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", service_name],
    ]
    for command in commands:
        completed = subprocess.run(command, capture_output=True, text=True, check=False, timeout=20)
        if completed.returncode != 0:
            return {
                "schema_version": 1,
                "phase": "admin-automation-service-start",
                "status": "service-error",
                "reason": "systemd user service start failed: " + _safe_subprocess_tail(completed.stderr or completed.stdout),
                "service_mode": "systemd-user-service",
                "unit_path": str(unit_path),
                "inbox_dir": str(recipe_inbox),
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
    return _wait_for_systemd_service_active(
        service_name,
        phase="admin-automation-service-start",
        service_mode="systemd-user-service",
        unit_path=unit_path,
        extra={"inbox_dir": str(recipe_inbox)},
    )


def _start_launchd_user_admin_automation_service(config_dir: Path, recipe_inbox: Path) -> dict[str, object]:
    label = "ai.tnmc.gpucall.recipe-admin-watch"
    plist_dir = Path.home() / "Library" / "LaunchAgents"
    plist_path = plist_dir / f"{label}.plist"
    plist_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "Label": label,
        "ProgramArguments": _admin_watch_command(config_dir, recipe_inbox),
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(default_state_dir() / "setup" / "admin-automation-service.log"),
        "StandardErrorPath": str(default_state_dir() / "setup" / "admin-automation-service.log"),
    }
    with plist_path.open("wb") as handle:
        plistlib.dump(payload, handle)
    plist_path.chmod(0o644)
    domain = f"gui/{os.getuid()}"
    bootstrap = subprocess.run(["launchctl", "bootstrap", domain, str(plist_path)], capture_output=True, text=True, check=False, timeout=20)
    if bootstrap.returncode != 0 and "already bootstrapped" not in (bootstrap.stderr or bootstrap.stdout):
        return {
            "schema_version": 1,
            "phase": "admin-automation-service-start",
            "status": "service-error",
            "reason": "launchd user agent bootstrap failed: " + _safe_subprocess_tail(bootstrap.stderr or bootstrap.stdout),
            "service_mode": "launchd-user-agent",
            "unit_path": str(plist_path),
            "inbox_dir": str(recipe_inbox),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    kickstart = subprocess.run(["launchctl", "kickstart", "-k", f"{domain}/{label}"], capture_output=True, text=True, check=False, timeout=20)
    if kickstart.returncode != 0:
        return {
            "schema_version": 1,
            "phase": "admin-automation-service-start",
            "status": "service-error",
            "reason": "launchd user agent kickstart failed: " + _safe_subprocess_tail(kickstart.stderr or kickstart.stdout),
            "service_mode": "launchd-user-agent",
            "unit_path": str(plist_path),
            "inbox_dir": str(recipe_inbox),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    status = subprocess.run(["launchctl", "print", f"{domain}/{label}"], capture_output=True, text=True, check=False, timeout=20)
    if status.returncode == 0:
        return {
            "schema_version": 1,
            "phase": "admin-automation-service-start",
            "status": "service-running",
            "reason": "launchd user agent is loaded",
            "service_mode": "launchd-user-agent",
            "unit_path": str(plist_path),
            "inbox_dir": str(recipe_inbox),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    return {
        "schema_version": 1,
        "phase": "admin-automation-service-start",
        "status": "service-error",
        "reason": "launchd user agent is not loaded: " + _safe_subprocess_tail(status.stderr or status.stdout),
        "service_mode": "launchd-user-agent",
        "unit_path": str(plist_path),
        "inbox_dir": str(recipe_inbox),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def _start_compose_admin_automation_service() -> dict[str, object]:
    base_command, error = _compose_base_command()
    service = os.getenv("GPUCALL_SETUP_ADMIN_COMPOSE_SERVICE", "gpucall-recipe-admin")
    if base_command is None:
        return {
            "schema_version": 1,
            "phase": "admin-automation-service-start",
            "status": "service-initialized",
            "reason": error or "docker compose service mode is not configured",
            "service_mode": "docker-compose-service",
            "service_name": service,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    completed = subprocess.run(base_command + ["up", "-d", service], capture_output=True, text=True, check=False, timeout=60)
    if completed.returncode != 0:
        return {
            "schema_version": 1,
            "phase": "admin-automation-service-start",
            "status": "service-error",
            "reason": "docker compose service start failed: " + _safe_subprocess_tail(completed.stderr or completed.stdout),
            "service_mode": "docker-compose-service",
            "service_name": service,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    return _wait_for_compose_service_running(base_command, service, phase="admin-automation-service-start")


def _wait_for_systemd_service_active(
    service_name: str,
    *,
    phase: str,
    service_mode: str,
    unit_path: Path,
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        active = subprocess.run(["systemctl", "--user", "is-active", "--quiet", service_name], capture_output=True, text=True, check=False, timeout=10)
        if active.returncode == 0:
            report = {
                "schema_version": 1,
                "phase": phase,
                "status": "service-running",
                "reason": "systemd user service is active",
                "service_mode": service_mode,
                "unit_path": str(unit_path),
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            if extra:
                report.update(extra)
            return report
        time.sleep(0.25)
    report = {
        "schema_version": 1,
        "phase": phase,
        "status": "service-error",
        "reason": "systemd user service did not become active within 5 seconds",
        "service_mode": service_mode,
        "unit_path": str(unit_path),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    if extra:
        report.update(extra)
    return report


def _wait_for_compose_service_running(base_command: list[str], service: str, *, phase: str) -> dict[str, object]:
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        status = subprocess.run(base_command + ["ps", "--status", "running", "--services"], capture_output=True, text=True, check=False, timeout=20)
        if status.returncode == 0 and service in {line.strip() for line in status.stdout.splitlines()}:
            return {
                "schema_version": 1,
                "phase": phase,
                "status": "service-running",
                "reason": "docker compose service is running",
                "service_mode": "docker-compose-service",
                "service_name": service,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        time.sleep(0.25)
    return {
        "schema_version": 1,
        "phase": phase,
        "status": "service-error",
        "reason": "docker compose service did not report running within 5 seconds",
        "service_mode": "docker-compose-service",
        "service_name": service,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def _redacted_panopticon_report(report: dict[str, object]) -> dict[str, object]:
    allowed = {
        "phase",
        "status",
        "snapshot_path",
        "scope_tuple_count",
        "selected_tuple_count",
        "observed_count",
        "snapshot_count",
        "preflight",
    }
    return {key: _setup_json_safe(value) for key, value in report.items() if key in allowed}


def _setup_json_safe(value: object) -> object:
    if isinstance(value, dict):
        return {str(key): _setup_json_safe(item) for key, item in value.items()}
    if isinstance(value, set):
        return [_setup_json_safe(item) for item in sorted(value, key=str)]
    if isinstance(value, (list, tuple)):
        return [_setup_json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _update_oob_control_plane_state(
    config_dir: Path,
    plan: SetupPlan,
    *,
    synthetic_result: dict[str, object],
    panopticon_bootstrap: dict[str, object],
    panopticon_service: dict[str, object],
    admin_service: dict[str, object],
) -> None:
    state_dir = default_state_dir()
    service_state = _load_control_plane_state()
    panopticon_status = str(panopticon_bootstrap.get("status") or "")
    synthetic_status = str(synthetic_result.get("status") or "")
    panopticon_service_state = str(panopticon_service.get("status") or "service-initialized")
    admin_service_state = str(admin_service.get("status") or "service-initialized")
    service_state.update(
        {
            "schema_version": 1,
            "profile": plan.profile,
            "config_dir": str(config_dir),
            "panopticon_path": str(default_panopticon_path()),
            "panopticon_service_mode": _service_mode_for(plan.profile),
            "panopticon_service_state": panopticon_service_state,
            "panopticon_service_health_url": panopticon_service.get("health_url"),
            "panopticon_service_pid": panopticon_service.get("pid"),
            "panopticon_bootstrap_status": panopticon_status,
            "admin_automation_service_mode": _service_mode_for(plan.profile),
            "admin_automation_service_state": admin_service_state,
            "admin_automation_service_unit_path": admin_service.get("unit_path"),
            "admin_automation_service_inbox_dir": admin_service.get("inbox_dir"),
            "admin_synthetic_status": synthetic_status,
            "admin_synthetic_dry_run_ttl_seconds": ADMIN_SYNTHETIC_DRY_RUN_TTL_SECONDS,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    _write_json_file(state_dir / "setup" / "control-plane-state.json", service_state, mode=0o600)


def _safe_subprocess_tail(text: str, *, limit: int = 1200) -> str:
    redacted = text
    modal_credentials = load_credentials().get("modal", {})
    for value in (modal_credentials.get("token_secret"), modal_credentials.get("token_id")):
        secret = str(value or "").strip()
        if secret:
            redacted = redacted.replace(secret, "<redacted>")
    return redacted.strip()[-limit:] or "no output"


def _apply_object_store(config_dir: Path, plan: SetupPlan) -> None:
    object_store = plan.object_store
    if object_store is None:
        return
    if object_store.credentials and object_store.credentials.source == "prompt":
        access_key = getpass.getpass("Object store access key: ").strip()
        secret_key = getpass.getpass("Object store secret key: ").strip()
        save_credentials("aws", {"access_key_id": access_key, "secret_access_key": secret_key})
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
        recipe_inbox_auto_provision_supply=plan.recipe_automation.auto_provision_supply,
        recipe_inbox_auto_apply_supply=plan.recipe_automation.auto_apply_supply,
        recipe_inbox_auto_billable_validation=plan.recipe_automation.auto_billable_validation,
        recipe_inbox_auto_validation_budget_usd=plan.recipe_automation.auto_validation_budget_usd,
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


def _maybe_create_local_inboxes(recipe_inbox: str | None) -> None:
    recipe_path = _local_path_from_inbox_spec(recipe_inbox)
    if recipe_path is None:
        return
    quality_path = Path(_default_quality_feedback_inbox(str(recipe_path)))
    for path in (recipe_path, quality_path):
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o700)


def _local_path_from_inbox_spec(value: str | None) -> Path | None:
    if not value:
        return None
    text = value.strip()
    if text.startswith("file://"):
        return Path(text[7:]).expanduser()
    if "@" in text.split(":", 1)[0]:
        return None
    if ":" in text and not text.startswith("/"):
        return None
    path = Path(text).expanduser()
    return path if path.is_absolute() else None


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


def _write_external_system_handoff_packages(config_dir: Path, plan: SetupPlan) -> list[str]:
    if not plan.external_systems:
        return []
    results: list[str] = []
    root = _default_handoff_root()
    for system in plan.external_systems:
        output_dir = root / _safe_handoff_dir_name(system.name)
        report = write_handoff_package(config_dir, system.name, output_dir)
        results.append(f"[ok] {system.name}: {report['output_dir']}")
    return results


def _synthetic_dry_run_report_text(result: dict[str, object]) -> str:
    status = str(result.get("status") or "unknown")
    reason = str(result.get("reason") or "")
    if status == "processed":
        return "\n\nAdmin automation synthetic dry-run:\n  [ok] synthetic intake parsed and classified without provider mutation or billable work"
    if status == "pending":
        return f"\n\nAdmin automation synthetic dry-run:\n  [warn] {reason}"
    return f"\n\nAdmin automation synthetic dry-run:\n  [fail] {reason or 'synthetic dry-run failed'}"


def _panopticon_bootstrap_report_text(result: dict[str, object]) -> str:
    status = str(result.get("status") or "unknown")
    reason = str(result.get("reason") or "")
    mode = str(result.get("mode") or "")
    if status == "processed" and mode == "preflight-only":
        return f"\n\nPanopticon bootstrap refresh:\n  [warn] {reason}"
    if status == "processed":
        if "warning" in reason:
            return f"\n\nPanopticon bootstrap refresh:\n  [warn] {reason}"
        return "\n\nPanopticon bootstrap refresh:\n  [ok] provider registry reloaded and non-generation readiness evidence refreshed"
    if status == "skipped":
        return f"\n\nPanopticon bootstrap refresh:\n  [warn] {reason}"
    if status == "blocked":
        return f"\n\nPanopticon bootstrap refresh:\n  [warn] {reason}"
    return f"\n\nPanopticon bootstrap refresh:\n  [fail] {reason or 'bootstrap refresh failed'}"


def _admin_automation_service_report_text(result: dict[str, object]) -> str:
    status = str(result.get("status") or "unknown")
    reason = str(result.get("reason") or "")
    if status == "service-running":
        return "\n\nAdmin automation service:\n  [ok] recipe admin watch service is running"
    if status == "service-initialized":
        return f"\n\nAdmin automation service:\n  [warn] {reason or 'service initialized but not running'}"
    return f"\n\nAdmin automation service:\n  [fail] {reason or 'service failed'}"


def _setup_completion_text(config_dir: Path, plan: SetupPlan, *, handoff_results: list[str]) -> str:
    status = _setup_status(config_dir, profile=plan.profile)
    missing_required = [item for item in status["required"] if item["state"] in {"missing", "partial", "warn", "service-error", "service-uninitialized"}]
    if missing_required:
        return (
            "Setup was applied, but this profile is not ready yet.\n\n"
            "Next, run:\n"
            "  gpucall setup next"
        )
    if plan.profile == "local-trial":
        return (
            "Local trial is complete.\n\n"
            "Next, configure the Modal happy path for external systems:\n"
            "  gpucall setup starter-plan --profile internal-team --provider modal --output gpucall.modal.setup.yml\n"
            "  gpucall setup apply --file gpucall.modal.setup.yml --dry-run\n"
            "  gpucall setup apply --file gpucall.modal.setup.yml"
        )
    if plan.profile == "internal-team":
        if status["oob_readiness"] == "onboarding-ready-provisional":
            return (
                "Setup is onboarding-ready-provisional.\n\n"
                "Caller onboarding may begin after you verify caller-side gateway reachability, but production routing still requires\n"
                "fresh Panopticon evidence and exact route validation evidence.\n\n"
                "Next, start or restart the gpucall gateway and Provider Panopticon service, then generate or hand off the caller package.\n"
                "The caller-side AI CLI entrypoint is caller-ai-onboarding-prompt.md."
            )
        if handoff_results:
            return (
                "Now you are good to go.\n\n"
                "Next, start or restart the gpucall gateway, then give the generated caller-ai-onboarding-prompt.md\n"
                "to the external system's coding AI CLI."
            )
        return (
            "Now you are good to go for an internal gateway setup.\n\n"
            "Next, generate a caller handoff package for each external system:\n"
            "  gpucall setup export-handoff-package --system-name <external-system> --output-dir \"$XDG_DATA_HOME/gpucall/handoffs/<external-system>\"\n\n"
            "Then start or restart the gpucall gateway and hand the generated caller-ai-onboarding-prompt.md to the caller-side AI CLI."
        )
    return (
        "All required setup checks are satisfied.\n\n"
        "Next, run:\n"
        "  gpucall launch-check --profile static"
    )


def _default_handoff_root() -> Path:
    explicit = os.getenv("GPUCALL_HANDOFF_DIR")
    if explicit:
        return Path(explicit).expanduser()
    xdg = os.getenv("XDG_DATA_HOME")
    if xdg:
        return Path(xdg).expanduser() / "gpucall" / "handoffs"
    return Path.home() / ".local" / "share" / "gpucall" / "handoffs"


def _safe_handoff_dir_name(name: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "-" for ch in name.strip())
    safe = safe.strip(".-")
    if not safe:
        raise SystemExit("external system name cannot be empty after path sanitization")
    return safe


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


def _write_json_file(path: Path, payload: dict[str, object], *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(mode)


def _provider_contracts(name: str) -> set[str]:
    contract = PROVIDER_SETUP_CONTRACTS.get(name)
    return set(contract.gpucall_credentials_required) if contract is not None else set()


def _confirm(message: str) -> bool:
    raw = input(message + " [y/N]: ").strip().lower()
    return raw in {"y", "yes"}
